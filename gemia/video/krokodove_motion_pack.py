"""Krokodove/Fusion-style reusable motion graphics pack for real clips."""
from __future__ import annotations

import json
import math
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import cv2
import numpy as np


KROKODOVE_NODE_PRESETS = {
    "orbit_grid": [
        {"node": "Background", "params": {"alpha": 0.0}},
        {"node": "GridWarp", "params": {"frequency": 12, "drift": "sin"}},
        {"node": "LightRays", "params": {"gain": 0.28}},
        {"node": "Merge", "params": {"blend": "screen"}},
    ],
    "radial_echo": [
        {"node": "Duplicate", "params": {"copies": 3}},
        {"node": "Transform", "params": {"rotation": "time*24"}},
        {"node": "Glow", "params": {"threshold": 0.62}},
        {"node": "Merge", "params": {"blend": "add"}},
    ],
    "scanline_caption": [
        {"node": "FastNoise", "params": {"scale": 18}},
        {"node": "Scanline", "params": {"spacing": 5}},
        {"node": "TextPlus", "params": {"tracking": 1.2}},
        {"node": "Merge", "params": {"blend": "normal"}},
    ],
}


@dataclass(frozen=True)
class KrokodoveMotionPackResult:
    output_path: str
    metadata_path: str
    preset: str


def render_krokodove_motion_pack(
    input_path: str,
    output_path: str,
    *,
    preset: str = "orbit_grid",
    title: str = "Lumeri",
    intensity: float = 0.55,
    max_seconds: float | None = None,
) -> str:
    """Render a real clip with a reusable Fusion/Krokodove-style node preset overlay."""
    source = Path(input_path).expanduser().resolve()
    output = Path(output_path).expanduser().resolve()
    if not source.exists():
        raise FileNotFoundError(f"Krokodove input does not exist: {source}")
    normalized_preset = str(preset or "orbit_grid").strip().lower()
    if normalized_preset not in KROKODOVE_NODE_PRESETS:
        raise ValueError(f"Unknown Krokodove preset: {preset}")
    output.parent.mkdir(parents=True, exist_ok=True)
    cap = cv2.VideoCapture(str(source))
    try:
        if not cap.isOpened():
            raise ValueError(f"Krokodove input is not readable: {source}")
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
        fps = float(cap.get(cv2.CAP_PROP_FPS) or 24.0) or 24.0
        if width <= 0 or height <= 0:
            raise ValueError("Krokodove input has invalid dimensions.")
        source_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        limit = source_frames
        if max_seconds is not None:
            limit = min(limit or int(fps * max_seconds), max(1, int(float(max_seconds) * fps)))
        if limit <= 0:
            limit = int(fps)
        with tempfile.TemporaryDirectory(prefix="gemia-krokodove-") as tmp:
            silent = Path(tmp) / "silent.mp4"
            writer = cv2.VideoWriter(str(silent), cv2.VideoWriter_fourcc(*"mp4v"), max(fps, 1.0), (width, height))
            if not writer.isOpened():
                raise RuntimeError(f"Could not open Krokodove writer: {silent}")
            rendered = 0
            while rendered < limit:
                ok, frame = cap.read()
                if not ok or frame is None:
                    break
                if (frame.shape[1], frame.shape[0]) != (width, height):
                    frame = cv2.resize(frame, (width, height), interpolation=cv2.INTER_AREA)
                progress = rendered / max(limit - 1, 1)
                writer.write(_apply_overlay(frame, preset=normalized_preset, title=title, progress=progress, intensity=float(intensity)))
                rendered += 1
            writer.release()
            if rendered <= 0:
                raise RuntimeError("Krokodove render produced no frames.")
            audio_copied = _mux_audio_if_present(source, silent, output)
    finally:
        cap.release()
    metadata = {
        "schema_version": 1,
        "effect": "resolve21_fusion_krokodove_motion_pack",
        "rendered_at": datetime.now(timezone.utc).isoformat(),
        "source_path": str(source),
        "output_path": str(output),
        "preset": normalized_preset,
        "title": title,
        "intensity": float(intensity),
        "node_preset": KROKODOVE_NODE_PRESETS[normalized_preset],
        "rendered_frames": rendered,
        "audio_copied": audio_copied,
        "review_hints": [
            "confirm the preset node stack is reusable in later Fusion-style scenes",
            "verify the overlay is visible without obscuring source action",
            "check audio continuity when source audio is present",
        ],
    }
    metadata_path = output.with_suffix(".krokodove_motion_pack.json")
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return str(output)


def _apply_overlay(frame: np.ndarray, *, preset: str, title: str, progress: float, intensity: float) -> np.ndarray:
    h, w = frame.shape[:2]
    overlay = frame.copy()
    amount = min(max(float(intensity), 0.0), 1.0)
    if preset == "orbit_grid":
        spacing = max(18, w // 12)
        phase = int(progress * spacing * 2)
        color = (40, 230, 255)
        for x in range(-spacing, w + spacing, spacing):
            cv2.line(overlay, (x + phase % spacing, 0), (x - spacing // 2, h), color, 1)
        for y in range(0, h, max(14, h // 9)):
            cv2.line(overlay, (0, y), (w, y), (255, 120, 40), 1)
        radius = int(min(w, h) * (0.12 + 0.04 * math.sin(progress * math.tau)))
        cv2.circle(overlay, (int(w * 0.78), int(h * 0.28)), radius, (255, 255, 255), 2)
    elif preset == "radial_echo":
        center = (w // 2, h // 2)
        for i in range(3):
            radius = int(min(w, h) * (0.18 + progress * 0.32 + i * 0.08)) % max(min(w, h), 1)
            cv2.circle(overlay, center, max(radius, 8), (180, 80 + i * 50, 255), 2)
        rot = cv2.getRotationMatrix2D(center, progress * 12.0, 1.0)
        echo = cv2.warpAffine(frame, rot, (w, h))
        overlay = cv2.addWeighted(overlay, 0.72, echo, 0.28, 0)
    else:
        for y in range(0, h, 5):
            cv2.line(overlay, (0, y), (w, y), (25, 25, 25), 1)
        band_h = max(34, h // 6)
        y0 = h - band_h - max(8, h // 28)
        cv2.rectangle(overlay, (0, y0), (w, y0 + band_h), (10, 10, 10), -1)
        cv2.putText(overlay, title[:42], (max(12, w // 24), y0 + band_h // 2 + 8), cv2.FONT_HERSHEY_SIMPLEX, max(0.55, w / 900), (245, 245, 245), 2, cv2.LINE_AA)
    return cv2.addWeighted(frame, 1.0 - amount * 0.45, overlay, amount * 0.45, 0)


def _mux_audio_if_present(source: Path, silent: Path, output: Path) -> bool:
    if not _has_audio(source):
        shutil.copy2(silent, output)
        return False
    proc = subprocess.run([
        "ffmpeg", "-y", "-i", str(silent), "-i", str(source), "-map", "0:v:0", "-map", "1:a:0",
        "-c:v", "copy", "-c:a", "aac", "-shortest", str(output),
    ], capture_output=True, text=True)
    if proc.returncode != 0:
        shutil.copy2(silent, output)
        return False
    return True


def _has_audio(source: Path) -> bool:
    proc = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index", "-of", "csv=p=0", str(source)], capture_output=True, text=True)
    return proc.returncode == 0 and bool(proc.stdout.strip())


__all__ = ["KROKODOVE_NODE_PRESETS", "KrokodoveMotionPackResult", "render_krokodove_motion_pack"]
