"""Semantic axes for edit grammar — words about *rhythm* become cut numbers.

The edit library reasons about how clips join: how fast they come, how seamless
the joins feel, how much a cut is allowed to shout, and how varied the grammar
is across a sequence. Four ``0..1`` axes carry that whole surface; every
low-level number (a dissolve length, a transition budget, a J-cut offset) is
*derived* from these axes in :mod:`lumenframe.edit.grammar`, never asked for by
the caller.

Axes
----
``pace``          how fast the cutting is (shot length ↓ as pace ↑).
``invisibility``  how seamless the joins should feel — the axis that keeps
                  straight cuts, match cuts and J/L cuts the default and holds
                  showy transitions to a small fraction of the joins.
``drama``         how much a cut is allowed to *land* — accents, dips to black,
                  hard punches. Shared with the craft spine so "dramatic" /
                  "戏剧" nudge it out of the box.
``variety``       how much the grammar changes across the sequence (one repeated
                  move vs. a rotating palette).

Only ``drama`` is a shared spine axis; the other three are domain-specific, so
this module supplies an :data:`EDIT_FEELINGS` table that teaches the shared
adjective vocabulary ("fast", "seamless", "dreamy", 快/无缝) to move them. Shared
feelings that name an axis we do not declare (e.g. "warm") stay inert and
harmless, exactly as the spine intends.
"""
from __future__ import annotations

from lumenframe.craft import AxisSpace, FeedbackVocab, axis_space

#: The four declared axes, in a stable order (used by the catalog + describe).
AXES: tuple[str, ...] = ("pace", "invisibility", "drama", "variety")

#: Neutral baseline when no style is chosen — a competent, mostly-invisible cut.
DEFAULTS: dict[str, float] = {
    "pace": 0.5,
    "invisibility": 0.7,   # straight cuts are the house default
    "drama": 0.3,
    "variety": 0.4,
}

#: Domain feeling adjectives → axis nudges. These OVERRIDE the shared table's
#: entries for the same word (e.g. shared "energetic" nudges ``energy`` which
#: this space does not declare — here it nudges ``pace``). Bilingual so a
#: Chinese brief steers the cut just as well as an English one.
EDIT_FEELINGS: dict[str, dict[str, float]] = {
    # tempo
    "fast": {"pace": +0.25}, "快": {"pace": +0.25},
    "quick": {"pace": +0.2},
    "slow": {"pace": -0.25}, "慢": {"pace": -0.25},
    "energetic": {"pace": +0.2, "drama": +0.05}, "活力": {"pace": +0.2, "drama": +0.05},
    "punchy": {"pace": +0.2, "drama": +0.15},
    "calm": {"pace": -0.2, "invisibility": +0.15}, "平静": {"pace": -0.2, "invisibility": +0.15},
    "breathing": {"pace": -0.15, "invisibility": +0.1},
    # seamlessness
    "seamless": {"invisibility": +0.3}, "无缝": {"invisibility": +0.3},
    "invisible": {"invisibility": +0.3},
    "smooth": {"invisibility": +0.2}, "顺滑": {"invisibility": +0.2},
    "dreamy": {"invisibility": +0.2, "pace": -0.15, "drama": +0.05},
    "raw": {"invisibility": -0.2, "drama": +0.1},
    "jumpy": {"invisibility": -0.25, "pace": +0.15},
    # weight
    "dramatic": {"drama": +0.2, "pace": +0.05}, "戏剧": {"drama": +0.2, "pace": +0.05},
    "bold": {"drama": +0.2, "pace": +0.1},
    "gentle": {"drama": -0.15, "invisibility": +0.15}, "克制": {"drama": -0.15, "invisibility": +0.1},
    # variety
    "varied": {"variety": +0.25}, "丰富": {"variety": +0.25},
    "eclectic": {"variety": +0.25},
    "consistent": {"variety": -0.25}, "统一": {"variety": -0.25},
    "montage": {"variety": +0.2, "pace": +0.2},
}


def edit_axis_space() -> AxisSpace:
    """The edit domain's :class:`AxisSpace`, seeded with the feeling table."""
    return axis_space(AXES, DEFAULTS, extra_feelings=EDIT_FEELINGS)


#: The one shared space instance the whole library resolves against.
SPACE: AxisSpace = edit_axis_space()


def edit_feedback() -> FeedbackVocab:
    """Feedback vocabulary — "more/less X" (中/英) → axis deltas → new brief.

    Extends the shared ``more <adjective>`` table with the same cut-craft words
    the feeling table knows, so "more seamless", "punchier"-adjacent phrasing
    and "更快"/"少一点戏剧" all steer the re-derived plan. Only adjectives that
    touch a declared axis count as recognised; the rest are reported honestly.
    """
    return FeedbackVocab(space=SPACE).extend({
        "fast": {"pace": +0.2}, "快": {"pace": +0.2},
        "slow": {"pace": -0.2}, "慢": {"pace": -0.2},
        "quick": {"pace": +0.2},
        "punchy": {"pace": +0.15, "drama": +0.1},
        "energetic": {"pace": +0.2, "drama": +0.05}, "活力": {"pace": +0.2},
        "calm": {"pace": -0.2, "invisibility": +0.15},
        "seamless": {"invisibility": +0.25}, "无缝": {"invisibility": +0.25},
        "invisible": {"invisibility": +0.25},
        "smooth": {"invisibility": +0.2}, "顺滑": {"invisibility": +0.2},
        "dreamy": {"invisibility": +0.2, "pace": -0.15},
        "raw": {"invisibility": -0.2, "drama": +0.1},
        "jumpy": {"invisibility": -0.25},
        "dramatic": {"drama": +0.2}, "戏剧": {"drama": +0.2},
        "bold": {"drama": +0.2, "pace": +0.1},
        "gentle": {"drama": -0.15, "invisibility": +0.15},
        "varied": {"variety": +0.2}, "丰富": {"variety": +0.2},
        "consistent": {"variety": -0.2}, "统一": {"variety": -0.2},
    })
