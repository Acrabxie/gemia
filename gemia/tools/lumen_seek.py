"""lumen_seek: locate/seek the current lumenframe doc to a moment in time.

Exposes the lumenframe SEEK feature (``lumenframe.seek``) as an agent verb so
Gemini can answer "what is the timeline doing at time T?" and show the user that
exact moment.

Two halves, both built on the existing read paths and the SAME asset-registration
path :mod:`gemia.tools.layer`'s ``lumen_render`` uses:

* ``state_at(doc, seconds)`` — a pixel-free state report (active layer ids +
  per-layer local_frame / source_frame / opacity / transform + the frame index).
* ``seek.seek(doc, ...)`` — render exactly that single frame to a preview PNG,
  registered as a session image asset (returns its asset_id).

ADD-ONLY: this module never edits the doc; it reuses ``layer._lumendoc`` to read
the session's current document and ``ctx.registry`` to register the preview.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from gemia.tools._context import ToolContext
from gemia.tools import layer as _layer

try:
    from lumenframe.seek import seek as _seek_frame, state_at as _state_at
except ImportError:  # pragma: no cover - graceful fallback if lumenframe missing
    _seek_frame = None
    _state_at = None


async def dispatch(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Locate/seek the current lumenframe doc to a time (seconds) or frame index.

    Reports the timeline state at that moment (which layers are active and how
    they are placed/sampled) AND renders that single frame to a preview PNG
    asset registered in the session.

    Args:
        seconds: A time on the document timeline to seek to. Mutually exclusive
            with ``frame``.
        frame: An explicit frame index to seek to. Mutually exclusive with
            ``seconds``.

    Returns:
        On success: ``{applied, frame, time, state, preview_asset_id, path,
        width, height}`` where ``state`` is the ``state_at`` report (time, frame,
        active_layer_ids, layers) and ``preview_asset_id`` names a registered
        image asset showing that frame.
        On failure: a structured error dict ``{applied: False, error_code,
        error_message}``.
    """
    if _seek_frame is None or _state_at is None:
        return {
            "applied": False,
            "error_code": "E_NOT_AVAILABLE",
            "error_message": "lumenframe.seek module not available",
        }

    seconds = args.get("seconds")
    frame = args.get("frame")

    # Validate: need exactly one locator (seconds | frame).
    if seconds is None and frame is None:
        return {
            "applied": False,
            "error_code": "E_ARG",
            "error_message": "lumen_seek: need one of 'seconds' or 'frame'",
        }
    if seconds is not None and frame is not None:
        return {
            "applied": False,
            "error_code": "E_ARG",
            "error_message": "lumen_seek: pass only one of 'seconds' or 'frame', not both",
        }

    try:
        seconds_f = None if seconds is None else float(seconds)
        frame_i = None if frame is None else int(frame)
    except (TypeError, ValueError) as e:
        return {
            "applied": False,
            "error_code": "E_ARG",
            "error_message": f"lumen_seek: invalid locator value: {e}",
        }

    doc = _layer._lumendoc(ctx)

    # Pixel-free state report. If a frame index was given, derive the equivalent
    # seconds via the document timebase so state_at lands on the same frame.
    try:
        if seconds_f is not None:
            state = _state_at(doc, seconds_f)
        else:
            from lumenframe import model, timebase

            norm = model.normalize_doc(doc or {})
            fps = float((norm.get("canvas") or {}).get("fps") or 0.0) or 0.0
            state = _state_at(doc, timebase.to_seconds(int(frame_i), fps))
    except Exception as e:
        return {
            "applied": False,
            "error_code": "E_STATE",
            "error_message": f"state_at failed: {e}",
        }

    # Render the single frame and register it as a preview image asset, mirroring
    # lumen_render's frame branch exactly (allocate_id -> child_path -> save PNG
    # -> register_output).
    try:
        if seconds_f is not None:
            frame_rgba = _seek_frame(doc, seconds=seconds_f)
        else:
            frame_rgba = _seek_frame(doc, frame=frame_i)
    except Exception as e:
        return {
            "applied": False,
            "error_code": "E_RENDER",
            "error_message": f"seek render failed: {e}",
        }

    output_kind = "image"
    asset_id = ctx.registry.allocate_id(output_kind)
    out_path = ctx.child_path(asset_id, ".png")

    try:
        import numpy as np
        from PIL import Image as PILImage

        frame_uint8 = np.asarray(frame_rgba * 255, dtype=np.uint8)
        img = PILImage.fromarray(frame_uint8, "RGBA")
        img.save(str(out_path))

        height, width = int(frame_uint8.shape[0]), int(frame_uint8.shape[1])
        seek_frame = int(state["frame"])
        summary = f"lumenframe seek preview (frame {seek_frame} @ {state['time']:.3f}s)"
        ctx.registry.register_output(
            asset_id,
            kind=output_kind,
            path=Path(out_path),
            summary=summary,
        )
    except Exception as e:
        return {
            "applied": False,
            "error_code": "E_SAVE",
            "error_message": f"failed to save preview frame: {e}",
        }

    return {
        "applied": True,
        "frame": int(state["frame"]),
        "time": state["time"],
        "state": state,
        "preview_asset_id": asset_id,
        "path": str(out_path),
        "width": width,
        "height": height,
        "summary": summary,
    }


__all__ = ["dispatch"]
