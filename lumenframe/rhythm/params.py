"""Semantic axes + feedback vocabulary for musical rhythm editing.

Rhythm is the point library that cuts a timeline **to the beat**. Its four
``0..1`` axes are the whole numeric surface an agent ever touches:

* ``energy``    — shared axis; overall intensity of the edit (also nudged by the
  shared ``energetic``/``calm``/``fast``/``slow`` feelings, so a brief written
  for another library still reads sensibly here).
* ``tightness`` — how strictly cuts lock to the grid. High = surgical, on the
  sample; low = loose, syncopation and human drift allowed.
* ``drive``     — cut *density*. High = choppy, many cuts per bar; low = long
  held shots.
* ``build``     — how hard the density *accelerates* into a drop (only the
  ``build_drop`` sync pattern reads it strongly, but every pattern carries it so
  feedback like "更 building" resolves).

Every low-level decision (how many beats between cuts, which off-beats get
syncopated, where the acceleration ramp starts) is derived from these axes by
the taste-floor maths in :mod:`lumenframe.rhythm.rhythm`. This module only turns
*words* into the four numbers, reusing the shared resolution order
(style baseline → feeling nudges → explicit overrides) so behaviour is identical
to every other Lumeri library.
"""
from __future__ import annotations

from lumenframe.craft import FeedbackVocab, axis_space

#: The four semantic axes rhythm speaks. ``energy`` is the shared axis name so
#: cross-library feelings keep working; the other three are domain-specific.
RHYTHM_AXES: tuple[str, ...] = ("energy", "tightness", "drive", "build")

#: Neutral baseline used when no style is chosen (the house pulse: a moderately
#: tight, medium-density on-beat cut with only a hint of build).
RHYTHM_DEFAULTS: dict[str, float] = {
    "energy": 0.5,
    "tightness": 0.6,
    "drive": 0.5,
    "build": 0.3,
}

#: Domain feeling adjectives (bilingual) → axis nudges, layered onto the shared
#: table. These let ``brief["feeling"]`` speak the language of an editor: a clip
#: is *driving*, *choppy*, *sparse*, *building* — never "drive=0.7".
RHYTHM_FEELINGS: dict[str, dict[str, float]] = {
    "driving": {"drive": +0.2, "energy": +0.1}, "推进": {"drive": +0.2, "energy": +0.1},
    "tight": {"tightness": +0.2}, "紧凑": {"tightness": +0.2},
    "locked": {"tightness": +0.25}, "卡点": {"tightness": +0.25, "drive": +0.05},
    "loose": {"tightness": -0.2}, "松弛": {"tightness": -0.2},
    "busy": {"drive": +0.2}, "密集": {"drive": +0.2},
    "choppy": {"drive": +0.25, "energy": +0.1}, "碎切": {"drive": +0.25, "energy": +0.1},
    "sparse": {"drive": -0.2}, "稀疏": {"drive": -0.2},
    "held": {"drive": -0.25, "energy": -0.1}, "长镜": {"drive": -0.25},
    "punchy": {"energy": +0.15, "drive": +0.1}, "有力": {"energy": +0.15, "drive": +0.1},
    "building": {"build": +0.25}, "渐强": {"build": +0.25},
    "explosive": {"build": +0.2, "energy": +0.15}, "爆发": {"build": +0.2, "energy": +0.15},
    "hype": {"energy": +0.2, "drive": +0.15}, "燃": {"energy": +0.2, "drive": +0.15},
    "syncopated": {"tightness": -0.15, "drive": +0.1}, "切分": {"tightness": -0.15, "drive": +0.1},
}

#: The domain axis space, seeded with the shared feeling table + rhythm feelings.
SPACE = axis_space(RHYTHM_AXES, RHYTHM_DEFAULTS, extra_feelings=RHYTHM_FEELINGS)

#: "more/less X" adjustment table for the human feedback loop. Mirrors the
#: feelings above; the shared base table (fast/slow/energetic/calm/…) is already
#: present, so we only add the domain-specific directions.
RHYTHM_ADJUSTMENTS: dict[str, dict[str, float]] = {
    "driving": {"drive": +0.2, "energy": +0.1}, "推进": {"drive": +0.2, "energy": +0.1},
    "tight": {"tightness": +0.2}, "紧凑": {"tightness": +0.2},
    "locked": {"tightness": +0.25}, "卡点": {"tightness": +0.25},
    "loose": {"tightness": -0.2}, "松弛": {"tightness": -0.2},
    "busy": {"drive": +0.2}, "密集": {"drive": +0.2},
    "choppy": {"drive": +0.25, "energy": +0.1}, "碎切": {"drive": +0.25, "energy": +0.1},
    "sparse": {"drive": -0.2}, "稀疏": {"drive": -0.2},
    "held": {"drive": -0.25}, "长镜": {"drive": -0.25},
    "punchy": {"energy": +0.15, "drive": +0.1}, "有力": {"energy": +0.15, "drive": +0.1},
    "build": {"build": +0.25}, "building": {"build": +0.25}, "渐强": {"build": +0.25},
    "explosive": {"build": +0.2, "energy": +0.15}, "爆发": {"build": +0.2, "energy": +0.15},
    "hype": {"energy": +0.2, "drive": +0.15}, "燃": {"energy": +0.2, "drive": +0.15},
    "syncopated": {"tightness": -0.15, "drive": +0.1}, "切分": {"tightness": -0.15, "drive": +0.1},
    "dense": {"drive": +0.2},
}


def rhythm_vocab() -> FeedbackVocab:
    """A fresh :class:`FeedbackVocab` over the rhythm axis space.

    Seeded with the shared base adjustments and extended with the domain table
    above. Constructed on demand so callers never mutate a shared instance.
    """
    return FeedbackVocab(space=SPACE).extend(RHYTHM_ADJUSTMENTS)
