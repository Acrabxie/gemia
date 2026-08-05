"""Semantic axes for synthetic camera movement — words become a motion feel.

The camera library speaks four ``0..1`` axes and never raw pixels:

* ``energy``     — how fast / how far the camera travels (speed + magnitude).
* ``smoothness`` — how gentle the acceleration (soft ease vs. a punchy snap).
* ``drama``      — how *motivated* and large the push toward the subject.
* ``drift``      — the organic handheld amount (0 = locked to a tripod,
  1 = a loose operator breathing with the frame).

``energy`` / ``smoothness`` / ``drama`` are three of the shared spine axes, so a
shared feeling like "calm" or "dramatic" and shared feedback like "更慢" already
land here; ``drift`` is the one domain axis this library adds. Everything a move
needs downstream (a scale delta, a sine amplitude, an easing name) is *derived*
from these axes in :mod:`lumenframe.camera.camera` — this module only declares
the axes and the words that nudge them.
"""
from __future__ import annotations

from lumenframe.craft import FeedbackVocab, axis_space

#: The four camera axes. ``drift`` is the domain-specific one.
CAMERA_AXES: tuple[str, ...] = ("energy", "smoothness", "drama", "drift")

#: Neutral baseline when no style is chosen (the "cinematic" archetype is the
#: house default, so this bare neutral is rarely hit — it stays deliberately
#: mild so an un-styled brief still eases and stays subtle).
CAMERA_DEFAULTS: dict[str, float] = {
    "energy": 0.35,
    "smoothness": 0.65,
    "drama": 0.45,
    "drift": 0.15,
}

#: Domain feelings/feedback adjectives → axis nudges (bilingual). These extend
#: the shared table so "handheld" / "手持" / "steady" mean something *here*
#: without polluting the other five libraries' vocabularies.
CAMERA_WORDS: dict[str, dict[str, float]] = {
    "handheld": {"drift": +0.3, "smoothness": -0.05},
    "手持": {"drift": +0.3, "smoothness": -0.05},
    "shaky": {"drift": +0.32, "energy": +0.1},
    "晃": {"drift": +0.32, "energy": +0.1},
    "steady": {"drift": -0.3, "smoothness": +0.15},
    "稳": {"drift": -0.3, "smoothness": +0.15},
    "locked": {"drift": -0.38, "energy": -0.15},
    "static": {"drift": -0.38, "energy": -0.2},
    "锁定": {"drift": -0.38, "energy": -0.15},
    "floaty": {"drift": +0.22, "smoothness": +0.15},
    "float": {"drift": +0.2, "smoothness": +0.12},
    "漂浮": {"drift": +0.22, "smoothness": +0.15},
    "punchy": {"energy": +0.28, "drama": +0.1, "smoothness": -0.1},
    "punch": {"energy": +0.28, "drama": +0.1, "smoothness": -0.1},
    "冲": {"energy": +0.28, "drama": +0.1},
    "epic": {"drama": +0.32, "energy": -0.1, "smoothness": +0.1},
    "史诗": {"drama": +0.32, "energy": -0.1, "smoothness": +0.1},
    "cinematic": {"drama": +0.16, "smoothness": +0.1, "energy": -0.08},
    "electric": {"energy": +0.22, "drama": +0.08},
    "slow": {"energy": -0.22, "smoothness": +0.1},
    "fast": {"energy": +0.24},
}

#: The camera axis space, seeded with the shared feeling table + camera words.
SPACE = axis_space(CAMERA_AXES, CAMERA_DEFAULTS, extra_feelings=CAMERA_WORDS)


def feedback_vocab() -> FeedbackVocab:
    """A fresh :class:`FeedbackVocab` over the camera space (shared + domain).

    A new instance per call keeps the adjustment table private to one adjust
    pass (mirrors the vector library), so concurrent adjusts never share state.
    """
    return FeedbackVocab(SPACE).extend(CAMERA_WORDS)
