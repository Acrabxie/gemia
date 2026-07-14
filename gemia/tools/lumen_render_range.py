"""lumen_render_range: render or export ONLY a time range of the current doc.

Exposes the lumenframe RANGE feature (``lumenframe.render_range``) as an agent
verb so Gemini can work with a *slice* of the timeline — "give me seconds 1.0
through 2.5" — instead of the whole document.

* ``export`` truthy -> ``export_range`` writes the half-open ``[t_in, t_out)``
  window to a video asset (MP4), registered via the SAME asset path
  :mod:`gemia.tools.layer`'s ``lumen_render`` uses. Returns ``{asset_id,
  frame_count, t_in, t_out}``.
* otherwise -> ``render_range`` returns a short in-memory preview; this verb
  reports the rendered frame count (and saves a representative middle frame as a
  preview image asset).

ADD-ONLY: this module never edits the doc; it reuses ``layer._lumendoc`` to read
the session's current document and ``ctx.registry`` to register outputs.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from gemia.tools._context import ToolContext
from gemia.tools import layer as _layer

try:
    from lumenframe.render_range import export_range as _export_range, render_range as _render_range
    from lumenframe.compile import compile_to_layer_stack as _compile
    from lumenframe import timebase as _timebase
except ImportError:  # pragma: no cover - graceful fallback if lumenframe missing
    _export_range = None
    _render_range = None
    _compile = None
    _timebase = None


def _range_frame_count(doc: dict[str, Any], t_in: float, t_out: float, step: int) -> int:
    """Frames that ``export_range`` / ``render_range`` will produce for this range.

    Mirrors ``render_range``'s bounds math exactly: compile once, convert the
    seconds bounds to frames with the document timebase (``int(round(seconds *
    fps))``), clamp into ``[0, total_frames]``, then count
    ``range(start, stop, step)``.
    """
    stack = _compile(doc, strict=False)
    fps = float(stack.fps)
    total = int(stack.total_frames)
    frame_in = _timebase.to_frame(t_in, fps)
    frame_out = _timebase.to_frame(t_out, fps)
    start = min(max(0, int(frame_in)), total)
    stop = min(max(0, int(frame_out)), total)
    if stop < start:
        stop = start
    if start >= stop:
        return 0
    return len(range(start, stop, max(1, int(step))))


async def dispatch(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Render or export only the time range ``[t_in, t_out)`` of the current doc.

    Args:
        t_in: Inclusive start time in seconds.
        t_out: Exclusive end time in seconds. Must be > ``t_in``.
        step: Stride between frames (>= 1). Default 1.
        export: If truthy, export the range to an MP4 video asset; otherwise
            render a short in-memory preview and report its frame count (plus a
            representative middle-frame preview image asset).

    Returns:
        export truthy -> ``{applied, asset_id, frame_count, t_in, t_out, path,
        ...}`` (video asset).
        else -> ``{applied, frame_count, t_in, t_out, preview_asset_id?, ...}``.
        On failure: ``{applied: False, error_code, error_message}``.
    """
    if _export_range is None or _render_range is None or _compile is None:
        return {
            "applied": False,
            "error_code": "E_NOT_AVAILABLE",
            "error_message": "lumenframe.render_range module not available",
        }

    t_in = args.get("t_in")
    t_out = args.get("t_out")

    if t_in is None or t_out is None:
        return {
            "applied": False,
            "error_code": "E_ARG",
            "error_message": "lumen_render_range: need both 't_in' and 't_out' (seconds)",
        }

    try:
        t_in_f = float(t_in)
        t_out_f = float(t_out)
    except (TypeError, ValueError) as e:
        return {
            "applied": False,
            "error_code": "E_ARG",
            "error_message": f"lumen_render_range: invalid t_in/t_out: {e}",
        }

    if not (t_in_f < t_out_f):
        return {
            "applied": False,
            "error_code": "E_ARG",
            "error_message": f"lumen_render_range: need t_in < t_out (got t_in={t_in_f}, t_out={t_out_f})",
        }

    try:
        step = int(args.get("step", 1))
    except (TypeError, ValueError):
        step = 1
    if step < 1:
        step = 1

    export = bool(args.get("export"))
    doc = _layer._lumendoc(ctx)

    if export:
        # Export the range to an MP4 video asset, mirroring lumen_render's video
        # asset path (allocate_id -> child_path -> register_output).
        output_kind = "video"
        asset_id = ctx.registry.allocate_id(output_kind)
        out_path = ctx.child_path(asset_id, ".mp4")
        try:
            out_path_str = _export_range(doc, t_in_f, t_out_f, str(out_path), step=step)
        except ValueError as e:
            # Empty range after clamping -> nothing to encode.
            return {
                "applied": False,
                "error_code": "E_EMPTY_RANGE",
                "error_message": f"export_range produced no frames: {e}",
            }
        except Exception as e:
            return {
                "applied": False,
                "error_code": "E_RENDER",
                "error_message": f"export_range failed: {e}",
            }

        try:
            frame_count = _range_frame_count(doc, t_in_f, t_out_f, step)
        except Exception:
            frame_count = None

        summary = (
            f"lumenframe range export [{t_in_f:.3f}s, {t_out_f:.3f}s)"
            + (f" ({frame_count} frames)" if frame_count is not None else "")
        )
        ctx.registry.register_output(
            asset_id,
            kind=output_kind,
            path=Path(out_path_str),
            summary=summary,
        )

        return {
            "applied": True,
            "asset_id": asset_id,
            "frame_count": frame_count,
            "t_in": t_in_f,
            "t_out": t_out_f,
            "step": step,
            "path": str(out_path_str),
            "format": "mp4",
            "summary": summary,
        }

    # Preview path: render the range in-memory and report the frame count. Save a
    # representative middle frame as a preview image asset when frames exist.
    try:
        frames = _render_range(doc, t_in_f, t_out_f, step=step)
    except Exception as e:
        return {
            "applied": False,
            "error_code": "E_RENDER",
            "error_message": f"render_range failed: {e}",
        }

    frame_count = len(frames)
    if frame_count == 0:
        return {
            "applied": True,
            "frame_count": 0,
            "t_in": t_in_f,
            "t_out": t_out_f,
            "step": step,
            "preview_asset_id": None,
            "summary": f"lumenframe range preview [{t_in_f:.3f}s, {t_out_f:.3f}s): no frames",
        }

    # Register a representative middle frame as a preview image asset.
    output_kind = "image"
    asset_id = ctx.registry.allocate_id(output_kind)
    out_path = ctx.child_path(asset_id, ".png")
    try:
        import numpy as np
        from PIL import Image as PILImage

        mid = frames[frame_count // 2]
        mid_uint8 = np.asarray(mid * 255, dtype=np.uint8)
        img = PILImage.fromarray(mid_uint8, "RGBA")
        img.save(str(out_path))
        summary = (
            f"lumenframe range preview [{t_in_f:.3f}s, {t_out_f:.3f}s) "
            f"({frame_count} frames; showing middle frame)"
        )
        ctx.registry.register_output(
            asset_id,
            kind=output_kind,
            path=Path(out_path),
            summary=summary,
        )
    except Exception as e:
        # Preview asset is best-effort; still report the frame count.
        return {
            "applied": True,
            "frame_count": frame_count,
            "t_in": t_in_f,
            "t_out": t_out_f,
            "step": step,
            "preview_asset_id": None,
            "summary": f"lumenframe range preview [{t_in_f:.3f}s, {t_out_f:.3f}s) ({frame_count} frames); preview save failed: {e}",
        }

    return {
        "applied": True,
        "frame_count": frame_count,
        "t_in": t_in_f,
        "t_out": t_out_f,
        "step": step,
        "preview_asset_id": asset_id,
        "path": str(out_path),
        "summary": summary,
    }


__all__ = ["dispatch"]
