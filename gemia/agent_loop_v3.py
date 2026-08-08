"""Lumeri v3 agent loop.

Contract — what this loop is and what it is NOT:

  - It streams from the model through the v3 client (any of the supported
    providers — Gemini, Claude, GPT, …) and forwards every real chunk to the
    SSE transport. ``model_text_delta`` events ONLY come from real model
    text chunks. A narrowly validated model-authored activity label may ride
    an existing tool-ready event; the host never fabricates status narration
    of its own. If the model is silent, the user-facing stream stays silent.

  - It accumulates function-calling tool_call args across stream
    chunks, then dispatches each call via ``gemia.tools.DISPATCHER``.
    Errors raised by a dispatcher are caught here, surfaced as
    ``tool_exec_error`` events, fed back to the model as a tool_result,
    and the loop continues. We do not swallow errors — every except
    block emits an event and appends a structured tool_result for the
    model to read.

  - It does NOT apply a generic cap to the total number of tool steps per turn.
    A research or see→modify→rerun build loop legitimately needs many steps.
    Every failed tool call immediately asks the model to re-evaluate whether
    the current direction still has evidence behind it. The host does not
    hard-stop an approach after an arbitrary number of failed or non-mutating
    rounds; the model may retry only when the structured error justifies that,
    otherwise it must change direction or report the blocker honestly. Real
    cost/time remain bounded by ``BudgetGuard`` ($ + execution seconds).
      * ``visual_inspections_this_turn``   — capped at
        ``max_visual_inspections`` (incremented ONLY when an
        ``analyze_media`` call actually produces a thumbnail for the
        next user message). Independent of failure-direction checks.

  - It implements Plan-B visual feedback: when a dispatcher returns
    ``thumbnail_for_next_message=True``, the loop appends a multimodal
    user message with the thumbnail image_url before the next model
    call. This path is ONLY triggered by a dispatcher-flagged result,
    which today only ``analyze_media`` produces. There is no keyword
    detection. Mid-turn, the host never decides to show the model a
    thumbnail because the user "seemed to want it"; the model has to ask
    via ``analyze_media`` explicitly.

  - The host records tool outcomes and final assets, but never turns that
    record into a completion checklist. A no-tool stop is the model's natural
    end of turn. Only explicit user-decision gates (for example ``elicit`` or
    Plan Mode approval) may pause progress for the user.
"""
from __future__ import annotations

import asyncio
import json
import queue
import re
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from gemia.agent_loop_v3_background import AgentLoopBackgroundMixin
from gemia.agent_loop_v3_production import AgentLoopProductionMixin
from gemia.agent_loop_v3_prompt import AgentLoopPromptMixin
from gemia.agent_loop_v3_protocol import (
    _DOOM_LOOP_EXEMPT_TOOLS,
    _DOOM_LOOP_THRESHOLD,
    _MAX_CONSECUTIVE_TOOL_FAILURES,
    _MODEL_STREAM_RECONNECT_RETRIES,
    _REPEATED_FAILURE_NUDGE_THRESHOLD,
    _TRANSIENT_RETRY_NUDGE_THRESHOLD,
    _activity_text_from_model_preamble,
    _commit_seconds,
    _DisplayStreamGate,
    _is_mutating_lumen_tool,
    _is_retryable_model_stream_error,
    _load_system_template,
    _manual_media_import_path,
    _model_stream_error_class,
    _model_stream_retry_delay,
    _parse_args,
    _progress_report_from_model_preamble,
    _StreamAccumulator,
    _strip_activity_markup,
    _strip_gate_images,
    _thumbnail_user_content,
    _tool_call_message,
    _ToolCallAccumulator,
)
from gemia.ai.openai_image_client import endpoint_from_chat_url
from gemia.budget_guard import BudgetGuard, is_paid_media_tool
from gemia.errors import RECOVERY_FIX_ARGS, RECOVERY_TRANSIENT_RETRY
from gemia.gemini_client import GeminiClientV3
from gemia.plan_mode import (
    PLAN_GATE_TURN_LIMIT,
    is_plan_safe,
    plan_gate_message,
)
from gemia.production_budget import PAID_MEDIA_CONTEXT_KEY, ProductionMediaBudget
from gemia.project_store import ProjectHandle
from gemia.skill_store import (
    SKILL_RECALL_GUIDANCE_STATE_KEY,
    sanitize_skill_recall_guidance_memory,
)
from gemia.tool_capability_registry import (
    ToolCapabilityRegistry,
    build_default_registry,
)
from gemia.tool_outcome import classify_tool_exception, classify_tool_result
from gemia.tool_router import MASTER_TOOL_SET, PRIVATE_SYSTEM_TOOLS, ToolRouter
from gemia.tools import DISPATCHER, AssetRegistry, ToolContext
from gemia.tools._ask_bridge import AskBridge
from gemia.tools._context import ProgressUpdate
from gemia.transport.sse import REGISTRY as SSE_REGISTRY
from gemia.turn_compaction import (
    compact_settled_tool_blocks,
    sanitize_tool_protocol_pairs,
)
from gemia.turn_control import (
    E_CLARIFICATION_POLICY,
    ClarificationGuard,
    TurnIntent,
    classify_turn_intent,
    extract_scoped_directive,
)
from gemia.turn_ledger import TurnLedger, tool_target_key

# ── Remote-session host protection ───────────────────────────────────────
# When a turn runs for a REMOTE (public, passcode-gated) visitor, these
# host-reaching tools are stripped from the model's tool surface AND refused
# at dispatch. Keeps a friend demoing Lumeri from driving shell, reading or
# writing arbitrary host files, or arbitrary network egress on the owner's
# machine. Creative tools (generate/edit/lumen/timeline/vector/paint…) are
# untouched, so remote sessions keep full creative parity.
_REMOTE_DENY_TOOLS = frozenset({
    "run_shell", "build", "kill_job",
    "read_file", "list_dir", "write_file",
    "copy_in", "move_file", "organize_files", "file_delete",
    "fetch", "web_search", "web_open", "stock_media",
}) | PRIVATE_SYSTEM_TOOLS


def _strip_remote_denied(schemas: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop host-reaching tools from a schema list for remote sessions."""
    return [s for s in schemas
            if s.get("function", {}).get("name") not in _REMOTE_DENY_TOOLS]


def _filter_provider_schemas(
    schemas: list[dict[str, Any]], *, provider: str
) -> list[dict[str, Any]]:
    """Apply provider-owned capability visibility before model invocation."""
    if str(provider or "").strip().lower() == "openai_subscription":
        return schemas
    return [
        schema
        for schema in schemas
        if schema.get("function", {}).get("name") != "generate_image"
    ]


_ROLLING_USER_TURNS = 8

def _relevant_existing_jobs(
    request: str, pending_jobs: Mapping[str, Any] | None
) -> dict[str, Any]:
    """Return only session jobs explicitly continued or named this turn."""
    if not isinstance(pending_jobs, Mapping):
        return {}
    text = str(request or "").strip().lower()
    continue_all = bool(
        re.search(
            r"(?:继续(?:处理|生成|等待)?|等待(?:完成|结果)?|查看(?:任务|作业|进度)|"
            r"任务(?:状态|进度)|作业(?:状态|进度)|continue\b|wait\b|"
            r"job\s+status|task\s+status|check\s+(?:the\s+)?(?:job|task))",
            text,
            re.I,
        )
    )
    return {
        str(job_id): status
        for job_id, status in pending_jobs.items()
        if continue_all or str(job_id).lower() in text
    }
# ──────────────────────────────────────────────────────────────────────
# Main loop
# ──────────────────────────────────────────────────────────────────────


EventSink = Callable[[dict[str, Any]], None]


class AgentLoopV3(
    AgentLoopBackgroundMixin,
    AgentLoopPromptMixin,
    AgentLoopProductionMixin,
):
    def __init__(
        self,
        *,
        session_id: str,
        output_dir: Path,
        max_visual_inspections: int = 3,
        budget_max_usd: float = 5.0,
        budget_max_seconds: float | None = 600.0,
        production_media_budget: ProductionMediaBudget | None = None,
        gemini_client: GeminiClientV3 | None = None,
        emit_event: EventSink | None = None,
        sessions_root: Path | None = None,
        extra: dict[str, Any] | None = None,
        project_root: Path | None = None,
        project_id: str | None = None,
        asset_registry: AssetRegistry | None = None,
        runtime_state: dict[str, Any] | None = None,
        manage_session_meta: bool = True,
    ) -> None:
        self.session_id = session_id
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._max_visual_inspections = int(max_visual_inspections)

        self.registry = asset_registry if asset_registry is not None else AssetRegistry()
        # The persistent run ledger is the sole paid-media hard gate.  Legacy
        # sessions retain the historical in-memory dollar/time cap; durable
        # production sessions disable that duplicate cap while still using the
        # guard for estimates and elapsed-tool telemetry.
        if production_media_budget is not None:
            budget_max_usd = 1.0e100
            budget_max_seconds = None
        # Formal production runs compile one fail-closed capability authority.
        # Legacy/non-production loops keep the historical mutable tables as a
        # compatibility seam for older embedders and their test doubles.
        self.capabilities: ToolCapabilityRegistry | None = (
            build_default_registry() if production_media_budget is not None else None
        )
        self.budget = BudgetGuard(
            max_usd=budget_max_usd,
            max_seconds=budget_max_seconds,
            production_media_budget=production_media_budget,
        )
        self.client = gemini_client or GeminiClientV3()

        self._messages: list[dict[str, Any]] = []
        # MiniMax needs its raw assistant message when tool results are sent
        # immediately afterwards.  This sidecar is deliberately transient: it
        # never enters session state and is consumed by the very next request.
        self._provider_continuation_text: dict[int, str] = {}
        # The most recent REAL user input (run_turn's argument). Retract needs
        # to distinguish it from host-injected role="user" rows (background
        # notes, failure nudges), which must never be a retract anchor.
        self._last_user_message: str | None = None
        self._last_segment_clip_id: str | None = None
        self._last_edited_clip_id: str | None = None
        self._turn_last_edited_clip_id: str | None = None
        # Thread-safe mailbox for guidance arriving from the HTTP thread while
        # this loop is streaming or awaiting a tool. Guidance is drained only
        # at model-round boundaries so it can never split an assistant
        # tool_call from its matching tool result.
        self._turn_guidance: queue.SimpleQueue[str] = queue.SimpleQueue()
        self._pinned_intent: str | None = None
        self._pending_thumbnails: list[Path] = []
        self._turn_count = 0
        self._turn_ledger: TurnLedger | None = None
        # Settled tool blocks may be compacted across several turns. Keep their
        # summaries session-scoped so creating the next TurnLedger cannot erase
        # the only remaining representation of those results.
        self._compacted_history: list[str] = []
        # Plan mode: while True, only plan_mode.PLAN_ALLOWED_TOOLS dispatch;
        # everything else is gated (see the plan-mode block in _drive_turn).
        self.plan_mode: bool = False
        self._system_template = _load_system_template()
        self._emit: EventSink = emit_event or self._emit_via_sse_registry

        self.project = ProjectHandle.open(
            project_root or (self.output_dir / "project"),
            project_id or session_id,
            session_id=session_id,
            on_patch=self._on_project_patch,
        )

        # Human-in-the-loop bridge for the ``elicit`` verb: lets a tool dispatcher
        # emit an ask_question event and await the user's answer (delivered from the
        # HTTP thread via deliver_ask_answer).
        self._ask_bridge = AskBridge(self._emit)

        # Completed-background-job notices awaiting injection into the
        # conversation. Plain list, no lock: the session watcher coroutine and
        # the turn coroutine run on the SAME per-session event loop thread.
        self._bg_notifications: list[dict[str, Any]] = []
        # Background-job watcher bookkeeping (all touched only on the loop
        # thread, so no lock): last SSE status emitted per job (emit-on-change),
        # jobs whose wall-clock was budget-committed (commit-once), and jobs
        # fully handled so the watcher can skip them.
        self._bg_last_emitted: dict[str, str] = {}
        self._bg_committed: set[str] = set()
        self._bg_finalized: set[str] = set()
        # Live-only presence for the workspace's Project-level task board.
        # Children register on start and remove themselves on their terminal
        # event, so completed subagents are never shown as pending work.
        self._active_subagents: dict[str, dict[str, Any]] = {}

        _extra = dict(extra or {})
        # Provider identity is a host-owned capability boundary.  In
        # particular, generate_image is only exposed for the OpenAI
        # subscription bridge and the dispatcher re-checks the same value.
        self._openai_subscription_enabled = bool(
            getattr(self.client, "using_subscription_bridge", False)
            or str(getattr(self.client, "provider", "") or "").strip().lower()
            == "openai_subscription"
        )
        _extra["provider"] = (
            "openai_subscription"
            if self._openai_subscription_enabled
            else str(getattr(self.client, "provider", "") or "").strip().lower()
        )
        if self._openai_subscription_enabled:
            chat_endpoint = str(getattr(self.client, "api_url", "") or "").strip()
            _extra.setdefault("openai_subscription_chat_endpoint", chat_endpoint)
            _extra.setdefault(
                "openai_subscription_image_endpoint",
                endpoint_from_chat_url(chat_endpoint),
            )
        _extra.setdefault("ask_bridge", self._ask_bridge)
        # The spawn_subtasks host verb needs a handle back to this loop (to share
        # the client / registry / project with its children and read plan_mode
        # live). Children strip ask_bridge from their own ctx.extra, so a child
        # cannot elicit; agent_loop is present in the PARENT ctx only for the
        # spawn dispatcher.
        _extra.setdefault("agent_loop", self)
        # Remote (public, passcode-gated demo) session: kept in ctx.extra so
        # spawned subtasks inherit the restriction (they copy parent extra).
        self._remote: bool = bool(_extra.get("remote"))
        self._tool_ctx = ToolContext(
            session_id=session_id,
            output_dir=self.output_dir,
            registry=self.registry,
            emit_progress=lambda _u: None,
            extra=_extra,
            project=self.project,
        )

        self._restore_runtime_state(runtime_state or {})
        self.sessions_root = Path(sessions_root) if sessions_root else None
        self._manage_session_meta = bool(manage_session_meta)
        if self.sessions_root is not None:
            if self._manage_session_meta:
                self._write_session_meta(turn_count=self._turn_count)
            # Restore + reconcile background shell jobs left over from a prior
            # process for this session id (best-effort; never raises).
            self._load_and_reconcile_jobs()

    def _write_session_meta(self, *, turn_count: int) -> None:
        """Write a v2-SessionStore-compatible meta.json so legacy loaders can read it."""
        sdir = self.sessions_root / self.session_id
        sdir.mkdir(parents=True, exist_ok=True)
        (sdir / "turns").mkdir(parents=True, exist_ok=True)
        now = datetime.now(timezone.utc).isoformat()
        path = sdir / "meta.json"
        existing: dict[str, Any] = {}
        try:
            if path.exists():
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    existing = loaded
        except (OSError, json.JSONDecodeError):
            existing = {}
        meta = {
            **existing,
            "session_id": self.session_id,
            "project_id": self.project.project_id,
            "goal": self._pinned_intent or "",
            "max_turns": None,  # no fixed per-turn tool-step cap
            "ai_model": self.client.model,
            "created_at": existing.get("created_at") or now,
            "updated_at": now,
            "status": "running",
            "turn_count": int(turn_count),
            "loop_version": "v3",
            "plan_mode": bool(self.plan_mode),
        }
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)

    def snapshot_runtime_state(self) -> dict[str, Any]:
        """Return the JSON-safe conversational state required for a restart.

        Project state, assets and background jobs have their own durable stores;
        this snapshot intentionally references rather than duplicates them.
        """

        return {
            "session_id": self.session_id,
            "project_id": self.project.project_id,
            "messages": list(self._messages),
            "pinned_intent": self._pinned_intent,
            "last_user_message": self._last_user_message,
            "turn_count": int(self._turn_count),
            "compacted_history": list(self._compacted_history),
            "plan_mode": bool(self.plan_mode),
            "budget": self.budget.snapshot(),
            "background_lineage": {
                key: list(value) if isinstance(value, (list, tuple)) else value
                for key, value in self._tool_ctx.extra.items()
                if str(key).startswith("_veo_lineage_")
            },
            "skill_recall_guidance": sanitize_skill_recall_guidance_memory(
                self._tool_ctx.extra.get(SKILL_RECALL_GUIDANCE_STATE_KEY)
            ),
        }

    def _restore_runtime_state(self, state: dict[str, Any]) -> None:
        if not isinstance(state, dict):
            return
        messages = state.get("messages")
        if isinstance(messages, list):
            self._messages = sanitize_tool_protocol_pairs(
                [dict(item) for item in messages if isinstance(item, dict)]
            )
        pinned = state.get("pinned_intent")
        self._pinned_intent = str(pinned) if isinstance(pinned, str) and pinned else None
        last_user = state.get("last_user_message")
        self._last_user_message = (
            str(last_user) if isinstance(last_user, str) and last_user else None
        )
        try:
            self._turn_count = max(0, int(state.get("turn_count") or 0))
        except (TypeError, ValueError):
            self._turn_count = 0
        compacted = state.get("compacted_history")
        if isinstance(compacted, list):
            self._compacted_history = [str(item)[:500] for item in compacted[-80:]]
        self.plan_mode = bool(state.get("plan_mode", False))
        lineage = state.get("background_lineage")
        if isinstance(lineage, dict):
            for key, value in lineage.items():
                if str(key).startswith("_veo_lineage_") and isinstance(value, list):
                    self._tool_ctx.extra[str(key)] = [str(item) for item in value]
        recall_guidance = state.get("skill_recall_guidance")
        if isinstance(recall_guidance, dict) and recall_guidance:
            self._tool_ctx.extra[SKILL_RECALL_GUIDANCE_STATE_KEY] = (
                sanitize_skill_recall_guidance_memory(recall_guidance)
            )
        budget = state.get("budget") if isinstance(state.get("budget"), dict) else {}
        try:
            self.budget.spent_usd = max(0.0, float(budget.get("spent_usd") or 0.0))
            self.budget.spent_seconds = max(
                0.0, float(budget.get("spent_seconds") or 0.0)
            )
        except (TypeError, ValueError):
            self.budget.spent_usd = 0.0
            self.budget.spent_seconds = 0.0

    # ── plumbing ─────────────────────────────────────────────────────

    def _emit_via_sse_registry(self, event: dict[str, Any]) -> None:
        SSE_REGISTRY.emit(self.session_id, event)

    def _on_project_patch(self, info: dict[str, Any]) -> None:
        """Surface one authoritative patch to both timeline and segment clients."""
        if info.get("clip_id"):
            self._last_edited_clip_id = str(info.get("clip_id"))
        if str(info.get("state_scope") or "") == "segment" and info.get("clip_id"):
            self._last_segment_clip_id = str(info.get("clip_id"))
            self._turn_last_segment_clip_id = self._last_segment_clip_id
        self._emit({"kind": "timeline_op", **info})
        if str(info.get("state_scope") or "") == "segment":
            self._emit({"kind": "segment_content", **info})
            self._emit({"kind": "segment_reservation", **info})

    def deliver_ask_answer(self, question_id: str, answers: dict[str, Any]) -> bool:
        """Deliver a user's answer to a pending ``elicit`` question.

        Thread-safe: called from the HTTP handler thread; the bridge hops back onto
        this session's event loop to resolve the awaiting future. Returns True if a
        matching pending question was found.
        """
        return self._ask_bridge.deliver(question_id, answers)

    def get_pending_question(self, question_id: str) -> dict[str, Any] | None:
        """Return the question dict for a pending elicit, or None."""
        return self._ask_bridge.get_pending_question(question_id)

    def set_plan_mode(self, enabled: bool) -> bool:
        """Toggle plan mode and broadcast the change. Returns the new state.

        Thread-safe like ``deliver_ask_answer``: a bool flip is atomic and the
        SSE registry accepts emits from any thread. The flag is read once per
        tool call in ``_drive_turn``, so a mid-turn toggle simply applies from
        the next tool call onward.
        """
        enabled = bool(enabled)
        if enabled != self.plan_mode:
            self.plan_mode = enabled
            self._emit({"kind": "plan_mode_changed", "enabled": enabled})
            if self.sessions_root is not None and self._manage_session_meta:
                self._write_session_meta(turn_count=self._turn_count)
        return self.plan_mode

    def add_external_asset(self, path: Path, *, summary: str = "") -> str:
        record = self.registry.add_external(Path(path), summary=summary or None)
        return record.asset_id

    # ── background jobs (watcher-facing) ─────────────────────────────

    def emit_background_update(self, payload: dict[str, Any]) -> None:
        """SSE emit for a background job state change (called by the session
        watcher). Lives here because agent_loop_v3.py is one of the four
        whitelisted emit sites — the kind must stay a literal string. The
        trailing literal wins over any stray "kind" in the payload."""
        self._emit({**payload, "kind": "background_task_update"})

    def _append_tool_result(self, call_id: str, payload: Any) -> None:
        if isinstance(payload, str):
            content = payload
        else:
            content = json.dumps(payload, ensure_ascii=False, default=str)
        self._messages.append(
            {"role": "tool", "tool_call_id": call_id, "content": content}
        )

    def _lumen_post_state_digest(self) -> str:
        """Build a compact POST-STATE digest of the live lumenframe document.

        Reuses the compact tree-summary helper in ``gemia.tools.layer`` and runs
        lumenframe's ``validate_doc`` to surface any structural warnings. Returns
        a short text block (layer-tree summary + warning line) or ``""`` when
        there is no document / lumenframe is unavailable. This is the LSP-style
        feedback opencode appends after an edit, adapted to the layer tree.
        """
        from gemia.tools import layer as _layer

        # Resolve the current doc the same way the layer dispatchers do, so the
        # digest reflects exactly what the just-applied edit produced. Prefer the
        # project-backed doc; fall back to the session memory cache.
        doc: dict[str, Any] | None = None
        try:
            doc = _layer._lumendoc(self._tool_ctx)
        except Exception:
            cache = getattr(_layer, "_DOC_CACHE", {})
            doc = cache.get(self.session_id)
        if not isinstance(doc, dict):
            return ""

        root = doc.get("root", {})
        tree = (
            _layer._compact_tree_summary(root)
            if root
            else "(empty composition)"
        )
        selection = doc.get("selection", []) or []

        # Run lumenframe's own validator; it raises on any invariant violation.
        # A clean doc => "ok"; a violation => the structured message as a warning.
        try:
            from lumenframe import validate_doc as _validate_doc

            _validate_doc(doc)
            warnings = "none"
        except Exception as exc:  # LayerPatchError or anything validate raises
            code = getattr(exc, "code", None)
            msg = getattr(exc, "message", None) or str(exc)
            warnings = f"{code}: {msg}" if code else str(msg)

        lines = [
            "[POST-EDIT STATE — the layer document AFTER this edit. Verify it "
            "matches your intent before the next step.]",
            "Layer tree:",
            tree,
        ]
        if selection:
            lines.append(
                # Full ids — the model targets layers by this exact string.
                "Selection: " + ", ".join(str(s) for s in selection)
            )
        lines.append(f"Validate: {warnings}")
        return "\n".join(lines)

    def _append_lumen_post_state(self, call_id: str) -> None:
        """ADDITIVE post-edit feedback: append the POST-STATE digest to the
        tool_result the model just received for ``call_id``.

        Mirrors opencode pattern #2 (appending LSP diagnostics after an edit):
        right after a successful mutating lumen verb, fold the resulting
        layer-tree summary + validate warnings into that exact tool message's
        text so the model is grounded in the new state. Fully wrapped in
        try/except by the caller — must never break the loop.
        """
        digest = self._lumen_post_state_digest()
        if not digest:
            return
        # The success path appended this tool_result as the last message. Find
        # it by call_id (robust even if ordering ever changes) and append the
        # digest to its text content. Keep it additive: we never replace.
        for msg in reversed(self._messages):
            if (
                msg.get("role") == "tool"
                and msg.get("tool_call_id") == call_id
                and isinstance(msg.get("content"), str)
            ):
                existing = msg["content"]
                msg["content"] = (
                    f"{existing}\n\n{digest}" if existing else digest
                )
                return

    def _note_tool_failure(
        self,
        fail_state: dict[str, tuple[str, int]],
        name: str,
        code: str,
        *,
        limit: int,
    ) -> tuple[bool, int]:
        """Record a non-successful call of ``name`` with failure class ``code``
        (error, parse failure, or gate).

        Counts consecutive failures of the same ``(name, code)`` for diagnostic
        context. Returns ``(should_nudge, streak)`` where ``should_nudge`` is
        True once ``streak`` reaches ``limit``. The production limit is one, so
        every failure prompts a direction check. This never means "stop the
        turn."
        """
        last_code, streak = fail_state.get(name, ("", 0))
        streak = streak + 1 if code == last_code else 1
        fail_state[name] = (code, streak)
        return streak >= limit, streak

    def _append_repeated_failure_nudge(self, name: str, code: str, count: int) -> None:
        del count
        recall_state = self._tool_ctx.extra.get(SKILL_RECALL_GUIDANCE_STATE_KEY)
        recalled_names = []
        if isinstance(recall_state, dict):
            recall_memory = sanitize_skill_recall_guidance_memory(recall_state)
            last_entry = recall_memory["entries"].get(
                recall_memory.get("last_query") or ""
            ) or {}
            recalled_names = [
                str(item)
                for item in (last_entry.get("last_result_names") or [])
                if str(item).strip()
            ][:8]
        skill_audit_note = ""
        if recalled_names:
            skill_audit_note = (
                " The most recent recall_skills result for this task was: "
                + ", ".join(f"`{item}`" for item in recalled_names)
                + ". If those skills materially caused this failed direction, "
                "audit the sorter result and call recall_skills again with "
                "routing_audit containing concrete failure_evidence, positive "
                "replacement guidance, and only the explicitly disproven skill "
                "names in avoid_skills. The sorter will remember that audit for "
                "the same query instead of returning the rejected pile again."
            )
        self._messages.append(
            {
                "role": "user",
                "content": (
                    f"Tool failure direction check: `{name}` failed with `{code}`. "
                    "Before the next tool call, decide from the structured error "
                    "whether the current approach still has concrete evidence of "
                    "working. Do not continue merely by renaming or reparameterizing "
                    "the same plan. Retry only when the recovery is explicitly "
                    "transient and the retry is materially justified; otherwise "
                    "change arguments, switch tools or approach, inspect state with "
                    "a cheaper read-only tool, or clearly report the blocker. Do "
                    "not claim a fallback worked until a tool result proves it."
                    + skill_audit_note
                ),
            }
        )

    def _emit_doom_loop(self, name: str, count: int) -> None:
        """Success-blind doom-loop signal: the last ``count`` tool calls were the
        SAME tool with byte-identical args, so the turn is repeating itself
        (regardless of whether each call succeeded). Stop the turn."""
        self._emit(
            {
                "kind": "turn_error",
                "reason": "doom_loop",
                "tool_name": name,
                "repeat_count": count,
                "error": (
                    f"doom loop: tool '{name}' was called {count} times in a row with "
                    f"byte-identical arguments this turn; stopping. The loop is "
                    f"repeating itself — change the arguments or the approach."
                ),
            }
        )

    @staticmethod
    def _synthesize_wrapup_message(
        reason: str,
        *,
        tools_succeeded: int,
        tools_failed: int,
        assets_produced: int,
        tool_name: str | None = None,
    ) -> str:
        """Build a short 'stopped because X; here's what was / wasn't done'
        summary LOCALLY from the known stop reason and the turn's tool / asset
        counts. No model API call — this is a cheap deterministic synthesis so a
        budget / doom-loop stop is *explained*
        to the user instead of being a bare silent halt.

        ``reason`` is a short machine code (e.g. ``"doom_loop"``,
        ``"budget_exhausted"``); the rest
        is synthesized from the turn state that is already on hand at the exit
        point."""
        del tools_succeeded, assets_produced, tool_name
        if reason == "doom_loop":
            return "执行陷入了重复，我已经停止继续重试，避免原地打转。"
        if reason == "budget_exhausted":
            return "这轮的执行预算已经用完，未完成的部分没有被算作成功。"
        if reason == "plan_gate_limit":
            return "现在仍是计划模式，修改操作需要先获得批准。"
        return "这轮执行没有完整结束。"

    def _emit_turn_wrapup(
        self,
        reason: str,
        *,
        tools_succeeded: int,
        tools_failed: int,
        assets_produced: int,
        tool_name: str | None = None,
    ) -> None:
        """ADDITIVE graceful wrap-up (ported from opencode pattern #5): at a
        non-success exit point (budget exhaustion or doom loop)
        emit a short assistant-facing ``turn_wrapup`` event that explains the
        stop, *in addition to* the existing turn_error
        event — so the user gets a 'stopped because X; here's what was / wasn't
        done' summary instead of a bare halt.

        Cheap and non-fatal by contract: the message is synthesized LOCALLY
        (no extra model call) and the whole thing is wrapped in try/except so a
        failure here can never break the loop. ``WHEN`` the turn stops is
        unchanged — this only ADDS the explanatory emission."""
        try:
            message = self._synthesize_wrapup_message(
                reason,
                tools_succeeded=tools_succeeded,
                tools_failed=tools_failed,
                assets_produced=assets_produced,
                tool_name=tool_name,
            )
            self._emit(
                {
                    "kind": "turn_wrapup",
                    "reason": reason,
                    "message": message,
                    "tools_succeeded": tools_succeeded,
                    "tools_failed": tools_failed,
                    "assets_produced": assets_produced,
                    **({"tool_name": tool_name} if tool_name else {}),
                }
            )
        except Exception:  # noqa: BLE001 — wrap-up must never break the turn
            pass

    @staticmethod
    def _is_doom_loop(recent: list[tuple[str, str]]) -> bool:
        """True when the last ``_DOOM_LOOP_THRESHOLD`` recorded tool calls are the
        SAME (tool_name, raw-args-JSON) tuple, byte-for-byte. ``recent`` is the
        per-turn rolling history of dispatched (name, args) tuples."""
        if len(recent) < _DOOM_LOOP_THRESHOLD:
            return False
        window = recent[-_DOOM_LOOP_THRESHOLD:]
        return all(item == window[0] for item in window)

    def _trim_rolling_window(self) -> None:
        user_idx = [i for i, m in enumerate(self._messages) if m.get("role") == "user"]
        if len(user_idx) <= _ROLLING_USER_TURNS:
            return
        cutoff = user_idx[-_ROLLING_USER_TURNS]
        # A user/host notice can sit between an older assistant tool-call row
        # and its results. Starting the rolling window at that user row would
        # retain protocol-orphaned tool outputs after dropping their call.
        # Discard only those leading orphan results; later complete call/result
        # blocks remain byte-for-byte intact.
        while (
            cutoff + 1 < len(self._messages)
            and self._messages[cutoff + 1].get("role") == "tool"
        ):
            cutoff += 1
        start = cutoff
        if self._messages[start].get("role") == "tool":
            start += 1
        self._messages = sanitize_tool_protocol_pairs(self._messages[start:])

    def _advance_production_after_tool(
        self, tool_name: str, result: Mapping[str, Any]
    ) -> None:
        """Advance from persisted facts; a tool success is never the gate."""

        transition = self._tool_ctx.extra.get("transition_production")
        if not callable(transition):
            return
        trace_id = str(self._tool_ctx.extra.get("active_trace_id") or "")
        try:
            from gemia.production_evidence import next_evidence_stage

            store = self._tool_ctx.extra.get("production_store")
            project_id = str(self._tool_ctx.extra.get("project_id") or "")
            run_id = str(self._tool_ctx.extra.get("run_id") or "")
            for _ in range(8):
                status = self._production_status()
                current = str(status.get("production_state") or "")
                gaps = list(status.get("evidence_gaps") or [])
                if gaps or current in {
                    "ready_for_review", "accepted", "blocked", "cancelled", "failed"
                }:
                    break
                # Re-enter at rough_cut so sound, visual and render evidence are
                # reconsidered in order. Existing facts advance immediately;
                # only the invalidated proof actually has to be recomputed.
                target = "rough_cut" if current == "revising" else next_evidence_stage(current)
                if not target:
                    break
                evidence = store.add_evidence(
                    project_id,
                    run_id,
                    kind="stage_gate",
                    payload={
                        "from": current,
                        "to": target,
                        "satisfied_by": status.get("evidence_facts") or {},
                        "contract_revision": int(
                            store.load_run(project_id, run_id).get("contract_revision") or 0
                        ),
                        "creative_ir_revision": int(
                            (status.get("creative_ir") or {}).get("revision") or 0
                        ),
                        "trigger_tool": str(tool_name),
                    },
                    project_revision=int(status.get("project_revision") or 0),
                    trace_id=trace_id,
                )
                transition(target, trace_id=str(evidence.get("evidence_id") or trace_id))
        except Exception as exc:  # telemetry cannot corrupt an already-settled tool
            self._emit(
                {
                    "kind": "turn_error",
                    "reason": "production_state_transition_failed",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )

    def _compact_turn_history(self) -> None:
        """Compact only complete protocol blocks; any error is fail-open."""
        ledger = self._turn_ledger
        if ledger is None:
            return
        protected = set(ledger.unresolved_failures)
        protected.update(
            record.call_id
            for record in ledger.outcomes
            if record.seq in {ledger.last_mutation_seq, ledger.last_verification_seq}
            or (
                record.facts.get("job_id")
                and str(record.facts.get("job_id")) in ledger.pending_jobs
            )
        )
        try:
            result = compact_settled_tool_blocks(
                self._messages, protected_call_ids=protected
            )
        except Exception:  # noqa: BLE001 — compression never breaks a turn
            return
        if result.removed_blocks:
            self._messages = result.messages
            ledger.add_compact_history(result.summaries)
            self._compacted_history.extend(str(item)[:500] for item in result.summaries)
            self._compacted_history = self._compacted_history[-80:]

    # ── live turn control ───────────────────────────────────────────

    def queue_turn_guidance(self, text: str) -> None:
        """Queue user guidance for the next safe model-round boundary.

        ``SimpleQueue`` makes this callable from the HTTP handler thread. The
        drive loop is the only consumer and never drains between an assistant
        tool call and its tool result, preserving provider protocol ordering.
        """
        value = str(text or "").strip()
        if value:
            self._turn_guidance.put(value)

    def _drain_turn_guidance(self) -> list[str]:
        items: list[str] = []
        while True:
            try:
                items.append(self._turn_guidance.get_nowait())
            except queue.Empty:
                return items

    def _clear_turn_guidance(self) -> None:
        self._drain_turn_guidance()

    # ── public entrypoint ────────────────────────────────────────────

    def retract_last_turn(self, expected_message: str | None = None) -> str | None:
        """Drop the most recent real user turn — that user message plus every
        row after it — from the conversation. Returns the retracted text, or
        ``None`` when nothing is retractable: no completed turn, the anchor
        was rewritten away by trimming/compaction, or ``expected_message``
        (the caller's view of the last turn) no longer matches ours.

        Anchors on content, not index: rolling-window trims and compaction
        rewrite ``_messages``, so a stored index could silently point at the
        wrong row. Only ever call between turns (the HTTP layer guards this).
        Side effects already applied by the turn (timeline edits, files) are
        deliberately NOT rolled back — retract rewrites the conversation, not
        the project.
        """
        target = self._last_user_message
        if not target:
            return None
        if expected_message is not None and expected_message != target:
            return None
        for i in range(len(self._messages) - 1, -1, -1):
            row = self._messages[i]
            if row.get("role") == "user" and row.get("content") == target:
                del self._messages[i:]
                self._last_user_message = None
                return target
        return None

    async def run_turn(self, user_message: str) -> None:
        """Run one user turn until the model stops calling tools."""
        if self._pinned_intent is None:
            self._pinned_intent = user_message
        self._messages.append({"role": "user", "content": user_message})
        self._last_user_message = user_message
        self._trim_rolling_window()
        checkpoint = self._tool_ctx.extra.get("persist_runtime_checkpoint")
        if callable(checkpoint):
            checkpoint()
        # Intent classification remains presentation metadata only. Streaming
        # visibility is decided from the actual model prefix so every intent
        # can receive real deltas without exposing tool UI markup.
        display_intent = classify_turn_intent(user_message)
        # Host-owned, per-turn clarification policy. The dispatcher reads it
        # from the shared context, so every elicit call in this turn follows
        # the same decision-only boundary.
        self._tool_ctx.extra["clarification_guard"] = ClarificationGuard()
        try:
            await self._drive_turn(user_message, display_intent)
        finally:
            self._clear_turn_guidance()
            self._turn_count += 1
            if self.sessions_root is not None and self._manage_session_meta:
                self._write_session_meta(turn_count=self._turn_count)

    # ── the loop ─────────────────────────────────────────────────────

    async def _drive_turn(
        self,
        turn_request: str,
        display_intent: TurnIntent,
    ) -> None:
        """One turn: stream → dispatch any tool_calls → repeat → emit turn_complete.

        There is no fixed cap on the total number of tool steps in a turn.
        ``visual_inspections_this_turn`` still caps analyze_media thumbnails,
        and ``tool_fail_counts`` drives immediate failure-direction checks. Genuine
        cost/time stay bounded by BudgetGuard.
        """
        pre_asset_ids = {r.asset_id for r in self.registry.list_records()}
        self._turn_last_segment_clip_id = None
        self._turn_last_edited_clip_id = None
        routing_state = self._routing_state()
        # "…做一个宣传片，你先把logo找到" budgets THIS turn against the staged
        # clause only; the model still sees the full request, so the deferred
        # goal remains context rather than a host-authored completion demand.
        scope_request = extract_scoped_directive(turn_request) or turn_request
        router = ToolRouter(scope_request, state=routing_state)
        workflow = router.decision.primary_workflow
        ledger = TurnLedger(
            scope_request,
            workflow=workflow,
            session_origin=self.session_id,
            workflows=router.decision.workflows,
        )
        ledger.add_compact_history(self._compacted_history)
        ledger.pending_jobs.update(
            _relevant_existing_jobs(
                turn_request, routing_state.get("pending_jobs")
            )
        )
        # Language classification and ambient state may expose capabilities,
        # but only a tool call from this turn creates an activity record. The
        # record is descriptive; it never decides whether the model may stop.
        ledger_active = False
        self._turn_ledger = None
        visual_inspections_this_turn = 0
        # Blocked-by-plan-mode calls this turn. Gated calls never reach the
        # doom-loop history (they don't dispatch), so this counter is the
        # host-side stop for a model that keeps hammering blocked tools.
        plan_gates_this_turn = 0
        # name → (last_failure_code, consecutive_streak) for immediate
        # failure-direction checks.
        tool_fail_counts: dict[str, tuple[str, int]] = {}
        # Rolling history of (tool_name, raw-args-JSON) for THIS turn, used by the
        # success-blind doom-loop guard. We only need the last few entries, but a
        # plain list is simplest; it stays tiny because the doom-loop guard still
        # stops byte-identical successful repeats.
        recent_tool_calls: list[tuple[str, str]] = []
        # Running tallies for the graceful wrap-up summary at non-success exits
        # (opencode pattern #5). Cheap counters, no extra model call: a
        # dispatch that returned is a success; any error / gate / parse-fail is
        # a failure. Asset count is computed from the registry at exit time.
        tools_succeeded = 0
        tools_failed = 0
        manual_import_recovery_used = False

        def _assets_produced() -> int:
            return sum(
                1
                for r in self.registry.list_records()
                if r.asset_id not in pre_asset_ids
            )

        self._emit({"kind": "turn_start"})

        # Every multimodal thumbnail message is one-shot. The exact messages
        # included in a model call are reclaimed immediately after that call,
        # including the ordinary analyze_media path.
        one_shot_image_messages: list[dict[str, Any]] = []
        def _apply_guidance(items: list[str]) -> str:
            joined = "\n".join(f"- {item}" for item in items)
            content = (
                "用户在本轮执行过程中给出了最新引导。立即调整后续工作；"
                "若它与原请求冲突，以最新引导为准。\n" + joined
            )
            self._messages.append({"role": "user", "content": content})
            self._last_user_message = content
            self._emit({"kind": "turn_guidance_applied", "guidance": items[-1]})
            return content

        while True:
            guidance = self._drain_turn_guidance()
            if guidance:
                guidance_context = _apply_guidance(guidance)
                router = ToolRouter(
                    f"{turn_request}\n\n{guidance_context}",
                    state=self._routing_state(),
                )
            # Background jobs that completed mid-turn: inject their notices as
            # one synthetic user message BEFORE the next model call (role
            # "user" — a role:"tool" message without a matching call id would
            # break the alternating-role contract).
            bg_note = self._drain_background_notifications()
            if bg_note is not None:
                self._messages.append({"role": "user", "content": bg_note})
                self._last_user_message = bg_note

            self._compact_turn_history()
            messages = self.render_messages()
            consumed_images = list(one_shot_image_messages)
            stream_error: str | None = None

            # ---- stream from model ---------------------------------
            # The model always sees the router-selected tools. A host text
            # classifier must not override its decision to answer or act.
            active_schemas = (
                self.capabilities.schemas(router.active_tool_names)
                if self.capabilities is not None
                else router.active_schemas
            )
            if self._remote:
                active_schemas = _strip_remote_denied(active_schemas)
            active_schemas = _filter_provider_schemas(
                active_schemas,
                provider=(
                    "openai_subscription"
                    if self._openai_subscription_enabled
                    else str(getattr(self.client, "provider", "") or "")
                ),
            )
            if self.plan_mode:
                active_schemas = [
                    schema
                    for schema in active_schemas
                    if is_plan_safe(str(schema["function"]["name"]))
                ]
            try:
                for stream_attempt in range(_MODEL_STREAM_RECONNECT_RETRIES + 1):
                    accum = _StreamAccumulator()
                    display_stream = _DisplayStreamGate()
                    stream_error = None
                    try:
                        async for delta in self.client.stream_turn(
                            messages, tools=active_schemas
                        ):
                            kind = delta["kind"]
                            if kind == "text_delta":
                                visible_text = str(delta.get("text") or "")
                                raw_text = (
                                    str(delta["raw_text"])
                                    if "raw_text" in delta
                                    else visible_text
                                )
                                if raw_text:
                                    accum.raw_text_buf.append(raw_text)
                                if not visible_text:
                                    continue
                                accum.text_buf.append(visible_text)
                                for visible_delta in display_stream.feed(visible_text):
                                    self._emit(
                                        {
                                            "kind": "model_text_delta",
                                            "delta": visible_delta,
                                            "display": "stream",
                                        }
                                    )
                            elif kind == "tool_call_start":
                                tc = _ToolCallAccumulator(
                                    index=int(delta["index"]),
                                    id=str(delta["id"] or f"call_{delta['index']}"),
                                    name=str(delta["name"]),
                                    extra_content=delta.get("extra_content"),
                                )
                                accum.tool_calls_by_index[tc.index] = tc
                                self._emit(
                                    {
                                        "kind": "model_tool_call_start",
                                        "call_id": tc.id,
                                        "tool_name": tc.name,
                                    }
                                )
                            elif kind == "tool_call_args_delta":
                                tc = accum.tool_calls_by_index.get(int(delta["index"]))
                                if tc is not None:
                                    tc.args_buf.append(str(delta["delta"]))
                            elif kind == "tool_call_extra":
                                tc = accum.tool_calls_by_index.get(int(delta["index"]))
                                if tc is not None:
                                    tc.extra_content = delta.get("extra_content")
                            elif kind == "finish":
                                accum.finish_reason = str(delta["reason"])
                            elif kind == "error":
                                stream_error = str(delta["error"])
                                break
                    except Exception as exc:  # noqa: BLE001 - transport reconnect path
                        stream_error = f"{type(exc).__name__}: {exc}"

                    if stream_error is None:
                        break

                    retry_number = stream_attempt + 1
                    if (
                        retry_number > _MODEL_STREAM_RECONNECT_RETRIES
                        or not _is_retryable_model_stream_error(stream_error)
                    ):
                        break

                    delay_sec = _model_stream_retry_delay(retry_number)
                    self._emit(
                        {
                            "kind": "model_stream_reset",
                            "retry": retry_number,
                            "max_retries": _MODEL_STREAM_RECONNECT_RETRIES,
                            "delay_sec": delay_sec,
                            "error_class": _model_stream_error_class(stream_error),
                        }
                    )
                    await asyncio.sleep(delay_sec)
            finally:
                # Iterator-level exceptions/cancellation must reclaim one-shot
                # base64 only after all reconnect attempts have settled, so
                # every retry receives the same one-shot visual context.
                for image_message in consumed_images:
                    _strip_gate_images(image_message)
                    if image_message in one_shot_image_messages:
                        one_shot_image_messages.remove(image_message)

            if stream_error is not None:
                self._emit(
                    {
                        "kind": "turn_error",
                        "reason": "stream_error",
                        "error": stream_error,
                    }
                )
                return

            if accum.tool_calls:
                if not ledger_active:
                    ledger_active = True
                    self._turn_ledger = ledger

            # ---- persist the assistant message ---------------------
            # Activity markup is a UI-only, model-authored label. It must not
            # become assistant history or leak into the eventual final reply.
            activity_text = _activity_text_from_model_preamble(
                accum.text,
                tool_names=[tc.name for tc in accum.tool_calls],
            )
            progress_report = _progress_report_from_model_preamble(
                accum.text,
                tool_names=[tc.name for tc in accum.tool_calls],
            )
            assistant_text = _strip_activity_markup(accum.text)
            assistant_msg: dict[str, Any] = {
                "role": "assistant",
                "content": assistant_text if assistant_text else None,
            }
            if accum.tool_calls:
                assistant_msg["tool_calls"] = [
                    _tool_call_message(tc)
                    for tc in accum.tool_calls
                ]
                if accum.raw_text != accum.text:
                    self._provider_continuation_text[id(assistant_msg)] = accum.raw_text
            self._messages.append(assistant_msg)

            # Guidance can arrive while a text-only model response is
            # streaming. Consume it before accepting that response as final,
            # then give the model a fresh round to follow the new direction.
            late_guidance = self._drain_turn_guidance()
            if not accum.tool_calls and late_guidance:
                guidance_context = _apply_guidance(late_guidance)
                router = ToolRouter(
                    f"{turn_request}\n\n{guidance_context}",
                    state=self._routing_state(),
                )
                continue

            # A local exact path is already enough for Lumeri to import media.
            # Do not end the turn by delegating a supported copy_in action back
            # to the user. Give the model one explicit recovery round so it can
            # register the exact file, use the returned asset_id, and continue
            # the requested timeline operation. Remote and Plan sessions stay
            # fail-closed because host file import is unavailable there.
            manual_import_path = (
                _manual_media_import_path(
                    assistant_text,
                    request_text=turn_request,
                )
                if not accum.tool_calls
                else None
            )
            if (
                manual_import_path is not None
                and not manual_import_recovery_used
                and not self._remote
                and not self.plan_mode
                and "copy_in" in router.active_tool_names
            ):
                manual_import_recovery_used = True
                recovery = (
                    "The exact local media file you named exists and this "
                    "session can import it directly. Do not ask the user to "
                    "drag or import it manually. Call copy_in now with this "
                    f"exact path: {manual_import_path}. Require "
                    "asset_registered=true and its returned asset_id, then "
                    "finish the already requested timeline change and verify it."
                )
                self._messages.append({"role": "user", "content": recovery})
                self._last_user_message = recovery
                continue

            # ---- model called no tools → natural end of turn -----------
            if not accum.tool_calls:
                # Tool activity, failures, and assets remain available to the
                # model as context. They are evidence, not a host-owned verdict.
                # If the model needs the user's decision it must use the
                # explicit elicitation path; otherwise its no-tool response is
                # accepted as the natural conclusion of this turn.
                if not display_stream.emitted and assistant_text:
                    self._emit(
                        {
                            "kind": "model_text_delta",
                            "delta": assistant_text,
                            "display": "stream",
                        }
                    )
                produced_ids = (
                    [
                        r.asset_id
                        for r in self.registry.list_records()
                        if r.asset_id not in pre_asset_ids
                    ]
                    if ledger_active
                    else []
                )
                # Production telemetry may describe whether this turn changed
                # anything, but it never decides whether a no-tool response may
                # end. Only an actual tool call activates this activity record;
                # ambient asset races cannot turn ordinary prose into work.
                turn_did_work = bool(
                    ledger_active
                    and (tools_succeeded or tools_failed or produced_ids)
                )
                final_ids = (
                    [
                        asset_id
                        for asset_id in ledger.final_asset_ids
                        if self.registry.contains(asset_id)
                    ]
                    if ledger_active
                    else []
                )
                self._auto_log_turn(
                    tools_succeeded=tools_succeeded,
                    tools_failed=tools_failed,
                    assets_produced=len(produced_ids),
                )
                production = self._production_status()
                production_state = str(
                    production.get("production_state") or "created"
                )
                if production_state == "ready_for_review":
                    outcome = "ready_for_review"
                elif production_state in {"blocked", "failed", "cancelled"} or production.get(
                    "blockers"
                ):
                    outcome = "blocked"
                elif turn_did_work:
                    outcome = "progressed"
                else:
                    outcome = "no_change"
                self._emit(
                    {
                        "kind": "turn_complete",
                        "outcome": outcome,
                        "final_asset_ids": final_ids,
                        "project_id": production.get("project_id"),
                        "run_id": production.get("run_id"),
                        "project_revision": production.get("project_revision", 0),
                        "production_state": production_state,
                        "last_edited_clip_id": self._turn_last_edited_clip_id,
                    }
                )
                return

            # ---- dispatch each tool call sequentially --------------
            for tc_position, tc in enumerate(accum.tool_calls):
                # Never let one provider reservation leak into a later tool or
                # an MCP call that reuses this context.
                self._tool_ctx.extra.pop(PAID_MEDIA_CONTEXT_KEY, None)
                self._tool_ctx.extra.pop("tool_call_context", None)
                parsed_args, parse_error = _parse_args(tc.args)
                call_target = tool_target_key(parsed_args) if parse_error is None else None

                ready_event: dict[str, Any] = {
                    "kind": "model_tool_call_ready",
                    "call_id": tc.id,
                    "tool_name": tc.name,
                    "args": (
                        parsed_args
                        if parse_error is None
                        else {"_raw": tc.args, "_parse_error": parse_error}
                    ),
                }
                if activity_text is not None:
                    ready_event["activity_text"] = activity_text
                if progress_report is not None:
                    ready_event["progress_report"] = progress_report
                self._emit(ready_event)

                if parse_error is not None:
                    parse_payload = {
                        "error": "arguments were not a valid JSON object",
                        "error_code": "E_BAD_ARG",
                        "recovery": RECOVERY_FIX_ARGS,
                        "parse_error": parse_error,
                        "raw_arguments": tc.args,
                    }
                    self._emit(
                        {
                            "kind": "tool_exec_error",
                            "call_id": tc.id,
                            "tool_name": tc.name,
                            "error": f"tool args not valid JSON object: {parse_error}",
                            "error_code": "E_BAD_ARG",
                            "recovery": RECOVERY_FIX_ARGS,
                        }
                    )
                    self._append_tool_result(
                        tc.id,
                        parse_payload,
                    )
                    ledger.record_outcome(
                        tc.name,
                        classify_tool_result(parse_payload),
                        call_id=tc.id,
                        target_key=call_target,
                    )
                    tools_failed += 1
                    should_nudge, streak = self._note_tool_failure(
                        tool_fail_counts, tc.name, "E_BAD_ARG",
                        limit=_REPEATED_FAILURE_NUDGE_THRESHOLD,
                    )
                    if should_nudge:
                        self._append_repeated_failure_nudge(tc.name, "E_BAD_ARG", streak)
                    continue

                # The schema subset is not the dispatcher boundary. A known
                # but currently hidden master tool expands its owning pack and
                # must be retried next round; an actually unknown dispatcher
                # name fails closed. Dynamically registered extension tools
                # (used by local integrations/tests) retain the legacy path.
                if (
                    tc.name in MASTER_TOOL_SET
                    and tc.name not in router.active_tool_names
                    and not (self.plan_mode and not is_plan_safe(tc.name))
                ):
                    added_pack = router.activate_for_tool(tc.name)
                    route_payload = {
                        "error": (
                            f"tool '{tc.name}' was not active for this turn; "
                            "its pack is now active, retry the call"
                        ),
                        "error_code": "E_TOOL_NOT_ACTIVE",
                        "recovery": RECOVERY_FIX_ARGS,
                        "activated_pack": added_pack,
                    }
                    self._emit(
                        {
                            "kind": "tool_exec_error",
                            "call_id": tc.id,
                            "tool_name": tc.name,
                            **route_payload,
                        }
                    )
                    self._append_tool_result(tc.id, route_payload)
                    ledger.record_outcome(
                        tc.name,
                        classify_tool_result(route_payload),
                        call_id=tc.id,
                        target_key=call_target,
                        blocking_failure=False,
                    )
                    tools_failed += 1
                    continue
                installed = (
                    tc.name in self.capabilities.names
                    if self.capabilities is not None
                    else tc.name in DISPATCHER
                )
                if not installed:
                    unknown_payload = {
                        "error": f"unknown tool: {tc.name}",
                        "error_code": "E_TOOL",
                        "recovery": RECOVERY_FIX_ARGS,
                    }
                    self._emit(
                        {
                            "kind": "tool_exec_error",
                            "call_id": tc.id,
                            "tool_name": tc.name,
                            **unknown_payload,
                        }
                    )
                    self._append_tool_result(tc.id, unknown_payload)
                    ledger.record_outcome(
                        tc.name,
                        classify_tool_result(unknown_payload),
                        call_id=tc.id,
                        target_key=call_target,
                    )
                    tools_failed += 1
                    continue

                # Plan-mode gate: while ON, only read-only inspection tools
                # dispatch. Same emit/append/continue shape as the budget gate
                # below, with its own event kind so both frontends can render
                # it distinctly. Checked BEFORE the budget gate: a blocked tool
                # is blocked no matter how affordable it is.
                if self.plan_mode and not is_plan_safe(tc.name):
                    plan_gates_this_turn += 1
                    gate_msg = plan_gate_message(tc.name)
                    self._emit(
                        {
                            "kind": "plan_gate",
                            "call_id": tc.id,
                            "tool_name": tc.name,
                            "message": gate_msg,
                        }
                    )
                    plan_payload = {
                        "blocked_by_plan_mode": True,
                        "error": gate_msg,
                        "error_code": "E_PLAN_MODE",
                        "message": gate_msg,
                    }
                    self._append_tool_result(tc.id, plan_payload)
                    ledger.record_outcome(
                        tc.name,
                        classify_tool_result(plan_payload),
                        call_id=tc.id,
                        target_key=call_target,
                        blocking_failure=False,
                    )
                    tools_failed += 1
                    if plan_gates_this_turn >= PLAN_GATE_TURN_LIMIT:
                        # The current call is settled above. Pair every later
                        # call from this same assistant batch with a structured
                        # cancellation before returning, so no provider sees an
                        # orphan assistant tool call on replay/compaction.
                        for skipped in accum.tool_calls[tc_position + 1:]:
                            cancel_payload = {
                                "error": "not executed because plan mode reached its per-turn gate limit",
                                "error_code": "E_PLAN_GATE_CANCELLED",
                                "recovery": RECOVERY_FIX_ARGS,
                            }
                            self._emit(
                                {
                                    "kind": "tool_exec_error",
                                    "call_id": skipped.id,
                                    "tool_name": skipped.name,
                                    **cancel_payload,
                                }
                            )
                            self._append_tool_result(skipped.id, cancel_payload)
                            ledger.record_outcome(
                                skipped.name,
                                classify_tool_result(cancel_payload),
                                call_id=skipped.id,
                                blocking_failure=False,
                            )
                            tools_failed += 1
                        # Hard stop: gated calls cost nothing, so neither
                        # BudgetGuard nor the doom-loop guard would ever end
                        # a turn that spins on blocked tools.
                        self._emit(
                            {
                                "kind": "turn_error",
                                "reason": "plan_gate_limit",
                                "tool_name": tc.name,
                                "error": (
                                    f"plan mode blocked {plan_gates_this_turn} tool "
                                    "calls this turn; stopping. Present the plan as "
                                    "text instead of calling mutating tools."
                                ),
                            }
                        )
                        self._emit_turn_wrapup(
                            "plan_gate_limit",
                            tools_succeeded=tools_succeeded,
                            tools_failed=tools_failed,
                            assets_produced=_assets_produced(),
                            tool_name=tc.name,
                        )
                        return
                    should_nudge, streak = self._note_tool_failure(
                        tool_fail_counts, tc.name, "E_PLAN_MODE",
                        limit=_REPEATED_FAILURE_NUDGE_THRESHOLD,
                    )
                    if should_nudge:
                        self._append_repeated_failure_nudge(
                            tc.name, "E_PLAN_MODE", streak
                        )
                    continue

                # Budget gate (cost + time). A fixed per-turn cap cannot be
                # lifted by asking the user; switch to a listed in-budget
                # alternative or disclose the blocker honestly.
                decision = self.budget.check(tc.name)
                if not decision.ok:
                    self._emit(
                        {
                            "kind": "budget_gate",
                            "call_id": tc.id,
                            "tool_name": tc.name,
                            "reason": decision.reason,
                            "alternatives": decision.alternatives,
                            "estimated_cost_usd": decision.estimated_cost_usd,
                            "estimated_eta_sec": decision.estimated_eta_sec,
                        }
                    )
                    budget_payload = {
                        "blocked_by_budget": True,
                        "approval_cannot_override": True,
                        "error": decision.reason,
                        "error_code": "E_BUDGET",
                        "reason": decision.reason,
                        "alternatives": decision.alternatives,
                        "estimated_cost_usd": decision.estimated_cost_usd,
                        "estimated_eta_sec": decision.estimated_eta_sec,
                    }
                    self._append_tool_result(tc.id, budget_payload)
                    ledger.record_outcome(
                        tc.name,
                        classify_tool_result(budget_payload),
                        call_id=tc.id,
                        target_key=call_target,
                        blocking_failure=False,
                    )
                    # A gate is a non-dispatch: count it so the model cannot
                    # spin forever re-requesting an over-budget tool.
                    tools_failed += 1
                    should_nudge, streak = self._note_tool_failure(
                        tool_fail_counts, tc.name, "E_BUDGET",
                        limit=_REPEATED_FAILURE_NUDGE_THRESHOLD,
                    )
                    if should_nudge:
                        self._append_repeated_failure_nudge(tc.name, "E_BUDGET", streak)
                    continue

                # Independent visual_inspections cap. Only enforced for
                # analyze_media.
                if (
                    tc.name == "analyze_media"
                    and visual_inspections_this_turn >= self._max_visual_inspections
                ):
                    cap_reason = (
                        f"max_visual_inspections={self._max_visual_inspections} "
                        f"already reached in this turn; no further thumbnails "
                        f"will be attached."
                    )
                    self._emit(
                        {
                            "kind": "budget_gate",
                            "call_id": tc.id,
                            "tool_name": tc.name,
                            "reason": cap_reason,
                            "alternatives": [],
                        }
                    )
                    cap_payload = {
                        "blocked_by_visual_limit": True,
                        "approval_cannot_override": True,
                        "error": cap_reason,
                        "error_code": "E_VISUAL_CAP",
                        "reason": cap_reason,
                    }
                    self._append_tool_result(tc.id, cap_payload)
                    ledger.record_outcome(
                        tc.name,
                        classify_tool_result(cap_payload),
                        call_id=tc.id,
                        target_key=call_target,
                        blocking_failure=False,
                    )
                    tools_failed += 1
                    should_nudge, streak = self._note_tool_failure(
                        tool_fail_counts, tc.name, "E_VISUAL_CAP",
                        limit=_REPEATED_FAILURE_NUDGE_THRESHOLD,
                    )
                    if should_nudge:
                        self._append_repeated_failure_nudge(tc.name, "E_VISUAL_CAP", streak)
                    continue

                # Durable paid-media reservation.  This happens after all
                # non-dispatch gates and before the provider-facing dispatcher.
                # The child atomically claims the reservation immediately before
                # submit, so replay/concurrency cannot send a duplicate request.
                if (
                    is_paid_media_tool(tc.name)
                    and self.budget.production_media_budget is not None
                    and not (self._remote and tc.name in _REMOTE_DENY_TOOLS)
                ):
                    production_store = self._tool_ctx.extra.get("production_store")
                    project_id = str(self._tool_ctx.extra.get("project_id") or "")
                    policy = {"allowed": True}
                    if (
                        production_store is not None
                        and project_id
                        and tc.name in {"generate_video", "generate_image"}
                    ):
                        policy = production_store.media_policy_decision(
                            project_id,
                            str(self._tool_ctx.extra.get("run_id") or ""),
                            tc.name,
                        )
                    if not bool(policy.get("allowed")):
                        reason = str(policy.get("reason") or "media policy refused")
                        policy_payload = {
                            "blocked_by_media_policy": True,
                            "approval_cannot_override": True,
                            "error": reason,
                            "error_code": "E_MEDIA_POLICY",
                            "reason": reason,
                            "media_policy": policy,
                        }
                        self._emit(
                            {
                                "kind": "tool_exec_error",
                                "call_id": tc.id,
                                "tool_name": tc.name,
                                **policy_payload,
                            }
                        )
                        self._append_tool_result(tc.id, policy_payload)
                        ledger.record_outcome(
                            tc.name,
                            classify_tool_result(policy_payload),
                            call_id=tc.id,
                            target_key=call_target,
                            blocking_failure=False,
                        )
                        tools_failed += 1
                        should_nudge, streak = self._note_tool_failure(
                            tool_fail_counts,
                            tc.name,
                            "E_MEDIA_POLICY",
                            limit=_REPEATED_FAILURE_NUDGE_THRESHOLD,
                        )
                        if should_nudge:
                            self._append_repeated_failure_nudge(
                                tc.name, "E_MEDIA_POLICY", streak
                            )
                        continue
                    requested_duration_sec: float | None = None
                    if tc.name == "generate_video":
                        try:
                            requested_duration_sec = float(
                                min(max(int(round(float(parsed_args.get("duration_sec", 8)))), 1), 8)
                            )
                        except (TypeError, ValueError):
                            requested_duration_sec = 8.0
                    run_id = str(self._tool_ctx.extra.get("run_id") or "run")
                    idempotency_key = (
                        f"{run_id}:{self.session_id}:{self._turn_count}:{tc.id}"
                    )
                    self._tool_ctx.extra["tool_call_context"] = {
                        "trace_id": str(
                            self._tool_ctx.extra.get("active_trace_id")
                            or idempotency_key
                        ),
                        "idempotency_key": idempotency_key,
                        "call_id": tc.id,
                    }
                    provider = (
                        str(parsed_args.get("provider") or "auto")
                        if tc.name == "stock_media"
                        else "vertex"
                    )
                    media_decision = self.budget.reserve_paid_media(
                        tc.name,
                        idempotency_key=idempotency_key,
                        provider=provider,
                        model="",
                        requested_duration_sec=requested_duration_sec,
                    )
                    media_snapshot = self.budget.production_media_budget.snapshot()
                    self._emit(
                        {
                            "kind": "budget_updated",
                            "project_id": self._tool_ctx.extra.get("project_id"),
                            "run_id": self._tool_ctx.extra.get("run_id"),
                            "budget": media_snapshot,
                            "reservation": media_decision.to_dict(),
                        }
                    )
                    if not media_decision.ok or media_decision.reservation is None:
                        reason = media_decision.reason or "production media budget refused"
                        media_payload = {
                            "blocked_by_budget": True,
                            "approval_cannot_override": True,
                            "error": reason,
                            "error_code": "E_BUDGET",
                            "reason": reason,
                            "alternatives": decision.alternatives,
                            "estimated_cost_usd": decision.estimated_cost_usd,
                            "estimated_eta_sec": decision.estimated_eta_sec,
                        }
                        self._emit(
                            {
                                "kind": "budget_gate",
                                "call_id": tc.id,
                                "tool_name": tc.name,
                                **media_payload,
                            }
                        )
                        self._append_tool_result(tc.id, media_payload)
                        ledger.record_outcome(
                            tc.name,
                            classify_tool_result(media_payload),
                            call_id=tc.id,
                            target_key=call_target,
                            blocking_failure=False,
                        )
                        tools_failed += 1
                        continue
                    self._tool_ctx.extra[PAID_MEDIA_CONTEXT_KEY] = (
                        self.budget.paid_media_call_context(
                            media_decision.reservation.reservation_id
                        ).to_dict()
                    )

                # Real dispatch ------------------------------------------------
                self._emit(
                    {
                        "kind": "tool_exec_start",
                        "call_id": tc.id,
                        "tool_name": tc.name,
                        "est_cost_usd": decision.estimated_cost_usd,
                        "eta_seconds": decision.estimated_eta_sec,
                    }
                )
                self._tool_ctx.emit_progress = self._make_progress_cb(tc.id, tc.name)
                # The spawn_subtasks dispatcher anchors its children's SSE events
                # (subagent_start/result + child tool_exec_*) to THIS call's id.
                self._tool_ctx.extra["call_id"] = tc.id
                pre_dispatch_asset_ids = {
                    record.asset_id for record in self.registry.list_records()
                }

                if self._remote and tc.name in _REMOTE_DENY_TOOLS:
                    self._tool_ctx.extra.pop(PAID_MEDIA_CONTEXT_KEY, None)
                    denied_payload = {
                        "error": f"tool '{tc.name}' is disabled in this shared demo",
                        "error_code": "E_REMOTE_BLOCKED",
                        "recovery": RECOVERY_FIX_ARGS,
                    }
                    self._emit({
                        "kind": "tool_exec_error",
                        "call_id": tc.id,
                        "tool_name": tc.name,
                        **denied_payload,
                    })
                    self._append_tool_result(tc.id, denied_payload)
                    ledger.record_outcome(
                        tc.name,
                        classify_tool_result(denied_payload),
                        call_id=tc.id,
                        target_key=call_target,
                    )
                    tools_failed += 1
                    continue
                start_ts = time.monotonic()
                try:
                    execute_registered = self._tool_ctx.extra.get(
                        "execute_registered_capability"
                    )
                    if callable(execute_registered):
                        result = await execute_registered(
                            tc.name, parsed_args, self._tool_ctx
                        )
                    else:
                        dispatcher = (
                            self.capabilities.dispatcher(tc.name)
                            if self.capabilities is not None
                            else DISPATCHER[tc.name]
                        )
                        result = await dispatcher(parsed_args, self._tool_ctx)
                except Exception as exc:
                    elapsed = time.monotonic() - start_ts
                    self.budget.commit(
                        tc.name, actual_seconds=_commit_seconds(tc.name, elapsed)
                    )
                    outcome = classify_tool_exception(exc)
                    err_payload = outcome.error_payload(tool_name=tc.name)
                    err_code = str(outcome.error_code or "E_TOOL_FAILED")
                    recovery = outcome.recovery
                    self._emit(
                        {
                            "kind": "tool_exec_error",
                            "call_id": tc.id,
                            "tool_name": tc.name,
                            "elapsed_seconds": elapsed,
                            **err_payload,
                        }
                    )
                    self._append_tool_result(
                        tc.id, {**err_payload, "tool_name": tc.name}
                    )
                    ledger.record_outcome(
                        tc.name,
                        outcome,
                        call_id=tc.id,
                        target_key=call_target,
                        call_args=parsed_args,
                    )
                    tools_failed += 1
                    limit = (
                        _TRANSIENT_RETRY_NUDGE_THRESHOLD
                        if recovery == RECOVERY_TRANSIENT_RETRY
                        else _REPEATED_FAILURE_NUDGE_THRESHOLD
                    )
                    should_nudge, streak = self._note_tool_failure(
                        tool_fail_counts, tc.name, err_code, limit=limit
                    )
                    if should_nudge:
                        self._append_repeated_failure_nudge(tc.name, err_code, streak)
                    continue
                finally:
                    self._tool_ctx.extra.pop(PAID_MEDIA_CONTEXT_KEY, None)
                    self._tool_ctx.extra.pop("tool_call_context", None)

                elapsed = time.monotonic() - start_ts
                self.budget.commit(
                    tc.name, actual_seconds=_commit_seconds(tc.name, elapsed)
                )
                outcome = classify_tool_result(result)
                new_dispatch_asset_ids = [
                    record.asset_id
                    for record in self.registry.list_records()
                    if record.asset_id not in pre_dispatch_asset_ids
                ]
                artifact_kinds = {
                    asset_id: self.registry.get(asset_id).kind
                    for asset_id in new_dispatch_asset_ids
                    if self.registry.contains(asset_id)
                }
                ledger.record_outcome(
                    tc.name,
                    outcome,
                    call_id=tc.id,
                    mutation=(
                        True
                        if new_dispatch_asset_ids and tc.name not in MASTER_TOOL_SET
                        else None
                    ),
                    target_key=call_target,
                    artifact_kinds=artifact_kinds,
                    call_args=parsed_args,
                    blocking_failure=not (
                        tc.name == "elicit"
                        and str(outcome.error_code or "")
                        == E_CLARIFICATION_POLICY
                    ),
                )
                if outcome.is_failure:
                    err_payload = outcome.error_payload(tool_name=tc.name)
                    err_code = str(outcome.error_code or "E_TOOL_FAILED")
                    self._emit(
                        {
                            "kind": "tool_exec_error",
                            "call_id": tc.id,
                            "tool_name": tc.name,
                            "elapsed_seconds": elapsed,
                            **err_payload,
                        }
                    )
                    self._append_tool_result(
                        tc.id, {**err_payload, "tool_name": tc.name}
                    )
                    tools_failed += 1
                    limit = (
                        _TRANSIENT_RETRY_NUDGE_THRESHOLD
                        if outcome.recovery == RECOVERY_TRANSIENT_RETRY
                        else _REPEATED_FAILURE_NUDGE_THRESHOLD
                    )
                    should_nudge, streak = self._note_tool_failure(
                        tool_fail_counts, tc.name, err_code, limit=limit
                    )
                    if should_nudge:
                        self._append_repeated_failure_nudge(tc.name, err_code, streak)
                    continue

                # Only terminal success repairs this tool's failure streak.
                # pending/noop/partial are honest non-failures, but treating
                # them as recovery would hide a still-unresolved failure.
                if outcome.state == "success":
                    tool_fail_counts.pop(tc.name, None)
                    tools_succeeded += 1
                    self._advance_production_after_tool(tc.name, result)
                # Progress is assessed once for the complete assistant tool
                # batch below. Per-call resets can otherwise hide a later
                # irrelevant/noop call and prevent deterministic expansion.

                # ---- success-blind doom-loop guard -------------------
                # Ported from opencode processor.ts (DOOM_LOOP_THRESHOLD=3):
                # the per-tool direction check above only tracks FAILURES, but a
                # turn can also get stuck re-issuing a call that keeps DISPATCHING
                # (succeeding, or returning a result the model ignores) with the
                # exact same arguments forever — pure echo, no progress. Record
                # each dispatched call as (tool_name, byte-identical raw args).
                # If the last _DOOM_LOOP_THRESHOLD dispatched calls are identical,
                # the turn is looping on itself — emit a structured turn_error and
                # stop. This is independent of the RESULT content (success-blind):
                # distinct args (real work) never trip it. Like opencode, a call
                # that did not actually dispatch (raised / gated / bad-JSON args)
                # is not recorded here — those stay owned by failure-direction checks.
                # Polling verbs are exempt (see _DOOM_LOOP_EXEMPT_TOOLS):
                # identical repeated check_job calls are legal waiting.
                if tc.name not in _DOOM_LOOP_EXEMPT_TOOLS:
                    recent_tool_calls.append((tc.name, tc.args))
                doom_loop_detected = self._is_doom_loop(recent_tool_calls)

                # Model-facing tool_result: strip thumbnail_path (file
                # path leakage), keep thumbnail_for_next_message=False
                # in the model copy too (the thumbnail itself is going
                # in a separate user message; the model doesn't need a
                # flag).
                model_result = {
                    k: v
                    for k, v in result.items()
                    if k not in {"thumbnail_path", "thumbnail_for_next_message"}
                }
                # SSE result also strips file paths; replaces with a
                # preview_uri pointing at the produced asset's on-disk
                # path so the frontend can render a preview.
                event_result = dict(model_result)
                produced_id = result.get("asset_id")
                if produced_id and self.registry.contains(str(produced_id)):
                    event_result["preview_uri"] = str(
                        self.registry.get(str(produced_id)).path
                    )

                self._emit(
                    {
                        "kind": "tool_exec_result",
                        "call_id": tc.id,
                        "tool_name": tc.name,
                        "result": event_result,
                        "elapsed_seconds": elapsed,
                    }
                )
                self._append_tool_result(tc.id, model_result)

                # ---- post-edit self-correction (opencode pattern #2) ----
                # After a SUCCESSFUL *mutating* lumen verb, append a compact
                # POST-STATE digest (layer-tree summary + validate_doc
                # warnings) to the tool_result the model just received, so it is
                # grounded in the new layer state right where it edited —
                # mirroring opencode appending LSP diagnostics after edits.
                # ADDITIVE, cheap, and non-fatal: any failure here must never
                # break the loop, so the whole thing is wrapped in try/except.
                if _is_mutating_lumen_tool(tc.name):
                    try:
                        self._append_lumen_post_state(tc.id)
                    except Exception:  # noqa: BLE001 — never break the turn
                        pass

                # Plan-B visual feedback. ONLY triggered by a
                # dispatcher-flagged result — there is no keyword
                # detection here. Today this is exclusively
                # analyze_media; the host does not auto-decide to show
                # thumbnails for any other tool.
                if result.get("thumbnail_for_next_message") and result.get(
                    "thumbnail_path"
                ):
                    visual_inspections_this_turn += 1
                    self._pending_thumbnails.append(Path(result["thumbnail_path"]))

                if doom_loop_detected:
                    # The current call is fully settled above. Settle every
                    # remaining call from the same assistant message as a
                    # structured cancellation before stopping; otherwise the
                    # next provider request contains orphan tool calls.
                    for skipped in accum.tool_calls[tc_position + 1:]:
                        cancel_payload = {
                            "error": "not executed because the host stopped a repeated-call loop",
                            "error_code": "E_DOOM_LOOP_CANCELLED",
                            "recovery": RECOVERY_FIX_ARGS,
                        }
                        self._emit(
                            {
                                "kind": "tool_exec_error",
                                "call_id": skipped.id,
                                "tool_name": skipped.name,
                                **cancel_payload,
                            }
                        )
                        self._append_tool_result(skipped.id, cancel_payload)
                        ledger.record_outcome(
                            skipped.name,
                            classify_tool_result(cancel_payload),
                            call_id=skipped.id,
                        )
                        tools_failed += 1
                    self._emit_doom_loop(tc.name, _DOOM_LOOP_THRESHOLD)
                    self._emit_turn_wrapup(
                        "doom_loop",
                        tools_succeeded=tools_succeeded,
                        tools_failed=tools_failed,
                        assets_produced=_assets_produced(),
                        tool_name=tc.name,
                    )
                    return

            # After the dispatch sub-loop, inject queued thumbnails as
            # a multimodal user message before the next model call.
            if self._pending_thumbnails:
                thumbnail_msg = {
                    "role": "user",
                    "content": _thumbnail_user_content(self._pending_thumbnails),
                }
                self._messages.append(thumbnail_msg)
                one_shot_image_messages.append(thumbnail_msg)
                self._pending_thumbnails = []

            # Loop: call the model again with updated messages.

    # ── progress callback factory ────────────────────────────────────

    def _make_progress_cb(
        self, call_id: str, tool_name: str
    ) -> Callable[[ProgressUpdate], None]:
        emit = self._emit

        def cb(update: ProgressUpdate) -> None:
            event: dict[str, Any] = {
                "kind": "tool_exec_progress",
                "call_id": call_id,
                "tool_name": tool_name,
            }
            if update.percent is not None:
                event["percent"] = update.percent
            if update.message:
                event["message"] = update.message
            if update.eta_sec is not None:
                event["eta_seconds"] = update.eta_sec
            emit(event)

        return cb


__all__ = [
    "AgentLoopV3",
    "_DOOM_LOOP_THRESHOLD",
    "_DisplayStreamGate",
    "_MAX_CONSECUTIVE_TOOL_FAILURES",
    "_REPEATED_FAILURE_NUDGE_THRESHOLD",
    "_activity_text_from_model_preamble",
    "_progress_report_from_model_preamble",
    "_strip_activity_markup",
]
