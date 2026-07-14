"""Resolve-style AI Multicam SmartSwitch preview rendering."""
from __future__ import annotations

import json
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass
class MulticamSmartSwitchResult:
    output_path: str
    metadata_path: str


def render_ai_multicam_smartswitch_plan(
    camera_paths: list[str],
    output_path: str,
    *,
    speaker_segments: list[dict[str, Any]] | None = None,
    angle_labels: list[str] | None = None,
    clip_duration_seconds: float = 1.0,
    strategy: str = "speaker_round_robin",
) -> str:
    """Render a speaker-aware multicam preview and sidecar switch plan."""
    cameras = [Path(path).expanduser().resolve() for path in camera_paths]
    if len(cameras) < 2:
        raise ValueError("AI Multicam SmartSwitch requires at least two camera paths")
    for camera in cameras:
        if not camera.exists():
            raise FileNotFoundError(f"Camera input does not exist: {camera}")
    if clip_duration_seconds <= 0:
        raise ValueError("clip_duration_seconds must be greater than 0")

    output = Path(output_path).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    durations = [_probe_duration(camera) for camera in cameras]
    usable_duration = min(duration for duration in durations if duration > 0) if any(duration > 0 for duration in durations) else 0.0
    if usable_duration <= 0:
        raise ValueError("Could not determine usable duration for multicam inputs")

    labels = _normalize_angle_labels(angle_labels, len(cameras))
    decisions, effective_strategy, diagnostics = _build_decisions(
        speaker_segments=speaker_segments,
        camera_count=len(cameras),
        labels=labels,
        clip_duration_seconds=clip_duration_seconds,
        usable_duration=usable_duration,
        strategy=strategy,
    )
    if not decisions:
        raise ValueError("No multicam switch decisions could be generated")

    render_mode = _render_preview(cameras, output, decisions)
    metadata_path = output.with_suffix(".multicam_smartswitch.json")
    metadata = {
        "schema_version": 1,
        "effect": "resolve21_ai_multicam_smartswitch",
        "rendered_at": datetime.now(timezone.utc).isoformat(),
        "camera_paths": [str(camera) for camera in cameras],
        "output_path": str(output),
        "metadata_path": str(metadata_path),
        "strategy": effective_strategy,
        "requested_strategy": strategy,
        "angle_labels": labels,
        "clip_duration_seconds": clip_duration_seconds,
        "usable_duration_seconds": round(usable_duration, 3),
        "input_durations_seconds": [round(duration, 3) for duration in durations],
        "switch_decisions": decisions,
        "diagnostics": diagnostics,
        "render_mode": render_mode,
        "review_hints": [
            "confirm speaker-to-angle choices match the edit intent",
            "check switch cadence for jump cuts or repeated angles",
            "replace deterministic speaker mapping with diarization backend when available",
        ],
    }
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return str(output)


def _normalize_angle_labels(angle_labels: list[str] | None, camera_count: int) -> list[str]:
    labels = [str(label).strip() or f"Camera {index + 1}" for index, label in enumerate(angle_labels or [])]
    while len(labels) < camera_count:
        labels.append(f"Camera {len(labels) + 1}")
    return labels[:camera_count]


def _build_decisions(
    *,
    speaker_segments: list[dict[str, Any]] | None,
    camera_count: int,
    labels: list[str],
    clip_duration_seconds: float,
    usable_duration: float,
    strategy: str,
) -> tuple[list[dict[str, Any]], str, list[str]]:
    normalized_strategy = strategy.strip().lower()
    diagnostics: list[str] = []
    if normalized_strategy in {"speaker", "speaker_round_robin", "auto"} and speaker_segments:
        decisions = _speaker_decisions(speaker_segments, camera_count, labels, usable_duration)
        if decisions:
            return decisions, "speaker_round_robin", diagnostics
        diagnostics.append("speaker_segments were provided but no valid time ranges survived normalization")
    if normalized_strategy not in {"round_robin", "speaker_round_robin", "auto", "speaker"}:
        raise ValueError(f"Unsupported multicam smartswitch strategy: {strategy}")
    decisions = []
    start = 0.0
    index = 0
    while start < usable_duration:
        end = min(start + clip_duration_seconds, usable_duration)
        if end <= start:
            break
        camera_index = index % camera_count
        decisions.append(_decision(camera_index, labels[camera_index], start, end, "round_robin", None))
        start = end
        index += 1
    if not speaker_segments and normalized_strategy in {"speaker", "speaker_round_robin", "auto"}:
        diagnostics.append("speaker_segments missing; used deterministic round-robin angle selection")
    return decisions, "round_robin", diagnostics


def _speaker_decisions(
    speaker_segments: list[dict[str, Any]],
    camera_count: int,
    labels: list[str],
    usable_duration: float,
) -> list[dict[str, Any]]:
    speaker_to_camera: dict[str, int] = {}
    decisions: list[dict[str, Any]] = []
    for raw in sorted(speaker_segments, key=lambda item: float(item.get("start_seconds", 0.0))):
        speaker = " ".join(str(raw.get("speaker", "speaker")).split()) or "speaker"
        start = max(float(raw.get("start_seconds", 0.0)), 0.0)
        end = min(max(float(raw.get("end_seconds", start)), start), usable_duration)
        if end <= start:
            continue
        if speaker not in speaker_to_camera:
            speaker_to_camera[speaker] = len(speaker_to_camera) % camera_count
        camera_index = speaker_to_camera[speaker]
        decisions.append(_decision(camera_index, labels[camera_index], start, end, "speaker_active", speaker))
    return decisions


def _decision(camera_index: int, angle_label: str, start: float, end: float, reason: str, speaker: str | None) -> dict[str, Any]:
    payload = {
        "camera_index": camera_index,
        "angle_label": angle_label,
        "start_seconds": round(start, 3),
        "end_seconds": round(end, 3),
        "reason": reason,
    }
    if speaker is not None:
        payload["speaker"] = speaker
    return payload


def _render_preview(cameras: list[Path], output: Path, decisions: list[dict[str, Any]]) -> str:
    with tempfile.TemporaryDirectory() as td:
        temp_dir = Path(td)
        segment_paths: list[Path] = []
        for index, decision in enumerate(decisions):
            start = float(decision["start_seconds"])
            duration = max(float(decision["end_seconds"]) - start, 0.05)
            camera = cameras[int(decision["camera_index"])]
            segment_path = temp_dir / f"segment_{index:04d}.mp4"
            _run(
                [
                    "ffmpeg", "-y", "-ss", f"{start:.3f}", "-t", f"{duration:.3f}", "-i", str(camera),
                    "-map", "0:v:0", "-map", "0:a?", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                    "-movflags", "+faststart", str(segment_path),
                ],
                f"ffmpeg multicam segment render failed for {camera}",
            )
            segment_paths.append(segment_path)
        concat_list = temp_dir / "concat.txt"
        concat_list.write_text("".join(f"file '{path.as_posix()}'\n" for path in segment_paths), encoding="utf-8")
        _run(
            [
                "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat_list),
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(output),
            ],
            "ffmpeg multicam concat failed",
        )
    return "ffmpeg_segment_concat"


def _probe_duration(path: Path) -> float:
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    try:
        return max(float(proc.stdout.strip()), 0.0)
    except ValueError:
        return 0.0


def _run(cmd: list[str], message: str) -> str:
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    combined = "\n".join(part for part in (proc.stdout, proc.stderr) if part)
    if proc.returncode != 0:
        raise RuntimeError(f"{message}: {combined[-1000:]}")
    return combined


__all__ = ["MulticamSmartSwitchResult", "render_ai_multicam_smartswitch_plan"]
