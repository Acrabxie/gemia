"""Video compositing: overlay, add_audio_track."""
from __future__ import annotations

import subprocess
from pathlib import Path


def _run(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(
            f"ffmpeg failed: {' '.join(cmd)}\nSTDERR:\n{proc.stderr}"
        )


def overlay(base_path: str, overlay_path: str, output_path: str, *,
            x: int = 0, y: int = 0, start_sec: float = 0.0,
            end_sec: float | None = None) -> str:
    """Overlay a video/image on top of a base video.

    Args:
        base_path: Background video.
        overlay_path: Foreground video or image.
        output_path: Destination.
        x: Horizontal offset of overlay.
        y: Vertical offset of overlay.
        start_sec: When the overlay appears (seconds).
        end_sec: When the overlay disappears.  ``None`` = until end.

    Returns:
        The *output_path*.
    """
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    enable = f"between(t,{start_sec},{end_sec})" if end_sec else f"gte(t,{start_sec})"
    _run([
        "ffmpeg", "-y",
        "-i", base_path,
        "-i", overlay_path,
        "-filter_complex",
        f"[1:v]format=yuva420p[ovr];[0:v][ovr]overlay={x}:{y}:enable='{enable}'[v]",
        "-map", "[v]", "-map", "0:a?",
        "-c:v", "libx264", "-c:a", "aac",
        output_path,
    ])
    return output_path


def add_audio_track(video_path: str, audio_path: str, output_path: str, *,
                    replace: bool = False, volume: float = 1.0) -> str:
    """Add an audio track to a video.

    Args:
        video_path: Input video.
        audio_path: Audio file to add (wav, mp3, aac, etc.).
        output_path: Destination.
        replace: If True, replace existing audio.  If False, mix with
            the original audio.
        volume: Volume multiplier for the new audio track.

    Returns:
        The *output_path*.
    """
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    if replace:
        _run([
            "ffmpeg", "-y",
            "-i", video_path,
            "-i", audio_path,
            "-c:v", "copy",
            "-filter_complex", f"[1:a]volume={volume}[a]",
            "-map", "0:v", "-map", "[a]",
            "-c:a", "aac", "-shortest",
            output_path,
        ])
    else:
        _run([
            "ffmpeg", "-y",
            "-i", video_path,
            "-i", audio_path,
            "-filter_complex",
            f"[1:a]volume={volume}[bg];[0:a][bg]amix=inputs=2:duration=first[a]",
            "-map", "0:v", "-map", "[a]",
            "-c:v", "copy", "-c:a", "aac",
            output_path,
        ])
    return output_path


# ---------------------------------------------------------------------------
# object_remove
# ---------------------------------------------------------------------------
def object_remove(input_path: str, output_path: str, *, mask: str | None = None) -> str:
    """Remove objects from video using ffmpeg removelogo or blur inpainting.

    Note: Production-quality removal requires external tools (Runway, Adobe).
    This provides a best-effort ffmpeg approximation.

    Args:
        input_path: Source video.
        output_path: Destination video.
        mask: Path to binary mask image (white=remove, black=keep).
              If None, applies full-frame blur as placeholder.

    Returns:
        output_path
    """
    if mask:
        _run([
            "ffmpeg", "-y",
            "-i", input_path,
            "-i", mask,
            "-lavfi", f"removelogo={mask}",
            "-c:a", "copy",
            output_path,
        ])
    else:
        _run([
            "ffmpeg", "-y",
            "-i", input_path,
            "-vf", "boxblur=10:1",
            "-c:a", "copy",
            output_path,
        ])
    return output_path


# ---------------------------------------------------------------------------
# background_replace
# ---------------------------------------------------------------------------
def background_replace(
    input_path: str,
    output_path: str,
    *,
    bg: str,
    method: str = "chroma",
) -> str:
    """Replace video background using chroma or luma key.

    Args:
        input_path: Foreground video (green/blue screen or white background).
        output_path: Destination video.
        bg: Path to replacement background image or video.
        method: ``"chroma"`` (green screen) or ``"luma"`` (white background).

    Returns:
        output_path
    """
    from pathlib import Path as _Path
    bg_ext = _Path(bg).suffix.lower()
    is_bg_image = bg_ext in {".jpg", ".jpeg", ".png", ".bmp", ".tiff"}

    if is_bg_image:
        bg_inputs = ["-loop", "1", "-i", bg]
        shortest = ["-shortest"]
    else:
        bg_inputs = ["-i", bg]
        shortest = []

    if method == "chroma":
        filtergraph = "[1:v][0:v]scale2ref[bg][fg];[fg]chromakey=0x00ff00:0.1:0.2[fgkey];[bg][fgkey]overlay"
    else:
        filtergraph = "[1:v][0:v]scale2ref[bg][fg];[fg]lumakey=0.0:0.1:0.1[fgkey];[bg][fgkey]overlay"

    _run([
        "ffmpeg", "-y",
        "-i", input_path,
        *bg_inputs,
        "-filter_complex", filtergraph,
        "-c:a", "copy",
        *shortest,
        output_path,
    ])
    return output_path


# ---------------------------------------------------------------------------
# stereo_3d_align  (#46)
# ---------------------------------------------------------------------------
def stereo_3d_align(
    left_path: str,
    right_path: str,
    output_path: str,
    *,
    convergence_offset: int = 0,
    format: str = "sbs",
) -> str:
    """Create stereoscopic 3D output from left/right eye videos.

    Args:
        left_path: Left-eye video path.
        right_path: Right-eye video path.
        output_path: Destination video path.
        convergence_offset: Pixels to shift right eye horizontally.
        format: ``"sbs"`` (side-by-side), ``"anaglyph"`` (red-cyan),
                or ``"ou"`` (over-under).

    Returns:
        output_path
    """
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    # Build right-eye stream (with optional convergence crop + pad to restore width)
    if convergence_offset != 0:
        offset = convergence_offset
        crop_x = offset if offset > 0 else 0
        pad_x = 0 if offset > 0 else abs(offset)
        right_stream = (
            f"[1:v]crop=iw-{abs(offset)}:ih:{crop_x}:0,"
            f"pad=iw+{abs(offset)}:ih:{pad_x}:0[right]"
        )
        right_label = "[right]"
    else:
        right_stream = None
        right_label = "[1:v]"

    left_label = "[0:v]"

    if format == "sbs":
        if right_stream:
            fc = f"{right_stream};{left_label}{right_label}hstack[v]"
        else:
            fc = f"{left_label}{right_label}hstack[v]"
    elif format == "anaglyph":
        if right_stream:
            fc = (
                f"{right_stream};"
                f"{left_label}lutrgb=g=0:b=0[r];"
                f"{right_label}lutrgb=r=0[c];"
                f"[r][c]blend=all_mode=addition[v]"
            )
        else:
            fc = (
                f"{left_label}lutrgb=g=0:b=0[r];"
                f"{right_label}lutrgb=r=0[c];"
                f"[r][c]blend=all_mode=addition[v]"
            )
    elif format == "ou":
        if right_stream:
            fc = f"{right_stream};{left_label}{right_label}vstack[v]"
        else:
            fc = f"{left_label}{right_label}vstack[v]"
    else:
        raise ValueError(f"Unknown format '{format}'. Choose from: sbs, anaglyph, ou")

    _run([
        "ffmpeg", "-y",
        "-i", left_path,
        "-i", right_path,
        "-filter_complex", fc,
        "-map", "[v]", "-map", "0:a?",
        "-c:v", "libx264",
        "-c:a", "aac", "-ac", "2",
        "-shortest",
        output_path,
    ])
    return output_path


# ---------------------------------------------------------------------------
# #58  depth_mask
# ---------------------------------------------------------------------------
def depth_mask(
    input_path: str,
    output_path: str,
    *,
    fg_blur: int = 0,
    bg_blur: int = 15,
    depth_threshold: float = 0.5,
) -> str:
    """Simulate depth-of-field by blurring the background based on a simple
    luminance-based depth proxy (bright = near, dark = far).

    Mirrors DaVinci Resolve 19's *Magic Mask → Depth Map* workflow with a
    luminance-threshold approximation using ffmpeg.

    Args:
        input_path: Source video or image path.
        output_path: Destination path.
        fg_blur: Blur radius for foreground (bright/near) region. 0 = sharp.
        bg_blur: Blur radius for background (dark/far) region. Default 15.
        depth_threshold: Luminance threshold [0, 1] separating FG/BG.
            Default 0.5.

    Returns:
        output_path
    """
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    t = int(depth_threshold * 255)
    fb = max(1, fg_blur) if fg_blur > 0 else 0
    bb = max(1, bg_blur)

    # Build mask: pixels brighter than threshold → white (FG), else black (BG)
    # Foreground: original (or lightly blurred)
    # Background: heavily blurred
    # Composite: mask blends FG over BG
    fg_stream = f"[0:v]{'boxblur=' + str(fb) if fb else 'copy'}[fg]"
    bg_stream = f"[0:v]boxblur={bb}[bg]"
    mask_stream = (
        f"[0:v]geq="
        f"lum='if(gt(lum(X,Y),{t}),255,0)':"
        f"cb=128:cr=128[mask]"
    )
    composite = (
        "[bg][fg]blend=all_expr='A*(1-ALPHA/255)+B*(ALPHA/255)'[out]"
        if False  # placeholder; use overlay with alphaextract below
        else "[fg][mask]alphamerge[fga];[bg][fga]overlay[out]"
    )

    fc = f"{bg_stream};{fg_stream};{mask_stream};{composite}"
    _run([
        "ffmpeg", "-y", "-i", input_path,
        "-filter_complex", fc,
        "-map", "[out]", "-map", "0:a?",
        "-c:v", "libx264", "-c:a", "copy",
        output_path,
    ])
    return output_path


# ---------------------------------------------------------------------------
# #52  lut_apply
# ---------------------------------------------------------------------------
def lut_apply(input_path: str, output_path: str, *, lut_path: str, intensity: float = 1.0) -> str:
    """Apply a .cube LUT file to a video using ffmpeg lut3d filter.

    Args:
        input_path: Source video or image.
        output_path: Destination.
        lut_path: Path to a .cube LUT file.
        intensity: Blend strength [0, 1]. 1 = full LUT, 0 = passthrough.

    Returns:
        output_path
    """
    intensity = max(0.0, min(1.0, intensity))
    if intensity >= 0.999:
        vf = f"lut3d=file='{lut_path}'"
    else:
        # Blend with original using mix
        vf = f"split[a][b];[b]lut3d=file='{lut_path}'[c];[a][c]blend=all_expr='A*{1-intensity}+B*{intensity}'"
        _run([
            "ffmpeg", "-y", "-i", input_path,
            "-filter_complex", vf,
            "-c:a", "copy",
            output_path,
        ])
        return output_path

    _run([
        "ffmpeg", "-y", "-i", input_path,
        "-vf", vf,
        "-c:a", "copy",
        output_path,
    ])
    return output_path


# ---------------------------------------------------------------------------
# #53  vhs_effect
# ---------------------------------------------------------------------------
def vhs_effect(input_path: str, output_path: str, *, strength: float = 0.5) -> str:
    """Apply VHS/retro aesthetic (color bleed, scan lines, noise, desaturation).

    Args:
        input_path: Source video.
        output_path: Destination video.
        strength: Effect intensity [0, 1]. Default 0.5.

    Returns:
        output_path
    """
    s = max(0.0, min(1.0, strength))
    noise_level = int(s * 25)
    sat = 1.0 - s * 0.3          # slight desaturation
    contrast = 1.0 + s * 0.1

    # Scanlines via geq: darken every other row slightly
    scanline = f"geq=lum='lum(X,Y)*if(mod(Y,2),1,{1.0 - s*0.12})':cb='cb(X,Y)':cr='cr(X,Y)'"

    vf = ",".join([
        # 1. Color bleed: slight horizontal chroma shift via hue+eq
        f"hue=s={sat:.2f}",
        f"eq=contrast={contrast:.2f}",
        # 2. Noise
        f"noise=alls={noise_level}:allf=t+u",
        # 3. Scan lines
        scanline,
        # 4. Slight vignette
        f"vignette=PI/{int(3 + (1-s)*4)}",
    ])

    _run([
        "ffmpeg", "-y", "-i", input_path,
        "-vf", vf,
        "-c:a", "copy",
        output_path,
    ])
    return output_path


# ---------------------------------------------------------------------------
# #54  chroma_aberration
# ---------------------------------------------------------------------------
def chroma_aberration(input_path: str, output_path: str, *, strength: int = 2) -> str:
    """Add chromatic aberration (RGB channel split) effect.

    Args:
        input_path: Source video or image.
        output_path: Destination.
        strength: Pixel offset for colour channel separation. Default 2.

    Returns:
        output_path
    """
    s = max(1, int(strength))
    # Split into R/G/B channels, shift R left and B right, then merge
    fc = (
        f"[0:v]split=3[r][g][b];"
        f"[r]lutrgb=g=0:b=0,pad=iw+{s*2}:ih:{s}:0[rs];"
        f"[g]lutrgb=r=0:b=0,pad=iw+{s*2}:ih:{s}:0[gs];"
        f"[b]lutrgb=r=0:g=0,pad=iw+{s*2}:ih:0:0[bs];"
        f"[rs][gs]blend=all_mode=addition[rg];"
        f"[rg][bs]blend=all_mode=addition[rgb];"
        f"[rgb]crop=iw-{s*2}:ih:{s}:0[out]"
    )
    _run([
        "ffmpeg", "-y", "-i", input_path,
        "-filter_complex", fc,
        "-map", "[out]", "-map", "0:a?",
        "-c:a", "copy",
        output_path,
    ])
    return output_path


# ---------------------------------------------------------------------------
# #55  zoom_pan
# ---------------------------------------------------------------------------
def zoom_pan(
    input_path: str,
    output_path: str,
    *,
    start_zoom: float = 1.0,
    end_zoom: float = 1.3,
    x: float = 0.5,
    y: float = 0.5,
) -> str:
    """Ken Burns zoom-pan effect.

    Args:
        input_path: Source video or image.
        output_path: Destination.
        start_zoom: Starting zoom level (1.0 = original). Default 1.0.
        end_zoom: Ending zoom level. Default 1.3.
        x: Horizontal anchor [0, 1] (0=left, 0.5=centre, 1=right). Default 0.5.
        y: Vertical anchor [0, 1] (0=top, 0.5=centre, 1=bottom). Default 0.5.

    Returns:
        output_path
    """
    import subprocess as _sp
    # Probe duration and fps
    r = _sp.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=r_frame_rate,width,height",
         "-of", "csv=p=0", input_path],
        capture_output=True, text=True, check=True,
    )
    parts = r.stdout.strip().split(",")
    fps_raw = parts[0] if parts else "30/1"
    w = int(parts[1]) if len(parts) > 1 else 1280
    h = int(parts[2]) if len(parts) > 2 else 720
    fps_num, fps_den = map(int, fps_raw.split("/"))
    fps = fps_num / fps_den

    dur_r = _sp.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", input_path],
        capture_output=True, text=True, check=True,
    )
    dur = float(dur_r.stdout.strip())
    total_frames = int(dur * fps)

    sz = start_zoom
    ez = end_zoom
    # zoompan: z expr animates zoom, x/y keep anchor
    z_expr = f"'{sz}+({ez}-{sz})*on/{total_frames}'"
    px = f"'(iw-iw/zoom)*{x}'"
    py = f"'(ih-ih/zoom)*{y}'"

    vf = f"zoompan=z={z_expr}:x={px}:y={py}:d={total_frames}:s={w}x{h}:fps={fps}"

    _run([
        "ffmpeg", "-y", "-i", input_path,
        "-vf", vf,
        "-c:a", "copy",
        output_path,
    ])
    return output_path


# ---------------------------------------------------------------------------
# #56  color_wheels
# ---------------------------------------------------------------------------
def color_wheels(
    input_path: str,
    output_path: str,
    *,
    lift: tuple[float, float, float] = (0.0, 0.0, 0.0),
    gamma: tuple[float, float, float] = (1.0, 1.0, 1.0),
    gain: tuple[float, float, float] = (1.0, 1.0, 1.0),
) -> str:
    """3-way colour wheel adjustment (lift/gamma/gain = shadows/mids/highlights).

    Each parameter is an (R, G, B) tuple:
    - lift: additive offset for shadows [-0.5, 0.5]. Default (0, 0, 0).
    - gamma: midtone power curve [0.1, 4.0]. Default (1, 1, 1).
    - gain: multiplicative gain for highlights [0, 4]. Default (1, 1, 1).

    Returns:
        output_path
    """
    lr, lg, lb = lift
    gr, gg, gb = gamma
    nr, ng, nb = gain

    # Build curves expression per channel:
    # out = clamp((in * gain + lift) ^ (1/gamma), 0, 1)
    # ffmpeg colorlevels handles gain+lift; curves handles gamma
    color_levels = (
        f"colorlevels="
        f"rimin=0:rimax=1:romin={lr:.4f}:romax={nr:.4f},"
        # repeat for g and b via separate filter invocations isn't straightforward;
        # use colorchannelmixer for lift then curves for gamma
    )
    # Simpler: use geq per channel
    def _ch(lift_v, gamma_v, gain_v):
        # clamp((X/255 * gain + lift)^(1/gamma) * 255, 0, 255)
        expr = f"clip(pow(clip(val*{gain_v:.4f}+{lift_v:.4f},0,1),1/{gamma_v:.4f})*255,0,255)"
        return expr

    vf = (
        f"geq="
        f"r='{_ch(lr,gr,nr)}':"
        f"g='{_ch(lg,gg,ng)}':"
        f"b='{_ch(lb,gb,nb)}'"
    )

    _run([
        "ffmpeg", "-y", "-i", input_path,
        "-vf", vf,
        "-c:a", "copy",
        output_path,
    ])
    return output_path


# ---------------------------------------------------------------------------
# picture_in_picture
# ---------------------------------------------------------------------------

def picture_in_picture(
    main_path: str,
    overlay_path: str,
    output_path: str,
    *,
    position: str = "top_right",
    scale: float = 0.25,
    margin: int = 20,
    start_sec: float = 0.0,
    end_sec: float | None = None,
) -> str:
    """Composite a picture-in-picture overlay onto a main video.

    Args:
        main_path: Background/main video file.
        overlay_path: PiP clip to overlay.
        output_path: Destination video file.
        position: Corner placement — ``"top_left"``, ``"top_right"``,
            ``"bottom_left"``, ``"bottom_right"``, or ``"center"``.
        scale: PiP size as a fraction of the main video width (0.0–1.0).
        margin: Pixel gap from the edge of the frame.
        start_sec: When (in main video seconds) the PiP appears.
        end_sec: When the PiP disappears (``None`` = until overlay ends).

    Returns:
        The *output_path*.
    """
    from pathlib import Path as _Path
    _Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    scale_vf = f"scale=iw*{scale:.4f}:-2"

    _pos_map = {
        "top_left":     f"{margin}:{margin}",
        "top_right":    f"main_w-overlay_w-{margin}:{margin}",
        "bottom_left":  f"{margin}:main_h-overlay_h-{margin}",
        "bottom_right": f"main_w-overlay_w-{margin}:main_h-overlay_h-{margin}",
        "center":       "(main_w-overlay_w)/2:(main_h-overlay_h)/2",
    }
    xy = _pos_map.get(position, _pos_map["top_right"])

    if end_sec is not None:
        enable = f":enable='between(t,{start_sec},{end_sec})'"
    else:
        enable = f":enable='gte(t,{start_sec})'"

    fc = (
        f"[1:v]{scale_vf}[pip];"
        f"[0:v][pip]overlay={xy}{enable}[v]"
    )
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries",
         "stream=codec_type", "-of", "csv=p=0", main_path],
        capture_output=True, text=True,
    )
    has_audio = bool(probe.stdout.strip())
    cmd = [
        "ffmpeg", "-y",
        "-i", main_path,
        "-i", overlay_path,
        "-filter_complex", fc,
        "-map", "[v]",
    ]
    if has_audio:
        cmd += ["-map", "0:a"]
    cmd += ["-c:v", "libx264", "-c:a", "aac", output_path]
    _run(cmd)
    return output_path
