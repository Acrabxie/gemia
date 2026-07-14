"""DaVinci-style *curves* colour tool for lumenframe.

A curve is a set of control points ``[[x, y], ...]`` in ``[0, 1]`` that defines
an input→output transfer function. We build a 256-entry, monotone lookup table
(LUT) from those points and apply it per channel. The transfer is applied to a
single channel (``r`` / ``g`` / ``b``), to all RGB channels together
(``rgb``), or to a computed ``luma`` channel (which scales RGB by the per-pixel
luma ratio so hue is preserved). Alpha is always passed through untouched, and
the operation is a pure function of the frame, so curves stack cleanly with one
another and with other effects.

Monotonicity matters: a non-monotone LUT would fold the tone scale back on
itself (two inputs mapping through a dip), producing posterisation/banding. We
interpolate with the Fritsch–Carlson monotone cubic Hermite scheme, which is
smooth (DaVinci-like S-curves look like curves, not polylines) yet provably
monotone whenever the control points are monotone, and degrades to clamped
linear behaviour at the segment ends.

The identity curve ``[[0, 0], [1, 1]]`` produces a LUT equal (within float
tolerance) to ``i / 255`` and is therefore a no-op.

This kernel is intentionally standalone (NumPy only) and does not depend on the
renderer's dispatch table, so it can be unit-tested in isolation.
"""
from __future__ import annotations

from typing import Sequence

import numpy as np

LUT_SIZE = 256

_LUMA_WEIGHTS = (0.299, 0.587, 0.114)


def _normalize_points(points: Sequence[Sequence[float]]) -> tuple[np.ndarray, np.ndarray]:
    """Return sorted, deduped, clamped (xs, ys) arrays from raw control points.

    Points are clamped to ``[0, 1]``, sorted by x, and deduplicated on x
    (keeping the last y for a repeated x). The endpoints ``x=0`` and ``x=1`` are
    synthesised by clamping if the caller didn't provide them, so the LUT always
    spans the full input range. Falls back to the identity ``[[0,0],[1,1]]`` for
    empty / degenerate input.
    """
    pts: list[tuple[float, float]] = []
    for p in points or []:
        try:
            x = float(p[0])
            y = float(p[1])
        except (TypeError, IndexError, ValueError):
            continue
        x = min(1.0, max(0.0, x))
        y = min(1.0, max(0.0, y))
        pts.append((x, y))

    if len(pts) < 2:
        pts = [(0.0, 0.0), (1.0, 1.0)]

    pts.sort(key=lambda t: t[0])

    # Deduplicate on x (keep last), preserving sorted order.
    dedup: dict[float, float] = {}
    for x, y in pts:
        dedup[x] = y
    xs = sorted(dedup)
    ys = [dedup[x] for x in xs]

    if len(xs) < 2:
        xs = [0.0, 1.0]
        ys = [ys[0], ys[0]] if ys else [0.0, 1.0]

    return np.asarray(xs, dtype=np.float64), np.asarray(ys, dtype=np.float64)


def _fritsch_carlson_slopes(xs: np.ndarray, ys: np.ndarray) -> np.ndarray:
    """Tangents for monotone cubic Hermite interpolation (Fritsch–Carlson)."""
    n = len(xs)
    h = np.diff(xs)
    delta = np.diff(ys) / h  # secant slopes

    m = np.empty(n, dtype=np.float64)
    # Interior tangents: average of adjacent secants, zeroed across extrema.
    m[1:-1] = (delta[:-1] + delta[1:]) / 2.0
    m[0] = delta[0]
    m[-1] = delta[-1]
    for i in range(1, n - 1):
        if delta[i - 1] * delta[i] <= 0.0:
            m[i] = 0.0

    # Fritsch–Carlson limiter: keep tangents in the monotone region.
    for i in range(n - 1):
        if delta[i] == 0.0:
            m[i] = 0.0
            m[i + 1] = 0.0
            continue
        a = m[i] / delta[i]
        b = m[i + 1] / delta[i]
        s = a * a + b * b
        if s > 9.0:
            t = 3.0 / np.sqrt(s)
            m[i] = t * a * delta[i]
            m[i + 1] = t * b * delta[i]
    return m


def build_curve_lut(points: Sequence[Sequence[float]], size: int = LUT_SIZE) -> np.ndarray:
    """Build a ``size``-entry monotone LUT (float32 in ``[0, 1]``) from points.

    The LUT maps an input level ``i`` (``i / (size - 1)``) to an output value.
    The identity curve ``[[0, 0], [1, 1]]`` yields ``lut[i] == i / (size - 1)``.
    """
    xs, ys = _normalize_points(points)
    m = _fritsch_carlson_slopes(xs, ys)

    sample = np.linspace(0.0, 1.0, size)
    # Locate each sample's segment: index of the right knot.
    idx = np.searchsorted(xs, sample, side="right") - 1
    idx = np.clip(idx, 0, len(xs) - 2)

    x0 = xs[idx]
    x1 = xs[idx + 1]
    y0 = ys[idx]
    y1 = ys[idx + 1]
    m0 = m[idx]
    m1 = m[idx + 1]

    h = x1 - x0
    # Guard against zero-width segments (deduped, but be safe).
    h = np.where(h == 0.0, 1.0, h)
    t = (sample - x0) / h
    t = np.clip(t, 0.0, 1.0)

    t2 = t * t
    t3 = t2 * t
    # Cubic Hermite basis.
    h00 = 2 * t3 - 3 * t2 + 1
    h10 = t3 - 2 * t2 + t
    h01 = -2 * t3 + 3 * t2
    h11 = t3 - t2

    lut = h00 * y0 + h10 * h * m0 + h01 * y1 + h11 * h * m1
    lut = np.clip(lut, 0.0, 1.0)
    return lut.astype(np.float32)


def _apply_lut_channel(channel: np.ndarray, lut: np.ndarray) -> np.ndarray:
    """Apply a LUT to a float channel in ``[0, 1]`` with linear LUT sampling."""
    size = lut.shape[0]
    pos = np.clip(channel, 0.0, 1.0) * (size - 1)
    lo = np.floor(pos).astype(np.int64)
    hi = np.minimum(lo + 1, size - 1)
    frac = pos - lo
    return lut[lo] * (1.0 - frac) + lut[hi] * frac


def apply_curves(
    frame: np.ndarray,
    channel: str = "rgb",
    points: Sequence[Sequence[float]] | None = None,
) -> np.ndarray:
    """Apply a curves transfer to an RGBA frame, preserving alpha.

    ``channel`` is one of ``"rgb"`` (all three RGB channels), ``"r"`` / ``"g"``
    / ``"b"`` (a single channel), or ``"luma"`` (scale RGB by the curve applied
    to per-pixel luma, preserving hue). Identity points are a no-op. The result
    is a fresh ``float32`` array; the input is not mutated, so curves stack.
    """
    frame = np.asarray(frame, dtype=np.float32)
    if frame.ndim != 3 or frame.shape[2] != 4:
        return frame

    lut = build_curve_lut(points if points is not None else [[0.0, 0.0], [1.0, 1.0]])

    rgb = frame[..., :3].copy()
    alpha = frame[..., 3:4]

    ch = (channel or "rgb").lower()
    if ch in ("r", "g", "b"):
        ci = {"r": 0, "g": 1, "b": 2}[ch]
        rgb[..., ci] = _apply_lut_channel(rgb[..., ci], lut)
    elif ch == "luma":
        wr, wg, wb = _LUMA_WEIGHTS
        luma = wr * rgb[..., 0] + wg * rgb[..., 1] + wb * rgb[..., 2]
        graded = _apply_lut_channel(luma, lut)
        eps = 1e-6
        ratio = np.where(luma > eps, graded / np.maximum(luma, eps), 1.0)
        ratio = ratio[..., np.newaxis]
        rgb = np.clip(rgb * ratio, 0.0, 1.0)
    else:  # "rgb" (default) and any unknown channel -> all RGB.
        for ci in range(3):
            rgb[..., ci] = _apply_lut_channel(rgb[..., ci], lut)

    rgb = np.clip(rgb, 0.0, 1.0)
    return np.concatenate([rgb, alpha], axis=2).astype(np.float32)
