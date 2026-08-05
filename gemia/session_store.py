"""Persistent session log for the Lumeri agent loop.

Disk layout::

    sessions/<session_id>/meta.json     # session metadata + status
    sessions/<session_id>/turns/NNNN.json  # one record per turn
    sessions/<session_id>/events.jsonl  # append-only event log

Sessions reference a ``project_id`` (a ``ProjectStore`` project) so the
loop can re-bind on subsequent invocations without losing context.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]{0,63}$")


class SessionStoreError(ValueError):
    pass


class SessionStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def session_dir(self, session_id: str) -> Path:
        self._validate_id(session_id)
        return self.root / session_id

    def meta_path(self, session_id: str) -> Path:
        return self.session_dir(session_id) / "meta.json"

    def turns_dir(self, session_id: str) -> Path:
        return self.session_dir(session_id) / "turns"

    def events_path(self, session_id: str) -> Path:
        return self.session_dir(session_id) / "events.jsonl"

    def exists(self, session_id: str) -> bool:
        try:
            return self.meta_path(session_id).exists()
        except SessionStoreError:
            return False

    def create(
        self,
        session_id: str,
        *,
        project_id: str,
        goal: str,
        max_turns: int,
        ai_model: str,
    ) -> dict[str, Any]:
        sdir = self.session_dir(session_id)
        if self.meta_path(session_id).exists():
            raise SessionStoreError(f"session already exists: {session_id}")
        sdir.mkdir(parents=True, exist_ok=True)
        self.turns_dir(session_id).mkdir(parents=True, exist_ok=True)
        now = datetime.now(timezone.utc).isoformat()
        meta = {
            "session_id": session_id,
            "project_id": project_id,
            "goal": goal,
            "max_turns": int(max_turns),
            "ai_model": ai_model,
            "created_at": now,
            "updated_at": now,
            "status": "running",
            "turn_count": 0,
        }
        self._write_json(self.meta_path(session_id), meta)
        return meta

    def load_meta(self, session_id: str) -> dict[str, Any]:
        path = self.meta_path(session_id)
        if not path.exists():
            raise SessionStoreError(f"session not found: {session_id}")
        return json.loads(path.read_text(encoding="utf-8"))

    def write_turn(self, session_id: str, turn: dict[str, Any]) -> Path:
        seq = int(turn.get("seq") or 0)
        if seq <= 0:
            raise SessionStoreError("turn seq must be a positive integer")
        path = self.turns_dir(session_id) / f"{seq:04d}.json"
        self._write_json(path, turn)
        return path

    def update_meta(self, session_id: str, updates: dict[str, Any]) -> dict[str, Any]:
        meta = self.load_meta(session_id)
        meta.update(updates)
        meta["updated_at"] = datetime.now(timezone.utc).isoformat()
        self._write_json(self.meta_path(session_id), meta)
        return meta

    def read_turns(self, session_id: str) -> list[dict[str, Any]]:
        tdir = self.turns_dir(session_id)
        if not tdir.exists():
            return []
        out: list[dict[str, Any]] = []
        for path in sorted(tdir.glob("*.json")):
            if path.name.startswith("._"):
                continue
            try:
                out.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                continue
        return out

    def read_events(self, session_id: str) -> list[dict[str, Any]]:
        path = self.events_path(session_id)
        if not path.exists():
            return []
        events: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return events

    def append_event(
        self,
        session_id: str,
        event_type: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Append one runtime event and return its serialized shape."""
        # Validates session id and ensures the event log lives under the
        # session directory. If a caller appends before create(), still keep the
        # event in the canonical path so tests and experimental tools can read
        # it back.
        path = self.events_path(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        event = {
            "type": str(event_type),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "payload": dict(payload or {}),
        }
        with path.open("a", encoding="utf-8") as file_obj:
            file_obj.write(json.dumps(event, ensure_ascii=False) + "\n")
        return event

    @staticmethod
    def _validate_id(session_id: str) -> None:
        if not isinstance(session_id, str) or not _SESSION_ID_RE.match(session_id):
            raise SessionStoreError(
                f"invalid session_id (must match [A-Za-z0-9][A-Za-z0-9_-]{{0,63}}): {session_id!r}"
            )

    @staticmethod
    def _write_json(path: Path, payload: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)
