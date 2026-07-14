"""``circle`` element — a filled disc *or* a hollow ring (outline).

An *element* is a pure ``(**params) -> [op dicts]`` overlay macro (see
:mod:`lumenframe.elements`): it draws one graphic and composes onto whatever is
beneath it. This one stamps a circle of the given ``radius`` centred on
``(x, y)`` px-from-centre. In ``mode="disc"`` it is a solid filled ellipse; in
``mode="ring"`` the fill is dropped (``fill=None``) and only a ``stroke`` of the
given width is drawn, so the frame's corners stay transparent — it is an
overlay, never a background.
"""
from __future__ import annotations

from typing import Any

from lumenframe.templates import theme


def circle(
    *,
    x: float = 0.0,
    y: float = 0.0,
    radius: float = 200.0,
    mode: str = "disc",
    color: str | None = None,
    stroke: float = 12.0,
    start: float = 0.0,
    duration: float = 3.0,
    prefix: str = "circle",
    animate: bool = True,
    width: int = theme.DEFAULT_W,
    height: int = theme.DEFAULT_H,
) -> list[dict[str, Any]]:
    """Draw a filled disc or a hollow ring.

    Args:
        x, y: circle centre, px from canvas centre (x right+, y down+).
        radius: circle radius in px (mapped to normalised rx/ry per axis).
        mode: ``"disc"`` for a solid fill, ``"ring"`` for a stroked outline.
        color: fill (disc) / stroke (ring) colour; ``None`` == brand ice-blue.
        stroke: ring outline thickness in px (ignored for a disc).
        start, duration: timeline placement of the layer (seconds).
        prefix: id prefix so the element can be stamped twice without clash.
        animate: when True, the layer fades in (opacity 0→1 over ~0.4s).
        width, height: canvas size the px→normalised bridge tracks.

    Returns:
        A list containing one ``add_shape`` ellipse op (plus its fade-in
        keyframes) — no background, overlay only.
    """
    col = color or theme.PALETTES["lumeri"]["accent"]
    start = float(start)
    duration = float(duration)
    r = max(1.0, float(radius))
    ring = str(mode).lower() == "ring"

    cx = theme.nx(x, width)
    cy = theme.ny(y, height)
    rx = r / float(width)
    ry = r / float(height)

    circle_id = f"{prefix}_circle"
    shape: dict[str, Any] = {
        "op": "add_shape", "id": circle_id,
        "name": "Ring" if ring else "Disc", "kind": "ellipse",
        "cx": cx, "cy": cy, "rx": rx, "ry": ry,
        "at_time": start, "duration": duration,
    }
    if ring:
        shape["fill"] = None
        shape["stroke"] = {"color": col, "width": float(stroke)}
    else:
        shape["fill"] = col

    ops: list[dict[str, Any]] = [shape]
    if animate:
        ops += _fade_in(circle_id, start, 0.4, clamp=duration)
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
