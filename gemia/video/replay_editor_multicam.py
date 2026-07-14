"""Resolve 21 Replay Editor multicam action manifest."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from gemia.video.timeline_assets import cache_key_for_path, probe_media


DEFAULT_REPLAY_ACTIONS: list[dict[str, Any]] = [
    {
        "id": "instant_replay_slow_push",
        "label": "Instant replay slow push",
        "action_kind": "instant_replay",
        "preferred_angle": "auto_best",
        "pre_roll_seconds": 3.0,
        "post_roll_seconds": 2.0,
        "replay_speed": 0.5,
        "priority": 5,
        "marker_color": "red",
        "review_role": "sports_replay",
    },
    {
        "id": "reaction_cutaway_return",
        "label": "Reaction cutaway return",
        "action_kind": "angle_cut",
        "preferred_angle": "reaction",
        "pre_roll_seconds": 1.5,
        "post_roll_seconds": 1.5,
        "replay_speed": 1.0,
        "priority": 3,
        "marker_color": "blue",
        "review_role": "producer",
    },
]


def render_replay_editor_multicam_action_manifest(
    input_paths: list[str],
    output_dir: str,
    *,
    package_id: str = "resolve21_replay_editor_multicam_action",
    preset_name: str = "replay_editor_action_review",
    replay_actions: list[dict] | dict | None = None,
    camera_angles: list[dict] | dict | None = None,
) -> str:
    """Emit replay-style action markers and camera selections for real clips."""
    if not input_paths:
        raise ValueError("input_paths must contain at least one media path")
    output_root = Path(output_dir).expanduser().resolve()
    output_root.mkdir(parents=True, exist_ok=True)

    sources = []
    for index, raw_path in enumerate(input_paths):
        source = Path(raw_path).expanduser().resolve()
        if not source.exists():
            raise FileNotFoundError(f"Input media not found: {source}")
        if not source.is_file():
            raise OSError(f"Input media is not a file: {source}")
        probe = probe_media(str(source))
        if int(probe.get("width") or 0) <= 0 or int(probe.get("height") or 0) <= 0:
            raise ValueError(f"Replay editor action manifests require visual media: {source}")
        sources.append(
            {
                "clip_id": f"rem_clip_{index:02d}_{_safe_id(source.stem)}",
                "source_path": str(source),
                "asset_ref": _asset_ref(source, probe),
                "cache_key": cache_key_for_path(str(source)),
                "source_probe": probe,
            }
        )

    angles = _normalize_camera_angles(camera_angles, sources)
    actions = _normalize_replay_actions(replay_actions)
    replay_segments = [
        _replay_segment(source, actions[index % len(actions)], angles[index % len(angles)], index)
        for index, source in enumerate(sources)
    ]
    manifest = {
        "schema_version": 1,
        "effect": "resolve21_replay_editor_multicam_action_manifest",
        "rendered_at": datetime.now(timezone.utc).isoformat(),
        "package": {
            "package_id": _safe_id(package_id),
            "preset_name": _safe_id(preset_name),
            "clip_count": len(sources),
            "angle_count": len(angles),
            "action_count": len(actions),
        },
        "sources": sources,
        "camera_angles": angles,
        "replay_actions": actions,
        "replay_segments": replay_segments,
        "resolve_controls": {
            "page": "Edit",
            "workflow": "Replay Editor multicam action marking",
            "panels": ["Replay Editor", "Multicam Viewer", "Inspector", "Markers"],
            "hardware_surfaces": ["DaVinci Resolve Replay Editor", "speed editor jog/shuttle"],
            "editable_metadata": ["action markers", "angle selections", "pre-roll", "post-roll", "replay speed"],
        },
        "diagnostics": [
            f"{len(sources)} real clips linked to replay action metadata",
            f"{len(angles)} camera angle selections emitted",
            f"{len(actions)} bounded replay actions emitted",
        ],
        "review_hints": [
            "manifest is metadata only; source media is not destructively modified",
            "re-analyze if source cache_key changes before replay review",
            "camera selections are deterministic and can be overridden by a future multicam analyzer",
        ],
    }
    manifest_path = output_root / "replay_editor_multicam_action_manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return str(manifest_path)


def _normalize_camera_angles(camera_angles: list[dict] | dict | None, sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    raw_angles: list[dict[str, Any]] = []
    if isinstance(camera_angles, dict):
        for angle_id, config in camera_angles.items():
            if isinstance(config, dict):
                raw_angles.append({"id": angle_id, **config})
    elif isinstance(camera_angles, list):
        raw_angles = [item for item in camera_angles if isinstance(item, dict)]
    if not raw_angles:
        raw_angles = [
            {
                "id": f"camera_{index + 1}",
                "label": f"Camera {index + 1}",
                "role": _default_role(index),
                "source_index": index,
                "quality_rank": min(index + 1, 5),
            }
            for index, _source in enumerate(sources)
        ]
    angles = [_normalize_single_angle(item, sources, index) for index, item in enumerate(raw_angles)]
    return angles or [_normalize_single_angle({"id": "camera_1", "source_index": 0}, sources, 0)]


def _normalize_single_angle(raw: dict[str, Any], sources: list[dict[str, Any]], index: int) -> dict[str, Any]:
    source_index = _clamp_int(raw.get("source_index", raw.get("clip_index", index)), 0, len(sources) - 1, index % len(sources))
    source = sources[source_index]
    angle_id = raw.get("id") or raw.get("angle_id") or f"camera_{index + 1}"
    role = _safe_choice(
        str(raw.get("role") or raw.get("angle_role") or _default_role(index)),
        {"wide", "close_up", "reaction", "program", "handheld", "iso"},
        _default_role(index),
    )
    return {
        "angle_id": _safe_id(str(angle_id)),
        "label": str(raw.get("label") or raw.get("name") or angle_id),
        "role": role,
        "source_index": source_index,
        "clip_id": source["clip_id"],
        "asset_ref": source["asset_ref"],
        "cache_key": source["cache_key"],
        "quality_rank": _clamp_int(raw.get("quality_rank"), 1, 5, min(index + 1, 5)),
        "sync_basis": _safe_choice(
            str(raw.get("sync_basis") or "source_timecode_or_start"),
            {"source_timecode_or_start", "audio_waveform", "manual_marker"},
            "source_timecode_or_start",
        ),
    }


def _normalize_replay_actions(replay_actions: list[dict] | dict | None) -> list[dict[str, Any]]:
    raw_actions: list[dict[str, Any]] = []
    if isinstance(replay_actions, dict):
        if "action_kind" in replay_actions or "replay_speed" in replay_actions:
            raw_actions.append(replay_actions)
        else:
            for action_id, config in replay_actions.items():
                if isinstance(config, dict):
                    raw_actions.append({"id": action_id, **config})
    elif isinstance(replay_actions, list):
        raw_actions = [item for item in replay_actions if isinstance(item, dict)]
    if not raw_actions:
        raw_actions = DEFAULT_REPLAY_ACTIONS
    actions = [_normalize_single_action(item) for item in raw_actions]
    return actions or [_normalize_single_action(DEFAULT_REPLAY_ACTIONS[0])]


def _normalize_single_action(raw: dict[str, Any]) -> dict[str, Any]:
    action_id = raw.get("id") or raw.get("action_id") or "replay_action"
    return {
        "id": _safe_id(str(action_id)),
        "label": str(raw.get("label") or raw.get("name") or action_id),
        "action_kind": _safe_choice(
            str(raw.get("action_kind") or raw.get("kind") or "instant_replay"),
            {"instant_replay", "angle_cut", "slow_motion", "freeze_frame", "roll_back", "live_return"},
            "instant_replay",
        ),
        "preferred_angle": _safe_id(str(raw.get("preferred_angle") or raw.get("angle") or "auto_best")),
        "pre_roll_seconds": _clamp_float(raw.get("pre_roll_seconds", raw.get("pre_roll")), 0.0, 30.0, 3.0),
        "post_roll_seconds": _clamp_float(raw.get("post_roll_seconds", raw.get("post_roll")), 0.05, 30.0, 2.0),
        "replay_speed": _clamp_float(raw.get("replay_speed", raw.get("speed")), 0.1, 2.0, 0.5),
        "priority": _clamp_int(raw.get("priority"), 1, 5, 3),
        "marker_color": _safe_choice(
            str(raw.get("marker_color") or raw.get("color") or "red"),
            {"red", "orange", "yellow", "green", "blue", "purple", "white"},
            "red",
        ),
        "review_role": _safe_choice(
            str(raw.get("review_role") or "sports_replay"),
            {"producer", "sports_replay", "news_replay", "social_highlight"},
            "sports_replay",
        ),
    }


def _replay_segment(source: dict[str, Any], action: dict[str, Any], angle: dict[str, Any], index: int) -> dict[str, Any]:
    window = _analysis_window(source)
    duration = max(float(window["end_seconds"] or 0.0), 0.05)
    marker_time = round(min(max(duration * (0.35 + 0.15 * (index % 3)), 0.0), duration), 3)
    in_seconds = round(max(0.0, marker_time - action["pre_roll_seconds"]), 3)
    out_seconds = round(min(duration, marker_time + action["post_roll_seconds"]), 3)
    source_span = max(out_seconds - in_seconds, 0.05)
    return {
        "segment_id": f"replay_segment_{index:03d}",
        "clip_id": source["clip_id"],
        "asset_ref": source["asset_ref"],
        "marker": {
            "marker_id": f"action_marker_{index:03d}",
            "time_seconds": marker_time,
            "color": action["marker_color"],
            "label": action["label"],
            "priority": action["priority"],
        },
        "action": action,
        "camera_selection": {
            "angle_id": angle["angle_id"],
            "label": angle["label"],
            "role": angle["role"],
            "asset_ref": angle["asset_ref"],
            "selection_reason": _selection_reason(action, angle),
        },
        "source_range": {"in_seconds": in_seconds, "out_seconds": out_seconds},
        "estimated_output_seconds": round(source_span / max(action["replay_speed"], 0.1), 3),
        "analysis_window": window,
        "cache_key": source["cache_key"],
    }


def _selection_reason(action: dict[str, Any], angle: dict[str, Any]) -> str:
    if action["preferred_angle"] in {"auto_best", angle["angle_id"], angle["role"]}:
        return "preferred_or_best_available_angle"
    return "fallback_angle_for_available_source"


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


def _asset_ref(path: Path, probe: dict[str, Any]) -> str:
    duration = round(float(probe.get("duration") or 0.0), 3)
    return f"{_safe_id(path.stem)}:{path.stat().st_size}:{duration}:{int(probe.get('width') or 0)}x{int(probe.get('height') or 0)}"


def _default_role(index: int) -> str:
    return ("wide", "close_up", "reaction", "program", "iso")[index % 5]


def _clamp_float(value: Any, min_val: float, max_val: float, default: float) -> float:
    try:
        number = float(value)
    except Exception:
        number = default
    return round(max(min_val, min(number, max_val)), 3)


def _clamp_int(value: Any, min_val: int, max_val: int, default: int) -> int:
    try:
        number = int(value)
    except Exception:
        number = default
    return max(min_val, min(number, max_val))


def _safe_choice(value: str, choices: set[str], default: str) -> str:
    safe = _safe_id(value)
    return safe if safe in choices else default


def _to_snake_case(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def _safe_id(value: str) -> str:
    value = _to_snake_case(value)
    return re.sub(r"[^a-z0-9_-]+", "_", value.strip()).strip("_") or "item"


__all__ = ["DEFAULT_REPLAY_ACTIONS", "render_replay_editor_multicam_action_manifest"]
