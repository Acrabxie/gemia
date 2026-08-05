"""Framing archetypes — the named looks a single word reshapes the frame with.

A framing is one word that sets the four-axis baseline *and* the ``grid`` the
composition is built on. Choosing ``golden`` versus ``centered`` versus
``dynamic`` re-tunes tension/balance/space/tightness together and swaps the
underlying anchor lattice — the agent never touches a coordinate.

Every framing pins a ``grid`` hint (``"thirds" | "golden" | "center"``) that
:mod:`lumenframe.compose.framing` reads to pick anchor points; the taste floor
lives there, not here. Archetype names are generic/trademark-safe; the common
synonyms an agent reaches for (``symmetry`` → ``centered``, ``phi`` → ``golden``,
``establishing`` → ``wide``, ``closeup`` → ``tight``) resolve as aliases. An
unknown *framing* raises (silently restyling misleads); an unknown *feeling*
does not.
"""
from __future__ import annotations

from lumenframe.craft import StyleBook

from lumenframe.compose.params import COMPOSE_SPACE

#: The framing style book. Baselines are hand-tuned per the axis meanings in
#: :mod:`lumenframe.compose.params`; ``grid`` selects the anchor lattice.
FRAMINGS = StyleBook(space=COMPOSE_SPACE, default="thirds")

FRAMINGS.add(
    "thirds",
    "Rule of thirds — subject on a thirds intersection, classic and steady.",
    {"tension": 0.5, "balance": 0.6, "negative_space": 0.45, "tightness": 0.45},
    hints={"grid": "thirds"},
)
FRAMINGS.add(
    "centered",
    "Dead-centre symmetry — subject on the middle axis, formal and calm.",
    {"tension": 0.22, "balance": 0.92, "negative_space": 0.4, "tightness": 0.5},
    hints={"grid": "center"},
)
FRAMINGS.add(
    "golden",
    "Golden ratio (phi) placement — subject on a 0.382/0.618 intersection.",
    {"tension": 0.55, "balance": 0.55, "negative_space": 0.45, "tightness": 0.45},
    hints={"grid": "golden"},
)
FRAMINGS.add(
    "negative_space",
    "A small subject adrift in breathing room — quiet, editorial, minimal.",
    {"tension": 0.4, "balance": 0.5, "negative_space": 0.9, "tightness": 0.18},
    hints={"grid": "thirds"},
)
FRAMINGS.add(
    "dynamic",
    "Diagonal tension — subject pushed to a strong third, unresolved energy.",
    {"tension": 0.9, "balance": 0.3, "negative_space": 0.45, "tightness": 0.5},
    hints={"grid": "thirds", "diagonal": True},
)
FRAMINGS.add(
    "tight",
    "Close and pressing — subject fills the frame, intimate and intense.",
    {"tension": 0.62, "balance": 0.42, "negative_space": 0.1, "tightness": 0.9},
    hints={"grid": "thirds"},
)
FRAMINGS.add(
    "wide",
    "Establishing wide — subject small in a broad scene; room to read place.",
    {"tension": 0.35, "balance": 0.6, "negative_space": 0.72, "tightness": 0.12},
    hints={"grid": "thirds"},
)

FRAMINGS.alias("symmetry", "centered")
FRAMINGS.alias("symmetric", "centered")
FRAMINGS.alias("phi", "golden")
FRAMINGS.alias("goldenratio", "golden")
FRAMINGS.alias("rule_of_thirds", "thirds")
FRAMINGS.alias("thirds_grid", "thirds")
FRAMINGS.alias("establishing", "wide")
FRAMINGS.alias("closeup", "tight")
FRAMINGS.alias("minimal", "negative_space")
FRAMINGS.alias("breathing_room", "negative_space")
