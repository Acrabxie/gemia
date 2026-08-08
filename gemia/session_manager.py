"""SessionManager + SessionRunner for the Lumeri v3 HTTP API.

Each session owns:
  - one ``AgentLoopV3`` instance (state: messages, registry, budget)
  - one background thread running a dedicated ``asyncio`` event loop
  - one SSE queue + replay buffer registered in
    ``gemia.transport.sse.REGISTRY`` under the same session_id

HTTP handler threads interact with a session by submitting coroutines
to its loop via ``asyncio.run_coroutine_threadsafe``. The loop runs
the agent and emits events to the SSE queue (which any thread can
emit to safely).

Multi-session: ``SessionManager`` holds a dict of runners. No
artificial single-session restriction. No fairness/rate-limiting in
M1 — that's a separate concern if real concurrency becomes a need.
"""
from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import os
import shutil
import signal
import threading
import time
import uuid
from concurrent.futures import TimeoutError as FuturesTimeoutError
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from gemia.agent_loop_v3 import AgentLoopV3
from gemia.production_store import (
    IdempotencyConflictError,
    ProductionNotFoundError,
    ProductionStore,
    ProductionStoreError,
    RevisionConflictError,
    default_reality_contract,
)
from gemia import project_context
from gemia.project_model import empty_project
from gemia.project_store import ProjectStore
from gemia.project_workspace import FileMutationJournal, validate_source_root
from gemia.tools._context import AssetRegistry, ProgressCallback
from gemia.transport.sse import REGISTRY as SSE_REGISTRY


_DEFAULT_OUTPUT_ROOT = Path.home() / ".gemia" / "v3"
_DEFAULT_MAX_SESSIONS = 20
# Runtime threads may sleep after this idle period. Durable creator sessions
# themselves do not expire; session routes transparently resume them.
_DEFAULT_IDLE_TIMEOUT_SEC = 2 * 60 * 60
_DEFAULT_SWEEP_INTERVAL_SEC = 60

# Background-job watcher / auto-resume tuning.
_BG_WATCH_INTERVAL_SEC = 2.0
_BG_RESUME_MAX_PER_HOUR = 12
_BG_RESUME_MIN_INTERVAL_SEC = 10.0
_BG_RESUME_FASTFAIL_INTERVAL_SEC = 30.0

_PRODUCTION_TERMINAL_STATES = frozenset({"accepted", "cancelled", "failed"})


def _autoresume_enabled() -> bool:
    """Auto-wakeup on background completion, default ON (LUMERI_BG_AUTORESUME)."""
    val = str(os.environ.get("LUMERI_BG_AUTORESUME", "1")).strip().lower()
    return val not in ("0", "false", "no", "off")


class SessionLimitError(RuntimeError):
    """Raised when the process-wide v3 session cap has been reached."""


class VerbGateError(RuntimeError):
    """A verb routed through ``SessionRunner.run_verb`` was refused by a host
    gate (membership / plan mode / budget / turn-collision / timeout) rather
    than by the dispatcher.

    Carries the structured payload the agent loop would have appended so the
    MCP layer can surface it byte-compatibly as an ``isError`` tool result.
    ``code`` is one of the frozen ``ERROR_CODES`` gate codes (``E_PLAN_MODE``,
    ``E_BUDGET``, ``E_BUSY``) or ``E_TOOL`` for an unknown/excluded verb.
    """

    def __init__(self, code: str, message: str, payload: dict[str, Any]) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.payload = payload


def _production_stage_tool_names(capabilities: Any, state: str) -> tuple[str, ...] | None:
    """Return the installed capabilities exposed by one production stage.

    ``PRODUCTION_STAGE_PACKS`` remains the stage-to-workflow authority and
    ``ToolRouter`` remains the workflow-to-tool authority.  Starting from a
    conversation-only router gives this host gate an empty surface before the
    stage packs are activated, so prompt keywords cannot widen a formal
    operator call.  Intersecting with the compiled registry then guarantees
    that only installed, schema/dispatcher-validated capabilities are allowed.

    ``None`` means the persisted state has no declared capability surface and
    must fail closed.  Terminal states are handled separately by the caller.
    """

    from gemia.tool_router import PRODUCTION_STAGE_PACKS, ToolRouter

    packs = PRODUCTION_STAGE_PACKS.get(str(state or "").strip().lower())
    if packs is None:
        return None
    router = ToolRouter("hello", enabled=True)
    for pack_name in packs:
        router.activate_pack(pack_name)
    routed = set(router.active_tool_names)
    return tuple(name for name in capabilities.names if name in routed)


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    try:
        value = int(os.environ.get(name) or default)
    except ValueError:
        return default
    return value if value >= minimum else default


def _iso_to_epoch(value: Any, default: float) -> float:
    if not isinstance(value, str) or not value:
        return default
    try:
        from datetime import datetime

        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return default


def _last_transcript_seq(path: Path) -> int:
    """Recover the highest durable seq so resumed transcripts stay monotonic."""

    if not path.exists():
        return 0
    # JSONL sequence numbers are append-only and monotonic.  Read a bounded
    # tail first so reopening a long production transcript does not replay
    # every historical event merely to find its final counter.  A truncated
    # final line is harmless: scan backwards for the preceding valid record.
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            read_size = min(size, 256 * 1024)
            fh.seek(size - read_size)
            tail = fh.read(read_size).decode("utf-8", errors="ignore")
        for line in reversed(tail.splitlines()):
            try:
                value = json.loads(line)
                seq = int(value.get("seq") or 0)
            except (json.JSONDecodeError, AttributeError, TypeError, ValueError):
                continue
            if seq > 0:
                return seq
    except OSError:
        pass
    # Corrupt/legacy logs without a valid bounded tail keep the conservative
    # full-scan fallback.
    highest = 0
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                try:
                    value = json.loads(line)
                    highest = max(highest, int(value.get("seq") or 0))
                except (json.JSONDecodeError, TypeError, ValueError):
                    continue
    except OSError:
        return 0
    return highest


def _production_budget_view(ledger: Any) -> dict[str, Any]:
    """Normalize the canonical micro-USD ledger for REST/SSE consumers.

    ``ProductionMediaBudget`` remains the only authority.  This helper only
    derives display fields from its persisted document; it never approves or
    mutates a provider call.
    """

    snapshot = dict(ledger.snapshot())
    # Filesystem topology is not part of the public budget contract.  The
    # canonical ledger object retains it internally; REST/SSE views never do.
    snapshot.pop("ledger_path", None)
    limit = float(snapshot.get("cap_usd") or 0.0)
    # ProductionMediaBudget.snapshot computes this split under the same ledger
    # lock as the hard-cap view.  Do not reread the JSON after releasing that
    # lock: besides doubling I/O, it could combine two different revisions.
    spent = float(snapshot.get("spent_usd", snapshot.get("committed_usd") or 0.0))
    reserved = float(snapshot.get("reserved_usd") or 0.0)
    return {
        **snapshot,
        "limit_usd": limit,
        "spent_usd": round(spent, 6),
        "reserved_usd": round(reserved, 6),
        "remaining_usd": round(max(0.0, limit - spent - reserved), 6),
        "over_cap": spent + reserved > limit + 1e-9,
    }


class SessionRunner:
    """Owns one AgentLoopV3 inside a dedicated thread + asyncio loop."""

    def __init__(
        self,
        *,
        session_id: str,
        output_dir: Path,
        sessions_root: Path,
        account_id: str | None = None,
        remote: bool = False,
        production_store: ProductionStore | None = None,
        project_root: Path | None = None,
        project_id: str | None = None,
        run_id: str | None = None,
        runtime_state: dict[str, Any] | None = None,
        asset_registry: AssetRegistry | None = None,
        session_meta: dict[str, Any] | None = None,
        budget_max_usd: float = 5.0,
        budget_max_seconds: float | None = 600.0,
    ) -> None:
        self.session_id = session_id
        self.output_dir = Path(output_dir)
        self.sessions_root = Path(sessions_root)
        self.account_id = str(account_id or "").strip()
        # Remote = a public, passcode-gated visitor session; host-dangerous
        # tools are stripped for it (see agent_loop_v3._REMOTE_DENY_TOOLS).
        self.remote = bool(remote)
        self.production_store = production_store
        self.project_root = Path(project_root) if project_root is not None else None
        self.project_id = str(project_id or session_id)
        self.run_id = str(run_id or session_id)
        self._runtime_state = dict(runtime_state or {})
        self._asset_registry = asset_registry
        self._budget_max_usd = float(budget_max_usd)
        self._budget_max_seconds = (
            float(budget_max_seconds) if budget_max_seconds is not None else None
        )
        self._active_client_turn_id: str | None = None
        self._persistence_error: str | None = None
        self._runtime_persist_generation = 0
        self._last_emitted_project_revision = int(
            self._runtime_state.get("project_revision") or 0
        )
        raw_receipts = self._runtime_state.get("capability_receipts")
        self._capability_receipts: dict[str, dict[str, Any]] = (
            dict(raw_receipts) if isinstance(raw_receipts, dict) else {}
        )
        self._capability_receipts_lock = threading.Lock()

        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._state_lock = threading.Lock()
        self._turn_in_progress = False
        self._turn_future = None
        now = time.time()
        self.created_at = _iso_to_epoch((session_meta or {}).get("created_at"), now)
        self.last_used_at = _iso_to_epoch((session_meta or {}).get("updated_at"), now)

        # Background-job watcher: single lazily-started task on this session's
        # loop; auto-resume rate-limit bookkeeping guarded by _state_lock.
        self._bg_watcher_task: asyncio.Task | None = None
        self._bg_resume_times: list[float] = []
        self._bg_last_resume_at = 0.0

        # Durable transcript: every event the agent emits is appended to
        # <sessions_root>/<sid>/transcript.jsonl BEFORE it reaches the SSE
        # ring buffer (which holds only 200 events and dies with the process).
        # This is the resync source for late-attaching clients and the only
        # record that survives a server restart. Per-connection synthetic
        # frames (protocol_hello, replay_gap) are emitted by the transport,
        # not the agent, so they never pollute the transcript.
        self._transcript_lock = threading.Lock()
        self._transcript_seq = 0
        self._transcript_file = None
        self._transcript_failed = False
        self._transcript_path = (
            self.sessions_root / self.session_id / "transcript.jsonl"
        )
        self._transcript_seq = _last_transcript_seq(self._transcript_path)
        if self._asset_registry is not None and self.production_store is not None:
            self._asset_registry.set_on_change(self._on_asset_registry_change)
        self._thread = threading.Thread(
            target=self._run_loop,
            name=f"lumeri-v3-{session_id}",
            daemon=True,
        )
        self._thread.start()
        self._ready.wait()

        fut = asyncio.run_coroutine_threadsafe(self._create_agent(), self._loop)
        self.agent: AgentLoopV3 = fut.result(timeout=20)
        # Construction/resume is a read-path operation.  The persisted
        # project revision is already authoritative here; re-hashing the
        # complete design program can involve large toolchains and render
        # outputs and must not block session navigation.  Mutating production
        # paths still call _sync_project_revision() with verification enabled.
        revision = self._sync_project_revision(verify_design_program=False)
        self._persist_runtime_state(project_revision=revision)

    def touch(self) -> None:
        with self._state_lock:
            self.last_used_at = time.time()

    @property
    def turn_in_progress(self) -> bool:
        with self._state_lock:
            return self._turn_in_progress

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._ready.set()
        try:
            self._loop.run_forever()
        finally:
            try:
                pending = asyncio.all_tasks(self._loop)
                for t in pending:
                    t.cancel()
            except Exception:
                pass
            self._loop.close()

    async def _create_agent(self) -> AgentLoopV3:
        extra: dict[str, Any] = {
            "on_background_job": self._on_background_job,
            "execute_registered_capability": self._execute_registered_capability,
            # AgentLoopV3 invokes this immediately after accepting a real user
            # message, before the first model request.  A daemon restart must
            # recover the same conversation/retract anchor instead of the
            # previously completed turn.
            "persist_runtime_checkpoint": self._persist_turn_start_checkpoint,
        }
        production_media_budget = None
        if self.account_id:
            extra["account_id"] = self.account_id
        if self.remote:
            extra["remote"] = True
        if self.production_store is not None:
            production_media_budget = self.production_store.media_budget(
                self.project_id, self.run_id
            )
            project_record = self.production_store.load_project(self.project_id)
            extra.update(
                {
                    "project_id": self.project_id,
                    "run_id": self.run_id,
                    "production_store": self.production_store,
                    "transition_production": self._transition_production,
                    "project_source_root": str(project_record.get("source_root") or ""),
                    "project_edit_root": str(
                        project_record.get("edit_root")
                        or (self.production_store.project_dir(self.project_id) / "design")
                    ),
                }
            )
        return AgentLoopV3(
            session_id=self.session_id,
            output_dir=self.output_dir,
            sessions_root=self.sessions_root,
            emit_event=self._emit_event,
            # extra is never empty now (on_background_job is always present).
            extra=extra,
            project_root=self.project_root,
            project_id=self.project_id,
            asset_registry=self._asset_registry,
            runtime_state=self._runtime_state,
            manage_session_meta=self.production_store is None,
            budget_max_usd=self._budget_max_usd,
            budget_max_seconds=self._budget_max_seconds,
            production_media_budget=production_media_budget,
        )

    def _persist_turn_start_checkpoint(self) -> None:
        self._persist_runtime_state(project_revision=self.cached_project_revision)

    def _transition_production(
        self, state: str, *, trace_id: str | None = None
    ) -> dict[str, Any]:
        """Transition this run and publish the persisted fact on its SSE log."""

        if self.production_store is None:
            raise RuntimeError("production state is unavailable for a legacy session")
        run = self.production_store.transition_run(
            self.project_id,
            self.run_id,
            state,
            trace_id=trace_id,
        )
        delivery = self.production_store.public_delivery(
            self.project_id,
            self.run_id,
            run=run,
        )
        self._emit_event(
            {
                "kind": "production_state_changed",
                "project_id": self.project_id,
                "run_id": self.run_id,
                "production_state": run.get("state"),
                "production_revision": int(run.get("revision") or 0),
                "blockers": list(run.get("blockers") or []),
                "delivery": delivery,
                "trace_id": str(trace_id or ""),
            }
        )
        if str(run.get("state") or "") == "ready_for_review":
            self._emit_event(
                {
                    "kind": "delivery_ready",
                    "project_id": self.project_id,
                    "run_id": self.run_id,
                    "project_revision": int(run.get("project_revision") or 0),
                    "production_revision": int(run.get("revision") or 0),
                    "delivery": delivery,
                }
            )
        return run

    # ── background-job watcher + auto-resume ─────────────────────────

    def _on_background_job(self, job_id: str) -> None:
        """Callback fired (on the session loop) when run_shell submits a
        background job — starts the watcher and snapshots the registry so the
        job's pid/pgid survive a crash before the first watcher poll."""
        self._ensure_bg_watcher()
        try:
            self.agent.persist_jobs()
        except Exception:
            pass

    def _ensure_bg_watcher(self) -> None:
        """Idempotently start the watcher task on this session's loop.

        Must run on the loop thread (it is: the only callers are the
        on_background_job tool callback and a resume turn, both on-loop).
        """
        if self._bg_watcher_task is not None and not self._bg_watcher_task.done():
            return
        self._bg_watcher_task = asyncio.ensure_future(self._bg_watch())

    async def _bg_watch(self) -> None:
        """Poll pending background shell jobs until none remain.

        On each tick: advance job state + emit SSE + queue completion notices
        (all inside agent.poll_background_jobs), then auto-resume an idle
        session to process queued notices. Exits when there is nothing left to
        watch, so it stays dormant between bursts of background work.
        """
        try:
            while True:
                try:
                    summary = self.agent.poll_background_jobs()
                except Exception:
                    summary = {"pending": 0, "had_fast_fail": False}
                pending = int(summary.get("pending", 0) or 0)

                if self.agent.has_pending_background_notifications() and not self.turn_in_progress:
                    self._auto_resume(bool(summary.get("had_fast_fail")))

                if pending == 0:
                    if not self.agent.has_pending_background_notifications():
                        return  # nothing pending, nothing queued → stop watching
                    # Notices are queued. A turn in progress MIGHT drain them at
                    # its next top-of-loop, but a turn that ends on a no-tool
                    # response never loops back to drain — so we must KEEP
                    # watching while a turn runs and auto-resume once it ends
                    # idle. Give up only when the session is already idle AND we
                    # cannot auto-resume (disabled/capped); the next user turn
                    # will drain them then.
                    if not self.turn_in_progress and (
                        not _autoresume_enabled() or self._resume_capped()
                    ):
                        return

                await asyncio.sleep(_BG_WATCH_INTERVAL_SEC)
        except asyncio.CancelledError:
            raise
        except Exception:
            return

    def _resume_capped(self) -> bool:
        now = time.time()
        with self._state_lock:
            self._bg_resume_times = [t for t in self._bg_resume_times if now - t < 3600.0]
            return len(self._bg_resume_times) >= _BG_RESUME_MAX_PER_HOUR

    def _auto_resume(self, fast_fail: bool) -> None:
        """If the session is idle and within rate limits, CAS-acquire the turn
        flag and schedule a background-resume turn on the loop.

        Same _state_lock as submit_turn, so a concurrent user turn and an
        auto-resume can never both start — the loser just no-ops.
        """
        if not _autoresume_enabled():
            return
        now = time.time()
        with self._state_lock:
            if self._turn_in_progress:
                return
            self._bg_resume_times = [t for t in self._bg_resume_times if now - t < 3600.0]
            if len(self._bg_resume_times) >= _BG_RESUME_MAX_PER_HOUR:
                return
            min_interval = (
                _BG_RESUME_FASTFAIL_INTERVAL_SEC if fast_fail else _BG_RESUME_MIN_INTERVAL_SEC
            )
            if self._bg_last_resume_at and (now - self._bg_last_resume_at) < min_interval:
                return
            self._turn_in_progress = True
            self._bg_last_resume_at = now
            self._bg_resume_times.append(now)
            self.last_used_at = now

        async def _run() -> None:
            try:
                await self.agent.run_background_resume_turn()
            finally:
                with self._state_lock:
                    self._turn_in_progress = False
                    self.last_used_at = time.time()

        asyncio.ensure_future(_run())

    def _sweep_background_jobs(self) -> None:
        """Kill any still-running background shell jobs owned by this session
        (orphan prevention on close). SIGTERM → brief grace → unconditional group
        SIGKILL, keyed on the pgid persisted at spawn (getpgid on a dead leader
        would raise). Terminal states are persisted so a restart does not
        resurrect a job this close just finished."""
        try:
            from gemia.tools import build as _build

            ctx = self.agent._tool_ctx  # noqa: SLF001 — same-package plumbing
            for record in list(ctx.jobs.list_records()):
                if record.kind != "shell" or record.last_polled_status in ("done", "failed"):
                    continue
                entry = _build._PROCESSES.get(record.job_id)  # noqa: SLF001
                proc = entry[0] if entry is not None else None
                # If we hold a handle and the process already exited (e.g. reaped
                # by a cap-count poll), it is gone — never killpg its pgid, which
                # the OS may have recycled onto an unrelated process group.
                if proc is not None and proc.poll() is not None:
                    _build._PROCESSES.pop(record.job_id, None)  # noqa: SLF001
                    ctx.jobs.update_from_poll(record.job_id, "failed", error="session closed")
                    continue
                pgid = record.pgid or (proc.pid if proc is not None else None)
                if pgid is None:
                    continue
                try:
                    os.killpg(pgid, signal.SIGTERM)
                except OSError:
                    pass
                if proc is not None:
                    try:
                        proc.wait(timeout=2)  # brief grace for a clean SIGTERM exit
                    except Exception:
                        pass
                # Escalate to the whole group unconditionally: a SIGTERM-ignoring
                # grandchild can outlive a direct child that exited on SIGTERM, so
                # gating SIGKILL on the direct child still-alive would leak it.
                try:
                    os.killpg(pgid, signal.SIGKILL)
                except OSError:
                    pass
                if proc is not None:
                    try:
                        proc.wait(timeout=1)
                    except Exception:
                        pass
                _build._PROCESSES.pop(record.job_id, None)  # noqa: SLF001
                ctx.jobs.update_from_poll(
                    record.job_id, "failed", error="session closed"
                )
            self.agent.persist_jobs()  # flush terminal states for restart reconcile
        except Exception:
            pass

    def _emit_event(self, event: dict[str, Any]) -> None:
        """Agent event sink: durable facts, transcript, then SSE fan-out.

        Production handling may enrich the same event (normalized budget,
        revision-bound turn outcome) and therefore runs before the transcript;
        failures are captured but never suppress transcript/SSE delivery.
        """
        if getattr(self, "production_store", None) is not None:
            try:
                self._handle_durable_event(event)
            except Exception as exc:  # persistence failure is surfaced in snapshots
                self._persistence_error = f"{type(exc).__name__}: {exc}"
        if not self._transcript_failed:
            try:
                with self._transcript_lock:
                    if self._transcript_file is None:
                        self._transcript_path.parent.mkdir(parents=True, exist_ok=True)
                        self._transcript_file = open(  # noqa: SIM115 — long-lived handle
                            self._transcript_path, "a", encoding="utf-8"
                        )
                    self._transcript_seq += 1
                    line = json.dumps(
                        {
                            "seq": self._transcript_seq,
                            "ts": time.time(),
                            "event": event,
                        },
                        ensure_ascii=False,
                        default=str,
                    )
                    self._transcript_file.write(line + "\n")
                    self._transcript_file.flush()
            except Exception:
                self._transcript_failed = True
        SSE_REGISTRY.emit(self.session_id, event)

    def _on_asset_registry_change(self, registry: AssetRegistry) -> None:
        if self.production_store is None:
            return
        registry.save(self.production_store.asset_registry_path(self.project_id))
        if hasattr(self, "agent"):
            revision = self._sync_project_revision()
            self._persist_runtime_state(project_revision=revision)

    def _sync_project_revision(self, *, verify_design_program: bool = True) -> int:
        if getattr(self, "production_store", None) is None or not hasattr(self, "agent"):
            return 0
        if hasattr(self.agent, "project"):
            project_store = self.agent.project.store
            durable_project_id = self.agent.project.project_id
        elif self.project_root is not None:
            # Small test/integration doubles may not expose ProjectHandle.  The
            # durable source of truth is the ProjectStore, not the loop object.
            project_store = ProjectStore(self.project_root)
            durable_project_id = self.project_id
        else:
            return 0
        # ``normalize_project`` refreshes its returned ``updated_at`` on every
        # read.  Hash the atomic on-disk snapshot instead, otherwise a GET,
        # close or resume falsely advances revision and invalidates receipts.
        state = json.loads(
            project_store.state_path(durable_project_id).read_text(encoding="utf-8")
        )
        meta = project_store.load_meta(durable_project_id)
        registry = (
            self.agent.registry
            if hasattr(self.agent, "registry")
            else (self._asset_registry or AssetRegistry())
        )
        # Project revision represents the editable canonical graph and the
        # exact inputs it references. Allocation counters and derived preview /
        # export assets are runtime facts; including them would make a receipt
        # stale the instant its output was registered.
        referenced_asset_ids = {
            str(asset.get("id") or asset.get("asset_id") or "")
            for asset in (state.get("assets") or [])
            if isinstance(asset, dict)
        }
        timeline = state.get("timeline") if isinstance(state.get("timeline"), dict) else {}
        referenced_asset_ids.update(
            str(clip.get("asset_id") or "")
            for clip in (timeline.get("clips") or [])
            if isinstance(clip, dict) and clip.get("asset_id")
        )
        registry_payload = {
            "records": [
                record.to_dict()
                for record in sorted(
                    registry.list_records(), key=lambda item: item.asset_id
                )
                if record.asset_id in referenced_asset_ids
            ]
        }
        encoded = json.dumps(
            {"project": state, "assets": registry_payload},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
        record = self.production_store.observe_project_state(
            self.project_id,
            state_hash=hashlib.sha256(encoded).hexdigest(),
            timeline_patch_seq=int(meta.get("patch_seq") or 0),
        )
        # Project code is part of the editable canonical graph even when a
        # human changes it outside the write_file verb. Observe the persistent
        # tree on every revision sync so a build can never consume unversioned
        # algorithm bytes or reuse stale render evidence.
        if verify_design_program:
            design_root = self.production_store.project_dir(self.project_id) / "design"
            has_design_files = design_root.exists() and any(
                path.is_file() and not path.is_symlink()
                for path in design_root.rglob("*")
            )
            if has_design_files or bool(record.get("design_program_hash")):
                record = self.production_store.observe_design_program(
                    self.project_id,
                    self.run_id,
                    design_root=design_root,
                )
        revision = int(record.get("revision") or 0)
        before_run = self.production_store.load_run(self.project_id, self.run_id)
        synced_run = self.production_store.sync_run_project_revision(
            self.project_id, self.run_id, revision
        )
        synced_delivery = self.production_store.public_delivery(
            self.project_id,
            self.run_id,
            run=synced_run,
        )
        if str(synced_run.get("state") or "") != str(before_run.get("state") or ""):
            self._emit_event(
                {
                    "kind": "production_state_changed",
                    "project_id": self.project_id,
                    "run_id": self.run_id,
                    "production_state": synced_run.get("state"),
                    "production_revision": int(synced_run.get("revision") or 0),
                    "delivery": synced_delivery,
                    "reason": "project_revision_invalidated_acceptance",
                }
            )
        if revision > self._last_emitted_project_revision:
            self._last_emitted_project_revision = revision
            self._emit_event(
                {
                    "kind": "project_revision_committed",
                    "project_id": self.project_id,
                    "run_id": self.run_id,
                    "project_revision": revision,
                    "timeline_patch_seq": int(meta.get("patch_seq") or 0),
                    "delivery": synced_delivery,
                }
            )
        return revision

    def _persist_runtime_state(self, *, project_revision: int | None = None) -> None:
        if getattr(self, "production_store", None) is None or not hasattr(self, "agent"):
            return
        revision = (
            int(project_revision)
            if project_revision is not None
            else self._sync_project_revision()
        )
        state = (
            self.agent.snapshot_runtime_state()
            if hasattr(self.agent, "snapshot_runtime_state")
            else {}
        )
        state.update(
            {
                "project_id": self.project_id,
                "run_id": self.run_id,
                "project_revision": revision,
                "active_client_turn_id": self._active_client_turn_id,
                "capability_receipts": dict(self._capability_receipts),
            }
        )
        self.production_store.save_runtime_state(self.session_id, state)
        self.production_store.update_session(
            self.session_id,
            {
                "turn_count": int(state.get("turn_count") or 0),
                "plan_mode": bool(state.get("plan_mode", False)),
                "active_client_turn_id": self._active_client_turn_id,
            },
        )
        # Only a fully completed runtime + session-meta commit advances this
        # generation.  Direct edits use it to coalesce the synchronous
        # ``timeline_op`` persistence with their post-mutation fallback.
        self._runtime_persist_generation = int(
            getattr(self, "_runtime_persist_generation", 0)
        ) + 1

    def _handle_durable_event(self, event: dict[str, Any]) -> None:
        kind = str(event.get("kind") or "")
        if kind == "budget_updated":
            canonical_host_view = (
                event.get("origin") == "production_host"
                and isinstance(event.get("budget"), dict)
            )
            if not canonical_host_view:
                self.production_store.refresh_budget_summary(self.project_id, self.run_id)
                event["budget"] = _production_budget_view(
                    self.production_store.media_budget(self.project_id, self.run_id)
                )
        if kind in {
            "timeline_op",
            "tool_exec_result",
            "tool_exec_error",
            "background_task_update",
            "plan_mode_changed",
            "budget_updated",
        }:
            if kind in {"tool_exec_result", "background_task_update"}:
                try:
                    self.agent.persist_jobs()
                except Exception:
                    pass
            self._persist_runtime_state()
        if kind not in {"turn_complete", "turn_cancelled"}:
            return
        with self._state_lock:
            client_turn_id = self._active_client_turn_id
        if client_turn_id:
            revision = self._sync_project_revision()
            run = self.production_store.load_run(self.project_id, self.run_id)
            if kind == "turn_cancelled":
                status, outcome = "cancelled", "no_change"
            else:
                status = "completed"
                outcome = str(event.get("outcome") or "progressed")
                if outcome not in {"progressed", "ready_for_review", "blocked", "no_change"}:
                    outcome = "progressed"
                if str(run.get("state") or "") == "ready_for_review":
                    outcome = "ready_for_review"
                elif str(run.get("state") or "") in {"blocked", "failed", "cancelled"}:
                    outcome = "blocked"
            event.update(
                {
                    "outcome": outcome,
                    "project_id": self.project_id,
                    "run_id": self.run_id,
                    "project_revision": revision,
                    "production_state": str(run.get("state") or "created"),
                }
            )
            self.production_store.complete_turn(
                self.project_id,
                self.run_id,
                client_turn_id,
                status=status,
                outcome=outcome,
                project_revision=revision,
            )
            with self._state_lock:
                if self._active_client_turn_id == client_turn_id:
                    self._active_client_turn_id = None
        self._persist_runtime_state()

    def add_external_asset(self, path: Path, *, summary: str = "") -> str:
        self.touch()

        async def _add() -> str:
            return self.agent.add_external_asset(Path(path), summary=summary)

        fut = asyncio.run_coroutine_threadsafe(_add(), self._loop)
        return fut.result(timeout=30)

    def submit_turn(self, message: str) -> bool:
        """Fire-and-forget if no turn is active.

        Returns ``True`` when the turn was scheduled, or ``False`` when the
        session already has a turn running. The frontend disables the send
        button, but the HTTP layer needs this guard for direct/concurrent
        callers too.
        """

        return bool(self.submit_turn_request(message).get("scheduled"))

    def submit_turn_request(
        self,
        message: str,
        *,
        client_turn_id: str | None = None,
        expected_project_revision: int | None = None,
    ) -> dict[str, Any]:
        """Durably claim then schedule one turn.

        Replaying the same ``client_turn_id`` returns the stored status without
        scheduling a second model/provider execution.  Legacy callers use
        :meth:`submit_turn` and keep the original bool contract.
        """

        text = str(message or "").strip()
        if not text:
            raise ValueError("message must be non-empty")
        with self._state_lock:
            if self._turn_in_progress:
                return {
                    "accepted": False,
                    "scheduled": False,
                    "duplicate": False,
                    "code": "E_BUSY",
                    "session_id": self.session_id,
                    "project_id": self.project_id,
                    "run_id": self.run_id,
                }
            self._turn_in_progress = True
            self.last_used_at = time.time()

        turn_id = str(client_turn_id or f"turn-{uuid.uuid4().hex}")
        project_revision = self._sync_project_revision()
        if self.production_store is not None:
            try:
                claim = self.production_store.claim_turn(
                    self.project_id,
                    self.run_id,
                    session_id=self.session_id,
                    client_turn_id=turn_id,
                    message=text,
                    project_revision=project_revision,
                    expected_project_revision=expected_project_revision,
                )
            except Exception:
                with self._state_lock:
                    self._turn_in_progress = False
                raise
            if claim.get("duplicate"):
                with self._state_lock:
                    self._turn_in_progress = False
                return {
                    "accepted": True,
                    "scheduled": False,
                    "duplicate": True,
                    "client_turn_id": turn_id,
                    "turn_status": claim.get("status"),
                    "session_id": self.session_id,
                    "project_id": self.project_id,
                    "run_id": self.run_id,
                    "project_revision": int(claim.get("project_revision") or 0),
                }
        with self._state_lock:
            self._active_client_turn_id = turn_id
        self._persist_runtime_state()

        async def _run() -> None:
            tool_ctx = getattr(self.agent, "_tool_ctx", None)
            tool_extra = getattr(tool_ctx, "extra", None)
            try:
                if isinstance(tool_extra, dict):
                    tool_extra["active_trace_id"] = turn_id
                await self.agent.run_turn(text)
            except asyncio.CancelledError:
                self._emit_event({
                    "kind": "turn_cancelled",
                })
                raise
            except Exception:
                if self.production_store is not None:
                    with self._state_lock:
                        active_id = self._active_client_turn_id
                    if active_id:
                        try:
                            self.production_store.complete_turn(
                                self.project_id,
                                self.run_id,
                                active_id,
                                status="failed",
                                outcome="blocked",
                                project_revision=self._sync_project_revision(),
                            )
                        finally:
                            with self._state_lock:
                                self._active_client_turn_id = None
                raise
            finally:
                if isinstance(tool_extra, dict):
                    tool_extra.pop("active_trace_id", None)
                with self._state_lock:
                    self._turn_in_progress = False
                    self._turn_future = None
                    self.last_used_at = time.time()
                self._persist_runtime_state()

        future = asyncio.run_coroutine_threadsafe(_run(), self._loop)
        with self._state_lock:
            if self._turn_in_progress:
                self._turn_future = future
        return {
            "accepted": True,
            "scheduled": True,
            "duplicate": False,
            "client_turn_id": turn_id,
            "turn_status": "accepted",
            "session_id": self.session_id,
            "project_id": self.project_id,
            "run_id": self.run_id,
            "project_revision": project_revision,
        }

    def steer_turn(self, guidance: str) -> bool:
        """Queue guidance for an active turn without starting a second turn."""
        text = str(guidance or "").strip()
        if not text:
            return False
        with self._state_lock:
            future = self._turn_future
            active = self._turn_in_progress and future is not None and not future.done()
            if active:
                self.last_used_at = time.time()
        if not active:
            return False
        self.agent.queue_turn_guidance(text)
        self._emit_event({"kind": "turn_guidance_queued", "guidance": text})
        return True

    def stop_turn(self) -> bool:
        """Request cancellation of the active turn, preserving completed work."""
        with self._state_lock:
            future = self._turn_future
            active = self._turn_in_progress and future is not None and not future.done()
            if active:
                self.last_used_at = time.time()
        if not active:
            return False
        return bool(future.cancel())

    def retract_turn(self, expected_message: str | None = None) -> dict[str, Any]:
        """Remove the last completed user turn from the agent conversation.

        Refuses while a turn is running (the user must stop it first) and hops
        onto the session loop because ``_messages`` is only ever mutated there.
        ``expected_message`` is the caller's view of the turn being retracted;
        a mismatch (stale UI, snapshot replay) refuses rather than deleting
        the wrong turn.
        """
        with self._state_lock:
            if self._turn_in_progress:
                return {"ok": False, "reason": "turn_in_progress"}
            self.last_used_at = time.time()
        if self._loop.is_closed():
            raise RuntimeError("session is closed")

        async def _call() -> str | None:
            return self.agent.retract_last_turn(expected_message)

        fut = asyncio.run_coroutine_threadsafe(_call(), self._loop)
        retracted = fut.result(timeout=10)
        if retracted is None:
            return {"ok": False, "reason": "nothing_to_retract"}
        return {"ok": True, "message": retracted}

    def run_project_edit(
        self,
        fn,
        *,
        expected_project_revision: int | None = None,
        timeout: float | None = None,
    ) -> Any:
        """Run a project mutation on the session's event loop and return its
        result (exceptions propagate unchanged).

        /timeline/op and undo used to mutate ProjectStore straight from HTTP
        handler threads while agent verbs mutated it from this loop — the
        per-project lock makes that data-safe, but hopping user edits onto the
        loop also keeps their ``timeline_op`` SSE emits ordered with the
        in-flight turn's event stream (a user edit can no longer interleave
        inside one verb's start/result pair). User edits execute at the turn's
        await boundaries.  The default acknowledgement window is deliberately
        longer than the old 30 seconds: on a durable external project store a
        mutation may be committed before revision/runtime receipts finish
        flushing, and returning E_BUSY in that gap makes a successful edit look
        retryable.  An explicit timeout still preserves the asynchronous escape
        hatch for callers that can reconcile by operation id.
        """
        self.touch()
        if self._loop.is_closed():
            raise RuntimeError("session is closed")

        async def _call() -> Any:
            if expected_project_revision is not None:
                current_revision = self._sync_project_revision()
                if int(expected_project_revision) != int(current_revision):
                    raise RevisionConflictError(
                        "project revision mismatch: "
                        f"expected {expected_project_revision}, current {current_revision}"
                    )
            persist_generation = int(getattr(self, "_runtime_persist_generation", 0))
            result = fn()
            # ProjectHandle.apply_ops emits ``timeline_op`` synchronously; its
            # durable event path already commits revision + runtime.  Undo and
            # test doubles do not emit that event, and a failed event persist
            # deliberately leaves the generation unchanged, so they take this
            # one fallback commit.  No mutation loses persistence and no edit
            # fsyncs the same receipts twice.
            if int(getattr(self, "_runtime_persist_generation", 0)) == persist_generation:
                self._persist_runtime_state()
            return result

        fut = asyncio.run_coroutine_threadsafe(_call(), self._loop)
        if timeout is None:
            try:
                timeout = float(os.environ.get("LUMERI_V3_EDIT_TIMEOUT_SEC") or 180.0)
            except ValueError:
                timeout = 180.0
        return fut.result(timeout=max(1.0, float(timeout)))

    def run_production_verb(
        self,
        tool_name: str,
        args: dict[str, Any],
        *,
        trace_id: str,
        idempotency_key: str,
        expected_project_revision: int | None = None,
        emit_progress: ProgressCallback | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Execute one installed capability through the formal production gate.

        Unlike the intentionally narrow MCP surface, this host/operator entry
        can invoke any capability compiled into ``ToolCapabilityRegistry``.
        An active agent turn or a capability outside the persisted
        ``ProductionRun`` stage is refused before the immutable tool receipt is
        claimed.  Paid-media tools then bind a run-scoped reservation before
        their dispatcher can submit.  Replaying a successful key returns its
        stored result and never touches a provider.
        """

        from gemia.production_budget import (
            PAID_MEDIA_CONTEXT_KEY,
            ProductionBudgetError,
        )

        name = str(tool_name or "").strip()
        trace = str(trace_id or "").strip()
        key = str(idempotency_key or "").strip()
        if not name or not trace or not key:
            raise ValueError(
                "run_production_verb requires tool_name, trace_id and idempotency_key"
            )
        if self.production_store is None:
            raise RuntimeError("formal production verbs require a ProductionStore")
        capabilities = getattr(self.agent, "capabilities", None)
        if capabilities is None:
            raise RuntimeError("formal production capability registry is unavailable")
        capability = capabilities.get(name)
        call_args = dict(args or {})
        self.touch()
        if self._loop.is_closed():
            raise RuntimeError("session is closed")

        def _raise_if_agent_turn_active() -> None:
            if not self.turn_in_progress:
                return
            message = "agent turn active; retry when the turn completes"
            raise VerbGateError(
                "E_BUSY",
                message,
                {
                    "error": message,
                    "error_code": "E_BUSY",
                    "tool_name": name,
                    "project_id": self.project_id,
                    "run_id": self.run_id,
                },
            )

        # Fail immediately for the common collision path.  The same check is
        # repeated on the session loop below to close the scheduling window;
        # both checks happen before any receipt or reservation is persisted.
        _raise_if_agent_turn_active()

        async def _execute() -> dict[str, Any]:
            _raise_if_agent_turn_active()
            run = self.production_store.load_run(self.project_id, self.run_id)
            production_state = str(run.get("state") or "created").strip().lower()
            production_revision = int(run.get("revision") or 0)
            if production_state in _PRODUCTION_TERMINAL_STATES:
                message = (
                    f"production run is terminal ({production_state}); "
                    "transition it to an active state before invoking formal tools"
                )
                raise VerbGateError(
                    "E_PRODUCTION_STATE",
                    message,
                    {
                        "error": message,
                        "error_code": "E_PRODUCTION_STATE",
                        "reason": "terminal_state",
                        "tool_name": name,
                        "project_id": self.project_id,
                        "run_id": self.run_id,
                        "production_state": production_state,
                        "production_revision": production_revision,
                    },
                )
            allowed_names = _production_stage_tool_names(
                capabilities, production_state
            )
            if allowed_names is None:
                message = (
                    f"production state {production_state!r} has no formal capability surface"
                )
                raise VerbGateError(
                    "E_PRODUCTION_STATE",
                    message,
                    {
                        "error": message,
                        "error_code": "E_PRODUCTION_STATE",
                        "reason": "unknown_state",
                        "tool_name": name,
                        "project_id": self.project_id,
                        "run_id": self.run_id,
                        "production_state": production_state,
                        "production_revision": production_revision,
                        "allowed_tools": [],
                    },
                )
            if name not in allowed_names:
                message = (
                    f"tool {name!r} is not allowed during production state "
                    f"{production_state!r}"
                )
                raise VerbGateError(
                    "E_PRODUCTION_STATE",
                    message,
                    {
                        "error": message,
                        "error_code": "E_PRODUCTION_STATE",
                        "reason": "tool_not_allowed",
                        "tool_name": name,
                        "project_id": self.project_id,
                        "run_id": self.run_id,
                        "production_state": production_state,
                        "production_revision": production_revision,
                        "allowed_tools": list(allowed_names),
                    },
                )
            if capability.paid_media and name in {"generate_video", "generate_image"}:
                policy = self.production_store.media_policy_decision(
                    self.project_id, self.run_id, name
                )
                if not bool(policy.get("allowed")):
                    message = str(policy.get("reason") or "media policy refused")
                    raise VerbGateError(
                        "E_MEDIA",
                        message,
                        {
                            "error": message,
                            "error_code": "E_MEDIA_POLICY",
                            "reason": "media_policy",
                            "tool_name": name,
                            "project_id": self.project_id,
                            "run_id": self.run_id,
                            "media_policy": policy,
                        },
                    )
            revision_before = self._sync_project_revision()
            if (
                expected_project_revision is not None
                and int(expected_project_revision) != int(revision_before)
            ):
                raise VerbGateError(
                    "E_REVISION_CONFLICT",
                    (
                        "project revision mismatch: "
                        f"expected {expected_project_revision}, current {revision_before}"
                    ),
                    {
                        "error": "project revision mismatch",
                        "error_code": "E_REVISION_CONFLICT",
                        "tool_name": name,
                        "expected_project_revision": int(expected_project_revision),
                        "project_revision": int(revision_before),
                    },
                )
            receipt = self.production_store.claim_tool_call(
                self.project_id,
                self.run_id,
                tool_name=name,
                args=call_args,
                trace_id=trace,
                idempotency_key=key,
                project_revision=revision_before,
            )
            if receipt.get("duplicate"):
                status = str(receipt.get("status") or "")
                if status in {"succeeded", "background"} and isinstance(
                    receipt.get("result"), dict
                ):
                    return {
                        **dict(receipt["result"]),
                        "production_duplicate": True,
                        "production_tool_call_id": receipt.get("tool_call_id"),
                        "production_status": status,
                    }
                raise VerbGateError(
                    "E_IDEMPOTENCY",
                    f"formal tool call is already {status}; automatic retry is forbidden",
                    {
                        "error_code": "E_IDEMPOTENCY",
                        "tool_name": name,
                        "tool_call_id": receipt.get("tool_call_id"),
                        "status": status,
                    },
                )

            ledger = getattr(self.agent.budget, "production_media_budget", None)
            reservation_id: str | None = None
            tool_ctx = self.agent._tool_ctx  # noqa: SLF001 — host execution seam
            extra = dict(tool_ctx.extra)
            call_id = str(receipt.get("tool_call_id") or f"tool-{uuid.uuid4().hex[:12]}")
            try:
                if capability.paid_media:
                    if ledger is None:
                        raise ProductionBudgetError(
                            "paid capability has no production media ledger"
                        )
                    duration_value = (
                        call_args.get("duration_sec")
                        if call_args.get("duration_sec") is not None
                        else call_args.get("duration")
                    )
                    decision = ledger.reserve(
                        idempotency_key=key,
                        tool_name=name,
                        estimated_usd=capability.estimated_usd,
                        provider=str(call_args.get("provider") or "configured"),
                        model=str(call_args.get("model") or ""),
                        requested_duration_sec=(
                            float(duration_value)
                            if duration_value is not None and name == "generate_video"
                            else None
                        ),
                    )
                    if not decision.ok or decision.reservation is None:
                        self.production_store.complete_tool_call(
                            self.project_id,
                            self.run_id,
                            key,
                            status="failed",
                            project_revision=revision_before,
                            error=decision.reason or "production budget refused",
                        )
                        raise VerbGateError(
                            "E_BUDGET",
                            decision.reason or "production budget refused",
                            {
                                "error_code": "E_BUDGET",
                                "tool_name": name,
                                "budget": _production_budget_view(ledger),
                            },
                        )
                    reservation_id = decision.reservation.reservation_id
                    self.production_store.bind_tool_call_reservation(
                        self.project_id,
                        self.run_id,
                        key,
                        reservation_id=reservation_id,
                    )
                    extra[PAID_MEDIA_CONTEXT_KEY] = ledger.call_context(
                        reservation_id
                    ).to_dict()

                extra.update(
                    {
                        "active_trace_id": trace,
                        "tool_call_context": {
                            "trace_id": trace,
                            "idempotency_key": key,
                            "call_id": call_id,
                        },
                    }
                )

                def _progress(update: Any) -> None:
                    event: dict[str, Any] = {
                        "kind": "tool_exec_progress",
                        "origin": "production_host",
                        "call_id": call_id,
                        "tool_name": name,
                        "trace_id": trace,
                    }
                    if getattr(update, "percent", None) is not None:
                        event["percent"] = update.percent
                    if getattr(update, "message", None):
                        event["message"] = update.message
                    self._emit_event(event)
                    if emit_progress is not None:
                        try:
                            emit_progress(update)
                        except Exception:
                            pass

                ctx = dataclasses.replace(
                    tool_ctx,
                    extra=extra,
                    emit_progress=_progress,
                )
                self._emit_event(
                    {
                        "kind": "tool_exec_start",
                        "origin": "production_host",
                        "call_id": call_id,
                        "tool_name": name,
                        "trace_id": trace,
                        "idempotency_key": key,
                        "project_revision": revision_before,
                        "reservation_id": reservation_id,
                    }
                )
                started = time.monotonic()
                result = await capability.dispatcher(call_args, ctx)
                elapsed = time.monotonic() - started
                self.agent.budget.commit(
                    name,
                    actual_usd=0.0,
                    actual_seconds=elapsed,
                )
                revision_after = self._sync_project_revision()
                receipt_status = "succeeded"
                if capability.paid_media and reservation_id and ledger is not None:
                    reservation = ledger.get(reservation_id)
                    if reservation.status != "settled":
                        if isinstance(result, dict) and result.get("job_id"):
                            receipt_status = "background"
                        else:
                            raise ProductionBudgetError(
                                "paid dispatcher returned without a settled receipt or durable background job"
                            )
                self.production_store.complete_tool_call(
                    self.project_id,
                    self.run_id,
                    key,
                    status=receipt_status,
                    project_revision=revision_after,
                    result=result,
                    reservation_id=reservation_id,
                )
                if capability.paid_media and ledger is not None:
                    self.production_store.refresh_budget_summary(
                        self.project_id, self.run_id
                    )
                    self._emit_event(
                        {
                            "kind": "budget_updated",
                            "origin": "production_host",
                            "trace_id": trace,
                            "budget": _production_budget_view(ledger),
                        }
                    )
                public_result = {
                    **dict(result),
                    "production_duplicate": False,
                    "production_tool_call_id": call_id,
                    "production_status": receipt_status,
                }
                self._emit_event(
                    {
                        "kind": "tool_exec_result",
                        "origin": "production_host",
                        "call_id": call_id,
                        "tool_name": name,
                        "trace_id": trace,
                        "elapsed_seconds": elapsed,
                        "result": public_result,
                    }
                )
                return public_result
            except (Exception, asyncio.CancelledError) as exc:
                receipt_status = "failed"
                if capability.paid_media and reservation_id and ledger is not None:
                    try:
                        reservation = ledger.get(reservation_id)
                        if reservation.status == "reserved":
                            ledger.release(
                                reservation_id,
                                reason="dispatcher failed before provider submission",
                            )
                        elif reservation.status == "submitted":
                            ledger.mark_uncertain(
                                reservation_id,
                                error=f"{type(exc).__name__}: {exc}",
                            )
                            receipt_status = "uncertain"
                        elif reservation.status in {"uncertain", "settled"}:
                            receipt_status = "uncertain"
                    except Exception:
                        receipt_status = "uncertain"
                try:
                    self.production_store.complete_tool_call(
                        self.project_id,
                        self.run_id,
                        key,
                        status=receipt_status,
                        project_revision=self._sync_project_revision(),
                        error=f"{type(exc).__name__}: {exc}",
                        reservation_id=reservation_id,
                    )
                except Exception:
                    pass
                self._emit_event(
                    {
                        "kind": "tool_exec_error",
                        "origin": "production_host",
                        "call_id": call_id,
                        "tool_name": name,
                        "trace_id": trace,
                        "error": f"{type(exc).__name__}: {exc}",
                        "error_code": getattr(exc, "code", "E_TOOL_FAILED"),
                    }
                )
                raise

        fut = asyncio.run_coroutine_threadsafe(_execute(), self._loop)
        wait = 300.0 if timeout is None else max(1.0, float(timeout))
        return fut.result(timeout=wait)

    async def _execute_registered_capability(
        self,
        tool_name: str,
        args: dict[str, Any],
        ctx: Any,
    ) -> dict[str, Any]:
        """The one dispatcher seam shared by Agent, HTTP and MCP."""

        capabilities = getattr(self.agent, "capabilities", None)
        if capabilities is None:
            raise RuntimeError("capability registry is unavailable")
        capabilities.validate_arguments(tool_name, args)
        return await capabilities.get(tool_name).dispatcher(dict(args), ctx)

    def execute_capability(
        self,
        tool_name: str,
        args: dict[str, Any],
        *,
        origin: str,
        allowed_names: set[str] | frozenset[str] | None = None,
        call_id: str | None = None,
        request_id: str | None = None,
        idempotency_key: str | None = None,
        expected_project_revision: int | None = None,
        require_mutation_tokens: bool = False,
        emit_progress: ProgressCallback | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Execute one registered capability for Agent adapters, HTTP or MCP.

        The registry is the authority for exposure, read/write classification,
        execution mode, schema, cost and dispatcher.  Every non-Agent adapter
        passes through this method so membership, collision, plan, revision,
        idempotency, budget, dispatch and events retain one ordering.

          1. Membership — the capability must expose the requested origin and
             be present in the adapter's optional curated ``allowed_names``.
          2. Turn-collision guard — a MUTATING verb landing between two agent
             tool calls silently invalidates the model's mid-turn context, so
             while ``turn_in_progress`` a plan-BLOCKED verb fails fast with
             ``E_BUSY`` (mirrors the 409 on double ``submit_turn``). Read verbs
             may interleave.
          3. Plan gate FIRST (same order as ``agent_loop_v3.py``'s plan gate,
             checked before the budget gate): a blocked verb is blocked no
             matter how affordable.
          4. Budget gate — against the SAME ``BudgetGuard`` instance as the
             loop (MCP spend and model spend share the one $5/600s pot). The
             fixed cap has no approval override; it is raised as ``E_BUDGET``.
          5. Dispatch on the session loop with a SHALLOW-COPIED tool context
             (only ``emit_progress`` differs) so an interleaved read verb can't
             cross progress streams with the agent loop's shared ctx.
          6. Commit actuals on success AND failure (same as the loop).
          7. SSE mirror — ``tool_exec_start`` / ``tool_exec_result`` /
             ``tool_exec_error`` with the caller origin and stable call id.

        Returns the dispatcher's result dict. Raises ``VerbGateError`` for a
        gate refusal, or the dispatcher's own exception unchanged.
        """
        from gemia.plan_mode import is_plan_safe, plan_gate_message
        from gemia.tool_outcome import classify_tool_result

        self.touch()
        if self._loop.is_closed():
            raise RuntimeError("session is closed")

        normalized_origin = str(origin or "").strip()
        origin_exposure = {
            "agent": "agent",
            "internal_http": "http",
            "mcp": "mcp",
        }.get(normalized_origin)
        if origin_exposure is None:
            raise ValueError(f"unknown capability origin: {origin!r}")
        call_id = str(call_id or f"{normalized_origin}-{uuid.uuid4().hex[:12]}")
        request_id = str(request_id or call_id)
        capabilities = getattr(self.agent, "capabilities", None)
        if capabilities is None:
            raise RuntimeError("capability registry is unavailable")

        # 1. Membership: generated exposure policy ∩ curated adapter surface.
        try:
            capability = capabilities.get(tool_name)
        except KeyError:
            capability = None
        if (
            capability is None
            or origin_exposure not in capability.exposed_via
            or (allowed_names is not None and tool_name not in allowed_names)
        ):
            raise VerbGateError(
                "E_TOOL",
                f"unknown or excluded {origin_exposure} capability: {tool_name}",
                {
                    "error": f"unknown or excluded {origin_exposure} capability: {tool_name}",
                    "error_code": "E_TOOL",
                    "tool_name": tool_name,
                },
            )

        is_read_only = capability.effect == "read"

        normalized_key = str(idempotency_key or "").strip()
        receipt_key = f"{tool_name}:{normalized_key}" if normalized_key else ""
        args_hash = hashlib.sha256(
            json.dumps(
                dict(args or {}),
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode()
        ).hexdigest()
        if require_mutation_tokens and capability.requires_idempotency_key and not normalized_key:
            raise VerbGateError(
                "E_IDEMPOTENCY_CONFLICT",
                "idempotency_key is required for write and paid capabilities",
                {
                    "error": "idempotency_key is required for write and paid capabilities",
                    "error_code": "E_IDEMPOTENCY_CONFLICT",
                    "tool_name": tool_name,
                },
            )
        if receipt_key:
            with self._capability_receipts_lock:
                prior_receipt = self._capability_receipts.get(receipt_key)
            if prior_receipt is not None:
                if prior_receipt.get("args_hash") != args_hash:
                    raise VerbGateError(
                        "E_IDEMPOTENCY_CONFLICT",
                        "idempotency key was already used with different arguments",
                        {
                            "error": "idempotency key was already used with different arguments",
                            "error_code": "E_IDEMPOTENCY_CONFLICT",
                            "tool_name": tool_name,
                        },
                    )
                if prior_receipt.get("status") == "succeeded":
                    prior = prior_receipt.get("result")
                    return dict(prior) if isinstance(prior, dict) else {"result": prior}
                code = (
                    "E_BUSY"
                    if prior_receipt.get("status") == "in_progress"
                    else "E_IDEMPOTENCY_CONFLICT"
                )
                raise VerbGateError(
                    code,
                    (
                        "capability call with this idempotency key is already in progress"
                        if code == "E_BUSY"
                        else "capability call with this idempotency key previously failed"
                    ),
                    {
                        "error": (
                            "capability call is already in progress"
                            if code == "E_BUSY"
                            else "capability call previously failed"
                        ),
                        "error_code": code,
                        "tool_name": tool_name,
                    },
                )
        if (
            require_mutation_tokens
            and capability.requires_project_revision
            and expected_project_revision is None
        ):
            raise VerbGateError(
                "E_REVISION_CONFLICT",
                "expected_project_revision is required for project writes",
                {
                    "error": "expected_project_revision is required for project writes",
                    "error_code": "E_REVISION_CONFLICT",
                    "tool_name": tool_name,
                    "project_revision": self.project_revision,
                },
            )
        if expected_project_revision is not None:
            current_revision = self.project_revision
            if int(expected_project_revision) != int(current_revision):
                raise VerbGateError(
                    "E_REVISION_CONFLICT",
                    (
                        "project revision mismatch: "
                        f"expected {expected_project_revision}, current {current_revision}"
                    ),
                    {
                        "error": "project revision mismatch",
                        "error_code": "E_REVISION_CONFLICT",
                        "tool_name": tool_name,
                        "expected_project_revision": int(expected_project_revision),
                        "project_revision": int(current_revision),
                    },
                )

        # 2. Turn-collision guard: mutating verbs can't land mid-turn.
        if not is_read_only and self.turn_in_progress:
            raise VerbGateError(
                "E_BUSY",
                "agent turn active; retry when the turn completes",
                {
                    "error": "agent turn active; retry when the turn completes",
                    "error_code": "E_BUSY",
                    "tool_name": tool_name,
                },
            )

        # 3. Plan gate FIRST (byte-compatible with the loop's plan gate).
        if self.plan_mode and not is_plan_safe(tool_name):
            msg = plan_gate_message(tool_name)
            self._emit_event(
                {
                    "kind": "plan_gate",
                    "call_id": call_id,
                    "request_id": request_id,
                    "tool_name": tool_name,
                    "message": msg,
                    "origin": normalized_origin,
                }
            )
            raise VerbGateError(
                "E_PLAN_MODE",
                msg,
                {
                    "blocked_by_plan_mode": True,
                    "error_code": "E_PLAN_MODE",
                    "message": msg,
                    "tool_name": tool_name,
                },
            )

        # 4. Budget gate — same BudgetGuard instance as the loop.
        decision = self.agent.budget.check(tool_name)
        if not decision.ok:
            self._emit_event(
                {
                    "kind": "budget_gate",
                    "call_id": call_id,
                    "request_id": request_id,
                    "tool_name": tool_name,
                    "reason": decision.reason,
                    "alternatives": decision.alternatives,
                    "estimated_cost_usd": decision.estimated_cost_usd,
                    "estimated_eta_sec": decision.estimated_eta_sec,
                    "origin": normalized_origin,
                }
            )
            raise VerbGateError(
                "E_BUDGET",
                decision.reason,
                {
                    "blocked_by_budget": True,
                    "approval_cannot_override": True,
                    "error_code": "E_BUDGET",
                    "reason": decision.reason,
                    "alternatives": decision.alternatives,
                    "estimated_cost_usd": decision.estimated_cost_usd,
                    "estimated_eta_sec": decision.estimated_eta_sec,
                    "tool_name": tool_name,
                },
            )

        if receipt_key:
            with self._capability_receipts_lock:
                receipt = self._capability_receipts.get(receipt_key)
                if receipt is not None:
                    if receipt.get("args_hash") != args_hash:
                        raise VerbGateError(
                            "E_IDEMPOTENCY_CONFLICT",
                            "idempotency key was already used with different arguments",
                            {
                                "error": "idempotency key was already used with different arguments",
                                "error_code": "E_IDEMPOTENCY_CONFLICT",
                                "tool_name": tool_name,
                            },
                        )
                    if receipt.get("status") == "succeeded":
                        prior = receipt.get("result")
                        return dict(prior) if isinstance(prior, dict) else {"result": prior}
                    raise VerbGateError(
                        "E_BUSY",
                        "capability call with this idempotency key is already in progress",
                        {
                            "error": "capability call is already in progress",
                            "error_code": "E_BUSY",
                            "tool_name": tool_name,
                        },
                    )
                self._capability_receipts[receipt_key] = {
                    "status": "in_progress",
                    "args_hash": args_hash,
                    "call_id": call_id,
                    "request_id": request_id,
                }
            self._persist_runtime_state()

        # 5. Dispatch on the session loop with a shallow-copied ctx.
        def _progress_cb(update: Any) -> None:
            # SSE mirror of progress (additive origin), then forward to the
            # MCP progress callback (best-effort, exactly like the SSE path).
            try:
                event: dict[str, Any] = {
                    "kind": "tool_exec_progress",
                    "call_id": call_id,
                    "request_id": request_id,
                    "tool_name": tool_name,
                    "origin": normalized_origin,
                }
                if getattr(update, "percent", None) is not None:
                    event["percent"] = update.percent
                if getattr(update, "message", None):
                    event["message"] = update.message
                if getattr(update, "eta_sec", None) is not None:
                    event["eta_seconds"] = update.eta_sec
                self._emit_event(event)
            except Exception:
                pass
            if emit_progress is not None:
                try:
                    emit_progress(update)
                except Exception:
                    pass

        ctx = dataclasses.replace(self.agent._tool_ctx, emit_progress=_progress_cb)

        async def _dispatch() -> dict[str, Any]:
            execute_registered = self.agent._tool_ctx.extra.get(
                "execute_registered_capability"
            )
            if not callable(execute_registered):
                raise RuntimeError("registered capability executor is unavailable")
            return await execute_registered(tool_name, args, ctx)

        self._emit_event(
            {
                "kind": "tool_exec_start",
                "call_id": call_id,
                "request_id": request_id,
                "tool_name": tool_name,
                "est_cost_usd": decision.estimated_cost_usd,
                "eta_seconds": decision.estimated_eta_sec,
                "origin": normalized_origin,
            }
        )

        _, eta = self.agent.budget.estimate(tool_name)
        wait = timeout if timeout is not None else max(60.0, eta * 6)
        start_ts = time.monotonic()
        fut = asyncio.run_coroutine_threadsafe(_dispatch(), self._loop)
        try:
            result = fut.result(timeout=wait)
        except FuturesTimeoutError:
            # The verb may still land; the caller should re-read state. Do NOT
            # commit budget — the dispatch is still running on the loop and will
            # not report back here.
            self._emit_event(
                {
                    "kind": "tool_exec_error",
                    "call_id": call_id,
                    "request_id": request_id,
                    "tool_name": tool_name,
                    "error": (
                        "verb is queued behind a long-running step and has not "
                        "completed yet — re-read the timeline/session to see "
                        "whether it landed"
                    ),
                    "error_code": "E_BUSY",
                    "origin": normalized_origin,
                }
            )
            raise VerbGateError(
                "E_BUSY",
                "verb is queued behind a long-running step and has not "
                "completed yet — re-read the timeline/session to see whether "
                "it landed",
                {
                    "error": "verb did not complete before timeout",
                    "error_code": "E_BUSY",
                    "tool_name": tool_name,
                },
            ) from None
        except Exception as exc:
            elapsed = time.monotonic() - start_ts
            # 6. Commit actuals on failure too (same as the loop).
            self.agent.budget.commit(tool_name, actual_seconds=elapsed)
            from gemia.errors import GemiaError

            if isinstance(exc, GemiaError):
                err_payload = exc.to_payload()
            else:
                err_payload = {
                    "error": f"{type(exc).__name__}: {exc}",
                    "error_code": "E_UNCAUGHT",
                }
            self._emit_event(
                {
                    "kind": "tool_exec_error",
                    "call_id": call_id,
                    "request_id": request_id,
                    "tool_name": tool_name,
                    "elapsed_seconds": elapsed,
                    "origin": normalized_origin,
                    **err_payload,
                }
            )
            if receipt_key:
                with self._capability_receipts_lock:
                    self._capability_receipts[receipt_key] = {
                        "status": "failed",
                        "args_hash": args_hash,
                        "call_id": call_id,
                        "request_id": request_id,
                        "error": err_payload,
                    }
                self._persist_runtime_state()
            raise

        elapsed = time.monotonic() - start_ts
        self.agent.budget.commit(tool_name, actual_seconds=elapsed)

        outcome = classify_tool_result(result)
        if outcome.is_failure:
            err_payload = outcome.error_payload(tool_name=tool_name)
            err_code = str(outcome.error_code or "E_TOOL_FAILED")
            self._emit_event(
                {
                    "kind": "tool_exec_error",
                    "call_id": call_id,
                    "request_id": request_id,
                    "tool_name": tool_name,
                    "elapsed_seconds": elapsed,
                    "origin": normalized_origin,
                    **err_payload,
                }
            )
            if receipt_key:
                with self._capability_receipts_lock:
                    self._capability_receipts[receipt_key] = {
                        "status": "failed",
                        "args_hash": args_hash,
                        "call_id": call_id,
                        "request_id": request_id,
                        "error": err_payload,
                    }
                self._persist_runtime_state()
            raise VerbGateError(
                err_code,
                str(err_payload.get("error") or f"{tool_name} execution failed"),
                {**err_payload, "tool_name": tool_name},
            )

        # 7. SSE mirror of the result (strip file paths like the loop does).
        event_result = {
            k: v
            for k, v in result.items()
            if k not in {"thumbnail_path", "thumbnail_for_next_message"}
        }
        produced_id = result.get("asset_id")
        if produced_id and self.agent.registry.contains(str(produced_id)):
            event_result["preview_uri"] = str(
                self.agent.registry.get(str(produced_id)).path
            )
        self._emit_event(
            {
                "kind": "tool_exec_result",
                "call_id": call_id,
                "request_id": request_id,
                "tool_name": tool_name,
                "elapsed_seconds": elapsed,
                "result": event_result,
                "origin": normalized_origin,
            }
        )
        if receipt_key:
            with self._capability_receipts_lock:
                self._capability_receipts[receipt_key] = {
                    "status": "succeeded",
                    "args_hash": args_hash,
                    "call_id": call_id,
                    "request_id": request_id,
                    "result": dict(result),
                }
            self._persist_runtime_state()
        return result

    def run_verb(
        self,
        tool_name: str,
        args: dict[str, Any],
        *,
        emit_progress: ProgressCallback | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Compatibility adapter for the curated MCP surface."""

        from gemia.mcp.toolset import MCP_TOOLSET

        return self.execute_capability(
            tool_name,
            args,
            origin="mcp",
            allowed_names=MCP_TOOLSET,
            emit_progress=emit_progress,
            timeout=timeout,
        )

    def deliver_ask_answer(self, question_id: str, answers: dict[str, Any]) -> bool:
        """Deliver a user's answer to a pending ``elicit`` question.

        Returns True if a matching pending question was found. The agent's bridge
        hops the resolution back onto this session's event loop, so this is safe to
        call directly from the HTTP handler thread.
        """
        self.touch()
        return self.agent.deliver_ask_answer(question_id, answers)

    def get_pending_question(self, question_id: str) -> dict[str, Any] | None:
        """Return the question dict for a pending elicit, or None."""
        return self.agent.get_pending_question(question_id)

    def set_plan_mode(self, enabled: bool) -> bool:
        """Toggle the agent's plan mode. Safe from the HTTP handler thread
        (atomic bool flip + thread-safe SSE emit). Returns the new state."""
        self.touch()
        return self.agent.set_plan_mode(enabled)

    @property
    def plan_mode(self) -> bool:
        return bool(self.agent.plan_mode)

    def asset_path(self, asset_id: str) -> Path | None:
        self.touch()
        if not self.agent.registry.contains(asset_id):
            return None
        return self.agent.registry.get(asset_id).path

    @staticmethod
    def _asset_availability(records: list[Any]) -> dict[str, bool]:
        """Check each asset once, grouping external-volume metadata reads.

        Most project assets share one workdir.  One ``scandir`` gives the same
        file/symlink-following semantics as ``Path.is_file`` without issuing a
        separate path walk for every record.  A per-path fallback preserves
        behavior for inaccessible or unusual parents.
        """

        grouped: dict[Path, list[Any]] = {}
        for record in records:
            grouped.setdefault(record.path.parent, []).append(record)
        availability: dict[str, bool] = {}
        for parent, members in grouped.items():
            names = {record.path.name for record in members}
            try:
                with os.scandir(parent) as entries:
                    found = {
                        entry.name: entry.is_file()
                        for entry in entries
                        if entry.name in names
                    }
                for record in members:
                    availability[record.asset_id] = bool(found.get(record.path.name, False))
            except OSError:
                for record in members:
                    try:
                        availability[record.asset_id] = record.path.is_file()
                    except OSError:
                        availability[record.asset_id] = False
        return availability

    def list_assets(
        self,
        *,
        records: list[Any] | None = None,
        availability: dict[str, bool] | None = None,
    ) -> list[dict[str, Any]]:
        self.touch()
        records = list(records) if records is not None else self.agent.registry.list_records()
        availability = availability or self._asset_availability(records)
        return [
            {
                "asset_id": r.asset_id,
                "kind": r.kind,
                "summary": r.summary,
                "created_at": r.created_at,
                "lineage": list(r.lineage),
                "sha256": r.sha256,
                "source": dict(r.source),
                "license": dict(r.license),
                "available": bool(availability.get(r.asset_id, False)),
            }
            for r in records
        ]

    def list_tasks(self) -> list[dict[str, Any]]:
        """Snapshot the session's background shell jobs for REST + reconnect
        reconcile. Direct registry read from the HTTP thread — same discipline
        as ``list_assets`` (a plain snapshot; the registry is only mutated on
        the loop thread, and a read racing a mutation sees a coherent record)."""
        self.touch()
        records = self.agent._tool_ctx.jobs.list_records()  # noqa: SLF001 — same-package plumbing
        out: list[dict[str, Any]] = []
        for r in records:
            raw = r.last_polled_status
            status = "running" if raw in ("submitted", "queued", "running") else raw
            out.append({
                "job_id": r.job_id,
                "kind": r.kind,
                "status": status,
                "summary": r.summary,
                "submitted_at": r.submitted_at,
                "elapsed_sec": round(time.monotonic() - r.submitted_mono, 1),
                "error": r.final_error,
            })
        return out

    def kill_task(self, job_id: str) -> dict[str, Any]:
        """Kill a background shell job by hopping the kill_job dispatch onto the
        session loop (killpg + registry mutation must run where the job was
        spawned). Raises KeyError for an unknown job_id (route maps to 404)."""
        self.touch()
        if self._loop.is_closed():
            raise RuntimeError("session is closed")
        from gemia.tools import build as _build

        ctx = self.agent._tool_ctx  # noqa: SLF001 — same-package plumbing

        async def _call() -> dict[str, Any]:
            result = await _build.dispatch_kill({"job_id": job_id}, ctx)
            self.agent.persist_jobs()  # record the terminal state on the loop thread
            return result

        fut = asyncio.run_coroutine_threadsafe(_call(), self._loop)
        return fut.result(timeout=15)

    @property
    def project_revision(self) -> int:
        return self._sync_project_revision()

    @property
    def cached_project_revision(self) -> int:
        """Last durably observed revision without another filesystem sync."""
        return int(self._last_emitted_project_revision)

    def _asset_mix(
        self,
        *,
        records: list[Any] | None = None,
        availability: dict[str, bool] | None = None,
    ) -> dict[str, Any]:
        records = (
            list(records)
            if records is not None
            else list(self.agent.registry.list_records())
        )
        availability = availability or self._asset_availability(records)
        mix: dict[str, Any] = {
            "total": len(records),
            "video": 0,
            "image": 0,
            "audio": 0,
            "lottie": 0,
            "external": 0,
            "generated": 0,
            "generated_video": 0,
            "generated_image": 0,
            "generated_audio": 0,
            "derived": 0,
            "missing": 0,
            "provenance_complete": True,
        }
        for record in records:
            if record.kind in {"video", "image", "audio", "lottie"}:
                mix[record.kind] += 1
            source_kind = str((record.source or {}).get("kind") or "").lower()
            external_source_kinds = {
                "external", "stock", "licensed", "public", "public_stock",
            }
            generated_source_kinds = {
                "generated", "generated_image", "generated_video",
                "generated_audio", "provider", "ai",
            }
            if source_kind in external_source_kinds:
                mix["external"] += 1
            elif source_kind in generated_source_kinds:
                mix["generated"] += 1
                if record.kind == "video":
                    mix["generated_video"] += 1
                elif record.kind == "image":
                    mix["generated_image"] += 1
                elif record.kind == "audio":
                    mix["generated_audio"] += 1
            else:
                mix["derived"] += 1
            if not availability.get(record.asset_id, False):
                mix["missing"] += 1
            # Derived assets inherit provenance through lineage.  Root assets
            # must identify a source and carry a content hash; externally
            # sourced roots additionally require license metadata.
            provenance_ok = bool(record.sha256) and bool(record.source)
            if not record.lineage and source_kind in external_source_kinds:
                provenance_ok = provenance_ok and bool(record.license)
            if not provenance_ok:
                mix["provenance_complete"] = False
        return mix

    def snapshot(self) -> dict[str, Any]:
        """Return the durable production view used by v2 routes and reconnect."""

        snapshot_started = time.monotonic()
        snapshot_trace: list[dict[str, Any]] = []

        def mark_snapshot_phase(phase: str) -> None:
            snapshot_trace.append(
                {
                    "phase": phase,
                    "elapsed_ms": round(
                        (time.monotonic() - snapshot_started) * 1000, 3
                    ),
                }
            )

        # A snapshot is a read model.  Design-program verification belongs to
        # production mutation/preflight paths, not GET /sessions or switching
        # between chats.
        revision = self._sync_project_revision(verify_design_program=False)
        mark_snapshot_phase("project_revision_synced")
        run: dict[str, Any] = {}
        legacy_budget = getattr(self.agent, "budget", None)
        budget: dict[str, Any] = {}
        if self.production_store is not None:
            run = self.production_store.load_run(self.project_id, self.run_id)
            mark_snapshot_phase("production_run_loaded")
            ledger = getattr(legacy_budget, "production_media_budget", None)
            if ledger is None:
                ledger = self.production_store.media_budget(self.project_id, self.run_id)
            budget = _production_budget_view(ledger)
        elif legacy_budget is not None:
            budget = legacy_budget.snapshot()
        mark_snapshot_phase("budget_loaded")
        records = list(self.agent.registry.list_records())
        availability = self._asset_availability(records)
        mark_snapshot_phase("asset_availability_loaded")
        assets = self.list_assets(records=records, availability=availability)
        asset_mix = self._asset_mix(records=records, availability=availability)
        tasks = self.list_tasks()
        delivery = (
            self.production_store.public_delivery(
                self.project_id,
                self.run_id,
                run=run,
            )
            if self.production_store is not None
            else None
        )
        mark_snapshot_phase("payload_built")
        return {
            "session_id": self.session_id,
            "project_id": self.project_id,
            "run_id": self.run_id,
            "project_revision": revision,
            "production_revision": int(run.get("revision") or 0),
            "production_state": str(run.get("state") or "created"),
            "turn_in_progress": self.turn_in_progress,
            "plan_mode": bool(getattr(self.agent, "plan_mode", False)),
            "assets": assets,
            "tasks": tasks,
            "blockers": list(run.get("blockers") or []),
            "budget": budget,
            "asset_mix": asset_mix,
            "delivery": delivery,
            "persistence_error": self._persistence_error,
            "resume_trace": list(getattr(self, "resume_trace", [])),
            "snapshot_trace": snapshot_trace,
        }

    def close(self) -> None:
        if self._loop.is_closed():
            return
        try:
            self._persist_runtime_state()
            if self.production_store is not None:
                self.production_store.update_session(
                    self.session_id, {"status": "closed"}
                )
        except Exception as exc:
            self._persistence_error = f"{type(exc).__name__}: {exc}"
        try:
            fut = asyncio.run_coroutine_threadsafe(self._cancel_pending(), self._loop)
            fut.result(timeout=5)
        except Exception:
            pass
        # After the watcher task is cancelled (above), reap any background
        # shell children so they don't outlive the session as orphans.
        self._sweep_background_jobs()
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)
        with self._transcript_lock:
            if self._transcript_file is not None:
                try:
                    self._transcript_file.close()
                except Exception:
                    pass
                self._transcript_file = None

    async def _cancel_pending(self) -> None:
        current = asyncio.current_task(self._loop)
        tasks = [t for t in asyncio.all_tasks(self._loop) if t is not current and not t.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


class SessionManager:
    """Process-wide directory of active v3 sessions."""

    def __init__(
        self,
        *,
        output_root: Path,
        max_sessions: int | None = None,
        idle_timeout_sec: int | None = None,
        sweep_interval_sec: int | None = None,
    ) -> None:
        self._output_root = Path(output_root).expanduser().resolve()
        self._output_root.mkdir(parents=True, exist_ok=True)
        self._sessions_root = self._output_root / "sessions"
        self._workdirs_root = self._output_root / "workdirs"
        self._workdirs_root.mkdir(parents=True, exist_ok=True)
        self._production_store = ProductionStore(self._output_root)
        self._projects_root = self._production_store.projects_root
        self._project_store = ProjectStore(self._projects_root)
        self._lock = threading.Lock()
        self._runners: dict[str, SessionRunner] = {}
        self._creating_sessions = 0
        self._max_sessions = max(
            1,
            int(
                max_sessions
                if max_sessions is not None
                else _env_int("LUMERI_V3_MAX_SESSIONS", _DEFAULT_MAX_SESSIONS, minimum=1)
            ),
        )
        self._idle_timeout_sec = max(
            0,
            int(
                idle_timeout_sec
                if idle_timeout_sec is not None
                else _env_int("LUMERI_V3_IDLE_TIMEOUT_SEC", _DEFAULT_IDLE_TIMEOUT_SEC, minimum=0)
            ),
        )
        self._sweep_interval_sec = max(
            0,
            int(
                sweep_interval_sec
                if sweep_interval_sec is not None
                else _env_int("LUMERI_V3_SWEEP_INTERVAL_SEC", _DEFAULT_SWEEP_INTERVAL_SEC, minimum=0)
            ),
        )
        self._stop_sweeper = threading.Event()
        self._sweeper: threading.Thread | None = None
        if self._sweep_interval_sec > 0:
            self._sweeper = threading.Thread(
                target=self._sweep_loop,
                name="lumeri-v3-session-sweeper",
                daemon=True,
            )
            self._sweeper.start()

    def create_session(
        self,
        *,
        project_id: str | None = None,
        run_id: str | None = None,
        fork_from_project_id: str | None = None,
        account_id: str | None = None,
        remote: bool = False,
        reality_contract: dict[str, Any] | None = None,
    ) -> SessionRunner:
        self.cleanup_idle()
        session_id = f"v3-{uuid.uuid4().hex[:12]}"
        if fork_from_project_id and (project_id or run_id):
            raise ValueError(
                "fork_from_project_id cannot be combined with project_id or run_id"
            )
        fork_source = None
        # Creating another session beneath an existing Project is always a new
        # production workspace.  Keep this rule on the backend so an old open
        # Web tab, CLI build, or other client that still posts only project_id
        # cannot silently rejoin the previous session's timeline, design files,
        # project memory, or activity log.  Supplying both project_id and
        # run_id remains the explicit durable-resume contract.
        if project_id and not run_id and not fork_from_project_id:
            requested_project = self._production_store.load_project(
                str(project_id)
            )
            if requested_project.get("session_ids"):
                fork_from_project_id = str(
                    requested_project.get("forked_from_project_id")
                    or requested_project.get("project_id")
                    or project_id
                )
                project_id = None
        if fork_from_project_id:
            fork_source = self._production_store.load_project(
                str(fork_from_project_id)
            )
            project_id = f"project-{uuid.uuid4().hex[:12]}"
        project_id = str(project_id or f"project-{uuid.uuid4().hex[:12]}")
        run_id = str(run_id or f"run-{uuid.uuid4().hex[:12]}")
        with self._lock:
            active_or_creating = len(self._runners) + self._creating_sessions
            if active_or_creating >= self._max_sessions:
                raise SessionLimitError(
                    f"too many active v3 sessions ({active_or_creating} >= {self._max_sessions})"
                )
            self._creating_sessions += 1
        # Register SSE BEFORE the agent thread starts so the agent's
        # first emit (turn_start) isn't dropped.
        runner: SessionRunner | None = None
        registered = False
        created = False
        try:
            self._production_store.create_project(
                project_id,
                name=(
                    str(fork_source.get("name") or "")
                    if fork_source is not None
                    else None
                ),
                source_root=(
                    str(fork_source.get("source_root") or "") or None
                    if fork_source is not None
                    else None
                ),
                forked_from_project_id=(
                    str(fork_from_project_id)
                    if fork_source is not None
                    else None
                ),
            )
            if not self._project_store.exists(project_id):
                seed = empty_project(account_id=account_id)
                seed["project_id"] = project_id
                self._project_store.create(project_id, seed=seed)
            if fork_source is not None:
                inherited_assets = AssetRegistry.load(
                    self._production_store.asset_registry_path(
                        str(fork_from_project_id)
                    )
                )
                inherited_assets.save(
                    self._production_store.asset_registry_path(project_id)
                )
            run = self._production_store.create_run(
                project_id,
                run_id,
                reality_contract=reality_contract,
                hard_cap_usd=15.0,
            )
            output_dir = self._workdirs_root / session_id
            session_meta = self._production_store.create_session_record(
                session_id,
                project_id=project_id,
                run_id=run_id,
                output_dir=output_dir,
                account_id=str(account_id or ""),
                remote=remote,
            )
            asset_registry = AssetRegistry.load(
                self._production_store.asset_registry_path(project_id)
            )
            SSE_REGISTRY.register(
                session_id,
                last_event_id=_last_transcript_seq(
                    self._sessions_root / session_id / "transcript.jsonl"
                ),
            )
            registered = True
            runner = SessionRunner(
                session_id=session_id,
                output_dir=output_dir,
                sessions_root=self._sessions_root,
                account_id=account_id,
                remote=remote,
                production_store=self._production_store,
                project_root=self._projects_root,
                project_id=project_id,
                run_id=run_id,
                runtime_state={},
                asset_registry=asset_registry,
                session_meta=session_meta,
                # Durable production runs have no cumulative 600-second stop.
                # Paid media is gated solely by ProductionMediaBudget.
                budget_max_usd=1.0e100,
                budget_max_seconds=None,
            )
            self._production_store.update_session(session_id, {"status": "running"})
            created = True
        except Exception as exc:
            if runner is not None:
                runner.close()
            if registered:
                SSE_REGISTRY.close(session_id)
                SSE_REGISTRY.unregister(session_id)
            try:
                self._production_store.update_session(
                    session_id,
                    {"status": "failed", "error": f"{type(exc).__name__}: {exc}"},
                )
            except ProductionStoreError:
                pass
            raise
        finally:
            with self._lock:
                self._creating_sessions -= 1
                if created and runner is not None:
                    self._runners[session_id] = runner
        assert runner is not None
        return runner

    def resume_session(self, session_id: str) -> SessionRunner:
        """Rebuild an inactive runner from durable project/run/session facts."""

        resume_started = time.monotonic()
        resume_trace: list[dict[str, Any]] = []

        def mark_resume_phase(phase: str) -> None:
            resume_trace.append(
                {
                    "phase": phase,
                    "elapsed_ms": round((time.monotonic() - resume_started) * 1000, 3),
                }
            )

        session_id = str(session_id or "").strip()
        if not session_id:
            raise ValueError("session_id must be non-empty")
        active = self.get(session_id)
        if active is not None:
            return active
        self.cleanup_idle()
        with self._lock:
            active_or_creating = len(self._runners) + self._creating_sessions
            if active_or_creating >= self._max_sessions:
                raise SessionLimitError(
                    f"too many active v3 sessions ({active_or_creating} >= {self._max_sessions})"
                )
            self._creating_sessions += 1

        runner: SessionRunner | None = None
        registered = False
        installed = False
        try:
            meta = self._production_store.load_session(session_id)
            mark_resume_phase("session_loaded")
            if meta.get("deleted_at"):
                raise ProductionNotFoundError(f"session not found: {session_id}")
            project_id = str(meta.get("project_id") or "")
            run_id = str(meta.get("run_id") or "")
            if not project_id or not run_id:
                raise ProductionNotFoundError(
                    f"session has chat history only and no durable project/run: {session_id}"
                )
            project_record = self._production_store.load_project(project_id)
            run = self._production_store.load_run(project_id, run_id)
            mark_resume_phase("project_and_run_loaded")
            if not self._project_store.exists(project_id):
                raise ProductionNotFoundError(
                    f"project timeline is missing: {project_id}"
                )
            output_dir = Path(str(meta.get("output_dir") or "")).expanduser().resolve()
            try:
                output_dir.relative_to(self._workdirs_root.resolve())
            except ValueError as exc:
                raise ProductionStoreError(
                    f"session output_dir is outside the v3 work root: {output_dir}"
                ) from exc
            self._production_store.reconcile_inflight_turns(
                project_id, run_id, session_id=session_id
            )
            self._production_store.reconcile_inflight_tool_calls(
                project_id,
                run_id,
                project_revision=int(project_record.get("revision") or 0),
            )
            mark_resume_phase("inflight_reconciled")
            runtime_state = self._production_store.load_runtime_state(session_id)
            asset_registry = AssetRegistry.load(
                self._production_store.asset_registry_path(project_id)
            )
            mark_resume_phase("runtime_and_assets_loaded")
            # EventSource persists Last-Event-ID across a durable session
            # sleep/resume. Seed the new in-process ring from the transcript
            # sequence so resumed events remain strictly monotonic instead of
            # restarting at 1 and being discarded forever as stale.
            SSE_REGISTRY.register(
                session_id,
                last_event_id=_last_transcript_seq(
                    self._sessions_root / session_id / "transcript.jsonl"
                ),
            )
            registered = True
            runner = SessionRunner(
                session_id=session_id,
                output_dir=output_dir,
                sessions_root=self._sessions_root,
                account_id=str(meta.get("account_id") or ""),
                remote=bool(meta.get("remote", False)),
                production_store=self._production_store,
                project_root=self._projects_root,
                project_id=project_id,
                run_id=run_id,
                runtime_state=runtime_state,
                asset_registry=asset_registry,
                session_meta=meta,
                budget_max_usd=1.0e100,
                budget_max_seconds=None,
            )
            mark_resume_phase("runner_rebuilt")
            # SessionRunner._create_agent already opens and validates the one
            # canonical ledger before construction can succeed.  Reopening it
            # here used to turn resume into another external-volume read/write.
            mark_resume_phase("budget_validated_in_runner")
            self._production_store.update_session(
                session_id,
                {"status": "running", "last_resume_trace": resume_trace},
            )
            mark_resume_phase("session_running_committed")
            runner.resume_trace = list(resume_trace)
            installed = True
        except Exception as exc:
            if runner is not None:
                runner.close()
            if registered:
                SSE_REGISTRY.close(session_id)
                SSE_REGISTRY.unregister(session_id)
            try:
                self._production_store.update_session(
                    session_id,
                    {"status": "failed", "error": f"{type(exc).__name__}: {exc}"},
                )
            except ProductionStoreError:
                pass
            raise
        finally:
            with self._lock:
                self._creating_sessions -= 1
                if installed and runner is not None:
                    # A concurrent resume can only reach here if both callers
                    # passed the early active check.  Keep the first and close
                    # the duplicate below rather than replacing a live runner.
                    existing = self._runners.get(session_id)
                    if existing is None:
                        self._runners[session_id] = runner
                    elif existing is not runner:
                        installed = False
        if not installed:
            assert runner is not None
            runner.close()
            existing = self.get(session_id)
            if existing is not None:
                return existing
            raise ProductionStoreError(
                f"concurrent session resume failed to install runner: {session_id}",
                code="E_PROJECT_BUSY",
            )
        assert runner is not None
        return runner

    def get(self, session_id: str) -> SessionRunner | None:
        with self._lock:
            runner = self._runners.get(session_id)
        if runner is not None:
            runner.touch()
        return runner

    def list_sessions(self) -> list[str]:
        with self._lock:
            return sorted(self._runners.keys())

    def list_persisted_sessions(
        self, *, include_deleted: bool = False
    ) -> list[dict[str, Any]]:
        return self._production_store.list_session_records(
            include_deleted=include_deleted
        )

    def set_session_pinned(self, session_id: str, pinned: bool) -> dict[str, Any]:
        record = self._production_store.load_session(session_id)
        if record.get("deleted_at"):
            raise FileNotFoundError(f"session not found: {session_id}")
        return self._production_store.update_session(
            session_id, {"pinned": bool(pinned)}
        )

    def handoff_session_assets(
        self,
        source_session_id: str,
        target_session_id: str,
    ) -> dict[str, Any]:
        """Copy the usable assets from one Project session to another.

        A handoff is deliberately an asset boundary: it gives the receiving
        production a durable, provenance-marked copy of the source material,
        while leaving timelines, design files, chat messages, undo history and
        run state untouched.  Both sessions must belong to the same creator
        Project family, and neither may be actively producing while the
        handoff snapshot is taken.
        """
        source_session_id = str(source_session_id or "")
        target_session_id = str(target_session_id or "")
        if not source_session_id or not target_session_id:
            raise ValueError("source and target session ids are required")
        if source_session_id == target_session_id:
            raise ValueError("a session cannot hand off to itself")

        source_meta = self._production_store.load_session(source_session_id)
        target_meta = self._production_store.load_session(target_session_id)
        if source_meta.get("deleted_at") or target_meta.get("deleted_at"):
            raise FileNotFoundError("source or target session was deleted")

        source_project_id = str(source_meta.get("project_id") or "")
        target_project_id = str(target_meta.get("project_id") or "")
        if not source_project_id or not target_project_id:
            raise ValueError("source or target session has no Project")
        if self._project_family_id(source_project_id) != self._project_family_id(target_project_id):
            raise ValueError("sessions must belong to the same Project")

        source_runner = self.get(source_session_id)
        if source_runner is None:
            source_runner = self.resume_session(source_session_id)
        target_runner = self.get(target_session_id)
        if source_runner.turn_in_progress or (
            target_runner is not None and target_runner.turn_in_progress
        ):
            raise ValueError("finish the active work before handing off results")

        source_registry = source_runner.agent.registry
        target_registry = (
            target_runner.agent.registry
            if target_runner is not None
            else AssetRegistry.load(
                self._production_store.asset_registry_path(target_project_id)
            )
        )
        target_records = target_registry.list_records()
        transferred: list[dict[str, str]] = []
        already_available: list[dict[str, str]] = []
        unavailable: list[dict[str, str]] = []

        for source_record in source_registry.list_records():
            if not source_record.path.is_file():
                unavailable.append({
                    "asset_id": source_record.asset_id,
                    "summary": source_record.summary,
                })
                continue
            existing = next(
                (
                    record
                    for record in target_records
                    if (
                        source_record.sha256
                        and source_record.sha256 == record.sha256
                    )
                    or source_record.path == record.path
                ),
                None,
            )
            if existing is not None:
                already_available.append({
                    "source_asset_id": source_record.asset_id,
                    "asset_id": existing.asset_id,
                    "summary": existing.summary,
                })
                continue

            source_info = dict(source_record.source)
            source_info["handoff"] = {
                "from_session_id": source_session_id,
                "from_project_id": source_project_id,
                "source_asset_id": source_record.asset_id,
            }
            imported = target_registry.register_output(
                target_registry.allocate_id(source_record.kind),
                kind=source_record.kind,
                path=source_record.path,
                summary=source_record.summary,
                lineage=source_record.lineage,
                source=source_info,
                license=source_record.license,
            )
            target_records.append(imported)
            transferred.append({
                "source_asset_id": source_record.asset_id,
                "asset_id": imported.asset_id,
                "kind": imported.kind,
                "summary": imported.summary,
            })

        # Sleeping sessions do not have an on-change callback.  Persist the
        # receiving registry directly so reopening that session sees exactly
        # the same handoff without reviving its chat or agent context.
        if target_runner is None:
            target_registry.save(
                self._production_store.asset_registry_path(target_project_id)
            )

        return {
            "source_session_id": source_session_id,
            "target_session_id": target_session_id,
            "transferred": transferred,
            "already_available": already_available,
            "unavailable": unavailable,
        }

    def _project_family_id(self, project_id: str) -> str:
        """Return the visible root Project for a production fork."""
        current_id = str(project_id)
        seen: set[str] = set()
        while current_id:
            if current_id in seen:
                raise ValueError("Project fork ancestry contains a cycle")
            seen.add(current_id)
            record = self._production_store.load_project(current_id)
            parent_id = str(record.get("forked_from_project_id") or "")
            if not parent_id:
                return current_id
            current_id = parent_id
        raise ValueError("Project has no root")

    def delete_session(self, session_id: str) -> dict[str, Any]:
        record = self._production_store.load_session(session_id)
        if record.get("deleted_at"):
            return record
        with self._lock:
            runner = self._runners.get(session_id)
        if runner is not None:
            try:
                pending = [
                    task
                    for task in runner.list_tasks()
                    if str(task.get("status") or "").lower()
                    not in {"done", "completed", "failed", "cancelled", "killed"}
                ]
            except Exception as exc:
                raise ValueError("session activity could not be verified") from exc
            if runner.turn_in_progress or pending:
                raise ValueError("session still has running work")
        # Sidebar deletion is a recoverable tombstone. Keep the durable
        # transcript/project/run files intact and only remove it from listings.
        record = self._production_store.update_session(
            session_id,
            {
                "deleted_at": datetime.now(timezone.utc).isoformat(),
                "pinned": False,
            },
        )
        self.close_session(session_id)
        return record

    def create_project(
        self,
        *,
        name: str,
        source_root: str | Path | None = None,
    ) -> dict[str, Any]:
        resolved = (
            validate_source_root(source_root, internal_root=self._output_root)
            if str(source_root or "").strip()
            else None
        )
        project_name = str(name or "").strip() or (
            resolved.name if resolved is not None else "Untitled Project"
        )
        project_id = f"project-{uuid.uuid4().hex[:12]}"
        record = self._production_store.create_project(
            project_id,
            name=project_name,
            source_root=resolved,
        )
        if not self._project_store.exists(project_id):
            seed = empty_project()
            seed["project_id"] = project_id
            seed["title"] = record["name"]
            self._project_store.create(project_id, seed=seed)
        project_context.bootstrap(self._production_store, project_id)
        return self.get_project(project_id)

    def list_projects(self) -> list[dict[str, Any]]:
        sessions = {
            str(item.get("session_id") or ""): item
            for item in self._production_store.list_session_records()
        }
        records = self._production_store.list_projects()
        records_by_id = {
            str(record.get("project_id") or ""): record for record in records
        }
        branch_session_ids: dict[str, list[str]] = {}
        for record in records:
            parent_id = str(record.get("forked_from_project_id") or "")
            if parent_id and parent_id in records_by_id:
                branch_session_ids.setdefault(parent_id, []).extend(
                    str(session_id)
                    for session_id in (record.get("session_ids") or [])
                    if str(session_id)
                )
        out: list[dict[str, Any]] = []
        for record in records:
            parent_id = str(record.get("forked_from_project_id") or "")
            if parent_id and parent_id in records_by_id:
                # A production fork owns independent timeline/design state but
                # remains presented beneath its source Project in the creator
                # sidebar.  Its session record retains the fork's real
                # project_id for resume and all backend operations.
                continue
            context = project_context.summary(
                self._production_store, str(record["project_id"])
            )
            history = FileMutationJournal(
                self._production_store.project_dir(record["project_id"]) / "file-history",
                allowed_roots={
                    "source": str(record.get("source_root") or ""),
                    "edit": str(record.get("edit_root") or ""),
                },
            ).state()
            project_sessions = [
                sessions[sid]
                for sid in [
                    *(str(item) for item in (record.get("session_ids") or [])),
                    *branch_session_ids.get(str(record["project_id"]), []),
                ]
                if sid in sessions
            ]
            project_sessions.sort(
                key=lambda item: str(item.get("updated_at") or ""), reverse=True
            )
            project_sessions.sort(
                key=lambda item: bool(item.get("pinned")), reverse=True
            )
            out.append({
                **record,
                "context": context,
                "file_history": history,
                "sessions": project_sessions,
            })
        return out

    def get_project(self, project_id: str) -> dict[str, Any]:
        record = self._production_store.load_project(project_id)
        if not self._project_store.exists(project_id):
            raise ProductionNotFoundError(f"project timeline is missing: {project_id}")
        state = self._project_store.load(project_id)
        meta = self._project_store.load_meta(project_id)
        assets = AssetRegistry.load(
            self._production_store.asset_registry_path(project_id)
        ).to_dict()
        source_root = str(record.get("source_root") or "")
        edit_root = str(
            record.get("edit_root")
            or (self._production_store.project_dir(project_id) / "design")
        )
        history = FileMutationJournal(
            self._production_store.project_dir(project_id) / "file-history",
            allowed_roots={"source": source_root, "edit": edit_root},
        ).state()
        context = project_context.summary(self._production_store, project_id)
        sessions_by_id = {
            str(item.get("session_id") or ""): item
            for item in self._production_store.list_session_records()
        }
        return {
            **record,
            "project_revision": int(record.get("revision") or 0),
            "project_state": state,
            "timeline_meta": meta,
            "assets": assets,
            "context": context,
            "file_history": history,
            "sessions": [
                sessions_by_id[sid]
                for sid in record.get("session_ids") or []
                if sid in sessions_by_id
            ],
        }

    def undo_project_files(self, project_id: str) -> dict[str, Any]:
        record = self._production_store.load_project(project_id)
        return FileMutationJournal(
            self._production_store.project_dir(project_id) / "file-history",
            allowed_roots={
                "source": str(record.get("source_root") or ""),
                "edit": str(record.get("edit_root") or ""),
            },
        ).undo()

    def redo_project_files(self, project_id: str) -> dict[str, Any]:
        record = self._production_store.load_project(project_id)
        return FileMutationJournal(
            self._production_store.project_dir(project_id) / "file-history",
            allowed_roots={
                "source": str(record.get("source_root") or ""),
                "edit": str(record.get("edit_root") or ""),
            },
        ).redo()

    def get_run(self, project_id: str, run_id: str) -> dict[str, Any]:
        run = self._production_store.load_run(project_id, run_id)
        budget = _production_budget_view(
            self._production_store.media_budget(project_id, run_id)
        )
        return {
            **run,
            "production_state": str(run.get("state") or "created"),
            "production_revision": int(run.get("revision") or 0),
            "budget": budget,
            "delivery": self._production_store.public_delivery(
                project_id,
                run_id,
                run=run,
            ),
        }

    def transition_run(
        self,
        project_id: str,
        run_id: str,
        state: str,
        *,
        expected_revision: int | None = None,
        blocker: dict[str, Any] | None = None,
        trace_id: str | None = None,
    ) -> dict[str, Any]:
        run = self._production_store.transition_run(
            project_id,
            run_id,
            state,
            expected_revision=expected_revision,
            blocker=blocker,
            trace_id=trace_id,
        )
        delivery = self._production_store.public_delivery(
            project_id,
            run_id,
            run=run,
        )
        event = {
            "kind": "production_state_changed",
            "project_id": project_id,
            "run_id": run_id,
            "production_state": run.get("state"),
            "production_revision": int(run.get("revision") or 0),
            "blockers": list(run.get("blockers") or []),
            "delivery": delivery,
        }
        self._broadcast_project_event(project_id, run_id, event)
        if str(run.get("state")) == "ready_for_review":
            self._broadcast_project_event(
                project_id,
                run_id,
                {
                    "kind": "delivery_ready",
                    "project_id": project_id,
                    "run_id": run_id,
                    "project_revision": int(run.get("project_revision") or 0),
                    "production_revision": int(run.get("revision") or 0),
                    "delivery": delivery,
                },
            )
        return self.get_run(project_id, run_id)

    def review_run(
        self,
        project_id: str,
        run_id: str,
        *,
        action: str,
        note: str = "",
        start_sec: float | None = None,
        end_sec: float | None = None,
        expected_project_revision: int | None = None,
        reviewer_account_id: str | None = None,
        watched_full_video: bool | None = None,
        creative_checks: dict[str, bool] | None = None,
    ) -> dict[str, Any]:
        run = self._production_store.review_run(
            project_id,
            run_id,
            action=action,
            note=note,
            start_sec=start_sec,
            end_sec=end_sec,
            expected_project_revision=expected_project_revision,
            reviewer_account_id=reviewer_account_id,
            watched_full_video=watched_full_video,
            creative_checks=creative_checks,
        )
        delivery = self._production_store.public_delivery(
            project_id,
            run_id,
            run=run,
        )
        self._broadcast_project_event(
            project_id,
            run_id,
            {
                "kind": "acceptance_updated",
                "project_id": project_id,
                "run_id": run_id,
                "action": action,
                "production_state": run.get("state"),
                "project_revision": int(run.get("project_revision") or 0),
                "production_revision": int(run.get("revision") or 0),
                "review": run.get("review"),
                "delivery": delivery,
            },
        )
        self._broadcast_project_event(
            project_id,
            run_id,
            {
                "kind": "production_state_changed",
                "project_id": project_id,
                "run_id": run_id,
                "production_state": run.get("state"),
                "production_revision": int(run.get("revision") or 0),
                "delivery": delivery,
            },
        )
        return self.get_run(project_id, run_id)

    def record_evidence(
        self,
        project_id: str,
        run_id: str,
        *,
        kind: str,
        payload: dict[str, Any],
        project_revision: int,
        trace_id: str | None = None,
        evidence_id: str | None = None,
    ) -> dict[str, Any]:
        return self._production_store.add_evidence(
            project_id,
            run_id,
            kind=kind,
            payload=payload,
            project_revision=project_revision,
            trace_id=trace_id,
            evidence_id=evidence_id,
        )

    def artifact_path(self, project_id: str, asset_id: str) -> Path:
        return self._production_store.artifact_path(project_id, asset_id)

    def _broadcast_project_event(
        self, project_id: str, run_id: str, event: dict[str, Any]
    ) -> None:
        with self._lock:
            runners = [
                runner
                for runner in self._runners.values()
                if runner.project_id == project_id and runner.run_id == run_id
            ]
        for runner in runners:
            runner._emit_event(dict(event))  # noqa: SLF001 - manager owns runners

    def close_session(self, session_id: str, *, remove_workdir: bool = False) -> None:
        with self._lock:
            runner = self._runners.pop(session_id, None)
        if runner is None:
            return
        try:
            runner.close()
        finally:
            SSE_REGISTRY.close(session_id)
            SSE_REGISTRY.unregister(session_id)
            if remove_workdir:
                self._remove_workdir(runner.output_dir)

    def cleanup_idle(self) -> list[str]:
        if self._idle_timeout_sec <= 0:
            return []
        now = time.time()
        sleeping: list[str] = []
        with self._lock:
            for sid, runner in self._runners.items():
                if runner.turn_in_progress:
                    continue
                if now - runner.last_used_at >= self._idle_timeout_sec:
                    sleeping.append(sid)
        # Idle sweep only lets the runtime sleep. The durable session and its
        # files never expire, and the next session route resumes this runner.
        # Deletion happens only via an explicit close_session / close_all call
        # that opts in with remove_workdir(s)=True.
        for sid in sleeping:
            self.close_session(sid)
        return sleeping

    def close_all(self, *, remove_workdirs: bool = False) -> None:
        with self._lock:
            session_ids = list(self._runners.keys())
        for sid in session_ids:
            self.close_session(sid, remove_workdir=remove_workdirs)
        self._stop_sweeper.set()

    def _sweep_loop(self) -> None:
        while not self._stop_sweeper.wait(self._sweep_interval_sec):
            self.cleanup_idle()

    def _remove_workdir(self, path: Path) -> None:
        try:
            resolved = Path(path).resolve()
            resolved.relative_to(self._workdirs_root.resolve())
        except Exception:
            return
        shutil.rmtree(resolved, ignore_errors=True)

    @property
    def output_root(self) -> Path:
        return self._output_root

    @property
    def sessions_root(self) -> Path:
        """Where per-session durable artifacts (meta.json, transcript.jsonl)
        live. Public so routes can serve transcripts of CLOSED sessions —
        outliving the runner is the whole point of the transcript."""
        return self._sessions_root

    @property
    def projects_root(self) -> Path:
        return self._projects_root

    @property
    def production_store(self) -> ProductionStore:
        return self._production_store


_SINGLETON_LOCK = threading.Lock()
_SINGLETON: SessionManager | None = None


def get_manager() -> SessionManager:
    global _SINGLETON
    with _SINGLETON_LOCK:
        if _SINGLETON is None:
            root = Path(os.environ.get("LUMERI_V3_OUTPUT_ROOT") or _DEFAULT_OUTPUT_ROOT)
            _SINGLETON = SessionManager(output_root=root)
        return _SINGLETON


__all__ = [
    "SessionLimitError",
    "SessionManager",
    "SessionRunner",
    "VerbGateError",
    "get_manager",
]
