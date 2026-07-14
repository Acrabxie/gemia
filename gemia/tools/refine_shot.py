"""refine_shot -- edit ONE assembled shot in place without rebuilding the timeline.

Allows fine-grained mutations to a single placed shot: retime it, replace its
footage, recaption it, or remove it entirely. Each operation updates both the
shot's timeline clip(s) and its IR, returning the new timeline/shotlist views.

Errors if the shot has no clip_id (not yet assembled) when the op needs a placed
clip — returns a clear guidance message instead of raising, so the model knows
to assemble_shotlist first.
"""
from __future__ import annotations

from typing import Any

from gemia.project_model import iter_shots
from gemia.tools._context import ToolContext


def _project(ctx: ToolContext):
    if ctx.project is None:
        raise ValueError("refine_shot needs a project-backed session (ctx.project is None)")
    return ctx.project


async def dispatch(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    # Lazy imports: avoid cycles with timeline and shotlist dispatchers.
    from gemia.tools import shotlist as _shotlist
    from gemia.tools import timeline as _timeline

    project = _project(ctx)
    shot_id = str(args.get("shot_id") or "").strip()
    if not shot_id:
        raise ValueError("refine_shot requires 'shot_id'")

    # Look up the shot by id.
    shotlist = project.load().get("shotlist") or {}
    shot = None
    for _scene, s in iter_shots(shotlist):
        if str(s.get("id")) == shot_id:
            shot = s
            break
    if shot is None:
        raise ValueError(f"shot not found: {shot_id!r}")

    # Determine which operation is requested (exactly one field from the ops set).
    has_duration = "duration_sec" in args
    has_asset_id = "asset_id" in args
    has_text = "on_screen_text" in args
    has_remove = "remove" in args

    ops_count = sum([has_duration, has_asset_id, has_text, has_remove])
    if ops_count != 1:
        raise ValueError(
            "refine_shot requires exactly ONE of: duration_sec, asset_id, on_screen_text, remove"
        )

    clip_id = shot.get("clip_id")

    # ── duration_sec: retime the shot clip and text overlay (if present)
    if has_duration:
        new_duration = float(args.get("duration_sec"))
        if new_duration <= 0.0:
            raise ValueError("duration_sec must be positive")

        if not clip_id:
            return {
                "shot_id": shot_id,
                "operation": "retime",
                "clip_id": None,
                "timeline": project.compact_text(),
                "shotlist": _shotlist.render_shotlist_text(shotlist),
                "summary": (
                    f"shot {shot_id!r} is not yet placed (no clip_id). "
                    "Call assemble_shotlist first to place it onto the timeline."
                ),
            }

        # Retime the media clip.
        timeline = project.load().get("timeline") or {}
        clips = timeline.get("clips") or []
        shot_clip = next((c for c in clips if str(c.get("id")) == clip_id), None)
        if shot_clip is None:
            return {
                "shot_id": shot_id,
                "operation": "retime",
                "clip_id": clip_id,
                "timeline": project.compact_text(),
                "shotlist": _shotlist.render_shotlist_text(shotlist),
                "summary": f"clip {clip_id!r} not found in timeline (shot may be stale)",
            }

        track_id = shot_clip.get("track_id")
        media_kind = shot_clip.get("media_kind") or "video"

        # For video, adjust source_out via trim; for image/lottie, adjust duration.
        # Enable ripple to reflow subsequent clips when duration changes.
        if media_kind == "video":
            await _timeline.dispatch_trim(
                {"clip_id": clip_id, "source_out": new_duration, "ripple": True}, ctx
            )
        else:
            # image/lottie: set duration directly.
            await _timeline.dispatch_set_time(
                {"clip_id": clip_id, "duration": new_duration, "ripple": True}, ctx
            )

        # Retime the text overlay if present (find by start time == shot clip's start).
        if shot.get("on_screen_text"):
            shot_start = shot_clip.get("start") or 0.0
            overlay_tracks = [t for t in (timeline.get("tracks") or [])
                            if t.get("kind") == "overlay"]
            if overlay_tracks:
                overlay_id = str(overlay_tracks[0].get("id") or "OV1")
                for clip in clips:
                    if (str(clip.get("track_id")) == overlay_id and
                        abs(float(clip.get("start") or 0.0) - shot_start) < 0.01 and
                        clip.get("media_kind") == "text"):
                        await _timeline.dispatch_set_time(
                            {"clip_id": clip.get("id"), "duration": new_duration}, ctx
                        )
                        break

        # Update the shot IR.
        await _shotlist.dispatch_update_shot(
            {"shot_id": shot_id, "fields": {"duration_sec": new_duration}}, ctx
        )

    # ── asset_id: replace the shot's footage
    elif has_asset_id:
        new_asset_id = str(args.get("asset_id") or "").strip()
        if not new_asset_id:
            raise ValueError("asset_id cannot be empty")

        if not clip_id:
            return {
                "shot_id": shot_id,
                "operation": "replace",
                "clip_id": None,
                "timeline": project.compact_text(),
                "shotlist": _shotlist.render_shotlist_text(shotlist),
                "summary": (
                    f"shot {shot_id!r} is not yet placed (no clip_id). "
                    "Call assemble_shotlist first to place it onto the timeline."
                ),
            }

        # Verify new asset is registered.
        if not ctx.registry.contains(new_asset_id):
            raise ValueError(
                f"asset_id {new_asset_id!r} not in session registry. "
                f"Available: {', '.join(r.asset_id for r in ctx.registry.list_records())}"
            )

        # Locate the old clip and capture its position.
        timeline = project.load().get("timeline") or {}
        clips = timeline.get("clips") or []
        old_clip = next((c for c in clips if str(c.get("id")) == clip_id), None)
        if old_clip is None:
            return {
                "shot_id": shot_id,
                "operation": "replace",
                "clip_id": clip_id,
                "timeline": project.compact_text(),
                "shotlist": _shotlist.render_shotlist_text(shotlist),
                "summary": f"clip {clip_id!r} not found in timeline (shot may be stale)",
            }

        track_id = old_clip.get("track_id") or "V1"
        old_start = old_clip.get("start") or 0.0
        old_duration = shot.get("duration_sec") or 3.0

        # Delete the old clip.
        await _timeline.dispatch_delete({"clip_id": clip_id}, ctx)

        # Insert the new asset at the same start time.
        new_record = ctx.registry.get(new_asset_id)
        insert_args: dict[str, Any] = {
            "asset_id": new_asset_id,
            "track_id": track_id,
            "at_time": old_start,
        }
        if new_record.kind == "video":
            insert_args["source_in"] = 0.0
            insert_args["source_out"] = old_duration
        else:  # image/lottie
            insert_args["duration"] = old_duration

        result = await _timeline.dispatch_insert(insert_args, ctx)
        new_clip_id = result.get("clip_id")

        # Update the shot IR.
        await _shotlist.dispatch_update_shot(
            {
                "shot_id": shot_id,
                "fields": {
                    "asset_id": new_asset_id,
                    "clip_id": new_clip_id,
                    "source": "search",
                    "status": "placed",
                },
            },
            ctx,
        )

    # ── on_screen_text: recaption the shot
    elif has_text:
        new_text = args.get("on_screen_text")
        new_text = str(new_text).strip() if new_text else None

        if not clip_id:
            return {
                "shot_id": shot_id,
                "operation": "recaption",
                "clip_id": None,
                "timeline": project.compact_text(),
                "shotlist": _shotlist.render_shotlist_text(shotlist),
                "summary": (
                    f"shot {shot_id!r} is not yet placed (no clip_id). "
                    "Call assemble_shotlist first to place it onto the timeline."
                ),
            }

        # Remove the old text overlay if present.
        timeline = project.load().get("timeline") or {}
        clips = timeline.get("clips") or []
        shot_clip = next((c for c in clips if str(c.get("id")) == clip_id), None)
        if shot_clip:
            shot_start = shot_clip.get("start") or 0.0
            overlay_tracks = [t for t in (timeline.get("tracks") or [])
                            if t.get("kind") == "overlay"]
            if overlay_tracks:
                overlay_id = str(overlay_tracks[0].get("id") or "OV1")
                for clip in clips:
                    if (str(clip.get("track_id")) == overlay_id and
                        abs(float(clip.get("start") or 0.0) - shot_start) < 0.01 and
                        clip.get("media_kind") == "text"):
                        await _timeline.dispatch_delete({"clip_id": clip.get("id")}, ctx)
                        break

        # Insert new text overlay if the new text is non-empty.
        if new_text:
            shot_duration = shot.get("duration_sec") or 3.0
            await _timeline.dispatch_insert(
                {
                    "text": {"content": new_text},
                    "at_time": shot_clip.get("start") or 0.0,
                    "duration": shot_duration,
                },
                ctx,
            )

        # Update the shot IR.
        await _shotlist.dispatch_update_shot(
            {
                "shot_id": shot_id,
                "fields": {"on_screen_text": new_text if new_text else None},
            },
            ctx,
        )

    # ── remove: remove the shot from the cut
    elif has_remove:
        do_remove = bool(args.get("remove"))
        if not do_remove:
            return {
                "shot_id": shot_id,
                "operation": "remove",
                "clip_id": clip_id,
                "timeline": project.compact_text(),
                "shotlist": _shotlist.render_shotlist_text(shotlist),
                "summary": "remove=false is a no-op",
            }

        if not clip_id:
            return {
                "shot_id": shot_id,
                "operation": "remove",
                "clip_id": None,
                "timeline": project.compact_text(),
                "shotlist": _shotlist.render_shotlist_text(shotlist),
                "summary": (
                    f"shot {shot_id!r} is not yet placed (no clip_id). "
                    "Nothing to remove from the timeline."
                ),
            }

        # Capture the text content before deleting (to match it after delete).
        text_content = None
        if shot.get("on_screen_text"):
            text_content = str(shot.get("on_screen_text"))

        # Delete the media clip.
        await _timeline.dispatch_delete({"clip_id": clip_id}, ctx)

        # Delete the text overlay if present (search by content since start times may have shifted).
        if text_content:
            timeline = project.load().get("timeline") or {}
            clips = timeline.get("clips") or []
            overlay_tracks = [t for t in (timeline.get("tracks") or [])
                            if t.get("kind") == "overlay"]
            if overlay_tracks:
                overlay_id = str(overlay_tracks[0].get("id") or "OV1")
                for clip in clips:
                    if (str(clip.get("track_id")) == overlay_id and
                        clip.get("media_kind") == "text" and
                        (clip.get("text_config") or {}).get("content") == text_content):
                        await _timeline.dispatch_delete({"clip_id": clip.get("id")}, ctx)
                        break

        # Reset the shot IR.
        new_status = "filled" if shot.get("asset_id") else "draft"
        await _shotlist.dispatch_update_shot(
            {"shot_id": shot_id, "fields": {"clip_id": None, "status": new_status}}, ctx
        )

    # Build the result.
    operation = None
    if has_duration:
        operation = "retime"
    elif has_asset_id:
        operation = "replace"
    elif has_text:
        operation = "recaption"
    elif has_remove:
        operation = "remove"

    # Reload project state for final views.
    final_project = project.load()
    final_shotlist = final_project.get("shotlist") or {}
    final_shot = None
    for _scene, s in iter_shots(final_shotlist):
        if str(s.get("id")) == shot_id:
            final_shot = s
            break

    return {
        "shot_id": shot_id,
        "operation": operation,
        "clip_id": final_shot.get("clip_id") if final_shot else None,
        "timeline": project.compact_text(),
        "shotlist": _shotlist.render_shotlist_text(final_shotlist),
        "summary": f"shot {shot_id!r} {operation} applied",
    }


__all__ = ["dispatch"]
