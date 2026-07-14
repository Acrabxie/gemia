"""Timeline media assets for the desktop editor."""
from __future__ import annotations

import array
import hashlib
import json
import math
import mimetypes
import subprocess
from pathlib import Path
from typing import Any

VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
AUDIO_EXTENSIONS = {".flac", ".wav", ".mp3", ".m4a", ".aac"}
LOTTIE_EXTENSIONS = {".json", ".lottie"}
SUPPORTED_MEDIA_EXTENSIONS = VIDEO_EXTENSIONS | IMAGE_EXTENSIONS | AUDIO_EXTENSIONS | LOTTIE_EXTENSIONS


def media_kind_for_path(path: str | Path) -> str:
    suffix = Path(path).suffix.lower()
    if suffix in LOTTIE_EXTENSIONS:
        return "lottie"
    if suffix in IMAGE_EXTENSIONS:
        return "image"
    if suffix in AUDIO_EXTENSIONS:
        return "audio"
    return "video"


def _run_json(cmd: list[str]) -> dict[str, Any]:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "ffprobe failed")
    return json.loads(proc.stdout or "{}")


def probe_media(path: str) -> dict[str, Any]:
    """Return timeline-friendly media metadata from ffprobe."""
    media = Path(path)
    if not media.exists():
        raise FileNotFoundError(path)
    if media_kind_for_path(media) == "lottie":
        from gemia.video.lottie_renderer import select_lottie_renderer

        renderer = select_lottie_renderer()
        meta = renderer.get_metadata(str(media))
        fps = float(meta.get("fps") or 30.0)
        frames = max(int(meta.get("frames") or 1), 1)
        return {
            "duration": max(frames / max(fps, 1.0), 0.1),
            "media_kind": "lottie",
            "mime_type": "application/vnd.lottie+json" if media.suffix.lower() == ".json" else "application/dotlottie",
            "width": int(meta.get("width") or 0),
            "height": int(meta.get("height") or 0),
            "fps": fps,
            "frames": frames,
            "codec": "lottie",
            "audio_codec": "",
            "has_audio": False,
            "file_size_bytes": media.stat().st_size,
            "lottie_renderer": renderer.name,
        }
    payload = _run_json([
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration,size:stream=width,height,r_frame_rate,codec_name,codec_type",
        "-of",
        "json",
        str(media),
    ])
    streams = payload.get("streams") or []
    fmt = payload.get("format") or {}
    video = next((s for s in streams if s.get("codec_type") == "video"), {})
    audio = next((s for s in streams if s.get("codec_type") == "audio"), {})
    media_kind = media_kind_for_path(media)
    fps = 0.0
    fps_text = str(video.get("r_frame_rate") or "0/1")
    try:
        num, den = fps_text.split("/")
        fps = float(num) / max(float(den), 1.0)
    except Exception:
        fps = 0.0
    return {
        "duration": max(float(fmt.get("duration") or 0.0), 0.0),
        "media_kind": media_kind,
        "mime_type": mimetypes.guess_type(str(media))[0] or "application/octet-stream",
        "width": int(video.get("width") or 0),
        "height": int(video.get("height") or 0),
        "fps": fps,
        "codec": str(video.get("codec_name") or ""),
        "audio_codec": str(audio.get("codec_name") or ""),
        "has_audio": bool(audio),
        "file_size_bytes": int(fmt.get("size") or media.stat().st_size),
    }


def cache_key_for_path(path: str) -> str:
    media = Path(path)
    stat = media.stat()
    raw = f"{media.resolve()}:{stat.st_size}:{int(stat.st_mtime)}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def generate_timeline_thumbnails(
    path: str,
    output_dir: str | Path,
    *,
    count: int = 8,
    width: int = 176,
) -> list[str]:
    """Generate a small timeline thumbnail strip and return output paths."""
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    count = max(1, min(int(count), 24))
    meta = probe_media(path)
    if meta.get("media_kind") == "audio":
        return []
    if meta.get("media_kind") == "lottie":
        from gemia.video.lottie_renderer import save_lottie_frame_png

        dest = output / "thumb_000.png"
        if not dest.exists():
            save_lottie_frame_png(
                path,
                dest,
                width=width,
                height=max(1, round(width * max(int(meta.get("height") or 1), 1) / max(int(meta.get("width") or 1), 1))),
                frame_index=0,
            )
        return [str(dest)] if dest.exists() else []
    if meta.get("media_kind") == "image":
        dest = output / "thumb_000.jpg"
        if not dest.exists():
            proc = subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-i",
                    path,
                    "-frames:v",
                    "1",
                    "-vf",
                    f"scale={width}:-2",
                    "-q:v",
                    "4",
                    str(dest),
                ],
                capture_output=True,
                text=True,
            )
            if proc.returncode != 0:
                raise RuntimeError(proc.stderr.strip() or "thumbnail generation failed")
        return [str(dest)] if dest.exists() else []
    duration = max(float(meta.get("duration") or 0.0), 0.1)
    files: list[str] = []
    for index in range(count):
        timestamp = min(duration - 0.02, duration * (index + 0.5) / count)
        dest = output / f"thumb_{index:03d}.jpg"
        if not dest.exists():
            proc = subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-ss",
                    f"{max(timestamp, 0.0):.3f}",
                    "-i",
                    path,
                    "-frames:v",
                    "1",
                    "-vf",
                    f"scale={width}:-2",
                    "-q:v",
                    "4",
                    str(dest),
                ],
                capture_output=True,
                text=True,
            )
            if proc.returncode != 0 and index == 0:
                raise RuntimeError(proc.stderr.strip() or "thumbnail generation failed")
        if dest.exists():
            files.append(str(dest))
    return files


def extract_waveform_peaks(path: str, *, samples: int = 512, sample_rate: int = 8000) -> list[float]:
    """Extract normalized mono audio peaks for canvas waveform drawing.

    Videos without audio return a flat zero waveform so the timeline can render
    consistently without special cases.
    """
    samples = max(16, min(int(samples), 4096))
    try:
        meta = probe_media(path)
    except Exception:
        meta = {"has_audio": False}
    if not meta.get("has_audio"):
        return [0.0] * samples

    proc = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            path,
            "-map",
            "0:a:0",
            "-ac",
            "1",
            "-ar",
            str(sample_rate),
            "-f",
            "s16le",
            "-",
        ],
        capture_output=True,
    )
    if proc.returncode != 0 or not proc.stdout:
        return [0.0] * samples

    pcm = array.array("h")
    pcm.frombytes(proc.stdout)
    if not pcm:
        return [0.0] * samples
    step = max(1, math.ceil(len(pcm) / samples))
    peaks: list[float] = []
    max_i16 = 32768.0
    for start in range(0, len(pcm), step):
        window = pcm[start:start + step]
        peak = max((abs(value) for value in window), default=0) / max_i16
        peaks.append(round(min(1.0, peak), 4))
        if len(peaks) >= samples:
            break
    if len(peaks) < samples:
        peaks.extend([0.0] * (samples - len(peaks)))
    return peaks[:samples]
