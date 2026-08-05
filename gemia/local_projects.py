"""Account-free Project grouping for the public desktop runtime."""
from __future__ import annotations

import json
import os
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECTS_PATH = Path.home() / ".gemia" / "projects.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read() -> dict[str, Any]:
    try:
        payload = json.loads(PROJECTS_PATH.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {"projects": []}
    except (OSError, json.JSONDecodeError):
        return {"projects": []}


def _write(payload: dict[str, Any]) -> None:
    PROJECTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=PROJECTS_PATH.parent, delete=False) as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        temp = Path(handle.name)
    temp.replace(PROJECTS_PATH)


def list_projects() -> list[dict[str, Any]]:
    return [dict(item) for item in _read().get("projects", []) if isinstance(item, dict)]


def create_project(name: str, source_root: str = "") -> dict[str, Any]:
    title = str(name or "").strip() or "未命名 Project"
    source = str(source_root or "").strip()
    if source:
        # The public runtime stores this as creator-selected metadata only; it
        # never reads from or writes to the submitted path. The native folder
        # picker is the existence check, while normalization prevents home or
        # disk roots from being recorded as an over-broad Project boundary.
        normalized = os.path.normpath(os.path.abspath(os.path.expanduser(source)))
        home = os.path.normcase(os.path.normpath(str(Path.home())))
        if os.path.normcase(normalized) == home or os.path.dirname(normalized) == normalized:
            raise ValueError("不能把用户目录或磁盘根目录作为 Project")
        source = normalized
    payload = _read()
    project = {
        "project_id": f"project-{uuid.uuid4().hex[:12]}",
        "name": title,
        "source_root": source,
        "sessions": [],
        "created_at": _now(),
        "updated_at": _now(),
        "file_history": {"cursor": 0, "count": 0, "can_undo": False, "can_redo": False, "latest": None},
        "context": {"memory_entries": 0, "log_entries": 0, "has_recent_log": False},
    }
    payload.setdefault("projects", []).append(project)
    _write(payload)
    return project


def link_session(project_id: str, session_id: str) -> None:
    if not project_id or not session_id:
        return
    payload = _read()
    for project in payload.get("projects", []):
        if project.get("project_id") != project_id:
            continue
        sessions = project.setdefault("sessions", [])
        if not any(item.get("session_id") == session_id for item in sessions if isinstance(item, dict)):
            sessions.append({"session_id": session_id, "title": "新会话", "created_at": _now()})
        project["updated_at"] = _now()
        _write(payload)
        return


def project_for_session(session_id: str) -> str | None:
    for project in list_projects():
        if any(item.get("session_id") == session_id for item in project.get("sessions", []) if isinstance(item, dict)):
            return str(project.get("project_id") or "") or None
    return None


def session_metadata() -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for project in list_projects():
        for item in project.get("sessions", []):
            if isinstance(item, dict) and item.get("session_id"):
                result[str(item["session_id"])] = dict(item)
    return result


def update_session(session_id: str, **changes: Any) -> dict[str, Any] | None:
    payload = _read()
    for project in payload.get("projects", []):
        for item in project.get("sessions", []):
            if isinstance(item, dict) and item.get("session_id") == session_id:
                item.update(changes)
                project["updated_at"] = _now()
                _write(payload)
                return dict(item)
    return None


def remove_session(session_id: str) -> bool:
    payload = _read()
    for project in payload.get("projects", []):
        sessions = project.get("sessions", [])
        kept = [item for item in sessions if not isinstance(item, dict) or item.get("session_id") != session_id]
        if len(kept) != len(sessions):
            project["sessions"] = kept
            project["updated_at"] = _now()
            _write(payload)
            return True
    return False
