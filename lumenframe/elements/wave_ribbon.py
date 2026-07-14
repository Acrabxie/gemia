"""``wave_ribbon`` element — a horizontal sine-wave ribbon (the Lumeri motif).

An *element* is a pure ``(**params) -> [op dicts]`` overlay macro (see
:mod:`lumenframe.elements`): it draws one graphic and composes onto whatever is
beneath it. This one samples a sine curve into a polyline and stamps it as a
single ``add_shape`` line — the soft ice-blue "light-ribbon" that runs through
the Lumeri brand. It spans ``span`` px wide, waves ``amplitude`` px up/down over
``periods`` full cycles, centred on ``(x, y)`` px-from-centre. Stroke colour and
``thickness`` are params so it restyles in place. No background — overlay only.
"""
from __future__ import annotations

import math
from typing import Any

from lumenframe.templates import theme


def wave_ribbon(
    *,
    x: float = 0.0,
    y: float = 0.0,
    span: float = 1200.0,
    amplitude: float = 90.0,
    periods: float = 2.0,
    thickness: float = 8.0,
    color: str | None = None,
    start: float = 0.0,
    duration: float = 3.0,
    prefix: str = "wave_ribbon",
    animate: bool = True,
    width: int = theme.DEFAULT_W,
    height: int = theme.DEFAULT_H,
    samples: int = 96,
) -> list[dict[str, Any]]:
    """Draw a horizontal sine-wave ribbon (a sampled ``add_shape`` polyline).

    Args:
        x, y: ribbon centre, px from canvas centre (x right+, y down+).
        span: total horizontal width of the ribbon in px.
        amplitude: peak vertical excursion of the wave in px (crest→centre).
        periods: number of full sine cycles across ``span``.
        thickness: stroke width of the ribbon in px.
        color: ribbon colour; ``None`` == the brand ice-blue accent.
        start, duration: timeline placement of the layer (seconds).
        prefix: id prefix so the same element can be stamped twice without clash.
        animate: when True, the ribbon fades in (opacity 0→1 over ~0.4s).
        width, height: canvas size the px→normalised bridge tracks.
        samples: how many points to sample the sine into (smoothness).

    Returns:
        A list of op dicts (one ``add_shape`` line plus its fade-in keyframes) —
        no background, overlay only.
    """
    col = color or theme.PALETTES["lumeri"]["accent"]
    start = float(start)
    duration = float(duration)
    span = float(span)
    amp = float(amplitude)
    n = max(2, int(samples))

    half = span / 2.0
    pts: list[list[float]] = []
    for i in range(n):
        frac = i / (n - 1)  # 0..1 across the span
        px = x - half + frac * span
        py = y - amp * math.sin(frac * float(periods) * 2.0 * math.pi)
        pts.append([theme.nx(px, width), theme.ny(py, height)])

    ribbon_id = f"{prefix}_ribbon"
    ops: list[dict[str, Any]] = [
        {"op": "add_shape", "id": ribbon_id, "name": "Wave ribbon", "kind": "line",
         "points": pts,
         "stroke": {"color": col, "width": float(thickness)},
         "at_time": start, "duration": duration},
    ]

    if animate:
        ops += _fade_in(ribbon_id, start, 0.4, clamp=duration)

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
