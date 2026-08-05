"""Local session history storage for Gemia UI state."""
from __future__ import annotations

import json
import math
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from gemia.artifacts import is_document_artifact_output, is_media_output

SESSION_SCHEMA_VERSION = 2
SESSION_ROOT = Path.home() / ".gemia" / "sessions"
CURRENT_SESSION_PATH = SESSION_ROOT / "current.json"
SNAPSHOT_ROOT = SESSION_ROOT / "history"
MAX_SNAPSHOTS = 200
PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROJECT_FILE_ROOT_ORDER = ("outputs", "frames", "styled", "demo", "inputs", "uploads", "temp", "timeline")
PROJECT_FILE_ROOTS = set(PROJECT_FILE_ROOT_ORDER)
TRANSIENT_STATUS_TYPES = {"planning", "executing", "asking"}
TERMINAL_TASK_STATUSES = {"succeeded", "preview_ready", "artifact_ready", "failed", "cancelled"}


def empty_session(account_id: str | None = None) -> dict[str, Any]:
    now = _utc_now()
    return {
        "version": SESSION_SCHEMA_VERSION,
        "account_id": account_id,
        "session_id": "current",
        # ``session_id`` above remains the snapshot/UI id for backward
        # compatibility.  The v3 runtime id must be stored separately because
        # load_session_snapshot() replaces session_id with the snapshot file
        # name.  Conflating the two was why history could restore chat but not
        # the real project/SSE session.
        "v3_session_id": None,
        "project_id": None,
        "run_id": None,
        "project_revision": 0,
        "production_state": None,
        "chat_only": True,
        "title": "Gemia Session",
        "created_at": now,
        "updated_at": now,
        "messages": [],
        "timeline_markers": [],
        "project_state": None,
        "project": None,
        "server_video_path": None,
        "video_src": None,
        "pending_ask": None,
        "creative_runtime_task_id": None,
    }


def load_current_session(account_id: str | None = None) -> dict[str, Any]:
    """Load the current local UI session, or return an empty one."""
    _ensure_roots(account_id)
    current_path = current_session_path(account_id)
    if not current_path.exists():
        return empty_session(account_id)
    try:
        payload = json.loads(current_path.read_text(encoding="utf-8"))
    except Exception:
        return empty_session(account_id)
    return _normalize_session(payload, account_id=account_id)


def save_current_session(payload: dict[str, Any], account_id: str | None = None) -> dict[str, Any]:
    """Persist the current local UI session and write a compact snapshot."""
    _ensure_roots(account_id)
    session = _normalize_session(payload, account_id=account_id)
    if not session.get("created_at"):
        session["created_at"] = _utc_now()
    session["updated_at"] = _utc_now()
    _atomic_write_json(current_session_path(account_id), session)
    _write_snapshot(session, account_id=account_id)
    _prune_snapshots(account_id)
    return session


def list_session_snapshots(limit: int = 30, account_id: str | None = None) -> list[dict[str, Any]]:
    """Return recent snapshot metadata without full chat/project payloads."""
    _ensure_roots(account_id)
    items: list[dict[str, Any]] = []
    seen_linked_sessions: set[tuple[str, str, str]] = set()
    snapshots = snapshot_root(account_id)
    for path in sorted(snapshots.glob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        linked_identity = _linked_snapshot_identity(payload)
        if linked_identity is not None:
            if linked_identity in seen_linked_sessions:
                continue
            seen_linked_sessions.add(linked_identity)
        items.append(
            {
                "id": path.stem,
                "path": str(path),
                "title": str(payload.get("title") or "Gemia Session"),
                "updated_at": str(payload.get("updated_at") or ""),
                "message_count": len(payload.get("messages") or []),
                "clip_count": _clip_count(payload.get("project") or payload.get("project_state")),
                "v3_session_id": _optional_str(payload.get("v3_session_id")),
                "project_id": _optional_str(payload.get("project_id")),
                "run_id": _optional_str(payload.get("run_id")),
                "project_revision": _non_negative_int(payload.get("project_revision")),
                "production_state": _optional_str(payload.get("production_state")),
                "chat_only": bool(payload.get("chat_only", not payload.get("project_id"))),
            }
        )
        if len(items) >= max(int(limit), 1):
            break
    return items


def load_session_snapshot(
    snapshot_id: str,
    account_id: str | None = None,
    *,
    activate: bool = False,
) -> dict[str, Any]:
    """Load one saved UI session snapshot.

    When ``activate`` is true, copy the snapshot into ``current.json`` so a
    browser refresh keeps the opened history item instead of jumping back to
    the previously active session.
    """
    _ensure_roots(account_id)
    path = _snapshot_path(snapshot_id, account_id=account_id)
    if not path.exists():
        raise FileNotFoundError(snapshot_id)
    payload = json.loads(path.read_text(encoding="utf-8"))
    session = _normalize_session(payload, account_id=account_id)
    session["session_id"] = path.stem
    if activate:
        _atomic_write_json(current_session_path(account_id), session)
    return session


def current_session_path(account_id: str | None = None) -> Path:
    return session_root(account_id) / "current.json"


def snapshot_root(account_id: str | None = None) -> Path:
    return session_root(account_id) / "history"


def session_root(account_id: str | None = None) -> Path:
    return SESSION_ROOT


def _normalize_session(payload: dict[str, Any], account_id: str | None = None) -> dict[str, Any]:
    session = empty_session(account_id)
    if isinstance(payload, dict):
        session.update({key: value for key, value in payload.items() if key in session})
    session["version"] = SESSION_SCHEMA_VERSION
    session["account_id"] = account_id
    session["session_id"] = str(session.get("session_id") or "current")
    session["v3_session_id"] = _optional_str(session.get("v3_session_id"))
    session["project_id"] = _optional_str(session.get("project_id"))
    session["run_id"] = _optional_str(session.get("run_id"))
    session["project_revision"] = _non_negative_int(session.get("project_revision"))
    session["production_state"] = _optional_str(session.get("production_state"))
    # Old snapshots without a project remain readable as chat only.  A durable
    # project reference must never masquerade as chat-only: its complete
    # session/project/run tuple is validated by the fail-closed resume path.
    session["chat_only"] = not bool(session["project_id"])
    session["title"] = _session_title(session)
    session["messages"] = _messages(session.get("messages"))
    session["timeline_markers"] = _timeline_markers(session.get("timeline_markers"))
    session["creative_runtime_task_id"] = _optional_str(session.get("creative_runtime_task_id"))
    session["messages"] = _settle_transient_status_messages(
        session["messages"],
        session["creative_runtime_task_id"],
    )
    session["project_state"] = session.get("project_state") if isinstance(session.get("project_state"), dict) else None
    if session["project_state"]:
        session["project_state"] = _sanitize_project_state_media_refs(session["project_state"])
        session["project_state"] = _repair_project_state_from_runtime_task(
            session["project_state"],
            session["creative_runtime_task_id"],
        )
        session["project_state"] = _hydrate_project_state_output_from_runtime_task(
            session["project_state"],
            session["creative_runtime_task_id"],
        )
    raw_project = session.get("project") if isinstance(session.get("project"), dict) else None
    if raw_project or session["project_state"]:
        from gemia.project_model import normalize_project

        session["project"] = normalize_project(raw_project, project_state=session["project_state"], account_id=account_id)
    else:
        session["project"] = None
    session["server_video_path"] = _optional_non_artifact_str(session.get("server_video_path"))
    session["video_src"] = _optional_non_artifact_str(session.get("video_src"))
    task_output = _first_task_media_output(session["creative_runtime_task_id"])
    if task_output and not session["server_video_path"]:
        session["server_video_path"] = _optional_non_artifact_str(task_output)
    if task_output and not session["video_src"]:
        task_output_rel = _project_relative_file_ref(task_output)
        session["video_src"] = _optional_non_artifact_str(f"/file/{task_output_rel}" if task_output_rel else task_output)
    pending_ask = session.get("pending_ask")
    session["pending_ask"] = pending_ask if isinstance(pending_ask, dict) else None
    return session


def _messages(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    messages: list[dict[str, Any]] = []
    for item in value[-300:]:
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        content = item.get("content")
        if role not in {"user", "status"} or not isinstance(content, str):
            continue
        migrated_content = _migrate_legacy_artifact_message(content[:8000]) if role == "status" else content[:8000]
        messages.append(
            {
                "id": str(item.get("id") or f"restored_{len(messages)}"),
                "role": role,
                "content": migrated_content,
                "statusType": _optional_str(item.get("statusType")),
                "timestamp": int(item.get("timestamp") or 0),
            }
        )
    return messages


def _timeline_markers(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    markers: list[dict[str, Any]] = []
    for item in value[:500]:
        if not isinstance(item, dict):
            continue
        try:
            time_seconds = float(item.get("time"))
        except (TypeError, ValueError):
            continue
        if not math.isfinite(time_seconds) or time_seconds < 0:
            continue
        markers.append(
            {
                "time": round(time_seconds, 3),
                "label": str(item.get("label") or "")[:160],
                "color": str(item.get("color") or "")[:32],
            }
        )
    return markers


def _settle_transient_status_messages(messages: list[dict[str, Any]], task_id: str | None) -> list[dict[str, Any]]:
    status = _task_status(task_id)
    if status not in TERMINAL_TASK_STATUSES:
        return messages
    settled = "error" if status in {"failed", "cancelled"} else "done"
    settled_messages: list[dict[str, Any]] = []
    for message in messages:
        if message.get("role") == "status" and message.get("statusType") in TRANSIENT_STATUS_TYPES:
            settled_messages.append({**message, "statusType": settled})
        else:
            settled_messages.append(message)
    return settled_messages


def _task_status(task_id: str | None) -> str | None:
    payload = _task_payload(task_id)
    status = payload.get("status") if payload else None
    return str(status) if isinstance(status, str) else None


def _task_payload(task_id: str | None) -> dict[str, Any] | None:
    if not task_id or not re.match(r"^[A-Za-z0-9_.-]+$", task_id):
        return None
    try:
        payload = json.loads((PROJECT_ROOT / "tasks" / f"{task_id}.json").read_text(encoding="utf-8"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _first_task_media_output(task_id: str | None) -> str | None:
    payload = _task_payload(task_id)
    if not payload:
        return None
    values = []
    for key in ("outputs", "all_outputs"):
        value = payload.get(key)
        if isinstance(value, list):
            values.extend(str(item) for item in value if item)
    for output in values:
        if is_media_output(output) and not _is_invalid_restored_media_ref(output):
            return output
    return None


def _task_output_duration(task_id: str | None, output: str) -> float:
    payload = _task_payload(task_id)
    if not payload:
        return 3.0
    render_passes = payload.get("render_passes")
    if not isinstance(render_passes, list):
        return 3.0
    for render_pass in render_passes:
        if not isinstance(render_pass, dict):
            continue
        refs = {str(render_pass.get("output_path") or ""), str(render_pass.get("preview_path") or "")}
        if output not in refs:
            continue
        layers = render_pass.get("layers")
        if not isinstance(layers, list):
            return 3.0
        end_times: list[float] = []
        for layer in layers:
            timing = layer.get("timing") if isinstance(layer, dict) else None
            if not isinstance(timing, dict):
                continue
            start = _float_value(timing.get("start"))
            duration = _float_value(timing.get("duration"))
            if duration > 0:
                end_times.append(start + duration)
        return max(end_times) if end_times else 3.0
    return 3.0


def _sanitize_project_state_media_refs(project_state: dict[str, Any]) -> dict[str, Any]:
    state = dict(project_state)
    clips = state.get("clips")
    if isinstance(clips, list):
        sanitized: list[Any] = []
        for clip in clips:
            if not isinstance(clip, dict):
                sanitized.append(clip)
                continue
            item = dict(clip)
            for key in ("serverPath", "source_path", "previewSrc", "thumbnailSrc"):
                if key in item and _is_invalid_restored_media_ref(item.get(key)):
                    item[key] = ""
            strip = item.get("thumbnailStrip")
            if isinstance(strip, list):
                item["thumbnailStrip"] = [
                    value for value in strip if not _is_invalid_restored_media_ref(value)
                ]
            sanitized.append(item)
        state["clips"] = sanitized
    return state


def _repair_project_state_from_runtime_task(project_state: dict[str, Any], task_id: str | None) -> dict[str, Any]:
    if not _project_state_has_invalid_trim(project_state):
        return project_state
    task_project_state = _task_project_state(task_id)
    task_clips = task_project_state.get("clips") if isinstance(task_project_state, dict) else None
    if not isinstance(task_clips, list):
        return project_state

    by_id = {
        str(clip.get("id")): clip
        for clip in task_clips
        if isinstance(clip, dict) and clip.get("id")
    }
    by_name = {
        str(clip.get("name")): clip
        for clip in task_clips
        if isinstance(clip, dict) and clip.get("name")
    }
    clips = project_state.get("clips")
    if not isinstance(clips, list):
        return project_state

    repaired: list[Any] = []
    changed = False
    for clip in clips:
        if not isinstance(clip, dict):
            repaired.append(clip)
            continue
        source = by_id.get(str(clip.get("id"))) or by_name.get(str(clip.get("name")))
        if source and _clip_has_invalid_trim(clip) and not _clip_has_invalid_trim(source):
            fixed = dict(clip)
            for key in ("duration", "inPoint", "outPoint", "trimmed"):
                if key in source:
                    fixed[key] = source[key]
            repaired.append(fixed)
            changed = True
        else:
            repaired.append(clip)
    if not changed:
        return project_state
    state = dict(project_state)
    state["clips"] = repaired
    return state


def _hydrate_project_state_output_from_runtime_task(project_state: dict[str, Any], task_id: str | None) -> dict[str, Any]:
    clips = project_state.get("clips")
    if not isinstance(clips, list) or clips:
        return project_state
    output = _first_task_media_output(task_id)
    if not output:
        return project_state
    rel = _project_relative_file_ref(output)
    if not rel or _is_invalid_restored_media_ref(output):
        return project_state
    name = Path(output).name
    clip_id = f"task_output_{_slug(task_id or name)[:48]}"
    duration = _task_output_duration(task_id, output)
    state = dict(project_state)
    state["clips"] = [
        {
            "id": clip_id,
            "assetId": "",
            "name": name,
            "mediaKind": "video",
            "mimeType": "video/mp4",
            "trackId": "V1",
            "duration": duration,
            "inPoint": 0,
            "outPoint": duration,
            "trimmed": False,
            "serverPath": output,
            "previewSrc": f"/file/{rel}",
            "thumbnailStrip": [],
            "waveformPeaks": [],
            "effects": {},
        }
    ]
    state["selectedClipId"] = clip_id
    state["playhead"] = 0
    state["updatedAt"] = _utc_now()
    return state


def _project_state_has_invalid_trim(project_state: dict[str, Any]) -> bool:
    clips = project_state.get("clips")
    return isinstance(clips, list) and any(
        _clip_has_invalid_trim(clip) for clip in clips if isinstance(clip, dict)
    )


def _clip_has_invalid_trim(clip: dict[str, Any]) -> bool:
    in_point = _float_value(clip.get("inPoint") or clip.get("source_in"))
    out_point = _float_value(clip.get("outPoint") or clip.get("source_out"))
    trimmed = bool(clip.get("trimmed")) or in_point > 0.01
    return bool(trimmed and out_point > 0 and out_point <= in_point)


def _task_project_state(task_id: str | None) -> dict[str, Any] | None:
    task_id = _optional_str(task_id)
    if not task_id or not re.fullmatch(r"task_[A-Za-z0-9_-]+", task_id):
        return None
    path = PROJECT_ROOT / "tasks" / f"{task_id}.json"
    try:
        task = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    project_state = task.get("project_state")
    return project_state if isinstance(project_state, dict) else None


def _float_value(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _optional_valid_media_ref(value: Any) -> str | None:
    text = _optional_str(value)
    if text and _is_invalid_restored_media_ref(text):
        return None
    return text


def _optional_non_artifact_str(value: Any) -> str | None:
    return _optional_valid_media_ref(value)


def _is_document_artifact_value(value: Any) -> bool:
    text = _optional_str(value)
    return bool(text and is_document_artifact_output(text))


def _is_invalid_restored_media_ref(value: Any) -> bool:
    text = _optional_str(value)
    if not text:
        return False
    if _is_document_artifact_value(text):
        return True
    if text.startswith("blob:"):
        return True
    local_path = _local_media_path_from_ref(text)
    return bool(local_path and is_media_output(text) and not local_path.exists())


def _local_media_path_from_ref(text: str) -> Path | None:
    try:
        parsed = urlparse(text)
    except ValueError:
        parsed = None
    if parsed and parsed.scheme == "file":
        return Path(unquote(parsed.path)).expanduser()
    if parsed and parsed.scheme in {"http", "https"}:
        path = unquote(parsed.path)
        if path.startswith("/file/"):
            return _safe_project_file_path(path[len("/file/"):])
        return None
    if text.startswith("/file/"):
        return _safe_project_file_path(text[len("/file/"):])

    try:
        candidate = Path(text).expanduser()
    except (TypeError, ValueError):
        return None
    if candidate.is_absolute():
        return candidate
    return _safe_project_file_path(text)


def _safe_project_file_path(rel: str) -> Path | None:
    try:
        parts = Path(rel).parts
    except (TypeError, ValueError):
        return None
    if not parts or parts[0] not in PROJECT_FILE_ROOTS or ".." in parts:
        return None
    try:
        resolved = (PROJECT_ROOT / rel).resolve()
        resolved.relative_to(PROJECT_ROOT.resolve())
    except (OSError, ValueError):
        return None
    return resolved


def _project_relative_file_ref(value: str) -> str | None:
    text = str(value or "").replace("\\", "/")
    for root in PROJECT_FILE_ROOT_ORDER:
        marker = f"/{root}/"
        if marker in text:
            return f"{root}/{text.split(marker, 1)[1]}"
        if text.startswith(f"{root}/"):
            return text
    return None


def _migrate_legacy_artifact_message(content: str) -> str:
    if ".lumeri-dev-brief" not in content:
        return content
    if "Cannot open video:" in content:
        return "旧错误已修复：开发 brief 是文档产物，Lumeri 现在不会再把它作为视频预览打开。"
    if "渲染成可看的小样" in content:
        return "旧记录已更正：这一轮生成的是开发 brief 文档，不是可播放小样。"
    return content


def _session_title(session: dict[str, Any]) -> str:
    title = _optional_str(session.get("title"))
    if title and title != "Gemia Session":
        return title[:120]
    for message in _messages(session.get("messages")):
        if message.get("role") == "user":
            content = str(message.get("content") or "").strip()
            if content:
                return content[:80]
    project_state = session.get("project_state")
    project = session.get("project")
    if isinstance(project, dict):
        title = _optional_str(project.get("title"))
        if title and title != "Untitled Project":
            return title[:80]
    if isinstance(project_state, dict):
        clips = project_state.get("clips")
        if isinstance(clips, list) and clips:
            first = clips[0]
            if isinstance(first, dict) and first.get("name"):
                return str(first["name"])[:80]
    return "Gemia Session"


def _write_snapshot(session: dict[str, Any], account_id: str | None = None) -> None:
    linked_identity = _linked_snapshot_identity(session)
    if linked_identity is not None:
        snapshots = snapshot_root(account_id)
        for path in sorted(
            snapshots.glob("*.json"),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        ):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if _linked_snapshot_identity(payload) == linked_identity:
                # A durable v3 session/run is one history item.  Auto-save and
                # resume update that item in place instead of manufacturing a
                # new timestamped duplicate on every browser reload.
                _atomic_write_json(path, session)
                return
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    slug = _slug(session.get("title") or "session")
    _atomic_write_json(snapshot_root(account_id) / f"{timestamp}-{slug}.json", session)


def _linked_snapshot_identity(payload: Any) -> tuple[str, str, str] | None:
    if not isinstance(payload, dict):
        return None
    identity = tuple(
        str(payload.get(key) or "").strip()
        for key in ("v3_session_id", "project_id", "run_id")
    )
    return identity if all(identity) else None


def _prune_snapshots(account_id: str | None = None) -> None:
    snapshots = sorted(snapshot_root(account_id).glob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True)
    for path in snapshots[MAX_SNAPSHOTS:]:
        try:
            path.unlink()
        except OSError:
            pass


def _snapshot_path(snapshot_id: str, account_id: str | None = None) -> Path:
    raw = str(snapshot_id or "").strip()
    if not raw or "/" in raw or "\\" in raw:
        raise FileNotFoundError(snapshot_id)
    root = snapshot_root(account_id).resolve()
    candidate = (root / f"{raw}.json").resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise FileNotFoundError(snapshot_id) from exc
    return candidate


def _ensure_roots(account_id: str | None = None) -> None:
    root = session_root(account_id)
    snapshots = snapshot_root(account_id)
    root.mkdir(parents=True, exist_ok=True)
    snapshots.mkdir(parents=True, exist_ok=True)
    try:
        root.chmod(0o700)
        snapshots.chmod(0o700)
    except OSError:
        pass


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temp_name, path)
        path.chmod(0o600)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def _clip_count(project_state: Any) -> int:
    if not isinstance(project_state, dict):
        return 0
    try:
        from gemia.project_model import clip_count

        count = clip_count(project_state)
        if count:
            return count
    except Exception:
        pass
    clips = project_state.get("clips")
    return len(clips) if isinstance(clips, list) else 0


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


def _non_negative_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _slug(value: Any) -> str:
    text = re.sub(r"[^a-zA-Z0-9\u4e00-\u9fff]+", "-", str(value)).strip("-")
    return (text or "session")[:60]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = [
    "CURRENT_SESSION_PATH",
    "SESSION_ROOT",
    "current_session_path",
    "empty_session",
    "list_session_snapshots",
    "load_current_session",
    "load_session_snapshot",
    "save_current_session",
    "session_root",
    "snapshot_root",
]
