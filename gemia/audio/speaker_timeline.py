"""Optional pyannote-backed speaker timeline extraction for Gemia."""
from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class SpeakerTimelineResult:
    metadata_path: str
    backend: str
    speaker_segments: list[dict[str, Any]]


def render_pyannote_speaker_timeline_backend(
    input_path: str,
    output_path: str | None = None,
    *,
    pipeline_model: str = "pyannote/speaker-diarization-community-1",
    auth_token: str | None = None,
    use_pyannote: str = "auto",
    num_speakers: int | None = None,
    min_speakers: int | None = None,
    max_speakers: int | None = None,
    min_segment_seconds: float = 0.25,
) -> str:
    """Write Resolve-style speaker timeline metadata from pyannote or a deterministic fallback."""
    source = Path(input_path).expanduser().resolve()
    if not source.exists():
        raise FileNotFoundError(f"Speaker timeline input does not exist: {source}")
    if min_segment_seconds <= 0:
        raise ValueError("min_segment_seconds must be greater than 0")
    mode = use_pyannote.strip().lower()
    if mode not in {"auto", "pyannote", "fallback"}:
        raise ValueError("use_pyannote must be one of: auto, pyannote, fallback")

    metadata_path = Path(output_path).expanduser().resolve() if output_path else source.with_suffix(".speaker_timeline.json")
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    diagnostics: list[str] = []
    pyannote_info: dict[str, Any] = {"model": pipeline_model, "requested": mode != "fallback"}

    segments: list[dict[str, Any]] = []
    backend = "ffmpeg_silence_fallback"
    if mode != "fallback":
        token = auth_token or os.environ.get("PYANNOTE_AUTH_TOKEN") or os.environ.get("HUGGINGFACE_TOKEN") or os.environ.get("HF_TOKEN")
        try:
            segments = _run_pyannote(source, pipeline_model, token, num_speakers, min_speakers, max_speakers)
            backend = "pyannote.audio"
        except Exception as exc:
            if mode == "pyannote":
                raise RuntimeError(f"pyannote speaker timeline failed: {exc}") from exc
            diagnostics.append(f"pyannote unavailable; used ffmpeg fallback: {exc}")

    if not segments:
        segments = _fallback_segments(source, min_segment_seconds, diagnostics)
    normalized = normalize_speaker_segments(segments, min_segment_seconds=min_segment_seconds)
    if not normalized:
        raise RuntimeError("No speaker timeline segments could be produced")

    metadata = {
        "schema_version": 1,
        "effect": "github_pyannote_audio_speaker_timeline_backend",
        "rendered_at": datetime.now(timezone.utc).isoformat(),
        "input_path": str(source),
        "metadata_path": str(metadata_path),
        "backend": backend,
        "pyannote": pyannote_info,
        "speaker_segments": normalized,
        "speaker_count": len({item["speaker"] for item in normalized}),
        "duration_seconds": _probe_duration(source),
        "diagnostics": diagnostics,
        "review_hints": [
            "feed speaker_segments directly into multicam SmartSwitch",
            "align subtitle word timing against speaker boundaries",
            "replace fallback output with pyannote.audio when model/token dependencies are available",
        ],
    }
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return str(metadata_path)


def normalize_speaker_segments(
    segments: list[dict[str, Any]],
    *,
    min_segment_seconds: float = 0.25,
) -> list[dict[str, Any]]:
    """Normalize raw diarization records into Gemia's speaker segment contract."""
    normalized: list[dict[str, Any]] = []
    for raw in sorted(segments, key=lambda item: float(item.get("start_seconds", item.get("start", 0.0)))):
        start = max(_float(raw.get("start_seconds", raw.get("start", 0.0))), 0.0)
        end = max(_float(raw.get("end_seconds", raw.get("end", start))), start)
        if end - start < min_segment_seconds:
            continue
        speaker = " ".join(str(raw.get("speaker") or raw.get("label") or "SPEAKER_00").split()) or "SPEAKER_00"
        payload = {
            "speaker": speaker,
            "start_seconds": round(start, 3),
            "end_seconds": round(end, 3),
            "duration_seconds": round(end - start, 3),
        }
        if raw.get("confidence") is not None:
            payload["confidence"] = round(_float(raw["confidence"]), 4)
        if raw.get("source") is not None:
            payload["source"] = str(raw["source"])
        normalized.append(payload)
    return _merge_adjacent(normalized)


def _run_pyannote(
    source: Path,
    model: str,
    token: str | None,
    num_speakers: int | None,
    min_speakers: int | None,
    max_speakers: int | None,
) -> list[dict[str, Any]]:
    os.environ.setdefault("PYANNOTE_METRICS_ENABLED", "0")
    from pyannote.audio import Pipeline  # type: ignore

    kwargs: dict[str, Any] = {}
    if token:
        kwargs["token"] = token
    pipeline = Pipeline.from_pretrained(model, **kwargs)
    call_kwargs = {k: v for k, v in {
        "num_speakers": num_speakers,
        "min_speakers": min_speakers,
        "max_speakers": max_speakers,
    }.items() if v is not None}
    output = pipeline(str(source), **call_kwargs)
    return _segments_from_pyannote_output(output)


def _segments_from_pyannote_output(output: Any) -> list[dict[str, Any]]:
    segments: list[dict[str, Any]] = []
    speaker_diarization = getattr(output, "speaker_diarization", None)
    if speaker_diarization is not None:
        for item in speaker_diarization:
            turn, speaker = item[0], item[-1]
            segments.append(_segment(turn.start, turn.end, str(speaker), "pyannote"))
        return segments
    if hasattr(output, "itertracks"):
        for turn, _, speaker in output.itertracks(yield_label=True):
            segments.append(_segment(turn.start, turn.end, str(speaker), "pyannote"))
    return segments


def _fallback_segments(source: Path, min_segment_seconds: float, diagnostics: list[str]) -> list[dict[str, Any]]:
    duration = _probe_duration(source)
    if duration <= 0:
        diagnostics.append("ffprobe could not determine input duration")
        return []
    if not _has_audio_stream(source):
        diagnostics.append("input has no audio stream; emitted timeline-wide diagnostic segment")
        return [_segment(0.0, duration, "SPEAKER_00", "no_audio_fallback", 0.25)]
    silences = _detect_silence(source)
    speech = _speech_ranges_from_silence(silences, duration, min_segment_seconds)
    if not speech:
        chunk = max(min(duration / 2.0, 1.5), min_segment_seconds)
        start = 0.0
        speech = []
        while start < duration:
            end = min(start + chunk, duration)
            if end - start >= min_segment_seconds:
                speech.append((start, end))
            start = end
    return [_segment(start, end, f"SPEAKER_{index % 2:02d}", "ffmpeg_silence_fallback", 0.55) for index, (start, end) in enumerate(speech)]


def _detect_silence(source: Path) -> list[tuple[float, float]]:
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", str(source), "-af", "silencedetect=n=-35dB:d=0.25", "-f", "null", "-"],
        capture_output=True,
        text=True,
        check=False,
    )
    text = "\n".join(part for part in (proc.stdout, proc.stderr) if part)
    starts = [float(value) for value in re.findall(r"silence_start:\s*([-+0-9.]+)", text)]
    ends = [float(value) for value in re.findall(r"silence_end:\s*([-+0-9.]+)", text)]
    return [(starts[i], ends[i]) for i in range(min(len(starts), len(ends))) if ends[i] > starts[i]]


def _speech_ranges_from_silence(silences: list[tuple[float, float]], duration: float, minimum: float) -> list[tuple[float, float]]:
    ranges: list[tuple[float, float]] = []
    cursor = 0.0
    for start, end in sorted(silences):
        if start - cursor >= minimum:
            ranges.append((cursor, min(start, duration)))
        cursor = max(cursor, end)
    if duration - cursor >= minimum:
        ranges.append((cursor, duration))
    return ranges


def _segment(start: float, end: float, speaker: str, source: str, confidence: float | None = None) -> dict[str, Any]:
    payload = {"speaker": speaker, "start_seconds": float(start), "end_seconds": float(end), "source": source}
    if confidence is not None:
        payload["confidence"] = confidence
    return payload


def _merge_adjacent(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    for item in segments:
        if merged and merged[-1]["speaker"] == item["speaker"] and item["start_seconds"] <= merged[-1]["end_seconds"] + 0.02:
            merged[-1]["end_seconds"] = item["end_seconds"]
            merged[-1]["duration_seconds"] = round(merged[-1]["end_seconds"] - merged[-1]["start_seconds"], 3)
        else:
            merged.append(dict(item))
    return merged


def _has_audio_stream(source: Path) -> bool:
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index", "-of", "csv=p=0", str(source)],
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode == 0 and bool(proc.stdout.strip())


def _probe_duration(source: Path) -> float:
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(source)],
        capture_output=True,
        text=True,
        check=False,
    )
    try:
        return round(max(float(proc.stdout.strip()), 0.0), 3)
    except ValueError:
        return 0.0


def _float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


__all__ = [
    "SpeakerTimelineResult",
    "normalize_speaker_segments",
    "render_pyannote_speaker_timeline_backend",
]
