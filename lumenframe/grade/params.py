"""Semantic axes for colour grading — creative words → the six grade dials.

A grading brief is written in feeling, never in numbers: *warm*, *faded*,
*moody*, *filmic*. This module declares the domain's six ``0..1`` axes and the
bilingual adjective tables that nudge them, riding the shared
:class:`~lumenframe.craft.params.AxisSpace` machinery so the resolution order is
identical to every other point library::

    style baseline  →  feeling adjectives (±nudges)  →  explicit overrides

The axes (all ``0..1``, ``0.5`` neutral unless noted):

* ``warmth``      — cool ↔ warm white balance (temperature/tint).
* ``contrast``    — how steep the tone S-curve is around its pivot.
* ``saturation``  — global colour intensity (with a hard, enforced ceiling).
* ``lift``        — lifted / faded blacks (0 = inky, 1 = milky matte).
* ``drama``       — mood: vignette weight + a downward contrast-pivot bias.
* ``filmic``      — analogue optics: grain + highlight halation.

We reuse the shared axis names ``warmth`` and ``drama`` so shared feelings
("warm", "moody", "dramatic", "cool") and shared feedback words work out of the
box; the domain axes ``contrast/saturation/lift/filmic`` add the rest. The
actual axis→recipe maths lives in :mod:`lumenframe.grade.grade` (the taste
floor), never here — this module only turns words into the six dial values.
"""
from __future__ import annotations

from lumenframe.craft import AxisSpace, FeedbackVocab, axis_space

#: The six grade axes, in a stable order (used by the catalog + prompts).
GRADE_AXES: tuple[str, ...] = (
    "warmth", "contrast", "saturation", "lift", "drama", "filmic",
)

#: Neutral fall-backs for any axis a style baseline leaves unspecified. These
#: describe an *honest* image: balanced white point, a gentle S, natural
#: saturation, barely-lifted blacks, almost no vignette or grain.
GRADE_DEFAULTS: dict[str, float] = {
    "warmth": 0.5,
    "contrast": 0.5,
    "saturation": 0.5,
    "lift": 0.12,
    "drama": 0.12,
    "filmic": 0.08,
}

#: Domain feeling adjectives → axis nudges (merged over the shared table).
#: Bilingual on purpose — briefs arrive in English and Chinese alike. Words
#: that touch an axis we do not declare are inert, never fatal. A few shared
#: words (``moody``/``cinematic``) are re-pointed here so they land on *grade*
#: axes rather than the motion axes the shared table assumes.
GRADE_FEELINGS: dict[str, dict[str, float]] = {
    # white balance
    "teal": {"warmth": -0.16, "drama": +0.08},
    "orange": {"warmth": +0.16},
    "sunny": {"warmth": +0.2, "saturation": +0.08},
    "golden": {"warmth": +0.22, "lift": +0.06},
    "cold": {"warmth": -0.22},
    "icy": {"warmth": -0.24, "saturation": -0.06},
    "阳光": {"warmth": +0.2, "saturation": +0.08},
    "冷调": {"warmth": -0.22},
    # tone / contrast
    "punchy": {"contrast": +0.2, "saturation": +0.12},
    "contrasty": {"contrast": +0.22},
    "flat": {"contrast": -0.2},
    "crushed": {"lift": -0.2, "contrast": +0.12},
    "对比": {"contrast": +0.22},
    "通透": {"contrast": +0.12, "saturation": +0.08},
    # blacks / matte
    "faded": {"lift": +0.25, "contrast": -0.12, "saturation": -0.1},
    "matte": {"lift": +0.22, "contrast": -0.08},
    "dreamy": {"lift": +0.2, "contrast": -0.1, "filmic": +0.12},
    "褪色": {"lift": +0.25, "contrast": -0.12, "saturation": -0.1},
    # saturation
    "vibrant": {"saturation": +0.2},
    "muted": {"saturation": -0.22},
    "desaturated": {"saturation": -0.28},
    "饱和": {"saturation": +0.2},
    "清淡": {"saturation": -0.18},
    # optics / mood
    "filmic": {"filmic": +0.3},
    "grainy": {"filmic": +0.28},
    "颗粒": {"filmic": +0.28},
    "moody": {"drama": +0.18},
    "氛围": {"drama": +0.18},
    "cinematic": {"drama": +0.12, "contrast": +0.06},
    "电影感": {"drama": +0.12, "contrast": +0.06},
    "vintage": {"warmth": +0.15, "saturation": -0.15, "lift": +0.15, "filmic": +0.1},
    "复古": {"warmth": +0.15, "saturation": -0.15, "lift": +0.15, "filmic": +0.1},
}

#: Domain feedback adjectives for ``more/less X`` (中/英) → axis deltas, merged
#: over the shared table. Comparatives ("warmer", "moodier") fall back through
#: the shared :meth:`FeedbackVocab._lookup` logic.
GRADE_ADJUSTMENTS: dict[str, dict[str, float]] = {
    "warm": {"warmth": +0.2}, "暖": {"warmth": +0.2},
    "cool": {"warmth": -0.2}, "冷": {"warmth": -0.2},
    "teal": {"warmth": -0.14},
    "punchy": {"contrast": +0.2, "saturation": +0.1},
    "contrasty": {"contrast": +0.2}, "contrast": {"contrast": +0.2}, "对比": {"contrast": +0.2},
    "flat": {"contrast": -0.2},
    "faded": {"lift": +0.2, "contrast": -0.1}, "褪色": {"lift": +0.2, "contrast": -0.1},
    "matte": {"lift": +0.18},
    "crushed": {"lift": -0.2, "contrast": +0.1},
    "saturated": {"saturation": +0.2}, "饱和": {"saturation": +0.2},
    "vibrant": {"saturation": +0.2},
    "desaturated": {"saturation": -0.2}, "muted": {"saturation": -0.2},
    "filmic": {"filmic": +0.2}, "grainy": {"filmic": +0.2}, "颗粒": {"filmic": +0.2},
    "moody": {"drama": +0.2}, "dramatic": {"drama": +0.2}, "戏剧": {"drama": +0.2},
    "cinematic": {"drama": +0.12, "contrast": +0.08},
}


def grade_space() -> AxisSpace:
    """The colour-grading :class:`AxisSpace` (six axes + merged feelings)."""
    return axis_space(GRADE_AXES, GRADE_DEFAULTS, extra_feelings=GRADE_FEELINGS)


def grade_feedback(space: AxisSpace) -> FeedbackVocab:
    """The grade :class:`FeedbackVocab` (shared words + domain adjustments)."""
    return FeedbackVocab(space=space).extend(GRADE_ADJUSTMENTS)


#: Module-level singletons so every layer resolves against the same space.
SPACE: AxisSpace = grade_space()
FEEDBACK: FeedbackVocab = grade_feedback(SPACE)
