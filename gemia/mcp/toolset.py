"""The frozen, curated MCP tool surface (docs/mcp-interface-plan.md §2.3, D3).

Exposing all ~95 internal verbs across an unauthenticated agent boundary would
(a) blow client context windows, (b) expose ``run_shell`` / file-write before
the security pass, and (c) surface Lumeri-internal plumbing (memory, skills,
ask-bridge) that makes no sense to an external agent. The MCP surface is a
**frozen curated set** with its own single source of truth (this module) and
its own exact drift test (``tests/test_mcp_server.py``) — the same
mechanism-over-memory philosophy as ``plan_mode``'s exact-coverage test,
applied to a different frozen set.

Naming rule (D3): MCP tool names are **byte-identical to internal verb names**
(``probe_media``, ``timeline_insert_clip``, …). The plan gate and budget gate
look tools up by exact name, so identical names mean zero mapping tables to
drift. MCP clients already namespace (Claude Code renders
``mcp__lumeri__probe_media``), so a ``lumeri_*`` prefix would only force a
strip/translate layer in front of both gates — rejected.

Schema derivation rule (D3, mechanical — no hand-typing): for each 1:1 verb,
the MCP ``inputSchema`` is the verb's ``TOOL_SCHEMAS`` entry's
``function.parameters`` with one injected ``session_id`` string property,
prepended to ``required``. ``mcp_input_schema`` performs that transform; a
drift test asserts the equality.
"""
from __future__ import annotations

from typing import Any

from gemia.tools._schema import TOOL_SCHEMAS

# ── the 5 MCP-native lifecycle/import tools ─────────────────────────────────
# These exist ONLY at the MCP layer — they have no internal verb / dispatcher
# entry. They wrap SessionManager / SessionRunner methods directly (see
# gemia/mcp/server.py). They are NOT routed through ``run_verb``; the plan/budget
# gates do not apply to them (they are session lifecycle, not creative work).
MCP_NATIVE_TOOLS: frozenset[str] = frozenset(
    {
        "create_session",  # -> SessionManager.create_session
        "list_sessions",   # -> SessionManager.list_sessions
        "get_session",     # -> assets + plan_mode + BudgetGuard.snapshot + PROTOCOL_VERSION
        "close_session",   # -> SessionManager.close_session
        "import_media",     # -> SessionRunner.add_external_asset (absolute local path)
    }
)

# Of the native tools, which are safe to call while a session is in plan mode.
# ``import_media`` registers a session asset (same rationale as ``copy_in`` —
# plan_mode.py:13-15) so it is plan-BLOCKED; the other four are read/lifecycle.
MCP_NATIVE_PLAN_SAFE: frozenset[str] = frozenset(
    {
        "create_session",
        "list_sessions",
        "get_session",
        "close_session",
    }
)

# ── the 1:1 curated verbs (names byte-identical to internal verbs) ──────────
# Phase 1 = the "read + timeline" set. Phase 2 rows are listed for the frozen
# full surface but are NOT part of PHASE1_TOOLSET.
_PHASE1_1TO1: frozenset[str] = frozenset(
    {
        # media (read)
        "probe_media",
        "analyze_media",
        "search_library",
        # timeline (read + edit)
        "get_timeline",
        "timeline_insert_clip",
        "timeline_move_clip",
        "timeline_trim_clip",
        "timeline_split_clip",
        "timeline_delete_clip",
        "timeline_add_transition",
        "timeline_undo",
        # lumen / annotations (read)
        "get_lumenframe",
        "get_media_annotations",
    }
)

# Phase 2 1:1 verbs — frozen in the full surface, added to PHASE1 later.
_PHASE2_1TO1: frozenset[str] = frozenset(
    {
        "lumen_add_layer",
        "lumen_patch",
        "lumen_delete_layer",
        "render_preview",
        "project_export",
        "extract_frame",
        "write_media_annotation",
    }
)

# The Phase 1 MCP surface actually exposed by the stdio server: the 13 read+
# timeline 1:1 verbs plus the 5 native lifecycle/import tools = 18 tools.
PHASE1_TOOLSET: frozenset[str] = _PHASE1_1TO1 | MCP_NATIVE_TOOLS

# The full frozen 1:1 verb surface (Phase 1 + Phase 2), used by ``run_verb``'s
# membership check so an excluded verb stays excluded even if someone calls
# ``run_verb`` directly. Native tools are NOT in here — they never route through
# ``run_verb`` (they are not internal verbs).
MCP_TOOLSET: frozenset[str] = _PHASE1_1TO1 | _PHASE2_1TO1

# Read-only 1:1 verbs (used to set MCP ``readOnlyHint`` and to decide, in
# ``run_verb``, whether a verb may interleave with an in-flight agent turn).
# These are exactly the plan-allowed members of the curated set — a verb that
# mutates is never "read-only". Kept explicit (not derived from PLAN_ALLOWED_
# TOOLS) so this module stays the single source of truth for the MCP surface.
MCP_READ_ONLY: frozenset[str] = frozenset(
    {
        "probe_media",
        "analyze_media",
        "search_library",
        "get_timeline",
        "get_lumenframe",
        "get_media_annotations",
    }
)

# Destructive 1:1 verbs (used to set MCP ``destructiveHint``).
MCP_DESTRUCTIVE: frozenset[str] = frozenset(
    {
        "timeline_delete_clip",
        "lumen_delete_layer",
    }
)

# ── Exclusions and why (documented verbatim per D3) ─────────────────────────
# elicit / ask-bridge: a model→human question routed over SSE ``ask_question``.
#   Across MCP the caller *is* an agent and there is no Lumeri-owned human
#   channel. MCP's own elicitation (2025-06-18) is an optional client
#   capability with patchy support — revisit after Phase 3.
# run_shell, build, check_job, wait_for_job, file_write, file_copy, file_move,
#   file_delete, write_file, move_file, organize_files: arbitrary code
#   execution / filesystem mutation offered to an unauthenticated localhost
#   caller — excluded until the security pass (same deferral as D2).
# remember, log_note, save_skill, recall_skills: Lumeri-private memory/skill
#   store. External agents have their own memory; letting them write ours
#   pollutes cross-session state invisibly.
# generate_image, generate_video, generate_audio, narrate: real provider spend
#   triggered by an unauthenticated caller. The $5 session cap still holds, but
#   pre-auth we keep spend behind Lumeri's own loop only.
# web_search, web_open, fetch: every MCP-capable client already has better web
#   tools; pure surface noise.
# Remaining one-shot ffmpeg verbs (edit_video, composite, color_grade,
#   adjust_media, arrange_timeline, mix_audio, edit_audio, edit_image,
#   transform_geometry, smart_reframe, subtitle, paint_*, shotlist/lottie/OTIO
#   verbs…): not excluded on principle — excluded to keep the surface
#   learnable. The timeline + lumen document verbs are the canonical editing
#   path; the one-shot verbs can be added individually later without any design
#   change (this adapter is table-driven).

_SESSION_ID_PROP: dict[str, Any] = {
    "type": "string",
    "description": "Session id from create_session.",
}

# Index the internal verb schemas once for the mechanical transform.
_VERB_PARAMS: dict[str, dict[str, Any]] = {
    s["function"]["name"]: s["function"]["parameters"] for s in TOOL_SCHEMAS
}
_VERB_DESCRIPTIONS: dict[str, str] = {
    s["function"]["name"]: s["function"].get("description", "") for s in TOOL_SCHEMAS
}


def internal_verb_description(tool_name: str) -> str:
    """The internal verb's description, verbatim (used as the MCP tool's
    ``description``)."""
    return _VERB_DESCRIPTIONS.get(tool_name, "")


def mcp_input_schema(tool_name: str) -> dict[str, Any]:
    """Mechanically derive the MCP ``inputSchema`` for a 1:1 verb (D3).

    = the verb's ``TOOL_SCHEMAS`` ``function.parameters`` with one injected
    ``session_id`` string property prepended to ``required``. No hand-typing.

    A fresh dict is returned each call (the transform must never mutate the
    frozen ``TOOL_SCHEMAS`` data). Raises ``KeyError`` for unknown verbs.
    """
    params = _VERB_PARAMS[tool_name]
    props = dict(params.get("properties") or {})
    required = list(params.get("required") or [])
    # session_id first in properties (dict insertion order) and in required.
    merged_props: dict[str, Any] = {"session_id": dict(_SESSION_ID_PROP)}
    merged_props.update(props)
    merged_required = ["session_id"] + [r for r in required if r != "session_id"]
    schema: dict[str, Any] = {
        "type": "object",
        "properties": merged_props,
        "required": merged_required,
    }
    # Preserve any additional top-level schema keys the verb declared
    # (e.g. additionalProperties) — additive, never dropping data.
    for key, value in params.items():
        if key not in {"type", "properties", "required"}:
            schema[key] = value
    return schema


__all__ = [
    "MCP_TOOLSET",
    "MCP_NATIVE_TOOLS",
    "MCP_NATIVE_PLAN_SAFE",
    "MCP_READ_ONLY",
    "MCP_DESTRUCTIVE",
    "PHASE1_TOOLSET",
    "internal_verb_description",
    "mcp_input_schema",
]
