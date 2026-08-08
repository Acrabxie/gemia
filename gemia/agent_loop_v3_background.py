"""Background-job persistence, reconciliation, and watcher integration."""

from __future__ import annotations

import time
from contextlib import suppress
from pathlib import Path
from typing import Any

from gemia.tools._jobs import JobRegistry
from gemia.turn_control import ClarificationGuard, TurnIntent


class AgentLoopBackgroundMixin:
    def _jobs_state_path(self) -> Path | None:
        """Where this session's background-job registry is persisted, or None
        for an ephemeral session (no sessions_root)."""
        if self.sessions_root is None:
            return None
        return self.sessions_root / self.session_id / "jobs.json"

    def persist_jobs(self) -> None:
        """Best-effort snapshot of the job registry to <sid>/jobs.json.

        Bookkeeping, not correctness-critical — a failed write must never surface
        to a tool call or the watcher, so every error is swallowed. Called on
        submit, after a watcher poll finalizes a job, and after a kill; the
        persisted pid/pgid/started_epoch let a restart reconcile mid-flight jobs.
        """
        path = self._jobs_state_path()
        if path is None:
            return
        with suppress(Exception):
            self._tool_ctx.jobs.save(path)

    def _load_and_reconcile_jobs(self) -> None:
        """Restore background jobs from <sid>/jobs.json after a restart.

        Provider LROs are durable remote jobs, so they are restored unchanged
        and remain pollable through ``check_job``.  Shell jobs cannot safely be
        reattached to a vanished Python process handle: an already-terminal
        shell job is re-injected as-is (and re-queues its
        completion notice if the model never got to see it); a non-terminal job
        is reconciled to an honest failed state via reconcile_orphan_shell_job
        (which NEVER kills). Every restored job is marked finalized in the
        watcher bookkeeping so the live watcher won't double-process it, and the
        reconciled states are persisted back immediately.
        """
        path = self._jobs_state_path()
        if path is None or not path.exists():
            return
        try:
            saved = JobRegistry.load(path)
        except Exception:
            return

        from gemia.tools.build import (
            reconcile_orphan_shell_job,
            shell_job_output_tail,
        )

        changed = False
        for record in saved.list_records():
            # A fresh session's registry is empty; skip a duplicate defensively.
            try:
                self._tool_ctx.jobs.get(record.job_id)
                continue
            except KeyError:
                pass
            self._tool_ctx.jobs._records[record.job_id] = record  # noqa: SLF001

            if record.kind != "shell":
                # Remote provider operations outlive this process.  Preserve
                # the operation, request and budget reservation bindings; the
                # normal check_job path will reconcile/settle them later.
                continue

            if record.last_polled_status in ("done", "failed"):
                if not record.announced:
                    elapsed = (
                        max(0.0, time.time() - record.started_epoch)
                        if record.started_epoch is not None
                        else None
                    )
                    self.queue_background_notification(
                        {
                            "job_id": record.job_id,
                            "status": record.last_polled_status,
                            "exit_code": None,
                            "summary": record.summary,
                            "error": record.final_error,
                            "elapsed_sec": elapsed,
                            "output_tail": shell_job_output_tail(record, self._tool_ctx),
                        }
                    )
                    record.announced = True
                    changed = True
            else:
                notice = reconcile_orphan_shell_job(record, self._tool_ctx)
                self.queue_background_notification(notice)
                record.announced = True
                changed = True

            # Restored jobs are terminal now: keep the live watcher from
            # re-emitting/re-committing/re-announcing them.
            self._bg_finalized.add(record.job_id)
            self._bg_last_emitted[record.job_id] = record.last_polled_status
            self._bg_committed.add(record.job_id)

        if changed:
            self.persist_jobs()

    def queue_background_notification(self, payload: dict[str, Any]) -> None:
        """Queue a completed-job notice for injection into the conversation.

        Same-loop only (watcher coroutine): if a turn is running, the notice
        is drained at the top of its next model-call iteration; otherwise the
        session watcher triggers run_background_resume_turn.
        """
        self._bg_notifications.append(dict(payload))

    def has_pending_background_notifications(self) -> bool:
        return bool(self._bg_notifications)

    def _drain_background_notifications(self) -> str | None:
        """Render and clear queued job notices as one synthetic message body."""
        if not self._bg_notifications:
            return None
        drained, self._bg_notifications = self._bg_notifications, []
        lines = ["[background job update — host notice, not user input]"]
        for p in drained:
            head = f"- {p.get('job_id')} → {p.get('status')}"
            details = []
            if p.get("exit_code") is not None:
                details.append(f"exit {p['exit_code']}")
            if p.get("elapsed_sec") is not None:
                details.append(f"took {p['elapsed_sec']:.0f}s")
            if details:
                head += f" ({', '.join(details)})"
            lines.append(head)
            if p.get("summary"):
                lines.append(f"  command: {p['summary']}")
            if p.get("error"):
                lines.append(f"  error: {p['error']}")
            tail = str(p.get("output_tail") or "").strip()
            if tail:
                lines.append("  output tail:")
                lines.extend(f"    {ln}" for ln in tail.splitlines()[-15:])
        lines.append(
            "If you were waiting on this job, continue that work now "
            "(check_job gives the full log); otherwise ignore this notice."
        )
        return "\n".join(lines)

    async def run_background_resume_turn(self) -> bool:
        """Run one model turn triggered by background-job completion, with no
        user input. Returns False without running when the queue is already
        empty (e.g. a concurrent turn drained it first).

        Deliberately does NOT touch _pinned_intent — a host notice is not the
        session goal — and reuses the normal turn bookkeeping otherwise.
        """
        note = self._drain_background_notifications()
        if note is None:
            return False
        self._messages.append({"role": "user", "content": note})
        self._last_user_message = note
        self._trim_rolling_window()
        # Mirror run_turn's per-turn setup. ACTIONABLE here only selects the
        # streamed-prose presentation path; it does not change tool access.
        self._tool_ctx.extra["clarification_guard"] = ClarificationGuard()
        try:
            await self._drive_turn(note, TurnIntent.ACTIONABLE)
        finally:
            self._clear_turn_guidance()
            self._turn_count += 1
            if self.sessions_root is not None and self._manage_session_meta:
                self._write_session_meta(turn_count=self._turn_count)
        return True

    # Cap the model-facing output tail carried in a completion notice; the
    # model can check_job for the full log if it needs more.
    _BG_NOTIFY_TAIL_CHARS = 2000
    # Cap the tail carried in the SSE background_task_update payload.
    _BG_SSE_TAIL_CHARS = 1000
    # A job that failed this fast is almost certainly a typo/immediate error;
    # back the auto-resume off harder so a broken command can't storm-wake.
    _BG_FAST_FAIL_SEC = 3.0

    def poll_background_jobs(self) -> dict[str, Any]:
        """Poll every pending background shell job once (called by the session
        watcher on the loop thread).

        Side effects: advances each job's registry state via _check_job_impl,
        emits a background_task_update SSE on status change, budget-commits the
        real wall-clock once per job at completion, and queues a completion
        notice for any newly-terminal job the model has not already seen.

        Returns {pending, newly_terminal, had_fast_fail} for the watcher's
        scheduling decisions.
        """
        from gemia.tools.build import _PROCESSES, _check_job_impl

        ctx = self._tool_ctx
        pending = 0
        newly_terminal: list[str] = []
        terminal_seen: list[str] = []
        had_fast_fail = False

        for record in list(ctx.jobs.list_records()):
            if record.kind != "shell" or record.job_id in self._bg_finalized:
                continue
            try:
                result = _check_job_impl(record.job_id, ctx, mark_announced=False)
            except Exception:
                # A transient poll error (e.g. a slow SIGKILL reap raising
                # TimeoutExpired) must not drop a still-live job from the pending
                # count — that could make the watcher exit and permanently strand
                # it. Keep it counted while its process is still tracked; the next
                # tick retries and self-heals.
                if record.job_id in _PROCESSES:
                    pending += 1
                continue
            status = str(result.get("status") or record.last_polled_status)
            if status not in ("done", "failed"):
                pending += 1

            elapsed = None
            if record.started_epoch is not None:
                elapsed = max(0.0, time.time() - record.started_epoch)

            if self._bg_last_emitted.get(record.job_id) != status:
                self._bg_last_emitted[record.job_id] = status
                tail = str(result.get("stdout_tail") or "")[-self._BG_SSE_TAIL_CHARS :]
                payload = {
                    "job_id": record.job_id,
                    "status": status,
                    "exit_code": result.get("exit_code"),
                    "summary": record.summary,
                    "output_tail": tail,
                }
                if elapsed is not None:
                    payload["elapsed_sec"] = round(elapsed, 1)
                self.emit_background_update(payload)

            if status in ("done", "failed"):
                # Persist the terminal state even when the model already saw it
                # via a check_job/wait_for_job (which sets announced=True WITHOUT
                # persisting). Otherwise jobs.json stays at the submit-time
                # non-terminal snapshot and a restart would reconcile a finished
                # job into a false "failed" with a bogus completion notice.
                terminal_seen.append(record.job_id)
                if record.job_id not in self._bg_committed:
                    self._bg_committed.add(record.job_id)
                    self.budget.commit(
                        "run_shell",
                        actual_seconds=elapsed if elapsed is not None else 0.0,
                    )
                if not record.announced:
                    record.announced = True
                    newly_terminal.append(record.job_id)
                    if (
                        status == "failed"
                        and elapsed is not None
                        and elapsed < self._BG_FAST_FAIL_SEC
                    ):
                        had_fast_fail = True
                    self.queue_background_notification(
                        {
                            "job_id": record.job_id,
                            "status": status,
                            "exit_code": result.get("exit_code"),
                            "summary": record.summary,
                            "error": result.get("error") or record.final_error,
                            "elapsed_sec": elapsed,
                            "output_tail": str(result.get("stdout_tail") or "")[
                                -self._BG_NOTIFY_TAIL_CHARS :
                            ],
                        }
                    )
                self._bg_finalized.add(record.job_id)

        # Persist durable transitions (submitted/running → done/failed) so a
        # restart reconciles from the true terminal state. terminal_seen covers
        # jobs the model already announced too, closing the resurrect-as-failed
        # gap; running-only ticks aren't persisted (reconcile treats a stale
        # 'running' the same anyway).
        if terminal_seen:
            self.persist_jobs()

        return {
            "pending": pending,
            "newly_terminal": newly_terminal,
            "had_fast_fail": had_fast_fail,
        }


__all__ = ["AgentLoopBackgroundMixin"]
