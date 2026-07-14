from __future__ import annotations

import json

import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path



@dataclass(frozen=True)
class MusicEditorResult:
    output_path: str
    metadata_path: str


def render_ai_music_editor_plan(
    video_path: str,
    music_path: str,
    output_path: str,
    *,
    target_duration_seconds: float | None = None,
    track_id: str = "A1",
    section_count: int = 4,
    fade_seconds: float = 0.15,
) -> str:
    """Fit music to video duration using ffmpeg and mux it into an MP4 preview.

    The music will be looped/trimmed to match the target duration or video duration.
    A metadata JSON file will be generated alongside the output video.
    """
    if section_count <= 0:
        raise ValueError("section_count must be greater than 0.")
    if fade_seconds < 0:
        raise ValueError("fade_seconds cannot be negative.")

    video_p = Path(video_path).expanduser().resolve()
    music_p = Path(music_path).expanduser().resolve()
    output_p = Path(output_path).expanduser().resolve()

    if not video_p.exists():
        raise FileNotFoundError(f"Video file not found: {video_p}")
    if not music_p.exists():
        raise FileNotFoundError(f"Music file not found: {music_p}")

    output_p.parent.mkdir(parents=True, exist_ok=True)
    metadata_path = output_p.with_suffix(".music_editor.json")

    video_duration = _probe_duration(video_p)
    if video_duration <= 0:
        raise ValueError(f"Could not determine duration for video: {video_p}")

    effective_target_duration = target_duration_seconds if target_duration_seconds is not None else video_duration
    diagnostics: list[str] = []
    if target_duration_seconds is not None and effective_target_duration > video_duration:
        diagnostics.append(f"Target duration ({target_duration_seconds:.2f}s) exceeds video duration ({video_duration:.2f}s). Clamping to video duration.")
        effective_target_duration = video_duration

    if effective_target_duration <= 0:
        raise ValueError("Effective target duration must be greater than 0.")

    # Determine music fitting strategy (loop or trim)
    music_duration = _probe_duration(music_p, is_audio=True)
    if music_duration <= 0:
        raise ValueError(f"Could not determine duration for music: {music_p}")

    ffmpeg_music_input_args: list[str] = []
    if music_duration < effective_target_duration:
        # Loop music
        loop_count = int(effective_target_duration / music_duration) + 1
        ffmpeg_music_input_args = ["-stream_loop", str(loop_count), "-i", str(music_p)]
        diagnostics.append(f"Music ({music_duration:.2f}s) is shorter than target ({effective_target_duration:.2f}s), looping {loop_count} times.")
    else:
        # Trim music
        ffmpeg_music_input_args = ["-i", str(music_p)]
        diagnostics.append(f"Music ({music_duration:.2f}s) is longer than target ({effective_target_duration:.2f}s), trimming.")

    # FFmpeg command to mux video and fitted music
    # Uses atrim and asetpts to trim audio to target_duration
    # Uses afade to add fade in/out
    safe_fade_seconds = min(fade_seconds, effective_target_duration / 2.0)
    if fade_seconds != safe_fade_seconds:
        diagnostics.append(f"Fade seconds ({fade_seconds:.2f}s) clamped to {safe_fade_seconds:.2f}s to prevent fade-out start from being negative.")

    _run(
        [
            "ffmpeg", "-y",
            "-i", str(video_p),
            *ffmpeg_music_input_args,
            "-map", "0:v",  # Map video stream from first input (video_p)
            "-map", "1:a?", # Map audio stream from second input (music_p), if it exists
            "-c:v", "libx264",
            "-c:a", "aac",
            "-shortest", # Finish encoding when the shortest input stream ends (video)
            "-t", str(effective_target_duration), # Explicitly set output duration
            "-af", f"afade=t=in:st=0:d={safe_fade_seconds},afade=t=out:st={effective_target_duration - safe_fade_seconds}:d={safe_fade_seconds}",
            str(output_p),
        ],
        f"ffmpeg music editor failed for {video_p} and {music_p}",
    )

    # Generate section_markers
    section_markers = []
    section_duration = effective_target_duration / section_count
    for i in range(section_count):
        start_seconds = i * section_duration
        end_seconds = (i + 1) * section_duration
        section_markers.append({
            "index": i,
            "start_seconds": round(start_seconds, 3),
            "end_seconds": round(end_seconds, 3),
            "label": f"Section {i + 1}",
        })

    # Determine edit_decisions
    edit_decision_type = "looped" if music_duration < effective_target_duration else "trimmed"
    edit_decisions = {
        "type": edit_decision_type,
        "fade_in_duration_seconds": safe_fade_seconds,
        "fade_out_duration_seconds": safe_fade_seconds,
    }

    metadata = {
        "schema_version": 1,
        "effect": "resolve21_ai_music_editor",
        "rendered_at": datetime.now(timezone.utc).isoformat(),
        "video_path": str(video_p),
        "music_path": str(music_p),
        "output_path": str(output_p),
        "metadata_path": str(metadata_path),
        "target_duration_seconds": effective_target_duration,
        "video_duration_seconds": video_duration,
        "music_duration_seconds": music_duration,
        "track_id": track_id,
        "section_count": section_count,
        "fade_seconds": safe_fade_seconds,
        "diagnostics": diagnostics,
        "section_markers": section_markers,
        "edit_decisions": edit_decisions,
        "review_hints": [
            "Confirm music fits the video length and mood.",
            "Check for smooth fades at the beginning and end.",
            "Review section markers for potential edit points."
        ],
    }
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return str(output_p)


def _run(cmd: list[str], message: str) -> str:
    """Helper to run a shell command and raise an error on failure."""
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    combined = "\n".join(part for part in (proc.stdout, proc.stderr) if part)
    if proc.returncode != 0:
        raise RuntimeError(f"{message}: {combined[-800:]}")
    return combined


def _probe_duration(input_path: Path, is_audio: bool = False) -> float:
    """Probe media file duration using ffprobe."""
    stream_type = "a" if is_audio else "v"
    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", stream_type,
        "-show_entries", "format=duration",
        "-of", "json",
        str(input_path)
    ]
    proc = subprocess.run(cmd, text=True, capture_output=True)
    if proc.returncode != 0:
        return 0.0
    try:
        payload = json.loads(proc.stdout or "{}")
        duration = float((payload.get("format") or {}).get("duration") or 0.0)
        return max(duration, 0.0)
    except Exception:
        return 0.0


__all__ = ["MusicEditorResult", "render_ai_music_editor_plan"]
