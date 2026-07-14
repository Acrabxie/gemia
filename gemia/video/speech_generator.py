"""Resolve-style AI Speech Generator request artifacts and dry-run voiceovers."""
from __future__ import annotations

import hashlib
import json
import math
import subprocess
import wave
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class SpeechGeneratorResult:
    output_path: str
    audio_path: str
    metadata_path: str


def render_ai_speech_generator_plan(
    input_path: str,
    output_path: str,
    *,
    script: str,
    voice: str = "narrator",
    performance: str = "clear documentary",
    language: str = "en-US",
    dry_run: bool = True,
    track_id: str = "A2",
    start_seconds: float = 0.0,
    target_duration_seconds: float | None = None,
    duck_original_db: float = -14.0,
    sample_rate: int = 24000,
) -> str:
    """Attach a repeatable AI Speech Generator voiceover asset to a video.

    The current implementation always writes a deterministic dry-run WAV plus
    the exact request/timing metadata a remote speech model would need. This
    keeps planning and review reproducible when real speech quota is absent.
    """
    source = Path(input_path).expanduser().resolve()
    output = Path(output_path).expanduser().resolve()
    if not source.exists():
        raise FileNotFoundError(f"Speech Generator input does not exist: {source}")
    clean_script = " ".join(str(script).split())
    if not clean_script:
        raise ValueError("script must contain at least one spoken word")
    if dry_run is False:
        raise ValueError("dry_run=False is not supported. This implementation only generates deterministic dry-run speech.")

    output.parent.mkdir(parents=True, exist_ok=True)
    source_duration = _probe_duration(source)
    words = _word_timings(clean_script, target_duration_seconds, source_duration)
    speech_duration = max((words[-1]["end_seconds"] if words else 0.0) + 0.2, 0.5)
    audio_path = output.with_suffix(".speech_generator.wav")
    _write_dry_run_voiceover(audio_path, words, voice=voice, performance=performance, sample_rate=sample_rate)
    source_has_audio = _has_audio_stream(source)
    _attach_voiceover(source, audio_path, output, has_source_audio=source_has_audio, duck_original_db=duck_original_db, start_seconds=start_seconds)

    metadata = {
        "schema_version": 1,
        "effect": "resolve21_ai_speech_generator",
        "rendered_at": datetime.now(timezone.utc).isoformat(),
        "source_path": str(source),
        "output_path": str(output),
        "voiceover_audio_path": str(audio_path),
        "generation": {
            "backend": "deterministic_dry_run",
            "dry_run": bool(dry_run),
            "script_sha1": hashlib.sha1(clean_script.encode("utf-8")).hexdigest(),
            "model_request_ready": True,
            "requested_model_role": "speech_generator",
        },
        "voice": {"name": voice, "language": language, "performance": performance},
        "timeline_attachment": {
            "track_id": track_id,
            "start_seconds": round(max(start_seconds, 0.0), 3),
            "end_seconds": round(max(start_seconds, 0.0) + speech_duration, 3),
            "duck_original_db": duck_original_db,
            "source_had_audio": source_has_audio,
        },
        "timing": {
            "source_duration_seconds": round(source_duration, 3),
            "speech_duration_seconds": round(speech_duration, 3),
            "word_count": len(words),
            "words": words,
        },
        "review_hints": [
            "confirm the A2 voiceover asset exists",
            "confirm word timing aligns with edit duration",
            "replace deterministic dry-run WAV with model output when speech generation is available",
        ],
    }
    output.with_suffix(".speech_generator.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return str(output)


def _word_timings(script: str, target_duration_seconds: float | None, source_duration_seconds: float) -> list[dict[str, Any]]:
    words = [word.strip() for word in script.split() if word.strip()]
    estimate = max(len(words) * 0.34, 0.8)
    if target_duration_seconds is not None:
        duration = max(float(target_duration_seconds), 0.5)
    elif source_duration_seconds > 0:
        duration = min(max(estimate, source_duration_seconds * 0.45), max(source_duration_seconds, estimate))
    else:
        duration = estimate
    slot = duration / max(len(words), 1)
    cursor = 0.0
    timings: list[dict[str, Any]] = []
    for index, word in enumerate(words):
        spoken = max(slot * 0.76, 0.12)
        timings.append({"index": index, "word": word, "start_seconds": round(cursor, 3), "end_seconds": round(cursor + spoken, 3)})
        cursor += slot
    return timings


def _write_dry_run_voiceover(path: Path, words: list[dict[str, Any]], *, voice: str, performance: str, sample_rate: int) -> None:
    seed = int(hashlib.sha1(f"{voice}|{performance}".encode("utf-8")).hexdigest()[:8], 16)
    total_seconds = max((words[-1]["end_seconds"] if words else 0.0) + 0.2, 0.5)
    total_samples = int(math.ceil(total_seconds * sample_rate))
    pcm = bytearray(total_samples * 2)
    for item in words:
        start = int(float(item["start_seconds"]) * sample_rate)
        end = min(total_samples, int(float(item["end_seconds"]) * sample_rate))
        if end <= start:
            continue
        word_seed = int(hashlib.sha1(str(item["word"]).encode("utf-8")).hexdigest()[:6], 16)
        freq = 175.0 + ((seed ^ word_seed) % 210)
        for sample_index in range(start, end):
            t = (sample_index - start) / sample_rate
            rel = (sample_index - start) / max(end - start, 1)
            envelope = min(rel * 8.0, (1.0 - rel) * 8.0, 1.0)
            value = int(10500 * envelope * math.sin(2.0 * math.pi * freq * t))
            offset = sample_index * 2
            pcm[offset:offset + 2] = int(value).to_bytes(2, "little", signed=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as fh:
        fh.setnchannels(1)
        fh.setsampwidth(2)
        fh.setframerate(sample_rate)
        fh.writeframes(bytes(pcm))


def _attach_voiceover(source: Path, audio_path: Path, output: Path, *, has_source_audio: bool, duck_original_db: float, start_seconds: float = 0.0) -> None:
    start_seconds = max(0.0, start_seconds)
    start_ms = int(start_seconds * 1000)
    if has_source_audio:
        duck = 10 ** (float(duck_original_db) / 20.0)
        cmd = [
            "ffmpeg", "-y", "-i", str(source), "-i", str(audio_path),
            "-filter_complex", f"[0:a]volume={duck:.5f}[base];[1:a]adelay={start_ms}|{start_ms}[delayed_voiceover];[base][delayed_voiceover]amix=inputs=2:duration=longest:dropout_transition=0[a]",
            "-map", "0:v:0", "-map", "[a]", "-c:v", "copy", "-c:a", "aac", str(output),
        ]
    else:
        cmd = [
            "ffmpeg", "-y", "-i", str(source), "-i", str(audio_path),
            "-filter_complex", f"[1:a]adelay={start_ms}|{start_ms}[a]",
            "-map", "0:v:0", "-map", "[a]", "-c:v", "copy", "-c:a", "aac", str(output),
        ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg speech attachment failed: {proc.stderr[-800:]}")


def _probe_duration(path: Path) -> float:
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True,
        text=True,
    )
    try:
        return max(float(proc.stdout.strip()), 0.0)
    except ValueError:
        return 0.0


def _has_audio_stream(path: Path) -> bool:
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index", "-of", "csv=p=0", str(path)],
        capture_output=True,
        text=True,
    )
    return proc.returncode == 0 and bool(proc.stdout.strip())


__all__ = ["SpeechGeneratorResult", "render_ai_speech_generator_plan"]
