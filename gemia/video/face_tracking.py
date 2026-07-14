"""Face tracking preview and metadata export for Lumeri planner runs."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import cv2
import numpy as np


@dataclass(frozen=True)
class FaceTrackingRenderResult:
    output_path: str
    metadata_path: str
    frame_count: int


def render_face_tracking_plan(
    input_path: str,
    output_path: str,
    *,
    target: str = "most_prominent_face",
    time_scope: str = "full_clip",
    overlay: bool = True,
    trail: bool = True,
    frame_step: int = 1,
    max_long_edge: int | None = 720,
) -> str:
    """Track the most prominent face with sensible defaults and write a preview video plus metadata.

    This planner-friendly primitive intentionally has no required semantic
    arguments beyond input/output paths, so short requests like "人脸跟踪" can
    execute without a clarification loop.
    """
    source = Path(input_path).expanduser().resolve()
    output = Path(output_path).expanduser().resolve()
    if not source.exists():
        raise FileNotFoundError(f"Face tracking input does not exist: {source}")

    cap = cv2.VideoCapture(str(source))
    try:
        if not cap.isOpened():
            raise ValueError(f"Face tracking input is not readable: {source}")
        source_width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
        source_height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
        source_fps = float(cap.get(cv2.CAP_PROP_FPS) or 24.0) or 24.0
        if source_width <= 0 or source_height <= 0:
            raise ValueError("Face tracking input has invalid dimensions.")

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
            raise RuntimeError(f"Could not open face tracking writer: {output}")

        cascade = _face_cascade()
        rendered = 0
        frame_index = 0
        frames_with_faces = 0
        total_faces = 0
        last_center: tuple[int, int] | None = None
        path_points: list[tuple[int, int]] = []
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
            selected = _select_face(faces)
            rendered_frame = frame.copy()
            if selected is not None:
                frames_with_faces += 1
                total_faces += len(faces)
                x, y, w, h = selected
                center = (int(x + w / 2), int(y + h / 2))
                last_center = center
                path_points.append(center)
                if overlay:
                    _draw_tracking_overlay(rendered_frame, selected, path_points if trail else [])
            elif overlay and last_center is not None:
                cv2.circle(rendered_frame, last_center, 5, (120, 220, 255), -1)

            if len(samples) < 16:
                samples.append(
                    {
                        "frame": frame_index,
                        "face_count": len(faces),
                        "selected_box": _box_json(selected),
                        "center": {"x": last_center[0], "y": last_center[1]} if selected is not None and last_center else None,
                    }
                )
            writer.write(rendered_frame)
            rendered += 1
            frame_index += 1
        writer.release()
    finally:
        cap.release()

    if rendered <= 0:
        raise RuntimeError("Face tracking produced no frames.")

    metadata_path = output.with_suffix(".face_tracking.json")
    metadata_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "effect": "lumeri_face_tracking",
                "rendered_at": datetime.now(timezone.utc).isoformat(),
                "source_path": str(source),
                "output_path": str(output),
                "rendered_frames": rendered,
                "frame_step": step,
                "target": target or "most_prominent_face",
                "time_scope": time_scope or "full_clip",
                "overlay": bool(overlay),
                "trail": bool(trail),
                "face_detection": {
                    "mode": "haar_frontalface" if _cascade_file_exists() else "unavailable",
                    "frames_with_faces": frames_with_faces,
                    "total_faces": total_faces,
                    "average_faces_per_rendered_frame": total_faces / rendered if rendered else 0.0,
                    "no_face_evidence": frames_with_faces == 0,
                },
                "tracking": {
                    "default_target_policy": "largest detected face per sampled frame",
                    "path_points_sampled": [{"x": x, "y": y} for x, y in path_points[:48]],
                    "tracked_frames": frames_with_faces,
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


def _draw_tracking_overlay(frame: np.ndarray, box: tuple[int, int, int, int], points: list[tuple[int, int]]) -> None:
    x, y, w, h = box
    x0, y0 = max(int(x), 0), max(int(y), 0)
    x1, y1 = min(x0 + max(int(w), 1), frame.shape[1] - 1), min(y0 + max(int(h), 1), frame.shape[0] - 1)
    color = (120, 220, 255)
    cv2.rectangle(frame, (x0, y0), (x1, y1), color, 2)
    center = (int((x0 + x1) / 2), int((y0 + y1) / 2))
    cv2.circle(frame, center, 5, color, -1)
    if len(points) >= 2:
        pts = np.array(points[-48:], dtype=np.int32).reshape((-1, 1, 2))
        cv2.polylines(frame, [pts], isClosed=False, color=(255, 255, 255), thickness=2)


def _select_face(faces: list[tuple[int, int, int, int]]) -> tuple[int, int, int, int] | None:
    if not faces:
        return None
    return max(faces, key=lambda item: int(item[2]) * int(item[3]))


def _box_json(box: tuple[int, int, int, int] | None) -> dict[str, int] | None:
    if box is None:
        return None
    x, y, w, h = box
    return {"x": int(x), "y": int(y), "width": int(w), "height": int(h)}


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


def _scaled_size(width: int, height: int, max_long_edge: int | None) -> tuple[int, int]:
    if not max_long_edge or max(width, height) <= int(max_long_edge):
        return width, height
    scale = float(max_long_edge) / float(max(width, height))
    return max(2, int(round(width * scale))), max(2, int(round(height * scale)))


__all__ = ["FaceTrackingRenderResult", "render_face_tracking_plan"]
