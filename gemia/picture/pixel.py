"""Pixel-level operations: blur, sharpen, denoise, add_grain, convolve."""
from __future__ import annotations

import cv2
import numpy as np

from gemia.primitives_common import Image, batchable, ensure_float32


@batchable
def blur(img: Image, *, radius: float = 1.0, method: str = "gaussian") -> Image:
    """Apply blur to the image.

    Args:
        img: Input image, float32 [0, 1].
        radius: Blur radius (kernel half-size). A larger value produces a
            stronger blur.
        method: ``'gaussian'`` (default), ``'box'``, or ``'median'``.

    Returns:
        Blurred image, float32 [0, 1].
    """
    img = ensure_float32(img)
    ksize = max(int(round(radius * 2)) | 1, 1)  # ensure odd and >= 1
    if method == "gaussian":
        return cv2.GaussianBlur(img, (ksize, ksize), sigmaX=radius)
    elif method == "box":
        return cv2.blur(img, (ksize, ksize))
    elif method == "median":
        # medianBlur requires uint8 for multi-channel
        from gemia.primitives_common import to_uint8
        u8 = to_uint8(img)
        result = cv2.medianBlur(u8, ksize)
        return result.astype(np.float32) / 255.0
    else:
        raise ValueError(f"Unknown blur method: {method!r}. Use 'gaussian', 'box', or 'median'.")


@batchable
def sharpen(img: Image, *, amount: float = 1.0, radius: float = 1.0) -> Image:
    """Unsharp-mask sharpening.

    Args:
        img: Input image.
        amount: Sharpening strength. 1.0 is moderate.
        radius: Blur radius for the unsharp mask.

    Returns:
        Sharpened image, float32 [0, 1].
    """
    img = ensure_float32(img)
    ksize = max(int(round(radius * 2)) | 1, 3)
    blurred = cv2.GaussianBlur(img, (ksize, ksize), sigmaX=radius)
    sharpened = img + amount * (img - blurred)
    return np.clip(sharpened, 0, 1).astype(np.float32)


@batchable
def denoise(img: Image, *, strength: float = 10.0) -> Image:
    """Non-local means denoising.

    Args:
        img: Input image.
        strength: Filter strength. Higher values remove more noise but may
            lose detail.

    Returns:
        Denoised image, float32 [0, 1].
    """
    img = ensure_float32(img)
    from gemia.primitives_common import to_uint8
    u8 = to_uint8(img)
    if u8.ndim == 3:
        result = cv2.fastNlMeansDenoisingColored(u8, None, strength, strength)
    else:
        result = cv2.fastNlMeansDenoising(u8, None, strength)
    return result.astype(np.float32) / 255.0


@batchable
def add_grain(img: Image, *, intensity: float = 0.05, seed: int | None = None) -> Image:
    """Add film-like grain noise.

    Args:
        img: Input image.
        intensity: Noise amplitude in [0, 1] range. 0.05 is subtle.
        seed: Random seed for reproducibility.

    Returns:
        Grainy image, float32 [0, 1].
    """
    img = ensure_float32(img)
    rng = np.random.default_rng(seed)
    noise = rng.normal(0, intensity, img.shape).astype(np.float32)
    return np.clip(img + noise, 0, 1).astype(np.float32)


@batchable
def convolve(img: Image, *, kernel: np.ndarray) -> Image:
    """Apply a custom convolution kernel.

    Args:
        img: Input image.
        kernel: 2D float32 array (e.g. 3×3 or 5×5).

    Returns:
        Filtered image, float32 [0, 1].
    """
    img = ensure_float32(img)
    kernel = np.asarray(kernel, dtype=np.float32)
    if kernel.ndim != 2:
        raise ValueError(f"Kernel must be 2D, got {kernel.ndim}D.")
    result = cv2.filter2D(img, -1, kernel)
    return np.clip(result, 0, 1).astype(np.float32)
