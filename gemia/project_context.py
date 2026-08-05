"""Project-scoped durable memory and activity logs.

Global Lumeri memory describes the creator across every Project.  This module
keeps a second, narrower layer beside the durable production record so every
session in one Project sees the same decisions, constraints and breadcrumbs.
"""
from __future__ import annotations

import os
import threading
from datetime import date, datetime
from pathlib import Path
from typing import Any

from gemia import memory

_LOCKS_GUARD = threading.Lock()
_LOCKS: dict[str, threading.RLock] = {}


def _lock_for(path: Path) -> threading.RLock:
    key = str(path.resolve())
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.RLock())


def context_root(store: Any, project_id: str) -> Path:
    return Path(store.project_dir(project_id)) / "context"


def memory_path(store: Any, project_id: str) -> Path:
    return context_root(store, project_id) / "MEMORY.md"


def logs_dir(store: Any, project_id: str) -> Path:
    return context_root(store, project_id) / "logs"


def log_path(store: Any, project_id: str, day: str | date | None = None) -> Path:
    if day is None:
        day_text = date.today().isoformat()
    elif isinstance(day, date):
        day_text = day.isoformat()
    else:
        day_text = str(day)
    return logs_dir(store, project_id) / f"{day_text}.md"


def is_project_workspace(store: Any, project_id: str) -> bool:
    """Return whether an id represents a user-created, named Project.

    Legacy standalone Chats also have internal production containers, with the
    container name equal to its id. Those are session implementation details,
    not Project workspaces and must keep using global memory semantics.
    """
    try:
        identifier = str(project_id or "").strip()
        if store is None or not identifier:
            return False
        record = store.load_project(identifier)
        name = str(record.get("name") or "").strip()
        return bool(name and name != identifier)
    except Exception:
        return False


def bootstrap(store: Any, project_id: str) -> dict[str, str]:
    root = context_root(store, project_id)
    with _lock_for(root):
        logs_dir(store, project_id).mkdir(parents=True, exist_ok=True)
        durable = memory_path(store, project_id)
        if not durable.exists():
            durable.write_text(
                "# Project Memory\n\n"
                "Facts here apply only to this Lumeri Project and all of its sessions.\n",
                encoding="utf-8",
            )
        today = log_path(store, project_id)
        if not today.exists():
            today.write_text(f"# {today.stem}\n\n", encoding="utf-8")
    return {"root": str(root), "memory": str(durable), "log": str(today)}


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        return ""


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def remember_fact(
    store: Any,
    project_id: str,
    content: str,
    *,
    title: str | None = None,
    kind: str | None = None,
) -> dict[str, str]:
    text = str(content or "").strip()
    if not text:
        raise ValueError("project remember requires non-empty content")
    title_text = str(title or "").strip()
    kind_text = str(kind or "").strip()
    memory.assert_memory_safe(text)
    if title_text:
        memory.assert_memory_safe(title_text)
    if kind_text:
        memory.assert_memory_safe(kind_text)

    bootstrap(store, project_id)
    path = memory_path(store, project_id)
    with _lock_for(context_root(store, project_id)):
        stamp = datetime.now().strftime("%Y-%m-%d")
        labels = []
        if title_text:
            labels.append(f"**{title_text}**")
        if kind_text:
            labels.append(f"({kind_text})")
        prefix = " ".join(labels)
        body = f"{prefix} — {text}" if prefix else text
        bullet = f"- {body}  _(updated {stamp})_"
        existing = _read(path)
        updated = False
        if title_text:
            marker = f"- **{title_text}**"
            lines: list[str] = []
            for line in existing.splitlines():
                if line.startswith(marker) and not updated:
                    lines.append(bullet)
                    updated = True
                else:
                    lines.append(line)
            if not updated:
                lines.append(bullet)
            output = "\n".join(lines).rstrip() + "\n"
        else:
            output = existing.rstrip() + "\n" + bullet + "\n"
        _atomic_write(path, output)
    return {
        "action": "updated" if updated else "appended",
        "scope": "project",
        "title": title_text,
        "kind": kind_text,
        "entry": body,
    }


def append_log(
    store: Any,
    project_id: str,
    text: str,
    *,
    day: str | date | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {"written": False, "entry": "", "scope": "project"}
    try:
        line = " ".join(str(text or "").split())
        if not line:
            result["reason"] = "empty"
            return result
        memory.assert_memory_safe(line)
        bootstrap(store, project_id)
        path = log_path(store, project_id, day)
        entry = f"- {datetime.now().strftime('%H:%M')} {line}"
        with _lock_for(context_root(store, project_id)):
            existing = _read(path)
            if not existing:
                existing = f"# {path.stem}\n\n"
            _atomic_write(path, existing.rstrip() + "\n" + entry + "\n")
        result.update(written=True, entry=entry)
        return result
    except ValueError:
        result["reason"] = "secret"
        return result
    except Exception as exc:  # logging must never break a turn
        result["reason"] = f"error: {type(exc).__name__}"
        return result


def format_for_prompt(
    store: Any,
    project_id: str,
    *,
    memory_chars: int = 4000,
    log_chars: int = 2400,
) -> str:
    try:
        bootstrap(store, project_id)
        durable = _read(memory_path(store, project_id)).strip()
        if len(durable) > memory_chars:
            durable = durable[: memory_chars - 1].rstrip() + "…"

        recent_lines: list[str] = []
        paths = sorted(logs_dir(store, project_id).glob("*.md"), reverse=True)[:2]
        for path in reversed(paths):
            lines = [line for line in _read(path).splitlines() if line.startswith("- ")]
            recent_lines.extend(lines[-24:])
        recent = "\n".join(recent_lines[-32:]).strip()
        if len(recent) > log_chars:
            recent = "…" + recent[-(log_chars - 1) :]

        return (
            "Project memory (only for this Project and shared by all its sessions):\n"
            + (durable or "(no project-specific memory yet)")
            + "\n\nRecent Project log:\n"
            + (recent or "(no project activity logged yet)")
        )
    except Exception:
        return "Project memory and log are unavailable for this session."


def summary(store: Any, project_id: str) -> dict[str, Any]:
    bootstrap(store, project_id)
    durable_lines = [
        line for line in _read(memory_path(store, project_id)).splitlines()
        if line.startswith("- ")
    ]
    log_lines: list[str] = []
    for path in logs_dir(store, project_id).glob("*.md"):
        log_lines.extend(line for line in _read(path).splitlines() if line.startswith("- "))
    return {
        "memory_entries": len(durable_lines),
        "log_entries": len(log_lines),
        "has_recent_log": bool(log_lines),
    }


__all__ = [
    "append_log",
    "bootstrap",
    "format_for_prompt",
    "is_project_workspace",
    "remember_fact",
    "summary",
]
