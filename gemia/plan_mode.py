"""Plan mode: a per-session read-only planning gate for AgentLoopV3.

While plan mode is ON the model may only inspect state — every tool that
mutates anything (timeline/layer document, media library, annotations,
memory, skills, files), registers a new asset in the session registry, spawns
or pays for generation jobs, exports, or runs arbitrary code is blocked by
the host. The model's deliverable is a concrete plan the user approves; the
client then turns plan mode off and asks for execution.

The allow/block split below was derived by reading every dispatcher
implementation, not from tool names. Notably, several "inspection" tools are
BLOCKED because they register user-visible session assets as a side effect
(``inspect_timeline`` renders a draft export + frame assets, ``extract_frame``
/ ``render_preview`` / ``lumen_render`` / ``lumen_render_range`` /
``lumen_seek`` register derived media, ``lumen_select`` saves the layer doc),
and ``remember`` / ``log_note`` write durable memory files. ``spawn_subtasks``
is BLOCKED because it reserves budget, launches model-driven children whose
profiles include mutating tools (``annotate_media`` / ``write_media_annotation``)
and register derived assets (``extract_frame``) — three independently
disqualifying side effects; children additionally re-read this live flag per
dispatch (defense in depth for mid-batch toggles). Planning-quality inspection
stays available through ``get_timeline`` / ``get_lumenframe`` / ``probe_media`` /
``analyze_media`` (whose thumbnail is NOT registered) / ``search_library`` /
``get_media_annotations``.

Fail closed: a tool name in neither set (e.g. a newly added tool nobody
classified yet) is treated as blocked. ``tests/test_plan_mode.py`` asserts
the two sets exactly cover ``TOOL_NAMES`` so a new tool fails loudly there.
"""
from __future__ import annotations

# Pure reads / state queries / user interaction. Safe while planning.
PLAN_ALLOWED_TOOLS = frozenset({
    "align_audio",          # audio analysis; detects sync offsets
    "analyze_media",        # ffprobe + unregistered thumbnail only
    "check_job",            # polls an already-submitted job
    "detect_beats",         # audio analysis; detects beats/onsets
    "elicit",               # ask the user; no state written
    "file_list",
    "file_read",
    "get_shotlist",
    "get_lumenframe",
    "get_media_annotations",
    "get_safe_areas",
    "get_timeline",
    "list_dir",
    "probe_media",
    "read_file",
    "recall_skills",
    "search_frames",       # probes existing footage; surfaces (no new derived media)
    "search_library",
    "search_media",
    "get_shotlist",         # read the storyboard plan
    "wait_for_job",         # waits on an already-submitted job
    "web_open",
    "web_search",
})

# Everything that edits, generates, registers assets, writes files/memory,
# exports, or executes code.
PLAN_BLOCKED_TOOLS = frozenset({
    "add_overlay", "adjust_media", "animate_captions", "annotate_media",
    "arrange_timeline", "assemble_shotlist",
    "build", "color_grade", "composite", "copy_in", "draft_shotlist",
    "edit_audio", "edit_image", "edit_video", "export",
    "extract_frame", "fetch", "file_copy", "file_delete", "file_move",
    "file_write", "generate_audio", "generate_image", "generate_video",
    "inspect_lottie", "inspect_timeline", "log_note",
    "lumen_add_layer", "lumen_delete_layer", "lumen_key",
    "lumen_merge_compositions", "lumen_move_layer", "lumen_patch",
    "lumen_render", "lumen_render_range", "lumen_retime_segment",
    "lumen_reverse", "lumen_ripple_delete", "lumen_seek", "lumen_select",
    "lumen_set_lane", "lumen_set_mask", "lumen_set_opacity",
    "lumen_set_range", "lumen_set_transform", "lumen_set_visibility",
    "lumen_set_work_area", "lumen_speed_ramp", "lumen_time_remap",
    "mix_audio", "move_file", "organize_files",
    "paint_mask_effect", "paint_overlay",
    "project_export", "project_export_otio", "project_import_otio",
    "narrate", "refine_shot", "remember", "render_preview", "run_shell",
    "save_skill", "set_shotlist", "smart_reframe", "spawn_subtasks",
    "subtitle",
    "timeline_add_track", "timeline_add_transition", "timeline_delete_clip",
    "timeline_insert_clip", "timeline_move_clip", "timeline_set_clip_effects",
    "timeline_set_clip_time", "timeline_set_track", "timeline_split_clip",
    "timeline_trim_clip", "timeline_undo",
    "transform_geometry", "update_shot", "write_file", "write_media_annotation",
})

# A turn that keeps hammering blocked tools is not planning — it is stuck.
# The per-(tool, code) repeated-failure nudge fires first (streak 5); this is
# the host-side hard stop across ALL blocked calls in one turn.
PLAN_GATE_TURN_LIMIT = 8


def is_plan_safe(tool_name: str) -> bool:
    """True if ``tool_name`` may dispatch while plan mode is on. Fail closed:
    unclassified names are blocked."""
    return tool_name in PLAN_ALLOWED_TOOLS


def plan_gate_message(tool_name: str) -> str:
    return (
        f"Plan mode is ON: '{tool_name}' is blocked because it would change "
        "state (edit/generate/write/export/execute). Do not retry it. "
        "Inspect with the read-only tools if needed, then present your plan "
        "as text: numbered steps, the tool for each step with its key "
        "arguments, expected intermediate assets, rough cost/time for paid "
        "generation, and any open questions. The user will approve the plan "
        "to unlock execution."
    )


# The allowed-tool list inside the prompt is generated from the frozenset so
# the prose can never drift from the actual gate (test_plan_mode anchors it).
_ALLOWED_TOOLS_TEXT = ", ".join(sorted(PLAN_ALLOWED_TOOLS))

# Injected into system_v3.md's {{plan_mode}} slot while the flag is on
# (replaced with "" when off). It must explicitly suspend the "Act — do not
# instruct" rule, which otherwise calls a text-only reply a failed turn.
PLAN_MODE_PROMPT = f"""\
## ⚠ PLAN MODE — planning only, no changes

The user switched this session into plan mode. Until they approve a plan:

- You may ONLY inspect and discuss. Available tools: {_ALLOWED_TOOLS_TEXT}.
- EVERY other tool (edits, generation, timeline/layer changes, painting,
  file writes, memory writes, export, build/run_shell) is blocked by the
  host and returns `blocked_by_plan_mode`. Do not call blocked tools —
  plan around them.
- Your deliverable THIS turn is a concrete plan: numbered steps, which tool
  each step uses and its key arguments, the intermediate assets you expect,
  rough cost/time for paid generation steps, and open questions if any.
- While plan mode is on, the "Act — do not instruct" rule is suspended for
  mutating work: presenting the plan IS the successful outcome of the turn.
  Do not apologize for not executing.
- End the turn by presenting the plan and asking the user to approve it via
  the plan-mode controls (or to keep refining it with you). Never busy-wait
  or re-issue blocked calls.
"""

__all__ = [
    "PLAN_ALLOWED_TOOLS",
    "PLAN_BLOCKED_TOOLS",
    "PLAN_GATE_TURN_LIMIT",
    "PLAN_MODE_PROMPT",
    "is_plan_safe",
    "plan_gate_message",
]
