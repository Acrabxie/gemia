"""``compose_frame`` — the composition/framing engine as ONE agent tool.

Single tool, op discriminator (the ``update_quantum`` / ``vector_motion``
pattern):

* ``op:"create"``  — a framing brief → a reframe recipe (crop, scale, anchor,
  guides) + a self-contained guide-overlay SVG. The recipe drives the
  transform/crop layer; the overlay is a preview aid.
* ``op:"adjust"``  — human feedback ("more tension", "留白多一点", "tighter")
  against a stored brief: folds semantic deltas in and re-composes with the same
  seed — a re-composition, never a nudge of the numbers.
* ``op:"catalog"`` — the framing vocabulary (framings, grids, axes, feedback
  words) for the model to compose briefs from.

Pure: this module needs no gemia import and returns plain dicts via ``ok()`` /
``err()``. The gemia adapter that writes the crop to a doc/timeline is a
separate, thin layer, exactly as ``gemia/tools/vector_motion.py`` wraps
``vector.api``.
"""
from __future__ import annotations

from typing import Any

from lumenframe.craft import err, ok, tool_dispatch

from lumenframe.compose.api import adjust_frame, build_frame
from lumenframe.compose.catalog import compose_catalog
from lumenframe.compose.render import compose_overlay_svg, validate_overlay

_TOOL = "compose_frame"


def _canvas_px(brief: dict[str, Any], plan: dict[str, Any]) -> tuple[int, int]:
    """The overlay canvas: the brief's explicit pixels, else a 1080p-tall frame
    at the delivered aspect."""
    canvas = brief.get("canvas") if isinstance(brief, dict) else None
    if isinstance(canvas, dict) and canvas.get("width") and canvas.get("height"):
        return int(canvas["width"]), int(canvas["height"])
    aspect = float(plan.get("target_aspect") or (16.0 / 9.0))
    height = 1080
    return max(1, round(height * aspect)), height


def _with_overlay(brief: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    """Attach a validated guide-overlay SVG to a build/adjust result."""
    width, height = _canvas_px(brief, result["plan"])
    svg = compose_overlay_svg(result["reframe"], width=width, height=height)
    validate_overlay(svg)
    return {
        "reframe": result["reframe"],
        "plan": result["plan"],
        "overlay_svg": svg,
        "overlay_bytes": len(svg),
        "notes": result["notes"],
    }


def _create(args: dict[str, Any]) -> dict[str, Any]:
    brief = args.get("brief")
    if not isinstance(brief, dict):
        return err("E_ARG", f"{_TOOL} create: 'brief' must be an object "
                            "(see op:'catalog' for the vocabulary)")
    try:
        result = build_frame(brief)
    except ValueError as exc:
        return err("E_ARG", f"{_TOOL} create: {exc}", recovery="fix_args")
    try:
        payload = _with_overlay(brief, result)
    except ValueError as exc:
        return err("E_RENDER", f"{_TOOL} create: overlay not render-safe: {exc}")
    return ok(**payload, next="apply the crop via the transform layer; "
                             "adjust with op:'adjust' + feedback phrases")


def _adjust(args: dict[str, Any]) -> dict[str, Any]:
    brief = args.get("brief")
    feedback = args.get("feedback")
    if not isinstance(brief, dict):
        return err("E_ARG", f"{_TOOL} adjust: 'brief' must be the stored object")
    if not isinstance(feedback, list) or not feedback:
        return err("E_ARG", f"{_TOOL} adjust: 'feedback' must be a non-empty list "
                            "of phrases like 'more tension' / '紧凑一点'")
    try:
        result = adjust_frame(brief, [str(p) for p in feedback])
    except ValueError as exc:
        return err("E_ARG", f"{_TOOL} adjust: {exc}", recovery="fix_args")
    new_brief = result.get("brief", brief)
    try:
        payload = _with_overlay(new_brief, result)
    except ValueError as exc:
        return err("E_RENDER", f"{_TOOL} adjust: overlay not render-safe: {exc}")
    return ok(**payload, brief=new_brief,
              next="re-render to preview the new framing")


async def dispatch(args: dict[str, Any], ctx: Any = None) -> dict[str, Any]:
    """Route a ``compose_frame`` call by ``op`` (create | adjust | catalog)."""
    return tool_dispatch(
        args, tool=_TOOL, catalog_fn=compose_catalog,
        create=_create, adjust=_adjust,
    )
