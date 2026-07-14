"""``bullet_list`` template — a headed list whose items build in one by one.

The workhorse of explainer / office video: an accent heading, then a column of
bulleted lines that rise + fade in on a stagger so the list "builds" the way a
presenter reveals points. Each line carries its own bullet glyph, so the bullet
always hugs its text and the whole group centres cleanly on any canvas — pure
``add_gradient`` / ``text`` + ``set_keyframe``, real pixels, no assets.
"""
from __future__ import annotations

from typing import Any

from lumenframe.templates import theme

#: bullet glyph prefixed to each line (travels with the text as one block).
_BULLET = "•  "


def bullet_list(
    heading: str = "Agenda",
    items: list[str] | None = None,
    *,
    start: float = 0.0,
    duration: float = 6.0,
    stagger: float = 0.5,
    bullet: bool = True,
    palette: str | dict[str, Any] | None = None,
    width: int = theme.DEFAULT_W,
    height: int = theme.DEFAULT_H,
    prefix: str = "bullets",
    animate: bool = True,
) -> list[dict[str, Any]]:
    """Build the op list for a heading + staggered bullet list.

    Args:
        heading: the list title (accent-coloured, above the list).
        items: the line strings (defaults to three placeholders).
        start / duration: timeline placement (seconds).
        stagger: seconds between successive line reveals.
        bullet: when True each line is prefixed with a ``•`` glyph.
        palette: palette name or ``{role: hex}`` override; ``None`` == brand.
        width / height: canvas size the layout adapts to.
        prefix: id prefix so two lists never collide.
        animate: when True lines rise + fade in on the stagger.

    Returns:
        A list of op dicts: gradient bg, the heading, and one text line per item
        (with their staggered rise-in keyframes when animated).
    """
    p = theme.resolve_palette(palette)
    rows = list(items) if items else ["First point", "Second point", "Third point"]
    start = float(start)
    duration = float(duration)

    body_px = theme.type_size("body", height)
    step = theme.line_step("body", height, lead=1.9)
    # Centre the block of lines a touch below centre; hang the heading above it.
    group_cy = 0.06 * height
    first_y = group_cy - (len(rows) - 1) * step / 2.0
    head_y = first_y - 0.17 * height
    fade = 0.45
    travel = 0.045 * height

    ops: list[dict[str, Any]] = [
        {"op": "add_gradient", "id": f"{prefix}_bg", "name": "List BG",
         "mode": "linear", "stops": p["grad"], "angle": 90,
         "at_time": start, "duration": duration},
    ]

    hid = f"{prefix}_head"
    ops += [
        {"op": "add_layer", "type": "text", "id": hid, "name": "Heading",
         "at_time": start, "duration": duration, "text": heading,
         "color": p["accent"], "font_size": theme.type_size("heading", height),
         "align": "center"},
        {"op": "set_transform", "layer_id": hid, "y": head_y},
    ]
    if animate:
        ops += _fade_kfs(hid, start, fade)

    for i, item in enumerate(rows):
        row_y = first_y + i * step
        t0 = start + (0.2 + i * stagger if animate else 0.0)
        tid = f"{prefix}_item{i}"
        line = f"{_BULLET}{item}" if bullet else str(item)
        ops += [
            {"op": "add_layer", "type": "text", "id": tid, "name": f"Item {i+1}",
             "at_time": start, "duration": duration, "text": line,
             "color": p["text"], "font_size": body_px, "align": "center"},
            {"op": "set_transform", "layer_id": tid, "y": row_y},
        ]
        if animate:
            ops += _rise_kfs(tid, row_y, t0, fade, travel=travel)

    return ops


def _fade_kfs(layer_id: str, t0: float, fade: float) -> list[dict[str, Any]]:
    """Opacity 0 → 1 over ``[t0, t0+fade]``."""
    return [
        {"op": "set_keyframe", "layer_id": layer_id, "property": "opacity", "t": round(t0, 4), "value": 0.0, "interp": "linear"},
        {"op": "set_keyframe", "layer_id": layer_id, "property": "opacity", "t": round(t0 + fade, 4), "value": 1.0, "interp": "ease_out"},
    ]


def _rise_kfs(layer_id: str, base_y: float, t0: float, fade: float, *, travel: float) -> list[dict[str, Any]]:
    """Fade + slide up: opacity 0 → 1 and transform.y (base+travel) → base."""
    return _fade_kfs(layer_id, t0, fade) + [
        {"op": "set_keyframe", "layer_id": layer_id, "property": "transform.y", "t": round(t0, 4), "value": round(base_y + travel, 3), "interp": "linear"},
        {"op": "set_keyframe", "layer_id": layer_id, "property": "transform.y", "t": round(t0 + fade, 4), "value": round(base_y, 3), "interp": "ease_out"},
    ]
