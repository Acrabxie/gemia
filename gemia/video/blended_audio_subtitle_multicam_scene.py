"""Resolve-style fused audio, subtitle, and multicam scene rendering."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from gemia.video.animated_subtitles import render_ai_animated_subtitles_plan
from gemia.video.dialogue_matcher import render_dialogue_matcher_plan
from gemia.video.multicam_smartswitch import render_ai_multicam_smartswitch_plan
from gemia.video.music_editor import render_ai_music_editor_plan


@dataclass(frozen=True)
class BlendedAudioSubtitleMulticamSceneResult:
    output_path: str
    metadata_path: str


def render_blended_audio_subtitle_multicam_scene(
    primary_video_path: str,
    secondary_video_path: str,
    music_path: str,
    output_path: str,
    *,
    transcript: str = "Review the cut",
    word_timings: list[dict[str, Any]] | None = None,
    speaker_segments: list[dict[str, Any]] | None = None,
    target_duration_seconds: float | None = 1.6,
    font_size: int = 42,
) -> str:
    """Compose dialogue matching, music fitting, word subtitles, and multicam switching."""
    primary = Path(primary_video_path).expanduser().resolve()
    secondary = Path(secondary_video_path).expanduser().resolve()
    music = Path(music_path).expanduser().resolve()
    output = Path(output_path).expanduser().resolve()
    for label, path in (("primary video", primary), ("secondary video", secondary), ("music", music)):
        if not path.exists():
            raise FileNotFoundError(f"Blended scene {label} does not exist: {path}")
    if target_duration_seconds is not None and target_duration_seconds <= 0:
        raise ValueError("target_duration_seconds must be greater than 0")

    output.parent.mkdir(parents=True, exist_ok=True)
    work_dir = output.parent / f"{output.stem}_blended_steps"
    work_dir.mkdir(parents=True, exist_ok=True)
    duration = float(target_duration_seconds) if target_duration_seconds is not None else None
    segments = speaker_segments or _default_speaker_segments(duration or 1.6)
    words = word_timings or _default_word_timings(transcript, duration or 1.6)

    dialogue_output = work_dir / "01_dialogue_match.mp4"
    render_dialogue_matcher_plan(str(primary), str(secondary), str(dialogue_output), sample_seconds=min(duration or 1.0, 1.5))

    multicam_output = work_dir / "02_multicam.mp4"
    render_ai_multicam_smartswitch_plan(
        [str(primary), str(secondary)],
        str(multicam_output),
        speaker_segments=segments,
        angle_labels=["primary dialogue", "secondary reaction"],
        clip_duration_seconds=min(duration or 0.8, 0.8),
    )

    subtitle_output = work_dir / "03_animated_subtitles.mp4"
    render_ai_animated_subtitles_plan(
        str(multicam_output),
        str(subtitle_output),
        word_timings=words,
        transcript=transcript,
        font_size=font_size,
        target_duration_seconds=duration,
    )

    render_ai_music_editor_plan(
        str(subtitle_output),
        str(music),
        str(output),
        target_duration_seconds=duration,
        fade_seconds=0.1,
    )

    metadata_path = output.with_suffix(".blended_audio_subtitle_multicam.json")
    metadata = {
        "schema_version": 1,
        "effect": "resolve21_blended_audio_subtitle_multicam_scene",
        "rendered_at": datetime.now(timezone.utc).isoformat(),
        "source_paths": {
            "primary_video": str(primary),
            "secondary_video": str(secondary),
            "music": str(music),
        },
        "output_path": str(output),
        "metadata_path": str(metadata_path),
        "target_duration_seconds": duration,
        "pipeline_steps": [
            _step("dialogue_matcher", dialogue_output, ".dialogue_matcher.json"),
            _step("multicam_smartswitch", multicam_output, ".multicam_smartswitch.json"),
            _step("animated_subtitles", subtitle_output, ".animated_subtitles.json"),
            _step("music_editor", output, ".music_editor.json"),
        ],
        "speaker_segments": segments,
        "word_timings": words,
        "diagnostics": [],
        "review_hints": [
            "confirm the dialogue analysis sidecar is present before final audio replacement",
            "check speaker-aware angle switches against subtitle timing",
            "verify the fitted music bed does not obscure dialogue intent",
            "review the final clip twice with real footage before marking the scene complete",
        ],
    }
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return str(output)


def _step(name: str, output: Path, metadata_suffix: str) -> dict[str, Any]:
    metadata_path = output.with_suffix(metadata_suffix)
    return {
        "name": name,
        "output_path": str(output),
        "metadata_path": str(metadata_path),
        "output_exists": output.exists(),
        "metadata_exists": metadata_path.exists(),
    }


def _default_speaker_segments(duration: float) -> list[dict[str, Any]]:
    midpoint = max(duration / 2.0, 0.2)
    return [
        {"speaker": "Host", "start_seconds": 0.0, "end_seconds": round(midpoint, 3)},
        {"speaker": "Guest", "start_seconds": round(midpoint, 3), "end_seconds": round(duration, 3)},
    ]


def _default_word_timings(transcript: str, duration: float) -> list[dict[str, Any]]:
    words = [part for part in str(transcript).split() if part.strip()] or ["Review", "the", "cut"]
    slot = duration / max(len(words), 1)
    timings = []
    for index, word in enumerate(words):
        start = index * slot
        timings.append(
            {
                "word": word,
                "start_seconds": round(start, 3),
                "end_seconds": round(min(duration, start + max(slot * 0.8, 0.12)), 3),
            }
        )
    return timings


__all__ = ["BlendedAudioSubtitleMulticamSceneResult", "render_blended_audio_subtitle_multicam_scene"]
