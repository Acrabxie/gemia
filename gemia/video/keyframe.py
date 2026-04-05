"""Keyframe animation system for animating any Primitive parameter over time."""
from __future__ import annotations

import math
from typing import Callable

import cv2
import numpy as np

from gemia.primitives_common import Image, ensure_float32, to_uint8


class KeyframeTrack:
    """Animate a single float parameter over time with easing.

    Example::

        track = KeyframeTrack()
        track.add_keyframe(0.0, 0.0)
        track.add_keyframe(1.0, 1.0, easing="ease_in_out")
        val = track.evaluate(0.5)
    """

    def __init__(self) -> None:
        self._keyframes: list[tuple[float, float, str]] = []

    def add_keyframe(self, timestamp: float, value: float, easing: str = "linear") -> None:
        """Add a keyframe at the given timestamp.

        Args:
            timestamp: Time in seconds.
            value: Value at this keyframe.
            easing: One of 'linear', 'ease_in', 'ease_out', 'ease_in_out', 'bezier'.
        """
        valid = {"linear", "ease_in", "ease_out", "ease_in_out", "bezier"}
        if easing not in valid:
            raise ValueError(f"Unknown easing: {easing!r}. Use one of {valid}.")
        self._keyframes.append((timestamp, value, easing))
        self._keyframes.sort(key=lambda k: k[0])

    def evaluate(self, timestamp: float) -> float:
        """Evaluate the track at the given timestamp.

        Args:
            timestamp: Time in seconds.

        Returns:
            Interpolated float value.
        """
        if not self._keyframes:
            return 0.0
        if timestamp <= self._keyframes[0][0]:
            return self._keyframes[0][1]
        if timestamp >= self._keyframes[-1][0]:
            return self._keyframes[-1][1]

        for i in range(len(self._keyframes) - 1):
            t0, v0, easing = self._keyframes[i]
            t1, v1, _ = self._keyframes[i + 1]
            if t0 <= timestamp <= t1:
                if t1 == t0:
                    return v0
                t = (timestamp - t0) / (t1 - t0)
                t = _apply_easing(t, easing)
                return v0 + (v1 - v0) * t
        return self._keyframes[-1][1]


def _apply_easing(t: float, easing: str) -> float:
    """Apply easing function to normalized time t in [0, 1]."""
    if easing == "linear":
        return t
    elif easing == "ease_in":
        return t * t
    elif easing == "ease_out":
        return t * (2.0 - t)
    elif easing == "ease_in_out":
        if t < 0.5:
            return 2.0 * t * t
        return -1.0 + (4.0 - 2.0 * t) * t
    elif easing == "bezier":
        return _cubic_bezier(t, 0.42, 0.0, 0.58, 1.0)
    return t


def _cubic_bezier(t: float, p1x: float, p1y: float, p2x: float, p2y: float) -> float:
    """Approximate CSS cubic-bezier easing."""
    cx = 3.0 * p1x
    bx = 3.0 * (p2x - p1x) - cx
    ax = 1.0 - cx - bx

    cy = 3.0 * p1y
    by = 3.0 * (p2y - p1y) - cy
    ay = 1.0 - cy - by

    def sample_x(t: float) -> float:
        return ((ax * t + bx) * t + cx) * t

    def sample_y(t: float) -> float:
        return ((ay * t + by) * t + cy) * t

    t_guess = t
    for _ in range(8):
        x = sample_x(t_guess) - t
        dx = (3.0 * ax * t_guess + 2.0 * bx) * t_guess + cx
        if abs(dx) < 1e-7:
            break
        t_guess -= x / dx
    t_guess = max(0.0, min(1.0, t_guess))
    return sample_y(t_guess)


def apply_animated_op(
    video_path: str,
    output_path: str,
    *,
    op_fn: Callable,
    param_name: str,
    keyframe_track: KeyframeTrack,
    base_kwargs: dict | None = None,
) -> str:
    """Apply op_fn to each frame, evaluating keyframe_track for param_name.

    Args:
        video_path: Input video path.
        output_path: Output video path.
        op_fn: Function to apply to each frame. Signature: (Image, **kwargs) -> Image.
        param_name: Keyword argument name in op_fn to animate.
        keyframe_track: KeyframeTrack providing animated values.
        base_kwargs: Additional fixed keyword arguments for op_fn.

    Returns:
        output_path.
    """
    from pathlib import Path

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise FileNotFoundError(f"Cannot open video: {video_path}")

    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(output_path, fourcc, fps, (w, h))

    kwargs = dict(base_kwargs or {})
    frame_idx = 0

    while True:
        ret, frame = cap.read()
        if not ret:
            break
        timestamp = frame_idx / fps
        kwargs[param_name] = keyframe_track.evaluate(timestamp)
        img = ensure_float32(frame)
        result = op_fn(img, **kwargs)
        writer.write(to_uint8(result))
        frame_idx += 1

    cap.release()
    writer.release()
    return output_path
