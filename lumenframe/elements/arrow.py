"""``arrow`` element — a directional arrow drawn as a shaft + a triangular head.

An *element* is a pure ``(**params) -> [op dicts]`` overlay macro (see
:mod:`lumenframe.elements`): it draws one graphic and composes onto whatever is
beneath it. This one stamps a straight arrow of a given ``length`` pointing along
``angle_deg`` (0° = right, 90° = up, 180° = left, −90°/270° = down, using the
canvas' y-down convention), centred on ``(x, y)`` px-from-centre. The shaft is an
``add_shape`` line and the head an ``add_shape`` polygon triangle — real pixels,
no background. Colour, thickness and head size are params so it restyles in place.
"""
from __future__ import annotations

import math
from typing import Any

from lumenframe.templates import theme


def arrow(
    *,
    x: float = 0.0,
    y: float = 0.0,
    length: float = 300.0,
    angle_deg: float = 0.0,
    color: str | None = None,
    width_px: float = 10.0,
    head_size: float = 60.0,
    start: float = 0.0,
    duration: float = 3.0,
    prefix: str = "arrow",
    animate: bool = True,
    width: int = theme.DEFAULT_W,
    height: int = theme.DEFAULT_H,
) -> list[dict[str, Any]]:
    """Draw a directional arrow (shaft line + triangular head).

    Args:
        x, y: arrow centre, px from canvas centre (x right+, y down+).
        length: total arrow length in px (tail tip → head tip).
        angle_deg: direction the arrow points (0=right, 90=up, 180=left,
            −90=down); measured counter-clockwise on the y-down canvas.
        color: shaft/head colour; ``None`` == the brand ice-blue accent.
        width_px: shaft stroke thickness in px.
        head_size: length of the triangular head in px (its base spans
            ``head_size`` across, so it reads as an equilateral-ish arrowhead).
        start, duration: timeline placement of the layers (seconds).
        prefix: id prefix so the same element can be stamped twice without clash.
        animate: when True, each layer fades in (opacity 0→1 over ~0.4s).
        width, height: canvas size the px→normalised bridge tracks.

    Returns:
        A list of op dicts (one ``add_shape`` line + one ``add_shape`` polygon,
        plus their fade-in keyframes) — no background, overlay only.
    """
    col = color or theme.PALETTES["lumeri"]["accent"]
    start = float(start)
    duration = float(duration)
    half = float(length) / 2.0
    head = max(1.0, float(head_size))

    # Unit direction (y-down canvas: negate sin so +angle points visually up).
    rad = math.radians(float(angle_deg))
    ux, uy = math.cos(rad), -math.sin(rad)
    # Perpendicular unit vector (for the head's base corners).
    px, py = -uy, ux

    tip = (x + half * ux, y + half * uy)
    tail = (x - half * ux, y - half * uy)
    # Base of the head, set back from the tip by ``head`` along the shaft.
    base = (tip[0] - head * ux, tip[1] - head * uy)
    half_base = head * 0.5
    left = (base[0] + half_base * px, base[1] + half_base * py)
    right = (base[0] - half_base * px, base[1] - half_base * py)

    def N(pt: tuple[float, float]) -> list[float]:
        return [theme.nx(pt[0], width), theme.ny(pt[1], height)]

    shaft_id = f"{prefix}_shaft"
    head_id = f"{prefix}_head"
    ops: list[dict[str, Any]] = [
        {"op": "add_shape", "id": shaft_id, "name": "Arrow shaft", "kind": "line",
         "points": [N(tail), N(base)],
         "stroke": {"color": col, "width": float(width_px)},
         "at_time": start, "duration": duration},
        {"op": "add_shape", "id": head_id, "name": "Arrow head", "kind": "polygon",
         "points": [N(tip), N(left), N(right)], "fill": col,
         "at_time": start, "duration": duration},
    ]

    if animate:
        for lid in (shaft_id, head_id):
            ops += _fade_in(lid, start, 0.4, clamp=duration)

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
