"""``pill`` element — a rounded-rect chip / badge, optionally with centred text.

An *element* is a pure ``(**params) -> [op dicts]`` overlay macro (see
:mod:`lumenframe.elements`): it draws one graphic and composes onto whatever is
beneath it. This one stamps a filled ``box_w`` × ``box_h`` rounded rectangle
(radius auto-defaults to a full "pill" = half the height) centred on ``(x, y)``
px-from-centre, and — when ``text`` is given — lays a centred text layer on top.
No background is painted; it composes as a badge over the frame.
"""
from __future__ import annotations

from typing import Any

from lumenframe.templates import theme


def pill(
    *,
    x: float = 0.0,
    y: float = 0.0,
    box_w: float = 360.0,
    box_h: float = 120.0,
    text: str = "",
    text_color: str | None = None,
    fill: str | None = None,
    color: str | None = None,
    radius: float | None = None,
    font_size: float | None = None,
    start: float = 0.0,
    duration: float = 3.0,
    prefix: str = "pill",
    animate: bool = True,
    width: int = theme.DEFAULT_W,
    height: int = theme.DEFAULT_H,
) -> list[dict[str, Any]]:
    """Draw a rounded-rect chip, optionally with centred text on it.

    Args:
        x, y: pill centre, px from canvas centre (x right+, y down+).
        box_w, box_h: chip width / height in px.
        text: optional label centred on the chip ("" == no text layer).
        text_color: label colour; ``None`` == the brand bg (dark ink on accent).
        fill: chip fill colour; falls back to ``color`` then brand ice-blue.
        color: alias for ``fill`` (the shared colour param).
        radius: corner radius in px; ``None`` == fully rounded (box_h / 2).
        font_size: label size in px; ``None`` == ~45% of box_h.
        start, duration: timeline placement of the layers (seconds).
        prefix: id prefix so the element can be stamped twice without clash.
        animate: when True, each layer fades in (opacity 0→1 over ~0.4s).
        width, height: canvas size the px→normalised bridge tracks.

    Returns:
        A list with one ``add_shape`` rect op (plus an optional centred text
        layer) and their fade-in keyframes — no background, overlay only.
    """
    pal = theme.PALETTES["lumeri"]
    chip_col = fill or color or pal["accent"]
    txt_col = text_color or pal["bg"]
    start = float(start)
    duration = float(duration)
    hw = max(1.0, float(box_w)) / 2.0
    hh = max(1.0, float(box_h)) / 2.0
    rad = (hh if radius is None else float(radius))

    x0 = theme.nx(x - hw, width)
    y0 = theme.ny(y - hh, height)
    x1 = theme.nx(x + hw, width)
    y1 = theme.ny(y + hh, height)

    chip_id = f"{prefix}_chip"
    ops: list[dict[str, Any]] = [
        {"op": "add_shape", "id": chip_id, "name": "Pill", "kind": "rect",
         "rect": [x0, y0, x1, y1], "fill": chip_col, "radius": float(rad),
         "at_time": start, "duration": duration},
    ]
    if animate:
        ops += _fade_in(chip_id, start, 0.4, clamp=duration)

    label = str(text)
    if label:
        fs = font_size if font_size is not None else max(8.0, float(box_h) * 0.45)
        txt_id = f"{prefix}_text"
        ops += [
            {"op": "add_layer", "type": "text", "id": txt_id, "name": "Pill label",
             "at_time": start, "duration": duration, "text": label,
             "color": txt_col, "font_size": float(fs), "align": "center"},
            {"op": "set_transform", "layer_id": txt_id, "x": float(x), "y": float(y)},
        ]
        if animate:
            ops += _fade_in(txt_id, start, 0.4, clamp=duration)

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
