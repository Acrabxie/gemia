"""Shadow preview rendering for layer-first workflows."""
from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from gemia.video.backends import RenderProfile, choose_render_backend
from gemia.video.compositing_graph import (
    build_compositing_graph_from_layer_plan,
    compile_compositing_graph,
    infer_layer_plan_metric_sources,
)
from gemia.video.layer_validation import validate_layer_plan, validate_layer_stack_preview
from gemia.video.layers import execute_layer_plan, materialize_layer_plan
from gemia.video.proxy import ProxyManager


@dataclass(frozen=True)
class ShadowPreviewResult:
    output_path: str
    manifest_path: str
    proxy_map: dict[str, str]
    render_backend: dict[str, str] | None = None


def _scale_keyframe_track(track_spec: Any, *, scale: float) -> dict[str, Any]:
    scaled_track: dict[str, Any] = {}
    for frame_key, value_spec in dict(track_spec or {}).items():
        if isinstance(value_spec, dict):
            scaled_value = dict(value_spec)
            if "value" in scaled_value:
                scaled_value["value"] = float(scaled_value["value"]) * scale
            scaled_track[str(frame_key)] = scaled_value
        else:
            scaled_track[str(frame_key)] = float(value_spec) * scale
    return scaled_track


def _scale_vector_value(value: Any, *, scale: float) -> Any:
    if isinstance(value, dict):
        scaled = dict(value)
        if "value" in scaled:
            scaled["value"] = _scale_vector_value(scaled["value"], scale=scale)
        for key in ("x", "y", "left", "top"):
            if key in scaled:
                scaled[key] = float(scaled[key]) * scale
        return scaled
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        scaled_items = list(value)
        scaled_items[0] = float(scaled_items[0]) * scale
        scaled_items[1] = float(scaled_items[1]) * scale
        return scaled_items
    return value


def _scale_vector_keyframe_track(track_spec: Any, *, scale: float) -> Any:
    if not isinstance(track_spec, dict):
        return track_spec
    scaled_track = deepcopy(track_spec)
    for list_key in ("points", "keyframes"):
        points = scaled_track.get(list_key)
        if isinstance(points, list):
            scaled_track[list_key] = [
                _scale_vector_value(point, scale=scale)
                for point in points
            ]
            return scaled_track
    ignored = {"mode", "relative_to", "points", "keyframes"}
    for frame_key, value_spec in list(scaled_track.items()):
        if frame_key in ignored:
            continue
        scaled_track[frame_key] = _scale_vector_value(value_spec, scale=scale)
    return scaled_track


def _scale_layer_size(layer: dict[str, Any], *, scale: float) -> None:
    raw_size = layer.get("size")
    if not isinstance(raw_size, (list, tuple)) or len(raw_size) < 2:
        return
    layer["size"] = [
        max(1, int(round(float(raw_size[0]) * scale))),
        max(1, int(round(float(raw_size[1]) * scale))),
    ]


def _layer_uses_proxy_source(layer: dict[str, Any]) -> bool:
    metadata = layer.get("metadata")
    return isinstance(metadata, dict) and bool(metadata.get("proxy_path"))


def _scale_layer_plan(plan: dict[str, Any], *, max_long_edge: int) -> dict[str, Any]:
    preview_plan = deepcopy(plan)
    width = int(preview_plan.get("width", 0) or 0)
    height = int(preview_plan.get("height", 0) or 0)
    if max_long_edge <= 0 or width <= 0 or height <= 0:
        return preview_plan
    scale = min(1.0, float(max_long_edge) / float(max(width, height)))
    preview_plan["width"] = max(1, int(round(width * scale)))
    preview_plan["height"] = max(1, int(round(height * scale)))
    for layer in preview_plan.get("layers", []):
        if "position" in layer and isinstance(layer["position"], (list, tuple)) and len(layer["position"]) == 2:
            layer["position"] = [
                int(round(float(layer["position"][0]) * scale)),
                int(round(float(layer["position"][1]) * scale)),
            ]
        if layer.get("type") == "text":
            font_config = dict(layer.get("font_config", {}) or {})
            font_config["size"] = max(1, int(round(float(font_config.get("size", 48)) * scale)))
            if "padding" in font_config or scale < 1.0:
                font_config["padding"] = max(0, int(round(float(font_config.get("padding", 4)) * scale)))
            layer["font_config"] = font_config
        elif layer.get("type") in {"solid", "html", "lottie"}:
            _scale_layer_size(layer, scale=scale)

        if layer.get("type") in {"image", "video"} and not _layer_uses_proxy_source(layer):
            layer["scale"] = float(layer.get("scale", 1.0) or 1.0) * scale
            keyframes = layer.get("keyframes")
            if isinstance(keyframes, dict) and "scale" in keyframes:
                keyframes["scale"] = _scale_keyframe_track(keyframes["scale"], scale=scale)
        keyframes = layer.get("keyframes")
        if isinstance(keyframes, dict):
            if "position" in keyframes:
                keyframes["position"] = _scale_vector_keyframe_track(keyframes["position"], scale=scale)
            for scalar_position_key in ("position_x", "position_y", "x", "y"):
                if scalar_position_key in keyframes:
                    keyframes[scalar_position_key] = _scale_keyframe_track(
                        keyframes[scalar_position_key],
                        scale=scale,
                    )
    preview_plan.setdefault("metadata", {})
    preview_plan["metadata"]["shadow_scale"] = scale
    return preview_plan


def _assert_preview_matches_compiled_metadata(preview_stack: Any, compiled: Any) -> None:
    mismatches: list[str] = []
    expected = {
        "width": int(getattr(preview_stack, "width", 0) or 0),
        "height": int(getattr(preview_stack, "height", 0) or 0),
        "fps": float(getattr(preview_stack, "fps", 0.0) or 0.0),
        "total_frames": int(getattr(preview_stack, "total_frames", 0) or 0),
    }
    for key, expected_value in expected.items():
        actual_value = compiled.metadata.get(key)
        if key == "fps":
            if abs(float(actual_value or 0.0) - expected_value) > 1e-6:
                mismatches.append(f"{key} expected {expected_value}, got {actual_value}")
            continue
        if actual_value != expected_value:
            mismatches.append(f"{key} expected {expected_value}, got {actual_value}")
    if mismatches:
        raise RuntimeError(
            "Shadow preview compiled graph metadata drifted from preview stack: "
            + "; ".join(mismatches)
        )


def render_shadow_preview(
    plan: dict[str, Any],
    output_path: str | Path,
    *,
    backend: str | None = None,
    max_long_edge: int = 540,
    frame_step: int = 2,
    proxy_resolution: int = 540,
    proxy_root: str | Path | None = None,
) -> ShadowPreviewResult:
    """Render a low-fidelity shadow preview and write a manifest beside it."""
    preview_plan = materialize_layer_plan(plan)
    validate_layer_plan(preview_plan)
    authored_metric_sources = infer_layer_plan_metric_sources(
        plan,
        materialized_plan=preview_plan,
    )
    proxy_map: dict[str, str] = {}

    if proxy_root is not None:
        manager = ProxyManager(proxy_root)
        preview_plan, proxy_map = manager.attach_to_plan(preview_plan, resolution=proxy_resolution)

    preview_plan = _scale_layer_plan(preview_plan, max_long_edge=max_long_edge)
    preview_plan.setdefault("metadata", {})
    preview_plan["metadata"]["authored_metric_sources"] = authored_metric_sources
    compiled = compile_compositing_graph(build_compositing_graph_from_layer_plan(preview_plan))
    preview_stack = execute_layer_plan(preview_plan)
    validate_layer_stack_preview(preview_stack)
    _assert_preview_matches_compiled_metadata(preview_stack, compiled)

    output_path = Path(output_path).expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    execution_graph = build_compositing_graph_from_layer_plan(preview_plan)
    render_backend, backend_decision = choose_render_backend(
        execution_graph,
        requested=backend,
    )
    render_result = render_backend.render_preview(
        execution_graph,
        output_path,
        profile=RenderProfile.preview(step=max(int(frame_step), 1)),
    )

    manifest_path = output_path.with_suffix(".preview.json")
    manifest = {
        "output_path": render_result.output_path,
        "frame_step": max(int(frame_step), 1),
        "max_long_edge": int(max_long_edge),
        "proxy_resolution": int(proxy_resolution),
        "proxy_map": proxy_map,
        "render_backend": backend_decision.to_dict(),
        "compiled_graph": compiled.to_dict(),
    }
    if render_result.compiled_plan is not None:
        manifest["execution_graph"] = render_result.compiled_plan.to_dict()
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return ShadowPreviewResult(
        output_path=render_result.output_path,
        manifest_path=str(manifest_path),
        proxy_map=proxy_map,
        render_backend=backend_decision.to_dict(),
    )


__all__ = [
    "ShadowPreviewResult",
    "render_shadow_preview",
]
