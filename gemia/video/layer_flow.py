"""Planner-facing layer-first workflow entry points."""
from __future__ import annotations

import json
import hashlib
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping, Sequence
from copy import deepcopy
from pathlib import Path
from typing import Any

from PIL import Image as PILImage
from PIL import ImageDraw

from gemia.video.layers import materialize_layer_plan
from gemia.video.preview import render_shadow_preview

_CANVAS_KEYS = {"width", "height", "fps", "total_frames"}
_DEFAULT_BLANK_CANVAS = {
    "width": 1920,
    "height": 1080,
    "fps": 30.0,
    "total_frames": 90,
}
_LAYER_KEYS = {
    "id",
    "name",
    "type",
    "source",
    "text",
    "html",
    "inline_html",
    "html_source",
    "color",
    "position",
    "size",
    "font_config",
    "start_frame",
    "end_frame",
    "duration",
    "z_index",
    "opacity",
    "scale",
    "blur_radius",
    "rotation_deg",
    "blend_mode",
    "mask_source",
    "primitives",
    "keyframes",
    "metadata",
}


def render_layer_workflow(
    input_path: str,
    output_path: str,
    *,
    overlay_layers: list[dict[str, Any]] | None = None,
    title: str = "",
    title_position: Sequence[int | float] | None = None,
    title_font_size: int = 48,
    title_start_frame: int = 0,
    title_duration_frames: int | None = None,
    canvas: dict[str, Any] | None = None,
    include_source: bool = True,
    backend: str | None = "auto",
    frame_step: int = 2,
    max_long_edge: int = 540,
    proxy_resolution: int = 540,
    proxy_root: str | None = None,
) -> str:
    """Render a planner-authored layer-first video workflow via the graph backend."""
    raw_input_path = str(input_path or "").strip()
    source_path: Path | None = None
    if raw_input_path:
        source_path = Path(raw_input_path).expanduser().resolve()
        if not source_path.exists():
            raise FileNotFoundError(f"Layer workflow input does not exist: {source_path}")
        if source_path.is_dir():
            raise IsADirectoryError(f"Layer workflow input must be a media file, got directory: {source_path}")
    else:
        include_source = False

    layer_plan = _build_layer_workflow_plan(
        source_path,
        overlay_layers=overlay_layers,
        title=title,
        title_position=title_position,
        title_font_size=title_font_size,
        title_start_frame=title_start_frame,
        title_duration_frames=title_duration_frames,
        canvas=canvas,
        include_source=include_source,
    )
    _resolve_layer_asset_references(layer_plan, output_path=output_path)
    materialized_plan = materialize_layer_plan(layer_plan)
    result = render_shadow_preview(
        layer_plan,
        output_path,
        backend=backend,
        frame_step=frame_step,
        max_long_edge=max_long_edge,
        proxy_resolution=proxy_resolution,
        proxy_root=proxy_root,
    )
    _write_layer_flow_manifest(
        result.output_path,
        layer_plan=layer_plan,
        materialized_plan=materialized_plan,
        preview_manifest_path=result.manifest_path,
    )
    return result.output_path


def _build_layer_workflow_plan(
    source_path: Path | None,
    *,
    overlay_layers: list[dict[str, Any]] | None,
    title: str,
    title_position: Sequence[int | float] | None,
    title_font_size: int,
    title_start_frame: int,
    title_duration_frames: int | None,
    canvas: dict[str, Any] | None,
    include_source: bool,
) -> dict[str, Any]:
    plan: dict[str, Any] = {
        "metadata": {
            "authoring_mode": "planner_controller_layer_flow",
            "source_input": str(source_path) if source_path is not None else "",
            "blank_canvas": source_path is None,
        },
        "layers": [],
    }
    canvas_defaults = dict(_DEFAULT_BLANK_CANVAS) if source_path is None else {}
    canvas_defaults.update(dict(canvas or {}))
    for key, value in canvas_defaults.items():
        if key in _CANVAS_KEYS and value is not None:
            plan[key] = value

    if include_source and source_path is not None:
        plan["layers"].append(
            {
                "id": "source_video",
                "type": "video",
                "source": str(source_path),
                "start_frame": 0,
                "z_index": 0,
            }
        )

    for index, layer in enumerate(list(overlay_layers or []), start=1):
        plan["layers"].append(_normalize_overlay_layer(layer, source_path, index=index))

    cleaned_title = str(title or "").strip()
    if cleaned_title:
        title_layer: dict[str, Any] = {
            "id": "title_overlay",
            "type": "text",
            "text": cleaned_title,
            "position": _position_pair(title_position, default=(48, 48)),
            "font_config": {"size": max(int(title_font_size or 48), 1)},
            "start_frame": max(int(title_start_frame or 0), 0),
            "z_index": 100,
        }
        if title_duration_frames is not None:
            title_layer["duration"] = max(int(title_duration_frames), 1)
        plan["layers"].append(title_layer)

    if not plan["layers"]:
        raise ValueError("Layer workflow needs a source video, title, or overlay layer.")
    return plan


def _normalize_overlay_layer(layer: Mapping[str, Any], source_path: Path | None, *, index: int) -> dict[str, Any]:
    if not isinstance(layer, Mapping):
        raise TypeError("overlay_layers entries must be objects.")
    normalized = {
        key: deepcopy(value)
        for key, value in layer.items()
        if key in _LAYER_KEYS and value is not None
    }
    if not normalized.get("id"):
        normalized["id"] = f"overlay_{index}"
    if "html" not in normalized:
        inline_html = normalized.pop("inline_html", None)
        html_source = normalized.pop("html_source", None)
        if inline_html is not None:
            normalized["html"] = inline_html
        elif html_source is not None:
            normalized["html"] = html_source

    if not normalized.get("type"):
        if normalized.get("html"):
            normalized["type"] = "html"
        elif normalized.get("color"):
            normalized["type"] = "solid"
        else:
            normalized["type"] = "text" if normalized.get("text") else "image"
    if normalized.get("source") == "$input":
        if source_path is None:
            normalized.pop("source", None)
        else:
            normalized["source"] = str(source_path)
    if normalized.get("mask_source") == "$input":
        if source_path is None:
            normalized.pop("mask_source", None)
        else:
            normalized["mask_source"] = str(source_path)
    normalized.setdefault("start_frame", 0)
    normalized.setdefault("z_index", index)
    if normalized["type"] == "text":
        normalized["text"] = str(normalized.get("text", "")).strip()
        normalized.setdefault("position", [48, 48 + (index - 1) * 56])
        font_config = dict(normalized.get("font_config", {}) or {})
        font_config.setdefault("size", 42)
        normalized["font_config"] = font_config
    return normalized


def _resolve_layer_asset_references(layer_plan: dict[str, Any], *, output_path: str) -> None:
    cache_dir = Path(output_path).expanduser().resolve().parent / "_layer_assets"
    for layer in layer_plan.get("layers", []):
        if not isinstance(layer, dict):
            continue
        for key in ("source", "mask_source"):
            value = layer.get(key)
            if not isinstance(value, str) or not _is_http_url(value):
                continue
            local_path = _materialize_remote_or_primitive_asset(value, cache_dir=cache_dir, layer=layer)
            layer[key] = str(local_path)
            metadata = dict(layer.get("metadata", {}) or {})
            metadata.setdefault("original_source_url" if key == "source" else "original_mask_source_url", value)
            layer["metadata"] = metadata


def _is_http_url(value: str) -> bool:
    parsed = urllib.parse.urlparse(value.strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _materialize_remote_or_primitive_asset(url: str, *, cache_dir: Path, layer: Mapping[str, Any]) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    parsed = urllib.parse.urlparse(url)
    suffix = Path(parsed.path).suffix.lower() or ".bin"
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
    local_path = cache_dir / f"{Path(parsed.path).stem or 'remote'}-{digest}{suffix}"
    if local_path.exists():
        return local_path

    primitive_path = str(parsed.path).lower()
    if parsed.netloc.endswith("lumeri.ai") and "/assets/primitives/" in primitive_path:
        return _write_builtin_primitive_asset(url, local_path, layer=layer)

    request = urllib.request.Request(url, headers={"User-Agent": "Lumeri/1.0 layer-asset-fetch"})
    tmp_path = local_path.with_suffix(local_path.suffix + ".tmp")
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            data = response.read(25 * 1024 * 1024 + 1)
        if len(data) > 25 * 1024 * 1024:
            raise ValueError(f"Remote layer asset is too large: {url}")
        tmp_path.write_bytes(data)
        tmp_path.replace(local_path)
        return local_path
    except (OSError, urllib.error.URLError, ValueError) as exc:
        if tmp_path.exists():
            tmp_path.unlink()
        raise RuntimeError(f"Could not fetch remote layer asset {url}: {exc}") from exc


def _write_builtin_primitive_asset(url: str, local_path: Path, *, layer: Mapping[str, Any]) -> Path:
    name = Path(urllib.parse.urlparse(url).path).stem.lower()
    if name not in {"ball", "circle", "dot", "shadow"}:
        raise FileNotFoundError(f"Unknown Lumeri primitive asset: {url}")

    size = _layer_size(layer, default=96)
    image = PILImage.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    if name == "shadow":
        width, height = _layer_dimensions(layer, default=(size, max(8, size // 4)))
        image = PILImage.new("RGBA", (width, height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        draw.ellipse([0, 0, width - 1, height - 1], fill=(42, 194, 230, 98))
        inner_pad_x = max(1, width // 8)
        inner_pad_y = max(1, height // 4)
        draw.ellipse(
            [inner_pad_x, inner_pad_y, width - inner_pad_x, height - inner_pad_y],
            fill=(134, 235, 255, 68),
        )
        image.save(local_path)
        return local_path

    pad = max(2, int(round(size * 0.08)))
    shadow_pad = max(1, int(round(size * 0.08)))
    fill_rgba = _layer_rgba(layer.get("color"), default=(94, 215, 255, 255))
    highlight_rgba = (
        min(fill_rgba[0] + 70, 255),
        min(fill_rgba[1] + 60, 255),
        min(fill_rgba[2] + 70, 255),
        min(max(fill_rgba[3], 160), 230),
    )
    draw.ellipse(
        [pad + shadow_pad, pad + shadow_pad, size - pad + shadow_pad, size - pad + shadow_pad],
        fill=(0, 0, 0, 58),
    )
    draw.ellipse([pad, pad, size - pad, size - pad], fill=fill_rgba)
    highlight = max(3, int(round(size * 0.22)))
    draw.ellipse(
        [pad + highlight // 2, pad + highlight // 2, pad + highlight * 2, pad + highlight * 2],
        fill=highlight_rgba,
    )
    image.save(local_path)
    return local_path


def _layer_rgba(value: object, *, default: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)) and len(value) >= 3:
        channels: list[int] = []
        for index in range(4):
            raw = value[index] if index < len(value) else (1.0 if index == 3 else 0.0)
            try:
                number = float(raw)
            except (TypeError, ValueError):
                return default
            if number <= 1.0:
                number *= 255.0
            channels.append(int(max(0, min(round(number), 255))))
        return (channels[0], channels[1], channels[2], channels[3])
    return default


def _layer_size(layer: Mapping[str, Any], *, default: int) -> int:
    raw_size = layer.get("size")
    if isinstance(raw_size, Sequence) and not isinstance(raw_size, (str, bytes)) and raw_size:
        try:
            return max(1, min(int(float(raw_size[0])), 512))
        except (TypeError, ValueError):
            return default
    return default


def _layer_dimensions(layer: Mapping[str, Any], *, default: tuple[int, int]) -> tuple[int, int]:
    raw_size = layer.get("size")
    if isinstance(raw_size, Sequence) and not isinstance(raw_size, (str, bytes)) and len(raw_size) >= 2:
        try:
            width = max(1, min(int(float(raw_size[0])), 1024))
            height = max(1, min(int(float(raw_size[1])), 1024))
            return width, height
        except (TypeError, ValueError):
            return default
    return default


def _position_pair(
    value: Sequence[int | float] | None,
    *,
    default: tuple[int, int],
) -> list[int]:
    if value is None or len(value) != 2:
        return [int(default[0]), int(default[1])]
    return [int(round(float(value[0]))), int(round(float(value[1])))]


def _write_layer_flow_manifest(
    output_path: str,
    *,
    layer_plan: dict[str, Any],
    materialized_plan: dict[str, Any],
    preview_manifest_path: str,
) -> None:
    path = Path(output_path).expanduser().resolve().with_suffix(".layer-flow.json")
    payload = {
        "output_path": str(Path(output_path).expanduser().resolve()),
        "preview_manifest_path": preview_manifest_path,
        "authoring_mode": "planner_controller_layer_flow",
        "layer_count": len(materialized_plan.get("layers", [])),
        "authored_plan": layer_plan,
        "materialized_plan": materialized_plan,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


__all__ = ["render_layer_workflow"]
