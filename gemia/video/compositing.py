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
