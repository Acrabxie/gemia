"""``rhythm_edit`` — the musical-rhythm editor as ONE agent tool.

Single tool, op discriminator (the ``vector_motion`` pattern — no flat-tool
proliferation):

* ``op:"create"``  — a rhythm brief → a beat grid + beat-aligned cut plan. The
  reply carries the full ``score`` (grid + cut plan), a compact ``plan`` digest,
  the ``timeline_ops`` the timeline adapter will execute, and the brief (so the
  plan can be re-derived / adjusted later).
* ``op:"adjust"``  — human feedback ("more driving", "更紧凑") against a prior
  brief: folds semantic deltas into the brief and rebuilds the cut plan
  deterministically (same seed) — never hand-patches cut times.
* ``op:"catalog"`` — the creative vocabulary (styles, sync patterns, feelings,
  feedback phrases) to compose briefs from.

Pure: no gemia import, no live session. The gemia adapter that writes the cut
plan onto a real timeline is a separate, thin layer (exactly as
``gemia/tools/vector_motion.py`` wraps ``vector.api``).
"""
from __future__ import annotations

from typing import Any

from lumenframe.craft import err, ok, tool_dispatch
from lumenframe.rhythm.api import BriefError, adjust as _adjust, build as _build
from lumenframe.rhythm.catalog import rhythm_catalog
from lumenframe.rhythm.render import plan_to_timeline_ops

_TOOL = "rhythm_edit"


def _create(args: dict[str, Any]) -> dict[str, Any]:
    brief = args.get("brief")
    if not isinstance(brief, dict):
        return err("E_ARG", f"{_TOOL} create: 'brief' must be an object "
                            "(see op:'catalog' for the vocabulary)")
    try:
        result = _build(brief)
    except BriefError as exc:
        return err("E_ARG", f"{_TOOL} create: {exc}", recovery="fix_args")
    return ok(
        score=result["score"],
        plan=result["plan"],
        timeline_ops=plan_to_timeline_ops(result["score"]),
        notes=result["notes"],
        next="preview the cut plan on the timeline; adjust with op:'adjust' + feedback phrases",
    )


def _adjust_op(args: dict[str, Any]) -> dict[str, Any]:
    brief = args.get("brief")
    feedback = args.get("feedback")
    if not isinstance(brief, dict):
        return err("E_ARG", f"{_TOOL} adjust: 'brief' (the prior brief) must be an object")
    if not isinstance(feedback, list) or not feedback:
        return err("E_ARG", f"{_TOOL} adjust: 'feedback' must be a non-empty list of "
                            "phrases like 'more driving' / '更紧凑'")
    try:
        result = _adjust(brief, [str(p) for p in feedback])
    except BriefError as exc:
        return err("E_ARG", f"{_TOOL} adjust: {exc}", recovery="fix_args")
    return ok(
        brief=result["brief"],
        score=result["score"],
        plan=result["plan"],
        timeline_ops=plan_to_timeline_ops(result["score"]),
        notes=result["notes"],
        next="preview the new cut plan; adjust again or accept",
    )


async def dispatch(args: dict[str, Any], ctx: Any = None) -> dict[str, Any]:
    """Route a ``rhythm_edit`` call by ``op`` (create | adjust | catalog).

    ``ctx`` is accepted for signature parity with session tools but unused — this
    surface is pure. Errors come back as uniform ``err()`` dicts.
    """
    return tool_dispatch(
        args,
        tool=_TOOL,
        catalog_fn=rhythm_catalog,
        create=_create,
        adjust=_adjust_op,
    )
