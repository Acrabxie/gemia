"""Helpers for separating playable media from workflow sidecar artifacts."""
from __future__ import annotations

from pathlib import Path
from typing import Any

VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff"}
AUDIO_EXTENSIONS = {".flac", ".wav", ".mp3", ".m4a", ".aac", ".ogg"}
MEDIA_EXTENSIONS = VIDEO_EXTENSIONS | IMAGE_EXTENSIONS | AUDIO_EXTENSIONS
DOCUMENT_ARTIFACT_EXTENSIONS = {".md", ".json", ".html", ".htm", ".txt", ".csv", ".yaml", ".yml", ".xml", ".log"}


def output_paths(value: Any) -> list[str]:
    """Flatten a loosely shaped outputs value into string paths."""
    paths: list[str] = []

    def visit(item: Any) -> None:
        if item is None:
            return
        if isinstance(item, Path):
            text = str(item)
        elif isinstance(item, str):
            text = item
        elif isinstance(item, dict):
            for key in ("path", "output_path", "preview_path", "abs_path", "artifact_path", "dev_brief_path"):
                nested = item.get(key)
                if nested:
                    visit(nested)
                    return
            return
        elif isinstance(item, (list, tuple, set)):
            for nested in item:
                visit(nested)
            return
        else:
            text = str(item)
        text = text.strip()
        if text:
            paths.append(text)

    visit(value)
    return paths


def path_suffix(path: Any) -> str:
    return Path(str(path or "").split("?", 1)[0]).suffix.lower()


def is_video_output(path: Any) -> bool:
    return path_suffix(path) in VIDEO_EXTENSIONS


def is_image_output(path: Any) -> bool:
    return path_suffix(path) in IMAGE_EXTENSIONS


def is_audio_output(path: Any) -> bool:
    return path_suffix(path) in AUDIO_EXTENSIONS


def is_media_output(path: Any) -> bool:
    return path_suffix(path) in MEDIA_EXTENSIONS


def is_document_artifact_output(path: Any) -> bool:
    suffix = path_suffix(path)
    if suffix in MEDIA_EXTENSIONS:
        return False
    if suffix in DOCUMENT_ARTIFACT_EXTENSIONS:
        return True
    try:
        candidate = Path(str(path)).expanduser()
    except (TypeError, ValueError):
        return False
    return candidate.exists() and candidate.is_file() and suffix not in MEDIA_EXTENSIONS


def media_outputs(value: Any) -> list[str]:
    return [path for path in output_paths(value) if is_media_output(path)]


def video_outputs(value: Any) -> list[str]:
    return [path for path in output_paths(value) if is_video_output(path)]


def artifact_outputs(value: Any) -> list[str]:
    return [path for path in output_paths(value) if is_document_artifact_output(path)]


def split_outputs(value: Any) -> tuple[list[str], list[str], list[str]]:
    """Return media, non-media artifacts, and all flattened outputs."""
    all_outputs = output_paths(value)
    media = [path for path in all_outputs if is_media_output(path)]
    artifacts = [path for path in all_outputs if is_document_artifact_output(path)]
    return media, artifacts, all_outputs
