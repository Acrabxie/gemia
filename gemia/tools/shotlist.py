"""shotlist_*: the model's read/write interface to the storyboard IR.

Outline/storyboard-driven editing works like this:

    1. The model turns the user's outline/brief into a structured shotlist and
       calls ``set_shotlist`` — scenes → shots, each shot carrying its *intent*
       (what to show, how long, on-screen text) and a fill *strategy*
       (search real footage first, generate only as fallback).
    2. Per shot it finds footage (``search_media``) or generates it, then marks
       the shot ``filled`` with ``update_shot`` (asset_id + source).
    3. ``assemble_shotlist`` lays the filled shots onto the timeline.

The shotlist lives inside project_state and every mutation flows through
``ctx.project.apply_ops`` — so drafts are versioned, auditable, and undoable
via ``timeline_undo`` exactly like timeline edits. Mutations return a compact
text view of the post-state so the model does not need a follow-up read.
"""
from __future__ import annotations

from typing import Any

from gemia.project_model import iter_shots
from gemia.tools._context import ToolContext


def _project(ctx: ToolContext):
    if ctx.project is None:
        raise ValueError(
            "shotlist verbs need a project-backed session (ctx.project is None)"
        )
    return ctx.project


def _shot_line(scene: dict[str, Any], shot: dict[str, Any]) -> str:
    bits = [f"  [{shot.get('id')}]", str(shot.get("status") or "draft"),
            f"{float(shot.get('duration_sec') or 0):.1f}s"]
    source = str(shot.get("source") or "unset")
    if shot.get("asset_id"):
        bits.append(f"{source}→{shot['asset_id']}")
    elif source == "search" and shot.get("search_query"):
        bits.append(f'search "{shot["search_query"]}"')
    else:
        bits.append(source)
    if shot.get("clip_id"):
        bits.append(f"clip:{shot['clip_id']}")
    desc = str(shot.get("description") or "").strip()
    line = " ".join(bits)
    if desc:
        line += f" — {desc}"
    if shot.get("on_screen_text"):
        line += f'  txt:"{shot["on_screen_text"]}"'
    if shot.get("mood"):
        line += f"  ~{shot['mood']}"
    if shot.get("narration"):
        line += f'  vo:"{shot["narration"]}"'
    if shot.get("transition_after"):
        t = shot["transition_after"]
        line += f"  ⇥{t.get('kind')}"
    return line


def render_shotlist_text(shotlist: dict[str, Any]) -> str:
    """One-screen human/model-readable view of the storyboard."""
    shotlist = shotlist or {}
    scenes = shotlist.get("scenes") or []
    if not scenes:
        return "(shotlist empty — call set_shotlist to draft scenes/shots)"
    head_bits = []
    if shotlist.get("logline"):
        head_bits.append(f"logline: {shotlist['logline']}")
    if shotlist.get("style"):
        head_bits.append(f"style: {shotlist['style']}")
    if shotlist.get("target_duration_sec"):
        head_bits.append(f"target: {shotlist['target_duration_sec']}s")
    lines = [" | ".join(head_bits)] if head_bits else []
    planned = 0.0
    for scene in scenes:
        if not isinstance(scene, dict):
            continue
        title = str(scene.get("title") or "")
        lines.append(f'Scene "{title}" ({scene.get("id")}):' if title else f"Scene ({scene.get('id')}):")
        for shot in scene.get("shots") or []:
            if isinstance(shot, dict):
                lines.append(_shot_line(scene, shot))
                planned += float(shot.get("duration_sec") or 0)
    lines.append(f"— {sum(1 for _ in iter_shots(shotlist))} shots, ~{planned:.1f}s planned")
    return "\n".join(lines)


def _summary(ctx: ToolContext, result: dict[str, Any], **extra: Any) -> dict[str, Any]:
    shotlist = (ctx.project.load() if ctx.project else {}).get("shotlist") or {}
    out = {
        "applied": True,
        "seq": result.get("patch_seq_end"),
        "shot_count": sum(1 for _ in iter_shots(shotlist)),
        "shotlist": render_shotlist_text(shotlist),
    }
    out.update(extra)
    return out


# ── write ───────────────────────────────────────────────────────────────
async def dispatch_set(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    shotlist = args.get("shotlist")
    if not isinstance(shotlist, dict):
        raise ValueError(
            "set_shotlist requires a 'shotlist' object with scenes[].shots[]"
        )
    project = _project(ctx)
    result = project.apply_ops(
        [{"op": "set_shotlist", "shotlist": shotlist}], label="set_shotlist"
    )
    return _summary(ctx, result)


async def dispatch_update_shot(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    shot_id = str(args.get("shot_id") or "")
    if not shot_id:
        raise ValueError("update_shot requires 'shot_id'")
    fields = args.get("fields")
    if not isinstance(fields, dict) or not fields:
        raise ValueError("update_shot requires a non-empty 'fields' object")
    project = _project(ctx)
    result = project.apply_ops(
        [{"op": "update_shot", "shot_id": shot_id, "fields": fields}],
        label="update_shot",
    )
    return _summary(ctx, result, updated_shot=shot_id)


# ── read ────────────────────────────────────────────────────────────────
async def dispatch_get(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    shotlist = _project(ctx).load().get("shotlist") or {}
    return {
        "shot_count": sum(1 for _ in iter_shots(shotlist)),
        "shotlist_text": render_shotlist_text(shotlist),
        "shotlist": shotlist,
    }


__all__ = ["dispatch_set", "dispatch_update_shot", "dispatch_get", "render_shotlist_text"]
