"""Motion deblur-style sharpening for real video clips."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import cv2
import numpy as np


@dataclass(frozen=True)
class MotionDeblurRenderResult:
    output_path: str
    metadata_path: str
    frame_count: int


def render_motion_deblur_plan(
    input_path: str,
    output_path: str,
    *,
    strength: float = 0.65,
    blur_radius: int = 7,
    frame_step: int = 1,
    max_long_edge: int | None = 540,
) -> str:
    """Render an AI Motion Deblur-style pass with sharpness metadata sidecar."""
    source = Path(input_path).expanduser().resolve()
    output = Path(output_path).expanduser().resolve()
    if not source.exists():
        raise FileNotFoundError(f"Motion Deblur input does not exist: {source}")

    cap = cv2.VideoCapture(str(source))
    try:
        if not cap.isOpened():
            raise ValueError(f"Motion Deblur input is not readable: {source}")
        source_width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
        source_height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
        source_fps = float(cap.get(cv2.CAP_PROP_FPS) or 24.0) or 24.0
        if source_width <= 0 or source_height <= 0:
            raise ValueError("Motion Deblur input has invalid dimensions.")

        width, height = _scaled_size(source_width, source_height, max_long_edge)
        output.parent.mkdir(parents=True, exist_ok=True)
        step = max(int(frame_step), 1)
        writer = cv2.VideoWriter(
            str(output),
            cv2.VideoWriter_fourcc(*"mp4v"),
            max(source_fps / step, 1.0),
            (width, height),
        )
        if not writer.isOpened():
            raise RuntimeError(f"Could not open Motion Deblur writer: {output}")

        rendered = 0
        frame_index = 0
        previous_gray: np.ndarray | None = None
        samples: list[dict[str, Any]] = []
        before_values: list[float] = []
        after_values: list[float] = []
        motion_values: list[float] = []
        while True:
            ok, frame = cap.read()
            if not ok or frame is None:
                break
            if frame_index % step:
                frame_index += 1
                continue
            if (frame.shape[1], frame.shape[0]) != (width, height):
                frame = cv2.resize(frame, (width, height), interpolation=cv2.INTER_AREA)
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            motion = _motion_score(gray, previous_gray)
            deblurred = _deblur_frame(frame, strength=strength, blur_radius=blur_radius, motion_score=motion)
            before = _sharpness(frame)
            after = _sharpness(deblurred)
            writer.write(deblurred)
            if len(samples) < 10:
                samples.append(
                    {
                        "frame": frame_index,
                        "motion_score": motion,
                        "input_sharpness": before,
                        "output_sharpness": after,
                    }
                )
            before_values.append(before)
            after_values.append(after)
            motion_values.append(motion)
            previous_gray = gray
            rendered += 1
            frame_index += 1
        writer.release()
    finally:
        cap.release()

    if rendered <= 0:
        raise RuntimeError("Motion Deblur produced no frames.")
    before_avg = float(np.mean(before_values)) if before_values else 0.0
    after_avg = float(np.mean(after_values)) if after_values else 0.0
    metadata_path = output.with_suffix(".motion_deblur.json")
    metadata_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "effect": "resolve21_ai_motion_deblur",
                "rendered_at": datetime.now(timezone.utc).isoformat(),
                "source_path": str(source),
                "output_path": str(output),
                "rendered_frames": rendered,
                "frame_step": step,
                "average_motion_score": float(np.mean(motion_values)) if motion_values else 0.0,
                "sharpness": {
                    "input_laplacian_variance": before_avg,
                    "output_laplacian_variance": after_avg,
                    "delta": after_avg - before_avg,
                    "ratio": (after_avg / before_avg) if before_avg > 0 else None,
                },
                "samples": samples,
                "parameters": {
                    "strength": _clamp(strength, 0.0, 1.5),
                    "blur_radius": max(int(blur_radius), 1),
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


def _deblur_frame(frame: np.ndarray, *, strength: float, blur_radius: int, motion_score: float) -> np.ndarray:
    radius = max(int(blur_radius), 1) | 1
    amount = _clamp(strength, 0.0, 1.5) * (0.7 + min(max(motion_score, 0.0) * 3.0, 0.8))
    denoised = cv2.bilateralFilter(frame, d=5, sigmaColor=28, sigmaSpace=28)
    blurred = cv2.GaussianBlur(denoised, (radius, radius), sigmaX=0)
    sharpened = cv2.addWeighted(denoised, 1.0 + amount, blurred, -amount, 0)
    return np.clip(sharpened, 0, 255).astype(np.uint8)


def _sharpness(frame: np.ndarray) -> float:
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def _motion_score(gray: np.ndarray, previous_gray: np.ndarray | None) -> float:
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


__all__ = ["MotionDeblurRenderResult", "render_motion_deblur_plan"]
