"""Color operations: color_grade, adjust_exposure, adjust_temperature, apply_lut."""
from __future__ import annotations

import cv2
import numpy as np

from gemia.primitives_common import Image, batchable, ensure_float32


@batchable
def color_grade(img: Image, *,
                shadows: tuple[float, float, float] = (0.0, 0.0, 0.0),
                midtones: tuple[float, float, float] = (0.0, 0.0, 0.0),
                highlights: tuple[float, float, float] = (0.0, 0.0, 0.0),
                preset: str | None = None) -> Image:
    """Apply 3-way color grading (lift / gamma / gain).

    Either provide per-range offsets manually, or use a named *preset*.

    Args:
        img: Input image, float32 [0, 1] BGR.
        shadows: ``(b, g, r)`` offset for dark tones, each in [-1, 1].
        midtones: ``(b, g, r)`` offset for mid tones.
        highlights: ``(b, g, r)`` offset for bright tones.
        preset: One of ``'warm'``, ``'cool'``, ``'vintage'``, ``'cyberpunk'``.
            If set, overrides the manual offsets.

    Returns:
        Color-graded image, float32 [0, 1].
    """
    img = ensure_float32(img)
    if preset is not None:
        shadows, midtones, highlights = _PRESETS.get(
            preset.lower(), _PRESETS["warm"]
        )

    s = np.array(shadows, dtype=np.float32)
    m = np.array(midtones, dtype=np.float32)
    h = np.array(highlights, dtype=np.float32)

    lum = np.mean(img, axis=2, keepdims=True) if img.ndim == 3 else img
    shadow_w = 1.0 - lum
    highlight_w = lum
    mid_w = 1.0 - np.abs(lum - 0.5) * 2.0

    result = img + shadow_w * s + mid_w * m + highlight_w * h
    return np.clip(result, 0, 1).astype(np.float32)


@batchable
def adjust_exposure(img: Image, *, stops: float = 0.0) -> Image:
    """Adjust exposure by N stops.

    Args:
        img: Input image.
        stops: Exposure adjustment. +1 = double brightness, -1 = half.

    Returns:
        Exposure-adjusted image, float32 [0, 1].
    """
    img = ensure_float32(img)
    factor = 2.0 ** stops
    return np.clip(img * factor, 0, 1).astype(np.float32)


@batchable
def adjust_temperature(img: Image, *, kelvin_shift: float = 0.0) -> Image:
    """Shift colour temperature.

    Args:
        img: Input BGR image.
        kelvin_shift: Positive = warmer (more orange), negative = cooler
            (more blue).  Reasonable range is [-50, 50].

    Returns:
        Temperature-shifted image, float32 [0, 1].
    """
    img = ensure_float32(img)
    if img.ndim != 3 or img.shape[2] != 3:
        raise ValueError("adjust_temperature requires a 3-channel BGR image.")
    shift = kelvin_shift / 100.0
    result = img.copy()
    result[:, :, 0] = np.clip(result[:, :, 0] - shift, 0, 1)  # B
    result[:, :, 2] = np.clip(result[:, :, 2] + shift, 0, 1)  # R
    return result


@batchable
def apply_lut(img: Image, *, lut: np.ndarray) -> Image:
    """Apply a 1D or 3D look-up table.

    Args:
        img: Input image, float32 [0, 1] BGR.
        lut: Either a 1D array of shape ``(256,)`` (applied per channel) or a
            3D cube of shape ``(N, N, N, 3)`` where N is the LUT size
            (commonly 33 or 64).

    Returns:
        LUT-mapped image, float32 [0, 1].
    """
    img = ensure_float32(img)
    lut = np.asarray(lut, dtype=np.float32)

    if lut.ndim == 1 and lut.shape[0] == 256:
        # 1D LUT: map each channel independently
        from gemia.primitives_common import to_uint8
        u8 = to_uint8(img)
        lut_u8 = np.clip(lut * 255, 0, 255).astype(np.uint8)
        if u8.ndim == 3:
            for c in range(u8.shape[2]):
                u8[:, :, c] = lut_u8[u8[:, :, c]]
        else:
            u8 = lut_u8[u8]
        return u8.astype(np.float32) / 255.0

    elif lut.ndim == 4 and lut.shape[3] == 3:
        # 3D cube LUT
        n = lut.shape[0]
        coords = img * (n - 1)
        b_idx = np.clip(coords[..., 0], 0, n - 1).astype(np.float32)
        g_idx = np.clip(coords[..., 1], 0, n - 1).astype(np.float32)
        r_idx = np.clip(coords[..., 2], 0, n - 1).astype(np.float32)
        # Nearest-neighbour lookup for simplicity
        bi = np.clip(np.round(b_idx).astype(int), 0, n - 1)
        gi = np.clip(np.round(g_idx).astype(int), 0, n - 1)
        ri = np.clip(np.round(r_idx).astype(int), 0, n - 1)
        return lut[bi, gi, ri].astype(np.float32)

    else:
        raise ValueError(
            f"LUT must be shape (256,) for 1D or (N,N,N,3) for 3D, got {lut.shape}."
        )


# ── Presets ────────────────────────────────────────────────────────────
_PRESETS: dict[str, tuple[tuple[float, ...], ...]] = {
    # (shadows_bgr, midtones_bgr, highlights_bgr)
    "warm":     ((-0.02, 0.01, 0.04), (0.0, 0.02, 0.03), (0.0, 0.01, 0.02)),
    "cool":     ((0.04, 0.01, -0.02), (0.02, 0.0, -0.01), (0.03, 0.0, -0.01)),
    "vintage":  ((0.03, 0.02, 0.0), (-0.02, -0.01, 0.02), (-0.03, 0.0, 0.04)),
    "cyberpunk": ((0.06, -0.01, 0.04), (0.04, 0.0, 0.06), (0.03, -0.02, 0.08)),
}
