"""``sparkle`` element — a 4-point sparkle/star twinkle.

An *element* is a pure ``(**params) -> [op dicts]`` overlay macro (see
:mod:`lumenframe.elements`): it draws one graphic and composes onto whatever is
beneath it. This one stamps a concave 8-vertex polygon — four long points
(up / right / down / left) tucked between four short inner corners — so it reads
as the classic "twinkle" spark. It is a single ``add_shape`` polygon filled with
the brand accent — real pixels, no background. Position, ``size`` and colour are
params so it restyles in place.
"""
from __future__ import annotations

from typing import Any

from lumenframe.templates import theme


def sparkle(
    *,
    x: float = 0.0,
    y: float = 0.0,
    size: float = 140.0,
    color: str | None = None,
    start: float = 0.0,
    duration: float = 3.0,
    prefix: str = "sparkle",
    animate: bool = True,
    width: int = theme.DEFAULT_W,
    height: int = theme.DEFAULT_H,
) -> list[dict[str, Any]]:
    """Draw a 4-point sparkle (concave 8-vertex polygon twinkle).

    Args:
        x, y: sparkle centre, px from canvas centre (x right+, y down+).
        size: outer radius in px (tip-to-centre of each of the 4 long points).
        color: fill colour; ``None`` == the brand ice-blue accent.
        start, duration: timeline placement of the layer (seconds).
        prefix: id prefix so the same element can be stamped twice without clash.
        animate: when True, the layer fades in (opacity 0→1 over ~0.4s).
        width, height: canvas size the px→normalised bridge tracks.

    Returns:
        A list of op dicts (one ``add_shape`` polygon plus its fade-in
        keyframes) — no background, overlay only.
    """
    col = color or theme.PALETTES["lumeri"]["accent"]
    start = float(start)
    duration = float(duration)
    outer = max(1.0, float(size))
    inner = outer * 0.34  # depth of the concave notches between long points

    # 8 vertices, alternating long point (outer, on-axis) and short corner
    # (inner, on the diagonal): up, up-right, right, down-right, down, …
    pts_px = [
        (x, y - outer),               # up point
        (x + inner, y - inner),       # inner (upper-right notch)
        (x + outer, y),               # right point
        (x + inner, y + inner),       # inner (lower-right notch)
        (x, y + outer),               # down point
        (x - inner, y + inner),       # inner (lower-left notch)
        (x - outer, y),               # left point
        (x - inner, y - inner),       # inner (upper-left notch)
    ]

    points = [[theme.nx(px, width), theme.ny(py, height)] for px, py in pts_px]

    star_id = f"{prefix}_star"
    ops: list[dict[str, Any]] = [
        {"op": "add_shape", "id": star_id, "name": "Sparkle", "kind": "polygon",
         "points": points, "fill": col,
         "at_time": start, "duration": duration},
    ]

    if animate:
        ops += _fade_in(star_id, start, 0.4, clamp=duration)

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
