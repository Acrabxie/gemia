"""Resolve 21 MainConcept H.265 and MV-HEVC delivery manifests."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from gemia.video.timeline_assets import probe_media


DEFAULT_MAINCONCEPT_PROFILES: list[dict[str, Any]] = [
    {
        "id": "h265_main10_hdr_master",
        "label": "MainConcept H.265 Main10 HDR master",
        "codec": "h265",
        "container": "mp4",
        "profile": "main10",
        "bit_depth": 10,
        "target_bitrate_mbps": 45,
        "gop_seconds": 1.0,
        "audio_codec": "aac",
    },
    {
        "id": "mv_hevc_spatial_delivery",
        "label": "MainConcept MV-HEVC spatial delivery",
        "codec": "mv-hevc",
        "container": "mov",
        "profile": "main10_multiview",
        "bit_depth": 10,
        "target_bitrate_mbps": 80,
        "gop_seconds": 0.5,
        "audio_codec": "aac",
        "view_count": 2,
        "stereo_layout": "left_right_views",
    },
]


def render_mainconcept_h265_mvhevc_delivery_manifest(
    input_paths: list[str],
    output_dir: str,
    *,
    package_id: str = "resolve21_mainconcept_h265_mvhevc_delivery",
    delivery_profiles: list[dict[str, Any]] | None = None,
    target_platforms: list[str] | None = None,
    color_space: str = "rec2020",
    hdr_transfer: str = "pq",
) -> str:
    """Emit MainConcept encode intent metadata for real media sources."""
    if not input_paths:
        raise ValueError("input_paths must contain at least one media path")
    output_root = Path(output_dir).expanduser().resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    package = _safe_id(package_id)
    platforms = _clean_list(target_platforms or ["resolve_delivery", "apple_immersive", "archive_master"])

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
                "clip_id": f"mainconcept_clip_{index:02d}_{_safe_id(source.stem)}",
                "source_path": str(source),
                "asset_ref": _asset_ref(source, probe),
                "source_probe": probe,
                "encode_readiness": _encode_readiness(probe),
            }
        )

    profiles = [_normalize_profile(raw, index) for index, raw in enumerate(delivery_profiles or DEFAULT_MAINCONCEPT_PROFILES)]
    deliverables = []
    for profile in profiles:
        deliverables.append(
            {
                "deliverable_id": f"{package}_{profile['profile_id']}",
                "label": profile["label"],
                "target_platforms": platforms,
                "clip_asset_refs": [source["asset_ref"] for source in sources],
                "mainconcept_settings": {
                    "codec": profile["codec"],
                    "container": profile["container"],
                    "profile": profile["profile"],
                    "bit_depth": profile["bit_depth"],
                    "target_bitrate_mbps": profile["target_bitrate_mbps"],
                    "gop_seconds": profile["gop_seconds"],
                    "audio_codec": profile["audio_codec"],
                    "view_count": profile["view_count"],
                    "stereo_layout": profile["stereo_layout"],
                    "color_space": _safe_id(color_space),
                    "hdr_transfer": _safe_id(hdr_transfer),
                },
                "resolve_controls": {
                    "page": "Deliver",
                    "encoder": "MainConcept",
                    "format": profile["container"].upper(),
                    "codec_menu": "H.265" if profile["codec"] == "h265" else "MV-HEVC",
                    "advanced_settings": ["profile", "bit_depth", "gop_seconds", "target_bitrate_mbps"],
                },
                "validation": _profile_validation(profile, sources),
            }
        )

    manifest = {
        "schema_version": 1,
        "effect": "resolve21_mainconcept_h265_mvhevc_delivery_manifest",
        "rendered_at": datetime.now(timezone.utc).isoformat(),
        "package": {
            "package_id": package,
            "clip_count": len(sources),
            "deliverable_count": len(deliverables),
            "target_platforms": platforms,
        },
        "sources": sources,
        "deliverables": deliverables,
        "diagnostics": [
            f"{len(sources)} real clips linked to MainConcept delivery metadata",
            f"{len(deliverables)} H.265/MV-HEVC encode intents emitted with stable asset refs",
        ],
        "review_hints": [
            "confirm source colorspace before choosing HDR Main10 settings",
            "use MV-HEVC only when stereo or immersive views are available",
            "preserve asset_ref values when replacing proxies with final exports",
        ],
    }
    manifest_path = output_root / "mainconcept_h265_mvhevc_delivery_manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return str(manifest_path)


def _normalize_profile(raw: dict[str, Any], index: int) -> dict[str, Any]:
    codec = str(raw.get("codec") or raw.get("encoder") or ("h265" if index == 0 else "mv-hevc")).lower().replace("_", "-")
    if codec in {"hevc", "h.265", "h-265"}:
        codec = "h265"
    if codec not in {"h265", "mv-hevc"}:
        codec = "h265"
    bit_depth = int(raw.get("bit_depth") or (10 if str(raw.get("profile") or "").lower().endswith("10") else 8))
    bitrate = float(raw.get("target_bitrate_mbps") or raw.get("bitrate_mbps") or (80 if codec == "mv-hevc" else 45))
    gop = float(raw.get("gop_seconds") or raw.get("gop") or 1.0)
    view_count = int(raw.get("view_count") or (2 if codec == "mv-hevc" else 1))
    return {
        "profile_id": _safe_id(str(raw.get("id") or raw.get("profile_id") or f"mainconcept_profile_{index}")),
        "label": str(raw.get("label") or ("MainConcept MV-HEVC" if codec == "mv-hevc" else "MainConcept H.265")),
        "codec": codec,
        "container": _container(codec, str(raw.get("container") or "")),
        "profile": _safe_profile(str(raw.get("profile") or ("main10_multiview" if codec == "mv-hevc" else "main10"))),
        "bit_depth": max(8, min(bit_depth, 12)),
        "target_bitrate_mbps": round(max(1.0, min(bitrate, 300.0)), 3),
        "gop_seconds": round(max(0.1, min(gop, 10.0)), 3),
        "audio_codec": _safe_id(str(raw.get("audio_codec") or "aac")),
        "view_count": max(1, min(view_count, 8)),
        "stereo_layout": _safe_id(str(raw.get("stereo_layout") or ("left_right_views" if codec == "mv-hevc" else "mono"))),
    }


def _container(codec: str, value: str) -> str:
    cleaned = _safe_id(value).replace("_", "")
    if cleaned in {"mp4", "mov", "m4v"}:
        return cleaned
    return "mov" if codec == "mv-hevc" else "mp4"


def _safe_profile(value: str) -> str:
    cleaned = _safe_id(value)
    return cleaned if cleaned in {"main", "main10", "main10_multiview", "main444"} else "main10"


def _profile_validation(profile: dict[str, Any], sources: list[dict[str, Any]]) -> dict[str, Any]:
    has_audio = any(bool(source["source_probe"].get("has_audio")) for source in sources)
    max_width = max(int(source["source_probe"].get("width") or 0) for source in sources)
    return {
        "source_count": len(sources),
        "source_has_audio": has_audio,
        "max_source_width": max_width,
        "h265_ready": profile["codec"] == "h265" and profile["bit_depth"] >= 8,
        "mv_hevc_ready": profile["codec"] == "mv-hevc" and profile["view_count"] >= 2,
        "hdr_ready": profile["bit_depth"] >= 10 and max_width >= 1920,
        "audio_passthrough_or_aac": profile["audio_codec"] in {"aac", "pcm", "alac"},
    }


def _encode_readiness(probe: dict[str, Any]) -> dict[str, Any]:
    width = int(probe.get("width") or 0)
    height = int(probe.get("height") or 0)
    fps = float(probe.get("fps") or 0.0)
    return {
        "duration_seconds": round(float(probe.get("duration") or 0.0), 3),
        "source_resolution": f"{width}x{height}",
        "frame_rate": round(fps, 3),
        "has_video": width > 0 and height > 0,
        "has_audio": bool(probe.get("has_audio")),
        "proxy_upscale_expected_for_uhd": width < 3840 or height < 2160,
    }


def _asset_ref(path: Path, probe: dict[str, Any]) -> str:
    duration = round(float(probe.get("duration") or 0.0), 3)
    return f"{_safe_id(path.stem)}:{path.stat().st_size}:{duration}:{int(probe.get('width') or 0)}x{int(probe.get('height') or 0)}"


def _clean_list(values: list[str]) -> list[str]:
    cleaned = [_safe_id(str(value)) for value in values if str(value).strip()]
    return sorted(set(cleaned)) or ["resolve_delivery"]


def _safe_id(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", value.strip()).strip("_").lower() or "item"


__all__ = ["DEFAULT_MAINCONCEPT_PROFILES", "render_mainconcept_h265_mvhevc_delivery_manifest"]
