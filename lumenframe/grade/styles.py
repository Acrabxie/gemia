"""Look archetypes — one word that reshapes the whole grade.

A **look** is a named axis baseline plus a small bag of *hints* that tell the
taste-floor maths what KIND of grade this is: is it monochrome (noir)? may it
crush to true black (a stylised look that demands clipping)? does it want a
complementary shadow/highlight split (every cinematic look), and around which
hues? is it stylised — i.e. is skin-tone protection *off* because the drift is
the whole point (day-for-night's blue faces, cyberpunk's magenta shadows)?

The hints are the contract between a look and :mod:`lumenframe.grade.grade`:

* ``cinematic``      — enforce a complementary shadow/highlight hue split.
* ``stylised``       — skin-tone protection is disabled (drift is intentional).
* ``monochrome``     — saturation is driven to zero; no split toning.
* ``allow_clip``     — the tone curve may push blacks/whites past the safe range
  (a look that *demands* crushed blacks, e.g. noir).
* ``shadow_hue`` / ``highlight_hue`` — explicit split hues in degrees; when a
  cinematic look omits them the maths falls back to teal/orange.

Baselines are given for the six axes ``warmth, contrast, saturation, lift,
drama, filmic``; anything omitted falls back to
:data:`lumenframe.grade.params.GRADE_DEFAULTS`.
"""
from __future__ import annotations

from lumenframe.craft import StyleBook

from lumenframe.grade.params import SPACE

#: Reference split hues (degrees). Teal shadows / orange highlights is the
#: canonical complementary cinematic pairing (≈180° apart).
TEAL = 200.0
ORANGE = 30.0


def grade_styles() -> StyleBook:
    """Build the grade :class:`StyleBook` (ten looks + brand-flavoured aliases)."""
    book = StyleBook(space=SPACE, default="neutral")

    book.add(
        "neutral",
        "honest colour, a gentle protective S — the house default",
        {"warmth": 0.5, "contrast": 0.5, "saturation": 0.5,
         "lift": 0.1, "drama": 0.08, "filmic": 0.06},
        hints={"cinematic": False},
    )
    book.add(
        "teal_orange",
        "blockbuster complementary split: teal shadows, warm skin, orange highlights",
        {"warmth": 0.52, "contrast": 0.64, "saturation": 0.58,
         "lift": 0.18, "drama": 0.5, "filmic": 0.24},
        hints={"cinematic": True, "shadow_hue": TEAL, "highlight_hue": ORANGE},
    )
    book.add(
        "film",
        "warm Kodak highlights, a soft lifted toe, fine grain",
        {"warmth": 0.62, "contrast": 0.52, "saturation": 0.48,
         "lift": 0.24, "drama": 0.3, "filmic": 0.55},
        hints={"cinematic": True, "shadow_hue": 210.0, "highlight_hue": 40.0},
    )
    book.add(
        "bleach_bypass",
        "silver-retention: desaturated, high-contrast, steely",
        {"warmth": 0.48, "contrast": 0.8, "saturation": 0.2,
         "lift": 0.08, "drama": 0.55, "filmic": 0.35},
        hints={"cinematic": True, "shadow_hue": 205.0, "highlight_hue": 45.0},
    )
    book.add(
        "noir",
        "high-contrast black & white; crushed shadows, glowing highlights",
        {"warmth": 0.5, "contrast": 0.92, "saturation": 0.0,
         "lift": 0.04, "drama": 0.72, "filmic": 0.3},
        hints={"monochrome": True, "allow_clip": True, "stylised": True},
    )
    book.add(
        "day_for_night",
        "cool crushed blue cast that reads as moonlight in daylight",
        {"warmth": 0.16, "contrast": 0.62, "saturation": 0.34,
         "lift": 0.05, "drama": 0.58, "filmic": 0.2},
        hints={"stylised": True, "allow_clip": True,
               "shadow_hue": 230.0, "highlight_hue": 220.0},
    )
    book.add(
        "pastel",
        "low-contrast lifted matte with soft, gentle hues",
        {"warmth": 0.55, "contrast": 0.3, "saturation": 0.44,
         "lift": 0.42, "drama": 0.12, "filmic": 0.16},
        hints={"cinematic": True, "shadow_hue": 215.0, "highlight_hue": 35.0},
    )
    book.add(
        "cyberpunk",
        "neon night: magenta shadows, cyan highlights, high saturation",
        {"warmth": 0.46, "contrast": 0.68, "saturation": 0.72,
         "lift": 0.26, "drama": 0.6, "filmic": 0.3},
        hints={"stylised": True, "shadow_hue": 310.0, "highlight_hue": 190.0},
    )
    book.add(
        "vintage",
        "faded, warm, low-saturation with a soft grain — instant nostalgia",
        {"warmth": 0.66, "contrast": 0.42, "saturation": 0.32,
         "lift": 0.36, "drama": 0.3, "filmic": 0.5},
        hints={"cinematic": True, "shadow_hue": 215.0, "highlight_hue": 45.0},
    )
    book.add(
        "clean",
        "commercial punch: neutral balance, crisp contrast, rich but honest",
        {"warmth": 0.5, "contrast": 0.58, "saturation": 0.6,
         "lift": 0.06, "drama": 0.08, "filmic": 0.04},
        hints={"cinematic": True, "shadow_hue": 205.0, "highlight_hue": 35.0},
    )

    (book
     .alias("kodak", "film")
     .alias("blockbuster", "teal_orange")
     .alias("bw", "noir")
     .alias("blackwhite", "noir")
     .alias("instagram", "vintage")
     .alias("cinematic", "teal_orange"))
    return book


#: Module-level singleton — one book shared across api / catalog / tool.
STYLES: StyleBook = grade_styles()
