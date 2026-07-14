"""``chevron`` element — a ">" chevron, or a stack of ``count`` chevrons.

An *element* is a pure ``(**params) -> [op dicts]`` overlay macro (see
:mod:`lumenframe.elements`): it draws one graphic and composes onto whatever is
beneath it. This one stamps one or more ``>`` chevrons — each a two-segment
polyline (an ``add_shape`` line through three points) — pointing along
``angle`` (0° = right ``>``, 90° = up, 180° = left ``<``, −90°/270° = down,
using the canvas' y-down convention). Passing ``count`` > 1 lays a stack of
chevrons ``»`` spaced ``gap`` px apart *along the pointing direction*, the classic
"more / continue / fast-forward" marker. Real pixels, no background — colour,
``size`` and stroke ``width_px`` are params so it restyles in place.
"""
from __future__ import annotations

import math
from typing import Any

from lumenframe.templates import theme


def chevron(
    *,
    x: float = 0.0,
    y: float = 0.0,
    size: float = 120.0,
    angle: float = 0.0,
    width_px: float = 12.0,
    count: int = 1,
    gap: float = 90.0,
    color: str | None = None,
    start: float = 0.0,
    duration: float = 3.0,
    prefix: str = "chevron",
    animate: bool = True,
    width: int = theme.DEFAULT_W,
    height: int = theme.DEFAULT_H,
) -> list[dict[str, Any]]:
    """Draw a ">" chevron, or a stack of ``count`` chevrons along ``angle``.

    Args:
        x, y: centre of the (stack of) chevron(s), px from canvas centre
            (x right+, y down+).
        size: chevron extent in px — the arms span ``size`` tall and reach
            ``size/2`` forward to the tip, so it reads as a bold ``>``.
        angle: direction the chevron points (0=right ``>``, 90=up, 180=left
            ``<``, −90=down); measured counter-clockwise on the y-down canvas.
        width_px: stroke thickness of each chevron in px.
        count: how many chevrons to stack (``»`` for 3); each is offset ``gap``
            px from the previous along the pointing direction.
        gap: spacing between stacked chevrons in px (ignored when count==1).
        color: stroke colour; ``None`` == the brand ice-blue accent.
        start, duration: timeline placement of the layers (seconds).
        prefix: id prefix so the same element can be stamped twice without clash.
        animate: when True, each layer fades in (opacity 0→1 over ~0.4s).
        width, height: canvas size the px→normalised bridge tracks.

    Returns:
        A list of op dicts (one ``add_shape`` line per chevron, plus their
        fade-in keyframes) — no background, overlay only.
    """
    col = color or theme.PALETTES["lumeri"]["accent"]
    start = float(start)
    duration = float(duration)
    s = float(size)
    n = max(1, int(count))
    half = s / 2.0

    # Rotation basis (y-down canvas): local +x = pointing direction maps to
    # (cos, -sin) so +angle points visually up, matching the arrow element.
    rad = math.radians(float(angle))
    ca, sa = math.cos(rad), math.sin(rad)

    def rot(lx: float, ly: float) -> tuple[float, float]:
        return (lx * ca + ly * sa, -lx * sa + ly * ca)

    def N(pt: tuple[float, float]) -> list[float]:
        return [theme.nx(pt[0], width), theme.ny(pt[1], height)]

    # Local chevron ">" (pointing +x): upper arm end, tip, lower arm end.
    local = [(-half, -half), (half, 0.0), (-half, half)]
    # Unit pointing direction (world) for stacking offsets.
    ux, uy = rot(1.0, 0.0)
    base = -(n - 1) / 2.0  # centre the stack on (x, y)

    ops: list[dict[str, Any]] = []
    ids: list[str] = []
    for i in range(n):
        off = (base + i) * float(gap)
        cx, cy = x + off * ux, y + off * uy
        pts = []
        for lx, ly in local:
            wx, wy = rot(lx, ly)
            pts.append(N((cx + wx, cy + wy)))
        cid = f"{prefix}_{i}"
        ids.append(cid)
        ops.append(
            {"op": "add_shape", "id": cid, "name": f"Chevron {i + 1}", "kind": "line",
             "points": pts, "stroke": {"color": col, "width": float(width_px)},
             "at_time": start, "duration": duration}
        )

    if animate:
        for cid in ids:
            ops += _fade_in(cid, start, 0.4, clamp=duration)

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
