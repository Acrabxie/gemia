"""Single source of truth for the v3 web/CLI protocol surface.

Phase 1 of docs/protocol-parity-plan.md: every SSE event kind, ask control
type, recovery hint, and stable error code the two frontends may see is
declared HERE first. ``scripts/export_contract.py`` renders this module to
``static/v3/contract.json`` and vendors a copy into the CLI repo; drift tests
on both ends (``tests/test_v3_contract.py`` and lumeri-cli
``test/contract.mjs``) red when an emit site, handler table, or the exported
JSON disagrees with this module.

Rules (from the parity plan):
- New event kinds are added here BEFORE the code that emits them.
- Emit sites use literal kind strings only (the backend test enforces this),
  so this list can be verified by static extraction.
- Unknown kinds must render a visible banner on both frontends — silent
  drops are bugs.
"""
from __future__ import annotations

# Bumped when the protocol surface changes shape (not for additive kinds —
# clients render unknown kinds as banners, so additions are non-breaking).
PROTOCOL_VERSION = 1

# Every SSE event kind any backend code path may emit. Emit sites live in
# exactly five files: agent_loop_v3.py, transport/sse.py (replay_gap),
# tools/_ask_bridge.py (ask_question), v3_routes.py, and subtasks.py
# (subagent_start / subagent_result + child tool_exec_* with agent_id).
EVENT_KINDS: frozenset[str] = frozenset({
    "turn_start",
    "model_text_delta",
    "model_tool_call_start",
    "model_tool_call_ready",
    "tool_exec_start",
    "tool_exec_progress",
    "tool_exec_result",
    "tool_exec_error",
    # Multi-agent fan-out (docs/multi-agent-plan.md §6). A spawn_subtasks call
    # launches N bounded children; each child opens with exactly one
    # subagent_start and closes with exactly one subagent_result. Child TOOL
    # activity rides the EXISTING tool_exec_* kinds carrying an optional
    # ``agent_id`` field (absent = the root/parent loop). There is deliberately
    # NO subagent_progress kind — a synthetic child-percent would be fabricated
    # narration (transport/sse.py invariant 1).
    "subagent_start",
    "subagent_result",
    "timeline_op",
    "budget_gate",
    "plan_gate",
    "plan_mode_changed",
    "completion_check",
    "turn_wrapup",
    "turn_complete",
    "turn_error",
    "ask_question",
    "replay_gap",
    # Per-connection synthetic frame at the top of every SSE stream (id-less,
    # never in the replay buffer): {"kind": "protocol_hello", "protocol_version": N}.
    "protocol_hello",
})

# Control types an ask_question payload may carry (gemia/tools/ask.py
# AskControlType). The CLI answers all of them; web is display-only for now
# (a documented, deliberate gap — see the parity plan).
ASK_CONTROLS: frozenset[str] = frozenset({
    "select",
    "multi_select",
    "text",
    "slider",
    "panel",
    "custom_panel",
})

# The only error field clients BRANCH on (web RECOVERABLE_RECOVERY set, CLI
# retry affordances). Values from gemia/errors.py _RECOVERY_VALUES.
RECOVERY: frozenset[str] = frozenset({
    "fix_args",
    "switch_tool",
    "transient_retry",
    "none",
})

# Stable error codes: the errors.py class hierarchy plus protocol-relevant
# gate/ask codes. Tool-local inline codes (E_BAD_ARG variants etc.) are
# deliberately NOT frozen — clients treat error_code as an opaque display
# chip, so freezing every string would make each new tool error a contract
# bump for zero client benefit.
ERROR_CODES: frozenset[str] = frozenset({
    "E_CONFIG",
    "E_AI",
    "E_MEDIA",
    "E_PLAN",
    "E_INPUT",
    "E_CANCELLED",
    "E_TOOL",
    "E_BUDGET",
    "E_PLAN_MODE",
    "E_BUSY",
    "E_ASK",
    "E_ASK_INVALID_ANSWER",
    "E_ASK_INVALID_CHOICE",
    "E_ASK_INVALID_SCHEMA",
    "E_ASK_INVALID_TYPE",
})


def as_dict() -> dict:
    """Deterministic JSON-ready rendering (sorted lists, not sets)."""
    return {
        "protocol_version": PROTOCOL_VERSION,
        "event_kinds": sorted(EVENT_KINDS),
        "ask_controls": sorted(ASK_CONTROLS),
        "recovery": sorted(RECOVERY),
        "error_codes": sorted(ERROR_CODES),
    }


__all__ = [
    "PROTOCOL_VERSION",
    "EVENT_KINDS",
    "ASK_CONTROLS",
    "RECOVERY",
    "ERROR_CODES",
    "as_dict",
]
