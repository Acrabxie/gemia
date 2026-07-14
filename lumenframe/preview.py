"""Sparse frame preview backend for lumenframe documents.

Rendering a whole timeline just to glance at a couple of frames is wasteful:
:func:`lumenframe.compile.compile_to_layer_stack` builds the full
``LayerStack`` once, but the expensive part is *per-frame* compositing. This
module (inspired by hyperframe-style snapshotting) compiles the document
**once** and then asks the stack for **only** the frame indices you request —
nothing in between.

``preview_frames(doc, frames)`` returns ``[(frame_index, rgba_float32), ...]``
in the order the indices were given. Each returned array is byte-for-byte
identical to what ``compile_to_layer_stack(doc).render_frame(idx)`` would
produce for the same (clamped) index, because that is literally the call made
under the hood — the compile happens just once for the whole batch.

Frame indices are **clamped** into the valid ``[0, total_frames)`` range so a
caller asking for "the last frame" with a sloppy large number, or a negative
index, still gets a sensible picture instead of an ``IndexError``. The returned
``frame_index`` is the clamped index actually rendered, so callers can see what
they got.

``preview_frames_png(doc, frames, ...)`` is an optional convenience that encodes
each previewed frame to PNG bytes via Pillow. It raises a clear error if Pillow
is not installed rather than failing deep inside the encoder.

This module is **add-only**: it composes the existing compile + render path and
introduces no new rendering behaviour.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Iterable

from lumenframe.compile import Resolver, compile_to_layer_stack

if TYPE_CHECKING:  # pragma: no cover - typing only
    import numpy as np


__all__ = ["preview_frames", "preview_frames_png"]


def _clamp_index(idx: Any, total_frames: int) -> int:
    """Coerce ``idx`` to an int and clamp it into ``[0, total_frames)``.

    ``total_frames`` is always ``>= 1`` for a compiled stack, so the clamped
    result is always a valid frame index for ``render_frame``.
    """
    i = int(idx)
    if i < 0:
        return 0
    last = total_frames - 1
    if i > last:
        return last
    return i


def preview_frames(
    doc: dict[str, Any] | None,
    frames: Iterable[Any],
    *,
    resolver: "Resolver | None" = None,
    strict: bool = False,
) -> list[tuple[int, "np.ndarray"]]:
    """Render a sparse set of frames from ``doc`` with a single compile.

    The document is compiled into a ``LayerStack`` exactly once; then
    ``LayerStack.render_frame`` is called only for each requested index
    (clamped into the valid range). No intermediate frames are rendered.

    Args:
        doc: A lumenframe document (same shape ``compile_to_layer_stack``
            accepts). May be ``None`` / partial — it is normalised on compile.
        frames: An iterable of requested frame indices. Each is coerced to an
            int and clamped to ``[0, total_frames)``. Order is preserved and
            duplicates are kept (one render per requested entry).
        resolver: Optional content resolver forwarded to the compile step.
        strict: Forwarded to the compile step; raise on unresolved content.

    Returns:
        A list of ``(clamped_frame_index, rgba_frame)`` tuples in request
        order. Each ``rgba_frame`` is a canvas-sized ``(height, width, 4)``
        float32 RGBA array, identical to a direct
        ``compile_to_layer_stack(doc).render_frame(clamped_frame_index)``.
    """
    stack = compile_to_layer_stack(doc, resolver=resolver, strict=strict)
    total = int(stack.total_frames)
    out: list[tuple[int, "np.ndarray"]] = []
    for requested in frames:
        idx = _clamp_index(requested, total)
        out.append((idx, stack.render_frame(idx)))
    return out


def preview_frames_png(
    doc: dict[str, Any] | None,
    frames: Iterable[Any],
    *,
    resolver: "Resolver | None" = None,
    strict: bool = False,
) -> list[tuple[int, bytes]]:
    """Like :func:`preview_frames`, but encode each frame to PNG bytes.

    Requires Pillow (``PIL``). Each previewed RGBA float32 frame is converted
    to 8-bit RGBA and encoded as a PNG.

    Args:
        doc / frames / resolver / strict: see :func:`preview_frames`.

    Returns:
        A list of ``(clamped_frame_index, png_bytes)`` tuples in request order.

    Raises:
        RuntimeError: if Pillow is not importable.
    """
    try:
        from PIL import Image
    except ImportError as exc:  # pragma: no cover - exercised only without PIL
        raise RuntimeError(
            "preview_frames_png requires Pillow (PIL); install it to encode PNGs."
        ) from exc

    import io

    import numpy as np

    out: list[tuple[int, bytes]] = []
    for idx, frame in preview_frames(doc, frames, resolver=resolver, strict=strict):
        rgba8 = (np.clip(frame, 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8)
        image = Image.fromarray(rgba8, mode="RGBA")
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        out.append((idx, buffer.getvalue()))
    return out
