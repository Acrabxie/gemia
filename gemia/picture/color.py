"""Color operations: color_grade, adjust_exposure, adjust_temperature, apply_lut."""
from __future__ import annotations

import struct
from pathlib import Path

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


def apply_3d_lut(img: Image, *, lut_path: str) -> Image:
    """Apply a .cube format 3D LUT file.

    Args:
        img: Input BGR float32 image.
        lut_path: Path to .cube LUT file.

    Returns:
        LUT-applied image, float32 [0,1].
    """
    img = ensure_float32(img)
    lut_size = None
    lut_data: list[list[float]] = []

    with open(lut_path, "r") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.upper().startswith("LUT_3D_SIZE"):
                lut_size = int(line.split()[-1])
            elif line.upper().startswith("TITLE") or line.upper().startswith("DOMAIN"):
                continue
            else:
                parts = line.split()
                if len(parts) == 3:
                    try:
                        lut_data.append([float(p) for p in parts])
                    except ValueError:
                        continue

    if lut_size is None or not lut_data:
        raise ValueError(f"Invalid .cube LUT file: {lut_path}")

    cube = np.array(lut_data, dtype=np.float32).reshape(lut_size, lut_size, lut_size, 3)

    n = lut_size
    coords = img * (n - 1)
    b_idx = np.clip(coords[..., 0], 0, n - 1)
    g_idx = np.clip(coords[..., 1], 0, n - 1)
    r_idx = np.clip(coords[..., 2], 0, n - 1)

    b0 = np.floor(b_idx).astype(int)
    g0 = np.floor(g_idx).astype(int)
    r0 = np.floor(r_idx).astype(int)
    b1 = np.minimum(b0 + 1, n - 1)
    g1 = np.minimum(g0 + 1, n - 1)
    r1 = np.minimum(r0 + 1, n - 1)

    tb = (b_idx - b0)[..., np.newaxis]
    tg = (g_idx - g0)[..., np.newaxis]
    tr = (r_idx - r0)[..., np.newaxis]

    c000 = cube[b0, g0, r0]
    c001 = cube[b0, g0, r1]
    c010 = cube[b0, g1, r0]
    c011 = cube[b0, g1, r1]
    c100 = cube[b1, g0, r0]
    c101 = cube[b1, g0, r1]
    c110 = cube[b1, g1, r0]
    c111 = cube[b1, g1, r1]

    result = (
        c000 * (1 - tb) * (1 - tg) * (1 - tr) +
        c001 * (1 - tb) * (1 - tg) * tr +
        c010 * (1 - tb) * tg * (1 - tr) +
        c011 * (1 - tb) * tg * tr +
        c100 * tb * (1 - tg) * (1 - tr) +
        c101 * tb * (1 - tg) * tr +
        c110 * tb * tg * (1 - tr) +
        c111 * tb * tg * tr
    )
    return np.clip(result, 0, 1).astype(np.float32)


def color_space_convert(img: Image, *, from_space: str, to_space: str) -> Image:
    """Convert between color spaces.

    Args:
        img: Input float32 image (BGR unless from_space implies otherwise).
        from_space: Source space: 'bgr', 'srgb', 'hsl', 'lab', 'yuv'.
        to_space: Target space: same options as from_space.

    Returns:
        Converted image, float32.
    """
    img = ensure_float32(img)

    def to_bgr(x: np.ndarray, space: str) -> np.ndarray:
        u8 = np.clip(x * 255, 0, 255).astype(np.uint8)
        if space in ("bgr", "srgb"):
            return x
        elif space == "hsl":
            bgr_u8 = cv2.cvtColor(u8, cv2.COLOR_HLS2BGR)
            return bgr_u8.astype(np.float32) / 255.0
        elif space == "lab":
            bgr_u8 = cv2.cvtColor(u8, cv2.COLOR_Lab2BGR)
            return bgr_u8.astype(np.float32) / 255.0
        elif space == "yuv":
            bgr_u8 = cv2.cvtColor(u8, cv2.COLOR_YUV2BGR)
            return bgr_u8.astype(np.float32) / 255.0
        else:
            raise ValueError(f"Unknown color space: {space!r}")

    def from_bgr(x: np.ndarray, space: str) -> np.ndarray:
        u8 = np.clip(x * 255, 0, 255).astype(np.uint8)
        if space in ("bgr", "srgb"):
            return x
        elif space == "hsl":
            hls_u8 = cv2.cvtColor(u8, cv2.COLOR_BGR2HLS)
            return hls_u8.astype(np.float32) / 255.0
        elif space == "lab":
            lab_u8 = cv2.cvtColor(u8, cv2.COLOR_BGR2Lab)
            return lab_u8.astype(np.float32) / 255.0
        elif space == "yuv":
            yuv_u8 = cv2.cvtColor(u8, cv2.COLOR_BGR2YUV)
            return yuv_u8.astype(np.float32) / 255.0
        else:
            raise ValueError(f"Unknown color space: {space!r}")

    bgr = to_bgr(img, from_space.lower())
    return from_bgr(bgr, to_space.lower())


@batchable
def lift_gamma_gain(img: Image, *,
                    lift: tuple[float, float, float] = (0.0, 0.0, 0.0),
                    gamma: tuple[float, float, float] = (1.0, 1.0, 1.0),
                    gain: tuple[float, float, float] = (1.0, 1.0, 1.0)) -> Image:
    """Professional 3-way color wheel: lift (blacks), gamma (mids), gain (whites).

    Args:
        img: Input BGR float32 image.
        lift: (b,g,r) additive offset for shadows. Range [-0.5, 0.5].
        gamma: (b,g,r) gamma curve for midtones. Range [0.1, 4.0], 1.0=neutral.
        gain: (b,g,r) multiplicative scale for highlights. Range [0.0, 4.0].

    Returns:
        Graded image, float32 [0,1].
    """
    img = ensure_float32(img)
    lift_arr = np.array(lift, dtype=np.float32)
    gamma_arr = np.array(gamma, dtype=np.float32)
    gain_arr = np.array(gain, dtype=np.float32)

    result = img * gain_arr + lift_arr
    result = np.clip(result, 0, 1)
    safe_gamma = np.maximum(gamma_arr, 1e-7)
    result = np.power(result, 1.0 / safe_gamma)
    return np.clip(result, 0, 1).astype(np.float32)


@batchable
def log_to_linear(img: Image, *, log_format: str = "slog2") -> Image:
    """Convert log-encoded footage to linear light.

    Args:
        img: Log-encoded BGR float32 image.
        log_format: One of 'slog2', 'slog3', 'logc', 'log3g10'.

    Returns:
        Linear light image, float32.
    """
    img = ensure_float32(img)
    fmt = log_format.lower()

    if fmt == "slog2":
        x = img
        linear = np.where(
            x >= 0.030001222851889303,
            np.power(10.0, (x - 0.616596 - 0.03) / 0.432699) * 0.18 - 0.01,
            (x - 0.030001222851889303) / 5.0,
        )
    elif fmt == "slog3":
        x = img
        linear = np.where(
            x >= 0.1709833,
            np.power(10.0, (x - 0.420705) / 0.261) * 0.18,
            (x - 0.092864) / 5.575628,
        )
    elif fmt == "logc":
        cut1 = 0.010591
        a = 5.555556
        b = 0.052272
        c = 0.247190
        d = 0.385537
        e = 5.367655
        f = 0.092809
        linear = np.where(
            img >= e * cut1 + f,
            (np.power(10.0, (img - d) / c) - b) / a,
            (img - f) / e,
        )
    elif fmt == "log3g10":
        a = 0.224282
        b = 155.975327
        c = 0.01
        d = 0.0
        linear = np.where(
            img >= d,
            (np.power(10.0, img / a) - 1.0) / b,
            (img - d) / (a * b * np.log(10.0)),
        )
    else:
        raise ValueError(f"Unknown log format: {log_format!r}. Use 'slog2', 'slog3', 'logc', 'log3g10'.")

    return np.clip(linear, 0, None).astype(np.float32)
