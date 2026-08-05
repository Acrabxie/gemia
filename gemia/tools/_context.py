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

import hashlib
import json
import os
from dataclasses import dataclass, field, replace
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


def _lexical_absolute_path(value: str | Path) -> Path:
    return Path(os.path.abspath(os.path.expanduser(str(value))))


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
    sha256: str = ""
    source: dict[str, Any] = field(default_factory=dict)
    license: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "asset_id": self.asset_id,
            "kind": self.kind,
            "path": str(self.path),
            "summary": self.summary,
            "created_at": self.created_at,
            "lineage": list(self.lineage),
            "sha256": self.sha256,
            "source": dict(self.source),
            "license": dict(self.license),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "AssetRecord":
        # Registry files are trusted durable records.  ``Path.resolve()`` is a
        # filesystem operation (realpath/lstat), and replaying a project on a
        # removable/exFAT media root used to perform it once per asset.  Keep
        # load lexical; existence, type and containment are still checked at
        # the artifact/tool boundary that consumes the path.
        absolute_path = _lexical_absolute_path(str(value["path"]))
        return cls(
            asset_id=str(value["asset_id"]),
            kind=str(value["kind"]),
            path=absolute_path,
            summary=str(value.get("summary") or ""),
            created_at=str(value.get("created_at") or _now()),
            lineage=tuple(str(item) for item in (value.get("lineage") or [])),
            sha256=str(value.get("sha256") or ""),
            source=dict(value.get("source") or {}),
            license=dict(value.get("license") or {}),
        )

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
    """Project-restorable asset_id ↔ path mapping with per-kind counters.

    ``on_change`` is synchronous on purpose: an allocated id or registered
    provider result is durable before the dispatcher reports success.  Legacy
    callers that construct ``AssetRegistry()`` keep the old in-memory behavior.
    """

    def __init__(self, *, on_change: Callable[["AssetRegistry"], None] | None = None) -> None:
        self._records: dict[str, AssetRecord] = {}
        self._counters: dict[str, int] = {kind: 0 for kind in _KIND_PREFIX}
        self._on_change = on_change

    def add_external(
        self,
        path: Path,
        *,
        summary: str | None = None,
        source: dict[str, Any] | None = None,
        license: dict[str, Any] | None = None,
    ) -> AssetRecord:
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
            sha256=_sha256_file(path),
            source=dict(source or {"kind": "external", "original_name": path.name}),
            license=dict(license or {}),
        )
        self._records[asset_id] = record
        self._changed()
        return record

    def allocate_id(self, kind: str) -> str:
        if kind not in _KIND_PREFIX:
            raise ValueError(f"unknown asset kind: {kind!r}")
        asset_id = self._next_id(kind)
        # Persist the counter before a paid provider call leaves.  A crash may
        # leave a gap, but can never reuse a pending asset id.
        self._changed()
        return asset_id

    def register_output(
        self,
        asset_id: str,
        *,
        kind: str,
        path: Path,
        summary: str,
        lineage: Iterable[str] = (),
        source: dict[str, Any] | None = None,
        license: dict[str, Any] | None = None,
    ) -> AssetRecord:
        if asset_id in self._records:
            raise ValueError(f"asset_id already registered: {asset_id}")
        resolved_path = Path(path).expanduser().resolve()
        record = AssetRecord(
            asset_id=asset_id,
            kind=kind,
            path=resolved_path,
            summary=summary,
            created_at=_now(),
            lineage=tuple(lineage),
            sha256=_sha256_file(resolved_path) if resolved_path.is_file() else "",
            source=dict(source or {"kind": "derived"}),
            license=dict(license or {}),
        )
        self._records[asset_id] = record
        self._changed()
        return record

    def update_record(
        self,
        asset_id: str,
        *,
        source_patch: dict[str, Any] | None = None,
        license_patch: dict[str, Any] | None = None,
        summary: str | None = None,
    ) -> AssetRecord:
        """Atomically enrich mutable metadata without changing asset identity.

        Paths, hashes, ids, kinds, timestamps and lineage are intentionally not
        accepted as inputs.  Post-inspection facts such as
        ``real_motion_verified`` or an audio role can therefore be attached
        without letting callers silently swap the underlying media.
        """

        current = self.get(str(asset_id))
        source = dict(current.source)
        license_info = dict(current.license)
        if source_patch is not None:
            if not isinstance(source_patch, dict):
                raise TypeError("source_patch must be an object")
            source.update(source_patch)
        if license_patch is not None:
            if not isinstance(license_patch, dict):
                raise TypeError("license_patch must be an object")
            license_info.update(license_patch)
        updated = replace(
            current,
            summary=current.summary if summary is None else str(summary),
            source=source,
            license=license_info,
        )
        self._records[current.asset_id] = updated
        self._changed()
        return updated

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

    def set_on_change(
        self, callback: Callable[["AssetRegistry"], None] | None
    ) -> None:
        self._on_change = callback

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "lumeri.asset-registry",
            "version": 1,
            "counters": dict(self._counters),
            "records": [record.to_dict() for record in self._records.values()],
        }

    @classmethod
    def from_dict(
        cls,
        value: dict[str, Any],
        *,
        on_change: Callable[["AssetRegistry"], None] | None = None,
    ) -> "AssetRegistry":
        registry = cls(on_change=on_change)
        raw_records = value.get("records") if isinstance(value, dict) else []
        if isinstance(raw_records, dict):
            raw_records = list(raw_records.values())
        for raw in raw_records or []:
            if not isinstance(raw, dict):
                continue
            try:
                record = AssetRecord.from_dict(raw)
            except (KeyError, TypeError, ValueError):
                continue
            if record.kind not in _KIND_PREFIX or record.asset_id in registry._records:
                continue
            registry._records[record.asset_id] = record
        raw_counters = value.get("counters") if isinstance(value, dict) else {}
        if isinstance(raw_counters, dict):
            for kind in _KIND_PREFIX:
                try:
                    registry._counters[kind] = max(0, int(raw_counters.get(kind) or 0))
                except (TypeError, ValueError):
                    registry._counters[kind] = 0
        # Counters from an older/corrupt snapshot may lag the records.  Repair
        # upward so the next allocation cannot collide.
        for record in registry._records.values():
            prefix = _KIND_PREFIX.get(record.kind, "") + "_"
            if record.asset_id.startswith(prefix):
                try:
                    suffix = int(record.asset_id[len(prefix):])
                except ValueError:
                    continue
                registry._counters[record.kind] = max(
                    registry._counters.get(record.kind, 0), suffix
                )
        return registry

    def save(self, path: str | Path) -> Path:
        target = _lexical_absolute_path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
        try:
            with tmp.open("w", encoding="utf-8") as fh:
                json.dump(self.to_dict(), fh, ensure_ascii=False, indent=2)
                fh.write("\n")
                fh.flush()
                try:
                    os.fsync(fh.fileno())
                except OSError:
                    pass
            os.replace(tmp, target)
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
        return target

    @classmethod
    def load(
        cls,
        path: str | Path,
        *,
        on_change: Callable[["AssetRegistry"], None] | None = None,
    ) -> "AssetRegistry":
        source = _lexical_absolute_path(path)
        if not source.exists():
            return cls(on_change=on_change)
        value = json.loads(source.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError(f"asset registry must be a JSON object: {source}")
        return cls.from_dict(value, on_change=on_change)

    def _next_id(self, kind: str) -> str:
        self._counters[kind] = self._counters.get(kind, 0) + 1
        return f"{_KIND_PREFIX[kind]}_{self._counters[kind]:03d}"

    def _changed(self) -> None:
        if self._on_change is not None:
            self._on_change(self)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with Path(path).open("rb") as fh:
            while chunk := fh.read(1024 * 1024):
                digest.update(chunk)
    except OSError:
        return ""
    return digest.hexdigest()


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
