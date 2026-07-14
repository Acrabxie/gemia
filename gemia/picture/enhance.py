"""gemia.picture.enhance — AI upscaling and color matching utilities."""

from __future__ import annotations

import os
import subprocess
import tempfile
from typing import Callable

_VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv"}


def _is_video(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in _VIDEO_EXTS


# ---------------------------------------------------------------------------
# super_scale
# ---------------------------------------------------------------------------

def super_scale(input_path: str, output_path: str, *, factor: int = 2) -> str:
    """Upscale *input_path* by *factor* and write result to *output_path*.

    Uses Real-ESRGAN when available; falls back to ffmpeg lanczos filter.
    Works for both images (.jpg/.png) and videos (.mp4/.mov).

    Returns *output_path*.
    """
    if _is_video(input_path):
        _super_scale_video_ffmpeg(input_path, output_path, factor)
        return output_path

    # Try Real-ESRGAN for images
    try:
        _super_scale_image_realesrgan(input_path, output_path, factor)
    except ImportError:
        _super_scale_image_ffmpeg(input_path, output_path, factor)

    return output_path


def _super_scale_image_realesrgan(input_path: str, output_path: str, factor: int) -> None:
    import numpy as np
    from PIL import Image  # noqa: PLC0415
    from realesrgan import RealESRGANer  # noqa: PLC0415
    from basicsr.archs.rrdbnet_arch import RRDBNet  # noqa: PLC0415

    # Locate model weights — search common locations
    model_candidates = [
        os.path.join(os.path.dirname(__file__), "weights", "RealESRGAN_x4plus.pth"),
        os.path.expanduser("~/.cache/realesrgan/RealESRGAN_x4plus.pth"),
        "RealESRGAN_x4plus.pth",
    ]
    model_path = next((p for p in model_candidates if os.path.isfile(p)), model_candidates[0])

    model = RRDBNet(
        num_in_ch=3, num_out_ch=3, num_feat=64,
        num_block=23, num_grow_ch=32, scale=4,
    )
    upsampler = RealESRGANer(
        scale=4,
        model_path=model_path,
        model=model,
        tile=0,
        tile_pad=10,
        pre_pad=0,
        half=False,
    )

    img = np.array(Image.open(input_path).convert("RGB"))
    # Real-ESRGAN always outputs 4× — resize again if a different factor was requested
    output, _ = upsampler.enhance(img, outscale=factor)
    Image.fromarray(output).save(output_path)


def _super_scale_image_ffmpeg(input_path: str, output_path: str, factor: int) -> None:
    cmd = [
        "ffmpeg", "-y", "-i", input_path,
        "-vf", f"scale=iw*{factor}:ih*{factor}:flags=lanczos",
        output_path,
    ]
    subprocess.run(cmd, check=True, capture_output=True)


def _super_scale_video_ffmpeg(input_path: str, output_path: str, factor: int) -> None:
    cmd = [
        "ffmpeg", "-y", "-i", input_path,
        "-vf", f"scale=iw*{factor}:ih*{factor}:flags=lanczos",
        "-c:a", "copy",
        output_path,
    ]
    subprocess.run(cmd, check=True, capture_output=True)


# ---------------------------------------------------------------------------
# match_color
# ---------------------------------------------------------------------------

def match_color(source_path: str, reference_path: str, output_path: str) -> str:
    """Color-grade *source_path* to match the palette of *reference_path*.

    Uses per-channel histogram matching (CDF-based lookup table).
    For video sources the operation is applied frame-by-frame.

    Returns *output_path*.
    """
    if _is_video(source_path):
        _match_color_video(source_path, reference_path, output_path)
        return output_path

    _match_color_image(source_path, reference_path, output_path)
    return output_path


def _build_channel_lut(src_channel, ref_channel):
    """Return a 256-element uint8 LUT mapping source→reference distribution."""
    import numpy as np  # noqa: PLC0415

    def cdf(channel):
        hist, _ = np.histogram(channel.flatten(), bins=256, range=(0, 256))
        c = hist.cumsum().astype(np.float64)
        c /= c[-1]  # normalize to [0, 1]
        return c

    src_cdf = cdf(src_channel)
    ref_cdf = cdf(ref_channel)

    lut = np.zeros(256, dtype=np.uint8)
    ref_idx = 0
    for src_idx in range(256):
        while ref_idx < 255 and ref_cdf[ref_idx] < src_cdf[src_idx]:
            ref_idx += 1
        lut[src_idx] = ref_idx
    return lut


def _match_color_image(source_path: str, reference_path: str, output_path: str) -> None:
    import numpy as np  # noqa: PLC0415
    from PIL import Image  # noqa: PLC0415

    src = np.array(Image.open(source_path).convert("RGB"))
    ref = np.array(Image.open(reference_path).convert("RGB"))

    result = np.empty_like(src)
    for ch in range(3):
        lut = _build_channel_lut(src[:, :, ch], ref[:, :, ch])
        result[:, :, ch] = lut[src[:, :, ch]]

    Image.fromarray(result).save(output_path)


def _match_color_video(source_path: str, reference_path: str, output_path: str) -> None:
    from gemia.video.frames import apply_picture_op_to_video  # noqa: PLC0415
    import numpy as np  # noqa: PLC0415
    from PIL import Image  # noqa: PLC0415

    # Pre-load reference image once; build LUTs outside the per-frame closure
    ref = np.array(Image.open(reference_path).convert("RGB"))

    def _frame_op(frame_bgr):
        """frame_bgr is a float32 [0,1] BGR ndarray as per gemia convention."""
        import cv2  # noqa: PLC0415

        src_uint8 = (frame_bgr[:, :, ::-1] * 255).clip(0, 255).astype(np.uint8)  # BGR→RGB
        result = np.empty_like(src_uint8)
        for ch in range(3):
            lut = _build_channel_lut(src_uint8[:, :, ch], ref[:, :, ch])
            result[:, :, ch] = lut[src_uint8[:, :, ch]]
        # Convert back to float32 BGR
        return (result[:, :, ::-1] / 255.0).astype(np.float32)

    apply_picture_op_to_video(source_path, output_path, op=_frame_op)


# ---------------------------------------------------------------------------
# skin_tone_protect
# ---------------------------------------------------------------------------

def skin_tone_protect(
    input_path: str,
    output_path: str,
    *,
    params: dict | None = None,
) -> str:
    """Protect skin tones during color grading by masking non-skin areas.

    params keys:
      hue_range        – (low, high) HSV hue degrees for skin detection (default (0, 35))
      saturation_boost – float, boost applied only to non-skin pixels (default 0.0)
      lightness_adjust – float -1.0..1.0, lightness delta for non-skin pixels (default 0.0)

    Returns output_path.
    """
    p = params or {}
    hue_range: tuple[float, float] = p.get("hue_range", (0, 35))
    saturation_boost: float = p.get("saturation_boost", 0.0)
    lightness_adjust: float = p.get("lightness_adjust", 0.0)

    if _is_video(input_path):
        from gemia.video.frames import apply_picture_op_to_video  # noqa: PLC0415

        def _frame_op(frame_bgr):
            return _skin_tone_protect_array(
                frame_bgr, hue_range, saturation_boost, lightness_adjust
            )

        apply_picture_op_to_video(input_path, output_path, op=_frame_op)
        return output_path

    import numpy as np  # noqa: PLC0415
    from PIL import Image  # noqa: PLC0415

    img = np.array(Image.open(input_path).convert("RGB")).astype(np.float32) / 255.0
    # Convert to float32 BGR for _skin_tone_protect_array
    img_bgr = img[:, :, ::-1]
    result_bgr = _skin_tone_protect_array(img_bgr, hue_range, saturation_boost, lightness_adjust)
    result_rgb = (result_bgr[:, :, ::-1] * 255).clip(0, 255).astype(np.uint8)
    Image.fromarray(result_rgb).save(output_path)
    return output_path


def _skin_tone_protect_array(frame_bgr, hue_range, saturation_boost, lightness_adjust):
    """Apply skin-tone-aware adjustments to a float32 BGR [0,1] array."""
    import numpy as np  # noqa: PLC0415
    import colorsys

    h_lo, h_hi = hue_range[0] / 360.0, hue_range[1] / 360.0

    # Work in float32
    arr = frame_bgr.astype(np.float32)
    h, w = arr.shape[:2]

    # Convert RGB->HSV per-pixel via vectorised approach
    r = arr[:, :, 2]
    g = arr[:, :, 1]
    b = arr[:, :, 0]

    cmax = np.maximum(np.maximum(r, g), b)
    cmin = np.minimum(np.minimum(r, g), b)
    delta = cmax - cmin

    # Hue
    hue = np.zeros((h, w), dtype=np.float32)
    mask_r = (cmax == r) & (delta > 0)
    mask_g = (cmax == g) & (delta > 0)
    mask_b = (cmax == b) & (delta > 0)
    hue[mask_r] = ((g[mask_r] - b[mask_r]) / delta[mask_r]) % 6
    hue[mask_g] = (b[mask_g] - r[mask_g]) / delta[mask_g] + 2
    hue[mask_b] = (r[mask_b] - g[mask_b]) / delta[mask_b] + 4
    hue /= 6.0  # normalise to [0, 1]

    # Saturation
    sat = np.where(cmax > 0, delta / cmax, 0.0).astype(np.float32)
    val = cmax

    # Skin mask: hue in range AND saturation > 0.1 AND value > 0.1
    skin_mask = (hue >= h_lo) & (hue <= h_hi) & (sat > 0.1) & (val > 0.1)
    non_skin = ~skin_mask

    result = arr.copy()

    # Apply saturation boost to non-skin pixels
    if saturation_boost != 0.0:
        boost = np.clip(sat + saturation_boost, 0.0, 1.0) - sat  # actual delta
        # Resaturate: move each channel toward/away from value
        for ch_idx in range(3):
            ch = result[:, :, ch_idx]
            delta_ch = val - ch
            result[:, :, ch_idx] = np.where(
                non_skin,
                np.clip(ch - boost * delta_ch, 0.0, 1.0),
                ch,
            )

    # Apply lightness adjust to non-skin pixels
    if lightness_adjust != 0.0:
        adj = lightness_adjust
        for ch_idx in range(3):
            ch = result[:, :, ch_idx]
            result[:, :, ch_idx] = np.where(
                non_skin,
                np.clip(ch + adj, 0.0, 1.0),
                ch,
            )

    # Blend back: skin pixels restored from original
    for ch_idx in range(3):
        result[:, :, ch_idx] = np.where(skin_mask, arr[:, :, ch_idx], result[:, :, ch_idx])

    return result


# ---------------------------------------------------------------------------
# hdr_grade
# ---------------------------------------------------------------------------

def hdr_grade(
    input_path: str,
    output_path: str,
    *,
    target_format: str = "hlg",
) -> str:
    """Grade video to HDR format using ffmpeg zscale filter.

    target_format: "hlg" (Hybrid Log-Gamma) or "pq" (HDR10 / SMPTE ST 2084).

    Returns output_path.
    """
    if target_format == "pq":
        vf = (
            "zscale=transfer=smpte2084:matrix=bt2020nc:primaries=bt2020:range=limited,"
            "format=yuv420p10le"
        )
        color_args = [
            "-color_trc", "smpte2084",
            "-colorspace", "bt2020nc",
            "-color_primaries", "bt2020",
        ]
    else:  # default hlg
        vf = "zscale=transfer=hlg:matrix=bt2020nc:primaries=bt2020,format=yuv420p10le"
        color_args = [
            "-color_trc", "hlg",
            "-colorspace", "bt2020nc",
            "-color_primaries", "bt2020",
        ]

    try:
        cmd = [
            "ffmpeg", "-y", "-i", input_path,
            "-vf", vf,
            *color_args,
            "-c:a", "copy",
            output_path,
        ]
        subprocess.run(cmd, check=True, capture_output=True)
    except subprocess.CalledProcessError:
        # zscale unavailable — approximate HDR look with curves + saturation boost
        from PIL import Image
        import numpy as np
        img = np.array(Image.open(input_path).convert("RGB")).astype(np.float32) / 255.0
        # OOTF-style gamma expand and contrast boost
        img = np.clip(img ** 0.9 * 1.1, 0, 1)
        # Slight warm shift for HLG look
        img[:, :, 0] = np.clip(img[:, :, 0] * 1.04, 0, 1)
        img[:, :, 2] = np.clip(img[:, :, 2] * 0.97, 0, 1)
        Image.fromarray((img * 255).astype(np.uint8)).save(output_path)
    return output_path


# ---------------------------------------------------------------------------
# film_grain_organic
# ---------------------------------------------------------------------------

def film_grain_organic(
    input_path: str,
    output_path: str,
    *,
    params: dict | None = None,
) -> str:
    """Add organic film grain texture using ffmpeg noise filter.

    params keys:
      strength – float 0.0-1.0, grain intensity (default 0.3)
      size     – int 1-5, grain size (default 2, reserved for future use)
      colored  – bool, use colored grain instead of luma-only (default False)

    Returns output_path.
    """
    p = params or {}
    strength: float = float(p.get("strength", 0.3))
    size: int = int(p.get("size", 2))
    colored: bool = bool(p.get("colored", False))

    s = int(strength * 100)

    if colored:
        noise_filter = (
            f"noise=c0s={s}:c0f=t:c1s={s // 2}:c1f=t:c2s={s // 2}:c2f=t"
        )
    else:
        noise_filter = f"noise=alls={s}:allf=t"

    vf = f"unsharp=3:3:0.5,{noise_filter}"

    cmd = [
        "ffmpeg", "-y", "-i", input_path,
        "-vf", vf,
        "-c:a", "copy",
        output_path,
    ]
    subprocess.run(cmd, check=True, capture_output=True)
    return output_path


# ---------------------------------------------------------------------------
# defocus_background
# ---------------------------------------------------------------------------
def defocus_background(
    input_path: str,
    output_path: str,
    *,
    depth_map: str | None = None,
    blur_strength: int = 15,
) -> str:
    """Apply depth-of-field blur, keeping foreground sharp.

    Args:
        input_path: Source image or video.
        output_path: Destination.
        depth_map: Optional path to grayscale depth map (bright=near, dark=far).
                   If None a radial gradient (sharp centre) is used.
        blur_strength: Blur radius in pixels for background regions.

    Returns:
        output_path
    """
    if _is_video(input_path):
        bs = blur_strength | 1  # must be odd for boxblur
        subprocess.run([
            "ffmpeg", "-y", "-i", input_path,
            "-vf", f"boxblur={bs}:{bs}",
            "-c:a", "copy",
            output_path,
        ], check=True, capture_output=True)
        return output_path

    from PIL import Image, ImageFilter
    import numpy as np

    src = Image.open(input_path).convert("RGB")
    w, h = src.size
    blurred = src.filter(ImageFilter.GaussianBlur(radius=blur_strength))

    if depth_map:
        mask = Image.open(depth_map).convert("L").resize((w, h))
    else:
        # Radial gradient: white at centre → black at edges
        cx, cy = w / 2, h / 2
        ys, xs = np.ogrid[:h, :w]
        dist = np.sqrt(((xs - cx) / cx) ** 2 + ((ys - cy) / cy) ** 2)
        dist = np.clip(1.0 - dist, 0.0, 1.0)
        mask = Image.fromarray((dist * 255).astype(np.uint8))

    result = Image.composite(src, blurred, mask)
    result.save(output_path)
    return output_path


# ---------------------------------------------------------------------------
# relight
# ---------------------------------------------------------------------------
def relight(
    input_path: str,
    output_path: str,
    *,
    light_direction: str = "top",
) -> str:
    """Simulate directional relighting via gradient multiply.

    Args:
        input_path: Source image or video.
        output_path: Destination.
        light_direction: One of ``"top"``, ``"bottom"``, ``"left"``, ``"right"``,
                         ``"top-left"``, ``"top-right"``.

    Returns:
        output_path
    """
    def _apply(img):
        import numpy as np
        from PIL import Image as _Image
        arr = np.array(img).astype(np.float32) / 255.0
        h, w = arr.shape[:2]
        xs = np.linspace(0.4, 1.2, w)
        ys = np.linspace(0.4, 1.2, h)
        x_grad = xs[np.newaxis, :]
        y_grad = ys[:, np.newaxis]

        d = light_direction.lower()
        if d == "top":
            grad = np.linspace(1.2, 0.4, h)[:, np.newaxis] * np.ones((1, w))
        elif d == "bottom":
            grad = np.linspace(0.4, 1.2, h)[:, np.newaxis] * np.ones((1, w))
        elif d == "left":
            grad = np.ones((h, 1)) * np.linspace(1.2, 0.4, w)[np.newaxis, :]
        elif d == "right":
            grad = np.ones((h, 1)) * np.linspace(0.4, 1.2, w)[np.newaxis, :]
        elif d == "top-left":
            grad = (np.linspace(1.2, 0.4, h)[:, np.newaxis] + np.linspace(1.2, 0.4, w)[np.newaxis, :]) / 2
        elif d == "top-right":
            grad = (np.linspace(1.2, 0.4, h)[:, np.newaxis] + np.linspace(0.4, 1.2, w)[np.newaxis, :]) / 2
        else:
            raise ValueError(f"Unknown light_direction '{light_direction}'. "
                             f"Choose from: top, bottom, left, right, top-left, top-right")

        grad = grad[:, :, np.newaxis]
        result = np.clip(arr * grad, 0.0, 1.0)
        return _Image.fromarray((result * 255).astype(np.uint8))

    if _is_video(input_path):
        from gemia.video.frames import apply_picture_op_to_video
        apply_picture_op_to_video(input_path, output_path, op=_apply)
        return output_path

    from PIL import Image
    img = Image.open(input_path).convert("RGB")
    _apply(img).save(output_path)
    return output_path


# ---------------------------------------------------------------------------
# motion_blur
# ---------------------------------------------------------------------------
def motion_blur(
    input_path: str,
    output_path: str,
    *,
    vector_field: dict | None = None,
) -> str:
    """Apply directional motion blur.

    Args:
        input_path: Source image or video.
        output_path: Destination.
        vector_field: Dict with ``angle`` (degrees, 0=horizontal) and
                      ``strength`` (kernel size in pixels, default 20).

    Returns:
        output_path
    """
    vf = vector_field or {}
    angle_deg = float(vf.get("angle", 0))
    strength = max(1, int(vf.get("strength", 20)))

    def _apply(img):
        import numpy as np
        from PIL import Image as _Image
        import math

        arr = np.array(img).astype(np.float32)
        k = strength if strength % 2 == 1 else strength + 1
        kernel = np.zeros((k, k), dtype=np.float32)
        cx = cy = k // 2
        angle_rad = math.radians(angle_deg)
        dx = math.cos(angle_rad)
        dy = math.sin(angle_rad)
        for i in range(-(k // 2), k // 2 + 1):
            px = int(round(cx + i * dx))
            py = int(round(cy + i * dy))
            if 0 <= px < k and 0 <= py < k:
                kernel[py, px] = 1.0
        s = kernel.sum()
        if s > 0:
            kernel /= s

        try:
            from scipy.ndimage import convolve
            result = np.stack([convolve(arr[:, :, c], kernel) for c in range(arr.shape[2])], axis=2)
            result = np.clip(result, 0, 255).astype(np.uint8)
            return _Image.fromarray(result)
        except ImportError:
            from PIL import ImageFilter
            return img.filter(ImageFilter.BLUR)

    if _is_video(input_path):
        from gemia.video.frames import apply_picture_op_to_video
        apply_picture_op_to_video(input_path, output_path, op=_apply)
        return output_path

    from PIL import Image
    img = Image.open(input_path).convert("RGB")
    _apply(img).save(output_path)
    return output_path


# ---------------------------------------------------------------------------
# color_balance
# ---------------------------------------------------------------------------

def color_balance(
    input_path: str,
    output_path: str,
    *,
    shadows: tuple[float, float, float] = (0.0, 0.0, 0.0),
    midtones: tuple[float, float, float] = (0.0, 0.0, 0.0),
    highlights: tuple[float, float, float] = (0.0, 0.0, 0.0),
) -> str:
    """Adjust color balance in shadow/midtone/highlight tonal ranges.

    Each range receives an RGB shift tuple where values are in [-1.0, 1.0].
    Positive values shift toward that channel; negative away.

    Args:
        input_path: Source image or video file.
        output_path: Destination file.
        shadows: (r, g, b) shift for dark pixels.
        midtones: (r, g, b) shift for mid-range pixels.
        highlights: (r, g, b) shift for bright pixels.

    Returns:
        The *output_path*.
    """
    import numpy as np
    from PIL import Image

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

    img = np.array(Image.open(input_path).convert("RGB")).astype(np.float32) / 255.0

    # Build per-pixel luminance mask
    lum = img.mean(axis=2, keepdims=True)  # (H, W, 1)

    # Shadow weight: peaks at lum=0, zero at lum=0.5+
    w_shadow = np.clip(1.0 - lum * 2.0, 0, 1)
    # Highlight weight: peaks at lum=1, zero at lum=0.5-
    w_highlight = np.clip((lum - 0.5) * 2.0, 0, 1)
    # Midtone weight: peaks at lum=0.5, zero at extremes
    w_mid = np.clip(1.0 - np.abs(lum - 0.5) * 4.0, 0, 1)

    sr, sg, sb = [float(x) * 0.3 for x in shadows]
    mr, mg, mb = [float(x) * 0.3 for x in midtones]
    hr, hg, hb = [float(x) * 0.3 for x in highlights]

    img[:, :, 0] += w_shadow[:, :, 0] * sr + w_mid[:, :, 0] * mr + w_highlight[:, :, 0] * hr
    img[:, :, 1] += w_shadow[:, :, 0] * sg + w_mid[:, :, 0] * mg + w_highlight[:, :, 0] * hg
    img[:, :, 2] += w_shadow[:, :, 0] * sb + w_mid[:, :, 0] * mb + w_highlight[:, :, 0] * hb

    img = np.clip(img, 0, 1)
    Image.fromarray((img * 255).astype(np.uint8)).save(output_path)
    return output_path


# ---------------------------------------------------------------------------
# color_lookup
# ---------------------------------------------------------------------------

def color_lookup(
    input_path: str,
    output_path: str,
    *,
    lut_file: str,
    strength: float = 1.0,
) -> str:
    """Apply a .cube LUT file to an image using trilinear interpolation.

    Args:
        input_path: Source image file.
        output_path: Destination image file.
        lut_file: Path to a ``.cube`` LUT file (3D LUT, 17³ or 33³ grid).
        strength: Blend strength between original (0.0) and LUT-graded (1.0).

    Returns:
        The *output_path*.
    """
    import numpy as np
    from PIL import Image

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

    # Parse .cube file
    size = 0
    table: list[list[float]] = []
    with open(lut_file, "r") as f:
        for raw_line in f:
            line = raw_line.strip()
            if line.startswith("LUT_3D_SIZE"):
                size = int(line.split()[-1])
            elif line and not line.startswith("#") and not line.startswith("TITLE") \
                    and not line.startswith("DOMAIN") and not line.startswith("LUT"):
                parts = line.split()
                if len(parts) == 3:
                    try:
                        table.append([float(x) for x in parts])
                    except ValueError:
                        pass

    if size == 0 or not table:
        raise ValueError(f"Could not parse LUT from {lut_file!r}")

    lut_data = np.array(table, dtype=np.float32).reshape(size, size, size, 3)

    img = np.array(Image.open(input_path).convert("RGB")).astype(np.float32) / 255.0

    # Trilinear lookup
    def _trilinear(img_arr: np.ndarray, lut: np.ndarray, n: int) -> np.ndarray:
        idx = img_arr * (n - 1)
        i0 = np.floor(idx).astype(int).clip(0, n - 2)
        i1 = i0 + 1
        f = idx - i0  # fractional part

        r0, g0, b0 = i0[..., 0], i0[..., 1], i0[..., 2]
        r1, g1, b1 = i1[..., 0], i1[..., 1], i1[..., 2]
        fr, fg, fb = f[..., 0:1], f[..., 1:2], f[..., 2:3]

        # Trilinear interpolation over 8 corners
        c000 = lut[r0, g0, b0]
        c100 = lut[r1, g0, b0]
        c010 = lut[r0, g1, b0]
        c110 = lut[r1, g1, b0]
        c001 = lut[r0, g0, b1]
        c101 = lut[r1, g0, b1]
        c011 = lut[r0, g1, b1]
        c111 = lut[r1, g1, b1]

        return (
            c000 * (1-fr)*(1-fg)*(1-fb) +
            c100 * fr*(1-fg)*(1-fb) +
            c010 * (1-fr)*fg*(1-fb) +
            c110 * fr*fg*(1-fb) +
            c001 * (1-fr)*(1-fg)*fb +
            c101 * fr*(1-fg)*fb +
            c011 * (1-fr)*fg*fb +
            c111 * fr*fg*fb
        )

    graded = _trilinear(img, lut_data, size)
    result = img * (1.0 - strength) + graded * strength
    result = np.clip(result, 0, 1)
    Image.fromarray((result * 255).astype(np.uint8)).save(output_path)
    return output_path


# ---------------------------------------------------------------------------
# batch_image_resize
# ---------------------------------------------------------------------------

def batch_image_resize(
    input_dir: str,
    output_dir: str,
    *,
    width: int,
    height: int,
    fit: str = "contain",
    bg_color: tuple[int, int, int] = (0, 0, 0),
    exts: tuple[str, ...] = (".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tiff"),
) -> list[str]:
    """Resize all images in a directory to the target dimensions.

    Args:
        input_dir: Source directory containing image files.
        output_dir: Destination directory for resized images.
        width: Target width in pixels.
        height: Target height in pixels.
        fit: ``"contain"`` (letterbox/pillarbox, preserves aspect),
            ``"cover"`` (crop to fill), or ``"stretch"`` (ignore aspect).
        bg_color: Background fill colour for ``"contain"`` mode (RGB).
        exts: File extensions to process (case-insensitive).

    Returns:
        List of output file paths.
    """
    import glob, numpy as np
    from pathlib import Path as _Path
    from PIL import Image

    _Path(output_dir).mkdir(parents=True, exist_ok=True)

    files = [
        f for f in sorted(_Path(input_dir).iterdir())
        if f.suffix.lower() in exts
    ]
    if not files:
        return []

    outputs: list[str] = []
    for src in files:
        img = Image.open(src).convert("RGB")
        iw, ih = img.size

        if fit == "stretch":
            resized = img.resize((width, height), Image.LANCZOS)
        elif fit == "cover":
            scale = max(width / iw, height / ih)
            nw, nh = int(iw * scale), int(ih * scale)
            resized = img.resize((nw, nh), Image.LANCZOS)
            left = (nw - width) // 2
            top = (nh - height) // 2
            resized = resized.crop((left, top, left + width, top + height))
        else:  # contain
            scale = min(width / iw, height / ih)
            nw, nh = int(iw * scale), int(ih * scale)
            resized_inner = img.resize((nw, nh), Image.LANCZOS)
            canvas = Image.new("RGB", (width, height), bg_color)
            x_off = (width - nw) // 2
            y_off = (height - nh) // 2
            canvas.paste(resized_inner, (x_off, y_off))
            resized = canvas

        out_path = str(_Path(output_dir) / src.name)
        resized.save(out_path)
        outputs.append(out_path)

    return outputs


# ---------------------------------------------------------------------------
# image_collage
# ---------------------------------------------------------------------------

def image_collage(
    image_paths: list[str],
    output_path: str,
    *,
    cols: int = 2,
    cell_width: int = 400,
    cell_height: int = 300,
    padding: int = 10,
    bg_color: tuple[int, int, int] = (255, 255, 255),
) -> str:
    """Assemble multiple images into a grid collage.

    Args:
        image_paths: List of source image file paths.
        output_path: Destination image file.
        cols: Number of columns in the grid.
        cell_width: Width of each cell in pixels.
        cell_height: Height of each cell in pixels.
        padding: Gap between cells and border in pixels.
        bg_color: Background fill colour (RGB).

    Returns:
        The *output_path*.
    """
    import math
    import numpy as np
    from PIL import Image

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

    n = len(image_paths)
    if n == 0:
        raise ValueError("image_collage: no images provided")

    rows = math.ceil(n / cols)
    total_w = cols * cell_width + (cols + 1) * padding
    total_h = rows * cell_height + (rows + 1) * padding

    canvas = Image.new("RGB", (total_w, total_h), bg_color)

    for idx, path in enumerate(image_paths):
        row = idx // cols
        col = idx % cols
        img = Image.open(path).convert("RGB")

        # Fit image into cell with letterboxing
        iw, ih = img.size
        scale = min(cell_width / iw, cell_height / ih)
        nw, nh = int(iw * scale), int(ih * scale)
        img_resized = img.resize((nw, nh), Image.LANCZOS)

        x = padding + col * (cell_width + padding) + (cell_width - nw) // 2
        y = padding + row * (cell_height + padding) + (cell_height - nh) // 2
        canvas.paste(img_resized, (x, y))

    canvas.save(output_path)
    return output_path


# ---------------------------------------------------------------------------
# image_sharpen
# ---------------------------------------------------------------------------

def image_sharpen(
    input_path: str,
    output_path: str,
    *,
    strength: float = 1.0,
    radius: int = 2,
    threshold: int = 3,
) -> str:
    """Sharpen an image using PIL unsharp mask.

    Args:
        input_path: Source image file.
        output_path: Destination image file.
        strength: Sharpening amount multiplier (1.0 = standard unsharp mask).
        radius: Blur radius for unsharp mask in pixels.
        threshold: Minimum brightness difference to sharpen (0–255).

    Returns:
        The *output_path*.
    """
    from PIL import Image, ImageFilter

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

    img = Image.open(input_path).convert("RGB")
    # PIL UnsharpMask: radius, percent (strength*100), threshold
    sharpened = img.filter(
        ImageFilter.UnsharpMask(radius=radius, percent=int(strength * 150), threshold=threshold)
    )
    sharpened.save(output_path)
    return output_path


# ---------------------------------------------------------------------------
# image_blur
# ---------------------------------------------------------------------------

def image_blur(
    input_path: str,
    output_path: str,
    *,
    radius: float = 2.0,
) -> str:
    """Apply Gaussian blur to an image.

    Args:
        input_path: Source image file.
        output_path: Destination image file.
        radius: Blur radius in pixels (larger = more blur).

    Returns:
        The *output_path*.
    """
    from PIL import Image, ImageFilter

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

    img = Image.open(input_path).convert("RGB")
    blurred = img.filter(ImageFilter.GaussianBlur(radius=radius))
    blurred.save(output_path)
    return output_path


# ---------------------------------------------------------------------------
# image_contrast
# ---------------------------------------------------------------------------

def image_contrast(
    input_path: str,
    output_path: str,
    *,
    factor: float = 1.5,
) -> str:
    """Adjust image contrast.

    Args:
        input_path: Source image file.
        output_path: Destination image file.
        factor: Contrast multiplier. 1.0 = original, > 1.0 = more contrast,
            0.0 = solid grey.

    Returns:
        The *output_path*.
    """
    from PIL import Image, ImageEnhance

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    img = Image.open(input_path).convert("RGB")
    enhanced = ImageEnhance.Contrast(img).enhance(factor)
    enhanced.save(output_path)
    return output_path


# ---------------------------------------------------------------------------
# image_saturation
# ---------------------------------------------------------------------------

def image_saturation(
    input_path: str,
    output_path: str,
    *,
    factor: float = 1.5,
) -> str:
    """Adjust image color saturation.

    Args:
        input_path: Source image file.
        output_path: Destination image file.
        factor: Saturation multiplier. 1.0 = original, > 1.0 = more vivid,
            0.0 = grayscale.

    Returns:
        The *output_path*.
    """
    from PIL import Image, ImageEnhance

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    img = Image.open(input_path).convert("RGB")
    enhanced = ImageEnhance.Color(img).enhance(factor)
    enhanced.save(output_path)
    return output_path


def image_flip(input_path: str, output_path: str, *, direction: str = "horizontal") -> None:
    """Flip image horizontally or vertically using PIL.
    
    Args:
        direction: 'horizontal' or 'vertical'
    """
    from PIL import Image
    img = Image.open(input_path).convert("RGB")
    if direction == "horizontal":
        flipped = img.transpose(Image.FLIP_LEFT_RIGHT)
    elif direction == "vertical":
        flipped = img.transpose(Image.FLIP_TOP_BOTTOM)
    else:
        raise ValueError(f"direction must be 'horizontal' or 'vertical', got {direction!r}")
    flipped.save(output_path)


def image_rotate(input_path: str, output_path: str, *, angle: float = 90.0, expand: bool = True) -> None:
    """Rotate image by arbitrary degrees.
    
    Args:
        angle: Rotation angle in degrees, counter-clockwise. Default 90.
        expand: If True, expand output to fit the rotated image. Default True.
    """
    from PIL import Image
    img = Image.open(input_path).convert("RGB")
    rotated = img.rotate(angle, expand=expand)
    rotated.save(output_path)


def image_crop(input_path: str, output_path: str, *, left: int = 0, top: int = 0, right: int | None = None, bottom: int | None = None) -> None:
    """Crop image to a specific region.
    
    Args:
        left: Left pixel offset. Default 0.
        top: Top pixel offset. Default 0.
        right: Right pixel boundary. None means image width.
        bottom: Bottom pixel boundary. None means image height.
    """
    from PIL import Image
    img = Image.open(input_path).convert("RGB")
    w, h = img.size
    r = right if right is not None else w
    b = bottom if bottom is not None else h
    cropped = img.crop((left, top, r, b))
    cropped.save(output_path)


def image_resize_to_fit(
    input_path: str,
    output_path: str,
    *,
    max_width: int = 1920,
    max_height: int = 1080,
) -> None:
    """Resize image to fit within max_width x max_height preserving aspect ratio.

    Args:
        max_width: Maximum output width in pixels.
        max_height: Maximum output height in pixels.
    """
    from PIL import Image
    img = Image.open(input_path).convert("RGB")
    img.thumbnail((max_width, max_height), Image.LANCZOS)
    img.save(output_path)


def image_add_border(
    input_path: str,
    output_path: str,
    *,
    size: int = 20,
    color: tuple[int, int, int] = (255, 255, 255),
) -> None:
    """Add a solid color border around an image.

    Args:
        size: Border width in pixels. Default 20.
        color: Border RGB color tuple. Default white (255, 255, 255).
    """
    from PIL import Image, ImageOps
    img = Image.open(input_path).convert("RGB")
    bordered = ImageOps.expand(img, border=size, fill=color)
    bordered.save(output_path)


def image_grayscale(input_path: str, output_path: str) -> None:
    """Convert image to grayscale."""
    from PIL import Image, ImageOps
    img = Image.open(input_path).convert("RGB")
    gray = ImageOps.grayscale(img).convert("RGB")
    gray.save(output_path)


def image_invert(input_path: str, output_path: str) -> None:
    """Invert image colors."""
    from PIL import Image, ImageOps
    img = Image.open(input_path).convert("RGB")
    inverted = ImageOps.invert(img)
    inverted.save(output_path)


def image_posterize(input_path: str, output_path: str, *, bits: int = 4) -> None:
    """Posterize image by reducing color bit depth.

    Args:
        bits: Number of bits per channel (1-8). Lower = more posterized. Default 4.
    """
    from PIL import Image, ImageOps
    img = Image.open(input_path).convert("RGB")
    posterized = ImageOps.posterize(img, bits)
    posterized.save(output_path)


def image_solarize(input_path: str, output_path: str, *, threshold: int = 128) -> None:
    """Apply solarize effect (invert pixels above threshold).

    Args:
        threshold: Pixel value threshold 0-255. Pixels above are inverted. Default 128.
    """
    from PIL import Image, ImageOps
    img = Image.open(input_path).convert("RGB")
    solarized = ImageOps.solarize(img, threshold=threshold)
    solarized.save(output_path)


def image_pixelate(input_path: str, output_path: str, *, block_size: int = 16) -> None:
    """Pixelate image by downscaling then upscaling with nearest-neighbor interpolation.

    Args:
        block_size: Pixel block size in pixels. Larger = more pixelated. Default 16.
    """
    from PIL import Image
    img = Image.open(input_path).convert("RGB")
    w, h = img.size
    small_w = max(1, w // block_size)
    small_h = max(1, h // block_size)
    pixelated = img.resize((small_w, small_h), Image.NEAREST).resize((w, h), Image.NEAREST)
    pixelated.save(output_path)


def image_emboss(input_path: str, output_path: str) -> None:
    """Apply emboss effect to image."""
    from PIL import Image, ImageFilter
    img = Image.open(input_path).convert("RGB")
    embossed = img.filter(ImageFilter.EMBOSS)
    embossed.save(output_path)


def image_find_edges(input_path: str, output_path: str) -> None:
    """Apply edge detection to image using PIL FIND_EDGES filter."""
    from PIL import Image, ImageFilter
    img = Image.open(input_path).convert("RGB")
    edges = img.filter(ImageFilter.FIND_EDGES)
    edges.save(output_path)


def image_smooth(input_path: str, output_path: str) -> None:
    """Smooth image using PIL SMOOTH_MORE filter."""
    from PIL import Image, ImageFilter
    img = Image.open(input_path).convert("RGB")
    smoothed = img.filter(ImageFilter.SMOOTH_MORE)
    smoothed.save(output_path)


def image_auto_enhance(input_path: str, output_path: str) -> None:
    """Auto-enhance image by applying auto-contrast and equalizing histogram."""
    from PIL import Image, ImageOps
    img = Image.open(input_path).convert("RGB")
    enhanced = ImageOps.autocontrast(img, cutoff=1)
    enhanced.save(output_path)


def image_tint(
    input_path: str,
    output_path: str,
    *,
    color: tuple[int, int, int] = (255, 100, 0),
    strength: float = 0.3,
) -> None:
    """Apply a color tint to image by blending with a solid color.

    Args:
        color: RGB color tuple for the tint. Default orange (255, 100, 0).
        strength: Blend strength 0-1. 0 = no tint, 1 = full color. Default 0.3.
    """
    from PIL import Image
    import numpy as np
    img = np.array(Image.open(input_path).convert("RGB"), dtype=np.float32)
    tint = np.array(color, dtype=np.float32)
    blended = img * (1.0 - strength) + tint * strength
    blended = np.clip(blended, 0, 255).astype(np.uint8)
    Image.fromarray(blended).save(output_path)


def image_watermark_text(
    input_path: str,
    output_path: str,
    *,
    text: str = "WATERMARK",
    position: str = "bottom_right",
    opacity: float = 0.5,
    font_size: int = 24,
    color: tuple[int, int, int] = (255, 255, 255),
) -> None:
    """Add text watermark to image.

    Args:
        text: Watermark text. Default 'WATERMARK'.
        position: One of 'top_left', 'top_right', 'bottom_left', 'bottom_right', 'center'. Default 'bottom_right'.
        opacity: Text opacity 0-1. Default 0.5.
        font_size: Font size in pixels. Default 24.
        color: RGB text color. Default white.
    """
    from PIL import Image, ImageDraw, ImageFont
    import os
    img = Image.open(input_path).convert("RGBA")
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    # Try to use a default font
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", font_size)
    except Exception:
        font = ImageFont.load_default()
    bbox = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    w, h = img.size
    margin = 10
    pos_map = {
        "top_left": (margin, margin),
        "top_right": (w - tw - margin, margin),
        "bottom_left": (margin, h - th - margin),
        "bottom_right": (w - tw - margin, h - th - margin),
        "center": ((w - tw) // 2, (h - th) // 2),
    }
    xy = pos_map.get(position, pos_map["bottom_right"])
    alpha = int(opacity * 255)
    draw.text(xy, text, font=font, fill=(*color, alpha))
    result = Image.alpha_composite(img, overlay).convert("RGB")
    result.save(output_path)


def image_rounded_corners(input_path: str, output_path: str, *, radius: int = 30) -> None:
    """Apply rounded corners to image using an alpha mask.

    Args:
        radius: Corner radius in pixels. Default 30.
    """
    from PIL import Image, ImageDraw
    img = Image.open(input_path).convert("RGBA")
    w, h = img.size
    mask = Image.new("L", (w, h), 0)
    draw = ImageDraw.Draw(mask)
    draw.rounded_rectangle([0, 0, w - 1, h - 1], radius=radius, fill=255)
    img.putalpha(mask)
    # Save with transparency or convert to RGB on white background if output is JPEG
    if output_path.lower().endswith((".jpg", ".jpeg")):
        bg = Image.new("RGB", (w, h), (255, 255, 255))
        bg.paste(img, mask=img.split()[3])
        bg.save(output_path)
    else:
        img.save(output_path)


def image_composite_alpha(
    background_path: str,
    foreground_path: str,
    output_path: str,
    *,
    x: int = 0,
    y: int = 0,
) -> None:
    """Composite a foreground RGBA image over a background image.

    Args:
        background_path: Path to background image (any mode).
        foreground_path: Path to foreground image (should have alpha channel).
        x: X offset for foreground placement. Default 0.
        y: Y offset for foreground placement. Default 0.
    """
    from PIL import Image
    bg = Image.open(background_path).convert("RGBA")
    fg = Image.open(foreground_path).convert("RGBA")
    canvas = Image.new("RGBA", bg.size, (0, 0, 0, 0))
    canvas.paste(bg, (0, 0))
    canvas.paste(fg, (x, y), mask=fg.split()[3])
    result = canvas.convert("RGB")
    result.save(output_path)


def image_adjust_hsl(
    input_path: str,
    output_path: str,
    *,
    hue_shift: float = 0.0,
    saturation: float = 1.0,
    lightness: float = 0.0,
) -> None:
    """Adjust image hue, saturation, and lightness.

    Args:
        hue_shift: Hue rotation in degrees (-180 to 180). Default 0.0.
        saturation: Saturation multiplier (0=gray, 1=original, 2=doubled). Default 1.0.
        lightness: Lightness offset (-1 to 1). Default 0.0.
    """
    import colorsys
    import numpy as np
    from PIL import Image
    img = np.array(Image.open(input_path).convert("RGB"), dtype=np.float32) / 255.0
    out = np.zeros_like(img)
    h_shift = hue_shift / 360.0
    for i in range(img.shape[0]):
        for j in range(img.shape[1]):
            r, g, b = img[i, j]
            h, l, s = colorsys.rgb_to_hls(r, g, b)
            h = (h + h_shift) % 1.0
            s = max(0.0, min(1.0, s * saturation))
            l = max(0.0, min(1.0, l + lightness))
            out[i, j] = colorsys.hls_to_rgb(h, l, s)
    result = (np.clip(out, 0, 1) * 255).astype(np.uint8)
    Image.fromarray(result).save(output_path)


def image_resize_canvas(
    input_path: str,
    output_path: str,
    *,
    width: int,
    height: int,
    fill_color: tuple[int, int, int] = (0, 0, 0),
    anchor: str = "center",
) -> None:
    """Resize canvas to target dimensions, padding with fill_color without scaling the image.

    Args:
        width: Target canvas width in pixels.
        height: Target canvas height in pixels.
        fill_color: Background fill color RGB. Default black.
        anchor: Image placement: 'center', 'top_left', 'top_right', 'bottom_left', 'bottom_right'. Default 'center'.
    """
    from PIL import Image
    img = Image.open(input_path).convert("RGB")
    iw, ih = img.size
    canvas = Image.new("RGB", (width, height), fill_color)
    if anchor == "center":
        x, y = (width - iw) // 2, (height - ih) // 2
    elif anchor == "top_left":
        x, y = 0, 0
    elif anchor == "top_right":
        x, y = width - iw, 0
    elif anchor == "bottom_left":
        x, y = 0, height - ih
    elif anchor == "bottom_right":
        x, y = width - iw, height - ih
    else:
        x, y = (width - iw) // 2, (height - ih) // 2
    canvas.paste(img, (max(0, x), max(0, y)))
    canvas.save(output_path)


def image_collage(
    image_paths: list[str],
    output_path: str,
    *,
    cols: int = 3,
    thumb_width: int = 200,
    thumb_height: int = 150,
    gap: int = 5,
    bg_color: tuple[int, int, int] = (40, 40, 40),
) -> None:
    """Create a grid collage from multiple images.

    Args:
        image_paths: List of input image paths.
        cols: Number of columns in the grid. Default 3.
        thumb_width: Width of each thumbnail. Default 200.
        thumb_height: Height of each thumbnail. Default 150.
        gap: Gap in pixels between thumbnails. Default 5.
        bg_color: Background color RGB. Default dark gray.
    """
    from PIL import Image
    n = len(image_paths)
    rows = (n + cols - 1) // cols
    canvas_w = cols * thumb_width + (cols + 1) * gap
    canvas_h = rows * thumb_height + (rows + 1) * gap
    canvas = Image.new("RGB", (canvas_w, canvas_h), bg_color)
    for idx, path in enumerate(image_paths):
        img = Image.open(path).convert("RGB")
        img.thumbnail((thumb_width, thumb_height), Image.LANCZOS)
        col = idx % cols
        row = idx // cols
        x = gap + col * (thumb_width + gap) + (thumb_width - img.width) // 2
        y = gap + row * (thumb_height + gap) + (thumb_height - img.height) // 2
        canvas.paste(img, (x, y))
    canvas.save(output_path)


def image_sketch(
    input_path: str,
    output_path: str,
    *,
    blur_radius: int = 21,
    intensity: float = 1.0,
) -> None:
    """Convert an image to a pencil-sketch look.

    Args:
        blur_radius: Gaussian blur radius (odd int) for the dodge step. Default 21.
        intensity: Blend intensity 0–1. Default 1.0 (full sketch).
    """
    from PIL import Image, ImageFilter, ImageChops
    import numpy as np

    img = Image.open(input_path).convert("RGB")
    gray = img.convert("L")
    # Invert and blur (dodge layer)
    inv = ImageChops.invert(gray)
    radius = max(1, blur_radius | 1)  # ensure odd
    blurred = inv.filter(ImageFilter.GaussianBlur(radius=radius))
    # Color dodge: gray / (1 - blurred/255)
    g = np.array(gray, dtype=np.float32)
    b = np.array(blurred, dtype=np.float32)
    dodge = np.clip(g / (1.0 - b / 255.0 + 1e-7) * 255.0, 0, 255).astype(np.uint8)
    sketch = Image.fromarray(dodge, "L")
    # Blend with original gray according to intensity
    if intensity < 1.0:
        sketch = Image.blend(gray, sketch, intensity)
    sketch.convert("RGB").save(output_path)


def image_oil_paint(
    input_path: str,
    output_path: str,
    *,
    radius: int = 4,
    levels: int = 8,
) -> None:
    """Apply an oil-paint stylization effect.

    Args:
        radius: Brush radius in pixels. Default 4.
        levels: Number of intensity levels for quantization. Default 8.
    """
    from PIL import Image
    import numpy as np
    from collections import Counter

    img = Image.open(input_path).convert("RGB")
    arr = np.array(img, dtype=np.uint8)
    h, w = arr.shape[:2]
    out = arr.copy()
    # Quantize intensity to discrete levels
    gray = (arr[..., 0].astype(np.float32) * 0.299
            + arr[..., 1].astype(np.float32) * 0.587
            + arr[..., 2].astype(np.float32) * 0.114)
    quantized = (gray / 255.0 * (levels - 1)).astype(np.int32)

    r = radius
    for y in range(r, h - r):
        for x in range(r, w - r):
            patch_q = quantized[y - r:y + r + 1, x - r:x + r + 1].ravel()
            patch_c = arr[y - r:y + r + 1, x - r:x + r + 1].reshape(-1, 3)
            # Find most common intensity level
            most_common_level = Counter(patch_q).most_common(1)[0][0]
            mask = patch_q == most_common_level
            out[y, x] = patch_c[mask].mean(axis=0).astype(np.uint8)

    Image.fromarray(out).save(output_path)


def image_cartoon(
    input_path: str,
    output_path: str,
    *,
    blur_radius: int = 5,
    edge_threshold: int = 100,
    levels: int = 6,
) -> None:
    """Apply a cartoon effect: color quantization + edge overlay.

    Args:
        blur_radius: Bilateral-like blur radius. Default 5.
        edge_threshold: Canny-style edge threshold (0-255). Default 100.
        levels: Color quantization levels. Default 6.
    """
    from PIL import Image, ImageFilter
    import numpy as np

    img = Image.open(input_path).convert("RGB")
    arr = np.array(img, dtype=np.float32)

    # Quantize colors
    quantized = (np.floor(arr / (256.0 / levels)) * (256.0 / levels)).clip(0, 255).astype(np.uint8)

    # Smooth the quantized image
    smooth = Image.fromarray(quantized).filter(ImageFilter.MedianFilter(size=max(3, blur_radius | 1)))

    # Edge detection on grayscale
    gray = img.convert("L")
    edges = gray.filter(ImageFilter.FIND_EDGES)
    edges_arr = np.array(edges)
    edge_mask = (edges_arr > edge_threshold).astype(np.uint8) * 255
    edge_img = Image.fromarray(edge_mask, "L")

    # Overlay edges as black lines onto smooth image
    smooth_arr = np.array(smooth)
    mask = np.array(edge_img) > 128
    smooth_arr[mask] = 0
    Image.fromarray(smooth_arr).save(output_path)


def image_sepia(
    input_path: str,
    output_path: str,
    *,
    intensity: float = 1.0,
) -> None:
    """Apply a sepia tone to an image.

    Args:
        intensity: Blend intensity 0–1. Default 1.0 (full sepia).
    """
    from PIL import Image
    import numpy as np

    img = Image.open(input_path).convert("RGB")
    arr = np.array(img, dtype=np.float32) / 255.0
    r = arr[..., 0] * 0.393 + arr[..., 1] * 0.769 + arr[..., 2] * 0.189
    g = arr[..., 0] * 0.349 + arr[..., 1] * 0.686 + arr[..., 2] * 0.168
    b = arr[..., 0] * 0.272 + arr[..., 1] * 0.534 + arr[..., 2] * 0.131
    sepia = np.stack([r, g, b], axis=-1).clip(0, 1)
    result = (sepia * intensity + arr * (1.0 - intensity)).clip(0, 1)
    Image.fromarray((result * 255).astype(np.uint8)).save(output_path)


def image_hdr_simulate(
    input_path: str,
    output_path: str,
    *,
    gamma: float = 0.85,
    unsharp_strength: float = 1.5,
    saturation_boost: float = 1.3,
) -> None:
    """Simulate an HDR look via gamma + local contrast + saturation boost.

    Args:
        gamma: Gamma correction < 1 brightens shadows. Default 0.85.
        unsharp_strength: Local contrast unsharp amount. Default 1.5.
        saturation_boost: Saturation multiplier. Default 1.3.
    """
    from PIL import Image, ImageFilter, ImageEnhance
    import numpy as np

    img = Image.open(input_path).convert("RGB")
    arr = np.array(img, dtype=np.float32) / 255.0

    # Gamma
    arr = np.power(arr, gamma)

    # Unsharp mask for local contrast
    base = Image.fromarray((arr * 255).clip(0, 255).astype(np.uint8))
    blurred = base.filter(ImageFilter.GaussianBlur(radius=3))
    b_arr = np.array(blurred, dtype=np.float32) / 255.0
    arr = np.clip(arr + unsharp_strength * (arr - b_arr), 0, 1)

    result = Image.fromarray((arr * 255).astype(np.uint8))
    # Saturation boost
    result = ImageEnhance.Color(result).enhance(saturation_boost)
    result.save(output_path)


def image_lens_blur(
    input_path: str,
    output_path: str,
    *,
    blur_radius: int = 15,
    center_x: float = 0.5,
    center_y: float = 0.5,
    focus_radius: float = 0.25,
) -> None:
    """Apply radial lens blur — sharp center, blurred periphery.

    Args:
        blur_radius: Gaussian blur radius for edges. Default 15.
        center_x: Horizontal center 0–1. Default 0.5.
        center_y: Vertical center 0–1. Default 0.5.
        focus_radius: Fraction of image dimension kept sharp. Default 0.25.
    """
    from PIL import Image, ImageFilter
    import numpy as np

    img = Image.open(input_path).convert("RGB")
    blurred = img.filter(ImageFilter.GaussianBlur(radius=blur_radius))
    w, h = img.size
    cx, cy = int(center_x * w), int(center_y * h)
    focus_r = focus_radius * min(w, h)

    ys, xs = np.mgrid[0:h, 0:w]
    dist = np.sqrt((xs - cx) ** 2 + (ys - cy) ** 2)
    # Alpha: 0 at center (use sharp), 1 far out (use blur)
    alpha = np.clip((dist - focus_r) / (focus_r * 2), 0, 1)[..., np.newaxis]

    orig_arr = np.array(img, dtype=np.float32)
    blur_arr = np.array(blurred, dtype=np.float32)
    result = (orig_arr * (1 - alpha) + blur_arr * alpha).clip(0, 255).astype(np.uint8)
    Image.fromarray(result).save(output_path)


def image_cross_process(
    input_path: str,
    output_path: str,
) -> None:
    """Simulate cross-process film look via channel-specific gamma curves."""
    from PIL import Image
    import numpy as np

    img = Image.open(input_path).convert("RGB")
    arr = np.array(img, dtype=np.float32) / 255.0
    # Red: boosted contrast
    r = np.power(arr[..., 0], 0.7)
    # Green: slight S-curve via gamma
    g = np.power(arr[..., 1], 1.2)
    # Blue: boosted and pushed towards cyan
    b = np.power(arr[..., 2], 0.6) * 0.9 + 0.05
    result = np.stack([r, g, b], axis=-1).clip(0, 1)
    Image.fromarray((result * 255).astype(np.uint8)).save(output_path)


def image_halftone(
    input_path: str,
    output_path: str,
    *,
    dot_size: int = 8,
) -> None:
    """Apply a halftone dot pattern effect.

    Args:
        dot_size: Cell size in pixels. Default 8.
    """
    from PIL import Image, ImageDraw
    import numpy as np

    img = Image.open(input_path).convert("L")
    w, h = img.size
    arr = np.array(img, dtype=np.float32) / 255.0
    canvas = Image.new("L", (w, h), 255)
    draw = ImageDraw.Draw(canvas)
    half = dot_size // 2
    for y in range(0, h, dot_size):
        for x in range(0, w, dot_size):
            # Average brightness in cell
            cell = arr[y:y + dot_size, x:x + dot_size]
            if cell.size == 0:
                continue
            brightness = cell.mean()
            radius = int((1 - brightness) * half)
            cx, cy = x + half, y + half
            draw.ellipse([cx - radius, cy - radius, cx + radius, cy + radius], fill=0)
    canvas.convert("RGB").save(output_path)


def image_noise(
    input_path: str,
    output_path: str,
    *,
    amount: float = 0.05,
    noise_type: str = "gaussian",
) -> None:
    """Add random noise to an image.

    Args:
        amount: Noise standard deviation (gaussian) or max uniform range, 0–1. Default 0.05.
        noise_type: 'gaussian' or 'uniform'. Default 'gaussian'.
    """
    from PIL import Image
    import numpy as np

    img = Image.open(input_path).convert("RGB")
    arr = np.array(img, dtype=np.float32) / 255.0
    if noise_type == "gaussian":
        noise = np.random.normal(0, amount, arr.shape).astype(np.float32)
    else:
        noise = np.random.uniform(-amount, amount, arr.shape).astype(np.float32)
    result = (arr + noise).clip(0, 1)
    Image.fromarray((result * 255).astype(np.uint8)).save(output_path)


def image_dither(
    input_path: str,
    output_path: str,
    *,
    colors: int = 16,
) -> None:
    """Reduce image to N colors with Floyd-Steinberg dithering.

    Args:
        colors: Number of colors. Default 16.
    """
    from PIL import Image

    img = Image.open(input_path).convert("RGB")
    # Quantize with dithering
    quantized = img.quantize(colors=colors, dither=Image.Dither.FLOYDSTEINBERG)
    quantized.convert("RGB").save(output_path)


def image_clahe(
    input_path: str,
    output_path: str,
    *,
    clip_limit: float = 2.0,
    tile_size: int = 8,
) -> None:
    """Apply CLAHE (Contrast-Limited Adaptive Histogram Equalization).

    Args:
        clip_limit: Contrast clipping limit. Default 2.0.
        tile_size: Grid tile size. Default 8.
    """
    from PIL import Image
    import numpy as np

    img = Image.open(input_path).convert("RGB")
    arr = np.array(img, dtype=np.uint8)

    def _clahe_channel(ch):
        h, w = ch.shape
        th, tw = tile_size, tile_size
        rows = max(1, (h + th - 1) // th)
        cols = max(1, (w + tw - 1) // tw)
        out = np.zeros_like(ch, dtype=np.float32)
        for r in range(rows):
            for c in range(cols):
                y0, y1 = r * th, min((r + 1) * th, h)
                x0, x1 = c * tw, min((c + 1) * tw, w)
                tile = ch[y0:y1, x0:x1]
                hist, bins = np.histogram(tile.ravel(), 256, [0, 256])
                # Clip and redistribute
                excess = np.sum(np.maximum(hist - clip_limit * tile.size / 256, 0))
                hist = np.minimum(hist, clip_limit * tile.size / 256)
                hist += excess / 256
                cdf = hist.cumsum()
                cdf_min = cdf[cdf > 0][0] if cdf[cdf > 0].size else 1
                total = tile.size
                lut = np.clip(np.round((cdf - cdf_min) / (total - cdf_min) * 255), 0, 255).astype(np.uint8)
                out[y0:y1, x0:x1] = lut[tile]
        return out.clip(0, 255).astype(np.uint8)

    result = np.stack([_clahe_channel(arr[..., i]) for i in range(3)], axis=-1)
    Image.fromarray(result).save(output_path)


def image_palette_swap(
    input_path: str,
    output_path: str,
    palette: list[tuple[int, int, int]],
) -> None:
    """Map every pixel to the nearest color in the provided palette.

    Args:
        palette: List of RGB tuples to use as the target palette.
    """
    from PIL import Image
    import numpy as np

    img = Image.open(input_path).convert("RGB")
    arr = np.array(img, dtype=np.float32)
    pal = np.array(palette, dtype=np.float32)  # (N, 3)
    # Compute nearest palette color per pixel
    flat = arr.reshape(-1, 3)  # (H*W, 3)
    diffs = flat[:, np.newaxis, :] - pal[np.newaxis, :, :]  # (H*W, N, 3)
    idx = np.argmin((diffs ** 2).sum(axis=-1), axis=1)  # (H*W,)
    result = pal[idx].reshape(arr.shape).astype(np.uint8)
    Image.fromarray(result).save(output_path)


def image_channel_split(
    input_path: str,
    output_r: str,
    output_g: str,
    output_b: str,
) -> None:
    """Split an RGB image into three grayscale channel images.

    Args:
        output_r: Path for the red-channel image.
        output_g: Path for the green-channel image.
        output_b: Path for the blue-channel image.
    """
    from PIL import Image

    img = Image.open(input_path).convert("RGB")
    r, g, b = img.split()
    r.save(output_r)
    g.save(output_g)
    b.save(output_b)


def image_channel_merge(
    input_r: str,
    input_g: str,
    input_b: str,
    output_path: str,
) -> None:
    """Merge three grayscale images into a single RGB image.

    Args:
        input_r: Path to red-channel image.
        input_g: Path to green-channel image.
        input_b: Path to blue-channel image.
    """
    from PIL import Image

    r = Image.open(input_r).convert("L")
    g = Image.open(input_g).convert("L")
    b = Image.open(input_b).convert("L")
    Image.merge("RGB", (r, g, b)).save(output_path)


def image_blend_overlay(
    base_path: str,
    blend_path: str,
    output_path: str,
    *,
    opacity: float = 1.0,
) -> None:
    """Blend two images using the Overlay blend mode.

    Overlay = 2*a*b if a<0.5, else 1-2*(1-a)*(1-b).

    Args:
        opacity: Blend strength 0–1. Default 1.0.
    """
    from PIL import Image
    import numpy as np

    base = Image.open(base_path).convert("RGB")
    blend = Image.open(blend_path).convert("RGB").resize(base.size, Image.LANCZOS)
    a = np.array(base, dtype=np.float32) / 255.0
    b = np.array(blend, dtype=np.float32) / 255.0
    overlay = np.where(a < 0.5, 2 * a * b, 1 - 2 * (1 - a) * (1 - b))
    result = (a * (1 - opacity) + overlay * opacity).clip(0, 1)
    Image.fromarray((result * 255).astype(np.uint8)).save(output_path)


def image_blend_multiply(
    base_path: str,
    blend_path: str,
    output_path: str,
    *,
    opacity: float = 1.0,
) -> None:
    """Blend two images using the Multiply blend mode.

    Args:
        opacity: Blend strength 0–1. Default 1.0.
    """
    from PIL import Image
    import numpy as np

    base = Image.open(base_path).convert("RGB")
    blend = Image.open(blend_path).convert("RGB").resize(base.size, Image.LANCZOS)
    a = np.array(base, dtype=np.float32) / 255.0
    b = np.array(blend, dtype=np.float32) / 255.0
    mult = a * b
    result = (a * (1 - opacity) + mult * opacity).clip(0, 1)
    Image.fromarray((result * 255).astype(np.uint8)).save(output_path)


def image_blend_screen(
    base_path: str,
    blend_path: str,
    output_path: str,
    *,
    opacity: float = 1.0,
) -> None:
    """Blend two images using the Screen blend mode.

    Screen = 1 - (1-a)*(1-b).

    Args:
        opacity: Blend strength 0–1. Default 1.0.
    """
    from PIL import Image
    import numpy as np

    base = Image.open(base_path).convert("RGB")
    blend = Image.open(blend_path).convert("RGB").resize(base.size, Image.LANCZOS)
    a = np.array(base, dtype=np.float32) / 255.0
    b = np.array(blend, dtype=np.float32) / 255.0
    screen = 1.0 - (1.0 - a) * (1.0 - b)
    result = (a * (1 - opacity) + screen * opacity).clip(0, 1)
    Image.fromarray((result * 255).astype(np.uint8)).save(output_path)


def image_pixelate_region(
    input_path: str,
    output_path: str,
    *,
    x: int = 0,
    y: int = 0,
    width: int = 50,
    height: int = 50,
    block_size: int = 10,
) -> None:
    """Pixelate a rectangular region of an image (for privacy/censorship).

    Args:
        x: X offset of region. Default 0.
        y: Y offset of region. Default 0.
        width: Region width in pixels. Default 50.
        height: Region height in pixels. Default 50.
        block_size: Pixel block size. Default 10.
    """
    from PIL import Image

    img = Image.open(input_path).convert("RGB")
    region = img.crop((x, y, x + width, y + height))
    # Downscale then upscale to pixelate
    small = region.resize(
        (max(1, width // block_size), max(1, height // block_size)),
        Image.NEAREST,
    )
    pixelated = small.resize((width, height), Image.NEAREST)
    img.paste(pixelated, (x, y))
    img.save(output_path)


def image_text_overlay(
    input_path: str,
    output_path: str,
    text: str,
    *,
    x: int = 10,
    y: int = 10,
    font_size: int = 20,
    color: tuple[int, int, int] = (255, 255, 255),
) -> None:
    """Draw text onto an image.

    Args:
        text: Text string to draw.
        x: X position. Default 10.
        y: Y position. Default 10.
        font_size: Font size in points. Default 20.
        color: RGB color tuple. Default white.
    """
    from PIL import Image, ImageDraw, ImageFont

    img = Image.open(input_path).convert("RGB")
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", font_size)
    except Exception:
        font = ImageFont.load_default()
    draw.text((x, y), text, fill=color, font=font)
    img.save(output_path)


def image_draw_rect(
    input_path: str,
    output_path: str,
    *,
    x: int = 10,
    y: int = 10,
    width: int = 50,
    height: int = 50,
    color: tuple[int, int, int] = (255, 0, 0),
    fill: bool = False,
    line_width: int = 2,
) -> None:
    """Draw a rectangle onto an image.

    Args:
        x: X offset. Default 10.
        y: Y offset. Default 10.
        width: Rectangle width. Default 50.
        height: Rectangle height. Default 50.
        color: RGB color. Default red.
        fill: Fill rectangle if True. Default False (outline only).
        line_width: Outline width in pixels. Default 2.
    """
    from PIL import Image, ImageDraw

    img = Image.open(input_path).convert("RGB")
    draw = ImageDraw.Draw(img)
    bbox = [x, y, x + width, y + height]
    if fill:
        draw.rectangle(bbox, fill=color)
    else:
        draw.rectangle(bbox, outline=color, width=line_width)
    img.save(output_path)


def image_histogram_equalize(
    input_path: str,
    output_path: str,
) -> None:
    """Apply global histogram equalization per channel for contrast enhancement."""
    from PIL import Image
    import numpy as np

    img = Image.open(input_path).convert("RGB")
    arr = np.array(img, dtype=np.uint8)

    def _equalize(ch):
        hist, _ = np.histogram(ch.ravel(), 256, [0, 256])
        cdf = hist.cumsum()
        cdf_min = cdf[cdf > 0][0]
        total = ch.size
        lut = np.clip(np.round((cdf - cdf_min) / (total - cdf_min) * 255), 0, 255).astype(np.uint8)
        return lut[ch]

    result = np.stack([_equalize(arr[..., i]) for i in range(3)], axis=-1)
    Image.fromarray(result).save(output_path)


def image_mosaic(
    input_path: str,
    output_path: str,
    *,
    tile_width: int = 50,
    tile_height: int = 50,
    cols: int = 5,
    rows: int = 5,
) -> None:
    """Create a mosaic by tiling a thumbnail of the image across a grid.

    Args:
        tile_width: Width of each tile. Default 50.
        tile_height: Height of each tile. Default 50.
        cols: Number of columns. Default 5.
        rows: Number of rows. Default 5.
    """
    from PIL import Image

    img = Image.open(input_path).convert("RGB")
    tile = img.resize((tile_width, tile_height), Image.LANCZOS)
    canvas = Image.new("RGB", (cols * tile_width, rows * tile_height))
    for r in range(rows):
        for c in range(cols):
            canvas.paste(tile, (c * tile_width, r * tile_height))
    canvas.save(output_path)


def image_perspective_warp(
    input_path: str,
    output_path: str,
    src_points: list[tuple[float, float]],
    dst_points: list[tuple[float, float]],
) -> None:
    """Apply perspective warp from 4 source corners to 4 destination corners.

    Args:
        src_points: 4 (x, y) source corner points.
        dst_points: 4 (x, y) destination corner points.
    """
    from PIL import Image
    import numpy as np

    img = Image.open(input_path).convert("RGB")
    w, h = img.size

    # Compute perspective transform coefficients
    def _find_coeffs(source, target):
        matrix = []
        for s, t in zip(source, target):
            matrix.append([t[0], t[1], 1, 0, 0, 0, -s[0]*t[0], -s[0]*t[1]])
            matrix.append([0, 0, 0, t[0], t[1], 1, -s[1]*t[0], -s[1]*t[1]])
        A = np.array(matrix, dtype=np.float64)
        B = np.array(src_points, dtype=np.float64).ravel()
        res = np.linalg.lstsq(A, B, rcond=None)[0]
        return tuple(res)

    coeffs = _find_coeffs(src_points, dst_points)
    result = img.transform((w, h), Image.PERSPECTIVE, coeffs, Image.BICUBIC)
    result.save(output_path)


def image_normalize_brightness(
    input_path: str,
    output_path: str,
    *,
    target: float = 0.5,
) -> None:
    """Normalize image so mean brightness equals target.

    Args:
        target: Target mean brightness 0–1. Default 0.5.
    """
    from PIL import Image
    import numpy as np

    img = Image.open(input_path).convert("RGB")
    arr = np.array(img, dtype=np.float32) / 255.0
    mean = arr.mean()
    if mean < 1e-6:
        Image.fromarray((arr * 255).astype(np.uint8)).save(output_path)
        return
    scale = target / mean
    result = (arr * scale).clip(0, 1)
    Image.fromarray((result * 255).astype(np.uint8)).save(output_path)


def image_split_quadrants(
    input_path: str,
    output_tl: str,
    output_tr: str,
    output_bl: str,
    output_br: str,
) -> None:
    """Split an image into 4 quadrant images (top-left, top-right, bottom-left, bottom-right).

    Args:
        output_tl: Path for top-left quadrant.
        output_tr: Path for top-right quadrant.
        output_bl: Path for bottom-left quadrant.
        output_br: Path for bottom-right quadrant.
    """
    from PIL import Image

    img = Image.open(input_path).convert("RGB")
    w, h = img.size
    hw, hh = w // 2, h // 2
    img.crop((0, 0, hw, hh)).save(output_tl)
    img.crop((hw, 0, w, hh)).save(output_tr)
    img.crop((0, hh, hw, h)).save(output_bl)
    img.crop((hw, hh, w, h)).save(output_br)


def image_stitch_horizontal(
    image_paths: list[str],
    output_path: str,
    *,
    align: str = "top",
) -> None:
    """Stitch images side-by-side horizontally.

    Args:
        align: Vertical alignment of images: 'top', 'center', or 'bottom'. Default 'top'.
    """
    from PIL import Image

    imgs = [Image.open(p).convert("RGB") for p in image_paths]
    total_w = sum(im.width for im in imgs)
    max_h = max(im.height for im in imgs)
    canvas = Image.new("RGB", (total_w, max_h), (0, 0, 0))
    x = 0
    for im in imgs:
        if align == "center":
            y = (max_h - im.height) // 2
        elif align == "bottom":
            y = max_h - im.height
        else:
            y = 0
        canvas.paste(im, (x, y))
        x += im.width
    canvas.save(output_path)


def image_stitch_vertical(
    image_paths: list[str],
    output_path: str,
    *,
    align: str = "left",
) -> None:
    """Stitch images vertically (top to bottom).

    Args:
        align: Horizontal alignment: 'left', 'center', or 'right'. Default 'left'.
    """
    from PIL import Image

    imgs = [Image.open(p).convert("RGB") for p in image_paths]
    max_w = max(im.width for im in imgs)
    total_h = sum(im.height for im in imgs)
    canvas = Image.new("RGB", (max_w, total_h), (0, 0, 0))
    y = 0
    for im in imgs:
        if align == "center":
            x = (max_w - im.width) // 2
        elif align == "right":
            x = max_w - im.width
        else:
            x = 0
        canvas.paste(im, (x, y))
        y += im.height
    canvas.save(output_path)


def image_radial_gradient(
    output_path: str,
    *,
    width: int = 256,
    height: int = 256,
    center_color: tuple[int, int, int] = (255, 255, 255),
    edge_color: tuple[int, int, int] = (0, 0, 0),
) -> None:
    """Generate a radial gradient image.

    Args:
        width: Output width. Default 256.
        height: Output height. Default 256.
        center_color: RGB color at center. Default white.
        edge_color: RGB color at edges. Default black.
    """
    from PIL import Image
    import numpy as np

    ys, xs = np.mgrid[0:height, 0:width]
    cx, cy = width / 2, height / 2
    dist = np.sqrt((xs - cx) ** 2 + (ys - cy) ** 2)
    max_dist = np.sqrt(cx ** 2 + cy ** 2)
    t = (dist / max_dist).clip(0, 1)[..., np.newaxis]
    c = np.array(center_color, dtype=np.float32)
    e = np.array(edge_color, dtype=np.float32)
    result = (c * (1 - t) + e * t).clip(0, 255).astype(np.uint8)
    Image.fromarray(result).save(output_path)


def image_linear_gradient(
    output_path: str,
    *,
    width: int = 256,
    height: int = 256,
    start_color: tuple[int, int, int] = (0, 0, 0),
    end_color: tuple[int, int, int] = (255, 255, 255),
    direction: str = "horizontal",
) -> None:
    """Generate a linear gradient image.

    Args:
        width: Output width. Default 256.
        height: Output height. Default 256.
        start_color: RGB color at start. Default black.
        end_color: RGB color at end. Default white.
        direction: 'horizontal' (left→right) or 'vertical' (top→bottom). Default 'horizontal'.
    """
    from PIL import Image
    import numpy as np

    s = np.array(start_color, dtype=np.float32)
    e = np.array(end_color, dtype=np.float32)
    if direction == "vertical":
        t = np.linspace(0, 1, height)[:, np.newaxis, np.newaxis]
        arr = (s * (1 - t) + e * t).clip(0, 255).astype(np.uint8)
        arr = np.broadcast_to(arr, (height, width, 3)).copy()
    else:
        t = np.linspace(0, 1, width)[np.newaxis, :, np.newaxis]
        arr = (s * (1 - t) + e * t).clip(0, 255).astype(np.uint8)
        arr = np.broadcast_to(arr, (height, width, 3)).copy()
    Image.fromarray(arr).save(output_path)


def image_detect_faces(
    input_path: str,
    *,
    min_size: int = 20,
) -> list[tuple[int, int, int, int]]:
    """Detect face-like regions using a simple skin-tone heuristic.

    Returns a list of (x, y, width, height) bounding boxes.
    Note: This is a heuristic approach — use OpenCV for production.

    Args:
        min_size: Minimum region size in pixels. Default 20.
    """
    from PIL import Image
    import numpy as np

    img = Image.open(input_path).convert("RGB")
    arr = np.array(img, dtype=np.float32)
    r, g, b = arr[..., 0], arr[..., 1], arr[..., 2]

    # Skin-tone mask: R > 95, G > 40, B > 20, R > G, R > B, |R-G| > 15
    mask = (
        (r > 95) & (g > 40) & (b > 20) &
        (r > g) & (r > b) &
        (np.abs(r - g) > 15)
    ).astype(np.uint8)

    # Find connected bounding box using row/col projections (simple approach)
    boxes = []
    rows_with_skin = np.where(mask.sum(axis=1) > min_size)[0]
    if len(rows_with_skin) == 0:
        return boxes

    # Group contiguous row spans
    spans = []
    start = rows_with_skin[0]
    prev = rows_with_skin[0]
    for row in rows_with_skin[1:]:
        if row - prev > 10:
            spans.append((start, prev))
            start = row
        prev = row
    spans.append((start, prev))

    for y0, y1 in spans:
        region_mask = mask[y0:y1 + 1]
        cols_with_skin = np.where(region_mask.sum(axis=0) > min_size)[0]
        if len(cols_with_skin) == 0:
            continue
        x0, x1 = int(cols_with_skin[0]), int(cols_with_skin[-1])
        w = x1 - x0
        h = int(y1 - y0)
        if w >= min_size and h >= min_size:
            boxes.append((x0, int(y0), w, h))
    return boxes


def image_grid_overlay(
    input_path: str,
    output_path: str,
    *,
    cols: int = 3,
    rows: int = 3,
    color: tuple[int, int, int] = (200, 200, 200),
    line_width: int = 1,
) -> None:
    """Draw a grid of lines over an image.

    Args:
        cols: Number of column divisions. Default 3 (rule-of-thirds).
        rows: Number of row divisions. Default 3.
        color: RGB line color. Default light gray.
        line_width: Line thickness. Default 1.
    """
    from PIL import Image, ImageDraw

    img = Image.open(input_path).convert("RGB")
    w, h = img.size
    draw = ImageDraw.Draw(img)
    for i in range(1, cols):
        x = w * i // cols
        draw.rectangle([x, 0, x + line_width - 1, h], fill=color)
    for i in range(1, rows):
        y = h * i // rows
        draw.rectangle([0, y, w, y + line_width - 1], fill=color)
    img.save(output_path)


def image_color_map(
    input_path: str,
    output_path: str,
    *,
    colormap: str = "viridis",
) -> None:
    """Apply a colormap to a grayscale image.

    Args:
        colormap: Colormap name ('viridis', 'plasma', 'hot', 'cool', 'jet'). Default 'viridis'.
    """
    from PIL import Image
    import numpy as np

    # Built-in colormaps (no matplotlib dependency)
    _MAPS = {
        "hot": lambda t: np.stack([
            np.clip(t * 3, 0, 1),
            np.clip(t * 3 - 1, 0, 1),
            np.clip(t * 3 - 2, 0, 1),
        ], axis=-1),
        "cool": lambda t: np.stack([t, 1 - t, np.ones_like(t)], axis=-1),
        "viridis": lambda t: np.stack([
            np.interp(t, [0, 0.5, 1], [0.267, 0.128, 0.993]),
            np.interp(t, [0, 0.5, 1], [0.005, 0.566, 0.906]),
            np.interp(t, [0, 0.5, 1], [0.329, 0.551, 0.143]),
        ], axis=-1),
        "plasma": lambda t: np.stack([
            np.interp(t, [0, 0.5, 1], [0.050, 0.900, 0.940]),
            np.interp(t, [0, 0.5, 1], [0.030, 0.200, 0.975]),
            np.interp(t, [0, 0.5, 1], [0.527, 0.420, 0.131]),
        ], axis=-1),
        "jet": lambda t: np.stack([
            np.clip(1.5 - np.abs(4 * t - 3), 0, 1),
            np.clip(1.5 - np.abs(4 * t - 2), 0, 1),
            np.clip(1.5 - np.abs(4 * t - 1), 0, 1),
        ], axis=-1),
    }
    img = Image.open(input_path).convert("L")
    t = np.array(img, dtype=np.float32) / 255.0
    fn = _MAPS.get(colormap, _MAPS["viridis"])
    rgb = (fn(t) * 255).clip(0, 255).astype(np.uint8)
    Image.fromarray(rgb).save(output_path)


def image_frames_to_gif(
    frame_paths: list[str],
    output_path: str,
    *,
    duration_ms: int = 100,
    loop: int = 0,
) -> None:
    """Combine image frames into an animated GIF.

    Args:
        frame_paths: Ordered list of image file paths.
        duration_ms: Frame duration in milliseconds. Default 100.
        loop: GIF loop count (0 = infinite). Default 0.
    """
    from PIL import Image

    frames = [Image.open(p).convert("RGBA") for p in frame_paths]
    if not frames:
        raise ValueError("No frames provided")
    frames[0].save(
        output_path,
        save_all=True,
        append_images=frames[1:],
        duration=duration_ms,
        loop=loop,
    )


def image_gif_to_frames(
    input_path: str,
    output_dir: str,
    *,
    fmt: str = "frame_{:04d}.png",
) -> list[str]:
    """Extract frames from an animated GIF as PNG files.

    Args:
        output_dir: Directory to write frames.
        fmt: Filename format with index placeholder. Default 'frame_{:04d}.png'.

    Returns:
        List of output file paths.
    """
    import os as _os
    from PIL import Image

    _os.makedirs(output_dir, exist_ok=True)
    gif = Image.open(input_path)
    paths = []
    for i in range(getattr(gif, "n_frames", 1)):
        gif.seek(i)
        out = _os.path.join(output_dir, fmt.format(i))
        gif.convert("RGB").save(out)
        paths.append(out)
    return paths


def image_save_as(
    input_path: str,
    output_path: str,
    *,
    quality: int = 85,
) -> None:
    """Re-save an image in a different format with quality control.

    Format is inferred from output_path extension (jpg, png, webp, etc.).

    Args:
        quality: JPEG/WEBP quality 1–95. Ignored for lossless formats. Default 85.
    """
    from PIL import Image

    img = Image.open(input_path).convert("RGB")
    ext = output_path.rsplit(".", 1)[-1].lower()
    if ext in ("jpg", "jpeg"):
        img.save(output_path, "JPEG", quality=quality)
    elif ext == "webp":
        img.save(output_path, "WEBP", quality=quality)
    elif ext == "png":
        img.save(output_path, "PNG")
    else:
        img.save(output_path)


def image_compare(
    path_a: str,
    path_b: str,
) -> dict:
    """Compare two images and return PSNR and MSE metrics.

    Returns:
        Dict with keys: mse, psnr (dB). psnr=inf means identical.
    """
    from PIL import Image
    import numpy as np

    a = np.array(Image.open(path_a).convert("RGB"), dtype=np.float64)
    b = np.array(Image.open(path_b).convert("RGB").resize(
        (Image.open(path_a).width, Image.open(path_a).height), Image.LANCZOS
    ), dtype=np.float64)
    mse = float(np.mean((a - b) ** 2))
    if mse == 0:
        psnr = float("inf")
    else:
        psnr = float(10 * np.log10(255.0 ** 2 / mse))
    return {"mse": mse, "psnr": psnr}


def image_mean_color(input_path: str) -> tuple[int, int, int]:
    """Return the mean RGB color of an image.

    Returns:
        (R, G, B) tuple with integer values 0–255.
    """
    from PIL import Image
    import numpy as np

    arr = np.array(Image.open(input_path).convert("RGB"), dtype=np.float32)
    mean = arr.mean(axis=(0, 1))
    return (int(mean[0]), int(mean[1]), int(mean[2]))


def image_make_transparent(
    input_path: str,
    output_path: str,
    *,
    color: tuple[int, int, int] = (0, 255, 0),
    tolerance: int = 30,
) -> None:
    """Replace a color with transparency (image chroma key).

    Args:
        color: RGB color to make transparent. Default green (0, 255, 0).
        tolerance: Color distance tolerance 0–255. Default 30.
    """
    from PIL import Image
    import numpy as np

    img = Image.open(input_path).convert("RGBA")
    arr = np.array(img, dtype=np.int32)
    r, g, b = color
    dist = np.sqrt(
        (arr[..., 0] - r) ** 2 +
        (arr[..., 1] - g) ** 2 +
        (arr[..., 2] - b) ** 2
    )
    mask = dist <= tolerance
    arr[mask, 3] = 0
    Image.fromarray(arr.astype(np.uint8), "RGBA").save(output_path)


def image_sobel(
    input_path: str,
    output_path: str,
) -> None:
    """Apply Sobel edge detection to produce a gradient magnitude image."""
    from PIL import Image, ImageFilter
    import numpy as np

    img = Image.open(input_path).convert("L")
    arr = np.array(img, dtype=np.float32)

    # Sobel kernels
    kx = np.array([[-1, 0, 1], [-2, 0, 2], [-1, 0, 1]], dtype=np.float32)
    ky = np.array([[-1, -2, -1], [0, 0, 0], [1, 2, 1]], dtype=np.float32)

    from scipy.ndimage import convolve  # type: ignore
    try:
        gx = convolve(arr, kx)
        gy = convolve(arr, ky)
    except ImportError:
        # Manual convolution fallback
        h, w = arr.shape
        gx = np.zeros_like(arr)
        gy = np.zeros_like(arr)
        p = np.pad(arr, 1, mode="edge")
        for dy in range(3):
            for dx in range(3):
                gx += kx[dy, dx] * p[dy:dy+h, dx:dx+w]
                gy += ky[dy, dx] * p[dy:dy+h, dx:dx+w]
    mag = np.sqrt(gx**2 + gy**2)
    mx = mag.max(); mag = (mag / mx * 255 if mx > 0 else mag).clip(0, 255).astype(np.uint8)
    Image.fromarray(mag, "L").convert("RGB").save(output_path)


def image_laplacian(
    input_path: str,
    output_path: str,
) -> None:
    """Apply Laplacian edge detection filter."""
    from PIL import Image, ImageFilter

    img = Image.open(input_path).convert("L")
    edges = img.filter(ImageFilter.FIND_EDGES)
    edges.convert("RGB").save(output_path)


def image_canny(
    input_path: str,
    output_path: str,
    *,
    low_threshold: float = 50.0,
    high_threshold: float = 150.0,
) -> None:
    """Canny edge detection (numpy-based, no OpenCV required)."""
    import numpy as np
    from PIL import Image

    img = Image.open(input_path).convert("L")
    arr = np.array(img, dtype=np.float32)

    # Gaussian blur (simple 5x5)
    def gaussian_blur(a, sigma=1.4):
        k = np.array([[2,4,5,4,2],[4,9,12,9,4],[5,12,15,12,5],[4,9,12,9,4],[2,4,5,4,2]], dtype=np.float32)
        k /= k.sum()
        h, w = a.shape
        p = np.pad(a, 2, mode="edge")
        out = np.zeros_like(a)
        for dy in range(5):
            for dx in range(5):
                out += k[dy, dx] * p[dy:dy+h, dx:dx+w]
        return out

    blurred = gaussian_blur(arr)

    # Sobel gradients
    kx = np.array([[-1,0,1],[-2,0,2],[-1,0,1]], dtype=np.float32)
    ky = kx.T
    h, w = blurred.shape
    p = np.pad(blurred, 1, mode="edge")
    gx = np.zeros_like(blurred); gy = np.zeros_like(blurred)
    for dy in range(3):
        for dx in range(3):
            gx += kx[dy,dx]*p[dy:dy+h,dx:dx+w]
            gy += ky[dy,dx]*p[dy:dy+h,dx:dx+w]
    mag = np.hypot(gx, gy)
    angle = np.degrees(np.arctan2(gy, gx)) % 180

    # Non-maximum suppression
    nms = np.zeros_like(mag)
    for y in range(1, h-1):
        for x in range(1, w-1):
            a = angle[y, x]
            m = mag[y, x]
            if a < 22.5 or a >= 157.5:
                q, r = mag[y, x-1], mag[y, x+1]
            elif a < 67.5:
                q, r = mag[y-1, x+1], mag[y+1, x-1]
            elif a < 112.5:
                q, r = mag[y-1, x], mag[y+1, x]
            else:
                q, r = mag[y-1, x-1], mag[y+1, x+1]
            if m >= q and m >= r:
                nms[y, x] = m

    # Double threshold
    strong = nms >= high_threshold
    weak = (nms >= low_threshold) & ~strong

    # Hysteresis (simple BFS)
    from collections import deque
    out = np.zeros((h, w), dtype=np.uint8)
    out[strong] = 255
    queue = deque(zip(*np.where(strong)))
    while queue:
        y, x = queue.popleft()
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                ny, nx = y+dy, x+dx
                if 0 <= ny < h and 0 <= nx < w and weak[ny, nx] and out[ny, nx] == 0:
                    out[ny, nx] = 255
                    queue.append((ny, nx))

    Image.fromarray(out, "L").convert("RGB").save(output_path)


def image_bilateral_blur(
    input_path: str,
    output_path: str,
    *,
    radius: int = 5,
    sigma_color: float = 40.0,
) -> None:
    """Edge-preserving bilateral-style blur using numpy."""
    import numpy as np
    from PIL import Image

    img = Image.open(input_path).convert("RGB")
    arr = np.array(img, dtype=np.float32)
    h, w, c = arr.shape
    pad = radius
    p = np.pad(arr, ((pad, pad), (pad, pad), (0, 0)), mode="edge")

    # Spatial Gaussian kernel
    ys, xs = np.mgrid[-pad:pad+1, -pad:pad+1]
    spatial = np.exp(-(xs**2 + ys**2) / (2 * (radius/2)**2))

    out = np.zeros_like(arr)
    for dy in range(2*radius+1):
        for dx in range(2*radius+1):
            neighbor = p[dy:dy+h, dx:dx+w]
            # Color Gaussian weight per pixel
            diff = np.sum((neighbor - arr)**2, axis=2)
            color_w = np.exp(-diff / (2 * sigma_color**2))
            w_2d = spatial[dy, dx] * color_w
            out += w_2d[:, :, np.newaxis] * neighbor

    # Normalise
    norm = np.zeros((h, w), dtype=np.float32)
    for dy in range(2*radius+1):
        for dx in range(2*radius+1):
            neighbor = p[dy:dy+h, dx:dx+w]
            diff = np.sum((neighbor - arr)**2, axis=2)
            color_w = np.exp(-diff / (2 * sigma_color**2))
            norm += spatial[dy, dx] * color_w
    out /= norm[:, :, np.newaxis]
    Image.fromarray(out.clip(0, 255).astype(np.uint8), "RGB").save(output_path)


def image_morphology(
    input_path: str,
    output_path: str,
    *,
    operation: str = "dilate",
    kernel_size: int = 3,
) -> None:
    """Morphological operation: erode, dilate, open, or close (numpy-based)."""
    import numpy as np
    from PIL import Image

    img = Image.open(input_path).convert("L")
    arr = np.array(img, dtype=np.uint8)
    pad = kernel_size // 2

    def _dilate(a):
        h, w = a.shape
        p = np.pad(a, pad, mode="edge")
        out = np.zeros_like(a)
        for dy in range(kernel_size):
            for dx in range(kernel_size):
                out = np.maximum(out, p[dy:dy+h, dx:dx+w])
        return out

    def _erode(a):
        h, w = a.shape
        p = np.pad(a, pad, mode="edge")
        out = np.full_like(a, 255)
        for dy in range(kernel_size):
            for dx in range(kernel_size):
                out = np.minimum(out, p[dy:dy+h, dx:dx+w])
        return out

    if operation == "dilate":
        result = _dilate(arr)
    elif operation == "erode":
        result = _erode(arr)
    elif operation == "open":
        result = _dilate(_erode(arr))
    elif operation == "close":
        result = _erode(_dilate(arr))
    else:
        raise ValueError(f"Unknown operation: {operation}")

    Image.fromarray(result, "L").convert("RGB").save(output_path)


def image_threshold(
    input_path: str,
    output_path: str,
    *,
    threshold: int = -1,
) -> None:
    """Threshold to binary. If threshold < 0, use Otsu's method."""
    import numpy as np
    from PIL import Image

    img = Image.open(input_path).convert("L")
    arr = np.array(img, dtype=np.uint8)

    if threshold < 0:
        # Otsu's method
        hist, bins = np.histogram(arr.flatten(), 256, [0, 256])
        hist = hist.astype(np.float64)
        total = arr.size
        sum_all = np.dot(np.arange(256), hist)
        sum_b = 0.0; w_b = 0.0; max_var = 0.0; thresh = 0
        for t in range(256):
            w_b += hist[t]
            if w_b == 0:
                continue
            w_f = total - w_b
            if w_f == 0:
                break
            sum_b += t * hist[t]
            mb = sum_b / w_b
            mf = (sum_all - sum_b) / w_f
            var = w_b * w_f * (mb - mf) ** 2
            if var > max_var:
                max_var = var; thresh = t
        threshold = thresh

    binary = ((arr >= threshold) * 255).astype(np.uint8)
    Image.fromarray(binary, "L").convert("RGB").save(output_path)


def image_warp_fisheye(
    input_path: str,
    output_path: str,
    *,
    strength: float = 0.5,
) -> None:
    """Apply barrel/fisheye lens distortion. strength > 0 = barrel, < 0 = pincushion."""
    import numpy as np
    from PIL import Image

    img = Image.open(input_path).convert("RGB")
    arr = np.array(img, dtype=np.uint8)
    h, w = arr.shape[:2]
    cx, cy = w / 2.0, h / 2.0
    max_r = np.sqrt(cx**2 + cy**2)

    xs = np.arange(w, dtype=np.float32)
    ys = np.arange(h, dtype=np.float32)
    xg, yg = np.meshgrid(xs, ys)
    dx = (xg - cx) / max_r
    dy = (yg - cy) / max_r
    r = np.sqrt(dx**2 + dy**2)
    r_src = r * (1 + strength * r**2)
    xsrc = (r_src * dx / np.where(r == 0, 1, r) * max_r + cx).clip(0, w - 1)
    ysrc = (r_src * dy / np.where(r == 0, 1, r) * max_r + cy).clip(0, h - 1)

    xi = xsrc.astype(np.int32)
    yi = ysrc.astype(np.int32)
    out = arr[yi, xi]
    Image.fromarray(out, "RGB").save(output_path)


def image_vignette(
    input_path: str,
    output_path: str,
    *,
    strength: float = 0.5,
    radius: float = 0.8,
) -> None:
    """Apply radial vignette darkening effect. strength in [0,1], radius in [0,1]."""
    import numpy as np
    from PIL import Image

    img = Image.open(input_path).convert("RGB")
    arr = np.array(img, dtype=np.float32)
    h, w = arr.shape[:2]
    xs = np.linspace(-1, 1, w)
    ys = np.linspace(-1, 1, h)
    xg, yg = np.meshgrid(xs, ys)
    dist = np.sqrt(xg**2 + yg**2)
    mask = 1 - strength * np.clip((dist - radius) / (1 - radius + 1e-6), 0, 1)
    arr *= mask[:, :, np.newaxis]
    Image.fromarray(arr.clip(0, 255).astype(np.uint8), "RGB").save(output_path)


def image_chromatic_aberration(
    input_path: str,
    output_path: str,
    *,
    shift: int = 4,
) -> None:
    """Simulate chromatic aberration by shifting R and B channels."""
    import numpy as np
    from PIL import Image

    img = Image.open(input_path).convert("RGB")
    arr = np.array(img, dtype=np.uint8)
    h, w = arr.shape[:2]
    out = arr.copy()
    # Shift R channel left/up
    if shift > 0:
        out[:, :w-shift, 0] = arr[:, shift:, 0]
        out[:, w-shift:, 0] = arr[:, w-1:w, 0]
        # Shift B channel right/down
        out[:, shift:, 2] = arr[:, :w-shift, 2]
        out[:, :shift, 2] = arr[:, :1, 2]
    Image.fromarray(out, "RGB").save(output_path)


def image_focus_region(
    input_path: str,
    output_path: str,
    *,
    blur_radius: int = 15,
    focus_radius: float = 0.35,
) -> None:
    """Sharp center with blurred periphery (simulated focus/tilt-shift effect)."""
    import numpy as np
    from PIL import Image, ImageFilter

    img = Image.open(input_path).convert("RGB")
    blurred = img.filter(ImageFilter.GaussianBlur(radius=blur_radius))
    arr = np.array(img, dtype=np.float32)
    barr = np.array(blurred, dtype=np.float32)
    h, w = arr.shape[:2]

    xs = np.linspace(-1, 1, w)
    ys = np.linspace(-1, 1, h)
    xg, yg = np.meshgrid(xs, ys)
    dist = np.sqrt(xg**2 + yg**2)
    # Smooth transition: 0 = sharp, 1 = fully blurred
    blend = np.clip((dist - focus_radius) / (1 - focus_radius + 1e-6), 0, 1)
    result = arr * (1 - blend[:, :, np.newaxis]) + barr * blend[:, :, np.newaxis]
    Image.fromarray(result.clip(0, 255).astype(np.uint8), "RGB").save(output_path)


def image_anaglyph(
    input_path: str,
    output_path: str,
    *,
    shift: int = 6,
) -> None:
    """Create red-cyan anaglyph 3D effect by horizontally shifting channels."""
    import numpy as np
    from PIL import Image

    img = Image.open(input_path).convert("RGB")
    arr = np.array(img, dtype=np.uint8)
    h, w = arr.shape[:2]
    out = np.zeros_like(arr)
    # Red channel shifted left
    out[:, :w-shift, 0] = arr[:, shift:, 0]
    # Cyan (G+B) channels shifted right
    out[:, shift:, 1] = arr[:, :w-shift, 1]
    out[:, shift:, 2] = arr[:, :w-shift, 2]
    Image.fromarray(out, "RGB").save(output_path)


def image_pixelate_mosaic(
    input_path: str,
    output_path: str,
    *,
    block_size: int = 16,
) -> None:
    """Mosaic pixelate: average color within each block."""
    import numpy as np
    from PIL import Image

    img = Image.open(input_path).convert("RGB")
    arr = np.array(img, dtype=np.float32)
    h, w = arr.shape[:2]
    out = arr.copy()
    for y in range(0, h, block_size):
        for x in range(0, w, block_size):
            block = arr[y:y+block_size, x:x+block_size]
            mean = block.mean(axis=(0, 1))
            out[y:y+block_size, x:x+block_size] = mean
    Image.fromarray(out.clip(0, 255).astype(np.uint8), "RGB").save(output_path)


def image_pencil_sketch(
    input_path: str,
    output_path: str,
    *,
    blur_radius: int = 21,
    intensity: float = 1.5,
) -> None:
    """Pencil sketch effect via invert+blur dodge blend."""
    import numpy as np
    from PIL import Image, ImageFilter

    img = Image.open(input_path).convert("L")
    arr = np.array(img, dtype=np.float32)
    inv = 255.0 - arr
    inv_img = Image.fromarray(inv.clip(0, 255).astype(np.uint8))
    blurred_inv = np.array(inv_img.filter(ImageFilter.GaussianBlur(radius=blur_radius)), dtype=np.float32)
    # Dodge blend: result = base / (1 - blend/255) clamped
    denom = np.clip(255.0 - blurred_inv, 1, 255)
    dodge = np.clip(arr * 255.0 / denom * intensity, 0, 255)
    Image.fromarray(dodge.astype(np.uint8), "L").convert("RGB").save(output_path)


def image_watercolor(
    input_path: str,
    output_path: str,
    *,
    smoothing_passes: int = 3,
    edge_strength: float = 0.4,
) -> None:
    """Watercolor painting effect: smooth + edge overlay."""
    import numpy as np
    from PIL import Image, ImageFilter

    img = Image.open(input_path).convert("RGB")
    # Multiple passes of median-like smoothing
    smooth = img
    for _ in range(smoothing_passes):
        smooth = smooth.filter(ImageFilter.MedianFilter(size=5))
    # Edge mask from original
    gray = img.convert("L")
    edges = np.array(gray.filter(ImageFilter.FIND_EDGES), dtype=np.float32)
    edges = (edges / (edges.max() + 1e-6) * 255).astype(np.uint8)
    edge_mask = 255 - edges  # dark edges
    arr_smooth = np.array(smooth, dtype=np.float32)
    # Slightly desaturate
    from PIL import ImageEnhance
    smooth_desat = ImageEnhance.Color(smooth).enhance(0.8)
    arr_sd = np.array(smooth_desat, dtype=np.float32)
    edge_f = edge_mask.astype(np.float32)[:, :, np.newaxis] / 255.0
    result = arr_sd * (1 - edge_strength * (1 - edge_f))
    Image.fromarray(result.clip(0, 255).astype(np.uint8), "RGB").save(output_path)


def image_stained_glass(
    input_path: str,
    output_path: str,
    *,
    num_cells: int = 200,
    edge_width: int = 2,
) -> None:
    """Stained glass effect using random Voronoi segmentation."""
    import numpy as np
    from PIL import Image

    img = Image.open(input_path).convert("RGB")
    arr = np.array(img, dtype=np.uint8)
    h, w = arr.shape[:2]
    rng = np.random.default_rng(42)
    # Random seed points
    seeds_y = rng.integers(0, h, num_cells)
    seeds_x = rng.integers(0, w, num_cells)
    # Assign each pixel to nearest seed (vectorized)
    ys = np.arange(h)[:, np.newaxis]
    xs = np.arange(w)[np.newaxis, :]
    out = np.zeros_like(arr)
    # Process in chunks to avoid large memory
    # Build label map
    label = np.zeros((h, w), dtype=np.int32)
    dist_min = np.full((h, w), np.inf)
    for i, (sy, sx) in enumerate(zip(seeds_y, seeds_x)):
        d = (ys - sy)**2 + (xs - sx)**2
        mask = d < dist_min
        dist_min[mask] = d[mask]
        label[mask] = i
    # Fill each cell with mean color
    for i in range(num_cells):
        m = label == i
        if m.any():
            mean_color = arr[m].mean(axis=0).astype(np.uint8)
            out[m] = mean_color
    # Draw edges: where label differs from neighbors
    edge = np.zeros((h, w), dtype=bool)
    edge[:-1, :] |= (label[:-1, :] != label[1:, :])
    edge[:, :-1] |= (label[:, :-1] != label[:, 1:])
    if edge_width > 1:
        from PIL import ImageFilter
        edge_img = Image.fromarray(edge.astype(np.uint8) * 255)
        edge_img = edge_img.filter(ImageFilter.MaxFilter(edge_width * 2 + 1))
        edge = np.array(edge_img) > 0
    out[edge] = 0
    Image.fromarray(out, "RGB").save(output_path)


def image_ascii_art(
    input_path: str,
    output_path: str,
    *,
    cols: int = 80,
    font_size: int = 10,
) -> None:
    """Convert image to ASCII art rendered back as an image."""
    import numpy as np
    from PIL import Image, ImageDraw, ImageFont

    chars = "@#S%?*+;:,. "
    img = Image.open(input_path).convert("L")
    # Resize to cols x rows
    aspect = img.height / img.width
    rows = max(1, int(cols * aspect * 0.5))  # chars are ~2x tall
    small = img.resize((cols, rows), Image.LANCZOS)
    arr = np.array(small)
    # Map brightness to chars
    indices = (arr / 255 * (len(chars) - 1)).astype(int)
    lines = ["".join(chars[indices[r, c]] for c in range(cols)) for r in range(rows)]
    # Render to image
    char_w, char_h = font_size, font_size * 2
    out_img = Image.new("RGB", (cols * char_w, rows * char_h), (255, 255, 255))
    draw = ImageDraw.Draw(out_img)
    try:
        font = ImageFont.load_default(size=font_size)
    except TypeError:
        font = ImageFont.load_default()
    for r, line in enumerate(lines):
        draw.text((0, r * char_h), line, fill=(0, 0, 0), font=font)
    out_img.save(output_path)


def image_noise_reduction(
    input_path: str,
    output_path: str,
    *,
    patch_size: int = 5,
    search_size: int = 11,
    h: float = 10.0,
) -> None:
    """Non-local means-inspired denoising (simplified patch averaging)."""
    import numpy as np
    from PIL import Image

    img = Image.open(input_path).convert("RGB")
    arr = np.array(img, dtype=np.float32)
    ih, iw = arr.shape[:2]
    half_p = patch_size // 2
    half_s = search_size // 2
    padded = np.pad(arr, ((half_s + half_p, half_s + half_p),
                          (half_s + half_p, half_s + half_p), (0, 0)), mode="reflect")
    out = np.zeros_like(arr)
    weight_sum = np.zeros((ih, iw), dtype=np.float32)

    for dy in range(-half_s, half_s + 1):
        for dx in range(-half_s, half_s + 1):
            # Shift
            shifted = padded[half_s + dy:half_s + dy + ih + 2*half_p,
                             half_s + dx:half_s + dx + iw + 2*half_p]
            # Original padded region
            orig = padded[half_s:half_s + ih + 2*half_p,
                          half_s:half_s + iw + 2*half_p]
            # Patch distance (box filter approximation)
            diff = ((orig - shifted) ** 2).sum(axis=2)
            # Average over patch window
            from PIL import ImageFilter
            diff_img = Image.fromarray(diff.astype(np.float32))
            # Box blur via numpy
            ky = np.ones((patch_size, 1), dtype=np.float32) / patch_size
            kx = np.ones((1, patch_size), dtype=np.float32) / patch_size
            diff_b = diff.copy()
            for _ in range(1):
                ph, pw = diff_b.shape
                pp = np.pad(diff_b, half_p, mode="reflect")
                d2 = np.zeros_like(diff_b)
                for yy in range(patch_size):
                    for xx in range(patch_size):
                        d2 += pp[yy:yy+ph, xx:xx+pw] / (patch_size * patch_size)
                diff_b = d2
            patch_dist = diff_b[half_p:half_p+ih, half_p:half_p+iw]
            w = np.exp(-patch_dist / (h ** 2))
            weight_sum += w
            s_crop = shifted[half_p:half_p+ih, half_p:half_p+iw]
            out += w[:, :, np.newaxis] * s_crop

    out /= np.maximum(weight_sum[:, :, np.newaxis], 1e-6)
    Image.fromarray(out.clip(0, 255).astype(np.uint8), "RGB").save(output_path)


def image_hue_shift(
    input_path: str,
    output_path: str,
    *,
    degrees: float = 90.0,
) -> None:
    """Shift image hue by degrees in HSV space."""
    import numpy as np
    from PIL import Image

    img = Image.open(input_path).convert("RGB")
    arr = np.array(img, dtype=np.float32) / 255.0
    # RGB to HSV
    r, g, b = arr[:, :, 0], arr[:, :, 1], arr[:, :, 2]
    cmax = np.maximum(np.maximum(r, g), b)
    cmin = np.minimum(np.minimum(r, g), b)
    delta = cmax - cmin
    # Hue
    h = np.zeros_like(r)
    m = delta > 0
    mr = m & (cmax == r); mg = m & (cmax == g); mb = m & (cmax == b)
    h[mr] = ((g[mr] - b[mr]) / delta[mr]) % 6
    h[mg] = (b[mg] - r[mg]) / delta[mg] + 2
    h[mb] = (r[mb] - g[mb]) / delta[mb] + 4
    h = h / 6.0  # [0,1]
    s = np.where(cmax > 0, delta / cmax, 0)
    v = cmax
    # Shift hue
    h = (h + degrees / 360.0) % 1.0
    # HSV to RGB
    hi = (h * 6).astype(int) % 6
    f = h * 6 - np.floor(h * 6)
    p = v * (1 - s); q = v * (1 - f * s); t = v * (1 - (1 - f) * s)
    out = np.zeros_like(arr)
    for i, (rr, gg, bb) in enumerate([(v,t,p),(q,v,p),(p,v,t),(p,q,v),(t,p,v),(v,p,q)]):
        m2 = hi == i
        out[:, :, 0][m2] = rr[m2]; out[:, :, 1][m2] = gg[m2]; out[:, :, 2][m2] = bb[m2]
    Image.fromarray((out * 255).clip(0, 255).astype(np.uint8), "RGB").save(output_path)


def image_split_tone(
    input_path: str,
    output_path: str,
    *,
    shadow_color: tuple = (0, 0, 255),
    highlight_color: tuple = (255, 200, 0),
    intensity: float = 0.3,
) -> None:
    """Split toning: tint shadows and highlights with different colors."""
    import numpy as np
    from PIL import Image

    img = Image.open(input_path).convert("RGB")
    arr = np.array(img, dtype=np.float32) / 255.0
    luminance = 0.299 * arr[:,:,0] + 0.587 * arr[:,:,1] + 0.114 * arr[:,:,2]
    # Shadow mask: dark areas
    shadow_mask = (1.0 - luminance)[:, :, np.newaxis]
    # Highlight mask: bright areas
    highlight_mask = luminance[:, :, np.newaxis]
    sc = np.array(shadow_color, dtype=np.float32) / 255.0
    hc = np.array(highlight_color, dtype=np.float32) / 255.0
    tinted = arr + intensity * (shadow_mask * sc + highlight_mask * hc - arr * intensity * 0.5)
    Image.fromarray((tinted.clip(0, 1) * 255).astype(np.uint8), "RGB").save(output_path)


def image_color_burn(
    input_path: str,
    blend_path: str,
    output_path: str,
    *,
    opacity: float = 1.0,
) -> None:
    """Color burn blend: darken base using blend layer."""
    import numpy as np
    from PIL import Image

    base = np.array(Image.open(input_path).convert("RGB"), dtype=np.float32) / 255.0
    blend_img = Image.open(blend_path).convert("RGB").resize(
        (base.shape[1], base.shape[0]), Image.LANCZOS)
    blend = np.array(blend_img, dtype=np.float32) / 255.0
    # Color burn: 1 - (1 - base) / blend
    with np.errstate(divide="ignore", invalid="ignore"):
        result = 1.0 - (1.0 - base) / np.where(blend == 0, 1e-6, blend)
    result = np.clip(result, 0, 1)
    # Apply opacity
    out = base * (1 - opacity) + result * opacity
    Image.fromarray((out * 255).astype(np.uint8), "RGB").save(output_path)


def image_dodge(
    input_path: str,
    blend_path: str,
    output_path: str,
    *,
    opacity: float = 1.0,
) -> None:
    """Color dodge blend: lighten base using blend layer."""
    import numpy as np
    from PIL import Image

    base = np.array(Image.open(input_path).convert("RGB"), dtype=np.float32) / 255.0
    blend_img = Image.open(blend_path).convert("RGB").resize(
        (base.shape[1], base.shape[0]), Image.LANCZOS)
    blend = np.array(blend_img, dtype=np.float32) / 255.0
    with np.errstate(divide="ignore", invalid="ignore"):
        result = base / np.where(1.0 - blend < 1e-6, 1e-6, 1.0 - blend)
    result = np.clip(result, 0, 1)
    out = base * (1 - opacity) + result * opacity
    Image.fromarray((out * 255).astype(np.uint8), "RGB").save(output_path)


def image_map_to_palette(
    input_path: str,
    output_path: str,
    *,
    num_colors: int = 16,
) -> None:
    """Quantize image to N colors using PIL's built-in quantize."""
    from PIL import Image

    img = Image.open(input_path).convert("RGB")
    quantized = img.quantize(colors=num_colors, method=Image.Quantize.MEDIANCUT)
    quantized.convert("RGB").save(output_path)


def image_lens_flare(
    input_path: str,
    output_path: str,
    *,
    cx: int = None,
    cy: int = None,
    intensity: float = 0.8,
    num_streaks: int = 8,
) -> None:
    """Simulate lens flare: bright halo + streaks from a light source point."""
    import numpy as np
    from PIL import Image

    img = Image.open(input_path).convert("RGB")
    arr = np.array(img, dtype=np.float32)
    h, w = arr.shape[:2]
    if cx is None: cx = w // 2
    if cy is None: cy = h // 4

    flare = np.zeros((h, w), dtype=np.float32)
    ys, xs = np.mgrid[0:h, 0:w]
    # Central halo
    dist = np.sqrt((xs - cx)**2 + (ys - cy)**2)
    halo_r = min(w, h) * 0.12
    flare += intensity * np.exp(-dist**2 / (2 * halo_r**2))
    # Streaks
    for i in range(num_streaks):
        angle = i * np.pi / num_streaks
        dx = np.cos(angle); dy = np.sin(angle)
        proj = (xs - cx) * dx + (ys - cy) * dy
        perp = abs((xs - cx) * (-dy) + (ys - cy) * dx)
        streak_len = min(w, h) * 0.4
        streak_w = 2.0
        streak = (np.exp(-perp**2 / (2 * streak_w**2)) *
                  np.exp(-proj**2 / (2 * (streak_len * 0.3)**2)) * intensity * 0.4)
        flare += streak

    flare = np.clip(flare, 0, 1)[:, :, np.newaxis]
    result = arr / 255.0 + flare * np.array([1.0, 0.9, 0.7])
    Image.fromarray((result.clip(0, 1) * 255).astype(np.uint8), "RGB").save(output_path)


def image_duotone(
    input_path: str,
    output_path: str,
    *,
    shadow_color: tuple = (20, 10, 80),
    highlight_color: tuple = (255, 230, 100),
) -> None:
    """Duotone: map grayscale linearly between two colors."""
    import numpy as np
    from PIL import Image

    img = Image.open(input_path).convert("L")
    arr = np.array(img, dtype=np.float32) / 255.0  # [0,1]
    sc = np.array(shadow_color, dtype=np.float32) / 255.0
    hc = np.array(highlight_color, dtype=np.float32) / 255.0
    # Interpolate per pixel
    result = sc[np.newaxis, np.newaxis, :] * (1 - arr[:, :, np.newaxis]) + hc[np.newaxis, np.newaxis, :] * arr[:, :, np.newaxis]
    Image.fromarray((result * 255).clip(0, 255).astype(np.uint8), "RGB").save(output_path)


def image_pixelate_faces(
    input_path: str,
    output_path: str,
    *,
    block_size: int = 12,
) -> None:
    """Detect skin-tone face regions and pixelate them for privacy."""
    import numpy as np
    from PIL import Image

    img = Image.open(input_path).convert("RGB")
    arr = np.array(img, dtype=np.uint8)
    h, w = arr.shape[:2]
    r, g, b = arr[:,:,0].astype(float), arr[:,:,1].astype(float), arr[:,:,2].astype(float)
    # Skin tone mask (simple heuristic)
    skin = (r > 90) & (g > 40) & (b > 20) & (r > g) & (r > b) & ((r - g) > 10)
    out = arr.copy()
    # Find bounding box of skin region
    rows = np.any(skin, axis=1); cols = np.any(skin, axis=0)
    if rows.any() and cols.any():
        rmin, rmax = np.where(rows)[0][[0, -1]]
        cmin, cmax = np.where(cols)[0][[0, -1]]
        # Pixelate the bounding box
        for y in range(rmin, rmax + 1, block_size):
            for x in range(cmin, cmax + 1, block_size):
                patch = arr[y:y+block_size, x:x+block_size]
                out[y:y+block_size, x:x+block_size] = patch.mean(axis=(0,1)).astype(np.uint8)
    Image.fromarray(out, "RGB").save(output_path)


def image_simulate_print(
    input_path: str,
    output_path: str,
    *,
    dot_size: int = 4,
    angle: float = 45.0,
) -> None:
    """Simulate print halftone dots using periodic pattern overlay."""
    import numpy as np
    from PIL import Image

    img = Image.open(input_path).convert("L")
    arr = np.array(img, dtype=np.float32) / 255.0
    h, w = arr.shape
    ys, xs = np.mgrid[0:h, 0:w]
    # Rotate coordinates for angled screen
    rad = np.radians(angle)
    xr = xs * np.cos(rad) + ys * np.sin(rad)
    yr = -xs * np.sin(rad) + ys * np.cos(rad)
    # Dot pattern: sine wave grid
    dot = (np.cos(2 * np.pi * xr / dot_size) * np.cos(2 * np.pi * yr / dot_size) + 1) / 2
    # Threshold: pixel is black if luminance < dot pattern
    result = (arr > dot).astype(np.uint8) * 255
    Image.fromarray(result, "L").convert("RGB").save(output_path)


def image_glitch_datamosh(
    input_path: str,
    output_path: str,
    *,
    intensity: float = 0.05,
    seed: int = 42,
) -> None:
    """Data-mosh glitch: randomly corrupt pixel row blocks."""
    import numpy as np
    from PIL import Image

    img = Image.open(input_path).convert("RGB")
    arr = np.array(img, dtype=np.uint8).copy()
    h, w = arr.shape[:2]
    rng = np.random.default_rng(seed)
    num_glitches = max(1, int(h * intensity))
    for _ in range(num_glitches):
        # Pick a source row and destination row, shift a band
        src = rng.integers(0, h)
        dst = rng.integers(0, h)
        blen = rng.integers(1, max(2, h // 10))
        shift = rng.integers(-w // 4, w // 4)
        for dy in range(blen):
            sr, dr = (src + dy) % h, (dst + dy) % h
            row = arr[sr].copy()
            if shift > 0:
                arr[dr, shift:] = row[:w - shift]
                arr[dr, :shift] = row[w - shift:]
            elif shift < 0:
                s = -shift
                arr[dr, :w - s] = row[s:]
                arr[dr, w - s:] = row[:s]
    Image.fromarray(arr, "RGB").save(output_path)


def image_cartoon_cel(
    input_path: str,
    output_path: str,
    *,
    num_colors: int = 6,
    edge_threshold: float = 30.0,
) -> None:
    """Cel-shading cartoon: quantize colors + bold black edge overlay."""
    import numpy as np
    from PIL import Image, ImageFilter

    img = Image.open(input_path).convert("RGB")
    # Quantize to few colors
    quantized = img.quantize(colors=num_colors).convert("RGB")
    # Edge detection on grayscale
    gray = img.convert("L")
    edges = np.array(gray.filter(ImageFilter.FIND_EDGES), dtype=np.float32)
    edge_mask = edges > edge_threshold
    arr = np.array(quantized, dtype=np.uint8)
    arr[edge_mask] = 0  # black edges
    Image.fromarray(arr, "RGB").save(output_path)


def image_bump_map(
    input_path: str,
    output_path: str,
    *,
    light_dir: tuple = (1.0, -1.0, 2.0),
    strength: float = 3.0,
) -> None:
    """Apply bump-map lighting: use luminance as height map for directional lighting."""
    import numpy as np
    from PIL import Image

    img = Image.open(input_path).convert("RGB")
    arr = np.array(img, dtype=np.float32) / 255.0
    gray = 0.299 * arr[:,:,0] + 0.587 * arr[:,:,1] + 0.114 * arr[:,:,2]
    h, w = gray.shape
    # Compute surface normals from height gradients
    pad = np.pad(gray, 1, mode="edge")
    dzdx = (pad[1:-1, 2:] - pad[1:-1, :-2]) * strength
    dzdy = (pad[2:, 1:-1] - pad[:-2, 1:-1]) * strength
    # Normal vector: (-dzdx, -dzdy, 1), normalized
    nz = np.ones((h, w))
    norm = np.sqrt(dzdx**2 + dzdy**2 + nz**2)
    nx = -dzdx / norm; ny = -dzdy / norm; nz = nz / norm
    # Normalize light direction
    lx, ly, lz = light_dir
    ll = (lx**2 + ly**2 + lz**2) ** 0.5
    lx, ly, lz = lx/ll, ly/ll, lz/ll
    # Diffuse lighting
    diffuse = np.clip(nx * lx + ny * ly + nz * lz, 0, 1)
    result = (arr * diffuse[:, :, np.newaxis]).clip(0, 1)
    Image.fromarray((result * 255).astype(np.uint8), "RGB").save(output_path)


def image_color_quantize_dither(
    input_path: str,
    output_path: str,
    *,
    num_colors: int = 8,
) -> None:
    """Quantize to N colors with Floyd-Steinberg dithering via PIL."""
    from PIL import Image

    img = Image.open(input_path).convert("RGB")
    # PIL's quantize with dithering
    quantized = img.quantize(colors=num_colors, dither=Image.Dither.FLOYDSTEINBERG)
    quantized.convert("RGB").save(output_path)


def image_cross_hatch(
    input_path: str,
    output_path: str,
    *,
    line_spacing: int = 6,
    num_directions: int = 2,
) -> None:
    """Cross-hatch effect: diagonal lines with density from luminance."""
    import numpy as np
    from PIL import Image

    img = Image.open(input_path).convert("L")
    arr = np.array(img, dtype=np.float32) / 255.0
    h, w = arr.shape
    canvas = np.ones((h, w), dtype=np.float32)

    angles = [45, 135, 90, 0][:num_directions]
    for angle in angles:
        rad = np.radians(angle)
        ys, xs = np.mgrid[0:h, 0:w]
        proj = xs * np.cos(rad) + ys * np.sin(rad)
        # Line pattern
        line = (proj % line_spacing) < 1.0
        # Only draw lines where image is dark enough
        threshold = 1.0 - (1.0 / num_directions)
        mask = (arr < threshold) & line
        canvas[mask] = 0.0

    result = (canvas * 255).astype(np.uint8)
    Image.fromarray(result, "L").convert("RGB").save(output_path)


def image_soft_light(
    input_path: str,
    blend_path: str,
    output_path: str,
    *,
    opacity: float = 1.0,
) -> None:
    """Soft light blend mode: subtle brightening/darkening."""
    import numpy as np
    from PIL import Image

    base = np.array(Image.open(input_path).convert("RGB"), dtype=np.float32) / 255.0
    blend_img = Image.open(blend_path).convert("RGB").resize(
        (base.shape[1], base.shape[0]), Image.LANCZOS)
    blend = np.array(blend_img, dtype=np.float32) / 255.0
    # Pegtop soft light formula
    result = (1 - 2*blend) * base**2 + 2*blend*base
    result = np.clip(result, 0, 1)
    out = base * (1 - opacity) + result * opacity
    Image.fromarray((out * 255).astype(np.uint8), "RGB").save(output_path)


def image_double_exposure(input_path: "str", blend_path: "str", output_path: "str", *, opacity: "float" = 0.5) -> "None":
    """Blend two images with screen mode to simulate double exposure."""
    from PIL import Image
    import numpy as np
    base = np.array(Image.open(input_path).convert("RGB")).astype(np.float32) / 255.0
    overlay = Image.open(blend_path).convert("RGB").resize(
        (base.shape[1], base.shape[0]), Image.LANCZOS)
    ov = np.array(overlay).astype(np.float32) / 255.0
    # Screen blend: 1 - (1-a)*(1-b)
    screen = 1.0 - (1.0 - base) * (1.0 - ov)
    result = base * (1.0 - opacity) + screen * opacity
    result = (result.clip(0, 1) * 255).astype(np.uint8)
    Image.fromarray(result).save(output_path)


def image_bokeh_blur(input_path: "str", output_path: "str", *, radius: "int" = 15) -> "None":
    """Simulate bokeh (disk) blur using repeated box blur approximation."""
    from PIL import Image, ImageFilter
    img = Image.open(input_path).convert("RGB")
    # Approximate disk blur with multiple box blurs (central limit theorem)
    for _ in range(3):
        img = img.filter(ImageFilter.BoxBlur(radius // 2))
    img.save(output_path)


def image_fog_effect(input_path: "str", output_path: "str", *, intensity: "float" = 0.4) -> "None":
    """Blend white radial gradient over image to simulate fog/haze."""
    from PIL import Image
    import numpy as np
    img = Image.open(input_path).convert("RGB")
    w, h = img.size
    arr = np.array(img).astype(np.float32) / 255.0
    # Radial gradient: stronger fog at edges (or centre — use centre fade)
    xs = np.linspace(-1, 1, w)
    ys = np.linspace(-1, 1, h)
    xx, yy = np.meshgrid(xs, ys)
    dist = np.clip(np.sqrt(xx**2 + yy**2), 0, 1)[:, :, np.newaxis]
    fog_alpha = dist * intensity
    fog_alpha = np.clip(fog_alpha, 0, 1)
    result = arr * (1.0 - fog_alpha) + fog_alpha  # blend with white
    result = (result.clip(0, 1) * 255).astype(np.uint8)
    Image.fromarray(result).save(output_path)


def image_infrared(input_path: "str", output_path: "str", *, boost: "float" = 1.3) -> "None":
    """Simulate infrared photography: swap R/G channels, desaturate, boost contrast."""
    from PIL import Image, ImageEnhance
    import numpy as np
    img = Image.open(input_path).convert("RGB")
    arr = np.array(img).astype(np.float32)
    # Swap red and green channels
    r, g, b = arr[:, :, 0].copy(), arr[:, :, 1].copy(), arr[:, :, 2].copy()
    arr[:, :, 0] = g
    arr[:, :, 1] = r
    arr[:, :, 2] = b * 0.5  # reduce blue
    arr = np.clip(arr, 0, 255).astype(np.uint8)
    result = Image.fromarray(arr)
    # Convert to grayscale then back for desaturated look
    gray = result.convert("L")
    result = Image.merge("RGB", [gray, gray, gray])
    # Boost contrast
    result = ImageEnhance.Contrast(result).enhance(boost)
    result.save(output_path)


def image_neon_glow(input_path: "str", output_path: "str", *, blur_radius: "int" = 3, brightness: "float" = 2.0) -> "None":
    """Detect edges and render as bright neon on dark background."""
    from PIL import Image, ImageFilter, ImageEnhance
    import numpy as np
    img = Image.open(input_path).convert("RGB")
    # Find edges
    edges = img.filter(ImageFilter.FIND_EDGES)
    # Blur slightly for glow
    glow = edges.filter(ImageFilter.GaussianBlur(radius=blur_radius))
    # Boost brightness
    glow = ImageEnhance.Brightness(glow).enhance(brightness)
    # Compose on black background
    bg = Image.new("RGB", img.size, (0, 0, 0))
    bg.paste(glow, mask=glow.convert("L"))
    bg.save(output_path)


def image_mirror_quad(input_path: "str", output_path: "str") -> "None":
    """Mirror top-left quadrant into all four quadrants."""
    from PIL import Image
    img = Image.open(input_path).convert("RGB")
    w, h = img.size
    hw, hh = w // 2, h // 2
    tl = img.crop((0, 0, hw, hh))
    tr = tl.transpose(Image.FLIP_LEFT_RIGHT)
    bl = tl.transpose(Image.FLIP_TOP_BOTTOM)
    br = tl.transpose(Image.ROTATE_180)
    result = Image.new("RGB", (w, h))
    result.paste(tl, (0, 0))
    result.paste(tr, (hw, 0))
    result.paste(bl, (0, hh))
    result.paste(br, (hw, hh))
    result.save(output_path)


def image_color_dodge(input_path: "str", blend_path: "str", output_path: "str", *, opacity: "float" = 1.0) -> "None":
    """Blend two images with color dodge mode: base / (1 - overlay)."""
    from PIL import Image
    import numpy as np
    base = np.array(Image.open(input_path).convert("RGB")).astype(np.float32) / 255.0
    ov_img = Image.open(blend_path).convert("RGB").resize(
        (base.shape[1], base.shape[0]), Image.LANCZOS)
    ov = np.array(ov_img).astype(np.float32) / 255.0
    denom = np.clip(1.0 - ov, 1e-6, 1.0)
    dodge = np.clip(base / denom, 0.0, 1.0)
    result = base * (1.0 - opacity) + dodge * opacity
    (result.clip(0, 1) * 255).astype(np.uint8)
    Image.fromarray((result.clip(0, 1) * 255).astype(np.uint8)).save(output_path)


def image_sunbeams(input_path: "str", output_path: "str", *, cx: "float" = 0.5, cy: "float" = 0.3, num_steps: "int" = 20, decay: "float" = 0.95) -> "None":
    """Simulate sun rays by iteratively shifting and adding image toward a focal point."""
    from PIL import Image
    import numpy as np
    img = Image.open(input_path).convert("RGB")
    arr = np.array(img).astype(np.float32) / 255.0
    h, w = arr.shape[:2]
    cx_px = cx * w
    cy_px = cy * h
    accumulated = arr.copy()
    current = arr.copy()
    weight = 1.0
    total_weight = 1.0
    step_scale = 0.02
    for i in range(1, num_steps + 1):
        scale = 1.0 - i * step_scale
        if scale <= 0:
            break
        # Compute translation toward focal point
        tx = int((cx_px - w / 2) * step_scale)
        ty = int((cy_px - h / 2) * step_scale)
        # Scale and shift using numpy roll (approximate)
        from PIL import Image as PILImage
        scaled = PILImage.fromarray((current * 255).astype(np.uint8))
        nw, nh = max(1, int(w * scale)), max(1, int(h * scale))
        scaled = scaled.resize((nw, nh), PILImage.BILINEAR)
        canvas = np.zeros_like(arr)
        x0 = max(0, (w - nw) // 2 + tx)
        y0 = max(0, (h - nh) // 2 + ty)
        x1 = min(w, x0 + nw)
        y1 = min(h, y0 + nh)
        sw = x1 - x0
        sh = y1 - y0
        if sw > 0 and sh > 0:
            patch = np.array(scaled.crop((0, 0, sw, sh))).astype(np.float32) / 255.0
            canvas[y0:y0+sh, x0:x0+sw] = patch
        weight *= decay
        total_weight += weight
        accumulated += canvas * weight
        current = canvas
    result = (accumulated / total_weight).clip(0, 1)
    Image.fromarray((result * 255).astype(np.uint8)).save(output_path)


def image_pencil_color(input_path: "str", output_path: "str", *, sketch_blend: "float" = 0.5) -> "None":
    """Color pencil sketch: blend grayscale sketch layer with original color."""
    from PIL import Image, ImageFilter, ImageChops
    import numpy as np
    img = Image.open(input_path).convert("RGB")
    gray = img.convert("L")
    # Pencil sketch: invert, blur, divide
    inv = gray.point(lambda p: 255 - p)
    blurred = inv.filter(ImageFilter.GaussianBlur(radius=10))
    # Dodge: gray / (1 - blurred/255)
    gray_arr = np.array(gray).astype(np.float32) / 255.0
    blur_arr = np.array(blurred).astype(np.float32) / 255.0
    denom = np.clip(1.0 - blur_arr, 1e-6, 1.0)
    sketch = np.clip(gray_arr / denom, 0, 1)
    sketch_img = Image.fromarray((sketch * 255).astype(np.uint8)).convert("RGB")
    # Blend sketch with original color
    result = Image.blend(sketch_img, img, alpha=sketch_blend)
    result.save(output_path)


def image_selective_blur(input_path: "str", output_path: "str", *, threshold: "float" = 0.5, blur_radius: "int" = 5) -> "None":
    """Blur only shadow/dark areas, keep highlights sharp."""
    from PIL import Image, ImageFilter
    import numpy as np
    img = Image.open(input_path).convert("RGB")
    blurred = img.filter(ImageFilter.GaussianBlur(radius=blur_radius))
    arr = np.array(img).astype(np.float32) / 255.0
    blur_arr = np.array(blurred).astype(np.float32) / 255.0
    lum = 0.299 * arr[:, :, 0] + 0.587 * arr[:, :, 1] + 0.114 * arr[:, :, 2]
    # Blend weight: 1 = full blur (dark), 0 = sharp (bright)
    weight = np.clip(1.0 - lum / threshold, 0, 1)[:, :, np.newaxis]
    result = arr * (1.0 - weight) + blur_arr * weight
    Image.fromarray((result.clip(0, 1) * 255).astype(np.uint8)).save(output_path)


def image_light_leak(input_path: "str", output_path: "str", *, corner: "str" = "top_right", intensity: "float" = 0.6) -> "None":
    """Overlay warm radial gradient at a corner to simulate film light leak."""
    from PIL import Image
    import numpy as np
    img = Image.open(input_path).convert("RGB")
    w, h = img.size
    arr = np.array(img).astype(np.float32) / 255.0
    xs = np.linspace(0, 1, w)
    ys = np.linspace(0, 1, h)
    xx, yy = np.meshgrid(xs, ys)
    corners = {
        "top_right": (1.0, 0.0),
        "top_left": (0.0, 0.0),
        "bottom_right": (1.0, 1.0),
        "bottom_left": (0.0, 1.0),
    }
    cx, cy = corners.get(corner, (1.0, 0.0))
    dist = np.sqrt((xx - cx) ** 2 + (yy - cy) ** 2)
    # Leak is strongest at corner, fades with distance
    leak_alpha = np.clip(1.0 - dist / 0.8, 0, 1) ** 1.5 * intensity
    leak_alpha = leak_alpha[:, :, np.newaxis]
    # Warm orange color
    leak_color = np.array([1.0, 0.55, 0.1], dtype=np.float32)
    # Screen blend with leak
    result = 1.0 - (1.0 - arr) * (1.0 - leak_color * leak_alpha)
    Image.fromarray((result.clip(0, 1) * 255).astype(np.uint8)).save(output_path)


def image_pixelate_grid(input_path: "str", output_path: "str", *, block_size: "int" = 16, grid_color: "tuple" = (0, 0, 0)) -> "None":
    """Pixelate image with visible grid lines between blocks."""
    from PIL import Image, ImageDraw
    import numpy as np
    img = Image.open(input_path).convert("RGB")
    w, h = img.size
    arr = np.array(img)
    result = arr.copy()
    # Average each block
    for y in range(0, h, block_size):
        for x in range(0, w, block_size):
            block = arr[y:y+block_size, x:x+block_size]
            avg = block.mean(axis=(0, 1)).astype(np.uint8)
            result[y:y+block_size, x:x+block_size] = avg
    out_img = Image.fromarray(result)
    draw = ImageDraw.Draw(out_img)
    gc = grid_color
    for x in range(0, w, block_size):
        draw.line([(x, 0), (x, h)], fill=gc, width=1)
    for y in range(0, h, block_size):
        draw.line([(0, y), (w, y)], fill=gc, width=1)
    out_img.save(output_path)


def image_frost(input_path: "str", output_path: "str", *, blur_radius: "int" = 8, noise_amount: "float" = 0.05) -> "None":
    """Simulate frosted glass: blur + noise overlay."""
    from PIL import Image, ImageFilter
    import numpy as np
    img = Image.open(input_path).convert("RGB")
    blurred = img.filter(ImageFilter.GaussianBlur(radius=blur_radius))
    arr = np.array(blurred).astype(np.float32) / 255.0
    noise = np.random.uniform(-noise_amount, noise_amount, arr.shape).astype(np.float32)
    result = np.clip(arr + noise, 0, 1)
    Image.fromarray((result * 255).astype(np.uint8)).save(output_path)


def image_color_halftone(input_path: "str", output_path: "str", *, dot_size: "int" = 8) -> "None":
    """CMYK-style color halftone with offset dot grids per channel."""
    from PIL import Image, ImageDraw
    import numpy as np, math
    img = Image.open(input_path).convert("RGB")
    w, h = img.size
    arr = np.array(img).astype(np.float32) / 255.0
    # Convert RGB to CMY
    c = 1.0 - arr[:, :, 0]
    m = 1.0 - arr[:, :, 1]
    y_ch = 1.0 - arr[:, :, 2]
    out = Image.new("RGB", (w, h), (255, 255, 255))
    draw = ImageDraw.Draw(out)
    angles = [15, 75, 0]  # C, M, Y screen angles
    channels = [(c, (0, 255, 255)), (m, (255, 0, 255)), (y_ch, (255, 255, 0))]
    for ch_arr, color in zip([c, m, y_ch], [(0, 255, 255), (255, 0, 255), (255, 255, 0)]):
        angle_deg = angles[channels.index((ch_arr, color)) if (ch_arr, color) in channels else 0]
        rad = math.radians(angle_deg)
        cos_a, sin_a = math.cos(rad), math.sin(rad)
        step = dot_size
        for gy in range(-step, h + step, step):
            for gx in range(-step, w + step, step):
                # Rotate grid point
                rx = int(gx * cos_a - gy * sin_a)
                ry = int(gx * sin_a + gy * cos_a)
                if 0 <= rx < w and 0 <= ry < h:
                    val = ch_arr[ry, rx]
                    r = val * dot_size * 0.7
                    if r > 0.5:
                        bbox = [rx - r, ry - r, rx + r, ry + r]
                        # Use multiply-like blend: draw dot with transparency via fill
                        draw.ellipse(bbox, fill=color)
    out.save(output_path)


def image_relief(input_path: "str", output_path: "str", *, angle: "float" = 135.0, depth: "float" = 2.0) -> "None":
    """Emboss-style relief with directional lighting."""
    from PIL import Image, ImageFilter
    import numpy as np, math
    img = Image.open(input_path).convert("L")
    arr = np.array(img).astype(np.float32)
    rad = math.radians(angle)
    kx = math.cos(rad) * depth
    ky = math.sin(rad) * depth
    kernel = [
        -ky - kx, -ky,      -ky + kx,
        -kx,       1.0,      kx,
         ky - kx,  ky,       ky + kx,
    ]
    # Normalize kernel
    k = np.array(kernel, dtype=np.float32).reshape(3, 3)
    from scipy.ndimage import convolve
    try:
        embossed = convolve(arr, k) + 128
    except ImportError:
        # Manual convolution via PIL ImageFilter
        img2 = img.filter(ImageFilter.Kernel(size=3, kernel=[int(v*10) for v in kernel], scale=10, offset=128))
        img2.convert("RGB").save(output_path)
        return
    embossed = np.clip(embossed, 0, 255).astype(np.uint8)
    Image.fromarray(embossed).convert("RGB").save(output_path)


def image_rainbow_gradient(input_path: "str", output_path: "str", *, opacity: "float" = 0.4) -> "None":
    """Overlay a horizontal rainbow gradient with screen blend."""
    from PIL import Image
    import numpy as np
    img = Image.open(input_path).convert("RGB")
    w, h = img.size
    arr = np.array(img).astype(np.float32) / 255.0
    # Build rainbow via HSV: hue sweeps 0→1 across width
    xs = np.linspace(0.0, 1.0, w)
    import colorsys
    rainbow = np.zeros((h, w, 3), dtype=np.float32)
    for i, hue in enumerate(xs):
        r, g, b = colorsys.hsv_to_rgb(hue, 1.0, 1.0)
        rainbow[:, i, 0] = r
        rainbow[:, i, 1] = g
        rainbow[:, i, 2] = b
    # Screen blend
    screen = 1.0 - (1.0 - arr) * (1.0 - rainbow)
    result = arr * (1.0 - opacity) + screen * opacity
    Image.fromarray((result.clip(0, 1) * 255).astype(np.uint8)).save(output_path)


def image_tilt_shift(input_path: "str", output_path: "str", *, focus_y: "float" = 0.5, band_height: "float" = 0.25, blur_radius: "int" = 12) -> "None":
    """Blur top/bottom bands to simulate tilt-shift miniature effect on a still image."""
    from PIL import Image, ImageFilter
    import numpy as np
    img = Image.open(input_path).convert("RGB")
    w, h = img.size
    blurred = img.filter(ImageFilter.GaussianBlur(radius=blur_radius))
    arr_sharp = np.array(img).astype(np.float32) / 255.0
    arr_blur = np.array(blurred).astype(np.float32) / 255.0
    ys = np.linspace(0, 1, h)
    focus_lo = focus_y - band_height / 2
    focus_hi = focus_y + band_height / 2
    # Weight: 0 = sharp (in band), 1 = blurred (outside band)
    weight = np.ones(h, dtype=np.float32)
    for i, y in enumerate(ys):
        if focus_lo <= y <= focus_hi:
            # Smooth transition inside band
            dist = min(abs(y - focus_lo), abs(y - focus_hi)) / (band_height / 2)
            weight[i] = 1.0 - dist
        else:
            dist_from_edge = min(abs(y - focus_lo), abs(y - focus_hi))
            weight[i] = min(1.0, dist_from_edge / (band_height / 2))
    weight = weight[:, np.newaxis, np.newaxis]
    result = arr_sharp * (1.0 - weight) + arr_blur * weight
    Image.fromarray((result.clip(0, 1) * 255).astype(np.uint8)).save(output_path)


def image_diffuse_glow(input_path: "str", output_path: "str", *, blur_radius: "int" = 15, glow_strength: "float" = 0.5, threshold: "float" = 0.7) -> "None":
    """Dreamy diffuse glow: blend blurred highlight regions back onto image."""
    from PIL import Image, ImageFilter
    import numpy as np
    img = Image.open(input_path).convert("RGB")
    arr = np.array(img).astype(np.float32) / 255.0
    # Extract highlights
    lum = 0.299 * arr[:, :, 0] + 0.587 * arr[:, :, 1] + 0.114 * arr[:, :, 2]
    highlights = np.clip((lum - threshold) / (1.0 - threshold + 1e-6), 0, 1)[:, :, np.newaxis] * arr
    hl_img = Image.fromarray((highlights.clip(0, 1) * 255).astype(np.uint8))
    hl_blur = np.array(hl_img.filter(ImageFilter.GaussianBlur(radius=blur_radius))).astype(np.float32) / 255.0
    # Screen blend glow onto original
    result = 1.0 - (1.0 - arr) * (1.0 - hl_blur * glow_strength)
    Image.fromarray((result.clip(0, 1) * 255).astype(np.uint8)).save(output_path)


def image_stipple(input_path: "str", output_path: "str", *, dot_density: "int" = 1000, max_radius: "int" = 8) -> "None":
    """Pointillist stipple: random dots sized by local darkness."""
    from PIL import Image, ImageDraw
    import numpy as np
    img = Image.open(input_path).convert("RGB")
    w, h = img.size
    arr = np.array(img)
    canvas = Image.new("RGB", (w, h), (255, 255, 255))
    draw = ImageDraw.Draw(canvas)
    rng = np.random.default_rng(42)
    xs = rng.integers(0, w, dot_density)
    ys = rng.integers(0, h, dot_density)
    for x, y in zip(xs, ys):
        lum = (0.299 * arr[y, x, 0] + 0.587 * arr[y, x, 1] + 0.114 * arr[y, x, 2]) / 255.0
        r = max(1, int((1.0 - lum) * max_radius))
        color = tuple(arr[y, x])
        draw.ellipse([x - r, y - r, x + r, y + r], fill=color)
    canvas.save(output_path)


def image_color_burn_blend(input_path: "str", blend_path: "str", output_path: "str", *, opacity: "float" = 1.0) -> "None":
    """Color burn blend mode: darkens base by increasing contrast toward blend color."""
    from PIL import Image
    import numpy as np
    base = np.array(Image.open(input_path).convert("RGB")).astype(np.float32) / 255.0
    ov_img = Image.open(blend_path).convert("RGB").resize(
        (base.shape[1], base.shape[0]), Image.LANCZOS)
    ov = np.array(ov_img).astype(np.float32) / 255.0
    denom = np.clip(ov, 1e-6, 1.0)
    burn = np.clip(1.0 - (1.0 - base) / denom, 0.0, 1.0)
    result = base * (1.0 - opacity) + burn * opacity
    Image.fromarray((result.clip(0, 1) * 255).astype(np.uint8)).save(output_path)


def image_noise_stipple(input_path: "str", output_path: "str", *, threshold_noise: "float" = 0.15) -> "None":
    """Blue-noise-style ordered dither for artistic stippling effect."""
    from PIL import Image
    import numpy as np
    img = Image.open(input_path).convert("RGB")
    w, h = img.size
    arr = np.array(img).astype(np.float32) / 255.0
    lum = 0.299 * arr[:, :, 0] + 0.587 * arr[:, :, 1] + 0.114 * arr[:, :, 2]
    # Bayer 4x4 ordered dither matrix
    bayer = np.array([
        [ 0,  8,  2, 10],
        [12,  4, 14,  6],
        [ 3, 11,  1,  9],
        [15,  7, 13,  5],
    ], dtype=np.float32) / 16.0
    # Tile bayer to image size
    tiles_y = (h // 4) + 2
    tiles_x = (w // 4) + 2
    bayer_tiled = np.tile(bayer, (tiles_y, tiles_x))[:h, :w]
    # Dither: pixel is dark if lum < bayer threshold
    dithered = (lum < bayer_tiled).astype(np.float32)
    # Apply: dark pixels use original color, light pixels white
    result = np.ones_like(arr)
    mask = dithered[:, :, np.newaxis]
    result = result * (1.0 - mask) + arr * mask
    Image.fromarray((result.clip(0, 1) * 255).astype(np.uint8)).save(output_path)


def image_gradient_map(input_path: "str", output_path: "str", *, shadow_color: "tuple" = (20, 10, 60), highlight_color: "tuple" = (255, 230, 180)) -> "None":
    """Map grayscale luminance to a two-color gradient (shadows→highlights)."""
    from PIL import Image
    import numpy as np
    img = Image.open(input_path).convert("RGB")
    arr = np.array(img).astype(np.float32) / 255.0
    lum = (0.299 * arr[:, :, 0] + 0.587 * arr[:, :, 1] + 0.114 * arr[:, :, 2])[:, :, np.newaxis]
    sc = np.array(shadow_color, dtype=np.float32) / 255.0
    hc = np.array(highlight_color, dtype=np.float32) / 255.0
    result = sc * (1.0 - lum) + hc * lum
    Image.fromarray((result.clip(0, 1) * 255).astype(np.uint8)).save(output_path)


def image_cross_process(input_path: "str", output_path: "str") -> "None":
    """Cross-process film look via channel-specific S-curve adjustments."""
    from PIL import Image
    import numpy as np
    img = Image.open(input_path).convert("RGB")
    arr = np.array(img).astype(np.float32) / 255.0
    # R: pushed highlights
    r = arr[:, :, 0]
    r = np.where(r < 0.5, r * 1.1, 0.55 + (r - 0.5) * 0.9)
    # G: slight S-curve
    g = arr[:, :, 1]
    g = np.where(g < 0.5, g * 0.85, 0.425 + (g - 0.5) * 1.15)
    # B: lifted shadows, compressed highlights
    b = arr[:, :, 2]
    b = np.where(b < 0.5, 0.05 + b * 1.0, 0.55 + (b - 0.5) * 0.85)
    result = np.stack([r, g, b], axis=2)
    Image.fromarray((result.clip(0, 1) * 255).astype(np.uint8)).save(output_path)


def image_lomo(input_path: "str", output_path: "str", *, saturation_boost: "float" = 1.5, vignette_strength: "float" = 0.7) -> "None":
    """Lomo camera look: boosted saturation + strong vignette + blue shadow lift."""
    from PIL import Image, ImageEnhance
    import numpy as np
    img = Image.open(input_path).convert("RGB")
    # Boost saturation
    img = ImageEnhance.Color(img).enhance(saturation_boost)
    arr = np.array(img).astype(np.float32) / 255.0
    # Lift blue in shadows
    lum = 0.299 * arr[:, :, 0] + 0.587 * arr[:, :, 1] + 0.114 * arr[:, :, 2]
    shadow_mask = np.clip(1.0 - lum * 2, 0, 1)[:, :, np.newaxis]
    arr[:, :, 2] = np.clip(arr[:, :, 2] + shadow_mask[:, :, 0] * 0.15, 0, 1)
    # Strong vignette
    h, w = arr.shape[:2]
    xs = np.linspace(-1, 1, w)
    ys = np.linspace(-1, 1, h)
    xx, yy = np.meshgrid(xs, ys)
    dist = np.sqrt(xx**2 + yy**2)
    vignette = np.clip(1.0 - dist * vignette_strength, 0, 1)[:, :, np.newaxis]
    result = arr * vignette
    Image.fromarray((result.clip(0, 1) * 255).astype(np.uint8)).save(output_path)


def image_pixel_sort(input_path: "str", output_path: "str", *, threshold_lo: "float" = 0.2, threshold_hi: "float" = 0.8) -> "None":
    """Sort pixels in each row by brightness within a threshold band — glitch art effect."""
    from PIL import Image
    import numpy as np
    img = Image.open(input_path).convert("RGB")
    arr = np.array(img).copy()
    h, w = arr.shape[:2]
    lum = (0.299 * arr[:, :, 0] + 0.587 * arr[:, :, 1] + 0.114 * arr[:, :, 2]) / 255.0
    for y in range(h):
        row_lum = lum[y]
        mask = (row_lum >= threshold_lo) & (row_lum <= threshold_hi)
        # Find contiguous spans in mask
        in_span = False
        start = 0
        for x in range(w + 1):
            if x < w and mask[x]:
                if not in_span:
                    start = x
                    in_span = True
            else:
                if in_span:
                    # Sort span by brightness
                    span = arr[y, start:x]
                    span_lum = row_lum[start:x]
                    order = np.argsort(span_lum)
                    arr[y, start:x] = span[order]
                    in_span = False
    Image.fromarray(arr).save(output_path)


def image_mosaic_portrait(input_path: "str", output_path: "str", *, block_size: "int" = 24) -> "None":
    """Pixelate into large averaged-color square blocks."""
    from PIL import Image
    import numpy as np
    img = Image.open(input_path).convert("RGB")
    w, h = img.size
    arr = np.array(img).copy()
    for y in range(0, h, block_size):
        for x in range(0, w, block_size):
            block = arr[y:y+block_size, x:x+block_size]
            avg = block.mean(axis=(0, 1)).astype(np.uint8)
            arr[y:y+block_size, x:x+block_size] = avg
    Image.fromarray(arr).save(output_path)


def image_watermark_logo(input_path: "str", logo_path: "str", output_path: "str", *, corner: "str" = "bottom_right", opacity: "float" = 0.6, scale: "float" = 0.2) -> "None":
    """Composite a logo as watermark at a corner with opacity control."""
    from PIL import Image
    import numpy as np
    base = Image.open(input_path).convert("RGBA")
    bw, bh = base.size
    logo = Image.open(logo_path).convert("RGBA")
    lw = int(bw * scale)
    lh = int(logo.height * lw / logo.width)
    logo = logo.resize((lw, lh), Image.LANCZOS)
    # Adjust opacity
    logo_arr = np.array(logo).astype(np.float32)
    logo_arr[:, :, 3] *= opacity
    logo = Image.fromarray(logo_arr.astype(np.uint8))
    margin = int(bw * 0.02)
    positions = {
        "bottom_right": (bw - lw - margin, bh - lh - margin),
        "bottom_left": (margin, bh - lh - margin),
        "top_right": (bw - lw - margin, margin),
        "top_left": (margin, margin),
    }
    pos = positions.get(corner, positions["bottom_right"])
    base.paste(logo, pos, logo)
    base.convert("RGB").save(output_path)


def image_orton_effect(input_path: "str", output_path: "str", *, blur_radius: "int" = 10, strength: "float" = 0.7) -> "None":
    """Orton effect: multiply sharp image with blurred copy for dreamy glow."""
    from PIL import Image, ImageFilter
    import numpy as np
    img = Image.open(input_path).convert("RGB")
    arr = np.array(img).astype(np.float32) / 255.0
    blurred = np.array(img.filter(ImageFilter.GaussianBlur(radius=blur_radius))).astype(np.float32) / 255.0
    # Multiply blend
    multiplied = arr * blurred
    # Blend with original
    result = arr * (1.0 - strength) + multiplied * strength
    Image.fromarray((result.clip(0, 1) * 255).astype(np.uint8)).save(output_path)


def image_scanline_art(input_path: "str", output_path: "str", *, line_height: "int" = 4, gap: "int" = 2) -> "None":
    """Draw horizontal scanlines with color sampled from image rows for retro CRT look."""
    from PIL import Image, ImageDraw
    import numpy as np
    img = Image.open(input_path).convert("RGB")
    w, h = img.size
    arr = np.array(img)
    canvas = Image.new("RGB", (w, h), (0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    y = 0
    while y < h:
        # Sample average color of this row band
        band = arr[y:min(y + line_height, h)]
        avg_color = tuple(band.mean(axis=(0, 1)).astype(int))
        draw.rectangle([0, y, w, y + line_height - 1], fill=avg_color)
        y += line_height + gap
    canvas.save(output_path)


def image_color_overlay(input_path: "str", output_path: "str", *, color: "tuple" = (255, 0, 128), opacity: "float" = 0.3, blend_mode: "str" = "normal") -> "None":
    """Overlay a solid color on the image with given blend mode and opacity."""
    from PIL import Image
    import numpy as np
    img = Image.open(input_path).convert("RGB")
    arr = np.array(img).astype(np.float32) / 255.0
    ov = np.array(color, dtype=np.float32) / 255.0
    if blend_mode == "screen":
        blended = 1.0 - (1.0 - arr) * (1.0 - ov)
    elif blend_mode == "multiply":
        blended = arr * ov
    else:  # normal
        blended = ov * np.ones_like(arr)
    result = arr * (1.0 - opacity) + blended * opacity
    Image.fromarray((result.clip(0, 1) * 255).astype(np.uint8)).save(output_path)


def image_warp_swirl(input_path: "str", output_path: "str", *, angle: "float" = 60.0, radius: "float" = 0.5) -> "None":
    """Swirl distortion: rotate pixels around center by angle proportional to distance."""
    from PIL import Image
    import numpy as np
    img = Image.open(input_path).convert("RGB")
    arr = np.array(img).astype(np.uint8)
    h, w = arr.shape[:2]
    cx, cy = w / 2.0, h / 2.0
    max_r = min(w, h) * radius
    result = np.zeros_like(arr)
    ys, xs = np.mgrid[0:h, 0:w]
    dx = xs - cx
    dy = ys - cy
    dist = np.sqrt(dx**2 + dy**2)
    # Swirl angle decreases with distance
    swirl_angle = np.radians(angle) * np.clip(1.0 - dist / max_r, 0, 1)
    cos_a = np.cos(swirl_angle)
    sin_a = np.sin(swirl_angle)
    src_x = (cos_a * dx - sin_a * dy + cx).astype(np.float32)
    src_y = (sin_a * dx + cos_a * dy + cy).astype(np.float32)
    src_xi = np.clip(src_x.astype(int), 0, w - 1)
    src_yi = np.clip(src_y.astype(int), 0, h - 1)
    result = arr[src_yi, src_xi]
    Image.fromarray(result).save(output_path)


def image_sketch_color(input_path: "str", output_path: "str", *, edge_threshold: "int" = 30, whitening: "float" = 0.8) -> "None":
    """Colored sketch: detect edges, keep original color on edges, whiten elsewhere."""
    from PIL import Image, ImageFilter
    import numpy as np
    img = Image.open(input_path).convert("RGB")
    arr = np.array(img).astype(np.float32) / 255.0
    gray = img.convert("L")
    edges = np.array(gray.filter(ImageFilter.FIND_EDGES)).astype(np.float32)
    edge_mask = np.clip(edges / edge_threshold, 0, 1)[:, :, np.newaxis]
    white = np.ones_like(arr)
    # Whiten background, keep color on edges
    result = arr * edge_mask + (arr * (1 - whitening) + white * whitening) * (1 - edge_mask)
    Image.fromarray((result.clip(0, 1) * 255).astype(np.uint8)).save(output_path)


def image_neon_outline(input_path: "str", output_path: "str", *, glow_radius: "int" = 4, brightness: "float" = 3.0) -> "None":
    """Thick glowing neon outlines on black background."""
    from PIL import Image, ImageFilter, ImageEnhance
    import numpy as np
    img = Image.open(input_path).convert("RGB")
    edges = img.filter(ImageFilter.FIND_EDGES)
    # Dilate edges by blurring then thresholding
    thick = edges.filter(ImageFilter.GaussianBlur(radius=glow_radius))
    bright = ImageEnhance.Brightness(thick).enhance(brightness)
    # Colorize with original hue
    arr_orig = np.array(img).astype(np.float32) / 255.0
    arr_glow = np.array(bright).astype(np.float32) / 255.0
    lum_glow = 0.299 * arr_glow[:, :, 0] + 0.587 * arr_glow[:, :, 1] + 0.114 * arr_glow[:, :, 2]
    result = arr_orig * lum_glow[:, :, np.newaxis]
    Image.fromarray((result.clip(0, 1) * 255).astype(np.uint8)).save(output_path)


def image_texture_overlay(input_path: "str", texture_path: "str", output_path: "str", *, opacity: "float" = 0.5, blend_mode: "str" = "multiply") -> "None":
    """Blend a texture image onto base using specified blend mode."""
    from PIL import Image
    import numpy as np
    base = Image.open(input_path).convert("RGB")
    w, h = base.size
    tex = Image.open(texture_path).convert("RGB").resize((w, h), Image.LANCZOS)
    arr = np.array(base).astype(np.float32) / 255.0
    tex_arr = np.array(tex).astype(np.float32) / 255.0
    if blend_mode == "multiply":
        blended = arr * tex_arr
    elif blend_mode == "screen":
        blended = 1.0 - (1.0 - arr) * (1.0 - tex_arr)
    elif blend_mode == "overlay":
        blended = np.where(arr < 0.5, 2 * arr * tex_arr, 1 - 2 * (1 - arr) * (1 - tex_arr))
    else:
        blended = tex_arr
    result = arr * (1.0 - opacity) + blended * opacity
    Image.fromarray((result.clip(0, 1) * 255).astype(np.uint8)).save(output_path)


def image_color_shift_channels(input_path: "str", output_path: "str", *, r_shift: "tuple" = (5, 0), g_shift: "tuple" = (0, 0), b_shift: "tuple" = (-5, 0)) -> "None":
    """Shift R/G/B channels independently by (dx, dy) pixel offsets."""
    from PIL import Image
    import numpy as np
    img = Image.open(input_path).convert("RGB")
    arr = np.array(img).astype(np.uint8)
    h, w = arr.shape[:2]
    result = np.zeros_like(arr)
    for ch_idx, (dx, dy) in enumerate([r_shift, g_shift, b_shift]):
        ch = arr[:, :, ch_idx]
        # Roll with fill (clip coordinates)
        shifted = np.zeros_like(ch)
        src_y_start = max(0, -dy)
        src_y_end = min(h, h - dy)
        dst_y_start = max(0, dy)
        dst_y_end = min(h, h + dy)
        src_x_start = max(0, -dx)
        src_x_end = min(w, w - dx)
        dst_x_start = max(0, dx)
        dst_x_end = min(w, w + dx)
        shifted[dst_y_start:dst_y_end, dst_x_start:dst_x_end] = ch[src_y_start:src_y_end, src_x_start:src_x_end]
        result[:, :, ch_idx] = shifted
    Image.fromarray(result).save(output_path)


def image_glamour_glow(input_path: "str", output_path: "str", *, blur_radius: "int" = 12, glow_strength: "float" = 0.4, warmth: "float" = 0.1) -> "None":
    """Soft glamour glow: blend blurred image back + warm color boost."""
    from PIL import Image, ImageFilter
    import numpy as np
    img = Image.open(input_path).convert("RGB")
    arr = np.array(img).astype(np.float32) / 255.0
    blurred = np.array(img.filter(ImageFilter.GaussianBlur(radius=blur_radius))).astype(np.float32) / 255.0
    # Screen blend glow
    glow = 1.0 - (1.0 - arr) * (1.0 - blurred * glow_strength)
    # Warm tint: boost red/reduce blue slightly
    glow[:, :, 0] = np.clip(glow[:, :, 0] + warmth * 0.1, 0, 1)
    glow[:, :, 2] = np.clip(glow[:, :, 2] - warmth * 0.05, 0, 1)
    Image.fromarray((glow.clip(0, 1) * 255).astype(np.uint8)).save(output_path)


def image_kaleidoscope(input_path: "str", output_path: "str", *, segments: "int" = 8) -> "None":
    """Create kaleidoscope by mirroring a wedge slice repeatedly around center."""
    from PIL import Image
    import numpy as np, math
    img = Image.open(input_path).convert("RGB")
    w, h = img.size
    size = min(w, h)
    # Crop to square centered
    left = (w - size) // 2
    top = (h - size) // 2
    img = img.crop((left, top, left + size, top + size)).resize((size, size), Image.LANCZOS)
    arr = np.array(img).astype(np.uint8)
    result = np.zeros((size, size, 3), dtype=np.uint8)
    cx, cy = size / 2.0, size / 2.0
    angle_step = 2 * math.pi / segments
    ys, xs = np.mgrid[0:size, 0:size]
    dx = xs - cx
    dy = ys - cy
    angles = np.arctan2(dy, dx)
    radii = np.sqrt(dx**2 + dy**2)
    # Map angle to first wedge
    wedge_angle = angles % angle_step
    # Mirror every other wedge
    wedge_idx = (angles // angle_step).astype(int) % 2
    mapped_angle = np.where(wedge_idx == 0, wedge_angle, angle_step - wedge_angle)
    src_x = np.clip((cx + radii * np.cos(mapped_angle)).astype(int), 0, size - 1)
    src_y = np.clip((cy + radii * np.sin(mapped_angle)).astype(int), 0, size - 1)
    result = arr[src_y, src_x]
    Image.fromarray(result).save(output_path)


def image_vintage_photo(input_path: "str", output_path: "str", *, scratch_count: "int" = 20) -> "None":
    """Vintage photo: sepia + vignette + dust scratches + slight blur."""
    from PIL import Image, ImageFilter, ImageDraw
    import numpy as np, random
    img = Image.open(input_path).convert("RGB")
    w, h = img.size
    arr = np.array(img).astype(np.float32) / 255.0
    # Sepia
    r = arr[:, :, 0] * 0.393 + arr[:, :, 1] * 0.769 + arr[:, :, 2] * 0.189
    g = arr[:, :, 0] * 0.349 + arr[:, :, 1] * 0.686 + arr[:, :, 2] * 0.168
    b = arr[:, :, 0] * 0.272 + arr[:, :, 1] * 0.534 + arr[:, :, 2] * 0.131
    arr = np.stack([r, g, b], axis=2).clip(0, 1)
    # Slight blur
    sepia_img = Image.fromarray((arr * 255).astype(np.uint8))
    sepia_img = sepia_img.filter(ImageFilter.GaussianBlur(radius=0.5))
    arr = np.array(sepia_img).astype(np.float32) / 255.0
    # Vignette
    xs = np.linspace(-1, 1, w)
    ys = np.linspace(-1, 1, h)
    xx, yy = np.meshgrid(xs, ys)
    vignette = np.clip(1.0 - (xx**2 + yy**2) * 0.6, 0, 1)[:, :, np.newaxis]
    arr = arr * vignette
    result_img = Image.fromarray((arr.clip(0, 1) * 255).astype(np.uint8))
    # Dust scratches
    draw = ImageDraw.Draw(result_img)
    rng = random.Random(42)
    for _ in range(scratch_count):
        x = rng.randint(0, w)
        y1, y2 = rng.randint(0, h // 2), rng.randint(h // 2, h)
        brightness = rng.randint(180, 255)
        draw.line([(x, y1), (x, y2)], fill=(brightness, brightness, brightness - 20), width=1)
    result_img.save(output_path)


def image_paint_strokes(input_path: "str", output_path: "str", *, stroke_size: "int" = 8, iterations: "int" = 3) -> "None":
    """Painterly effect via repeated local averaging in random small ellipses."""
    from PIL import Image, ImageFilter
    import numpy as np
    img = Image.open(input_path).convert("RGB")
    arr = np.array(img).astype(np.float32)
    # Approximate oil paint with multiple passes of median-like smoothing
    result = arr.copy()
    for _ in range(iterations):
        blurred = np.array(
            Image.fromarray(result.astype(np.uint8)).filter(
                ImageFilter.MedianFilter(size=stroke_size | 1)
            )
        ).astype(np.float32)
        # Keep edge detail by blending based on local variance
        result = blurred * 0.85 + arr * 0.15
    Image.fromarray(result.clip(0, 255).astype(np.uint8)).save(output_path)


def image_morning_haze(input_path: "str", output_path: "str", *, intensity: "float" = 0.45, warmth: "float" = 0.15) -> "None":
    """Atmospheric morning haze: bright warm mist overlay stronger at top."""
    from PIL import Image
    import numpy as np
    img = Image.open(input_path).convert("RGB")
    w, h = img.size
    arr = np.array(img).astype(np.float32) / 255.0
    # Gradient: stronger haze at top
    ys = np.linspace(1.0, 0.0, h)  # 1 at top, 0 at bottom
    haze_alpha = (ys * intensity)[:, np.newaxis, np.newaxis]
    # Warm haze color (pale yellow-white)
    haze_color = np.array([1.0, 0.97, 0.88], dtype=np.float32)
    result = arr * (1.0 - haze_alpha) + haze_color * haze_alpha
    # Slight warmth boost overall
    result[:, :, 0] = np.clip(result[:, :, 0] + warmth * 0.08, 0, 1)
    result[:, :, 2] = np.clip(result[:, :, 2] - warmth * 0.04, 0, 1)
    Image.fromarray((result.clip(0, 1) * 255).astype(np.uint8)).save(output_path)


def image_color_relief(input_path: "str", output_path: "str", *, azimuth: "float" = 315.0, elevation: "float" = 45.0, depth: "float" = 2.0) -> "None":
    """Color relief shading: directional light source creates 3-D emboss preserving hue."""
    from PIL import Image
    import numpy as np
    img = Image.open(input_path).convert("RGB")
    arr = np.array(img).astype(np.float32) / 255.0
    # Compute luminance for surface normal estimation
    lum = 0.299 * arr[:, :, 0] + 0.587 * arr[:, :, 1] + 0.114 * arr[:, :, 2]
    # Gradient of luminance
    dy = np.gradient(lum, axis=0) * depth
    dx = np.gradient(lum, axis=1) * depth
    # Light direction vector
    az = np.radians(azimuth)
    el = np.radians(elevation)
    lx = np.cos(el) * np.cos(az)
    ly = np.cos(el) * np.sin(az)
    lz = np.sin(el)
    # Normal dot light
    norm = np.sqrt(dx**2 + dy**2 + 1.0)
    shade = (-dx * lx - dy * ly + lz) / norm
    shade = np.clip((shade + 1.0) / 2.0, 0, 1)[:, :, np.newaxis]
    result = arr * shade
    Image.fromarray((result.clip(0, 1) * 255).astype(np.uint8)).save(output_path)


def image_glitter(input_path: "str", output_path: "str", *, density: "float" = 0.04, sparkle_size: "int" = 2, brightness: "float" = 1.5) -> "None":
    """Add random sparkle/glitter points that bloom on bright highlights."""
    from PIL import Image
    import numpy as np
    rng = np.random.default_rng(42)
    img = Image.open(input_path).convert("RGB")
    arr = np.array(img).astype(np.float32) / 255.0
    lum = 0.299 * arr[:, :, 0] + 0.587 * arr[:, :, 1] + 0.114 * arr[:, :, 2]
    h, w = lum.shape
    result = arr.copy()
    # Only sparkle on pixels above mean brightness
    threshold = lum.mean() + 0.15
    candidates = np.argwhere(lum > threshold)
    n_sparkles = int(len(candidates) * density)
    if n_sparkles > 0 and len(candidates) > 0:
        chosen = candidates[rng.integers(0, len(candidates), size=min(n_sparkles, len(candidates)))]
        for y, x in chosen:
            for dy in range(-sparkle_size, sparkle_size + 1):
                for dx in range(-sparkle_size, sparkle_size + 1):
                    ny, nx = y + dy, x + dx
                    if 0 <= ny < h and 0 <= nx < w:
                        dist = max(abs(dy), abs(dx))
                        strength = brightness * (1 - dist / (sparkle_size + 1))
                        result[ny, nx] = np.clip(result[ny, nx] * strength, 0, 1)
    Image.fromarray((result.clip(0, 1) * 255).astype(np.uint8)).save(output_path)


def image_watercolor_light(input_path: "str", output_path: "str", *, blur_radius: "int" = 3, wash_strength: "float" = 0.25) -> "None":
    """Light watercolor wash: soft edges with subtle color bleed and paper-white lift."""
    from PIL import Image, ImageFilter
    import numpy as np
    img = Image.open(input_path).convert("RGB")
    arr = np.array(img).astype(np.float32) / 255.0
    # Soft blur to simulate paint bleed
    blurred = np.array(img.filter(ImageFilter.GaussianBlur(radius=blur_radius))).astype(np.float32) / 255.0
    # Blend: mostly blurred with edge detail from original
    result = blurred * (1 - wash_strength) + arr * wash_strength
    # Lift to simulate paper white absorption
    result = result + (1 - result) * 0.08
    Image.fromarray((result.clip(0, 1) * 255).astype(np.uint8)).save(output_path)


def image_solarize_color(input_path: "str", output_path: "str", *, threshold: "float" = 0.5, hue_shift: "float" = 0.33) -> "None":
    """Color solarization: pixels above threshold have hue rotated, creating psychedelic inversion."""
    from PIL import Image
    import numpy as np
    img = Image.open(input_path).convert("RGB")
    arr = np.array(img).astype(np.float32) / 255.0
    lum = 0.299 * arr[:, :, 0] + 0.587 * arr[:, :, 1] + 0.114 * arr[:, :, 2]
    mask = lum > threshold
    solarized = arr.copy()
    # Rotate hue of pixels above threshold by shifting channels
    above = arr[mask]
    # Simple hue rotation: shift R→G→B channels
    shift = int(hue_shift * 3) % 3
    if shift == 1:
        above = above[:, [1, 2, 0]]
    elif shift == 2:
        above = above[:, [2, 0, 1]]
    solarized[mask] = above
    Image.fromarray((solarized.clip(0, 1) * 255).astype(np.uint8)).save(output_path)


def image_pixel_wave(input_path: "str", output_path: "str", *, amplitude: "int" = 10, wavelength: "float" = 40.0, direction: "str" = "horizontal") -> "None":
    """Sinusoidal pixel displacement wave distortion."""
    from PIL import Image
    import numpy as np
    img = Image.open(input_path).convert("RGB")
    arr = np.array(img).astype(np.float32)
    h, w = arr.shape[:2]
    result = np.zeros_like(arr)
    if direction == "horizontal":
        for y in range(h):
            shift = int(amplitude * np.sin(2 * np.pi * y / wavelength))
            result[y] = np.roll(arr[y], shift, axis=0)
    else:
        for x in range(w):
            shift = int(amplitude * np.sin(2 * np.pi * x / wavelength))
            result[:, x] = np.roll(arr[:, x], shift, axis=0)
    Image.fromarray(result.clip(0, 255).astype(np.uint8)).save(output_path)


def image_crystallize(input_path: "str", output_path: "str", *, n_cells: "int" = 200, seed: "int" = 0) -> "None":
    """Crystallize/Voronoi mosaic: each pixel takes the color of its nearest random seed point."""
    from PIL import Image
    import numpy as np
    rng = np.random.default_rng(seed)
    img = Image.open(input_path).convert("RGB")
    arr = np.array(img)
    h, w = arr.shape[:2]
    # Random seed points
    sy = rng.integers(0, h, size=n_cells)
    sx = rng.integers(0, w, size=n_cells)
    # Build voronoi by nearest neighbor via broadcasting (small n_cells)
    ys = np.arange(h)[:, np.newaxis]
    xs = np.arange(w)[np.newaxis, :]
    result = np.zeros_like(arr)
    # Process in chunks to manage memory
    chunk = 50
    label = np.full((h, w), -1, dtype=np.int32)
    min_dist = np.full((h, w), np.inf)
    for i in range(0, n_cells, chunk):
        sy_c = sy[i:i+chunk]
        sx_c = sx[i:i+chunk]
        for j, (py, px) in enumerate(zip(sy_c, sx_c)):
            dist = (ys - py) ** 2 + (xs - px) ** 2
            mask = dist < min_dist
            min_dist[mask] = dist[mask]
            label[mask] = i + j
    for i, (py, px) in enumerate(zip(sy, sx)):
        result[label == i] = arr[py, px]
    Image.fromarray(result.astype(np.uint8)).save(output_path)


def image_comic_dots(input_path: "str", output_path: "str", *, dot_size: "int" = 6, contrast: "float" = 1.2) -> "None":
    """Comic book Ben-Day dot pattern: halftone dots sized by luminance."""
    from PIL import Image, ImageDraw
    import numpy as np
    img = Image.open(input_path).convert("RGB")
    arr = np.array(img).astype(np.float32) / 255.0
    h, w = arr.shape[:2]
    # Create white canvas
    canvas = Image.new("RGB", (w, h), (255, 255, 255))
    draw = ImageDraw.Draw(canvas)
    s = dot_size
    for y in range(0, h, s):
        for x in range(0, w, s):
            block = arr[y:y+s, x:x+s]
            if block.size == 0:
                continue
            avg_color = block.mean(axis=(0, 1))
            lum = 0.299 * avg_color[0] + 0.587 * avg_color[1] + 0.114 * avg_color[2]
            # Dot radius proportional to darkness
            radius = (1.0 - lum) * contrast * (s / 2)
            radius = np.clip(radius, 0, s / 2)
            cx, cy = x + s // 2, y + s // 2
            color = tuple((avg_color * 255).astype(np.uint8))
            r = int(radius)
            if r > 0:
                draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=color)
    canvas.save(output_path)


def image_thermal(input_path: "str", output_path: "str", *, colormap: "str" = "jet") -> "None":
    """Thermal camera visualization: map luminance to heat colormap (cool→warm)."""
    from PIL import Image
    import numpy as np
    img = Image.open(input_path).convert("RGB")
    arr = np.array(img).astype(np.float32) / 255.0
    lum = 0.299 * arr[:, :, 0] + 0.587 * arr[:, :, 1] + 0.114 * arr[:, :, 2]
    t = lum  # 0=cold, 1=hot
    # Jet-like colormap: blue→cyan→green→yellow→red
    r = np.clip(1.5 - np.abs(t * 4 - 3), 0, 1)
    g = np.clip(1.5 - np.abs(t * 4 - 2), 0, 1)
    b = np.clip(1.5 - np.abs(t * 4 - 1), 0, 1)
    result = np.stack([r, g, b], axis=-1)
    Image.fromarray((result.clip(0, 1) * 255).astype(np.uint8)).save(output_path)


def image_pastel_wash(input_path: "str", output_path: "str", *, softness: "float" = 0.35, brightness: "float" = 0.08) -> "None":
    """Pastel wash effect with softened contrast and milky color lift."""
    from PIL import Image
    import numpy as np

    img = Image.open(input_path).convert("RGB")
    arr = np.array(img).astype(np.float32) / 255.0
    blur = arr.copy()
    for _ in range(2):
        blur = (
            blur
            + np.roll(blur, 1, axis=0)
            + np.roll(blur, -1, axis=0)
            + np.roll(blur, 1, axis=1)
            + np.roll(blur, -1, axis=1)
        ) / 5.0

    base = arr * (1.0 - softness) + blur * softness
    pastel = np.sqrt(np.clip(base + brightness, 0.0, 1.0))
    pastel = pastel * 0.88 + 0.12
    Image.fromarray((pastel.clip(0, 1) * 255).astype(np.uint8)).save(output_path)


def image_prism_burst(input_path: "str", output_path: "str", *, offset: "int" = 10, glow: "float" = 0.3) -> "None":
    """Prism burst effect using radial RGB offsets and soft additive glow."""
    from PIL import Image
    import numpy as np

    img = Image.open(input_path).convert("RGB")
    arr = np.array(img).astype(np.float32) / 255.0
    h, w = arr.shape[:2]
    yy, xx = np.indices((h, w), dtype=np.float32)
    cx = (w - 1) / 2.0
    cy = (h - 1) / 2.0
    dx = xx - cx
    dy = yy - cy
    dist = np.sqrt(dx * dx + dy * dy) + 1e-6
    ux = dx / dist
    uy = dy / dist
    shift_x = np.rint(ux * offset).astype(np.int32)
    shift_y = np.rint(uy * offset).astype(np.int32)

    def _sample(channel_index: int, mult: int) -> np.ndarray:
        sx = np.clip(np.arange(w)[None, :] + shift_x * mult, 0, w - 1)
        sy = np.clip(np.arange(h)[:, None] + shift_y * mult, 0, h - 1)
        return arr[:, :, channel_index][sy, sx]

    prism = np.stack([
        _sample(0, 1),
        arr[:, :, 1],
        _sample(2, -1),
    ], axis=-1)
    highlight = np.clip(arr.max(axis=2, keepdims=True) - 0.65, 0.0, 1.0) / 0.35
    result = prism + highlight * glow
    Image.fromarray((result.clip(0, 1) * 255).astype(np.uint8)).save(output_path)


def image_blueprint_edges(input_path: "str", output_path: "str", *, line_strength: "float" = 1.4) -> "None":
    """Blueprint-style white edge lines over a deep blue background."""
    from PIL import Image
    import numpy as np

    img = Image.open(input_path).convert("RGB")
    arr = np.array(img).astype(np.float32) / 255.0
    gray = 0.299 * arr[:, :, 0] + 0.587 * arr[:, :, 1] + 0.114 * arr[:, :, 2]
    gx = np.zeros_like(gray)
    gy = np.zeros_like(gray)
    gx[:, 1:-1] = gray[:, 2:] - gray[:, :-2]
    gy[1:-1, :] = gray[2:, :] - gray[:-2, :]
    edges = np.sqrt(gx * gx + gy * gy) * line_strength
    edges = np.clip(edges, 0.0, 1.0)
    base = np.zeros((gray.shape[0], gray.shape[1], 3), dtype=np.float32)
    base[:, :, 0] = 0.05
    base[:, :, 1] = 0.22
    base[:, :, 2] = 0.58
    result = base + edges[:, :, None] * 0.95
    Image.fromarray((result.clip(0, 1) * 255).astype(np.uint8)).save(output_path)


def image_paper_cutout(input_path: "str", output_path: "str", *, levels: "int" = 5, shadow_shift: "int" = 6) -> "None":
    """Paper cutout look with posterized layers and a soft offset shadow."""
    from PIL import Image
    import numpy as np

    img = Image.open(input_path).convert("RGB")
    arr = np.array(img).astype(np.float32) / 255.0
    levels = max(2, int(levels))
    quant = np.floor(arr * (levels - 1) + 0.5) / (levels - 1)
    shadow = np.roll(np.roll(quant.mean(axis=2, keepdims=True), shadow_shift, axis=0), shadow_shift, axis=1)
    shadow = np.repeat(shadow, 3, axis=2) * np.array([0.55, 0.52, 0.48], dtype=np.float32)
    result = np.clip(shadow * 0.35 + quant * 0.9 + 0.08, 0.0, 1.0)
    Image.fromarray((result * 255).astype(np.uint8)).save(output_path)


def image_charcoal_smudge(input_path: "str", output_path: "str", *, stroke_strength: "float" = 1.35) -> "None":
    """Charcoal sketch look with dark edges and soft paper smudging."""
    from PIL import Image
    import numpy as np

    img = Image.open(input_path).convert("RGB")
    arr = np.array(img).astype(np.float32) / 255.0
    gray = 0.299 * arr[:, :, 0] + 0.587 * arr[:, :, 1] + 0.114 * arr[:, :, 2]
    blur = gray.copy()
    for _ in range(3):
        blur = (
            blur
            + np.roll(blur, 1, axis=0)
            + np.roll(blur, -1, axis=0)
            + np.roll(blur, 1, axis=1)
            + np.roll(blur, -1, axis=1)
        ) / 5.0
    gx = np.zeros_like(blur)
    gy = np.zeros_like(blur)
    gx[:, 1:-1] = blur[:, 2:] - blur[:, :-2]
    gy[1:-1, :] = blur[2:, :] - blur[:-2, :]
    edges = np.sqrt(gx * gx + gy * gy) * stroke_strength
    paper = np.clip(0.96 - blur * 0.25, 0.0, 1.0)
    charcoal = np.clip(paper - edges * 2.4, 0.0, 1.0)
    result = np.stack([charcoal, charcoal, charcoal], axis=-1)
    Image.fromarray((result.clip(0, 1) * 255).astype(np.uint8)).save(output_path)


def image_cyanotype(input_path: "str", output_path: "str", *, contrast: "float" = 1.25, paper_tint: "float" = 0.12) -> "None":
    """Cyanotype print effect with cool blue shadows and bright paper highlights."""
    from PIL import Image
    import numpy as np

    img = Image.open(input_path).convert("RGB")
    arr = np.array(img).astype(np.float32) / 255.0
    lum = 0.299 * arr[:, :, 0] + 0.587 * arr[:, :, 1] + 0.114 * arr[:, :, 2]
    tone = np.clip((lum - 0.5) * contrast + 0.5, 0.0, 1.0)
    blue = np.zeros((arr.shape[0], arr.shape[1], 3), dtype=np.float32)
    blue[:, :, 0] = 0.05 + tone * paper_tint
    blue[:, :, 1] = 0.18 + tone * 0.48
    blue[:, :, 2] = 0.32 + tone * 0.62
    wash = np.clip((1.0 - tone)[:, :, None] * np.array([0.03, 0.08, 0.18], dtype=np.float32), 0.0, 1.0)
    result = np.clip(blue + wash, 0.0, 1.0)
    Image.fromarray((result.clip(0, 1) * 255).astype(np.uint8)).save(output_path)


def image_noir_silhouette(input_path: "str", output_path: "str", *, threshold: "float" = 0.42) -> "None":
    """High-contrast noir treatment with silhouette shadows and cool highlights."""
    from PIL import Image
    import numpy as np

    img = Image.open(input_path).convert("RGB")
    arr = np.array(img).astype(np.float32) / 255.0
    lum = 0.299 * arr[:, :, 0] + 0.587 * arr[:, :, 1] + 0.114 * arr[:, :, 2]
    shadows = (lum > threshold).astype(np.float32)
    soft = shadows.copy()
    for _ in range(2):
        soft = (
            soft
            + np.roll(soft, 1, axis=0)
            + np.roll(soft, -1, axis=0)
            + np.roll(soft, 1, axis=1)
            + np.roll(soft, -1, axis=1)
        ) / 5.0
    result = np.zeros((arr.shape[0], arr.shape[1], 3), dtype=np.float32)
    result[:, :, 0] = soft * 0.85
    result[:, :, 1] = soft * 0.88
    result[:, :, 2] = soft * 0.94
    Image.fromarray((result.clip(0, 1) * 255).astype(np.uint8)).save(output_path)


def image_risograph_dual(input_path: "str", output_path: "str", *, tint_strength: "float" = 0.85) -> "None":
    """Riso-inspired two-ink print using warm shadows and cool highlight ink."""
    from PIL import Image
    import numpy as np

    img = Image.open(input_path).convert("RGB")
    arr = np.array(img).astype(np.float32) / 255.0
    lum = 0.299 * arr[:, :, 0] + 0.587 * arr[:, :, 1] + 0.114 * arr[:, :, 2]
    grain = (
        np.sin(np.arange(arr.shape[1], dtype=np.float32)[None, :] * 0.13)
        + np.cos(np.arange(arr.shape[0], dtype=np.float32)[:, None] * 0.11)
    ) * 0.02
    tone = np.clip(lum + grain, 0.0, 1.0)
    shadow_ink = np.array([0.95, 0.39, 0.16], dtype=np.float32)
    highlight_ink = np.array([0.08, 0.56, 0.82], dtype=np.float32)
    paper = np.array([0.97, 0.94, 0.88], dtype=np.float32)
    shadow_mix = (1.0 - tone)[:, :, None] * shadow_ink
    light_mix = np.sqrt(tone)[:, :, None] * highlight_ink * 0.7
    result = np.clip(paper * (1.0 - tint_strength) + (shadow_mix + light_mix) * tint_strength, 0.0, 1.0)
    Image.fromarray((result.clip(0, 1) * 255).astype(np.uint8)).save(output_path)


def image_metallic_sheen(input_path: "str", output_path: "str", *, sheen: "float" = 0.55, coolness: "float" = 0.2) -> "None":
    """Metallic sheen effect with cool chrome highlights and contrasty reflections."""
    from PIL import Image
    import numpy as np

    img = Image.open(input_path).convert("RGB")
    arr = np.array(img).astype(np.float32) / 255.0
    lum = 0.299 * arr[:, :, 0] + 0.587 * arr[:, :, 1] + 0.114 * arr[:, :, 2]
    highlight = np.clip((lum - 0.45) / 0.55, 0.0, 1.0)
    reflected = np.power(np.clip(arr, 0.0, 1.0), 0.78)
    chrome = reflected * (1.0 + sheen * 0.45)
    chrome[:, :, 2] *= 1.0 + coolness * 0.35
    chrome[:, :, 1] *= 1.0 + coolness * 0.12
    chrome[:, :, 0] *= 1.0 - coolness * 0.18
    specular = highlight[:, :, None] * np.array([0.92, 0.97, 1.0], dtype=np.float32) * sheen
    result = np.clip(arr * (1.0 - sheen * 0.32) + chrome * 0.62 + specular, 0.0, 1.0)
    Image.fromarray((result * 255).astype(np.uint8)).save(output_path)


def image_velvet_shadow(input_path: "str", output_path: "str", *, softness: "float" = 0.35, tint: "float" = 0.18) -> "None":
    """Velvet-shadow look with plush darks, muted mids, and a soft plum tint."""
    from PIL import Image
    import numpy as np

    img = Image.open(input_path).convert("RGB")
    arr = np.array(img).astype(np.float32) / 255.0
    blur = arr.copy()
    for _ in range(2):
        blur = (
            blur
            + np.roll(blur, 1, axis=0)
            + np.roll(blur, -1, axis=0)
            + np.roll(blur, 1, axis=1)
            + np.roll(blur, -1, axis=1)
        ) / 5.0
    lum = 0.299 * blur[:, :, 0] + 0.587 * blur[:, :, 1] + 0.114 * blur[:, :, 2]
    shadow_mask = np.clip((0.62 - lum) / 0.62, 0.0, 1.0)
    plush = blur * (1.0 - softness * 0.22) + np.sqrt(np.clip(blur, 0.0, 1.0)) * softness * 0.36
    tint_color = np.array([0.22, 0.08, 0.24], dtype=np.float32)
    result = plush * (1.0 - shadow_mask[:, :, None] * tint) + tint_color * shadow_mask[:, :, None] * tint
    result = np.clip(result * 0.96 + shadow_mask[:, :, None] * 0.04, 0.0, 1.0)
    Image.fromarray((result * 255).astype(np.uint8)).save(output_path)


def image_porcelain_glow(input_path: "str", output_path: "str", *, smoothness: "float" = 0.3, blush: "float" = 0.08) -> "None":
    """Porcelain portrait glow with creamy smoothing and soft rosy highlights."""
    from PIL import Image
    import numpy as np

    img = Image.open(input_path).convert("RGB")
    arr = np.array(img).astype(np.float32) / 255.0
    smooth = arr.copy()
    for _ in range(3):
        smooth = (
            smooth
            + np.roll(smooth, 1, axis=0)
            + np.roll(smooth, -1, axis=0)
            + np.roll(smooth, 1, axis=1)
            + np.roll(smooth, -1, axis=1)
        ) / 5.0
    lift = np.sqrt(np.clip(smooth + 0.03, 0.0, 1.0))
    blush_tint = np.array([1.0, 0.92, 0.95], dtype=np.float32)
    result = arr * (1.0 - smoothness) + lift * smoothness
    result = np.clip(result * (1.0 - blush * 0.35) + blush_tint * blush, 0.0, 1.0)
    Image.fromarray((result * 255).astype(np.uint8)).save(output_path)


def image_ember_bloom(input_path: "str", output_path: "str", *, warmth: "float" = 0.24, glow: "float" = 0.28) -> "None":
    """Ember bloom treatment with warm highlights and a soft firelit halo."""
    from PIL import Image
    import numpy as np

    img = Image.open(input_path).convert("RGB")
    arr = np.array(img).astype(np.float32) / 255.0
    halo = arr.copy()
    for _ in range(2):
        halo = (
            halo
            + np.roll(halo, 1, axis=0)
            + np.roll(halo, -1, axis=0)
            + np.roll(halo, 1, axis=1)
            + np.roll(halo, -1, axis=1)
        ) / 5.0
    lum = 0.299 * halo[:, :, 0] + 0.587 * halo[:, :, 1] + 0.114 * halo[:, :, 2]
    flare = np.clip((lum - 0.48) / 0.52, 0.0, 1.0)[:, :, None]
    ember = halo.copy()
    ember[:, :, 0] *= 1.0 + warmth * 0.35
    ember[:, :, 1] *= 1.0 + warmth * 0.12
    ember[:, :, 2] *= 1.0 - warmth * 0.22
    result = np.clip(arr * 0.78 + ember * 0.22 + flare * np.array([1.0, 0.48, 0.12], dtype=np.float32) * glow, 0.0, 1.0)
    Image.fromarray((result * 255).astype(np.uint8)).save(output_path)


def image_opaline_glow(input_path: "str", output_path: "str", *, glow: "float" = 0.24, pearl: "float" = 0.14) -> "None":
    """Pearlescent glow with lifted highlights and a cool opal tint."""
    from PIL import Image
    import numpy as np

    img = Image.open(input_path).convert("RGB")
    arr = np.array(img).astype(np.float32) / 255.0
    blur = arr.copy()
    for _ in range(3):
        blur = (
            blur
            + np.roll(blur, 1, axis=0)
            + np.roll(blur, -1, axis=0)
            + np.roll(blur, 1, axis=1)
            + np.roll(blur, -1, axis=1)
        ) / 5.0
    lum = 0.299 * arr[:, :, 0] + 0.587 * arr[:, :, 1] + 0.114 * arr[:, :, 2]
    highlight = np.clip((lum - 0.42) / 0.58, 0.0, 1.0)[:, :, None]
    pearl_tint = np.array([0.95, 0.99, 1.0], dtype=np.float32)
    bloom = np.sqrt(np.clip(blur, 0.0, 1.0)) * glow
    result = np.clip(arr * (1.0 - glow * 0.28) + bloom * 0.55 + highlight * pearl_tint * pearl, 0.0, 1.0)
    Image.fromarray((result * 255).astype(np.uint8)).save(output_path)


def image_graphite_tint(input_path: "str", output_path: "str", *, contrast: "float" = 1.28, steel_blue: "float" = 0.12) -> "None":
    """Graphite-style monochrome with crisp edges and a restrained steel tint."""
    from PIL import Image
    import numpy as np

    img = Image.open(input_path).convert("RGB")
    arr = np.array(img).astype(np.float32) / 255.0
    lum = 0.299 * arr[:, :, 0] + 0.587 * arr[:, :, 1] + 0.114 * arr[:, :, 2]
    gx = np.roll(lum, -1, axis=1) - np.roll(lum, 1, axis=1)
    gy = np.roll(lum, -1, axis=0) - np.roll(lum, 1, axis=0)
    edges = np.clip(np.sqrt(gx * gx + gy * gy) * 2.6, 0.0, 1.0)
    tone = np.clip((lum - 0.5) * contrast + 0.5, 0.0, 1.0)
    graphite = np.stack([tone * 0.90, tone * 0.93 + steel_blue * 0.25, tone + steel_blue], axis=-1)
    result = np.clip(graphite * (1.0 - edges[:, :, None] * 0.22), 0.0, 1.0)
    Image.fromarray((result * 255).astype(np.uint8)).save(output_path)


def image_copper_patina(input_path: "str", output_path: "str", *, patina: "float" = 0.22, oxidation: "float" = 0.16) -> "None":
    """Copper-patina treatment with warm metal mids and oxidized teal shadows."""
    from PIL import Image
    import numpy as np

    img = Image.open(input_path).convert("RGB")
    arr = np.array(img).astype(np.float32) / 255.0
    lum = 0.299 * arr[:, :, 0] + 0.587 * arr[:, :, 1] + 0.114 * arr[:, :, 2]
    shadow_mask = np.clip((0.58 - lum) / 0.58, 0.0, 1.0)[:, :, None]
    highlight_mask = np.clip((lum - 0.45) / 0.55, 0.0, 1.0)[:, :, None]
    copper = arr.copy()
    copper[:, :, 0] *= 1.0 + patina * 0.32
    copper[:, :, 1] *= 1.0 + patina * 0.10
    copper[:, :, 2] *= 1.0 - patina * 0.18
    teal = np.array([0.20, 0.58, 0.54], dtype=np.float32)
    result = np.clip(copper * (1.0 - shadow_mask * oxidation) + teal * shadow_mask * oxidation + highlight_mask * np.array([0.92, 0.72, 0.48], dtype=np.float32) * 0.10, 0.0, 1.0)
    Image.fromarray((result * 255).astype(np.uint8)).save(output_path)


def image_lucid_dream(input_path: "str", output_path: "str", *, softness: "float" = 0.26, hue_drift: "float" = 0.18) -> "None":
    """Dreamlike pastel wash with softened contrast and a drifting cool-magenta tint."""
    from PIL import Image
    import numpy as np

    img = Image.open(input_path).convert("RGB")
    arr = np.array(img).astype(np.float32) / 255.0
    blur = arr.copy()
    for _ in range(2):
        blur = (
            blur
            + np.roll(blur, 1, axis=0)
            + np.roll(blur, -1, axis=0)
            + np.roll(blur, 1, axis=1)
            + np.roll(blur, -1, axis=1)
        ) / 5.0
    pastel = np.sqrt(np.clip(blur + 0.02, 0.0, 1.0))
    tint = np.array([1.0, 0.90 + hue_drift * 0.18, 1.0 - hue_drift * 0.12], dtype=np.float32)
    result = np.clip(arr * (1.0 - softness) + pastel * softness, 0.0, 1.0)
    result = np.clip(result * (1.0 - hue_drift * 0.18) + tint * hue_drift * 0.16, 0.0, 1.0)
    Image.fromarray((result * 255).astype(np.uint8)).save(output_path)


def image_liquid_chrome(input_path: "str", output_path: "str", *, ripple: "float" = 0.18) -> "None":
    """Reflective liquid-chrome finish with warped highlights and cool metal tint."""
    from PIL import Image
    import numpy as np

    ripple = max(0.02, min(0.45, ripple))
    img = Image.open(input_path).convert("RGB")
    arr = np.array(img).astype(np.float32) / 255.0
    h, w = arr.shape[:2]
    yy, xx = np.indices((h, w), dtype=np.float32)
    wave_x = np.sin(yy * 0.055) * ripple * 18.0 + np.cos(yy * 0.023) * ripple * 10.0
    wave_y = np.cos(xx * 0.047) * ripple * 14.0
    src_x = np.clip(np.rint(xx + wave_x), 0, w - 1).astype(np.int32)
    src_y = np.clip(np.rint(yy + wave_y), 0, h - 1).astype(np.int32)
    warped = arr[src_y, src_x]
    lum = 0.299 * warped[:, :, 0] + 0.587 * warped[:, :, 1] + 0.114 * warped[:, :, 2]
    highlight = np.clip((lum - 0.36) / 0.64, 0.0, 1.0)[:, :, None]
    chrome = np.power(np.clip(warped + 0.02, 0.0, 1.0), 0.78)
    chrome[:, :, 0] *= 1.05
    chrome[:, :, 1] *= 1.08
    chrome[:, :, 2] *= 1.14
    specular = highlight * np.array([0.90, 0.97, 1.0], dtype=np.float32) * (0.18 + ripple * 0.65)
    result = np.clip(arr * 0.36 + chrome * 0.58 + specular, 0.0, 1.0)
    Image.fromarray((result * 255).astype(np.uint8)).save(output_path)


def image_celestial_haze(input_path: "str", output_path: "str", *, aura: "float" = 0.22) -> "None":
    """Soft celestial haze with lifted highlights and a faint blue-rose atmosphere."""
    from PIL import Image
    import numpy as np

    aura = max(0.03, min(0.4, aura))
    img = Image.open(input_path).convert("RGB")
    arr = np.array(img).astype(np.float32) / 255.0
    glow = arr.copy()
    for _ in range(3):
        glow = (
            glow
            + np.roll(glow, 1, axis=0)
            + np.roll(glow, -1, axis=0)
            + np.roll(glow, 1, axis=1)
            + np.roll(glow, -1, axis=1)
        ) / 5.0
    lum = 0.299 * glow[:, :, 0] + 0.587 * glow[:, :, 1] + 0.114 * glow[:, :, 2]
    halo = np.clip((lum - 0.30) / 0.70, 0.0, 1.0)[:, :, None]
    tint = np.array([0.98, 0.93, 1.0], dtype=np.float32)
    nebula = glow.copy()
    nebula[:, :, 0] *= 1.0 + aura * 0.10
    nebula[:, :, 1] *= 1.0 - aura * 0.06
    nebula[:, :, 2] *= 1.0 + aura * 0.26
    result = np.clip(arr * (1.0 - aura * 0.28) + nebula * aura * 0.72 + halo * tint * aura * 0.42, 0.0, 1.0)
    Image.fromarray((result * 255).astype(np.uint8)).save(output_path)


def image_ink_bleed(input_path: "str", output_path: "str", *, spread: "float" = 0.18) -> "None":
    """Illustrative ink-bleed wash with softened pigment edges and paper-like pooling."""
    from PIL import Image
    import numpy as np

    spread = max(0.04, min(0.35, spread))
    img = Image.open(input_path).convert("RGB")
    arr = np.array(img).astype(np.float32) / 255.0
    smear = arr.copy()
    for _ in range(2):
        smear = (
            smear * 0.42
            + np.roll(smear, 1, axis=0) * 0.14
            + np.roll(smear, -1, axis=0) * 0.14
            + np.roll(smear, 1, axis=1) * 0.15
            + np.roll(smear, -1, axis=1) * 0.15
        )
    lum = 0.299 * arr[:, :, 0] + 0.587 * arr[:, :, 1] + 0.114 * arr[:, :, 2]
    edge_x = np.roll(lum, -1, axis=1) - np.roll(lum, 1, axis=1)
    edge_y = np.roll(lum, -1, axis=0) - np.roll(lum, 1, axis=0)
    edges = np.clip(np.sqrt(edge_x * edge_x + edge_y * edge_y) * (1.8 + spread * 2.5), 0.0, 1.0)[:, :, None]
    ink = np.power(np.clip(smear, 0.0, 1.0), 0.92)
    paper = np.array([0.96, 0.94, 0.90], dtype=np.float32)
    result = np.clip(paper * 0.18 + ink * 0.82 - edges * spread * 0.22, 0.0, 1.0)
    Image.fromarray((result * 255).astype(np.uint8)).save(output_path)


def image_arctic_glow(input_path: "str", output_path: "str", *, frost: "float" = 0.20) -> "None":
    """Cold arctic glow with icy highlights, pale cyan mids, and softened contrast."""
    from PIL import Image
    import numpy as np

    frost = max(0.03, min(0.38, frost))
    img = Image.open(input_path).convert("RGB")
    arr = np.array(img).astype(np.float32) / 255.0
    veil = arr.copy()
    for _ in range(2):
        veil = (
            veil
            + np.roll(veil, 1, axis=0)
            + np.roll(veil, -1, axis=0)
            + np.roll(veil, 1, axis=1)
            + np.roll(veil, -1, axis=1)
        ) / 5.0
    lum = 0.299 * veil[:, :, 0] + 0.587 * veil[:, :, 1] + 0.114 * veil[:, :, 2]
    ice_mask = np.clip((lum - 0.28) / 0.72, 0.0, 1.0)[:, :, None]
    cool = veil.copy()
    cool[:, :, 0] *= 0.90
    cool[:, :, 1] *= 1.02
    cool[:, :, 2] *= 1.18
    ice = np.array([0.86, 0.96, 1.0], dtype=np.float32)
    result = np.clip(arr * (1.0 - frost * 0.24) + cool * frost * 0.74 + ice_mask * ice * frost * 0.34, 0.0, 1.0)
    Image.fromarray((result * 255).astype(np.uint8)).save(output_path)


def image_saffron_bloom(input_path: "str", output_path: "str", *, warmth: "float" = 0.18) -> "None":
    """Saffron bloom with warm highlights and gently lifted golden mids."""
    from PIL import Image
    import numpy as np

    warmth = max(0.03, min(0.35, warmth))
    img = Image.open(input_path).convert("RGB")
    arr = np.array(img).astype(np.float32) / 255.0
    glow = arr.copy()
    for _ in range(2):
        glow = (
            glow
            + np.roll(glow, 1, axis=0)
            + np.roll(glow, -1, axis=0)
            + np.roll(glow, 1, axis=1)
            + np.roll(glow, -1, axis=1)
        ) / 5.0
    lum = 0.299 * glow[:, :, 0] + 0.587 * glow[:, :, 1] + 0.114 * glow[:, :, 2]
    highlight = np.clip((lum - 0.34) / 0.66, 0.0, 1.0)[:, :, None]
    warm = glow.copy()
    warm[:, :, 0] *= 1.0 + warmth * 0.24
    warm[:, :, 1] *= 1.0 + warmth * 0.12
    warm[:, :, 2] *= 1.0 - warmth * 0.16
    tint = np.array([1.0, 0.82, 0.42], dtype=np.float32)
    result = np.clip(arr * (1.0 - warmth * 0.20) + warm * warmth * 0.72 + highlight * tint * warmth * 0.32, 0.0, 1.0)
    Image.fromarray((result * 255).astype(np.uint8)).save(output_path)


def image_glacier_mist(input_path: "str", output_path: "str", *, haze: "float" = 0.20) -> "None":
    """Glacial mist with frosted blues and softened contrast in the highlights."""
    from PIL import Image
    import numpy as np

    haze = max(0.03, min(0.36, haze))
    img = Image.open(input_path).convert("RGB")
    arr = np.array(img).astype(np.float32) / 255.0
    veil = arr.copy()
    for _ in range(3):
        veil = (
            veil
            + np.roll(veil, 1, axis=0)
            + np.roll(veil, -1, axis=0)
            + np.roll(veil, 1, axis=1)
            + np.roll(veil, -1, axis=1)
        ) / 5.0
    cool = veil.copy()
    cool[:, :, 0] *= 0.90
    cool[:, :, 1] *= 1.00 + haze * 0.10
    cool[:, :, 2] *= 1.0 + haze * 0.24
    lum = 0.299 * cool[:, :, 0] + 0.587 * cool[:, :, 1] + 0.114 * cool[:, :, 2]
    mist = np.clip((lum - 0.25) / 0.75, 0.0, 1.0)[:, :, None]
    result = np.clip(arr * (1.0 - haze * 0.24) + cool * haze * 0.70 + mist * np.array([0.88, 0.96, 1.0], dtype=np.float32) * haze * 0.28, 0.0, 1.0)
    Image.fromarray((result * 255).astype(np.uint8)).save(output_path)


def image_rose_quartz(input_path: "str", output_path: "str", *, blush: "float" = 0.17) -> "None":
    """Rose-quartz finish with pink mineral warmth and softened luminance rolloff."""
    from PIL import Image
    import numpy as np

    blush = max(0.03, min(0.32, blush))
    img = Image.open(input_path).convert("RGB")
    arr = np.array(img).astype(np.float32) / 255.0
    lum = 0.299 * arr[:, :, 0] + 0.587 * arr[:, :, 1] + 0.114 * arr[:, :, 2]
    blush_mask = np.clip((lum - 0.18) / 0.82, 0.0, 1.0)[:, :, None]
    stone = arr.copy()
    stone[:, :, 0] *= 1.0 + blush * 0.18
    stone[:, :, 1] *= 1.0 - blush * 0.03
    stone[:, :, 2] *= 1.0 + blush * 0.06
    crystal = np.array([0.98, 0.82, 0.90], dtype=np.float32)
    result = np.clip(stone * (1.0 - blush * 0.10) + blush_mask * crystal * blush * 0.24, 0.0, 1.0)
    Image.fromarray((result * 255).astype(np.uint8)).save(output_path)


def image_shadow_teal(input_path: "str", output_path: "str", *, depth: "float" = 0.19) -> "None":
    """Shadow-teal grade with cooler blacks and restrained cyan-toned contrast."""
    from PIL import Image
    import numpy as np

    depth = max(0.03, min(0.34, depth))
    img = Image.open(input_path).convert("RGB")
    arr = np.array(img).astype(np.float32) / 255.0
    lum = 0.299 * arr[:, :, 0] + 0.587 * arr[:, :, 1] + 0.114 * arr[:, :, 2]
    shadow_mask = np.clip((0.62 - lum) / 0.62, 0.0, 1.0)[:, :, None]
    cool = arr.copy()
    cool[:, :, 0] *= 0.88
    cool[:, :, 1] *= 1.0 + depth * 0.10
    cool[:, :, 2] *= 1.0 + depth * 0.26
    teal = np.array([0.18, 0.54, 0.56], dtype=np.float32)
    result = np.clip(arr * (1.0 - shadow_mask * depth * 0.20) + cool * depth * 0.34 + teal * shadow_mask * depth * 0.26, 0.0, 1.0)
    Image.fromarray((result * 255).astype(np.uint8)).save(output_path)
