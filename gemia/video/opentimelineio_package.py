"""OpenTimelineIO-inspired timeline package manifests."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from gemia.video.timeline_assets import cache_key_for_path, probe_media

def render_opentimelineio_timeline_package_backend(
    input_paths: list[str],
    output_dir: str,
    *,
    package_id: str = "github_opentimelineio_timeline_package_backend",
    timeline_name: str = "gemia_timeline_package",
    frame_rate: float | None = None,
    markers: list[dict[str, Any]] | None = None,
    track_layout: list[dict[str, Any]] | None = None,
) -> str:
    """Emit an OTIO-style timeline interchange package for real media."""
    if not input_paths:
        raise ValueError("input_paths must contain at least one media path")
    output_root = Path(output_dir).expanduser().resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    package = _safe_id(package_id)
    timeline_id = _safe_id(timeline_name)
    sources = [_source_entry(raw_path, index) for index, raw_path in enumerate(input_paths)]
    fps = _choose_frame_rate(frame_rate, sources)
    tracks = _build_tracks(sources, track_layout, fps)
    timeline_duration = round(max((track["duration_seconds"] for track in tracks), default=0.0), 3)
    normalized_markers = _normalize_markers(markers, timeline_duration, fps)
    manifest = {
        "schema_version": 1,
        "effect": "github_opentimelineio_timeline_package_backend",
        "rendered_at": datetime.now(timezone.utc).isoformat(),
        "package": {
            "package_id": package,
            "timeline_id": timeline_id,
            "clip_count": len(sources),
            "track_count": len(tracks),
            "marker_count": len(normalized_markers),
            "requires_opentimelineio_runtime": False,
        },
        "timeline": {
            "name": timeline_name,
            "frame_rate": fps,
            "duration_seconds": timeline_duration,
            "tracks": tracks,
            "markers": normalized_markers,
        },
        "media_references": [
            {
                "media_ref_id": source["media_ref_id"],
                "source_path": source["source_path"],
                "target_url": source["target_url"],
                "available_range": source["available_range"],
                "metadata": source["metadata"],
            }
            for source in sources
        ],
    }
    relink_map = {
        "schema_version": 1,
        "package_id": package,
        "items": [
            {
                "media_ref_id": source["media_ref_id"],
                "asset_ref": source["metadata"]["asset_ref"],
                "source_path": source["source_path"],
                "cache_key": source["metadata"]["cache_key"],
                "ready": True,
            }
            for source in sources
        ],
    }
    manifest_path = output_root / "opentimelineio_timeline_package_manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (output_root / "timeline.otio.json").write_text(json.dumps(_to_otio_json(manifest), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (output_root / "relink_map.json").write_text(json.dumps(relink_map, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return str(manifest_path)


def import_opentimelineio_timeline_package(package_path: str | Path) -> dict[str, Any]:
    """Read a Gemia OTIO-style manifest or its OTIO JSON sidecar."""
    path = Path(package_path).expanduser().resolve()
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("effect") == "github_opentimelineio_timeline_package_backend":
        return payload
    if payload.get("OTIO_SCHEMA") == "Timeline.1":
        return _from_otio_json(payload)
    raise ValueError(f"Unsupported timeline package JSON: {path}")


def _source_entry(raw_path: str, index: int) -> dict[str, Any]:
    source = Path(raw_path).expanduser().resolve()
    if not source.exists():
        raise FileNotFoundError(f"Input media not found: {source}")
    if not source.is_file():
        raise OSError(f"Input media is not a file: {source}")
    probe = probe_media(str(source))
    duration = round(float(probe.get("duration") or 0.0), 3)
    media_ref_id = f"media_ref_{index:03d}_{_safe_id(source.stem)}"
    return {
        "clip_id": f"clip_{index:03d}_{_safe_id(source.stem)}",
        "media_ref_id": media_ref_id,
        "source_path": str(source),
        "target_url": source.as_uri(),
        "available_range": {"start_seconds": 0.0, "duration_seconds": duration},
        "source_probe": probe,
        "metadata": {
            "asset_ref": _asset_ref(source, probe),
            "cache_key": cache_key_for_path(str(source)),
            "media_kind": probe.get("media_kind") or "video",
            "codec": probe.get("codec") or "",
            "has_audio": bool(probe.get("has_audio")),
        },
    }


def _build_tracks(sources: list[dict[str, Any]], layout: list[dict[str, Any]] | None, fps: float) -> list[dict[str, Any]]:
    if not layout:
        video_indexes = [i for i, source in enumerate(sources) if source["metadata"]["media_kind"] != "audio"]
        audio_indexes = [i for i, source in enumerate(sources) if source["metadata"]["has_audio"] or source["metadata"]["media_kind"] == "audio"]
        layout = [{"name": "V1", "kind": "video", "source_indexes": video_indexes}]
        if audio_indexes:
            layout.append({"name": "A1", "kind": "audio", "source_indexes": audio_indexes})
    tracks = []
    for track_index, raw_track in enumerate(layout):
        kind = _safe_choice(str(raw_track.get("kind") or "video"), {"video", "audio", "data"})
        indexes = raw_track.get("source_indexes")
        if not isinstance(indexes, list) or not indexes:
            indexes = list(range(len(sources)))
        clips = []
        cursor = 0.0
        for clip_index, raw_index in enumerate(indexes):
            try:
                source = sources[int(raw_index) % len(sources)]
            except (TypeError, ValueError):
                source = sources[clip_index % len(sources)]
            duration = round(float(source["available_range"]["duration_seconds"] or 0.0), 3) or round(1.0 / fps, 3)
            clips.append({
                "clip_id": f"{_safe_id(str(raw_track.get('name') or kind))}_{clip_index:03d}_{source['clip_id']}",
                "name": Path(source["source_path"]).stem,
                "media_ref_id": source["media_ref_id"],
                "start_seconds": round(cursor, 3),
                "duration_seconds": duration,
                "source_range": source["available_range"],
            })
            cursor = round(cursor + duration, 3)
        tracks.append({
            "track_id": f"track_{track_index:02d}_{_safe_id(str(raw_track.get('name') or kind))}",
            "name": str(raw_track.get("name") or kind.upper()),
            "kind": kind,
            "clips": clips,
            "duration_seconds": round(cursor, 3),
        })
    return [track for track in tracks if track["clips"]]


def _normalize_markers(markers: list[dict[str, Any]] | None, duration: float, fps: float) -> list[dict[str, Any]]:
    if not markers:
        markers = [{"time": 0.0, "name": "timeline_start", "color": "green"}, {"time": duration, "name": "timeline_end", "color": "red"}]
    normalized = []
    for index, raw in enumerate(markers):
        time_seconds = max(0.0, min(_optional_float(raw.get("time") or raw.get("time_seconds")) or 0.0, max(duration, 0.0)))
        normalized.append({
            "marker_id": f"marker_{index:03d}_{_safe_id(str(raw.get('name') or raw.get('comment') or 'note'))}",
            "time_seconds": round(time_seconds, 3),
            "time_frames": int(round(time_seconds * fps)),
            "color": str(raw.get("color") or ["blue", "green", "yellow", "red", "purple"][index % 5]),
            "comment": str(raw.get("comment") or raw.get("name") or "Timeline marker"),
        })
    return normalized


def _to_otio_json(manifest: dict[str, Any]) -> dict[str, Any]:
    timeline = manifest["timeline"]
    return {
        "OTIO_SCHEMA": "Timeline.1",
        "name": timeline["name"],
        "metadata": {"gemia_package": manifest["package"], "media_references": manifest["media_references"]},
        "tracks": {
            "OTIO_SCHEMA": "Stack.1",
            "children": [
                {
                    "OTIO_SCHEMA": "Track.1",
                    "name": track["name"],
                    "kind": track["kind"],
                    "children": [
                        {
                            "OTIO_SCHEMA": "Clip.2",
                            "name": clip["name"],
                            "media_reference": {"target_url": _media_url(manifest, clip["media_ref_id"])},
                            "source_range": {
                                "start_time": {"value": clip["source_range"]["start_seconds"], "rate": timeline["frame_rate"]},
                                "duration": {"value": clip["duration_seconds"], "rate": timeline["frame_rate"]},
                            },
                            "metadata": {"gemia_clip_id": clip["clip_id"], "media_ref_id": clip["media_ref_id"]},
                        }
                        for clip in track["clips"]
                    ],
                }
                for track in timeline["tracks"]
            ],
        },
        "markers": timeline["markers"],
    }


def _from_otio_json(payload: dict[str, Any]) -> dict[str, Any]:
    metadata = payload.get("metadata") or {}
    return {
        "schema_version": 1,
        "effect": "github_opentimelineio_timeline_package_backend",
        "package": metadata.get("gemia_package") or {},
        "timeline": {"name": payload.get("name") or "otio_import"},
        "media_references": metadata.get("media_references") or [],
    }


def _media_url(manifest: dict[str, Any], media_ref_id: str) -> str:
    for ref in manifest["media_references"]:
        if ref["media_ref_id"] == media_ref_id:
            return ref["target_url"]
    return ""


def _choose_frame_rate(frame_rate: float | None, sources: list[dict[str, Any]]) -> float:
    if frame_rate and frame_rate > 0:
        return round(float(frame_rate), 3)
    for source in sources:
        fps = float(source["source_probe"].get("fps") or 0.0)
        if fps > 0:
            return round(fps, 3)
    return 24.0


def _asset_ref(source: Path, probe: dict[str, Any]) -> str:
    raw = f"{source.name}:{probe.get('duration', 0):.3f}:{probe.get('file_size_bytes', 0)}"
    return f"asset:{_safe_id(source.stem)}:{abs(hash(raw)) % 10_000_000:07d}"


def _safe_id(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", value.strip().lower()).strip("_")
    return slug or "item"


def _safe_choice(value: str, allowed: set[str]) -> str:
    return value if value in allowed else sorted(allowed)[0]


def _optional_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
