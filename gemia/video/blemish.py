"""Blemish-removal preview rendering for real portrait clips."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import cv2
import numpy as np


@dataclass(frozen=True)
class BlemishRemovalRenderResult:
    output_path: str
    metadata_path: str
    frame_count: int


def render_blemish_removal_plan(
    input_path: str,
    output_path: str,
    *,
    strength: float = 0.68,
    texture_preservation: float = 0.52,
    skin_threshold: float = 0.24,
    frame_step: int = 1,
    max_long_edge: int | None = 540,
) -> str:
    """Render a Resolve-style AI Blemish Removal preview with texture metadata."""
    source = Path(input_path).expanduser().resolve()
    output = Path(output_path).expanduser().resolve()
    if not source.exists():
        raise FileNotFoundError(f"Blemish Removal input does not exist: {source}")

    cap = cv2.VideoCapture(str(source))
    try:
        if not cap.isOpened():
            raise ValueError(f"Blemish Removal input is not readable: {source}")
        source_width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
        source_height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
        source_fps = float(cap.get(cv2.CAP_PROP_FPS) or 24.0) or 24.0
        if source_width <= 0 or source_height <= 0:
            raise ValueError("Blemish Removal input has invalid dimensions.")

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
            raise RuntimeError(f"Could not open Blemish Removal writer: {output}")

        cascade = _face_cascade()
        rendered = 0
        frame_index = 0
        frames_with_faces = 0
        total_faces = 0
        cleanup_deltas: list[float] = []
        texture_scores: list[float] = []
        skin_coverages: list[float] = []
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

            faces = _detect_faces(frame, cascade)
            if faces:
                rendered_frame, frame_delta, texture_score, skin_coverage = _apply_cleanup_preview(
                    frame,
                    faces,
                    strength=strength,
                    texture_preservation=texture_preservation,
                    skin_threshold=skin_threshold,
                )
                frames_with_faces += 1
                total_faces += len(faces)
                cleanup_deltas.append(frame_delta)
                texture_scores.append(texture_score)
                skin_coverages.append(skin_coverage)
            else:
                rendered_frame = frame.copy()
                frame_delta = 0.0
                texture_score = 1.0
                skin_coverage = 0.0
            writer.write(rendered_frame)
            if len(samples) < 10:
                samples.append(
                    {
                        "frame": frame_index,
                        "face_count": len(faces),
                        "cleanup_delta": frame_delta,
                        "texture_preservation_score": texture_score,
                        "skin_coverage": skin_coverage,
                        "boxes": [
                            {"x": int(x), "y": int(y), "width": int(w), "height": int(h)}
                            for x, y, w, h in faces[:6]
                        ],
                    }
                )
            rendered += 1
            frame_index += 1
        writer.release()
    finally:
        cap.release()

    if rendered <= 0:
        raise RuntimeError("Blemish Removal produced no frames.")
    metadata_path = output.with_suffix(".blemish.json")
    metadata_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "effect": "resolve21_ai_blemish_removal",
                "rendered_at": datetime.now(timezone.utc).isoformat(),
                "source_path": str(source),
                "output_path": str(output),
                "rendered_frames": rendered,
                "frame_step": step,
                "face_detection": {
                    "mode": "haar_frontalface" if _cascade_file_exists() else "unavailable",
                    "frames_with_faces": frames_with_faces,
                    "total_faces": total_faces,
                    "average_faces_per_rendered_frame": total_faces / rendered if rendered else 0.0,
                    "no_face_evidence": frames_with_faces == 0,
                },
                "preview_kind": "skin_cleanup_texture_preserving" if frames_with_faces else "no_face_diagnostic_passthrough",
                "cleanup": {
                    "average_delta": float(np.mean(cleanup_deltas)) if cleanup_deltas else 0.0,
                    "average_skin_coverage": float(np.mean(skin_coverages)) if skin_coverages else 0.0,
                    "texture_preservation_score": float(np.mean(texture_scores)) if texture_scores else 1.0,
                },
                "parameters": {
                    "strength": _clamp(strength, 0.0, 1.0),
                    "texture_preservation": _clamp(texture_preservation, 0.0, 1.0),
                    "skin_threshold": _clamp(skin_threshold, 0.0, 1.0),
                    "max_long_edge": max_long_edge,
                },
                "samples": samples,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return str(output)


def _apply_cleanup_preview(
    frame: np.ndarray,
    faces: list[tuple[int, int, int, int]],
    *,
    strength: float,
    texture_preservation: float,
    skin_threshold: float,
) -> tuple[np.ndarray, float, float, float]:
    result = frame.copy()
    deltas: list[float] = []
    texture_scores: list[float] = []
    coverages: list[float] = []
    for x, y, w, h in faces:
        x0, y0 = max(int(x), 0), max(int(y), 0)
        x1, y1 = min(x0 + max(int(w), 1), frame.shape[1]), min(y0 + max(int(h), 1), frame.shape[0])
        if x1 <= x0 or y1 <= y0:
            continue
        roi = frame[y0:y1, x0:x1]
        cleaned, mask = _cleanup_skin_roi(
            roi,
            strength=strength,
            texture_preservation=texture_preservation,
            skin_threshold=skin_threshold,
        )
        result[y0:y1, x0:x1] = (
            roi.astype(np.float32) * (1.0 - mask) + cleaned.astype(np.float32) * mask
        ).astype(np.uint8)
        deltas.append(float(np.mean(cv2.absdiff(roi, cleaned)) / 255.0))
        texture_scores.append(_texture_preservation_score(roi, cleaned))
        coverages.append(float(np.mean(mask[:, :, 0] > 0.05)))
    return (
        result,
        float(np.mean(deltas)) if deltas else 0.0,
        float(np.mean(texture_scores)) if texture_scores else 1.0,
        float(np.mean(coverages)) if coverages else 0.0,
    )


def _cleanup_skin_roi(
    roi: np.ndarray,
    *,
    strength: float,
    texture_preservation: float,
    skin_threshold: float,
) -> tuple[np.ndarray, np.ndarray]:
    amount = _clamp(strength, 0.0, 1.0)
    preserve = _clamp(texture_preservation, 0.0, 1.0)
    skin_mask = _skin_mask(roi, skin_threshold)
    oval = _oval_mask(roi.shape[0], roi.shape[1])
    mask = cv2.GaussianBlur((skin_mask * oval).astype(np.float32), (0, 0), sigmaX=1.4)[..., np.newaxis]
    mask *= amount

    smooth = cv2.bilateralFilter(roi, d=9, sigmaColor=42 + amount * 48, sigmaSpace=28 + amount * 38)
    detail = cv2.subtract(roi, cv2.GaussianBlur(roi, (0, 0), sigmaX=1.2))
    preserved = cv2.addWeighted(smooth, 1.0, detail, 0.25 + preserve * 0.75, 0)
    return np.clip(preserved, 0, 255).astype(np.uint8), np.clip(mask, 0.0, 1.0)


def _skin_mask(roi: np.ndarray, threshold: float) -> np.ndarray:
    ycrcb = cv2.cvtColor(roi, cv2.COLOR_BGR2YCrCb)
    _, cr, cb = cv2.split(ycrcb)
    center_cr = 150.0
    center_cb = 112.0
    dist = np.sqrt(((cr.astype(np.float32) - center_cr) / 34.0) ** 2 + ((cb.astype(np.float32) - center_cb) / 26.0) ** 2)
    softness = 0.7 + _clamp(threshold, 0.0, 1.0)
    mask = np.clip((softness - dist) / max(softness, 0.001), 0.0, 1.0)
    return cv2.GaussianBlur(mask.astype(np.float32), (0, 0), sigmaX=1.0)


def _texture_preservation_score(before: np.ndarray, after: np.ndarray) -> float:
    before_gray = cv2.cvtColor(before, cv2.COLOR_BGR2GRAY)
    after_gray = cv2.cvtColor(after, cv2.COLOR_BGR2GRAY)
    before_detail = float(cv2.Laplacian(before_gray, cv2.CV_64F).var())
    after_detail = float(cv2.Laplacian(after_gray, cv2.CV_64F).var())
    if before_detail <= 0:
        return 1.0
    return _clamp(after_detail / before_detail, 0.0, 2.0)


def _detect_faces(frame: np.ndarray, cascade: cv2.CascadeClassifier | None) -> list[tuple[int, int, int, int]]:
    if cascade is None or cascade.empty():
        return []
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    min_side = max(24, min(frame.shape[:2]) // 12)
    found = cascade.detectMultiScale(gray, scaleFactor=1.12, minNeighbors=4, minSize=(min_side, min_side))
    return [tuple(int(v) for v in face) for face in found]


def _face_cascade() -> cv2.CascadeClassifier | None:
    path = _cascade_path()
    if path is None:
        return None
    cascade = cv2.CascadeClassifier(str(path))
    return None if cascade.empty() else cascade


def _cascade_path() -> Path | None:
    base = getattr(getattr(cv2, "data", None), "haarcascades", "")
    if not base:
        return None
    path = Path(base) / "haarcascade_frontalface_default.xml"
    return path if path.exists() else None


def _cascade_file_exists() -> bool:
    return _cascade_path() is not None


def _oval_mask(height: int, width: int) -> np.ndarray:
    yy, xx = np.ogrid[:height, :width]
    cx, cy = (width - 1) / 2.0, (height - 1) / 2.0
    rx, ry = max(width * 0.48, 1.0), max(height * 0.55, 1.0)
    dist = ((xx - cx) / rx) ** 2 + ((yy - cy) / ry) ** 2
    mask = np.clip((1.13 - dist) / 0.24, 0.0, 1.0)
    return cv2.GaussianBlur(mask.astype(np.float32), (0, 0), sigmaX=2.0)


def _scaled_size(width: int, height: int, max_long_edge: int | None) -> tuple[int, int]:
    if not max_long_edge or max(width, height) <= int(max_long_edge):
        return width, height
    scale = float(max_long_edge) / float(max(width, height))
    return max(2, int(round(width * scale))), max(2, int(round(height * scale)))


def _clamp(value: float, low: float, high: float) -> float:
    return min(max(float(value), low), high)


__all__ = ["BlemishRemovalRenderResult", "render_blemish_removal_plan"]
