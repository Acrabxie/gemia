from __future__ import annotations

from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]
BUILDER = ROOT / "scripts" / "build_opencv_lgpl_macos.sh"


checked = subprocess.run(["bash", "-n", str(BUILDER)], capture_output=True, text=True)
assert checked.returncode == 0, checked.stderr

builder = BUILDER.read_text(encoding="utf-8")
for required in (
    "verify_ffmpeg_lgpl_distribution.py",
    "FindFFMPEG.cmake",
    "CMAKE_MODULE_PATH",
    "BUILD_SHARED_LIBS=OFF",
    "BUILD_opencv_python3=ON",
    "OPENCV_ENABLE_NONFREE=OFF",
    "WITH_FFMPEG=ON",
    "OPENCV_FFMPEG_USE_FIND_PACKAGE=FFMPEG",
    "OPENCV_PYTHON3_INSTALL_PATH",
    "@loader_path/.dylibs",
    "libx264*.dylib",
    "libx265*.dylib",
    "imageio_ffmpeg/binaries",
    "env -i",
    "OpenCV output roots must be new",
):
    assert required in builder

for forbidden in (
    "OPENCV_ENABLE_NONFREE=ON",
    "WITH_FFMPEG=OFF",
    "--enable-gpl",
    "--enable-nonfree",
):
    assert forbidden not in builder
