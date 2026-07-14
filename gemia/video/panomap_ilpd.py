"""Resolve 21 Panomap and ILPD stereo retarget manifests."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from gemia.video.timeline_assets import probe_media


DEFAULT_PANOMAP_PRESETS: list[dict[str, Any]] = [
    {
        "id": "headset_review_neutral",
        "label": "Headset review neutral",
        "yaw_degrees": 0,
        "pitch_degrees": 0,
        "roll_degrees": 0,
        "field_of_view_degrees": 180,
        "ilpd_mm": 63.5,
        "convergence_distance_m": 2.0,
    },
    {
        "id": "comfort_retarget_close_subject",
        "label": "Comfort retarget close subject",
        "yaw_degrees": 8,
        "pitch_degrees": -2,
        "roll_degrees": 0,
        "field_of_view_degrees": 160,
        "ilpd_mm": 60.0,
        "convergence_distance_m": 1.2,
    },
]


def render_panomap_ilpd_stereo_retarget_manifest(
    input_paths: list[str],
    output_dir: str,
    *,
    package_id: str = "resolve21_panomap_ilpd_stereo_retarget",
    retarget_presets: list[dict[str, Any]] | None = None,
    projection: str = "equirectangular",
    stereo_layout: str = "left_right",
) -> str:
    """Emit Panomap rotation plus ILPD stereo retarget metadata for real clips."""
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
        sources.append(
            {
                "clip_id": f"panomap_clip_{index:02d}_{_safe_id(source.stem)}",
                "source_path": str(source),
                "asset_ref": _asset_ref(source, probe),
                "source_probe": probe,
                "panomap_readiness": _panomap_readiness(probe),
            }
        )

    presets = [_normalize_preset(raw, index) for index, raw in enumerate(retarget_presets or DEFAULT_PANOMAP_PRESETS)]
    retargets = []
    for preset in presets:
        retargets.append(
            {
                "retarget_id": f"{package}_{preset['preset_id']}",
                "label": preset["label"],
                "clip_asset_refs": [source["asset_ref"] for source in sources],
                "panomap_controls": {
                    "projection": _safe_id(projection),
                    "stereo_layout": _safe_id(stereo_layout),
                    "yaw_degrees": preset["yaw_degrees"],
                    "pitch_degrees": preset["pitch_degrees"],
                    "roll_degrees": preset["roll_degrees"],
                    "field_of_view_degrees": preset["field_of_view_degrees"],
                    "stabilize_horizon": preset["stabilize_horizon"],
                },
                "ilpd_controls": {
                    "interpupillary_distance_mm": preset["ilpd_mm"],
                    "convergence_distance_m": preset["convergence_distance_m"],
                    "parallax_budget_percent": preset["parallax_budget_percent"],
                    "comfort_bias": preset["comfort_bias"],
                },
                "resolve_controls": {
                    "page": "Fusion",
                    "toolset": "Panomap",
                    "controls": ["yaw", "pitch", "roll", "field_of_view", "ilpd", "convergence"],
                    "stereo_retarget": True,
                },
                "validation": _retarget_validation(preset, sources),
            }
        )

    manifest = {
        "schema_version": 1,
        "effect": "resolve21_panomap_ilpd_stereo_retarget_manifest",
        "rendered_at": datetime.now(timezone.utc).isoformat(),
        "package": {
            "package_id": package,
            "clip_count": len(sources),
            "retarget_count": len(retargets),
            "projection": _safe_id(projection),
            "stereo_layout": _safe_id(stereo_layout),
        },
        "sources": sources,
        "retargets": retargets,
        "diagnostics": [
            f"{len(sources)} real clips linked to Panomap/ILPD retarget metadata",
            f"{len(retargets)} stereo retarget presets emitted with stable asset refs",
        ],
        "review_hints": [
            "confirm yaw/pitch/roll framing in headset review before export",
            "keep ILPD within comfort bounds for close-subject stereo material",
            "preserve asset_ref values when replacing proxy media",
        ],
    }
    manifest_path = output_root / "panomap_ilpd_stereo_retarget_manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return str(manifest_path)


def _normalize_preset(raw: dict[str, Any], index: int) -> dict[str, Any]:
    fov = int(raw.get("field_of_view_degrees") or raw.get("fov") or 180)
    ilpd = float(raw.get("ilpd_mm") or raw.get("interpupillary_distance_mm") or 63.5)
    convergence = float(raw.get("convergence_distance_m") or raw.get("convergence_m") or 2.0)
    parallax = float(raw.get("parallax_budget_percent") or 3.0)
    return {
        "preset_id": _safe_id(str(raw.get("id") or raw.get("preset_id") or f"panomap_preset_{index}")),
        "label": str(raw.get("label") or f"Panomap Preset {index + 1}"),
        "yaw_degrees": _clamp_float(raw.get("yaw_degrees", raw.get("yaw", 0.0)), -180.0, 180.0),
        "pitch_degrees": _clamp_float(raw.get("pitch_degrees", raw.get("pitch", 0.0)), -90.0, 90.0),
        "roll_degrees": _clamp_float(raw.get("roll_degrees", raw.get("roll", 0.0)), -45.0, 45.0),
        "field_of_view_degrees": max(60, min(fov, 220)),
        "ilpd_mm": round(max(50.0, min(ilpd, 75.0)), 3),
        "convergence_distance_m": round(max(0.3, min(convergence, 100.0)), 3),
        "parallax_budget_percent": round(max(0.5, min(parallax, 10.0)), 3),
        "comfort_bias": _safe_id(str(raw.get("comfort_bias") or "balanced")),
        "stabilize_horizon": bool(raw.get("stabilize_horizon", True)),
    }


def _retarget_validation(preset: dict[str, Any], sources: list[dict[str, Any]]) -> dict[str, Any]:
    min_width = min(int(source["source_probe"].get("width") or 0) for source in sources)
    min_height = min(int(source["source_probe"].get("height") or 0) for source in sources)
    return {
        "source_count": len(sources),
        "source_resolution": f"{min_width}x{min_height}",
        "rotation_within_safe_bounds": abs(preset["pitch_degrees"]) <= 45 and abs(preset["roll_degrees"]) <= 20,
        "ilpd_within_comfort_range": 55.0 <= preset["ilpd_mm"] <= 70.0,
        "close_subject_caution": preset["convergence_distance_m"] < 1.0,
        "headset_preview_ready": min_width > 0 and min_height > 0,
    }


def _panomap_readiness(probe: dict[str, Any]) -> dict[str, Any]:
    width = int(probe.get("width") or 0)
    height = int(probe.get("height") or 0)
    ratio = round(width / max(height, 1), 3)
    return {
        "duration_seconds": round(float(probe.get("duration") or 0.0), 3),
        "aspect_ratio": ratio,
        "looks_equirectangular": 1.8 <= ratio <= 2.1,
        "has_audio": bool(probe.get("has_audio")),
    }


def _asset_ref(path: Path, probe: dict[str, Any]) -> str:
    duration = round(float(probe.get("duration") or 0.0), 3)
    return f"{_safe_id(path.stem)}:{path.stat().st_size}:{duration}:{int(probe.get('width') or 0)}x{int(probe.get('height') or 0)}"


def _clamp_float(value: Any, low: float, high: float) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        numeric = 0.0
    return round(max(low, min(numeric, high)), 3)


def _safe_id(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", value.strip()).strip("_").lower() or "item"


__all__ = ["DEFAULT_PANOMAP_PRESETS", "render_panomap_ilpd_stereo_retarget_manifest"]
