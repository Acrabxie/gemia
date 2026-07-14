
"""Resolve 21 Photo album, LightBox, and tether ingest manifests."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from gemia.media_ingest import probe_image_sequence


def render_photo_album_lightbox_tether_ingest(
    image_paths: list[str],
    output_dir: str,
    *,
    album_name: str = "LightBox album",
    album_tags: list[str] | None = None,
    default_rating: int = 3,
    ratings_by_name: dict[str, int] | None = None,
    tether_session: dict[str, Any] | None = None,
    lightbox_columns: int = 4,
) -> str:
    """Create a Resolve-style album/lightbox manifest from real still frames."""
    if not image_paths:
        raise ValueError("image_paths cannot be empty")
    if lightbox_columns <= 0:
        raise ValueError("lightbox_columns must be greater than 0")
    _validate_rating(default_rating)

    sources = [Path(path).expanduser().resolve() for path in image_paths]
    sequence = probe_image_sequence(sources)
    output_root = Path(output_dir).expanduser().resolve()
    output_root.mkdir(parents=True, exist_ok=True)

    tags = _clean_tags(album_tags or ["photo", "lightbox", "tether"])
    tether = _normalize_tether_session(tether_session, image_count=len(sources))
    assets: list[dict[str, Any]] = []
    for index, (source, frame) in enumerate(zip(sources, sequence["frames"])):
        rating = _rating_for(source, default_rating=default_rating, ratings_by_name=ratings_by_name or {})
        asset_tags = sorted(set(tags + [f"rating-{rating}", source.suffix.lower().lstrip(".") or "still"]))
        assets.append(
            {
                "index": index,
                "asset_id": f"album_{_safe_stem(source)}_{index:03d}",
                "source_path": str(source),
                "name": source.name,
                "width": int(frame.get("width") or 0),
                "height": int(frame.get("height") or 0),
                "fingerprint": str(frame.get("fingerprint") or ""),
                "rating": rating,
                "tags": asset_tags,
                "capture": {
                    "capture_index": index,
                    "tether_session_id": tether["session_id"],
                    "camera_model": tether["camera_model"],
                    "capture_state": "ingested",
                    "live_view_supported": bool(tether["live_view_supported"]),
                },
                "ingest_metadata": frame,
            }
        )

    contact_sheet = _write_lightbox_contact_sheet(sources, output_root / "lightbox_contact_sheet.png", lightbox_columns, assets)
    manifest = {
        "schema_version": 1,
        "effect": "resolve21_photo_album_lightbox_tether_ingest",
        "rendered_at": datetime.now(timezone.utc).isoformat(),
        "album": {
            "name": str(album_name or "LightBox album"),
            "slug": _slug(album_name or "LightBox album"),
            "asset_count": len(assets),
            "tags": tags,
            "rating_summary": _rating_summary(assets),
        },
        "lightbox": contact_sheet,
        "tether_session": tether,
        "sequence": {
            "frame_count": sequence["frame_count"],
            "consistent_dimensions": sequence["consistent_dimensions"],
            "sequence_fingerprint": sequence["sequence_fingerprint"],
            "dimensions_summary": sequence["dimensions_summary"],
            "diagnostics": sequence["diagnostics"],
        },
        "assets": assets,
        "review_hints": [
            "open the lightbox contact sheet before approving selects",
            "check rating tags against the intended album selects",
            "confirm tether session metadata before relinking captured stills",
        ],
    }
    manifest_path = output_root / "photo_album_lightbox_tether_ingest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return str(manifest_path)


def _write_lightbox_contact_sheet(
    image_paths: list[Path],
    output: Path,
    columns: int,
    assets: list[dict[str, Any]],
) -> dict[str, Any]:
    thumb_w, thumb_h = 176, 120
    rows = int(np.ceil(len(image_paths) / columns))
    canvas = np.full((rows * thumb_h, columns * thumb_w, 3), 238, dtype=np.uint8)
    cells = []
    for index, path in enumerate(image_paths):
        image = cv2.imread(str(path), cv2.IMREAD_COLOR)
        if image is None:
            raise OSError(f"Could not read image file: {path}")
        thumb = _letterbox(image, thumb_w, thumb_h)
        row, col = divmod(index, columns)
        x, y = col * thumb_w, row * thumb_h
        canvas[y : y + thumb_h, x : x + thumb_w] = thumb
        cv2.putText(canvas, f"{index + 1} R{assets[index]['rating']}", (x + 8, y + 20), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (20, 20, 20), 1, cv2.LINE_AA)
        cells.append({"index": index, "x": x, "y": y, "width": thumb_w, "height": thumb_h, "asset_id": assets[index]["asset_id"]})
    output.parent.mkdir(parents=True, exist_ok=True)
    if not cv2.imwrite(str(output), canvas):
        raise OSError(f"Could not write lightbox contact sheet: {output}")
    return {"path": str(output), "columns": columns, "rows": rows, "thumbnail_width": thumb_w, "thumbnail_height": thumb_h, "cells": cells}


def _letterbox(image: np.ndarray, width: int, height: int) -> np.ndarray:
    src_h, src_w = image.shape[:2]
    scale = min(width / max(src_w, 1), height / max(src_h, 1))
    new_w = max(1, int(round(src_w * scale)))
    new_h = max(1, int(round(src_h * scale)))
    resized = cv2.resize(image, (new_w, new_h), interpolation=cv2.INTER_AREA)
    canvas = np.full((height, width, 3), 246, dtype=np.uint8)
    y = (height - new_h) // 2
    x = (width - new_w) // 2
    canvas[y : y + new_h, x : x + new_w] = resized
    return canvas


def _normalize_tether_session(session: dict[str, Any] | None, *, image_count: int) -> dict[str, Any]:
    payload = dict(session or {})
    return {
        "session_id": str(payload.get("session_id") or "tether_local_session"),
        "provider": str(payload.get("provider") or "local_still_ingest"),
        "camera_model": str(payload.get("camera_model") or "offline camera"),
        "capture_count": image_count,
        "live_view_supported": bool(payload.get("live_view_supported", True)),
        "started_at": str(payload.get("started_at") or datetime.now(timezone.utc).isoformat()),
    }


def _rating_for(path: Path, *, default_rating: int, ratings_by_name: dict[str, int]) -> int:
    for key in (str(path), path.name, path.stem):
        if key in ratings_by_name:
            return _validate_rating(ratings_by_name[key])
    return _validate_rating(default_rating)


def _validate_rating(value: int) -> int:
    rating = int(value)
    if rating < 0 or rating > 5:
        raise ValueError("ratings must be between 0 and 5")
    return rating


def _clean_tags(tags: list[str]) -> list[str]:
    cleaned = []
    for tag in tags:
        value = re.sub(r"\s+", "-", str(tag).strip().lower())
        if value:
            cleaned.append(value)
    return sorted(set(cleaned)) or ["photo"]


def _rating_summary(assets: list[dict[str, Any]]) -> dict[str, int]:
    summary = {str(value): 0 for value in range(6)}
    for asset in assets:
        summary[str(asset["rating"])] += 1
    return summary


def _safe_stem(path: Path) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", path.stem).strip("_") or "still"


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(value).strip().lower()).strip("-") or "album"


__all__ = ["render_photo_album_lightbox_tether_ingest"]
