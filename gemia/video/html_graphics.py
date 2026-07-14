"""HTML and Lottie-style alpha graphics overlays for real video clips."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from PIL import Image as PILImage
from PIL import ImageDraw, ImageFont

from gemia.video.lottie_renderer import select_lottie_renderer
from gemia.video.layers import render_layer_plan


@dataclass(frozen=True)
class HtmlGraphicsRenderResult:
    output_path: str
    metadata_path: str
    frame_count: int


class _HtmlBoxParser(HTMLParser):
    _NON_RENDERED_TAGS = {"script", "style"}

    def __init__(self) -> None:
        super().__init__()
        self._stack: list[dict[str, Any]] = []
        self.boxes: list[dict[str, Any]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag_name = tag.lower()
        if tag_name in self._NON_RENDERED_TAGS:
            self._stack.append({"tag": tag_name, "style": {}, "text": "", "non_rendered": True})
            return
        attr_map = {name: value or "" for name, value in attrs}
        self._stack.append({"tag": tag_name, "style": _parse_style(attr_map.get("style", "")), "text": ""})

    def handle_data(self, data: str) -> None:
        if any(item.get("non_rendered") for item in self._stack):
            return
        if self._stack:
            self._stack[-1]["text"] += data

    def handle_endtag(self, tag: str) -> None:
        if not self._stack:
            return
        item = self._stack.pop()
        if item["tag"] != tag.lower():
            return
        if item.get("non_rendered"):
            return
        text = re.sub(r"\s+", " ", item["text"]).strip()
        if text or item["style"].get("background") or item["style"].get("background-color"):
            self.boxes.append({"tag": item["tag"], "text": text, "style": item["style"]})


def render_html_graphics_plan(
    input_path: str,
    output_path: str,
    *,
    html_source: str | None = None,
    html: str | None = None,
    lottie_source: str | None = None,
    overlay_layers: list[dict[str, Any]] | None = None,
    frame_step: int = 1,
    max_long_edge: int | None = 540,
) -> str:
    """Render Resolve-style HTML graphics and Lottie alpha overlays over video."""
    raw_input_path = str(input_path or "").strip()
    source: Path | None = Path(raw_input_path).expanduser().resolve() if raw_input_path else None
    output = Path(output_path).expanduser().resolve()
    if source is not None and not source.exists():
        raise FileNotFoundError(f"HTML graphics input does not exist: {source}")
    if source is not None and source.is_dir():
        raise IsADirectoryError(f"HTML graphics input must be a media file, got directory: {source}")

    blank_canvas = source is None
    if blank_canvas:
        meta = {"width": 1920, "height": 1080, "fps": 30.0, "frames": 90}
        width, height = _scaled_size(int(meta["width"]), int(meta["height"]), max_long_edge)
        total_frames = int(meta["frames"])
        layers: list[dict[str, Any]] = [
            {
                "id": "blank_canvas_background",
                "type": "solid",
                "color": [0.015, 0.02, 0.035, 1.0],
                "start_frame": 0,
                "end_frame": total_frames,
                "z_index": 0,
            }
        ]
    else:
        meta = _video_metadata(source)
        width, height = _scaled_size(int(meta["width"]), int(meta["height"]), max_long_edge)
        scale = width / max(float(meta["width"]), 1.0)
        total_frames = int(meta["frames"] or 1)
        layers = [
            {
                "id": "source_video",
                "type": "video",
                "source": str(source),
                "start_frame": 0,
                "end_frame": total_frames,
                "scale": scale,
            }
        ]

    if overlay_layers:
        for index, layer in enumerate(overlay_layers):
            layer_spec = dict(layer)
            layer_spec.setdefault("id", f"graphic_{index + 1}")
            layer_spec.setdefault("start_frame", 0)
            layer_spec.setdefault("end_frame", total_frames)
            layer_spec.setdefault("z_index", index + 10)
            layers.append(layer_spec)
    else:
        if html_source or html:
            layers.append(
                {
                    "id": "html_title",
                    "type": "html",
                    "source": html_source,
                    "html": html,
                    "start_frame": 0,
                    "end_frame": total_frames,
                    "z_index": 10,
                    "position": (
                        [0, 0]
                        if blank_canvas
                        else [max(8, width // 18), max(8, height - max(72, height // 4))]
                    ),
                }
            )
        if lottie_source:
            layers.append(
                {
                    "id": "lottie_badge",
                    "type": "lottie",
                    "source": lottie_source,
                    "start_frame": 0,
                    "end_frame": total_frames,
                    "z_index": 11,
                    "position": [max(8, width - max(96, width // 5)), max(8, height // 14)],
                }
            )
    if len(layers) == 1:
        raise ValueError("HTML graphics render needs at least one html/lottie overlay.")

    plan = {
        "width": width,
        "height": height,
        "fps": float(meta["fps"] or 24.0),
        "total_frames": total_frames,
        "layers": layers,
    }
    rendered = render_layer_plan(plan, output, step=max(int(frame_step), 1))
    rendered_frames = max(1, (total_frames + max(int(frame_step), 1) - 1) // max(int(frame_step), 1))
    metadata_path = output.with_suffix(".html_graphics.json")
    metadata_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "effect": "resolve21_html_graphics_lottie_support",
                "rendered_at": datetime.now(timezone.utc).isoformat(),
                "source_path": str(source) if source is not None else "",
                "blank_canvas": blank_canvas,
                "output_path": str(output),
                "rendered_frames": rendered_frames,
                "frame_step": max(int(frame_step), 1),
                "overlay_count": len(layers) - 1,
                "overlay_types": [str(layer.get("type")) for layer in layers[1:]],
                "alpha_graphics": True,
                "plan": plan,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return str(Path(rendered).resolve())


def render_html_frame(source: str | None, html: str | None, *, width: int, height: int) -> np.ndarray:
    """Render a small, deterministic HTML subset to an RGBA float frame."""
    markup = html if html is not None else Path(str(source)).expanduser().read_text(encoding="utf-8")
    parser = _HtmlBoxParser()
    parser.feed(markup)
    canvas = PILImage.new("RGBA", (max(int(width), 1), max(int(height), 1)), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    fallback_text = _visible_text_from_markup(markup)
    boxes = parser.boxes or (
        [{"tag": "div", "text": fallback_text, "style": {}}] if fallback_text else []
    )
    cursor_y = 0
    for box in boxes:
        style = box["style"]
        font_size = int(_css_number(style.get("font-size"), 36 if box["tag"] in {"h1", "h2"} else 24))
        padding = int(_css_number(style.get("padding"), 12))
        left = int(_css_number(style.get("left"), 0))
        top = int(_css_number(style.get("top"), cursor_y))
        box_width = int(_css_number(style.get("width"), width - left))
        box_height = int(_css_number(style.get("height"), max(font_size + padding * 2, 1)))
        opacity = _clamp(_css_number(style.get("opacity"), 1.0), 0.0, 1.0)
        font = _font(font_size)
        bg = _css_color(style.get("background-color") or style.get("background"), default=(0, 0, 0, 0))
        fg = _css_color(style.get("color"), default=(255, 255, 255, 255))
        bg = (*bg[:3], int(bg[3] * opacity))
        fg = (*fg[:3], int(fg[3] * opacity))
        radius = int(_css_number(style.get("border-radius"), 0))
        if bg[3] > 0:
            draw.rounded_rectangle([left, top, left + box_width, top + box_height], radius=radius, fill=bg)
        text = str(box["text"])
        if text:
            draw.text((left + padding, top + padding), text, font=font, fill=fg)
        cursor_y = top + box_height + 8
    return np.asarray(canvas, dtype=np.float32) / 255.0


def lottie_renderer_metadata(source: str) -> dict[str, Any]:
    """Return renderer identity plus basic Lottie clip metadata."""
    renderer = select_lottie_renderer()
    metadata = dict(renderer.get_metadata(source))
    metadata["renderer"] = renderer.name
    metadata["source"] = str(source)
    return metadata


def render_lottie_frame(source: str, *, width: int, height: int, frame_index: int) -> np.ndarray:
    """Render a compact Lottie shape subset to an RGBA float frame."""
    renderer = select_lottie_renderer()
    return renderer.render_frame(source, width=width, height=height, frame_index=frame_index)


def lottie_metadata(source: str) -> dict[str, int | float]:
    data = json.loads(Path(source).expanduser().read_text(encoding="utf-8"))
    ip = int(data.get("ip", 0) or 0)
    op = int(data.get("op", 1) or 1)
    return {
        "width": int(data.get("w") or 1),
        "height": int(data.get("h") or 1),
        "fps": float(data.get("fr") or 30.0),
        "frames": max(op - ip, 1),
    }


def lottie_renderer_metadata(source: str) -> dict[str, Any]:
    from gemia.video.lottie_renderer import select_lottie_renderer

    renderer = select_lottie_renderer()
    meta: dict[str, Any] = dict(renderer.get_metadata(source))
    meta["renderer"] = renderer.name
    meta["source"] = source
    return meta


def _video_metadata(path: str | Path) -> dict[str, int | float]:
    cap = cv2.VideoCapture(str(path))
    try:
        if not cap.isOpened():
            raise FileNotFoundError(f"Cannot open video: {path}")
        return {
            "width": int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0),
            "height": int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0),
            "fps": float(cap.get(cv2.CAP_PROP_FPS) or 24.0) or 24.0,
            "frames": int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0),
        }
    finally:
        cap.release()


def _scaled_size(width: int, height: int, max_long_edge: int | None) -> tuple[int, int]:
    if not max_long_edge or max(width, height) <= int(max_long_edge):
        return width, height
    scale = float(max_long_edge) / float(max(width, height))
    return max(2, int(round(width * scale))), max(2, int(round(height * scale)))


def _parse_style(style: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in style.split(";"):
        if ":" in item:
            key, value = item.split(":", 1)
            out[key.strip().lower()] = value.strip()
    return out


def _visible_text_from_markup(markup: str) -> str:
    without_non_rendered = re.sub(
        r"<(script|style)\b[^>]*>.*?</\1>",
        " ",
        str(markup),
        flags=re.IGNORECASE | re.DOTALL,
    )
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", without_non_rendered)).strip()


def _css_number(value: str | None, default: float) -> float:
    if value is None:
        return float(default)
    match = re.search(r"[-+]?\d*\.?\d+", str(value))
    return float(match.group(0)) if match else float(default)


def _css_color(value: str | None, *, default: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    if not value:
        return default
    text = value.strip().lower()
    named = {"white": (255, 255, 255), "black": (0, 0, 0), "red": (255, 0, 0), "gold": (255, 196, 0)}
    if text in named:
        return (*named[text], 255)
    if text.startswith("#"):
        raw = text[1:]
        if len(raw) == 3:
            raw = "".join(ch * 2 for ch in raw)
        if len(raw) == 4:
            raw = "".join(ch * 2 for ch in raw)
        if len(raw) in {6, 8}:
            return tuple(int(raw[i:i + 2], 16) for i in range(0, len(raw), 2)) + (() if len(raw) == 8 else (255,))
    match = re.match(r"rgba?\(([^)]+)\)", text)
    if match:
        parts = [float(part.strip()) for part in match.group(1).split(",")]
        alpha = parts[3] if len(parts) > 3 else 1.0
        return (int(parts[0]), int(parts[1]), int(parts[2]), int(_clamp(alpha, 0, 1) * 255))
    return default


def _font(size: int) -> ImageFont.ImageFont | ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype("DejaVuSans.ttf", max(int(size), 1))
    except OSError:
        return ImageFont.load_default()


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


def _clamp(value: float, low: float, high: float) -> float:
    return min(max(float(value), low), high)


__all__ = [
    "HtmlGraphicsRenderResult",
    "lottie_metadata",
    "lottie_renderer_metadata",
    "render_html_frame",
    "render_html_graphics_plan",
    "render_lottie_frame",
]
