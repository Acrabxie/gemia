"""Semantic axes for kinetic typography — words become type parameters.

Kinetic type has six axes an agent sets with feeling words instead of raw
point sizes and millisecond delays:

* ``energy``      — how driven the motion reads (stagger snap, overshoot).
* ``weight``      — type boldness (font-weight band).
* ``elegance``    — restraint / refinement (serif lean, generous holds).
* ``playfulness`` — how loose and characterful the reveal is.
* ``pace``        — reveal speed (per-unit stagger + duration).
* ``density``     — tracking + leading tightness (also caps line length).

``energy``, ``elegance`` and ``playfulness`` are shared axis names, so the
spine's bilingual feeling table already nudges them; ``weight``, ``pace`` and
``density`` are this domain's own. Every low-level number (a point size, a
letter-spacing, a stagger in seconds) is *derived* from these axes in
:mod:`lumenframe.kinetic.typography`; nothing here maps to pixels — this module
only turns words into ``0..1`` axis values, exactly like the reference
:mod:`lumenframe.vector.params` does for motion.
"""
from __future__ import annotations

from lumenframe.craft import FeedbackVocab, axis_space

#: The domain's declared axes, in catalog/documentation order.
AXES: tuple[str, ...] = ("energy", "weight", "elegance", "playfulness", "pace", "density")

#: Neutral baseline used before a style is applied (all mid-scale).
DEFAULTS: dict[str, float] = {a: 0.5 for a in AXES}

#: Domain feeling adjectives (中/英) → axis nudges. These *extend* (and, where a
#: shared word like "bold" needs a domain meaning, *override*) the spine's base
#: table, so the six kinetic axes all respond to natural language. A nudge to an
#: axis this space does not declare is silently inert.
KINETIC_FEELINGS: dict[str, dict[str, float]] = {
    # weight
    "bold": {"weight": +0.25, "energy": +0.05},
    "heavy": {"weight": +0.3},
    "black": {"weight": +0.35},
    "chunky": {"weight": +0.28, "playfulness": +0.05},
    "light": {"weight": -0.25},
    "thin": {"weight": -0.3},
    "hairline": {"weight": -0.35, "elegance": +0.1},
    "粗": {"weight": +0.3}, "加粗": {"weight": +0.3},
    "细": {"weight": -0.3}, "纤细": {"weight": -0.3},
    # density (tracking / leading tightness / line length)
    "condensed": {"density": +0.25},
    "tight": {"density": +0.25},
    "compact": {"density": +0.2, "elegance": -0.05},
    "airy": {"density": -0.25, "elegance": +0.1},
    "open": {"density": -0.2},
    "loose": {"density": -0.25},
    "spacious": {"density": -0.3, "elegance": +0.1},
    "紧凑": {"density": +0.25}, "紧": {"density": +0.2},
    "宽松": {"density": -0.25}, "疏朗": {"density": -0.25, "elegance": +0.1},
    # pace (reveal speed) — override the shared energy-only "fast"/"slow"
    "fast": {"pace": +0.25, "energy": +0.1},
    "quick": {"pace": +0.25},
    "snappy": {"pace": +0.2, "energy": +0.15},
    "punchy": {"pace": +0.2, "energy": +0.2, "weight": +0.15},
    "slow": {"pace": -0.25, "energy": -0.1},
    "deliberate": {"pace": -0.2, "elegance": +0.1},
    "measured": {"pace": -0.15, "elegance": +0.1},
    "快": {"pace": +0.25, "energy": +0.1}, "慢": {"pace": -0.25, "energy": -0.1},
    # character
    "editorial": {"elegance": +0.2, "energy": -0.05},
    "refined": {"elegance": +0.25},
    "sporty": {"energy": +0.2, "playfulness": +0.1},
    "punk": {"playfulness": +0.25, "energy": +0.2, "elegance": -0.15},
}

#: Feedback adjectives (comparatives) → axis deltas for ``op:"adjust"``. Extends
#: the spine base table so "bolder", "更紧凑", "much faster" all move the right
#: kinetic axis (the base table only knows the shared axes).
KINETIC_ADJUSTMENTS: dict[str, dict[str, float]] = {
    "bold": {"weight": +0.2}, "bolder": {"weight": +0.2},
    "heavy": {"weight": +0.25}, "heavier": {"weight": +0.25},
    "light": {"weight": -0.2}, "lighter": {"weight": -0.2},
    "thin": {"weight": -0.2}, "thinner": {"weight": -0.2},
    "粗": {"weight": +0.2}, "细": {"weight": -0.2},
    "tight": {"density": +0.2}, "tighter": {"density": +0.2},
    "condensed": {"density": +0.2},
    "loose": {"density": -0.2}, "looser": {"density": -0.2},
    "airy": {"density": -0.2}, "airier": {"density": -0.2},
    "open": {"density": -0.15},
    "紧凑": {"density": +0.2}, "宽松": {"density": -0.2},
    "fast": {"pace": +0.2}, "faster": {"pace": +0.2},
    "quick": {"pace": +0.2}, "quicker": {"pace": +0.2},
    "snappy": {"pace": +0.2, "energy": +0.1},
    "punchy": {"pace": +0.15, "energy": +0.15, "weight": +0.1},
    "slow": {"pace": -0.2}, "slower": {"pace": -0.2},
    "快": {"pace": +0.2}, "慢": {"pace": -0.2},
    "editorial": {"elegance": +0.2},
}


def kinetic_space():
    """The :class:`~lumenframe.craft.params.AxisSpace` for kinetic type."""
    return axis_space(AXES, DEFAULTS, extra_feelings=KINETIC_FEELINGS)


#: Module-level singleton (styles + api resolve against the same space object).
SPACE = kinetic_space()


def kinetic_feedback() -> FeedbackVocab:
    """The feedback vocabulary bound to the kinetic axis space."""
    return FeedbackVocab(SPACE).extend(KINETIC_ADJUSTMENTS)
