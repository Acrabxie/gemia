"""Face Reshaper-style tracked warp previews for real video clips."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import cv2
import numpy as np


@dataclass(frozen=True)
class FaceReshaperRenderResult:
    output_path: str
    metadata_path: str
    frame_count: int


def render_face_reshaper_plan(
    input_path: str,
    output_path: str,
    *,
    cheek_scale: float = -0.12,
    jaw_scale: float = -0.08,
    eye_spacing: float = 0.05,
    smile_lift: float = 0.04,
    strength: float = 0.72,
    frame_step: int = 1,
    max_long_edge: int | None = 540,
) -> str:
    """Render a Resolve-style Face Reshaper preview with tracked warp metadata."""
    source = Path(input_path).expanduser().resolve()
    output = Path(output_path).expanduser().resolve()
    if not source.exists():
        raise FileNotFoundError(f"Face Reshaper input does not exist: {source}")

    cap = cv2.VideoCapture(str(source))
    try:
        if not cap.isOpened():
            raise ValueError(f"Face Reshaper input is not readable: {source}")
        source_width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
        source_height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
        source_fps = float(cap.get(cv2.CAP_PROP_FPS) or 24.0) or 24.0
        if source_width <= 0 or source_height <= 0:
            raise ValueError("Face Reshaper input has invalid dimensions.")

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
            raise RuntimeError(f"Could not open Face Reshaper writer: {output}")

        cascade = _face_cascade()
        rendered = 0
        frame_index = 0
        frames_with_faces = 0
        total_faces = 0
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
            rendered_frame = frame.copy()
            if faces:
                frames_with_faces += 1
                total_faces += len(faces)
                rendered_frame = _apply_reshape_preview(
                    frame,
                    faces,
                    cheek_scale=cheek_scale,
                    jaw_scale=jaw_scale,
                    eye_spacing=eye_spacing,
                    smile_lift=smile_lift,
                    strength=strength,
                )
            writer.write(rendered_frame)
            if len(samples) < 10:
                samples.append(
                    {
                        "frame": frame_index,
                        "face_count": len(faces),
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
        raise RuntimeError("Face Reshaper produced no frames.")
    metadata_path = output.with_suffix(".face_reshaper.json")
    metadata_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "effect": "resolve21_ai_face_reshaper",
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
                "preview_kind": "tracked_local_warp" if frames_with_faces else "no_face_diagnostic_passthrough",
                "reshape_controls": {
                    "cheek_scale": _clamp(cheek_scale, -0.5, 0.5),
                    "jaw_scale": _clamp(jaw_scale, -0.5, 0.5),
                    "eye_spacing": _clamp(eye_spacing, -0.35, 0.35),
                    "smile_lift": _clamp(smile_lift, -0.35, 0.35),
                    "strength": _clamp(strength, 0.0, 1.0),
                },
                "tracking": {
                    "tracked_frames": frames_with_faces,
                    "warp_model": "face_box_region_remap",
                },
                "samples": samples,
                "parameters": {"max_long_edge": max_long_edge},
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return str(output)


def _apply_reshape_preview(frame: np.ndarray, faces: list[tuple[int, int, int, int]], **controls: float) -> np.ndarray:
    result = frame.copy()
    for x, y, w, h in faces:
        x0, y0 = max(int(x), 0), max(int(y), 0)
        x1, y1 = min(x0 + max(int(w), 1), frame.shape[1]), min(y0 + max(int(h), 1), frame.shape[0])
        if x1 <= x0 or y1 <= y0:
            continue
        roi = frame[y0:y1, x0:x1]
        warped = _warp_face_roi(roi, **controls)
        mask = _oval_mask(y1 - y0, x1 - x0)
        result[y0:y1, x0:x1] = (
            roi.astype(np.float32) * (1.0 - mask) + warped.astype(np.float32) * mask
        ).astype(np.uint8)
    return result


def _warp_face_roi(
    roi: np.ndarray,
    *,
    cheek_scale: float,
    jaw_scale: float,
    eye_spacing: float,
    smile_lift: float,
    strength: float,
) -> np.ndarray:
    height, width = roi.shape[:2]
    yy, xx = np.indices((height, width), dtype=np.float32)
    nx = (xx - (width - 1) * 0.5) / max(width * 0.5, 1.0)
    ny = (yy - (height - 1) * 0.5) / max(height * 0.5, 1.0)
    amount = _clamp(strength, 0.0, 1.0)
    cheek_zone = np.exp(-((np.abs(ny) - 0.08) ** 2) / 0.18) * np.exp(-(nx**2) / 0.55)
    jaw_zone = np.clip((ny - 0.08) / 0.75, 0.0, 1.0) ** 2
    eye_zone = np.exp(-((ny + 0.28) ** 2) / 0.035) * np.exp(-(nx**2) / 0.7)
    mouth_zone = np.exp(-((ny - 0.38) ** 2) / 0.05) * np.exp(-(nx**2) / 0.55)

    dx = nx * width * amount * (
        _clamp(cheek_scale, -0.5, 0.5) * cheek_zone
        + _clamp(jaw_scale, -0.5, 0.5) * jaw_zone
        - _clamp(eye_spacing, -0.35, 0.35) * eye_zone
    )
    dy = -height * amount * _clamp(smile_lift, -0.35, 0.35) * mouth_zone
    map_x = np.clip(xx - dx, 0, width - 1).astype(np.float32)
    map_y = np.clip(yy - dy, 0, height - 1).astype(np.float32)
    return cv2.remap(roi, map_x, map_y, interpolation=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)


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
    rx, ry = max(width * 0.49, 1.0), max(height * 0.55, 1.0)
    dist = ((xx - cx) / rx) ** 2 + ((yy - cy) / ry) ** 2
    mask = np.clip((1.14 - dist) / 0.22, 0.0, 1.0)
    return cv2.GaussianBlur(mask.astype(np.float32), (0, 0), sigmaX=2.0)[..., np.newaxis]


def _scaled_size(width: int, height: int, max_long_edge: int | None) -> tuple[int, int]:
    if not max_long_edge or max(width, height) <= int(max_long_edge):
        return width, height
    scale = float(max_long_edge) / float(max(width, height))
    return max(2, int(round(width * scale))), max(2, int(round(height * scale)))


def _clamp(value: float, low: float, high: float) -> float:
    return min(max(float(value), low), high)


__all__ = ["FaceReshaperRenderResult", "render_face_reshaper_plan"]
