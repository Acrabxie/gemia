"""Resolve 21 track follow mask manifest."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from gemia.video.timeline_assets import cache_key_for_path, probe_media


# Helper functions copied from gemia.video.optical_flow_speed_change
def _to_snake_case(name: str) -> str:
    # Convert CamelCase/PascalCase to snake_case
    name = re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()
    return name


def _safe_id(value: str) -> str:
    # First, convert to snake_case to handle CamelCase/PascalCase
    value = _to_snake_case(value)
    return re.sub(r"[^a-z0-9_-]+", "_", value.strip()).strip("_") or "item"


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


def _safe_choice(value: str, choices: set[str], default: str) -> str:
    safe = _safe_id(value)
    if safe in choices:
        return safe
    return default


# Helper from gemia.video.advanced_noise_reduction
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


DEFAULT_TRACK_WINDOW: dict[str, Any] = {
    "id": "default_track_window",
    "label": "Default Track Window",
    "target_kind": "object",  # person/object/vehicle/face/generic
    "mask_shape": "rectangle",  # rectangle/ellipse/polygon
    "start_rect": [0.25, 0.25, 0.5, 0.5],  # x, y, width, height [0..1]
    "follow_mode": "point",  # point/window/planar
    "tracking_quality": 3,  # 1..5
    "softness": 0.2,  # 0..1
    "effect_target": "color_window",  # color_window/blur/highlight/stabilize
}


def render_track_follow_objects_mask_manifest(
    input_paths: list[str],
    output_dir: str,
    *,
    package_id: str = "resolve21_track_follow_objects_mask",
    preset_name: str = "track_follow_mask_review",
    track_windows: list[dict] | dict | None = None,
) -> str:
    """Emit Resolve 21 object/person tracking mask manifests."""
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
            raise ValueError(f"Tracking mask requires visual media: {source}")
        sources.append(
            {
                "clip_id": f"tfm_clip_{index:02d}_{_safe_id(source.stem)}",
                "source_path": str(source),
                "asset_ref": _asset_ref(source, probe),
                "cache_key": cache_key_for_path(str(source)),
                "source_probe": probe,
            }
        )

    normalized_track_windows = _normalize_track_windows(track_windows)
    clip_assignments = [
        {
            "clip_id": source["clip_id"],
            "asset_ref": source["asset_ref"],
            "track_window": normalized_track_windows[index % len(normalized_track_windows)],
            "analysis_window": _analysis_window(source),
            "cache_key": source["cache_key"],
        }
        for index, source in enumerate(sources)
    ]
    manifest = {
        "schema_version": 1,
        "effect": "resolve21_track_follow_objects_mask_manifest",
        "rendered_at": datetime.now(timezone.utc).isoformat(),
        "package": {
            "package_id": package,
            "preset_name": _safe_id(preset_name),
            "clip_count": len(sources),
            "window_count": len(normalized_track_windows),
        },
        "sources": sources,
        "track_windows": normalized_track_windows,
        "clip_assignments": clip_assignments,
        "resolve_controls": {
            "page": "Color",
            "panel": "Tracker / Window",
            "tracker_mode": "Object/Person Recognition",
            "window_controls": ["Shape", "Position", "Softness"],
        },
        "diagnostics": [
            f"{len(sources)} real clips linked to tracking mask metadata",
            f"{len(normalized_track_windows)} bounded track windows emitted",
        ],
        "review_hints": [
            "manifests are metadata manifests; source media is not destructively modified",
            "re-analyze if source cache_key changes",
            "tracking quality and softness affect mask precision and feathering",
        ],
    }
    manifest_path = output_root / "track_follow_objects_mask_manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return str(manifest_path)


def _normalize_track_windows(
    track_windows: list[dict] | dict | None
) -> list[dict[str, Any]]:
    if track_windows is None:
        return [_normalize_single_track_window(DEFAULT_TRACK_WINDOW)]

    normalized_windows: list[dict[str, Any]] = []

    if isinstance(track_windows, dict):
        if "target_kind" in track_windows or "mask_shape" in track_windows:
            # It's a single window definition without an explicit ID
            normalized_windows.append(_normalize_single_track_window(track_windows))
        else:
            # It's a mapping of ID -> window config
            for window_id, config in track_windows.items():
                if isinstance(config, dict):
                    config_with_id = {"id": window_id, **config}
                    normalized_windows.append(_normalize_single_track_window(config_with_id))
    elif isinstance(track_windows, list):
        for window in track_windows:
            if isinstance(window, dict):
                normalized_windows.append(_normalize_single_track_window(window))

    return normalized_windows or [_normalize_single_track_window(DEFAULT_TRACK_WINDOW)]


def _normalize_single_track_window(raw: dict[str, Any]) -> dict[str, Any]:
    window_id = raw.get("id") or DEFAULT_TRACK_WINDOW["id"]
    label = raw.get("label") or raw.get("id") or DEFAULT_TRACK_WINDOW["label"]

    target_kind = _safe_choice(
        str(raw.get("target_kind") or DEFAULT_TRACK_WINDOW["target_kind"]),
        {"person", "object", "vehicle", "face", "generic"},
        DEFAULT_TRACK_WINDOW["target_kind"],
    )
    mask_shape = _safe_choice(
        str(raw.get("mask_shape") or DEFAULT_TRACK_WINDOW["mask_shape"]),
        {"rectangle", "ellipse", "polygon"},
        DEFAULT_TRACK_WINDOW["mask_shape"],
    )

    # Normalize start_rect (x, y, width, height) to [0..1]
    start_rect_raw = raw.get("start_rect", DEFAULT_TRACK_WINDOW["start_rect"])
    if isinstance(start_rect_raw, list) and len(start_rect_raw) == 4:
        start_rect = [
            _clamp_float(start_rect_raw[0], 0.0, 1.0, 0.0),  # x
            _clamp_float(start_rect_raw[1], 0.0, 1.0, 0.0),  # y
            _clamp_float(start_rect_raw[2], 0.0, 1.0, 1.0),  # width
            _clamp_float(start_rect_raw[3], 0.0, 1.0, 1.0),  # height
        ]
    else:
        start_rect = DEFAULT_TRACK_WINDOW["start_rect"]

    follow_mode = _safe_choice(
        str(raw.get("follow_mode") or DEFAULT_TRACK_WINDOW["follow_mode"]),
        {"point", "window", "planar"},
        DEFAULT_TRACK_WINDOW["follow_mode"],
    )
    tracking_quality = _clamp_int(
        raw.get("tracking_quality"), 1, 5, DEFAULT_TRACK_WINDOW["tracking_quality"]
    )
    softness = _clamp_float(
        raw.get("softness"), 0.0, 1.0, DEFAULT_TRACK_WINDOW["softness"]
    )
    effect_target = _safe_choice(
        str(raw.get("effect_target") or DEFAULT_TRACK_WINDOW["effect_target"]),
        {"color_window", "blur", "highlight", "stabilize"},
        DEFAULT_TRACK_WINDOW["effect_target"],
    )

    return {
        "id": _safe_id(str(window_id)),
        "label": str(label),
        "target_kind": target_kind,
        "mask_shape": mask_shape,
        "start_rect": start_rect,
        "follow_mode": follow_mode,
        "tracking_quality": tracking_quality,
        "softness": softness,
        "effect_target": effect_target,
    }


__all__ = ["render_track_follow_objects_mask_manifest"]
