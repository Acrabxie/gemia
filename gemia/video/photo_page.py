"""Resolve 21 Photo-page-style still batch raw grading."""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from gemia.picture.color import adjust_exposure, adjust_temperature, color_grade
from gemia.primitives_common import to_uint8

SUPPORTED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".webp"}


def render_photo_page_batch_raw_grade(
    image_paths: list[str],
    output_dir: str,
    *,
    preset: str = "warm",
    exposure_stops: float = 0.0,
    temperature_shift: float = 0.0,
    contact_sheet_columns: int = 3,
) -> str:
    """Grade a still-image batch and write a Resolve Photo page review manifest."""
    if not image_paths:
        raise ValueError("image_paths cannot be empty")
    if contact_sheet_columns <= 0:
        raise ValueError("contact_sheet_columns must be greater than 0")

    sources = [_validate_image_path(path) for path in image_paths]
    output_root = Path(output_dir).expanduser().resolve()
    graded_dir = output_root / "graded"
    graded_dir.mkdir(parents=True, exist_ok=True)

    images: list[dict[str, Any]] = []
    for index, source in enumerate(sources):
        input_u8 = cv2.imread(str(source), cv2.IMREAD_COLOR)
        if input_u8 is None:
            raise OSError(f"Could not read image file: {source}")
        input_float = input_u8.astype(np.float32) / 255.0
        graded_float = _apply_grade(
            input_float,
            preset=preset,
            exposure_stops=exposure_stops,
            temperature_shift=temperature_shift,
        )
        output = graded_dir / f"{index:03d}_{_safe_stem(source)}_graded.png"
        if not cv2.imwrite(str(output), to_uint8(graded_float)):
            raise OSError(f"Could not write graded image: {output}")
        height, width = input_u8.shape[:2]
        images.append(
            {
                "index": index,
                "source_path": str(source),
                "output_path": str(output),
                "width": int(width),
                "height": int(height),
                "input_mean_bgr": _mean_bgr(input_float),
                "output_mean_bgr": _mean_bgr(graded_float),
                "input_luma_mean": _luma_mean(input_float),
                "output_luma_mean": _luma_mean(graded_float),
            }
        )

    contact_sheet = _write_contact_sheet(
        [Path(record["output_path"]) for record in images],
        output_root / "contact_sheet.png",
        columns=contact_sheet_columns,
    )
    manifest = {
        "schema_version": 1,
        "effect": "resolve21_photo_page_batch_raw_grade",
        "rendered_at": datetime.now(timezone.utc).isoformat(),
        "image_count": len(images),
        "output_dir": str(output_root),
        "grade_settings": {
            "preset": preset,
            "exposure_stops": float(exposure_stops),
            "temperature_shift": float(temperature_shift),
            "contact_sheet_columns": int(contact_sheet_columns),
        },
        "images": images,
        "contact_sheet": contact_sheet,
        "diagnostics": [],
        "review_hints": [
            "compare the contact sheet before approving the batch look",
            "spot-check the graded PNGs for clipped highlights or crushed shadows",
            "use the per-image mean/luma changes to catch inconsistent raw exposure",
        ],
    }
    manifest_path = output_root / "photo_page_batch_raw_grade.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return str(manifest_path)


def _validate_image_path(path: str) -> Path:
    image = Path(path).expanduser().resolve()
    if image.suffix.lower() not in SUPPORTED_IMAGE_EXTENSIONS:
        raise ValueError(f"Unsupported image extension for {image}: {image.suffix}")
    if not image.exists():
        raise FileNotFoundError(f"Image file not found: {image}")
    if not image.is_file():
        raise OSError(f"Image path is not a file: {image}")
    return image


def _apply_grade(
    image: np.ndarray,
    *,
    preset: str,
    exposure_stops: float,
    temperature_shift: float,
) -> np.ndarray:
    graded = adjust_exposure(image, stops=float(exposure_stops))
    graded = adjust_temperature(graded, kelvin_shift=float(temperature_shift))
    normalized_preset = str(preset or "").strip().lower()
    if normalized_preset and normalized_preset not in {"neutral", "none", "raw"}:
        graded = color_grade(graded, preset=normalized_preset)
    return np.clip(graded, 0.0, 1.0).astype(np.float32)


def _write_contact_sheet(image_paths: list[Path], output: Path, *, columns: int) -> dict[str, Any]:
    thumbs: list[np.ndarray] = []
    thumb_w = 180
    thumb_h = 120
    for path in image_paths:
        image = cv2.imread(str(path), cv2.IMREAD_COLOR)
        if image is None:
            raise OSError(f"Could not read graded image for contact sheet: {path}")
        resized = _letterbox_thumbnail(image, thumb_w, thumb_h)
        thumbs.append(resized)
    rows = math.ceil(len(thumbs) / columns)
    canvas = np.full((rows * thumb_h, columns * thumb_w, 3), 245, dtype=np.uint8)
    for index, thumb in enumerate(thumbs):
        row = index // columns
        col = index % columns
        y = row * thumb_h
        x = col * thumb_w
        canvas[y : y + thumb_h, x : x + thumb_w] = thumb
    output.parent.mkdir(parents=True, exist_ok=True)
    if not cv2.imwrite(str(output), canvas):
        raise OSError(f"Could not write contact sheet: {output}")
    return {
        "path": str(output),
        "columns": int(columns),
        "rows": int(rows),
        "thumbnail_width": thumb_w,
        "thumbnail_height": thumb_h,
    }


def _letterbox_thumbnail(image: np.ndarray, width: int, height: int) -> np.ndarray:
    src_h, src_w = image.shape[:2]
    scale = min(width / max(src_w, 1), height / max(src_h, 1))
    new_w = max(1, int(round(src_w * scale)))
    new_h = max(1, int(round(src_h * scale)))
    resized = cv2.resize(image, (new_w, new_h), interpolation=cv2.INTER_AREA)
    canvas = np.full((height, width, 3), 245, dtype=np.uint8)
    y = (height - new_h) // 2
    x = (width - new_w) // 2
    canvas[y : y + new_h, x : x + new_w] = resized
    return canvas


def _mean_bgr(image: np.ndarray) -> list[float]:
    return [round(float(value), 6) for value in image.mean(axis=(0, 1))]


def _luma_mean(image: np.ndarray) -> float:
    luma = image[:, :, 0] * 0.114 + image[:, :, 1] * 0.587 + image[:, :, 2] * 0.299
    return round(float(luma.mean()), 6)


def _safe_stem(path: Path) -> str:
    return "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in path.stem) or "image"


__all__ = ["SUPPORTED_IMAGE_EXTENSIONS", "render_photo_page_batch_raw_grade"]
