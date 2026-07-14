"""Resolve 21 Apple Immersive foveated rendering manifests."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from gemia.video.timeline_assets import probe_media


DEFAULT_RENDER_PROFILES: list[dict[str, Any]] = [
    {
        "id": "vision_pro_preview",
        "label": "Apple Vision Pro preview",
        "mode": "preview",
        "eye_buffer": "4320x4320",
        "foveation": "balanced",
        "periphery_scale": 0.55,
    },
    {
        "id": "apple_immersive_master",
        "label": "Apple Immersive master",
        "mode": "export",
        "eye_buffer": "8192x8192",
        "foveation": "quality",
        "periphery_scale": 0.72,
    },
]


def render_apple_immersive_foveated_rendering_manifest(
    input_paths: list[str],
    output_dir: str,
    *,
    package_id: str = "resolve21_apple_immersive_foveated_rendering",
    render_profiles: list[dict[str, Any]] | None = None,
    device_profile: str = "apple_vision_pro",
    field_of_view_degrees: int = 180,
) -> str:
    """Emit Apple Immersive foveated render metadata linked to real media."""
    if not input_paths:
        raise ValueError("input_paths must contain at least one media path")
    output_root = Path(output_dir).expanduser().resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    package = _safe_id(package_id)
    profiles = [_normalize_profile(raw, index) for index, raw in enumerate(render_profiles or DEFAULT_RENDER_PROFILES)]
    fov = max(90, min(int(field_of_view_degrees), 220))

    clips = []
    for index, raw_path in enumerate(input_paths):
        source = Path(raw_path).expanduser().resolve()
        if not source.exists():
            raise FileNotFoundError(f"Input media not found: {source}")
        if not source.is_file():
            raise OSError(f"Input media is not a file: {source}")
        probe = probe_media(str(source))
        clips.append(
            {
                "clip_id": f"immersive_clip_{index:02d}_{_safe_id(source.stem)}",
                "source_path": str(source),
                "asset_ref": _asset_ref(source, probe),
                "source_probe": probe,
                "immersive_readiness": _readiness(probe, fov),
            }
        )

    passes = []
    for profile in profiles:
        passes.append(
            {
                "pass_id": f"{package}_{profile['profile_id']}",
                "profile": profile,
                "device_profile": _safe_id(device_profile),
                "clip_asset_refs": [clip["asset_ref"] for clip in clips],
                "foveation_map": _foveation_map(profile, fov),
                "resolve_controls": {
                    "page": "Deliver",
                    "workflow": "Apple Immersive",
                    "preview_control": profile["mode"] == "preview",
                    "export_control": profile["mode"] == "export",
                    "stereo_layout": "left_right_eye_buffers",
                    "dynamic_foveation": True,
                },
                "validation": _pass_validation(profile, clips),
            }
        )

    manifest = {
        "schema_version": 1,
        "effect": "resolve21_apple_immersive_foveated_rendering_manifest",
        "rendered_at": datetime.now(timezone.utc).isoformat(),
        "package": {
            "package_id": package,
            "clip_count": len(clips),
            "render_pass_count": len(passes),
            "device_profile": _safe_id(device_profile),
            "field_of_view_degrees": fov,
        },
        "sources": clips,
        "render_passes": passes,
        "diagnostics": [
            f"{len(clips)} real clips linked to Apple Immersive foveated render metadata",
            f"{len(passes)} preview/export render profiles emitted with stable asset refs",
        ],
        "review_hints": [
            "verify the center focus window matches the intended subject framing",
            "check periphery_scale before final Apple Immersive export",
            "preserve asset_ref values when replacing proxies with final stereoscopic masters",
        ],
    }
    manifest_path = output_root / "apple_immersive_foveated_rendering_manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return str(manifest_path)


def _normalize_profile(raw: dict[str, Any], index: int) -> dict[str, Any]:
    mode = str(raw.get("mode") or ("preview" if index == 0 else "export")).lower()
    if mode not in {"preview", "export"}:
        mode = "preview"
    foveation = str(raw.get("foveation") or raw.get("foveation_strength") or "balanced").lower()
    if foveation not in {"light", "balanced", "quality"}:
        foveation = "balanced"
    width, height = _parse_buffer(str(raw.get("eye_buffer") or raw.get("eye_buffer_resolution") or "4320x4320"))
    scale = float(raw.get("periphery_scale") or (0.72 if foveation == "quality" else 0.55))
    return {
        "profile_id": _safe_id(str(raw.get("id") or raw.get("profile_id") or f"foveated_profile_{index}")),
        "label": str(raw.get("label") or f"Foveated {mode.title()}"),
        "mode": mode,
        "foveation": foveation,
        "eye_buffer_width": width,
        "eye_buffer_height": height,
        "periphery_scale": round(max(0.25, min(scale, 1.0)), 3),
    }


def _parse_buffer(value: str) -> tuple[int, int]:
    match = re.search(r"(\d{3,5})\s*[xX]\s*(\d{3,5})", value)
    if not match:
        return (4320, 4320)
    width = max(1024, min(int(match.group(1)), 16384))
    height = max(1024, min(int(match.group(2)), 16384))
    return width, height


def _foveation_map(profile: dict[str, Any], fov: int) -> dict[str, Any]:
    center_ratio = {"light": 0.42, "balanced": 0.34, "quality": 0.48}[profile["foveation"]]
    return {
        "center_region": {
            "x": 0.5,
            "y": 0.5,
            "radius": round(center_ratio, 3),
            "quality_scale": 1.0,
        },
        "mid_region": {
            "radius": round(min(0.92, center_ratio + 0.28), 3),
            "quality_scale": round(max(profile["periphery_scale"], 0.68), 3),
        },
        "periphery_region": {
            "field_of_view_degrees": fov,
            "quality_scale": profile["periphery_scale"],
        },
    }


def _pass_validation(profile: dict[str, Any], clips: list[dict[str, Any]]) -> dict[str, Any]:
    min_width = min(int(clip["source_probe"].get("width") or 0) for clip in clips)
    min_height = min(int(clip["source_probe"].get("height") or 0) for clip in clips)
    return {
        "source_count": len(clips),
        "has_audio_track": any(bool(clip["source_probe"].get("has_audio")) for clip in clips),
        "source_resolution": f"{min_width}x{min_height}",
        "eye_buffer_resolution": f"{profile['eye_buffer_width']}x{profile['eye_buffer_height']}",
        "proxy_upscale_expected": min_width < profile["eye_buffer_width"] or min_height < profile["eye_buffer_height"],
        "export_ready": profile["mode"] == "export" and profile["eye_buffer_width"] >= 4096,
    }


def _readiness(probe: dict[str, Any], fov: int) -> dict[str, Any]:
    width = int(probe.get("width") or 0)
    height = int(probe.get("height") or 0)
    return {
        "duration_seconds": round(float(probe.get("duration") or 0.0), 3),
        "aspect_ratio": round(width / max(height, 1), 3),
        "supports_preview": width > 0 and height > 0,
        "native_immersive_candidate": width >= 3840 and fov >= 180,
    }


def _asset_ref(path: Path, probe: dict[str, Any]) -> str:
    duration = round(float(probe.get("duration") or 0.0), 3)
    return f"{_safe_id(path.stem)}:{path.stat().st_size}:{duration}:{int(probe.get('width') or 0)}x{int(probe.get('height') or 0)}"


def _safe_id(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", value.strip()).strip("_").lower() or "item"


__all__ = ["DEFAULT_RENDER_PROFILES", "render_apple_immersive_foveated_rendering_manifest"]
