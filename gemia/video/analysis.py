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


def track_point(video_path: str, *, initial_point: tuple[float, float]) -> list[tuple[float, float]]:
    """Track a single point across all frames using optical flow.

    Args:
        video_path: Input video path.
        initial_point: (x, y) pixel coordinates in the first frame.

    Returns:
        List of (x, y) coordinates, one per frame.
    """
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise FileNotFoundError(f"Cannot open video: {video_path}")

    ret, frame = cap.read()
    if not ret:
        cap.release()
        return []

    prev_gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    pt = np.array([[initial_point]], dtype=np.float32)
    track: list[tuple[float, float]] = [(float(pt[0, 0, 0]), float(pt[0, 0, 1]))]

    lk_params = dict(winSize=(15, 15), maxLevel=2,
                     criteria=(cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 10, 0.03))

    while True:
        ret, frame = cap.read()
        if not ret:
            break
        curr_gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        new_pt, status, _ = cv2.calcOpticalFlowPyrLK(prev_gray, curr_gray, pt, None, **lk_params)
        if status is not None and status[0, 0] == 1:
            pt = new_pt
        track.append((float(pt[0, 0, 0]), float(pt[0, 0, 1])))
        prev_gray = curr_gray

    cap.release()
    return track


def track_plane(video_path: str, *, initial_quad: list[tuple[float, float]]) -> list[list[tuple[float, float]]]:
    """Track a planar quad region across all frames using homography.

    Args:
        video_path: Input video path.
        initial_quad: List of 4 (x, y) corner points in the first frame.

    Returns:
        List of quads (each a list of 4 (x,y) points), one per frame.
    """
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise FileNotFoundError(f"Cannot open video: {video_path}")

    ret, frame = cap.read()
    if not ret:
        cap.release()
        return []

    prev_gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    quad = np.array(initial_quad, dtype=np.float32)
    quads: list[list[tuple[float, float]]] = [[(float(p[0]), float(p[1])) for p in quad]]

    lk_params = dict(winSize=(15, 15), maxLevel=2,
                     criteria=(cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 10, 0.03))

    while True:
        ret, frame = cap.read()
        if not ret:
            break
        curr_gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        pts = quad.reshape(-1, 1, 2)
        new_pts, status, _ = cv2.calcOpticalFlowPyrLK(prev_gray, curr_gray, pts, None, **lk_params)
        good_old = pts[status.ravel() == 1]
        good_new = new_pts[status.ravel() == 1]

        if len(good_old) >= 4:
            H, mask = cv2.findHomography(good_old, good_new, cv2.RANSAC)
            if H is not None:
                new_quad = cv2.perspectiveTransform(quad.reshape(-1, 1, 2), H)
                quad = new_quad.reshape(-1, 2)
            else:
                valid = status.ravel() == 1
                quad[valid] = new_pts[valid].reshape(-1, 2)
        else:
            valid = status.ravel() == 1
            if valid.any():
                quad[valid] = new_pts[valid].reshape(-1, 2)

        quads.append([(float(p[0]), float(p[1])) for p in quad])
        prev_gray = curr_gray

    cap.release()
    return quads
