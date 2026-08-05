"""Plan → timeline adapter + validation — riding the existing timeline layer.

Rhythm is a *plan-only* library: it does not fork a renderer, it emits a cut plan
that the existing timeline layer executes. This module is the thin, spec'd
adapter from a ``score`` (as returned by :func:`lumenframe.rhythm.api.build`) to a
list of **timeline cut operations**, plus :func:`validate_cut_plan`, which
rejects a plan the timeline could not safely play (out-of-order cuts,
seizure-fast shots, cuts past the audio, non-numeric times).

The op contract (what the gemia timeline adapter consumes) is deliberately tiny
and declarative — one op per cut point::

    {"op": "cut", "t": 1.875, "clip": "c2", "bar": 1, "beat": 1, "accent": true}

* ``t``      — wall-clock seconds where a new shot begins (the split point).
* ``clip``   — the clip id that starts at ``t`` (omitted if the brief had no
  clips; the timeline then keeps whatever clip already occupies the lane).
* ``accent`` — whether this cut lands on a strong beat, so the adapter can, e.g.,
  add a flash/impact transition only on accents.

The first cut (``t`` == grid start) marks the first shot's IN point; each
subsequent op is a split at ``t``. The adapter maps these onto the timeline's
existing ``split_clip`` / ``insert_clip`` verbs — this module never touches a
live session.
"""
from __future__ import annotations

from typing import Any

from lumenframe.rhythm.rhythm import MIN_SHOT_SECONDS

#: A generous safety ceiling: a single build should never emit more cut ops than
#: this (a 10-minute double-time edit is well under it). Guards against a bad
#: brief producing a pathological plan the timeline would choke on.
MAX_CUTS: int = 4000


class CutPlanError(ValueError):
    """Raised when a cut plan is not safe to hand to the timeline."""


def plan_to_timeline_ops(score: dict[str, Any]) -> list[dict[str, Any]]:
    """Map a validated ``score`` to a list of timeline cut ops.

    Pure and side-effect free: returns the ops the gemia timeline adapter will
    translate into ``split_clip`` / ``insert_clip`` calls. Validates first so a
    malformed plan cannot reach the timeline.
    """
    validate_cut_plan(score)
    ops: list[dict[str, Any]] = []
    for cut in score["cut_plan"]:
        op = {
            "op": "cut",
            "t": round(float(cut["t"]), 6),
            "bar": int(cut["bar"]),
            "beat": int(cut["beat"]),
            "accent": bool(cut.get("accent")),
        }
        if cut.get("clip") is not None:
            op["clip"] = cut["clip"]
        ops.append(op)
    return ops


def validate_cut_plan(score: dict[str, Any]) -> None:
    """Assert a cut plan is safe to play, raising :class:`CutPlanError` if not.

    Checks the invariants the timeline relies on:

    * times are finite, non-negative numbers;
    * cuts are strictly increasing (a timeline cannot cut backwards);
    * no shot is shorter than the seizure floor;
    * the plan is not pathologically long;
    * every cut sits within the grid's total duration.
    """
    if not isinstance(score, dict) or not isinstance(score.get("cut_plan"), list):
        raise CutPlanError("score must be a dict with a 'cut_plan' list")
    cuts = score["cut_plan"]
    if len(cuts) > MAX_CUTS:
        raise CutPlanError(f"cut plan has {len(cuts)} cuts (max {MAX_CUTS})")

    spb = float(score.get("seconds_per_beat") or 0.0)
    total_beats = int(score.get("total_beats") or 0)
    grid_end = spb * total_beats if spb and total_beats else None

    last_t: float | None = None
    for i, cut in enumerate(cuts):
        t = cut.get("t")
        if not isinstance(t, (int, float)) or t != t or t < 0:  # NaN-safe
            raise CutPlanError(f"cut {i} has an invalid time {t!r}")
        t = float(t)
        if last_t is not None:
            if t <= last_t:
                raise CutPlanError(f"cut {i} at {t} is not after the previous cut {last_t}")
            if (t - last_t) < MIN_SHOT_SECONDS - 1e-9:
                raise CutPlanError(
                    f"cut {i} makes a shot shorter than the {MIN_SHOT_SECONDS}s floor")
        if grid_end is not None and t > grid_end + spb + 1e-6:
            raise CutPlanError(f"cut {i} at {t} lies past the grid end {grid_end}")
        last_t = t
