"""Pure Pillow rasterizer for deterministic Lumeri Quanta display lists."""
from __future__ import annotations

import io
import math
import re
from numbers import Real
from typing import Any, Mapping, Sequence

from PIL import Image, ImageChops, ImageDraw, ImageFont, ImageOps, UnidentifiedImageError


class QuantaRasterError(ValueError):
    """Raised when a placed slide cannot be rasterized honestly."""


_RGB_RE = re.compile(r"^rgba?\(([^()]*)\)$", re.IGNORECASE)
_GRADIENT_RE = re.compile(
    r"^linear-gradient\(180deg,\s*(#[0-9a-fA-F]{3,8}|rgba?\([^)]*\))\s+0%,\s*"
    r"(#[0-9a-fA-F]{3,8}|rgba?\([^)]*\))\s+100%\)$",
    re.IGNORECASE,
)


def _byte(value: str, name: str) -> int:
    try:
        number = float(value.strip())
    except ValueError as exc:
        raise QuantaRasterError(f"{name} must be numeric") from exc
    if not math.isfinite(number) or not 0 <= number <= 255:
        raise QuantaRasterError(f"{name} must be in [0, 255]")
    return int(round(number))


def _alpha(value: str) -> int:
    text = value.strip()
    try:
        if text.endswith("%"):
            number = float(text[:-1]) / 100.0
        else:
            number = float(text)
    except ValueError as exc:
        raise QuantaRasterError("alpha must be numeric") from exc
    if not math.isfinite(number) or not 0 <= number <= 1:
        raise QuantaRasterError("alpha must be in [0, 1]")
    return int(round(number * 255))


def _solid_color(value: Any) -> tuple[int, int, int, int]:
    text = str(value or "").strip()
    if text.startswith("#"):
        raw = text[1:]
        if len(raw) in {3, 4}:
            raw = "".join(char * 2 for char in raw)
        if len(raw) not in {6, 8} or not re.fullmatch(r"[0-9a-fA-F]+", raw):
            raise QuantaRasterError(f"unsupported hex color {text!r}")
        if len(raw) == 6:
            raw += "ff"
        return tuple(int(raw[index:index + 2], 16) for index in range(0, 8, 2))  # type: ignore[return-value]
    match = _RGB_RE.fullmatch(text)
    if not match:
        raise QuantaRasterError(f"unsupported solid color {text!r}")
    parts = [part.strip() for part in match.group(1).split(",")]
    expected = 4 if text.casefold().startswith("rgba") else 3
    if len(parts) != expected:
        raise QuantaRasterError(f"unsupported rgb color {text!r}")
    red, green, blue = (_byte(parts[index], f"rgb channel {index}") for index in range(3))
    alpha = _alpha(parts[3]) if expected == 4 else 255
    return red, green, blue, alpha


def _gradient_stops(value: Any) -> tuple[tuple[int, int, int, int], tuple[int, int, int, int]] | None:
    match = _GRADIENT_RE.fullmatch(str(value or "").strip())
    if not match:
        return None
    return _solid_color(match.group(1)), _solid_color(match.group(2))


def _scale_factor(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 4:
        raise QuantaRasterError("scale must be an integer in [1, 4]")
    return value


def _canvas(layout: Mapping[str, Any], scale: int) -> tuple[int, int]:
    raw = layout.get("canvas_px")
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)) or len(raw) != 2:
        raise QuantaRasterError("layout.canvas_px must be [width, height]")
    width, height = raw
    if (
        not isinstance(width, int) or isinstance(width, bool) or width <= 0
        or not isinstance(height, int) or isinstance(height, bool) or height <= 0
    ):
        raise QuantaRasterError("layout canvas dimensions must be positive integers")
    return width * scale, height * scale


def _rect(raw: Any, canvas: tuple[int, int], scale: int) -> tuple[int, int, int, int]:
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)) or len(raw) != 4:
        raise QuantaRasterError("primitive.rect_px must be [x, y, width, height]")
    if any(not isinstance(value, int) or isinstance(value, bool) for value in raw):
        raise QuantaRasterError("primitive.rect_px values must be integers")
    x, y, width, height = (int(value) * scale for value in raw)
    if x < 0 or y < 0 or width <= 0 or height <= 0:
        raise QuantaRasterError(f"invalid primitive rect {list(raw)!r}")
    if x + width > canvas[0] or y + height > canvas[1]:
        raise QuantaRasterError(f"primitive rect escapes canvas: {list(raw)!r}")
    return x, y, width, height


def _anchor_offset(container: int, item: int, mode: str, *, axis: str) -> int:
    if item >= container:
        return 0
    if axis == "x":
        if mode in {"left", "top-left", "bottom-left"}:
            return 0
        if mode in {"right", "top-right", "bottom-right"}:
            return container - item
    else:
        if mode in {"top", "top-left", "top-right"}:
            return 0
        if mode in {"bottom", "bottom-left", "bottom-right"}:
            return container - item
    return (container - item) // 2


def _open_image(payload: Any, asset_id: str) -> Image.Image:
    if not isinstance(payload, (bytes, bytearray, memoryview)):
        raise QuantaRasterError(f"image source {asset_id!r} must be bytes")
    try:
        with Image.open(io.BytesIO(bytes(payload))) as opened:
            image = ImageOps.exif_transpose(opened)
            image.load()
            return image.convert("RGBA")
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise QuantaRasterError(f"image source {asset_id!r} is not a decodable image") from exc


def _fit_image(source: Image.Image, size: tuple[int, int], fit: str, anchor: str) -> Image.Image:
    target_width, target_height = size
    if source.width <= 0 or source.height <= 0:
        raise QuantaRasterError("source image has empty dimensions")
    if fit == "cover":
        ratio = max(target_width / source.width, target_height / source.height)
    elif fit == "contain":
        ratio = min(target_width / source.width, target_height / source.height)
    else:
        raise QuantaRasterError(f"unsupported image fit {fit!r}")
    resized_width = max(1, int(math.ceil(source.width * ratio)))
    resized_height = max(1, int(math.ceil(source.height * ratio)))
    resized = source.resize((resized_width, resized_height), Image.Resampling.LANCZOS)
    if fit == "contain":
        result = Image.new("RGBA", size, (0, 0, 0, 0))
        x = _anchor_offset(target_width, resized_width, anchor, axis="x")
        y = _anchor_offset(target_height, resized_height, anchor, axis="y")
        result.alpha_composite(resized, (x, y))
        return result
    left = _anchor_offset(resized_width, target_width, anchor, axis="x")
    top = _anchor_offset(resized_height, target_height, anchor, axis="y")
    return resized.crop((left, top, left + target_width, top + target_height))


def _gradient(size: tuple[int, int], start: tuple[int, ...], end: tuple[int, ...]) -> Image.Image:
    width, height = size
    column = Image.new("RGBA", (1, height))
    pixels = column.load()
    denominator = max(height - 1, 1)
    for y in range(height):
        amount = y / denominator
        pixels[0, y] = tuple(
            int(round(start[channel] + (end[channel] - start[channel]) * amount))
            for channel in range(4)
        )
    return column.resize((width, height), Image.Resampling.NEAREST)


def _shape_layer(size: tuple[int, int], primitive: Mapping[str, Any], scale: int) -> Image.Image:
    fill = primitive.get("fill")
    stops = _gradient_stops(fill)
    paint = _gradient(size, *stops) if stops else Image.new("RGBA", size, _solid_color(fill))
    shape = str(primitive.get("shape") or "rect").strip().lower()
    if shape == "rect" or shape == "line":
        return paint
    if shape not in {"rounded-rect", "ellipse"}:
        raise QuantaRasterError(f"unsupported shape primitive {shape!r}")
    mask = Image.new("L", size, 0)
    draw = ImageDraw.Draw(mask)
    bounds = (0, 0, size[0] - 1, size[1] - 1)
    if shape == "ellipse":
        draw.ellipse(bounds, fill=255)
    else:
        raw_radius = primitive.get("corner_radius_px", 0)
        if not isinstance(raw_radius, int) or isinstance(raw_radius, bool) or raw_radius < 0:
            raise QuantaRasterError("corner_radius_px must be a non-negative integer")
        radius = min(raw_radius * scale, min(size) // 2)
        draw.rounded_rectangle(bounds, radius=radius, fill=255)
    paint.putalpha(ImageChops.multiply(paint.getchannel("A"), mask))
    return paint


def _render_shape(canvas: Image.Image, primitive: Mapping[str, Any], rect: tuple[int, int, int, int], scale: int) -> None:
    x, y, width, height = rect
    layer = _shape_layer((width, height), primitive, scale)
    canvas.alpha_composite(layer, (x, y))


def _render_image(
    canvas: Image.Image,
    primitive: Mapping[str, Any],
    rect: tuple[int, int, int, int],
    image_sources: Mapping[str, Any],
) -> None:
    asset_id = str(primitive.get("asset_id") or "").strip()
    if not asset_id:
        raise QuantaRasterError(
            f"image block {primitive.get('block_ref')!r} has no asset_id; fill it before rasterizing"
        )
    if asset_id not in image_sources:
        raise QuantaRasterError(f"image asset {asset_id!r} is missing from image_sources")
    source = _open_image(image_sources[asset_id], asset_id)
    fit = str(primitive.get("fit") or "cover").strip().lower()
    anchor = str(primitive.get("anchor") or "center").strip().lower()
    allowed_anchors = {
        "center", "top", "bottom", "left", "right", "top-left", "top-right",
        "bottom-left", "bottom-right",
    }
    if anchor not in allowed_anchors:
        raise QuantaRasterError(f"unsupported image anchor {anchor!r}")
    x, y, width, height = rect
    fitted = _fit_image(source, (width, height), fit, anchor)
    canvas.alpha_composite(fitted, (x, y))


def _render_text(canvas: Image.Image, primitive: Mapping[str, Any], rect: tuple[int, int, int, int], scale: int) -> None:
    style = primitive.get("style")
    if not isinstance(style, Mapping):
        raise QuantaRasterError("text primitive is missing style")
    path = str(style.get("path") or "").strip()
    face_index = style.get("face_index")
    final_size = style.get("final_size_px")
    if not path or not isinstance(face_index, int) or isinstance(face_index, bool):
        raise QuantaRasterError("text style must include path and integer face_index")
    if not isinstance(final_size, int) or isinstance(final_size, bool) or final_size <= 0:
        raise QuantaRasterError("text style.final_size_px must be a positive integer")
    try:
        font = ImageFont.truetype(path, final_size * scale, index=face_index)
    except (OSError, ValueError) as exc:
        raise QuantaRasterError(
            f"unable to load text font {path!r} face {face_index} at {final_size * scale}px"
        ) from exc
    lines = primitive.get("line_breaks")
    if not isinstance(lines, list) or any(not isinstance(line, str) for line in lines):
        raise QuantaRasterError("text primitive.line_breaks must be a list of strings")
    line_height = primitive.get("line_height_px")
    if not isinstance(line_height, int) or isinstance(line_height, bool) or line_height <= 0:
        raise QuantaRasterError("text primitive.line_height_px must be a positive integer")
    color = _solid_color(style.get("color"))
    align = str(primitive.get("align") or "left").strip().lower()
    if align not in {"left", "center", "right"}:
        raise QuantaRasterError(f"unsupported text alignment {align!r}")
    x, y, width, _height = rect
    draw = ImageDraw.Draw(canvas)
    for line_index, line in enumerate(lines):
        line_y = y + line_index * line_height * scale
        if align == "left":
            line_x = x
            anchor = "lt"
        elif align == "center":
            line_x = x + width // 2
            anchor = "mt"
        else:
            line_x = x + width
            anchor = "rt"
        draw.text((line_x, line_y), line, font=font, fill=color, anchor=anchor)


def rasterize_slide(
    placed_slide: Mapping[str, Any],
    *,
    image_sources: Mapping[str, bytes | bytearray | memoryview] | None = None,
    scale: int = 1,
) -> bytes:
    """Render one placed build state to a metadata-free deterministic PNG.

    The function performs no filesystem or registry access. Image bytes must be
    supplied by the caller under their semantic ``asset_id``.
    """
    if not isinstance(placed_slide, Mapping):
        raise QuantaRasterError("placed_slide must be a mapping")
    scale = _scale_factor(scale)
    canvas_size = _canvas(placed_slide, scale)
    background = _solid_color(placed_slide.get("background_color") or "#000000")
    if background[3] != 255:
        raise QuantaRasterError("background_color must be opaque")
    canvas = Image.new("RGBA", canvas_size, background)
    if image_sources is not None and not isinstance(image_sources, Mapping):
        raise QuantaRasterError("image_sources must be a mapping of asset_id to bytes")
    sources = image_sources or {}
    primitives = placed_slide.get("placed_blocks")
    if not isinstance(primitives, list):
        raise QuantaRasterError("placed_slide.placed_blocks must be a list")
    for index, primitive in enumerate(primitives):
        if not isinstance(primitive, Mapping):
            raise QuantaRasterError(f"placed primitive {index} must be a mapping")
        rect = _rect(primitive.get("rect_px"), canvas_size, scale)
        kind = str(primitive.get("kind") or "").strip().lower()
        if kind == "shape":
            _render_shape(canvas, primitive, rect, scale)
        elif kind == "image":
            _render_image(canvas, primitive, rect, sources)
        elif kind == "text":
            _render_text(canvas, primitive, rect, scale)
        else:
            raise QuantaRasterError(f"unsupported placed primitive kind {kind!r}")
    output = io.BytesIO()
    # The quanta canvas is fully painted by background_color. RGB avoids hidden
    # alpha differences and no pnginfo argument means no timestamps/metadata.
    canvas.convert("RGB").save(
        output,
        format="PNG",
        optimize=False,
        compress_level=9,
    )
    return output.getvalue()


__all__ = ["QuantaRasterError", "rasterize_slide"]
