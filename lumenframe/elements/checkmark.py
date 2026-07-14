"""``checkmark`` element — a check "✓" tick.

An *element* is a pure ``(**params) -> [op dicts]`` overlay macro (see
:mod:`lumenframe.elements`): it draws one graphic and composes onto whatever is
beneath it. This one stamps a tick as a single two-segment polyline (one
``add_shape`` line with three points: the short down-stroke into the elbow, then
the long up-stroke), centred on ``(x, y)`` px-from-centre — real pixels, no
background. ``size``, ``width_px`` and colour are params so it restyles in place.
"""
from __future__ import annotations

from typing import Any

from lumenframe.templates import theme


def checkmark(
    *,
    x: float = 0.0,
    y: float = 0.0,
    size: float = 160.0,
    width_px: float = 22.0,
    color: str | None = None,
    start: float = 0.0,
    duration: float = 3.0,
    prefix: str = "checkmark",
    animate: bool = True,
    width: int = theme.DEFAULT_W,
    height: int = theme.DEFAULT_H,
) -> list[dict[str, Any]]:
    """Draw a check tick (two-segment polyline: short stroke + long stroke).

    Args:
        x, y: tick centre, px from canvas centre (x right+, y down+).
        size: overall extent in px (roughly the tick's bounding-box side).
        width_px: stroke thickness in px (the task's ``width`` param — named
            ``width_px`` to avoid colliding with the shared canvas ``width``,
            exactly as the ``arrow`` exemplar does).
        color: stroke colour; ``None`` == the brand ice-blue accent.
        start, duration: timeline placement of the layer (seconds).
        prefix: id prefix so the same element can be stamped twice without clash.
        animate: when True, the layer fades in (opacity 0→1 over ~0.4s).
        width, height: canvas size the px→normalised bridge tracks.

    Returns:
        A list of op dicts (one ``add_shape`` line plus its fade-in keyframes)
        — no background, overlay only.
    """
    col = color or theme.PALETTES["lumeri"]["accent"]
    start = float(start)
    duration = float(duration)
    s = max(1.0, float(size))

    # Classic tick proportions inside an ``s``-wide box centred on (x, y):
    # start upper-left, drop to the elbow low-centre, rise to the tall right tip.
    p_start = (x - 0.45 * s, y - 0.02 * s)
    p_elbow = (x - 0.14 * s, y + 0.30 * s)
    p_tip = (x + 0.48 * s, y - 0.34 * s)

    points = [
        [theme.nx(p_start[0], width), theme.ny(p_start[1], height)],
        [theme.nx(p_elbow[0], width), theme.ny(p_elbow[1], height)],
        [theme.nx(p_tip[0], width), theme.ny(p_tip[1], height)],
    ]

    tick_id = f"{prefix}_tick"
    ops: list[dict[str, Any]] = [
        {"op": "add_shape", "id": tick_id, "name": "Checkmark", "kind": "line",
         "points": points, "stroke": {"color": col, "width": float(width_px), "cap": "round"},
         "at_time": start, "duration": duration},
    ]

    if animate:
        ops += _fade_in(tick_id, start, 0.4, clamp=duration)

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
