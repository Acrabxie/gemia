"""``intro`` template — a centred title card with an optional subtitle.

Expands to a full-canvas background ``solid`` plus a centred ``text`` title (and
an optional subtitle line). The title pops in (animate_text ``pop``) so the card
reads like a CapCut opener. Background is a ``solid`` (compiled directly to a
flat fill) so the card renders to real pixels with no asset/resolver needed.
"""
from __future__ import annotations

from typing import Any


def intro(
    title: str = "Title",
    *,
    subtitle: str | None = None,
    start: float = 0.0,
    duration: float = 3.0,
    background: str = "#0b0d10",
    title_color: str = "#ffffff",
    font_size: int = 96,
    prefix: str = "intro",
    animate: bool = True,
) -> list[dict[str, Any]]:
    """Build the op list for an intro title card.

    Args:
        title: the headline string.
        subtitle: optional second line under the title.
        start / duration: timeline placement (seconds).
        background: full-canvas solid fill (hex).
        title_color: title colour (hex).
        font_size: title point size.
        prefix: id prefix so two intros never collide.
        animate: when True the title pops in (animate_text pop).

    Returns:
        A list of op dicts: a full-canvas ``solid`` background, a centred
        ``text`` title, and (optionally) an ``animate_text`` pop on the title.
    """
    bg_id = f"{prefix}_bg"
    title_id = f"{prefix}_title"

    ops: list[dict[str, Any]] = [
        {
            "op": "add_layer",
            "type": "solid",
            "id": bg_id,
            "name": "Intro Background",
            "at_time": float(start),
            "duration": float(duration),
            "color": background,
        },
        {
            "op": "add_layer",
            "type": "text",
            "id": title_id,
            "name": "Intro Title",
            "at_time": float(start),
            "duration": float(duration),
            "text": title if not subtitle else f"{title}\n{subtitle}",
            "color": title_color,
            "font_size": int(font_size),
            "align": "center",
        },
    ]
    if animate:
        ops.append({
            "op": "animate_text",
            "layer_id": title_id,
            "preset": "pop",
            "duration": min(0.6, float(duration)),
        })
    return ops
