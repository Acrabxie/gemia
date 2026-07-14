"""analyze_media: examine an asset, return summary text + thumbnail.

Emits only tool_exec_start and tool_exec_result. No tool_exec_progress
events — there is no real progress signal for ffprobe + single-frame
extract, so the frontend renders an indeterminate spinner in between.
That is honest reporting.

Returns ``thumbnail_for_next_message: True`` plus a ``thumbnail_path``
so the agent loop knows to attach the thumbnail as image_url on the
next user message (Plan B visual feedback path). The host never
auto-triggers visual feedback for any other tool; only an explicit
analyze_media call.
"""
from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import Any

from gemia.tools._context import ToolContext
from gemia.tools._ffmpeg import ffprobe_duration, ffprobe_metadata, short_summary
from gemia.video.lottie_renderer import save_lottie_frame_png, select_lottie_renderer


_THUMB_DIR_NAME = "thumbnails"


async def dispatch(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    asset_id = str(args["asset_id"])
    focus = args.get("focus")
    record = ctx.registry.get(asset_id)

    loop = asyncio.get_running_loop()
    if record.kind == "lottie":
        lottie_meta = await loop.run_in_executor(None, select_lottie_renderer().get_metadata, str(record.path))
        fps = float(lottie_meta.get("fps") or 30.0)
        frames = max(int(lottie_meta.get("frames") or 1), 1)
        duration = frames / max(fps, 1.0)
        metadata = {
            "format": "lottie",
            "width": int(lottie_meta.get("width") or 0),
            "height": int(lottie_meta.get("height") or 0),
            "fps": fps,
            "frames": frames,
            "duration": duration,
            "codec": "lottie",
        }
        summary_line = f"Lottie {metadata['width']}x{metadata['height']} {frames} frames @ {fps:.2f} fps ({duration:.2f}s)"
    else:
        metadata = await loop.run_in_executor(None, ffprobe_metadata, record.path)
        duration = await loop.run_in_executor(None, ffprobe_duration, record.path)
        summary_line = short_summary(metadata)

    thumb_dir = Path(ctx.output_dir) / _THUMB_DIR_NAME
    thumb_dir.mkdir(parents=True, exist_ok=True)
    thumb_path = thumb_dir / f"{asset_id}.png"

    await loop.run_in_executor(
        None,
        _make_thumbnail,
        record.kind,
        record.path,
        thumb_path,
        duration,
    )

    description_parts = [f"{asset_id} ({record.kind}): {summary_line}"]
    if focus:
        description_parts.append(f"requested focus: {focus}")
    description_parts.append(
        f"thumbnail attached on next message ({thumb_path.name})."
    )
    summary = " ".join(description_parts)

    return {
        "summary": summary,
        "metadata": {
            "asset_id": asset_id,
            "kind": record.kind,
            "duration_sec": duration,
            "source_summary": summary_line,
        },
        "thumbnail_path": str(thumb_path),
        "thumbnail_for_next_message": True,
    }


def _make_thumbnail(kind: str, src: Path, dst: Path, duration: float) -> None:
    if kind == "video":
        ts = max(0.0, duration / 2.0) if duration > 0 else 0.0
        cmd = [
            "ffmpeg", "-y", "-loglevel", "error",
            "-ss", f"{ts:.3f}",
            "-i", str(src),
            "-frames:v", "1",
            "-vf",
            "scale=512:512:force_original_aspect_ratio=decrease,"
            "pad=512:512:(ow-iw)/2:(oh-ih)/2:color=black",
            str(dst),
        ]
    elif kind == "image":
        cmd = [
            "ffmpeg", "-y", "-loglevel", "error",
            "-i", str(src),
            "-vf",
            "scale=512:512:force_original_aspect_ratio=decrease,"
            "pad=512:512:(ow-iw)/2:(oh-ih)/2:color=black",
            str(dst),
        ]
    elif kind == "audio":
        cmd = [
            "ffmpeg", "-y", "-loglevel", "error",
            "-i", str(src),
            "-filter_complex",
            "[0:a]showwavespic=s=512x512:colors=white",
            "-frames:v", "1",
            str(dst),
        ]
    elif kind == "lottie":
        save_lottie_frame_png(src, dst, width=512, height=512, frame_index=0)
        return
    else:
        raise ValueError(f"unsupported asset kind for thumbnail: {kind!r}")
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(
            f"thumbnail extraction failed for {src}: {proc.stderr[-800:]}"
        )


__all__ = ["dispatch"]
