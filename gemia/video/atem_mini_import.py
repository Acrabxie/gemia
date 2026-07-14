"""Resolve 21 ATEM Mini ISO project import timeline manifests."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from gemia.video.timeline_assets import probe_media


DEFAULT_CAMERA_LABELS = ["cam_a", "cam_b", "cam_c", "cam_d"]


def render_atem_mini_project_import_timeline_manifest(
    input_paths: list[str],
    output_dir: str,
    *,
    package_id: str = "resolve21_atem_mini_project_import",
    project_name: str = "atem_iso_project",
    camera_labels: list[str] | None = None,
    switcher_cuts: list[dict[str, Any]] | None = None,
    relink_policy: str = "filename_and_probe",
    audio_source: str = "program",
) -> str:
    """Emit editable ATEM Mini ISO project ingest metadata for real media."""
    if not input_paths:
        raise ValueError("input_paths must contain at least one media path")
    output_root = Path(output_dir).expanduser().resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    package = _safe_id(package_id)
    project = _safe_id(project_name)
    labels = [_safe_id(label) for label in (camera_labels or DEFAULT_CAMERA_LABELS)]
    if not labels:
        labels = DEFAULT_CAMERA_LABELS[:]
    relink = _safe_choice(str(relink_policy or "filename_and_probe"), {"filename_and_probe", "asset_ref", "manual_review"})
    audio = _safe_choice(str(audio_source or "program"), {"program", "camera_a", "camera_audio"})

    iso_sources = []
    for index, raw_path in enumerate(input_paths):
        source = Path(raw_path).expanduser().resolve()
        if not source.exists():
            raise FileNotFoundError(f"Input media not found: {source}")
        if not source.is_file():
            raise OSError(f"Input media is not a file: {source}")
        probe = probe_media(str(source))
        if int(probe.get("width") or 0) <= 0 or int(probe.get("height") or 0) <= 0:
            raise ValueError(f"ATEM ISO ingest requires video/image media: {source}")
        label = labels[index % len(labels)]
        iso_sources.append(
            {
                "iso_source_id": f"atem_iso_{index:02d}_{label}",
                "camera_label": label,
                "source_path": str(source),
                "asset_ref": _asset_ref(source, probe),
                "source_probe": probe,
                "relink_key": _relink_key(source, probe, relink),
                "angle_metadata": _angle_metadata(index, label, probe),
            }
        )

    normalized_cuts = _normalize_switcher_cuts(switcher_cuts, iso_sources)
    program_edits = [_program_edit(cut, iso_sources, idx) for idx, cut in enumerate(normalized_cuts)]
    timeline_duration = round(max((edit["end_seconds"] for edit in program_edits), default=0.0), 3)
    manifest = {
        "schema_version": 1,
        "effect": "resolve21_atem_mini_project_import_timeline_manifest",
        "rendered_at": datetime.now(timezone.utc).isoformat(),
        "package": {
            "package_id": package,
            "project_name": project,
            "iso_source_count": len(iso_sources),
            "program_edit_count": len(program_edits),
            "relink_policy": relink,
            "audio_source": audio,
        },
        "iso_sources": iso_sources,
        "timelines": {
            "multicam_timeline": {
                "timeline_id": f"{project}_multicam",
                "angle_count": len(iso_sources),
                "angles": [_angle_track(source, index) for index, source in enumerate(iso_sources)],
                "sync_basis": "atem_iso_timecode_or_start_of_recording",
                "editable_after_import": True,
            },
            "program_timeline": {
                "timeline_id": f"{project}_program_cut",
                "duration_seconds": timeline_duration,
                "edits": program_edits,
                "audio_track_source": audio,
                "editable_after_import": True,
            },
        },
        "relink_manifest": {
            "policy": relink,
            "items": [
                {
                    "iso_source_id": source["iso_source_id"],
                    "asset_ref": source["asset_ref"],
                    "relink_key": source["relink_key"],
                    "target_bin": f"ATEM ISO/{source['camera_label']}",
                    "ready": bool(source["asset_ref"]),
                }
                for source in iso_sources
            ],
        },
        "resolve_controls": {
            "page": "Edit",
            "workflow": "Import ATEM Mini ISO project as editable multicam timeline",
            "bins": ["ATEM ISO", "Program Cut", "Relink Review"],
            "requires_external_switcher_project": False,
        },
        "review_hints": [
            "manifest models the ATEM ISO import contract without requiring proprietary project files",
            "all timeline edits point back to stable real-media asset refs",
            "relink_manifest is metadata only and does not move source media",
        ],
    }
    manifest_path = output_root / "atem_mini_project_import_timeline_manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return str(manifest_path)


def _normalize_switcher_cuts(cuts: list[dict[str, Any]] | None, sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not sources:
        return []
    if not cuts:
        duration = max(float(source["source_probe"].get("duration") or 0.0) for source in sources)
        span = max(round(duration / max(len(sources), 1), 3), 0.5)
        return [
            {"camera": source["camera_label"], "start": round(index * span, 3), "end": round((index + 1) * span, 3)}
            for index, source in enumerate(sources)
        ]
    normalized = []
    for index, raw in enumerate(cuts):
        camera = _safe_id(str(raw.get("camera") or raw.get("angle") or sources[index % len(sources)]["camera_label"]))
        start = max(_optional_float(raw.get("start")) or 0.0, 0.0)
        end = max(_optional_float(raw.get("end")) or (start + 1.0), start + 0.05)
        normalized.append({"camera": camera, "start": round(start, 3), "end": round(end, 3)})
    return normalized


def _program_edit(cut: dict[str, Any], sources: list[dict[str, Any]], index: int) -> dict[str, Any]:
    source = next((item for item in sources if item["camera_label"] == cut["camera"]), sources[index % len(sources)])
    return {
        "edit_id": f"program_edit_{index:03d}",
        "iso_source_id": source["iso_source_id"],
        "camera_label": source["camera_label"],
        "asset_ref": source["asset_ref"],
        "start_seconds": cut["start"],
        "end_seconds": cut["end"],
        "track": "V1",
        "transition": "cut",
    }


def _angle_track(source: dict[str, Any], index: int) -> dict[str, Any]:
    return {
        "angle_index": index + 1,
        "track": f"V{index + 1}",
        "camera_label": source["camera_label"],
        "iso_source_id": source["iso_source_id"],
        "asset_ref": source["asset_ref"],
    }


def _angle_metadata(index: int, label: str, probe: dict[str, Any]) -> dict[str, Any]:
    return {
        "angle_index": index + 1,
        "display_name": label.replace("_", " ").title(),
        "resolution": f"{int(probe.get('width') or 0)}x{int(probe.get('height') or 0)}",
        "has_camera_audio": bool(probe.get("has_audio")),
        "timecode_policy": "source_start_or_zero",
    }


def _asset_ref(path: Path, probe: dict[str, Any]) -> str:
    duration = round(float(probe.get("duration") or 0.0), 3)
    return f"{_safe_id(path.stem)}:{path.stat().st_size}:{duration}:{int(probe.get('width') or 0)}x{int(probe.get('height') or 0)}"


def _relink_key(path: Path, probe: dict[str, Any], policy: str) -> str:
    if policy == "asset_ref":
        return _asset_ref(path, probe)
    if policy == "manual_review":
        return f"manual:{_safe_id(path.name)}"
    return f"{_safe_id(path.name)}:{path.stat().st_size}:{int(probe.get('width') or 0)}x{int(probe.get('height') or 0)}"


def _optional_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except Exception:
        return None


def _safe_choice(value: str, choices: set[str]) -> str:
    key = _safe_id(value)
    return key if key in choices else sorted(choices)[0]


def _safe_id(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", value.strip()).strip("_").lower() or "item"


__all__ = ["DEFAULT_CAMERA_LABELS", "render_atem_mini_project_import_timeline_manifest"]
