"""Deterministic media checks used by formal production evidence.

These checks intentionally avoid model judgement. They sample decoded frames
through ffmpeg and distinguish a genuinely changing video stream from a still
image wrapped in a video container. Creative continuity, text quality and
watermark review remain separate human/vision evidence.
"""
from __future__ import annotations

import hashlib
import math
import subprocess
from pathlib import Path
from typing import Any

from gemia.compat import ffmpeg_path


def inspect_video_motion(
    path: str | Path,
    *,
    sample_fps: float = 2.0,
    sample_width: int = 64,
    sample_height: int = 36,
    timeout_sec: int = 90,
) -> dict[str, Any]:
    """Decode small grayscale samples and report observable frame movement."""

    source = Path(path).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"video source does not exist: {source}")
    fps = float(sample_fps)
    width, height = int(sample_width), int(sample_height)
    if not math.isfinite(fps) or fps <= 0:
        raise ValueError("sample_fps must be finite and > 0")
    if width <= 0 or height <= 0:
        raise ValueError("sample dimensions must be > 0")
    frame_size = width * height
    command = [
        ffmpeg_path(),
        "-v",
        "error",
        "-i",
        str(source),
        "-vf",
        f"fps={fps:g},scale={width}:{height}:flags=area,format=gray",
        "-an",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "gray",
        "pipe:1",
    ]
    try:
        proc = subprocess.run(
            command,
            capture_output=True,
            timeout=max(1, int(timeout_sec)),
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"motion inspection timed out after {exc.timeout}s") from exc
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", errors="replace")[-1200:]
        raise RuntimeError(f"motion inspection decode failed: {detail}")
    payload = proc.stdout
    frame_count = len(payload) // frame_size
    frames = [
        payload[index * frame_size : (index + 1) * frame_size]
        for index in range(frame_count)
    ]
    if frame_count < 2:
        return {
            "status": "failed",
            "reason": "fewer than two decoded sample frames",
            "sample_fps": fps,
            "sample_frame_count": frame_count,
            "real_motion_verified": False,
        }

    differences: list[float] = []
    for previous, current in zip(frames, frames[1:]):
        total = sum(abs(left - right) for left, right in zip(previous, current))
        differences.append(total / frame_size)
    sorted_differences = sorted(differences)
    p90_index = min(
        len(sorted_differences) - 1,
        max(0, int(math.ceil(len(sorted_differences) * 0.90)) - 1),
    )
    p90 = sorted_differences[p90_index]
    moving_pairs = sum(1 for value in differences if value >= 0.8)
    motion_pair_ratio = moving_pairs / len(differences)
    unique_frames = len({hashlib.sha256(frame).digest() for frame in frames})
    black_frames = sum(1 for frame in frames if sum(frame) / frame_size <= 8.0)
    black_frame_ratio = black_frames / frame_count
    # Both breadth (enough changing pairs) and amplitude are required. A
    # timestamp overlay changing on an otherwise frozen still is not enough.
    verified = unique_frames >= 3 and motion_pair_ratio >= 0.15 and p90 >= 1.2
    return {
        "status": "passed" if verified else "failed",
        "sample_fps": fps,
        "sample_frame_count": frame_count,
        "unique_frame_count": unique_frames,
        "motion_pair_ratio": round(motion_pair_ratio, 6),
        "mean_abs_frame_delta": round(sum(differences) / len(differences), 6),
        "p90_abs_frame_delta": round(p90, 6),
        "black_frame_ratio": round(black_frame_ratio, 6),
        "real_motion_verified": verified,
    }


__all__ = ["inspect_video_motion"]
