"""Keyframe animation system for animating Primitive parameters over time.

Layer plans evaluate tracks in frame-number space. ``apply_animated_op`` evaluates
the same track type in seconds because it operates directly on decoded video time.
"""
from __future__ import annotations

import math
import re
from typing import Callable

import cv2
import numpy as np

from gemia.primitives_common import Image, ensure_float32, to_uint8


_VALID_MODES = {"clamp", "loop", "pingpong", "relative"}


class KeyframeTrack:
    """Animate a single float parameter over a caller-defined time axis.

    Example::

        track = KeyframeTrack()
        track.add_keyframe(0.0, 0.0)
        track.add_keyframe(1.0, 1.0, easing="ease_in_out")
        val = track.evaluate(0.5)
    """

    def __init__(self, *, mode: str = "clamp", relative_to: float = 0.0) -> None:
        if mode not in _VALID_MODES:
            raise ValueError(f"Unknown keyframe mode: {mode!r}. Use one of {_VALID_MODES}.")
        self.mode = mode
        self.relative_to = float(relative_to)
        self._keyframes: list[tuple[float, float, str]] = []

    def add_keyframe(self, timestamp: float, value: float, easing: str = "linear") -> None:
        """Add a keyframe at the given timestamp.

        Args:
            timestamp: Time in seconds.
            value: Value at this keyframe.
            easing: One of 'linear', 'ease_in', 'ease_out', 'ease_in_out', 'bezier'.
        """
        valid = {"linear", "ease_in", "ease_out", "ease_in_out", "bezier"}
        if easing not in valid and _parse_bezier_easing(easing) is None:
            raise ValueError(f"Unknown easing: {easing!r}. Use one of {valid}.")
        self._keyframes.append((timestamp, value, easing))
        self._keyframes.sort(key=lambda k: k[0])

    def add_bezier_keyframe(
        self,
        timestamp: float,
        value: float,
        *,
        control_points: tuple[float, float, float, float],
    ) -> None:
        """Add a keyframe with explicit cubic Bezier retiming control points."""
        p1x, p1y, p2x, p2y = (float(item) for item in control_points)
        self.add_keyframe(timestamp, value, easing=f"bezier({p1x:g},{p1y:g},{p2x:g},{p2y:g})")

    def evaluate(self, timestamp: float) -> float:
        """Evaluate the track at the given timestamp.

        Args:
            timestamp: Time in seconds.

        Returns:
            Interpolated float value.
        """
        if not self._keyframes:
            return 0.0
        timestamp = self._mapped_timestamp(float(timestamp))
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

    def to_curve_metadata(self) -> dict[str, object]:
        """Return stable metadata for graph compilers and review artifacts."""
        return {
            "mode": self.mode,
            "relative_to": self.relative_to,
            "duration": self._duration(),
            "keyframes": [
                {"time": float(timestamp), "value": float(value), "easing": easing}
                for timestamp, value, easing in self._keyframes
            ],
        }

    def copy(self) -> "KeyframeTrack":
        track = KeyframeTrack(mode=self.mode, relative_to=self.relative_to)
        track._keyframes = list(self._keyframes)
        return track

    def _duration(self) -> float:
        if len(self._keyframes) < 2:
            return 0.0
        return max(0.0, self._keyframes[-1][0] - self._keyframes[0][0])

    def _mapped_timestamp(self, timestamp: float) -> float:
        if self.mode == "relative":
            return timestamp - self.relative_to
        if self.mode not in {"loop", "pingpong"} or len(self._keyframes) < 2:
            return timestamp

        start = self._keyframes[0][0]
        duration = self._duration()
        if duration <= 0.0:
            return start
        offset = (timestamp - start) % duration
        if self.mode == "pingpong" and int((timestamp - start) // duration) % 2 == 1:
            offset = duration - offset
        return start + offset


def _apply_easing(t: float, easing: str) -> float:
    """Apply easing function to normalized time t in [0, 1]."""
    bezier_points = _parse_bezier_easing(easing)
    if bezier_points is not None:
        return _cubic_bezier(t, *bezier_points)
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


def _parse_bezier_easing(easing: str) -> tuple[float, float, float, float] | None:
    match = re.fullmatch(
        r"bezier\(\s*([-+]?\d*\.?\d+)\s*,\s*([-+]?\d*\.?\d+)\s*,\s*([-+]?\d*\.?\d+)\s*,\s*([-+]?\d*\.?\d+)\s*\)",
        easing,
    )
    if not match:
        return None
    return tuple(float(part) for part in match.groups())  # type: ignore[return-value]


def retime_keyframe_track(
    track: KeyframeTrack,
    *,
    time_scale: float = 1.0,
    time_offset: float = 0.0,
    value_scale: float = 1.0,
    value_offset: float = 0.0,
    mode: str | None = None,
    relative_to: float | None = None,
) -> KeyframeTrack:
    """Return a retimed/value-adjusted copy of a keyframe track."""
    if time_scale == 0.0:
        raise ValueError("time_scale must not be 0.")
    result = KeyframeTrack(
        mode=mode or track.mode,
        relative_to=track.relative_to if relative_to is None else relative_to,
    )
    for timestamp, value, easing in track._keyframes:
        result.add_keyframe(
            timestamp * time_scale + time_offset,
            value * value_scale + value_offset,
            easing=easing,
        )
    return result


def adjust_keyframe_tracks_for_clips(
    clip_tracks: dict[str, dict[str, KeyframeTrack]],
    clip_offsets: dict[str, float],
    *,
    time_scale: float = 1.0,
    value_scale: float = 1.0,
    value_offset: float = 0.0,
    mode: str | None = None,
) -> dict[str, dict[str, KeyframeTrack]]:
    """Apply one timing/value adjustment across several clip keyframe maps."""
    adjusted: dict[str, dict[str, KeyframeTrack]] = {}
    for clip_id, tracks in clip_tracks.items():
        offset = float(clip_offsets.get(clip_id, 0.0))
        adjusted[clip_id] = {
            name: retime_keyframe_track(
                track,
                time_scale=time_scale,
                time_offset=offset,
                value_scale=value_scale,
                value_offset=value_offset,
                mode=mode,
            )
            for name, track in tracks.items()
        }
    return adjusted


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
