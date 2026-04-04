"""Frame operations: extract_frames, frames_to_video, apply_picture_op_to_video."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Callable

import cv2
import numpy as np

from gemia.primitives_common import Image, ensure_float32, to_uint8


def extract_frames(path: str, *, fps: float | None = None,
                   count: int | None = None) -> list[Image]:
    """Extract frames from a video as float32 BGR ndarrays.

    Specify *fps* (frames per second to extract) **or** *count* (total
    frames, evenly spaced).  If neither is given, all frames are extracted.

    Args:
        path: Path to input video.
        fps: Target extraction rate.
        count: Desired number of frames.

    Returns:
        List of float32 [0, 1] BGR images.
    """
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        raise FileNotFoundError(f"Cannot open video: {path}")

    video_fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

    if count is not None and total_frames > 0:
        indices = np.linspace(0, total_frames - 1, count, dtype=int)
    elif fps is not None:
        step = max(int(round(video_fps / fps)), 1)
        indices = np.arange(0, total_frames, step, dtype=int)
    else:
        indices = None  # all frames

    frames: list[Image] = []
    idx = 0
    while True:
        ret, frame = cap.read()
        if not ret:
            break
        if indices is None or idx in indices:
            frames.append(ensure_float32(frame))
        idx += 1
    cap.release()
    return frames


def frames_to_video(frames: list[Image], *, output_path: str,
                    fps: float = 30.0, codec: str = "mp4v") -> str:
    """Encode a list of frames into a video file.

    Args:
        frames: List of float32 [0, 1] BGR images (all same shape).
        output_path: Destination file path (e.g. ``'out.mp4'``).
        fps: Output frame rate.
        codec: FourCC codec string (default ``'mp4v'``).

    Returns:
        The *output_path* for convenience.
    """
    if not frames:
        raise ValueError("No frames to encode.")
    h, w = frames[0].shape[:2]
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    fourcc = cv2.VideoWriter_fourcc(*codec)
    writer = cv2.VideoWriter(output_path, fourcc, fps, (w, h))
    for f in frames:
        writer.write(to_uint8(f))
    writer.release()
    return output_path


def apply_picture_op_to_video(input_path: str, output_path: str, *,
                               op: Callable[[Image], Image],
                               fps: float | None = None) -> str:
    """Apply a picture primitive to every frame of a video.

    This is the core bridge between ``gemia.picture`` and ``gemia.video``.
    The audio track is preserved by re-muxing with ffmpeg after the
    frame-level processing.

    Args:
        input_path: Source video.
        output_path: Destination video.
        op: A callable ``Image -> Image`` (any gemia.picture function or
            lambda wrapping one).
        fps: If set, resample to this frame rate during read/write.

    Returns:
        The *output_path*.

    Example::

        from gemia.picture.color import color_grade
        apply_picture_op_to_video(
            "in.mp4", "out.mp4",
            op=lambda f: color_grade(f, preset="cyberpunk"),
        )
    """
    cap = cv2.VideoCapture(input_path)
    if not cap.isOpened():
        raise FileNotFoundError(f"Cannot open video: {input_path}")

    video_fps = fps or cap.get(cv2.CAP_PROP_FPS) or 30.0
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))

    out_tmp = output_path + ".tmp_noaudio.mp4"
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(out_tmp, fourcc, video_fps, (w, h))

    while True:
        ret, frame = cap.read()
        if not ret:
            break
        processed = op(ensure_float32(frame))
        out_frame = to_uint8(processed)
        # Ensure shape matches (op might change size)
        if out_frame.shape[:2] != (h, w):
            out_frame = cv2.resize(out_frame, (w, h))
        writer.write(out_frame)

    cap.release()
    writer.release()

    # Re-mux: take processed video + original audio
    _remux_with_audio(out_tmp, input_path, output_path)
    Path(out_tmp).unlink(missing_ok=True)
    return output_path


def _remux_with_audio(video_path: str, audio_source: str, output_path: str) -> None:
    """Combine processed video with audio from the original file."""
    proc = subprocess.run(
        [
            "ffmpeg", "-y",
            "-i", video_path,
            "-i", audio_source,
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            "-map", "0:v:0", "-map", "1:a:0?",
            "-shortest",
            output_path,
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        # If audio extraction fails (no audio track), just re-encode video
        subprocess.run(
            ["ffmpeg", "-y", "-i", video_path,
             "-c:v", "libx264", "-pix_fmt", "yuv420p", output_path],
            capture_output=True,
            check=True,
        )
