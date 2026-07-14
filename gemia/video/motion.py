"""Motion primitives for real video clips.

These are low-level, planner-visible building blocks. They intentionally avoid
remote AI calls and write deterministic metadata sidecars for later review,
search, speed-curve, mask, and tracking features.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import cv2
import numpy as np


@dataclass(frozen=True)
class MotionRenderResult:
    output_path: str
    metadata_path: str
    frame_count: int


def render_motion_heatmap(
    input_path: str,
    output_path: str,
    *,
    opacity: float = 0.62,
    gain: float = 3.2,
    frame_step: int = 1,
    max_long_edge: int | None = 540,
    colormap: str = "turbo",
) -> str:
    """Render an optical-flow motion heatmap overlay with a `.motion.json` sidecar."""
    source, output = _resolve_io(input_path, output_path, label="motion heatmap")
    reader = _VideoReader(source, max_long_edge=max_long_edge, frame_step=frame_step)
    writer = _open_writer(output, reader.fps_out, reader.size)

    rendered = 0
    previous_gray: np.ndarray | None = None
    motion_values: list[float] = []
    samples: list[dict[str, Any]] = []
    cmap = _colormap_id(colormap)
    alpha = _clamp(opacity, 0.0, 1.0)
    flow_gain = max(float(gain), 0.01)

    try:
        for frame_index, frame in reader.frames():
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            if previous_gray is None:
                rendered_frame = frame
                mean_motion = 0.0
                max_motion = 0.0
                p95_motion = 0.0
            else:
                flow = _dense_flow(previous_gray, gray)
                mag = np.sqrt(flow[..., 0] ** 2 + flow[..., 1] ** 2)
                heat_u8 = _motion_to_u8(mag, gain=flow_gain)
                heat = cv2.applyColorMap(heat_u8, cmap)
                rendered_frame = cv2.addWeighted(frame, 1.0 - alpha, heat, alpha, 0.0)
                mean_motion = float(np.mean(mag))
                max_motion = float(np.max(mag))
                p95_motion = float(np.percentile(mag, 95))
            writer.write(rendered_frame)
            motion_values.append(mean_motion)
            if len(samples) < 12:
                samples.append(
                    {
                        "frame": frame_index,
                        "mean_motion_px": mean_motion,
                        "p95_motion_px": p95_motion,
                        "max_motion_px": max_motion,
                    }
                )
            previous_gray = gray
            rendered += 1
    finally:
        reader.close()
        writer.release()

    _ensure_rendered(rendered, "motion heatmap")
    _write_motion_metadata(
        output,
        effect="gemia_motion_heatmap",
        source=source,
        rendered_frames=rendered,
        frame_step=reader.frame_step,
        source_fps=reader.source_fps,
        output_fps=reader.fps_out,
        size=reader.size,
        metrics={
            "average_motion_px": _mean(motion_values),
            "peak_mean_motion_px": max(motion_values) if motion_values else 0.0,
        },
        samples=samples,
        parameters={
            "opacity": alpha,
            "gain": flow_gain,
            "max_long_edge": max_long_edge,
            "colormap": colormap,
        },
    )
    return str(output)


def render_motion_trails(
    input_path: str,
    output_path: str,
    *,
    decay: float = 0.86,
    threshold: float = 12.0,
    opacity: float = 0.58,
    tint: str = "iceblue",
    frame_step: int = 1,
    max_long_edge: int | None = 540,
) -> str:
    """Render temporal motion trails from frame differences."""
    source, output = _resolve_io(input_path, output_path, label="motion trails")
    reader = _VideoReader(source, max_long_edge=max_long_edge, frame_step=frame_step)
    writer = _open_writer(output, reader.fps_out, reader.size)

    trail: np.ndarray | None = None
    previous_gray: np.ndarray | None = None
    rendered = 0
    coverage_values: list[float] = []
    samples: list[dict[str, Any]] = []
    trail_decay = _clamp(decay, 0.0, 0.98)
    alpha = _clamp(opacity, 0.0, 1.0)
    motion_threshold = _clamp(threshold, 0.0, 255.0)
    color = _tint_color(tint)

    try:
        for frame_index, frame in reader.frames():
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            if trail is None:
                trail = np.zeros_like(gray, dtype=np.float32)
            if previous_gray is None:
                coverage = 0.0
            else:
                diff = cv2.absdiff(gray, previous_gray)
                mask = (diff >= motion_threshold).astype(np.float32)
                trail = np.maximum(trail * trail_decay, mask)
                trail = cv2.GaussianBlur(trail, (0, 0), sigmaX=1.4)
                coverage = float(np.mean(mask))
            overlay = _colorize_alpha(trail, color)
            rendered_frame = cv2.addWeighted(frame, 1.0, overlay, alpha, 0.0)
            writer.write(rendered_frame)
            coverage_values.append(coverage)
            if len(samples) < 12:
                samples.append(
                    {
                        "frame": frame_index,
                        "motion_coverage": coverage,
                        "trail_strength": float(np.mean(trail)) if trail is not None else 0.0,
                    }
                )
            previous_gray = gray
            rendered += 1
    finally:
        reader.close()
        writer.release()

    _ensure_rendered(rendered, "motion trails")
    _write_motion_metadata(
        output,
        effect="gemia_motion_trails",
        source=source,
        rendered_frames=rendered,
        frame_step=reader.frame_step,
        source_fps=reader.source_fps,
        output_fps=reader.fps_out,
        size=reader.size,
        metrics={
            "average_motion_coverage": _mean(coverage_values),
            "peak_motion_coverage": max(coverage_values) if coverage_values else 0.0,
        },
        samples=samples,
        parameters={
            "decay": trail_decay,
            "threshold": motion_threshold,
            "opacity": alpha,
            "tint": tint,
            "max_long_edge": max_long_edge,
        },
    )
    return str(output)


def render_motion_stabilize(
    input_path: str,
    output_path: str,
    *,
    smoothing_radius: int = 12,
    crop_zoom: float = 1.04,
    max_features: int = 240,
    quality_level: float = 0.01,
    min_distance: int = 20,
    max_long_edge: int | None = 540,
) -> str:
    """Render a lightweight camera-motion stabilization pass."""
    source, output = _resolve_io(input_path, output_path, label="motion stabilize")
    frames, source_fps, source_size = _load_video_frames(source, max_long_edge=max_long_edge)
    if not frames:
        raise RuntimeError("motion stabilize produced no frames.")

    transforms, samples = _estimate_camera_transforms(
        frames,
        max_features=max_features,
        quality_level=quality_level,
        min_distance=min_distance,
    )
    smoothed = _smooth_transforms(transforms, radius=max(int(smoothing_radius), 0))
    writer = _open_writer(output, source_fps, source_size)
    zoom = max(float(crop_zoom), 1.0)
    rendered = 0
    try:
        writer.write(_fix_border(frames[0], zoom))
        rendered += 1
        for index, frame in enumerate(frames[1:], start=1):
            dx, dy, da = smoothed[index - 1]
            matrix = np.array(
                [
                    [math.cos(da), -math.sin(da), dx],
                    [math.sin(da), math.cos(da), dy],
                ],
                dtype=np.float32,
            )
            stabilized = cv2.warpAffine(
                frame,
                matrix,
                source_size,
                flags=cv2.INTER_LINEAR,
                borderMode=cv2.BORDER_REFLECT,
            )
            writer.write(_fix_border(stabilized, zoom))
            rendered += 1
    finally:
        writer.release()

    translations = [math.hypot(float(dx), float(dy)) for dx, dy, _ in smoothed]
    rotations = [abs(math.degrees(float(da))) for _, _, da in smoothed]
    _write_motion_metadata(
        output,
        effect="gemia_motion_stabilize",
        source=source,
        rendered_frames=rendered,
        frame_step=1,
        source_fps=source_fps,
        output_fps=source_fps,
        size=source_size,
        metrics={
            "average_translation_px": _mean(translations),
            "peak_translation_px": max(translations) if translations else 0.0,
            "average_rotation_deg": _mean(rotations),
            "peak_rotation_deg": max(rotations) if rotations else 0.0,
            "tracked_pairs": len(transforms),
        },
        samples=samples[:12],
        parameters={
            "smoothing_radius": max(int(smoothing_radius), 0),
            "crop_zoom": zoom,
            "max_features": max(int(max_features), 8),
            "quality_level": _clamp(quality_level, 0.001, 0.2),
            "min_distance": max(int(min_distance), 1),
            "max_long_edge": max_long_edge,
        },
    )
    return str(output)


class _VideoReader:
    def __init__(self, source: Path, *, max_long_edge: int | None, frame_step: int) -> None:
        self.source = source
        self.cap = cv2.VideoCapture(str(source))
        if not self.cap.isOpened():
            raise ValueError(f"Motion input is not readable: {source}")
        width = int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
        height = int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
        if width <= 0 or height <= 0:
            self.cap.release()
            raise ValueError("Motion input has invalid dimensions.")
        self.source_fps = float(self.cap.get(cv2.CAP_PROP_FPS) or 24.0) or 24.0
        self.frame_step = max(int(frame_step), 1)
        self.fps_out = max(self.source_fps / self.frame_step, 1.0)
        self.size = _scaled_size(width, height, max_long_edge)

    def frames(self):
        frame_index = 0
        while True:
            ok, frame = self.cap.read()
            if not ok or frame is None:
                break
            if frame_index % self.frame_step:
                frame_index += 1
                continue
            if (frame.shape[1], frame.shape[0]) != self.size:
                frame = cv2.resize(frame, self.size, interpolation=cv2.INTER_AREA)
            yield frame_index, frame
            frame_index += 1

    def close(self) -> None:
        self.cap.release()


def _resolve_io(input_path: str, output_path: str, *, label: str) -> tuple[Path, Path]:
    source = Path(input_path).expanduser().resolve()
    output = Path(output_path).expanduser().resolve()
    if not source.exists():
        raise FileNotFoundError(f"{label} input does not exist: {source}")
    output.parent.mkdir(parents=True, exist_ok=True)
    return source, output


def _open_writer(output: Path, fps: float, size: tuple[int, int]) -> cv2.VideoWriter:
    writer = cv2.VideoWriter(
        str(output),
        cv2.VideoWriter_fourcc(*"mp4v"),
        max(float(fps), 1.0),
        size,
    )
    if not writer.isOpened():
        raise RuntimeError(f"Could not open motion writer: {output}")
    return writer


def _dense_flow(previous_gray: np.ndarray, gray: np.ndarray) -> np.ndarray:
    return cv2.calcOpticalFlowFarneback(
        previous_gray,
        gray,
        None,
        pyr_scale=0.5,
        levels=3,
        winsize=15,
        iterations=3,
        poly_n=5,
        poly_sigma=1.2,
        flags=0,
    )


def _motion_to_u8(mag: np.ndarray, *, gain: float) -> np.ndarray:
    if mag.size == 0:
        return np.zeros_like(mag, dtype=np.uint8)
    p95 = float(np.percentile(mag, 95))
    denom = max(p95, 0.1)
    normalized = np.clip((mag / denom) * gain, 0.0, 1.0)
    return (normalized * 255).astype(np.uint8)


def _colormap_id(name: str) -> int:
    key = (name or "").strip().lower()
    mapping = {
        "turbo": getattr(cv2, "COLORMAP_TURBO", cv2.COLORMAP_JET),
        "magma": cv2.COLORMAP_MAGMA,
        "inferno": cv2.COLORMAP_INFERNO,
        "plasma": cv2.COLORMAP_PLASMA,
        "viridis": cv2.COLORMAP_VIRIDIS,
        "jet": cv2.COLORMAP_JET,
        "hot": cv2.COLORMAP_HOT,
    }
    return mapping.get(key, getattr(cv2, "COLORMAP_TURBO", cv2.COLORMAP_JET))


def _tint_color(name: str) -> tuple[int, int, int]:
    key = (name or "").strip().lower()
    mapping = {
        "iceblue": (255, 225, 120),
        "cyan": (255, 230, 80),
        "blue": (255, 170, 70),
        "white": (235, 235, 235),
        "red": (40, 60, 255),
        "gold": (70, 190, 255),
        "green": (80, 230, 120),
    }
    return mapping.get(key, mapping["iceblue"])


def _colorize_alpha(alpha: np.ndarray, color: tuple[int, int, int]) -> np.ndarray:
    clipped = np.clip(alpha, 0.0, 1.0)[..., np.newaxis]
    base = np.zeros((*alpha.shape, 3), dtype=np.float32)
    base[:, :, 0] = color[0]
    base[:, :, 1] = color[1]
    base[:, :, 2] = color[2]
    return np.clip(base * clipped, 0, 255).astype(np.uint8)


def _load_video_frames(source: Path, *, max_long_edge: int | None) -> tuple[list[np.ndarray], float, tuple[int, int]]:
    cap = cv2.VideoCapture(str(source))
    if not cap.isOpened():
        raise ValueError(f"Motion input is not readable: {source}")
    try:
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
        fps = float(cap.get(cv2.CAP_PROP_FPS) or 24.0) or 24.0
        size = _scaled_size(width, height, max_long_edge)
        frames: list[np.ndarray] = []
        while True:
            ok, frame = cap.read()
            if not ok or frame is None:
                break
            if (frame.shape[1], frame.shape[0]) != size:
                frame = cv2.resize(frame, size, interpolation=cv2.INTER_AREA)
            frames.append(frame)
        return frames, fps, size
    finally:
        cap.release()


def _estimate_camera_transforms(
    frames: list[np.ndarray],
    *,
    max_features: int,
    quality_level: float,
    min_distance: int,
) -> tuple[list[tuple[float, float, float]], list[dict[str, Any]]]:
    transforms: list[tuple[float, float, float]] = []
    samples: list[dict[str, Any]] = []
    feature_params = {
        "maxCorners": max(int(max_features), 8),
        "qualityLevel": _clamp(quality_level, 0.001, 0.2),
        "minDistance": max(int(min_distance), 1),
        "blockSize": 3,
    }
    lk_params = {
        "winSize": (21, 21),
        "maxLevel": 3,
        "criteria": (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 30, 0.01),
    }
    previous_gray = cv2.cvtColor(frames[0], cv2.COLOR_BGR2GRAY)
    for index, frame in enumerate(frames[1:], start=1):
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        points = cv2.goodFeaturesToTrack(previous_gray, **feature_params)
        dx = dy = da = 0.0
        tracked = 0
        if points is not None and len(points) >= 4:
            next_points, status, _ = cv2.calcOpticalFlowPyrLK(previous_gray, gray, points, None, **lk_params)
            if next_points is not None and status is not None:
                keep = status.reshape(-1).astype(bool)
                src = points.reshape(-1, 2)[keep]
                dst = next_points.reshape(-1, 2)[keep]
                tracked = int(len(src))
                if tracked >= 4:
                    matrix, _ = cv2.estimateAffinePartial2D(src, dst)
                    if matrix is not None:
                        dx = float(matrix[0, 2])
                        dy = float(matrix[1, 2])
                        da = float(math.atan2(matrix[1, 0], matrix[0, 0]))
        transforms.append((dx, dy, da))
        if len(samples) < 12:
            samples.append({"frame": index, "dx": dx, "dy": dy, "rotation_deg": math.degrees(da), "tracked_points": tracked})
        previous_gray = gray
    return transforms, samples


def _smooth_transforms(
    transforms: list[tuple[float, float, float]],
    *,
    radius: int,
) -> list[tuple[float, float, float]]:
    if not transforms or radius <= 0:
        return transforms
    arr = np.array(transforms, dtype=np.float32)
    trajectory = np.cumsum(arr, axis=0)
    smoothed = np.zeros_like(trajectory)
    for index in range(len(trajectory)):
        start = max(0, index - radius)
        end = min(len(trajectory), index + radius + 1)
        smoothed[index] = np.mean(trajectory[start:end], axis=0)
    correction = smoothed - trajectory
    result = arr + correction
    return [(float(dx), float(dy), float(da)) for dx, dy, da in result]


def _fix_border(frame: np.ndarray, zoom: float) -> np.ndarray:
    if zoom <= 1.0001:
        return frame
    h, w = frame.shape[:2]
    matrix = cv2.getRotationMatrix2D((w / 2.0, h / 2.0), 0, zoom)
    return cv2.warpAffine(frame, matrix, (w, h), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)


def _scaled_size(width: int, height: int, max_long_edge: int | None) -> tuple[int, int]:
    if width <= 0 or height <= 0:
        raise ValueError("Motion input has invalid dimensions.")
    if not max_long_edge or max(width, height) <= int(max_long_edge):
        return _even(width), _even(height)
    scale = float(max_long_edge) / float(max(width, height))
    return _even(max(2, int(round(width * scale)))), _even(max(2, int(round(height * scale))))


def _even(value: int) -> int:
    value = max(int(value), 2)
    return value if value % 2 == 0 else value - 1


def _write_motion_metadata(
    output: Path,
    *,
    effect: str,
    source: Path,
    rendered_frames: int,
    frame_step: int,
    source_fps: float,
    output_fps: float,
    size: tuple[int, int],
    metrics: dict[str, Any],
    samples: list[dict[str, Any]],
    parameters: dict[str, Any],
) -> Path:
    metadata_path = output.with_suffix(".motion.json")
    metadata_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "effect": effect,
                "rendered_at": datetime.now(timezone.utc).isoformat(),
                "source_path": str(source),
                "output_path": str(output),
                "rendered_frames": rendered_frames,
                "frame_step": max(int(frame_step), 1),
                "source_fps": source_fps,
                "output_fps": output_fps,
                "width": size[0],
                "height": size[1],
                "metrics": metrics,
                "samples": samples,
                "parameters": parameters,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return metadata_path


def _ensure_rendered(rendered: int, label: str) -> None:
    if rendered <= 0:
        raise RuntimeError(f"{label} produced no frames.")


def _mean(values: list[float]) -> float:
    return float(np.mean(values)) if values else 0.0


def _clamp(value: float, low: float, high: float) -> float:
    return min(max(float(value), low), high)


__all__ = [
    "MotionRenderResult",
    "render_motion_heatmap",
    "render_motion_trails",
    "render_motion_stabilize",
]
