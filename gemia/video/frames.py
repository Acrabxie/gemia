"""Frame operations: extract_frames, frames_to_video, apply_picture_op_to_video."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Callable

import cv2
import numpy as np

from gemia.primitives_common import Image, ensure_float32, to_uint8


def _warp_frame(frame: np.ndarray, flow: np.ndarray, t: float) -> np.ndarray:
    """Warp a frame by a fraction t of the given flow field."""
    h, w = frame.shape[:2]
    flow_t = flow * t
    map_x = np.tile(np.arange(w, dtype=np.float32), (h, 1)) + flow_t[..., 0]
    map_y = np.tile(np.arange(h, dtype=np.float32).reshape(h, 1), (1, w)) + flow_t[..., 1]
    return cv2.remap(frame, map_x, map_y, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)


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


def optical_flow_interpolate(frame_a: Image, frame_b: Image, *, steps: int = 1) -> list[Image]:
    """Generate intermediate frames between two frames using optical flow.

    Uses cv2.calcOpticalFlowFarneback.

    Args:
        frame_a: First frame, float32 BGR.
        frame_b: Second frame, float32 BGR.
        steps: Number of intermediate frames to generate.

    Returns:
        List of *steps* interpolated frames, float32 BGR.
    """
    frame_a = ensure_float32(frame_a)
    frame_b = ensure_float32(frame_b)

    a_u8 = to_uint8(frame_a)
    b_u8 = to_uint8(frame_b)
    a_gray = cv2.cvtColor(a_u8, cv2.COLOR_BGR2GRAY)
    b_gray = cv2.cvtColor(b_u8, cv2.COLOR_BGR2GRAY)

    flow_ab = cv2.calcOpticalFlowFarneback(
        a_gray, b_gray, None, 0.5, 3, 15, 3, 5, 1.2, 0
    )
    flow_ba = cv2.calcOpticalFlowFarneback(
        b_gray, a_gray, None, 0.5, 3, 15, 3, 5, 1.2, 0
    )

    results: list[Image] = []
    for i in range(1, steps + 1):
        t = i / (steps + 1)
        warped_a = _warp_frame(a_u8.astype(np.float32), flow_ab, t)
        warped_b = _warp_frame(b_u8.astype(np.float32), flow_ba, 1.0 - t)
        blended = warped_a * (1.0 - t) + warped_b * t
        results.append(np.clip(blended / 255.0, 0, 1).astype(np.float32))
    return results


def retime(video_path: str, output_path: str, *,
           speed_map: list[tuple[float, float]],
           method: str = "linear") -> str:
    """Retime a video with variable speed.

    Args:
        video_path: Input video path.
        output_path: Output video path.
        speed_map: List of (timestamp_sec, speed_factor) keyframes.
            speed_factor 1.0=normal, 2.0=2x fast, 0.5=slow.
        method: 'linear' (frame sampling) or 'optical_flow' (interpolated).

    Returns:
        output_path.
    """
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise FileNotFoundError(f"Cannot open video: {video_path}")

    src_fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))

    all_frames: list[np.ndarray] = []
    while True:
        ret, frame = cap.read()
        if not ret:
            break
        all_frames.append(frame)
    cap.release()

    if not all_frames:
        raise RuntimeError("No frames read from video.")

    speed_map_sorted = sorted(speed_map, key=lambda kv: kv[0])

    def get_speed(t: float) -> float:
        if not speed_map_sorted:
            return 1.0
        if t <= speed_map_sorted[0][0]:
            return speed_map_sorted[0][1]
        if t >= speed_map_sorted[-1][0]:
            return speed_map_sorted[-1][1]
        for j in range(len(speed_map_sorted) - 1):
            t0, s0 = speed_map_sorted[j]
            t1, s1 = speed_map_sorted[j + 1]
            if t0 <= t <= t1:
                frac = (t - t0) / (t1 - t0) if t1 > t0 else 0.0
                return s0 + (s1 - s0) * frac
        return 1.0

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(output_path, fourcc, src_fps, (w, h))

    src_time = 0.0
    out_time = 0.0
    dt = 1.0 / src_fps

    while src_time < len(all_frames) / src_fps:
        speed = get_speed(out_time)
        src_idx = int(src_time * src_fps)
        src_idx = min(src_idx, len(all_frames) - 1)

        if method == "optical_flow" and src_idx + 1 < len(all_frames):
            frac = src_time * src_fps - int(src_time * src_fps)
            if frac > 0.01:
                fa = ensure_float32(all_frames[src_idx])
                fb = ensure_float32(all_frames[src_idx + 1])
                interp = optical_flow_interpolate(fa, fb, steps=1)
                frame = to_uint8(interp[0])
            else:
                frame = all_frames[src_idx]
        else:
            frame = all_frames[src_idx]

        writer.write(frame)
        src_time += dt * speed
        out_time += dt

    writer.release()
    return output_path


def stabilize(video_path: str, output_path: str, *, smoothness: int = 30) -> str:
    """Stabilize a shaky video using affine motion estimation.

    Args:
        video_path: Input video path.
        output_path: Output video path.
        smoothness: Temporal smoothing window in frames. Higher = smoother.

    Returns:
        output_path.
    """
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise FileNotFoundError(f"Cannot open video: {video_path}")

    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))

    all_frames: list[np.ndarray] = []
    while True:
        ret, frame = cap.read()
        if not ret:
            break
        all_frames.append(frame)
    cap.release()

    if len(all_frames) < 2:
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        writer = cv2.VideoWriter(output_path, fourcc, fps, (w, h))
        for f in all_frames:
            writer.write(f)
        writer.release()
        return output_path

    transforms = []
    prev_gray = cv2.cvtColor(all_frames[0], cv2.COLOR_BGR2GRAY)

    for i in range(1, len(all_frames)):
        curr_gray = cv2.cvtColor(all_frames[i], cv2.COLOR_BGR2GRAY)
        prev_pts = cv2.goodFeaturesToTrack(
            prev_gray, maxCorners=200, qualityLevel=0.01, minDistance=30
        )
        if prev_pts is None or len(prev_pts) < 4:
            transforms.append(np.eye(2, 3, dtype=np.float64))
            prev_gray = curr_gray
            continue

        curr_pts, status, _ = cv2.calcOpticalFlowPyrLK(prev_gray, curr_gray, prev_pts, None)
        good_prev = prev_pts[status.ravel() == 1]
        good_curr = curr_pts[status.ravel() == 1]

        if len(good_prev) < 4:
            transforms.append(np.eye(2, 3, dtype=np.float64))
        else:
            M, _ = cv2.estimateAffinePartial2D(good_prev, good_curr)
            if M is None:
                M = np.eye(2, 3, dtype=np.float64)
            transforms.append(M)
        prev_gray = curr_gray

    dx = np.array([T[0, 2] for T in transforms])
    dy = np.array([T[1, 2] for T in transforms])

    cum_dx = np.cumsum(dx)
    cum_dy = np.cumsum(dy)

    kernel = np.ones(smoothness) / smoothness
    smooth_dx = np.convolve(cum_dx, kernel, mode="same")
    smooth_dy = np.convolve(cum_dy, kernel, mode="same")

    ddx = smooth_dx - cum_dx
    ddy = smooth_dy - cum_dy

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(output_path, fourcc, fps, (w, h))
    writer.write(all_frames[0])

    for i, frame in enumerate(all_frames[1:]):
        T = transforms[i].copy()
        T[0, 2] += ddx[i]
        T[1, 2] += ddy[i]
        stabilized = cv2.warpAffine(frame, T, (w, h), borderMode=cv2.BORDER_REPLICATE)
        writer.write(stabilized)

    writer.release()
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
