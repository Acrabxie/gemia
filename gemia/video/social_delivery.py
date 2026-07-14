"""Resolve 21 vertical and social-resolution delivery manifests."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


from gemia.video.timeline_assets import probe_media


DEFAULT_DELIVERY_TARGETS: list[dict[str, Any]] = [
    {"id": "vertical_9x16", "label": "Vertical 9:16", "width": 1080, "height": 1920, "platform_hint": "reels_shorts_tiktok"},
    {"id": "square_1x1", "label": "Square 1:1", "width": 1080, "height": 1080, "platform_hint": "feed_square"},
    {"id": "portrait_4x5", "label": "Portrait 4:5", "width": 1080, "height": 1350, "platform_hint": "feed_portrait"},
]


def render_vertical_social_resolution_delivery_manifest(
    input_paths: list[str],
    output_dir: str,
    *,
    package_id: str = "resolve21_vertical_social_resolution_delivery",
    delivery_targets: list[dict[str, Any]] | None = None,
    safe_area_percent: float = 0.9,
    reframing_mode: str = "center_crop",
) -> str:
    """Emit Resolve-style vertical/square delivery layout metadata for real clips."""
    if not input_paths:
        raise ValueError("input_paths must contain at least one media path")
    output_root = Path(output_dir).expanduser().resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    package = _safe_id(package_id)
    safe_area = round(_clamp_float(safe_area_percent, 0.5, 1.0), 3)
    mode = _safe_choice(str(reframing_mode or "center_crop"), {"center_crop", "fit_pad", "smart_reframe"})
    targets = [_normalize_target(raw, index, safe_area) for index, raw in enumerate(delivery_targets or DEFAULT_DELIVERY_TARGETS)]

    sources = []
    for index, raw_path in enumerate(input_paths):
        source = Path(raw_path).expanduser().resolve()
        if not source.exists():
            raise FileNotFoundError(f"Input media not found: {source}")
        if not source.is_file():
            raise OSError(f"Input media is not a file: {source}")
        probe = probe_media(str(source))
        if int(probe.get("width") or 0) <= 0 or int(probe.get("height") or 0) <= 0:
            raise ValueError(f"Social delivery requires a video/image source: {source}")
        sources.append(
            {
                "clip_id": f"social_clip_{index:02d}_{_safe_id(source.stem)}",
                "source_path": str(source),
                "asset_ref": _asset_ref(source, probe),
                "source_probe": probe,
                "source_layout": _source_layout(probe),
            }
        )

    render_jobs = []
    for target in targets:
        render_jobs.append(
            {
                "job_id": f"{package}_{target['target_id']}_delivery",
                "target": target,
                "source_layouts": [_layout_decision(source, target, mode) for source in sources],
                "timeline_settings": {
                    "resolution": f"{target['width']}x{target['height']}",
                    "timeline_aspect": target["aspect_ratio"],
                    "output_scaling": "scale_entire_image_to_fit" if mode == "fit_pad" else "scale_full_frame_with_crop",
                    "reframing_mode": mode,
                    "frame_rate_policy": "preserve_source_or_project_default",
                },
                "caption_and_ui_safe": {
                    "title_safe_box": _safe_box(target["width"], target["height"], safe_area),
                    "subtitle_band": _subtitle_band(target["width"], target["height"], safe_area),
                    "avoid_platform_ui_zones": ["top_profile_chrome", "right_action_stack", "bottom_caption_controls"],
                },
                "validation": _job_validation(sources, target),
            }
        )

    manifest = {
        "schema_version": 1,
        "effect": "resolve21_vertical_social_resolution_delivery_manifest",
        "rendered_at": datetime.now(timezone.utc).isoformat(),
        "package": {
            "package_id": package,
            "clip_count": len(sources),
            "target_count": len(targets),
            "safe_area_percent": safe_area,
            "reframing_mode": mode,
        },
        "sources": sources,
        "delivery_targets": targets,
        "render_jobs": render_jobs,
        "resolve_controls": {
            "pages": ["Edit", "Deliver"],
            "timeline_resolution": "Custom vertical/square/portrait",
            "output_scaling": "Project Settings > Image Scaling",
            "deliver_preset_scope": "social_resolution_manifest_only",
        },
        "diagnostics": [
            f"{len(sources)} real clips linked to social-resolution delivery metadata",
            f"{len(render_jobs)} vertical/square render jobs emitted with safe-area boxes",
        ],
        "review_hints": [
            "manifest is metadata only and does not crop source media",
            "center_crop favors full-frame social outputs; fit_pad preserves all pixels",
            "caption_and_ui_safe boxes protect text from platform chrome",
        ],
    }
    manifest_path = output_root / "vertical_social_resolution_delivery_manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + chr(10), encoding="utf-8")
    return str(manifest_path)


def _normalize_target(raw: dict[str, Any], index: int, safe_area: float) -> dict[str, Any]:
    width = max(240, min(_optional_int(raw.get("width")) or 1080, 4320))
    height = max(240, min(_optional_int(raw.get("height")) or 1920, 4320))
    return {
        "target_id": _safe_id(str(raw.get("id") or f"social_target_{index}")),
        "label": str(raw.get("label") or raw.get("name") or f"Social target {index + 1}"),
        "width": width,
        "height": height,
        "aspect_ratio": _aspect(width, height),
        "platform_hint": _safe_id(str(raw.get("platform_hint") or raw.get("platform") or "social")),
        "safe_area_box": _safe_box(width, height, safe_area),
    }


def _layout_decision(source: dict[str, Any], target: dict[str, Any], mode: str) -> dict[str, Any]:
    probe = source["source_probe"]
    source_w = int(probe.get("width") or 1)
    source_h = int(probe.get("height") or 1)
    target_w = int(target["width"])
    target_h = int(target["height"])
    source_ratio = source_w / max(source_h, 1)
    target_ratio = target_w / max(target_h, 1)
    if mode == "fit_pad":
        scale = min(target_w / source_w, target_h / source_h)
        out_w = int(round(source_w * scale))
        out_h = int(round(source_h * scale))
        action = "pad_top_bottom" if out_w == target_w else "pad_left_right"
    else:
        scale = max(target_w / source_w, target_h / source_h)
        out_w = int(round(source_w * scale))
        out_h = int(round(source_h * scale))
        action = "crop_left_right" if source_ratio > target_ratio else "crop_top_bottom"
    return {
        "clip_id": source["clip_id"],
        "asset_ref": source["asset_ref"],
        "source_resolution": f"{source_w}x{source_h}",
        "target_resolution": f"{target_w}x{target_h}",
        "scale_factor": round(scale, 4),
        "scaled_resolution": f"{out_w}x{out_h}",
        "layout_action": action,
        "anchor": "subject_weighted_center" if mode == "smart_reframe" else "center",
        "pixel_loss_or_pad": {
            "horizontal_pixels": max(0, out_w - target_w) if mode != "fit_pad" else max(0, target_w - out_w),
            "vertical_pixels": max(0, out_h - target_h) if mode != "fit_pad" else max(0, target_h - out_h),
        },
    }


def _job_validation(sources: list[dict[str, Any]], target: dict[str, Any]) -> dict[str, Any]:
    return {
        "source_count": len(sources),
        "all_sources_have_pixels": all(int(source["source_probe"].get("width") or 0) > 0 for source in sources),
        "target_has_vertical_or_square_shape": target["height"] >= target["width"],
        "manifest_ready": bool(sources) and target["width"] > 0 and target["height"] > 0,
    }


def _source_layout(probe: dict[str, Any]) -> dict[str, Any]:
    width = int(probe.get("width") or 0)
    height = int(probe.get("height") or 0)
    return {"resolution": f"{width}x{height}", "aspect_ratio": _aspect(width, height)}


def _safe_box(width: int, height: int, percent: float) -> dict[str, int]:
    box_w = int(round(width * percent))
    box_h = int(round(height * percent))
    return {"x": (width - box_w) // 2, "y": (height - box_h) // 2, "width": box_w, "height": box_h}


def _subtitle_band(width: int, height: int, percent: float) -> dict[str, int]:
    box = _safe_box(width, height, percent)
    band_h = max(72, int(round(height * 0.14)))
    return {"x": box["x"], "y": min(height - band_h, box["y"] + box["height"] - band_h), "width": box["width"], "height": band_h}


def _asset_ref(path: Path, probe: dict[str, Any]) -> str:
    duration = round(float(probe.get("duration") or 0.0), 3)
    return f"{_safe_id(path.stem)}:{path.stat().st_size}:{duration}:{int(probe.get('width') or 0)}x{int(probe.get('height') or 0)}"


def _aspect(width: int, height: int) -> str:
    if width <= 0 or height <= 0:
        return "0:0"
    a, b = width, height
    while b:
        a, b = b, a % b
    return f"{width // a}:{height // a}"


def _optional_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except Exception:
        return None


def _clamp_float(value: Any, low: float, high: float) -> float:
    try:
        numeric = float(value)
    except Exception:
        numeric = low
    return max(low, min(numeric, high))


def _safe_choice(value: str, choices: set[str]) -> str:
    key = _safe_id(value)
    return key if key in choices else "center_crop"


def _safe_id(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", value.strip()).strip("_").lower() or "item"


__all__ = ["DEFAULT_DELIVERY_TARGETS", "render_vertical_social_resolution_delivery_manifest"]
