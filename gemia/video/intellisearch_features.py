"""Private feature extraction helpers for Gemia IntelliSearch."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import cv2
import numpy as np

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_STOP_WORDS = {
    "a",
    "an",
    "and",
    "as",
    "at",
    "by",
    "for",
    "from",
    "in",
    "is",
    "of",
    "on",
    "or",
    "the",
    "to",
    "with",
}


def _probe_video(path: Path, *, max_samples: int) -> dict[str, Any]:
    if not path.exists():
        return {"path": str(path), "exists": False, "readable": False, "error": "missing"}
    cap = cv2.VideoCapture(str(path))
    try:
        if not cap.isOpened():
            return {"path": str(path), "exists": True, "readable": False, "error": "not_readable"}
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
        fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
        frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        frame_stats: list[dict[str, Any]] = []
        previous_gray: np.ndarray | None = None
        motion_values: list[float] = []
        for frame_index in _sample_indexes(frame_count, max_samples=max_samples):
            cap.set(cv2.CAP_PROP_POS_FRAMES, max(int(frame_index), 0))
            ok, frame = cap.read()
            if not ok or frame is None:
                continue
            frame_float = frame.astype(np.float32) / 255.0
            bgr_mean = np.mean(frame_float, axis=(0, 1))
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).astype(np.float32) / 255.0
            if previous_gray is not None and previous_gray.shape == gray.shape:
                motion_values.append(float(np.mean(np.abs(gray - previous_gray))))
            previous_gray = gray
            frame_stats.append(
                {
                    "frame": int(frame_index),
                    "mean": float(np.mean(frame_float)),
                    "stddev": float(np.std(frame_float)),
                    "bgr_mean": [float(value) for value in bgr_mean.tolist()],
                }
            )
        duration = float(frame_count) / fps if fps > 0.0 and frame_count > 0 else 0.0
        means = [float(item["mean"]) for item in frame_stats]
        stddevs = [float(item["stddev"]) for item in frame_stats]
        bgr = (
            np.array([item["bgr_mean"] for item in frame_stats], dtype=np.float32)
            if frame_stats
            else np.zeros((1, 3), dtype=np.float32)
        )
        return {
            "path": str(path),
            "exists": True,
            "readable": True,
            "width": width,
            "height": height,
            "fps": fps,
            "frame_count": frame_count,
            "duration_seconds": duration,
            "sampled_frames": len(frame_stats),
            "sample_mean": float(np.mean(means)) if means else None,
            "sample_stddev": float(np.mean(stddevs)) if stddevs else None,
            "motion_score": float(np.mean(motion_values)) if motion_values else 0.0,
            "dominant_channel": _dominant_channel(np.mean(bgr, axis=0)),
            "frame_stats": frame_stats,
        }
    finally:
        cap.release()


def _visual_labels(probe: dict[str, Any]) -> list[str]:
    if not probe.get("readable"):
        return ["unreadable"]
    labels = ["real_video", "review_media"]
    width = int(probe.get("width") or 0)
    height = int(probe.get("height") or 0)
    if width > height:
        labels.append("landscape")
    elif height > width:
        labels.append("vertical")
    mean = probe.get("sample_mean")
    if isinstance(mean, (int, float)):
        if mean < 0.28:
            labels.append("dark")
        elif mean > 0.68:
            labels.append("bright")
        else:
            labels.append("balanced_exposure")
    stddev = probe.get("sample_stddev")
    if isinstance(stddev, (int, float)):
        labels.append("visual_detail" if stddev >= 0.02 else "low_detail")
    motion = float(probe.get("motion_score") or 0.0)
    if motion >= 0.08:
        labels.append("high_motion")
    elif motion >= 0.025:
        labels.append("moderate_motion")
    else:
        labels.append("stable_shot")
    channel = str(probe.get("dominant_channel") or "")
    if channel:
        labels.append(f"{channel}_dominant")
    return labels


def _dialog_labels_for(path: Path) -> list[str]:
    labels = []
    for suffix in (".srt", ".vtt", ".txt"):
        sidecar = path.with_suffix(suffix)
        if not sidecar.exists():
            continue
        try:
            labels.extend(_label_candidates_from_text(sidecar.read_text(encoding="utf-8", errors="ignore")))
        except OSError:
            continue
    if labels:
        labels.append("dialog_keywords")
    return labels


def _phrase_labels(labels: list[str]) -> list[str]:
    phrases = []
    joined = " ".join(label.replace("_", " ") for label in labels)
    tokens = _terms_from_text(joined)
    for size in (2, 3):
        for index in range(0, max(len(tokens) - size + 1, 0)):
            phrase = "_".join(tokens[index : index + size])
            if phrase:
                phrases.append(phrase)
    return phrases


def _label_candidates_from_text(text: str) -> list[str]:
    return [token for token in _terms_from_text(text) if token not in _STOP_WORDS]


def _terms_from_text(text: str) -> list[str]:
    return _TOKEN_RE.findall(str(text).lower())


def _time_ranges(probe: dict[str, Any]) -> list[dict[str, Any]]:
    duration = float(probe.get("duration_seconds") or 0.0)
    if duration <= 0.0:
        return []
    return [{"start_seconds": 0.0, "end_seconds": round(duration, 3), "label": "semantic_clip"}]


def _sample_indexes(frame_count: int, *, max_samples: int) -> list[int]:
    if frame_count <= 0:
        return [0]
    if frame_count <= max_samples:
        return list(range(frame_count))
    ratios = np.linspace(0.0, 1.0, num=max_samples)
    return sorted({min(frame_count - 1, max(0, int(round((frame_count - 1) * float(ratio))))) for ratio in ratios})


def _dominant_channel(bgr_mean: np.ndarray) -> str:
    if bgr_mean.size < 3:
        return ""
    return ["blue", "green", "red"][int(np.argmax(bgr_mean[:3]))]
