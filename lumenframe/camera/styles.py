"""Camera archetypes — one word that re-tunes the whole move.

A style sets the axis *baseline*; the chosen move then reads those axes and
shapes itself accordingly. "cinematic" is the house default (a slow, motivated,
lightly-breathing push is the safest camera move there is). The archetypes span
the axis extremes so the agent can reach any feel with a single word:

    locked ──────────── cinematic ──────────── energetic
    (still, micro drift)  (slow, motivated)      (fast, punchy)

    handheld / documentary  (organic sway)      epic (big slow push)

Aliases resolve brand/vernacular words agents reach for (``doc`` → documentary,
``still`` → locked). An unknown style *raises* (silently restyling misleads),
unlike an unknown feeling which is merely ignored.
"""
from __future__ import annotations

from lumenframe.craft import StyleBook

from lumenframe.camera.params import SPACE


def camera_styles() -> StyleBook:
    """The camera :class:`StyleBook`, built fresh (no shared mutable state)."""
    book = StyleBook(space=SPACE, default="cinematic")
    book.add(
        "locked", "near-static tripod hold with only a micro drift of life",
        {"energy": 0.08, "smoothness": 0.85, "drama": 0.12, "drift": 0.1},
    )
    book.add(
        "cinematic", "a slow, motivated move that settles onto the subject",
        {"energy": 0.32, "smoothness": 0.72, "drama": 0.5, "drift": 0.16},
    )
    book.add(
        "energetic", "fast and punchy — quick to move, quick to land",
        {"energy": 0.82, "smoothness": 0.35, "drama": 0.55, "drift": 0.35},
    )
    book.add(
        "handheld", "an organic operator sway, always breathing with the frame",
        {"energy": 0.45, "smoothness": 0.42, "drama": 0.3, "drift": 0.82},
    )
    book.add(
        "documentary", "observational and human — steady but never dead still",
        {"energy": 0.3, "smoothness": 0.5, "drama": 0.28, "drift": 0.55},
    )
    book.add(
        "epic", "a big, slow, deliberate push with a wide sense of scale",
        {"energy": 0.22, "smoothness": 0.8, "drama": 0.9, "drift": 0.14},
    )
    book.alias("doc", "documentary")
    book.alias("still", "locked")
    book.alias("tripod", "locked")
    book.alias("run-and-gun", "energetic")
    book.alias("verite", "documentary")
    return book
