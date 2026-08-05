"""composite: combine two visual layers (image+image, video+video, video+image).

Modes map to ffmpeg's overlay (alpha) or blend filter (blend/screen/multiply).

``alpha``    — straight overlay honoring the overlay's alpha channel.
``blend``    — normal blend (all_mode='normal') at the requested opacity.
``screen``   — additive lightening blend.
``multiply`` — darkening blend.

``position`` is the overlay's top-left pixel offset; default (0, 0).
``scale``    multiplies the overlay size before compositing; default 1.0.

The kind of the produced asset matches the base asset's kind.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from gemia.tools._context import ToolContext
from gemia.tools._ffmpeg import ffprobe_duration, get_video_encoder_args, run_ffmpeg_with_progress


_BLEND_MODES: dict[str, str] = {
    "blend":    "normal",
    "screen":   "screen",
    "multiply": "multiply",
}


async def dispatch(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    base_id = str(args["base_asset_id"])
    overlay_id = str(args["overlay_asset_id"])
    mode = str(args["mode"])
    if mode not in _BLEND_MODES and mode != "alpha":
        raise ValueError(
            f"unknown composite mode: {mode!r}. Known: alpha, blend, screen, multiply"
        )

    opacity_raw = args.get("opacity", 1.0)
    try:
        opacity = float(opacity_raw)
    except (TypeError, ValueError):
        raise ValueError(f"opacity must be a number 0..1, got {opacity_raw!r}")
    if not 0.0 <= opacity <= 1.0:
        raise ValueError(f"opacity must be in [0, 1], got {opacity}")

    position = args.get("position") or {}
    x = int(position.get("x", 0))
    y = int(position.get("y", 0))
    scale_raw = args.get("scale")
    scale = float(scale_raw) if scale_raw is not None else 1.0
    if scale <= 0:
        raise ValueError(f"scale must be > 0, got {scale}")

    base = ctx.registry.get(base_id)
    overlay = ctx.registry.get(overlay_id)
    if base.kind not in {"video", "image"}:
        raise ValueError(f"composite base must be video or image, got {base.kind!r}")
    if overlay.kind not in {"video", "image"}:
        raise ValueError(f"composite overlay must be video or image, got {overlay.kind!r}")

    duration = ffprobe_duration(base.path) if base.kind == "video" else 0.0
    out_kind = base.kind
    new_id = ctx.registry.allocate_id(out_kind)
    out_ext = ".mp4" if out_kind == "video" else ".png"
    out_path = ctx.child_path(new_id, out_ext)

    overlay_pre = f"scale=iw*{scale}:ih*{scale}" if scale != 1.0 else "null"

    if mode == "alpha":
        filter_complex = (
            f"[1:v]{overlay_pre},format=yuva420p,colorchannelmixer=aa={opacity:.4f}[ovl];"
            f"[0:v][ovl]overlay={x}:{y}[out]"
        )
    else:
        blend_mode = _BLEND_MODES[mode]
        # blend filter needs both inputs same size; scale overlay to base size.
        filter_complex = (
            f"[1:v]{overlay_pre},format=yuva420p,colorchannelmixer=aa={opacity:.4f}[ovl_a];"
            f"[0:v]format=yuva420p[base_a];"
            f"[base_a][ovl_a]blend=all_mode='{blend_mode}':shortest=1[out]"
        )

    cmd_input_extra = ["-i", str(overlay.path)]
    if out_kind == "image":
        cmd = [
            "ffmpeg", "-y",
            "-i", str(base.path),
            *cmd_input_extra,
            "-filter_complex", filter_complex,
            "-map", "[out]",
            "-frames:v", "1",
            str(out_path),
        ]
    else:
        cmd = [
            "ffmpeg", "-y",
            "-i", str(base.path),
            *cmd_input_extra,
            "-filter_complex", filter_complex,
            "-map", "[out]",
            "-map", "0:a?",
            *get_video_encoder_args("h264"),
            "-c:a", "copy",
            "-movflags", "+faststart",
            str(out_path),
        ]
    await run_ffmpeg_with_progress(cmd, total_seconds=duration, progress=ctx.emit_progress)

    pos_label = f"at ({x},{y})" if (x or y) else "centered-origin"
    scale_label = f" scale={scale:.2f}x" if scale != 1.0 else ""
    summary = (
        f"composited {overlay_id} onto {base_id} mode={mode} opacity={opacity:.2f}"
        f" {pos_label}{scale_label}"
    )
    record = ctx.registry.register_output(
        new_id, kind=out_kind, path=out_path, summary=summary,
        lineage=[base_id, overlay_id],
    )
    return {
        "asset_id": new_id,
        "summary": record.summary,
        "metadata": {
            "mode": mode,
            "opacity": opacity,
            "position": {"x": x, "y": y},
            "scale": scale,
            "kind": out_kind,
            "duration_sec": duration if out_kind == "video" else None,
        },
    }


__all__ = ["dispatch"]
