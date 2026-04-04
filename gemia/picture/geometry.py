"""Geometric transformations: resize, crop, rotate, perspective_transform."""
from __future__ import annotations

import cv2
import numpy as np

from gemia.primitives_common import Image, batchable, ensure_float32, to_uint8


@batchable
def resize(img: Image, *, width: int | None = None, height: int | None = None,
           scale: float | None = None, interpolation: str = "linear") -> Image:
    """Resize an image by target dimensions or scale factor.

    Args:
        img: Input image, float32 [0, 1].
        width: Target width in pixels.  If only width is given, height is
            computed to preserve aspect ratio.
        height: Target height in pixels.  If only height is given, width is
            computed to preserve aspect ratio.
        scale: Scale factor (e.g. 0.5 = half size).  Ignored if width/height
            are provided.
        interpolation: ``'linear'``, ``'nearest'``, ``'cubic'``, ``'area'``,
            ``'lanczos'``.

    Returns:
        Resized image, float32 [0, 1].
    """
    img = ensure_float32(img)
    h, w = img.shape[:2]
    interp = _INTERP_MAP.get(interpolation, cv2.INTER_LINEAR)

    if width is not None or height is not None:
        if width is not None and height is not None:
            new_w, new_h = width, height
        elif width is not None:
            new_w = width
            new_h = int(round(h * width / w))
        else:
            assert height is not None
            new_h = height
            new_w = int(round(w * height / h))
    elif scale is not None:
        new_w = int(round(w * scale))
        new_h = int(round(h * scale))
    else:
        raise ValueError("Provide width, height, or scale.")

    if new_w <= 0 or new_h <= 0:
        raise ValueError(f"Invalid target size: {new_w}x{new_h}")
    return cv2.resize(img, (new_w, new_h), interpolation=interp)


@batchable
def crop(img: Image, *, x: int, y: int, width: int, height: int) -> Image:
    """Crop a rectangular region from the image.

    Args:
        img: Input image.
        x: Left column (0-indexed).
        y: Top row (0-indexed).
        width: Crop width in pixels.
        height: Crop height in pixels.

    Returns:
        Cropped image, float32 [0, 1].
    """
    img = ensure_float32(img)
    h, w = img.shape[:2]
    if x < 0 or y < 0 or x + width > w or y + height > h:
        raise ValueError(
            f"Crop region ({x},{y},{width},{height}) exceeds image bounds ({w},{h})."
        )
    return img[y : y + height, x : x + width].copy()


@batchable
def rotate(img: Image, *, angle: float, center: tuple[float, float] | None = None,
           expand: bool = False) -> Image:
    """Rotate the image by *angle* degrees counter-clockwise.

    Args:
        img: Input image.
        angle: Rotation angle in degrees.
        center: Rotation center as ``(cx, cy)``.  Defaults to image center.
        expand: If True, the output image is enlarged to contain the full
            rotated image (no clipping).

    Returns:
        Rotated image, float32 [0, 1].
    """
    img = ensure_float32(img)
    h, w = img.shape[:2]
    cx, cy = center if center is not None else (w / 2, h / 2)
    M = cv2.getRotationMatrix2D((cx, cy), angle, 1.0)

    if expand:
        cos_a = abs(M[0, 0])
        sin_a = abs(M[0, 1])
        new_w = int(np.ceil(h * sin_a + w * cos_a))
        new_h = int(np.ceil(h * cos_a + w * sin_a))
        M[0, 2] += (new_w - w) / 2
        M[1, 2] += (new_h - h) / 2
    else:
        new_w, new_h = w, h

    return cv2.warpAffine(img, M, (new_w, new_h), borderMode=cv2.BORDER_REFLECT_101)


@batchable
def perspective_transform(img: Image, *, src_points: np.ndarray,
                          dst_points: np.ndarray,
                          output_size: tuple[int, int] | None = None) -> Image:
    """Apply a perspective (homography) warp.

    Args:
        img: Input image.
        src_points: 4×2 float32 array — corners in the source image.
        dst_points: 4×2 float32 array — corresponding corners in the output.
        output_size: ``(width, height)`` of the output.  Defaults to input size.

    Returns:
        Warped image, float32 [0, 1].
    """
    img = ensure_float32(img)
    src = np.asarray(src_points, dtype=np.float32)
    dst = np.asarray(dst_points, dtype=np.float32)
    if src.shape != (4, 2) or dst.shape != (4, 2):
        raise ValueError("src_points and dst_points must be 4×2 arrays.")
    M = cv2.getPerspectiveTransform(src, dst)
    h, w = img.shape[:2]
    out_w, out_h = output_size if output_size else (w, h)
    return cv2.warpPerspective(img, M, (out_w, out_h), borderMode=cv2.BORDER_REFLECT_101)


_INTERP_MAP = {
    "nearest": cv2.INTER_NEAREST,
    "linear": cv2.INTER_LINEAR,
    "cubic": cv2.INTER_CUBIC,
    "area": cv2.INTER_AREA,
    "lanczos": cv2.INTER_LANCZOS4,
}
