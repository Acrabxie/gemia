"""``title_card`` template — a hero opener: kicker · title · rule · subtitle.

A richer sibling of :func:`intro`. Expands to a gradient background, an optional
eyebrow *kicker*, a big centred title (pops in), a short accent rule, and an
optional subtitle. Everything renders to real pixels from ``add_gradient`` /
``text`` / ``add_shape`` — no asset or resolver hand-off needed — and the whole
card is restyled by naming one ``palette``.
"""
from __future__ import annotations

from typing import Any

from lumenframe.templates import theme


def title_card(
    title: str = "Title",
    *,
    kicker: str | None = None,
    subtitle: str | None = None,
    start: float = 0.0,
    duration: float = 3.5,
    palette: str | dict[str, Any] | None = None,
    width: int = theme.DEFAULT_W,
    height: int = theme.DEFAULT_H,
    prefix: str = "title_card",
    animate: bool = True,
) -> list[dict[str, Any]]:
    """Build the op list for a hero title card.

    Args:
        title: the headline.
        kicker: optional eyebrow line above the title (accent colour).
        subtitle: optional line below the accent rule.
        start / duration: timeline placement (seconds).
        palette: palette name (``lumeri``/``ink``/``paper``/``noir``/``sunset``)
            or a partial ``{role: hex}`` override; ``None`` == the brand palette.
        width / height: canvas size the layout adapts to.
        prefix: id prefix so two cards never collide.
        animate: when True the title pops in and the rest fades up.

    Returns:
        A list of op dicts: gradient bg, optional kicker, title, accent rule and
        optional subtitle, plus their intro animations.
    """
    p = theme.resolve_palette(palette)
    start = float(start)
    duration = float(duration)

    kicker_y = -0.14 * height
    title_y = -0.02 * height
    rule_y = 0.075 * height
    sub_y = 0.15 * height

    ops: list[dict[str, Any]] = [
        {"op": "add_gradient", "id": f"{prefix}_bg", "name": "Title BG",
         "mode": "linear", "stops": p["grad"], "angle": 90,
         "at_time": start, "duration": duration},
    ]

    if kicker:
        kid = f"{prefix}_kicker"
        ops += [
            {"op": "add_layer", "type": "text", "id": kid, "name": "Kicker",
             "at_time": start, "duration": duration, "text": kicker,
             "color": p["accent"], "font_size": theme.type_size("kicker", height),
             "align": "center"},
            {"op": "set_transform", "layer_id": kid, "y": kicker_y},
        ]
        if animate:
            ops += _fade_in(kid, start, 0.5)

    tid = f"{prefix}_title"
    ops += [
        {"op": "add_layer", "type": "text", "id": tid, "name": "Title",
         "at_time": start, "duration": duration, "text": title,
         "color": p["text"], "font_size": theme.type_size("title", height),
         "align": "center"},
        {"op": "set_transform", "layer_id": tid, "y": title_y},
    ]
    if animate:
        ops.append({"op": "animate_text", "layer_id": tid, "preset": "pop",
                    "duration": min(0.6, duration)})

    # Short accent rule, centred under the title.
    ops.append({"op": "add_shape", "id": f"{prefix}_rule", "name": "Rule", "kind": "rect",
                "fill": p["accent"], "radius": 4,
                "rect": [0.44, theme.ny(rule_y, height), 0.56, theme.ny(rule_y, height) + 0.007],
                "at_time": start, "duration": duration})

    if subtitle:
        sid = f"{prefix}_sub"
        ops += [
            {"op": "add_layer", "type": "text", "id": sid, "name": "Subtitle",
             "at_time": start, "duration": duration, "text": subtitle,
             "color": p["subtext"], "font_size": theme.type_size("subhead", height),
             "align": "center"},
            {"op": "set_transform", "layer_id": sid, "y": sub_y},
        ]
        if animate:
            ops += _fade_in(sid, start + 0.15, 0.5, clamp=duration)

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
