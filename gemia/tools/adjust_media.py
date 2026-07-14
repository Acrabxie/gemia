"""adjust_media: direct photo-app style color controls for image/video assets."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from gemia.errors import RECOVERY_FIX_ARGS, ToolError
from gemia.tools._context import ToolContext
from gemia.tools._ffmpeg import ffprobe_duration, run_ffmpeg_with_progress


_RANGES: dict[str, tuple[float, float, float]] = {
    "brightness": (-1.0, 1.0, 0.0),
    "contrast": (0.0, 3.0, 1.0),
    "saturation": (0.0, 3.0, 1.0),
    "exposure": (-5.0, 5.0, 0.0),
    "gamma": (0.1, 10.0, 1.0),
}


async def dispatch(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    asset_id = str(args["asset_id"])
    src = ctx.registry.get(asset_id)
    if src.kind not in {"video", "image"}:
        raise ToolError(
            f"adjust_media works on video or image assets; {asset_id} is a {src.kind}.",
            code="E_UNSUPPORTED",
            recovery=RECOVERY_FIX_ARGS,
            hint="Pass a video/image asset_id. For audio loudness use mix_audio or audio tools.",
        )

    params = _params(args)
    vf = _filter_graph(params)
    return await _run(ctx, src.path, src.kind, asset_id, vf, params)


def _params(args: dict[str, Any]) -> dict[str, float]:
    params: dict[str, float] = {}
    for key, (lo, hi, default) in _RANGES.items():
        raw = args.get(key, default)
        try:
            value = float(raw)
        except (TypeError, ValueError) as exc:
            raise ToolError(
                f"{key} must be a number, got {raw!r}.",
                code="E_BAD_ARG",
                recovery=RECOVERY_FIX_ARGS,
            ) from exc
        if not lo <= value <= hi:
            raise ToolError(
                f"{key} must be in [{lo}, {hi}], got {value}.",
                code="E_BAD_ARG",
                recovery=RECOVERY_FIX_ARGS,
                hint=(
                    "Use photo-app style ranges: brightness -1..1, "
                    "contrast/saturation 0..3, exposure -5..5 stops, gamma 0.1..10."
                ),
            )
        params[key] = value
    return params


def _filter_graph(params: dict[str, float]) -> str:
    brightness = params["brightness"]
    contrast = params["contrast"]
    saturation = params["saturation"]
    gamma = params["gamma"]
    exposure = params["exposure"]
    eq = (
        f"eq=brightness={brightness:.6f}:"
        f"contrast={contrast:.6f}:"
        f"saturation={saturation:.6f}:"
        f"gamma={gamma:.6f}"
    )
    if abs(exposure) < 1e-9:
        return eq
    gain = 2.0 ** exposure
    lut = (
        f"lutrgb=r='clip(val*{gain:.8f},0,255)':"
        f"g='clip(val*{gain:.8f},0,255)':"
        f"b='clip(val*{gain:.8f},0,255)'"
    )
    return f"{lut},{eq}"


async def _run(
    ctx: ToolContext,
    src_path: Path,
    kind: str,
    src_id: str,
    vf: str,
    params: dict[str, float],
) -> dict[str, Any]:
    new_id = ctx.registry.allocate_id(kind)
    out_path = ctx.child_path(new_id, ".mp4" if kind == "video" else _image_out_ext(src_path))
    duration = ffprobe_duration(src_path) if kind == "video" else 0.0
    if kind == "video":
        cmd = [
            "ffmpeg", "-y",
            "-i", str(src_path),
            "-vf", vf,
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
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
    summary = (
        f"adjusted {src_id}: brightness={params['brightness']:.2f}, "
        f"contrast={params['contrast']:.2f}, saturation={params['saturation']:.2f}, "
        f"exposure={params['exposure']:+.2f} stops, gamma={params['gamma']:.2f}"
    )
    record = ctx.registry.register_output(
        new_id, kind=kind, path=out_path, summary=summary, lineage=[src_id]
    )
    return {
        "asset_id": new_id,
        "summary": record.summary,
        "metadata": {
            "kind": kind,
            "duration_sec": duration if kind == "video" else None,
            "filter": vf,
            **params,
        },
    }


def _image_out_ext(src_path: Path) -> str:
    ext = src_path.suffix.lower()
    if ext in {".png", ".jpg", ".jpeg", ".webp"}:
        return ext
    return ".png"


__all__ = ["dispatch", "_filter_graph"]
