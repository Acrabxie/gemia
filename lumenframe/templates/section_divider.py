"""``section_divider`` template — a chapter break: big index · label · rule.

Marks a new part of the video: an oversized index number (``01``) in the accent
colour, the section label beneath it, and a thin accent rule between them. Clean
and editorial; ``palette="noir"`` gives the stark magazine look.
"""
from __future__ import annotations

from typing import Any

from lumenframe.templates import theme


def section_divider(
    label: str = "Introduction",
    *,
    index: str | int | None = 1,
    start: float = 0.0,
    duration: float = 2.5,
    palette: str | dict[str, Any] | None = None,
    width: int = theme.DEFAULT_W,
    height: int = theme.DEFAULT_H,
    prefix: str = "divider",
    animate: bool = True,
) -> list[dict[str, Any]]:
    """Build the op list for a section-divider / chapter card.

    Args:
        label: the section name.
        index: chapter number; an int is zero-padded to two digits (``1`` →
            ``01``). Pass ``None`` to omit the number entirely.
        start / duration: timeline placement (seconds).
        palette: palette name or ``{role: hex}`` override; ``None`` == brand.
        width / height: canvas size the layout adapts to.
        prefix: id prefix so two dividers never collide.
        animate: when True the index pops and the label fades up.

    Returns:
        A list of op dicts: gradient bg, optional index number, accent rule and
        the label.
    """
    p = theme.resolve_palette(palette)
    start = float(start)
    duration = float(duration)

    idx_y = -0.10 * height
    rule_y = 0.045 * height
    label_y = 0.135 * height

    ops: list[dict[str, Any]] = [
        {"op": "add_gradient", "id": f"{prefix}_bg", "name": "Divider BG",
         "mode": "linear", "stops": p["grad"], "angle": 90,
         "at_time": start, "duration": duration},
    ]

    if index is not None:
        num = f"{index:02d}" if isinstance(index, int) else str(index)
        nid = f"{prefix}_index"
        ops += [
            {"op": "add_layer", "type": "text", "id": nid, "name": "Index",
             "at_time": start, "duration": duration, "text": num,
             "color": p["accent"], "font_size": theme.type_size("display", height),
             "align": "center"},
            {"op": "set_transform", "layer_id": nid, "y": idx_y},
        ]
        if animate:
            ops.append({"op": "animate_text", "layer_id": nid, "preset": "pop",
                        "duration": min(0.6, duration)})

    # Thin centred accent rule.
    ops.append({"op": "add_shape", "id": f"{prefix}_rule", "name": "Rule", "kind": "rect",
                "fill": p["accent"], "radius": 3,
                "rect": [0.42, theme.ny(rule_y, height), 0.58, theme.ny(rule_y, height) + 0.006],
                "at_time": start, "duration": duration})

    lid = f"{prefix}_label"
    ops += [
        {"op": "add_layer", "type": "text", "id": lid, "name": "Label",
         "at_time": start, "duration": duration, "text": label,
         "color": p["text"], "font_size": theme.type_size("title", height),
         "align": "center"},
        {"op": "set_transform", "layer_id": lid, "y": label_y},
    ]
    if animate:
        t0 = start + 0.15
        ops += [
            {"op": "set_keyframe", "layer_id": lid, "property": "opacity", "t": round(t0, 4), "value": 0.0, "interp": "linear"},
            {"op": "set_keyframe", "layer_id": lid, "property": "opacity", "t": round(t0 + 0.45, 4), "value": 1.0, "interp": "ease_out"},
        ]

    return ops
