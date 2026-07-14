"""Resolve 21 advanced noise-reduction profile manifests."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from gemia.video.timeline_assets import cache_key_for_path, probe_media


DEFAULT_DENOISE_PROFILES: list[dict[str, Any]] = [
    {
        "id": "low_light_temporal",
        "label": "Low-light temporal cleanup",
        "temporal_frames": 3,
        "temporal_strength": 0.62,
        "spatial_strength": 0.28,
        "motion_estimation": "better",
        "luma_threshold": 0.36,
        "chroma_threshold": 0.42,
    },
    {
        "id": "fine_texture_spatial",
        "label": "Fine texture spatial cleanup",
        "temporal_frames": 1,
        "temporal_strength": 0.24,
        "spatial_strength": 0.58,
        "motion_estimation": "faster",
        "luma_threshold": 0.22,
        "chroma_threshold": 0.3,
    },
]


def render_advanced_noise_reduction_profile_manifest(
    input_paths: list[str],
    output_dir: str,
    *,
    package_id: str = "resolve21_advanced_noise_reduction_profile",
    profile_name: str = "advanced_noise_reduction_review",
    profiles: list[dict[str, Any]] | dict[str, dict[str, Any]] | None = None,
) -> str:
    """Emit Resolve-style temporal/spatial denoise profiles linked to real clips."""
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
            raise ValueError(f"Advanced noise reduction requires visual media: {source}")
        sources.append(
            {
                "clip_id": f"nr_clip_{index:02d}_{_safe_id(source.stem)}",
                "source_path": str(source),
                "asset_ref": _asset_ref(source, probe),
                "cache_key": cache_key_for_path(str(source)),
                "source_probe": probe,
                "noise_profile_probe": _noise_profile_probe(probe),
            }
        )

    denoise_profiles = [_normalize_profile(raw, index) for index, raw in enumerate(_profile_items(profiles))]
    clip_assignments = [
        {
            "clip_id": source["clip_id"],
            "asset_ref": source["asset_ref"],
            "profile_id": denoise_profiles[index % len(denoise_profiles)]["profile_id"],
            "analysis_window": _analysis_window(source),
            "cache_key": source["cache_key"],
        }
        for index, source in enumerate(sources)
    ]
    manifest = {
        "schema_version": 1,
        "effect": "resolve21_advanced_noise_reduction_profile_manifest",
        "rendered_at": datetime.now(timezone.utc).isoformat(),
        "package": {
            "package_id": package,
            "profile_name": _safe_id(profile_name),
            "clip_count": len(sources),
            "profile_count": len(denoise_profiles),
        },
        "sources": sources,
        "denoise_profiles": denoise_profiles,
        "clip_assignments": clip_assignments,
        "resolve_controls": {
            "page": "Color",
            "panel": "Motion Effects",
            "temporal_noise_reduction": ["frames", "motion_estimation", "luma", "chroma"],
            "spatial_noise_reduction": ["mode", "radius", "luma", "chroma"],
        },
        "diagnostics": [
            f"{len(sources)} real clips linked to advanced noise-reduction profile metadata",
            f"{len(denoise_profiles)} bounded temporal/spatial profiles emitted",
        ],
        "review_hints": [
            "profiles are metadata manifests; source media is not destructively modified",
            "re-analyze if source cache_key changes",
            "prefer lower spatial strength when source_probe reports small frame dimensions",
        ],
    }
    manifest_path = output_root / "advanced_noise_reduction_profile_manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return str(manifest_path)


def _profile_items(profiles: list[dict[str, Any]] | dict[str, dict[str, Any]] | None) -> list[dict[str, Any]]:
    if profiles is None:
        return DEFAULT_DENOISE_PROFILES
    if isinstance(profiles, dict):
        return [{"id": key, **(value if isinstance(value, dict) else {})} for key, value in profiles.items()]
    return [item for item in profiles if isinstance(item, dict)] or DEFAULT_DENOISE_PROFILES


def _normalize_profile(raw: dict[str, Any], index: int) -> dict[str, Any]:
    temporal_frames = max(0, min(_optional_int(raw.get("temporal_frames"), 2), 5))
    temporal_strength = _strength(raw, "temporal_strength", "temporal_nr_strength", 0.5)
    spatial_strength = _strength(raw, "spatial_strength", "spatial_nr_strength", 0.35)
    motion = _safe_choice(str(raw.get("motion_estimation") or "better"), {"none", "faster", "better"})
    return {
        "profile_id": _safe_id(str(raw.get("id") or raw.get("profile_id") or f"denoise_profile_{index}")),
        "label": str(raw.get("label") or raw.get("name") or f"Advanced NR profile {index + 1}"),
        "temporal": {
            "enabled": bool(raw.get("temporal_enabled", raw.get("temporal_nr_enabled", temporal_frames > 0))),
            "frames": temporal_frames,
            "motion_estimation": motion,
            "strength": temporal_strength,
            "luma_threshold": _strength(raw, "luma_threshold", "temporal_luma", 0.32),
            "chroma_threshold": _strength(raw, "chroma_threshold", "temporal_chroma", 0.38),
        },
        "spatial": {
            "enabled": bool(raw.get("spatial_enabled", raw.get("spatial_nr_enabled", spatial_strength > 0.0))),
            "mode": _safe_choice(str(raw.get("spatial_mode") or "better"), {"faster", "better"}),
            "radius": max(1, min(_optional_int(raw.get("spatial_radius"), 2), 5)),
            "strength": spatial_strength,
            "luma_threshold": _strength(raw, "spatial_luma", "luma_threshold", 0.24),
            "chroma_threshold": _strength(raw, "spatial_chroma", "chroma_threshold", 0.3),
        },
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


def _noise_profile_probe(probe: dict[str, Any]) -> dict[str, Any]:
    width = int(probe.get("width") or 0)
    height = int(probe.get("height") or 0)
    duration = float(probe.get("duration") or 0.0)
    return {
        "source_resolution": f"{width}x{height}",
        "duration_seconds": round(duration, 3),
        "has_audio": bool(probe.get("has_audio")),
        "suggested_profile": "low_light_temporal" if width * height >= 1280 * 720 else "fine_texture_spatial",
        "supports_temporal_sampling": (probe.get("media_kind") or "video") == "video" and duration > 0.0,
    }


def _asset_ref(path: Path, probe: dict[str, Any]) -> str:
    duration = round(float(probe.get("duration") or 0.0), 3)
    return f"{_safe_id(path.stem)}:{path.stat().st_size}:{duration}:{int(probe.get('width') or 0)}x{int(probe.get('height') or 0)}"


def _strength(raw: dict[str, Any], primary: str, alternate: str, default: float) -> float:
    value = raw.get(primary, raw.get(alternate, default))
    try:
        number = float(value)
    except Exception:
        number = default
    return round(max(0.0, min(number, 1.0)), 3)


def _optional_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _safe_choice(value: str, choices: set[str]) -> str:
    safe = _safe_id(value)
    return safe if safe in choices else sorted(choices)[0]


def _safe_id(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", value.strip()).strip("_").lower() or "item"


__all__ = ["DEFAULT_DENOISE_PROFILES", "render_advanced_noise_reduction_profile_manifest"]
