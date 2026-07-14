"""OpenCV optical-flow and sparse tracking analysis backend."""
from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from gemia.video.timeline_assets import cache_key_for_path, probe_media


@dataclass(frozen=True)
class FlowTrackingAnalysisResult:
    """Bounded motion-analysis payload for timeline/tracker interchange."""

    backend: str
    source_path: str
    frame_size: tuple[int, int]
    fps: float
    sampled_frame_count: int
    analyzed_pair_count: int
    mean_magnitude: float
    max_magnitude: float
    confidence: float
    sample_summaries: list[dict[str, Any]]
    tracker_points: list[dict[str, Any]]
    tracked_point_count: int
    cache_key: str
    source_probe: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class OpenCVFlowTrackingBackend:
    """Compute deterministic OpenCV motion summaries for real video clips."""

    name = "github_opencv_flow_tracking_backend"

    def analyze(
        self,
        source_path: str | Path,
        *,
        sample_stride: int = 4,
        max_samples: int = 24,
        max_sparse_points: int = 80,
        resize_long_edge: int = 320,
    ) -> FlowTrackingAnalysisResult:
        source = _validate_visual_media(source_path)
        probe = probe_media(str(source))
        width = int(probe.get("width") or 0)
        height = int(probe.get("height") or 0)
        if width <= 0 or height <= 0:
            raise ValueError(f"OpenCV flow tracking requires visual media: {source}")

        sample_stride = _bounded_int(sample_stride, 1, 120, 4)
        max_samples = _bounded_int(max_samples, 2, 240, 24)
        max_sparse_points = _bounded_int(max_sparse_points, 0, 1000, 80)
        resize_long_edge = _bounded_int(resize_long_edge, 64, 1280, 320)

        cap = cv2.VideoCapture(str(source))
        if not cap.isOpened():
            raise OSError(f"OpenCV could not open video: {source}")

        sample_summaries: list[dict[str, Any]] = []
        tracker_points: list[dict[str, Any]] = []
        magnitudes: list[float] = []
        prev_gray: np.ndarray | None = None
        sampled_frames = 0
        analyzed_pairs = 0
        frame_index = -1
        reported_points = 0

        try:
            while sampled_frames < max_samples:
                ok, frame = cap.read()
                if not ok:
                    break
                frame_index += 1
                if frame_index % sample_stride != 0:
                    continue

                gray = _prepare_gray(frame, resize_long_edge=resize_long_edge)
                sampled_frames += 1
                if prev_gray is None:
                    prev_gray = gray
                    continue

                flow = cv2.calcOpticalFlowFarneback(
                    prev_gray,
                    gray,
                    None,
                    0.5,
                    3,
                    15,
                    3,
                    5,
                    1.2,
                    0,
                )
                magnitude, _angle = cv2.cartToPolar(flow[..., 0], flow[..., 1])
                mean_mag = float(np.mean(magnitude))
                max_mag = float(np.max(magnitude))
                magnitudes.append(mean_mag)
                analyzed_pairs += 1
                sample_summaries.append(
                    {
                        "frame_index": frame_index,
                        "mean_magnitude": round(mean_mag, 5),
                        "max_magnitude": round(max_mag, 5),
                        "motion_vectors": _grid_motion_vectors(flow, magnitude),
                    }
                )

                if max_sparse_points > 0 and reported_points < max_sparse_points:
                    points = _sparse_tracker_points(
                        prev_gray,
                        gray,
                        frame_index=frame_index,
                        max_points=max_sparse_points - reported_points,
                    )
                    tracker_points.extend(points)
                    reported_points += len(points)

                prev_gray = gray
        finally:
            cap.release()

        mean_magnitude = float(np.mean(magnitudes)) if magnitudes else 0.0
        max_magnitude = float(max((item["max_magnitude"] for item in sample_summaries), default=0.0))
        confidence = _confidence(
            sampled_frame_count=sampled_frames,
            analyzed_pair_count=analyzed_pairs,
            mean_magnitude=mean_magnitude,
            tracked_point_count=len(tracker_points),
        )
        return FlowTrackingAnalysisResult(
            backend=self.name,
            source_path=str(source),
            frame_size=(width, height),
            fps=round(float(probe.get("fps") or cap.get(cv2.CAP_PROP_FPS) or 0.0), 3),
            sampled_frame_count=sampled_frames,
            analyzed_pair_count=analyzed_pairs,
            mean_magnitude=round(mean_magnitude, 5),
            max_magnitude=round(max_magnitude, 5),
            confidence=confidence,
            sample_summaries=sample_summaries,
            tracker_points=tracker_points,
            tracked_point_count=len(tracker_points),
            cache_key=cache_key_for_path(str(source)),
            source_probe=probe,
        )


def render_opencv_flow_tracking_backend_manifest(
    input_paths: list[str | Path],
    output_dir: str | Path,
    *,
    package_id: str = "github_opencv_flow_tracking_backend",
    sample_stride: int = 4,
    max_samples: int = 24,
    max_sparse_points: int = 80,
) -> str:
    """Write an OpenCV flow/tracking architecture manifest for real clips."""
    if not input_paths:
        raise ValueError("input_paths must contain at least one media path")
    output_root = Path(output_dir).expanduser().resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    backend = OpenCVFlowTrackingBackend()
    analyses = [
        backend.analyze(
            path,
            sample_stride=sample_stride,
            max_samples=max_samples,
            max_sparse_points=max_sparse_points,
        )
        for path in input_paths
    ]
    manifest = {
        "schema_version": 1,
        "effect": "github_opencv_flow_tracking_backend",
        "rendered_at": datetime.now(timezone.utc).isoformat(),
        "package": {
            "package_id": _safe_id(package_id),
            "backend": backend.name,
            "clip_count": len(analyses),
            "sample_stride": _bounded_int(sample_stride, 1, 120, 4),
            "max_samples": _bounded_int(max_samples, 2, 240, 24),
        },
        "sources": [
            {
                "source_path": item.source_path,
                "asset_ref": _asset_ref(Path(item.source_path), item.source_probe),
                "cache_key": item.cache_key,
                "source_probe": item.source_probe,
            }
            for item in analyses
        ],
        "analyses": [item.to_dict() for item in analyses],
        "interchange": {
            "retime_signal": "mean/max dense-flow magnitude can guide generated-frame ranges",
            "tracker_signal": "sparse point survival count can guide mask/replay confidence",
            "replay_signal": "sample summaries are bounded enough to attach to replay action manifests",
        },
        "diagnostics": [
            f"{len(analyses)} real clips analyzed with OpenCV Farneback optical flow",
            f"{sum(item.analyzed_pair_count for item in analyses)} sampled frame pairs analyzed",
            f"{sum(item.tracked_point_count for item in analyses)} sparse tracker points emitted",
        ],
        "review_hints": [
            "re-run when source cache_key changes",
            "low confidence means too few sampled pairs or little stable motion evidence",
            "manifest is analysis metadata only; source media is not modified",
        ],
    }
    manifest_path = output_root / "opencv_flow_tracking_backend_manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return str(manifest_path)


def _validate_visual_media(source_path: str | Path) -> Path:
    source = Path(source_path).expanduser().resolve()
    if not source.exists():
        raise FileNotFoundError(f"Input media not found: {source}")
    if not source.is_file():
        raise OSError(f"Input media is not a file: {source}")
    probe = probe_media(str(source))
    if int(probe.get("width") or 0) <= 0 or int(probe.get("height") or 0) <= 0:
        raise ValueError(f"OpenCV flow tracking requires visual media: {source}")
    return source


def _prepare_gray(frame: np.ndarray, *, resize_long_edge: int) -> np.ndarray:
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    height, width = gray.shape[:2]
    long_edge = max(width, height)
    if long_edge <= resize_long_edge:
        return gray
    scale = resize_long_edge / float(long_edge)
    size = (max(1, int(round(width * scale))), max(1, int(round(height * scale))))
    return cv2.resize(gray, size, interpolation=cv2.INTER_AREA)


def _grid_motion_vectors(flow: np.ndarray, magnitude: np.ndarray) -> list[dict[str, float]]:
    rows, cols = 3, 3
    height, width = magnitude.shape[:2]
    vectors: list[dict[str, float]] = []
    for row in range(rows):
        y0 = int(row * height / rows)
        y1 = int((row + 1) * height / rows)
        for col in range(cols):
            x0 = int(col * width / cols)
            x1 = int((col + 1) * width / cols)
            cell_flow = flow[y0:y1, x0:x1]
            cell_mag = magnitude[y0:y1, x0:x1]
            if cell_flow.size == 0:
                continue
            vectors.append(
                {
                    "x": round((col + 0.5) / cols, 4),
                    "y": round((row + 0.5) / rows, 4),
                    "dx": round(float(np.mean(cell_flow[..., 0])), 5),
                    "dy": round(float(np.mean(cell_flow[..., 1])), 5),
                    "magnitude": round(float(np.mean(cell_mag)), 5),
                }
            )
    return vectors


def _sparse_tracker_points(
    prev_gray: np.ndarray,
    gray: np.ndarray,
    *,
    frame_index: int,
    max_points: int,
) -> list[dict[str, float]]:
    if max_points <= 0:
        return []
    points = cv2.goodFeaturesToTrack(
        prev_gray,
        maxCorners=max_points,
        qualityLevel=0.01,
        minDistance=5,
        blockSize=5,
    )
    if points is None:
        return []
    next_points, status, errors = cv2.calcOpticalFlowPyrLK(prev_gray, gray, points, None)
    if next_points is None or status is None:
        return []
    payload: list[dict[str, float]] = []
    flat_errors = errors.reshape(-1) if errors is not None else np.zeros((len(points),), dtype=np.float32)
    for index, (old, new, ok, error) in enumerate(zip(points.reshape(-1, 2), next_points.reshape(-1, 2), status.reshape(-1), flat_errors)):
        if int(ok) != 1:
            continue
        dx = float(new[0] - old[0])
        dy = float(new[1] - old[1])
        payload.append(
            {
                "frame_index": float(frame_index),
                "point_index": float(index),
                "x": round(float(old[0]), 3),
                "y": round(float(old[1]), 3),
                "dx": round(dx, 5),
                "dy": round(dy, 5),
                "error": round(float(error), 5),
            }
        )
        if len(payload) >= max_points:
            break
    return payload


def _confidence(
    *,
    sampled_frame_count: int,
    analyzed_pair_count: int,
    mean_magnitude: float,
    tracked_point_count: int,
) -> float:
    if sampled_frame_count < 2 or analyzed_pair_count <= 0:
        return 0.0
    pair_coverage = analyzed_pair_count / max(sampled_frame_count - 1, 1)
    motion_score = min(1.0, mean_magnitude / 1.5)
    tracker_score = min(1.0, tracked_point_count / 20.0)
    return round(max(0.0, min(1.0, 0.55 * pair_coverage + 0.3 * motion_score + 0.15 * tracker_score)), 3)


def _bounded_int(value: Any, minimum: int, maximum: int, default: int) -> int:
    try:
        number = int(value)
    except Exception:
        number = default
    return max(minimum, min(maximum, number))


def _asset_ref(path: Path, probe: dict[str, Any]) -> str:
    duration = round(float(probe.get("duration") or 0.0), 3)
    return f"{_safe_id(path.stem)}:{path.stat().st_size}:{duration}:{int(probe.get('width') or 0)}x{int(probe.get('height') or 0)}"


def _safe_id(value: str) -> str:
    return "".join(ch.lower() if ch.isalnum() or ch in {"-", "_"} else "_" for ch in str(value)).strip("_") or "item"


__all__ = [
    "FlowTrackingAnalysisResult",
    "OpenCVFlowTrackingBackend",
    "render_opencv_flow_tracking_backend_manifest",
]
