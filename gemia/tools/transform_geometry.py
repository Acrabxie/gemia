"""transform_geometry: crop / rotate / scale / perspective for image or video.

Operations and their ``params`` shape:

- ``crop``        — {x: int, y: int, w: int, h: int} pixels. Origin top-left.
- ``rotate``      — {angle_deg: number}. Positive = clockwise. Output canvas
  expands to fit; padding is transparent (image) or black (video).
- ``scale``       — {w: int} or {h: int} or {factor: number}. When only one
  dimension is given the other is derived to preserve aspect. ``factor`` is
  a multiplicative shorthand.
- ``perspective`` — {top_left, top_right, bottom_left, bottom_right} each
  ``[x, y]`` pixel destinations of the four source corners.

The output kind matches the input kind.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from gemia.tools._context import ToolContext
from gemia.tools._ffmpeg import ffprobe_duration, get_video_encoder_args, run_ffmpeg_with_progress


async def dispatch(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    asset_id = str(args["asset_id"])
    operation = str(args["operation"])
    params = args.get("params") or {}
    if not isinstance(params, dict):
        raise ValueError(f"params must be an object, got {type(params).__name__}")

    src = ctx.registry.get(asset_id)
    if src.kind not in {"video", "image"}:
        raise ValueError(
            f"transform_geometry requires video or image, got {src.kind!r}"
        )

    handler = _OPS.get(operation)
    if handler is None:
        raise ValueError(
            f"unknown transform_geometry operation: {operation!r}. Known: {', '.join(_OPS.keys())}"
        )

    vf, label, metadata = handler(params)
    return await _run(ctx, src.path, src.kind, asset_id, vf, label, metadata)


def _op_crop(params: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    for key in ("x", "y", "w", "h"):
        if key not in params:
            raise ValueError(f"crop requires params.{key}")
    x, y, w, h = (int(params[k]) for k in ("x", "y", "w", "h"))
    if w <= 0 or h <= 0:
        raise ValueError(f"crop w/h must be > 0, got w={w} h={h}")
    return (
        f"crop={w}:{h}:{x}:{y}",
        f"crop {w}x{h}@({x},{y})",
        {"x": x, "y": y, "w": w, "h": h},
    )


def _op_rotate(params: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    if "angle_deg" not in params:
        raise ValueError("rotate requires params.angle_deg")
    angle = float(params["angle_deg"])
    rad = angle * 3.141592653589793 / 180.0
    # Expand canvas via rotated_w/h expressions; fill transparent (yuva) or black.
    return (
        f"rotate={rad:.6f}:ow=rotw({rad:.6f}):oh=roth({rad:.6f}):c=black",
        f"rotate {angle:+.1f}°",
        {"angle_deg": angle},
    )


def _op_scale(params: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    factor = params.get("factor")
    target_w = params.get("w")
    target_h = params.get("h")
    if factor is None and target_w is None and target_h is None:
        raise ValueError("scale requires one of params.factor / params.w / params.h")
    if factor is not None and (target_w is not None or target_h is not None):
        raise ValueError("scale: pass either factor OR w/h, not both")

    if factor is not None:
        factor = float(factor)
        if factor <= 0:
            raise ValueError(f"scale factor must be > 0, got {factor}")
        return (
            f"scale=iw*{factor}:ih*{factor}",
            f"scale {factor:.2f}x",
            {"factor": factor},
        )
    w_expr = int(target_w) if target_w is not None else -2
    h_expr = int(target_h) if target_h is not None else -2
    if isinstance(w_expr, int) and w_expr > 0 and isinstance(h_expr, int) and h_expr <= 0:
        return (
            f"scale={w_expr}:-2",
            f"scale w={w_expr}",
            {"w": w_expr},
        )
    if isinstance(h_expr, int) and h_expr > 0 and isinstance(w_expr, int) and w_expr <= 0:
        return (
            f"scale=-2:{h_expr}",
            f"scale h={h_expr}",
            {"h": h_expr},
        )
    return (
        f"scale={w_expr}:{h_expr}",
        f"scale {w_expr}x{h_expr}",
        {"w": w_expr, "h": h_expr},
    )


def _op_perspective(params: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    corners = ("top_left", "top_right", "bottom_left", "bottom_right")
    for key in corners:
        if key not in params:
            raise ValueError(f"perspective requires params.{key} (a [x, y] pair)")
        pair = params[key]
        if not (isinstance(pair, (list, tuple)) and len(pair) == 2):
            raise ValueError(f"perspective {key} must be a [x, y] pair, got {pair!r}")
    pts = {k: (float(params[k][0]), float(params[k][1])) for k in corners}
    spec = (
        f"perspective=x0={pts['top_left'][0]:.2f}:y0={pts['top_left'][1]:.2f}"
        f":x1={pts['top_right'][0]:.2f}:y1={pts['top_right'][1]:.2f}"
        f":x2={pts['bottom_left'][0]:.2f}:y2={pts['bottom_left'][1]:.2f}"
        f":x3={pts['bottom_right'][0]:.2f}:y3={pts['bottom_right'][1]:.2f}"
        f":interpolation=linear"
    )
    return spec, "perspective warp", {k: list(v) for k, v in pts.items()}


async def _run(
    ctx: ToolContext,
    src_path: Path,
    kind: str,
    src_id: str,
    vf: str,
    label: str,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    new_id = ctx.registry.allocate_id(kind)
    out_ext = ".mp4" if kind == "video" else _image_out_ext(src_path)
    out_path = ctx.child_path(new_id, out_ext)
    duration = ffprobe_duration(src_path) if kind == "video" else 0.0

    if kind == "video":
        cmd = [
            "ffmpeg", "-y",
            "-i", str(src_path),
            "-vf", vf,
            *get_video_encoder_args("h264"),
            "-c:a", "copy",
            "-movflags", "+faststart",
            str(out_path),
        ]
    else:
        cmd = [
            "ffmpeg", "-y",
            "-i", str(src_path),
            "-vf", vf,
            "-frames:v", "1",
            str(out_path),
        ]
    await run_ffmpeg_with_progress(cmd, total_seconds=duration, progress=ctx.emit_progress)

    summary = f"transformed {src_id} ({kind}) with {label}"
    record = ctx.registry.register_output(
        new_id, kind=kind, path=out_path, summary=summary, lineage=[src_id]
    )
    return {
        "asset_id": new_id,
        "summary": record.summary,
        "metadata": {
            "operation": label.split()[0],
            "params": metadata,
            "kind": kind,
            "duration_sec": duration if kind == "video" else None,
        },
    }


def _image_out_ext(src_path: Path) -> str:
    ext = src_path.suffix.lower()
    if ext in {".png", ".jpg", ".jpeg", ".webp"}:
        return ext
    return ".png"


_OPS: dict[str, Callable[[dict[str, Any]], tuple[str, str, dict[str, Any]]]] = {
    "crop": _op_crop,
    "rotate": _op_rotate,
    "scale": _op_scale,
    "perspective": _op_perspective,
}


__all__ = ["dispatch"]
