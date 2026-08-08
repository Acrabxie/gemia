"""G14 (charter §7) — global token uniqueness across creative point-libraries.

P3/P6: a library's named archetype/catalog vocabulary must not collide with
another library's, or the same word means two things to the model. This gate
computes the collision set from the LIVE registries (self-updating as libraries
evolve) and pins it to an explicit BOUNDED known set — the same honesty idiom
the charter uses for ``CRAFT_GUARD_PENDING_VERBS``: nothing is hidden, a NEW
collision turns this red, and resolving a known one also turns it red (forcing
the list to shrink deliberately).

Explicitly OUT of the unique namespace (legitimately shared by design, and so
never flagged): the op discriminators ``create/adjust/catalog``; every library's
semantic axes (``energy``/``drama``… are deliberately shared so cross-library
feedback works); and vector's TIME-domain control vocabulary (intents, phase
names, stagger patterns, roles, behaviour families).
"""
from __future__ import annotations

from collections import defaultdict

import pytest

from lumenframe.camera.camera import MOVES as CAM_MOVES
from lumenframe.camera.styles import camera_styles
from lumenframe.compose.framing import GRIDS
from lumenframe.compose.styles import FRAMINGS
from lumenframe.edit.grammar import TRANSITIONS as EDIT_TRANS
from lumenframe.edit.styles import STYLES as EDIT_STYLES
from lumenframe.elements import element_names
from lumenframe.grade.grade import REGISTRY as GRADE_OPS
from lumenframe.grade.styles import STYLES as GRADE_STYLES
from lumenframe.kinetic.styles import STYLES as KIN_STYLES
from lumenframe.kinetic.typography import LAYOUTS as KIN_LAYOUTS, REVEALS as KIN_REVEALS
from lumenframe.rhythm.rhythm import SYNC as RHY_SYNC
from lumenframe.rhythm.styles import BOOK as RHY_STYLES
from lumenframe.templates import template_names
from lumenframe.vector.behaviors import behavior_names as vector_behavior_names
from lumenframe.vector.styles import style_names as vector_style_names

#: Known, adjudication-pending cross-library collisions. Each entry is a
#: DELIBERATE record, not an excuse — see the note per group.
_KNOWN_CROSS_LIBRARY_COLLISIONS: dict[str, set[str]] = {
    # (a) Style-name overlaps — the same aesthetic adjective applied in two
    # different domains. A "documentary" project legitimately wants a
    # documentary EDIT and a documentary CAMERA; the token is always scoped by
    # which verb the model calls, so a brief never resolves ambiguously.
    # Likely intentional shared vocabulary; kept visible pending adjudication.
    "documentary": {"camera", "edit"},
    "energetic": {"camera", "edit"},
    "minimal": {"kinetic", "vector"},
    # (b) Catalog-key overlaps — the SHARPER case: kinetic_type's text LAYOUTS
    # and the Shape-B template library both name the same artifact, so "make a
    # lower third" has two library answers. Flagged for a design call (rename,
    # or fold one face into the other).
    "caption": {"kinetic", "templates"},
    "lower_third": {"kinetic", "templates"},
    "title_card": {"kinetic", "templates"},
}


def _unique_namespace() -> dict[str, set[str]]:
    """The must-be-globally-unique token set per library: style/look/framing
    archetypes ∪ closed CATALOG keys. Read from the live registries so the gate
    self-updates as libraries evolve."""
    return {
        "vector": set(vector_style_names()) | set(vector_behavior_names()),
        "grade": set(GRADE_STYLES.names()) | set(GRADE_OPS.names()),
        "kinetic": (set(KIN_STYLES.names()) | set(KIN_LAYOUTS.names())
                    | set(KIN_REVEALS.names())),
        "edit": set(EDIT_STYLES.names()) | set(EDIT_TRANS.names()),
        "camera": set(camera_styles().names()) | set(CAM_MOVES.names()),
        "compose": set(FRAMINGS.names()) | set(GRIDS.names()),
        "rhythm": set(RHY_STYLES.names()) | set(RHY_SYNC.names()),
        "templates": set(template_names()),
        "elements": set(element_names()),
    }


def _collisions(sets: dict[str, set[str]]) -> dict[str, set[str]]:
    """Tokens appearing in 2+ DISTINCT libraries. Intra-library reuse (rhythm
    style==sync pattern, camera style==move, compose framing==grid) is
    intentional and never flagged — each library contributes one deduped set."""
    owners: dict[str, set[str]] = defaultdict(set)
    for lib, tokens in sets.items():
        for token in tokens:
            owners[token].add(lib)
    return {tok: libs for tok, libs in owners.items() if len(libs) >= 2}


def test_cross_library_token_collisions_are_the_known_bounded_set() -> None:
    """The archetype/catalog namespace collides ONLY where explicitly recorded.
    A new collision (or a resolved one) turns this red — no silent drift."""
    actual = _collisions(_unique_namespace())
    assert actual == _KNOWN_CROSS_LIBRARY_COLLISIONS, (
        "cross-library token collisions drifted from the bounded known set.\n"
        f"  unexpected: { {k: sorted(v) for k, v in actual.items() if k not in _KNOWN_CROSS_LIBRARY_COLLISIONS} }\n"
        f"  resolved (remove from the known set): { {k: sorted(v) for k, v in _KNOWN_CROSS_LIBRARY_COLLISIONS.items() if k not in actual} }\n"
        "A NEW collision means two libraries now name the same thing — rename "
        "one or namespace it (charter P3/G14)."
    )


def test_every_library_contributes_a_nonempty_namespace() -> None:
    """Anti-vacuity: an import regression that emptied a registry would make the
    collision check trivially pass."""
    ns = _unique_namespace()
    assert len(ns) == 9, f"expected 9 model-facing libraries, got {sorted(ns)}"
    for lib, tokens in ns.items():
        assert tokens, f"library {lib!r} contributed no tokens — registry broken?"


def test_shared_control_vocabulary_is_not_in_the_unique_namespace() -> None:
    """The op discriminators, semantic axes and TIME-domain control words are
    shared BY DESIGN and must never be folded into the unique namespace (doing
    so would flag ~every library against every other)."""
    ns = _unique_namespace()
    flat_collisions = _collisions(ns)
    # op discriminators — shared by every Shape-A verb
    for op in ("create", "adjust", "catalog"):
        assert op not in flat_collisions, f"op discriminator {op!r} must be excluded"
    # semantic axes — deliberately shared so cross-library feedback works
    for axis in ("energy", "drama", "elegance", "pace", "tightness"):
        assert axis not in flat_collisions, f"semantic axis {axis!r} must be excluded"
    # vector TIME-domain control vocabulary (intents / phases / roles)
    for word in ("intro", "loop", "outro", "transition", "focal"):
        assert word not in flat_collisions, f"control word {word!r} must be excluded"


def test_collision_detector_is_nontrivial() -> None:
    """Anti-triviality: the detector DOES report a synthetic cross-library
    duplicate, and does NOT report intentional intra-library reuse."""
    synthetic = {"lib_a": {"halo", "shared_token"}, "lib_b": {"shared_token"}}
    assert _collisions(synthetic) == {"shared_token": {"lib_a", "lib_b"}}
    # intra-library duplication is impossible to flag (one set per library)
    assert _collisions({"lib_a": {"halo"}, "lib_b": {"orbit"}}) == {}


@pytest.mark.parametrize("token,libs", sorted(_KNOWN_CROSS_LIBRARY_COLLISIONS.items()))
def test_each_known_collision_still_reproduces(token: str, libs: set[str]) -> None:
    """Each recorded collision is real and still owned by exactly those
    libraries — the known set may not rot into fiction."""
    ns = _unique_namespace()
    owners = {lib for lib, tokens in ns.items() if token in tokens}
    assert owners == libs, f"{token!r} is now owned by {sorted(owners)}, not {sorted(libs)}"
