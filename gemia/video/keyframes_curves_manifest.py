"""Resolve 21 keyframes/curves loop-pingpong manifest."""
from __future__ import annotations
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from gemia.video.timeline_assets import cache_key_for_path, probe_media
DEFAULT_CURVE_TRACKS: list[dict[str, Any]] = [
    {
        "id": "opacity_loop_curve",
        "label": "Opacity loop curve",
        "target": "Edit timeline clip",
        "parameter": "opacity",
        "mode": "loop",
        "curve_editor": "Edit Page Curves",
        "keyframes": [
            {"time_fraction": 0.0, "value": 0.22, "easing": "linear"},
            {"time_fraction": 0.5, "value": 1.0, "easing": "bezier(0.42,0,0.58,1)"},
            {"time_fraction": 1.0, "value": 0.22, "easing": "ease_out"},
        ],
    },
    {
        "id": "scale_pingpong_curve",
        "label": "Scale ping-pong curve",
        "target": "Inspector Transform",
        "parameter": "zoom",
        "mode": "pingpong",
        "curve_editor": "Edit Page Keyframes",
        "keyframes": [
            {"time_fraction": 0.0, "value": 1.0, "easing": "ease_in_out"},
            {"time_fraction": 1.0, "value": 1.12, "easing": "bezier(0.25,0.1,0.25,1)"},
        ],
    },
    {
        "id": "relative_position_curve",
        "label": "Relative Y-position curve",
        "target": "Multi-clip adjustment",
        "parameter": "position_y",
        "mode": "relative",
        "relative_to": "clip_start",
        "curve_editor": "Inspector Keyframes",
        "keyframes": [
            {"time_fraction": 0.0, "value": -24.0, "easing": "ease_out"},
            {"time_fraction": 1.0, "value": 24.0, "easing": "ease_in"},
        ],
    },
]
def render_keyframes_curves_loop_pingpong_manifest(
    input_paths: list[str],
    output_dir: str,
    *,
    package_id: str = "resolve21_keyframes_curves_loop_pingpong",
    preset_name: str = "multi_clip_curve_loop_pingpong_review",
    curve_tracks: list[dict] | dict | None = None,
    clip_offsets: dict[str, float] | list[float] | None = None,
) -> str:
    """Emit timeline keyframe/curve manifests linked to real clips."""
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
            raise ValueError(f"Keyframe curves require visual media: {source}")
        sources.append(
            {
                "clip_id": f"kfc_clip_{index:02d}_{_safe_id(source.stem)}",
                "source_path": str(source),
                "asset_ref": _asset_ref(source, probe),
                "cache_key": cache_key_for_path(str(source)),
                "source_probe": probe,
            }
        )
    tracks = _normalize_curve_tracks(curve_tracks)
    offsets = _normalize_clip_offsets(sources, clip_offsets)
    assignments = [
        {
            "clip_id": source["clip_id"],
            "asset_ref": source["asset_ref"],
            "timeline_offset_seconds": offsets[source["clip_id"]],
            "analysis_window": _analysis_window(source),
            "adjusted_curve_tracks": [
                _track_for_clip(track, source, offsets[source["clip_id"]]) for track in tracks
            ],
            "cache_key": source["cache_key"],
        }
        for source in sources
    ]
    manifest = {
        "schema_version": 1,
        "effect": "resolve21_keyframes_curves_loop_pingpong_manifest",
        "rendered_at": datetime.now(timezone.utc).isoformat(),
        "package": {
            "package_id": _safe_id(package_id),
            "preset_name": _safe_id(preset_name),
            "clip_count": len(sources),
            "curve_track_count": len(tracks),
        },
        "sources": sources,
        "curve_tracks": tracks,
        "clip_assignments": assignments,
        "resolve_controls": {
            "page": "Edit",
            "panels": ["Keyframes", "Curves", "Inspector"],
            "animation_modes": ["clamp", "loop", "pingpong", "relative"],
            "curve_handles": "four_point_bezier",
        },
        "diagnostics": [
            f"{len(sources)} real clips linked to timeline keyframe/curve metadata",
            f"{len(tracks)} loop/ping-pong/relative curve tracks emitted",
        ],
        "review_hints": [
            "manifest is non-destructive metadata; source media is not modified",
            "re-analyze if source cache_key changes",
            "time fractions are clamped to 0..1 and mapped to per-clip timeline seconds",
        ],
    }
    manifest_path = output_root / "keyframes_curves_loop_pingpong_manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return str(manifest_path)
def _normalize_curve_tracks(curve_tracks: list[dict] | dict | None) -> list[dict[str, Any]]:
    if curve_tracks is None:
        return [_normalize_single_track(item) for item in DEFAULT_CURVE_TRACKS]
    items: list[dict[str, Any]] = []
    if isinstance(curve_tracks, dict):
        if "keyframes" in curve_tracks or "parameter" in curve_tracks:
            items.append(_normalize_single_track(curve_tracks))
        else:
            for track_id, config in curve_tracks.items():
                if isinstance(config, dict):
                    items.append(_normalize_single_track({"id": track_id, **config}))
    elif isinstance(curve_tracks, list):
        items.extend(_normalize_single_track(item) for item in curve_tracks if isinstance(item, dict))
    return items or [_normalize_single_track(DEFAULT_CURVE_TRACKS[0])]
def _normalize_single_track(raw: dict[str, Any]) -> dict[str, Any]:
    track_id = raw.get("id") or raw.get("track_id") or raw.get("parameter") or "curve_track"
    mode = _safe_choice(str(raw.get("mode") or "clamp"), {"clamp", "loop", "pingpong", "relative"}, "clamp")
    return {
        "id": _safe_id(str(track_id)),
        "label": str(raw.get("label") or raw.get("name") or track_id),
        "target": str(raw.get("target") or "Edit timeline clip"),
        "parameter": _safe_id(str(raw.get("parameter") or "opacity")),
        "mode": mode,
        "relative_to": str(raw.get("relative_to") or ("clip_start" if mode == "relative" else "timeline_start")),
        "curve_editor": _safe_choice(
            str(raw.get("curve_editor") or "Edit Page Curves"),
            {"edit_page_curves", "edit_page_keyframes", "inspector_keyframes"},
            "edit_page_curves",
        ),
        "keyframes": _normalize_keyframes(raw.get("keyframes")),
    }
def _normalize_keyframes(raw_keyframes: Any) -> list[dict[str, Any]]:
    frames = [
        _normalize_keyframe(item, index)
        for index, item in enumerate(raw_keyframes if isinstance(raw_keyframes, list) else [])
        if isinstance(item, dict)
    ]
    if len(frames) < 2:
        frames = [
            {"time_fraction": 0.0, "value": 0.0, "easing": "linear", "bezier_handles": None},
            {"time_fraction": 1.0, "value": 1.0, "easing": "ease_out", "bezier_handles": None},
        ]
    return sorted(frames, key=lambda item: item["time_fraction"])
def _normalize_keyframe(raw: dict[str, Any], index: int) -> dict[str, Any]:
    easing, handles = _normalize_easing(raw)
    return {
        "time_fraction": _clamp_float(raw.get("time_fraction", raw.get("position", index)), 0.0, 1.0, float(index)),
        "value": _clamp_float(raw.get("value"), -10000.0, 10000.0, 0.0),
        "easing": easing,
        "bezier_handles": handles,
    }
def _track_for_clip(track: dict[str, Any], source: dict[str, Any], offset: float) -> dict[str, Any]:
    duration = float(source["source_probe"].get("duration") or 0.0)
    mapped = []
    for point in track["keyframes"]:
        local_seconds = round(point["time_fraction"] * duration, 3)
        mapped.append({**point, "local_seconds": local_seconds, "timeline_seconds": round(offset + local_seconds, 3)})
    return {**track, "keyframes": mapped}
def _normalize_clip_offsets(sources: list[dict[str, Any]], raw: dict[str, float] | list[float] | None) -> dict[str, float]:
    offsets: dict[str, float] = {}
    cursor = 0.0
    for index, source in enumerate(sources):
        if isinstance(raw, list) and index < len(raw):
            offset = _clamp_float(raw[index], 0.0, 86400.0, cursor)
        elif isinstance(raw, dict):
            offset = _clamp_float(raw.get(source["clip_id"], raw.get(str(index))), 0.0, 86400.0, cursor)
        else:
            offset = cursor
        offsets[source["clip_id"]] = offset
        cursor = offset + float(source["source_probe"].get("duration") or 0.0)
    return offsets
def _analysis_window(source: dict[str, Any]) -> dict[str, Any]:
    probe = source["source_probe"]
    duration = round(float(probe.get("duration") or 0.0), 3)
    fps = max(float(probe.get("fps") or 24.0), 1.0)
    frames = max(1, int(round(duration * fps))) if duration else 1
    return {"start_seconds": 0.0, "end_seconds": duration, "estimated_frames": frames, "sample_stride": 4 if frames >= 24 else 1}
def _normalize_easing(raw: dict[str, Any]) -> tuple[str, list[float] | None]:
    handles = raw.get("bezier_handles") or raw.get("control_points")
    if isinstance(handles, (list, tuple)) and len(handles) == 4:
        values = [_clamp_float(item, 0.0, 1.0, default) for item, default in zip(handles, (0.25, 0.1, 0.25, 1.0))]
        return f"bezier({values[0]:g},{values[1]:g},{values[2]:g},{values[3]:g})", values
    raw_easing = str(raw.get("easing") or raw.get("curve") or "linear")
    match = re.fullmatch(r"bezier\(\s*([-+]?\d*\.?\d+)\s*,\s*([-+]?\d*\.?\d+)\s*,\s*([-+]?\d*\.?\d+)\s*,\s*([-+]?\d*\.?\d+)\s*\)", raw_easing)
    if match:
        values = [_clamp_float(part, 0.0, 1.0, 0.0) for part in match.groups()]
        return f"bezier({values[0]:g},{values[1]:g},{values[2]:g},{values[3]:g})", values
    easing = _safe_choice(raw_easing, {"linear", "ease_in", "ease_out", "ease_in_out", "hold"}, "linear")
    return easing, None
def _asset_ref(path: Path, probe: dict[str, Any]) -> str:
    duration = round(float(probe.get("duration") or 0.0), 3)
    return f"{_safe_id(path.stem)}:{path.stat().st_size}:{duration}:{int(probe.get('width') or 0)}x{int(probe.get('height') or 0)}"
def _clamp_float(value: Any, min_val: float, max_val: float, default: float) -> float:
    try:
        number = float(value)
    except Exception:
        number = default
    return round(max(min_val, min(number, max_val)), 3)
def _safe_choice(value: str, choices: set[str], default: str) -> str:
    candidates = (value.lower(), _safe_id(value), _safe_id(value).replace("_", ""))
    for candidate in candidates:
        if candidate in choices:
            return candidate
    return default
def _to_snake_case(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()
def _safe_id(value: str) -> str:
    value = _to_snake_case(value)
    return re.sub(r"[^a-z0-9_-]+", "_", value.strip()).strip("_") or "item"
__all__ = ["DEFAULT_CURVE_TRACKS", "render_keyframes_curves_loop_pingpong_manifest"]
