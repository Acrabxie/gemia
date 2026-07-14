"""``diagonal_stripes`` element — a bounded band of parallel diagonal stripes.

An *element* is a pure ``(**params) -> [op dicts]`` overlay macro (see
:mod:`lumenframe.elements`): it draws one graphic and composes onto whatever is
beneath it. This one stamps ``count`` parallel stripes at ``angle`` degrees
(``add_shape`` line each), evenly distributed across a bounded ``box_w × box_h``
band centred on ``(x, y)`` px-from-centre. Each stripe is clipped (Liang–Barsky)
to the band rectangle so nothing spills past the box — it is a *bounded block*,
never a full-frame fill, so a corner of the canvas stays transparent and it
reads as an overlay hatch.

Note: the task names this element's stroke param ``width`` (a *content* param),
which shadows the shared canvas-``width`` param. Canvas width is therefore
tracked here through ``canvas_width`` (default :data:`theme.DEFAULT_W`); canvas
height stays the shared ``height``.
"""
from __future__ import annotations

import math
from typing import Any

from lumenframe.templates import theme


def diagonal_stripes(
    *,
    x: float = 0.0,
    y: float = 0.0,
    box_w: float = 640.0,
    box_h: float = 480.0,
    count: int = 8,
    angle: float = 45.0,
    width: float = 6.0,
    color: str | None = None,
    start: float = 0.0,
    duration: float = 3.0,
    prefix: str = "diagonal_stripes",
    animate: bool = True,
    canvas_width: int = theme.DEFAULT_W,
    height: int = theme.DEFAULT_H,
) -> list[dict[str, Any]]:
    """Draw ``count`` parallel diagonal stripes clipped to a ``box_w × box_h`` band.

    Args:
        x, y: band centre, px from canvas centre (x right+, y down+).
        box_w, box_h: size of the bounded stripe band in px.
        count: number of parallel stripes (>= 1).
        angle: stripe direction in degrees (0=horizontal, 45=up-right,
            90=vertical), y-down canvas.
        width: stroke thickness of each stripe in px.
        color: stripe colour; ``None`` == the brand ice-blue accent.
        start, duration: timeline placement of the layers (seconds).
        prefix: id prefix so the same element can be stamped twice without clash.
        animate: when True, each stripe fades in (opacity 0->1 over ~0.4s).
        canvas_width, height: canvas size the px->normalised bridge tracks.

    Returns:
        A list of op dicts (one ``add_shape`` line per visible stripe plus their
        fade-in keyframes) — no background, overlay only.
    """
    col = color or theme.PALETTES["lumeri"]["accent"]
    start = float(start)
    duration = float(duration)
    bw = float(box_w)
    bh = float(box_h)
    count = max(1, int(count))
    stroke = max(0.5, float(width))
    cw = int(canvas_width)

    # Direction unit vector (y-down: negate sin so +angle reads visually up).
    rad = math.radians(float(angle))
    dx, dy = math.cos(rad), -math.sin(rad)
    # Perpendicular unit vector — stripes are offset along this axis.
    perp_x, perp_y = -dy, dx

    # Half-extent of the box projected onto the perpendicular axis: keeps the
    # offsets spread just wide enough to cover the band without wasted stripes.
    half_perp = 0.5 * (bw * abs(perp_x) + bh * abs(perp_y))

    left, right = x - bw / 2.0, x + bw / 2.0
    top, bottom = y - bh / 2.0, y + bh / 2.0
    reach = bw + bh  # long enough to cross the whole box before clipping

    def N(px: float, py: float) -> list[float]:
        return [theme.nx(px, cw), theme.ny(py, height)]

    ops: list[dict[str, Any]] = []
    layer_ids: list[str] = []

    for i in range(count):
        # Evenly space offsets across [-half_perp, half_perp].
        if count == 1:
            off = 0.0
        else:
            off = -half_perp + (2.0 * half_perp) * (i / (count - 1))
        # A point on this stripe's centreline, extended far each way…
        cxp, cyp = x + off * perp_x, y + off * perp_y
        ax, ay = cxp - reach * dx, cyp - reach * dy
        bx, by = cxp + reach * dx, cyp + reach * dy
        seg = _clip_to_box(ax, ay, bx, by, left, top, right, bottom)
        if seg is None:
            continue
        (sx, sy), (ex, ey) = seg
        lid = f"{prefix}_{i}"
        layer_ids.append(lid)
        ops.append({
            "op": "add_shape", "id": lid, "name": f"Stripe {i}", "kind": "line",
            "points": [N(sx, sy), N(ex, ey)],
            "stroke": {"color": col, "width": stroke},
            "at_time": start, "duration": duration,
        })

    if animate:
        for lid in layer_ids:
            ops += _fade_in(lid, start, 0.4, clamp=duration)

    return ops


def _clip_to_box(
    x0: float, y0: float, x1: float, y1: float,
    xmin: float, ymin: float, xmax: float, ymax: float,
) -> tuple[tuple[float, float], tuple[float, float]] | None:
    """Liang–Barsky clip of segment (x0,y0)->(x1,y1) to an axis-aligned box.

    Returns the clipped endpoints, or ``None`` if the segment misses the box.
    """
    dx = x1 - x0
    dy = y1 - y0
    p = [-dx, dx, -dy, dy]
    q = [x0 - xmin, xmax - x0, y0 - ymin, ymax - y0]
    t0, t1 = 0.0, 1.0
    for pi, qi in zip(p, q):
        if pi == 0.0:
            if qi < 0.0:
                return None  # parallel and outside this boundary
            continue
        t = qi / pi
        if pi < 0.0:
            if t > t1:
                return None
            if t > t0:
                t0 = t
        else:
            if t < t0:
                return None
            if t < t1:
                t1 = t
    return ((x0 + t0 * dx, y0 + t0 * dy), (x0 + t1 * dx, y0 + t1 * dy))


def _fade_in(layer_id: str, t0: float, fade: float, *, clamp: float | None = None) -> list[dict[str, Any]]:
    """Two opacity keyframes (0 -> 1) for a soft fade-in at absolute time ``t0``."""
    t1 = t0 + fade
    if clamp is not None:
        t1 = min(t1, t0 + max(0.05, clamp))
    return [
        {"op": "set_keyframe", "layer_id": layer_id, "property": "opacity", "t": round(t0, 4), "value": 0.0, "interp": "linear"},
        {"op": "set_keyframe", "layer_id": layer_id, "property": "opacity", "t": round(t1, 4), "value": 1.0, "interp": "ease_out"},
    ]
