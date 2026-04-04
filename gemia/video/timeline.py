"""Timeline operations: cut, concat, speed, reverse."""
from __future__ import annotations

import subprocess
import uuid
from pathlib import Path


def _run(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(
            f"ffmpeg failed: {' '.join(cmd)}\nSTDERR:\n{proc.stderr}"
        )


def cut(input_path: str, output_path: str, *,
        start_sec: float, end_sec: float) -> str:
    """Cut a segment from a video.

    Uses stream copy when possible for speed.

    Args:
        input_path: Source video.
        output_path: Destination.
        start_sec: Start time in seconds.
        end_sec: End time in seconds.

    Returns:
        The *output_path*.
    """
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    _run([
        "ffmpeg", "-y",
        "-ss", str(start_sec), "-to", str(end_sec),
        "-i", input_path,
        "-c:v", "libx264", "-c:a", "aac",
        output_path,
    ])
    return output_path


def concat(paths: list[str], output_path: str) -> str:
    """Concatenate multiple video files in order.

    Args:
        paths: Ordered list of input video paths.
        output_path: Destination.

    Returns:
        The *output_path*.
    """
    if not paths:
        raise ValueError("At least one input path is required.")
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    list_file = Path(output_path).parent / f".concat_{uuid.uuid4().hex[:8]}.txt"
    list_file.write_text(
        "\n".join(f"file '{Path(p).resolve()}'" for p in paths) + "\n"
    )
    try:
        _run([
            "ffmpeg", "-y", "-f", "concat", "-safe", "0",
            "-i", str(list_file),
            "-c:v", "libx264", "-c:a", "aac",
            output_path,
        ])
    finally:
        list_file.unlink(missing_ok=True)
    return output_path


def speed(input_path: str, output_path: str, *, factor: float) -> str:
    """Change playback speed.

    Args:
        input_path: Source video.
        output_path: Destination.
        factor: Speed multiplier. >1 = faster, <1 = slower.

    Returns:
        The *output_path*.
    """
    if factor <= 0:
        raise ValueError("factor must be > 0.")
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    vf = f"setpts={1/factor}*PTS"
    cmd = [
        "ffmpeg", "-y", "-i", input_path,
        "-filter:v", vf,
    ]
    # atempo only supports [0.5, 100.0]
    if 0.5 <= factor <= 100.0:
        cmd += ["-filter:a", f"atempo={factor}"]
    else:
        cmd += ["-an"]
    cmd += ["-c:v", "libx264", "-c:a", "aac", output_path]
    _run(cmd)
    return output_path


def reverse(input_path: str, output_path: str) -> str:
    """Reverse a video (and its audio).

    Args:
        input_path: Source video.
        output_path: Destination.

    Returns:
        The *output_path*.
    """
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    _run([
        "ffmpeg", "-y", "-i", input_path,
        "-vf", "reverse", "-af", "areverse",
        "-c:v", "libx264", "-c:a", "aac",
        output_path,
    ])
    return output_path
