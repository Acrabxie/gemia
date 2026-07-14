"""Resolve 21 optical flow speed change manifest."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from gemia.video.timeline_assets import cache_key_for_path, probe_media


def render_optical_flow_speed_change_manifest(
    input_paths: list[str],
    output_dir: str,
    *,
    package_id: str = "resolve21_optical_flow_speed_change",
    preset_name: str = "optical_flow_slow_motion_review",
    retime_targets: list[dict] | dict | None = None,
) -> str:
    """Emit Resolve-style optical flow speed change manifests linked to real clips."""
    if not input_paths:
        raise ValueError("input_paths must contain at least one media path")
    output_root = Path(output_dir).expanduser().resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    package = _safe_id(package_id)

    sources = []
    for index, raw_path in enumerate(input_paths):
        source = Path(raw_path).expanduser().resolve()
        if not source.exists():
            raise FileNotFoundError(f"Input media not found: {source}")
        if not source.is_file():
            raise OSError(f"Input media is not a file: {source}")
        probe = probe_media(str(source))
        if int(probe.get("width") or 0) <= 0 or int(probe.get("height") or 0) <= 0:
            raise ValueError(
                f"Optical flow speed change requires visual media: {source}"
            )
        sources.append(
            {
                "clip_id": f"of_clip_{index:02d}_{_safe_id(source.stem)}",
                "source_path": str(source),
                "asset_ref": _asset_ref(source, probe),
                "cache_key": cache_key_for_path(str(source)),
                "source_probe": probe,
            }
        )

    targets = _normalize_retime_targets(retime_targets)
    clip_assignments = [
        {
            "clip_id": source["clip_id"],
            "asset_ref": source["asset_ref"],
            "retime_target": targets[index % len(targets)],
            "analysis_window": _analysis_window(source),
            "cache_key": source["cache_key"],
        }
        for index, source in enumerate(sources)
    ]
    manifest = {
        "schema_version": 1,
        "effect": "resolve21_optical_flow_speed_change_manifest",
        "rendered_at": datetime.now(timezone.utc).isoformat(),
        "package": {
            "package_id": package,
            "preset_name": _safe_id(preset_name),
            "clip_count": len(sources),
            "target_count": len(targets),
        },
        "sources": sources,
        "retime_targets": targets,
        "clip_assignments": clip_assignments,
        "resolve_controls": {
            "page": "Edit",
            "panel": "Retiming and Scaling",
            "retime_process": "Optical Flow",
            "motion_estimation_quality": "Better",
            "motion_range_scale": "Small",
        },
        "diagnostics": [
            f"{len(sources)} real clips linked to optical flow speed change metadata",
            f"{len(targets)} bounded retime targets emitted",
        ],
        "review_hints": [
            "manifests are metadata manifests; source media is not destructively modified",
            "re-analyze if source cache_key changes",
            "speed factors clamped to 0.1..4.0, quality clamped to 1..5",
        ],
    }
    manifest_path = output_root / "optical_flow_speed_change_manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(manifest_path)


def _normalize_retime_targets(
    retime_targets: list[dict] | dict | None
) -> list[dict[str, Any]]:
    if retime_targets is None:
        return [_default_retime_target()]

    normalized_targets: list[dict[str, Any]] = []

    if isinstance(retime_targets, dict):
        # Check if the dictionary itself looks like a single target config
        if "speed_factor" in retime_targets or "interpolation_quality" in retime_targets:
            normalized_targets.append(_normalize_single_target(retime_targets))
        else:
            # Otherwise, assume it's a mapping of target_id -> config
            for target_id, config in retime_targets.items():
                if isinstance(config, dict):
                    # Inject the target_id as the 'id' for the config
                    config_with_id = {"id": target_id, **config}
                    normalized_targets.append(_normalize_single_target(config_with_id))
    elif isinstance(retime_targets, list):
        for t in retime_targets:
            if isinstance(t, dict):
                normalized_targets.append(_normalize_single_target(t))

    return normalized_targets or [_default_retime_target()]


def _default_retime_target() -> dict[str, Any]:
    return {
        "id": "default_slow_motion",
        "label": "Default Slow Motion",
        "speed_factor": 0.5,
        "interpolation_quality": 3,
        "generated_frame_range": "full_clip",
    }


def _normalize_single_target(raw: dict[str, Any]) -> dict[str, Any]:
    speed_factor = _clamp_float(raw.get("speed_factor"), 0.1, 4.0, 1.0)
    interpolation_quality = _clamp_int(raw.get("interpolation_quality"), 1, 5, 3)
    generated_frame_range = _safe_choice(
        str(raw.get("generated_frame_range") or "full_clip"),
        {"full_clip", "effect_region"},
    )
    # Prioritize 'id' from raw input, then 'profile_id', then generate a default
    target_id = raw.get("id") or raw.get("profile_id")
    if target_id is None:
        target_id = f"retime_target_{_safe_id(str(raw.get('label') or speed_factor))}"
    else:
        target_id = str(target_id)

    return {
        "id": _safe_id(target_id),
        "label": str(raw.get("label") or f"Speed Factor {speed_factor}"),
        "speed_factor": speed_factor,
        "interpolation_quality": interpolation_quality,
        "generated_frame_range": generated_frame_range,
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


def _clamp_int(value: Any, min_val: int, max_val: int, default: int) -> int:
    try:
        number = int(value)
    except Exception:
        number = default
    return max(min_val, min(number, max_val))


def _safe_choice(value: str, choices: set[str]) -> str:
    safe = _safe_id(value)
    if safe in choices:
        return safe
    return "full_clip" if "full_clip" in choices else sorted(choices)[0]


def _to_snake_case(name: str) -> str:
    # Convert CamelCase/PascalCase to snake_case
    name = re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()
    return name


def _safe_id(value: str) -> str:
    # First, convert to snake_case to handle CamelCase/PascalCase
    value = _to_snake_case(value)
    return re.sub(r"[^a-z0-9_-]+", "_", value.strip()).strip("_") or "item"


__all__ = ["render_optical_flow_speed_change_manifest"]
