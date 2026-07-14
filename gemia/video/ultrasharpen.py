"""UltraSharpen-style detail enhancement for real video clips."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import cv2
import numpy as np


@dataclass(frozen=True)
class UltraSharpenRenderResult:
    output_path: str
    metadata_path: str
    frame_count: int


def render_ultrasharpen_plan(
    input_path: str,
    output_path: str,
    *,
    strength: float = 0.72,
    detail_radius: int = 5,
    edge_threshold: float = 0.04,
    denoise: float = 0.22,
    frame_step: int = 1,
    max_long_edge: int | None = 540,
) -> str:
    """Render a Resolve-style UltraSharpen pass with before/after metadata."""
    source = Path(input_path).expanduser().resolve()
    output = Path(output_path).expanduser().resolve()
    if not source.exists():
        raise FileNotFoundError(f"UltraSharpen input does not exist: {source}")

    cap = cv2.VideoCapture(str(source))
    try:
        if not cap.isOpened():
            raise ValueError(f"UltraSharpen input is not readable: {source}")
        source_width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
        source_height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
        source_fps = float(cap.get(cv2.CAP_PROP_FPS) or 24.0) or 24.0
        if source_width <= 0 or source_height <= 0:
            raise ValueError("UltraSharpen input has invalid dimensions.")

        width, height = _scaled_size(source_width, source_height, max_long_edge)
        step = max(int(frame_step), 1)
        output.parent.mkdir(parents=True, exist_ok=True)
        writer = cv2.VideoWriter(
            str(output),
            cv2.VideoWriter_fourcc(*"mp4v"),
            max(source_fps / step, 1.0),
            (width, height),
        )
        if not writer.isOpened():
            raise RuntimeError(f"Could not open UltraSharpen writer: {output}")

        rendered = 0
        frame_index = 0
        previous_gray: np.ndarray | None = None
        before_values: list[float] = []
        after_values: list[float] = []
        edge_values: list[float] = []
        temporal_values: list[float] = []
        samples: list[dict[str, Any]] = []

        while True:
            ok, frame = cap.read()
            if not ok or frame is None:
                break
            if frame_index % step:
                frame_index += 1
                continue
            if (frame.shape[1], frame.shape[0]) != (width, height):
                frame = cv2.resize(frame, (width, height), interpolation=cv2.INTER_AREA)

            sharpened, edge_density = _ultrasharpen_frame(
                frame,
                strength=strength,
                detail_radius=detail_radius,
                edge_threshold=edge_threshold,
                denoise=denoise,
            )
            before = _sharpness(frame)
            after = _sharpness(sharpened)
            if after < before:
                sharpened = _fallback_unsharp(frame, strength=strength, detail_radius=detail_radius)
                after = _sharpness(sharpened)
            gray = cv2.cvtColor(sharpened, cv2.COLOR_BGR2GRAY)
            temporal_delta = _temporal_delta(gray, previous_gray)

            writer.write(sharpened)
            if len(samples) < 10:
                samples.append(
                    {
                        "frame": frame_index,
                        "input_sharpness": before,
                        "output_sharpness": after,
                        "edge_density": edge_density,
                        "temporal_delta": temporal_delta,
                    }
                )
            before_values.append(before)
            after_values.append(after)
            edge_values.append(edge_density)
            temporal_values.append(temporal_delta)
            previous_gray = gray
            rendered += 1
            frame_index += 1
        writer.release()
    finally:
        cap.release()

    if rendered <= 0:
        raise RuntimeError("UltraSharpen produced no frames.")
    before_avg = float(np.mean(before_values)) if before_values else 0.0
    after_avg = float(np.mean(after_values)) if after_values else 0.0
    metadata_path = output.with_suffix(".ultrasharpen.json")
    metadata_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "effect": "resolve21_ai_ultrasharpen",
                "rendered_at": datetime.now(timezone.utc).isoformat(),
                "source_path": str(source),
                "output_path": str(output),
                "rendered_frames": rendered,
                "frame_step": step,
                "sharpness": {
                    "input_laplacian_variance": before_avg,
                    "output_laplacian_variance": after_avg,
                    "delta": after_avg - before_avg,
                    "ratio": (after_avg / before_avg) if before_avg > 0 else None,
                },
                "average_edge_density": float(np.mean(edge_values)) if edge_values else 0.0,
                "average_temporal_delta": float(np.mean(temporal_values)) if temporal_values else 0.0,
                "samples": samples,
                "parameters": {
                    "strength": _clamp(strength, 0.0, 1.5),
                    "detail_radius": max(int(detail_radius), 1),
                    "edge_threshold": _clamp(edge_threshold, 0.0, 1.0),
                    "denoise": _clamp(denoise, 0.0, 1.0),
                    "max_long_edge": max_long_edge,
                },
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return str(output)


def _ultrasharpen_frame(
    frame: np.ndarray,
    *,
    strength: float,
    detail_radius: int,
    edge_threshold: float,
    denoise: float,
) -> tuple[np.ndarray, float]:
    radius = max(int(detail_radius), 1) | 1
    amount = _clamp(strength, 0.0, 1.5)
    denoise_amount = _clamp(denoise, 0.0, 1.0)
    base = cv2.bilateralFilter(frame, d=5, sigmaColor=18 + denoise_amount * 36, sigmaSpace=18 + denoise_amount * 36)
    ycrcb = cv2.cvtColor(base, cv2.COLOR_BGR2YCrCb)
    y = ycrcb[:, :, 0]
    blurred = cv2.GaussianBlur(y, (radius, radius), sigmaX=0)
    high = cv2.addWeighted(y, 1.0 + amount, blurred, -amount, 0)
    edges = cv2.Laplacian(y, cv2.CV_32F)
    edge_strength = np.abs(edges) / 255.0
    mask = np.clip((edge_strength - _clamp(edge_threshold, 0.0, 1.0)) * 6.0, 0.0, 1.0)
    mask = cv2.GaussianBlur(mask, (0, 0), sigmaX=0.8)[..., np.newaxis]
    y_mixed = y.astype(np.float32) * (1.0 - mask[:, :, 0]) + high.astype(np.float32) * mask[:, :, 0]
    ycrcb[:, :, 0] = np.clip(y_mixed, 0, 255).astype(np.uint8)
    result = cv2.cvtColor(ycrcb, cv2.COLOR_YCrCb2BGR)
    edge_density = float(np.mean(edge_strength > _clamp(edge_threshold, 0.0, 1.0)))
    return result, edge_density


def _fallback_unsharp(frame: np.ndarray, *, strength: float, detail_radius: int) -> np.ndarray:
    radius = max(int(detail_radius), 1) | 1
    blurred = cv2.GaussianBlur(frame, (radius, radius), sigmaX=0)
    amount = _clamp(strength, 0.0, 1.5)
    return np.clip(cv2.addWeighted(frame, 1.0 + amount, blurred, -amount, 0), 0, 255).astype(np.uint8)


def _sharpness(frame: np.ndarray) -> float:
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def _temporal_delta(gray: np.ndarray, previous_gray: np.ndarray | None) -> float:
    if previous_gray is None or previous_gray.shape != gray.shape:
        return 0.0
    return float(np.mean(cv2.absdiff(gray, previous_gray)) / 255.0)


def _scaled_size(width: int, height: int, max_long_edge: int | None) -> tuple[int, int]:
    if not max_long_edge or max(width, height) <= int(max_long_edge):
        return width, height
    scale = float(max_long_edge) / float(max(width, height))
    return max(2, int(round(width * scale))), max(2, int(round(height * scale)))


def _clamp(value: float, low: float, high: float) -> float:
    return min(max(float(value), low), high)


__all__ = ["UltraSharpenRenderResult", "render_ultrasharpen_plan"]
