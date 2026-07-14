"""Session-scoped types shared by every v3 tool dispatcher.

- ``AssetRegistry``: the only place asset_id → file-path mapping lives.
  Independent counters per kind (video/image/audio) so ids stay short.
- ``ToolContext``: what the agent loop hands every dispatcher.
- ``ProgressUpdate``: what a dispatcher emits to the progress callback.

A dispatcher signature is ``async def dispatch(args: dict, ctx: ToolContext) -> dict``.
The agent loop catches any raised exception and turns it into a
``tool_exec_error`` event; dispatchers must NOT swallow errors.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterable

from gemia.tools._jobs import JobRegistry

if TYPE_CHECKING:  # runtime-free: only the loop constructs the handle
    from gemia.project_store import ProjectHandle


_KIND_PREFIX = {"video": "v", "image": "img", "audio": "aud", "lottie": "lot", "otio": "otio"}
_VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}
_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
_AUDIO_EXTS = {".wav", ".mp3", ".aac", ".flac", ".ogg", ".m4a"}
_LOTTIE_EXTS = {".json", ".lottie"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def infer_kind(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in _VIDEO_EXTS:
        return "video"
    if ext in _IMAGE_EXTS:
        return "image"
    if ext in _AUDIO_EXTS:
        return "audio"
    if ext in _LOTTIE_EXTS:
        return "lottie"
    raise ValueError(f"cannot infer asset kind from extension: {ext!r} ({path})")


@dataclass
class AssetRecord:
    asset_id: str
    kind: str
    path: Path
    summary: str
    created_at: str
    lineage: tuple[str, ...] = ()

    def to_compact_line(self) -> str:
        size = ""
        try:
            if self.path.exists():
                size = f" {self.path.stat().st_size:,}B"
        except OSError:
            size = ""
        line = f"- {self.asset_id} [{self.kind}] {self.summary}{size}"
        if self.lineage:
            line += f" (from {', '.join(self.lineage)})"
        return line


class AssetRegistry:
    """Session-scoped asset_id ↔ path mapping with per-kind counters."""

    def __init__(self) -> None:
        self._records: dict[str, AssetRecord] = {}
        self._counters: dict[str, int] = {"video": 0, "image": 0, "audio": 0, "lottie": 0}

    def add_external(self, path: Path, *, summary: str | None = None) -> AssetRecord:
        path = Path(path).expanduser().resolve()
        if not path.exists():
            raise FileNotFoundError(f"external asset path does not exist: {path}")
        kind = infer_kind(path)
        asset_id = self._next_id(kind)
        record = AssetRecord(
            asset_id=asset_id,
            kind=kind,
            path=path,
            summary=summary or f"user-provided {kind} ({path.name})",
            created_at=_now(),
            lineage=(),
        )
        self._records[asset_id] = record
        return record

    def allocate_id(self, kind: str) -> str:
        if kind not in _KIND_PREFIX:
            raise ValueError(f"unknown asset kind: {kind!r}")
        return self._next_id(kind)

    def register_output(
        self,
        asset_id: str,
        *,
        kind: str,
        path: Path,
        summary: str,
        lineage: Iterable[str] = (),
    ) -> AssetRecord:
        if asset_id in self._records:
            raise ValueError(f"asset_id already registered: {asset_id}")
        record = AssetRecord(
            asset_id=asset_id,
            kind=kind,
            path=Path(path).resolve(),
            summary=summary,
            created_at=_now(),
            lineage=tuple(lineage),
        )
        self._records[asset_id] = record
        return record

    def get(self, asset_id: str) -> AssetRecord:
        try:
            return self._records[asset_id]
        except KeyError:
            known = ", ".join(self._records.keys()) or "(none)"
            raise KeyError(
                f"asset_id not in session registry: {asset_id!r}. Known: {known}"
            ) from None

    def contains(self, asset_id: str) -> bool:
        return asset_id in self._records

    def list_records(self) -> list[AssetRecord]:
        return list(self._records.values())

    def compact_text(self) -> str:
        if not self._records:
            return "(no assets in session yet)"
        return "\n".join(r.to_compact_line() for r in self._records.values())

    def _next_id(self, kind: str) -> str:
        self._counters[kind] = self._counters.get(kind, 0) + 1
        return f"{_KIND_PREFIX[kind]}_{self._counters[kind]:03d}"


@dataclass
class ProgressUpdate:
    percent: float | None = None
    message: str | None = None
    eta_sec: float | None = None


ProgressCallback = Callable[[ProgressUpdate], None]


@dataclass
class ToolContext:
    session_id: str
    output_dir: Path
    registry: AssetRegistry
    emit_progress: ProgressCallback
    extra: dict[str, Any] = field(default_factory=dict)
    jobs: JobRegistry = field(default_factory=JobRegistry)
    project: ProjectHandle | None = None  # timeline document handle (None in legacy tests)

    def child_path(self, asset_id: str, ext: str) -> Path:
        ext = ext if ext.startswith(".") else f".{ext}"
        return Path(self.output_dir) / f"{asset_id}{ext}"


__all__ = [
    "AssetRecord",
    "AssetRegistry",
    "ProgressUpdate",
    "ProgressCallback",
    "ToolContext",
    "infer_kind",
]
