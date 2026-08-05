"""smart_reframe: deterministic aspect-ratio adaptation for visual media."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from gemia.errors import RECOVERY_FIX_ARGS, ToolError
from gemia.tools._context import ToolContext
from gemia.tools._ffmpeg import ffprobe_duration, get_video_encoder_args, run_ffmpeg_with_progress


_TARGETS: dict[str, tuple[int, int]] = {
    "vertical_9_16": (1080, 1920),
    "9:16": (1080, 1920),
    "tiktok": (1080, 1920),
    "reels": (1080, 1920),
    "shorts": (1080, 1920),
    "square_1_1": (1080, 1080),
    "1:1": (1080, 1080),
    "portrait_4_5": (1080, 1350),
    "4:5": (1080, 1350),
    "landscape_16_9": (1920, 1080),
    "16:9": (1920, 1080),
}


async def dispatch(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    asset_id = str(args["asset_id"])
    record = ctx.registry.get(asset_id)
    if record.kind not in {"video", "image"}:
        raise ToolError(
            f"smart_reframe works on video or image assets; {asset_id} is a {record.kind}.",
            code="E_UNSUPPORTED",
            recovery=RECOVERY_FIX_ARGS,
        )
    width, height = _target_size(args)
    mode = str(args.get("mode") or "center_crop")
    if mode not in {"center_crop", "fit_pad"}:
        raise ToolError("mode must be center_crop or fit_pad.", code="E_BAD_ARG", recovery=RECOVERY_FIX_ARGS)
    anchor_x = _clamp_float(args.get("anchor_x", 0.5), 0.0, 1.0)
    anchor_y = _clamp_float(args.get("anchor_y", 0.5), 0.0, 1.0)
    background = _color(str(args.get("background") or "black"))
    vf = _video_filter(width, height, mode=mode, anchor_x=anchor_x, anchor_y=anchor_y, background=background)
    duration = ffprobe_duration(record.path) if record.kind == "video" else 0.0
    new_id = ctx.registry.allocate_id(record.kind)
    out_path = ctx.child_path(new_id, ".mp4" if record.kind == "video" else _image_ext(record.path))
    if record.kind == "video":
        cmd = [
            "ffmpeg", "-y",
            "-i", str(record.path),
            "-vf", vf,
            *get_video_encoder_args("h264"),
            "-c:a", "copy",
            "-movflags", "+faststart",
            str(out_path),
        ]
    else:
        cmd = [
            "ffmpeg", "-y",
            "-i", str(record.path),
            "-vf", vf,
            "-frames:v", "1",
            str(out_path),
        ]
    await run_ffmpeg_with_progress(cmd, total_seconds=duration, progress=ctx.emit_progress)
    summary = f"reframed {asset_id} to {width}x{height} using {mode}"
    out = ctx.registry.register_output(
        new_id,
        kind=record.kind,
        path=out_path,
        summary=summary,
        lineage=[asset_id],
    )
    return {
        "asset_id": new_id,
        "summary": out.summary,
        "metadata": {
            "width": width,
            "height": height,
            "mode": mode,
            "anchor_x": anchor_x,
            "anchor_y": anchor_y,
            "filter": vf,
        },
    }


def _target_size(args: dict[str, Any]) -> tuple[int, int]:
    target = str(args.get("target") or "vertical_9_16").strip().lower()
    if target == "custom":
        return _size(args.get("width"), args.get("height"))
    if target not in _TARGETS:
        raise ToolError(f"unknown target: {target}", code="E_BAD_ARG", recovery=RECOVERY_FIX_ARGS)
    default_w, default_h = _TARGETS[target]
    width = int(args.get("width") or default_w)
    height = int(args.get("height") or default_h)
    return _size(width, height)


def _size(width: Any, height: Any) -> tuple[int, int]:
    try:
        w = int(width)
        h = int(height)
    except Exception as exc:
        raise ToolError("width and height must be integers.", code="E_BAD_ARG", recovery=RECOVERY_FIX_ARGS) from exc
    if not (240 <= w <= 4320 and 240 <= h <= 4320):
        raise ToolError("width and height must be between 240 and 4320.", code="E_BAD_ARG", recovery=RECOVERY_FIX_ARGS)
    return w, h


def _video_filter(width: int, height: int, *, mode: str, anchor_x: float, anchor_y: float, background: str) -> str:
    if mode == "fit_pad":
        return (
            f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color={background},setsar=1"
        )
    return (
        f"scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height}:(iw-ow)*{anchor_x:.6f}:(ih-oh)*{anchor_y:.6f},setsar=1"
    )


def _clamp_float(value: Any, low: float, high: float) -> float:
    try:
        numeric = float(value)
    except Exception:
        numeric = low
    return max(low, min(numeric, high))


def _color(value: str) -> str:
    if value.lower() in {"black", "white", "gray", "grey", "transparent"}:
        return value.lower()
    if re.fullmatch(r"#[0-9a-fA-F]{6}", value):
        return "0x" + value[1:]
    return "black"


def _image_ext(path: Path) -> str:
    ext = path.suffix.lower()
    return ext if ext in {".png", ".jpg", ".jpeg", ".webp"} else ".png"


__all__ = ["dispatch", "_video_filter"]
