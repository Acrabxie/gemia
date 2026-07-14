"""edit_image: crop | rotate | resize | blur | denoise | remove_background.

The ffmpeg ops (``crop``/``rotate``/``resize``/``blur``/``denoise``) overlap
with ``transform_geometry`` on purpose — this verb is the image-specific
surface the model reaches for when it's working with an image asset and
doesn't need to think about geometry math.

``remove_background`` is a real ML subject/portrait matting op (see
``gemia.picture.matting``): a U2Net human-segmentation net whose mask is
refined edge-aware and colour-decontaminated, producing a clean alpha cutout
for an *arbitrary* background — not a chroma/luma key that only works on green
screens. It degrades to a GrabCut fallback when the model is unavailable, so
it never hard-fails. Params: ``background`` (null → transparent PNG, a colour
name / ``[r,g,b]`` / an asset_id / a path → composite over it), ``feather``
(edge softness in px), ``matte_only`` (write the grayscale alpha instead).
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Callable

from gemia.errors import RECOVERY_FIX_ARGS, RECOVERY_SWITCH_TOOL, ToolError
from gemia.tools._context import ProgressUpdate, ToolContext
from gemia.tools._ffmpeg import run_ffmpeg_with_progress


async def dispatch(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    asset_id = str(args["asset_id"])
    operation = str(args["operation"])
    params = args.get("params") or {}
    if not isinstance(params, dict):
        raise ToolError(
            f"params must be an object, got {type(params).__name__}.",
            code="E_BAD_ARG",
            recovery=RECOVERY_FIX_ARGS,
        )

    src = ctx.registry.get(asset_id)
    if src.kind != "image":
        raise ToolError(
            f"edit_image works on image assets; {asset_id} is a {src.kind}.",
            code="E_UNSUPPORTED",
            recovery=RECOVERY_SWITCH_TOOL,
            hint="For video use edit_video, color_grade, or transform_geometry. Extract a frame first if you need to edit a still.",
        )

    if operation == "remove_background":
        return await _run_remove_background(ctx, src, params)

    handler = _OPS.get(operation)
    if handler is None:
        raise ToolError(
            f"unknown edit_image operation: {operation!r}.",
            code="E_BAD_ARG",
            recovery=RECOVERY_FIX_ARGS,
            valid_options=list(_OPS) + ["remove_background"],
        )

    vf, label, metadata = handler(params)
    return await _run(ctx, src.path, asset_id, vf, label, metadata, operation)


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
    return (
        f"rotate={rad:.6f}:ow=rotw({rad:.6f}):oh=roth({rad:.6f}):c=black",
        f"rotate {angle:+.1f}°",
        {"angle_deg": angle},
    )


def _op_resize(params: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    target_w = params.get("w")
    target_h = params.get("h")
    if target_w is None and target_h is None:
        raise ValueError("resize requires params.w and/or params.h")
    w_expr = int(target_w) if target_w is not None else -2
    h_expr = int(target_h) if target_h is not None else -2
    return (
        f"scale={w_expr}:{h_expr}",
        f"resize w={w_expr} h={h_expr}",
        {"w": w_expr if target_w is not None else None,
         "h": h_expr if target_h is not None else None},
    )


def _op_blur(params: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    radius_raw = params.get("radius", 5.0)
    try:
        radius = float(radius_raw)
    except (TypeError, ValueError):
        raise ValueError(f"blur radius must be a number, got {radius_raw!r}")
    if radius <= 0:
        raise ValueError(f"blur radius must be > 0, got {radius}")
    sigma = max(0.5, radius / 2.0)
    return (
        f"gblur=sigma={sigma:.3f}",
        f"gaussian blur σ={sigma:.2f}",
        {"radius": radius, "sigma": sigma},
    )


def _op_denoise(params: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    strength_raw = params.get("strength", 1.0)
    try:
        strength = float(strength_raw)
    except (TypeError, ValueError):
        raise ValueError(f"denoise strength must be a number, got {strength_raw!r}")
    if strength <= 0:
        raise ValueError(f"denoise strength must be > 0, got {strength}")
    # nlmeans is high-quality but slow; tune patch/research sizes by strength.
    s = max(1.0, min(strength, 10.0))
    return (
        f"nlmeans=s={s:.2f}:p=7:r=15",
        f"denoise strength={s:.2f}",
        {"strength": s, "method": "nlmeans"},
    )


async def _run_remove_background(
    ctx: ToolContext, src: Any, params: dict[str, Any]
) -> dict[str, Any]:
    """Real ML matting: cut the subject out onto transparency (or a new bg)."""
    try:
        from gemia.picture import matting  # noqa: PLC0415
    except Exception as exc:  # pragma: no cover - env without cv2/numpy
        raise ToolError(
            f"remove_background backend unavailable: {exc}",
            code="E_NOT_AVAILABLE",
            recovery=RECOVERY_SWITCH_TOOL,
            hint="Install opencv-python + numpy; drop a U2Net onnx in ./models for best quality.",
        ) from exc

    background = params.get("background")
    # An asset_id background composites the subject over another loaded image.
    if isinstance(background, str) and ctx.registry.contains(background):
        background = str(ctx.registry.get(background).path)
    try:
        feather = float(params.get("feather", 0.0) or 0.0)
    except (TypeError, ValueError):
        raise ToolError(
            f"feather must be a number, got {params.get('feather')!r}.",
            code="E_BAD_ARG", recovery=RECOVERY_FIX_ARGS,
        )
    matte_only = bool(params.get("matte_only", False))

    new_id = ctx.registry.allocate_id("image")
    out_path = ctx.child_path(new_id, ".png")  # alpha needs PNG
    ctx.emit_progress(ProgressUpdate(message="removing background (ML matting)…"))
    info = await asyncio.to_thread(
        matting.remove_background,
        str(src.path), str(out_path),
        background=background, feather=feather, matte_only=matte_only,
    )
    what = "matte" if matte_only else ("cutout" if info.get("transparent") else "composite")
    tier = "ML" if info.get("ml") else "fallback"
    summary = (
        f"remove_background {src.asset_id} — {what} "
        f"[{tier}:{info.get('backend')}, subject {info.get('coverage', 0):.0%}]"
    )
    record = ctx.registry.register_output(
        new_id, kind="image", path=out_path, summary=summary, lineage=[src.asset_id]
    )
    return {
        "asset_id": new_id,
        "summary": record.summary,
        "metadata": {"operation": "remove_background", "kind": "image", **info},
    }


async def _run(
    ctx: ToolContext,
    src_path: Path,
    src_id: str,
    vf: str,
    label: str,
    metadata: dict[str, Any],
    op_name: str,
) -> dict[str, Any]:
    new_id = ctx.registry.allocate_id("image")
    out_path = ctx.child_path(new_id, _image_out_ext(src_path))
    cmd = [
        "ffmpeg", "-y",
        "-i", str(src_path),
        "-vf", vf,
        "-frames:v", "1",
        str(out_path),
    ]
    await run_ffmpeg_with_progress(cmd, total_seconds=0.0, progress=ctx.emit_progress)

    summary = f"edited image {src_id} — {label}"
    record = ctx.registry.register_output(
        new_id, kind="image", path=out_path, summary=summary, lineage=[src_id]
    )
    return {
        "asset_id": new_id,
        "summary": record.summary,
        "metadata": {"operation": op_name, "params": metadata, "kind": "image"},
    }


def _image_out_ext(src_path: Path) -> str:
    ext = src_path.suffix.lower()
    if ext in {".png", ".jpg", ".jpeg", ".webp"}:
        return ext
    return ".png"


_OPS: dict[str, Callable[[dict[str, Any]], tuple[str, str, dict[str, Any]]]] = {
    "crop": _op_crop,
    "rotate": _op_rotate,
    "resize": _op_resize,
    "blur": _op_blur,
    "denoise": _op_denoise,
}


__all__ = ["dispatch"]
