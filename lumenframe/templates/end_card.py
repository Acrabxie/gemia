"""``end_card`` template — the sign-off: title · CTA · handle.

Closes the video: a centred brand/closing title, an optional call-to-action line
under it, and an optional handle / URL at the bottom safe margin. Fades up as a
whole so it settles rather than pops.
"""
from __future__ import annotations

from typing import Any

from lumenframe.templates import theme


def end_card(
    title: str = "Thanks for watching",
    *,
    cta: str | None = None,
    handle: str | None = None,
    start: float = 0.0,
    duration: float = 3.0,
    palette: str | dict[str, Any] | None = None,
    width: int = theme.DEFAULT_W,
    height: int = theme.DEFAULT_H,
    prefix: str = "end_card",
    animate: bool = True,
) -> list[dict[str, Any]]:
    """Build the op list for an end / sign-off card.

    Args:
        title: the closing line (brand name, "Thanks for watching", …).
        cta: optional call-to-action under the title (accent colour).
        handle: optional @handle / URL pinned to the bottom safe margin.
        start / duration: timeline placement (seconds).
        palette: palette name or ``{role: hex}`` override; ``None`` == brand.
        width / height: canvas size the layout adapts to.
        prefix: id prefix so two end cards never collide.
        animate: when True the title pops and the rest fades up.

    Returns:
        A list of op dicts: gradient bg, title, optional CTA and optional handle.
    """
    p = theme.resolve_palette(palette)
    start = float(start)
    duration = float(duration)

    title_y = -0.03 * height
    cta_y = 0.10 * height

    ops: list[dict[str, Any]] = [
        {"op": "add_gradient", "id": f"{prefix}_bg", "name": "End BG",
         "mode": "linear", "stops": p["grad"], "angle": 90,
         "at_time": start, "duration": duration},
    ]

    tid = f"{prefix}_title"
    ops += [
        {"op": "add_layer", "type": "text", "id": tid, "name": "End Title",
         "at_time": start, "duration": duration, "text": title,
         "color": p["text"], "font_size": theme.type_size("title", height),
         "align": "center"},
        {"op": "set_transform", "layer_id": tid, "y": title_y},
    ]
    if animate:
        ops.append({"op": "animate_text", "layer_id": tid, "preset": "pop",
                    "duration": min(0.6, duration)})

    if cta:
        cid = f"{prefix}_cta"
        ops += [
            {"op": "add_layer", "type": "text", "id": cid, "name": "CTA",
             "at_time": start, "duration": duration, "text": cta,
             "color": p["accent"], "font_size": theme.type_size("subhead", height),
             "align": "center"},
            {"op": "set_transform", "layer_id": cid, "y": cta_y},
        ]
        if animate:
            ops += _fade(cid, start + 0.2, 0.45)

    if handle:
        hid = f"{prefix}_handle"
        ops += [
            {"op": "add_layer", "type": "text", "id": hid, "name": "Handle",
             "at_time": start, "duration": duration, "text": handle,
             "color": p["subtext"], "font_size": theme.type_size("caption", height),
             "align": "center"},
            {"op": "set_transform", "layer_id": hid, "y": theme.bottom_edge(height)},
        ]
        if animate:
            ops += _fade(hid, start + 0.32, 0.45)

    return ops


def _fade(layer_id: str, t0: float, fade: float) -> list[dict[str, Any]]:
    return [
        {"op": "set_keyframe", "layer_id": layer_id, "property": "opacity", "t": round(t0, 4), "value": 0.0, "interp": "linear"},
        {"op": "set_keyframe", "layer_id": layer_id, "property": "opacity", "t": round(t0 + fade, 4), "value": 1.0, "interp": "ease_out"},
    ]
