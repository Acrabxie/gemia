"""Plan → timeline adapter spec — how a cut plan lowers to real timeline ops.

The edit library rides the **timeline** layer: it never touches a live session,
it emits a pure recipe (the cut plan). This module is the documented, testable
bridge from that recipe to the timeline patch ops the renderer already
understands — ``add_transition`` and ``trim`` (see
``lumenframe.ops`` / ``lumenframe.catalog``). It does two jobs:

* :func:`validate_cut_plan` — reject a plan that would render badly (unknown
  transition, negative or oversized duration, a straight join carrying a
  duration, an over-long plan) *before* any op reaches a document.
* :func:`plan_to_timeline_ops` — map each join to concrete ``add_transition`` /
  ``trim`` op dicts, given the clip→layer-id mapping. This is a *spec*: it
  returns plain op dicts, so it can be unit-tested and reviewed with no live
  timeline. :data:`ADAPTER_SPEC` documents the transition→renderer-kind lowering
  for inspection.

The renderer exposes only ``fade / dissolve / wipe_* / slide`` kinds, so the
creative vocabulary lowers onto them: dips become a coloured ``fade``, whip pans
a ``slide``, wipes a ``wipe_l``; straight and match cuts emit *no* transition op
(that is the point — a cut is the absence of a transition).
"""
from __future__ import annotations

from typing import Any

from lumenframe.edit.grammar import MAX_TRANSITION_MS, TRANSITIONS, transition_meta

#: Hard ceiling on plan size the validator enforces (a sane sequence length).
MAX_JOINS = 2000


class EditRenderError(ValueError):
    """Raised when a cut plan cannot be safely lowered to timeline ops."""


#: The transition→renderer lowering, for inspection and prompts. Straight joins
#: map to ``None`` (no op emitted); coloured dips carry a ``color`` the renderer
#: may honour, degrading to a plain fade if it cannot.
ADAPTER_SPEC: dict[str, dict[str, Any]] = {
    e["name"]: {
        "renders_as": e["renders_as"],
        "straight": e["straight"],
        "color": e.get("color"),
    }
    for e in TRANSITIONS.catalog()
}


def validate_cut_plan(cut_plan: list[dict[str, Any]]) -> None:
    """Reject a structurally-unsafe or oversized plan (raise :class:`EditRenderError`).

    Enforces the invariants the renderer depends on: known transition names, a
    bounded plan length, non-negative durations that never exceed
    :data:`~lumenframe.edit.grammar.MAX_TRANSITION_MS`, and the core rule that a
    *straight* join (cut / match_cut) carries no transition duration at all.
    """
    if not isinstance(cut_plan, list) or not cut_plan:
        raise EditRenderError("cut plan must be a non-empty list")
    if len(cut_plan) > MAX_JOINS:
        raise EditRenderError(f"cut plan too long ({len(cut_plan)} > {MAX_JOINS} joins)")
    known = set(TRANSITIONS.names())
    for i, e in enumerate(cut_plan):
        name = e.get("transition")
        if name not in known:
            raise EditRenderError(f"join #{i}: unknown transition {name!r}")
        dur = int(e.get("duration_ms") or 0)
        if dur < 0 or dur > MAX_TRANSITION_MS:
            raise EditRenderError(f"join #{i}: duration_ms {dur} out of range")
        if transition_meta(name)["straight"] and dur != 0:
            raise EditRenderError(f"join #{i}: straight cut {name!r} must have duration_ms 0")


def plan_to_timeline_ops(
    cut_plan: list[dict[str, Any]],
    clip_layer_ids: dict[str, str],
) -> list[dict[str, Any]]:
    """Lower a validated cut plan to ``add_transition`` / ``trim`` op dicts.

    ``clip_layer_ids`` maps each brief clip id to the timeline layer id it became
    once the clips were laid down. For each join we emit, in order:

    * a ``trim`` on the outgoing layer when ``trim_out_adjust`` is set, and one
      on the incoming layer when ``trim_in_adjust`` is set (frames/seconds are
      the caller's unit — we pass a millisecond delta the caller resolves);
    * an ``add_transition`` on the *incoming* layer's ``in`` edge for any
      non-straight transition, carrying the lowered renderer ``kind`` (and a
      ``color`` for dips). Straight cuts and match cuts emit no transition op.

    Returns plain dicts — no document is touched. Missing layer ids are skipped
    with the join left cut-only, so a partial mapping degrades safely.
    """
    validate_cut_plan(cut_plan)
    ops: list[dict[str, Any]] = []
    for e in cut_plan:
        from_id = clip_layer_ids.get(e["from_clip"])
        to_id = clip_layer_ids.get(e["to_clip"])
        if e.get("trim_out_adjust") and from_id:
            ops.append({"op": "trim", "layer_id": from_id, "edge": "out",
                        "delta_ms": int(e["trim_out_adjust"])})
        if e.get("trim_in_adjust") and to_id:
            ops.append({"op": "trim", "layer_id": to_id, "edge": "in",
                        "delta_ms": int(e["trim_in_adjust"])})
        meta = transition_meta(e["transition"])
        if not meta["straight"] and meta["renders_as"] and to_id:
            op: dict[str, Any] = {
                "op": "add_transition",
                "layer_id": to_id,
                "kind": meta["renders_as"],
                "duration": round(int(e["duration_ms"]) / 1000.0, 3),
                "at": "in",
            }
            if meta.get("color"):
                op["color"] = meta["color"]
            ops.append(op)
    return ops
