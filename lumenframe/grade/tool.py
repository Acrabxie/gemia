"""``grade`` — the colour-grading point library as ONE agent tool.

Single tool, op discriminator (the shared ``update_quantum`` / ``vector_motion``
shape — no flat-tool proliferation):

* ``op:"create"``  — a grading brief → a pure grade recipe + a preview SVG + an
  ffmpeg filter string, ready to ride the effect layer.
* ``op:"adjust"``  — human feedback ("more teal", "更暖") against a stored brief:
  folds semantic deltas into the brief and re-derives the grade deterministically
  (same seed). Adjustment is a re-derived grade, never a nudged LUT.
* ``op:"catalog"`` — the creative vocabulary (looks, axes, ops, feelings,
  feedback phrases) for the model to compose briefs from.

The tool is pure (no gemia import): brief in, plain dict out, wrapped with the
shared ``ok()``/``err()`` helpers. The thin session adapter that writes the recipe
onto a doc's effect layer lives elsewhere, exactly as ``vector_motion`` wraps
``vector.api``.
"""
from __future__ import annotations

from typing import Any

from lumenframe.craft import err, ok
from lumenframe.craft.styles import StyleError

from lumenframe.grade.api import BriefError, adjust_grade, build_grade
from lumenframe.grade.catalog import grade_catalog
from lumenframe.grade.render import validate_grade_recipe, validate_grade_svg

_OPS = ("create", "adjust", "catalog")


def _plan_digest(plan: dict[str, Any]) -> dict[str, Any]:
    """The plan, compacted for a tool reply (full curve stays out of the wire)."""
    return {
        "look": plan.get("look"),
        "seed": plan.get("seed"),
        "intensity": plan.get("intensity"),
        "ops": plan.get("ops"),
        "split": plan.get("split"),
        "skin_drift_deg": plan.get("skin_drift_deg"),
        "skin_protected": plan.get("skin_protected"),
        "digest": plan.get("digest"),
    }


async def dispatch(args: dict[str, Any], ctx: Any = None) -> dict[str, Any]:
    """Route a ``grade`` tool call by ``op`` (create | adjust | catalog)."""
    op = str(args.get("op") or "create")
    if op not in _OPS:
        return err("E_ARG", f"grade: unknown op {op!r} (use {', '.join(_OPS)})")
    if op == "catalog":
        return ok(catalog=grade_catalog())
    if op == "create":
        return _create(args)
    return _adjust(args)


def _create(args: dict[str, Any]) -> dict[str, Any]:
    brief = args.get("brief")
    if not isinstance(brief, dict):
        return err("E_ARG", "grade create: 'brief' must be an object "
                            "(see op:'catalog' for the vocabulary)")
    try:
        result = build_grade(brief)
    except (BriefError, StyleError, ValueError) as exc:
        return err("E_ARG", f"grade create: {exc}", recovery="fix_args")
    try:
        validate_grade_recipe(result["recipe"])
        validate_grade_svg(result["preview_svg"])
    except Exception as exc:  # never emit an unsafe / out-of-bounds grade
        return err("E_RENDER", f"grade create: output failed safety validation: {exc}")
    return ok(
        recipe=result["recipe"],
        plan=_plan_digest(result["plan"]),
        preview_svg=result["preview_svg"],
        ffmpeg_filter=result["ffmpeg_filter"],
        notes=result["notes"],
        next="preview the SVG swatch or feed ffmpeg_filter to the render pipeline; "
             "adjust with op:'adjust' + feedback phrases",
    )


def _adjust(args: dict[str, Any]) -> dict[str, Any]:
    brief = args.get("brief")
    feedback = args.get("feedback")
    if not isinstance(brief, dict):
        return err("E_ARG", "grade adjust: 'brief' must be the object to adjust")
    if not isinstance(feedback, list) or not feedback:
        return err("E_ARG", "grade adjust: 'feedback' must be a non-empty list of "
                            "phrases like 'more teal' / '更暖'")
    try:
        result = adjust_grade(brief, [str(p) for p in feedback])
    except (BriefError, StyleError, ValueError) as exc:
        return err("E_ARG", f"grade adjust: {exc}", recovery="fix_args")
    try:
        validate_grade_recipe(result["recipe"])
        validate_grade_svg(result["preview_svg"])
    except Exception as exc:
        return err("E_RENDER", f"grade adjust: output failed safety validation: {exc}")
    return ok(
        recipe=result["recipe"],
        brief=result["brief"],
        plan=_plan_digest(result["plan"]),
        preview_svg=result["preview_svg"],
        ffmpeg_filter=result["ffmpeg_filter"],
        notes=result["notes"],
        next="preview the re-derived grade; adjust again or persist the brief",
    )
