"""``sunburst`` element — rays radiating from a centre point.

An *element* is a pure ``(**params) -> [op dicts]`` overlay macro (see
:mod:`lumenframe.elements`): it draws one graphic and composes onto whatever is
beneath it. This one stamps ``rays`` straight lines evenly spread around a full
circle, each running from ``inner_r`` to ``outer_r`` px out from the centre
``(x, y)`` px-from-centre — a radial burst / starburst / sun motif. Each ray is
an ``add_shape`` line; colour and ``width`` are params so it restyles in place.
No background — overlay only.
"""
from __future__ import annotations

import math
from typing import Any

from lumenframe.templates import theme


def sunburst(
    *,
    x: float = 0.0,
    y: float = 0.0,
    rays: int = 16,
    inner_r: float = 60.0,
    outer_r: float = 260.0,
    width_px: float = 6.0,
    color: str | None = None,
    start: float = 0.0,
    duration: float = 3.0,
    prefix: str = "sunburst",
    animate: bool = True,
    width: int = theme.DEFAULT_W,
    height: int = theme.DEFAULT_H,
) -> list[dict[str, Any]]:
    """Draw a radial burst of evenly-angled rays.

    Args:
        x, y: burst centre, px from canvas centre (x right+, y down+).
        rays: number of rays evenly spread around the full circle.
        inner_r: distance from centre where each ray starts, px.
        outer_r: distance from centre where each ray ends, px.
        width_px: stroke width of each ray in px.
        color: ray colour; ``None`` == the brand ice-blue accent.
        start, duration: timeline placement of the layers (seconds).
        prefix: id prefix so the same element can be stamped twice without clash.
        animate: when True, each ray fades in (opacity 0→1 over ~0.4s).
        width, height: canvas size the px→normalised bridge tracks.

    Returns:
        A list of op dicts (one ``add_shape`` line per ray plus fade-in
        keyframes) — no background, overlay only.
    """
    col = color or theme.PALETTES["lumeri"]["accent"]
    start = float(start)
    duration = float(duration)
    n = max(1, int(rays))
    r0 = float(inner_r)
    r1 = float(outer_r)

    def N(px: float, py: float) -> list[float]:
        return [theme.nx(px, width), theme.ny(py, height)]

    ops: list[dict[str, Any]] = []
    ray_ids: list[str] = []
    for i in range(n):
        ang = (i / n) * 2.0 * math.pi
        ux, uy = math.cos(ang), math.sin(ang)
        p0 = N(x + r0 * ux, y + r0 * uy)
        p1 = N(x + r1 * ux, y + r1 * uy)
        rid = f"{prefix}_ray{i}"
        ray_ids.append(rid)
        ops.append(
            {"op": "add_shape", "id": rid, "name": f"Sunburst ray {i}", "kind": "line",
             "points": [p0, p1],
             "stroke": {"color": col, "width": float(width_px)},
             "at_time": start, "duration": duration}
        )

    if animate:
        for rid in ray_ids:
            ops += _fade_in(rid, start, 0.4, clamp=duration)

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
