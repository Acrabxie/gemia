"""Validation helpers for layer-first plans and preview smoke checks."""
from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from numbers import Real
from pathlib import Path
from typing import Any

import numpy as np

from gemia.registry import get_info, get_registry

SUPPORTED_LAYER_TYPES = frozenset({"video", "image", "text", "solid", "html", "lottie"})
SUPPORTED_BLEND_MODES = frozenset({"normal", "multiply", "screen", "overlay"})
SUPPORTED_KEYFRAME_PROPERTIES = frozenset({"opacity", "scale", "rotation_deg", "position", "position_x", "position_y", "x", "y"})
SUPPORTED_EASINGS = frozenset({"linear", "ease_in", "ease_out", "ease_in_out", "bezier"})
SUPPORTED_KEYFRAME_MODES = frozenset({"clamp", "loop", "pingpong", "relative"})


class LayerPlanValidationError(ValueError):
    """Raised when a layer plan violates schema or renderability invariants."""

    def __init__(self, issues: Sequence[str]) -> None:
        self.issues = list(issues)
        message = "Invalid layer plan:\n" + "\n".join(f" - {issue}" for issue in self.issues)
        super().__init__(message)


def _is_real_number(value: Any) -> bool:
    return isinstance(value, Real) and not isinstance(value, bool) and math.isfinite(float(value))


def _parse_int_like(value: Any, *, field_path: str, issues: list[str]) -> int | None:
    if isinstance(value, bool):
        issues.append(f"{field_path} must be an integer, got bool.")
        return None
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            issues.append(f"{field_path} must be an integer, got empty string.")
            return None
        try:
            parsed = int(stripped)
        except ValueError:
            issues.append(f"{field_path} must be an integer, got {value!r}.")
            return None
        if str(parsed) != stripped and stripped not in {f"+{parsed}", f"-{abs(parsed)}"}:
            issues.append(f"{field_path} must be an integer, got {value!r}.")
            return None
        return parsed
    if isinstance(value, Real):
        numeric = float(value)
        if numeric.is_integer():
            return int(numeric)
    issues.append(f"{field_path} must be an integer, got {value!r}.")
    return None


def _parse_float_like(value: Any, *, field_path: str, issues: list[str]) -> float | None:
    if _is_real_number(value):
        return float(value)
    issues.append(f"{field_path} must be a finite number, got {value!r}.")
    return None


def _validate_unit_rgba(value: Any, *, field_path: str, issues: list[str]) -> None:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or len(value) != 4:
        issues.append(f"{field_path} must be a 4-item RGBA sequence.")
        return
    for channel_index, channel in enumerate(value):
        channel_path = f"{field_path}[{channel_index}]"
        parsed = _parse_float_like(channel, field_path=channel_path, issues=issues)
        if parsed is None:
            continue
        if parsed < 0.0 or parsed > 1.0:
            issues.append(f"{channel_path} must be within [0.0, 1.0], got {parsed}.")


def _validate_position(value: Any, *, field_path: str, issues: list[str]) -> None:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or len(value) != 2:
        issues.append(f"{field_path} must be a 2-item (x, y) sequence.")
        return
    for index, component in enumerate(value):
        _parse_int_like(component, field_path=f"{field_path}[{index}]", issues=issues)


def _validate_font_config(value: Any, *, field_path: str, issues: list[str]) -> None:
    if not isinstance(value, Mapping):
        issues.append(f"{field_path} must be an object.")
        return
    if "size" in value:
        size = _parse_int_like(value["size"], field_path=f"{field_path}.size", issues=issues)
        if size is not None and size <= 0:
            issues.append(f"{field_path}.size must be > 0, got {size}.")
    if "padding" in value:
        padding = _parse_int_like(value["padding"], field_path=f"{field_path}.padding", issues=issues)
        if padding is not None and padding < 0:
            issues.append(f"{field_path}.padding must be >= 0, got {padding}.")
    if "color" in value:
        _validate_unit_rgba(value["color"], field_path=f"{field_path}.color", issues=issues)
    if "path" in value and not isinstance(value["path"], str):
        issues.append(f"{field_path}.path must be a string if provided.")


def _validate_size(value: Any, *, field_path: str, issues: list[str]) -> None:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or len(value) != 2:
        issues.append(f"{field_path} must be a 2-item (width, height) sequence.")
        return
    for index, component in enumerate(value):
        parsed = _parse_int_like(component, field_path=f"{field_path}[{index}]", issues=issues)
        if parsed is not None and parsed <= 0:
            issues.append(f"{field_path}[{index}] must be > 0, got {parsed}.")


def _validate_source_path(value: Any, *, field_path: str, issues: list[str]) -> str | None:
    if not isinstance(value, str) or not value.strip():
        issues.append(f"{field_path} must be a non-empty path string.")
        return None
    path = Path(value)
    if not path.exists():
        issues.append(f"{field_path} does not exist: {value}")
    elif not path.is_file():
        issues.append(f"{field_path} must point to a file: {value}")
    return value


def _validate_picture_primitive(name: str, *, field_path: str, issues: list[str]) -> None:
    if "." in name:
        try:
            info = get_info(name)
        except KeyError:
            issues.append(f"{field_path} references unknown primitive {name!r}.")
            return
        if info.domain != "picture":
            issues.append(f"{field_path} must reference a picture primitive, got {name!r} ({info.domain}).")
        return

    picture_matches = [
        info for info in get_registry().values()
        if info.name == name and info.domain == "picture"
    ]
    if len(picture_matches) == 1:
        return
    if not picture_matches:
        issues.append(f"{field_path} references unknown picture primitive {name!r}.")
        return
    fqns = ", ".join(sorted(info.fqn for info in picture_matches))
    issues.append(f"{field_path} is ambiguous for primitive {name!r}: {fqns}.")


def _validate_primitive_chain(value: Any, *, field_path: str, issues: list[str]) -> None:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        issues.append(f"{field_path} must be a list of [primitive, params] pairs.")
        return
    for index, item in enumerate(value):
        item_path = f"{field_path}[{index}]"
        if not isinstance(item, Sequence) or isinstance(item, (str, bytes)) or len(item) != 2:
            issues.append(f"{item_path} must be a 2-item [primitive, params] pair.")
            continue
        primitive_name, primitive_kwargs = item
        if not isinstance(primitive_name, str) or not primitive_name.strip():
            issues.append(f"{item_path}[0] must be a non-empty primitive name.")
        else:
            _validate_picture_primitive(primitive_name, field_path=f"{item_path}[0]", issues=issues)
        if not isinstance(primitive_kwargs, Mapping):
            issues.append(f"{item_path}[1] must be an object of primitive kwargs.")


def _validate_keyframes(
    value: Any,
    *,
    field_path: str,
    issues: list[str],
    layer_start: int,
    layer_end: int | None,
) -> None:
    if not isinstance(value, Mapping):
        issues.append(f"{field_path} must be an object keyed by animatable property.")
        return
    for property_name, track_spec in value.items():
        property_path = f"{field_path}.{property_name}"
        if property_name not in SUPPORTED_KEYFRAME_PROPERTIES:
            supported = ", ".join(sorted(SUPPORTED_KEYFRAME_PROPERTIES))
            issues.append(f"{property_path} is unsupported. Supported keyframes: {supported}.")
            continue
        if not isinstance(track_spec, Mapping) or not track_spec:
            issues.append(f"{property_path} must be a non-empty frame:value mapping.")
            continue
        mode = str(track_spec.get("mode", "clamp"))
        if mode not in SUPPORTED_KEYFRAME_MODES:
            valid_modes = ", ".join(sorted(SUPPORTED_KEYFRAME_MODES))
            issues.append(f"{property_path}.mode must be one of {valid_modes}, got {mode!r}.")
        if "relative_to" in track_spec:
            _parse_float_like(track_spec["relative_to"], field_path=f"{property_path}.relative_to", issues=issues)
        relative_to = float(track_spec.get("relative_to", 0.0) or 0.0)
        track_items = _iter_keyframe_track_items(track_spec)
        if not track_items:
            issues.append(f"{property_path} must contain at least one keyframe.")
            continue
        parsed_frames: list[float] = []
        for frame_key, frame_value_spec in track_items:
            frame_key_path = f"{property_path}[{frame_key!r}]"
            try:
                frame_number = float(frame_key)
            except (TypeError, ValueError):
                issues.append(f"{frame_key_path} must use numeric frame keys.")
                continue
            if not math.isfinite(frame_number):
                issues.append(f"{frame_key_path} must use finite frame keys.")
                continue
            if frame_number < 0:
                issues.append(f"{frame_key_path} must use frame keys >= 0.")
                continue
            validation_frame = frame_number + relative_to if mode == "relative" else frame_number
            if validation_frame < layer_start:
                issues.append(f"{frame_key_path} occurs before the layer start_frame {layer_start}.")
            if layer_end is not None and validation_frame >= layer_end:
                issues.append(f"{frame_key_path} occurs at or after the layer end_frame {layer_end}.")
            parsed_frames.append(frame_number)

            easing = "linear"
            if isinstance(frame_value_spec, Mapping):
                raw_value = frame_value_spec.get("value")
                easing = str(frame_value_spec.get("easing", "linear"))
            else:
                raw_value = frame_value_spec

            if property_name == "position":
                _validate_position_keyframe_value(raw_value, field_path=f"{frame_key_path}.value", issues=issues)
            else:
                parsed_value = _parse_float_like(raw_value, field_path=f"{frame_key_path}.value", issues=issues)
                if parsed_value is None:
                    continue
                if property_name == "opacity" and not 0.0 <= parsed_value <= 1.0:
                    issues.append(f"{frame_key_path}.value must stay within [0.0, 1.0], got {parsed_value}.")
                if property_name == "scale" and parsed_value <= 0.0:
                    issues.append(f"{frame_key_path}.value must be > 0 for scale, got {parsed_value}.")
            if easing not in SUPPORTED_EASINGS and not _is_bezier_easing(easing):
                valid = ", ".join(sorted(SUPPORTED_EASINGS))
                issues.append(f"{frame_key_path}.easing must be one of {valid}, got {easing!r}.")
        if parsed_frames != sorted(parsed_frames):
            issues.append(f"{property_path} frame keys must be sorted in ascending order.")


def _iter_keyframe_track_items(track_spec: Mapping[str, Any]) -> list[tuple[Any, Any]]:
    raw_points = track_spec.get("keyframes", track_spec.get("points"))
    if isinstance(raw_points, Sequence) and not isinstance(raw_points, (str, bytes)):
        return [
            (point.get("time", point.get("frame", index)), point)
            for index, point in enumerate(raw_points)
            if isinstance(point, Mapping)
        ]
    if isinstance(raw_points, Mapping):
        return list(raw_points.items())
    ignored = {"mode", "relative_to", "keyframes", "points"}
    return [(key, value) for key, value in track_spec.items() if key not in ignored]


def _validate_position_keyframe_value(value: Any, *, field_path: str, issues: list[str]) -> None:
    if isinstance(value, Mapping):
        x_value = value.get("x", value.get("left"))
        y_value = value.get("y", value.get("top"))
        _parse_float_like(x_value, field_path=f"{field_path}.x", issues=issues)
        _parse_float_like(y_value, field_path=f"{field_path}.y", issues=issues)
        return
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or len(value) != 2:
        issues.append(f"{field_path} must be a 2-item [x, y] sequence or {{x, y}} object.")
        return
    for index, component in enumerate(value):
        _parse_float_like(component, field_path=f"{field_path}[{index}]", issues=issues)


def _is_bezier_easing(easing: str) -> bool:
    return re.fullmatch(
        r"bezier\(\s*[-+]?\d*\.?\d+\s*,\s*[-+]?\d*\.?\d+\s*,\s*[-+]?\d*\.?\d+\s*,\s*[-+]?\d*\.?\d+\s*\)",
        easing,
    ) is not None


def _validate_layer_spec(
    layer_spec: Any,
    *,
    index: int,
    issues: list[str],
    total_frames: int | None,
    effective_ids: dict[str, int],
) -> None:
    layer_path = f"layers[{index}]"
    if not isinstance(layer_spec, Mapping):
        issues.append(f"{layer_path} must be an object.")
        return

    layer_type = layer_spec.get("type")
    if layer_type not in SUPPORTED_LAYER_TYPES:
        supported = ", ".join(sorted(SUPPORTED_LAYER_TYPES))
        issues.append(f"{layer_path}.type must be one of {supported}, got {layer_type!r}.")
        return

    effective_id = layer_spec.get("id")
    if not effective_id:
        if layer_type in {"video", "image"} and isinstance(layer_spec.get("source"), str):
            effective_id = Path(layer_spec["source"]).stem
        else:
            effective_id = f"{layer_type}_{index}"
    if not isinstance(effective_id, str) or not effective_id:
        issues.append(f"{layer_path}.id must resolve to a non-empty string.")
    else:
        duplicate_index = effective_ids.get(effective_id)
        if duplicate_index is not None:
            issues.append(f"{layer_path}.id duplicates layers[{duplicate_index}].id: {effective_id!r}.")
        else:
            effective_ids[effective_id] = index

    start_frame = 0
    if "start_frame" in layer_spec:
        parsed = _parse_int_like(layer_spec["start_frame"], field_path=f"{layer_path}.start_frame", issues=issues)
        if parsed is not None:
            start_frame = parsed
    if start_frame < 0:
        issues.append(f"{layer_path}.start_frame must be >= 0, got {start_frame}.")

    duration = None
    if "duration" in layer_spec:
        duration = _parse_int_like(layer_spec["duration"], field_path=f"{layer_path}.duration", issues=issues)
        if duration is not None and duration <= 0:
            issues.append(f"{layer_path}.duration must be > 0, got {duration}.")

    end_frame = None
    if "end_frame" in layer_spec and layer_spec.get("end_frame") is not None:
        end_frame = _parse_int_like(layer_spec["end_frame"], field_path=f"{layer_path}.end_frame", issues=issues)
        if end_frame is not None and end_frame <= start_frame:
            issues.append(
                f"{layer_path}.end_frame must be greater than start_frame ({start_frame}), got {end_frame}."
            )

    if end_frame is None and duration is not None:
        end_frame = start_frame + duration

    if total_frames is not None and end_frame is not None and end_frame > total_frames:
        issues.append(
            f"{layer_path} ends at frame {end_frame}, beyond plan.total_frames={total_frames}."
        )

    if "z_index" in layer_spec:
        _parse_int_like(layer_spec["z_index"], field_path=f"{layer_path}.z_index", issues=issues)
    if "position" in layer_spec:
        _validate_position(layer_spec["position"], field_path=f"{layer_path}.position", issues=issues)
    if "opacity" in layer_spec:
        opacity = _parse_float_like(layer_spec["opacity"], field_path=f"{layer_path}.opacity", issues=issues)
        if opacity is not None and not 0.0 <= opacity <= 1.0:
            issues.append(f"{layer_path}.opacity must be within [0.0, 1.0], got {opacity}.")
    if "scale" in layer_spec:
        scale = _parse_float_like(layer_spec["scale"], field_path=f"{layer_path}.scale", issues=issues)
        if scale is not None and scale <= 0.0:
            issues.append(f"{layer_path}.scale must be > 0, got {scale}.")
    if "rotation_deg" in layer_spec:
        _parse_float_like(layer_spec["rotation_deg"], field_path=f"{layer_path}.rotation_deg", issues=issues)
    if "blend_mode" in layer_spec and layer_spec["blend_mode"] not in SUPPORTED_BLEND_MODES:
        supported = ", ".join(sorted(SUPPORTED_BLEND_MODES))
        issues.append(
            f"{layer_path}.blend_mode must be one of {supported}, got {layer_spec['blend_mode']!r}."
        )

    if "size" in layer_spec:
        _validate_size(layer_spec["size"], field_path=f"{layer_path}.size", issues=issues)

    if layer_type in {"video", "image"}:
        _validate_source_path(layer_spec.get("source"), field_path=f"{layer_path}.source", issues=issues)
    elif layer_type == "text":
        text = layer_spec.get("text")
        if not isinstance(text, str) or not text.strip():
            issues.append(f"{layer_path}.text must be a non-empty string.")
        if "font_config" in layer_spec:
            _validate_font_config(layer_spec["font_config"], field_path=f"{layer_path}.font_config", issues=issues)
    elif layer_type == "solid":
        _validate_unit_rgba(layer_spec.get("color"), field_path=f"{layer_path}.color", issues=issues)
    elif layer_type == "html":
        has_source = bool(layer_spec.get("source"))
        has_inline = isinstance(layer_spec.get("html"), str) and bool(layer_spec.get("html", "").strip())
        if has_source:
            _validate_source_path(layer_spec.get("source"), field_path=f"{layer_path}.source", issues=issues)
        if not has_source and not has_inline:
            issues.append(f"{layer_path} needs source or inline html.")
    elif layer_type == "lottie":
        _validate_source_path(layer_spec.get("source"), field_path=f"{layer_path}.source", issues=issues)

    if layer_type in {"image", "text", "solid", "html", "lottie"} and end_frame is None and total_frames is None:
        issues.append(
            f"{layer_path} needs duration/end_frame or plan.total_frames so preview length is explicit."
        )

    if "mask_source" in layer_spec:
        _validate_source_path(layer_spec.get("mask_source"), field_path=f"{layer_path}.mask_source", issues=issues)
    if "primitives" in layer_spec:
        _validate_primitive_chain(layer_spec["primitives"], field_path=f"{layer_path}.primitives", issues=issues)
    if "keyframes" in layer_spec:
        _validate_keyframes(
            layer_spec["keyframes"],
            field_path=f"{layer_path}.keyframes",
            issues=issues,
            layer_start=start_frame,
            layer_end=end_frame,
        )


def validate_layer_plan(plan: Any) -> None:
    """Raise LayerPlanValidationError when the plan violates layer invariants."""
    issues: list[str] = []
    if not isinstance(plan, Mapping):
        raise LayerPlanValidationError(["plan must be an object."])

    layers = plan.get("layers")
    if not isinstance(layers, list) or not layers:
        issues.append("layers must be a non-empty list.")

    total_frames = None
    if "width" in plan:
        width = _parse_int_like(plan["width"], field_path="width", issues=issues)
        if width is not None and width <= 0:
            issues.append(f"width must be > 0, got {width}.")
    if "height" in plan:
        height = _parse_int_like(plan["height"], field_path="height", issues=issues)
        if height is not None and height <= 0:
            issues.append(f"height must be > 0, got {height}.")
    if "fps" in plan:
        fps = _parse_float_like(plan["fps"], field_path="fps", issues=issues)
        if fps is not None and fps <= 0.0:
            issues.append(f"fps must be > 0, got {fps}.")
    if "total_frames" in plan:
        total_frames = _parse_int_like(plan["total_frames"], field_path="total_frames", issues=issues)
        if total_frames is not None and total_frames <= 0:
            issues.append(f"total_frames must be > 0, got {total_frames}.")

    effective_ids: dict[str, int] = {}
    if isinstance(layers, list):
        for index, layer_spec in enumerate(layers):
            _validate_layer_spec(
                layer_spec,
                index=index,
                issues=issues,
                total_frames=total_frames,
                effective_ids=effective_ids,
            )

    if issues:
        raise LayerPlanValidationError(issues)


def _default_sample_frames(stack: Any) -> list[int]:
    total_frames = int(getattr(stack, "total_frames", 0) or 0)
    if total_frames <= 0:
        return []
    sample_frames: set[int] = {0, total_frames - 1}
    for layer in getattr(stack, "layers", []):
        start = max(0, int(getattr(layer, "start_frame", 0)))
        raw_end = getattr(layer, "end_frame", None)
        end = total_frames if raw_end is None else min(total_frames, int(raw_end))
        if end <= start:
            continue
        sample_frames.add(start)
        sample_frames.add(end - 1)
        sample_frames.add(start + ((end - start - 1) // 2))
    return sorted(frame for frame in sample_frames if 0 <= frame < total_frames)


def validate_layer_stack_preview(stack: Any, *, sample_frames: Sequence[int] | None = None) -> list[int]:
    """Render representative frames and assert that preview output is sane."""
    issues: list[str] = []
    indices = list(sample_frames) if sample_frames is not None else _default_sample_frames(stack)
    if not indices:
        raise LayerPlanValidationError(["preview smoke check needs at least one sample frame."])

    width = int(getattr(stack, "width", 0) or 0)
    height = int(getattr(stack, "height", 0) or 0)
    saw_visible_pixels = False

    for frame_index in indices:
        try:
            frame = stack.render_frame(frame_index)
        except Exception as exc:  # pragma: no cover - error path is the assertion target
            issues.append(f"render_frame({frame_index}) failed during preview smoke check: {exc}")
            continue
        if not isinstance(frame, np.ndarray):
            issues.append(f"render_frame({frame_index}) must return a numpy array, got {type(frame)!r}.")
            continue
        if frame.shape != (height, width, 4):
            issues.append(
                f"render_frame({frame_index}) returned shape {frame.shape}, expected {(height, width, 4)}."
            )
        if frame.dtype != np.float32:
            issues.append(f"render_frame({frame_index}) returned dtype {frame.dtype}, expected float32.")
        if not np.isfinite(frame).all():
            issues.append(f"render_frame({frame_index}) returned non-finite pixels.")
        if np.min(frame) < -1e-6 or np.max(frame) > 1.0 + 1e-6:
            issues.append(f"render_frame({frame_index}) produced pixels outside [0.0, 1.0].")
        if frame.shape[-1] == 4 and float(np.max(frame[..., 3])) > 0.0:
            saw_visible_pixels = True

    if not saw_visible_pixels:
        issues.append("preview smoke check rendered only fully transparent frames across all samples.")

    if issues:
        raise LayerPlanValidationError(issues)
    return indices


__all__ = [
    "LayerPlanValidationError",
    "SUPPORTED_BLEND_MODES",
    "SUPPORTED_KEYFRAME_PROPERTIES",
    "SUPPORTED_LAYER_TYPES",
    "validate_layer_plan",
    "validate_layer_stack_preview",
]
