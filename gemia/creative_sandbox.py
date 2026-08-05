"""Host-owned Creative Dev Sandbox workspace API.

This module is the v0 backend state contract for the hidden freer creative
coding surface. It deliberately does not execute commands or write outside the
workspace tree; runner integrations can consume the same JSONL event log later.
"""
from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


EVENT_DEV_WORKSPACE_READY = "dev_workspace_ready"
EVENT_DEV_FILE_WRITTEN = "dev_file_written"
EVENT_DEV_COMMAND_STARTED = "dev_command_started"
EVENT_DEV_COMMAND_FINISHED = "dev_command_finished"
EVENT_DEV_ARTIFACT_READY = "dev_artifact_ready"
EVENT_DEV_SKILL_DRAFT_READY = "dev_skill_draft_ready"
EVENT_FAILED = "failed"

CREATIVE_SANDBOX_EVENT_TYPES = (
    EVENT_DEV_WORKSPACE_READY,
    EVENT_DEV_FILE_WRITTEN,
    EVENT_DEV_COMMAND_STARTED,
    EVENT_DEV_COMMAND_FINISHED,
    EVENT_DEV_ARTIFACT_READY,
    EVENT_DEV_SKILL_DRAFT_READY,
    EVENT_FAILED,
)

WORKSPACE_SUBDIRS = ("scripts", "artifacts", "previews", "skills", "logs")
WRITABLE_SUBDIRS = frozenset(WORKSPACE_SUBDIRS)

_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]{0,63}$")
_SECRET_NAME_RE = re.compile(r"(^|[._-])(secret|token|password|passwd|apikey|api_key|credential|private)([._-]|$)", re.I)
_SECRET_CONTENT_RE = re.compile(r"(api[_-]?key|secret|token|password|private[_-]?key)\s*[:=]", re.I)


class CreativeSandboxError(RuntimeError):
    def __init__(self, code: str, message: str, *, status: int = 400, detail: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.detail = detail


class CreativeSandboxService:
    def __init__(self, root_dir: str | Path) -> None:
        self.root_dir = Path(root_dir).expanduser().resolve()
        self.workspaces_root = self.root_dir / "workspaces"
        self.workspaces_root.mkdir(parents=True, exist_ok=True)

    def create_workspace(self, payload: dict[str, Any] | None = None, *, account_id: str | None = None) -> dict[str, Any]:
        payload = payload or {}
        session_id = _safe_session_id(str(payload.get("session_id") or payload.get("sessionId") or ""))
        if not session_id:
            session_id = f"dev_{uuid.uuid4().hex[:10]}"
        workspace_dir = self.workspace_dir(session_id)
        created = not self.meta_path(session_id).exists()
        _ensure_layout(workspace_dir)
        now = _now()
        if created:
            meta = {
                "session_id": session_id,
                "project_id": _safe_optional_id(str(payload.get("project_id") or payload.get("projectId") or "")),
                "goal": str(payload.get("goal") or "Creative Dev Sandbox").strip() or "Creative Dev Sandbox",
                "account_id": account_id or "",
                "created_at": now,
                "updated_at": now,
                "status": "ready",
                "policy": sandbox_policy(),
            }
        else:
            meta = self.load_meta(session_id)
            updates = {
                "project_id": _safe_optional_id(str(payload.get("project_id") or payload.get("projectId") or meta.get("project_id") or "")),
                "goal": str(payload.get("goal") or meta.get("goal") or "Creative Dev Sandbox").strip() or "Creative Dev Sandbox",
                "updated_at": now,
                "status": "ready",
                "policy": sandbox_policy(),
            }
            if account_id:
                updates["account_id"] = account_id
            meta.update(updates)
        self._write_json(self.meta_path(session_id), meta)
        event = self.append_event(
            session_id,
            EVENT_DEV_WORKSPACE_READY,
            {
                "workspace": self._workspace_payload(session_id, meta=meta),
                "created": created,
            },
        )
        return {
            "status": "succeeded",
            "session_id": session_id,
            "created": created,
            "workspace": self._workspace_payload(session_id, meta=meta),
            "event": event,
            "events": self.read_events(session_id),
        }

    def get_workspace(self, session_id: str) -> dict[str, Any]:
        session_id = _require_session_id(session_id)
        meta = self.load_meta(session_id)
        return {
            "status": "succeeded",
            "session_id": session_id,
            "workspace": self._workspace_payload(session_id, meta=meta),
            "artifacts": self.list_artifacts(session_id)["artifacts"],
            "logs": self.list_logs(session_id)["logs"],
            "events": self.read_events(session_id),
        }

    def list_artifacts(self, session_id: str) -> dict[str, Any]:
        session_id = _require_session_id(session_id)
        if not self.meta_path(session_id).exists():
            raise CreativeSandboxError("workspace_not_found", f"workspace not found: {session_id}", status=404)
        workspace_dir = self.workspace_dir(session_id)
        items: list[dict[str, Any]] = []
        for kind in ("artifacts", "previews", "skills", "scripts"):
            base = workspace_dir / kind
            items.extend(_list_files(base, workspace_dir=workspace_dir, kind=kind))
        items.sort(key=lambda item: item["updated_at"], reverse=True)
        return {"status": "succeeded", "session_id": session_id, "artifacts": items}

    def latest_preview(self, session_id: str) -> dict[str, Any]:
        artifacts = self.list_artifacts(session_id)["artifacts"]
        previews = [
            item
            for item in artifacts
            if item.get("kind") == "previews" and _is_preview_media(str(item.get("path") or ""))
        ]
        preview = previews[0] if previews else None
        return {
            "status": "succeeded",
            "session_id": session_id,
            "preview": preview,
            "has_preview": bool(preview),
        }

    def report(self, session_id: str) -> dict[str, Any]:
        session_id = _require_session_id(session_id)
        meta = self.load_meta(session_id)
        artifacts = self.list_artifacts(session_id)["artifacts"]
        logs = self.list_logs(session_id)["logs"]
        events = self.read_events(session_id)
        preview = self.latest_preview(session_id)
        commands = _command_summaries(events)
        failures = _failure_summaries(events)
        file_counts = {kind: 0 for kind in WORKSPACE_SUBDIRS}
        for item in artifacts + logs:
            kind = str(item.get("kind") or "")
            if kind in file_counts:
                file_counts[kind] += 1
        return {
            "status": "succeeded",
            "session_id": session_id,
            "brief": _report_brief(
                preview=preview,
                commands=commands,
                failures=failures,
                artifacts=artifacts,
            ),
            "summary": {
                "goal": meta.get("goal") or "Creative Dev Sandbox",
                "workspace_status": meta.get("status") or "unknown",
                "file_counts": file_counts,
                "event_count": len(events),
                "command_count": len(commands),
                "failure_count": len(failures),
                "has_preview": bool(preview.get("has_preview")),
                "latest_preview_path": (preview.get("preview") or {}).get("path") if preview.get("preview") else "",
            },
            "preview": preview,
            "recent_files": artifacts[:12],
            "recent_logs": logs[:6],
            "recent_events": events[-12:],
            "commands": commands[-8:],
            "failures": failures[-5:],
            "next_diagnostic": _next_diagnostic(preview, commands, failures, artifacts),
        }

    def list_logs(self, session_id: str) -> dict[str, Any]:
        session_id = _require_session_id(session_id)
        if not self.meta_path(session_id).exists():
            raise CreativeSandboxError("workspace_not_found", f"workspace not found: {session_id}", status=404)
        workspace_dir = self.workspace_dir(session_id)
        logs = _list_files(workspace_dir / "logs", workspace_dir=workspace_dir, kind="logs")
        logs.sort(key=lambda item: item["updated_at"], reverse=True)
        return {
            "status": "succeeded",
            "session_id": session_id,
            "logs": logs,
            "events": self.read_events(session_id),
        }

    def read_file(self, session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        session_id = _require_session_id(session_id)
        if not self.meta_path(session_id).exists():
            raise CreativeSandboxError("workspace_not_found", f"workspace not found: {session_id}", status=404)
        kind = str(payload.get("kind") or payload.get("area") or "scripts").strip()
        if kind not in WORKSPACE_SUBDIRS:
            raise CreativeSandboxError("workspace_area_forbidden", f"workspace area is not readable: {kind}")
        rel_path = str(payload.get("path") or payload.get("relative_path") or "").strip()
        target = self._safe_child_path(session_id, kind, rel_path)
        if not target.exists() or not target.is_file():
            raise CreativeSandboxError("file_not_found", f"workspace file not found: {kind}/{rel_path}", status=404)
        if target.stat().st_size > int(payload.get("max_bytes") or 512_000):
            raise CreativeSandboxError("content_too_large", "file content exceeds the v0 workspace read limit")
        return {
            "status": "succeeded",
            "session_id": session_id,
            "file": _file_payload(target, workspace_dir=self.workspace_dir(session_id), kind=kind),
            "content": target.read_text(encoding="utf-8"),
        }

    def file_path(self, session_id: str, payload: dict[str, Any]) -> Path:
        session_id = _require_session_id(session_id)
        if not self.meta_path(session_id).exists():
            raise CreativeSandboxError("workspace_not_found", f"workspace not found: {session_id}", status=404)
        kind = str(payload.get("kind") or payload.get("area") or "scripts").strip()
        if kind not in WORKSPACE_SUBDIRS:
            raise CreativeSandboxError("workspace_area_forbidden", f"workspace area is not readable: {kind}")
        rel_path = str(payload.get("path") or payload.get("relative_path") or "").strip()
        target = self._safe_child_path(session_id, kind, rel_path)
        if not target.exists() or not target.is_file():
            raise CreativeSandboxError("file_not_found", f"workspace file not found: {kind}/{rel_path}", status=404)
        return target

    def write_file(self, session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        session_id = _require_session_id(session_id)
        if not self.meta_path(session_id).exists():
            raise CreativeSandboxError("workspace_not_found", f"workspace not found: {session_id}", status=404)
        kind = str(payload.get("kind") or payload.get("area") or "scripts").strip()
        if kind not in WRITABLE_SUBDIRS:
            raise CreativeSandboxError("workspace_area_forbidden", f"workspace area is not writable: {kind}")
        rel_path = str(payload.get("path") or payload.get("relative_path") or "").strip()
        content = payload.get("content", "")
        if not isinstance(content, str):
            raise CreativeSandboxError("invalid_content", "content must be a string")
        if len(content.encode("utf-8")) > int(payload.get("max_bytes") or 512_000):
            raise CreativeSandboxError("content_too_large", "file content exceeds the v0 workspace limit")
        target = self._safe_child_path(session_id, kind, rel_path, content=content)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        rel = _relative_to(target, self.workspace_dir(session_id))
        event = self.append_event(
            session_id,
            EVENT_DEV_FILE_WRITTEN,
            {
                "kind": kind,
                "path": rel,
                "size": target.stat().st_size,
            },
        )
        events = [event]
        if kind in {"artifacts", "previews"}:
            events.append(
                self.append_event(
                    session_id,
                    EVENT_DEV_ARTIFACT_READY,
                    {
                        "kind": kind,
                        "path": rel,
                        "size": target.stat().st_size,
                    },
                )
            )
        if kind == "skills":
            events.append(
                self.append_event(
                    session_id,
                    EVENT_DEV_SKILL_DRAFT_READY,
                    {
                        "path": rel,
                        "size": target.stat().st_size,
                    },
                )
            )
        self._touch_meta(session_id)
        return {
            "status": "succeeded",
            "session_id": session_id,
            "file": _file_payload(target, workspace_dir=self.workspace_dir(session_id), kind=kind),
            "events": events,
        }

    def record_command_event(self, session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        session_id = _require_session_id(session_id)
        if not self.meta_path(session_id).exists():
            raise CreativeSandboxError("workspace_not_found", f"workspace not found: {session_id}", status=404)
        phase = str(payload.get("phase") or payload.get("status") or "started").strip().lower()
        if phase not in {"started", "finished"}:
            raise CreativeSandboxError("invalid_command_phase", "command phase must be started or finished")
        event_type = EVENT_DEV_COMMAND_STARTED if phase == "started" else EVENT_DEV_COMMAND_FINISHED
        command = str(payload.get("command") or "").strip()
        event_payload = {
            "command_id": _safe_optional_id(str(payload.get("command_id") or payload.get("commandId") or "")) or f"cmd_{uuid.uuid4().hex[:10]}",
            "label": str(payload.get("label") or "").strip(),
            "command": command[:240],
            "exit_code": payload.get("exit_code"),
            "summary": str(payload.get("summary") or "").strip()[:1000],
            "executed": False,
            "note": "CreativeSandboxService records command lifecycle only; it does not execute commands.",
        }
        event = self.append_event(session_id, event_type, event_payload)
        self._touch_meta(session_id)
        return {"status": "succeeded", "session_id": session_id, "event": event}

    def append_event(self, session_id: str, event_type: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        session_id = _require_session_id(session_id)
        event = {
            "type": str(event_type),
            "timestamp": _now(),
            "payload": _sanitize_payload(payload or {}),
        }
        path = self.events_path(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as file_obj:
            file_obj.write(json.dumps(event, ensure_ascii=False) + "\n")
        return event

    def read_events(self, session_id: str) -> list[dict[str, Any]]:
        path = self.events_path(session_id)
        if not path.exists():
            return []
        events: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return events

    def workspace_dir(self, session_id: str) -> Path:
        return self.workspaces_root / _require_session_id(session_id)

    def meta_path(self, session_id: str) -> Path:
        return self.workspace_dir(session_id) / "meta.json"

    def events_path(self, session_id: str) -> Path:
        return self.workspace_dir(session_id) / "logs" / "events.jsonl"

    def load_meta(self, session_id: str) -> dict[str, Any]:
        path = self.meta_path(session_id)
        if not path.exists():
            raise CreativeSandboxError("workspace_not_found", f"workspace not found: {session_id}", status=404)
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise CreativeSandboxError("workspace_meta_invalid", "workspace metadata is invalid") from exc

    def _workspace_payload(self, session_id: str, *, meta: dict[str, Any]) -> dict[str, Any]:
        workspace_dir = self.workspace_dir(session_id)
        return {
            "session_id": session_id,
            "root": str(workspace_dir),
            "layout": {name: str(workspace_dir / name) for name in WORKSPACE_SUBDIRS},
            "relative_layout": {name: f"workspaces/{session_id}/{name}" for name in WORKSPACE_SUBDIRS},
            "meta": dict(meta),
            "event_types": list(CREATIVE_SANDBOX_EVENT_TYPES),
            "policy": sandbox_policy(),
        }

    def _safe_child_path(self, session_id: str, kind: str, rel_path: str, *, content: str = "") -> Path:
        if not rel_path:
            raise CreativeSandboxError("missing_path", "relative path is required")
        path = Path(rel_path)
        if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
            raise CreativeSandboxError("path_forbidden", "workspace writes must use a simple relative path")
        if _SECRET_NAME_RE.search(path.name) or path.name.startswith(".env"):
            raise CreativeSandboxError("secret_path_forbidden", "secret-looking filenames are not allowed in the sandbox")
        if _SECRET_CONTENT_RE.search(content):
            raise CreativeSandboxError("secret_content_forbidden", "secret-looking content is not allowed in the sandbox")
        base = self.workspace_dir(session_id) / kind
        target = (base / path).resolve()
        try:
            target.relative_to(base.resolve())
        except ValueError as exc:
            raise CreativeSandboxError("path_forbidden", "workspace write escaped its subdirectory") from exc
        return target

    def _touch_meta(self, session_id: str) -> None:
        meta = self.load_meta(session_id)
        meta["updated_at"] = _now()
        self._write_json(self.meta_path(session_id), meta)

    @staticmethod
    def _write_json(path: Path, payload: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)


def creative_sandbox_error_payload(exc: Exception) -> tuple[int, dict[str, Any]]:
    if isinstance(exc, CreativeSandboxError):
        payload: dict[str, Any] = {"status": "failed", "error": {"code": exc.code, "message": str(exc)}}
        if exc.detail:
            payload["error"]["detail"] = exc.detail
        return exc.status, payload
    return 500, {"status": "failed", "error": {"code": "creative_sandbox_failed", "message": f"{type(exc).__name__}: {exc}"}}


def sandbox_policy() -> dict[str, Any]:
    return {
        "network": "disabled_in_v0",
        "command_execution": "not_implemented_in_api_slice",
        "writes": [f"workspaces/<session_id>/{name}/" for name in WORKSPACE_SUBDIRS],
        "core_source_writes": "forbidden",
        "secret_storage": "forbidden",
        "max_file_bytes": 512_000,
    }


def _ensure_layout(workspace_dir: Path) -> None:
    for name in WORKSPACE_SUBDIRS:
        (workspace_dir / name).mkdir(parents=True, exist_ok=True)


def _list_files(base: Path, *, workspace_dir: Path, kind: str) -> list[dict[str, Any]]:
    if not base.exists():
        return []
    items: list[dict[str, Any]] = []
    for path in base.rglob("*"):
        if path.is_file():
            items.append(_file_payload(path, workspace_dir=workspace_dir, kind=kind))
    return items


def _file_payload(path: Path, *, workspace_dir: Path, kind: str) -> dict[str, Any]:
    stat = path.stat()
    return {
        "kind": kind,
        "path": _relative_to(path, workspace_dir),
        "name": path.name,
        "size": stat.st_size,
        "updated_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
    }


def _is_preview_media(path: str) -> bool:
    return path.lower().endswith((".mp4", ".mov", ".m4v", ".webm"))


def _command_summaries(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    commands: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for event in events:
        event_type = event.get("type")
        if event_type not in {EVENT_DEV_COMMAND_STARTED, EVENT_DEV_COMMAND_FINISHED}:
            continue
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        command_id = str(payload.get("command_id") or "")
        if not command_id:
            command_id = f"event_{len(order)}"
        if command_id not in commands:
            commands[command_id] = {"command_id": command_id}
            order.append(command_id)
        summary = commands[command_id]
        if event_type == EVENT_DEV_COMMAND_STARTED:
            summary.update(
                {
                    "command": str(payload.get("command") or summary.get("command") or "")[:240],
                    "label": str(payload.get("label") or summary.get("label") or "")[:120],
                    "started_at": event.get("timestamp") or "",
                    "executed": bool(payload.get("executed", summary.get("executed", False))),
                }
            )
        else:
            summary.update(
                {
                    "finished_at": event.get("timestamp") or "",
                    "status": str(payload.get("status") or ""),
                    "exit_code": payload.get("exit_code"),
                    "duration_ms": payload.get("duration_ms"),
                    "stdout_tail": str(payload.get("stdout_tail") or "")[-1000:],
                    "stderr_tail": str(payload.get("stderr_tail") or "")[-1000:],
                    "artifact_count": payload.get("artifact_count"),
                    "executed": bool(payload.get("executed", summary.get("executed", False))),
                }
            )
            if payload.get("command") and not summary.get("command"):
                summary["command"] = str(payload.get("command") or "")[:240]
    return [commands[command_id] for command_id in order]


def _failure_summaries(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    for command in _command_summaries(events):
        status = str(command.get("status") or "")
        exit_code = command.get("exit_code")
        if status in {"failed", "blocked", "timeout"} or (isinstance(exit_code, int) and exit_code != 0):
            failures.append(
                {
                    "type": "command",
                    "command_id": command.get("command_id") or "",
                    "status": status or "failed",
                    "exit_code": exit_code,
                    "command": command.get("command") or "",
                    "stderr_tail": command.get("stderr_tail") or "",
                }
            )
    for event in events:
        if event.get("type") != EVENT_FAILED:
            continue
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        failures.append(
            {
                "type": "event",
                "status": "failed",
                "timestamp": event.get("timestamp") or "",
                "code": payload.get("code") or "",
                "message": payload.get("message") or payload.get("summary") or "",
            }
        )
    return failures


def _next_diagnostic(
    preview: dict[str, Any],
    commands: list[dict[str, Any]],
    failures: list[dict[str, Any]],
    artifacts: list[dict[str, Any]],
) -> dict[str, Any]:
    if failures:
        latest = failures[-1]
        return {
            "kind": "inspect_failure",
            "message": "Inspect the latest failed command stderr/logs, revise the script, then rerun the sandbox command.",
            "command_id": latest.get("command_id") or "",
        }
    if preview.get("has_preview"):
        return {
            "kind": "review_preview",
            "message": "Review the latest preview artifact and collect feedback before the next edit/run cycle.",
            "path": (preview.get("preview") or {}).get("path") or "",
        }
    has_script = any(str(item.get("path") or "").startswith("scripts/") for item in artifacts)
    if has_script:
        return {
            "kind": "run_script",
            "message": "Run the saved runtime script with a declared preview artifact path.",
        }
    if commands:
        return {
            "kind": "declare_preview",
            "message": "The workspace has commands but no reviewable preview; declare or write a preview artifact next.",
        }
    return {
        "kind": "write_script",
        "message": "Write a runtime script or TimelinePatch before running the sandbox.",
    }


def _report_brief(
    *,
    preview: dict[str, Any],
    commands: list[dict[str, Any]],
    failures: list[dict[str, Any]],
    artifacts: list[dict[str, Any]],
) -> dict[str, Any]:
    """Small UI-facing summary of the larger machine report."""
    preview_path = ""
    if preview.get("has_preview") and isinstance(preview.get("preview"), dict):
        preview_path = str((preview.get("preview") or {}).get("path") or "")
    if failures:
        latest = failures[-1]
        command = str(latest.get("command") or "").strip()
        return {
            "state": "failed",
            "title": "Sandbox run needs revision",
            "summary": "Latest command failed; inspect stderr/logs, edit the script, then rerun.",
            "primary_path": "",
            "next_action": "Inspect failure logs and revise the runtime script.",
            "command": command[:240],
        }
    if preview_path:
        return {
            "state": "preview_ready",
            "title": "Preview ready",
            "summary": f"Generated reviewable preview {preview_path}.",
            "primary_path": preview_path,
            "next_action": "Review the preview artifact and capture feedback for the next edit.",
            "artifact_count": len(artifacts),
        }
    script_count = sum(1 for item in artifacts if str(item.get("path") or "").startswith("scripts/"))
    if script_count:
        return {
            "state": "script_ready",
            "title": "Script ready",
            "summary": "Workspace has saved script code but no reviewable preview yet.",
            "primary_path": "",
            "next_action": "Run the script with a declared preview artifact path.",
            "script_count": script_count,
        }
    if commands:
        return {
            "state": "ran_without_preview",
            "title": "Run completed without preview",
            "summary": "Commands ran, but the workspace has no reviewable preview artifact.",
            "primary_path": "",
            "next_action": "Declare or write a preview artifact in the next run.",
            "command_count": len(commands),
        }
    return {
        "state": "empty",
        "title": "Workspace waiting for code",
        "summary": "No runtime script, command, or preview has been produced yet.",
        "primary_path": "",
        "next_action": "Write a runtime script or TimelinePatch, then run it.",
    }


def _sanitize_payload(payload: dict[str, Any]) -> dict[str, Any]:
    cleaned: dict[str, Any] = {}
    for key, value in payload.items():
        if isinstance(value, str):
            cleaned[key] = _sanitize_text(value)
        elif isinstance(value, dict):
            cleaned[key] = _sanitize_payload(value)
        elif isinstance(value, list):
            cleaned[key] = [_sanitize_text(v) if isinstance(v, str) else v for v in value[:50]]
        else:
            cleaned[key] = value
    return cleaned


def _sanitize_text(value: str) -> str:
    text = value.replace("Traceback (most recent call last):", "runtime error")
    if _SECRET_CONTENT_RE.search(text):
        return "[redacted]"
    return text[:4000]


def _relative_to(path: Path, root: Path) -> str:
    return "/".join(path.resolve().relative_to(root.resolve()).parts)


def _safe_session_id(value: str) -> str:
    value = str(value or "").strip()
    if not value:
        return ""
    allowed = "".join(ch for ch in value if ch.isalnum() or ch in {"_", "-"})
    if _SESSION_ID_RE.match(allowed):
        return allowed[:64]
    return ""


def _safe_optional_id(value: str) -> str:
    return _safe_session_id(value)


def _require_session_id(value: str) -> str:
    session_id = _safe_session_id(value)
    if not session_id:
        raise CreativeSandboxError(
            "invalid_session_id",
            "session_id must match [A-Za-z0-9][A-Za-z0-9_-]{0,63}",
        )
    return session_id


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
