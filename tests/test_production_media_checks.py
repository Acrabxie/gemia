from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from gemia.compat import ffmpeg_path
from gemia.production_media_checks import inspect_video_motion


def _render(path: Path, source: str, *, duration: float = 2.0) -> Path:
    command = [
        ffmpeg_path(),
        "-y",
        "-v",
        "error",
        "-f",
        "lavfi",
        "-i",
        source,
        "-t",
        str(duration),
        "-r",
        "30",
        "-pix_fmt",
        "yuv420p",
        str(path),
    ]
    subprocess.run(command, check=True, capture_output=True)
    return path


def test_motion_check_rejects_still_container(tmp_path: Path) -> None:
    still = _render(tmp_path / "still.mp4", "color=c=blue:s=320x180:r=30")
    result = inspect_video_motion(still)
    assert result["real_motion_verified"] is False
    assert result["unique_frame_count"] == 1


def test_motion_check_accepts_observable_video_motion(tmp_path: Path) -> None:
    moving = _render(tmp_path / "moving.mp4", "testsrc2=s=320x180:r=30")
    result = inspect_video_motion(moving)
    assert result["real_motion_verified"] is True
    assert result["motion_pair_ratio"] >= 0.15


def test_motion_check_rejects_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        inspect_video_motion(tmp_path / "missing.mp4")
