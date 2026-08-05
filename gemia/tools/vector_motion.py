"""``vector_motion`` — the vector motion-design engine as ONE agent tool.

Single tool, op discriminator (the ``update_quantum`` pattern — no flat-tool
proliferation):

* ``op:"create"``  — creative brief → choreographed VectorScene → animated
  SVG → an ``html`` layer added to the session's lumenframe doc in one atomic
  patch. The layer's ``props.vector_brief`` keeps the brief so the scene can
  be re-derived later. Preview/verify with the EXISTING verbs: ``lumen_seek``
  (a frame), ``lumen_render`` / ``lumen_render_range`` (mp4).
* ``op:"adjust"``  — human feedback ("more playful", "更高级") against a
  vector layer: folds semantic deltas into the stored brief and rebuilds the
  SVG deterministically (same seed) — adjustment is re-choreography, never
  SVG text surgery. Same layer id; one atomic patch.
* ``op:"catalog"`` — the creative vocabulary (styles, behaviours, feelings,
  mark presets, feedback phrases) for the model to compose briefs from.

ADD-ONLY integration: doc access reuses ``layer._lumendoc`` /
``layer._save_lumendoc`` exactly like lumen_seek / lumen_render_range; the
render path is the core ``html`` layer resolver — this module never renders.
"""
from __future__ import annotations

from typing import Any

from gemia.tools._context import ToolContext

try:  # pragma: no cover - exercised via the E_NOT_AVAILABLE branch in tests
    from lumenframe.ops import apply_layer_patch
    from lumenframe.vector.api import adjust_scene, build_scene
    from lumenframe.vector.catalog import vector_catalog
    from lumenframe.vector.render import scene_to_html_layer, validate_html_layer
    _IMPORT_ERROR: str | None = None
except ImportError as exc:  # lumenframe optional-dependency convention
    apply_layer_patch = None  # type: ignore[assignment]
    _IMPORT_ERROR = str(exc)

#: Layer keys an ``adjust`` must carry over from the existing layer — a
#: re-choreography changes the *content* (the SVG in props.html), never the
#: user's placement, timing, or compositing. props is merged specially so the
#: rebuilt vector_scene/html win while any user-added props survive.
_PRESERVED_LAYER_KEYS = (
    "transform", "opacity", "blend_mode", "visible", "locked",
    "mask", "effects", "lane",
)

_OPS = ("create", "adjust", "catalog")


def _err(code: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"applied": False, "error_code": code, "error_message": message, **extra}


def _plan_digest(plan: dict[str, Any]) -> dict[str, Any]:
    """The plan, compacted for a tool reply (full plan stays on the layer)."""
    return {
        "style": plan.get("style"),
        "intent": plan.get("intent"),
        "duration": plan.get("duration"),
        "seed": plan.get("seed"),
        "focal": plan.get("focal"),
        "phases": [
            {k: p.get(k) for k in ("phase", "behavior", "t0", "t1")}
            for p in plan.get("phases") or []
        ],
        "structure": [
            f"{s['kind']}:{s['id']}({s.get('role')})"
            for s in plan.get("structure") or []
        ],
    }


async def dispatch(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    if apply_layer_patch is None:
        return _err("E_NOT_AVAILABLE", f"lumenframe is not importable: {_IMPORT_ERROR}")

    op = str(args.get("op") or "create")
    if op not in _OPS:
        return _err("E_ARG", f"vector_motion: unknown op {op!r} (use {', '.join(_OPS)})")
    if op == "catalog":
        return {"applied": True, "catalog": vector_catalog()}
    if op == "create":
        return await _create(args, ctx)
    return await _adjust(args, ctx)


async def _create(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    from gemia.tools import layer as _layer

    brief = args.get("brief")
    if not isinstance(brief, dict):
        return _err("E_ARG", "vector_motion create: 'brief' must be an object "
                             "(see op:'catalog' for the vocabulary)")

    doc = _layer._lumendoc(ctx)
    brief = _brief_with_doc_canvas(brief, doc)
    try:
        result = build_scene(brief)
    except ValueError as exc:
        return _err("E_ARG", f"vector_motion create: {exc}", recovery="fix_args")

    place = args.get("place") if isinstance(args.get("place"), dict) else {}
    html_layer = scene_to_html_layer(
        result["scene"],
        name=str(place.get("name") or _default_name(brief)),
        start=float(place.get("start") or 0.0),
        lane=int(place.get("lane") or 0),
        brief=brief,
    )
    # Validate the render-safety of the SVG BEFORE it touches the doc, so a
    # bad scene can never poison every subsequent render of the document.
    try:
        validate_html_layer(html_layer)
    except Exception as exc:
        return _err("E_RENDER", f"vector_motion create: generated SVG is not "
                                f"render-safe: {exc}")

    try:
        new_doc = apply_layer_patch(
            doc, {"version": 1, "ops": [{"op": "add_layer", "type": "html", "layer": html_layer}]}
        )
    except Exception as exc:  # LayerPatchError carries code/message
        return _err(getattr(exc, "code", "E_UNKNOWN"), str(exc))
    _layer._save_lumendoc(ctx, new_doc)

    return {
        "applied": True,
        "layer_id": html_layer["id"],
        "layer_name": html_layer["name"],
        "start": html_layer["start"],
        "duration": html_layer["duration"],
        "svg_bytes": len(html_layer["props"]["html"]),
        "plan": _plan_digest(result["plan"]),
        "notes": result["notes"],
        "next": "lumen_seek a frame or lumen_render_range to verify; "
                "adjust with op:'adjust' + feedback phrases",
    }


async def _adjust(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    from gemia.tools import layer as _layer
    from lumenframe.model import find_layer

    layer_id = str(args.get("layer_id") or "")
    feedback = args.get("feedback")
    if not layer_id:
        return _err("E_ARG", "vector_motion adjust: 'layer_id' is required")
    if not isinstance(feedback, list) or not feedback:
        return _err("E_ARG", "vector_motion adjust: 'feedback' must be a non-empty "
                             "list of phrases like 'more playful' / '更高级'")

    doc = _layer._lumendoc(ctx)
    target = find_layer(doc, layer_id)
    if target is None:
        return _err("E_NOT_FOUND", f"vector_motion adjust: no layer {layer_id!r}")
    brief = (target.get("props") or {}).get("vector_brief")
    if not isinstance(brief, dict):
        return _err("E_ARG", f"vector_motion adjust: layer {layer_id!r} carries no "
                             "vector_brief (only vector_motion-created layers adjust)")

    try:
        result = adjust_scene(brief, [str(p) for p in feedback])
    except ValueError as exc:
        return _err("E_ARG", f"vector_motion adjust: {exc}", recovery="fix_args")

    refreshed = scene_to_html_layer(
        result["scene"],
        id=target.get("id"),
        name=str(target.get("name") or _default_name(brief)),
        start=float(target.get("start") or 0.0),
        lane=int(target.get("lane") or 0),
        brief=result["brief"],
    )
    # Re-choreography changes the CONTENT, not the user's placement: carry over
    # every customization the user made to the layer (transform, effects, mask,
    # opacity, blend, visibility, lock) and preserve any user-added props keys.
    for key in _PRESERVED_LAYER_KEYS:
        if key in target and target[key] is not None:
            refreshed[key] = target[key]
    old_props = target.get("props") or {}
    merged_props = {**old_props, **refreshed.get("props", {})}
    refreshed["props"] = merged_props

    try:
        validate_html_layer(refreshed)
    except Exception as exc:
        return _err("E_RENDER", f"vector_motion adjust: rebuilt SVG is not "
                                f"render-safe: {exc}")

    # One atomic patch: swap the old layer for its rebuilt twin at the same
    # tree position (delete + add at index preserves stacking order).
    from lumenframe.model import locate

    parent, index = locate(doc, layer_id) or (None, None)
    ops: list[dict[str, Any]] = [
        {"op": "delete_layer", "layer_id": layer_id},
        {"op": "add_layer", "type": "html", "layer": refreshed,
         "id": layer_id, "index": index,
         **({"parent_id": parent.get("id")} if parent is not None and parent.get("id") != "root" else {})},
    ]
    try:
        new_doc = apply_layer_patch(doc, {"version": 1, "ops": ops})
    except Exception as exc:
        return _err(getattr(exc, "code", "E_UNKNOWN"), str(exc))
    _layer._save_lumendoc(ctx, new_doc)

    return {
        "applied": True,
        "layer_id": layer_id,
        "adjusted_params": (result["brief"].get("params") or {}),
        "plan": _plan_digest(result["plan"]),
        "notes": result["notes"],
        "next": "lumen_seek / lumen_render_range to verify the new feel",
    }


def _default_name(brief: dict[str, Any]) -> str:
    subject = brief.get("subject") or {}
    label = subject.get("text") or subject.get("preset") or subject.get("kind") or "scene"
    return f"Vector · {label}"


def _brief_with_doc_canvas(brief: dict[str, Any], doc: dict[str, Any]) -> dict[str, Any]:
    """Default the brief's canvas to the DOC canvas so the SVG fills the frame.

    The html layer renders at the doc canvas with ``overflow:hidden`` and no
    scaling, so a brief that leaves canvas unset must inherit the doc's real
    dimensions — otherwise a 1920×1080 SVG is clipped on a 1080×1920 (vertical)
    project. An explicit brief.canvas still wins.
    """
    if brief.get("canvas"):
        return brief
    canvas = doc.get("canvas") if isinstance(doc, dict) else None
    if not isinstance(canvas, dict):
        return brief
    w, h = canvas.get("width"), canvas.get("height")
    if not w or not h:
        return brief
    return {**brief, "canvas": {"width": int(w), "height": int(h)}}
