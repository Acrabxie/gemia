"""``callout`` template — an accent chip that pops on to emphasise one thing.

A rounded accent-filled pill with dark text, dropped over the current shot to
spotlight a term, number or label ("NEW", "-40%", "Live"). Pops in so the eye
snaps to it. Positioned at the top / centre / bottom band via ``position``.
"""
from __future__ import annotations

from typing import Any

from lumenframe.templates import theme

#: position keyword -> y (fraction of height, from centre).
_POSITIONS: dict[str, float] = {"top": -0.32, "center": 0.0, "bottom": 0.32}


def callout(
    text: str = "NEW",
    *,
    position: str = "top",
    start: float = 0.0,
    duration: float = 2.5,
    palette: str | dict[str, Any] | None = None,
    width: int = theme.DEFAULT_W,
    height: int = theme.DEFAULT_H,
    prefix: str = "callout",
    animate: bool = True,
) -> list[dict[str, Any]]:
    """Build the op list for an accent callout chip.

    Args:
        text: the (short) label — a word or number reads best.
        position: ``"top"`` (default), ``"center"`` or ``"bottom"`` band.
        start / duration: timeline placement (seconds).
        palette: palette name or ``{role: hex}`` override; ``None`` == brand.
        width / height: canvas size the layout adapts to.
        prefix: id prefix so two callouts never collide.
        animate: when True the chip fades in and the label pops.

    Returns:
        A list of op dicts: an accent pill (``add_shape``) and dark label text on
        top. No full-canvas background — the chip overlays the shot beneath it.
    """
    p = theme.resolve_palette(palette)
    start = float(start)
    duration = float(duration)

    y = _POSITIONS.get(position, _POSITIONS["top"]) * height
    cy = theme.ny(y, height)
    chip_half_h = 0.055
    chip_half_w = 0.13

    chip_id = f"{prefix}_chip"
    tid = f"{prefix}_text"
    ops: list[dict[str, Any]] = [
        {"op": "add_shape", "id": chip_id, "name": "Callout Chip", "kind": "rect",
         "fill": p["accent"], "radius": 48,
         "rect": [0.5 - chip_half_w, cy - chip_half_h, 0.5 + chip_half_w, cy + chip_half_h],
         "at_time": start, "duration": duration},
        {"op": "add_layer", "type": "text", "id": tid, "name": "Callout",
         "at_time": start, "duration": duration, "text": text,
         "color": p["bg"], "font_size": theme.type_size("subhead", height),
         "align": "center"},
        {"op": "set_transform", "layer_id": tid, "y": y},
    ]
    if animate:
        ops += [
            {"op": "set_keyframe", "layer_id": chip_id, "property": "opacity", "t": round(start, 4), "value": 0.0, "interp": "linear"},
            {"op": "set_keyframe", "layer_id": chip_id, "property": "opacity", "t": round(start + 0.25, 4), "value": 1.0, "interp": "ease_out"},
            {"op": "animate_text", "layer_id": tid, "preset": "pop", "duration": min(0.5, duration)},
        ]

    return ops
