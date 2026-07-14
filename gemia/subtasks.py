"""Multi-agent capability v1 — bounded sub-task fan-out (``spawn_subtasks``).

Implements Phase 1 of docs/multi-agent-plan.md: one host verb, ``spawn_subtasks``,
fans out 1-4 bounded child agents that work IN PARALLEL on independent goals and
return structured results. Each child is a lightweight ``SubtaskLoop`` — NOT a
second ``AgentLoopV3`` — that reuses the parent's streaming/dispatch primitives
but runs a host-fixed tool profile, cannot ask the user, cannot spawn further
children, and draws cost/time from the parent session's ``BudgetGuard`` via a
reserved slice (unspent returns on settlement).

Architecture (§3): children are ``asyncio`` tasks on the session's single event
loop — no threads, ever — so ``AssetRegistry`` thread-confinement, the loop-hop
edit discipline, and ``AskBridge`` future resolution all stay intact. A child
shares the parent's ``GeminiClientV3`` (stateless per call), ``AssetRegistry``,
``JobRegistry``, and ``ProjectHandle`` via a per-child ``ToolContext`` whose
``extra`` has ``ask_bridge`` stripped (elicit is structurally impossible) and
whose ``output_dir`` is a per-child subdir.

Protocol (§6): a child opens with exactly one ``subagent_start`` and closes with
exactly one ``subagent_result``. Child TOOL activity rides the EXISTING
``tool_exec_*`` kinds carrying an optional ``agent_id`` field (absent = the
root/parent loop). There is no ``subagent_progress`` kind and children never emit
``model_text_delta`` — child final text folds into the structured-result summary.

Budget (§5): the parent reserves a 20% floor for itself, splits the rest across
N children (``max_cost_usd`` can only clamp a child DOWN), gives each child its
own capped ``BudgetGuard``, and settles every reservation in a mandatory
``try/finally`` (unspent returns). The parent loop special-cases ``spawn_subtasks``
to commit ``actual_seconds=0.0`` so the batch wall-clock does not double-count on
top of the children's settled seconds.
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import TYPE_CHECKING, Any, Callable

from gemia.budget_guard import BudgetGuard, BudgetReservation
from gemia.errors import (
    GemiaError,
    RECOVERY_FIX_ARGS,
    RECOVERY_SWITCH_TOOL,
    RECOVERY_TRANSIENT_RETRY,
    ToolError,
)
from gemia.plan_mode import is_plan_safe, plan_gate_message
from gemia.tools import DISPATCHER, TOOL_SCHEMAS
from gemia.tool_outcome import classify_tool_exception, classify_tool_result
from gemia.tools._context import ProgressUpdate, ToolContext
from gemia.turn_ledger import MUTATION_TOOLS, tool_target_key

if TYPE_CHECKING:  # avoid an import cycle: agent_loop_v3 imports tools which... etc.
    from gemia.agent_loop_v3 import AgentLoopV3


# ── tool profiles (fixed frozensets, plan_mode-style — §4.2) ─────────────────
# Derived by reading dispatcher behavior (same doctrine as plan_mode.py), names
# cross-checked against gemia/tools/__init__.py DISPATCHER. P1 ships annotate +
# probe only; P2/P3 profiles are listed in the doc, not registered here.

PROFILE_ANNOTATE = frozenset({
    "probe_media", "analyze_media", "extract_frame", "search_library",
    "get_media_annotations", "annotate_media", "write_media_annotation",
})
PROFILE_PROBE = frozenset({
    "probe_media", "analyze_media", "search_library",
    "get_media_annotations", "get_timeline", "get_lumenframe", "get_safe_areas",
})

# Globally forbidden in every profile, regardless of what a profile lists. A
# schema subset alone is not a security boundary (a model can hallucinate a tool
# name), so the child dispatch path checks membership fail-closed against BOTH
# the active profile and this set.
FORBIDDEN_IN_ANY_PROFILE = frozenset({
    "spawn_subtasks", "elicit", "remember", "log_note", "save_skill",
    "file_delete", "run_shell", "build", "export", "project_export",
})

# tool_profile enum → frozenset. Kept in one place so the schema enum, the
# dispatcher, and the coverage test cannot drift.
PROFILES: dict[str, frozenset[str]] = {
    "annotate": PROFILE_ANNOTATE,
    "probe": PROFILE_PROBE,
}


# ── rails (§8) ───────────────────────────────────────────────────────────────

MAX_CHILDREN = 4                 # schema maxItems + host assert
CHILD_DEPTH = 1                  # children cannot spawn (depth 1)
DEFAULT_MAX_STEPS = 10           # child model-call cap
HARD_MAX_STEPS = 16
DEFAULT_DEADLINE_SEC = 240.0     # per-batch shared wall-clock deadline
HARD_DEADLINE_SEC = 480.0
PARENT_BUDGET_FLOOR = 0.20       # parent keeps 20% of remaining on each axis
_DOOM_LOOP_THRESHOLD = 3         # per-child, byte-identical (name,args) streak
_REPEATED_FAILURE_NUDGE_THRESHOLD = 5
_TRANSIENT_RETRY_NUDGE_THRESHOLD = 8
_PROGRESS_COALESCE_SEC = 1.0     # ≥1 s between child tool_exec_progress emits
_SUMMARY_CAP = 1200              # per-child summary char cap (§4.3)
_RESULT_CAP = 16_000             # whole tool_result byte cap (§4.3)

_VALID_STATUSES = {"ok", "error", "timeout", "cancelled", "needs_user"}


class SubtaskError(ToolError):
    """Raised by the spawn dispatcher for structural refusals (over-cap children,
    unknown profile, exhausted budget pool) so the model reads a typed refusal.

    A ``ToolError`` subclass so it carries ``code`` + ``recovery`` and the parent
    loop surfaces both in the tool_result and the ``tool_exec_error`` event."""

    def __init__(
        self, message: str, *, code: str = "E_SUBTASK", recovery: str = RECOVERY_FIX_ARGS
    ) -> None:
        super().__init__(message, code=code, recovery=recovery)


def _profile_tools(profile_name: str) -> frozenset[str]:
    tools = PROFILES.get(profile_name)
    if tools is None:
        raise SubtaskError(
            f"unknown tool_profile {profile_name!r}; valid: {sorted(PROFILES)}",
            code="E_SUBTASK_PROFILE",
            recovery=RECOVERY_FIX_ARGS,
        )
    return tools


def _child_tool_schemas(profile_tools: frozenset[str]) -> list[dict[str, Any]]:
    """The subset of TOOL_SCHEMAS whose names are in the profile — the child model
    physically cannot see out-of-profile verbs."""
    return [t for t in TOOL_SCHEMAS if t["function"]["name"] in profile_tools]


# ── the child loop ───────────────────────────────────────────────────────────


class SubtaskLoop:
    """A restricted AgentLoopV3-lite. Reuses only streaming + dispatch primitives.

    One instance per child within a single ``spawn_subtasks`` call. Never mutates
    the parent; reads ``plan_mode`` and the client from it. Its transcript, its
    ``BudgetGuard`` (capped at the reserved slice), and its ``ToolContext`` are
    all its own.
    """

    def __init__(
        self,
        *,
        agent_id: str,
        parent: "AgentLoopV3",
        call_id: str,
        goal: str,
        profile_name: str,
        guard: BudgetGuard,
        asset_ids: list[str] | None = None,
        max_steps: int = DEFAULT_MAX_STEPS,
        depth: int = CHILD_DEPTH,
    ) -> None:
        # Depth-1 invariant: a child is always depth 1 and has no spawn path;
        # asserting here makes an accidental nested-spawn wiring fail loudly.
        assert depth == CHILD_DEPTH, f"subtask depth must be {CHILD_DEPTH}, got {depth}"
        self.agent_id = agent_id
        self.parent = parent
        self.call_id = call_id
        self.goal = goal
        self.profile_name = profile_name
        self.profile_tools = _profile_tools(profile_name)
        self.guard = guard
        self.asset_ids = list(asset_ids or [])
        self.max_steps = max(1, min(int(max_steps), HARD_MAX_STEPS))

        self._messages: list[dict[str, Any]] = []
        self._final_text_parts: list[str] = []
        self._new_asset_ids: list[str] = []
        self._tool_fail_counts: dict[str, tuple[str, int]] = {}
        self._unresolved_failures: dict[str, str] = {}
        self._recent_calls: list[tuple[str, str]] = []
        self._last_progress_ts: dict[str, float] = {}
        self.steps = 0
        # True once a subagent_result has been emitted for this child (normal
        # completion path). The dispatcher's finally uses this to guarantee
        # exactly one terminal result per started child, even under cancellation.
        self._emitted_result = False

        # Per-child ToolContext: shares registry / jobs / project with the parent
        # (single-loop confinement makes that race-free), but with its own
        # output_dir, its own agent-scoped emit_progress, and ask_bridge REMOVED
        # so elicit is structurally impossible even if a profile bug let it in.
        child_output = self.parent.output_dir / "subtasks" / agent_id
        child_output.mkdir(parents=True, exist_ok=True)
        parent_extra = getattr(self.parent, "_tool_ctx").extra
        child_extra = {k: v for k, v in dict(parent_extra).items() if k != "ask_bridge"}
        self._ctx = ToolContext(
            session_id=self.parent.session_id,
            output_dir=child_output,
            registry=self.parent.registry,
            emit_progress=lambda _u: None,
            extra=child_extra,
            jobs=getattr(self.parent, "_tool_ctx").jobs,
            project=self.parent.project,
        )

    # ── child SSE emits (agent_id attached) ──────────────────────────────

    def _emit(self, event: dict[str, Any]) -> None:
        """Route a child event through the parent's emit sink with agent_id set."""
        event.setdefault("agent_id", self.agent_id)
        event.setdefault("call_id", self.call_id)
        self.parent._emit(event)

    def _make_progress_cb(self, tool_call_id: str, tool_name: str) -> Callable[[ProgressUpdate], None]:
        """Child progress callback, coalesced to ≥1 s per child so 4 verbose
        children cannot evict a disconnected client's replay window (§8)."""
        emit = self.parent._emit
        agent_id = self.agent_id
        call_id = self.call_id
        last = self._last_progress_ts

        def cb(update: ProgressUpdate) -> None:
            now = time.monotonic()
            # Always forward a terminal (100%) update; coalesce the rest.
            is_terminal = update.percent is not None and update.percent >= 100
            if not is_terminal and (now - last.get(agent_id, 0.0)) < _PROGRESS_COALESCE_SEC:
                return
            last[agent_id] = now
            event: dict[str, Any] = {
                "kind": "tool_exec_progress",
                "call_id": call_id,
                "agent_id": agent_id,
                "tool_name": tool_name,
                "tool_call_id": tool_call_id,
            }
            if update.percent is not None:
                event["percent"] = update.percent
            if update.message:
                event["message"] = update.message
            if update.eta_sec is not None:
                event["eta_seconds"] = update.eta_sec
            emit(event)

        return cb

    # ── transcript helpers (mirrors AgentLoopV3 shapes) ──────────────────

    def _system_prompt(self) -> str:
        tool_list = ", ".join(sorted(self.profile_tools))
        scope = (
            f"\nScoped assets: {', '.join(self.asset_ids)}." if self.asset_ids else ""
        )
        return (
            "You are a bounded Lumeri sub-agent working on ONE independent goal in "
            "parallel with sibling sub-agents. You run a restricted tool set and "
            "CANNOT ask the user, spawn further sub-agents, or edit the shared "
            "project document.\n"
            f"Available tools: {tool_list}.\n"
            f"Your goal:\n{self.goal}{scope}\n\n"
            "Work efficiently: call tools to accomplish the goal, then STOP with a "
            "short final text summary of what you found/did and any asset_ids you "
            "produced. Your final text is the ONLY thing the parent sees, so make "
            "it self-contained. If you need a human decision you cannot make, stop "
            "and say so plainly (the parent will decide whether to ask the user)."
        )

    def _append_tool_result(self, tool_call_id: str, payload: Any) -> None:
        content = payload if isinstance(payload, str) else json.dumps(
            payload, ensure_ascii=False, default=str
        )
        self._messages.append(
            {"role": "tool", "tool_call_id": tool_call_id, "content": content}
        )

    def _note_failure(self, name: str, code: str, *, limit: int) -> tuple[bool, int]:
        last_code, streak = self._tool_fail_counts.get(name, ("", 0))
        streak = streak + 1 if code == last_code else 1
        self._tool_fail_counts[name] = (code, streak)
        return streak >= limit, streak

    def _append_nudge(self, name: str, code: str, count: int) -> None:
        self._messages.append({
            "role": "user",
            "content": (
                f"Repeated tool failure: `{name}` failed with `{code}` {count} times "
                "in a row. Change arguments, switch tools, or stop and summarize the "
                "blocker in your final text."
            ),
        })

    @staticmethod
    def _operation_class(name: str) -> str:
        """Coarse semantic class used when correlating repaired failures.

        A successful read of an asset proves that the asset is readable; it
        does not prove that an earlier annotation/write against that same
        asset succeeded.  Keeping reads and mutations in separate classes
        still permits legitimate same-target alternatives inside each class
        (for example, ``probe_media`` followed by ``analyze_media``).
        """
        return "mutation" if name in MUTATION_TOOLS else "read"

    @staticmethod
    def _failure_key(name: str, args: dict[str, Any] | None = None) -> str:
        target = tool_target_key(args)
        if target:
            operation_class = SubtaskLoop._operation_class(name)
            return f"target:{operation_class}:{target}"
        return f"tool:{name}"

    def _record_unresolved(
        self, name: str, code: str, args: dict[str, Any] | None = None
    ) -> None:
        self._unresolved_failures[self._failure_key(name, args)] = code

    def _resolve_unresolved(
        self, name: str, args: dict[str, Any] | None = None
    ) -> None:
        self._unresolved_failures.pop(self._failure_key(name, args), None)
        # A successful corrected call also resolves a prior parse failure for
        # that tool, while target-scoped failures on other assets remain.
        self._unresolved_failures.pop(f"tool:{name}", None)

    @staticmethod
    def _is_doom_loop(recent: list[tuple[str, str]]) -> bool:
        if len(recent) < _DOOM_LOOP_THRESHOLD:
            return False
        window = recent[-_DOOM_LOOP_THRESHOLD:]
        return len(set(window)) == 1

    # ── the run ──────────────────────────────────────────────────────────

    async def run(self) -> dict[str, Any]:
        """Drive the child to completion and return its structured result dict.

        Never raises for in-child failures (doom loop, budget, plan block) —
        those fold into ``status``/``summary``. asyncio.CancelledError DOES
        propagate (the parent's finally settles + records the terminal status).
        """
        self._emit({
            "kind": "subagent_start",
            "goal": self.goal,
            "tool_profile": self.profile_name,
            "budget": {"max_usd": self.guard.max_usd, "max_seconds": self.guard.max_seconds},
        })

        self._messages.append({"role": "system", "content": self._system_prompt()})
        self._messages.append({"role": "user", "content": self.goal})

        status = "ok"
        child_schemas = _child_tool_schemas(self.profile_tools)

        while self.steps < self.max_steps:
            self.steps += 1
            accum_text: list[str] = []
            tool_calls: list[dict[str, Any]] = []
            by_index: dict[int, dict[str, Any]] = {}

            try:
                async for delta in self.parent.client.stream_turn(
                    self._messages, tools=child_schemas
                ):
                    kind = delta.get("kind")
                    if kind == "text_delta":
                        # Child text is NEVER emitted to SSE (§D6) — accumulate only.
                        accum_text.append(str(delta.get("text", "")))
                    elif kind == "tool_call_start":
                        idx = int(delta["index"])
                        tc = {
                            "index": idx,
                            "id": str(delta.get("id") or f"{self.agent_id}_call_{idx}"),
                            "name": str(delta.get("name")),
                            "args_buf": [],
                            "extra_content": delta.get("extra_content"),
                        }
                        by_index[idx] = tc
                    elif kind == "tool_call_args_delta":
                        tc = by_index.get(int(delta["index"]))
                        if tc is not None:
                            tc["args_buf"].append(str(delta.get("delta", "")))
                    elif kind == "tool_call_extra":
                        tc = by_index.get(int(delta["index"]))
                        if tc is not None:
                            tc["extra_content"] = delta.get("extra_content")
                    elif kind == "error":
                        # A model-stream error ends the child in error status.
                        self._final_text_parts.append(
                            f"[stream error: {delta.get('error')}]"
                        )
                        status = "error"
                        break
            except asyncio.CancelledError:
                raise
            if status == "error":
                break

            if accum_text:
                self._final_text_parts.append("".join(accum_text))

            tool_calls = [by_index[k] for k in sorted(by_index)]

            # Persist the assistant message (text + tool_calls) into the child
            # transcript so the follow-up model call has context.
            assistant_msg: dict[str, Any] = {
                "role": "assistant",
                "content": "".join(accum_text) or None,
            }
            if tool_calls:
                assistant_msg["tool_calls"] = [
                    _child_tool_call_message(tc) for tc in tool_calls
                ]
            self._messages.append(assistant_msg)

            if not tool_calls:
                # A returned/raised tool failure is not repaired by merely
                # stopping in prose. Only a later successful call of that same
                # tool clears its unresolved entry.
                if self._unresolved_failures:
                    status = "error"
                break

            for tc in tool_calls:
                await self._dispatch_child_call(tc)
                if self._is_doom_loop(self._recent_calls):
                    self._final_text_parts.append(
                        f"[stopped: repeated the same call {_DOOM_LOOP_THRESHOLD}x with "
                        "no progress]"
                    )
                    status = "error"
                    break
            if status == "error":
                break
        else:
            # Ran out of steps.
            self._final_text_parts.append(
                f"[reached the {self.max_steps}-step cap before finishing]"
            )
            status = "error"

        summary = "\n".join(p for p in self._final_text_parts if p).strip()
        summary = summary[:_SUMMARY_CAP]
        snap = self.guard.snapshot()
        result = {
            "agent_id": self.agent_id,
            "status": status,
            "summary": summary or "(no summary)",
            "asset_ids": list(self._new_asset_ids),
            "data": {},
            "steps": self.steps,
            "spent_usd": snap["spent_usd"],
            "spent_seconds": snap["spent_seconds"],
        }
        self._emit_result(result, elapsed_seconds=snap.get("elapsed_seconds", 0.0))
        return result

    def _emit_result(self, result: dict[str, Any], *, elapsed_seconds: float) -> None:
        if self._emitted_result:
            return
        self._emitted_result = True
        self._emit({
            "kind": "subagent_result",
            "status": result["status"],
            "summary": result["summary"],
            "asset_ids": result["asset_ids"],
            "steps": result["steps"],
            "spent_usd": result["spent_usd"],
            "spent_seconds": result["spent_seconds"],
            "elapsed_seconds": round(float(elapsed_seconds), 2),
        })

    async def _dispatch_child_call(self, tc: dict[str, Any]) -> None:
        """Dispatch one child tool call with the full per-child rail stack:
        fail-closed profile check, live plan-mode re-read, budget gate, then the
        real dispatch through the shared DISPATCHER."""
        tool_call_id = tc["id"]
        name = tc["name"]
        raw_args = "".join(tc["args_buf"])
        parsed_args, parse_error = _parse_child_args(raw_args)

        # Record for the doom-loop guard (byte-identical name+args = pure echo).
        self._recent_calls.append((name, raw_args))

        if parse_error is not None:
            self._append_tool_result(tool_call_id, {
                "error": "arguments were not a valid JSON object",
                "error_code": "E_BAD_ARG",
                "recovery": RECOVERY_FIX_ARGS,
                "parse_error": parse_error,
            })
            self._maybe_nudge(name, "E_BAD_ARG")
            self._record_unresolved(name, "E_BAD_ARG")
            return

        # Fail-closed profile enforcement: the schema subset is NOT a boundary
        # (a model can hallucinate a tool name), so gate every dispatch against
        # BOTH the active profile and the global forbidden set.
        if name not in self.profile_tools or name in FORBIDDEN_IN_ANY_PROFILE:
            self._append_tool_result(tool_call_id, {
                "error": (
                    f"'{name}' is not available in the '{self.profile_name}' "
                    "sub-agent profile"
                ),
                "error_code": "E_SUBTASK_PROFILE",
                "recovery": RECOVERY_SWITCH_TOOL,
            })
            self._maybe_nudge(name, "E_SUBTASK_PROFILE")
            self._record_unresolved(name, "E_SUBTASK_PROFILE", parsed_args)
            return

        # Plan-mode inheritance (§7.2): re-read the PARENT's LIVE flag per
        # dispatch so a mid-batch toggle clamps children within one dispatch.
        # Child plan-blocks count toward the CHILD's own failure state only,
        # never the parent's plan_gate hard-stop counter.
        if self.parent.plan_mode and not is_plan_safe(name):
            gate_msg = plan_gate_message(name)
            self._append_tool_result(tool_call_id, {
                "blocked_by_plan_mode": True,
                "error_code": "E_PLAN_MODE",
                "message": gate_msg,
            })
            self._maybe_nudge(name, "E_PLAN_MODE")
            self._record_unresolved(name, "E_PLAN_MODE", parsed_args)
            return

        # Budget gate against the CHILD's own capped guard — a child structurally
        # cannot exceed its slice because its own guard gates it.
        decision = self.guard.check(name)
        if not decision.ok:
            self._append_tool_result(tool_call_id, {
                "blocked_by_budget": True,
                "approval_cannot_override": True,
                "error_code": "E_BUDGET",
                "reason": decision.reason,
                "estimated_cost_usd": decision.estimated_cost_usd,
                "estimated_eta_sec": decision.estimated_eta_sec,
            })
            self._maybe_nudge(name, "E_BUDGET")
            self._record_unresolved(name, "E_BUDGET", parsed_args)
            return

        # Real dispatch. Child tool activity rides the EXISTING tool_exec_*
        # kinds with agent_id attached (§6.2).
        pre_ids = {r.asset_id for r in self.parent.registry.list_records()}
        self._emit({
            "kind": "tool_exec_start",
            "tool_name": name,
            "tool_call_id": tool_call_id,
            "est_cost_usd": decision.estimated_cost_usd,
            "eta_seconds": decision.estimated_eta_sec,
        })
        self._ctx.emit_progress = self._make_progress_cb(tool_call_id, name)

        start_ts = time.monotonic()
        try:
            result = await DISPATCHER[name](parsed_args, self._ctx)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — surface, never swallow
            elapsed = time.monotonic() - start_ts
            self.guard.commit(name, actual_seconds=elapsed)
            outcome = classify_tool_exception(exc)
            err_payload = outcome.error_payload(tool_name=name)
            err_code = str(outcome.error_code or "E_TOOL_FAILED")
            recovery = outcome.recovery
            self._emit({
                "kind": "tool_exec_error",
                "tool_name": name,
                "tool_call_id": tool_call_id,
                "elapsed_seconds": elapsed,
                **err_payload,
            })
            self._append_tool_result(tool_call_id, {**err_payload, "tool_name": name})
            limit = (
                _TRANSIENT_RETRY_NUDGE_THRESHOLD
                if recovery == RECOVERY_TRANSIENT_RETRY
                else _REPEATED_FAILURE_NUDGE_THRESHOLD
            )
            should_nudge, streak = self._note_failure(name, err_code, limit=limit)
            if should_nudge:
                self._append_nudge(name, err_code, streak)
            self._record_unresolved(name, err_code, parsed_args)
            return

        elapsed = time.monotonic() - start_ts
        self.guard.commit(name, actual_seconds=elapsed)
        outcome = classify_tool_result(result)
        if outcome.is_failure:
            err_payload = outcome.error_payload(tool_name=name)
            err_code = str(outcome.error_code or "E_TOOL_FAILED")
            self._emit({
                "kind": "tool_exec_error",
                "tool_name": name,
                "tool_call_id": tool_call_id,
                "elapsed_seconds": elapsed,
                **err_payload,
            })
            self._append_tool_result(tool_call_id, {**err_payload, "tool_name": name})
            limit = (
                _TRANSIENT_RETRY_NUDGE_THRESHOLD
                if outcome.recovery == RECOVERY_TRANSIENT_RETRY
                else _REPEATED_FAILURE_NUDGE_THRESHOLD
            )
            should_nudge, streak = self._note_failure(name, err_code, limit=limit)
            if should_nudge:
                self._append_nudge(name, err_code, streak)
            self._record_unresolved(name, err_code, parsed_args)
            return

        # Only a terminal success proves that this child action completed.
        # ``pending``/``noop``/``partial`` are honest non-failures, but treating
        # them like success would let the child clear an earlier failure and
        # stop in prose with ``status=ok``.  Keep an unresolved marker instead;
        # a later real success on the same target will clear it through the
        # normal retry path below.
        if outcome.state == "success":
            self._tool_fail_counts.pop(name, None)
            self._resolve_unresolved(name, parsed_args)
        else:
            key = self._failure_key(name, parsed_args)
            self._unresolved_failures.setdefault(
                key, f"E_TOOL_{outcome.state.upper()}"
            )

        # New assets this child registered (shared registry; loop-confined ids).
        for r in self.parent.registry.list_records():
            if r.asset_id not in pre_ids and r.asset_id not in self._new_asset_ids:
                self._new_asset_ids.append(r.asset_id)

        model_result = {
            k: v for k, v in result.items()
            if k not in {"thumbnail_path", "thumbnail_for_next_message"}
        } if isinstance(result, dict) else {"result": result}
        event_result = dict(model_result)
        produced_id = model_result.get("asset_id") if isinstance(model_result, dict) else None
        if produced_id and self.parent.registry.contains(str(produced_id)):
            event_result["preview_uri"] = str(
                self.parent.registry.get(str(produced_id)).path
            )
        self._emit({
            "kind": "tool_exec_result",
            "tool_name": name,
            "tool_call_id": tool_call_id,
            "result": event_result,
            "elapsed_seconds": elapsed,
        })
        self._append_tool_result(tool_call_id, model_result)

    def _maybe_nudge(self, name: str, code: str) -> None:
        should_nudge, streak = self._note_failure(
            name, code, limit=_REPEATED_FAILURE_NUDGE_THRESHOLD
        )
        if should_nudge:
            self._append_nudge(name, code, streak)


# ── streaming helpers ────────────────────────────────────────────────────────


def _parse_child_args(raw: str) -> tuple[dict[str, Any] | None, str | None]:
    text = (raw or "").strip()
    if not text:
        return {}, None
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        return None, f"JSONDecodeError: {exc}"
    if not isinstance(value, dict):
        return None, f"tool args must be a JSON object, got {type(value).__name__}"
    return value, None


def _child_tool_call_message(tc: dict[str, Any]) -> dict[str, Any]:
    message = {
        "id": tc["id"],
        "type": "function",
        "function": {"name": tc["name"], "arguments": "".join(tc["args_buf"])},
    }
    if tc.get("extra_content") is not None:
        message["extra_content"] = tc["extra_content"]
    return message


# ── the spawn_subtasks host verb ─────────────────────────────────────────────


def _slice_budget(
    guard: BudgetGuard, n: int
) -> tuple[float, float, str | None]:
    """Return (pool_usd, pool_sec, refusal_reason). The pool is the remaining
    budget minus the parent's 20% floor, to be split across N children."""
    remaining_usd = guard.max_usd - guard.spent_usd
    remaining_sec = guard.max_seconds - guard.spent_seconds
    parent_floor_usd = PARENT_BUDGET_FLOOR * guard.max_usd
    parent_floor_sec = PARENT_BUDGET_FLOOR * guard.max_seconds
    pool_usd = remaining_usd - parent_floor_usd
    pool_sec = remaining_sec - parent_floor_sec
    if pool_sec <= 0:
        return 0.0, 0.0, (
            "not enough session time budget remains to fan out sub-agents while "
            "keeping the parent's 20% floor to integrate results"
        )
    if pool_usd < 0:
        # A zero-cost profile still runs on time alone; only refuse if the money
        # floor is already breached (negative remaining).
        return 0.0, 0.0, (
            "not enough session cost budget remains to fan out sub-agents while "
            "keeping the parent's 20% floor"
        )
    return pool_usd, pool_sec, None


async def dispatch(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """``spawn_subtasks`` host verb. Slices the parent budget, fans children out
    as asyncio tasks on the session loop under a shared deadline, and settles
    every reservation in a mandatory try/finally (unspent returns)."""
    parent: "AgentLoopV3" | None = ctx.extra.get("agent_loop")
    if parent is None:
        raise SubtaskError(
            "spawn_subtasks is only available inside a live agent loop",
            code="E_SUBTASK", recovery=RECOVERY_SWITCH_TOOL,
        )
    call_id: str = str(ctx.extra.get("call_id") or "spawn")

    subtasks = args.get("subtasks")
    if not isinstance(subtasks, list) or not subtasks:
        raise SubtaskError(
            "subtasks must be a non-empty array", code="E_SUBTASK", recovery=RECOVERY_FIX_ARGS
        )
    n = len(subtasks)
    if n > MAX_CHILDREN:
        raise SubtaskError(
            f"at most {MAX_CHILDREN} sub-agents per spawn_subtasks call (got {n})",
            code="E_SUBTASK", recovery=RECOVERY_FIX_ARGS,
        )

    # Validate specs and profiles UP FRONT (fail before reserving anything).
    specs: list[dict[str, Any]] = []
    for i, st in enumerate(subtasks):
        if not isinstance(st, dict):
            raise SubtaskError(
                f"subtasks[{i}] must be an object", code="E_SUBTASK", recovery=RECOVERY_FIX_ARGS
            )
        goal = st.get("goal")
        profile_name = st.get("tool_profile")
        if not isinstance(goal, str) or not goal.strip():
            raise SubtaskError(
                f"subtasks[{i}].goal is required", code="E_SUBTASK", recovery=RECOVERY_FIX_ARGS
            )
        _profile_tools(str(profile_name))  # raises E_SUBTASK_PROFILE if unknown
        specs.append(st)

    deadline = float(args.get("deadline_sec") or DEFAULT_DEADLINE_SEC)
    deadline = max(1.0, min(deadline, HARD_DEADLINE_SEC))

    guard = parent.budget
    pool_usd, pool_sec, refusal = _slice_budget(guard, n)
    if refusal is not None:
        # A budget refusal is not fixable by re-args; the model should switch
        # tactic (do the work sequentially) — mirror the budget-gate affordance.
        raise SubtaskError(refusal, code="E_BUDGET", recovery=RECOVERY_SWITCH_TOOL)

    slice_usd_base = pool_usd / n
    slice_sec = pool_sec / n

    children: list[SubtaskLoop] = []
    reservations: list[BudgetReservation] = []
    tasks: list[asyncio.Task] = []
    # agent_id → result; a started child ALWAYS gets a terminal result (§accept 3).
    results_by_agent: dict[str, dict[str, Any]] = {}

    try:
        for i, st in enumerate(specs):
            agent_id = f"sub_{i + 1}"
            requested = st.get("max_cost_usd")
            slice_usd = slice_usd_base
            if isinstance(requested, (int, float)) and requested >= 0:
                slice_usd = min(float(requested), slice_usd_base)

            decision, res = guard.reserve_amount(
                f"spawn_subtasks:{agent_id}", usd=slice_usd, seconds=slice_sec
            )
            if res is None:
                # Pool exhausted mid-loop (siblings already reserved). Record a
                # synthetic terminal result and stop launching more.
                results_by_agent[agent_id] = {
                    "agent_id": agent_id, "status": "error",
                    "summary": f"budget slice refused: {decision.reason}",
                    "asset_ids": [], "data": {}, "steps": 0,
                    "spent_usd": 0.0, "spent_seconds": 0.0,
                }
                break
            reservations.append(res)

            child = SubtaskLoop(
                agent_id=agent_id,
                parent=parent,
                call_id=call_id,
                goal=str(st["goal"]),
                profile_name=str(st["tool_profile"]),
                guard=BudgetGuard(max_usd=slice_usd, max_seconds=slice_sec),
                asset_ids=[str(a) for a in (st.get("asset_ids") or [])],
                max_steps=DEFAULT_MAX_STEPS,
            )
            children.append(child)
            tasks.append(asyncio.ensure_future(_run_child(child, results_by_agent)))

        if tasks:
            # Shared batch deadline: stragglers are cancelled → status "timeout".
            await asyncio.wait(tasks, timeout=deadline)

    finally:
        # MANDATORY: create_task children are NOT auto-cancelled when this
        # coroutine is cancelled, so cancel every outstanding task, drain it,
        # and settle EVERY reservation with spent-so-far (unspent returns).
        for t in tasks:
            if not t.done():
                t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for child, res in zip(children, reservations):
            snap = child.guard.snapshot()
            guard.commit_reserved(
                res,
                actual_usd=snap["spent_usd"],
                actual_seconds=snap["spent_seconds"],
            )
            # Ensure every STARTED child has EXACTLY ONE terminal subagent_result.
            # A child cancelled (deadline / parent error) before run() emitted its
            # own result gets a synthetic terminal record + emit here. The child's
            # _emitted_result flag makes _emit_result idempotent, so a child that
            # already emitted normally is not double-closed (§accept 3).
            terminal = results_by_agent.get(child.agent_id)
            if terminal is None:
                terminal = {
                    "agent_id": child.agent_id, "status": "timeout",
                    "summary": "cancelled before completion",
                    "asset_ids": list(child._new_asset_ids),
                    "data": {}, "steps": child.steps,
                    "spent_usd": snap["spent_usd"], "spent_seconds": snap["spent_seconds"],
                }
                results_by_agent[child.agent_id] = terminal
            try:
                child._emit_result(terminal, elapsed_seconds=snap.get("elapsed_seconds", 0.0))
            except Exception:  # noqa: BLE001 — cleanup emit must never raise out of finally
                pass

    # Ordered results (sub_1, sub_2, …) capped at 16 KB total.
    ordered = [
        results_by_agent[f"sub_{i + 1}"]
        for i in range(n)
        if f"sub_{i + 1}" in results_by_agent
    ]
    ordered = _cap_results(ordered)
    payload = {
        "summary": _batch_summary(ordered),
        "subtasks": ordered,
        "count": len(ordered),
    }
    failed = [r for r in ordered if r.get("status") != "ok"]
    if failed:
        payload.update(
            {
                "status": "failed",
                "error": f"{len(failed)} subtask(s) did not complete successfully",
                "error_code": "E_SUBTASK_FAILED",
            }
        )
    return payload


async def _run_child(
    child: SubtaskLoop, sink: dict[str, dict[str, Any]]
) -> None:
    """Run a child; on cancellation record a terminal 'timeout' result so the
    finally settlement never overwrites a real one and a subagent_result exists.

    The result is written to ``sink`` (not returned) so the finally block can see
    completions even for tasks it later gathers after cancelling."""
    try:
        result = await child.run()
        sink[child.agent_id] = result
    except asyncio.CancelledError:
        # Straggler past the deadline or parent-error unwind. Record a terminal
        # timeout status here; the subagent_result emit + settlement happen in
        # the dispatcher's finally (which owns the reservation).
        snap = child.guard.snapshot()
        sink.setdefault(child.agent_id, {
            "agent_id": child.agent_id, "status": "timeout",
            "summary": "sub-agent cancelled (deadline or parent error)",
            "asset_ids": list(child._new_asset_ids), "data": {},
            "steps": child.steps,
            "spent_usd": snap["spent_usd"], "spent_seconds": snap["spent_seconds"],
        })
        raise


def _cap_results(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Truncate summaries round-robin until the whole payload is ≤16 KB (§4.3)."""
    def size(rs: list[dict[str, Any]]) -> int:
        return len(json.dumps(rs, ensure_ascii=False, default=str).encode("utf-8"))

    if size(results) <= _RESULT_CAP:
        return results
    # Round-robin shave 200 chars off the longest summary until we fit (or all
    # summaries are minimal).
    guard = 0
    while size(results) > _RESULT_CAP and guard < 10_000:
        guard += 1
        longest = max(results, key=lambda r: len(r.get("summary", "")))
        s = longest.get("summary", "")
        if len(s) <= 40:
            break
        longest["summary"] = s[: max(40, len(s) - 200)] + "…"
    return results


def _batch_summary(results: list[dict[str, Any]]) -> str:
    by_status: dict[str, int] = {}
    for r in results:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
    parts = [f"{v} {k}" for k, v in sorted(by_status.items())]
    total_usd = round(sum(float(r.get("spent_usd", 0.0)) for r in results), 4)
    total_sec = round(sum(float(r.get("spent_seconds", 0.0)) for r in results), 2)
    return (
        f"{len(results)} sub-agent(s): {', '.join(parts)}; "
        f"spent ${total_usd} / {total_sec}s total"
    )


__all__ = [
    "PROFILE_ANNOTATE",
    "PROFILE_PROBE",
    "PROFILES",
    "FORBIDDEN_IN_ANY_PROFILE",
    "MAX_CHILDREN",
    "DEFAULT_MAX_STEPS",
    "HARD_MAX_STEPS",
    "DEFAULT_DEADLINE_SEC",
    "HARD_DEADLINE_SEC",
    "SubtaskLoop",
    "SubtaskError",
    "dispatch",
]
