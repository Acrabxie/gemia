"""Image analysis: histogram, dominant_colors, edge_detect."""
from __future__ import annotations

import cv2
import numpy as np

from gemia.primitives_common import Image, batchable, ensure_float32, to_uint8


@batchable
def histogram(img: Image, *, bins: int = 256) -> dict[str, np.ndarray]:
    """Compute per-channel histogram.

    Args:
        img: Input image, float32 [0, 1] BGR or grayscale.
        bins: Number of bins (default 256).

    Returns:
        Dict with keys ``'b'``, ``'g'``, ``'r'`` (or ``'gray'``) mapping to
        1D arrays of shape ``(bins,)``.
    """
    img = ensure_float32(img)
    u8 = to_uint8(img)
    if u8.ndim == 2:
        hist = cv2.calcHist([u8], [0], None, [bins], [0, 256]).ravel()
        return {"gray": hist}
    result = {}
    for i, ch in enumerate("bgr"):
        result[ch] = cv2.calcHist([u8], [i], None, [bins], [0, 256]).ravel()
    return result


@batchable
def dominant_colors(img: Image, *, k: int = 5) -> np.ndarray:
    """Find the *k* dominant colors using k-means clustering.

    Args:
        img: Input BGR image.
        k: Number of clusters (colors).

    Returns:
        Array of shape ``(k, 3)`` with BGR float32 [0, 1] colors, sorted by
        cluster size (most dominant first).
    """
    img = ensure_float32(img)
    pixels = img.reshape(-1, 3) if img.ndim == 3 else img.reshape(-1, 1)
    pixels = np.float32(pixels)
    criteria = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 20, 0.5)
    _, labels, centers = cv2.kmeans(pixels, k, None, criteria, 3, cv2.KMEANS_PP_CENTERS)
    # sort by cluster population
    counts = np.bincount(labels.ravel(), minlength=k)
    order = np.argsort(-counts)
    return centers[order].astype(np.float32)


@batchable
def edge_detect(img: Image, *, method: str = "canny",
                low_threshold: float = 0.1,
                high_threshold: float = 0.3) -> Image:
    """Detect edges.

    Args:
        img: Input image (will be converted to grayscale internally).
        method: ``'canny'`` (default) or ``'sobel'``.
        low_threshold: Canny low threshold (fraction of 255).
        high_threshold: Canny high threshold (fraction of 255).

    Returns:
        Single-channel float32 edge map, [0, 1].
    """
    img = ensure_float32(img)
    if img.ndim == 3:
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    else:
        gray = img
    u8 = to_uint8(gray)

    if method == "canny":
        edges = cv2.Canny(u8, int(low_threshold * 255), int(high_threshold * 255))
    elif method == "sobel":
        sx = cv2.Sobel(u8, cv2.CV_64F, 1, 0, ksize=3)
        sy = cv2.Sobel(u8, cv2.CV_64F, 0, 1, ksize=3)
        edges = np.sqrt(sx ** 2 + sy ** 2)
        edges = np.clip(edges / edges.max(), 0, 1) * 255 if edges.max() > 0 else edges
        edges = edges.astype(np.uint8)
    else:
        raise ValueError(f"Unknown edge method: {method!r}. Use 'canny' or 'sobel'.")

    return edges.astype(np.float32) / 255.0
