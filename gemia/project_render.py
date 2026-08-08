"""Preview adapter for the canonical project export graph.

Preview is a preset, not a second renderer.  Keeping this module as a thin
compatibility adapter preserves the old public API while ensuring overlays,
audio, transitions, timing and honesty fields are interpreted by exactly the
same pipeline as a final export.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from .compat import ffprobe_path
from .project_export import ProjectExportError, export_project
from .project_store import ProjectStore


class ProjectRenderError(RuntimeError):
    """Compatibility error raised by the preview API."""

    def __init__(self, code: str, message: str, *, detail: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.detail = detail


def render_project_preview(
    store: ProjectStore,
    project_id: str,
    *,
    output_root: str | Path,
    max_long_edge: int = 640,
    label: str = "preview",
    timeout_sec: int = 120,
) -> dict[str, Any]:
    """Render a draft preset through the canonical export implementation."""
    try:
        exported = export_project(
            store,
            project_id,
            output_root=output_root,
            quality="draft",
            label=label,
            timeout_sec=timeout_sec,
            max_long_edge=max_long_edge,
            verify_decode=False,
        )
    except ProjectExportError as exc:
        raise ProjectRenderError(exc.code, str(exc), detail=exc.detail) from exc

    result = dict(exported)
    result["render_id"] = exported.get("export_id")
    result["preview_path"] = exported.get("export_path")
    result["preview_profile"] = exported.get("render_preset")
    return result


def ffprobe_media(path: str | Path) -> dict[str, Any]:
    """Retained public helper for callers that inspect a preview directly."""
    media_path = Path(path)
    proc = subprocess.run(
        [
            ffprobe_path(),
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(media_path),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if proc.returncode != 0:
        raise ProjectRenderError(
            "ffprobe_failed",
            "Rendered preview could not be probed.",
            detail=(proc.stderr or proc.stdout or "").strip()[-1200:],
        )
    try:
        payload = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise ProjectRenderError(
            "ffprobe_invalid_json", f"ffprobe returned invalid JSON: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise ProjectRenderError(
            "ffprobe_invalid_json", "ffprobe JSON payload was not an object."
        )
    return payload


__all__ = ["ProjectRenderError", "render_project_preview", "ffprobe_media"]
