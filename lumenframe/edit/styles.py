"""Cut-style archetypes — one word that re-tunes the whole grammar of a sequence.

A **cut style** sets the axis baseline *and* carries the taste-floor knobs the
grammar reads directly: how large a fraction of the joins may become a showy
transition (``cut_frac``), how heavily the style leans on J/L audio splits
(``audio``), which transitions it reaches for when a join *does* earn one
(``palette`` / ``primary``), and whether it accelerates toward the end
(``accelerate``). Choosing a style is therefore not just a mood — it changes the
structural rules the plan is built under.

The six archetypes are trademark-safe; the brand-flavoured names an editor
reaches for ("mtv", "film", "music_video") resolve to them via aliases. An
unknown *style* raises (silently restyling would mislead), unlike an unknown
feeling which is merely reported.
"""
from __future__ import annotations

from lumenframe.craft import StyleBook

from lumenframe.edit.params import SPACE


def _hints(*, cut_frac: float, audio: float, primary: str, palette: list[str],
           accelerate: bool = False, dissolve_scale: float = 1.0,
           match_cuts: bool = False) -> dict:
    """Assemble one style's taste-floor knob block.

    ``cut_frac`` is the *ceiling* on the fraction of joins that may carry a
    seasoning transition — this is the single most important number in the whole
    library, the thing that keeps straight cuts the default. ``audio`` scales
    the J/L split offsets. ``primary``/``palette`` name the transitions this
    style earns. ``match_cuts`` invites action/match cuts on continuous motion.
    """
    return {
        "cut_frac": cut_frac,
        "audio": audio,
        "primary": primary,
        "palette": list(palette),
        "accelerate": accelerate,
        "dissolve_scale": dissolve_scale,
        "match_cuts": match_cuts,
    }


def edit_stylebook() -> StyleBook:
    """The edit domain's :class:`StyleBook` (six archetypes + three aliases)."""
    book = StyleBook(space=SPACE, default="invisible")

    book.add(
        "invisible",
        "Match & action cuts, straight joins; dissolves rare — the cut you never notice.",
        {"pace": 0.45, "invisibility": 0.92, "drama": 0.2, "variety": 0.25},
        hints=_hints(cut_frac=0.10, audio=0.6, primary="dissolve",
                     palette=["dissolve"], match_cuts=True),
    )
    book.add(
        "energetic",
        "Fast hard cuts and whip pans; almost no dissolves — cut on the beat.",
        {"pace": 0.85, "invisibility": 0.35, "drama": 0.6, "variety": 0.65},
        hints=_hints(cut_frac=0.30, audio=0.1, primary="whip_pan",
                     palette=["whip_pan", "dip_to_white", "cut"]),
    )
    book.add(
        "dreamy",
        "Long cross-dissolves, unhurried; every join melts into the next.",
        {"pace": 0.25, "invisibility": 0.6, "drama": 0.35, "variety": 0.3},
        hints=_hints(cut_frac=0.60, audio=0.3, primary="dissolve",
                     palette=["dissolve", "fade"], dissolve_scale=1.4),
    )
    book.add(
        "documentary",
        "J/L cuts and breathing room; audio leads and trails carry the edit.",
        {"pace": 0.4, "invisibility": 0.78, "drama": 0.3, "variety": 0.4},
        hints=_hints(cut_frac=0.18, audio=0.85, primary="dissolve",
                     palette=["dissolve", "fade"], match_cuts=True),
    )
    book.add(
        "montage",
        "Accelerating rhythm; shots tighten toward a climax then punctuate.",
        {"pace": 0.7, "invisibility": 0.45, "drama": 0.55, "variety": 0.6},
        hints=_hints(cut_frac=0.30, audio=0.15, primary="dissolve",
                     palette=["dissolve", "dip_to_black", "whip_pan"],
                     accelerate=True),
    )
    book.add(
        "commercial",
        "Punchy and varied; a rotating palette of quick, confident transitions.",
        {"pace": 0.75, "invisibility": 0.45, "drama": 0.5, "variety": 0.82},
        hints=_hints(cut_frac=0.45, audio=0.2, primary="whip_pan",
                     palette=["whip_pan", "wipe", "dip_to_white", "dissolve"]),
    )

    book.alias("mtv", "energetic")
    book.alias("film", "invisible")
    book.alias("music_video", "energetic")
    return book


#: The one shared stylebook instance the library resolves against.
STYLES: StyleBook = edit_stylebook()
