"""``kinetic_type`` — animated titles / kinetic typography as ONE agent verb.

Single tool, op discriminator (the ``vector_motion`` / ``update_quantum`` pattern):

* ``op:"create"`` — a text brief → a typeset, choreographed scene → a
  self-contained animated SVG → an ``html`` layer added to the session's
  lumenframe doc in one atomic patch. ``props.kinetic_brief`` keeps the brief so
  the scene can be re-derived later. Verify with ``lumen_seek`` / ``lumen_render``.
* ``op:"adjust"`` — human feedback ('more energetic', '更优雅') against a kinetic
  layer: folds semantic deltas into the stored brief and re-typesets the SVG
  deterministically (same seed) — adjustment is re-typesetting, never SVG surgery.
* ``op:"catalog"`` — the creative vocabulary (styles, layouts, reveals, feelings,
  feedback phrases) the model composes briefs from.

Rides the SAME core paths as ``vector_motion``: ``layer._lumendoc`` /
``layer._save_lumendoc`` for doc access, the html-layer resolver for rendering —
this module never renders.
"""
from __future__ import annotations

from typing import Any

from gemia.tools._context import ToolContext

try:  # pragma: no cover - exercised via the E_NOT_AVAILABLE branch in tests
    from lumenframe.kinetic.api import adjust as _kinetic_adjust
    from lumenframe.kinetic.api import build as _kinetic_build
    from lumenframe.kinetic.catalog import kinetic_catalog
    from lumenframe.kinetic.render import validate_svg
    from lumenframe.model import new_layer
    from lumenframe.ops import apply_layer_patch
    _IMPORT_ERROR: str | None = None
except ImportError as exc:  # lumenframe optional-dependency convention
    apply_layer_patch = None  # type: ignore[assignment]
    _IMPORT_ERROR = str(exc)

_STAGE_CSS = "#lumeri-stage svg { display: block; }"

#: Layer keys an ``adjust`` carries over — re-typesetting changes the CONTENT
#: (the SVG), never the user's placement/timing/compositing.
_PRESERVED_LAYER_KEYS = (
    "transform", "opacity", "blend_mode", "visible", "locked", "mask", "effects", "lane",
)
_OPS = ("create", "adjust", "catalog")


def _err(code: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"applied": False, "error_code": code, "error_message": message, **extra}


def _default_name(brief: dict[str, Any]) -> str:
    label = brief.get("text") or (brief.get("lines") or [None])[0] or brief.get("layout") or "text"
    return f"Kinetic · {str(label)[:24]}"


def _brief_with_doc_canvas(brief: dict[str, Any], doc: dict[str, Any]) -> dict[str, Any]:
    """Default the brief canvas to the doc canvas so the SVG fills the frame."""
    if brief.get("canvas"):
        return brief
    canvas = doc.get("canvas") if isinstance(doc, dict) else None
    if not isinstance(canvas, dict) or not canvas.get("width") or not canvas.get("height"):
        return brief
    return {**brief, "canvas": {"width": int(canvas["width"]), "height": int(canvas["height"])}}


def _html_layer(result: dict[str, Any], brief: dict[str, Any], *, id: str | None = None,
                name: str | None = None, start: float = 0.0, lane: int = 0) -> dict[str, Any]:
    svg = result.get("svg") or ""
    plan = result.get("plan") or {}
    props: dict[str, Any] = {
        "html": svg,
        "css": _STAGE_CSS,
        "kinetic_scene": {"plan": plan, "duration": plan.get("duration")},
        "kinetic_brief": dict(brief),
    }
    return new_layer(
        "html", id=id, name=name or _default_name(brief),
        start=float(start), duration=float(plan.get("duration") or brief.get("duration") or 5.0),
        lane=int(lane), props=props,
    )


async def dispatch(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    if apply_layer_patch is None:
        return _err("E_NOT_AVAILABLE", f"lumenframe is not importable: {_IMPORT_ERROR}")
    op = str(args.get("op") or "create")
    if op not in _OPS:
        return _err("E_ARG", f"kinetic_type: unknown op {op!r} (use {', '.join(_OPS)})")
    if op == "catalog":
        return {"applied": True, "catalog": kinetic_catalog()}
    if op == "create":
        return await _create(args, ctx)
    return await _adjust(args, ctx)


async def _create(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    from gemia.tools import layer as _layer

    brief = args.get("brief")
    if not isinstance(brief, dict):
        return _err("E_ARG", "kinetic_type create: 'brief' must be an object "
                             "(see op:'catalog' for the vocabulary)")
    doc = _layer._lumendoc(ctx)
    brief = _brief_with_doc_canvas(brief, doc)
    try:
        result = _kinetic_build(brief)
    except ValueError as exc:
        return _err("E_ARG", f"kinetic_type create: {exc}", recovery="fix_args")

    try:
        validate_svg(result.get("svg") or "")
    except Exception as exc:
        return _err("E_RENDER", f"kinetic_type create: generated SVG is not render-safe: {exc}")

    place = args.get("place") if isinstance(args.get("place"), dict) else {}
    html_layer = _html_layer(result, brief, name=place.get("name"),
                             start=float(place.get("start") or 0.0), lane=int(place.get("lane") or 0))
    try:
        new_doc = apply_layer_patch(
            doc, {"version": 1, "ops": [{"op": "add_layer", "type": "html", "layer": html_layer}]})
    except Exception as exc:
        return _err(getattr(exc, "code", "E_UNKNOWN"), str(exc))
    _layer._save_lumendoc(ctx, new_doc)
    return {
        "applied": True,
        "layer_id": html_layer["id"],
        "layer_name": html_layer["name"],
        "duration": html_layer["duration"],
        "svg_bytes": len(html_layer["props"]["html"]),
        "plan": result.get("plan"),
        "notes": result.get("notes", []),
        "next": "lumen_seek a frame or lumen_render_range to verify; adjust with op:'adjust' + feedback",
    }


async def _adjust(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    from gemia.tools import layer as _layer
    from lumenframe.model import find_layer, locate

    layer_id = str(args.get("layer_id") or "")
    feedback = args.get("feedback")
    if not layer_id:
        return _err("E_ARG", "kinetic_type adjust: 'layer_id' is required")
    if not isinstance(feedback, list) or not feedback:
        return _err("E_ARG", "kinetic_type adjust: 'feedback' must be a non-empty list of phrases "
                             "like 'more energetic' / '更优雅'")
    doc = _layer._lumendoc(ctx)
    target = find_layer(doc, layer_id)
    if target is None:
        return _err("E_NOT_FOUND", f"kinetic_type adjust: no layer {layer_id!r}")
    brief = (target.get("props") or {}).get("kinetic_brief")
    if not isinstance(brief, dict):
        return _err("E_ARG", f"kinetic_type adjust: layer {layer_id!r} carries no kinetic_brief "
                             "(only kinetic_type-created layers adjust)")
    try:
        result = _kinetic_adjust(brief, [str(p) for p in feedback])
    except ValueError as exc:
        return _err("E_ARG", f"kinetic_type adjust: {exc}", recovery="fix_args")

    refreshed = _html_layer(result, result.get("brief", brief), id=target.get("id"),
                            name=target.get("name"), start=float(target.get("start") or 0.0),
                            lane=int(target.get("lane") or 0))
    for key in _PRESERVED_LAYER_KEYS:
        if key in target and target[key] is not None:
            refreshed[key] = target[key]
    refreshed["props"] = {**(target.get("props") or {}), **refreshed.get("props", {})}
    try:
        validate_svg(refreshed["props"].get("html") or "")
    except Exception as exc:
        return _err("E_RENDER", f"kinetic_type adjust: rebuilt SVG is not render-safe: {exc}")

    parent, index = locate(doc, layer_id) or (None, None)
    ops: list[dict[str, Any]] = [
        {"op": "delete_layer", "layer_id": layer_id},
        {"op": "add_layer", "type": "html", "layer": refreshed, "id": layer_id, "index": index,
         **({"parent_id": parent.get("id")} if parent is not None and parent.get("id") != "root" else {})},
    ]
    try:
        new_doc = apply_layer_patch(doc, {"version": 1, "ops": ops})
    except Exception as exc:
        return _err(getattr(exc, "code", "E_UNKNOWN"), str(exc))
    _layer._save_lumendoc(ctx, new_doc)
    return {
        "applied": True, "layer_id": layer_id,
        "adjusted_params": (result.get("brief", {}).get("params") or {}),
        "plan": result.get("plan"), "notes": result.get("notes", []),
        "next": "lumen_seek / lumen_render_range to verify the new feel",
    }
