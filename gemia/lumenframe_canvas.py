"""Canonical LumenFrame canvas reads and direct-manipulation edits.

Metadata, alpha bounds, PNG previews, and transform writes all consume the
same persisted LumenDoc and ``compile_to_layer_stack(...).render_frame(...)``
path as export.  The web editor therefore does not introduce a second scene
model or a preview-only transform state.
"""
from __future__ import annotations

import copy
import hashlib
import io
import math
import re
from typing import Any

from gemia.tools import layer as layer_store
from gemia.tools._context import ToolContext
from lumenframe.compile import compile_to_layer_stack
from lumenframe.model import BLEND_MODES, DEFAULT_TRANSFORM, doc_duration
from lumenframe.seek import state_at

_EDITABLE_TYPES = {
    "composition", "video", "image", "text", "shape", "gradient",
    "solid", "html",
}
_PROPERTY_EDITABLE_TYPES = _EDITABLE_TYPES | {"adjustment"}
_CANVAS_OPS = {
    "set_transform", "set_opacity", "set_blend_mode", "clip_to_below",
    "add_effect", "set_effect_params", "set_effect_enabled",
    "reorder_effect", "remove_effect",
    "add_gradient", "set_gradient", "add_shape", "add_adjustment_layer",
    "set_mask", "clear_mask", "set_time", "undo", "redo",
}
_HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}(?:[0-9a-fA-F]{2})?$")
_BLUR_RADIUS_MAX = 200.0
_TRANSFORM_CHANNELS = {
    "transform.x", "transform.y", "transform.scale", "transform.scale_x",
    "transform.scale_y", "transform.rotation", "x", "y", "scale",
    "scale_x", "scale_y", "rotation", "position_x", "position_y",
    "rotation_deg",
}


class CanvasRequestError(ValueError):
    """Stable request failure for the HTTP adapter."""

    def __init__(self, code: str, message: str) -> None:
        self.code = str(code)
        self.message = str(message)
        super().__init__(message)


def _root_layers(doc: dict[str, Any]) -> list[dict[str, Any]]:
    root = doc.get("root") if isinstance(doc.get("root"), dict) else {}
    return [layer for layer in root.get("children") or [] if isinstance(layer, dict)]


def _root_layer(doc: dict[str, Any], layer_id: str) -> dict[str, Any] | None:
    return next(
        (layer for layer in _root_layers(doc) if str(layer.get("id") or "") == layer_id),
        None,
    )


def _transform_bindings(layer: dict[str, Any]) -> bool:
    keyframes = layer.get("keyframes") if isinstance(layer.get("keyframes"), dict) else {}
    expressions = layer.get("expressions") if isinstance(layer.get("expressions"), dict) else {}
    props = layer.get("props") if isinstance(layer.get("props"), dict) else {}
    if not expressions and isinstance(props.get("expressions"), dict):
        expressions = props["expressions"]
    return any(
        key in _TRANSFORM_CHANNELS and bool(value)
        for key, value in keyframes.items()
    ) or any(
        key in _TRANSFORM_CHANNELS and bool(value)
        for key, value in expressions.items()
    )


def _editable_reason(layer: dict[str, Any], *, active: bool) -> str | None:
    if str(layer.get("type") or "") not in _EDITABLE_TYPES:
        return "unsupported_layer_type"
    if not active:
        return "not_active_at_frame"
    if bool(layer.get("locked", False)):
        return "locked"
    if _transform_bindings(layer):
        return "animated_transform"
    transform = {**DEFAULT_TRANSFORM, **(layer.get("transform") or {})}
    try:
        values = [float(transform[key]) for key in DEFAULT_TRANSFORM]
    except (TypeError, ValueError, KeyError):
        return "invalid_transform"
    if not all(math.isfinite(value) for value in values):
        return "invalid_transform"
    if float(transform["scale_x"]) <= 0.0 or float(transform["scale_y"]) <= 0.0:
        return "invalid_transform"
    if not math.isclose(
        float(transform["scale_x"]),
        float(transform["scale_y"]),
        rel_tol=1e-6,
        abs_tol=1e-6,
    ):
        return "non_uniform_scale"
    if not (
        math.isclose(float(transform["anchor_x"]), 0.5, abs_tol=1e-6)
        and math.isclose(float(transform["anchor_y"]), 0.5, abs_tol=1e-6)
    ):
        return "non_center_anchor"
    return None


def _property_editable_reason(layer: dict[str, Any]) -> str | None:
    if str(layer.get("type") or "") not in _PROPERTY_EDITABLE_TYPES:
        return "unsupported_layer_type"
    if bool(layer.get("locked", False)):
        return "locked"
    return None


def _safe_mask_summary(mask: Any) -> dict[str, Any] | None:
    """Return editor-safe mask metadata without inline pixel/path payloads."""
    if not isinstance(mask, dict):
        return None
    summary: dict[str, Any] = {
        "kind": str(mask.get("kind") or "shape"),
        "invert": bool(mask.get("invert", False)),
        "feather": float(mask.get("feather") or 0.0),
    }
    for key in (
        "source_layer_id", "asset_id", "channel", "mode", "threshold",
        "softness", "width", "height",
    ):
        value = mask.get(key)
        if isinstance(value, (str, int, float, bool)) and not (
            isinstance(value, float) and not math.isfinite(value)
        ):
            summary[key] = value
    shape = mask.get("shape")
    if isinstance(shape, dict):
        safe_shape: dict[str, Any] = {}
        for key in (
            "type", "rect", "x0", "y0", "x1", "y1", "cx", "cy",
            "rx", "ry", "radius",
        ):
            value = shape.get(key)
            if isinstance(value, (str, int, float, bool)):
                safe_shape[key] = copy.deepcopy(value)
            elif isinstance(value, (list, tuple)) and len(value) <= 4:
                safe_shape[key] = [
                    item for item in value
                    if isinstance(item, (str, int, float, bool))
                ]
        summary["shape"] = safe_shape
    if "alpha" in mask or "data" in mask:
        summary["has_inline_data"] = True
    return summary


def _gradient_summary(props: Any) -> dict[str, Any] | None:
    if not isinstance(props, dict):
        return None
    stops = []
    for stop in props.get("stops") or []:
        if isinstance(stop, (list, tuple)) and len(stop) >= 2:
            try:
                position = float(stop[0])
            except (TypeError, ValueError):
                continue
            if math.isfinite(position):
                stops.append([position, str(stop[1])])
    out: dict[str, Any] = {
        "mode": str(props.get("mode") or "linear"),
        "stops": stops,
    }
    if props.get("angle") is not None:
        out["angle"] = float(props["angle"])
    center = props.get("center")
    if isinstance(center, (list, tuple)) and len(center) >= 2:
        out["center"] = [float(center[0]), float(center[1])]
    if props.get("radius") is not None:
        out["radius"] = float(props["radius"])
    return out


def _shape_summary(props: Any) -> dict[str, Any] | None:
    if not isinstance(props, dict):
        return None
    out: dict[str, Any] = {
        "kind": str(props.get("kind") or "rect"),
        "fill": str(props["fill"]) if props.get("fill") is not None else None,
    }
    for key in ("rect", "points", "cx", "cy", "rx", "ry", "radius"):
        if key in props:
            out[key] = copy.deepcopy(props[key])
    if isinstance(props.get("stroke"), dict):
        out["stroke"] = {
            key: copy.deepcopy(value)
            for key, value in props["stroke"].items()
            if key in {"color", "width"}
        }
    return out


def _alpha_bounds(frame: Any) -> dict[str, int]:
    import numpy as np

    alpha = frame[..., 3]
    ys, xs = np.where(alpha > (1.0 / 255.0))
    if not len(xs):
        return {"x": 0, "y": 0, "width": 0, "height": 0}
    x0, x1 = int(xs.min()), int(xs.max()) + 1
    y0, y1 = int(ys.min()), int(ys.max()) + 1
    return {"x": x0, "y": y0, "width": x1 - x0, "height": y1 - y0}


def _encode_png(frame: Any) -> bytes:
    import numpy as np
    from PIL import Image

    rgba8 = (np.clip(frame, 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8)
    output = io.BytesIO()
    Image.fromarray(rgba8).save(output, format="PNG")
    return output.getvalue()


def _clamped_frame(stack: Any, requested: int) -> int:
    return max(0, min(int(requested), int(stack.total_frames) - 1))


def canvas_metadata(ctx: ToolContext, *, frame: int = 0) -> dict[str, Any]:
    """Return active top-level layers with canonical rendered alpha bounds."""
    doc = layer_store.get_lumendoc_snapshot(ctx)
    stack = compile_to_layer_stack(doc, strict=False)
    actual_frame = _clamped_frame(stack, frame)
    at = state_at(doc, actual_frame / float(stack.fps))
    active_ids = [str(layer_id) for layer_id in at.get("active_layer_ids") or []]
    active_set = set(active_ids)
    roots = {str(layer.get("id") or ""): layer for layer in _root_layers(doc)}
    runtime = {str(layer.id): layer for layer in stack.layers}
    layers: list[dict[str, Any]] = []

    # state_at and compile share bottom-to-top order. Pointer hit-testing can
    # therefore select the last matching item as the visible topmost layer.
    for z, layer_id in enumerate(active_ids):
        layer = roots.get(layer_id)
        if layer is None:
            continue
        if runtime.get(layer_id) is None:
            bounds = {"x": 0, "y": 0, "width": 0, "height": 0}
        else:
            # Resolve the layer inside its canonical sibling stack so a
            # clip_to_below layer receives the immediate lower layer's alpha.
            bounds = _alpha_bounds(stack.render_layer_frame(actual_frame, layer_id))

        transform = {**DEFAULT_TRANSFORM, **(layer.get("transform") or {})}
        reason = _editable_reason(layer, active=layer_id in active_set)
        property_reason = _property_editable_reason(layer)
        props = layer.get("props") if isinstance(layer.get("props"), dict) else {}
        layer_payload: dict[str, Any] = {
            "id": layer_id,
            "name": str(layer.get("name") or layer_id),
            "type": str(layer.get("type") or "unknown"),
            "z": z,
            # Compatibility: ``editable`` has always meant transform-editable.
            "editable": reason is None,
            "disabled_reason": reason,
            "transform_editable": reason is None,
            "property_editable": property_reason is None,
            "property_disabled_reason": property_reason,
            "transform": {
                "x": float(transform["x"]),
                "y": float(transform["y"]),
                "scale": float(transform["scale_x"]),
                "rotation": float(transform["rotation"]),
            },
            "opacity": float(layer.get("opacity", 1.0)),
            "blend_mode": str(layer.get("blend_mode") or "normal"),
            "clip_to_below": bool(layer.get("clip_to_below", False)),
            "effects": [
                {
                    "id": str(effect.get("id") or ""),
                    "type": str(effect.get("type") or "unknown"),
                    "enabled": bool(effect.get("enabled", True)),
                    "params": copy.deepcopy(effect.get("params") or {}),
                }
                for effect in layer.get("effects") or []
                if isinstance(effect, dict)
            ],
            "mask": _safe_mask_summary(layer.get("mask")),
            "time": {
                "start": float(layer.get("start") or 0.0),
                "duration": float(layer.get("duration") or 0.0),
                "end": float(layer.get("start") or 0.0)
                + float(layer.get("duration") or 0.0),
            },
            "bounds": bounds,
        }
        if str(layer.get("type") or "") == "gradient":
            layer_payload["gradient"] = _gradient_summary(props)
        elif str(layer.get("type") or "") == "shape":
            layer_payload["shape"] = _shape_summary(props)
        layers.append(layer_payload)

    history = layer_store.lumenframe_history_status(ctx, doc)
    return {
        "revision": history["revision"],
        "canvas": {
            "width": int(stack.width),
            "height": int(stack.height),
            "fps": float(stack.fps),
            "frame": actual_frame,
            "time": actual_frame / float(stack.fps),
            "total_frames": int(stack.total_frames),
        },
        "layers": layers,
        "selection_ids": list(doc.get("selection") or []),
        "can_undo": history["can_undo"],
        "can_redo": history["can_redo"],
        "capabilities": {
            "blend_modes": sorted(BLEND_MODES),
            "effect_types": [{
                "type": "gaussian_blur",
                "params": {
                    "radius": {
                        "min": 0.0,
                        "max": _BLUR_RADIUS_MAX,
                        "step": 0.5,
                        "default": 8.0,
                    },
                },
            }],
            "add_layer_types": ["gradient", "shape", "adjustment"],
            "mask_shapes": ["rectangle", "ellipse"],
        },
        # OpenCV/LumenFrame positive rotation is counter-clockwise while CSS
        # positive rotation is clockwise.
        "rotation_css_sign": -1,
    }


def render_canvas_png(
    ctx: ToolContext,
    *,
    frame: int = 0,
    mode: str = "composite",
    layer_id: str | None = None,
    revision: str | None = None,
) -> tuple[int, str, bytes]:
    """Render the composite, one layer, or the composition excluding one layer."""
    doc = layer_store.get_lumendoc_snapshot(ctx)
    current_revision = layer_store.lumenframe_revision(doc)
    if revision is not None and str(revision) != current_revision:
        raise layer_store.LumenframeRevisionConflict(current_revision)
    mode = str(mode or "composite")
    if mode not in {"composite", "only", "exclude"}:
        raise CanvasRequestError("E_BAD_ARG", "mode must be composite, only, or exclude")
    requested_id = str(layer_id or "")
    if mode != "composite" and not requested_id:
        raise CanvasRequestError("E_BAD_ARG", f"mode={mode} requires layer_id")
    if mode != "composite" and _root_layer(doc, requested_id) is None:
        raise CanvasRequestError("E_NOT_FOUND", f"unknown top-level layer: {requested_id}")

    if mode == "exclude":
        render_doc = copy.deepcopy(doc)
        target = _root_layer(render_doc, requested_id)
        assert target is not None
        target["visible"] = False
        stack = compile_to_layer_stack(render_doc, strict=False)
    else:
        stack = compile_to_layer_stack(doc, strict=False)

    actual_frame = _clamped_frame(stack, frame)
    if mode == "only":
        rendered_frame = stack.render_layer_frame(actual_frame, requested_id)
    else:
        rendered_frame = stack.render_frame(actual_frame)
    return actual_frame, current_revision, _encode_png(rendered_frame)


def _finite_float(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise CanvasRequestError("E_BAD_ARG", f"{label} must be a number")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise CanvasRequestError("E_BAD_ARG", f"{label} must be a number") from exc
    if not math.isfinite(result):
        raise CanvasRequestError("E_BAD_ARG", f"{label} must be finite")
    return result


def _bounded_float(
    value: Any,
    label: str,
    *,
    minimum: float,
    maximum: float,
) -> float:
    result = _finite_float(value, label)
    if result < minimum or result > maximum:
        raise CanvasRequestError(
            "E_BAD_ARG", f"{label} must be between {minimum:g} and {maximum:g}",
        )
    return result


def _required_string(body: dict[str, Any], key: str, op: str) -> str:
    value = str(body.get(key) or "").strip()
    if not value:
        raise CanvasRequestError("E_BAD_ARG", f"{op} requires {key}")
    return value


def _property_target(
    doc: dict[str, Any],
    layer_id: str,
    *,
    op: str,
) -> dict[str, Any]:
    target = _root_layer(doc, layer_id)
    if target is None:
        raise CanvasRequestError("E_NOT_FOUND", f"unknown top-level layer: {layer_id}")
    reason = _property_editable_reason(target)
    if reason is not None:
        raise CanvasRequestError("E_LAYER_LOCKED", f"layer cannot be edited: {reason}")
    return target


def _effect_target(layer: dict[str, Any], effect_id: str, *, op: str) -> dict[str, Any]:
    for effect in layer.get("effects") or []:
        if isinstance(effect, dict) and str(effect.get("id") or "") == effect_id:
            return effect
    raise CanvasRequestError("E_NOT_FOUND", f"{op}: effect not found: {effect_id}")


def _stable_generated_id(
    doc: dict[str, Any],
    *,
    prefix: str,
    op: str,
    client_op_id: str,
) -> str:
    seed = f"{doc.get('id', '')}\0{op}\0{client_op_id}".encode()
    return f"{prefix}_{hashlib.sha256(seed).hexdigest()[:12]}"


def _valid_color(value: Any, label: str, *, default: str | None = None) -> str:
    if value is None and default is not None:
        value = default
    color = str(value or "")
    if not _HEX_COLOR_RE.fullmatch(color):
        raise CanvasRequestError(
            "E_BAD_ARG", f"{label} must be #RRGGBB or #RRGGBBAA",
        )
    return color


def _gradient_stops(raw: Any, *, label: str = "stops") -> list[list[Any]]:
    if not isinstance(raw, list) or len(raw) < 2:
        raise CanvasRequestError("E_BAD_ARG", f"{label} must contain at least 2 stops")
    stops: list[list[Any]] = []
    for index, stop in enumerate(raw):
        if not isinstance(stop, (list, tuple)) or len(stop) != 2:
            raise CanvasRequestError(
                "E_BAD_ARG", f"{label}[{index}] must be [position, color]",
            )
        position = _bounded_float(
            stop[0], f"{label}[{index}].position", minimum=0.0, maximum=1.0,
        )
        stops.append([round(position, 6), _valid_color(stop[1], f"{label}[{index}].color")])
    stops.sort(key=lambda stop: stop[0])
    return stops


def _gradient_op_fields(
    body: dict[str, Any],
    *,
    current: dict[str, Any] | None = None,
) -> dict[str, Any]:
    existing = current if isinstance(current, dict) else {}
    mode = str(body.get("mode") or existing.get("mode") or "linear")
    if mode not in {"linear", "radial"}:
        raise CanvasRequestError("E_BAD_ARG", "gradient mode must be linear or radial")
    raw_stops = body.get("stops") if body.get("stops") is not None else existing.get("stops")
    fields: dict[str, Any] = {
        "mode": mode,
        "stops": _gradient_stops(raw_stops),
    }
    if mode == "linear":
        fields["angle"] = _finite_float(
            body.get("angle", existing.get("angle", 0.0)), "angle",
        )
    else:
        center = body.get("center", existing.get("center", [0.5, 0.5]))
        if not isinstance(center, (list, tuple)) or len(center) != 2:
            raise CanvasRequestError("E_BAD_ARG", "center must be [x, y]")
        fields["center"] = [
            _bounded_float(center[0], "center.x", minimum=0.0, maximum=1.0),
            _bounded_float(center[1], "center.y", minimum=0.0, maximum=1.0),
        ]
        radius = body.get("radius", existing.get("radius", 0.5))
        fields["radius"] = _bounded_float(
            radius, "radius", minimum=0.001, maximum=1.0,
        )
    return fields


def _shape_rect(raw: Any) -> list[float]:
    if not isinstance(raw, (list, tuple)) or len(raw) != 4:
        raise CanvasRequestError("E_BAD_ARG", "rect must be [x0, y0, x1, y1]")
    rect = [
        _bounded_float(value, f"rect[{index}]", minimum=0.0, maximum=1.0)
        for index, value in enumerate(raw)
    ]
    if rect[2] <= rect[0] or rect[3] <= rect[1]:
        raise CanvasRequestError("E_BAD_ARG", "rect must have positive width and height")
    return rect


def _shape_mask(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise CanvasRequestError("E_BAD_ARG", "set_mask requires a mask object")
    if set(raw) - {"kind", "shape", "invert", "feather"}:
        raise CanvasRequestError("E_BAD_ARG", "canvas masks only accept shape, invert, and feather")
    if str(raw.get("kind") or "shape") != "shape":
        raise CanvasRequestError("E_BAD_ARG", "canvas masks currently support shape masks only")
    shape = raw.get("shape")
    if not isinstance(shape, dict):
        raise CanvasRequestError("E_BAD_ARG", "mask.shape must be an object")
    if set(shape) - {
        "type", "rect", "x0", "y0", "x1", "y1", "cx", "cy", "rx", "ry",
        "radius",
    }:
        raise CanvasRequestError("E_BAD_ARG", "mask.shape contains unsupported fields")
    shape_type = str(shape.get("type") or "rectangle").lower()
    if shape_type == "rect":
        shape_type = "rectangle"
    if shape_type not in {"rectangle", "ellipse"}:
        raise CanvasRequestError("E_BAD_ARG", "mask shape must be rectangle or ellipse")
    normalized_shape: dict[str, Any] = {"type": shape_type}
    if shape.get("rect") is not None:
        normalized_shape["rect"] = _shape_rect(shape["rect"])
    elif all(shape.get(key) is not None for key in ("x0", "y0", "x1", "y1")):
        normalized_shape["rect"] = _shape_rect([
            shape["x0"], shape["y0"], shape["x1"], shape["y1"],
        ])
    elif all(shape.get(key) is not None for key in ("cx", "cy", "rx", "ry")):
        normalized_shape.update({
            "cx": _bounded_float(shape["cx"], "mask.shape.cx", minimum=0.0, maximum=1.0),
            "cy": _bounded_float(shape["cy"], "mask.shape.cy", minimum=0.0, maximum=1.0),
            "rx": _bounded_float(shape["rx"], "mask.shape.rx", minimum=0.001, maximum=1.0),
            "ry": _bounded_float(shape["ry"], "mask.shape.ry", minimum=0.001, maximum=1.0),
        })
    else:
        normalized_shape["rect"] = [0.0, 0.0, 1.0, 1.0]
    if shape.get("radius") is not None:
        normalized_shape["radius"] = _bounded_float(
            shape["radius"], "mask.shape.radius", minimum=0.0, maximum=0.5,
        )
    invert = raw.get("invert", False)
    if not isinstance(invert, bool):
        raise CanvasRequestError("E_BAD_ARG", "mask.invert must be a boolean")
    feather = _bounded_float(
        raw.get("feather", 0.0), "mask.feather", minimum=0.0, maximum=0.25,
    )
    return {
        "kind": "shape",
        "shape": normalized_shape,
        "invert": invert,
        "feather": feather,
    }


def _visual_span(doc: dict[str, Any], body: dict[str, Any]) -> tuple[float, float]:
    start = _finite_float(body.get("start", 0.0), "start")
    if start < 0.0:
        raise CanvasRequestError("E_BAD_ARG", "start must be at least 0")
    if body.get("duration") is not None:
        duration = _finite_float(body["duration"], "duration")
    else:
        canvas = doc.get("canvas") if isinstance(doc.get("canvas"), dict) else {}
        fps = _finite_float(canvas.get("fps", 30.0), "canvas.fps")
        duration = max(doc_duration(doc) - start, 1.0 / max(fps, 1e-6))
    if duration <= 0.0:
        raise CanvasRequestError("E_BAD_ARG", "duration must be greater than 0")
    return start, duration


def _duplicate_response(
    ctx: ToolContext,
    doc: dict[str, Any],
    body: dict[str, Any],
    *,
    op: str,
    client_op_id: str,
) -> dict[str, Any]:
    history = layer_store.lumenframe_history_status(ctx, doc)
    payload: dict[str, Any] = {
        "op": op,
        "revision": history["revision"],
        "can_undo": history["can_undo"],
        "can_redo": history["can_redo"],
        "duplicate": True,
    }
    if op == "add_effect":
        payload["layer_id"] = str(body.get("layer_id") or "")
        payload["effect_id"] = _stable_generated_id(
            doc, prefix="fx", op=op, client_op_id=client_op_id,
        )
    elif op in {"add_gradient", "add_shape", "add_adjustment_layer"}:
        prefix = {"add_gradient": "grad", "add_shape": "shape", "add_adjustment_layer": "adj"}[op]
        payload["layer_id"] = _stable_generated_id(
            doc, prefix=prefix, op=op, client_op_id=client_op_id,
        )
    elif body.get("layer_id"):
        payload["layer_id"] = str(body["layer_id"])
    if body.get("effect_id"):
        payload["effect_id"] = str(body["effect_id"])
    if op == "set_transform" and payload.get("layer_id"):
        target = _root_layer(doc, str(payload["layer_id"]))
        transform = {**DEFAULT_TRANSFORM, **((target or {}).get("transform") or {})}
        payload["transform"] = {
            "x": float(transform["x"]),
            "y": float(transform["y"]),
            "scale": float(transform["scale_x"]),
            "rotation": float(transform["rotation"]),
        }
    return payload


def apply_canvas_operation(ctx: ToolContext, body: dict[str, Any]) -> dict[str, Any]:
    """Apply one serialized, revisioned direct-manipulation operation."""
    if not isinstance(body, dict):
        raise CanvasRequestError("E_BAD_ARG", "canvas operation body must be an object")
    if "ops" in body or "patch" in body:
        raise CanvasRequestError("E_BAD_ARG", "raw LayerPatch payloads are not accepted")
    op = str(body.get("op") or "")
    if op not in _CANVAS_OPS:
        raise CanvasRequestError("E_BAD_ARG", f"unsupported canvas op: {op or '<missing>'}")

    client_op_id = str(body.get("client_op_id") or "") or None
    base_revision = str(body.get("base_revision") or "") or None
    if client_op_id is None:
        raise CanvasRequestError("E_BAD_ARG", f"{op} requires client_op_id")
    if base_revision is None:
        raise CanvasRequestError("E_BAD_ARG", f"{op} requires base_revision")

    doc = layer_store.get_lumendoc_snapshot(ctx)
    if layer_store.lumenframe_client_op_seen(ctx, client_op_id, doc):
        return _duplicate_response(
            ctx, doc, body, op=op, client_op_id=client_op_id,
        )
    current_revision = layer_store.lumenframe_revision(doc)
    if base_revision != current_revision:
        raise layer_store.LumenframeRevisionConflict(current_revision)

    if op in {"undo", "redo"}:
        try:
            steps = int(body.get("steps") or 1)
        except (TypeError, ValueError) as exc:
            raise CanvasRequestError("E_BAD_ARG", "steps must be an integer") from exc
        if isinstance(body.get("steps"), bool) or not 1 <= steps <= 50:
            raise CanvasRequestError("E_BAD_ARG", "steps must be between 1 and 50")
        result = layer_store.restore_lumendoc_history(
            ctx,
            direction=op,
            steps=steps,
            client_op_id=client_op_id,
            base_revision=base_revision,
        )
        return {
            "op": op,
            "revision": result["revision"],
            "can_undo": result["can_undo"],
            "can_redo": result["can_redo"],
            "duplicate": bool(result.get("duplicate", False)),
        }

    patch_op: dict[str, Any]
    response: dict[str, Any] = {"op": op}
    label = f"user canvas {op}"

    if op == "set_transform":
        layer_id = _required_string(body, "layer_id", op)
        target = _root_layer(doc, layer_id)
        if target is None:
            raise CanvasRequestError("E_NOT_FOUND", f"unknown top-level layer: {layer_id}")
        transform_body = body.get("transform")
        if not isinstance(transform_body, dict) or not transform_body:
            raise CanvasRequestError("E_BAD_ARG", "set_transform requires a transform object")
        unknown = set(transform_body) - {"x", "y", "scale", "rotation"}
        if unknown:
            raise CanvasRequestError(
                "E_BAD_ARG", f"unsupported transform fields: {sorted(unknown)}",
            )
        canvas = doc.get("canvas") if isinstance(doc.get("canvas"), dict) else {}
        fps = _finite_float(canvas.get("fps", 30.0), "canvas.fps")
        try:
            frame = int(body.get("frame") or 0)
        except (TypeError, ValueError) as exc:
            raise CanvasRequestError("E_BAD_ARG", "frame must be an integer") from exc
        active_ids = set(state_at(doc, frame / fps).get("active_layer_ids") or [])
        reason = _editable_reason(target, active=layer_id in active_ids)
        if reason is not None:
            raise CanvasRequestError("E_LAYER_LOCKED", f"layer cannot be transformed: {reason}")
        current_transform = {**DEFAULT_TRANSFORM, **(target.get("transform") or {})}
        values = {
            "x": _finite_float(transform_body.get("x", current_transform["x"]), "transform.x"),
            "y": _finite_float(transform_body.get("y", current_transform["y"]), "transform.y"),
            "scale": _finite_float(
                transform_body.get("scale", current_transform["scale_x"]), "transform.scale",
            ),
            "rotation": _finite_float(
                transform_body.get("rotation", current_transform["rotation"]),
                "transform.rotation",
            ),
        }
        if values["scale"] <= 0.0:
            raise CanvasRequestError("E_BAD_ARG", "transform.scale must be greater than zero")
        patch_op = {"op": "set_transform", "layer_id": layer_id, **values}
        response.update({"layer_id": layer_id, "transform": values})
        label = "user direct transform"

    elif op in {
        "set_opacity", "set_blend_mode", "clip_to_below", "add_effect", "set_effect_params",
        "set_effect_enabled", "reorder_effect", "remove_effect", "set_gradient",
        "set_mask", "clear_mask", "set_time",
    }:
        layer_id = _required_string(body, "layer_id", op)
        target = _property_target(doc, layer_id, op=op)
        response["layer_id"] = layer_id

        if op == "set_opacity":
            if body.get("opacity") is None:
                raise CanvasRequestError("E_BAD_ARG", "set_opacity requires opacity")
            opacity = _bounded_float(body["opacity"], "opacity", minimum=0.0, maximum=1.0)
            patch_op = {"op": op, "layer_id": layer_id, "opacity": opacity}
        elif op == "set_blend_mode":
            blend_mode = _required_string(body, "blend_mode", op)
            if blend_mode not in BLEND_MODES:
                raise CanvasRequestError("E_BAD_ARG", f"unsupported blend mode: {blend_mode}")
            patch_op = {"op": op, "layer_id": layer_id, "blend_mode": blend_mode}
        elif op == "clip_to_below":
            enabled = body.get("enabled")
            if not isinstance(enabled, bool):
                raise CanvasRequestError("E_BAD_ARG", "enabled must be a boolean")
            patch_op = {"op": op, "layer_id": layer_id, "enabled": enabled}
        elif op == "add_effect":
            effect_type = str(body.get("effect_type") or body.get("type") or "")
            if effect_type != "gaussian_blur":
                raise CanvasRequestError("E_BAD_ARG", "canvas effects currently support gaussian_blur only")
            raw_params = body.get("params", {})
            if not isinstance(raw_params, dict) or set(raw_params) - {"radius"}:
                raise CanvasRequestError("E_BAD_ARG", "gaussian_blur params only accept radius")
            radius = _bounded_float(
                raw_params.get("radius", 8.0), "params.radius",
                minimum=0.0, maximum=_BLUR_RADIUS_MAX,
            )
            effect_id = _stable_generated_id(
                doc, prefix="fx", op=op, client_op_id=client_op_id,
            )
            patch_op = {
                "op": op,
                "layer_id": layer_id,
                "effect_id": effect_id,
                "type": effect_type,
                "params": {"radius": radius},
            }
            response["effect_id"] = effect_id
        else:
            effect_id = str(body.get("effect_id") or "")
            effect: dict[str, Any] | None = None
            if op in {
                "set_effect_params", "set_effect_enabled", "reorder_effect", "remove_effect",
            }:
                effect_id = _required_string(body, "effect_id", op)
                effect = _effect_target(target, effect_id, op=op)
                response["effect_id"] = effect_id

            if op == "set_effect_params":
                if str((effect or {}).get("type") or "") != "gaussian_blur":
                    raise CanvasRequestError("E_BAD_ARG", "only gaussian_blur params are canvas-editable")
                params = body.get("params")
                if not isinstance(params, dict) or not params or set(params) - {"radius"}:
                    raise CanvasRequestError("E_BAD_ARG", "gaussian_blur params require radius only")
                radius = _bounded_float(
                    params.get("radius"), "params.radius",
                    minimum=0.0, maximum=_BLUR_RADIUS_MAX,
                )
                patch_op = {
                    "op": op, "layer_id": layer_id, "effect_id": effect_id,
                    "params": {"radius": radius}, "merge": False,
                }
            elif op == "set_effect_enabled":
                enabled = body.get("enabled")
                if not isinstance(enabled, bool):
                    raise CanvasRequestError("E_BAD_ARG", "enabled must be a boolean")
                patch_op = {
                    "op": op, "layer_id": layer_id, "effect_id": effect_id,
                    "enabled": enabled,
                }
            elif op == "reorder_effect":
                index = body.get("index")
                if isinstance(index, bool) or not isinstance(index, int):
                    raise CanvasRequestError("E_BAD_ARG", "index must be an integer")
                effect_count = len(target.get("effects") or [])
                if not 0 <= index < effect_count:
                    raise CanvasRequestError("E_BAD_ARG", "index is outside the effect stack")
                patch_op = {
                    "op": op, "layer_id": layer_id, "effect_id": effect_id, "index": index,
                }
            elif op == "remove_effect":
                patch_op = {"op": op, "layer_id": layer_id, "effect_id": effect_id}
            elif op == "set_gradient":
                if str(target.get("type") or "") != "gradient":
                    raise CanvasRequestError("E_BAD_ARG", "set_gradient requires a gradient layer")
                props = target.get("props") if isinstance(target.get("props"), dict) else {}
                patch_op = {
                    "op": op,
                    "layer_id": layer_id,
                    **_gradient_op_fields(body, current=props),
                }
            elif op == "set_mask":
                patch_op = {
                    "op": "set_mask", "layer_id": layer_id,
                    "mask": _shape_mask(body.get("mask")),
                }
            elif op == "clear_mask":
                patch_op = {"op": "set_mask", "layer_id": layer_id, "mask": None}
            else:  # set_time
                if str(target.get("type") or "") != "adjustment":
                    raise CanvasRequestError("E_BAD_ARG", "set_time is limited to adjustment layers")
                if body.get("start") is None and body.get("duration") is None:
                    raise CanvasRequestError("E_BAD_ARG", "set_time requires start or duration")
                patch_op = {"op": "set_time", "layer_id": layer_id}
                if body.get("start") is not None:
                    start = _finite_float(body["start"], "start")
                    if start < 0.0:
                        raise CanvasRequestError("E_BAD_ARG", "start must be at least 0")
                    patch_op["start"] = start
                if body.get("duration") is not None:
                    duration = _finite_float(body["duration"], "duration")
                    if duration <= 0.0:
                        raise CanvasRequestError("E_BAD_ARG", "duration must be greater than 0")
                    patch_op["duration"] = duration

    elif op in {"add_gradient", "add_shape", "add_adjustment_layer"}:
        start, duration = _visual_span(doc, body)
        prefix = {"add_gradient": "grad", "add_shape": "shape", "add_adjustment_layer": "adj"}[op]
        layer_id = _stable_generated_id(
            doc, prefix=prefix, op=op, client_op_id=client_op_id,
        )
        response["layer_id"] = layer_id
        name = str(body.get("name") or {
            "add_gradient": "Gradient",
            "add_shape": "Shape",
            "add_adjustment_layer": "Adjustment",
        }[op]).strip()[:128]
        if not name:
            raise CanvasRequestError("E_BAD_ARG", "name cannot be empty")
        if op == "add_gradient":
            patch_op = {
                "op": op, "id": layer_id, "name": name,
                "start": start, "duration": duration,
                **_gradient_op_fields(body),
            }
        elif op == "add_shape":
            kind = str(body.get("kind") or "rect")
            if kind not in {"rect", "ellipse"}:
                raise CanvasRequestError("E_BAD_ARG", "canvas shapes support rect or ellipse")
            patch_op = {
                "op": op, "id": layer_id, "name": name,
                "start": start, "duration": duration,
                "kind": kind,
                "fill": _valid_color(body.get("fill"), "fill", default="#ffffff"),
                "rect": _shape_rect(body.get("rect", [0.0, 0.0, 1.0, 1.0])),
            }
        else:
            patch_op = {
                "op": op, "id": layer_id, "name": name,
                "start": start, "duration": duration,
            }
    else:  # pragma: no cover - allow-list and branches are intentionally exhaustive.
        raise CanvasRequestError("E_BAD_ARG", f"unsupported canvas op: {op}")

    result = layer_store.apply_lumendoc_ops(
        ctx,
        [patch_op],
        label=label,
        client_op_id=client_op_id,
        base_revision=base_revision,
    )
    response.update({
        "op": op,
        "revision": result["revision"],
        "can_undo": result["can_undo"],
        "can_redo": result["can_redo"],
        "duplicate": bool(result.get("duplicate", False)),
    })
    return response


__all__ = [
    "CanvasRequestError",
    "canvas_metadata",
    "render_canvas_png",
    "apply_canvas_operation",
]
