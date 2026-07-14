"""Resolve 21 Picture in Picture Resolve FX layout manifests."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from gemia.video.timeline_assets import probe_media


DEFAULT_PIP_LAYOUTS: list[dict[str, Any]] = [
    {
        "id": "corner_presenter",
        "label": "Corner presenter inset",
        "anchor": "bottom_right",
        "scale_percent": 28,
        "margin_percent": 4,
        "border_width_px": 4,
        "corner_radius_px": 12,
        "drop_shadow": True,
    },
    {
        "id": "side_by_side_review",
        "label": "Side-by-side review inset",
        "anchor": "right_center",
        "scale_percent": 42,
        "margin_percent": 3,
        "border_width_px": 2,
        "corner_radius_px": 6,
        "drop_shadow": False,
    },
]


def render_picture_in_picture_resolvefx_layout(
    input_paths: list[str],
    output_dir: str,
    *,
    package_id: str = "resolve21_picture_in_picture_resolvefx_layout",
    layout_presets: list[dict[str, Any]] | None = None,
    background_fit: str = "fill",
    inset_fit: str = "contain",
) -> str:
    """Emit Resolve FX Picture in Picture layout metadata for real clip pairs."""
    if len(input_paths) < 2:
        raise ValueError("input_paths must contain at least two media paths")
    output_root = Path(output_dir).expanduser().resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    package = _safe_id(package_id)

    sources = []
    for index, raw_path in enumerate(input_paths):
        source = Path(raw_path).expanduser().resolve()
        if not source.exists():
            raise FileNotFoundError(f"Input media not found: {source}")
        if not source.is_file():
            raise OSError(f"Input media is not a file: {source}")
        probe = probe_media(str(source))
        if int(probe.get("width") or 0) <= 0 or int(probe.get("height") or 0) <= 0:
            raise ValueError(f"PiP source has no video stream: {source}")
        sources.append(
            {
                "clip_id": f"pip_clip_{index:02d}_{_safe_id(source.stem)}",
                "role": "background" if index == 0 else "inset",
                "source_path": str(source),
                "asset_ref": _asset_ref(source, probe),
                "source_probe": probe,
                "layout_readiness": _layout_readiness(probe),
            }
        )

    presets = [_normalize_layout(raw, index) for index, raw in enumerate(layout_presets or DEFAULT_PIP_LAYOUTS)]
    background = sources[0]
    insets = sources[1:]
    layouts = []
    for preset in presets:
        for inset_index, inset in enumerate(insets):
            box = _layout_box(preset, inset_index)
            layouts.append(
                {
                    "layout_id": f"{package}_{preset['layout_id']}_{inset_index:02d}",
                    "label": preset["label"],
                    "background_asset_ref": background["asset_ref"],
                    "inset_asset_ref": inset["asset_ref"],
                    "resolvefx_controls": {
                        "effect": "Picture in Picture",
                        "page": "Edit",
                        "anchor": preset["anchor"],
                        "position_percent": {"x": box["x"], "y": box["y"]},
                        "size_percent": {"width": box["width"], "height": box["height"]},
                        "background_fit": _safe_id(background_fit),
                        "inset_fit": _safe_id(inset_fit),
                        "border_width_px": preset["border_width_px"],
                        "corner_radius_px": preset["corner_radius_px"],
                        "drop_shadow": preset["drop_shadow"],
                    },
                    "timeline_controls": {
                        "background_track": "V1",
                        "inset_track": f"V{inset_index + 2}",
                        "sync_duration_seconds": _sync_duration(background, inset),
                        "requires_transform_keyframes": False,
                    },
                    "validation": _layout_validation(preset, background, inset),
                }
            )

    manifest = {
        "schema_version": 1,
        "effect": "resolve21_picture_in_picture_resolvefx_layout",
        "rendered_at": datetime.now(timezone.utc).isoformat(),
        "package": {
            "package_id": package,
            "clip_count": len(sources),
            "layout_count": len(layouts),
            "background_fit": _safe_id(background_fit),
            "inset_fit": _safe_id(inset_fit),
        },
        "sources": sources,
        "layouts": layouts,
        "diagnostics": [
            f"{len(sources)} real clips linked to Picture in Picture layout metadata",
            f"{len(layouts)} Resolve FX layout intents emitted with stable asset refs",
        ],
        "review_hints": [
            "confirm inset does not cover important subject or captions",
            "verify border and shadow survive delivery scaling",
            "preserve asset_ref values when replacing proxy media",
        ],
    }
    manifest_path = output_root / "picture_in_picture_resolvefx_layout_manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return str(manifest_path)


def _normalize_layout(raw: dict[str, Any], index: int) -> dict[str, Any]:
    return {
        "layout_id": _safe_id(str(raw.get("id") or raw.get("layout_id") or f"pip_layout_{index}")),
        "label": str(raw.get("label") or f"Picture in Picture Layout {index + 1}"),
        "anchor": _anchor(str(raw.get("anchor") or "bottom_right")),
        "scale_percent": _clamp_float(raw.get("scale_percent", raw.get("scale", 30)), 8.0, 70.0),
        "margin_percent": _clamp_float(raw.get("margin_percent", raw.get("margin", 4)), 0.0, 20.0),
        "border_width_px": int(_clamp_float(raw.get("border_width_px", raw.get("border", 2)), 0.0, 32.0)),
        "corner_radius_px": int(_clamp_float(raw.get("corner_radius_px", raw.get("radius", 8)), 0.0, 80.0)),
        "drop_shadow": bool(raw.get("drop_shadow", True)),
    }


def _layout_box(preset: dict[str, Any], inset_index: int) -> dict[str, float]:
    size = preset["scale_percent"]
    margin = preset["margin_percent"] + inset_index * 2.0
    width = size
    height = round(size * 9.0 / 16.0, 3)
    anchor = preset["anchor"]
    x = 50.0
    y = 50.0
    if "left" in anchor:
        x = margin + width / 2.0
    if "right" in anchor:
        x = 100.0 - margin - width / 2.0
    if "top" in anchor:
        y = margin + height / 2.0
    if "bottom" in anchor:
        y = 100.0 - margin - height / 2.0
    return {"x": round(x, 3), "y": round(y, 3), "width": round(width, 3), "height": height}


def _layout_validation(preset: dict[str, Any], background: dict[str, Any], inset: dict[str, Any]) -> dict[str, Any]:
    bg_probe = background["source_probe"]
    inset_probe = inset["source_probe"]
    bg_duration = float(bg_probe.get("duration") or 0.0)
    inset_duration = float(inset_probe.get("duration") or 0.0)
    return {
        "source_count": 2,
        "background_resolution": f"{int(bg_probe.get('width') or 0)}x{int(bg_probe.get('height') or 0)}",
        "inset_resolution": f"{int(inset_probe.get('width') or 0)}x{int(inset_probe.get('height') or 0)}",
        "scale_within_comfort_range": 12.0 <= preset["scale_percent"] <= 55.0,
        "durations_overlap": min(bg_duration, inset_duration) > 0.0,
        "audio_collision_review_needed": bool(bg_probe.get("has_audio")) and bool(inset_probe.get("has_audio")),
    }


def _layout_readiness(probe: dict[str, Any]) -> dict[str, Any]:
    width = int(probe.get("width") or 0)
    height = int(probe.get("height") or 0)
    return {
        "duration_seconds": round(float(probe.get("duration") or 0.0), 3),
        "source_resolution": f"{width}x{height}",
        "aspect_ratio": round(width / max(height, 1), 3),
        "has_audio": bool(probe.get("has_audio")),
    }


def _sync_duration(background: dict[str, Any], inset: dict[str, Any]) -> float:
    return round(
        min(float(background["source_probe"].get("duration") or 0.0), float(inset["source_probe"].get("duration") or 0.0)),
        3,
    )


def _asset_ref(path: Path, probe: dict[str, Any]) -> str:
    duration = round(float(probe.get("duration") or 0.0), 3)
    return f"{_safe_id(path.stem)}:{path.stat().st_size}:{duration}:{int(probe.get('width') or 0)}x{int(probe.get('height') or 0)}"


def _anchor(value: str) -> str:
    cleaned = _safe_id(value)
    allowed = {"top_left", "top_right", "bottom_left", "bottom_right", "left_center", "right_center", "center"}
    return cleaned if cleaned in allowed else "bottom_right"


def _clamp_float(value: Any, low: float, high: float) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        numeric = 0.0
    return round(max(low, min(numeric, high)), 3)


def _safe_id(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", value.strip()).strip("_").lower() or "item"


__all__ = ["DEFAULT_PIP_LAYOUTS", "render_picture_in_picture_resolvefx_layout"]
