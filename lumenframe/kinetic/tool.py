"""``kinetic_type`` — the kinetic typography engine as ONE agent tool.

Single tool, op discriminator (the ``vector_motion`` pattern):

* ``op:"create"``  — a text brief → a choreographed text scene → a
  self-contained animated SVG, returned with its explainable plan. The brief is
  echoed back on ``brief`` so a caller can persist it and adjust later.
* ``op:"adjust"``  — human feedback ("bolder", "更紧凑") against a stored brief:
  folds semantic deltas in and rebuilds deterministically (same seed) — never
  SVG text surgery.
* ``op:"catalog"`` — the creative vocabulary (layouts, reveals, styles, axes,
  feelings) to compose briefs from.

Pure and side-effect-free: it validates, routes and returns plain dicts via the
shared :func:`~lumenframe.craft.tool.dispatch` helper. The gemia adapter that
writes the SVG to a doc's ``html`` layer is a separate thin layer, exactly as
``gemia/tools/vector_motion.py`` wraps ``vector.api``.
"""
from __future__ import annotations

from typing import Any

from lumenframe.craft import err, ok, tool_dispatch

from lumenframe.kinetic.api import adjust as _adjust
from lumenframe.kinetic.api import build as _build
from lumenframe.kinetic.catalog import kinetic_catalog

TOOL = "kinetic_type"


def _plan_reply(result: dict[str, Any], **extra: Any) -> dict[str, Any]:
    """Shape a create/adjust result into a compact, agent-friendly reply."""
    scene = result["scene"]
    return ok(
        svg=result["svg"],
        svg_bytes=len(result["svg"].encode("utf-8")),
        scene_digest=scene["digest"],
        layout=scene["layout"],
        style=scene["style"],
        duration=scene["duration"],
        plan=result["plan"],
        notes=result["notes"],
        **extra,
    )


def _create(args: dict[str, Any]) -> dict[str, Any]:
    brief = args.get("brief")
    if not isinstance(brief, dict):
        return err("E_ARG", f"{TOOL} create: 'brief' must be an object "
                            "(see op:'catalog' for the vocabulary)")
    try:
        result = _build(brief)
    except ValueError as exc:
        return err("E_ARG", f"{TOOL} create: {exc}", recovery="fix_args")
    return _plan_reply(result, next="adjust with op:'adjust' + feedback phrases")


def _adjust_op(args: dict[str, Any]) -> dict[str, Any]:
    brief = args.get("brief")
    feedback = args.get("feedback")
    if not isinstance(brief, dict):
        return err("E_ARG", f"{TOOL} adjust: 'brief' (the stored brief) is required")
    if not isinstance(feedback, list) or not feedback:
        return err("E_ARG", f"{TOOL} adjust: 'feedback' must be a non-empty list "
                            "of phrases like 'bolder' / '更紧凑'")
    try:
        result = _adjust(brief, [str(p) for p in feedback])
    except ValueError as exc:
        return err("E_ARG", f"{TOOL} adjust: {exc}", recovery="fix_args")
    return _plan_reply(result, brief=result["brief"])


async def dispatch(args: dict[str, Any], ctx: Any = None) -> dict[str, Any]:
    """Route a ``kinetic_type`` call by ``op`` (create | adjust | catalog)."""
    return tool_dispatch(
        args, tool=TOOL, catalog_fn=kinetic_catalog,
        create=_create, adjust=_adjust_op,
    )
