"""assemble_shotlist -- lay the filled storyboard onto the timeline.

This is the payoff of outline/storyboard-driven editing: it walks the shotlist
in scene/shot order and, for every shot that has been *filled* with an
``asset_id`` (via search_media or a generate_* tool), appends a clip to the
video track trimmed to the shot's planned ``duration_sec``, aligns any
``on_screen_text`` as a text overlay over that clip, applies the shot's
``transition_after``, and marks the shot ``placed`` with its ``clip_id``.

It reuses the ``timeline_*`` verb machinery (asset probing, auto track
creation, patch/undo) rather than re-deriving it — each placement lands as its
own auditable patch, so the whole assembly is undoable step by step.

Unfilled shots (no asset_id) are skipped and reported so the model knows what
still needs footage. ``rebuild=true`` clears the current timeline first so a
revised plan reassembles cleanly.
"""
from __future__ import annotations

from typing import Any

from gemia.project_model import iter_shots
from gemia.tools._context import ToolContext


def _project(ctx: ToolContext):
    if ctx.project is None:
        raise ValueError("assemble_shotlist needs a project-backed session (ctx.project is None)")
    return ctx.project


async def dispatch(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    # Lazy imports: assemble_shotlist is imported by gemia.tools.__init__ before
    # its timeline/shotlist siblings, so import their dispatchers at call time.
    from gemia.tools import shotlist as _shotlist
    from gemia.tools import timeline as _timeline

    project = _project(ctx)
    rebuild = bool(args.get("rebuild", False))
    shotlist = project.load().get("shotlist") or {}

    shots = [shot for _scene, shot in iter_shots(shotlist)]
    if not shots:
        return {"assembled": 0, "skipped": [], "summary": "shotlist is empty — call set_shotlist first"}

    if rebuild:
        clips = project.load().get("timeline", {}).get("clips") or []
        for clip in clips:
            await _timeline.dispatch_delete({"clip_id": clip.get("id")}, ctx)
        for shot in shots:
            if shot.get("clip_id"):
                await _shotlist.dispatch_update_shot(
                    {"shot_id": shot["id"], "fields": {"clip_id": None, "status": "filled" if shot.get("asset_id") else "draft"}},
                    ctx,
                )

    placed: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    # Pass 1 — lay every filled shot's media (and aligned text) onto the timeline.
    for shot in shots:
        shot_id = str(shot.get("id") or "")
        asset_id = str(shot.get("asset_id") or "")
        if not asset_id:
            skipped.append({"shot_id": shot_id, "reason": "unfilled (no asset_id — search_media or generate it)"})
            continue
        if shot.get("clip_id") and not rebuild:
            skipped.append({"shot_id": shot_id, "reason": "already placed"})
            continue
        if not ctx.registry.contains(asset_id):
            skipped.append({"shot_id": shot_id, "reason": f"asset {asset_id!r} not in session registry"})
            continue

        duration = max(0.1, float(shot.get("duration_sec") or 3.0))
        record = ctx.registry.get(asset_id)
        insert_args: dict[str, Any] = {"asset_id": asset_id, "track_id": "V1"}
        if record.kind == "video":
            insert_args["source_in"] = 0.0
            insert_args["source_out"] = duration  # trim to the shot's planned length
        else:  # image / lottie: hold for the planned duration
            insert_args["duration"] = duration

        media = await _timeline.dispatch_insert(insert_args, ctx)
        clip_id = media.get("clip_id")
        start = media.get("start") or 0.0

        # On-screen title/caption aligned over this shot.
        if shot.get("on_screen_text"):
            await _timeline.dispatch_insert(
                {"text": {"content": str(shot["on_screen_text"])}, "at_time": start, "duration": duration},
                ctx,
            )

        await _shotlist.dispatch_update_shot(
            {"shot_id": shot_id, "fields": {"clip_id": clip_id, "status": "placed"}}, ctx
        )
        placed.append({
            "shot_id": shot_id, "clip_id": clip_id, "duration_sec": duration,
            "transition_after": shot.get("transition_after"),
        })

    # Pass 2 — apply transitions now that every clip exists. add_transition needs
    # a following clip on the track, so the last placed clip has no transition to
    # apply (its transition_after, if any, is a no-op with nothing after it).
    for entry in placed[:-1]:
        trans = entry.get("transition_after")
        if isinstance(trans, dict) and str(trans.get("kind") or "cut") != "cut":
            await _timeline.dispatch_transition(
                {"clip_id": entry["clip_id"], "kind": trans.get("kind"), "duration_sec": trans.get("duration_sec")},
                ctx,
            )

    timeline_text = project.compact_text()
    return {
        "assembled": len(placed),
        "placed": placed,
        "skipped": skipped,
        "timeline": timeline_text,
        "summary": (
            f"assembled {len(placed)} shot(s) onto the timeline"
            + (f", {len(skipped)} skipped" if skipped else "")
        ),
    }


__all__ = ["dispatch"]
