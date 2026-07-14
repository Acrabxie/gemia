"""Resolve 21 Final Draft IntelliScript ingest manifests."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from gemia.video.timeline_assets import probe_media


DEFAULT_SCRIPT_TEXT = """
INT. EDIT BAY - DAY
The editor reviews the hero shot and marks the opening beat.

EXT. CITY ROOFTOP - SUNSET
The second clip becomes the visual reference for the reveal.
"""


def render_finaldraft_intelliscript_ingest_manifest(
    input_paths: list[str],
    output_dir: str,
    *,
    script_text: str | None = None,
    script_path: str | None = None,
    package_id: str = "resolve21_finaldraft_intelliscript_ingest",
    reel_name: str = "script_ingest_reel",
) -> str:
    """Emit Final Draft / IntelliScript-style scene-to-media metadata."""
    if not input_paths:
        raise ValueError("input_paths must contain at least one media path")
    output_root = Path(output_dir).expanduser().resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    package = _safe_id(package_id)
    script = _load_script(script_text=script_text, script_path=script_path)
    scenes = _parse_scenes(script)

    sources = []
    for index, raw_path in enumerate(input_paths):
        source = Path(raw_path).expanduser().resolve()
        if not source.exists():
            raise FileNotFoundError(f"Input media not found: {source}")
        if not source.is_file():
            raise OSError(f"Input media is not a file: {source}")
        probe = probe_media(str(source))
        if int(probe.get("width") or 0) <= 0 or int(probe.get("height") or 0) <= 0:
            raise ValueError(f"Script ingest source has no video stream: {source}")
        sources.append(
            {
                "clip_id": f"script_clip_{index:02d}_{_safe_id(source.stem)}",
                "source_path": str(source),
                "asset_ref": _asset_ref(source, probe),
                "source_probe": probe,
                "ingest_readiness": _ingest_readiness(probe),
            }
        )

    assignments = []
    for index, scene in enumerate(scenes):
        source = sources[index % len(sources)]
        duration = round(float(source["source_probe"].get("duration") or 0.0), 3)
        assignments.append(
            {
                "assignment_id": f"{package}_{scene['scene_id']}",
                "scene_id": scene["scene_id"],
                "scene_heading": scene["heading"],
                "beat_summary": scene["summary"],
                "line_range": scene["line_range"],
                "clip_asset_ref": source["asset_ref"],
                "timeline_intent": {
                    "reel": _safe_id(reel_name),
                    "bin": scene["location_type"].lower(),
                    "track": "V1",
                    "duration_seconds": duration,
                    "marker_color": _marker_color(scene["location_type"], index),
                },
                "intelliscript_controls": {
                    "source": "Final Draft",
                    "match_mode": "scene_heading_and_beat_keywords",
                    "scene_number": index + 1,
                    "location_type": scene["location_type"],
                    "keywords": scene["keywords"],
                    "auto_create_bins": True,
                    "attach_script_notes": True,
                },
                "validation": _assignment_validation(scene, source),
            }
        )

    manifest = {
        "schema_version": 1,
        "effect": "resolve21_finaldraft_intelliscript_ingest_manifest",
        "rendered_at": datetime.now(timezone.utc).isoformat(),
        "package": {
            "package_id": package,
            "clip_count": len(sources),
            "scene_count": len(scenes),
            "assignment_count": len(assignments),
            "reel_name": _safe_id(reel_name),
        },
        "script": {
            "line_count": len([line for line in script.splitlines() if line.strip()]),
            "source_path": str(Path(script_path).expanduser().resolve()) if script_path else None,
            "fingerprint": _fingerprint(script),
        },
        "sources": sources,
        "assignments": assignments,
        "diagnostics": [
            f"{len(scenes)} script scenes mapped to {len(sources)} real clips",
            "Final Draft / IntelliScript ingest metadata emitted without script parser runtime dependency",
        ],
        "review_hints": [
            "confirm scene headings match the intended timeline bins",
            "check round-robin assignments when scene count exceeds media count",
            "preserve script fingerprint when revising notes only",
        ],
    }
    manifest_path = output_root / "finaldraft_intelliscript_ingest_manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return str(manifest_path)


def _load_script(*, script_text: str | None, script_path: str | None) -> str:
    if script_path:
        path = Path(script_path).expanduser().resolve()
        if not path.exists():
            raise FileNotFoundError(f"Script file not found: {path}")
        text = path.read_text(encoding="utf-8")
    else:
        text = script_text or DEFAULT_SCRIPT_TEXT
    if not text.strip():
        raise ValueError("script_text or script_path must contain non-empty script text")
    return text


def _parse_scenes(script: str) -> list[dict[str, Any]]:
    raw_lines = [line.rstrip() for line in script.splitlines()]
    heading_re = re.compile(r"^\s*(INT\.|EXT\.|INT/EXT\.|I/E\.)\s+(.+)$", re.IGNORECASE)
    starts = []
    for index, line in enumerate(raw_lines):
        match = heading_re.match(line)
        if match:
            starts.append((index, match.group(1).upper().replace(".", ""), line.strip()))
    if not starts:
        starts = [(0, "SCENE", "SCENE 1")]
    scenes = []
    for scene_index, (line_index, location_type, heading) in enumerate(starts):
        next_line = starts[scene_index + 1][0] if scene_index + 1 < len(starts) else len(raw_lines)
        body = [line.strip() for line in raw_lines[line_index + 1:next_line] if line.strip()]
        summary = " ".join(body)[:180] or heading
        scenes.append(
            {
                "scene_id": f"scene_{scene_index + 1:03d}_{_safe_id(heading)}",
                "heading": heading,
                "location_type": location_type,
                "summary": summary,
                "line_range": {"start": line_index + 1, "end": max(line_index + 1, next_line)},
                "keywords": _keywords(" ".join([heading, summary])),
            }
        )
    return scenes


def _assignment_validation(scene: dict[str, Any], source: dict[str, Any]) -> dict[str, Any]:
    probe = source["source_probe"]
    keywords = set(scene["keywords"])
    stem_tokens = set(_keywords(Path(source["source_path"]).stem.replace("_", " ")))
    return {
        "scene_has_heading": bool(scene["heading"]),
        "clip_has_video": int(probe.get("width") or 0) > 0 and int(probe.get("height") or 0) > 0,
        "clip_duration_seconds": round(float(probe.get("duration") or 0.0), 3),
        "keyword_overlap": sorted(keywords & stem_tokens),
        "manual_review_recommended": not bool(keywords & stem_tokens),
    }


def _ingest_readiness(probe: dict[str, Any]) -> dict[str, Any]:
    width = int(probe.get("width") or 0)
    height = int(probe.get("height") or 0)
    return {
        "duration_seconds": round(float(probe.get("duration") or 0.0), 3),
        "source_resolution": f"{width}x{height}",
        "has_audio": bool(probe.get("has_audio")),
        "timeline_ready": width > 0 and height > 0,
    }


def _asset_ref(path: Path, probe: dict[str, Any]) -> str:
    duration = round(float(probe.get("duration") or 0.0), 3)
    return f"{_safe_id(path.stem)}:{path.stat().st_size}:{duration}:{int(probe.get('width') or 0)}x{int(probe.get('height') or 0)}"


def _keywords(text: str) -> list[str]:
    words = re.findall(r"[A-Za-z0-9]{3,}", text.lower())
    stop = {"the", "and", "with", "into", "scene", "clip", "shot"}
    return sorted({word for word in words if word not in stop})[:12]


def _fingerprint(text: str) -> str:
    total = sum((index + 1) * ord(char) for index, char in enumerate(text))
    return f"script-{len(text)}-{total % 1000003:06d}"


def _marker_color(location_type: str, index: int) -> str:
    if location_type == "INT":
        return "blue"
    if location_type == "EXT":
        return "green"
    return ["cyan", "purple", "orange"][index % 3]


def _safe_id(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", value.strip()).strip("_").lower() or "item"


__all__ = ["DEFAULT_SCRIPT_TEXT", "render_finaldraft_intelliscript_ingest_manifest"]
