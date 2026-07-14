"""Stable video masking/keying primitives for local compositing previews."""
from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from gemia.video.frames import _remux_with_audio


ColorSpec = str | tuple[float, float, float] | list[float]
PairSpec = tuple[float, float] | list[float]

_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp"}
_VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm"}
_NAMED_BGR: dict[str, tuple[float, float, float]] = {
    "black": (0.0, 0.0, 0.0),
    "white": (1.0, 1.0, 1.0),
    "green": (0.0, 1.0, 0.0),
    "blue": (1.0, 0.0, 0.0),
    "red": (0.0, 0.0, 1.0),
    "gray": (0.5, 0.5, 0.5),
    "grey": (0.5, 0.5, 0.5),
}


def render_chroma_key_preview(
    input_path: str,
    output_path: str,
    *,
    key_color: ColorSpec = "green",
    tolerance: float = 0.28,
    spill_suppress: float = 0.5,
    feather: float = 1.5,
    background_color: ColorSpec = "black",
    background_path: str | None = None,
    matte_view: bool = False,
    invert: bool = False,
) -> str:
    """Render a browser-playable green/blue-screen key preview.

    Use this for DaVinci-style chroma key, green screen, alpha matte preview,
    spill suppression, and quick background replacement. ``background_path``
    may be an image or video; when omitted the keyed area is filled with
    ``background_color``.
    """
    key_bgr = _color_bgr(key_color)
    background = _BackgroundSource(background_path, color=background_color)
    try:
        def transform(frame: np.ndarray, frame_index: int) -> np.ndarray:
            frame_f = _to_float(frame)
            alpha = _chroma_alpha(frame_f, key_bgr, tolerance=tolerance)
            if invert:
                alpha = 1.0 - alpha
            alpha = _feather_mask(alpha, feather)
            if matte_view:
                return _matte_frame(alpha)
            fg = _suppress_spill(frame_f, alpha, key_bgr, amount=spill_suppress)
            bg = background.frame(frame_index, frame.shape[1], frame.shape[0])
            return _composite(fg, bg, alpha)

        return _process_video(
            input_path,
            output_path,
            transform,
            manifest={
                "primitive": "gemia.video.masking.render_chroma_key_preview",
                "mode": "chroma",
                "key_color": _color_for_manifest(key_color),
                "tolerance": tolerance,
                "spill_suppress": spill_suppress,
                "feather": feather,
                "background_path": background_path,
                "matte_view": matte_view,
                "invert": invert,
            },
        )
    finally:
        background.close()


def render_luma_key_preview(
    input_path: str,
    output_path: str,
    *,
    low: float = 0.0,
    high: float = 0.18,
    soft: float = 0.06,
    feather: float = 1.5,
    background_color: ColorSpec = "black",
    background_path: str | None = None,
    matte_view: bool = False,
    invert: bool = False,
) -> str:
    """Render a luma-key preview that removes dark/bright luminance ranges.

    Defaults remove near-black areas. For bright/white backgrounds use
    ``low=0.82, high=1.0``. The result is composited onto a solid or media
    background and encoded as H.264 MP4.
    """
    background = _BackgroundSource(background_path, color=background_color)
    try:
        def transform(frame: np.ndarray, frame_index: int) -> np.ndarray:
            frame_f = _to_float(frame)
            luma = cv2.cvtColor(frame_f, cv2.COLOR_BGR2GRAY)
            alpha = _luma_alpha(luma, low=low, high=high, soft=soft)
            if invert:
                alpha = 1.0 - alpha
            alpha = _feather_mask(alpha, feather)
            if matte_view:
                return _matte_frame(alpha)
            bg = background.frame(frame_index, frame.shape[1], frame.shape[0])
            return _composite(frame_f, bg, alpha)

        return _process_video(
            input_path,
            output_path,
            transform,
            manifest={
                "primitive": "gemia.video.masking.render_luma_key_preview",
                "mode": "luma",
                "low": low,
                "high": high,
                "soft": soft,
                "feather": feather,
                "background_path": background_path,
                "matte_view": matte_view,
                "invert": invert,
            },
        )
    finally:
        background.close()


def render_shape_mask_preview(
    input_path: str,
    output_path: str,
    *,
    shape: str = "ellipse",
    center: PairSpec = (0.5, 0.5),
    size: PairSpec = (0.72, 0.72),
    feather: float = 0.04,
    invert: bool = False,
    outside_color: ColorSpec = "black",
    dim_outside: float = 0.45,
    matte_view: bool = False,
) -> str:
    """Render a soft rectangle/ellipse mask over a video.

    The selected region stays visible; the outside can be dimmed or replaced.
    Use it for DaVinci-style power-window previews, vignettes, quick subject
    isolation, inverse masks, and clean matte debugging.
    """
    outside_bgr = _color_bgr(outside_color)

    def transform(frame: np.ndarray, _frame_index: int) -> np.ndarray:
        frame_f = _to_float(frame)
        mask = _shape_mask(
            frame.shape[1],
            frame.shape[0],
            shape=shape,
            center=center,
            size=size,
            feather=feather,
        )
        if invert:
            mask = 1.0 - mask
        if matte_view:
            return _matte_frame(mask)
        dim = float(np.clip(dim_outside, 0.0, 1.0))
        outside = frame_f * (1.0 - dim) + outside_bgr.reshape(1, 1, 3) * dim
        return _composite(frame_f, outside, mask)

    return _process_video(
        input_path,
        output_path,
        transform,
        manifest={
            "primitive": "gemia.video.masking.render_shape_mask_preview",
            "mode": "shape",
            "shape": shape,
            "center": list(center),
            "size": list(size),
            "feather": feather,
            "invert": invert,
            "outside_color": _color_for_manifest(outside_color),
            "dim_outside": dim_outside,
            "matte_view": matte_view,
        },
    )


def render_masked_composite(
    input_path: str,
    output_path: str,
    *,
    mode: str = "chroma",
    background_path: str | None = None,
    background_color: ColorSpec = "black",
    key_color: ColorSpec = "green",
    tolerance: float = 0.28,
    spill_suppress: float = 0.5,
    low: float = 0.0,
    high: float = 0.18,
    soft: float = 0.06,
    shape: str = "ellipse",
    center: PairSpec = (0.5, 0.5),
    size: PairSpec = (0.72, 0.72),
    feather: float = 1.5,
    invert: bool = False,
    matte_view: bool = False,
) -> str:
    """Composite a video using chroma, luma, or shape-mask alpha.

    This is the general masking primitive: pick ``mode="chroma"``, ``"luma"``,
    or ``"shape"``; provide an optional background image/video; get a stable
    H.264 preview plus a sidecar manifest for review.
    """
    normalized_mode = str(mode or "chroma").strip().lower().replace("-", "_")
    if normalized_mode in {"green_screen", "greenscreen", "key", "chroma_key"}:
        normalized_mode = "chroma"
    if normalized_mode in {"luma_key", "lumakey"}:
        normalized_mode = "luma"
    if normalized_mode not in {"chroma", "luma", "shape"}:
        raise ValueError("mode must be 'chroma', 'luma', or 'shape'.")

    key_bgr = _color_bgr(key_color)
    background = _BackgroundSource(background_path, color=background_color)
    try:
        def transform(frame: np.ndarray, frame_index: int) -> np.ndarray:
            frame_f = _to_float(frame)
            if normalized_mode == "chroma":
                alpha = _chroma_alpha(frame_f, key_bgr, tolerance=tolerance)
                fg = _suppress_spill(frame_f, alpha, key_bgr, amount=spill_suppress)
            elif normalized_mode == "luma":
                luma = cv2.cvtColor(frame_f, cv2.COLOR_BGR2GRAY)
                alpha = _luma_alpha(luma, low=low, high=high, soft=soft)
                fg = frame_f
            else:
                alpha = _shape_mask(
                    frame.shape[1],
                    frame.shape[0],
                    shape=shape,
                    center=center,
                    size=size,
                    feather=feather,
                )
                fg = frame_f
            if invert:
                alpha = 1.0 - alpha
            alpha = _feather_mask(alpha, feather if normalized_mode != "shape" else 0)
            if matte_view:
                return _matte_frame(alpha)
            bg = background.frame(frame_index, frame.shape[1], frame.shape[0])
            return _composite(fg, bg, alpha)

        return _process_video(
            input_path,
            output_path,
            transform,
            manifest={
                "primitive": "gemia.video.masking.render_masked_composite",
                "mode": normalized_mode,
                "background_path": background_path,
                "background_color": _color_for_manifest(background_color),
                "key_color": _color_for_manifest(key_color),
                "tolerance": tolerance,
                "spill_suppress": spill_suppress,
                "low": low,
                "high": high,
                "soft": soft,
                "shape": shape,
                "center": list(center),
                "size": list(size),
                "feather": feather,
                "invert": invert,
                "matte_view": matte_view,
            },
        )
    finally:
        background.close()


class _BackgroundSource:
    def __init__(self, path: str | None, *, color: ColorSpec = "black") -> None:
        self.path = str(path).strip() if path else ""
        self.color = _color_bgr(color)
        self.image: np.ndarray | None = None
        self.capture: cv2.VideoCapture | None = None
        if not self.path:
            return
        source = Path(self.path).expanduser()
        if not source.exists():
            raise FileNotFoundError(f"Cannot open background media: {self.path}")
        suffix = source.suffix.lower()
        if suffix in _IMAGE_EXTS:
            raw = cv2.imread(str(source), cv2.IMREAD_COLOR)
            if raw is None:
                raise FileNotFoundError(f"Cannot open background image: {self.path}")
            self.image = _to_float(raw)
        elif suffix in _VIDEO_EXTS:
            cap = cv2.VideoCapture(str(source))
            if not cap.isOpened():
                raise FileNotFoundError(f"Cannot open background video: {self.path}")
            self.capture = cap
        else:
            raise ValueError(f"Unsupported background media format: {suffix}")

    def frame(self, frame_index: int, width: int, height: int) -> np.ndarray:
        if self.image is not None:
            return _resize_float(self.image, width, height)
        if self.capture is not None:
            ok, frame = self.capture.read()
            if not ok or frame is None:
                self.capture.set(cv2.CAP_PROP_POS_FRAMES, 0)
                ok, frame = self.capture.read()
            if ok and frame is not None:
                return _resize_float(_to_float(frame), width, height)
        solid = np.zeros((height, width, 3), dtype=np.float32)
        solid[:, :] = self.color.reshape(1, 1, 3)
        return solid

    def close(self) -> None:
        if self.capture is not None:
            self.capture.release()


def _process_video(
    input_path: str,
    output_path: str,
    transform: Any,
    *,
    manifest: dict[str, Any],
) -> str:
    source = Path(input_path).expanduser()
    if not source.exists():
        raise FileNotFoundError(f"Cannot open video: {input_path}")
    if source.is_dir():
        raise ValueError(f"Input path is a directory, not a video: {input_path}")
    output = Path(output_path).expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)

    cap = cv2.VideoCapture(str(source))
    if not cap.isOpened():
        raise FileNotFoundError(f"Cannot open video: {input_path}")
    fps = float(cap.get(cv2.CAP_PROP_FPS) or 30.0)
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
    if width <= 0 or height <= 0:
        cap.release()
        raise ValueError(f"Cannot read video size: {input_path}")

    temp = output.with_name(f"{output.stem}.{uuid.uuid4().hex[:8]}.masking-tmp.mp4")
    writer = cv2.VideoWriter(str(temp), cv2.VideoWriter_fourcc(*"mp4v"), fps, (width, height))
    frame_index = 0
    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            rendered = transform(frame, frame_index)
            rendered_u8 = _to_u8(rendered)
            if rendered_u8.shape[:2] != (height, width):
                rendered_u8 = cv2.resize(rendered_u8, (width, height), interpolation=cv2.INTER_LINEAR)
            writer.write(rendered_u8)
            frame_index += 1
    finally:
        cap.release()
        writer.release()

    if frame_index == 0:
        temp.unlink(missing_ok=True)
        raise RuntimeError(f"No frames read from video: {input_path}")
    _remux_with_audio(str(temp), str(source), str(output))
    temp.unlink(missing_ok=True)
    _write_manifest(output, manifest | {"input_path": str(source), "output_path": str(output), "frames": frame_index, "fps": fps})
    return str(output)


def _write_manifest(output: Path, manifest: dict[str, Any]) -> None:
    output.with_suffix(".masking.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _to_float(frame: np.ndarray) -> np.ndarray:
    arr = np.asarray(frame)
    if arr.dtype == np.uint8:
        return (arr.astype(np.float32) / 255.0).clip(0.0, 1.0)
    return arr.astype(np.float32).clip(0.0, 1.0)


def _to_u8(frame: np.ndarray) -> np.ndarray:
    arr = np.asarray(frame)
    if arr.dtype == np.uint8:
        return arr
    return np.clip(arr * 255.0, 0, 255).astype(np.uint8)


def _resize_float(frame: np.ndarray, width: int, height: int) -> np.ndarray:
    if frame.shape[:2] == (height, width):
        return frame.astype(np.float32)
    return cv2.resize(frame, (width, height), interpolation=cv2.INTER_LINEAR).astype(np.float32)


def _color_bgr(color: ColorSpec) -> np.ndarray:
    if isinstance(color, str):
        raw = color.strip().lower()
        if raw in _NAMED_BGR:
            return np.array(_NAMED_BGR[raw], dtype=np.float32)
        if raw.startswith("0x"):
            raw = "#" + raw[2:]
        if raw.startswith("#") and len(raw) == 7:
            red = int(raw[1:3], 16) / 255.0
            green = int(raw[3:5], 16) / 255.0
            blue = int(raw[5:7], 16) / 255.0
            return np.array((blue, green, red), dtype=np.float32)
        raise ValueError(f"Unknown color: {color!r}")
    if len(color) != 3:
        raise ValueError("Color tuples must contain three values.")
    values = np.array([float(v) for v in color], dtype=np.float32)
    if float(np.max(values)) > 1.0:
        values = values / 255.0
    return values.clip(0.0, 1.0)


def _color_for_manifest(color: ColorSpec) -> str | list[float]:
    if isinstance(color, str):
        return color
    return [float(v) for v in color]


def _chroma_alpha(frame: np.ndarray, key_bgr: np.ndarray, *, tolerance: float) -> np.ndarray:
    tol = float(np.clip(tolerance, 0.001, 0.999))
    dist = np.linalg.norm(frame - key_bgr.reshape(1, 1, 3), axis=2)
    return np.clip((dist - tol) / max(1.0 - tol, 1e-6), 0.0, 1.0).astype(np.float32)


def _luma_alpha(luma: np.ndarray, *, low: float, high: float, soft: float) -> np.ndarray:
    lo = float(np.clip(low, 0.0, 1.0))
    hi = float(np.clip(high, lo, 1.0))
    softness = max(float(soft), 1e-6)
    above_low = np.clip((luma - (lo - softness)) / softness, 0.0, 1.0)
    below_high = np.clip(((hi + softness) - luma) / softness, 0.0, 1.0)
    key_weight = above_low * below_high
    return np.clip(1.0 - key_weight, 0.0, 1.0).astype(np.float32)


def _suppress_spill(frame: np.ndarray, alpha: np.ndarray, key_bgr: np.ndarray, *, amount: float) -> np.ndarray:
    value = float(np.clip(amount, 0.0, 1.0))
    if value <= 0:
        return frame
    result = frame.copy()
    key_channel = int(np.argmax(key_bgr))
    other = [idx for idx in range(3) if idx != key_channel]
    spill = (1.0 - alpha) * value
    replacement = (result[:, :, other[0]] + result[:, :, other[1]]) * 0.5
    result[:, :, key_channel] = np.clip(result[:, :, key_channel] * (1.0 - spill) + replacement * spill, 0.0, 1.0)
    return result


def _shape_mask(
    width: int,
    height: int,
    *,
    shape: str,
    center: PairSpec,
    size: PairSpec,
    feather: float,
) -> np.ndarray:
    cx, cy = _pair(center, default=(0.5, 0.5))
    sx, sy = _pair(size, default=(0.72, 0.72))
    center_px = (_dimension_value(cx, width), _dimension_value(cy, height))
    size_px = (max(1, _dimension_value(sx, width)), max(1, _dimension_value(sy, height)))
    mask = np.zeros((height, width), dtype=np.float32)
    normalized_shape = str(shape or "ellipse").strip().lower().replace("-", "_")
    if normalized_shape in {"rect", "rectangle", "box", "window"}:
        x0 = int(round(center_px[0] - size_px[0] / 2))
        y0 = int(round(center_px[1] - size_px[1] / 2))
        x1 = int(round(center_px[0] + size_px[0] / 2))
        y1 = int(round(center_px[1] + size_px[1] / 2))
        cv2.rectangle(mask, (x0, y0), (x1, y1), 1.0, thickness=-1)
    elif normalized_shape in {"circle", "ellipse", "oval", "power_window"}:
        axes = (max(1, size_px[0] // 2), max(1, size_px[1] // 2))
        cv2.ellipse(mask, center_px, axes, 0.0, 0.0, 360.0, 1.0, thickness=-1)
    else:
        raise ValueError("shape must be 'ellipse' or 'rectangle'.")
    return _feather_mask(mask, feather)


def _pair(value: PairSpec, *, default: tuple[float, float]) -> tuple[float, float]:
    if not isinstance(value, (list, tuple)) or len(value) < 2:
        return default
    return float(value[0]), float(value[1])


def _dimension_value(value: float, dimension: int) -> int:
    if -1.0 <= value <= 1.0:
        return int(round(value * dimension))
    return int(round(value))


def _feather_mask(mask: np.ndarray, feather: float) -> np.ndarray:
    amount = float(feather)
    if amount <= 0:
        return mask.astype(np.float32).clip(0.0, 1.0)
    radius = amount * min(mask.shape[:2]) if amount < 1.0 else amount
    kernel = max(1, int(round(radius)) * 2 + 1)
    if kernel <= 1:
        return mask.astype(np.float32).clip(0.0, 1.0)
    blurred = cv2.GaussianBlur(mask.astype(np.float32), (kernel, kernel), 0)
    return blurred.clip(0.0, 1.0).astype(np.float32)


def _matte_frame(alpha: np.ndarray) -> np.ndarray:
    return np.repeat(alpha[:, :, np.newaxis], 3, axis=2).astype(np.float32)


def _composite(fg: np.ndarray, bg: np.ndarray, alpha: np.ndarray) -> np.ndarray:
    a = alpha[:, :, np.newaxis].astype(np.float32)
    return np.clip(fg * a + bg * (1.0 - a), 0.0, 1.0).astype(np.float32)
