"""Resolve-style slate metadata detection for real clips."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import cv2
import numpy as np


@dataclass(frozen=True)
class SlateIdRenderResult:
    output_path: str
    metadata_path: str
    frame_count: int


def render_slate_id_metadata_plan(
    input_path: str,
    output_path: str,
    *,
    frame_step: int = 6,
    max_long_edge: int | None = 540,
    min_confidence: float = 0.42,
    metadata_hints: dict[str, Any] | None = None,
) -> str:
    """Copy a preview video and write searchable AI Slate ID metadata."""
    source = Path(input_path).expanduser().resolve()
    output = Path(output_path).expanduser().resolve()
    if not source.exists():
        raise FileNotFoundError(f"Slate ID input does not exist: {source}")

    cap = cv2.VideoCapture(str(source))
    try:
        if not cap.isOpened():
            raise ValueError(f"Slate ID input is not readable: {source}")
        source_width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
        source_height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
        source_fps = float(cap.get(cv2.CAP_PROP_FPS) or 24.0) or 24.0
        if source_width <= 0 or source_height <= 0:
            raise ValueError("Slate ID input has invalid dimensions.")

        width, height = _scaled_size(source_width, source_height, max_long_edge)
        step = max(int(frame_step), 1)
        output.parent.mkdir(parents=True, exist_ok=True)
        writer = cv2.VideoWriter(
            str(output),
            cv2.VideoWriter_fourcc(*"mp4v"),
            max(source_fps, 1.0),
            (width, height),
        )
        if not writer.isOpened():
            raise RuntimeError(f"Could not open Slate ID writer: {output}")

        rendered = 0
        frame_index = 0
        samples: list[dict[str, Any]] = []
        slate_candidates: list[dict[str, Any]] = []
        while True:
            ok, frame = cap.read()
            if not ok or frame is None:
                break
            if (frame.shape[1], frame.shape[0]) != (width, height):
                frame = cv2.resize(frame, (width, height), interpolation=cv2.INTER_AREA)
            writer.write(frame)
            if frame_index % step == 0:
                analysis = _analyze_frame(frame, frame_index)
                if len(samples) < 12:
                    samples.append(analysis)
                if analysis["slate_score"] >= _clamp(min_confidence, 0.0, 1.0):
                    slate_candidates.append(analysis)
            rendered += 1
            frame_index += 1
        writer.release()
    finally:
        cap.release()

    if rendered <= 0:
        raise RuntimeError("Slate ID produced no frames.")

    slate_candidates.sort(key=lambda item: item["slate_score"], reverse=True)
    detected = bool(slate_candidates)
    top = slate_candidates[0] if detected else {}
    clip_metadata = _clip_metadata(source, top, metadata_hints or {}, detected=detected)
    metadata_path = output.with_suffix(".slate_id.json")
    metadata_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "effect": "resolve21_ai_slate_id_metadata",
                "rendered_at": datetime.now(timezone.utc).isoformat(),
                "source_path": str(source),
                "output_path": str(output),
                "rendered_frames": rendered,
                "frame_step": step,
                "preview_kind": "slate_metadata_detected" if detected else "no_slate_diagnostic_passthrough",
                "slate_detection": {
                    "mode": "opencv_contrast_components",
                    "frames_with_slate": len(slate_candidates),
                    "no_slate_evidence": not detected,
                    "top_confidence": top.get("slate_score", 0.0),
                    "top_frame": top.get("frame"),
                    "top_bbox": top.get("board_bbox"),
                },
                "clip_metadata": clip_metadata,
                "parameters": {
                    "frame_step": step,
                    "max_long_edge": max_long_edge,
                    "min_confidence": _clamp(min_confidence, 0.0, 1.0),
                    "metadata_hints": sorted((metadata_hints or {}).keys()),
                },
                "samples": samples,
                "candidates": slate_candidates[:8],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return str(output)


def _analyze_frame(frame: np.ndarray, frame_index: int) -> dict[str, Any]:
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    blur = cv2.GaussianBlur(gray, (3, 3), 0)
    _, binary = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    inv_binary = 255 - binary
    components = _text_like_components(binary) + _text_like_components(inv_binary)
    edge_density = _edge_density(gray)
    bbox, rect_score = _largest_board_bbox(binary)
    component_score = min(len(components) / 18.0, 1.0)
    row_score = _row_alignment_score(components)
    contrast_score = _contrast_score(gray)
    slate_score = _clamp(
        rect_score * 0.34 + component_score * 0.28 + row_score * 0.22 + edge_density * 1.8 * 0.10 + contrast_score * 0.06,
        0.0,
        1.0,
    )
    crop = _crop_bbox(gray, bbox) if bbox else gray
    visual_hash = hashlib.sha1(crop.tobytes()).hexdigest()[:12]
    return {
        "frame": frame_index,
        "slate_score": round(slate_score, 4),
        "board_bbox": bbox,
        "text_component_count": len(components),
        "text_row_count": _text_row_count(components),
        "edge_density": round(edge_density, 4),
        "contrast_score": round(contrast_score, 4),
        "visual_text_signature": visual_hash,
        "searchable_text": _searchable_text(visual_hash, components, slate_score),
    }


def _clip_metadata(source: Path, top: dict[str, Any], hints: dict[str, Any], *, detected: bool) -> dict[str, Any]:
    slate_id = str(hints.get("slate_id") or hints.get("scene") or top.get("visual_text_signature") or source.stem)
    searchable = [
        "slate",
        "ai_slate_id",
        f"source:{source.stem}",
        f"slate_id:{slate_id}",
    ]
    if detected:
        searchable.extend(str(top.get("searchable_text", "")).split())
    for key in ("scene", "shot", "take", "roll", "camera", "date"):
        value = hints.get(key)
        if value:
            searchable.append(f"{key}:{value}")
    return {
        "slate_id": slate_id,
        "scene": hints.get("scene"),
        "shot": hints.get("shot"),
        "take": hints.get("take"),
        "roll": hints.get("roll"),
        "camera": hints.get("camera"),
        "detected_text": top.get("searchable_text", "") if detected else "",
        "searchable_tokens": sorted({str(token) for token in searchable if token}),
        "confidence": top.get("slate_score", 0.0) if detected else 0.0,
    }


def _text_like_components(binary: np.ndarray) -> list[tuple[int, int, int, int]]:
    count, _, stats, _ = cv2.connectedComponentsWithStats(binary, 8)
    frame_area = binary.shape[0] * binary.shape[1]
    components: list[tuple[int, int, int, int]] = []
    for idx in range(1, count):
        x, y, w, h, area = (int(v) for v in stats[idx])
        if area < 6 or area > frame_area * 0.12:
            continue
        if w < 2 or h < 3:
            continue
        ratio = w / max(h, 1)
        if 0.08 <= ratio <= 10.0 and h <= binary.shape[0] * 0.28:
            components.append((x, y, w, h))
    return components


def _largest_board_bbox(binary: np.ndarray) -> tuple[dict[str, int] | None, float]:
    contours, _ = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    frame_area = binary.shape[0] * binary.shape[1]
    best_bbox: dict[str, int] | None = None
    best_score = 0.0
    for contour in contours:
        x, y, w, h = cv2.boundingRect(contour)
        area = w * h
        if area < frame_area * 0.04:
            continue
        aspect = w / max(h, 1)
        if not 0.9 <= aspect <= 5.5:
            continue
        extent = area / max(frame_area, 1)
        rect = min(extent / 0.45, 1.0)
        score = rect * (1.0 if area < frame_area * 0.95 else 0.35)
        if score > best_score:
            best_score = score
            best_bbox = {"x": int(x), "y": int(y), "width": int(w), "height": int(h)}
    return best_bbox, float(best_score)


def _row_alignment_score(components: list[tuple[int, int, int, int]]) -> float:
    rows = _text_row_count(components)
    if not components:
        return 0.0
    return _clamp(rows / 5.0, 0.0, 1.0) * _clamp(len(components) / 12.0, 0.0, 1.0)


def _text_row_count(components: list[tuple[int, int, int, int]]) -> int:
    centers = sorted(y + h / 2.0 for _, y, _, h in components)
    rows: list[float] = []
    for center in centers:
        if not rows or abs(rows[-1] - center) > 8:
            rows.append(center)
        else:
            rows[-1] = (rows[-1] + center) / 2.0
    return len(rows)


def _edge_density(gray: np.ndarray) -> float:
    edges = cv2.Canny(gray, 70, 160)
    return float(np.mean(edges > 0))


def _contrast_score(gray: np.ndarray) -> float:
    p5, p95 = np.percentile(gray, [5, 95])
    return _clamp((float(p95) - float(p5)) / 255.0, 0.0, 1.0)


def _crop_bbox(gray: np.ndarray, bbox: dict[str, int] | None) -> np.ndarray:
    if not bbox:
        return gray
    x0 = max(int(bbox["x"]), 0)
    y0 = max(int(bbox["y"]), 0)
    x1 = min(x0 + int(bbox["width"]), gray.shape[1])
    y1 = min(y0 + int(bbox["height"]), gray.shape[0])
    return gray[y0:y1, x0:x1] if x1 > x0 and y1 > y0 else gray


def _searchable_text(visual_hash: str, components: list[tuple[int, int, int, int]], slate_score: float) -> str:
    if slate_score <= 0:
        return ""
    rows = _text_row_count(components)
    return f"visual_slate_{visual_hash} components_{len(components)} rows_{rows}"


def _scaled_size(width: int, height: int, max_long_edge: int | None) -> tuple[int, int]:
    if not max_long_edge or max(width, height) <= int(max_long_edge):
        return width, height
    scale = float(max_long_edge) / float(max(width, height))
    return max(2, int(round(width * scale))), max(2, int(round(height * scale)))


def _clamp(value: float, low: float, high: float) -> float:
    return min(max(float(value), low), high)


__all__ = ["SlateIdRenderResult", "render_slate_id_metadata_plan"]
