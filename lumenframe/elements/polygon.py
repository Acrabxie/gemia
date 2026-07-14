"""``polygon`` element — a regular N-gon (triangle, pentagon, hexagon…).

An *element* is a pure ``(**params) -> [op dicts]`` overlay macro (see
:mod:`lumenframe.elements`): it draws one graphic and composes onto whatever is
beneath it. This one computes the ``sides`` vertices of a regular polygon of the
given ``radius`` (circumradius) about ``(x, y)`` px-from-centre, spun by
``rotation`` degrees, and stamps them as a single ``add_shape`` polygon. In
``mode="fill"`` it is solid; in ``mode="outline"`` the fill is dropped and only a
``stroke`` is drawn — either way the canvas corners stay transparent.
"""
from __future__ import annotations

import math
from typing import Any

from lumenframe.templates import theme


def polygon(
    *,
    x: float = 0.0,
    y: float = 0.0,
    radius: float = 200.0,
    sides: int = 6,
    rotation: float = 0.0,
    mode: str = "fill",
    color: str | None = None,
    fill: str | None = None,
    stroke: float = 10.0,
    start: float = 0.0,
    duration: float = 3.0,
    prefix: str = "polygon",
    animate: bool = True,
    width: int = theme.DEFAULT_W,
    height: int = theme.DEFAULT_H,
) -> list[dict[str, Any]]:
    """Draw a regular N-gon from center + radius + sides + rotation.

    Args:
        x, y: polygon centre, px from canvas centre (x right+, y down+).
        radius: circumradius in px (centre → each vertex).
        sides: number of sides (>= 3); e.g. 3 triangle, 5 pentagon, 6 hexagon.
        rotation: spin in degrees (0 puts the first vertex straight up).
        mode: ``"fill"`` for a solid polygon, ``"outline"`` for stroke only.
        color: primary colour; ``None`` == brand ice-blue accent. Used as the
            fill (fill mode) or the stroke (outline mode).
        fill: explicit fill override; falls back to ``color`` when None.
        stroke: outline thickness in px (used in outline mode).
        start, duration: timeline placement of the layer (seconds).
        prefix: id prefix so the element can be stamped twice without clash.
        animate: when True, the layer fades in (opacity 0→1 over ~0.4s).
        width, height: canvas size the px→normalised bridge tracks.

    Returns:
        A list containing one ``add_shape`` polygon op (plus its fade-in
        keyframes) — no background, overlay only.
    """
    col = color or theme.PALETTES["lumeri"]["accent"]
    start = float(start)
    duration = float(duration)
    n = max(3, int(sides))
    r = max(1.0, float(radius))
    outline = str(mode).lower() == "outline"

    # Vertices: start at top (−90° on the y-down canvas) and step evenly.
    pts: list[list[float]] = []
    base = math.radians(float(rotation)) - math.pi / 2.0
    for k in range(n):
        a = base + 2.0 * math.pi * k / n
        px = x + r * math.cos(a)
        py = y + r * math.sin(a)
        pts.append([theme.nx(px, width), theme.ny(py, height)])

    poly_id = f"{prefix}_polygon"
    shape: dict[str, Any] = {
        "op": "add_shape", "id": poly_id,
        "name": f"{n}-gon", "kind": "polygon", "points": pts,
        "at_time": start, "duration": duration,
    }
    if outline:
        shape["fill"] = None
        shape["stroke"] = {"color": col, "width": float(stroke)}
    else:
        shape["fill"] = fill or col

    ops: list[dict[str, Any]] = [shape]
    if animate:
        ops += _fade_in(poly_id, start, 0.4, clamp=duration)
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
