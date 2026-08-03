"""Cross-platform runtime paths for the local Lumeri service."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path


def output_root() -> Path:
    """Return the session runtime root without assuming a Unix ``/tmp``."""

    configured = str(os.environ.get("LUMERI_V3_OUTPUT_ROOT") or "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path(tempfile.gettempdir()) / "lumeri-v3"


def temp_file(name: str) -> Path:
    """Return a process-safe temporary path for a caller-provided file name."""

    return Path(tempfile.gettempdir()) / name


__all__ = ["output_root", "temp_file"]
