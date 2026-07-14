"""Safe-area presets for social/video canvases."""
from __future__ import annotations

from typing import Any


_PRESETS: dict[str, dict[str, Any]] = {
    "generic_vertical": {
        "width": 1080,
        "height": 1920,
        "title_margin": {"left": 90, "right": 90, "top": 180, "bottom": 300},
        "subtitle_margin": {"left": 90, "right": 90, "top": 1280, "bottom": 300},
        "avoid_zones": [
            {"id": "top_chrome", "x": 0, "y": 0, "width": 1080, "height": 150},
            {"id": "right_actions", "x": 900, "y": 540, "width": 180, "height": 820},
            {"id": "bottom_caption_controls", "x": 0, "y": 1540, "width": 1080, "height": 380},
        ],
    },
    "tiktok": {
        "width": 1080,
        "height": 1920,
        "title_margin": {"left": 96, "right": 220, "top": 180, "bottom": 360},
        "subtitle_margin": {"left": 96, "right": 220, "top": 1260, "bottom": 360},
        "avoid_zones": [
            {"id": "right_action_stack", "x": 875, "y": 510, "width": 205, "height": 820},
            {"id": "bottom_caption_and_nav", "x": 0, "y": 1500, "width": 1080, "height": 420},
            {"id": "top_status_chrome", "x": 0, "y": 0, "width": 1080, "height": 150},
        ],
    },
    "reels": {
        "width": 1080,
        "height": 1920,
        "title_margin": {"left": 90, "right": 210, "top": 170, "bottom": 330},
        "subtitle_margin": {"left": 90, "right": 210, "top": 1280, "bottom": 330},
        "avoid_zones": [
            {"id": "right_actions", "x": 880, "y": 560, "width": 200, "height": 720},
            {"id": "bottom_caption_nav", "x": 0, "y": 1510, "width": 1080, "height": 410},
        ],
    },
    "shorts": {
        "width": 1080,
        "height": 1920,
        "title_margin": {"left": 90, "right": 230, "top": 180, "bottom": 340},
        "subtitle_margin": {"left": 90, "right": 230, "top": 1280, "bottom": 340},
        "avoid_zones": [
            {"id": "right_actions", "x": 870, "y": 600, "width": 210, "height": 690},
            {"id": "bottom_title_nav", "x": 0, "y": 1500, "width": 1080, "height": 420},
        ],
    },
    "square_feed": {
        "width": 1080,
        "height": 1080,
        "title_margin": {"left": 86, "right": 86, "top": 86, "bottom": 120},
        "subtitle_margin": {"left": 86, "right": 86, "top": 760, "bottom": 120},
        "avoid_zones": [],
    },
}

_ALIASES = {
    "instagram": "reels",
    "instagram_reels": "reels",
    "ig_reels": "reels",
    "youtube_shorts": "shorts",
    "youtube": "shorts",
    "feed": "square_feed",
    "square": "square_feed",
    "generic": "generic_vertical",
}


async def dispatch(args: dict[str, Any], _ctx: Any) -> dict[str, Any]:
    platform = _normalize_platform(str(args.get("platform") or "generic_vertical"))
    width = _int(args.get("width"), _PRESETS[platform]["width"])
    height = _int(args.get("height"), _PRESETS[platform]["height"])
    preset = _scale_preset(_PRESETS[platform], width, height)
    return {
        "platform": platform,
        "width": width,
        "height": height,
        "title_safe_box": _box_from_margin(width, height, preset["title_margin"]),
        "subtitle_safe_box": _box_from_margin(width, height, preset["subtitle_margin"]),
        "avoid_zones": preset["avoid_zones"],
        "notes": [
            "Safe areas are conservative layout guides, not platform guarantees.",
            "Keep essential captions inside subtitle_safe_box and away from avoid_zones.",
        ],
    }


def _normalize_platform(value: str) -> str:
    key = value.strip().lower().replace("-", "_")
    key = _ALIASES.get(key, key)
    if key not in _PRESETS:
        return "generic_vertical"
    return key


def _scale_preset(preset: dict[str, Any], width: int, height: int) -> dict[str, Any]:
    sx = width / max(int(preset["width"]), 1)
    sy = height / max(int(preset["height"]), 1)
    return {
        "title_margin": _scale_margin(preset["title_margin"], sx, sy),
        "subtitle_margin": _scale_margin(preset["subtitle_margin"], sx, sy),
        "avoid_zones": [_scale_zone(zone, sx, sy) for zone in preset["avoid_zones"]],
    }


def _scale_margin(margin: dict[str, int], sx: float, sy: float) -> dict[str, int]:
    return {
        "left": int(round(margin["left"] * sx)),
        "right": int(round(margin["right"] * sx)),
        "top": int(round(margin["top"] * sy)),
        "bottom": int(round(margin["bottom"] * sy)),
    }


def _scale_zone(zone: dict[str, Any], sx: float, sy: float) -> dict[str, Any]:
    return {
        "id": zone["id"],
        "x": int(round(zone["x"] * sx)),
        "y": int(round(zone["y"] * sy)),
        "width": int(round(zone["width"] * sx)),
        "height": int(round(zone["height"] * sy)),
    }


def _box_from_margin(width: int, height: int, margin: dict[str, int]) -> dict[str, int]:
    x = max(0, margin["left"])
    y = max(0, margin["top"])
    right = max(x, width - max(0, margin["right"]))
    bottom = max(y, height - max(0, margin["bottom"]))
    return {"x": x, "y": y, "width": right - x, "height": bottom - y}


def _int(value: Any, default: int) -> int:
    try:
        numeric = int(value)
    except Exception:
        numeric = int(default)
    return max(240, min(numeric, 4320))


__all__ = ["dispatch"]
