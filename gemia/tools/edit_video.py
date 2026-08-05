"""edit_video: trim | concat | reverse | speed."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from gemia.tools._context import ToolContext
from gemia.tools._ffmpeg import audio_stream, ffprobe_duration, ffprobe_metadata, get_video_encoder_args, run_ffmpeg_with_progress


async def dispatch(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    asset_id = str(args["asset_id"])
    operation = str(args["operation"])
    src = ctx.registry.get(asset_id)
    if src.kind != "video":
        raise ValueError(
            f"edit_video requires a video asset, got {src.kind!r} for {asset_id}"
        )
    handler = _OPS.get(operation)
    if handler is None:
        known = ", ".join(_OPS.keys())
        raise ValueError(f"unknown edit_video operation: {operation!r}. Known: {known}")
    return await handler(args, ctx, src.path, asset_id)


async def _trim(args, ctx: ToolContext, src_path: Path, src_id: str) -> dict[str, Any]:
    trim = args.get("trim") or {}
    start = float(trim.get("start_sec", 0))
    raw_end = trim.get("end_sec")
    source_duration = ffprobe_duration(src_path)
    end = source_duration if raw_end is None else float(raw_end)
    if end <= start:
        raise ValueError(f"trim end_sec ({end}) must be > start_sec ({start})")
    duration = end - start
    new_id = ctx.registry.allocate_id("video")
    out_path = ctx.child_path(new_id, ".mp4")
    cmd = [
        "ffmpeg", "-y",
        "-ss", f"{start:.3f}",
        "-to", f"{end:.3f}",
        "-i", str(src_path),
        *get_video_encoder_args("h264"),
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        str(out_path),
    ]
    await run_ffmpeg_with_progress(cmd, total_seconds=duration, progress=ctx.emit_progress)
    summary = f"trimmed {src_id} [{start:.2f}s..{end:.2f}s] -> {duration:.2f}s"
    record = ctx.registry.register_output(
        new_id, kind="video", path=out_path, summary=summary, lineage=[src_id]
    )
    return {
        "asset_id": new_id,
        "summary": record.summary,
        "metadata": {"duration_sec": duration, "start_sec": start, "end_sec": end},
    }


async def _concat(args, ctx: ToolContext, src_path: Path, src_id: str) -> dict[str, Any]:
    extra_ids = list(args.get("concat_with") or [])
    if not extra_ids:
        raise ValueError("concat requires concat_with: array of additional asset_ids")
    all_ids = [src_id, *extra_ids]
    paths: list[Path] = [src_path]
    durations: list[float] = [ffprobe_duration(src_path)]
    for aid in extra_ids:
        rec = ctx.registry.get(aid)
        if rec.kind != "video":
            raise ValueError(f"concat input {aid} is {rec.kind!r}, expected video")
        paths.append(rec.path)
        durations.append(ffprobe_duration(rec.path))
    total = sum(durations)
    new_id = ctx.registry.allocate_id("video")
    out_path = ctx.child_path(new_id, ".mp4")
    list_file = ctx.child_path(new_id, ".concat.txt")
    list_file.write_text(
        "\n".join(f"file '{p.resolve()}'" for p in paths) + "\n",
        encoding="utf-8",
    )
    cmd = [
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0",
        "-i", str(list_file),
        *get_video_encoder_args("h264"),
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        str(out_path),
    ]
    try:
        await run_ffmpeg_with_progress(cmd, total_seconds=total, progress=ctx.emit_progress)
    finally:
        list_file.unlink(missing_ok=True)
    summary = f"concatenated {len(all_ids)} clips ({' + '.join(all_ids)}) -> {total:.2f}s"
    record = ctx.registry.register_output(
        new_id, kind="video", path=out_path, summary=summary, lineage=all_ids
    )
    return {
        "asset_id": new_id,
        "summary": record.summary,
        "metadata": {"duration_sec": total, "clip_count": len(all_ids)},
    }


async def _reverse(args, ctx: ToolContext, src_path: Path, src_id: str) -> dict[str, Any]:
    duration = ffprobe_duration(src_path)
    new_id = ctx.registry.allocate_id("video")
    out_path = ctx.child_path(new_id, ".mp4")
    has_audio = audio_stream(ffprobe_metadata(src_path)) is not None
    if has_audio:
        cmd = [
            "ffmpeg", "-y",
            "-i", str(src_path),
            "-vf", "reverse",
            "-af", "areverse",
            *get_video_encoder_args("h264"),
            "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart",
            str(out_path),
        ]
    else:
        cmd = [
            "ffmpeg", "-y",
            "-i", str(src_path),
            "-vf", "reverse",
            "-an",
            *get_video_encoder_args("h264"),
            "-movflags", "+faststart",
            str(out_path),
        ]
    await run_ffmpeg_with_progress(cmd, total_seconds=duration, progress=ctx.emit_progress)
    summary = f"reversed {src_id} ({duration:.2f}s, video+audio reversed)"
    record = ctx.registry.register_output(
        new_id, kind="video", path=out_path, summary=summary, lineage=[src_id]
    )
    return {
        "asset_id": new_id,
        "summary": record.summary,
        "metadata": {"duration_sec": duration, "reversed": True},
    }


async def _speed(args, ctx: ToolContext, src_path: Path, src_id: str) -> dict[str, Any]:
    factor = float(args.get("speed_factor", 1.0))
    if factor <= 0:
        raise ValueError(f"speed_factor must be > 0, got {factor}")
    src_duration = ffprobe_duration(src_path)
    new_duration = src_duration / factor
    new_id = ctx.registry.allocate_id("video")
    out_path = ctx.child_path(new_id, ".mp4")
    has_audio = audio_stream(ffprobe_metadata(src_path)) is not None
    if has_audio:
        atempo_chain = _atempo_chain(factor)
        cmd = [
            "ffmpeg", "-y",
            "-i", str(src_path),
            "-filter_complex",
            f"[0:v]setpts=PTS/{factor}[v];[0:a]{atempo_chain}[a]",
            "-map", "[v]", "-map", "[a]",
            *get_video_encoder_args("h264"),
            "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart",
            str(out_path),
        ]
    else:
        cmd = [
            "ffmpeg", "-y",
            "-i", str(src_path),
            "-vf", f"setpts=PTS/{factor}",
            "-an",
            *get_video_encoder_args("h264"),
            "-movflags", "+faststart",
            str(out_path),
        ]
    await run_ffmpeg_with_progress(cmd, total_seconds=new_duration, progress=ctx.emit_progress)
    summary = f"sped {src_id} by {factor}x ({src_duration:.2f}s -> {new_duration:.2f}s)"
    record = ctx.registry.register_output(
        new_id, kind="video", path=out_path, summary=summary, lineage=[src_id]
    )
    return {
        "asset_id": new_id,
        "summary": record.summary,
        "metadata": {"factor": factor, "duration_sec": new_duration},
    }


def _atempo_chain(factor: float) -> str:
    """ffmpeg atempo is limited to [0.5, 2.0] per filter — chain to cover wider ranges."""
    remaining = factor
    parts: list[str] = []
    while remaining > 2.0:
        parts.append("atempo=2.0")
        remaining /= 2.0
    while remaining < 0.5:
        parts.append("atempo=0.5")
        remaining /= 0.5
    parts.append(f"atempo={remaining:.6f}")
    return ",".join(parts)


_OPS: dict[str, Callable] = {
    "trim": _trim,
    "concat": _concat,
    "reverse": _reverse,
    "speed": _speed,
}


__all__ = ["dispatch"]
