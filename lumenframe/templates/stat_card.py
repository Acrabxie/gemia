"""``stat_card`` template — a metric highlight: big number · label · caption.

Puts a single figure on screen at hero size (the accent-coloured ``value``),
with a label under it and an optional caption for context. The number pops in so
it lands with weight — the "3.2× faster" / "10M users" moment.
"""
from __future__ import annotations

from typing import Any

from lumenframe.templates import theme


def stat_card(
    value: str = "100%",
    label: str = "Metric",
    *,
    caption: str | None = None,
    start: float = 0.0,
    duration: float = 3.0,
    palette: str | dict[str, Any] | None = None,
    width: int = theme.DEFAULT_W,
    height: int = theme.DEFAULT_H,
    prefix: str = "stat",
    animate: bool = True,
) -> list[dict[str, Any]]:
    """Build the op list for a big-number stat card.

    Args:
        value: the hero figure (kept short — ``"3.2×"``, ``"$1.4B"``, ``"98%"``).
        label: the metric name under the number.
        caption: optional context line under the label.
        start / duration: timeline placement (seconds).
        palette: palette name or ``{role: hex}`` override; ``None`` == brand.
        width / height: canvas size the layout adapts to.
        prefix: id prefix so two stats never collide.
        animate: when True the value pops and the rest fades up.

    Returns:
        A list of op dicts: gradient bg, hero value, label and optional caption.
    """
    p = theme.resolve_palette(palette)
    start = float(start)
    duration = float(duration)

    value_y = -0.05 * height
    label_y = 0.11 * height
    caption_y = 0.185 * height

    ops: list[dict[str, Any]] = [
        {"op": "add_gradient", "id": f"{prefix}_bg", "name": "Stat BG",
         "mode": "linear", "stops": p["grad"], "angle": 90,
         "at_time": start, "duration": duration},
    ]

    vid = f"{prefix}_value"
    ops += [
        {"op": "add_layer", "type": "text", "id": vid, "name": "Value",
         "at_time": start, "duration": duration, "text": str(value),
         "color": p["accent"], "font_size": theme.type_size("display", height),
         "align": "center"},
        {"op": "set_transform", "layer_id": vid, "y": value_y},
    ]
    if animate:
        ops.append({"op": "animate_text", "layer_id": vid, "preset": "pop",
                    "duration": min(0.6, duration)})

    lid = f"{prefix}_label"
    ops += [
        {"op": "add_layer", "type": "text", "id": lid, "name": "Label",
         "at_time": start, "duration": duration, "text": label,
         "color": p["text"], "font_size": theme.type_size("subhead", height),
         "align": "center"},
        {"op": "set_transform", "layer_id": lid, "y": label_y},
    ]
    if animate:
        ops += _fade(lid, start + 0.2, 0.45)

    if caption:
        cid = f"{prefix}_caption"
        ops += [
            {"op": "add_layer", "type": "text", "id": cid, "name": "Caption",
             "at_time": start, "duration": duration, "text": caption,
             "color": p["subtext"], "font_size": theme.type_size("caption", height),
             "align": "center"},
            {"op": "set_transform", "layer_id": cid, "y": caption_y},
        ]
        if animate:
            ops += _fade(cid, start + 0.32, 0.45)

    return ops


def _fade(layer_id: str, t0: float, fade: float) -> list[dict[str, Any]]:
    return [
        {"op": "set_keyframe", "layer_id": layer_id, "property": "opacity", "t": round(t0, 4), "value": 0.0, "interp": "linear"},
        {"op": "set_keyframe", "layer_id": layer_id, "property": "opacity", "t": round(t0 + fade, 4), "value": 1.0, "interp": "ease_out"},
    ]
