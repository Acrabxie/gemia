"""Lottie animation renderer boundary and adapters."""
from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import numpy as np
from PIL import Image as PILImage
from PIL import ImageDraw


@runtime_checkable
class LottieRenderer(Protocol):
    """Protocol for Lottie animation renderers."""

    def render_frame(self, source: str, *, width: int, height: int, frame_index: int) -> np.ndarray:
        """Render a single frame to an RGBA float32 array in [0, 1]."""
        ...

    def get_metadata(self, source: str) -> dict[str, Any]:
        """Return Lottie metadata (width, height, fps, frames)."""
        ...

    @property
    def name(self) -> str:
        """Name of the renderer."""
        ...


class DeterministicLottieRenderer:
    """Pure-Python fallback Lottie renderer using PIL."""

    @property
    def name(self) -> str:
        return "deterministic_pil"

    def render_frame(self, source: str, *, width: int, height: int, frame_index: int) -> np.ndarray:
        # Implementation moved from html_graphics.py
        data = load_lottie_json(source)
        source_w = int(data.get("w") or width or 1)
        source_h = int(data.get("h") or height or 1)
        scale_x = max(int(width), 1) / max(source_w, 1)
        scale_y = max(int(height), 1) / max(source_h, 1)
        canvas = PILImage.new("RGBA", (max(int(width), 1), max(int(height), 1)), (0, 0, 0, 0))
        draw = ImageDraw.Draw(canvas)

        for layer in data.get("layers", []):
            if int(layer.get("ty", 4)) != 4:
                continue
            ip = int(layer.get("ip", data.get("ip", 0)) or 0)
            op = int(layer.get("op", frame_index + 1))
            if frame_index < ip or frame_index >= op:
                continue
            transform = layer.get("ks") or {}
            opacity = _clamp(_animated_value(transform.get("o", {"k": 100}), frame_index) / 100.0, 0.0, 1.0)
            position = _animated_list(transform.get("p", {"k": [source_w / 2, source_h / 2, 0]}), frame_index)
            layer_scale = _animated_list(transform.get("s", {"k": [100, 100, 100]}), frame_index)
            fill = (255, 255, 255, int(255 * opacity))
            pending_shapes: list[dict[str, Any]] = []
            for shape in _flatten_shapes(layer.get("shapes", [])):
                if shape.get("ty") == "fl":
                    color = shape.get("c", {}).get("k", [1, 1, 1, 1])
                    fill_opacity = _animated_value(shape.get("o", {"k": 100}), frame_index) / 100.0
                    fill = (
                        int(_clamp(float(color[0]), 0, 1) * 255),
                        int(_clamp(float(color[1]), 0, 1) * 255),
                        int(_clamp(float(color[2]), 0, 1) * 255),
                        int(255 * _clamp(opacity * fill_opacity, 0, 1)),
                    )
                    for pending in pending_shapes:
                        _draw_shape(
                            draw,
                            pending,
                            fill=fill,
                            frame_index=frame_index,
                            position=position,
                            layer_scale=layer_scale,
                            scale_x=scale_x,
                            scale_y=scale_y,
                        )
                    pending_shapes.clear()
                elif shape.get("ty") in {"rc", "el"}:
                    pending_shapes.append(shape)
            for pending in pending_shapes:
                _draw_shape(
                    draw,
                    pending,
                    fill=fill,
                    frame_index=frame_index,
                    position=position,
                    layer_scale=layer_scale,
                    scale_x=scale_x,
                    scale_y=scale_y,
                )
        return np.asarray(canvas, dtype=np.float32) / 255.0

    def get_metadata(self, source: str) -> dict[str, Any]:
        data = load_lottie_json(source)
        ip = int(data.get("ip", 0) or 0)
        op = int(data.get("op", 1) or 1)
        return {
            "width": int(data.get("w") or 1),
            "height": int(data.get("h") or 1),
            "fps": float(data.get("fr") or 30.0),
            "frames": max(op - ip, 1),
        }


class RlottieRenderer:
    """Optional rlottie-backed renderer boundary."""

    def __init__(self, rlottie_module: Any = None) -> None:
        self._rlottie = rlottie_module
        self._cache: dict[str, Any] = {}

    @property
    def name(self) -> str:
        return "rlottie"

    def is_available(self) -> bool:
        if self._rlottie is not None:
            return True
        try:
            import rlottie_python  # type: ignore
            self._rlottie = rlottie_python
            return True
        except ImportError:
            try:
                import rlottie  # type: ignore
                self._rlottie = rlottie
                return True
            except ImportError:
                return False

    def render_frame(self, source: str, *, width: int, height: int, frame_index: int) -> np.ndarray:
        if not self.is_available():
            raise RuntimeError("rlottie is not available.")
        
        # Simplified boundary for rlottie integration
        # Many rlottie-python bindings use a LottieAnimation(path) interface
        anim = self._get_animation(source)
        # Expected interface: anim.render_frame(frame_index, width, height) -> np.ndarray (RGBA uint8)
        # If it doesn't match, we would need more complex adaptation.
        # Given this is a boundary, we'll try a common pattern.
        try:
            frame_u8 = anim.render_frame(frame_index, width, height)
            return _normalize_rgba_frame(frame_u8, width=width, height=height)
        except Exception as e:
            raise RuntimeError(f"rlottie render failed: {e}") from e

    def get_metadata(self, source: str) -> dict[str, Any]:
        if not self.is_available():
            raise RuntimeError("rlottie is not available.")
        anim = self._get_animation(source)
        width = int(getattr(anim, "width", 0))
        height = int(getattr(anim, "height", 0))
        fps = float(getattr(anim, "fps", 30.0))
        frames = int(getattr(anim, "total_frames", 1))
        if width <= 0 or height <= 0 or fps <= 0 or frames <= 0:
            raise RuntimeError("rlottie metadata is invalid.")
        return {"width": width, "height": height, "fps": fps, "frames": frames}

    def _get_animation(self, source: str) -> Any:
        if source not in self._cache:
            # Assuming LottieAnimation(path) or similar
            if hasattr(self._rlottie, "LottieAnimation"):
                self._cache[source] = self._rlottie.LottieAnimation(source)
            else:
                # Fallback to deterministic if binding is weird
                raise RuntimeError("rlottie module missing LottieAnimation.")
        return self._cache[source]


def select_lottie_renderer() -> LottieRenderer:
    """Select the best available Lottie renderer."""
    rlottie = RlottieRenderer()
    if rlottie.is_available():
        return FallbackLottieRenderer(rlottie, DeterministicLottieRenderer())
    return DeterministicLottieRenderer()


def load_lottie_json(source: str | Path) -> dict[str, Any]:
    """Load a Lottie JSON document from .json or the first animation in .lottie."""
    path = Path(source).expanduser()
    if path.suffix.lower() == ".lottie":
        with zipfile.ZipFile(path) as archive:
            candidates = [
                name for name in archive.namelist()
                if name.lower().endswith(".json") and (name.startswith("animations/") or name.endswith("manifest.json"))
            ]
            animation_names = [name for name in candidates if name.startswith("animations/")]
            names = animation_names or [name for name in candidates if not name.endswith("manifest.json")]
            if not names:
                raise ValueError(f"dotLottie archive has no animation JSON: {path}")
            with archive.open(sorted(names)[0]) as handle:
                data = json.loads(handle.read().decode("utf-8"))
    else:
        data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or "layers" not in data:
        raise ValueError(f"not a Lottie animation document: {path}")
    return data


def save_lottie_frame_png(
    source: str | Path,
    output: str | Path,
    *,
    width: int | None = None,
    height: int | None = None,
    frame_index: int = 0,
) -> dict[str, Any]:
    """Render one Lottie frame to PNG and return metadata about the frame."""
    renderer = select_lottie_renderer()
    metadata = renderer.get_metadata(str(source))
    out_width = max(int(width or metadata.get("width") or 512), 1)
    out_height = max(int(height or metadata.get("height") or 512), 1)
    frame_count = max(int(metadata.get("frames") or 1), 1)
    frame = max(0, min(int(frame_index), frame_count - 1))
    arr = renderer.render_frame(str(source), width=out_width, height=out_height, frame_index=frame)
    rgba = np.clip(arr * 255.0, 0, 255).astype(np.uint8)
    output_path = Path(output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    PILImage.fromarray(rgba, mode="RGBA").save(output_path)
    return {
        **metadata,
        "frame_index": frame,
        "renderer": renderer.name,
        "output_path": str(output_path),
    }


# Helper functions from html_graphics.py

def _clamp(value: float, low: float, high: float) -> float:
    return min(max(float(value), low), high)


def _animated_value(prop: dict[str, Any], frame: int) -> float:
    value = prop.get("k", prop)
    if isinstance(value, list) and value and isinstance(value[0], dict):
        interpolated = _interpolate_keyframes(value, frame)
        if isinstance(interpolated, list):
            return float(interpolated[0])
        return float(interpolated)
    if isinstance(value, list):
        return float(value[0])
    return float(value)


def _animated_list(prop: dict[str, Any], frame: int) -> list[float]:
    value = prop.get("k", prop)
    if isinstance(value, list) and value and isinstance(value[0], dict):
        value = _interpolate_keyframes(value, frame)
    if isinstance(value, list):
        return [float(item) for item in value[:3]]
    return [float(value), float(value), 0.0]


def _interpolate_keyframes(items: list[dict[str, Any]], frame: int) -> Any:
    previous = items[0]
    next_item = items[-1]
    for index, item in enumerate(items):
        if int(item.get("t", 0)) <= frame:
            previous = item
        if int(item.get("t", 0)) >= frame:
            next_item = item
            break
        if index + 1 < len(items):
            next_item = items[index + 1]
    start = int(previous.get("t", 0))
    end = int(next_item.get("t", start))
    t = 0.0 if end <= start else _clamp((frame - start) / float(end - start), 0.0, 1.0)
    a = previous.get("s", previous.get("e", 0))
    b = previous.get("e", next_item.get("s", a))
    if isinstance(a, list) and isinstance(b, list):
        return [float(x) + (float(y) - float(x)) * t for x, y in zip(a, b)]
    return float(a) + (float(b) - float(a)) * t


def _flatten_shapes(shapes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for shape in shapes:
        if shape.get("ty") == "gr":
            out.extend(_flatten_shapes(shape.get("it", [])))
        else:
            out.append(shape)
    return out


class FallbackLottieRenderer:
    """Renderer wrapper that keeps deterministic output available if rlottie fails."""

    def __init__(self, primary: LottieRenderer, fallback: LottieRenderer) -> None:
        self._primary = primary
        self._fallback = fallback

    @property
    def name(self) -> str:
        return f"{self._primary.name}_with_{self._fallback.name}_fallback"

    def render_frame(self, source: str, *, width: int, height: int, frame_index: int) -> np.ndarray:
        try:
            return self._primary.render_frame(source, width=width, height=height, frame_index=frame_index)
        except Exception:
            return self._fallback.render_frame(source, width=width, height=height, frame_index=frame_index)

    def get_metadata(self, source: str) -> dict[str, Any]:
        try:
            return self._primary.get_metadata(source)
        except Exception:
            return self._fallback.get_metadata(source)


def _draw_shape(
    draw: ImageDraw.ImageDraw,
    shape: dict[str, Any],
    *,
    fill: tuple[int, int, int, int],
    frame_index: int,
    position: list[float],
    layer_scale: list[float],
    scale_x: float,
    scale_y: float,
) -> None:
    size = _animated_list(shape.get("s", {"k": [80, 80]}), frame_index)
    pos = _animated_list(shape.get("p", {"k": [0, 0]}), frame_index)
    cx = (position[0] + pos[0]) * scale_x
    cy = (position[1] + pos[1]) * scale_y
    w = size[0] * (layer_scale[0] / 100.0) * scale_x
    h = size[1] * (layer_scale[1] / 100.0) * scale_y
    bounds = [cx - w / 2.0, cy - h / 2.0, cx + w / 2.0, cy + h / 2.0]
    if shape.get("ty") == "el":
        draw.ellipse(bounds, fill=fill)
    else:
        draw.rounded_rectangle(bounds, radius=float(shape.get("r", {}).get("k", 0) or 0), fill=fill)


def _normalize_rgba_frame(frame: Any, *, width: int, height: int) -> np.ndarray:
    arr = np.asarray(frame)
    expected_shape = (max(int(height), 1), max(int(width), 1), 4)
    if arr.shape != expected_shape:
        raise RuntimeError(f"rlottie frame has shape {arr.shape}, expected {expected_shape}.")
    if not np.all(np.isfinite(arr)):
        raise RuntimeError("rlottie frame contains non-finite values.")
    if np.issubdtype(arr.dtype, np.integer):
        if int(arr.min()) < 0 or int(arr.max()) > 255:
            raise RuntimeError("rlottie integer frame values must be in [0, 255].")
        return arr.astype(np.float32) / 255.0
    arr = arr.astype(np.float32, copy=False)
    min_value = float(arr.min())
    max_value = float(arr.max())
    if min_value < 0.0:
        raise RuntimeError("rlottie float frame values must not be negative.")
    if max_value <= 1.0:
        return arr.astype(np.float32, copy=False)
    if max_value <= 255.0:
        return arr.astype(np.float32, copy=False) / 255.0
    raise RuntimeError("rlottie float frame values must be in [0, 1] or [0, 255].")
