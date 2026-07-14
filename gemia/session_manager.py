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
import json
import os
import shutil
import threading
import time
import uuid
from concurrent.futures import TimeoutError as FuturesTimeoutError
from pathlib import Path
from typing import Any

from gemia.agent_loop_v3 import AgentLoopV3
from gemia.tools._context import ProgressCallback
from gemia.transport.sse import REGISTRY as SSE_REGISTRY


_DEFAULT_OUTPUT_ROOT = Path("/tmp/lumeri-v3")
_DEFAULT_MAX_SESSIONS = 20
_DEFAULT_IDLE_TIMEOUT_SEC = 2 * 60 * 60
_DEFAULT_SWEEP_INTERVAL_SEC = 60


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


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    try:
        value = int(os.environ.get(name) or default)
    except ValueError:
        return default
    return value if value >= minimum else default


class SessionRunner:
    """Owns one AgentLoopV3 inside a dedicated thread + asyncio loop."""

    def __init__(
        self,
        *,
        session_id: str,
        output_dir: Path,
        sessions_root: Path,
        account_id: str | None = None,
    ) -> None:
        self.session_id = session_id
        self.output_dir = Path(output_dir)
        self.sessions_root = Path(sessions_root)
        self.account_id = str(account_id or "").strip()

        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._state_lock = threading.Lock()
        self._turn_in_progress = False
        now = time.time()
        self.created_at = now
        self.last_used_at = now

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
        self._thread = threading.Thread(
            target=self._run_loop,
            name=f"lumeri-v3-{session_id}",
            daemon=True,
        )
        self._thread.start()
        self._ready.wait()

        fut = asyncio.run_coroutine_threadsafe(self._create_agent(), self._loop)
        self.agent: AgentLoopV3 = fut.result(timeout=20)

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
        return AgentLoopV3(
            session_id=self.session_id,
            output_dir=self.output_dir,
            sessions_root=self.sessions_root,
            emit_event=self._emit_event,
            extra={"account_id": self.account_id} if self.account_id else None,
        )

    def _emit_event(self, event: dict[str, Any]) -> None:
        """Agent event sink: durable transcript first, then the SSE fan-out.

        The transcript write must never break the loop — on the first failure
        it disables itself for the session (one warning path, no spam) and
        events keep flowing to SSE.
        """
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

        with self._state_lock:
            if self._turn_in_progress:
                return False
            self._turn_in_progress = True
            self.last_used_at = time.time()

        async def _run() -> None:
            try:
                await self.agent.run_turn(message)
            finally:
                with self._state_lock:
                    self._turn_in_progress = False
                    self.last_used_at = time.time()

        asyncio.run_coroutine_threadsafe(_run(), self._loop)
        return True

    def run_project_edit(self, fn, *, timeout: float = 30.0) -> Any:
        """Run a project mutation on the session's event loop and return its
        result (exceptions propagate unchanged).

        /timeline/op and undo used to mutate ProjectStore straight from HTTP
        handler threads while agent verbs mutated it from this loop — the
        per-project lock makes that data-safe, but hopping user edits onto the
        loop also keeps their ``timeline_op`` SSE emits ordered with the
        in-flight turn's event stream (a user edit can no longer interleave
        inside one verb's start/result pair). User edits execute at the turn's
        await boundaries; a CPU-bound stretch longer than ``timeout`` raises
        ``concurrent.futures.TimeoutError`` (the edit may still apply late —
        callers should say so, not claim failure).
        """
        self.touch()
        if self._loop.is_closed():
            raise RuntimeError("session is closed")

        async def _call() -> Any:
            return fn()

        fut = asyncio.run_coroutine_threadsafe(_call(), self._loop)
        return fut.result(timeout=timeout)

    def run_verb(
        self,
        tool_name: str,
        args: dict[str, Any],
        *,
        emit_progress: ProgressCallback | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Dispatch ONE internal verb on the session loop with the same gates
        as the agent loop (docs/mcp-interface-plan.md §2.6, D7).

        This is the single MCP execution choke point. It applies, in order:

          1. Membership — ``tool_name`` must be in ``MCP_TOOLSET`` ∩
             ``DISPATCHER``. ``run_verb`` is not a general RPC hatch; excluded
             verbs stay excluded even if called directly.
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
             ``tool_exec_error`` with one additive ``origin: "mcp"`` field and
             a synthetic ``call_id`` (``mcp-<uuid8>``). Existing kinds only ⇒
             zero contract change; the durable transcript picks them up free.

        Returns the dispatcher's result dict. Raises ``VerbGateError`` for a
        gate refusal, or the dispatcher's own exception unchanged.
        """
        from gemia.mcp.toolset import MCP_READ_ONLY, MCP_TOOLSET
        from gemia.plan_mode import is_plan_safe, plan_gate_message
        from gemia.tool_outcome import classify_tool_result
        from gemia.tools import DISPATCHER

        self.touch()
        if self._loop.is_closed():
            raise RuntimeError("session is closed")

        call_id = f"mcp-{uuid.uuid4().hex[:8]}"

        # 1. Membership: curated surface ∩ real dispatch table.
        if tool_name not in MCP_TOOLSET or tool_name not in DISPATCHER:
            raise VerbGateError(
                "E_TOOL",
                f"unknown or excluded MCP tool: {tool_name}",
                {
                    "error": f"unknown or excluded MCP tool: {tool_name}",
                    "error_code": "E_TOOL",
                    "tool_name": tool_name,
                },
            )

        is_read_only = tool_name in MCP_READ_ONLY

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
                    "tool_name": tool_name,
                    "message": msg,
                    "origin": "mcp",
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
                    "tool_name": tool_name,
                    "reason": decision.reason,
                    "alternatives": decision.alternatives,
                    "estimated_cost_usd": decision.estimated_cost_usd,
                    "estimated_eta_sec": decision.estimated_eta_sec,
                    "origin": "mcp",
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

        # 5. Dispatch on the session loop with a shallow-copied ctx.
        def _progress_cb(update: Any) -> None:
            # SSE mirror of progress (additive origin), then forward to the
            # MCP progress callback (best-effort, exactly like the SSE path).
            try:
                event: dict[str, Any] = {
                    "kind": "tool_exec_progress",
                    "call_id": call_id,
                    "tool_name": tool_name,
                    "origin": "mcp",
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
            return await DISPATCHER[tool_name](dict(args), ctx)

        self._emit_event(
            {
                "kind": "tool_exec_start",
                "call_id": call_id,
                "tool_name": tool_name,
                "est_cost_usd": decision.estimated_cost_usd,
                "eta_seconds": decision.estimated_eta_sec,
                "origin": "mcp",
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
                    "tool_name": tool_name,
                    "error": (
                        "verb is queued behind a long-running step and has not "
                        "completed yet — re-read the timeline/session to see "
                        "whether it landed"
                    ),
                    "error_code": "E_BUSY",
                    "origin": "mcp",
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
                    "tool_name": tool_name,
                    "elapsed_seconds": elapsed,
                    "origin": "mcp",
                    **err_payload,
                }
            )
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
                    "tool_name": tool_name,
                    "elapsed_seconds": elapsed,
                    "origin": "mcp",
                    **err_payload,
                }
            )
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
                "tool_name": tool_name,
                "elapsed_seconds": elapsed,
                "result": event_result,
                "origin": "mcp",
            }
        )
        return result

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

    def list_assets(self) -> list[dict[str, Any]]:
        self.touch()
        records = self.agent.registry.list_records()
        return [
            {
                "asset_id": r.asset_id,
                "kind": r.kind,
                "summary": r.summary,
                "created_at": r.created_at,
                "lineage": list(r.lineage),
            }
            for r in records
        ]

    def close(self) -> None:
        if self._loop.is_closed():
            return
        try:
            fut = asyncio.run_coroutine_threadsafe(self._cancel_pending(), self._loop)
            fut.result(timeout=5)
        except Exception:
            pass
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
        cleanup_workdirs: bool | None = None,
    ) -> None:
        self._cleanup_workdirs = (
            cleanup_workdirs
            if cleanup_workdirs is not None
            else os.environ.get("LUMERI_V3_KEEP_CLOSED_WORKDIRS") not in {"1", "true", "TRUE"}
        )
        self._output_root = Path(output_root).expanduser().resolve()
        self._output_root.mkdir(parents=True, exist_ok=True)
        self._sessions_root = self._output_root / "sessions"
        self._workdirs_root = self._output_root / "workdirs"
        self._workdirs_root.mkdir(parents=True, exist_ok=True)
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

    def create_session(self, *, account_id: str | None = None) -> SessionRunner:
        self.cleanup_idle()
        session_id = f"v3-{uuid.uuid4().hex[:12]}"
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
            SSE_REGISTRY.register(session_id)
            registered = True
            runner = SessionRunner(
                session_id=session_id,
                output_dir=self._workdirs_root / session_id,
                sessions_root=self._sessions_root,
                account_id=account_id,
            )
            created = True
        except Exception:
            if runner is not None:
                runner.close()
            if registered:
                SSE_REGISTRY.close(session_id)
                SSE_REGISTRY.unregister(session_id)
            raise
        finally:
            with self._lock:
                self._creating_sessions -= 1
                if created and runner is not None:
                    self._runners[session_id] = runner
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
        expired: list[str] = []
        with self._lock:
            for sid, runner in self._runners.items():
                if runner.turn_in_progress:
                    continue
                if now - runner.last_used_at >= self._idle_timeout_sec:
                    expired.append(sid)
        # Idle sweep only frees the runner; workdir files are user data and
        # must survive — deletion happens only via an explicit close_session
        # / close_all call that opts in with remove_workdir(s)=True.
        for sid in expired:
            self.close_session(sid)
        return expired

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
