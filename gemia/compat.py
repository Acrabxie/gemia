"""Cross-platform compatibility layer for Lumeri.

Resolves bundled binary paths (ffmpeg, ffprobe), data directories (models),
and user-data locations so the same codebase works whether running from:
  - a development venv on macOS (current)
  - a PyInstaller/Nuitka frozen exe on Windows
  - a packaged .app/.dmg on macOS (Electron + embedded Python)

Resolution order for binaries (e.g. ffmpeg):
  1. LUMERI_FFMPEG_PATH env var (explicit override)
  2. Bundled location next to the frozen exe / app resources
  3. shutil.which() on system PATH

Resolution order for data files (e.g. u2net model):
  1. LUMERI_DATA_DIR env var
  2. <app-bundle>/data/  (frozen builds)
  3. <repo-root>/models/ (dev mode)
"""
from __future__ import annotations

import os
import shutil
import sys
from functools import lru_cache
from pathlib import Path

__all__ = [
    "is_frozen",
    "is_windows",
    "is_macos",
    "app_dir",
    "data_dir",
    "user_data_dir",
    "resolve_binary",
    "ffmpeg_path",
    "ffprobe_path",
]


def is_frozen() -> bool:
    return getattr(sys, "frozen", False)


def is_windows() -> bool:
    return sys.platform == "win32"


def is_macos() -> bool:
    return sys.platform == "darwin"


@lru_cache(maxsize=1)
def app_dir() -> Path:
    """Root directory of the application bundle or repo."""
    if is_frozen():
        # PyInstaller sets sys._MEIPASS; Nuitka uses __compiled__
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return Path(meipass)
        return Path(sys.executable).parent
    # Dev mode: repo root is two levels up from gemia/compat.py
    return Path(__file__).resolve().parent.parent


@lru_cache(maxsize=1)
def data_dir() -> Path:
    """Directory containing bundled data files (models, assets)."""
    env = os.environ.get("LUMERI_DATA_DIR")
    if env:
        return Path(env)
    if is_frozen():
        return app_dir() / "data"
    return app_dir() / "models"


@lru_cache(maxsize=1)
def user_data_dir() -> Path:
    """Per-user writable data directory (sessions, cache, config)."""
    env = os.environ.get("LUMERI_USER_DATA")
    if env:
        p = Path(env)
        p.mkdir(parents=True, exist_ok=True)
        return p
    if is_windows():
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        p = base / "Lumeri"
    elif is_macos():
        p = Path.home() / "Library" / "Application Support" / "Lumeri"
    else:
        p = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "lumeri"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _bundled_binary_dir() -> Path:
    """Directory where bundled executables (ffmpeg, ffprobe) live."""
    if is_frozen():
        return app_dir() / "bin"
    # Electron wrapper may set this to point at its unpacked ffmpeg-static
    env = os.environ.get("LUMERI_BIN_DIR")
    if env:
        return Path(env)
    return app_dir() / "bin"


def resolve_binary(name: str, env_override: str | None = None) -> str:
    """Resolve a binary by name, returning the full path or bare name as fallback.

    Args:
        name: Binary name without extension (e.g. "ffmpeg").
        env_override: Env var name that, if set, provides the full path directly.

    Returns:
        Absolute path to the binary if found; bare name otherwise (relies on PATH).
    """
    # 1. Explicit env override
    if env_override:
        env_val = os.environ.get(env_override)
        if env_val and Path(env_val).is_file():
            return env_val

    exe_name = f"{name}.exe" if is_windows() else name

    # 2. Bundled location
    bundled = _bundled_binary_dir() / exe_name
    if bundled.is_file():
        return str(bundled)

    # 3. System PATH
    found = shutil.which(exe_name)
    if found:
        return found

    # 4. Fallback: bare name (subprocess will search PATH at runtime)
    return exe_name


@lru_cache(maxsize=1)
def ffmpeg_path() -> str:
    """Resolved path to the ffmpeg binary."""
    return resolve_binary("ffmpeg", env_override="LUMERI_FFMPEG_PATH")


@lru_cache(maxsize=1)
def ffprobe_path() -> str:
    """Resolved path to the ffprobe binary."""
    return resolve_binary("ffprobe", env_override="LUMERI_FFPROBE_PATH")
