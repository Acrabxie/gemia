"""Resolve-style AI Animated Subtitles preview rendering."""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class AnimatedSubtitlesResult:
    output_path: str
    metadata_path: str


def render_ai_animated_subtitles_plan(
    input_path: str,
    output_path: str,
    *,
    word_timings: list[dict[str, Any]] | None = None,
    transcript: str | None = None,
    preset: str = "karaoke_pop",
    track_id: str = "V2",
    font_size: int = 54,
    active_color: str = "yellow",
    inactive_color: str = "white",
    target_duration_seconds: float | None = None,
) -> str:
    """Render animated word subtitles from transcription timing metadata."""
    source = Path(input_path).expanduser().resolve()
    output = Path(output_path).expanduser().resolve()
    if not source.exists():
        raise FileNotFoundError(f"Animated Subtitles input does not exist: {source}")
    if font_size <= 0:
        raise ValueError("font_size must be greater than 0")

    output.parent.mkdir(parents=True, exist_ok=True)
    source_duration = _probe_duration(source)
    if source_duration <= 0:
        raise ValueError(f"Could not determine duration for video: {source}")
    effective_duration = min(float(target_duration_seconds or source_duration), source_duration)
    words = _normalize_words(word_timings, transcript, effective_duration)
    if not words:
        raise ValueError("word_timings or transcript must contain at least one word")

    subtitle_layers = _build_layer_plan(
        words,
        preset=preset,
        track_id=track_id,
        font_size=font_size,
        active_color=active_color,
        inactive_color=inactive_color,
    )
    render_mode = _render_preview(
        source,
        output,
        words,
        preset=preset,
        font_size=font_size,
        active_color=active_color,
        inactive_color=inactive_color,
        effective_duration=effective_duration,
    )
    metadata_path = output.with_suffix(".animated_subtitles.json")
    metadata = {
        "schema_version": 1,
        "effect": "resolve21_ai_animated_subtitles",
        "rendered_at": datetime.now(timezone.utc).isoformat(),
        "source_path": str(source),
        "output_path": str(output),
        "metadata_path": str(metadata_path),
        "track_id": track_id,
        "preset": preset,
        "render_mode": render_mode,
        "source_duration_seconds": round(source_duration, 3),
        "target_duration_seconds": round(effective_duration, 3),
        "word_count": len(words),
        "word_timings": words,
        "subtitle_layers": subtitle_layers,
        "review_hints": [
            "confirm active words animate at the spoken timing",
            "check subtitle placement does not cover the main subject",
            "replace deterministic transcript timing with model transcription when available",
        ],
    }
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return str(output)


def _normalize_words(
    word_timings: list[dict[str, Any]] | None,
    transcript: str | None,
    duration: float,
) -> list[dict[str, Any]]:
    if word_timings:
        normalized: list[dict[str, Any]] = []
        previous_end = 0.0
        for item in word_timings:
            word = " ".join(str(item.get("word", "")).split())
            if not word:
                continue
            start = max(float(item.get("start_seconds", previous_end)), 0.0)
            end = max(float(item.get("end_seconds", start + 0.25)), start + 0.05)
            start = min(start, duration)
            end = min(end, duration)
            if end <= start:
                continue
            normalized.append(
                {
                    "index": len(normalized),
                    "word": word,
                    "start_seconds": round(start, 3),
                    "end_seconds": round(end, 3),
                }
            )
            previous_end = end
        return normalized

    words = [" ".join(part.split()) for part in str(transcript or "").split() if part.strip()]
    if not words:
        return []
    slot = duration / max(len(words), 1)
    normalized = []
    for index, word in enumerate(words):
        start = index * slot
        end = min(duration, start + max(slot * 0.82, 0.12))
        normalized.append(
            {
                "index": index,
                "word": word,
                "start_seconds": round(start, 3),
                "end_seconds": round(end, 3),
            }
        )
    return normalized


def _build_layer_plan(
    words: list[dict[str, Any]],
    *,
    preset: str,
    track_id: str,
    font_size: int,
    active_color: str,
    inactive_color: str,
) -> list[dict[str, Any]]:
    layers = []
    for word in words:
        start = float(word["start_seconds"])
        end = float(word["end_seconds"])
        layers.append(
            {
                "id": f"{track_id}_word_{word['index']:03d}",
                "type": "text",
                "track_id": track_id,
                "text": word["word"],
                "start_seconds": start,
                "end_seconds": end,
                "style": {
                    "font_size": font_size,
                    "active_color": active_color,
                    "inactive_color": inactive_color,
                    "position": "bottom_center",
                    "preset": preset,
                },
                "keyframes": [
                    {"time_seconds": start, "opacity": 0.0, "scale": 0.92},
                    {"time_seconds": round(start + min(0.12, (end - start) / 2), 3), "opacity": 1.0, "scale": 1.08},
                    {"time_seconds": end, "opacity": 0.0, "scale": 1.0},
                ],
            }
        )
    return layers


def _render_preview(
    source: Path,
    output: Path,
    words: list[dict[str, Any]],
    *,
    preset: str,
    font_size: int,
    active_color: str,
    inactive_color: str,
    effective_duration: float,
) -> str:
    from PIL import Image, ImageDraw, ImageFont

    info = _probe_video_info(source)
    width = int(info["width"])
    height = int(info["height"])
    fps = float(info["fps"])
    active_rgb = _rgb(active_color)
    inactive_rgb = _rgb(inactive_color)
    with tempfile.TemporaryDirectory() as td:
        frame_pattern = str(Path(td) / "frame_%06d.png")
        _run(
            ["ffmpeg", "-y", "-t", str(effective_duration), "-i", str(source), "-vf", f"fps={fps}", frame_pattern],
            f"ffmpeg frame extraction failed for {source}",
        )
        frames = sorted(name for name in os.listdir(td) if name.endswith(".png"))
        for frame_index, name in enumerate(frames):
            t = frame_index / max(fps, 1.0)
            active = _active_word(words, t)
            if active is None:
                continue
            start = float(active["start_seconds"])
            end = float(active["end_seconds"])
            progress = min(max((t - start) / max(end - start, 0.001), 0.0), 1.0)
            scale = 1.0 + (0.12 * max(0.0, 1.0 - abs(progress - 0.25) * 4.0))
            y_offset = int(8 * max(0.0, 1.0 - progress) if preset != "quiet_captions" else 0)
            frame_path = Path(td) / name
            image = Image.open(frame_path).convert("RGBA")
            overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
            draw = ImageDraw.Draw(overlay)
            resolved_size = max(12, int(font_size * scale))
            try:
                from gemia.video.fonts import resolve_font_path
                _font_cfg = {"family": "Hiragino Sans GB", "path": "/System/Library/Fonts/Hiragino Sans GB.ttc", "weight": 600}
                _resolved = resolve_font_path(_font_cfg)
                font = ImageFont.truetype(_resolved or "/System/Library/Fonts/Helvetica.ttc", resolved_size)
            except Exception:
                try:
                    font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", resolved_size)
                except Exception:
                    font = ImageFont.load_default()
            text = str(active["word"])
            bbox = draw.textbbox((0, 0), text, font=font, stroke_width=3)
            text_w = bbox[2] - bbox[0]
            text_h = bbox[3] - bbox[1]
            x = (width - text_w) // 2
            y = height - 110 - y_offset
            draw.rounded_rectangle(
                [x - 18, y - 10, x + text_w + 18, y + text_h + 16],
                radius=10,
                fill=(0, 0, 0, 150),
            )
            fill = active_rgb if preset != "quiet_captions" else inactive_rgb
            draw.text((x, y), text, font=font, fill=(*fill, 255), stroke_width=3, stroke_fill=(0, 0, 0, 210))
            Image.alpha_composite(image, overlay).convert("RGB").save(frame_path)

        audio_path = Path(td) / "audio.aac"
        audio_ok = subprocess.run(
            ["ffmpeg", "-y", "-t", str(effective_duration), "-i", str(source), "-vn", "-c:a", "aac", str(audio_path)],
            capture_output=True,
            text=True,
            check=False,
        ).returncode == 0 and audio_path.exists() and audio_path.stat().st_size > 0
        cmd = ["ffmpeg", "-y", "-framerate", str(fps), "-i", frame_pattern]
        if audio_ok:
            cmd += ["-i", str(audio_path), "-c:a", "copy"]
        cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p", str(output)]
        _run(cmd, f"ffmpeg PIL subtitle preview encode failed for {source}")
    return "pil_word_layer_fallback"


def _active_word(words: list[dict[str, Any]], t: float) -> dict[str, Any] | None:
    for word in words:
        if float(word["start_seconds"]) <= t <= float(word["end_seconds"]):
            return word
    return None


def _rgb(color: str) -> tuple[int, int, int]:
    named = {
        "white": (255, 255, 255),
        "yellow": (255, 228, 80),
        "black": (0, 0, 0),
        "cyan": (80, 230, 255),
        "red": (255, 80, 80),
    }
    clean = color.strip().lower()
    if clean in named:
        return named[clean]
    if clean.startswith("#") and len(clean) == 7:
        return int(clean[1:3], 16), int(clean[3:5], 16), int(clean[5:7], 16)
    return named["white"]


def _probe_video_info(path: Path) -> dict[str, float]:
    proc = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height,r_frame_rate",
            "-of", "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"ffprobe stream info failed for {path}: {proc.stderr[-500:]}")
    stream = (json.loads(proc.stdout).get("streams") or [{}])[0]
    num, den = str(stream.get("r_frame_rate") or "30/1").split("/")
    fps = float(num) / max(float(den), 1.0)
    return {"width": float(stream.get("width") or 1280), "height": float(stream.get("height") or 720), "fps": fps}


def _run(cmd: list[str], message: str) -> str:
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    combined = "\n".join(part for part in (proc.stdout, proc.stderr) if part)
    if proc.returncode != 0:
        raise RuntimeError(f"{message}: {combined[-1000:]}")
    return combined


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


__all__ = ["AnimatedSubtitlesResult", "render_ai_animated_subtitles_plan"]
