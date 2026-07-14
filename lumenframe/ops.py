"""LayerPatch — the atomic, validated editing vocabulary of lumenframe.

A ``LayerPatch`` mirrors the timeline patch contract so the two feel like one
system::

    {"version": 1, "ops": [{"op": "add_layer", ...}, {"op": "set_transform", ...}]}

:func:`apply_layer_patch` deep-copies the (normalised) document, runs every op
in order, then validates the whole tree. The patch applies *atomically*: if any
op raises, the caller's document is never touched and the error names exactly
what went wrong (``LayerPatchError`` carries both a stable ``code`` and a
message).

Ops fall into the three families the editor exposes:

* **layer management** — ``add_layer`` / ``delete_layer`` / ``duplicate_layer``
  / ``select`` / ``move_layer`` / ``reorder_layer`` / ``group_layers`` /
  ``ungroup_layer`` / ``merge_layers`` / ``rename_layer`` / ``set_visibility``
  / ``set_lock``
* **time** — ``set_time`` / ``trim`` / ``split`` / ``set_speed``
* **per-layer & inter-layer** — ``set_transform`` / ``set_opacity`` /
  ``set_blend_mode`` / ``set_mask`` / ``clip_to_below`` / ``add_adjustment_layer``
  / ``add_effect`` / ``remove_effect`` / ``set_effect_params`` / ``color_grade``
  / ``add_transition`` / ``set_keyframe`` / ``remove_keyframe``

Extensions register further ops through :mod:`lumenframe.registry`.
"""
from __future__ import annotations

import copy
from typing import Any

from lumenframe import model, timebase
from lumenframe.model import (
    BLEND_MODES,
    DEFAULT_TRANSFORM,
    INTERP_KINDS,
    MASK_KINDS,
    TIME_NDIGITS,
    TIME_REMAP_EXTRAPOLATE,
    TIME_REMAP_INTERP,
    new_layer,
    normalize_doc,
)
from lumenframe.registry import op_handler, register_layer_type, register_op


class LayerPatchError(ValueError):
    """Structured layer-edit failure; ``str(e)`` carries code + message."""

    def __init__(self, code: str, message: str) -> None:
        self.code = str(code)
        self.message = str(message)
        super().__init__(f"{self.code}: {self.message}")


# ── entry point ─────────────────────────────────────────────────────────


def apply_layer_patch(
    doc: dict[str, Any] | None,
    patches: list[dict[str, Any]] | dict[str, Any],
) -> dict[str, Any]:
    """Apply one or more LayerPatches atomically; return the new document."""
    working = copy.deepcopy(normalize_doc(doc or {}))
    patch_list = [patches] if isinstance(patches, dict) else list(patches or [])
    for patch in patch_list:
        if not isinstance(patch, dict) or patch.get("version") != 1:
            raise LayerPatchError("E_PATCH", "Unsupported LayerPatch (need version: 1)")
        for op in patch.get("ops") or []:
            _dispatch(working, op)
    _finalize(working)
    validate_doc(working)
    return working


def _dispatch(doc: dict[str, Any], op: dict[str, Any]) -> None:
    if not isinstance(op, dict):
        raise LayerPatchError("E_OP", "LayerPatch op must be an object")
    name = str(op.get("op") or "")
    handler = op_handler(name)
    if handler is None:
        raise LayerPatchError("E_OP_UNKNOWN", f"Unknown LayerPatch op: {name or '<empty>'}")
    handler(doc, op)


# ── dry-run validation (ADD-only; never raises, never mutates) ───────────


# Short, actionable guidance keyed by the stable ``LayerPatchError.code``.
# Anything unmapped falls back to a generic hint so Gemini always gets one.
_HINTS: dict[str, str] = {
    "E_PATCH": "patch envelope is wrong; send {\"version\": 1, \"ops\": [...]}",
    "E_OP": "each op must be a JSON object with an 'op' key",
    "E_OP_UNKNOWN": "unknown op name; list available ops before patching",
    "E_ARG": "a required argument is missing or null for this op",
    "E_NOT_FOUND": "the layer id does not exist; inspect current layers first",
    "E_RANGE": "value out of allowed range",
    "E_SPEED": "speed must be greater than 0",
    "E_ROOT": "this op cannot target the root composition",
    "E_CONTAINER": "that parent layer cannot hold children",
    "E_CYCLE": "cannot move a layer into itself or one of its descendants",
    "E_GROUP_PARENT": "all selected layers must share the same parent",
    "E_TYPE": "the layer type is unknown or not valid for this op",
    "E_MASK": "mask/matte source is missing, self-referential, or unknown",
    "E_UNSAFE": "the expression is invalid or not allowed",
    "E_DOC": "the document is missing its root composition",
    "E_ID": "every layer needs an id",
    "E_DUP_ID": "duplicate layer id in the resulting document",
}


def _hint_for(code: str) -> str:
    """Map a stable error ``code`` to a short, actionable hint."""
    return _HINTS.get(code, "see message; inspect current state and retry")


def validate_patch(
    doc: dict[str, Any] | None,
    patch: list[dict[str, Any]] | dict[str, Any],
) -> dict[str, Any]:
    """Dry-run a LayerPatch without mutating ``doc`` or raising.

    Runs the *same* op dispatch as :func:`apply_layer_patch` against a private
    deep copy of the (normalised) document, catching :class:`LayerPatchError`
    per op so Gemini can see every reachable problem in one pass. The caller's
    ``doc`` is never touched.

    Returns ``{"ok": bool, "errors": [...]}`` where each error is
    ``{"op_index": int, "code": str, "message": str, "hint": str}``. ``op_index``
    is the position of the failing op across all ops in all patches (0-based);
    structural/finalize failures that are not tied to a single op use ``-1``.
    """
    working = copy.deepcopy(normalize_doc(doc or {}))
    patch_list = [patch] if isinstance(patch, dict) else list(patch or [])
    errors: list[dict[str, Any]] = []
    op_index = -1
    for entry in patch_list:
        if not isinstance(entry, dict) or entry.get("version") != 1:
            err = LayerPatchError("E_PATCH", "Unsupported LayerPatch (need version: 1)")
            errors.append({
                "op_index": op_index + 1,
                "code": err.code,
                "message": err.message,
                "hint": _hint_for(err.code),
            })
            # The envelope is unusable; skip its ops but keep checking siblings.
            continue
        for op in entry.get("ops") or []:
            op_index += 1
            try:
                _dispatch(working, op)
            except LayerPatchError as err:
                errors.append({
                    "op_index": op_index,
                    "code": err.code,
                    "message": err.message,
                    "hint": _hint_for(err.code),
                })
    # Even if every op applied cleanly, the assembled document may still violate
    # a structural invariant — surface that the way apply_layer_patch would.
    if not errors:
        try:
            _finalize(working)
            validate_doc(working)
        except LayerPatchError as err:
            errors.append({
                "op_index": -1,
                "code": err.code,
                "message": err.message,
                "hint": _hint_for(err.code),
            })
    return {"ok": not errors, "errors": errors}


# ── shared helpers ──────────────────────────────────────────────────────


def _require_layer(doc: dict[str, Any], layer_id: Any, *, op: str) -> dict[str, Any]:
    layer = model.find_layer(doc, str(layer_id)) if layer_id is not None else None
    if layer is None:
        raise LayerPatchError("E_NOT_FOUND", f"{op}: layer not found: {layer_id!r}")
    return layer


def _require_locate(doc: dict[str, Any], layer_id: Any, *, op: str) -> tuple[dict[str, Any], int]:
    found = model.locate(doc, str(layer_id)) if layer_id is not None else None
    if found is None:
        raise LayerPatchError("E_NOT_FOUND", f"{op}: layer not found: {layer_id!r}")
    return found


def _require_arg(op: dict[str, Any], key: str) -> Any:
    if key not in op or op.get(key) is None:
        raise LayerPatchError("E_ARG", f"{op.get('op')}: missing required arg {key!r}")
    return op[key]


def _is_container(layer: dict[str, Any]) -> bool:
    spec = None
    from lumenframe.registry import layer_type_spec
    spec = layer_type_spec(str(layer.get("type")))
    if spec is not None:
        return bool(spec.get("container"))
    return str(layer.get("type")) in model.CONTAINER_TYPES


def _round_t(value: Any) -> float:
    return round(model._as_float(value), TIME_NDIGITS)


def _canvas_fps(doc: dict[str, Any]) -> float:
    """Canvas fps (the only timebase frame<->second uses), defaulting safely."""
    canvas = doc.get("canvas") if isinstance(doc.get("canvas"), dict) else {}
    return float(canvas.get("fps") or model.DEFAULT_CANVAS["fps"])


def _seconds_or_frame(
    doc: dict[str, Any],
    op: dict[str, Any],
    sec_key: str,
    frame_key: str,
    *,
    op_name: str,
) -> float | None:
    """Resolve one timeline edge given in *either* seconds or frames.

    The document stays seconds-canonical: a ``{frame}`` value is converted with
    ``timebase.to_seconds(frame, canvas.fps)`` and the doc only ever stores
    seconds. Supplying *both* the seconds and the frame form for the same edge is
    a contradiction and raises ``E_ARG``. Returns the edge in seconds, or
    ``None`` when neither form was given.
    """
    has_sec = op.get(sec_key) is not None
    has_frame = op.get(frame_key) is not None
    if has_sec and has_frame:
        raise LayerPatchError(
            "E_ARG",
            f"{op_name}: give {sec_key!r} (seconds) or {frame_key!r} (frame) for one edge, not both",
        )
    if has_frame:
        return _round_t(timebase.to_seconds(model._as_float(op.get(frame_key)), _canvas_fps(doc)))
    if has_sec:
        return _round_t(op.get(sec_key))
    return None


def _fresh_ids_deep(layer: dict[str, Any]) -> dict[str, Any]:
    """Deep-copy a subtree with brand-new ids; remap intra-subtree mask refs."""
    clone = copy.deepcopy(layer)
    id_map: dict[str, str] = {}
    for node in model.walk(clone):
        old = str(node.get("id"))
        new = model.gen_id(model._id_prefix(str(node.get("type"))))
        id_map[old] = new
        node["id"] = new
        for fx in node.get("effects") or []:
            if isinstance(fx, dict):
                fx["id"] = model.gen_id("fx")
    # Re-point any track-matte that referenced a layer inside this subtree.
    for node in model.walk(clone):
        mask = node.get("mask")
        if isinstance(mask, dict) and mask.get("source_layer_id") in id_map:
            mask["source_layer_id"] = id_map[mask["source_layer_id"]]
    return clone


def _splice_out(doc: dict[str, Any], layer_id: str, *, op: str) -> tuple[dict[str, Any], dict[str, Any], int]:
    parent, index = _require_locate(doc, layer_id, op=op)
    layer = parent["children"].pop(index)
    return layer, parent, index


def _insert(parent: dict[str, Any], layer: dict[str, Any], index: int | None) -> None:
    children = parent.setdefault("children", [])
    if index is None or index < 0 or index > len(children):
        children.append(layer)
    else:
        children.insert(index, layer)


def _set_selection(doc: dict[str, Any], ids: list[str]) -> None:
    doc["selection"] = list(dict.fromkeys(str(i) for i in ids))


def _finalize(doc: dict[str, Any]) -> None:
    """Round times, prune dangling selection, recompute durations."""
    known = {str(n.get("id")) for n in model.walk(doc["root"]) if n.get("id")}
    doc["selection"] = [s for s in doc.get("selection", []) if s in known]
    for node in model.walk(doc["root"]):
        for key in ("start", "duration", "source_in", "source_out"):
            node[key] = _round_t(node.get(key))
    doc["root"]["duration"] = model.doc_duration(doc)


# ── validation ──────────────────────────────────────────────────────────


def validate_doc(doc: dict[str, Any]) -> None:
    """Raise :class:`LayerPatchError` on any structural invariant violation."""
    root = doc.get("root")
    if not isinstance(root, dict):
        raise LayerPatchError("E_DOC", "document is missing its root composition")

    seen: set[str] = set()
    from lumenframe.registry import layer_type_spec
    sibling_index: dict[str, list[str]] = {}
    for node in model.walk(root):
        lid = str(node.get("id"))
        if not lid:
            raise LayerPatchError("E_ID", "every layer needs an id")
        if lid in seen:
            raise LayerPatchError("E_DUP_ID", f"duplicate layer id: {lid}")
        seen.add(lid)
        ltype = str(node.get("type"))
        if layer_type_spec(ltype) is None and ltype not in model.LAYER_TYPES:
            raise LayerPatchError("E_TYPE", f"layer {lid}: unknown type {ltype!r}")
        children = node.get("children") or []
        if children and not _is_container(node):
            raise LayerPatchError("E_CONTAINER", f"layer {lid} ({ltype}) cannot hold children")
        sibling_index[lid] = [str(c.get("id")) for c in children if isinstance(c, dict)]
        if model._as_float(node.get("duration")) < 0:
            raise LayerPatchError("E_RANGE", f"layer {lid}: negative duration")
        if model._as_float(node.get("speed")) <= 0 and node is not root:
            raise LayerPatchError("E_SPEED", f"layer {lid}: speed must be > 0")

    # Track mattes must reference an existing *sibling* layer (not self).
    for node in model.walk(root):
        mask = node.get("mask")
        if not isinstance(mask, dict):
            continue
        kind = str(mask.get("kind"))
        if kind in {"alpha_matte", "luma_matte"}:
            src = str(mask.get("source_layer_id") or "")
            if not src or src not in seen:
                raise LayerPatchError("E_MASK", f"layer {node['id']}: matte source {src!r} not found")
            if src == str(node.get("id")):
                raise LayerPatchError("E_MASK", f"layer {node['id']}: matte cannot reference itself")


# ════════════════════════════════════════════════════════════════════════
# Layer management
# ════════════════════════════════════════════════════════════════════════


_ADD_LAYER_CONTROL_KEYS = {"op", "parent_id", "index", "at_time", "lane", "select", "layer"}


@register_op("add_layer", source="core")
def _op_add_layer(doc: dict[str, Any], op: dict[str, Any]) -> None:
    spec = op.get("layer") if isinstance(op.get("layer"), dict) else {}
    # Layer fields may be nested under "layer" or written inline on the op;
    # inline keys win so agent-authored ``{"op":"add_layer","type":"text",...}``
    # works without wrapping every field in a "layer" object.
    inline = {k: v for k, v in op.items() if k not in _ADD_LAYER_CONTROL_KEYS}
    fields = {**spec, **inline}
    ltype = str(op.get("type") or spec.get("type") or "solid")
    fields["type"] = ltype
    layer = model._normalize_layer(fields)
    if op.get("id"):
        layer["id"] = str(op["id"])
    elif spec.get("id"):
        layer["id"] = str(spec["id"])
    if model.find_layer(doc, layer["id"]) is not None:
        layer["id"] = model.gen_id(model._id_prefix(ltype))
    if op.get("at_time") is not None:
        layer["start"] = _round_t(op["at_time"])
    if op.get("lane") is not None:
        layer["lane"] = int(model._as_float(op["lane"]))

    parent_id = op.get("parent_id")
    parent = _require_layer(doc, parent_id, op="add_layer") if parent_id else doc["root"]
    if not _is_container(parent):
        raise LayerPatchError("E_CONTAINER", f"add_layer: parent {parent.get('id')} cannot hold children")
    index = op.get("index")
    _insert(parent, layer, int(index) if index is not None else None)
    if op.get("select", True):
        _set_selection(doc, [layer["id"]])


@register_op("delete_layer", source="core")
def _op_delete_layer(doc: dict[str, Any], op: dict[str, Any]) -> None:
    ids = op.get("layer_ids") or ([op.get("layer_id")] if op.get("layer_id") else [])
    if not ids:
        raise LayerPatchError("E_ARG", "delete_layer: need layer_id or layer_ids")
    for raw in ids:
        lid = str(raw)
        if lid == str(doc["root"].get("id")):
            raise LayerPatchError("E_ROOT", "delete_layer: cannot delete the root composition")
        _splice_out(doc, lid, op="delete_layer")
        # Drop mattes that pointed at the now-deleted layer.
        for node in model.walk(doc["root"]):
            mask = node.get("mask")
            if isinstance(mask, dict) and str(mask.get("source_layer_id")) == lid:
                node["mask"] = None


@register_op("duplicate_layer", source="core")
def _op_duplicate_layer(doc: dict[str, Any], op: dict[str, Any]) -> None:
    layer_id = _require_arg(op, "layer_id")
    parent, index = _require_locate(doc, layer_id, op="duplicate_layer")
    original = parent["children"][index]
    clone = _fresh_ids_deep(original)
    if op.get("name"):
        clone["name"] = str(op["name"])
    else:
        clone["name"] = f"{original.get('name', 'Layer')} copy"
    if op.get("offset_time") is not None:
        clone["start"] = _round_t(model._as_float(clone.get("start")) + model._as_float(op["offset_time"]))
    parent["children"].insert(index + 1, clone)
    _set_selection(doc, [clone["id"]])


@register_op("select", source="core")
def _op_select(doc: dict[str, Any], op: dict[str, Any]) -> None:
    mode = str(op.get("mode") or "replace")
    if mode == "clear":
        doc["selection"] = []
        return
    ids = op.get("layer_ids") or ([op.get("layer_id")] if op.get("layer_id") else [])
    ids = [str(i) for i in ids]
    for lid in ids:
        _require_layer(doc, lid, op="select")
    current = list(doc.get("selection", []))
    if mode == "replace":
        current = ids
    elif mode == "add":
        current = current + ids
    elif mode == "toggle":
        for lid in ids:
            if lid in current:
                current.remove(lid)
            else:
                current.append(lid)
    else:
        raise LayerPatchError("E_ARG", f"select: unknown mode {mode!r}")
    _set_selection(doc, current)


@register_op("move_layer", source="core")
def _op_move_layer(doc: dict[str, Any], op: dict[str, Any]) -> None:
    """Universal move: reparent (z-tree), reorder (z within parent), retime, relane."""
    layer_id = str(_require_arg(op, "layer_id"))
    if layer_id == str(doc["root"].get("id")):
        raise LayerPatchError("E_ROOT", "move_layer: cannot move the root composition")
    layer = _require_layer(doc, layer_id, op="move_layer")

    target_parent = doc["root"]
    if op.get("parent_id"):
        target_parent = _require_layer(doc, op["parent_id"], op="move_layer")
        if not _is_container(target_parent):
            raise LayerPatchError("E_CONTAINER", f"move_layer: parent {target_parent['id']} cannot hold children")
        descendant_ids = {str(n.get("id")) for n in model.walk(layer)}
        if str(target_parent.get("id")) in descendant_ids:
            raise LayerPatchError("E_CYCLE", "move_layer: cannot move a layer into itself or a descendant")
    else:
        target_parent, _ = _require_locate(doc, layer_id, op="move_layer")

    detached, _old_parent, _old_index = _splice_out(doc, layer_id, op="move_layer")
    index = op.get("index")
    # ``index`` is the desired *final* position in the target parent's children
    # (computed after the layer is detached), so it needs no reorder fix-up.
    target_index = int(index) if index is not None else None
    _insert(target_parent, detached, target_index)

    if op.get("lane") is not None:
        detached["lane"] = int(model._as_float(op["lane"]))
    if op.get("start") is not None:
        detached["start"] = _round_t(op["start"])
    elif op.get("delta_start") is not None:
        detached["start"] = _round_t(model._as_float(detached.get("start")) + model._as_float(op["delta_start"]))


@register_op("reorder_layer", source="core")
def _op_reorder_layer(doc: dict[str, Any], op: dict[str, Any]) -> None:
    """Pure z-order change within the current parent (bring forward / send back)."""
    layer_id = str(_require_arg(op, "layer_id"))
    parent, index = _require_locate(doc, layer_id, op="reorder_layer")
    count = len(parent["children"])
    to = op.get("to")
    if to == "top":
        target = count - 1
    elif to == "bottom":
        target = 0
    elif to == "forward":
        target = min(index + 1, count - 1)
    elif to == "backward":
        target = max(index - 1, 0)
    elif op.get("index") is not None:
        target = max(0, min(int(op["index"]), count - 1))
    elif op.get("delta") is not None:
        target = max(0, min(index + int(op["delta"]), count - 1))
    else:
        raise LayerPatchError("E_ARG", "reorder_layer: need to / index / delta")
    layer = parent["children"].pop(index)
    parent["children"].insert(target, layer)


@register_op("rename_layer", source="core")
def _op_rename_layer(doc: dict[str, Any], op: dict[str, Any]) -> None:
    layer = _require_layer(doc, _require_arg(op, "layer_id"), op="rename_layer")
    layer["name"] = str(_require_arg(op, "name"))


@register_op("set_visibility", source="core")
def _op_set_visibility(doc: dict[str, Any], op: dict[str, Any]) -> None:
    layer = _require_layer(doc, _require_arg(op, "layer_id"), op="set_visibility")
    layer["visible"] = bool(_require_arg(op, "visible"))


@register_op("set_lock", source="core")
def _op_set_lock(doc: dict[str, Any], op: dict[str, Any]) -> None:
    layer = _require_layer(doc, _require_arg(op, "layer_id"), op="set_lock")
    layer["locked"] = bool(_require_arg(op, "locked"))


# ── grouping / merging (precompose) ─────────────────────────────────────


def _group_into_composition(
    doc: dict[str, Any], op: dict[str, Any], *, merged: bool, default_name: str
) -> None:
    ids = op.get("layer_ids") or []
    ids = [str(i) for i in ids]
    if len(ids) < 1:
        raise LayerPatchError("E_ARG", f"{op.get('op')}: need layer_ids")
    located = [(_require_locate(doc, lid, op=str(op.get("op")))) for lid in ids]
    parent = located[0][0]
    if any(p is not parent for p, _ in located):
        raise LayerPatchError("E_GROUP_PARENT", f"{op.get('op')}: all layers must share one parent")

    # Capture members in tree order, then splice them all out.
    ordered = sorted(zip(ids, (idx for _, idx in located)), key=lambda pair: pair[1])
    min_index = located and min(idx for _, idx in located)
    members = [model.find_layer(doc, lid) for lid, _ in ordered]
    group_start = min(model._as_float(m.get("start")) for m in members)
    group_end = max(model._as_float(m.get("start")) + model._as_float(m.get("duration")) for m in members)
    for lid, _ in ordered:
        _splice_out(doc, lid, op=str(op.get("op")))

    comp = new_layer(
        "composition",
        id=str(op["into_id"]) if op.get("into_id") else None,
        name=str(op.get("name") or default_name),
        start=_round_t(group_start),
        duration=_round_t(group_end - group_start),
        merged=merged,
    )
    for member in members:
        member["start"] = _round_t(model._as_float(member.get("start")) - group_start)
    comp["children"] = members
    insert_at = min(int(min_index), len(parent["children"])) if min_index is not None else None
    _insert(parent, comp, insert_at)
    _set_selection(doc, [comp["id"]])


@register_op("group_layers", source="core")
def _op_group_layers(doc: dict[str, Any], op: dict[str, Any]) -> None:
    _group_into_composition(doc, op, merged=False, default_name="Group")


@register_op("merge_layers", source="core")
def _op_merge_layers(doc: dict[str, Any], op: dict[str, Any]) -> None:
    # Flatten = precompose marked for raster baking at compile time.
    _group_into_composition(doc, op, merged=True, default_name="Merged")


@register_op("ungroup_layer", source="core")
def _op_ungroup_layer(doc: dict[str, Any], op: dict[str, Any]) -> None:
    layer_id = str(_require_arg(op, "layer_id"))
    if layer_id == str(doc["root"].get("id")):
        raise LayerPatchError("E_ROOT", "ungroup_layer: cannot ungroup the root composition")
    comp, parent, index = _splice_out(doc, layer_id, op="ungroup_layer")
    if not _is_container(comp):
        # Put it back; ungroup only applies to compositions.
        parent["children"].insert(index, comp)
        raise LayerPatchError("E_TYPE", f"ungroup_layer: {layer_id} is not a composition")
    offset = model._as_float(comp.get("start"))
    lifted = comp.get("children") or []
    for child in lifted:
        child["start"] = _round_t(model._as_float(child.get("start")) + offset)
    parent["children"][index:index] = lifted
    _set_selection(doc, [str(c.get("id")) for c in lifted])


def _repoint_refs(node: dict[str, Any], id_map: dict[str, str]) -> None:
    """Re-point any cross-layer reference in ``node`` that lands in ``id_map``.

    The only structural layer<->layer references in the model are the track-matte
    ``mask.source_layer_id`` and the optional parenting hint authors may stash as
    ``props.parent_id`` / ``parent_id``. When a subtree is lifted and re-ided, any
    such reference that pointed *inside the moved set* must follow to the new id;
    references that pointed outside the moved set are left untouched.
    """
    mask = node.get("mask")
    if isinstance(mask, dict):
        src = mask.get("source_layer_id")
        if src is not None and str(src) in id_map:
            mask["source_layer_id"] = id_map[str(src)]
    for key in ("parent_id",):
        if node.get(key) is not None and str(node.get(key)) in id_map:
            node[key] = id_map[str(node[key])]
    props = node.get("props")
    if isinstance(props, dict):
        for key in ("parent_id", "track_matte_id"):
            if props.get(key) is not None and str(props.get(key)) in id_map:
                props[key] = id_map[str(props[key])]


def _fresh_ids_for_group(groups: list[list[dict[str, Any]]]) -> list[list[dict[str, Any]]]:
    """Deep-copy several groups of sibling subtrees with fresh ids in ONE id-space.

    Like :func:`_fresh_ids_deep` but the id-map spans *every* subtree across *all*
    groups, so a matte or parent reference in one lifted subtree that pointed at a
    layer in another lifted subtree — even one belonging to a different source
    composition merged in the same call — is re-pointed to that layer's new id (a
    per-subtree, or per-source, remap would leave such a cross reference dangling
    at the old id). Group structure is preserved so callers can still place each
    source's children with its own time shift.
    """
    cloned_groups = [[copy.deepcopy(child) for child in group] for group in groups]
    id_map: dict[str, str] = {}
    for group in cloned_groups:
        for clone in group:
            for node in model.walk(clone):
                old = str(node.get("id"))
                new = model.gen_id(model._id_prefix(str(node.get("type"))))
                id_map[old] = new
                node["id"] = new
                for fx in node.get("effects") or []:
                    if isinstance(fx, dict):
                        fx["id"] = model.gen_id("fx")
    for group in cloned_groups:
        for clone in group:
            for node in model.walk(clone):
                _repoint_refs(node, id_map)
    return cloned_groups


@register_op("merge_compositions", source="core")
def _op_merge_compositions(doc: dict[str, Any], op: dict[str, Any]) -> None:
    """Merge the children of one or more source compositions into a target comp.

    Lifts the *children* of each ``source_ids`` composition into ``into_id`` (re-
    timing them onto ``into_id``'s local timeline, just like ungroup), then removes
    the now-empty source containers (unless ``keep_sources`` is set).

    * ``mode="overlay"`` — children keep their original local start (+ ``offset``),
      so the merged content composites *over* whatever ``into_id`` already holds.
    * ``mode="append"`` — children are shifted past ``into_id``'s current extent
      (max child end) (+ ``offset``), so the merged content plays *after* it.

    Every lifted subtree is re-ided fresh (one shared id-space) so nothing collides
    with the target, and any matte/parent reference inside the moved set follows to
    its new id. Lane is preserved.
    """
    into_id = str(_require_arg(op, "into_id"))
    target = model.find_layer(doc, into_id)
    if target is None:
        raise LayerPatchError("E_NOT_FOUND", f"merge_compositions: into_id not found: {into_id!r}")
    if not _is_container(target):
        raise LayerPatchError("E_ARG", f"merge_compositions: into_id {into_id!r} is not a composition")

    raw_sources = op.get("source_ids")
    if not isinstance(raw_sources, list) or not raw_sources:
        raise LayerPatchError("E_ARG", "merge_compositions: need source_ids")
    source_ids = [str(s) for s in raw_sources]

    mode = str(op.get("mode") or "overlay")
    if mode not in {"overlay", "append"}:
        raise LayerPatchError("E_ARG", f"merge_compositions: mode must be 'overlay' or 'append', got {mode!r}")
    offset = model._as_float(op.get("offset"))
    keep_sources = bool(op.get("keep_sources", False))

    # Validate every source before mutating anything (atomic-friendly).
    for sid in source_ids:
        if sid == into_id:
            raise LayerPatchError("E_ARG", "merge_compositions: into_id cannot be one of source_ids")
        if sid == str(doc["root"].get("id")):
            raise LayerPatchError("E_ARG", "merge_compositions: cannot merge the root composition")
        src = model.find_layer(doc, sid)
        if src is None:
            raise LayerPatchError("E_NOT_FOUND", f"merge_compositions: source not found: {sid!r}")
        if not _is_container(src):
            raise LayerPatchError("E_ARG", f"merge_compositions: source {sid!r} is not a composition")
        # ``into_id`` may not live inside a source subtree — lifting that source's
        # children would move (and re-id) the target itself.
        if into_id in {str(n.get("id")) for n in model.walk(src)}:
            raise LayerPatchError("E_ARG", f"merge_compositions: into_id {into_id!r} is inside source {sid!r}")

    # Splice every source out first so none counts toward the target's extent
    # while we append, and so the fresh-id pass spans all of them at once (a matte
    # in one source that points into another source must follow the new ids).
    spliced: list[tuple[dict[str, Any], dict[str, Any], int]] = []
    source_children: list[list[dict[str, Any]]] = []
    for sid in source_ids:
        src_comp, src_parent, src_index = _splice_out(doc, sid, op="merge_compositions")
        spliced.append((src_comp, src_parent, src_index))
        source_children.append([c for c in (src_comp.get("children") or []) if isinstance(c, dict)])

    cloned_groups = _fresh_ids_for_group(source_children)

    # Remember the target's explicit duration *before* we add anything. A nested
    # comp compiles its length from this explicit duration (compile.py
    # ``_composition_content`` sets ``sub_total = round(duration * fps)`` and clamps
    # every local frame to ``sub_total - 1``), so children placed past it would
    # NEVER render. We grow the duration below to cover the lifted content while
    # never *shrinking* a comp that was authored longer than its content.
    target_current_duration = _round_t(model._as_float(target.get("duration")))

    lifted_ids: list[str] = []
    for group in cloned_groups:
        # The shift baseline is re-read per source so successive appends stack onto
        # the content the previous source just added.
        shift = (model._composition_extent(target) + offset) if mode == "append" else offset
        for clone in group:
            clone["start"] = _round_t(model._as_float(clone.get("start")) + shift)
        target.setdefault("children", []).extend(group)
        lifted_ids.extend(str(c.get("id")) for c in group)

    # Extend the target so the lifted content actually renders. For ``append`` this
    # covers children placed past the original extent; for ``overlay`` it covers a
    # lifted child that overruns the target's current duration (longer overlaid
    # content). ``max`` keeps a comp authored longer than its content unchanged.
    # Applies whether the target is the root or a nested comp (a no-op for root,
    # whose timeline already auto-sizes to ``_composition_extent``).
    target["duration"] = _round_t(max(target_current_duration, model._composition_extent(target)))

    if keep_sources:
        # Re-insert the (now child-less) source containers where they were. Restore
        # outermost-last so each original index is still valid as we re-insert.
        for src_comp, src_parent, src_index in reversed(spliced):
            src_comp["children"] = []
            src_parent["children"].insert(src_index, src_comp)

    _set_selection(doc, lifted_ids)


# ════════════════════════════════════════════════════════════════════════
# Time
# ════════════════════════════════════════════════════════════════════════


@register_op("set_time", source="core")
def _op_set_time(doc: dict[str, Any], op: dict[str, Any]) -> None:
    layer = _require_layer(doc, _require_arg(op, "layer_id"), op="set_time")
    if op.get("start") is not None:
        layer["start"] = _round_t(op["start"])
    if op.get("duration") is not None:
        dur = _round_t(op["duration"])
        if dur < 0:
            raise LayerPatchError("E_RANGE", "set_time: duration must be >= 0")
        layer["duration"] = dur


@register_op("trim", source="core")
def _op_trim(doc: dict[str, Any], op: dict[str, Any]) -> None:
    layer = _require_layer(doc, _require_arg(op, "layer_id"), op="trim")
    edge = str(_require_arg(op, "edge"))
    if edge not in {"in", "out"}:
        raise LayerPatchError("E_ARG", "trim: edge must be 'in' or 'out'")
    speed = max(model._as_float(layer.get("speed")) or 1.0, 1e-6)
    start = model._as_float(layer.get("start"))
    duration = model._as_float(layer.get("duration"))
    # An edge may be given in seconds (``to``) or frames (``frame_in``/
    # ``frame_out``) — converted via the canvas timebase. Mixing both for the
    # same edge is E_ARG; the doc stays seconds-canonical either way.
    if edge == "in":
        edge_sec = _seconds_or_frame(doc, op, "to", "frame_in", op_name="trim")
        new_start = edge_sec if edge_sec is not None else _round_t(start + model._as_float(op.get("delta")))
        delta_t = new_start - start
        new_duration = duration - delta_t
        if new_duration <= 0:
            raise LayerPatchError("E_RANGE", "trim: in-edge would collapse the layer")
        layer["start"] = new_start
        layer["duration"] = _round_t(new_duration)
        layer["source_in"] = _round_t(model._as_float(layer.get("source_in")) + delta_t * speed)
    else:
        edge_sec = _seconds_or_frame(doc, op, "to", "frame_out", op_name="trim")
        new_end = edge_sec if edge_sec is not None else _round_t(start + duration + model._as_float(op.get("delta")))
        new_duration = new_end - start
        if new_duration <= 0:
            raise LayerPatchError("E_RANGE", "trim: out-edge would collapse the layer")
        layer["duration"] = _round_t(new_duration)
        layer["source_out"] = _round_t(model._as_float(layer.get("source_in")) + new_duration * speed)


@register_op("split", source="core")
def _op_split(doc: dict[str, Any], op: dict[str, Any]) -> None:
    layer_id = str(_require_arg(op, "layer_id"))
    # Cut point may be seconds (``at_time``) or frames (``at_frame``); the doc
    # stays seconds-canonical. Mixing both is E_ARG; neither is E_ARG.
    at_time = _seconds_or_frame(doc, op, "at_time", "at_frame", op_name="split")
    if at_time is None:
        raise LayerPatchError("E_ARG", "split: missing required arg 'at_time' (or 'at_frame')")
    parent, index = _require_locate(doc, layer_id, op="split")
    left = parent["children"][index]
    start = model._as_float(left.get("start"))
    duration = model._as_float(left.get("duration"))
    if not (start < at_time < start + duration):
        raise LayerPatchError("E_RANGE", f"split: at_time {at_time} must fall inside the layer")
    speed = max(model._as_float(left.get("speed")) or 1.0, 1e-6)
    left_dur = at_time - start
    split_source = model._as_float(left.get("source_in")) + left_dur * speed

    right = _fresh_ids_deep(left)
    right["name"] = f"{left.get('name', 'Layer')} (2)"
    right["start"] = at_time
    right["duration"] = _round_t(duration - left_dur)
    right["source_in"] = _round_t(split_source)

    left["duration"] = _round_t(left_dur)
    left["source_out"] = _round_t(split_source)
    parent["children"].insert(index + 1, right)
    _set_selection(doc, [left["id"], right["id"]])


@register_op("set_speed", source="core")
def _op_set_speed(doc: dict[str, Any], op: dict[str, Any]) -> None:
    layer = _require_layer(doc, _require_arg(op, "layer_id"), op="set_speed")
    speed = model._as_float(_require_arg(op, "speed"))
    if speed <= 0:
        raise LayerPatchError("E_SPEED", "set_speed: speed must be > 0")
    old_speed = max(model._as_float(layer.get("speed")) or 1.0, 1e-6)
    src_range = model._as_float(layer.get("source_out")) - model._as_float(layer.get("source_in"))
    if src_range > 0:
        new_duration = src_range / speed
    else:
        new_duration = model._as_float(layer.get("duration")) * old_speed / speed
    layer["speed"] = speed
    layer["duration"] = _round_t(new_duration)


@register_op("retime_segment", source="core")
def _op_retime_segment(doc: dict[str, Any], op: dict[str, Any]) -> None:
    """Speed-change ONLY the sub-range ``[t0, t1]`` of a layer, frame-accurately.

    This is the one-op ergonomic for "make just this part of the clip slow/fast".
    It is pure *sugar* composed from the existing, already-tested primitives:

    1. snap ``t0`` / ``t1`` to canvas-fps frame boundaries (so the cuts land on
       exact frames — the same quantisation ``timebase.snap_seconds`` gives);
    2. ``split`` the layer at ``t1`` and at ``t0`` (reusing :func:`_op_split`,
       which is frame-precise and splits ``source_in``/``source_out`` correctly)
       so the middle ``[t0, t1]`` piece becomes its own layer;
    3. ``set_speed`` = ``speed`` on that middle piece (reusing
       :func:`_op_set_speed`), so its source-frame mapping is *identical* to a
       hand-authored split + set_speed (same numbers, same compile output).

    Boundary cases are handled by skipping the unneeded cut: if ``t0`` equals the
    layer ``start`` (resp. ``t1`` equals ``start + duration``) no split is made on
    that edge and the existing piece *is* the segment. If the whole range covers
    the layer exactly, this degenerates to a plain ``set_speed`` on it.

    Args: ``layer_id``, ``t0``, ``t1`` (output seconds on the parent timeline,
    snapped to frames), ``speed`` (> 0, constant).

    Errors: ``E_ARG`` when ``layer_id`` / ``t0`` / ``t1`` is missing or
    ``speed <= 0``; ``E_NOT_FOUND`` when the layer id does not exist; ``E_RANGE``
    when ``[t0, t1]`` is not within the layer's ``[start, start + duration]`` (or
    is empty/inverted).
    """
    layer = _require_layer(doc, _require_arg(op, "layer_id"), op="retime_segment")
    layer_id = str(layer["id"])

    # speed validated up front (mirrors set_speed: > 0 only). E_ARG for a missing
    # speed, E_ARG for a non-positive one (the task spec asks for E_ARG here).
    speed = model._as_float(_require_arg(op, "speed"))
    if speed <= 0:
        raise LayerPatchError("E_ARG", "retime_segment: speed must be > 0")

    # t0 / t1 may be given in seconds (t0/t1) or frames (frame0/frame1); the doc
    # stays seconds-canonical. Both are required (one edge each).
    t0 = _seconds_or_frame(doc, op, "t0", "frame0", op_name="retime_segment")
    t1 = _seconds_or_frame(doc, op, "t1", "frame1", op_name="retime_segment")
    if t0 is None or t1 is None:
        raise LayerPatchError(
            "E_ARG", "retime_segment: missing required arg 't0'/'t1' (or 'frame0'/'frame1')"
        )

    # Snap both edges to exact frame boundaries so the cuts are frame-accurate.
    fps = _canvas_fps(doc)
    t0 = _round_t(timebase.snap_seconds(t0, fps))
    t1 = _round_t(timebase.snap_seconds(t1, fps))

    start = _round_t(model._as_float(layer.get("start")))
    duration = model._as_float(layer.get("duration"))
    end = _round_t(start + duration)

    # The segment must be a non-empty sub-range inside the layer's placement.
    if not (t0 < t1):
        raise LayerPatchError("E_RANGE", f"retime_segment: empty range [{t0}, {t1}]")
    if t0 < start - timebase.FRAME_EPS or t1 > end + timebase.FRAME_EPS:
        raise LayerPatchError(
            "E_RANGE",
            f"retime_segment: range [{t0}, {t1}] is not within the layer [{start}, {end}]",
        )

    # Cut the *back* edge first (at t1), then the *front* edge (at t0): doing t1
    # first keeps the front piece carrying the original layer_id so the second
    # split is found by the same id. Each split is skipped when the edge already
    # sits on a layer boundary (split itself would reject a boundary cut). After
    # both cuts the segment is the piece whose start == t0.
    if t1 < end - timebase.FRAME_EPS:
        _op_split(doc, {"op": "split", "layer_id": layer_id, "at_time": t1})
    if t0 > start + timebase.FRAME_EPS:
        _op_split(doc, {"op": "split", "layer_id": layer_id, "at_time": t0})

    # Locate the middle piece: the sibling that now starts at t0. (When t0 ==
    # start no front cut happened and the original layer_id still starts at t0.)
    parent, _index = _require_locate(doc, layer_id, op="retime_segment")
    middle = None
    for child in parent["children"]:
        if isinstance(child, dict) and abs(_round_t(model._as_float(child.get("start"))) - t0) <= timebase.FRAME_EPS:
            middle = child
            break
    if middle is None:  # pragma: no cover — defensive; the cuts guarantee it.
        raise LayerPatchError("E_RANGE", f"retime_segment: could not isolate segment at t0={t0}")

    # Capture the middle's placement *before* the speed change. Its current end is
    # where the trailing piece(s) begin (the t1 cut). ``set_speed`` rescales the
    # middle's duration, so without rippling the tail would OVERLAP the slowed
    # middle (speed < 1) or leave a GAP after a sped-up one (speed > 1).
    middle_start = _round_t(model._as_float(middle.get("start")))
    old_middle_duration = model._as_float(middle.get("duration"))
    old_middle_end = _round_t(middle_start + old_middle_duration)

    _op_set_speed(doc, {"op": "set_speed", "layer_id": str(middle["id"]), "speed": speed})

    # Ripple: shift every trailing same-layer split piece (those that started at or
    # past the middle's old end — i.e. the tail produced by the t1 cut, and any
    # piece beyond it) by the duration delta, so the tail begins exactly at the
    # middle's NEW end. No overlap, no gap; starts stay frame-snapped because the
    # delta is a difference of frame-snapped placements.
    new_middle_duration = model._as_float(middle.get("duration"))
    delta = _round_t(new_middle_duration - old_middle_duration)
    if delta != 0.0:
        for child in parent["children"]:
            if not isinstance(child, dict) or child is middle:
                continue
            child_start = _round_t(model._as_float(child.get("start")))
            if child_start >= old_middle_end - timebase.FRAME_EPS:
                child["start"] = _round_t(child_start + delta)

    _set_selection(doc, [str(middle["id"])])


@register_op("set_time_remap", source="core")
def _op_set_time_remap(doc: dict[str, Any], op: dict[str, Any]) -> None:
    """Attach a time-remap (speed-ramp) curve to a layer.

    ``keyframes`` is a list of ``{"t": output_seconds, "value": source_seconds,
    "interp": "linear"|"hold"}`` points: at output-time ``t`` the layer samples
    its source at ``value`` seconds. ``extrapolate`` (``hold``/``loop``/
    ``pingpong``, default ``hold``) governs output times outside the curve span.

    Mutual exclusion with a non-unit constant ``speed``: a remap *is* the speed
    curve, so this op CLEARS any constant speed back to ``1.0`` (documented; we
    reset rather than raise so a single call fully describes the retime). Passing
    an empty keyframe list, or a point missing ``t``/``value``, raises ``E_ARG``;
    two keyframes with the same output ``t`` mapping to different source values
    raise ``E_RANGE`` (an ambiguous, non-functional curve).
    """
    layer = _require_layer(doc, _require_arg(op, "layer_id"), op="set_time_remap")
    raw = _require_arg(op, "keyframes")
    if not isinstance(raw, list) or not raw:
        raise LayerPatchError("E_ARG", "set_time_remap: keyframes must be a non-empty list")

    points: list[dict[str, Any]] = []
    for pt in raw:
        if not isinstance(pt, dict) or pt.get("t") is None or pt.get("value") is None:
            raise LayerPatchError("E_ARG", "set_time_remap: each keyframe needs t and value")
        interp = str(pt.get("interp") or "linear")
        if interp not in TIME_REMAP_INTERP:
            raise LayerPatchError(
                "E_ARG", f"set_time_remap: unknown interp {interp!r} (use linear|hold)"
            )
        points.append({
            "t": _round_t(pt.get("t")),
            "value": _round_t(pt.get("value")),
            "interp": interp,
        })
    points.sort(key=lambda k: k["t"])

    # Reject an ambiguous (non-functional) curve: same output t, different source.
    for prev, nxt in zip(points, points[1:]):
        if abs(prev["t"] - nxt["t"]) <= 1e-9 and abs(prev["value"] - nxt["value"]) > 1e-9:
            raise LayerPatchError(
                "E_RANGE",
                f"set_time_remap: two keyframes at t={prev['t']} map to different source times",
            )

    extrapolate = str(op.get("extrapolate") or "hold")
    if extrapolate not in TIME_REMAP_EXTRAPOLATE:
        raise LayerPatchError(
            "E_ARG",
            f"set_time_remap: unknown extrapolate {extrapolate!r} (use hold|loop|pingpong)",
        )

    layer["time_remap"] = {"keyframes": points, "extrapolate": extrapolate}
    # A remap supersedes constant speed; clear it so the retime is unambiguous.
    layer["speed"] = 1.0


def _reverse_layer_in_place(doc: dict[str, Any], layer: dict[str, Any]) -> None:
    """Make ``layer`` play BACKWARDS over its whole placement, frame-accurately.

    Reverse is expressed through the existing ``time_remap`` machinery: a
    ``time_remap`` is an *output_seconds -> source_seconds* curve that compile
    turns into a nearest-frame ``time_map_fn`` (``src_frame =
    to_frame(eval(curve, k/fps) - source_in, fps)``). So to play the clip
    backwards we flip that curve along the OUTPUT-time axis, anchored on exact
    frame boundaries so the result is frame-for-frame the forward clip reversed.

    Let ``N = to_frame(duration, fps)`` be the layer's output-frame count and
    ``t_last = to_seconds(N - 1, fps)`` the last output frame's time. We mirror
    the curve about ``t_last`` (the curve's own output extent), not ``duration``:
    mirroring about ``t_last`` is the construction for which ``time_map_fn`` lands
    on integer-exact frames and is a true *involution* (reverse twice == original
    mapping, frame for frame).

    * **No existing remap** (constant speed ``s``): the forward source-second
      mapping is ``src(k) = source_in + (k/fps)*s``. The reversed straight line
      is ``t=0 -> source_in + t_last*s`` … ``t=t_last -> source_in`` — source
      runs from its last sampled frame down to ``source_in`` over the output
      span. (``set_time_remap`` semantics: the remap supersedes constant speed,
      so ``speed`` resets to 1.)
    * **Existing remap**: every keyframe ``(t, value)`` maps to
      ``(t_curve_last - t, value)`` where ``t_curve_last`` is the largest
      keyframe ``t`` (the curve's output extent). Re-sorted, this mirrors the
      sampled source value about the curve mid-time, so applying it twice
      restores the original curve exactly.

    ``duration`` is unchanged (reverse preserves length), so callers never need
    to ripple following siblings. Every value is a difference of already-rounded
    times rounded to ``TIME_NDIGITS``, so the doc round-trips byte-stably.
    """
    fps = _canvas_fps(doc)
    duration = _round_t(model._as_float(layer.get("duration")))
    n_frames = max(1, timebase.to_frame(duration, fps))
    t_last = _round_t(timebase.to_seconds(n_frames - 1, fps))

    existing = layer.get("time_remap")
    if isinstance(existing, dict) and existing.get("keyframes"):
        # Mirror an existing curve about ``t_last`` — the layer's LAST RENDERED
        # output frame ((N-1)/fps), NOT ``max(keyframe t)``. A user-authored curve
        # usually spans to ``duration`` (= N/fps, one frame past the last rendered
        # frame); mirroring about that pivots one frame too far and renders a wrong
        # source frame at output 0 for interior remaps. Anchoring on t_last matches
        # the constant-speed branch and is a true involution for nearest-frame
        # mapping (a curve produced by the else branch already maxes at t_last, so
        # reverse-twice is unchanged).
        kfs = [pt for pt in existing["keyframes"] if isinstance(pt, dict)]
        pivot = t_last
        points = [
            {
                "t": _round_t(pivot - model._as_float(pt.get("t"))),
                "value": _round_t(pt.get("value")),
                "interp": str(pt.get("interp") or "linear"),
            }
            for pt in kfs
        ]
        extrapolate = str(existing.get("extrapolate") or "hold")
    else:
        # Constant-speed layer: synthesise the reversed straight-line curve whose
        # endpoints sit on exact frames (anchored on t_last, not source_out, so
        # nearest-frame mapping mirrors the forward frames one-for-one).
        source_in = _round_t(model._as_float(layer.get("source_in")))
        speed = model._as_float(layer.get("speed")) or 1.0
        hi = _round_t(source_in + t_last * speed)
        points = [
            {"t": 0.0, "value": hi, "interp": "linear"},
            {"t": t_last, "value": source_in, "interp": "linear"},
        ]
        extrapolate = "hold"

    # Route through set_time_remap so curve validation, sorting and the
    # speed->1.0 reset are the shared, already-tested code path.
    _op_set_time_remap(doc, {
        "op": "set_time_remap",
        "layer_id": str(layer["id"]),
        "keyframes": points,
        "extrapolate": extrapolate,
    })


@register_op("reverse", source="core")
def _op_reverse(doc: dict[str, Any], op: dict[str, Any]) -> None:
    """Play a layer (or the sub-segment ``[t0, t1]``) BACKWARDS, frame-accurately.

    Reverse is built entirely on the existing time machinery — it composes a
    ``time_remap`` curve so that output time samples the source running from
    ``source_out`` down to ``source_in`` (linear, nearest-frame) over the
    affected span (see :func:`_reverse_layer_in_place`). Because a remap *is* the
    output->source curve, reversing twice mirrors the curve back to its original
    mapping (an involution), so a whole-layer reverse round-trips.

    Whole layer (no ``t0``/``t1``): the layer itself is reversed in place; its
    ``duration`` is preserved.

    Sub-segment (``t0``/``t1`` given): reuses the ``split`` approach of
    :func:`_op_retime_segment` — snap ``t0``/``t1`` to frame boundaries, ``split``
    at ``t1`` then at ``t0`` so the middle ``[t0, t1]`` piece is its own layer,
    and reverse *that* piece. RIPPLE is not needed: reverse keeps each piece's
    duration, so the head/tail stay put and the timeline is unchanged in length.

    Args: ``layer_id`` (required); optional ``t0`` / ``t1`` (output seconds on the
    parent timeline; or ``frame0`` / ``frame1`` in frames), snapped to frames.

    Errors: ``E_ARG`` when ``layer_id`` is missing, or only one of ``t0``/``t1``
    is given; ``E_NOT_FOUND`` when the layer id does not exist; ``E_RANGE`` when
    ``[t0, t1]`` is empty/inverted or not within the layer's
    ``[start, start + duration]``.
    """
    layer = _require_layer(doc, _require_arg(op, "layer_id"), op="reverse")
    layer_id = str(layer["id"])

    t0 = _seconds_or_frame(doc, op, "t0", "frame0", op_name="reverse")
    t1 = _seconds_or_frame(doc, op, "t1", "frame1", op_name="reverse")

    # Whole-layer reverse when neither edge is given.
    if t0 is None and t1 is None:
        _reverse_layer_in_place(doc, layer)
        _set_selection(doc, [layer_id])
        return

    # A partial range (only one edge) is ambiguous.
    if t0 is None or t1 is None:
        raise LayerPatchError(
            "E_ARG", "reverse: give both 't0' and 't1' (or 'frame0'/'frame1'), or neither"
        )

    # Snap both edges to exact frame boundaries so the cuts are frame-accurate.
    fps = _canvas_fps(doc)
    t0 = _round_t(timebase.snap_seconds(t0, fps))
    t1 = _round_t(timebase.snap_seconds(t1, fps))

    start = _round_t(model._as_float(layer.get("start")))
    duration = model._as_float(layer.get("duration"))
    end = _round_t(start + duration)

    if not (t0 < t1):
        raise LayerPatchError("E_RANGE", f"reverse: empty range [{t0}, {t1}]")
    if t0 < start - timebase.FRAME_EPS or t1 > end + timebase.FRAME_EPS:
        raise LayerPatchError(
            "E_RANGE",
            f"reverse: range [{t0}, {t1}] is not within the layer [{start}, {end}]",
        )

    # Cut the back edge first (at t1), then the front edge (at t0): doing t1 first
    # keeps the front piece carrying the original layer_id so the second split is
    # found by the same id. Each split is skipped when the edge already sits on a
    # layer boundary. After both cuts the segment is the piece whose start == t0.
    if t1 < end - timebase.FRAME_EPS:
        _op_split(doc, {"op": "split", "layer_id": layer_id, "at_time": t1})
    if t0 > start + timebase.FRAME_EPS:
        _op_split(doc, {"op": "split", "layer_id": layer_id, "at_time": t0})

    parent, _index = _require_locate(doc, layer_id, op="reverse")
    middle = None
    for child in parent["children"]:
        if isinstance(child, dict) and abs(_round_t(model._as_float(child.get("start"))) - t0) <= timebase.FRAME_EPS:
            middle = child
            break
    if middle is None:  # pragma: no cover — defensive; the cuts guarantee it.
        raise LayerPatchError("E_RANGE", f"reverse: could not isolate segment at t0={t0}")

    _reverse_layer_in_place(doc, middle)
    _set_selection(doc, [str(middle["id"])])


@register_op("ripple_delete", source="core")
def _op_ripple_delete(doc: dict[str, Any], op: dict[str, Any]) -> None:
    """Delete a layer AND close the gap by rippling later same-lane siblings.

    The DaVinci-style ripple delete: removing a clip pulls every LATER clip on
    the SAME lane earlier by the deleted clip's duration, so no hole is left
    behind (contrast the plain ``delete_layer``, which leaves the gap).

    Only direct siblings (same parent) on the SAME lane whose ``start`` is at or
    after the deleted layer's ``start`` are shifted, each by ``-duration``. Other
    lanes are untouched (they are independent tracks), and ``doc_duration``
    shrinks accordingly when the deleted clip was the extent driver.

    Args: ``layer_id`` (required).

    Errors: ``E_ARG`` when ``layer_id`` is missing; ``E_NOT_FOUND`` when the
    layer id does not exist; ``E_ROOT`` when targeting the root composition.
    """
    layer_id = str(_require_arg(op, "layer_id"))
    if layer_id == str(doc["root"].get("id")):
        raise LayerPatchError("E_ROOT", "ripple_delete: cannot delete the root composition")

    # Capture the deleted layer's lane / start / duration BEFORE removing it.
    target, parent, _index = _splice_out(doc, layer_id, op="ripple_delete")
    try:
        lane = int(model._as_float(target.get("lane")))
    except (TypeError, ValueError):
        lane = 0
    deleted_start = _round_t(model._as_float(target.get("start")))
    deleted_duration = _round_t(model._as_float(target.get("duration")))

    # Drop mattes that pointed at the now-deleted layer (mirror delete_layer).
    for node in model.walk(doc["root"]):
        mask = node.get("mask")
        if isinstance(mask, dict) and str(mask.get("source_layer_id")) == layer_id:
            node["mask"] = None

    # Ripple: same-parent, same-lane siblings that started at/after the deleted
    # clip slide earlier by its duration, closing the gap. Other lanes untouched.
    if deleted_duration > 0:
        for child in parent.get("children") or []:
            if not isinstance(child, dict):
                continue
            try:
                child_lane = int(model._as_float(child.get("lane")))
            except (TypeError, ValueError):
                child_lane = 0
            if child_lane != lane:
                continue
            child_start = _round_t(model._as_float(child.get("start")))
            if child_start >= deleted_start - timebase.FRAME_EPS:
                child["start"] = _round_t(max(0.0, child_start - deleted_duration))


@register_op("set_lane", source="core")
def _op_set_lane(doc: dict[str, Any], op: dict[str, Any]) -> None:
    """Assign an EXISTING layer to a timeline lane (track).

    ``lane`` is the stacked-track index ``compile`` reads via
    ``_lane_ordered_children`` (a stable sort): a HIGHER lane composites ABOVE a
    lower one, ties keep tree order, and the default lane ``0`` reproduces plain
    tree order byte-for-byte. This op is the explicit, single-purpose way to put a
    layer onto a second timeline / track (``move_layer`` can also relane, but
    ``set_lane`` says exactly that and nothing else).

    Args: ``layer_id``, ``lane`` (int — negatives sink below lane 0).

    Errors: ``E_ARG`` when ``layer_id`` / ``lane`` is missing or ``lane`` is not
    an integer; ``E_NOT_FOUND`` when the layer id does not exist.
    """
    layer = _require_layer(doc, _require_arg(op, "layer_id"), op="set_lane")
    raw = _require_arg(op, "lane")
    # Accept ints (and bools are rejected) and integral floats like 2.0; anything
    # with a fractional part — or a non-number — is a lane error (E_ARG), because
    # lanes index discrete tracks.
    if isinstance(raw, bool):
        raise LayerPatchError("E_ARG", "set_lane: lane must be an integer")
    if isinstance(raw, int):
        lane = raw
    elif isinstance(raw, float) and float(raw).is_integer():
        lane = int(raw)
    else:
        raise LayerPatchError("E_ARG", f"set_lane: lane must be an integer, got {raw!r}")
    layer["lane"] = lane


@register_op("set_range", source="core")
def _op_set_range(doc: dict[str, Any], op: dict[str, Any]) -> None:
    """Frame-native one-shot placement: set start + duration from a frame range.

    Given an EXISTING layer and an inclusive-of-``frame_in`` / exclusive-of-
    ``frame_out`` frame window, this sets the layer's parent-timeline placement
    directly from frames via the canvas timebase::

        start    = to_seconds(frame_in,             fps)
        duration = to_seconds(frame_out - frame_in, fps)

    Both edges are snapped to exact frame boundaries (``timebase.snap_seconds``)
    so the placement is frame-accurate regardless of float noise in the inputs.
    This is the ergonomic counterpart to ``set_time`` for agents that think in
    frames rather than seconds; it leaves ``source_in`` / ``source_out`` and
    speed untouched (it places the clip on the timeline, it does not retime it).

    Args: ``layer_id``, ``frame_in`` (int frame), ``frame_out`` (int frame, must
    be ``> frame_in``).

    Errors: ``E_ARG`` when ``layer_id`` / ``frame_in`` / ``frame_out`` is missing
    or ``frame_out <= frame_in``; ``E_NOT_FOUND`` when the layer id does not exist.
    """
    layer = _require_layer(doc, _require_arg(op, "layer_id"), op="set_range")
    frame_in = model._as_float(_require_arg(op, "frame_in"))
    frame_out = model._as_float(_require_arg(op, "frame_out"))
    if not (frame_out > frame_in):
        raise LayerPatchError(
            "E_ARG", f"set_range: frame_out ({frame_out}) must be greater than frame_in ({frame_in})"
        )

    fps = _canvas_fps(doc)
    # Snap each frame-derived second to its exact frame boundary so the result is
    # frame-accurate (snap_seconds == to_seconds(to_frame(...))).
    start = _round_t(timebase.snap_seconds(timebase.to_seconds(frame_in, fps), fps))
    duration = _round_t(timebase.snap_seconds(timebase.to_seconds(frame_out - frame_in, fps), fps))
    layer["start"] = start
    layer["duration"] = duration


# ════════════════════════════════════════════════════════════════════════
# Per-layer (intra) ops
# ════════════════════════════════════════════════════════════════════════


@register_op("set_transform", source="core")
def _op_set_transform(doc: dict[str, Any], op: dict[str, Any]) -> None:
    layer = _require_layer(doc, _require_arg(op, "layer_id"), op="set_transform")
    transform = {**DEFAULT_TRANSFORM, **(layer.get("transform") or {})}
    if op.get("scale") is not None:
        transform["scale_x"] = model._as_float(op["scale"])
        transform["scale_y"] = model._as_float(op["scale"])
    for key in ("x", "y", "scale_x", "scale_y", "rotation", "anchor_x", "anchor_y"):
        if op.get(key) is not None:
            transform[key] = model._as_float(op[key])
    layer["transform"] = transform


@register_op("set_opacity", source="core")
def _op_set_opacity(doc: dict[str, Any], op: dict[str, Any]) -> None:
    layer = _require_layer(doc, _require_arg(op, "layer_id"), op="set_opacity")
    layer["opacity"] = max(0.0, min(1.0, model._as_float(_require_arg(op, "opacity"))))


@register_op("set_blend_mode", source="core")
def _op_set_blend_mode(doc: dict[str, Any], op: dict[str, Any]) -> None:
    layer = _require_layer(doc, _require_arg(op, "layer_id"), op="set_blend_mode")
    mode = str(_require_arg(op, "blend_mode"))
    # Unknown modes are allowed (an extension may render them) but core modes
    # are spell-checked so typos surface early.
    layer["blend_mode"] = mode


# ── inter-layer ──────────────────────────────────────────────────────────


@register_op("set_mask", source="core")
def _op_set_mask(doc: dict[str, Any], op: dict[str, Any]) -> None:
    layer = _require_layer(doc, _require_arg(op, "layer_id"), op="set_mask")
    if op.get("mask") is None and not op.get("kind"):
        layer["mask"] = None
        return
    spec = op.get("mask") if isinstance(op.get("mask"), dict) else {
        "kind": op.get("kind"),
        "source_layer_id": op.get("source_layer_id"),
        "shape": op.get("shape"),
        "invert": op.get("invert", False),
        "feather": op.get("feather", 0.0),
    }
    kind = str(spec.get("kind") or "shape")
    if kind not in MASK_KINDS:
        raise LayerPatchError("E_MASK", f"set_mask: unknown mask kind {kind!r}")
    if kind in {"alpha_matte", "luma_matte"}:
        src = str(spec.get("source_layer_id") or "")
        _require_layer(doc, src, op="set_mask")
        if src == str(layer.get("id")):
            raise LayerPatchError("E_MASK", "set_mask: matte cannot reference itself")
    layer["mask"] = model._normalize_mask(spec)


@register_op("clip_to_below", source="core")
def _op_clip_to_below(doc: dict[str, Any], op: dict[str, Any]) -> None:
    layer = _require_layer(doc, _require_arg(op, "layer_id"), op="clip_to_below")
    layer["clip_to_below"] = bool(op.get("enabled", True))


@register_op("add_adjustment_layer", source="core")
def _op_add_adjustment_layer(doc: dict[str, Any], op: dict[str, Any]) -> None:
    parent = doc["root"]
    if op.get("parent_id"):
        parent = _require_layer(doc, op["parent_id"], op="add_adjustment_layer")
        if not _is_container(parent):
            raise LayerPatchError("E_CONTAINER", "add_adjustment_layer: parent cannot hold children")
    effects = [model._normalize_effect(e) for e in (op.get("effects") or []) if isinstance(e, dict)]
    layer = new_layer(
        "adjustment",
        id=str(op["id"]) if op.get("id") else None,
        name=str(op.get("name") or "Adjustment"),
        start=_round_t(op.get("start") or 0.0),
        duration=_round_t(op.get("duration") or 0.0),
        effects=effects,
    )
    index = op.get("index")
    _insert(parent, layer, int(index) if index is not None else None)
    if op.get("select", True):
        _set_selection(doc, [layer["id"]])


# ── effects / colour ─────────────────────────────────────────────────────


@register_op("add_effect", source="core")
def _op_add_effect(doc: dict[str, Any], op: dict[str, Any]) -> None:
    layer = _require_layer(doc, _require_arg(op, "layer_id"), op="add_effect")
    spec = op.get("effect") if isinstance(op.get("effect"), dict) else {
        "type": op.get("type"),
        "params": op.get("params"),
        "id": op.get("effect_id"),
    }
    if "enabled" in op:
        spec["enabled"] = op["enabled"]
    if not spec.get("type"):
        raise LayerPatchError("E_ARG", "add_effect: effect needs a type")
    effect = model._normalize_effect(spec)
    effects = layer.setdefault("effects", [])
    index = op.get("index")
    if index is not None and 0 <= int(index) <= len(effects):
        effects.insert(int(index), effect)
    else:
        effects.append(effect)


@register_op("remove_effect", source="core")
def _op_remove_effect(doc: dict[str, Any], op: dict[str, Any]) -> None:
    layer = _require_layer(doc, _require_arg(op, "layer_id"), op="remove_effect")
    effect_id = str(_require_arg(op, "effect_id"))
    before = len(layer.get("effects") or [])
    layer["effects"] = [e for e in (layer.get("effects") or []) if str(e.get("id")) != effect_id]
    if len(layer["effects"]) == before:
        raise LayerPatchError("E_NOT_FOUND", f"remove_effect: effect {effect_id} not found")


@register_op("set_effect_params", source="core")
def _op_set_effect_params(doc: dict[str, Any], op: dict[str, Any]) -> None:
    layer = _require_layer(doc, _require_arg(op, "layer_id"), op="set_effect_params")
    effect_id = str(_require_arg(op, "effect_id"))
    params = _require_arg(op, "params")
    if not isinstance(params, dict):
        raise LayerPatchError("E_ARG", "set_effect_params: params must be an object")
    merge = bool(op.get("merge", True))
    for effect in layer.get("effects") or []:
        if str(effect.get("id")) == effect_id:
            effect["params"] = {**(effect.get("params") or {}), **params} if merge else dict(params)
            return
    raise LayerPatchError("E_NOT_FOUND", f"set_effect_params: effect {effect_id} not found")


@register_op("color_grade", source="core")
def _op_color_grade(doc: dict[str, Any], op: dict[str, Any]) -> None:
    """Convenience: upsert a single ``color_grade`` effect on the layer.

    Recognised params (all optional, all free-form floats so DaVinci-style
    grading can grow): brightness, contrast, saturation, exposure, temperature,
    tint, highlights, shadows, gamma, lift, gain.
    """
    layer = _require_layer(doc, _require_arg(op, "layer_id"), op="color_grade")
    grade_keys = {
        "brightness", "contrast", "saturation", "exposure", "temperature",
        "tint", "highlights", "shadows", "gamma", "lift", "gain", "hue", "vibrance",
    }
    params = {k: model._as_float(v) for k, v in op.items() if k in grade_keys}
    if isinstance(op.get("params"), dict):
        params.update({k: v for k, v in op["params"].items()})
    for effect in layer.setdefault("effects", []):
        if str(effect.get("type")) == "color_grade":
            effect["params"] = {**(effect.get("params") or {}), **params}
            effect["enabled"] = True
            return
    layer["effects"].append(model._normalize_effect({"type": "color_grade", "params": params}))


@register_op("flip_layer", source="core")
def _op_flip_layer(doc: dict[str, Any], op: dict[str, Any]) -> None:
    """Convenience: upsert a mirror effect on the layer's effect chain.

    Mirrors can be applied with direction: horizontal, vertical, or both.
    """
    layer = _require_layer(doc, _require_arg(op, "layer_id"), op="flip_layer")
    direction = str(op.get("direction", "horizontal"))
    params = {"direction": direction}
    for effect in layer.setdefault("effects", []):
        if str(effect.get("type")) == "mirror":
            effect["params"] = {**(effect.get("params") or {}), **params}
            effect["enabled"] = True
            return
    layer["effects"].append(model._normalize_effect({"type": "mirror", "params": params}))


@register_op("add_transition", source="core")
def _op_add_transition(doc: dict[str, Any], op: dict[str, Any]) -> None:
    layer = _require_layer(doc, _require_arg(op, "layer_id"), op="add_transition")
    kind = str(_require_arg(op, "kind"))
    duration = _round_t(op.get("duration") or 0.5)
    at = str(op.get("at") or "in")
    if at not in {"in", "out", "both"}:
        raise LayerPatchError("E_ARG", "add_transition: at must be in / out / both")
    transitions = layer.setdefault("props", {}).setdefault("transitions", {})
    descriptor = {"kind": kind, "duration": duration}
    for edge in (("in", "out") if at == "both" else (at,)):
        transitions[edge] = dict(descriptor)


# ── keyframes ─────────────────────────────────────────────────────────────


@register_op("set_keyframe", source="core")
def _op_set_keyframe(doc: dict[str, Any], op: dict[str, Any]) -> None:
    layer = _require_layer(doc, _require_arg(op, "layer_id"), op="set_keyframe")
    prop = str(_require_arg(op, "property"))
    # Time may be seconds (``t``) or frames (``frame``); doc stays seconds-canonical.
    t = _seconds_or_frame(doc, op, "t", "frame", op_name="set_keyframe")
    if t is None:
        raise LayerPatchError("E_ARG", "set_keyframe: missing required arg 't' (or 'frame')")
    interp = str(op.get("interp") or "linear")
    if interp not in INTERP_KINDS:
        raise LayerPatchError("E_ARG", f"set_keyframe: unknown interp {interp!r}")
    track = layer.setdefault("keyframes", {}).setdefault(prop, [])
    track[:] = [k for k in track if abs(model._as_float(k.get("t")) - t) > 1e-9]
    track.append({"t": t, "value": op.get("value"), "interp": interp})
    track.sort(key=lambda k: k["t"])


@register_op("remove_keyframe", source="core")
def _op_remove_keyframe(doc: dict[str, Any], op: dict[str, Any]) -> None:
    layer = _require_layer(doc, _require_arg(op, "layer_id"), op="remove_keyframe")
    prop = str(_require_arg(op, "property"))
    # Time may be seconds (``t``) or frames (``frame``); doc stays seconds-canonical.
    t = _seconds_or_frame(doc, op, "t", "frame", op_name="remove_keyframe")
    if t is None:
        raise LayerPatchError("E_ARG", "remove_keyframe: missing required arg 't' (or 'frame')")
    track = layer.get("keyframes", {}).get(prop)
    if not track:
        raise LayerPatchError("E_NOT_FOUND", f"remove_keyframe: no track {prop!r}")
    kept = [k for k in track if abs(model._as_float(k.get("t")) - t) > 1e-9]
    if len(kept) == len(track):
        raise LayerPatchError("E_NOT_FOUND", f"remove_keyframe: no keyframe at t={t}")
    if kept:
        layer["keyframes"][prop] = kept
    else:
        layer["keyframes"].pop(prop, None)


@register_op("set_expression", source="core")
def _op_set_expression(doc: dict[str, Any], op: dict[str, Any]) -> None:
    """Bind an expression string to a layer property (time-driven animation).

    Expressions are evaluated at render-time using SafeEvaluator. This allows
    time-dependent properties without manually creating keyframes.

    Args:
        layer_id: Layer to bind expression to
        property: Property name (e.g., "opacity", "transform.x", "transform.rotation")
        expression: Expression string (e.g., "0.5 + 0.2 * sin(time * 8)")

    The expression has access to:
        - time: Current frame time in seconds
        - duration: Layer total duration in seconds
        - width, height: Canvas dimensions

    Precedence (at render): expression > keyframes > static value.
    Use expressions for rhythmic, data-driven, or time-locked properties.
    """
    from gemia.expressions import validate_expression, ExprError
    
    layer = _require_layer(doc, _require_arg(op, "layer_id"), op="set_expression")
    prop = str(_require_arg(op, "property"))
    expression_str = str(_require_arg(op, "expression")).strip()
    
    if not expression_str:
        raise LayerPatchError("E_ARG", "set_expression: expression cannot be empty")
    
    # Validate expression syntax & safety
    is_valid, err_msg = validate_expression(expression_str)
    if not is_valid:
        raise LayerPatchError("E_UNSAFE", f"set_expression: invalid expression: {err_msg}")
    
    # Store in layer.expressions dict
    expressions = layer.setdefault("expressions", {})
    expressions[prop] = {
        "expr": expression_str,
    }




# ════════════════════════════════════════════════════════════════════════
# Text styling
# ════════════════════════════════════════════════════════════════════════


@register_op("set_text", source="core")
def _op_set_text(doc: dict[str, Any], op: dict[str, Any]) -> None:
    """Merge text props into a layer's props (only provided keys are updated).

    Recognised props: text, font, font_size, color, align, stroke, shadow,
    background, line_spacing. All optional; omitted props are left unchanged.

    For nested dict props (stroke, shadow), performs shallow merge: new values
    override old, but unspecified keys in the old dict are preserved.
    """
    layer = _require_layer(doc, _require_arg(op, "layer_id"), op="set_text")
    props = layer.setdefault("props", {})

    # Text props allowed in this op.
    text_keys = {"text", "font", "font_size", "color", "align", "stroke", "shadow", "background", "line_spacing"}

    for key in text_keys:
        if key in op:
            # For nested dicts (stroke, shadow), do shallow merge; otherwise replace.
            if key in ("stroke", "shadow") and isinstance(op[key], dict) and isinstance(props.get(key), dict):
                props[key] = {**props[key], **op[key]}
            else:
                props[key] = op[key]


# ════════════════════════════════════════════════════════════════════════
# Audio properties
# ════════════════════════════════════════════════════════════════════════


@register_op("set_volume", source="core")
def _op_set_volume(doc: dict[str, Any], op: dict[str, Any]) -> None:
    """Set the volume level (linear, 0..1+, default 1.0) in layer props."""
    layer = _require_layer(doc, _require_arg(op, "layer_id"), op="set_volume")
    volume = model._as_float(_require_arg(op, "volume"))
    layer.setdefault("props", {})["volume"] = volume


#: Audio fade curve shapes a downstream mixer can apply to a fade ramp.
_AUDIO_FADE_SHAPES: frozenset[str] = frozenset({"linear", "exp", "log"})


@register_op("set_audio_fade", source="core")
def _op_set_audio_fade(doc: dict[str, Any], op: dict[str, Any]) -> None:
    """Set fade-in and/or fade-out durations (in seconds) in layer props.

    Durations stay as plain scalars under ``props.fade_in`` / ``props.fade_out``
    (unchanged contract). The optional ``shape`` selects the fade *curve* a
    downstream mixer should ramp along — one of ``linear`` (default), ``exp``
    (slow start, fast finish) or ``log`` (fast start, slow finish) — and is
    stored alongside as ``props.fade_in_shape`` / ``props.fade_out_shape`` so the
    structured audio metadata travels with the layer (lumenframe compile is
    video-only). The shape applies to whichever edge(s) this op writes.
    """
    layer = _require_layer(doc, _require_arg(op, "layer_id"), op="set_audio_fade")
    props = layer.setdefault("props", {})

    shape = op.get("shape")
    if shape is not None:
        shape = str(shape)
        if shape not in _AUDIO_FADE_SHAPES:
            raise LayerPatchError(
                "E_ARG",
                f"set_audio_fade: unknown fade shape {shape!r} "
                f"(expected one of {sorted(_AUDIO_FADE_SHAPES)})",
            )
    else:
        shape = "linear"

    if op.get("fade_in") is not None:
        props["fade_in"] = model._as_float(op["fade_in"])
        props["fade_in_shape"] = shape
    if op.get("fade_out") is not None:
        props["fade_out"] = model._as_float(op["fade_out"])
        props["fade_out_shape"] = shape


@register_op("mute_layer", source="core")
def _op_mute_layer(doc: dict[str, Any], op: dict[str, Any]) -> None:
    """Mute or unmute a layer (default muted=true) in props."""
    layer = _require_layer(doc, _require_arg(op, "layer_id"), op="mute_layer")
    muted = bool(op.get("muted", True))
    layer.setdefault("props", {})["muted"] = muted


@register_op("duck_audio", source="core")
def _op_duck_audio(doc: dict[str, Any], op: dict[str, Any]) -> None:
    """Store a sidechain-ducking descriptor on an audio layer.

    Ducking lowers this layer's level whenever a *target* layer (typically a
    voiceover/dialogue track) is audible — the CapCut/DaVinci "auto-duck music
    under speech" behaviour. lumenframe compile is video-only, so this is pure
    structured metadata for a downstream mixer; it is written to
    ``props.ducking = {target_id, amount, attack, release}``:

    * ``target_id`` — the layer that triggers the duck (must exist → else
      ``E_NOT_FOUND``);
    * ``amount`` — how much to attenuate while the target is audible. A value
      ``<= 0`` is read as dB of attenuation (e.g. ``-12``); a value in ``0..1``
      is read as a linear gain floor. Stored verbatim (default ``-12`` dB);
    * ``attack`` / ``release`` — duck-in / duck-out times in seconds
      (defaults ``0.05`` / ``0.3``); both must be ``>= 0`` → else ``E_RANGE``.
    """
    layer = _require_layer(doc, _require_arg(op, "layer_id"), op="duck_audio")
    target_id = str(_require_arg(op, "target_id"))
    # The target must be a real, existing layer — surface E_NOT_FOUND otherwise.
    _require_layer(doc, target_id, op="duck_audio")

    amount: float = model._as_float(op["amount"]) if op.get("amount") is not None else -12.0
    attack = model._as_float(op["attack"]) if op.get("attack") is not None else 0.05
    release = model._as_float(op["release"]) if op.get("release") is not None else 0.3
    if attack < 0 or release < 0:
        raise LayerPatchError(
            "E_RANGE", "duck_audio: attack and release must be >= 0"
        )

    layer.setdefault("props", {})["ducking"] = {
        "target_id": target_id,
        "amount": amount,
        "attack": attack,
        "release": release,
    }


# ════════════════════════════════════════════════════════════════════════
# Animation presets
# ════════════════════════════════════════════════════════════════════════


_EASING_MAP: dict[str, str] = {
    "linear": "linear",
    "ease": "ease",
    "ease_in": "ease_in",
    "ease_out": "ease_out",
}


@register_op("animate_layer", source="core")
def _op_animate_layer(doc: dict[str, Any], op: dict[str, Any]) -> None:
    """Generate keyframes on a layer using an animation preset.

    Presets: fade_in, fade_out, fly_in_left, fly_in_right, fly_in_top,
    fly_in_bottom, fly_out_left, fly_out_right, fly_out_top, fly_out_bottom,
    zoom_in, zoom_out, ken_burns.

    Times are absolute (global document time). Fly-in/out offsets use canvas
    dimensions from doc. Fly-out durations clamp to layer duration.
    """
    layer = _require_layer(doc, _require_arg(op, "layer_id"), op="animate_layer")
    preset = str(_require_arg(op, "preset"))
    duration = model._as_float(op.get("duration") or 0.5)
    easing = str(op.get("easing") or "linear")
    interp = _EASING_MAP.get(easing, "linear")

    layer_start = model._as_float(layer.get("start"))
    layer_duration = model._as_float(layer.get("duration"))

    # Get canvas dimensions for fly-in/out offsets.
    canvas = doc.get("canvas", {})
    canvas_width = int(canvas.get("width", 1920))
    canvas_height = int(canvas.get("height", 1080))

    # Clamp duration to layer duration.
    duration = min(duration, layer_duration)

    if preset == "fade_in":
        # Opacity 0 → 1, from layer start to start + duration.
        keyframes = layer.setdefault("keyframes", {})
        track = keyframes.setdefault("opacity", [])
        track[:] = [k for k in track if not (
            abs(model._as_float(k.get("t")) - layer_start) < 1e-9 or
            abs(model._as_float(k.get("t")) - _round_t(layer_start + duration)) < 1e-9
        )]
        track.append({"t": _round_t(layer_start), "value": 0.0, "interp": interp})
        track.append({"t": _round_t(layer_start + duration), "value": 1.0, "interp": interp})
        track.sort(key=lambda k: k["t"])

    elif preset == "fade_out":
        # Opacity 1 → 0, from layer end - duration to layer end.
        end_t = _round_t(layer_start + layer_duration)
        start_t = _round_t(max(layer_start, end_t - duration))
        keyframes = layer.setdefault("keyframes", {})
        track = keyframes.setdefault("opacity", [])
        track[:] = [k for k in track if not (
            abs(model._as_float(k.get("t")) - start_t) < 1e-9 or
            abs(model._as_float(k.get("t")) - end_t) < 1e-9
        )]
        track.append({"t": start_t, "value": 1.0, "interp": interp})
        track.append({"t": end_t, "value": 0.0, "interp": interp})
        track.sort(key=lambda k: k["t"])

    elif preset in {"fly_in_left", "fly_in_right", "fly_in_top", "fly_in_bottom"}:
        # Position starts off-canvas, ends at 0.
        keyframes = layer.setdefault("keyframes", {})
        start_t = _round_t(layer_start)
        end_t = _round_t(layer_start + duration)

        if preset == "fly_in_left":
            prop = "transform.x"
            track = keyframes.setdefault(prop, [])
            track[:] = [k for k in track if not (
                abs(model._as_float(k.get("t")) - start_t) < 1e-9 or
                abs(model._as_float(k.get("t")) - end_t) < 1e-9
            )]
            track.append({"t": start_t, "value": float(-canvas_width), "interp": interp})
            track.append({"t": end_t, "value": 0.0, "interp": interp})

        elif preset == "fly_in_right":
            prop = "transform.x"
            track = keyframes.setdefault(prop, [])
            track[:] = [k for k in track if not (
                abs(model._as_float(k.get("t")) - start_t) < 1e-9 or
                abs(model._as_float(k.get("t")) - end_t) < 1e-9
            )]
            track.append({"t": start_t, "value": float(canvas_width), "interp": interp})
            track.append({"t": end_t, "value": 0.0, "interp": interp})

        elif preset == "fly_in_top":
            prop = "transform.y"
            track = keyframes.setdefault(prop, [])
            track[:] = [k for k in track if not (
                abs(model._as_float(k.get("t")) - start_t) < 1e-9 or
                abs(model._as_float(k.get("t")) - end_t) < 1e-9
            )]
            track.append({"t": start_t, "value": float(-canvas_height), "interp": interp})
            track.append({"t": end_t, "value": 0.0, "interp": interp})

        elif preset == "fly_in_bottom":
            prop = "transform.y"
            track = keyframes.setdefault(prop, [])
            track[:] = [k for k in track if not (
                abs(model._as_float(k.get("t")) - start_t) < 1e-9 or
                abs(model._as_float(k.get("t")) - end_t) < 1e-9
            )]
            track.append({"t": start_t, "value": float(canvas_height), "interp": interp})
            track.append({"t": end_t, "value": 0.0, "interp": interp})

        track.sort(key=lambda k: k["t"])

    elif preset in {"fly_out_left", "fly_out_right", "fly_out_top", "fly_out_bottom"}:
        # Position goes from 0 to off-canvas at the end. Clamp duration upfront.
        duration = min(duration, layer_duration)
        keyframes = layer.setdefault("keyframes", {})
        start_t = _round_t(layer_start + layer_duration - duration)
        end_t = _round_t(layer_start + layer_duration)

        if preset == "fly_out_left":
            prop = "transform.x"
            track = keyframes.setdefault(prop, [])
            track[:] = [k for k in track if not (
                abs(model._as_float(k.get("t")) - start_t) < 1e-9 or
                abs(model._as_float(k.get("t")) - end_t) < 1e-9
            )]
            track.append({"t": start_t, "value": 0.0, "interp": interp})
            track.append({"t": end_t, "value": float(-canvas_width), "interp": interp})

        elif preset == "fly_out_right":
            prop = "transform.x"
            track = keyframes.setdefault(prop, [])
            track[:] = [k for k in track if not (
                abs(model._as_float(k.get("t")) - start_t) < 1e-9 or
                abs(model._as_float(k.get("t")) - end_t) < 1e-9
            )]
            track.append({"t": start_t, "value": 0.0, "interp": interp})
            track.append({"t": end_t, "value": float(canvas_width), "interp": interp})

        elif preset == "fly_out_top":
            prop = "transform.y"
            track = keyframes.setdefault(prop, [])
            track[:] = [k for k in track if not (
                abs(model._as_float(k.get("t")) - start_t) < 1e-9 or
                abs(model._as_float(k.get("t")) - end_t) < 1e-9
            )]
            track.append({"t": start_t, "value": 0.0, "interp": interp})
            track.append({"t": end_t, "value": float(-canvas_height), "interp": interp})

        elif preset == "fly_out_bottom":
            prop = "transform.y"
            track = keyframes.setdefault(prop, [])
            track[:] = [k for k in track if not (
                abs(model._as_float(k.get("t")) - start_t) < 1e-9 or
                abs(model._as_float(k.get("t")) - end_t) < 1e-9
            )]
            track.append({"t": start_t, "value": 0.0, "interp": interp})
            track.append({"t": end_t, "value": float(canvas_height), "interp": interp})

        track.sort(key=lambda k: k["t"])

    elif preset == "zoom_in":
        # Scale 1.0 → 1.5 (or similar) over duration.
        keyframes = layer.setdefault("keyframes", {})
        start_t = _round_t(layer_start)
        end_t = _round_t(layer_start + duration)

        for prop in ("transform.scale_x", "transform.scale_y"):
            track = keyframes.setdefault(prop, [])
            track[:] = [k for k in track if not (
                abs(model._as_float(k.get("t")) - start_t) < 1e-9 or
                abs(model._as_float(k.get("t")) - end_t) < 1e-9
            )]
            track.append({"t": start_t, "value": 1.0, "interp": interp})
            track.append({"t": end_t, "value": 1.5, "interp": interp})
            track.sort(key=lambda k: k["t"])

    elif preset == "zoom_out":
        # Scale 1.5 → 1.0 (shrink) over duration.
        keyframes = layer.setdefault("keyframes", {})
        start_t = _round_t(layer_start)
        end_t = _round_t(layer_start + duration)

        for prop in ("transform.scale_x", "transform.scale_y"):
            track = keyframes.setdefault(prop, [])
            track[:] = [k for k in track if not (
                abs(model._as_float(k.get("t")) - start_t) < 1e-9 or
                abs(model._as_float(k.get("t")) - end_t) < 1e-9
            )]
            track.append({"t": start_t, "value": 1.5, "interp": interp})
            track.append({"t": end_t, "value": 1.0, "interp": interp})
            track.sort(key=lambda k: k["t"])

    elif preset == "ken_burns":
        # Pan + zoom over the specified duration: scale 1.0 → 1.1, slight pan.
        duration = min(duration, layer_duration)
        keyframes = layer.setdefault("keyframes", {})
        start_t = _round_t(layer_start)
        end_t = _round_t(layer_start + duration)

        # Scale keyframes.
        for prop in ("transform.scale_x", "transform.scale_y"):
            track = keyframes.setdefault(prop, [])
            track[:] = [k for k in track if not (
                abs(model._as_float(k.get("t")) - start_t) < 1e-9 or
                abs(model._as_float(k.get("t")) - end_t) < 1e-9
            )]
            track.append({"t": start_t, "value": 1.0, "interp": interp})
            track.append({"t": end_t, "value": 1.1, "interp": interp})
            track.sort(key=lambda k: k["t"])

        # Pan keyframes (gentle x, y shift).
        for prop, start_val in [("transform.x", -30.0), ("transform.y", -20.0)]:
            track = keyframes.setdefault(prop, [])
            track[:] = [k for k in track if not (
                abs(model._as_float(k.get("t")) - start_t) < 1e-9 or
                abs(model._as_float(k.get("t")) - end_t) < 1e-9
            )]
            track.append({"t": start_t, "value": start_val, "interp": interp})
            track.append({"t": end_t, "value": 0.0, "interp": interp})
            track.sort(key=lambda k: k["t"])

    else:
        raise LayerPatchError("E_ARG", f"animate_layer: unknown preset {preset!r}")


# ════════════════════════════════════════════════════════════════════════
# CapCut-style preset sugar (speed_ramp / animate_text / apply_template)
#
# These three ops are pure *sugar* over the primitives above: each one builds a
# set of standard op dicts (or a time_remap curve) and delegates to the existing
# set_time_remap / set_keyframe / dispatch machinery. They introduce no new
# document fields and no resolver/compile change.
# ════════════════════════════════════════════════════════════════════════


#: Speed-ramp presets -> a profile of (output_fraction, source_fraction) control
#: points across the layer span. ``source_fraction`` is how far through the
#: source media we are at that ``output_fraction`` of the (unchanged) layer
#: duration. A segment whose source advances *slower* than output (slope < 1) is
#: slow-motion; faster (slope > 1) is fast-forward. Endpoints are always (0,0)
#: and (1,1) so the clip starts and ends on the same source frames.
_SPEED_RAMP_PROFILES: dict[str, list[tuple[float, float]]] = {
    # Fast in, slow middle, fast out — the classic "hero moment" ramp.
    "hero": [(0.0, 0.0), (0.3, 0.45), (0.7, 0.55), (1.0, 1.0)],
    # Quick energetic cuts: source races ahead in the middle (fast-forward).
    "montage": [(0.0, 0.0), (0.3, 0.15), (0.7, 0.85), (1.0, 1.0)],
    # Punch in: normal then a hard slow on the back half (bullet-time).
    "bullet": [(0.0, 0.0), (0.5, 0.5), (0.6, 0.55), (1.0, 1.0)],
    # Ease the speed up from a slow start (slow -> fast).
    "ease_in": [(0.0, 0.0), (0.5, 0.25), (1.0, 1.0)],
    # Ease the speed down to a slow finish (fast -> slow).
    "ease_out": [(0.0, 0.0), (0.5, 0.75), (1.0, 1.0)],
}


@register_op("speed_ramp", source="core")
def _op_speed_ramp(doc: dict[str, Any], op: dict[str, Any]) -> None:
    """Apply a named speed-ramp preset by emitting a ``time_remap`` curve.

    Presets (``preset``): ``hero`` (slow middle), ``montage`` (fast middle),
    ``bullet`` (slow back-half), ``ease_in`` (slow->fast), ``ease_out``
    (fast->slow). Each preset is a fixed profile of (output_fraction,
    source_fraction) control points; this op scales those fractions across the
    layer's output ``duration`` and its source range, builds the keyframes, and
    delegates to the existing ``set_time_remap`` handler (so the curve, its
    validation, and the constant-speed reset are all shared with that op).

    The output duration is preserved: a ramp redistributes *which* source frames
    play *when*, it does not change the clip length. A segment with slope < 1 is
    slow-motion; slope > 1 is fast-forward.
    """
    layer = _require_layer(doc, _require_arg(op, "layer_id"), op="speed_ramp")
    preset = str(_require_arg(op, "preset"))
    profile = _SPEED_RAMP_PROFILES.get(preset)
    if profile is None:
        raise LayerPatchError(
            "E_ARG",
            f"speed_ramp: unknown preset {preset!r} (use {', '.join(sorted(_SPEED_RAMP_PROFILES))})",
        )

    out_dur = model._as_float(layer.get("duration"))
    if out_dur <= 0:
        raise LayerPatchError("E_RANGE", "speed_ramp: layer has no duration to ramp")
    src_in = model._as_float(layer.get("source_in"))
    src_out = model._as_float(layer.get("source_out"))
    src_range = src_out - src_in
    if src_range <= 0:
        # No source span recorded (e.g. a synthetic layer) — ramp across the
        # output duration itself so the curve is still well-defined.
        src_range = out_dur

    keyframes = [
        {
            "t": _round_t(out_frac * out_dur),
            "value": _round_t(src_in + src_frac * src_range),
            "interp": "linear",
        }
        for out_frac, src_frac in profile
    ]
    # Delegate to set_time_remap so the curve validation + speed reset are shared.
    _op_set_time_remap(doc, {
        "op": "set_time_remap",
        "layer_id": layer["id"],
        "keyframes": keyframes,
        "extrapolate": str(op.get("extrapolate") or "hold"),
    })


@register_op("animate_text", source="core")
def _op_animate_text(doc: dict[str, Any], op: dict[str, Any]) -> None:
    """Animate a text layer with a CapCut-style title preset.

    Presets (``preset``): ``fade_in_words``, ``pop`` (scale punch 0->~1.2->1),
    ``wave`` (damped vertical settle), ``rise`` (slide up + fade in). The preset
    is expanded by :func:`lumenframe.text_anim.text_anim_ops` into plain
    ``set_keyframe`` ops on ``opacity`` / ``transform.*`` only (no new property,
    no resolver change); each is then run through the existing ``set_keyframe``
    handler so the keyframes are identical to hand-authored ones.
    """
    from lumenframe.text_anim import text_anim_ops, TextAnimError

    layer = _require_layer(doc, _require_arg(op, "layer_id"), op="animate_text")
    preset = str(_require_arg(op, "preset"))
    duration = model._as_float(op.get("duration") or 0.5)
    easing = str(op.get("easing") or "linear")
    try:
        sub_ops = text_anim_ops(
            layer["id"],
            preset,
            layer_start=model._as_float(layer.get("start")),
            layer_duration=model._as_float(layer.get("duration")),
            duration=duration,
            easing=easing,
        )
    except TextAnimError as err:
        raise LayerPatchError("E_ARG", str(err)) from err
    for sub in sub_ops:
        _op_set_keyframe(doc, sub)


@register_op("apply_template", source="core")
def _op_apply_template(doc: dict[str, Any], op: dict[str, Any]) -> None:
    """Stamp a named scene template (lower_third / intro) into the document.

    The template is a pure ``params -> [op dicts]`` function; this op looks it up
    in :data:`lumenframe.templates.TEMPLATES`, expands it with ``params``, and
    applies each resulting op through the *same* dispatch path used for every
    other op. Templates therefore introduce no new vocabulary — they are a macro
    over ``add_layer`` / ``set_transform`` / ``animate_text`` / ….
    """
    from lumenframe.templates import TEMPLATES, template_names

    name = str(_require_arg(op, "template"))
    template = TEMPLATES.get(name)
    if template is None:
        raise LayerPatchError(
            "E_ARG",
            f"apply_template: unknown template {name!r} (use {', '.join(template_names())})",
        )
    params = op.get("params") or {}
    if not isinstance(params, dict):
        raise LayerPatchError("E_ARG", "apply_template: params must be an object")
    try:
        sub_ops = template(**params)
    except TypeError as err:
        raise LayerPatchError("E_ARG", f"apply_template: bad params for {name!r}: {err}") from err
    for sub in sub_ops:
        _dispatch(doc, sub)


@register_op("apply_element", source="core")
def _op_apply_element(doc: dict[str, Any], op: dict[str, Any]) -> None:
    """Stamp a named graphic element (arrow / …) into the document as an overlay.

    Mirrors :func:`_op_apply_template` exactly, but for the element library: the
    element is a pure ``params -> [op dicts]`` overlay macro; this op looks it up
    in :data:`lumenframe.elements.ELEMENTS`, expands it with ``params``, and
    applies each resulting op through the *same* dispatch path used for every
    other op. Elements introduce no new vocabulary — they are a macro over
    ``add_shape`` / ``set_keyframe`` / … that draws one graphic and composes onto
    whatever is beneath.
    """
    from lumenframe.elements import ELEMENTS, element_names

    name = str(_require_arg(op, "element"))
    element = ELEMENTS.get(name)
    if element is None:
        raise LayerPatchError(
            "E_ARG",
            f"apply_element: unknown element {name!r} (use {', '.join(element_names())})",
        )
    params = op.get("params") or {}
    if not isinstance(params, dict):
        raise LayerPatchError("E_ARG", "apply_element: params must be an object")
    try:
        sub_ops = element(**params)
    except TypeError as err:
        raise LayerPatchError("E_ARG", f"apply_element: bad params for {name!r}: {err}") from err
    for sub in sub_ops:
        _dispatch(doc, sub)


# ── multi-layer compositing sugar ─────────────────────────────────────────
#
# These ops are *convenience macros* over the existing primitives (transform /
# mask / opacity-keyframes / add_layer). They introduce no new compile path:
# ``pip`` is pure transform + shape-mask (+ an optional helper shape layer),
# ``crossfade`` only writes opacity keyframes, ``add_gradient`` / ``add_shape``
# build plain ``gradient`` / ``shape`` layers per the shared layer schema. The
# gradient / shape *renderers* live elsewhere (lumenframe.resolve); here we only
# guarantee the produced layer dicts match the contract so a renderer can draw
# them.

#: Picture-in-picture corner -> (x_sign, y_sign) on the canvas-centre transform
#: (x>0 right, y>0 down — matching DEFAULT_TRANSFORM / _centred_position).
_PIP_CORNERS: dict[str, tuple[float, float]] = {
    "br": (+1.0, +1.0),
    "bl": (-1.0, +1.0),
    "tr": (+1.0, -1.0),
    "tl": (-1.0, -1.0),
}


def _ensure_gradient_layer_type() -> None:
    """Register the ``gradient`` layer type so ``add_gradient`` validates.

    ``shape`` is already a built-in (``model.LAYER_TYPES``), but ``gradient`` is
    new sugar: register a minimal, non-container spec idempotently. The renderer
    for it lives in :mod:`lumenframe.resolve`; this only teaches the registry /
    validator that the type exists. Idempotent so it is safe to call after a
    ``reset_for_tests`` (which drops non-built-in layer types).
    """
    from lumenframe.registry import layer_type_spec

    if layer_type_spec("gradient") is None:
        register_layer_type("gradient", {
            "container": False,
            "defaults": {},
            "source": "core",
            "description": (
                "A gradient fill layer (linear/radial) defined by colour stops; "
                "rasterised to a canvas-sized RGBA fill by the resolver."
            ),
        })


_ensure_gradient_layer_type()


def _append_blur_effect(layer: dict[str, Any], radius: float) -> None:
    """Append a ``gaussian_blur`` effect of ``radius`` px to ``layer``.

    Shared by the depth-of-field sugar (``pip`` background blur and
    ``focus_pull``): it uses the very same effect type / shape as ``add_effect``
    so the blur composites through the standard effect chain with no new compile
    path. The effect is *appended* (never replaces an existing chain entry).
    """
    effect = model._normalize_effect({"type": "gaussian_blur", "params": {"radius": radius}})
    layer.setdefault("effects", []).append(effect)


@register_op("set_blend", source="core")
def _op_set_blend(doc: dict[str, Any], op: dict[str, Any]) -> None:
    """Set a layer's blend mode, validated against the supported core set.

    Sugar over the raw ``blend_mode`` field that, unlike ``set_blend_mode``,
    *rejects* an unknown mode up front (``E_ARG``) so a typo never silently
    degrades to ``normal`` at compile. The accepted set is
    :data:`lumenframe.model.BLEND_MODES`.
    """
    layer = _require_layer(doc, _require_arg(op, "layer_id"), op="set_blend")
    mode = str(_require_arg(op, "mode"))
    if mode not in BLEND_MODES:
        raise LayerPatchError(
            "E_ARG",
            f"set_blend: unknown blend mode {mode!r} (use one of {', '.join(sorted(BLEND_MODES))})",
        )
    layer["blend_mode"] = mode


@register_op("pip", source="core")
def _op_pip(doc: dict[str, Any], op: dict[str, Any]) -> None:
    """Turn a layer into a picture-in-picture inset.

    Pure transform + mask (+ optional helper layer), no compile change:

    * sets ``transform.scale`` and ``transform.x/y`` so the scaled layer tucks
      into the chosen ``corner`` (br|bl|tr|tl) with ``margin`` (fraction of the
      canvas), or to an explicit ``x``/``y`` pixel offset if given;
    * attaches a full-frame rounded-rect ``shape`` mask so the inset has rounded
      corners (``radius`` in px, stored normalised as a fraction of the smaller
      canvas dimension so the mask is resolution-independent);
    * when ``border`` or ``shadow`` is requested, inserts one extra ``shape``
      layer *beneath* the pip (same transform, rounded rect) carrying the
      border stroke / shadow fill, per the shape-layer schema.

    Depth-of-field: when ``blur_background`` (px radius) is > 0, a
    ``gaussian_blur`` effect of that radius is appended to *every* sibling layer
    that sits **below** the pip in the same composition (lower z, i.e. drawn
    behind it), so the inset pops in focus over a softened background. The pip
    layer itself and any layers above it are left sharp. Absent or ``0`` leaves
    the doc byte-identical to the un-blurred pip.

    The layer's own opacity / time range / blend are left untouched.
    """
    layer_id = str(_require_arg(op, "layer_id"))
    parent, index = _require_locate(doc, layer_id, op="pip")
    layer = parent["children"][index]

    # Depth-of-field radius (px). Validate up front; the sibling-blur is applied
    # *after* the transform/mask are set but *before* any helper shape is spliced
    # in, so the helper (which frames the pip) never gets blurred.
    blur_bg = model._as_float(op.get("blur_background")) if op.get("blur_background") is not None else 0.0
    if blur_bg < 0:
        raise LayerPatchError("E_RANGE", "pip: blur_background must be >= 0")

    canvas = doc.get("canvas") if isinstance(doc.get("canvas"), dict) else {}
    width = int(canvas.get("width") or model.DEFAULT_CANVAS["width"])
    height = int(canvas.get("height") or model.DEFAULT_CANVAS["height"])

    scale = model._as_float(op.get("scale")) if op.get("scale") is not None else 0.3
    if scale <= 0:
        raise LayerPatchError("E_RANGE", "pip: scale must be > 0")
    margin = model._as_float(op.get("margin")) if op.get("margin") is not None else 0.04

    # Position: explicit x/y (pixels) wins; otherwise tuck into the named corner.
    if op.get("x") is not None or op.get("y") is not None:
        tx = model._as_float(op.get("x"))
        ty = model._as_float(op.get("y"))
    else:
        corner = str(op.get("corner") or "br")
        signs = _PIP_CORNERS.get(corner)
        if signs is None:
            raise LayerPatchError(
                "E_ARG",
                f"pip: unknown corner {corner!r} (use br|bl|tr|tl, or pass x/y)",
            )
        # Push the scaled content's edge to (margin) from the canvas edge: its
        # centre sits at half-canvas minus margin minus half the scaled size.
        x_mag = width * (0.5 - margin - scale / 2.0)
        y_mag = height * (0.5 - margin - scale / 2.0)
        tx = signs[0] * x_mag
        ty = signs[1] * y_mag

    transform = {**DEFAULT_TRANSFORM, **(layer.get("transform") or {})}
    transform["scale_x"] = scale
    transform["scale_y"] = scale
    transform["x"] = round(tx, 6)
    transform["y"] = round(ty, 6)
    layer["transform"] = transform

    # Rounded-rect mask over the whole (pre-transform) layer frame. The radius is
    # given in px; the mask rasteriser reads it as a fraction of min(W,H), so
    # convert. radius<=0 => square corners (still a valid full-frame mask).
    radius_px = model._as_float(op.get("radius")) if op.get("radius") is not None else 0.0
    if radius_px < 0:
        raise LayerPatchError("E_RANGE", "pip: radius must be >= 0")
    radius_frac = radius_px / float(min(width, height)) if radius_px > 0 else 0.0
    layer["mask"] = model._normalize_mask({
        "kind": "shape",
        "shape": {"type": "rectangle", "rect": [0.0, 0.0, 1.0, 1.0], "radius": radius_frac},
        "invert": False,
        "feather": 0.0,
    })

    # Depth-of-field: blur every sibling that sits *below* the pip (lower index =
    # composited first = behind it). Siblings are captured by their current
    # position before any helper is inserted, so the pip and its frame stay sharp.
    if blur_bg > 0:
        for sibling in parent["children"][:index]:
            if isinstance(sibling, dict):
                _append_blur_effect(sibling, blur_bg)

    # Optional helper shape layer (border + drop shadow) directly beneath the pip.
    border = op.get("border") if isinstance(op.get("border"), dict) else None
    shadow = bool(op.get("shadow"))
    if border or shadow:
        helper_props: dict[str, Any] = {
            "kind": "rect",
            "fill": None,
            "rect": [0.0, 0.0, 1.0, 1.0],
            "radius": radius_px,
            "opacity_baked": False,
        }
        if border:
            helper_props["stroke"] = {
                "color": str(border.get("color") or "#ffffff"),
                "width": model._as_float(border.get("width")) if border.get("width") is not None else 2.0,
            }
        if shadow:
            # A soft dark fill behind the inset reads as a drop shadow.
            helper_props["fill"] = "#000000"
            helper_props["shadow"] = True
        helper = new_layer(
            "shape",
            id=str(op["border_id"]) if op.get("border_id") else None,
            name=f"{layer.get('name', 'PiP')} frame",
            start=model._as_float(layer.get("start")),
            duration=model._as_float(layer.get("duration")),
            transform=dict(transform),
            props=helper_props,
        )
        # Insert just below the pip so it composites first (behind it).
        parent["children"].insert(index, helper)


@register_op("focus_pull", source="core")
def _op_focus_pull(doc: dict[str, Any], op: dict[str, Any]) -> None:
    """Rack-focus: keep ``layer_id`` sharp and blur every one of its siblings.

    Depth-of-field sugar that pulls focus to one layer — it appends a
    ``gaussian_blur`` effect (``blur`` px radius) to every *sibling* in the same
    composition while leaving the focused layer (and its effect chain) untouched.
    Unlike ``pip``'s ``blur_background`` (which blurs only what's *below* the
    inset), ``focus_pull`` blurs *all* siblings — both behind and in front — so
    the chosen foreground is the only sharp element. Pure effect-chain edit, no
    compile change.
    """
    layer_id = str(_require_arg(op, "layer_id"))
    parent, _index = _require_locate(doc, layer_id, op="focus_pull")
    blur = model._as_float(op.get("blur")) if op.get("blur") is not None else 0.0
    if blur <= 0:
        raise LayerPatchError("E_RANGE", "focus_pull: blur must be > 0")
    for sibling in parent["children"]:
        if isinstance(sibling, dict) and str(sibling.get("id")) != layer_id:
            _append_blur_effect(sibling, blur)


@register_op("crossfade", source="core")
def _op_crossfade(doc: dict[str, Any], op: dict[str, Any]) -> None:
    """Overlap two layers in time and cross-dissolve A → B via opacity keyframes.

    Over ``duration`` seconds starting at output time ``at``: ``from_id`` ramps
    opacity 1 → 0 while ``to_id`` ramps 0 → 1, so the outgoing layer dissolves
    into the incoming one. Only opacity keyframes are written (the same channel
    ``animate_layer``'s fade presets use); no compile change.
    """
    from_id = str(_require_arg(op, "from_id"))
    to_id = str(_require_arg(op, "to_id"))
    from_layer = _require_layer(doc, from_id, op="crossfade")
    to_layer = _require_layer(doc, to_id, op="crossfade")
    duration = model._as_float(op.get("duration")) if op.get("duration") is not None else 0.5
    if duration <= 0:
        raise LayerPatchError("E_RANGE", "crossfade: duration must be > 0")
    at = _round_t(op.get("at")) if op.get("at") is not None else _round_t(from_layer.get("start"))
    end = _round_t(at + duration)

    def _ramp(layer: dict[str, Any], v_start: float, v_end: float) -> None:
        track = layer.setdefault("keyframes", {}).setdefault("opacity", [])
        track[:] = [
            k for k in track
            if abs(model._as_float(k.get("t")) - at) > 1e-9
            and abs(model._as_float(k.get("t")) - end) > 1e-9
        ]
        track.append({"t": at, "value": v_start, "interp": "linear"})
        track.append({"t": end, "value": v_end, "interp": "linear"})
        track.sort(key=lambda k: k["t"])

    _ramp(from_layer, 1.0, 0.0)  # outgoing fades out
    _ramp(to_layer, 0.0, 1.0)    # incoming fades in


@register_op("add_gradient", source="core")
def _op_add_gradient(doc: dict[str, Any], op: dict[str, Any]) -> None:
    """Create a ``gradient`` layer per the shared layer schema.

    props = {mode: linear|radial, stops: [[pos,#rrggbb], ...], angle (linear),
    center: [cx, cy] / radius (radial)}. Stops are sorted by position and clamped
    to ``[0, 1]``; positions/colours are otherwise passed through untouched.
    """
    _ensure_gradient_layer_type()  # survive a prior reset_for_tests()
    mode = str(op.get("mode") or "linear")
    if mode not in {"linear", "radial"}:
        raise LayerPatchError("E_ARG", f"add_gradient: mode must be linear|radial, got {mode!r}")
    raw_stops = _require_arg(op, "stops")
    if not isinstance(raw_stops, list) or len(raw_stops) < 2:
        raise LayerPatchError("E_ARG", "add_gradient: need at least 2 stops [[pos, '#rrggbb'], ...]")
    stops: list[list[Any]] = []
    for stop in raw_stops:
        if not isinstance(stop, (list, tuple)) or len(stop) < 2:
            raise LayerPatchError("E_ARG", "add_gradient: each stop must be [pos(0..1), '#rrggbb']")
        pos = max(0.0, min(1.0, model._as_float(stop[0])))
        stops.append([round(pos, 6), str(stop[1])])
    stops.sort(key=lambda s: s[0])

    props: dict[str, Any] = {"mode": mode, "stops": stops}
    if mode == "linear":
        props["angle"] = model._as_float(op.get("angle")) if op.get("angle") is not None else 0.0
    else:  # radial
        center = op.get("center") if isinstance(op.get("center"), (list, tuple)) else (0.5, 0.5)
        props["center"] = [
            max(0.0, min(1.0, model._as_float(center[0]))),
            max(0.0, min(1.0, model._as_float(center[1]))),
        ]
        props["radius"] = model._as_float(op.get("radius")) if op.get("radius") is not None else 0.5

    _add_visual_layer(doc, op, "gradient", "Gradient", props)


@register_op("add_shape", source="core")
def _op_add_shape(doc: dict[str, Any], op: dict[str, Any]) -> None:
    """Create a ``shape`` layer per the shared layer schema.

    kind = rect|ellipse|polygon|line. Geometry follows the kind: rect/ellipse use
    ``rect`` (normalised [x0,y0,x1,y1]) or a centre form; polygon/line use
    ``points`` [[x,y], ...]. ``fill`` (or null), optional ``stroke``
    {color,width}, optional rect corner ``radius`` (px). ``opacity_baked`` stays
    ``False`` so the compositor applies layer opacity.
    """
    kind = str(_require_arg(op, "kind"))
    if kind not in {"rect", "ellipse", "polygon", "line"}:
        raise LayerPatchError("E_ARG", f"add_shape: kind must be rect|ellipse|polygon|line, got {kind!r}")

    props: dict[str, Any] = {"kind": kind, "opacity_baked": False}
    props["fill"] = str(op["fill"]) if op.get("fill") else None
    if isinstance(op.get("stroke"), dict):
        stroke = op["stroke"]
        props["stroke"] = {
            "color": str(stroke.get("color") or "#000000"),
            "width": model._as_float(stroke.get("width")) if stroke.get("width") is not None else 1.0,
        }

    if kind in {"polygon", "line"}:
        pts = op.get("points")
        if not isinstance(pts, list) or len(pts) < 2:
            raise LayerPatchError("E_ARG", f"add_shape: {kind} needs points [[x, y], ...]")
        props["points"] = [
            [round(model._as_float(p[0]), 6), round(model._as_float(p[1]), 6)]
            for p in pts if isinstance(p, (list, tuple)) and len(p) >= 2
        ]
        if len(props["points"]) < 2:
            raise LayerPatchError("E_ARG", f"add_shape: {kind} needs >= 2 valid points")
    else:  # rect / ellipse
        if isinstance(op.get("rect"), (list, tuple)) and len(op["rect"]) >= 4:
            props["rect"] = [round(model._as_float(v), 6) for v in op["rect"][:4]]
        elif op.get("cx") is not None and op.get("cy") is not None:
            for key in ("cx", "cy", "rx", "ry"):
                if op.get(key) is not None:
                    props[key] = round(model._as_float(op[key]), 6)
        else:
            props["rect"] = [0.0, 0.0, 1.0, 1.0]
        if kind == "rect" and op.get("radius") is not None:
            props["radius"] = model._as_float(op["radius"])

    _add_visual_layer(doc, op, "shape", "Shape", props)


def _add_visual_layer(
    doc: dict[str, Any],
    op: dict[str, Any],
    ltype: str,
    default_name: str,
    props: dict[str, Any],
) -> None:
    """Insert a freshly-built ``ltype`` layer carrying ``props`` (gradient/shape).

    Shared tail of :func:`_op_add_gradient` / :func:`_op_add_shape`: honours the
    same ``parent_id`` / ``index`` / ``at_time`` / ``duration`` / ``select``
    controls as ``add_layer`` so the sugar feels native.
    """
    parent = doc["root"]
    if op.get("parent_id"):
        parent = _require_layer(doc, op["parent_id"], op=f"add_{ltype}")
        if not _is_container(parent):
            raise LayerPatchError("E_CONTAINER", f"add_{ltype}: parent {parent.get('id')} cannot hold children")
    layer = new_layer(
        ltype,
        id=str(op["id"]) if op.get("id") else None,
        name=str(op.get("name") or default_name),
        start=_round_t(op.get("at_time") if op.get("at_time") is not None else op.get("start") or 0.0),
        duration=_round_t(op.get("duration") or 0.0),
        props=props,
    )
    if model.find_layer(doc, layer["id"]) is not None:
        layer["id"] = model.gen_id(model._id_prefix(ltype))
    index = op.get("index")
    _insert(parent, layer, int(index) if index is not None else None)
    if op.get("select", True):
        _set_selection(doc, [layer["id"]])
