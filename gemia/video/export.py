"""Export and proxy utilities for platform-optimized delivery."""
from __future__ import annotations

import subprocess
from pathlib import Path


def _run(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(
            f"ffmpeg failed: {' '.join(cmd)}\nSTDERR:\n{proc.stderr}"
        )


_PLATFORM_FLAGS: dict[str, list[str]] = {
    "youtube": [
        "-c:v", "libx264", "-crf", "18", "-preset", "slow",
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart", "-pix_fmt", "yuv420p",
    ],
    "instagram": [
        "-c:v", "libx264", "-crf", "23", "-preset", "medium",
        "-c:a", "aac", "-b:a", "128k",
        "-vf", "scale=1080:1080:force_original_aspect_ratio=decrease,"
               "pad=1080:1080:(ow-iw)/2:(oh-ih)/2",
        "-t", "60",
    ],
    "tiktok": [
        "-c:v", "libx264", "-crf", "23", "-preset", "medium",
        "-c:a", "aac", "-b:a", "128k",
        "-vf", "scale=1080:1920:force_original_aspect_ratio=decrease,"
               "pad=1080:1920:(ow-iw)/2:(oh-ih)/2",
        "-t", "180",
    ],
    "twitter": [
        "-c:v", "libx264", "-crf", "23", "-preset", "medium",
        "-c:a", "aac", "-b:a", "128k",
        "-t", "140", "-fs", "512M",
    ],
    "prores": [
        "-c:v", "prores_ks", "-profile:v", "3", "-c:a", "pcm_s16le",
    ],
    "gif": [
        "-vf", "fps=15,scale=480:-1:flags=lanczos,"
               "split[s0][s1];[s0]palettegen[p];[s1][p]paletteuse",
        "-an",
    ],
}


def export_preset(input_path: str, output_path: str, *, platform: str = "youtube") -> str:
    """Export video with platform-optimized ffmpeg settings.

    Args:
        input_path: Source video file.
        output_path: Destination file.
        platform: One of ``youtube``, ``instagram``, ``tiktok``, ``twitter``,
            ``prores``, or ``gif``.

    Returns:
        ``output_path``

    Raises:
        ValueError: If *platform* is not supported.
        RuntimeError: If ffmpeg exits with a non-zero return code.
    """
    if platform not in _PLATFORM_FLAGS:
        raise ValueError(
            f"Unsupported platform {platform!r}. "
            f"Choose from: {', '.join(_PLATFORM_FLAGS)}"
        )
    flags = _PLATFORM_FLAGS[platform]
    cmd = ["ffmpeg", "-y", "-i", input_path, *flags, output_path]
    _run(cmd)
    return output_path


def proxy_generate(input_path: str, output_path: str, *, resolution: int = 720) -> str:
    """Generate a low-resolution proxy file for offline editing.

    Args:
        input_path: Source video file.
        output_path: Destination file. If you want the default ``_proxy``
            naming, pass the desired path explicitly (e.g. via
            ``Path(input).stem + '_proxy' + Path(input).suffix``).
        resolution: Target height in pixels; width scales proportionally
            maintaining the original aspect ratio.

    Returns:
        ``output_path``

    Raises:
        RuntimeError: If ffmpeg exits with a non-zero return code.
    """
    cmd = [
        "ffmpeg", "-y", "-i", input_path,
        "-c:v", "libx264", "-crf", "28", "-preset", "ultrafast",
        "-c:a", "aac", "-b:a", "96k",
        "-vf", f"scale=-2:{resolution}",
        output_path,
    ]
    _run(cmd)
    return output_path


def batch_export(
    input_paths: list[str],
    output_dir: str,
    *,
    presets: list[str],
) -> dict[str, list[str]]:
    """Export multiple videos with multiple platform presets.

    For each (input, preset) combination, calls :func:`export_preset` and
    writes the result to *output_dir* as ``{stem}_{preset}{suffix}``.

    Runs sequentially to avoid CPU saturation.

    Args:
        input_paths: List of source video files.
        output_dir: Directory for all output files (created if absent).
        presets: List of platform names accepted by :func:`export_preset`.

    Returns:
        Dict mapping each preset name to the list of output paths produced
        for that preset.
    """
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    results: dict[str, list[str]] = {preset: [] for preset in presets}

    for preset in presets:
        for inp in input_paths:
            p = Path(inp)
            out_name = f"{p.stem}_{preset}{p.suffix}"
            out_path = str(out_dir / out_name)
            export_preset(inp, out_path, platform=preset)
            results[preset].append(out_path)

    return results
