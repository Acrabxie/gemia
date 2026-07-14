"""``quote_card`` template — a pull-quote: mark · quote · attribution.

A large centred quotation with an oversized accent quote-mark above it and an
optional "— Name, Role" attribution below. Rises in gently so it reads like a
statement. ``palette="noir"`` gives the stark editorial treatment.
"""
from __future__ import annotations

from typing import Any

from lumenframe.templates import theme


def quote_card(
    quote: str = "The best way to predict the future is to invent it.",
    *,
    author: str | None = None,
    role: str | None = None,
    start: float = 0.0,
    duration: float = 4.0,
    palette: str | dict[str, Any] | None = None,
    width: int = theme.DEFAULT_W,
    height: int = theme.DEFAULT_H,
    prefix: str = "quote",
    animate: bool = True,
) -> list[dict[str, Any]]:
    """Build the op list for a pull-quote card.

    Args:
        quote: the quotation body (wrap long quotes with ``\\n`` yourself).
        author: optional attribution name.
        role: optional role/title, appended after the name (``Name · Role``).
        start / duration: timeline placement (seconds).
        palette: palette name or ``{role: hex}`` override; ``None`` == brand.
        width / height: canvas size the layout adapts to.
        prefix: id prefix so two quotes never collide.
        animate: when True the quote rises in and the attribution fades up.

    Returns:
        A list of op dicts: gradient bg, a big accent quote-mark, the quote body
        and (optionally) the attribution line.
    """
    p = theme.resolve_palette(palette)
    start = float(start)
    duration = float(duration)

    mark_y = -0.20 * height
    quote_y = 0.0
    attr_y = 0.20 * height

    ops: list[dict[str, Any]] = [
        {"op": "add_gradient", "id": f"{prefix}_bg", "name": "Quote BG",
         "mode": "linear", "stops": p["grad"], "angle": 90,
         "at_time": start, "duration": duration},
        # Oversized decorative quote mark.
        {"op": "add_layer", "type": "text", "id": f"{prefix}_mark", "name": "Quote Mark",
         "at_time": start, "duration": duration, "text": "“",
         "color": p["accent"], "font_size": theme.type_size("display", height),
         "align": "center"},
        {"op": "set_transform", "layer_id": f"{prefix}_mark", "y": mark_y},
    ]

    qid = f"{prefix}_body"
    ops += [
        {"op": "add_layer", "type": "text", "id": qid, "name": "Quote",
         "at_time": start, "duration": duration, "text": quote,
         "color": p["text"], "font_size": theme.type_size("heading", height),
         "align": "center"},
        {"op": "set_transform", "layer_id": qid, "y": quote_y},
    ]
    if animate:
        ops.append({"op": "animate_text", "layer_id": qid, "preset": "rise",
                    "duration": min(0.6, duration)})

    if author:
        line = author if not role else f"{author} · {role}"
        aid = f"{prefix}_attr"
        ops += [
            {"op": "add_layer", "type": "text", "id": aid, "name": "Attribution",
             "at_time": start, "duration": duration, "text": f"— {line}",
             "color": p["subtext"], "font_size": theme.type_size("caption", height),
             "align": "center"},
            {"op": "set_transform", "layer_id": aid, "y": attr_y},
        ]
        if animate:
            t0 = start + 0.3
            ops += [
                {"op": "set_keyframe", "layer_id": aid, "property": "opacity", "t": round(t0, 4), "value": 0.0, "interp": "linear"},
                {"op": "set_keyframe", "layer_id": aid, "property": "opacity", "t": round(t0 + 0.45, 4), "value": 1.0, "interp": "ease_out"},
            ]

    return ops
