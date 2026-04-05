"""Image analysis: histogram, dominant_colors, edge_detect."""
from __future__ import annotations

import cv2
import numpy as np

from gemia.primitives_common import Image, batchable, ensure_float32, to_uint8  # noqa: F401


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


def waveform_monitor(img: Image, *, width: int = 256) -> np.ndarray:
    """Generate waveform monitor data (luma values per column).

    Args:
        img: Input BGR float32 image.
        width: Number of horizontal bins.

    Returns:
        2D float32 array shape (height_bins, width) with luma density [0,1].
    """
    img = ensure_float32(img)
    if img.ndim == 3:
        luma = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    else:
        luma = img

    h, w = luma.shape
    height_bins = 256
    waveform = np.zeros((height_bins, width), dtype=np.float32)

    x_indices = np.linspace(0, w - 1, width, dtype=int)
    for col_out, col_in in enumerate(x_indices):
        col_vals = luma[:, col_in]
        bin_indices = np.clip((col_vals * (height_bins - 1)).astype(int), 0, height_bins - 1)
        np.add.at(waveform[:, col_out], bin_indices, 1)

    col_max = waveform.max(axis=0, keepdims=True)
    col_max = np.where(col_max == 0, 1, col_max)
    waveform = waveform / col_max
    return waveform.astype(np.float32)


def vectorscope(img: Image, *, size: int = 256) -> np.ndarray:
    """Generate vectorscope data (Cb/Cr chrominance distribution).

    Args:
        img: Input BGR float32 image.
        size: Output grid size.

    Returns:
        2D float32 array (size, size) representing chroma distribution.
    """
    img = ensure_float32(img)
    u8 = np.clip(img * 255, 0, 255).astype(np.uint8)
    if u8.ndim == 3:
        ycrcb = cv2.cvtColor(u8, cv2.COLOR_BGR2YCrCb)
        cr = ycrcb[:, :, 1].ravel().astype(np.float32) / 255.0
        cb = ycrcb[:, :, 2].ravel().astype(np.float32) / 255.0
    else:
        cr = np.full(u8.size, 0.5, dtype=np.float32)
        cb = np.full(u8.size, 0.5, dtype=np.float32)

    scope = np.zeros((size, size), dtype=np.float32)
    xi = np.clip((cr * (size - 1)).astype(int), 0, size - 1)
    yi = np.clip((cb * (size - 1)).astype(int), 0, size - 1)
    np.add.at(scope, (yi, xi), 1)

    mx = scope.max()
    if mx > 0:
        scope /= mx
    return scope.astype(np.float32)


def histogram_rgb(img: Image, *, bins: int = 256) -> dict[str, np.ndarray]:
    """Compute separate R, G, B channel histograms.

    Args:
        img: Input BGR float32 image.
        bins: Number of histogram bins.

    Returns:
        Dict with keys 'r', 'g', 'b' mapping to 1D arrays of shape (bins,).
    """
    img = ensure_float32(img)
    u8 = to_uint8(img)
    result: dict[str, np.ndarray] = {}
    for i, ch in enumerate(["b", "g", "r"]):
        result[ch] = cv2.calcHist([u8], [i], None, [bins], [0, 256]).ravel().astype(np.float32)
    return result


def check_clipping(img: Image, *, ceiling: float = 0.95) -> dict:
    """Detect highlight and shadow clipping.

    Args:
        img: Input float32 image.
        ceiling: Threshold above which pixels are clipped highlights.

    Returns:
        Dict with 'highlight_pct' (% pixels above ceiling),
        'shadow_pct' (% pixels below 0.05), 'is_clipped' (bool).
    """
    img = ensure_float32(img)
    total = img.size
    highlight_pct = float(np.sum(img > ceiling) / total * 100.0)
    shadow_pct = float(np.sum(img < 0.05) / total * 100.0)
    return {
        "highlight_pct": highlight_pct,
        "shadow_pct": shadow_pct,
        "is_clipped": highlight_pct > 0 or shadow_pct > 0,
    }
