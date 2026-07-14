"""Resolve 21 media-pool rating and tagging column manifests."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from gemia.video.timeline_assets import probe_media


DEFAULT_TAGGING_RULES: list[dict[str, Any]] = [
    {"column": "shot_type", "tag": "wide", "when": {"min_width": 960}},
    {"column": "audio_state", "tag": "sync_sound", "when": {"has_audio": True}},
    {"column": "duration_bucket", "tag": "short_take", "when": {"max_duration": 8.0}},
]


def render_media_pool_rating_tagging_columns_manifest(
    input_paths: list[str],
    output_dir: str,
    *,
    package_id: str = "resolve21_media_pool_rating_tagging_columns",
    ratings: dict[str, int] | None = None,
    tagging_rules: list[dict[str, Any]] | None = None,
    default_rating: int = 3,
    scene_label: str = "scene_001",
) -> str:
    """Emit Resolve-style media-pool rating/tagging metadata for real assets."""
    if not input_paths:
        raise ValueError("input_paths must contain at least one media path")
    output_root = Path(output_dir).expanduser().resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    package = _safe_id(package_id)
    rating_map = {_safe_id(key): _clamp_rating(value) for key, value in (ratings or {}).items()}
    rules = [_normalize_rule(rule, index) for index, rule in enumerate(tagging_rules or DEFAULT_TAGGING_RULES)]

    assets = []
    for index, raw_path in enumerate(input_paths):
        source = Path(raw_path).expanduser().resolve()
        if not source.exists():
            raise FileNotFoundError(f"Input media not found: {source}")
        if not source.is_file():
            raise OSError(f"Input media is not a file: {source}")
        probe = probe_media(str(source))
        asset_id = f"media_pool_asset_{index:02d}_{_safe_id(source.stem)}"
        rating = rating_map.get(_safe_id(source.name), rating_map.get(_safe_id(source.stem), _clamp_rating(default_rating)))
        tags = _tags_for_probe(probe, rules)
        assets.append(
            {
                "asset_id": asset_id,
                "source_path": str(source),
                "asset_ref": _asset_ref(source, probe),
                "source_probe": probe,
                "rating": rating,
                "columns": {
                    "rating": rating,
                    "scene": _safe_id(scene_label),
                    "shot": f"{_safe_id(scene_label)}_{index + 1:03d}",
                    "take": index + 1,
                    "camera": _camera_label(index),
                    "clip_color": _clip_color(rating),
                    "flags": _flags_for_rating(rating),
                },
                "tags": tags,
                "take_selection": _take_selection(rating, tags, probe),
            }
        )

    manifest = {
        "schema_version": 1,
        "effect": "resolve21_media_pool_rating_tagging_columns_manifest",
        "rendered_at": datetime.now(timezone.utc).isoformat(),
        "package": {
            "package_id": package,
            "asset_count": len(assets),
            "scene": _safe_id(scene_label),
            "rating_average": round(sum(item["rating"] for item in assets) / len(assets), 3),
            "tag_columns": sorted({rule["column"] for rule in rules}),
        },
        "assets": assets,
        "tagging_rules": rules,
        "resolve_controls": {
            "page": "Media",
            "panel": "Media Pool",
            "columns": ["Rating", "Scene", "Shot", "Take", "Camera", "Clip Color", "Flags", "Keywords"],
        },
        "review_hints": [
            "ratings are metadata only and do not modify source media",
            "asset_ref stays stable across media-pool column exports",
            "use take_selection.keep to filter review selects before timeline assembly",
        ],
    }
    manifest_path = output_root / "media_pool_rating_tagging_columns_manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return str(manifest_path)


def _normalize_rule(raw: dict[str, Any], index: int) -> dict[str, Any]:
    condition = raw.get("when") if isinstance(raw.get("when"), dict) else {}
    return {
        "rule_id": _safe_id(str(raw.get("id") or f"tag_rule_{index}")),
        "column": _safe_id(str(raw.get("column") or "keywords")),
        "tag": _safe_id(str(raw.get("tag") or raw.get("value") or f"tag_{index}")),
        "when": {
            "min_width": _optional_int(condition.get("min_width")),
            "max_duration": _optional_float(condition.get("max_duration")),
            "has_audio": condition.get("has_audio") if isinstance(condition.get("has_audio"), bool) else None,
        },
    }


def _tags_for_probe(probe: dict[str, Any], rules: list[dict[str, Any]]) -> dict[str, list[str]]:
    tags: dict[str, list[str]] = {}
    for rule in rules:
        if _rule_matches(probe, rule["when"]):
            tags.setdefault(rule["column"], []).append(rule["tag"])
    if not tags:
        tags["keywords"] = ["review"]
    return {column: sorted(set(values)) for column, values in sorted(tags.items())}


def _rule_matches(probe: dict[str, Any], condition: dict[str, Any]) -> bool:
    min_width = condition.get("min_width")
    max_duration = condition.get("max_duration")
    has_audio = condition.get("has_audio")
    if min_width is not None and int(probe.get("width") or 0) < int(min_width):
        return False
    if max_duration is not None and float(probe.get("duration") or 0.0) > float(max_duration):
        return False
    if has_audio is not None and bool(probe.get("has_audio")) is not bool(has_audio):
        return False
    return True


def _take_selection(rating: int, tags: dict[str, list[str]], probe: dict[str, Any]) -> dict[str, Any]:
    keywords = sorted({tag for values in tags.values() for tag in values})
    keep = rating >= 3 and int(probe.get("width") or 0) > 0
    return {
        "keep": keep,
        "select_priority": max(1, min(10, rating * 2 + (1 if "sync_sound" in keywords else 0))),
        "reason": "high_rating" if rating >= 4 else ("review_select" if keep else "low_rating"),
        "keywords": keywords,
    }


def _asset_ref(path: Path, probe: dict[str, Any]) -> str:
    duration = round(float(probe.get("duration") or 0.0), 3)
    return f"{_safe_id(path.stem)}:{path.stat().st_size}:{duration}:{int(probe.get('width') or 0)}x{int(probe.get('height') or 0)}"


def _camera_label(index: int) -> str:
    return f"cam_{chr(ord('a') + min(index, 25))}"


def _clip_color(rating: int) -> str:
    return "green" if rating >= 4 else ("blue" if rating == 3 else "orange")


def _flags_for_rating(rating: int) -> list[str]:
    return ["favorite"] if rating >= 4 else (["needs_review"] if rating <= 2 else [])


def _clamp_rating(value: Any) -> int:
    try:
        rating = int(value)
    except Exception:
        rating = 3
    return max(0, min(rating, 5))


def _optional_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except Exception:
        return None


def _optional_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except Exception:
        return None


def _safe_id(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", value.strip()).strip("_").lower() or "item"


__all__ = ["DEFAULT_TAGGING_RULES", "render_media_pool_rating_tagging_columns_manifest"]
