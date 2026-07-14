"""``underline`` element — an emphasis underline stroke under a text region.

An *element* is a pure ``(**params) -> [op dicts]`` overlay macro (see
:mod:`lumenframe.elements`): it draws one graphic and composes onto whatever is
beneath it. This one stamps a single thin rounded bar — an ``add_shape`` rect
with a pill ``radius`` — of a given ``length`` and ``thickness``, centred on
``(x, y)`` px-from-centre, meant to sit just under a word or headline to
underscore it. Real pixels, no background — colour, ``length`` and ``thickness``
are params so it restyles in place. When ``animate`` it fades in softly (a true
left→right wipe would need an edge-anchored scale the renderer's centre-anchored
transform can't express, so a fade is used per the library's motion rule).
"""
from __future__ import annotations

from typing import Any

from lumenframe.templates import theme


def underline(
    *,
    x: float = 0.0,
    y: float = 0.0,
    length: float = 480.0,
    thickness: float = 16.0,
    color: str | None = None,
    start: float = 0.0,
    duration: float = 3.0,
    prefix: str = "underline",
    animate: bool = True,
    width: int = theme.DEFAULT_W,
    height: int = theme.DEFAULT_H,
) -> list[dict[str, Any]]:
    """Draw a rounded emphasis underline bar centred on ``(x, y)``.

    Args:
        x, y: centre of the underline bar, px from canvas centre (x right+,
            y down+). Place ``y`` a little below the text baseline it underscores.
        length: bar length in px (the underlined span's width).
        thickness: bar height in px; also drives the pill ``radius`` so the
            ends are fully rounded.
        color: fill colour; ``None`` == the brand ice-blue accent.
        start, duration: timeline placement of the layer (seconds).
        prefix: id prefix so the same element can be stamped twice without clash.
        animate: when True, the bar fades in (opacity 0→1 over ~0.4s).
        width, height: canvas size the px→normalised bridge tracks.

    Returns:
        A list of op dicts (one ``add_shape`` rounded rect, plus its fade-in
        keyframes) — no background, overlay only.
    """
    col = color or theme.PALETTES["lumeri"]["accent"]
    start = float(start)
    duration = float(duration)
    half_len = float(length) / 2.0
    half_th = max(0.5, float(thickness) / 2.0)

    x0 = theme.nx(x - half_len, width)
    x1 = theme.nx(x + half_len, width)
    y0 = theme.ny(y - half_th, height)
    y1 = theme.ny(y + half_th, height)

    bar_id = f"{prefix}_bar"
    ops: list[dict[str, Any]] = [
        {"op": "add_shape", "id": bar_id, "name": "Underline", "kind": "rect",
         "rect": [x0, y0, x1, y1], "fill": col,
         "radius": float(half_th), "stroke": None,
         "at_time": start, "duration": duration},
    ]

    if animate:
        ops += _fade_in(bar_id, start, 0.4, clamp=duration)

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
