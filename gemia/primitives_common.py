"""Shared primitives for gemia.picture, gemia.audio, gemia.video.

Provides type aliases, input normalization, and the @batchable decorator.
"""
from __future__ import annotations

import functools
from typing import Any, Callable, TypeVar

import numpy as np

# ── Type aliases ───────────────────────────────────────────────────────
Image = np.ndarray
"""float32 ndarray, shape (H, W, 3) BGR or (H, W) grayscale, range [0, 1]."""

ImageBatch = list[Image]

AudioData = np.ndarray
"""float32 ndarray, shape (samples,) mono or (channels, samples) stereo, range [-1, 1]."""

F = TypeVar("F", bound=Callable[..., Any])


# ── Decorator ──────────────────────────────────────────────────────────
def batchable(func: F) -> F:
    """Make a single-input function accept a list and map over it.

    If the first positional argument is a ``list``, the function is called
    once per element and a list of results is returned.  Otherwise the
    function is called directly and a single result is returned.

    Example::

        @batchable
        def blur(img: Image, *, radius: float = 1.0) -> Image:
            ...

        blur(single_img)          # -> Image
        blur([img1, img2, img3])  # -> [Image, Image, Image]
    """

    @functools.wraps(func)
    def wrapper(data: Any, *args: Any, **kwargs: Any) -> Any:
        if isinstance(data, list):
            return [func(item, *args, **kwargs) for item in data]
        return func(data, *args, **kwargs)

    return wrapper  # type: ignore[return-value]


# ── Input normalization ────────────────────────────────────────────────
def ensure_float32(img: np.ndarray) -> Image:
    """Convert an image to float32 [0, 1].

    Accepts uint8 [0, 255] or float32 [0, 1].  Raises TypeError otherwise.
    """
    if img.dtype == np.float32:
        return img
    if img.dtype == np.uint8:
        return img.astype(np.float32) / 255.0
    if np.issubdtype(img.dtype, np.floating):
        return img.astype(np.float32)
    raise TypeError(f"Unsupported image dtype: {img.dtype}. Expected uint8 or float32.")


def to_uint8(img: Image) -> np.ndarray:
    """Convert float32 [0, 1] image back to uint8 [0, 255]."""
    return np.clip(img * 255.0, 0, 255).astype(np.uint8)
