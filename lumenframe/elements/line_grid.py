"""``line_grid`` element — a bounded grid of thin horizontal + vertical rules.

An *element* is a pure ``(**params) -> [op dicts]`` overlay macro (see
:mod:`lumenframe.elements`): it draws one graphic and composes onto whatever is
beneath it. This one stamps a bounded ``box_w × box_h`` rectangle divided into a
``cols × rows`` grid by thin rules (``add_shape`` line each): ``cols+1`` vertical
rules and ``rows+1`` horizontal rules, centred on ``(x, y)`` px-from-centre. It
is a *bounded block* — the rules only span the box, never the whole frame, so a
corner of the canvas stays transparent and it reads as an overlay grid.

Note: the task names this element's stroke param ``width`` (a *content* param),
which shadows the shared canvas-``width`` param. Canvas width is therefore
tracked here through ``canvas_width`` (default :data:`theme.DEFAULT_W`); canvas
height stays the shared ``height``.
"""
from __future__ import annotations

from typing import Any

from lumenframe.templates import theme


def line_grid(
    *,
    x: float = 0.0,
    y: float = 0.0,
    cols: int = 6,
    rows: int = 4,
    box_w: float = 720.0,
    box_h: float = 480.0,
    width: float = 3.0,
    color: str | None = None,
    start: float = 0.0,
    duration: float = 3.0,
    prefix: str = "line_grid",
    animate: bool = True,
    canvas_width: int = theme.DEFAULT_W,
    height: int = theme.DEFAULT_H,
) -> list[dict[str, Any]]:
    """Draw a bounded ``cols × rows`` grid of thin rules inside a ``box_w × box_h`` box.

    Args:
        x, y: grid centre, px from canvas centre (x right+, y down+).
        cols: number of grid columns (draws ``cols+1`` vertical rules).
        rows: number of grid rows (draws ``rows+1`` horizontal rules).
        box_w, box_h: size of the bounded grid box in px.
        width: stroke thickness of each rule in px.
        color: rule colour; ``None`` == the brand ice-blue accent.
        start, duration: timeline placement of the layers (seconds).
        prefix: id prefix so the same element can be stamped twice without clash.
        animate: when True, each rule fades in (opacity 0->1 over ~0.4s).
        canvas_width, height: canvas size the px->normalised bridge tracks.

    Returns:
        A list of op dicts (one ``add_shape`` line per rule plus their fade-in
        keyframes) — no background, overlay only.
    """
    col = color or theme.PALETTES["lumeri"]["accent"]
    start = float(start)
    duration = float(duration)
    cols = max(1, int(cols))
    rows = max(1, int(rows))
    bw = float(box_w)
    bh = float(box_h)
    stroke = max(0.5, float(width))
    cw = int(canvas_width)

    left = x - bw / 2.0
    right = x + bw / 2.0
    top = y - bh / 2.0
    bottom = y + bh / 2.0

    def N(px: float, py: float) -> list[float]:
        return [theme.nx(px, cw), theme.ny(py, height)]

    ops: list[dict[str, Any]] = []
    layer_ids: list[str] = []

    # Vertical rules (cols+1 of them).
    for i in range(cols + 1):
        px = left + (right - left) * (i / cols)
        lid = f"{prefix}_v{i}"
        layer_ids.append(lid)
        ops.append({
            "op": "add_shape", "id": lid, "name": f"V rule {i}", "kind": "line",
            "points": [N(px, top), N(px, bottom)],
            "stroke": {"color": col, "width": stroke},
            "at_time": start, "duration": duration,
        })

    # Horizontal rules (rows+1 of them).
    for j in range(rows + 1):
        py = top + (bottom - top) * (j / rows)
        lid = f"{prefix}_h{j}"
        layer_ids.append(lid)
        ops.append({
            "op": "add_shape", "id": lid, "name": f"H rule {j}", "kind": "line",
            "points": [N(left, py), N(right, py)],
            "stroke": {"color": col, "width": stroke},
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
