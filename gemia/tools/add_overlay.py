"""add_overlay: burn text caption, image overlay, or subtitle on a video."""
from __future__ import annotations

import glob
from pathlib import Path
from typing import Any

from gemia.errors import RECOVERY_FIX_ARGS, RECOVERY_SWITCH_TOOL, ToolError
from gemia.tools._context import ToolContext
from gemia.tools._ffmpeg import ffprobe_duration, get_video_encoder_args, run_ffmpeg_with_progress


_FONT_CACHE: list[str] = []


def _font_file() -> str:
    if _FONT_CACHE:
        return _FONT_CACHE[0]
    candidates = (
        glob.glob("/System/Library/Fonts/Supplemental/Arial.ttf")
        + glob.glob("/System/Library/Fonts/**/*.ttf", recursive=True)
        + glob.glob("/Library/Fonts/*.ttf")
        + glob.glob("/usr/share/fonts/**/*.ttf", recursive=True)
    )
    if not candidates:
        raise RuntimeError("no usable .ttf font found for drawtext overlay")
    _FONT_CACHE.append(candidates[0])
    return candidates[0]


_POSITIONS: dict[str, tuple[str, str]] = {
    "top_left":      ("20",                       "20"),
    "top_center":    ("(w-text_w)/2",             "20"),
    "top_right":     ("w-text_w-20",              "20"),
    "center_left":   ("20",                       "(h-text_h)/2"),
    "center":        ("(w-text_w)/2",             "(h-text_h)/2"),
    "center_right":  ("w-text_w-20",              "(h-text_h)/2"),
    "bottom_left":   ("20",                       "h-text_h-20"),
    "bottom_center": ("(w-text_w)/2",             "h-text_h-40"),
    "bottom_right":  ("w-text_w-20",              "h-text_h-20"),
}

_OVERLAY_POSITIONS: dict[str, tuple[str, str]] = {
    "top_left":      ("20",             "20"),
    "top_center":    ("(W-w)/2",        "20"),
    "top_right":     ("W-w-20",         "20"),
    "center_left":   ("20",             "(H-h)/2"),
    "center":        ("(W-w)/2",        "(H-h)/2"),
    "center_right":  ("W-w-20",         "(H-h)/2"),
    "bottom_left":   ("20",             "H-h-20"),
    "bottom_center": ("(W-w)/2",        "H-h-40"),
    "bottom_right":  ("W-w-20",         "H-h-20"),
}


def _drawtext_filter(text: str, position: str, start: float, end: float, size: int, color: str) -> str:
    if position not in _POSITIONS:
        raise ValueError(
            f"unknown position {position!r}. Known: {', '.join(_POSITIONS.keys())}"
        )
    x, y = _POSITIONS[position]
    escaped = (
        text.replace("\\", "\\\\")
            .replace(":", "\\:")
            .replace("'", "\\'")
            .replace("%", "\\%")
    )
    return (
        f"drawtext=fontfile='{_font_file()}':text='{escaped}'"
        f":x={x}:y={y}:fontsize={size}:fontcolor={color}"
        f":box=1:boxcolor=black@0.5:boxborderw=10"
        f":enable='between(t,{start:.3f},{end:.3f})'"
    )


async def dispatch(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    asset_id = str(args["asset_id"])
    kind = str(args["kind"])
    src = ctx.registry.get(asset_id)
    if src.kind != "video":
        raise ToolError(
            f"add_overlay burns onto video; {asset_id} is a {src.kind}.",
            code="E_UNSUPPORTED",
            recovery=RECOVERY_SWITCH_TOOL,
            hint="To layer onto a still image, use composite. To caption an image, place it on the timeline as a clip first.",
        )

    duration = ffprobe_duration(src.path)
    start = float(args.get("start_sec", 0.0))
    end_raw = args.get("end_sec")
    end = duration if end_raw is None else float(end_raw)
    if end <= start:
        raise ToolError(
            f"end_sec ({end}) must be greater than start_sec ({start}).",
            code="E_BAD_ARG",
            recovery=RECOVERY_FIX_ARGS,
        )
    position = str(args.get("position", "bottom_center"))

    if kind == "text":
        text = str(args.get("text") or "")
        if not text:
            raise ToolError(
                "add_overlay kind=text requires non-empty text.",
                code="E_BAD_ARG",
                recovery=RECOVERY_FIX_ARGS,
            )
        size = int(args.get("font_size", 32))
        color = str(args.get("font_color", "white"))
        filter_str = _drawtext_filter(text, position, start, end, size, color)
        cmd_input_extra: list[str] = []
        vf_arg = ["-vf", filter_str]
        label = f"text {text!r} {position} {start:.2f}-{end:.2f}s"
    elif kind == "subtitle":
        text = str(args.get("text") or "")
        if not text:
            raise ToolError(
                "add_overlay kind=subtitle requires non-empty text.",
                code="E_BAD_ARG",
                recovery=RECOVERY_FIX_ARGS,
            )
        size = int(args.get("font_size", 28))
        color = str(args.get("font_color", "white"))
        filter_str = _drawtext_filter(text, "bottom_center", start, end, size, color)
        cmd_input_extra = []
        vf_arg = ["-vf", filter_str]
        label = f"subtitle {text!r} {start:.2f}-{end:.2f}s"
    elif kind == "image":
        overlay_id = args.get("overlay_asset_id")
        if not overlay_id:
            raise ToolError(
                "add_overlay kind=image requires overlay_asset_id.",
                code="E_BAD_ARG",
                recovery=RECOVERY_FIX_ARGS,
                hint="Pass the image asset_id to overlay via overlay_asset_id.",
            )
        overlay_rec = ctx.registry.get(str(overlay_id))
        if overlay_rec.kind != "image":
            raise ToolError(
                f"overlay_asset_id {overlay_id} is a {overlay_rec.kind}, expected an image.",
                code="E_BAD_ARG",
                recovery=RECOVERY_FIX_ARGS,
            )
        x, y = _OVERLAY_POSITIONS.get(position, _OVERLAY_POSITIONS["bottom_center"])
        cmd_input_extra = ["-i", str(overlay_rec.path)]
        vf_arg = [
            "-filter_complex",
            f"[0:v][1:v]overlay={x}:{y}:enable='between(t,{start:.3f},{end:.3f})'",
        ]
        label = f"image overlay {overlay_id} {position} {start:.2f}-{end:.2f}s"
    else:
        raise ToolError(
            f"unknown overlay kind: {kind!r}.",
            code="E_BAD_ARG",
            recovery=RECOVERY_FIX_ARGS,
            valid_options=["text", "image", "subtitle"],
        )

    new_id = ctx.registry.allocate_id("video")
    out_path = ctx.child_path(new_id, ".mp4")
    cmd = [
        "ffmpeg", "-y",
        "-i", str(src.path),
        *cmd_input_extra,
        *vf_arg,
        *get_video_encoder_args("h264"),
        "-c:a", "copy",
        "-movflags", "+faststart",
        str(out_path),
    ]
    await run_ffmpeg_with_progress(cmd, total_seconds=duration, progress=ctx.emit_progress)
    summary = f"added {label} to {asset_id}"
    lineage = [asset_id]
    if kind == "image":
        lineage.append(str(args["overlay_asset_id"]))
    record = ctx.registry.register_output(
        new_id, kind="video", path=out_path, summary=summary, lineage=lineage
    )
    return {
        "asset_id": new_id,
        "summary": record.summary,
        "metadata": {
            "kind": kind,
            "position": position,
            "start_sec": start,
            "end_sec": end,
            "duration_sec": duration,
        },
    }


__all__ = ["dispatch"]
