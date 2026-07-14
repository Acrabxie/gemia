"""``cross`` element — a plus "+" or an "x" cross.

An *element* is a pure ``(**params) -> [op dicts]`` overlay macro (see
:mod:`lumenframe.elements`): it draws one graphic and composes onto whatever is
beneath it. This one stamps two crossed line segments (two ``add_shape`` lines):
``kind="plus"`` draws an upright "+" (a horizontal and a vertical bar), while
``kind="x"`` draws a diagonal "✗" (two diagonals). Both are centred on
``(x, y)`` px-from-centre — real pixels, no background. ``size``, ``width_px``,
``kind`` and colour are params so it restyles in place.
"""
from __future__ import annotations

from typing import Any

from lumenframe.templates import theme


def cross(
    *,
    x: float = 0.0,
    y: float = 0.0,
    size: float = 160.0,
    width_px: float = 22.0,
    kind: str = "plus",
    color: str | None = None,
    start: float = 0.0,
    duration: float = 3.0,
    prefix: str = "cross",
    animate: bool = True,
    width: int = theme.DEFAULT_W,
    height: int = theme.DEFAULT_H,
) -> list[dict[str, Any]]:
    """Draw a "+" (``kind="plus"``) or "✗" (``kind="x"``) from two crossed lines.

    Args:
        x, y: cross centre, px from canvas centre (x right+, y down+).
        size: full arm-span in px (tip-to-tip of each of the two bars).
        width_px: stroke thickness in px (the task's ``width`` param — named
            ``width_px`` to avoid colliding with the shared canvas ``width``,
            exactly as the ``arrow`` exemplar does).
        kind: ``"plus"`` for an upright + (horizontal + vertical bars) or
            ``"x"`` for a diagonal cross. Any other value falls back to plus.
        color: stroke colour; ``None`` == the brand ice-blue accent.
        start, duration: timeline placement of the layers (seconds).
        prefix: id prefix so the same element can be stamped twice without clash.
        animate: when True, each layer fades in (opacity 0→1 over ~0.4s).
        width, height: canvas size the px→normalised bridge tracks.

    Returns:
        A list of op dicts (two ``add_shape`` lines plus their fade-in
        keyframes) — no background, overlay only.
    """
    col = color or theme.PALETTES["lumeri"]["accent"]
    start = float(start)
    duration = float(duration)
    h = max(1.0, float(size)) / 2.0

    if str(kind).lower() == "x":
        # Two diagonals: top-left→bottom-right and top-right→bottom-left.
        seg_a = [(x - h, y - h), (x + h, y + h)]
        seg_b = [(x + h, y - h), (x - h, y + h)]
    else:
        # Upright plus: one horizontal bar, one vertical bar.
        seg_a = [(x - h, y), (x + h, y)]
        seg_b = [(x, y - h), (x, y + h)]

    def N(seg: list[tuple[float, float]]) -> list[list[float]]:
        return [[theme.nx(px, width), theme.ny(py, height)] for px, py in seg]

    id_a = f"{prefix}_seg_a"
    id_b = f"{prefix}_seg_b"
    stroke = {"color": col, "width": float(width_px), "cap": "round"}
    ops: list[dict[str, Any]] = [
        {"op": "add_shape", "id": id_a, "name": "Cross arm A", "kind": "line",
         "points": N(seg_a), "stroke": stroke,
         "at_time": start, "duration": duration},
        {"op": "add_shape", "id": id_b, "name": "Cross arm B", "kind": "line",
         "points": N(seg_b), "stroke": dict(stroke),
         "at_time": start, "duration": duration},
    ]

    if animate:
        for lid in (id_a, id_b):
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
