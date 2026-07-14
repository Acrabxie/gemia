"""``dot_grid`` element — a bounded rectangular grid of small dots.

An *element* is a pure ``(**params) -> [op dicts]`` overlay macro (see
:mod:`lumenframe.elements`): it draws one graphic and composes onto whatever is
beneath it. This one stamps a ``cols × rows`` lattice of tiny filled circles
(one ``add_shape`` ellipse per dot), evenly spaced by ``gap`` px and centred on
``(x, y)`` px-from-centre. It is a *bounded block* — the lattice only spans
``(cols-1)*gap × (rows-1)*gap`` px, it never fills the frame edge-to-edge, so a
corner of the canvas stays transparent and it reads as an overlay texture.
"""
from __future__ import annotations

from typing import Any

from lumenframe.templates import theme


def dot_grid(
    *,
    x: float = 0.0,
    y: float = 0.0,
    cols: int = 6,
    rows: int = 4,
    gap: float = 90.0,
    dot_radius: float = 10.0,
    color: str | None = None,
    start: float = 0.0,
    duration: float = 3.0,
    prefix: str = "dot_grid",
    animate: bool = True,
    width: int = theme.DEFAULT_W,
    height: int = theme.DEFAULT_H,
) -> list[dict[str, Any]]:
    """Draw a bounded ``cols × rows`` grid of small dots.

    Args:
        x, y: grid centre, px from canvas centre (x right+, y down+).
        cols: number of dot columns (>= 1).
        rows: number of dot rows (>= 1).
        gap: centre-to-centre spacing between adjacent dots in px.
        dot_radius: radius of each dot in px.
        color: dot fill colour; ``None`` == the brand ice-blue accent.
        start, duration: timeline placement of the layers (seconds).
        prefix: id prefix so the same element can be stamped twice without clash.
        animate: when True, each dot fades in (opacity 0->1 over ~0.4s).
        width, height: canvas size the px->normalised bridge tracks.

    Returns:
        A list of op dicts (one ``add_shape`` ellipse per dot plus their
        fade-in keyframes) — no background, overlay only.
    """
    col = color or theme.PALETTES["lumeri"]["accent"]
    start = float(start)
    duration = float(duration)
    cols = max(1, int(cols))
    rows = max(1, int(rows))
    gap = float(gap)
    r = max(0.5, float(dot_radius))

    # Normalised radii (ellipse rx/ry are in canvas fractions).
    rx = r / float(width)
    ry = r / float(height)

    # Offset so the block is centred on (x, y): span is (n-1)*gap.
    x0 = x - (cols - 1) * gap / 2.0
    y0 = y - (rows - 1) * gap / 2.0

    ops: list[dict[str, Any]] = []
    layer_ids: list[str] = []
    for j in range(rows):
        for i in range(cols):
            px = x0 + i * gap
            py = y0 + j * gap
            lid = f"{prefix}_{i}_{j}"
            layer_ids.append(lid)
            ops.append({
                "op": "add_shape", "id": lid, "name": f"Dot {i},{j}", "kind": "ellipse",
                "cx": theme.nx(px, width), "cy": theme.ny(py, height),
                "rx": rx, "ry": ry, "fill": col,
                "at_time": start, "duration": duration,
            })

    if animate:
        for lid in layer_ids:
            ops += _fade_in(lid, start, 0.4, clamp=duration)

    return ops


def _fade_in(layer_id: str, t0: float, fade: float, *, clamp: float | None = None) -> list[dict[str, Any]]:
    """Two opacity keyframes (0 -> 1) for a soft fade-in at absolute time ``t0``."""
    t1 = t0 + fade
    if clamp is not None:
        t1 = min(t1, t0 + max(0.05, clamp))
    return [
        {"op": "set_keyframe", "layer_id": layer_id, "property": "opacity", "t": round(t0, 4), "value": 0.0, "interp": "linear"},
        {"op": "set_keyframe", "layer_id": layer_id, "property": "opacity", "t": round(t1, 4), "value": 1.0, "interp": "ease_out"},
    ]
