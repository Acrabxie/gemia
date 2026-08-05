"""Codex-like local folder binding and recoverable project file history."""
from __future__ import annotations

import json
import os
import shutil
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class ProjectWorkspaceError(ValueError):
    """Raised when a project folder or history operation is unsafe."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def validate_source_root(value: str | Path, *, internal_root: str | Path) -> Path:
    """Resolve one user-selected project folder and reject broad roots.

    A Project may grant destructive access inside its folder, so filesystem,
    home, mounted-volume and Lumeri's own data roots are never valid choices.
    """

    raw = str(value or "").strip()
    if not raw:
        raise ProjectWorkspaceError("project folder path must be non-empty")
    lexical_root = Path(raw).expanduser()
    if lexical_root.is_symlink():
        raise ProjectWorkspaceError("project folder cannot be a symlink")
    root = lexical_root.resolve()
    internal = Path(internal_root).expanduser().resolve()
    home = Path.home().resolve()
    if not root.exists():
        raise ProjectWorkspaceError(f"project folder does not exist: {root}")
    if not root.is_dir():
        raise ProjectWorkspaceError(f"project folder is not a directory: {root}")
    protected = {Path(root.anchor).resolve(), home, internal}
    volumes = Path("/Volumes")
    if volumes.exists():
        try:
            protected.update(path.resolve() for path in volumes.iterdir() if path.is_dir())
        except OSError:
            pass
    if root in protected:
        raise ProjectWorkspaceError(f"protected root cannot be used as a project folder: {root}")
    if _is_within(root, internal) or _is_within(internal, root):
        raise ProjectWorkspaceError("project folder cannot contain Lumeri's internal project data")
    return root


class FileMutationJournal:
    """Move-based undo/redo for Lumeri-authored source/edit mutations.

    Large deleted media are moved into the private history directory instead
    of copied.  Undo and redo therefore remain recoverable without eagerly
    duplicating a whole project tree.
    """

    _locks_guard = threading.Lock()
    _locks: dict[str, threading.RLock] = {}

    def __init__(self, root: str | Path, *, allowed_roots: dict[str, str | Path]) -> None:
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.allowed_roots = {
            str(name): Path(path).expanduser().resolve()
            for name, path in allowed_roots.items()
            if str(path or "").strip()
        }
        with self._locks_guard:
            self._lock = self._locks.setdefault(str(self.root), threading.RLock())

    @property
    def index_path(self) -> Path:
        return self.root / "index.json"

    def describe_path(self, path: str | Path) -> dict[str, str]:
        resolved = Path(path).expanduser().resolve()
        matches = [
            (name, root)
            for name, root in self.allowed_roots.items()
            if _is_within(resolved, root)
        ]
        if not matches:
            raise ProjectWorkspaceError(f"path is outside the bound project roots: {resolved}")
        name, root = max(matches, key=lambda item: len(item[1].parts))
        relative = resolved.relative_to(root)
        if str(relative) == ".":
            raise ProjectWorkspaceError("the project root itself cannot be mutated")
        return {"root": name, "path": relative.as_posix()}

    def resolve_path(self, ref: dict[str, Any]) -> Path:
        name = str(ref.get("root") or "")
        relative = str(ref.get("path") or "")
        root = self.allowed_roots.get(name)
        if root is None or not relative or relative in {".", "/"}:
            raise ProjectWorkspaceError("invalid project history path")
        candidate = (root / relative).resolve()
        if not _is_within(candidate, root) or candidate == root:
            raise ProjectWorkspaceError("project history path escapes its root")
        return candidate

    def write(self, target: str | Path, writer, *, label: str = "write") -> dict[str, Any]:
        with self._lock:
            entry = self._new_entry(label, target=self.describe_path(target))
            path = self.resolve_path(entry["target"])
            self._stash_existing(path, self._entry_dir(entry) / "before")
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                writer(path)
                if not path.exists():
                    raise ProjectWorkspaceError("file writer did not create its target")
            except Exception:
                self._restore_after_failure(path, self._entry_dir(entry) / "before")
                raise
            return self._commit(entry)

    def delete(self, target: str | Path, *, label: str = "delete") -> dict[str, Any]:
        with self._lock:
            entry = self._new_entry(label, target=self.describe_path(target))
            path = self.resolve_path(entry["target"])
            if not path.exists():
                raise FileNotFoundError(f"file does not exist: {path}")
            self._move(path, self._entry_dir(entry) / "before")
            return self._commit(entry)

    def move(self, source: str | Path, dest: str | Path, *, label: str = "move") -> dict[str, Any]:
        with self._lock:
            entry = self._new_entry(
                label,
                source=self.describe_path(source),
                dest=self.describe_path(dest),
            )
            source_path = self.resolve_path(entry["source"])
            dest_path = self.resolve_path(entry["dest"])
            if not source_path.exists():
                raise FileNotFoundError(f"file does not exist: {source_path}")
            if source_path == dest_path:
                raise ProjectWorkspaceError("source and destination are the same path")
            self._stash_existing(dest_path, self._entry_dir(entry) / "dest-before")
            try:
                self._move(source_path, dest_path)
            except Exception:
                self._restore_after_failure(dest_path, self._entry_dir(entry) / "dest-before")
                raise
            return self._commit(entry)

    def undo(self) -> dict[str, Any]:
        with self._lock:
            index = self._load_index()
            cursor = int(index["cursor"])
            if cursor <= 0:
                raise ProjectWorkspaceError("nothing to undo")
            entry = dict(index["entries"][cursor - 1])
            entry_dir = self._entry_dir(entry)
            if "source" in entry:
                source = self.resolve_path(entry["source"])
                dest = self.resolve_path(entry["dest"])
                if not dest.exists():
                    raise ProjectWorkspaceError("cannot undo move because destination changed or vanished")
                self._move(dest, source)
                self._restore(entry_dir / "dest-before", dest)
            else:
                target = self.resolve_path(entry["target"])
                if target.exists():
                    self._move(target, entry_dir / "after")
                self._restore(entry_dir / "before", target)
            index["cursor"] = cursor - 1
            self._write_index(index)
            return {**entry, "status": "undone", "cursor": index["cursor"]}

    def redo(self) -> dict[str, Any]:
        with self._lock:
            index = self._load_index()
            cursor = int(index["cursor"])
            if cursor >= len(index["entries"]):
                raise ProjectWorkspaceError("nothing to redo")
            entry = dict(index["entries"][cursor])
            entry_dir = self._entry_dir(entry)
            if "source" in entry:
                source = self.resolve_path(entry["source"])
                dest = self.resolve_path(entry["dest"])
                self._stash_existing(dest, entry_dir / "dest-before")
                if not source.exists():
                    raise ProjectWorkspaceError("cannot redo move because source changed or vanished")
                self._move(source, dest)
            else:
                target = self.resolve_path(entry["target"])
                if (entry_dir / "after").exists():
                    self._stash_existing(target, entry_dir / "before")
                    self._restore(entry_dir / "after", target)
                else:
                    if not target.exists():
                        raise ProjectWorkspaceError("cannot redo delete because restored item changed or vanished")
                    self._move(target, entry_dir / "before")
            index["cursor"] = cursor + 1
            self._write_index(index)
            return {**entry, "status": "redone", "cursor": index["cursor"]}

    def state(self) -> dict[str, Any]:
        with self._lock:
            index = self._load_index()
            cursor = int(index["cursor"])
            return {
                "cursor": cursor,
                "count": len(index["entries"]),
                "can_undo": cursor > 0,
                "can_redo": cursor < len(index["entries"]),
                "latest": index["entries"][cursor - 1] if cursor > 0 else None,
            }

    def _new_entry(self, label: str, **refs: dict[str, str]) -> dict[str, Any]:
        entry = {
            "id": f"file-{uuid.uuid4().hex}",
            "label": str(label or "file mutation"),
            "created_at": _now(),
            **refs,
        }
        self._entry_dir(entry).mkdir(parents=True, exist_ok=False)
        return entry

    def _commit(self, entry: dict[str, Any]) -> dict[str, Any]:
        index = self._load_index()
        cursor = int(index["cursor"])
        if cursor < len(index["entries"]):
            abandoned = self.root / "abandoned" / uuid.uuid4().hex
            abandoned.mkdir(parents=True, exist_ok=True)
            for old in index["entries"][cursor:]:
                old_dir = self._entry_dir(old)
                if old_dir.exists():
                    shutil.move(str(old_dir), str(abandoned / old_dir.name))
            index["entries"] = index["entries"][:cursor]
        index["entries"].append(entry)
        index["cursor"] = len(index["entries"])
        self._write_index(index)
        return {**entry, "status": "recorded", "cursor": index["cursor"]}

    def _load_index(self) -> dict[str, Any]:
        if not self.index_path.exists():
            return {"version": 1, "cursor": 0, "entries": []}
        value = json.loads(self.index_path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or not isinstance(value.get("entries"), list):
            raise ProjectWorkspaceError("project file history index is invalid")
        value["cursor"] = max(0, min(int(value.get("cursor") or 0), len(value["entries"])))
        return value

    def _write_index(self, value: dict[str, Any]) -> None:
        tmp = self.index_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, self.index_path)

    def _entry_dir(self, entry: dict[str, Any]) -> Path:
        entry_id = str(entry.get("id") or "")
        if not entry_id.startswith("file-") or "/" in entry_id or ".." in entry_id:
            raise ProjectWorkspaceError("invalid project history entry id")
        return self.root / "entries" / entry_id

    @staticmethod
    def _move(source: Path, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            raise ProjectWorkspaceError(f"history destination already exists: {dest}")
        shutil.move(str(source), str(dest))

    def _stash_existing(self, path: Path, backup: Path) -> None:
        if path.exists():
            self._move(path, backup)

    def _restore(self, backup: Path, path: Path) -> None:
        if backup.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            self._move(backup, path)

    def _restore_after_failure(self, path: Path, backup: Path) -> None:
        try:
            if path.exists():
                failed = backup.parent / "failed-output"
                self._move(path, failed)
            self._restore(backup, path)
        except Exception:
            pass


__all__ = ["FileMutationJournal", "ProjectWorkspaceError", "validate_source_root"]
