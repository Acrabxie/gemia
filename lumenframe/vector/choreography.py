"""Composition intelligence — phases, stagger, focal order, enforced holds.

A scene is not a bag of animations. It is planned as a **phase arc**:

    anticipation → entrance → emphasis → hold          (intent: reveal/intro)
    entrance → cycle → hold                            (intent: loop)
    anticipation → entrance → emphasis → exit          (intent: transition)

The allocator turns (duration, intent, params) into concrete phase windows.
The final ``hold`` is *enforced* from ``params.hold_fraction`` — 留白 is a
budget line, not an aspiration. Behaviours receive their phase window and must
stay inside it.

Stagger patterns compute per-member normalised delays (0..1 of the stagger
spread) for groups and particle fields:

* ``sequential``  — reading order (index order)
* ``center_out``  — focal centre first, edges last
* ``edges_in``    — frame first, centre lands last (good for logo landings)
* ``random``      — seeded shuffle (playful scatter)

Focal order: nodes are ranked primary → secondary → decoration. Entrances
lead with context and LAND on the focal element; decoration never animates
after the focal hold begins.
"""
from __future__ import annotations

import random
from typing import Any, Sequence

from lumenframe.vector.params import ResolvedParams

PHASE_NAMES: set[str] = {"anticipation", "entrance", "emphasis", "cycle", "hold", "exit"}

STAGGER_PATTERNS: set[str] = {"sequential", "center_out", "edges_in", "random"}

#: intent → ordered phase recipe with weight shares of the *animated* span
#: (hold is carved out first from hold_fraction; weights normalise over rest).
INTENT_ARCS: dict[str, list[tuple[str, float]]] = {
    "reveal": [("anticipation", 0.18), ("entrance", 0.52), ("emphasis", 0.30)],
    "intro": [("anticipation", 0.15), ("entrance", 0.55), ("emphasis", 0.30)],
    # A quick lead-in, then the cycle owns the rest and must start/end at rest
    # so the clip loops seamlessly — no trailing hold (a freeze would pop on
    # every repeat).
    "loop": [("entrance", 0.16), ("cycle", 0.84)],
    # A transition MUST leave: it ends in ``exit`` so the outgoing subject is
    # gone by the end (its contract is to hand off, not park on screen).
    "transition": [("entrance", 0.4), ("emphasis", 0.22), ("exit", 0.38)],
    "outro": [("emphasis", 0.3), ("exit", 0.7)],
}

#: Intents that end at rest and deserve a trailing negative-space hold. Arcs
#: ending in ``exit`` never hold (nothing is left to sit still), and ``loop``
#: must not hold or the repeat would jump.
_HOLDING_INTENTS: frozenset[str] = frozenset({"reveal", "intro"})


class ChoreographyError(ValueError):
    """Raised for unknown intents/patterns or impossible windows."""


def phase_windows(
    *,
    duration: float,
    intent: str = "reveal",
    params: ResolvedParams,
) -> list[dict[str, Any]]:
    """Allocate phase windows over ``duration`` for an intent.

    Returns ``[{"name", "t0", "t1"}, …]`` covering [0, duration] exactly.
    ``hold`` is appended for arcs that don't end in ``exit`` and consumes
    ``params.hold_fraction`` of the total; energy squeezes anticipation
    (a high-energy scene barely waits).
    """
    arc = INTENT_ARCS.get(str(intent))
    if arc is None:
        raise ChoreographyError(f"unknown intent {intent!r} (use {sorted(INTENT_ARCS)})")
    duration = float(duration)
    if duration <= 0:
        raise ChoreographyError("duration must be > 0")

    holds = str(intent) in _HOLDING_INTENTS
    hold = duration * params.hold_fraction if holds else 0.0
    animated = duration - hold

    weights: dict[str, float] = dict(arc)
    if "anticipation" in weights:
        # High energy compresses anticipation to as little as 40% of its share.
        squeeze = 0.4 + 0.6 * (1.0 - params.axes["energy"])
        weights["anticipation"] *= squeeze
    total = sum(weights[name] for name, _ in arc)

    windows: list[dict[str, Any]] = []
    cursor = 0.0
    for name, _ in arc:
        span = animated * (weights[name] / total)
        windows.append({"name": name, "t0": round(cursor, 6), "t1": round(cursor + span, 6)})
        cursor += span
    if hold > 0:
        windows.append({"name": "hold", "t0": round(cursor, 6), "t1": round(duration, 6)})
    else:
        windows[-1]["t1"] = round(duration, 6)
    return windows


def window_of(windows: Sequence[dict[str, Any]], name: str) -> tuple[float, float] | None:
    for w in windows:
        if w["name"] == name:
            return (float(w["t0"]), float(w["t1"]))
    return None


# ── stagger ──────────────────────────────────────────────────────────────


def stagger_delays(
    count: int,
    *,
    pattern: str = "sequential",
    rng: random.Random | None = None,
    positions: Sequence[tuple[float, float]] | None = None,
    center: tuple[float, float] = (0.0, 0.0),
) -> list[float]:
    """Normalised delays (0..1) for ``count`` members under a pattern.

    ``center_out`` / ``edges_in`` rank by distance from ``center`` when
    ``positions`` are given (particle fields), by index distance otherwise.
    ``random`` needs ``rng`` (seeded) — determinism is on the caller.
    The returned delays span exactly [0, 1] (single member ⇒ [0]).
    """
    if pattern not in STAGGER_PATTERNS:
        raise ChoreographyError(f"unknown stagger pattern {pattern!r} (use {sorted(STAGGER_PATTERNS)})")
    n = max(0, int(count))
    if n == 0:
        return []
    if n == 1:
        return [0.0]

    if pattern == "sequential":
        order = list(range(n))
    elif pattern == "random":
        if rng is None:
            raise ChoreographyError("random stagger requires a seeded rng")
        order = list(range(n))
        rng.shuffle(order)
    else:
        if positions is not None:
            if len(positions) != n:
                raise ChoreographyError("positions length must match count")
            dist = [((p[0] - center[0]) ** 2 + (p[1] - center[1]) ** 2) for p in positions]
        else:
            mid = (n - 1) / 2.0
            dist = [abs(i - mid) for i in range(n)]
        ranked = sorted(range(n), key=lambda i: (dist[i], i))
        order = ranked if pattern == "center_out" else ranked[::-1]

    delays = [0.0] * n
    for rank, member in enumerate(order):
        delays[member] = rank / (n - 1)
    return delays


# ── focal order ──────────────────────────────────────────────────────────

#: Node roles, most important first. Stored in node["meta"]["role"].
ROLES: tuple[str, ...] = ("focal", "secondary", "decoration", "background")


def assign_roles(nodes: Sequence[dict[str, Any]], *, focal_id: str | None = None) -> None:
    """Stamp ``meta.role`` on nodes: explicit focal wins, else the largest
    text node, else the last (top-most) path node. Everything already stamped
    keeps its role — builders may pre-assign."""
    unstamped = [n for n in nodes if not (n.get("meta") or {}).get("role")]
    if not unstamped:
        return
    focal: dict[str, Any] | None = None
    if focal_id is not None:
        focal = next((n for n in unstamped if str(n.get("id")) == str(focal_id)), None)
    if focal is None:
        texts = [n for n in unstamped if n.get("kind") == "text"]
        if texts:
            focal = max(texts, key=lambda n: float((n.get("text") or {}).get("font_size") or 0))
    if focal is None:
        paths = [n for n in unstamped if n.get("kind") in ("path", "group")]
        focal = paths[-1] if paths else unstamped[-1]
    for node in unstamped:
        meta = node.setdefault("meta", {})
        if node is focal:
            meta["role"] = "focal"
        elif node.get("kind") == "particles":
            meta["role"] = "decoration"
        else:
            meta["role"] = "secondary"


def entrance_order(nodes: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Context first, focal LANDS last; stable within a role."""
    rank = {"background": 0, "decoration": 1, "secondary": 2, "focal": 3}
    return sorted(
        nodes,
        key=lambda n: rank.get((n.get("meta") or {}).get("role") or "secondary", 2),
    )
