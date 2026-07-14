"""timeline_*: fine-grained verbs over the session's persistent timeline document.

Design contract (docs/timeline-v1/01-op-vocabulary.md, user-approved 2026-06-13):

- Every verb compiles to exactly ONE TimelinePatch (one or two ops) applied
  through ``ctx.project`` — so each model step is auditable in the patch log,
  undoable via ``timeline_undo``, and visible to the UI as a ``timeline_op``
  SSE event. No big apply-patch(json) black box.
- ``ripple`` defaults to False everywhere: ops never shift other clips unless
  the model explicitly opts in.
- v1 surface: video tracks + overlay (image/text) tracks. Audio tracks and
  keyframes are reserved; the patch layer rejects them.
- Mutation verbs return the post-state compact summary so the model does not
  need a follow-up ``get_timeline`` call.

Errors: ``TimelinePatchError`` (typed ``E_*`` codes) propagates — the agent
loop renders it as ``tool_exec_error`` and the model can read the code.
"""
from __future__ import annotations

import uuid
from typing import Any

from gemia.tools._context import ToolContext
from gemia.tools._ffmpeg import ffprobe_duration
from gemia.video.lottie_renderer import select_lottie_renderer
from lumerai.export_support import effects_warnings, transition_warnings


_TEXT_DEFAULT_DURATION = 3.0


def _project(ctx: ToolContext):
    if ctx.project is None:
        raise ValueError(
            "timeline verbs need a project-backed session (ctx.project is None)"
        )
    return ctx.project


def _new_clip_id() -> str:
    return f"clip_{uuid.uuid4().hex[:8]}"


def _summary(ctx: ToolContext, result: dict[str, Any], **extra: Any) -> dict[str, Any]:
    out = {
        "applied": True,
        "seq": result.get("patch_seq_end"),
        "timeline": _project(ctx).compact_text(),
    }
    out.update(extra)
    return out


def _float_arg(args: dict[str, Any], name: str, *, required: bool = False) -> float | None:
    value = args.get(name)
    if value is None:
        if required:
            raise ValueError(f"missing required argument: {name}")
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ValueError(f"argument {name} must be a number, got {value!r}") from None


# ── read ────────────────────────────────────────────────────────────────


async def dispatch_get(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    history = int(args.get("history") or 0)
    return _project(ctx).inspect(history=max(0, min(history, 20)))


# ── insert ──────────────────────────────────────────────────────────────


async def dispatch_insert(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    project = _project(ctx)
    text = args.get("text") if isinstance(args.get("text"), dict) else None
    asset_id = str(args.get("asset_id") or "")
    if not text and not asset_id:
        raise ValueError("timeline_insert_clip needs asset_id (media) or text (title/caption)")
    if text and asset_id:
        raise ValueError("pass either asset_id or text, not both")

    ops: list[dict[str, Any]] = []
    state = project.load()
    tracks = state.get("timeline", {}).get("tracks") or []

    if text:
        content = str(text.get("content") or "").strip()
        if not content:
            raise ValueError("text.content must be a non-empty string")
        media_kind = "text"
        asset_payload = None
        duration = _float_arg(args, "duration") or _TEXT_DEFAULT_DURATION
        clip: dict[str, Any] = {
            "id": _new_clip_id(),
            "asset_id": "",
            "media_kind": "text",
            "name": content[:24] or "text",
            "duration": round(duration, 6),
            "source_in": 0.0,
            "source_out": round(duration, 6),
            "text_config": {
                "content": content,
                "font_size": float(text.get("font_size") or 64.0),
                "color": str(text.get("color") or "#ffffff"),
                "position": text.get("position") if isinstance(text.get("position"), dict) else None,
                "align": str(text.get("align") or "center"),
            },
        }
    else:
        record = ctx.registry.get(asset_id)
        media_kind = record.kind
        # Video/audio/lottie carry real duration; images don't.
        probe_duration = 0.0
        if record.kind in {"video", "audio"}:
            probe_duration = float(ffprobe_duration(record.path))
        elif record.kind == "lottie":
            meta = select_lottie_renderer().get_metadata(str(record.path))
            fps = float(meta.get("fps") or 30.0)
            probe_duration = max(int(meta.get("frames") or 1) / max(fps, 1.0), 0.1)
        asset_payload = {
            "id": record.asset_id,
            "asset_id": record.asset_id,
            "name": record.path.name,
            "media_kind": record.kind,
            "source_path": str(record.path),
            "duration": probe_duration,
        }
        source_in = _float_arg(args, "source_in") or 0.0
        source_out = _float_arg(args, "source_out")
        if record.kind in {"video", "audio", "lottie"}:
            if source_out is None:
                source_out = probe_duration or source_in + 0.1
            duration = max(round(source_out - source_in, 6), 0.1)
        else:  # image
            duration = _float_arg(args, "duration") or _TEXT_DEFAULT_DURATION
            source_in, source_out = 0.0, duration
        clip = {
            "id": _new_clip_id(),
            "asset_id": record.asset_id,
            "media_kind": media_kind,
            "name": record.path.name,
            "duration": round(duration, 6),
            "source_in": round(source_in, 6),
            "source_out": round(source_out, 6),
        }

    # Resolve target track; auto-create OV1/A1 for the first overlay/audio clip.
    track_id = str(args.get("track_id") or "")
    if media_kind == "image":
        # Images can go on video or overlay tracks (explicit track_id = V1 for main chain)
        if track_id:
            # Explicit track specified; ensure it exists
            if not any(str(t.get("id")) == track_id for t in tracks):
                # Find kind for track_id to know what kind of track to create
                track_kind = "video" if track_id == "V1" else "overlay"
                ops.append({"op": "add_track", "kind": track_kind, "track_id": track_id})
        else:
            # Default to overlay if no explicit track
            overlay_tracks = [t for t in tracks if t.get("kind") == "overlay"]
            track_id = str(overlay_tracks[0]["id"]) if overlay_tracks else "OV1"
            if not any(str(t.get("id")) == track_id for t in tracks):
                ops.append({"op": "add_track", "kind": "overlay", "track_id": track_id})
    elif media_kind in {"text", "lottie"}:
        overlay_tracks = [t for t in tracks if t.get("kind") == "overlay"]
        if not track_id:
            track_id = str(overlay_tracks[0]["id"]) if overlay_tracks else "OV1"
        if not any(str(t.get("id")) == track_id for t in tracks):
            ops.append({"op": "add_track", "kind": "overlay", "track_id": track_id})
    elif media_kind == "audio":
        audio_tracks = [t for t in tracks if t.get("kind") == "audio"]
        if not track_id:
            track_id = str(audio_tracks[0]["id"]) if audio_tracks else "A1"
        if not any(str(t.get("id")) == track_id for t in tracks):
            ops.append({"op": "add_track", "kind": "audio", "track_id": track_id})
    else:
        track_id = track_id or "V1"
        if not any(str(t.get("id")) == track_id for t in tracks):
            ops.append({"op": "add_track", "kind": "video", "track_id": track_id})
    clip["track_id"] = track_id

    at_time = _float_arg(args, "at_time")
    at_index = args.get("at_index")
    if at_time is not None and at_index is not None:
        raise ValueError("pass either at_time or at_index, not both")
    at: Any = "append"
    if at_time is not None:
        at = {"time": round(at_time, 6)}
    elif at_index is not None:
        at = {"index": int(at_index)}

    insert_op: dict[str, Any] = {
        "op": "insert_clip",
        "data": ({"asset": asset_payload, "clip": clip} if asset_payload else {"clip": clip}),
        "track_id": track_id,
        "at": at,
        "ripple": bool(args.get("ripple", False)),
        "provenance": {"verb": "timeline_insert_clip", "session_id": ctx.session_id},
    }
    ops.append(insert_op)

    result = project.apply_ops(ops, label="timeline_insert_clip")
    placed = next(
        (
            c
            for c in (project.load().get("timeline", {}).get("clips") or [])
            if str(c.get("id")) == clip["id"]
        ),
        clip,
    )
    return _summary(
        ctx,
        result,
        clip_id=clip["id"],
        track_id=track_id,
        start=placed.get("start"),
        duration=placed.get("duration"),
    )


# ── single-clip mutations ───────────────────────────────────────────────


async def dispatch_delete(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    clip_id = str(args.get("clip_id") or "")
    op = {"op": "delete_clip", "clip_id": clip_id, "ripple": bool(args.get("ripple", False))}
    result = _project(ctx).apply_ops([op], label="timeline_delete_clip")
    return _summary(ctx, result, clip_id=clip_id)


async def dispatch_move(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    clip_id = str(args.get("clip_id") or "")
    op: dict[str, Any] = {
        "op": "move_clip",
        "clip_id": clip_id,
        "ripple": bool(args.get("ripple", False)),
    }
    start = _float_arg(args, "start")
    if start is not None:
        op["start"] = start
    if args.get("track_id"):
        op["track_id"] = str(args["track_id"])
    result = _project(ctx).apply_ops([op], label="timeline_move_clip")
    return _summary(ctx, result, clip_id=clip_id)


async def dispatch_trim(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    clip_id = str(args.get("clip_id") or "")
    op: dict[str, Any] = {
        "op": "trim_clip",
        "clip_id": clip_id,
        "ripple": bool(args.get("ripple", False)),
    }
    for key in ("source_in", "source_out"):
        value = _float_arg(args, key)
        if value is not None:
            op[key] = value
    result = _project(ctx).apply_ops([op], label="timeline_trim_clip")
    return _summary(ctx, result, clip_id=clip_id)


async def dispatch_split(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    clip_id = str(args.get("clip_id") or "")
    new_clip_id = _new_clip_id()
    op = {
        "op": "split_clip",
        "clip_id": clip_id,
        "at_time": _float_arg(args, "at_time", required=True),
        "new_clip_id": new_clip_id,
        "provenance": {"verb": "timeline_split_clip", "session_id": ctx.session_id},
    }
    result = _project(ctx).apply_ops([op], label="timeline_split_clip")
    return _summary(ctx, result, clip_id=clip_id, new_clip_id=new_clip_id)


async def dispatch_set_time(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    clip_id = str(args.get("clip_id") or "")
    op: dict[str, Any] = {
        "op": "set_clip_time",
        "clip_id": clip_id,
        "ripple": bool(args.get("ripple", False)),
    }
    for key in ("start", "duration"):
        value = _float_arg(args, key)
        if value is not None:
            op[key] = value
    result = _project(ctx).apply_ops([op], label="timeline_set_clip_time")
    return _summary(ctx, result, clip_id=clip_id)


async def dispatch_transition(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    clip_id = str(args.get("clip_id") or "")
    op: dict[str, Any] = {
        "op": "add_transition",
        "clip_id": clip_id,
        "kind": str(args.get("kind") or "cut"),
    }
    duration_sec = _float_arg(args, "duration_sec")
    if duration_sec is not None:
        op["duration_sec"] = duration_sec
    result = _project(ctx).apply_ops([op], label="timeline_add_transition")
    out = _summary(ctx, result, clip_id=clip_id)
    # Export honesty (docs/timeline-canonical-plan.md §4): fade/dissolve render
    # on export since Phase 1; kinds without a renderer (wipe) warn at write —
    # never silently, never rejected (OTIO/replay compatibility).
    warnings = transition_warnings(op["kind"])
    if warnings:
        out["warnings"] = warnings
        out["export_note"] = (
            f"transition '{op['kind']}' is recorded and visible on the "
            "timeline, but final export still renders a hard cut here "
            "(fade/dissolve render; see docs/timeline-canonical-plan.md)."
        )
    return out


async def dispatch_effects(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    clip_id = str(args.get("clip_id") or "")
    effects = args.get("effects")
    if not isinstance(effects, dict) or not effects:
        raise ValueError("timeline_set_clip_effects needs a non-empty effects object")
    op = {"op": "set_clip_effects", "clip_id": clip_id, "effects": effects}
    project = _project(ctx)
    result = project.apply_ops([op], label="timeline_set_clip_effects")
    out = _summary(ctx, result, clip_id=clip_id)
    # Export honesty (docs/timeline-canonical-plan.md §4): warn — never reject —
    # when the write stores fields the exporter will not render today.
    clip = next(
        (
            c
            for c in (project.load().get("timeline", {}).get("clips") or [])
            if str(c.get("id")) == clip_id
        ),
        None,
    )
    media_kind = str((clip or {}).get("media_kind") or "video")
    warnings = effects_warnings(media_kind, effects)
    if warnings:
        out["warnings"] = warnings
    return out


async def dispatch_add_track(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    op: dict[str, Any] = {"op": "add_track", "kind": str(args.get("kind") or "")}
    if args.get("track_id"):
        op["track_id"] = str(args["track_id"])
    if args.get("name"):
        op["name"] = str(args["name"])
    result = _project(ctx).apply_ops([op], label="timeline_add_track")
    return _summary(ctx, result)


async def dispatch_set_track(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Set track-level fields. Currently the ducking relationship: pass
    duck_under=<audio track id> to make this (audio) track duck under that
    trigger track, or duck_under=null to clear it."""
    track_id = str(args.get("track_id") or "")
    op: dict[str, Any] = {"op": "set_track", "track_id": track_id}
    if "duck_under" in args:
        duck = args.get("duck_under")
        op["duck_under"] = str(duck) if duck else None
    result = _project(ctx).apply_ops([op], label="timeline_set_track")
    return _summary(ctx, result, track_id=track_id)


# ── undo ────────────────────────────────────────────────────────────────


async def dispatch_undo(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    steps = int(args.get("steps") or 1)
    if steps < 1 or steps > 10:
        raise ValueError(f"undo steps must be in 1..10, got {steps}")
    project = _project(ctx)
    result = project.undo(steps)
    return {
        "applied": True,
        "from_seq": result.get("from_seq"),
        "to_seq": result.get("to_seq"),
        "timeline": project.compact_text(),
    }


# ── preview render ──────────────────────────────────────────────────────


async def dispatch_render_preview(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    from gemia.project_render import render_project_preview  # heavy import kept lazy

    project = _project(ctx)
    label = str(args.get("label") or "preview")[:40]
    result = render_project_preview(
        project.store,
        project.project_id,
        output_root=ctx.output_dir,
        label=label,
    )
    preview_path = result.get("preview_path")
    asset_id = None
    if preview_path:
        asset_id = ctx.registry.allocate_id("video")
        ctx.registry.register_output(
            asset_id,
            kind="video",
            path=preview_path,
            summary=f"timeline preview ({label}, seq={result.get('patch_seq')})",
        )
    resolution = result.get("resolution") if isinstance(result.get("resolution"), dict) else {}
    return {
        "asset_id": asset_id,
        "render_id": result.get("render_id"),
        "duration": result.get("duration"),
        "width": resolution.get("width"),
        "height": resolution.get("height"),
        "note": "low-res proxy preview of the timeline document; use analyze_media to look at it",
    }


# ── project export ──────────────────────────────────────────────────────


async def dispatch_project_export(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    from gemia.project_export import export_project  # heavy import kept lazy

    project = _project(ctx)
    quality = str(args.get("quality") or "1080p")
    label = str(args.get("label") or "export")[:40]
    result = export_project(
        project.store,
        project.project_id,
        output_root=ctx.output_dir,
        quality=quality,
        label=label,
    )
    export_path = result.get("export_path")
    asset_id = None
    if export_path:
        asset_id = ctx.registry.allocate_id("video")
        ctx.registry.register_output(
            asset_id,
            kind="video",
            path=export_path,
            summary=f"project export ({quality}, seq={result.get('patch_seq')})",
        )
    resolution = result.get("resolution") if isinstance(result.get("resolution"), dict) else {}
    return {
        "asset_id": asset_id,
        "export_id": result.get("export_id"),
        "duration": result.get("duration"),
        "width": resolution.get("width"),
        "height": resolution.get("height"),
        "quality": quality,
        "video_clips": result.get("video_clips_rendered"),
        "overlay_clips": result.get("overlay_clips_rendered"),
        "audio_clips": result.get("audio_clips_rendered"),
        "has_audio": bool(result.get("has_audio")),
        "export_path": export_path,
        "note": "full-quality export; use analyze_media to inspect, or deliver the file directly",
    }


async def dispatch_export_otio(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Export the current project to an OTIO-family interchange file.

    ``format`` (default "otio"): otio (JSON), otioz / otiod (bundles w/ media),
    edl (cmx_3600), fcp7 (fcp_xml), fcpx (fcpx_xml). EDL/FCP need the optional
    `interop` plugins and are lossy.
    """
    from lumerai.otio_adapter import LOSSY_FORMATS, format_extension, write_project_to_file

    project = _project(ctx)
    p = project.load()
    fmt = str(args.get("format") or "otio")
    label = str(args.get("label") or "project")[:40]
    ext = format_extension(fmt)  # raises OtioFormatError on an unknown token
    out_path = ctx.output_dir / f"{label}{ext}"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    write_project_to_file(p, out_path, fmt)  # raises OtioFormatError if adapter missing
    asset_id = ctx.registry.allocate_id("otio")
    ctx.registry.register_output(
        asset_id,
        kind="otio",
        path=str(out_path),
        summary=f"{fmt} export of project {project.project_id}",
    )
    if fmt in LOSSY_FORMATS:
        note = (
            f"{fmt} written (LOSSY interchange): cuts, timing, timecode and clip names survive; "
            "overlays, audio gain/fades, ducking and rich effects are dropped or simplified"
        )
    else:
        bundled = " with bundled media" if fmt in {"otioz", "otiod"} else ""
        note = (
            f"{fmt} written (lossless{bundled}); opens in DaVinci Resolve, Premiere, "
            "Final Cut and other NLEs"
        )
    return {
        "asset_id": asset_id,
        "otio_path": str(out_path),
        "format": fmt,
        "project_id": project.project_id,
        "clip_count": len((p.get("timeline") or {}).get("clips") or []),
        "note": note,
    }


async def dispatch_import_otio(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Import an OTIO-family interchange file and replace the current timeline.

    ``format`` (default "otio") matches the export tokens. The imported assets,
    tracks and clips are applied as one atomic patch (undoable via timeline_undo).
    """
    from pathlib import Path as _Path

    from lumerai.otio_adapter import read_project_from_file

    otio_path_str = str(args.get("otio_path") or "")
    if not otio_path_str:
        raise ValueError("import_otio requires 'otio_path'")
    otio_path = _Path(otio_path_str)
    if not otio_path.exists():
        raise FileNotFoundError(f"OTIO file not found: {otio_path}")
    fmt = str(args.get("format") or "otio")
    imported = read_project_from_file(otio_path, fmt)  # raises OtioFormatError if unsupported
    imported_tl = imported.get("timeline") or {}

    project = _project(ctx)
    existing_ids = {
        str(t.get("id"))
        for t in (project.load().get("timeline", {}).get("tracks") or [])
        if isinstance(t, dict)
    }
    ops: list[dict[str, Any]] = [
        {
            "op": "set_timeline_format",
            "fps": imported_tl.get("fps", 30.0),
            "width": imported_tl.get("width", 1920),
            "height": imported_tl.get("height", 1080),
        }
    ]
    # Carry imported assets so clip media resolves on a later export.
    for asset in imported.get("assets") or []:
        if isinstance(asset, dict) and (asset.get("id") or asset.get("asset_id")):
            ops.append({"op": "upsert_asset", "asset": asset})
    # Create any non-default tracks before inserting clips onto them.
    for track in imported_tl.get("tracks") or []:
        tid = str(track.get("id") or "")
        kind = str(track.get("kind") or "video")
        if tid and tid not in existing_ids and kind in {"video", "overlay", "audio"}:
            ops.append({"op": "add_track", "kind": kind, "track_id": tid, "name": track.get("name")})
            existing_ids.add(tid)
    # Insert each imported clip at its timeline start (extended insert form).
    for clip in imported_tl.get("clips") or []:
        if not isinstance(clip, dict):
            continue
        ops.append({
            "op": "insert_clip",
            "track_id": str(clip.get("track_id") or "V1"),
            "at": {"time": round(float(clip.get("start") or 0.0), 6)},
            "data": {"clip": clip},
        })
    project.apply_ops(ops, label=f"timeline_import_otio:{fmt}")

    final = project.load()
    tl = final.get("timeline") or {}
    return {
        "project_id": project.project_id,
        "format": fmt,
        "title": imported.get("title"),
        "clip_count": len(tl.get("clips") or []),
        "track_count": len(tl.get("tracks") or []),
        "duration": tl.get("duration"),
        "fps": tl.get("fps"),
        "note": "OTIO timeline imported and applied as one patch; use timeline_undo to revert.",
    }
