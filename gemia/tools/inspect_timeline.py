"""inspect_timeline: render the composited project timeline and sample frames.

Unlike ``get_timeline`` (structure only) or ``render_preview`` (video asset
only), this tool gives the agent actual pixels from the composed timeline.
It renders the same low-res proxy preview used by ``render_preview``, extracts
one or more PNG frames, registers them as image assets, and attaches a contact
sheet thumbnail for the next model turn when possible.
"""
from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from gemia.tools._context import ToolContext
from gemia.tools._ffmpeg import run_ffmpeg_with_progress


_MAX_FRAMES = 12


def _project(ctx: ToolContext):
    if ctx.project is None:
        raise ValueError("inspect_timeline needs a project-backed session (ctx.project is None)")
    return ctx.project


def _float_arg(args: dict[str, Any], name: str) -> float | None:
    value = args.get(name)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ValueError(f"argument {name} must be a number, got {value!r}") from None


def _int_arg(args: dict[str, Any], name: str) -> int | None:
    value = args.get(name)
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValueError(f"argument {name} must be an integer, got {value!r}") from None


def _timeline_fps(project_state: dict[str, Any]) -> float:
    timeline = project_state.get("timeline") if isinstance(project_state, dict) else {}
    try:
        fps = float((timeline or {}).get("fps") or 30.0)
    except (TypeError, ValueError):
        fps = 30.0
    return fps if fps > 0 else 30.0


def _timeline_duration(project_state: dict[str, Any], render_result: dict[str, Any]) -> float:
    raw = render_result.get("duration")
    if raw is None:
        timeline = project_state.get("timeline") if isinstance(project_state, dict) else {}
        raw = (timeline or {}).get("duration")
    try:
        return max(0.0, float(raw or 0.0))
    except (TypeError, ValueError):
        return 0.0


def _sample_times(args: dict[str, Any], *, fps: float, duration: float) -> list[float]:
    max_frames = _int_arg(args, "max_frames")
    if max_frames is None:
        max_frames = 1
    max_frames = max(1, min(int(max_frames), _MAX_FRAMES))

    frame = _int_arg(args, "frame")
    time_sec = _float_arg(args, "time_sec")
    if time_sec is None:
        time_sec = _float_arg(args, "time")
    if frame is not None and time_sec is not None:
        raise ValueError("pass either frame or time_sec/time, not both")
    if frame is not None:
        if frame < 0:
            raise ValueError(f"frame must be >= 0, got {frame}")
        return [_clamp_time(frame / fps, duration, fps)]
    if time_sec is not None:
        if time_sec < 0:
            raise ValueError(f"time_sec must be >= 0, got {time_sec}")
        return [_clamp_time(time_sec, duration, fps)]

    start_frame = _int_arg(args, "start_frame")
    end_frame = _int_arg(args, "end_frame")
    start_sec = _float_arg(args, "start_sec")
    end_sec = _float_arg(args, "end_sec")
    if start_sec is None:
        start_sec = _float_arg(args, "start")
    if end_sec is None:
        end_sec = _float_arg(args, "end")
    frame_range = start_frame is not None or end_frame is not None
    second_range = start_sec is not None or end_sec is not None
    if frame_range and second_range:
        raise ValueError("pass a frame range or a seconds range, not both")

    if frame_range:
        start_sec = float(start_frame or 0) / fps
        if end_frame is None:
            end_sec = duration
        else:
            if end_frame <= (start_frame or 0):
                raise ValueError("end_frame must be greater than start_frame")
            end_sec = float(end_frame) / fps
    elif second_range:
        start_sec = float(start_sec or 0.0)
        end_sec = duration if end_sec is None else float(end_sec)
        if start_sec < 0 or end_sec < 0:
            raise ValueError("start_sec/end_sec must be >= 0")
        if end_sec <= start_sec:
            raise ValueError("end_sec/end must be greater than start_sec/start")
    else:
        return [_clamp_time(0.0, duration, fps)]

    assert start_sec is not None and end_sec is not None
    start_sec = _clamp_time(start_sec, duration, fps)
    end_sec = _clamp_time(end_sec, duration, fps)
    if end_sec <= start_sec:
        return [start_sec]
    count = min(max_frames, max(1, math.ceil((end_sec - start_sec) * fps)))
    span = end_sec - start_sec
    return [_clamp_time(start_sec + span * (i + 0.5) / count, duration, fps) for i in range(count)]


def _clamp_time(value: float, duration: float, fps: float) -> float:
    if duration <= 0:
        return max(0.0, value)
    epsilon = max(0.001, 0.5 / fps)
    return max(0.0, min(float(value), max(0.0, duration - epsilon)))


async def _extract_frame(video_path: Path, out_path: Path, at_sec: float, ctx: ToolContext) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-y",
        "-ss", f"{at_sec:.3f}",
        "-i", str(video_path),
        "-frames:v", "1",
        "-vf", "scale=512:512:force_original_aspect_ratio=decrease,pad=512:512:(ow-iw)/2:(oh-ih)/2:color=black",
        str(out_path),
    ]
    await run_ffmpeg_with_progress(cmd, total_seconds=0.0, progress=ctx.emit_progress)


def _make_contact_sheet(frame_paths: list[Path], out_path: Path) -> bool:
    if len(frame_paths) <= 1:
        return False
    try:
        from PIL import Image
    except Exception:
        return False

    images = [Image.open(path).convert("RGB") for path in frame_paths]
    try:
        width, height = images[0].size
        cols = min(3, len(images))
        rows = math.ceil(len(images) / cols)
        sheet = Image.new("RGB", (cols * width, rows * height), "black")
        for index, image in enumerate(images):
            sheet.paste(image, ((index % cols) * width, (index // cols) * height))
        out_path.parent.mkdir(parents=True, exist_ok=True)
        sheet.save(out_path)
    finally:
        for image in images:
            image.close()
    return True


async def dispatch(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    from gemia.project_export import export_project  # heavy import kept lazy

    project = _project(ctx)
    label = str(args.get("label") or "inspect")[:40]
    project_state = project.load()
    fps = _timeline_fps(project_state)
    render_result = export_project(
        project.store,
        project.project_id,
        output_root=ctx.output_dir,
        quality="draft",
        label=label,
    )
    preview_path_raw = render_result.get("export_path")
    if not preview_path_raw:
        raise RuntimeError("timeline inspection render did not return an export_path")
    preview_path = Path(preview_path_raw)
    duration = _timeline_duration(project_state, render_result)
    sample_times = _sample_times(args, fps=fps, duration=duration)

    preview_asset_id = ctx.registry.allocate_id("video")
    ctx.registry.register_output(
        preview_asset_id,
        kind="video",
        path=preview_path,
        summary=f"timeline inspection composited draft ({label}, seq={render_result.get('patch_seq')})",
    )

    frame_asset_ids: list[str] = []
    frame_paths: list[Path] = []
    sample_frames: list[int] = []
    for at_sec in sample_times:
        frame_asset_id = ctx.registry.allocate_id("image")
        frame_path = ctx.child_path(frame_asset_id, ".png")
        await _extract_frame(preview_path, frame_path, at_sec, ctx)
        frame_no = int(round(at_sec * fps))
        ctx.registry.register_output(
            frame_asset_id,
            kind="image",
            path=frame_path,
            summary=f"composited timeline frame {frame_no} at {at_sec:.3f}s",
            lineage=[preview_asset_id],
        )
        frame_asset_ids.append(frame_asset_id)
        frame_paths.append(frame_path)
        sample_frames.append(frame_no)

    thumbnail_path = frame_paths[0] if frame_paths else None
    contact_sheet_asset_id = None
    if len(frame_paths) > 1:
        contact_sheet_asset_id = ctx.registry.allocate_id("image")
        contact_sheet_path = ctx.child_path(contact_sheet_asset_id, ".png")
        if _make_contact_sheet(frame_paths, contact_sheet_path):
            ctx.registry.register_output(
                contact_sheet_asset_id,
                kind="image",
                path=contact_sheet_path,
                summary=f"contact sheet for {len(frame_paths)} composited timeline frames",
                lineage=frame_asset_ids,
            )
            thumbnail_path = contact_sheet_path
        else:
            contact_sheet_asset_id = None

    resolution = render_result.get("resolution") if isinstance(render_result.get("resolution"), dict) else {}
    return {
        "preview_asset_id": preview_asset_id,
        "frame_asset_ids": frame_asset_ids,
        "contact_sheet_asset_id": contact_sheet_asset_id,
        "sample_times": [round(t, 3) for t in sample_times],
        "sample_frames": sample_frames,
        "duration": duration,
        "fps": fps,
        "width": resolution.get("width"),
        "height": resolution.get("height"),
        "thumbnail_for_next_message": bool(thumbnail_path),
        "thumbnail_path": str(thumbnail_path) if thumbnail_path else None,
        "note": "composited timeline frames from an overlay-aware draft render; inspect these before further visual timeline edits",
    }


__all__ = ["dispatch"]
