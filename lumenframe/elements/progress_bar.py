"""``progress_bar`` element — a rounded track with a filled bar to ``progress``.

An *element* is a pure ``(**params) -> [op dicts]`` overlay macro (see
:mod:`lumenframe.elements`): it draws one graphic and composes onto whatever is
beneath it. This one stamps two rounded ``add_shape`` rects — a muted full-width
*track* and, on top, an accent *fill* that runs from the left edge to
``progress`` (0..1) of the track's length — a horizontal progress / loading bar
centred on ``(x, y)`` px-from-centre. Fill colour, ``track_color`` and geometry
are params so it restyles in place. No background — overlay only.
"""
from __future__ import annotations

from typing import Any

from lumenframe.templates import theme


def progress_bar(
    *,
    x: float = 0.0,
    y: float = 0.0,
    length: float = 900.0,
    thickness: float = 36.0,
    progress: float = 0.6,
    track_color: str | None = None,
    color: str | None = None,
    start: float = 0.0,
    duration: float = 3.0,
    prefix: str = "progress_bar",
    animate: bool = True,
    width: int = theme.DEFAULT_W,
    height: int = theme.DEFAULT_H,
) -> list[dict[str, Any]]:
    """Draw a rounded progress bar (muted track rect + accent fill rect).

    Args:
        x, y: bar centre, px from canvas centre (x right+, y down+).
        length: total width of the track in px.
        thickness: height of the bar in px (its rounded-corner radius is half).
        progress: fraction filled, clamped to ``[0, 1]``.
        track_color: colour of the empty track; ``None`` == the palette
            ``surface`` (a muted raised-panel tone).
        color: fill colour; ``None`` == the brand ice-blue accent.
        start, duration: timeline placement of the layers (seconds).
        prefix: id prefix so the same element can be stamped twice without clash.
        animate: when True, both layers fade in (opacity 0→1 over ~0.4s).
        width, height: canvas size the px→normalised bridge tracks.

    Returns:
        A list of op dicts (two ``add_shape`` rects plus their fade-in
        keyframes) — no background, overlay only.
    """
    pal = theme.PALETTES["lumeri"]
    fill_col = color or pal["accent"]
    track_col = track_color or pal["surface"]
    start = float(start)
    duration = float(duration)
    length = float(length)
    thick = float(thickness)
    frac = max(0.0, min(1.0, float(progress)))

    half_len = length / 2.0
    half_thick = thick / 2.0
    radius = half_thick

    # Track spans the full length; fill starts at the left edge and runs to frac.
    x_left = x - half_len
    y_top = y - half_thick
    y_bot = y + half_thick

    track_rect = [
        theme.nx(x_left, width), theme.ny(y_top, height),
        theme.nx(x + half_len, width), theme.ny(y_bot, height),
    ]
    fill_right = x_left + frac * length
    fill_rect = [
        theme.nx(x_left, width), theme.ny(y_top, height),
        theme.nx(fill_right, width), theme.ny(y_bot, height),
    ]

    track_id = f"{prefix}_track"
    fill_id = f"{prefix}_fill"
    ops: list[dict[str, Any]] = [
        {"op": "add_shape", "id": track_id, "name": "Progress track", "kind": "rect",
         "rect": track_rect, "fill": track_col, "radius": float(radius),
         "at_time": start, "duration": duration},
    ]
    # Only emit the fill when there is something to fill (progress > 0).
    if frac > 0.0:
        ops.append(
            {"op": "add_shape", "id": fill_id, "name": "Progress fill", "kind": "rect",
             "rect": fill_rect, "fill": fill_col, "radius": float(radius),
             "at_time": start, "duration": duration}
        )

    if animate:
        ops += _fade_in(track_id, start, 0.4, clamp=duration)
        if frac > 0.0:
            ops += _fade_in(fill_id, start, 0.4, clamp=duration)

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
