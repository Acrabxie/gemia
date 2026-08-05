"""Style archetypes — one word that reshapes the whole title.

A kinetic style sets the axis baseline *and* the typographic kit the taste
floor reads: the font family (generic ``sans-serif`` / ``serif`` — never a
webfont, so the SVG stays self-contained), whether the face is condensed, the
modular **type-scale ratio** (1.2 minor-third → 1.333 perfect-fourth), letter
case, and the layout/reveal the style prefers when the brief leaves them open.

Styles carry *hints*, not logic: every pixel is still derived from the resolved
axes in :mod:`lumenframe.kinetic.typography`. Archetype names are trademark-safe;
the brand-flavoured aliases agents reach for ("apple", "news", "google") resolve
onto them exactly as the vector library does.
"""
from __future__ import annotations

from lumenframe.craft import StyleBook

from lumenframe.kinetic.params import SPACE

#: Default archetype (the house register for titles).
DEFAULT_STYLE = "title_hero"


def kinetic_styles() -> StyleBook:
    """Build the kinetic :class:`~lumenframe.craft.styles.StyleBook`.

    Each style's ``hints`` are the type kit; the ``baseline`` is the axis
    fingerprint. Hints keys read downstream:

    * ``family``        — ``"sans-serif"`` | ``"serif"`` (generic families only).
    * ``condensed``     — narrower advance width (a condensed cut lookalike).
    * ``scale_ratio``   — the modular type scale (>= 1.2, enforced).
    * ``case``          — ``"none"`` | ``"upper"`` letter case for headings.
    * ``layout``        — the layout used when the brief names none.
    * ``reveal``        — the reveal used when the brief names none.
    * ``palette``       — the theme palette the look ships with.
    """
    book = StyleBook(space=SPACE, default=DEFAULT_STYLE)

    book.add(
        "title_hero",
        "bold centered sans hero title — the confident house headline",
        {"energy": 0.55, "weight": 0.8, "elegance": 0.4,
         "playfulness": 0.3, "pace": 0.5, "density": 0.45},
        hints={"family": "sans-serif", "condensed": False, "scale_ratio": 1.333,
               "case": "none", "layout": "title_card", "reveal": "rise_fade",
               "palette": "ink"},
    )
    book.add(
        "editorial",
        "elegant serif — long-form titles, quotes, restrained line reveals",
        {"energy": 0.3, "weight": 0.45, "elegance": 0.85,
         "playfulness": 0.15, "pace": 0.35, "density": 0.4},
        hints={"family": "serif", "condensed": False, "scale_ratio": 1.25,
               "case": "none", "layout": "quote", "reveal": "per_line",
               "palette": "noir"},
    )
    book.add(
        "kinetic",
        "punchy condensed sans — upper-case, tight, word-by-word snap",
        {"energy": 0.85, "weight": 0.75, "elegance": 0.3,
         "playfulness": 0.55, "pace": 0.8, "density": 0.7},
        hints={"family": "sans-serif", "condensed": True, "scale_ratio": 1.333,
               "case": "upper", "layout": "title_card", "reveal": "per_word",
               "palette": "ink"},
    )
    book.add(
        "broadcast",
        "clean lower-third — name + role in the title-safe corner",
        {"energy": 0.45, "weight": 0.6, "elegance": 0.55,
         "playfulness": 0.25, "pace": 0.6, "density": 0.5},
        hints={"family": "sans-serif", "condensed": False, "scale_ratio": 1.25,
               "case": "none", "layout": "lower_third", "reveal": "mask_wipe",
               "palette": "ink"},
    )
    book.add(
        "lyric",
        "word-synced center — one phrase at a time, sung to camera",
        {"energy": 0.6, "weight": 0.55, "elegance": 0.5,
         "playfulness": 0.5, "pace": 0.55, "density": 0.45},
        hints={"family": "sans-serif", "condensed": False, "scale_ratio": 1.25,
               "case": "none", "layout": "kinetic_lyric", "reveal": "per_word",
               "palette": "lumeri"},
    )
    book.add(
        "minimal",
        "restrained — light weight, airy tracking, a single quiet rise",
        {"energy": 0.3, "weight": 0.4, "elegance": 0.8,
         "playfulness": 0.1, "pace": 0.4, "density": 0.35},
        hints={"family": "sans-serif", "condensed": False, "scale_ratio": 1.2,
               "case": "none", "layout": "title_card", "reveal": "rise_fade",
               "palette": "noir"},
    )

    book.alias("apple", "minimal")
    book.alias("news", "broadcast")
    book.alias("google", "kinetic")
    return book


#: Module-level singleton so styles/api/catalog share one book.
STYLES = kinetic_styles()
