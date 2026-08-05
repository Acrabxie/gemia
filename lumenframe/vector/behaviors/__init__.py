"""Behaviour registry — the high-level motion vocabulary.

A **behaviour** is a named, registered, pure function that choreographs one
visual idea over a set of nodes inside a time window::

    @behavior("reveal.draw_on", family="reveal",
              summary="strokes draw themselves in")
    def draw_on(scene, nodes, window, level, rng):
        ...

Contract (enforced by tests, relied on by the compiler):

* ``scene``  — the whole VectorScene (context; e.g. canvas size).
* ``nodes``  — the target nodes, already in entrance order. A behaviour
  mutates ONLY these nodes' tracks (and, for ``particles`` nodes, their
  instances' tracks).
* ``window`` — ``(t0, t1)`` absolute seconds. Every keyframe a behaviour
  writes must satisfy ``t0 <= t <= t1``.
* ``level``  — :class:`lumenframe.vector.params.ResolvedParams`; behaviours
  read semantics (``level.overshoot``, ``level.tempo``, ``level.hints``…)
  and never invent their own numbers for those concepts.
* ``rng``    — the scene's seeded ``random.Random``. Module-level randomness
  is a bug; determinism is part of the contract.

Behaviours compose the track builders in :mod:`lumenframe.vector.motion`;
they do not hand-write keyframe dicts.

Discovery mirrors the template/element pattern: :data:`BEHAVIORS` (name →
fn), :data:`BEHAVIOR_CATALOG` (metadata the agent reads), and
:func:`describe_behaviors` (compact prompt block). A test pins the catalog
to real registrations so docs cannot drift.
"""
from __future__ import annotations

import random
from typing import Any, Callable, Protocol, Sequence

from lumenframe.vector.params import ResolvedParams

__all__ = [
    "BEHAVIORS",
    "BEHAVIOR_CATALOG",
    "behavior",
    "apply_behavior",
    "behavior_names",
    "describe_behaviors",
    "BehaviorError",
]


class BehaviorError(ValueError):
    """Unknown behaviour, bad targets, or a contract violation."""


class BehaviorFn(Protocol):
    def __call__(
        self,
        scene: dict[str, Any],
        nodes: Sequence[dict[str, Any]],
        window: tuple[float, float],
        level: ResolvedParams,
        rng: random.Random,
    ) -> None: ...


#: behaviour name ("family.verb") → implementation.
BEHAVIORS: dict[str, BehaviorFn] = {}

#: agent-facing metadata, appended in registration order.
BEHAVIOR_CATALOG: list[dict[str, Any]] = []

FAMILIES: tuple[str, ...] = ("reveal", "explode", "assemble", "flow", "transform")


def behavior(
    name: str,
    *,
    family: str,
    summary: str,
    kinds: tuple[str, ...] = ("path", "text", "group"),
) -> Callable[[BehaviorFn], BehaviorFn]:
    """Register a behaviour. ``kinds`` documents which node kinds it accepts
    (``particles`` behaviours say so explicitly)."""
    if family not in FAMILIES:
        raise ValueError(f"unknown family {family!r} (use {FAMILIES})")
    if not name.startswith(family + "."):
        raise ValueError(f"behaviour name {name!r} must be '{family}.<verb>'")

    def _register(fn: BehaviorFn) -> BehaviorFn:
        if name in BEHAVIORS:
            raise ValueError(f"behaviour {name!r} already registered")
        BEHAVIORS[name] = fn
        BEHAVIOR_CATALOG.append({
            "name": name,
            "family": family,
            "summary": summary,
            "kinds": list(kinds),
        })
        return fn

    return _register


def behavior_names() -> list[str]:
    _load()
    return sorted(BEHAVIORS)


def apply_behavior(
    scene: dict[str, Any],
    name: str,
    nodes: Sequence[dict[str, Any]],
    window: tuple[float, float],
    level: ResolvedParams,
    rng: random.Random,
) -> None:
    """Look up and run a behaviour, validating the window."""
    _load()
    fn = BEHAVIORS.get(str(name))
    if fn is None:
        raise BehaviorError(f"unknown behaviour {name!r} (use {behavior_names()})")
    t0, t1 = float(window[0]), float(window[1])
    if t1 <= t0:
        raise BehaviorError(f"behaviour {name}: empty window {window!r}")
    if not nodes:
        return
    fn(scene, list(nodes), (t0, t1), level, rng)


def describe_behaviors() -> str:
    """Compact agent-prompt block, one line per behaviour, grouped by family."""
    _load()
    lines = ["Vector motion behaviours (family.verb):"]
    for family in FAMILIES:
        verbs = [e for e in BEHAVIOR_CATALOG if e["family"] == family]
        if not verbs:
            continue
        lines.append(f"- {family}: " + "; ".join(
            f"{e['name'].split('.', 1)[1]} ({e['summary']})" for e in verbs
        ))
    return "\n".join(lines)


_LOADED = False


def _load() -> None:
    """Import the family modules exactly once (registration side-effects)."""
    global _LOADED
    if _LOADED:
        return
    _LOADED = True
    from lumenframe.vector.behaviors import (  # noqa: F401
        assemble, explode, flow, reveal, transform,
    )
