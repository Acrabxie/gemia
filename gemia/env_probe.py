"""Dynamic environment probe for the Lumeri agent.

Why this exists
---------------
The v3 system prompt used to *statically* claim "Full access to NumPy/PIL/
OpenCV/pandas" and implicitly assume a ``python`` interpreter exists. On a
fresh VM that is simply wrong: ``python`` may not be on PATH (only
``python3``), and any of those packages may be missing. The agent then
writes ``build``/``run_shell`` code that fails on the first import.

This module gives the agent *real* awareness of its own interpreter and
installed dependencies, computed once per process and injected into the
system prompt each session.

Design rules
------------
- **Never raise.** Probing is best-effort. A missing package, a broken
  ``importlib.metadata`` entry, or an absent binary is reported as absent —
  it must not crash prompt assembly.
- **Cheap and cached.** The full probe (including a couple of subprocess
  ``--version`` calls) runs once; subsequent calls return the cached dict.
- **Honest.** ``None`` means "not installed / not found", never "assume
  it's there".
"""
from __future__ import annotations

import importlib
import importlib.metadata as _md
import platform
import shutil
import subprocess
import sys
from typing import Any

__all__ = [
    "probe_environment",
    "format_environment_summary",
    "clear_cache",
]


# The set of packages the creative agent actually reaches for in build/
# run_shell code. Each entry maps a *probe key* (the name used in the
# summary and tests) to the import module name and the distribution name
# used by importlib.metadata. We probe BOTH: importlib.metadata gives a
# clean version when the dist is installed; a real import is the ground
# truth for "can the agent actually `import` it" (e.g. cv2 vs opencv-python,
# PIL vs Pillow, where module name != distribution name).
#
#   (probe_key, import_name, distribution_name)
_RELEVANT_PACKAGES: tuple[tuple[str, str, str], ...] = (
    ("numpy", "numpy", "numpy"),
    ("cv2", "cv2", "opencv-python"),
    ("PIL", "PIL", "Pillow"),
    ("skimage", "skimage", "scikit-image"),
    ("moviepy", "moviepy", "moviepy"),
    ("scipy", "scipy", "scipy"),
    ("librosa", "librosa", "librosa"),
    ("pydub", "pydub", "pydub"),
    ("soundfile", "soundfile", "soundfile"),
    ("opentimelineio", "opentimelineio", "opentimelineio"),
    ("pandas", "pandas", "pandas"),
)

# Cached probe result. ``None`` until the first probe_environment() call.
_CACHE: dict[str, Any] | None = None


def clear_cache() -> None:
    """Drop the cached probe result.

    Mainly for tests that monkeypatch the probe internals and want a fresh
    run. Production code never needs this — the environment does not change
    within a process.
    """
    global _CACHE
    _CACHE = None


def _dist_version(distribution_name: str) -> str | None:
    """Best-effort distribution version via importlib.metadata. None if absent."""
    try:
        return _md.version(distribution_name)
    except Exception:
        return None


def _probe_package(import_name: str, distribution_name: str) -> str | None:
    """Return a version string if the package is importable, else None.

    Ground truth is whether the module actually imports. We prefer the
    distribution version (cleaner, PEP 440), then fall back to the module's
    ``__version__`` attribute, then to a bare ``""``-marker promoted to a
    truthy sentinel so the caller still knows it's present.
    """
    # importlib.metadata first — it's cheap and doesn't execute import side
    # effects. But it can report a version for a dist whose import is broken,
    # so we still verify importability below.
    version = _dist_version(distribution_name)
    try:
        module = importlib.import_module(import_name)
    except Exception:
        # Not importable -> treat as absent, even if a stale dist record
        # exists. The agent cares about "can I import this", not metadata.
        return None
    if version:
        return version
    # Importable but no dist metadata: fall back to module __version__.
    mod_version = getattr(module, "__version__", None)
    if isinstance(mod_version, str) and mod_version:
        return mod_version
    # Present but version unknown — return a stable sentinel so the package
    # is reported as installed (truthy) without a misleading number.
    return "unknown"


def _tool_version(binary: str) -> str | None:
    """Return a short version string for a CLI binary, or None if absent."""
    path = shutil.which(binary)
    if not path:
        return None
    try:
        proc = subprocess.run(
            [path, "-version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except Exception:
        # which() found it but it won't run; report present-but-unknown
        # rather than absent (it's on PATH).
        return "unknown"
    out = (proc.stdout or "") + (proc.stderr or "")
    first = out.strip().splitlines()[0] if out.strip() else ""
    # Typical: "ffmpeg version 6.1.1 Copyright ...". Pull the token after
    # the word "version" if present, else the whole first line trimmed.
    parts = first.split()
    if "version" in parts:
        idx = parts.index("version")
        if idx + 1 < len(parts):
            return parts[idx + 1]
    return first[:80] if first else "unknown"


def probe_environment() -> dict[str, Any]:
    """Probe the running environment. Cached; never raises.

    Returns a dict with::

        {
          "python_version": "3.12.12",
          "python_executable": "/path/to/python",
          "os": "Darwin",
          "platform": "<platform.platform()>",
          "packages": {"numpy": "2.4.4", ..., "pandas": None},
          "tools": {"ffmpeg": "6.1.1" | None, "ffprobe": True | False},
        }

    ``packages`` values are version strings when installed, ``None`` when
    absent. ``tools["ffmpeg"]`` is a version string or ``None``;
    ``tools["ffprobe"]`` is a bool (present on PATH or not).
    """
    global _CACHE
    if _CACHE is not None:
        return _CACHE

    try:
        py_version = (
            f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
        )
    except Exception:
        py_version = "unknown"

    packages: dict[str, str | None] = {}
    for probe_key, import_name, dist_name in _RELEVANT_PACKAGES:
        try:
            packages[probe_key] = _probe_package(import_name, dist_name)
        except Exception:
            packages[probe_key] = None

    try:
        ffmpeg_version = _tool_version("ffmpeg")
    except Exception:
        ffmpeg_version = None
    try:
        ffprobe_present = shutil.which("ffprobe") is not None
    except Exception:
        ffprobe_present = False

    try:
        os_name = platform.system() or "unknown"
    except Exception:
        os_name = "unknown"
    try:
        plat = platform.platform()
    except Exception:
        plat = "unknown"

    result: dict[str, Any] = {
        "python_version": py_version,
        "python_executable": sys.executable or "python3",
        "os": os_name,
        "platform": plat,
        "packages": packages,
        "tools": {
            "ffmpeg": ffmpeg_version,
            "ffprobe": ffprobe_present,
        },
    }
    _CACHE = result
    return result


# Friendly display names for the packages, so the summary reads
# "opencv 4.x, Pillow 11.x" rather than "cv2 4.x, PIL 11.x".
_PACKAGE_DISPLAY: dict[str, str] = {
    "numpy": "numpy",
    "cv2": "opencv",
    "PIL": "Pillow",
    "skimage": "scikit-image",
    "moviepy": "moviepy",
    "scipy": "scipy",
    "librosa": "librosa",
    "pydub": "pydub",
    "soundfile": "soundfile",
    "opentimelineio": "opentimelineio",
    "pandas": "pandas",
}


def format_environment_summary(env: dict[str, Any] | None = None) -> str:
    """Render a compact, agent-readable environment block. Never raises.

    Example::

        Environment: Python 3.12.12 at /.../python (use python3, not python).
        Installed: numpy 2.4.4, opencv 4.13, Pillow 12.2, scipy, librosa,
        soundfile, opentimelineio. NOT installed: pandas. ffmpeg ✓ (6.1.1),
        ffprobe ✓.
    """
    try:
        if env is None:
            env = probe_environment()

        py_version = env.get("python_version", "unknown")
        exe = env.get("python_executable", "python3")
        packages: dict[str, str | None] = env.get("packages", {}) or {}
        tools: dict[str, Any] = env.get("tools", {}) or {}

        installed_bits: list[str] = []
        absent_names: list[str] = []
        for key, version in packages.items():
            display = _PACKAGE_DISPLAY.get(key, key)
            if version:
                if version == "unknown":
                    installed_bits.append(display)
                else:
                    # Keep it compact: major.minor is enough for the agent.
                    short = ".".join(str(version).split(".")[:2])
                    installed_bits.append(f"{display} {short}")
            else:
                absent_names.append(display)

        installed_text = (
            ", ".join(installed_bits) if installed_bits else "(none of the probed set)"
        )
        absent_text = ", ".join(absent_names) if absent_names else "(none)"

        ffmpeg = tools.get("ffmpeg")
        if ffmpeg:
            ffmpeg_text = (
                f"ffmpeg ✓ ({ffmpeg})" if ffmpeg != "unknown" else "ffmpeg ✓"
            )
        else:
            ffmpeg_text = "ffmpeg ✗ (not installed)"
        ffprobe_text = "ffprobe ✓" if tools.get("ffprobe") else "ffprobe ✗"

        return (
            f"Environment: Python {py_version} at {exe} "
            f"(use python3, not python). "
            f"Installed: {installed_text}. "
            f"NOT installed: {absent_text}. "
            f"{ffmpeg_text}, {ffprobe_text}."
        )
    except Exception:
        # Last-resort fallback: a minimal but valid, non-empty summary that
        # still names python3 and ffmpeg so prompt assembly never breaks.
        return (
            "Environment: Python (version unknown) "
            "(use python3, not python). "
            "Installed: (probe failed). ffmpeg ✗, ffprobe ✗."
        )
