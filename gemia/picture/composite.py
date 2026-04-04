"""Compositing: create_mask, blend, composite."""
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
