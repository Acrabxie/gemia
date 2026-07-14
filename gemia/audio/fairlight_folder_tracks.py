"""Fairlight-style folder track manifests for Gemia audio timelines."""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


DEFAULT_FOLDER_RULES = {
    "dialogue": ("dialog", "voice", "speech", "vocal", "interview", "host"),
    "music": ("music", "score", "song", "bed", "theme"),
    "sfx": ("sfx", "effect", "fx", "hit", "whoosh", "impact"),
    "ambience": ("ambience", "ambient", "room", "tone", "field", "crowd"),
}


@dataclass(frozen=True)
class FairlightFolderTracksResult:
    manifest_path: str
    track_count: int
    folder_count: int


def render_fairlight_folder_tracks_manifest(
    track_assets: list[str | dict[str, Any]] | tuple[str | dict[str, Any], ...],
    output_path: str,
    *,
    folder_rules: dict[str, list[str] | tuple[str, ...]] | None = None,
    timeline_id: str | None = None,
) -> str:
    """Group audio-capable media into Fairlight-style folder track metadata."""
    if not track_assets:
        raise ValueError("track_assets cannot be empty")
    rules = _normalize_rules(folder_rules)
    records: list[dict[str, Any]] = []
    for index, item in enumerate(track_assets):
        record = _asset_record(item, index=index, rules=rules)
        records.append(record)
    folders = _folder_records(records)
    payload = {
        "schema_version": 1,
        "effect": "resolve21_fairlight_folder_tracks",
        "rendered_at": datetime.now(timezone.utc).isoformat(),
        "timeline_id": timeline_id or "fairlight_folder_tracks",
        "track_count": len(records),
        "folder_count": len(folders),
        "folders": folders,
        "tracks": records,
        "diagnostics": _diagnostics(records),
        "review_hints": [
            "confirm dialogue, music, effects, and ambience tracks are collapsible by folder",
            "check unreadable or no-audio tracks before render approval",
            "use folder duration totals to catch dropped audio assets",
        ],
    }
    output = Path(output_path).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return str(output)


def _asset_record(item: str | dict[str, Any], *, index: int, rules: dict[str, tuple[str, ...]]) -> dict[str, Any]:
    if isinstance(item, dict):
        raw_path = item.get("path") or item.get("source") or item.get("input_path")
        label = str(item.get("label") or item.get("name") or Path(str(raw_path or "")).stem)
        role = str(item.get("role") or item.get("type") or "").strip().lower()
    else:
        raw_path = item
        label = Path(str(item)).stem
        role = ""
    if not raw_path:
        raise ValueError(f"track_assets[{index}] is missing a path")
    path = Path(str(raw_path)).expanduser().resolve()
    probe = _probe_media(path)
    folder = _folder_for(label=label, role=role, path=path, rules=rules)
    return {
        "index": index,
        "path": str(path),
        "label": label,
        "role": role or folder,
        "folder": folder,
        "exists": path.exists(),
        "readable": probe["readable"],
        "has_audio": probe["has_audio"],
        "duration_seconds": probe["duration_seconds"],
        "audio_streams": probe["audio_streams"],
        "sample_rates": probe["sample_rates"],
        "channels": probe["channels"],
    }


def _folder_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    folders: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        folders.setdefault(record["folder"], []).append(record)
    result = []
    for folder, items in sorted(folders.items()):
        duration = sum(float(item.get("duration_seconds") or 0.0) for item in items if item.get("has_audio"))
        result.append({
            "id": folder,
            "label": folder.replace("_", " ").title(),
            "track_count": len(items),
            "audio_track_count": sum(1 for item in items if item.get("has_audio")),
            "total_duration_seconds": round(duration, 3),
            "tracks": [item["index"] for item in items],
        })
    return result


def _folder_for(*, label: str, role: str, path: Path, rules: dict[str, tuple[str, ...]]) -> str:
    haystack = " ".join([label, role, path.stem]).lower().replace("-", " ").replace("_", " ")
    for folder, tokens in rules.items():
        if role == folder or any(token and token in haystack for token in tokens):
            return folder
    return "misc"


def _normalize_rules(rules: dict[str, Iterable[str]] | None) -> dict[str, tuple[str, ...]]:
    source = rules or DEFAULT_FOLDER_RULES
    return {str(key).strip().lower(): tuple(str(token).strip().lower() for token in value) for key, value in source.items()}


def _probe_media(path: Path) -> dict[str, Any]:
    if not path.exists() or not path.is_file():
        return {"readable": False, "has_audio": False, "duration_seconds": 0.0, "audio_streams": 0, "sample_rates": [], "channels": []}
    proc = subprocess.run(
        [
            "ffprobe", "-v", "error", "-print_format", "json",
            "-show_entries", "format=duration:stream=codec_type,sample_rate,channels",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return {"readable": False, "has_audio": False, "duration_seconds": 0.0, "audio_streams": 0, "sample_rates": [], "channels": []}
    try:
        payload = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        payload = {}
    streams = [stream for stream in payload.get("streams", []) if stream.get("codec_type") == "audio"]
    duration = float(payload.get("format", {}).get("duration") or 0.0)
    return {
        "readable": True,
        "has_audio": bool(streams),
        "duration_seconds": round(max(duration, 0.0), 3),
        "audio_streams": len(streams),
        "sample_rates": sorted({int(stream["sample_rate"]) for stream in streams if str(stream.get("sample_rate", "")).isdigit()}),
        "channels": sorted({int(stream["channels"]) for stream in streams if isinstance(stream.get("channels"), int)}),
    }


def _diagnostics(records: list[dict[str, Any]]) -> list[dict[str, str]]:
    diagnostics = []
    for record in records:
        if not record["exists"]:
            diagnostics.append({"severity": "error", "code": "track_missing", "track": record["label"]})
        elif not record["readable"]:
            diagnostics.append({"severity": "error", "code": "track_unreadable", "track": record["label"]})
        elif not record["has_audio"]:
            diagnostics.append({"severity": "warning", "code": "track_has_no_audio_stream", "track": record["label"]})
    return diagnostics


__all__ = ["DEFAULT_FOLDER_RULES", "FairlightFolderTracksResult", "render_fairlight_folder_tracks_manifest"]
