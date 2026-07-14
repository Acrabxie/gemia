"""Resolve 21 Magic Mask render-in-place cache manifests."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from gemia.video.timeline_assets import cache_key_for_path, probe_media


DEFAULT_MAGIC_MASK_TRACKS: list[dict[str, Any]] = [
    {
        "id": "hero_subject",
        "label": "Hero subject",
        "target_type": "person",
        "tracking_mode": "better",
        "include_alpha": True,
        "handles_frames": 12,
    },
    {
        "id": "background_holdout",
        "label": "Background holdout",
        "target_type": "background",
        "tracking_mode": "faster",
        "include_alpha": True,
        "handles_frames": 6,
    },
]


def render_magic_mask_render_in_place_cache_manifest(
    input_paths: list[str],
    output_dir: str,
    *,
    package_id: str = "resolve21_magic_mask_render_in_place_cache",
    mask_tracks: list[dict[str, Any]] | None = None,
    cache_codec: str = "prores_4444",
    cache_resolution: str = "source",
    track_quality: str = "better",
) -> str:
    """Emit deterministic Magic Mask tracking and render-in-place cache metadata."""
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
            raise ValueError(f"Magic Mask requires a video/image source: {source}")
        sources.append(
            {
                "clip_id": f"magic_mask_clip_{index:02d}_{_safe_id(source.stem)}",
                "source_path": str(source),
                "asset_ref": _asset_ref(source, probe),
                "cache_key": cache_key_for_path(str(source)),
                "source_probe": probe,
                "mask_readiness": _mask_readiness(probe),
            }
        )

    tracks = [_normalize_track(raw, index, track_quality) for index, raw in enumerate(mask_tracks or DEFAULT_MAGIC_MASK_TRACKS)]
    cache_entries = []
    for track in tracks:
        cache_entries.append(
            {
                "cache_id": f"{package}_{track['track_id']}_rip_cache",
                "mask_track_id": track["track_id"],
                "label": track["label"],
                "clip_asset_refs": [source["asset_ref"] for source in sources],
                "tracking_windows": [_tracking_window(source, track) for source in sources],
                "render_in_place": {
                    "cache_codec": _safe_codec(cache_codec),
                    "cache_resolution": _safe_resolution(cache_resolution),
                    "alpha_mode": "straight_alpha" if track["include_alpha"] else "matte_only",
                    "handles_frames": track["handles_frames"],
                    "estimated_playback_gain": _playback_gain(track, sources),
                    "cache_path_hint": f"CacheClip/{package}/{track['track_id']}",
                },
                "invalidation_keys": [
                    "source_path",
                    "source_size",
                    "source_mtime",
                    "track_quality",
                    "target_type",
                    "mask_refinement",
                ],
                "validation": _entry_validation(track, sources),
            }
        )

    manifest = {
        "schema_version": 1,
        "effect": "resolve21_magic_mask_render_in_place_cache_manifest",
        "rendered_at": datetime.now(timezone.utc).isoformat(),
        "package": {
            "package_id": package,
            "clip_count": len(sources),
            "mask_track_count": len(tracks),
            "cache_entry_count": len(cache_entries),
        },
        "sources": sources,
        "mask_tracks": tracks,
        "cache_entries": cache_entries,
        "resolve_controls": {
            "page": "Color",
            "panel": "Magic Mask",
            "operation": "Render In Place",
            "cache_scope": "tracked_mask_nodes",
        },
        "diagnostics": [
            f"{len(sources)} real clips linked to Magic Mask cache metadata",
            f"{len(cache_entries)} render-in-place cache entries emitted with stable asset refs",
        ],
        "review_hints": [
            "retrack if source cache_key changes",
            "prefer prores_4444 or dnxhr_444 when alpha is required",
            "use handles_frames to protect edit trims around tracked mask caches",
        ],
    }
    manifest_path = output_root / "magic_mask_render_in_place_cache_manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return str(manifest_path)


def _normalize_track(raw: dict[str, Any], index: int, default_quality: str) -> dict[str, Any]:
    tracking = _safe_choice(str(raw.get("tracking_mode") or raw.get("quality") or default_quality), {"faster", "better"})
    target_type = _safe_choice(str(raw.get("target_type") or raw.get("target") or "person"), {"person", "object", "background", "custom"})
    refinement = raw.get("refinement") or {}
    if not isinstance(refinement, dict):
        refinement = {}
    return {
        "track_id": _safe_id(str(raw.get("id") or raw.get("track_id") or f"mask_track_{index}")),
        "label": str(raw.get("label") or raw.get("name") or f"Magic Mask track {index + 1}"),
        "target_type": target_type,
        "tracking_mode": tracking,
        "include_alpha": bool(raw.get("include_alpha", True)),
        "handles_frames": max(0, min(int(raw.get("handles_frames") or 8), 120)),
        "refinement": {
            "denoise": round(_clamp_float(refinement.get("denoise", 0.25), 0.0, 1.0), 3),
            "clean_black": round(_clamp_float(refinement.get("clean_black", 0.1), 0.0, 1.0), 3),
            "clean_white": round(_clamp_float(refinement.get("clean_white", 0.1), 0.0, 1.0), 3),
            "blur_radius": round(_clamp_float(refinement.get("blur_radius", 1.0), 0.0, 24.0), 3),
        },
    }


def _tracking_window(source: dict[str, Any], track: dict[str, Any]) -> dict[str, Any]:
    probe = source["source_probe"]
    duration = round(float(probe.get("duration") or 0.0), 3)
    fps = max(float(probe.get("fps") or 24.0), 1.0)
    frames = max(1, int(round(duration * fps))) if duration else 1
    return {
        "clip_id": source["clip_id"],
        "asset_ref": source["asset_ref"],
        "start_seconds": 0.0,
        "end_seconds": duration,
        "estimated_frames": frames,
        "keyframe_stride": 4 if track["tracking_mode"] == "better" else 8,
        "cache_key": source["cache_key"],
    }


def _entry_validation(track: dict[str, Any], sources: list[dict[str, Any]]) -> dict[str, Any]:
    durations = [float(source["source_probe"].get("duration") or 0.0) for source in sources]
    widths = [int(source["source_probe"].get("width") or 0) for source in sources]
    return {
        "source_count": len(sources),
        "track_has_alpha": bool(track["include_alpha"]),
        "all_sources_have_pixels": all(width > 0 for width in widths),
        "max_source_width": max(widths) if widths else 0,
        "total_tracked_seconds": round(sum(durations), 3),
        "render_in_place_ready": bool(sources) and all(width > 0 for width in widths),
    }


def _mask_readiness(probe: dict[str, Any]) -> dict[str, Any]:
    width = int(probe.get("width") or 0)
    height = int(probe.get("height") or 0)
    fps = float(probe.get("fps") or 0.0)
    return {
        "duration_seconds": round(float(probe.get("duration") or 0.0), 3),
        "source_resolution": f"{width}x{height}",
        "frame_rate": round(fps, 3),
        "pixel_count": width * height,
        "supports_temporal_tracking": probe.get("media_kind") == "video" and fps > 0.0,
    }


def _playback_gain(track: dict[str, Any], sources: list[dict[str, Any]]) -> float:
    base = 2.0 if track["tracking_mode"] == "better" else 1.45
    total_pixels = sum(int(source["source_probe"].get("width") or 0) * int(source["source_probe"].get("height") or 0) for source in sources)
    if total_pixels > 3840 * 2160:
        base += 0.55
    return round(base, 2)


def _asset_ref(path: Path, probe: dict[str, Any]) -> str:
    duration = round(float(probe.get("duration") or 0.0), 3)
    return f"{_safe_id(path.stem)}:{path.stat().st_size}:{duration}:{int(probe.get('width') or 0)}x{int(probe.get('height') or 0)}"


def _safe_codec(value: str) -> str:
    codec = _safe_id(value)
    return codec if codec in {"prores_4444", "dnxhr_444", "h264_proxy", "png_sequence"} else "prores_4444"


def _safe_resolution(value: str) -> str:
    resolution = _safe_id(value)
    return resolution if resolution in {"source", "half", "quarter", "uhd", "hd"} else "source"


def _safe_choice(value: str, allowed: set[str]) -> str:
    cleaned = _safe_id(value)
    return cleaned if cleaned in allowed else sorted(allowed)[0]


def _clamp_float(value: Any, low: float, high: float) -> float:
    try:
        number = float(value)
    except Exception:
        number = low
    return max(low, min(number, high))


def _safe_id(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", value.strip()).strip("_").lower() or "item"


__all__ = ["DEFAULT_MAGIC_MASK_TRACKS", "render_magic_mask_render_in_place_cache_manifest"]
