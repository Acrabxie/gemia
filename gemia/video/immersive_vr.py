"""Resolve 21 immersive and VR delivery manifests."""
from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_DELIVERABLES: list[dict[str, Any]] = [
    {
        "id": "mono_review",
        "label": "Mono review",
        "layout": "mono_equirectangular",
        "projection": "equirectangular",
        "stereo_mode": "mono",
        "eye_order": "none",
    },
    {
        "id": "stereo_over_under",
        "label": "Stereo over-under",
        "layout": "stereo_over_under",
        "projection": "equirectangular",
        "stereo_mode": "stereo",
        "eye_order": "left_right",
    },
]


def render_immersive_vr_delivery_manifest(
    input_paths: list[str],
    output_dir: str,
    *,
    package_id: str = "resolve21_immersive_vr_delivery",
    deliverables: list[dict[str, Any]] | None = None,
    target_platforms: list[str] | None = None,
) -> str:
    """Emit immersive/VR delivery metadata for real clips without transcoding."""
    if not input_paths:
        raise ValueError("input_paths must contain at least one media path")
    output_root = Path(output_dir).expanduser().resolve()
    output_root.mkdir(parents=True, exist_ok=True)

    sources = []
    for index, raw_path in enumerate(input_paths):
        source = Path(raw_path).expanduser().resolve()
        if not source.exists():
            raise FileNotFoundError(f"Input media not found: {source}")
        probe = _probe_media(source)
        sources.append(
            {
                "clip_id": f"vr_clip_{index:02d}_{_safe_id(source.stem)}",
                "source_path": str(source),
                "source_probe": probe,
                "asset_ref": _asset_ref(source, probe),
                "vr_readiness": _readiness(probe),
            }
        )

    normalized = [_normalize_deliverable(raw, index) for index, raw in enumerate(deliverables or DEFAULT_DELIVERABLES)]
    platforms = _clean_platforms(target_platforms or ["headset_review", "web360", "archive_master"])
    package_deliverables = []
    for item in normalized:
        package_deliverables.append(
            {
                "deliverable_id": item["id"],
                "label": item["label"],
                "layout": item["layout"],
                "projection": item["projection"],
                "stereo_mode": item["stereo_mode"],
                "eye_order": item["eye_order"],
                "target_platforms": platforms,
                "clip_asset_refs": [source["asset_ref"] for source in sources],
                "metadata": {
                    "spatial_audio": bool(item["spatial_audio"]),
                    "field_of_view_degrees": item["field_of_view_degrees"],
                    "recommended_review_player": "Resolve immersive viewer",
                    "requires_rewrap": bool(item["requires_rewrap"]),
                },
                "review_key": f"{package_id}:{item['id']}:{len(sources)}clips",
            }
        )

    manifest = {
        "schema_version": 1,
        "effect": "resolve21_immersive_vr_delivery_manifest",
        "rendered_at": datetime.now(timezone.utc).isoformat(),
        "package": {
            "package_id": package_id,
            "clip_count": len(sources),
            "deliverable_count": len(package_deliverables),
            "target_platforms": platforms,
        },
        "sources": sources,
        "deliverables": package_deliverables,
        "diagnostics": [
            f"{len(sources)} real clips prepared for immersive delivery metadata",
            f"{len(package_deliverables)} delivery layouts emitted with stable clip refs",
        ],
        "review_hints": [
            "confirm mono versus stereo layout before final export",
            "check field_of_view_degrees and projection metadata in the target review player",
            "use clip asset_ref values to relink final transcodes to source clips",
        ],
    }
    manifest_path = output_root / "immersive_vr_delivery_manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return str(manifest_path)


def _normalize_deliverable(raw: dict[str, Any], index: int) -> dict[str, Any]:
    layout = str(raw.get("layout") or ("mono_equirectangular" if index == 0 else "stereo_over_under"))
    stereo = str(raw.get("stereo_mode") or ("mono" if "mono" in layout else "stereo"))
    return {
        "id": _safe_id(str(raw.get("id") or layout or f"vr_delivery_{index}")),
        "label": str(raw.get("label") or layout.replace("_", " ").title()),
        "layout": layout,
        "projection": str(raw.get("projection") or "equirectangular"),
        "stereo_mode": stereo,
        "eye_order": str(raw.get("eye_order") or ("none" if stereo == "mono" else "left_right")),
        "spatial_audio": bool(raw.get("spatial_audio", stereo != "mono")),
        "field_of_view_degrees": int(raw.get("field_of_view_degrees") or 360),
        "requires_rewrap": bool(raw.get("requires_rewrap", False)),
    }


def _probe_media(path: Path) -> dict[str, Any]:
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"ffprobe failed for {path}: {proc.stderr[-800:]}")
    payload = json.loads(proc.stdout or "{}")
    fmt = payload.get("format") or {}
    video = next((stream for stream in payload.get("streams", []) if stream.get("codec_type") == "video"), {})
    audio = next((stream for stream in payload.get("streams", []) if stream.get("codec_type") == "audio"), None)
    return {
        "duration_seconds": round(float(fmt.get("duration") or 0.0), 3),
        "size_bytes": int(fmt.get("size") or path.stat().st_size),
        "width": int(video.get("width") or 0),
        "height": int(video.get("height") or 0),
        "video_codec": str(video.get("codec_name") or ""),
        "has_audio": audio is not None,
        "audio_codec": str((audio or {}).get("codec_name") or ""),
    }


def _readiness(probe: dict[str, Any]) -> dict[str, Any]:
    width = int(probe["width"])
    height = int(probe["height"])
    ratio = round(width / max(height, 1), 3)
    return {
        "aspect_ratio": ratio,
        "looks_equirectangular": 1.8 <= ratio <= 2.1,
        "minimum_headset_width_ok": width >= 1920,
        "duration_seconds": probe["duration_seconds"],
    }


def _asset_ref(path: Path, probe: dict[str, Any]) -> str:
    return f"{_safe_id(path.stem)}:{path.stat().st_size}:{probe['duration_seconds']}:{probe['width']}x{probe['height']}"


def _clean_platforms(platforms: list[str]) -> list[str]:
    cleaned = [_safe_id(str(platform)) for platform in platforms if str(platform).strip()]
    return sorted(set(cleaned)) or ["headset_review"]


def _safe_id(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", value.strip()).strip("_").lower() or "item"


__all__ = ["DEFAULT_DELIVERABLES", "render_immersive_vr_delivery_manifest"]
