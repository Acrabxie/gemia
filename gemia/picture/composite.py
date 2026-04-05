"""Compositing: create_mask, blend, composite, blend modes, chroma/luma key."""
from __future__ import annotations

import cv2
import numpy as np

from gemia.primitives_common import Image, batchable, ensure_float32


@batchable
def create_mask(img: Image, *, method: str = "threshold",
                threshold: float = 0.5,
                channel: int | None = None) -> Image:
    """Generate a single-channel mask from an image.

    Args:
        img: Input image.
        method: ``'threshold'`` (default) — binary threshold on luminance,
            ``'luminance'`` — raw luminance as mask.
        threshold: Cutoff for binary threshold method.
        channel: If set, use a specific BGR channel (0=B, 1=G, 2=R) instead
            of luminance.

    Returns:
        Single-channel float32 mask, [0, 1].
    """
    img = ensure_float32(img)
    if channel is not None:
        if img.ndim != 3 or channel >= img.shape[2]:
            raise ValueError(f"Channel {channel} invalid for image with shape {img.shape}.")
        gray = img[:, :, channel]
    elif img.ndim == 3:
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    else:
        gray = img

    if method == "threshold":
        return (gray >= threshold).astype(np.float32)
    elif method == "luminance":
        return gray.astype(np.float32)
    else:
        raise ValueError(f"Unknown mask method: {method!r}. Use 'threshold' or 'luminance'.")


@batchable
def blend(a: Image, b: Image, *, alpha: float = 0.5) -> Image:
    """Alpha-blend two images of the same shape.

    Args:
        a: First image.
        b: Second image (same shape as *a*).
        alpha: Blend factor. 0.0 = pure *a*, 1.0 = pure *b*.

    Returns:
        Blended image, float32 [0, 1].

    Note:
        When used with ``@batchable``, pass ``a`` as a list; ``b`` must
        also be a list of the same length (handled by the caller).
    """
    a = ensure_float32(a)
    b = ensure_float32(b)
    if a.shape != b.shape:
        raise ValueError(f"Shape mismatch: {a.shape} vs {b.shape}.")
    result = a * (1.0 - alpha) + b * alpha
    return np.clip(result, 0, 1).astype(np.float32)


def composite(fg: Image, bg: Image, *, mask: Image) -> Image:
    """Composite foreground over background using a mask.

    Args:
        fg: Foreground image, float32 BGR.
        bg: Background image, float32 BGR (same spatial size as *fg*).
        mask: Single-channel float32 mask [0, 1] where 1.0 = foreground.

    Returns:
        Composited image, float32 [0, 1].
    """
    fg = ensure_float32(fg)
    bg = ensure_float32(bg)
    mask = ensure_float32(mask)

    if fg.shape[:2] != bg.shape[:2]:
        raise ValueError(f"fg/bg size mismatch: {fg.shape[:2]} vs {bg.shape[:2]}.")
    if mask.shape[:2] != fg.shape[:2]:
        raise ValueError(f"Mask size mismatch: {mask.shape[:2]} vs {fg.shape[:2]}.")

    if mask.ndim == 2:
        mask = mask[:, :, np.newaxis]

    result = fg * mask + bg * (1.0 - mask)
    return np.clip(result, 0, 1).astype(np.float32)


@batchable
def blend_multiply(a: Image, b: Image) -> Image:
    """Multiply blend mode: a * b.

    Args:
        a: First image, float32 BGR [0, 1].
        b: Second image, float32 BGR [0, 1].

    Returns:
        Blended image, float32 [0, 1].
    """
    a = ensure_float32(a)
    b = ensure_float32(b)
    return np.clip(a * b, 0, 1).astype(np.float32)


@batchable
def blend_screen(a: Image, b: Image) -> Image:
    """Screen blend mode: 1 - (1-a)*(1-b).

    Args:
        a: First image, float32 BGR [0, 1].
        b: Second image, float32 BGR [0, 1].

    Returns:
        Blended image, float32 [0, 1].
    """
    a = ensure_float32(a)
    b = ensure_float32(b)
    return np.clip(1.0 - (1.0 - a) * (1.0 - b), 0, 1).astype(np.float32)


@batchable
def blend_overlay(a: Image, b: Image) -> Image:
    """Overlay blend mode: 2*a*b where a<0.5, else 1-2*(1-a)*(1-b).

    Args:
        a: First image, float32 BGR [0, 1].
        b: Second image, float32 BGR [0, 1].

    Returns:
        Blended image, float32 [0, 1].
    """
    a = ensure_float32(a)
    b = ensure_float32(b)
    low = 2.0 * a * b
    high = 1.0 - 2.0 * (1.0 - a) * (1.0 - b)
    result = np.where(a < 0.5, low, high)
    return np.clip(result, 0, 1).astype(np.float32)


@batchable
def blend_soft_light(a: Image, b: Image) -> Image:
    """Soft light blend mode using Photoshop formula.

    Args:
        a: First image, float32 BGR [0, 1].
        b: Second image, float32 BGR [0, 1].

    Returns:
        Blended image, float32 [0, 1].
    """
    a = ensure_float32(a)
    b = ensure_float32(b)

    def D(x: np.ndarray) -> np.ndarray:
        return np.where(x >= 0.25, np.sqrt(np.maximum(x, 0)), ((16.0 * x - 12.0) * x + 4.0) * x)

    low = a - (1.0 - 2.0 * b) * a * (1.0 - a)
    high = a + (2.0 * b - 1.0) * (D(a) - a)
    result = np.where(b <= 0.5, low, high)
    return np.clip(result, 0, 1).astype(np.float32)


@batchable
def blend_hard_light(a: Image, b: Image) -> Image:
    """Hard light blend mode: overlay with a and b swapped.

    Args:
        a: First image, float32 BGR [0, 1].
        b: Second image, float32 BGR [0, 1].

    Returns:
        Blended image, float32 [0, 1].
    """
    a = ensure_float32(a)
    b = ensure_float32(b)
    low = 2.0 * b * a
    high = 1.0 - 2.0 * (1.0 - b) * (1.0 - a)
    result = np.where(b < 0.5, low, high)
    return np.clip(result, 0, 1).astype(np.float32)


@batchable
def blend_color_dodge(a: Image, b: Image) -> Image:
    """Color dodge blend mode: a / (1 - b), clamped.

    Args:
        a: First image, float32 BGR [0, 1].
        b: Second image, float32 BGR [0, 1].

    Returns:
        Blended image, float32 [0, 1].
    """
    a = ensure_float32(a)
    b = ensure_float32(b)
    denom = np.maximum(1.0 - b, 1e-7)
    return np.clip(a / denom, 0, 1).astype(np.float32)


@batchable
def blend_color_burn(a: Image, b: Image) -> Image:
    """Color burn blend mode: 1 - (1-a)/b, clamped.

    Args:
        a: First image, float32 BGR [0, 1].
        b: Second image, float32 BGR [0, 1].

    Returns:
        Blended image, float32 [0, 1].
    """
    a = ensure_float32(a)
    b = ensure_float32(b)
    denom = np.maximum(b, 1e-7)
    return np.clip(1.0 - (1.0 - a) / denom, 0, 1).astype(np.float32)


@batchable
def blend_difference(a: Image, b: Image) -> Image:
    """Difference blend mode: abs(a - b).

    Args:
        a: First image, float32 BGR [0, 1].
        b: Second image, float32 BGR [0, 1].

    Returns:
        Blended image, float32 [0, 1].
    """
    a = ensure_float32(a)
    b = ensure_float32(b)
    return np.clip(np.abs(a - b), 0, 1).astype(np.float32)


@batchable
def blend_exclusion(a: Image, b: Image) -> Image:
    """Exclusion blend mode: a + b - 2*a*b.

    Args:
        a: First image, float32 BGR [0, 1].
        b: Second image, float32 BGR [0, 1].

    Returns:
        Blended image, float32 [0, 1].
    """
    a = ensure_float32(a)
    b = ensure_float32(b)
    return np.clip(a + b - 2.0 * a * b, 0, 1).astype(np.float32)


def _hls_blend(a: Image, b: Image, channels: list[int]) -> Image:
    """Internal HLS-based blend: take listed channel indices from b, rest from a."""
    a_u8 = np.clip(a * 255, 0, 255).astype(np.uint8)
    b_u8 = np.clip(b * 255, 0, 255).astype(np.uint8)
    a_hls = cv2.cvtColor(a_u8, cv2.COLOR_BGR2HLS).astype(np.float32)
    b_hls = cv2.cvtColor(b_u8, cv2.COLOR_BGR2HLS).astype(np.float32)
    result_hls = a_hls.copy()
    for c in channels:
        result_hls[:, :, c] = b_hls[:, :, c]
    result_hls = np.clip(result_hls, 0, 255).astype(np.uint8)
    result_bgr = cv2.cvtColor(result_hls, cv2.COLOR_HLS2BGR)
    return result_bgr.astype(np.float32) / 255.0


@batchable
def blend_hue(a: Image, b: Image) -> Image:
    """Hue blend mode: hue from b, lightness and saturation from a.

    Args:
        a: Base image, float32 BGR [0, 1].
        b: Blend image, float32 BGR [0, 1].

    Returns:
        Blended image, float32 [0, 1].
    """
    a = ensure_float32(a)
    b = ensure_float32(b)
    return np.clip(_hls_blend(a, b, [0]), 0, 1).astype(np.float32)


@batchable
def blend_saturation(a: Image, b: Image) -> Image:
    """Saturation blend mode: saturation from b, hue and lightness from a.

    Args:
        a: Base image, float32 BGR [0, 1].
        b: Blend image, float32 BGR [0, 1].

    Returns:
        Blended image, float32 [0, 1].
    """
    a = ensure_float32(a)
    b = ensure_float32(b)
    return np.clip(_hls_blend(a, b, [2]), 0, 1).astype(np.float32)


@batchable
def blend_color(a: Image, b: Image) -> Image:
    """Color blend mode: hue and saturation from b, lightness from a.

    Args:
        a: Base image, float32 BGR [0, 1].
        b: Blend image, float32 BGR [0, 1].

    Returns:
        Blended image, float32 [0, 1].
    """
    a = ensure_float32(a)
    b = ensure_float32(b)
    return np.clip(_hls_blend(a, b, [0, 2]), 0, 1).astype(np.float32)


@batchable
def blend_luminosity(a: Image, b: Image) -> Image:
    """Luminosity blend mode: lightness from b, hue and saturation from a.

    Args:
        a: Base image, float32 BGR [0, 1].
        b: Blend image, float32 BGR [0, 1].

    Returns:
        Blended image, float32 [0, 1].
    """
    a = ensure_float32(a)
    b = ensure_float32(b)
    return np.clip(_hls_blend(a, b, [1]), 0, 1).astype(np.float32)


def chroma_key(img: Image, *, key_color: tuple[float, float, float],
               tolerance: float = 0.3, spill_suppress: float = 0.5) -> Image:
    """Remove a chroma key color and return BGRA image with alpha.

    Args:
        img: Input BGR float32 image.
        key_color: (b, g, r) float32 [0,1] color to remove (e.g. green screen).
        tolerance: Distance threshold for keying [0, 1].
        spill_suppress: Amount of spill suppression [0, 1].

    Returns:
        BGRA float32 image with alpha channel.
    """
    img = ensure_float32(img)
    key = np.array(key_color, dtype=np.float32)
    dist = np.sqrt(np.sum((img - key) ** 2, axis=2))
    alpha = np.clip((dist - tolerance) / (1.0 - tolerance + 1e-7), 0, 1).astype(np.float32)

    result = img.copy()
    if spill_suppress > 0.0:
        key_ch = int(np.argmax(key))
        other_chs = [c for c in range(3) if c != key_ch]
        spill_mask = (1.0 - alpha) * spill_suppress
        avg_other = (result[:, :, other_chs[0]] + result[:, :, other_chs[1]]) * 0.5
        result[:, :, key_ch] = np.clip(
            result[:, :, key_ch] * (1.0 - spill_mask) + avg_other * spill_mask, 0, 1
        )

    bgra = np.concatenate([result, alpha[:, :, np.newaxis]], axis=2)
    return bgra.astype(np.float32)


def luma_key(img: Image, *, low: float = 0.0, high: float = 0.2, soft: float = 0.05) -> Image:
    """Key out dark or bright regions based on luminance.

    Args:
        img: Input BGR float32 image.
        low: Lower luma bound for keying.
        high: Upper luma bound for keying.
        soft: Softness of edges (feather amount).

    Returns:
        BGRA float32 image with alpha channel.
    """
    img = ensure_float32(img)
    if img.ndim == 3:
        luma = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    else:
        luma = img

    soft = max(soft, 1e-7)
    above_low = np.clip((luma - (low - soft)) / soft, 0, 1)
    below_high = np.clip(((high + soft) - luma) / soft, 0, 1)
    key_weight = above_low * below_high
    alpha = np.clip(1.0 - key_weight, 0, 1).astype(np.float32)

    if img.ndim == 3:
        bgra = np.concatenate([img, alpha[:, :, np.newaxis]], axis=2)
    else:
        bgra = np.stack([img, img, img, alpha], axis=2)
    return bgra.astype(np.float32)


def create_edge_mask(img: Image, *, radius: float = 2.0, feather: float = 1.0) -> Image:
    """Create a mask that highlights edges with feathering.

    Args:
        img: Input image.
        radius: Edge detection radius (Canny aperture influence).
        feather: Gaussian blur applied to soften edges.

    Returns:
        Single-channel float32 mask [0, 1].
    """
    img = ensure_float32(img)
    if img.ndim == 3:
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    else:
        gray = img

    u8 = np.clip(gray * 255, 0, 255).astype(np.uint8)
    low = max(1, int(radius * 20))
    high = min(255, int(radius * 60))
    edges = cv2.Canny(u8, low, high)

    if feather > 0:
        ksize = max(1, int(feather * 4) | 1)
        edges_f = edges.astype(np.float32) / 255.0
        edges_f = cv2.GaussianBlur(edges_f, (ksize, ksize), feather)
    else:
        edges_f = edges.astype(np.float32) / 255.0

    return np.clip(edges_f, 0, 1).astype(np.float32)
