"""Resolve 21 group-version color workflow manifests."""
from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_GRADE_VERSIONS: list[dict[str, Any]] = [
    {
        "id": "base_balance",
        "label": "Base balance",
        "look": "neutral_rec709",
        "temperature": 0,
        "exposure": 0.0,
        "contrast": 1.0,
    },
    {
        "id": "warm_client_alt",
        "label": "Warm client alt",
        "look": "warm_highlight_rolloff",
        "temperature": 420,
        "exposure": 0.05,
        "contrast": 1.08,
    },
]


def render_group_versions_color_workflow(
    input_paths: list[str],
    output_dir: str,
    *,
    group_id: str = "resolve21_group_versions",
    grade_versions: list[dict[str, Any]] | None = None,
    group_assignments: dict[str, str] | None = None,
) -> str:
    """Track group-level Resolve color versions across related real clips."""
    if not input_paths:
        raise ValueError("input_paths must contain at least one media path")
    output_root = Path(output_dir).expanduser().resolve()
    output_root.mkdir(parents=True, exist_ok=True)

    clips = []
    assignments = group_assignments or {}
    for index, raw_path in enumerate(input_paths):
        source = Path(raw_path).expanduser().resolve()
        if not source.exists():
            raise FileNotFoundError(f"Input media not found: {source}")
        probe = _probe_media(source)
        clip_id = f"clip_{index:02d}_{_safe_id(source.stem)}"
        clips.append(
            {
                "clip_id": clip_id,
                "source_path": str(source),
                "source_probe": probe,
                "asset_ref": _asset_ref(source, probe),
                "group_name": _group_for(source, clip_id, assignments, index),
            }
        )

    versions = [_normalize_grade(raw, index) for index, raw in enumerate(grade_versions or DEFAULT_GRADE_VERSIONS)]
    groups = []
    for group_name in sorted({clip["group_name"] for clip in clips}):
        members = [clip for clip in clips if clip["group_name"] == group_name]
        version_payloads = []
        for version_index, version in enumerate(versions):
            version_payloads.append(
                {
                    "version_id": version["id"],
                    "label": version["label"],
                    "look": version["look"],
                    "rank": version_index,
                    "applies_to_clip_ids": [clip["clip_id"] for clip in members],
                    "grade": {
                        "temperature": version["temperature"],
                        "exposure": version["exposure"],
                        "contrast": version["contrast"],
                        "saturation": version["saturation"],
                        "pivot": version["pivot"],
                    },
                    "node_recipe": _node_recipe(version),
                    "group_grade_ref": f"{group_id}:{_safe_id(group_name)}:{version['id']}",
                }
            )
        groups.append(
            {
                "group_name": group_name,
                "group_id": f"{group_id}:{_safe_id(group_name)}",
                "clip_count": len(members),
                "member_clip_ids": [clip["clip_id"] for clip in members],
                "asset_refs": [clip["asset_ref"] for clip in members],
                "grade_versions": version_payloads,
            }
        )

    manifest = {
        "schema_version": 1,
        "effect": "resolve21_group_versions_color_workflow",
        "rendered_at": datetime.now(timezone.utc).isoformat(),
        "workflow": {
            "group_id": group_id,
            "clip_count": len(clips),
            "group_count": len(groups),
            "version_count": len(versions),
            "version_ids": [version["id"] for version in versions],
        },
        "clips": clips,
        "groups": groups,
        "diagnostics": [
            f"{len(clips)} clips assigned to {len(groups)} color workflow groups",
            f"{len(versions)} group-level grade versions are tracked per group",
        ],
        "review_hints": [
            "confirm clips in each group should share the same grade version stack",
            "compare group_grade_ref values before sending variants to review",
            "use asset_ref to relink clips if group membership changes",
        ],
    }
    manifest_path = output_root / "group_versions_color_workflow.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return str(manifest_path)


def _group_for(source: Path, clip_id: str, assignments: dict[str, str], index: int) -> str:
    for key in (str(source), source.name, source.stem, clip_id):
        if key in assignments:
            value = str(assignments[key]).strip()
            if value:
                return value
    return "group_a" if index % 2 == 0 else "group_b"


def _normalize_grade(raw: dict[str, Any], index: int) -> dict[str, Any]:
    version_id = _safe_id(str(raw.get("id") or f"grade_version_{index}"))
    return {
        "id": version_id,
        "label": str(raw.get("label") or version_id.replace("_", " ").title()),
        "look": str(raw.get("look") or "custom_group_grade"),
        "temperature": int(raw.get("temperature") or 0),
        "exposure": round(float(raw.get("exposure") or 0.0), 3),
        "contrast": round(float(raw.get("contrast") or 1.0), 3),
        "saturation": round(float(raw.get("saturation") or 1.0), 3),
        "pivot": round(float(raw.get("pivot") or 0.42), 3),
    }


def _node_recipe(version: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {"node_id": f"{version['id']}:input", "kind": "group_input", "parameters": {}},
        {"node_id": f"{version['id']}:balance", "kind": "primary_balance", "parameters": {"temperature": version["temperature"], "exposure": version["exposure"]}},
        {"node_id": f"{version['id']}:look", "kind": "group_look", "parameters": {"look": version["look"], "contrast": version["contrast"], "saturation": version["saturation"], "pivot": version["pivot"]}},
        {"node_id": f"{version['id']}:output", "kind": "group_output", "parameters": {}},
    ]


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
    return {
        "duration_seconds": round(float(fmt.get("duration") or 0.0), 3),
        "size_bytes": int(fmt.get("size") or path.stat().st_size),
        "width": int(video.get("width") or 0),
        "height": int(video.get("height") or 0),
        "video_codec": str(video.get("codec_name") or ""),
    }


def _asset_ref(path: Path, probe: dict[str, Any]) -> str:
    return f"{_safe_id(path.stem)}:{path.stat().st_size}:{probe['duration_seconds']}:{probe['width']}x{probe['height']}"


def _safe_id(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", value.strip()).strip("_").lower() or "item"


__all__ = ["DEFAULT_GRADE_VERSIONS", "render_group_versions_color_workflow"]
