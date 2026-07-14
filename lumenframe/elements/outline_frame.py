"""``outline_frame`` element — a hollow rectangle outline (stroke only).

An *element* is a pure ``(**params) -> [op dicts]`` overlay macro (see
:mod:`lumenframe.elements`): it draws one graphic and composes onto whatever is
beneath it. This one stamps a ``box_w`` × ``box_h`` rectangle centred on
``(x, y)`` px-from-centre, drawn as an ``add_shape`` rect with ``fill=None`` and
a ``stroke`` — so it reads as a border / picture-frame, its interior (and the
canvas corners) staying transparent. Optional ``radius`` rounds the corners.
"""
from __future__ import annotations

from typing import Any

from lumenframe.templates import theme


def outline_frame(
    *,
    x: float = 0.0,
    y: float = 0.0,
    box_w: float = 600.0,
    box_h: float = 400.0,
    color: str | None = None,
    stroke: float = 10.0,
    radius: float = 0.0,
    start: float = 0.0,
    duration: float = 3.0,
    prefix: str = "outline_frame",
    animate: bool = True,
    width: int = theme.DEFAULT_W,
    height: int = theme.DEFAULT_H,
) -> list[dict[str, Any]]:
    """Draw a rectangle outline (stroke only, no fill).

    Args:
        x, y: frame centre, px from canvas centre (x right+, y down+).
        box_w, box_h: frame width / height in px.
        color: stroke colour; ``None`` == brand ice-blue accent.
        stroke: outline thickness in px.
        radius: corner radius in px (0 = sharp corners).
        start, duration: timeline placement of the layer (seconds).
        prefix: id prefix so the element can be stamped twice without clash.
        animate: when True, the layer fades in (opacity 0→1 over ~0.4s).
        width, height: canvas size the px→normalised bridge tracks.

    Returns:
        A list containing one ``add_shape`` rect op (fill=None + stroke) plus its
        fade-in keyframes — no background, overlay only.
    """
    col = color or theme.PALETTES["lumeri"]["accent"]
    start = float(start)
    duration = float(duration)
    hw = max(1.0, float(box_w)) / 2.0
    hh = max(1.0, float(box_h)) / 2.0

    x0 = theme.nx(x - hw, width)
    y0 = theme.ny(y - hh, height)
    x1 = theme.nx(x + hw, width)
    y1 = theme.ny(y + hh, height)

    frame_id = f"{prefix}_frame"
    ops: list[dict[str, Any]] = [
        {"op": "add_shape", "id": frame_id, "name": "Outline frame", "kind": "rect",
         "rect": [x0, y0, x1, y1], "fill": None,
         "stroke": {"color": col, "width": float(stroke)},
         "radius": float(radius),
         "at_time": start, "duration": duration},
    ]
    if animate:
        ops += _fade_in(frame_id, start, 0.4, clamp=duration)
    return ops


def _fade_in(layer_id: str, t0: float, fade: float, *, clamp: float | None = None) -> list[dict[str, Any]]:
    """Two opacity keyframes (0 → 1) for a soft fade-in at absolute time ``t0``."""
    t1 = t0 + fade
    if clamp is not None:
        t1 = min(t1, t0 + max(0.05, clamp))
    return [
        {"op": "set_keyframe", "layer_id": layer_id, "property": "opacity", "t": round(t0, 4), "value": 0.0, "interp": "linear"},
        {"op": "set_keyframe", "layer_id": layer_id, "property": "opacity", "t": round(t1, 4), "value": 1.0, "interp": "ease_out"},
    ]
