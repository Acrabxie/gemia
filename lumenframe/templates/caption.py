"""``caption`` template — a centred subtitle in the lower-third safe band.

The dialogue / narration subtitle: a line of text pinned to the lower-third
band, over an optional translucent surface strip that keeps it legible on busy
footage. Distinct from :func:`lower_third` (a left-aligned name/title lockup) —
this is the centred, per-line caption.
"""
from __future__ import annotations

from typing import Any

from lumenframe.templates import theme


def caption(
    text: str = "Caption",
    *,
    start: float = 0.0,
    duration: float = 3.0,
    band: bool = True,
    palette: str | dict[str, Any] | None = None,
    width: int = theme.DEFAULT_W,
    height: int = theme.DEFAULT_H,
    prefix: str = "caption",
    animate: bool = True,
) -> list[dict[str, Any]]:
    """Build the op list for a lower-third subtitle line.

    Args:
        text: the caption string (one line; pre-wrap with ``\\n`` if needed).
        start / duration: timeline placement (seconds).
        band: when True, draw a translucent full-width strip behind the text for
            legibility; set False for clean text-only captions.
        palette: palette name or ``{role: hex}`` override; ``None`` == brand.
        width / height: canvas size the layout adapts to.
        prefix: id prefix so two captions never collide.
        animate: when True the caption fades in quickly.

    Returns:
        A list of op dicts: an optional surface band and the centred caption text.
        (No full-canvas background — a caption overlays whatever is beneath it.)
    """
    p = theme.resolve_palette(palette)
    start = float(start)
    duration = float(duration)

    band_cy = theme.ny(theme.lower_third_y(height), height)
    band_half = 0.075

    ops: list[dict[str, Any]] = []

    if band:
        bid = f"{prefix}_band"
        ops += [
            {"op": "add_shape", "id": bid, "name": "Caption Band", "kind": "rect",
             "fill": p["bg"],
             "rect": [0.0, band_cy - band_half, 1.0, band_cy + band_half],
             "at_time": start, "duration": duration},
            {"op": "set_opacity", "layer_id": bid, "opacity": 0.55},
        ]

    tid = f"{prefix}_text"
    ops += [
        {"op": "add_layer", "type": "text", "id": tid, "name": "Caption",
         "at_time": start, "duration": duration, "text": text,
         "color": p["text"], "font_size": theme.type_size("subhead", height),
         "align": "center"},
        {"op": "set_transform", "layer_id": tid, "y": theme.lower_third_y(height)},
    ]
    if animate:
        ops += [
            {"op": "set_keyframe", "layer_id": tid, "property": "opacity", "t": round(start, 4), "value": 0.0, "interp": "linear"},
            {"op": "set_keyframe", "layer_id": tid, "property": "opacity", "t": round(start + 0.3, 4), "value": 1.0, "interp": "ease_out"},
        ]

    return ops
