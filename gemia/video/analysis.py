"""Video analysis: detect_scenes, get_metadata."""
from __future__ import annotations

import json
import subprocess

import cv2
import numpy as np

from gemia.primitives_common import ensure_float32


def get_metadata(path: str) -> dict:
    """Get video metadata via ffprobe.

    Args:
        path: Video file path.

    Returns:
        Dict with keys: ``duration`` (float seconds), ``width``, ``height``,
        ``fps`` (float), ``codec``, ``audio_codec``, ``file_size_bytes``.
    """
    proc = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration,size:stream=width,height,r_frame_rate,codec_name,codec_type",
            "-of", "json",
            path,
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {proc.stderr}")
    data = json.loads(proc.stdout)

    fmt = data.get("format", {})
    streams = data.get("streams", [])
    video_stream = next((s for s in streams if s.get("codec_type") == "video"), {})
    audio_stream = next((s for s in streams if s.get("codec_type") == "audio"), {})

    fps_str = video_stream.get("r_frame_rate", "30/1")
    try:
        num, den = fps_str.split("/")
        fps = float(num) / float(den)
    except Exception:
        fps = 30.0

    return {
        "duration": float(fmt.get("duration", 0)),
        "width": int(video_stream.get("width", 0)),
        "height": int(video_stream.get("height", 0)),
        "fps": fps,
        "codec": video_stream.get("codec_name", ""),
        "audio_codec": audio_stream.get("codec_name", ""),
        "file_size_bytes": int(fmt.get("size", 0)),
    }


def detect_scenes(path: str, *, threshold: float = 30.0) -> list[float]:
    """Detect scene changes by frame difference.

    Args:
        path: Video file path.
        threshold: Mean absolute difference threshold (0-255 scale).
            Lower values = more sensitive.

    Returns:
        List of timestamps (seconds) where scene changes occur.
    """
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        raise FileNotFoundError(f"Cannot open video: {path}")

    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    prev_frame = None
    scenes: list[float] = []
    idx = 0

    while True:
        ret, frame = cap.read()
        if not ret:
            break
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).astype(np.float32)
        if prev_frame is not None:
            diff = np.abs(gray - prev_frame).mean()
            if diff > threshold:
                scenes.append(idx / fps)
        prev_frame = gray
        idx += 1

    cap.release()
    return scenes
