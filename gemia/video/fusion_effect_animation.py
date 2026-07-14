"""Resolve 21 Edit-page Fusion effect animation manifest."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from gemia.video.timeline_assets import cache_key_for_path, probe_media


DEFAULT_FUSION_EFFECT_CONTROLS: list[dict[str, Any]] = [
    {
        "id": "glow_intensity_ramp",
        "label": "Glow intensity ramp",
        "fusion_effect": "Glow",
        "parameter": "blend",
        "edit_lane": "Inspector/Fusion Effects",
        "curve_editor": "Edit Page Curves",
        "keyframes": [
            {"time_fraction": 0.0, "value": 0.0, "easing": "ease_in"},
            {"time_fraction": 0.5, "value": 0.72, "easing": "ease_in_out"},
            {"time_fraction": 1.0, "value": 0.24, "easing": "ease_out"},
        ],
    },
    {
        "id": "transform_zoom_pulse",
        "label": "Transform zoom pulse",
        "fusion_effect": "Transform",
        "parameter": "size",
        "edit_lane": "Inspector/Fusion Effects",
        "curve_editor": "Edit Page Keyframes",
        "keyframes": [
            {"time_fraction": 0.0, "value": 1.0, "easing": "linear"},
            {"time_fraction": 0.42, "value": 1.08, "easing": "ease_in_out"},
            {"time_fraction": 1.0, "value": 1.0, "easing": "ease_out"},
        ],
    },
]


def render_animate_fusion_effects_edit_page_manifest(
    input_paths: list[str],
    output_dir: str,
    *,
    package_id: str = "resolve21_animate_fusion_effects_edit_page",
    preset_name: str = "fusion_effects_edit_page_animation_review",
    effect_controls: list[dict] | dict | None = None,
) -> str:
    """Emit Edit-page Fusion effect keyframe/curve manifests linked to real clips."""
    if not input_paths:
        raise ValueError("input_paths must contain at least one media path")
    output_root = Path(output_dir).expanduser().resolve()
    output_root.mkdir(parents=True, exist_ok=True)

    sources = []
    for index, raw_path in enumerate(input_paths):
        source = Path(raw_path).expanduser().resolve()
        if not source.exists():
            raise FileNotFoundError(f"Input media not found: {source}")
        if not source.is_file():
            raise OSError(f"Input media is not a file: {source}")
        probe = probe_media(str(source))
        if int(probe.get("width") or 0) <= 0 or int(probe.get("height") or 0) <= 0:
            raise ValueError(f"Fusion effect animation requires visual media: {source}")
        sources.append(
            {
                "clip_id": f"fea_clip_{index:02d}_{_safe_id(source.stem)}",
                "source_path": str(source),
                "asset_ref": _asset_ref(source, probe),
                "cache_key": cache_key_for_path(str(source)),
                "source_probe": probe,
            }
        )

    controls = _normalize_effect_controls(effect_controls)
    clip_assignments = [
        {
            "clip_id": source["clip_id"],
            "asset_ref": source["asset_ref"],
            "effect_control": controls[index % len(controls)],
            "analysis_window": _analysis_window(source),
            "cache_key": source["cache_key"],
        }
        for index, source in enumerate(sources)
    ]
    manifest = {
        "schema_version": 1,
        "effect": "resolve21_animate_fusion_effects_edit_page_manifest",
        "rendered_at": datetime.now(timezone.utc).isoformat(),
        "package": {
            "package_id": _safe_id(package_id),
            "preset_name": _safe_id(preset_name),
            "clip_count": len(sources),
            "control_count": len(controls),
        },
        "sources": sources,
        "fusion_effect_controls": controls,
        "clip_assignments": clip_assignments,
        "resolve_controls": {
            "page": "Edit",
            "panels": ["Inspector", "Keyframes", "Curves"],
            "fusion_effect_surface": "Fusion Effects on Edit/Cut timeline clips",
            "editable_metadata": ["effect", "parameter", "keyframes", "curve handles"],
        },
        "diagnostics": [
            f"{len(sources)} real clips linked to Edit-page Fusion effect animation metadata",
            f"{len(controls)} bounded Fusion effect controls emitted",
        ],
        "review_hints": [
            "manifests are metadata manifests; source media is not destructively modified",
            "re-analyze if source cache_key changes",
            "keyframe time fractions are clamped to 0..1 and sorted for timeline review",
        ],
    }
    manifest_path = output_root / "fusion_effects_edit_page_animation_manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return str(manifest_path)


def _normalize_effect_controls(effect_controls: list[dict] | dict | None) -> list[dict[str, Any]]:
    if effect_controls is None:
        return [_normalize_single_effect_control(item) for item in DEFAULT_FUSION_EFFECT_CONTROLS]

    normalized: list[dict[str, Any]] = []
    if isinstance(effect_controls, dict):
        if "fusion_effect" in effect_controls or "keyframes" in effect_controls:
            normalized.append(_normalize_single_effect_control(effect_controls))
        else:
            for control_id, config in effect_controls.items():
                if isinstance(config, dict):
                    normalized.append(_normalize_single_effect_control({"id": control_id, **config}))
    elif isinstance(effect_controls, list):
        for item in effect_controls:
            if isinstance(item, dict):
                normalized.append(_normalize_single_effect_control(item))

    return normalized or [_normalize_single_effect_control(DEFAULT_FUSION_EFFECT_CONTROLS[0])]


def _normalize_single_effect_control(raw: dict[str, Any]) -> dict[str, Any]:
    control_id = raw.get("id") or raw.get("control_id") or "fusion_effect_control"
    return {
        "id": _safe_id(str(control_id)),
        "label": str(raw.get("label") or raw.get("name") or control_id),
        "fusion_effect": str(raw.get("fusion_effect") or raw.get("effect") or "Fusion Effect"),
        "parameter": _safe_id(str(raw.get("parameter") or raw.get("parameter_name") or "blend")),
        "edit_lane": str(raw.get("edit_lane") or "Inspector/Fusion Effects"),
        "curve_editor": _safe_choice(
            str(raw.get("curve_editor") or "Edit Page Curves"),
            {"edit_page_curves", "edit_page_keyframes", "inspector_keyframes"},
            "edit_page_curves",
        ),
        "keyframes": _normalize_keyframes(raw.get("keyframes")),
        "duration_policy": _safe_choice(
            str(raw.get("duration_policy") or "scale_to_clip"),
            {"scale_to_clip", "absolute_seconds", "effect_region"},
            "scale_to_clip",
        ),
    }


def _normalize_keyframes(raw_keyframes: Any) -> list[dict[str, Any]]:
    source_keyframes = raw_keyframes if isinstance(raw_keyframes, list) else []
    keyframes = [
        _normalize_keyframe(item, index)
        for index, item in enumerate(source_keyframes)
        if isinstance(item, dict)
    ]
    if len(keyframes) < 2:
        keyframes = [
            {"time_fraction": 0.0, "value": 0.0, "easing": "linear"},
            {"time_fraction": 1.0, "value": 1.0, "easing": "ease_out"},
        ]
    return sorted(keyframes, key=lambda item: item["time_fraction"])


def _normalize_keyframe(raw: dict[str, Any], index: int) -> dict[str, Any]:
    time_value = raw.get("time_fraction", raw.get("position", raw.get("time", index)))
    return {
        "time_fraction": _clamp_float(time_value, 0.0, 1.0, float(index)),
        "value": _clamp_float(raw.get("value"), -10.0, 10.0, 0.0),
        "easing": _safe_choice(
            str(raw.get("easing") or raw.get("curve") or "linear"),
            {"linear", "ease_in", "ease_out", "ease_in_out", "hold"},
            "linear",
        ),
    }


def _analysis_window(source: dict[str, Any]) -> dict[str, Any]:
    probe = source["source_probe"]
    duration = round(float(probe.get("duration") or 0.0), 3)
    fps = max(float(probe.get("fps") or 24.0), 1.0)
    frames = max(1, int(round(duration * fps))) if duration else 1
    return {
        "start_seconds": 0.0,
        "end_seconds": duration,
        "estimated_frames": frames,
        "sample_stride": 4 if frames >= 24 else 1,
    }


def _asset_ref(path: Path, probe: dict[str, Any]) -> str:
    duration = round(float(probe.get("duration") or 0.0), 3)
    return f"{_safe_id(path.stem)}:{path.stat().st_size}:{duration}:{int(probe.get('width') or 0)}x{int(probe.get('height') or 0)}"


def _clamp_float(value: Any, min_val: float, max_val: float, default: float) -> float:
    try:
        number = float(value)
    except Exception:
        number = default
    return round(max(min_val, min(number, max_val)), 3)


def _safe_choice(value: str, choices: set[str], default: str) -> str:
    safe = _safe_id(value)
    return safe if safe in choices else default


def _to_snake_case(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def _safe_id(value: str) -> str:
    value = _to_snake_case(value)
    return re.sub(r"[^a-z0-9_-]+", "_", value.strip()).strip("_") or "item"


__all__ = ["DEFAULT_FUSION_EFFECT_CONTROLS", "render_animate_fusion_effects_edit_page_manifest"]
