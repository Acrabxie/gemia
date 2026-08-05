"""Composition axes — where framing language becomes placement numbers.

``compose`` closes the *framing* domain: given where subjects sit in a source
image, decide where they should sit in the delivered frame. Four semantic axes
(each ``0..1``) drive every geometric choice; nothing downstream invents its own
axis-to-number mapping:

* ``tension``        — how far the subject is pushed off dead centre, how
  diagonal / unresolved the arrangement feels (0 = serene & central,
  1 = edge-of-frame, strong diagonal pull).
* ``balance``       — how much counter-mass is asked for on the opposite side;
  high balance pulls toward symmetry and steadier weight distribution.
* ``negative_space`` — how much empty "breathing room" surrounds the subject
  (0 = subject fills the frame, 1 = a small subject in a large void).
* ``tightness``     — crop closeness: how large the subject reads in frame
  (0 = wide establishing, 1 = intimate close crop). The natural opposite pull
  to ``negative_space``, kept separate so "tight *and* airy" (a small-but-close
  vignette) stays expressible.

Resolution order is the shared one — ``style baseline → feelings → overrides`` —
so a single framing word re-tunes all four axes at once and a stray adjective
never fails a brief. The domain feelings below let the shared bilingual surface
("more tension", "留白多一点", "airy", "紧凑") land on these four axes; shared
feelings that name axes we do not declare stay inert, exactly as the spine
intends.
"""
from __future__ import annotations

from lumenframe.craft import FeedbackVocab, axis_space

#: The four framing axes, in a stable order (used by the catalog + tests).
COMPOSE_AXES: tuple[str, ...] = ("tension", "balance", "negative_space", "tightness")

#: Neutral baseline — a mild, classic thirds-ish default before any style.
COMPOSE_NEUTRAL: dict[str, float] = {
    "tension": 0.5, "balance": 0.55, "negative_space": 0.4, "tightness": 0.45,
}

#: Domain feeling adjectives → axis nudges (bilingual, on purpose). These make
#: the shared feeling/feedback surface expressive for framing; each names only
#: the four declared axes so nothing leaks. Extend freely — unknown feelings are
#: reported, never fatal.
COMPOSE_FEELINGS: dict[str, dict[str, float]] = {
    # tension / drama / diagonal pull
    "tense": {"tension": +0.25},
    "edgy": {"tension": +0.2, "balance": -0.1},
    "dynamic": {"tension": +0.2, "balance": -0.1},
    "dramatic": {"tension": +0.2},
    "punchy": {"tension": +0.15, "tightness": +0.1},
    "epic": {"negative_space": +0.2, "tension": +0.1},
    "张力": {"tension": +0.25},
    "紧张": {"tension": +0.25},
    "动感": {"tension": +0.2, "balance": -0.1},
    # balance / symmetry / stillness
    "balanced": {"balance": +0.25, "tension": -0.1},
    "symmetric": {"balance": +0.3, "tension": -0.15},
    "symmetrical": {"balance": +0.3, "tension": -0.15},
    "stable": {"balance": +0.2, "tension": -0.1},
    "calm": {"tension": -0.2, "balance": +0.15},
    "serene": {"tension": -0.2, "balance": +0.15},
    "平衡": {"balance": +0.25, "tension": -0.1},
    "对称": {"balance": +0.3, "tension": -0.15},
    "稳定": {"balance": +0.2, "tension": -0.1},
    # negative space / breathing room
    "airy": {"negative_space": +0.3, "tightness": -0.15},
    "spacious": {"negative_space": +0.3, "tightness": -0.2},
    "open": {"negative_space": +0.2},
    "breathing": {"negative_space": +0.25},
    "minimal": {"negative_space": +0.25, "tightness": -0.1},
    "留白": {"negative_space": +0.3, "tightness": -0.15},
    "空旷": {"negative_space": +0.3},
    "极简": {"negative_space": +0.25, "tightness": -0.1},
    # tightness / intimacy / closeness
    "tight": {"tightness": +0.3, "negative_space": -0.2},
    "close": {"tightness": +0.25, "negative_space": -0.15},
    "intimate": {"tightness": +0.25, "negative_space": -0.1},
    "cramped": {"tightness": +0.2, "negative_space": -0.25},
    "紧凑": {"tightness": +0.3, "negative_space": -0.2},
    "特写": {"tightness": +0.3, "negative_space": -0.15},
}

#: The composition axis space — the one place framing words become axis values.
COMPOSE_SPACE = axis_space(
    COMPOSE_AXES, COMPOSE_NEUTRAL, extra_feelings=COMPOSE_FEELINGS,
)

#: Feedback ("more tension" / "紧凑一点" / "tighter") → axis deltas. The direct
#: axis names are registered as adjectives so "more tension" works verbatim, and
#: comparatives ("tighter", "airier") fall back through the shared parser.
COMPOSE_FEEDBACK: dict[str, dict[str, float]] = {
    "tension": {"tension": +0.2},
    "tense": {"tension": +0.2},
    "dynamic": {"tension": +0.2, "balance": -0.1},
    "dramatic": {"tension": +0.2},
    "edgy": {"tension": +0.2},
    "balance": {"balance": +0.2, "tension": -0.1},
    "balanced": {"balance": +0.2, "tension": -0.1},
    "symmetric": {"balance": +0.25, "tension": -0.15},
    "stable": {"balance": +0.2, "tension": -0.1},
    "calm": {"tension": -0.2, "balance": +0.15},
    "negative space": {"negative_space": +0.25, "tightness": -0.1},
    "negativespace": {"negative_space": +0.25, "tightness": -0.1},
    "breathing room": {"negative_space": +0.25},
    "airy": {"negative_space": +0.25, "tightness": -0.15},
    "spacious": {"negative_space": +0.25, "tightness": -0.15},
    "open": {"negative_space": +0.2},
    "minimal": {"negative_space": +0.2, "tightness": -0.1},
    "tightness": {"tightness": +0.2, "negative_space": -0.1},
    "tight": {"tightness": +0.25, "negative_space": -0.15},
    "close": {"tightness": +0.2, "negative_space": -0.1},
    "intimate": {"tightness": +0.2, "negative_space": -0.1},
    "cramped": {"tightness": +0.2, "negative_space": -0.2},
    # bilingual
    "张力": {"tension": +0.2},
    "平衡": {"balance": +0.2, "tension": -0.1},
    "对称": {"balance": +0.25, "tension": -0.15},
    "留白": {"negative_space": +0.25, "tightness": -0.1},
    "紧凑": {"tightness": +0.25, "negative_space": -0.15},
    "特写": {"tightness": +0.25, "negative_space": -0.15},
    "动感": {"tension": +0.2, "balance": -0.1},
}


def compose_feedback() -> FeedbackVocab:
    """A fresh feedback vocabulary over the composition axes.

    Constructed per call so callers never share mutable adjustment state; the
    shared base adjectives (which mostly name axes we do not declare) stay
    inert, and our domain table is layered on top.
    """
    return FeedbackVocab(space=COMPOSE_SPACE).extend(COMPOSE_FEEDBACK)
