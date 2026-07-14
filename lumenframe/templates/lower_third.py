"""``lower_third`` template — a captioned bar pinned to the lower third.

Expands to two layers: a flat background bar (a ``shape`` layer carrying a
``color`` fill in its props, sitting low on the canvas) and a ``text`` layer on
top of it. The pair is the bread-and-butter broadcast caption ("Name / Title").

Returned ops use only ``add_layer`` + ``set_transform`` + ``animate_text`` (the
text rise-in), so the whole template rides the normal dispatch.
"""
from __future__ import annotations

from typing import Any


def lower_third(
    text: str = "Lower Third",
    *,
    subtitle: str | None = None,
    start: float = 0.0,
    duration: float = 4.0,
    color: str = "#101418",
    text_color: str = "#ffffff",
    font_size: int = 56,
    prefix: str = "lt",
    animate: bool = True,
) -> list[dict[str, Any]]:
    """Build the op list for a lower-third caption.

    Args:
        text: the headline caption string.
        subtitle: optional second line; appended under ``text`` with a newline.
        start / duration: timeline placement (seconds).
        color: background bar fill (hex).
        text_color: caption colour (hex).
        font_size: caption point size.
        prefix: id prefix so two lower-thirds never collide.
        animate: when True, the text layer rises + fades in (animate_text rise).

    Returns:
        A list of op dicts: a ``shape`` bar, a ``text`` caption, and (optionally)
        an ``animate_text`` rise on the caption.
    """
    bar_id = f"{prefix}_bar"
    text_id = f"{prefix}_text"
    caption = text if not subtitle else f"{text}\n{subtitle}"

    ops: list[dict[str, Any]] = [
        # Background bar: a shape layer with a flat colour fill, low on canvas.
        {
            "op": "add_layer",
            "type": "shape",
            "id": bar_id,
            "name": "Lower Third Bar",
            "at_time": float(start),
            "duration": float(duration),
            "shape": "rectangle",
            "color": color,
            "width": 1200,
            "height": 200,
        },
        # Pin the bar to the lower third of the canvas (y > 0 == below centre).
        {"op": "set_transform", "layer_id": bar_id, "y": 300.0},
        # Caption text on top of the bar.
        {
            "op": "add_layer",
            "type": "text",
            "id": text_id,
            "name": "Lower Third Text",
            "at_time": float(start),
            "duration": float(duration),
            "text": caption,
            "color": text_color,
            "font_size": int(font_size),
            "align": "left",
        },
        {"op": "set_transform", "layer_id": text_id, "y": 300.0, "x": -300.0},
    ]
    if animate:
        ops.append({
            "op": "animate_text",
            "layer_id": text_id,
            "preset": "rise",
            "duration": min(0.6, float(duration)),
        })
    return ops
