"""CineFocus-style rack-focus rendering for real video clips."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import cv2
import numpy as np


@dataclass(frozen=True)
class CineFocusRenderResult:
    output_path: str
    metadata_path: str
    frame_count: int


def render_cinefocus_plan(
    input_path: str,
    output_path: str,
    *,
    focus_keyframes: list[dict[str, Any]] | None = None,
    focus_x: float = 0.5,
    focus_y: float = 0.5,
    focus_radius: float = 0.28,
    rack_to_x: float | None = None,
    rack_to_y: float | None = None,
    aperture: float = 0.75,
    blur_radius: int = 17,
    feather: float = 0.22,
    frame_step: int = 1,
    max_long_edge: int | None = 540,
) -> str:
    """Render a CineFocus rack-focus pass with focus metadata sidecar.

    Coordinates are normalized 0..1 values. ``aperture`` controls blur strength:
    higher values simulate shallower focus and stronger background blur.
    """
    source = Path(input_path).expanduser().resolve()
    output = Path(output_path).expanduser().resolve()
    if not source.exists():
        raise FileNotFoundError(f"CineFocus input does not exist: {source}")

    cap = cv2.VideoCapture(str(source))
    try:
        if not cap.isOpened():
            raise ValueError(f"CineFocus input is not readable: {source}")
        source_width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
        source_height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
        source_fps = float(cap.get(cv2.CAP_PROP_FPS) or 24.0) or 24.0
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        if source_width <= 0 or source_height <= 0:
            raise ValueError("CineFocus input has invalid dimensions.")

        width, height = _scaled_size(source_width, source_height, max_long_edge)
        output.parent.mkdir(parents=True, exist_ok=True)
        writer = cv2.VideoWriter(
            str(output),
            cv2.VideoWriter_fourcc(*"mp4v"),
            max(source_fps / max(int(frame_step), 1), 1.0),
            (width, height),
        )
        if not writer.isOpened():
            raise RuntimeError(f"Could not open CineFocus writer: {output}")

        keyframes = _normalized_keyframes(
            focus_keyframes,
            focus_x=focus_x,
            focus_y=focus_y,
            focus_radius=focus_radius,
            rack_to_x=rack_to_x,
            rack_to_y=rack_to_y,
            aperture=aperture,
            total_frames=total_frames,
        )
        rendered = 0
        samples: list[dict[str, Any]] = []
        step = max(int(frame_step), 1)
        frame_index = 0
        while True:
            ok, frame = cap.read()
            if not ok or frame is None:
                break
            if frame_index % step:
                frame_index += 1
                continue
            if (frame.shape[1], frame.shape[0]) != (width, height):
                frame = cv2.resize(frame, (width, height), interpolation=cv2.INTER_AREA)
            state = _interpolated_state(keyframes, frame_index)
            writer.write(_apply_focus(frame, state, blur_radius=blur_radius, feather=feather))
            if len(samples) < 8:
                samples.append({"frame": frame_index, **state})
            rendered += 1
            frame_index += 1
        writer.release()
    finally:
        cap.release()

    if rendered <= 0:
        raise RuntimeError("CineFocus produced no frames.")
    metadata_path = output.with_suffix(".cinefocus.json")
    metadata_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "effect": "resolve21_ai_cinefocus",
                "rendered_at": datetime.now(timezone.utc).isoformat(),
                "source_path": str(source),
                "output_path": str(output),
                "rendered_frames": rendered,
                "frame_step": max(int(frame_step), 1),
                "focus_keyframes": keyframes,
                "focal_emphasis_samples": samples,
                "parameters": {
                    "blur_radius": max(int(blur_radius), 1),
                    "feather": _clamp(float(feather), 0.01, 1.0),
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


def _scaled_size(width: int, height: int, max_long_edge: int | None) -> tuple[int, int]:
    if not max_long_edge or max(width, height) <= int(max_long_edge):
        return width, height
    scale = float(max_long_edge) / float(max(width, height))
    return max(2, int(round(width * scale))), max(2, int(round(height * scale)))


def _normalized_keyframes(
    raw: list[dict[str, Any]] | None,
    *,
    focus_x: float,
    focus_y: float,
    focus_radius: float,
    rack_to_x: float | None,
    rack_to_y: float | None,
    aperture: float,
    total_frames: int,
) -> list[dict[str, float | int]]:
    if raw:
        keyframes = [_clean_keyframe(item) for item in raw]
    else:
        keyframes = [
            {
                "frame": 0,
                "x": _clamp(focus_x, 0.0, 1.0),
                "y": _clamp(focus_y, 0.0, 1.0),
                "radius": _clamp(focus_radius, 0.05, 1.0),
                "aperture": _clamp(aperture, 0.0, 1.0),
            }
        ]
        if rack_to_x is not None or rack_to_y is not None:
            keyframes.append(
                {
                    "frame": max(total_frames - 1, 1),
                    "x": _clamp(rack_to_x if rack_to_x is not None else focus_x, 0.0, 1.0),
                    "y": _clamp(rack_to_y if rack_to_y is not None else focus_y, 0.0, 1.0),
                    "radius": _clamp(focus_radius, 0.05, 1.0),
                    "aperture": _clamp(aperture, 0.0, 1.0),
                }
            )
    keyframes.sort(key=lambda item: int(item["frame"]))
    if not keyframes:
        raise ValueError("CineFocus needs at least one focus keyframe.")
    return keyframes


def _clean_keyframe(item: dict[str, Any]) -> dict[str, float | int]:
    return {
        "frame": max(int(item.get("frame", 0)), 0),
        "x": _clamp(float(item.get("x", item.get("focus_x", 0.5))), 0.0, 1.0),
        "y": _clamp(float(item.get("y", item.get("focus_y", 0.5))), 0.0, 1.0),
        "radius": _clamp(float(item.get("radius", item.get("focus_radius", 0.28))), 0.05, 1.0),
        "aperture": _clamp(float(item.get("aperture", 0.75)), 0.0, 1.0),
    }


def _interpolated_state(keyframes: list[dict[str, float | int]], frame: int) -> dict[str, float]:
    prev = keyframes[0]
    next_item = keyframes[-1]
    for index, item in enumerate(keyframes):
        if int(item["frame"]) <= frame:
            prev = item
        if int(item["frame"]) >= frame:
            next_item = item
            break
    start = int(prev["frame"])
    end = int(next_item["frame"])
    t = 0.0 if end <= start else _clamp((frame - start) / float(end - start), 0.0, 1.0)
    return {
        name: float(prev[name]) + (float(next_item[name]) - float(prev[name])) * t
        for name in ("x", "y", "radius", "aperture")
    }


def _apply_focus(frame: np.ndarray, state: dict[str, float], *, blur_radius: int, feather: float) -> np.ndarray:
    h, w = frame.shape[:2]
    sigma = max(float(blur_radius) * (0.25 + state["aperture"]), 0.1)
    blurred = cv2.GaussianBlur(frame, (0, 0), sigmaX=sigma, sigmaY=sigma)
    yy, xx = np.ogrid[:h, :w]
    cx = state["x"] * max(w - 1, 1)
    cy = state["y"] * max(h - 1, 1)
    radius = max(state["radius"] * min(w, h), 1.0)
    dist = np.sqrt((xx - cx) ** 2 + (yy - cy) ** 2)
    blur_weight = np.clip((dist - radius) / max(radius * _clamp(feather, 0.01, 1.0), 1.0), 0.0, 1.0)
    mask = blur_weight[..., np.newaxis].astype(np.float32)
    mixed = frame.astype(np.float32) * (1.0 - mask) + blurred.astype(np.float32) * mask
    return np.clip(mixed, 0, 255).astype(np.uint8)


def _clamp(value: float, low: float, high: float) -> float:
    return min(max(float(value), low), high)


__all__ = ["CineFocusRenderResult", "render_cinefocus_plan"]
