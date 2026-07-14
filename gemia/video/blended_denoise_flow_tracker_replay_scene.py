"""Resolve 21 blended denoise, flow, tracker, Fusion, and replay scene."""
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from gemia.video.advanced_noise_reduction import render_advanced_noise_reduction_profile_manifest
from gemia.video.fusion_effect_animation import render_animate_fusion_effects_edit_page_manifest
from gemia.video.optical_flow_speed_change import render_optical_flow_speed_change_manifest
from gemia.video.replay_editor_multicam import render_replay_editor_multicam_action_manifest
from gemia.video.track_follow_mask import render_track_follow_objects_mask_manifest


def render_blended_denoise_flow_tracker_replay_scene(
    video_paths: Iterable[str],
    output_path: str,
    *,
    scene_id: str = "resolve21_denoise_flow_tracker_replay_scene",
    max_seconds: float = 1.0,
    temp_dir: str | Path | None = None,
) -> str:
    """Create a fused real-media review package across Resolve 21 batch 009."""
    videos = [Path(path).expanduser().resolve() for path in video_paths]
    if not videos:
        raise ValueError("video_paths cannot be empty")
    for video in videos:
        if not video.exists():
            raise FileNotFoundError(f"Video file not found: {video}")
        if not video.is_file():
            raise OSError(f"Video path is not a file: {video}")
    if max_seconds <= 0:
        raise ValueError("max_seconds must be greater than 0")

    output = Path(output_path).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    work_dir = Path(temp_dir).expanduser().resolve() if temp_dir else output.parent / f"{output.stem}_denoise_flow_tracker_replay_steps"
    work_dir.mkdir(parents=True, exist_ok=True)
    inputs = [str(path) for path in videos]

    denoise_manifest = Path(render_advanced_noise_reduction_profile_manifest(
        inputs,
        str(work_dir / "01_denoise_profiles"),
        package_id=f"{scene_id}:denoise_profiles",
        profile_name="event_cleanup",
    ))
    flow_manifest = Path(render_optical_flow_speed_change_manifest(
        inputs,
        str(work_dir / "02_optical_flow_retime"),
        package_id=f"{scene_id}:optical_flow",
        preset_name="replay_slow_motion_handles",
        retime_targets={
            "highlight_slow_push": {
                "label": "Highlight slow push",
                "speed_factor": 0.5,
                "interpolation_quality": 4,
                "generated_frame_range": "effect_region",
            },
            "reaction_hold": {
                "label": "Reaction hold",
                "speed_factor": 0.75,
                "interpolation_quality": 3,
                "generated_frame_range": "full_clip",
            },
        },
    ))
    tracker_manifest = Path(render_track_follow_objects_mask_manifest(
        inputs,
        str(work_dir / "03_track_follow_masks"),
        package_id=f"{scene_id}:track_masks",
        preset_name="follow_player_and_reaction",
        track_windows={
            "PlayerWindow": {
                "label": "Player window",
                "target_kind": "person",
                "mask_shape": "ellipse",
                "start_rect": [0.22, 0.2, 0.42, 0.48],
                "follow_mode": "window",
                "tracking_quality": 4,
                "softness": 0.22,
                "effect_target": "highlight",
            },
            "ReactionWindow": {
                "label": "Reaction window",
                "target_kind": "object",
                "mask_shape": "rectangle",
                "start_rect": [0.48, 0.18, 0.34, 0.42],
                "follow_mode": "point",
                "tracking_quality": 3,
                "softness": 0.18,
                "effect_target": "color_window",
            },
        },
    ))
    fusion_manifest = Path(render_animate_fusion_effects_edit_page_manifest(
        inputs,
        str(work_dir / "04_fusion_effect_animation"),
        package_id=f"{scene_id}:fusion_effects",
        preset_name="replay_emphasis_curves",
        effect_controls={
            "ReplayGlow": {
                "label": "Replay glow",
                "fusion_effect": "Glow",
                "parameter": "Blend",
                "curve_editor": "EditPageCurves",
                "duration_policy": "effect_region",
                "keyframes": [
                    {"time_fraction": 0.0, "value": 0.0, "easing": "ease_in"},
                    {"time_fraction": 0.45, "value": 0.78, "easing": "ease_in_out"},
                    {"time_fraction": 1.0, "value": 0.2, "easing": "ease_out"},
                ],
            },
            "ReturnZoom": {
                "label": "Return zoom",
                "fusion_effect": "Transform",
                "parameter": "Size",
                "curve_editor": "EditPageKeyframes",
                "duration_policy": "scale_to_clip",
                "keyframes": [
                    {"time_fraction": 0.0, "value": 1.0, "easing": "linear"},
                    {"time_fraction": 0.5, "value": 1.06, "easing": "ease_in_out"},
                    {"time_fraction": 1.0, "value": 1.0, "easing": "ease_out"},
                ],
            },
        },
    ))
    replay_manifest = Path(render_replay_editor_multicam_action_manifest(
        inputs,
        str(work_dir / "05_replay_editor"),
        package_id=f"{scene_id}:replay_editor",
        preset_name="event_replay_multicam_review",
    ))

    _write_review_video(videos, output, max_seconds=max_seconds)
    denoise_payload = _read_json(denoise_manifest)
    flow_payload = _read_json(flow_manifest)
    tracker_payload = _read_json(tracker_manifest)
    fusion_payload = _read_json(fusion_manifest)
    replay_payload = _read_json(replay_manifest)
    metadata = {
        "schema_version": 1,
        "effect": "resolve21_blended_denoise_flow_tracker_replay_scene",
        "rendered_at": datetime.now(timezone.utc).isoformat(),
        "scene": {
            "scene_id": scene_id,
            "source_video_paths": [str(path) for path in videos],
            "output_path": str(output),
            "clip_count": len(videos),
            "max_seconds": float(max_seconds),
        },
        "components": {
            "denoise_profile_manifest": str(denoise_manifest),
            "optical_flow_manifest": str(flow_manifest),
            "track_follow_mask_manifest": str(tracker_manifest),
            "fusion_effect_animation_manifest": str(fusion_manifest),
            "replay_editor_multicam_manifest": str(replay_manifest),
        },
        "continuity": {
            "denoise_asset_refs": _refs(denoise_payload.get("sources", [])),
            "flow_asset_refs": _refs(flow_payload.get("sources", [])),
            "tracker_asset_refs": _refs(tracker_payload.get("sources", [])),
            "fusion_asset_refs": _refs(fusion_payload.get("sources", [])),
            "replay_asset_refs": _refs(replay_payload.get("sources", [])),
            "shared_asset_refs": _shared_refs(denoise_payload, flow_payload, tracker_payload, fusion_payload, replay_payload),
            "denoise_assignment_count": len(denoise_payload.get("clip_assignments", [])),
            "retime_assignment_count": len(flow_payload.get("clip_assignments", [])),
            "track_assignment_count": len(tracker_payload.get("clip_assignments", [])),
            "fusion_assignment_count": len(fusion_payload.get("clip_assignments", [])),
            "replay_segment_count": len(replay_payload.get("replay_segments", [])),
        },
        "review_hints": [
            "open the review mp4 and confirm the same real clips drive denoise, retime, tracking, Fusion, and replay manifests",
            "compare shared asset refs before judging retime or tracker continuity",
            "verify replay segments inherit non-destructive analysis metadata rather than baking source changes",
            "run two real-video reproductions before marking this blended scene stable",
        ],
    }
    output.with_suffix(".blended_denoise_flow_tracker_replay.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return str(output)


def _write_review_video(inputs: list[Path], output: Path, *, max_seconds: float) -> None:
    review_dir = output.parent / f"{output.stem}_review_clips"
    review_dir.mkdir(parents=True, exist_ok=True)
    clips: list[Path] = []
    for index, source in enumerate(inputs):
        clip = review_dir / f"{index:02d}_{_safe_stem(source)}.mp4"
        _trim_normalized(source, clip, max_seconds=max_seconds)
        clips.append(clip)
    concat_file = review_dir / "concat.txt"
    concat_file.write_text("".join(f"file '{clip.as_posix()}'\n" for clip in clips), encoding="utf-8")
    proc = subprocess.run(
        ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat_file), "-c", "copy", str(output)],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg review concat failed: {proc.stderr[-1000:]}")


def _trim_normalized(source: Path, output: Path, *, max_seconds: float) -> None:
    proc = subprocess.run(
        [
            "ffmpeg", "-y", "-i", str(source), "-t", f"{max_seconds:.3f}",
            "-vf", "scale='if(gte(iw,ih),320,-2)':'if(gte(iw,ih),-2,320)'",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-movflags", "+faststart", str(output),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg review clip failed for {source}: {proc.stderr[-1000:]}")


def _refs(items: list[dict[str, Any]]) -> list[str]:
    return [str(item["asset_ref"]) for item in items if isinstance(item, dict) and item.get("asset_ref")]


def _shared_refs(*payloads: dict[str, Any]) -> list[str]:
    ref_sets: list[set[str]] = []
    for payload in payloads:
        refs = set(_refs(payload.get("sources", [])))
        if refs:
            ref_sets.append(refs)
    return sorted(set.intersection(*ref_sets)) if ref_sets else []


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _safe_stem(path: Path) -> str:
    return "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in path.stem) or "media"


__all__ = ["render_blended_denoise_flow_tracker_replay_scene"]
