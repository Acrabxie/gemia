"""HTTP routes for Lumeri v3 sessions.

    POST   /sessions                            create session (optional project_id/run_id)
    GET    /projects                            list local Projects and their sessions
    POST   /projects                            create a Project (optional local folder)
    POST   /projects/pick-folder                open the native local folder picker
    POST   /projects/{project_id}/undo|redo      file mutation history
    POST   /sessions/{id}/resume                restore a durable session runner
    GET    /sessions/{id}                       info (assets, tasks, latest_event_id, plan_mode, protocol_version)
    POST   /sessions/{id}/turn                  submit user message (202)
    POST   /sessions/{id}/steer                 guide the active turn (202)
    POST   /sessions/{id}/stop                  stop the active turn (202)
    POST   /sessions/{id}/plan_mode             toggle plan mode {"enabled": bool}
    POST   /sessions/{id}/assets                upload asset (raw body + X-Filename)
    GET    /sessions/{id}/assets                list session assets
    GET    /sessions/{id}/assets/{asset_id}     serve asset file (Range supported)
    GET    /sessions/{id}/tasks                 list background shell jobs
    POST   /sessions/{id}/tasks/{job_id}/kill   kill a background shell job
    POST   /sessions/{id}/close                 close session
    POST   /sessions/{id}/pin                   pin or unpin session
    POST   /sessions/{id}/handoff               give this session's assets to another Project session
    GET    /sessions/{id}/lumenframe/canvas     direct-manipulation metadata
    GET    /sessions/{id}/lumenframe/frame.png  canonical canvas/layer PNG
    POST   /sessions/{id}/lumenframe/op         transform / undo / redo
    GET    /sessions/{id}/segments/{clip_or_ref} expandable Clip structure
    POST   /sessions/{id}/segments/{clip_or_ref}/op|save|branch|view
    DELETE /sessions/{id}                       hide session (durable data retained)
    GET    /sessions/{id}/stream                SSE event stream (Last-Event-ID)
    GET    /sessions/{id}/transcript            durable NDJSON transcript (?since_seq=N; works after close)
    GET    /projects/{project_id}                durable project + production summary
    GET    /projects/{project_id}/runs/{run_id}  durable production run
    POST   /projects/{project_id}/runs/{run_id}/review
                                                   approve or request_changes
    GET    /projects/{project_id}/artifacts/{asset_id}
                                                   serve durable project artifact

``try_handle(handler, method=...)`` is the single entrypoint server.py
calls. Returns True if the request was handled, False to let the host
server continue routing.

Uploads: raw body POST. ``X-Filename`` header carries the original
filename (URL-encoded; Unicode safe). Size capped by
``LUMERI_V3_UPLOAD_MAX_BYTES`` (default 500 MiB).

Asset URLs: protocol v2 uses durable project URLs such as
``/projects/p-abc/artifacts/v_002``.  The v1 per-session URL remains an alias
for old Web/CLI callers and transparently resumes a sleeping durable session.
"""
from __future__ import annotations

import json
import mimetypes
import os
import re
import subprocess
import sys
import uuid
from concurrent.futures import TimeoutError as FuturesTimeoutError
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from gemia.session_manager import SessionLimitError, SessionRunner, get_manager
from gemia.transport.sse import REGISTRY as SSE_REGISTRY
from gemia.transport.sse import iter_events
from gemia.v3_contract import PROTOCOL_VERSION
from lumerai.export_support import effects_warnings, transition_warnings
from lumerai.patches import _TRANSITION_KINDS, TimelinePatchError


_DEFAULT_MAX_UPLOAD_BYTES = 500 * 1024 * 1024
_CHUNK = 64 * 1024
_SERVER_INSTANCE_ID = uuid.uuid4().hex


def _max_upload_bytes() -> int:
    try:
        return int(os.environ.get("LUMERI_V3_UPLOAD_MAX_BYTES") or _DEFAULT_MAX_UPLOAD_BYTES)
    except ValueError:
        return _DEFAULT_MAX_UPLOAD_BYTES


def try_handle(handler, *, method: str) -> bool:
    parsed = urlparse(handler.path)
    path = unquote(parsed.path).rstrip("/") or "/"
    query = parse_qs(parsed.query)

    if (
        path not in {"/sessions", "/projects"}
        and not path.startswith("/sessions/")
        and not path.startswith("/projects/")
    ):
        return False

    try:
        if method == "POST":
            return _route_post(handler, path, query)
        if method == "DELETE":
            return _route_delete(handler, path)
        if method in {"GET", "HEAD"}:
            return _route_get(handler, path, query, body=(method == "GET"))
    except (BrokenPipeError, ConnectionResetError):
        # Media elements routinely cancel an in-flight Range request while
        # seeking, reloading, or replacing their source.  Headers may already
        # contain a valid 206 response, so attempting to append a JSON 500 is
        # both false reporting and another write to the dead socket.
        return True
    except Exception as exc:
        if os.environ.get("LUMERI_V3_DEBUG_ERRORS") in {"1", "true", "TRUE"}:
            _json_error(handler, 500, f"{type(exc).__name__}: {exc}")
        else:
            _json_error(handler, 500, "internal server error")
        return True

    _json_error(handler, 405, f"method {method} not allowed on {path}")
    return True


# ── routing tables ────────────────────────────────────────────────────


def _route_post(handler, path: str, query: dict) -> bool:
    if path == "/sessions":
        return _create_session(handler)
    if path == "/projects":
        return _create_project(handler)
    if path == "/projects/pick-folder":
        return _pick_project_folder(handler)

    m = re.match(r"^/projects/([^/]+)/(undo|redo)$", path)
    if m:
        return _project_file_history_action(handler, m.group(1), m.group(2))

    m = re.match(r"^/sessions/([^/]+)/resume$", path)
    if m:
        return _resume_session(handler, m.group(1))

    m = re.match(r"^/sessions/([^/]+)/pin$", path)
    if m:
        return _set_session_pinned(handler, m.group(1))

    m = re.match(r"^/sessions/([^/]+)/handoff$", path)
    if m:
        return _handoff_session_assets(handler, m.group(1))

    m = re.match(r"^/projects/([^/]+)/runs/([^/]+)/review$", path)
    if m:
        return _review_run(handler, m.group(1), m.group(2))

    m = re.match(r"^/sessions/([^/]+)/lumenframe/op$", path)
    if m:
        runner = _session_runner(handler, m.group(1))
        if runner is None:
            return True
        return _session_lumenframe_op(handler, runner)

    # Direct-edit op endpoint (user drag/trim/split/delete) — same patch path
    # as the model's timeline_* verbs.
    m = re.match(r"^/sessions/([^/]+)/timeline/op$", path)
    if m:
        runner = _session_runner(handler, m.group(1))
        if runner is None:
            return True
        return _session_timeline_op(handler, runner)

    m = re.match(r"^/sessions/([^/]+)/segments/([^/]+)/(op|save|branch|view)$", path)
    if m:
        runner = _session_runner(handler, m.group(1))
        if runner is None:
            return True
        return _session_segment_post(handler, runner, m.group(2), m.group(3))

    # Kill a background shell job. Distinct path shape from the action verbs
    # below (carries a job_id segment), so it matches first.
    m = re.match(r"^/sessions/([^/]+)/tasks/([^/]+)/kill$", path)
    if m:
        runner = _session_runner(handler, m.group(1))
        if runner is None:
            return True
        return _kill_task(handler, runner, m.group(2))

    m = re.match(r"^/sessions/([^/]+)/(turn|steer|stop|retract|assets|close|ask_response|plan_mode|auto_title)$", path)
    if not m:
        return False
    session_id, action = m.group(1), m.group(2)
    runner = _session_runner(handler, session_id)
    if runner is None:
        return True

    if action == "turn":
        return _submit_turn(handler, runner)
    if action == "steer":
        return _steer_turn(handler, runner)
    if action == "stop":
        return _stop_turn(handler, runner)
    if action == "retract":
        return _retract_turn(handler, runner)
    if action == "assets":
        return _upload_asset(handler, runner)
    if action == "close":
        return _close_session(handler, runner)
    if action == "ask_response":
        return _ask_response(handler, runner)
    if action == "plan_mode":
        return _set_plan_mode(handler, runner)
    if action == "auto_title":
        return _auto_title(handler, runner)
    return False


def _route_delete(handler, path: str) -> bool:
    m = re.match(r"^/sessions/([^/]+)$", path)
    if not m:
        return False
    session_id = m.group(1)
    if not _valid_resource_id(session_id):
        _json_error(handler, 400, "invalid session id", code="E_INPUT")
        return True
    try:
        record = get_manager().delete_session(session_id)
    except FileNotFoundError as exc:
        _json_error(handler, 404, str(exc) or f"unknown session: {session_id}")
        return True
    except ValueError as exc:
        _json_error(handler, 409, str(exc), code="E_BUSY")
        return True
    except Exception as exc:
        if _respond_production_error(handler, exc):
            return True
        raise
    _json_response(
        handler,
        200,
        {
            "session_id": session_id,
            "deleted": True,
            "deleted_at": record.get("deleted_at"),
        },
    )
    return True


def _route_get(handler, path: str, query: dict, *, body: bool) -> bool:
    if path == "/sessions":
        compact = str((query.get("compact") or [""])[0]).lower() in {
            "1", "true", "yes", "on",
        }
        return _list_sessions(handler, compact=compact)
    if path == "/projects":
        return _list_projects(handler)

    m = re.match(r"^/projects/([^/]+)/artifacts/([^/]+)$", path)
    if m:
        return _serve_project_artifact(handler, m.group(1), m.group(2), body=body)

    m = re.match(r"^/projects/([^/]+)/runs/([^/]+)$", path)
    if m:
        return _project_run_info(handler, m.group(1), m.group(2))

    m = re.match(r"^/projects/([^/]+)$", path)
    if m:
        return _project_info(handler, m.group(1))

    m = re.match(r"^/sessions/([^/]+)/stream$", path)
    if m:
        return _sse_stream(handler, m.group(1), query, body=body)

    m = re.match(r"^/sessions/([^/]+)/transcript$", path)
    if m:
        return _session_transcript(handler, m.group(1), query, body=body)

    m = re.match(r"^/sessions/([^/]+)/lumenframe/canvas$", path)
    if m:
        return _session_lumenframe_canvas(handler, m.group(1), query)

    m = re.match(r"^/sessions/([^/]+)/lumenframe/frame\.png$", path)
    if m:
        return _session_lumenframe_frame(handler, m.group(1), query, body=body)

    m = re.match(r"^/sessions/([^/]+)/timeline$", path)
    if m:
        return _session_timeline(handler, m.group(1))

    m = re.match(r"^/sessions/([^/]+)/segments/([^/]+)$", path)
    if m:
        return _session_segment(handler, m.group(1), m.group(2))

    m = re.match(r"^/sessions/([^/]+)/quanta$", path)
    if m:
        return _session_quanta(handler, m.group(1))

    m = re.match(r"^/sessions/([^/]+)/assets/([^/]+)$", path)
    if m:
        return _serve_asset(handler, m.group(1), m.group(2), body=body)

    m = re.match(r"^/sessions/([^/]+)/assets$", path)
    if m:
        return _list_assets(handler, m.group(1))

    m = re.match(r"^/sessions/([^/]+)/tasks$", path)
    if m:
        return _list_tasks(handler, m.group(1))

    m = re.match(r"^/sessions/([^/]+)$", path)
    if m:
        return _session_info(handler, m.group(1))

    return False


# ── POST handlers ─────────────────────────────────────────────────────


def _is_remote_request(handler) -> bool:
    try:
        return str(handler.headers.get("X-Lumeri-Remote", "")).strip() == "1"
    except Exception:
        return False


def _require_local_project_access(handler) -> bool:
    if _is_remote_request(handler):
        _json_error(handler, 403, "local project folders are unavailable remotely", code="E_DENIED")
        return False
    return True


def _create_project(handler) -> bool:
    if not _require_local_project_access(handler):
        return True
    body = _read_json_body(handler)
    if body is None:
        return True
    source_root = body.get("source_root") or body.get("path")
    name = body.get("name")
    if source_root is not None and not isinstance(source_root, str):
        _json_error(handler, 400, "source_root must be a folder path string", code="E_INPUT")
        return True
    if name is not None and not isinstance(name, str):
        _json_error(handler, 400, "name must be a string", code="E_INPUT")
        return True
    try:
        project = get_manager().create_project(
            name=str(name or ""),
            source_root=(source_root.strip() if isinstance(source_root, str) and source_root.strip() else None),
        )
    except (OSError, ValueError) as exc:
        _json_error(handler, 400, str(exc), code="E_INPUT")
        return True
    _json_response(handler, 201, project)
    return True


def _pick_project_folder(handler) -> bool:
    if not _require_local_project_access(handler):
        return True
    if _read_optional_json_body(handler) is None:
        return True
    if sys.platform != "darwin":
        _json_error(handler, 501, "native folder picker is not available on this platform")
        return True
    result = subprocess.run(
        [
            "osascript",
            "-e",
            'POSIX path of (choose folder with prompt "选择 Lumeri Project 文件夹")',
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        _json_response(handler, 200, {"path": "", "cancelled": True})
        return True
    _json_response(handler, 200, {"path": result.stdout.strip(), "cancelled": False})
    return True


def _project_file_history_action(handler, project_id: str, action: str) -> bool:
    if not _require_local_project_access(handler):
        return True
    if not _valid_resource_id(project_id):
        _json_error(handler, 400, "invalid project_id", code="E_INPUT")
        return True
    if _read_optional_json_body(handler) is None:
        return True
    try:
        manager = get_manager()
        result = (
            manager.undo_project_files(project_id)
            if action == "undo"
            else manager.redo_project_files(project_id)
        )
    except FileNotFoundError as exc:
        _json_error(handler, 404, str(exc), code="E_NOT_FOUND")
        return True
    except ValueError as exc:
        _json_error(handler, 409, str(exc), code="E_PROJECT_HISTORY")
        return True
    _json_response(handler, 200, result)
    return True


def _create_session(handler) -> bool:
    body = _read_optional_json_body(handler)
    if body is None:
        return True
    project_id = body.get("project_id")
    run_id = body.get("run_id")
    fork_from_project_id = body.get("fork_from_project_id")
    reality_contract = body.get("reality_contract")
    if project_id is not None and not _valid_resource_id(project_id):
        _json_error(handler, 400, "invalid project_id", code="E_INPUT")
        return True
    if run_id is not None and not _valid_resource_id(run_id):
        _json_error(handler, 400, "invalid run_id", code="E_INPUT")
        return True
    if run_id and not project_id:
        _json_error(handler, 400, "run_id requires project_id", code="E_INPUT")
        return True
    if fork_from_project_id is not None and not _valid_resource_id(
        fork_from_project_id
    ):
        _json_error(handler, 400, "invalid fork_from_project_id", code="E_INPUT")
        return True
    if fork_from_project_id and (project_id or run_id):
        _json_error(
            handler,
            400,
            "fork_from_project_id cannot be combined with project_id or run_id",
            code="E_INPUT",
        )
        return True
    if reality_contract is not None and not isinstance(reality_contract, dict):
        _json_error(handler, 400, "reality_contract must be an object", code="E_INPUT")
        return True
    try:
        from gemia import identity

        # Per-request pin honored: a client that pins X-Lumeri-Account gets
        # its session bound to THAT account even if another client flips the
        # global active.json mid-flight.
        account_id = identity.resolve_account_id(handler)
    except Exception:
        account_id = None
    # X-Lumeri-Remote is injected by the public edge (nginx) and cannot be
    # cleared by the client; local/native callers never send it. Marks a
    # public demo session so host-dangerous tools are stripped.
    try:
        remote = str(handler.headers.get("X-Lumeri-Remote", "")).strip() == "1"
    except Exception:
        remote = False
    try:
        create_kwargs: dict[str, Any] = {"account_id": account_id, "remote": remote}
        # Only pass the v2 keywords when the client supplied them.  This keeps
        # the legacy empty POST contract compatible with test doubles and old
        # SessionManager implementations during a rolling local upgrade.
        if project_id:
            create_kwargs["project_id"] = str(project_id)
        if run_id:
            create_kwargs["run_id"] = str(run_id)
        if fork_from_project_id:
            create_kwargs["fork_from_project_id"] = str(fork_from_project_id)
        if reality_contract is not None:
            create_kwargs["reality_contract"] = reality_contract
        runner = get_manager().create_session(**create_kwargs)
    except SessionLimitError as exc:
        _json_error(handler, 503, str(exc))
        return True
    except (FileNotFoundError, ValueError) as exc:
        _json_error(handler, 404, str(exc), code="E_INPUT")
        return True
    except Exception as exc:
        if _respond_production_error(handler, exc):
            return True
        raise
    _json_response(handler, 201, _session_response_payload(runner))
    return True


def _resume_session(handler, session_id: str) -> bool:
    """Rebuild a runner from durable project/session state.

    Resuming an already-live runner is idempotent.  This endpoint is separate
    for clients that want an explicit resume acknowledgement; ordinary session
    routes also resume sleeping durable sessions transparently.
    """
    if not _valid_resource_id(session_id):
        _json_error(handler, 400, "invalid session id", code="E_INPUT")
        return True
    # Accept an empty body for navigator/sendBeacon and simple clients, but
    # reject malformed JSON when a body is actually present.
    if _read_optional_json_body(handler) is None:
        return True
    manager = get_manager()
    try:
        runner = manager.resume_session(session_id)
    except SessionLimitError as exc:
        _json_error(handler, 503, str(exc), code="E_BUSY")
        return True
    except FileNotFoundError as exc:
        _json_error(handler, 404, str(exc) or f"unknown session: {session_id}")
        return True
    except ValueError as exc:
        _json_error(handler, 409, str(exc), code="E_PRODUCTION_STATE")
        return True
    except Exception as exc:
        if _respond_production_error(handler, exc):
            return True
        raise
    if runner is None:
        _json_error(handler, 404, f"unknown session: {session_id}")
        return True
    _json_response(handler, 200, _session_response_payload(runner))
    return True


def _set_session_pinned(handler, session_id: str) -> bool:
    if not _valid_resource_id(session_id):
        _json_error(handler, 400, "invalid session id", code="E_INPUT")
        return True
    body = _read_json_body(handler)
    if body is None:
        return True
    if not isinstance(body.get("pinned"), bool):
        _json_error(handler, 400, "pinned must be a boolean", code="E_INPUT")
        return True
    try:
        record = get_manager().set_session_pinned(session_id, body["pinned"])
    except FileNotFoundError as exc:
        _json_error(handler, 404, str(exc) or f"unknown session: {session_id}")
        return True
    except Exception as exc:
        if _respond_production_error(handler, exc):
            return True
        raise
    _json_response(
        handler,
        200,
        {"session_id": session_id, "pinned": bool(record.get("pinned"))},
    )
    return True


def _handoff_session_assets(handler, source_session_id: str) -> bool:
    """Give a completed session's material to a sibling Project session.

    The manager copies only durable asset records; it never aliases the
    source timeline, design state, transcript, run or memory into the target.
    """
    if not _valid_resource_id(source_session_id):
        _json_error(handler, 400, "invalid source session id", code="E_INPUT")
        return True
    body = _read_json_body(handler)
    if body is None:
        return True
    target_session_id = body.get("target_session_id")
    if not _valid_resource_id(target_session_id):
        _json_error(handler, 400, "target_session_id is required", code="E_INPUT")
        return True
    try:
        result = get_manager().handoff_session_assets(
            source_session_id, str(target_session_id)
        )
    except FileNotFoundError as exc:
        _json_error(handler, 404, str(exc), code="E_NOT_FOUND")
        return True
    except ValueError as exc:
        _json_error(handler, 409, str(exc), code="E_HANDOFF")
        return True
    except Exception as exc:
        if _respond_production_error(handler, exc):
            return True
        raise
    _json_response(handler, 200, result)
    return True


def _session_runner(handler, session_id: str) -> SessionRunner | None:
    """Return a live runner, transparently rebuilding a durable session.

    Idle cleanup may reclaim the thread and SSE registry, but it must never
    make a creator-visible session expire.  Every route that needs a runner
    comes through this helper so the next request wakes the same durable
    project/run/session instead of returning a false 404.
    """
    if not _valid_resource_id(session_id):
        _json_error(handler, 400, "invalid session id", code="E_INPUT")
        return None
    manager = get_manager()
    runner = manager.get(session_id)
    if runner is not None:
        return runner
    resume = getattr(manager, "resume_session", None)
    if not callable(resume):
        _json_error(handler, 404, f"unknown session: {session_id}")
        return None
    try:
        runner = resume(session_id)
    except SessionLimitError as exc:
        _json_error(handler, 503, str(exc), code="E_BUSY")
        return None
    except FileNotFoundError as exc:
        _json_error(handler, 404, str(exc) or f"unknown session: {session_id}")
        return None
    except ValueError as exc:
        _json_error(handler, 409, str(exc), code="E_PRODUCTION_STATE")
        return None
    except Exception as exc:
        if _respond_production_error(handler, exc):
            return None
        raise
    if runner is None:
        _json_error(handler, 404, f"unknown session: {session_id}")
        return None
    return runner


def _review_run(handler, project_id: str, run_id: str) -> bool:
    """Record the human production gate; tool/model success cannot call this."""
    if not _valid_resource_id(project_id) or not _valid_resource_id(run_id):
        _json_error(handler, 400, "invalid project_id or run_id", code="E_INPUT")
        return True
    body = _read_json_body(handler)
    if body is None:
        return True
    action = body.get("action")
    if action not in {"approve", "request_changes"}:
        _json_error(
            handler,
            400,
            "review action must be 'approve' or 'request_changes'",
            code="E_REVIEW_INVALID",
        )
        return True
    note = body.get("note")
    if note is not None and not isinstance(note, str):
        _json_error(handler, 400, "review note must be a string", code="E_REVIEW_INVALID")
        return True
    note = str(note or "").strip()
    if action == "request_changes" and not note:
        _json_error(
            handler,
            400,
            "request_changes requires a non-empty note",
            code="E_REVIEW_INVALID",
        )
        return True

    start_sec = body.get("start_sec")
    end_sec = body.get("end_sec")
    if (start_sec is None) != (end_sec is None):
        _json_error(
            handler,
            400,
            "start_sec and end_sec must be provided together",
            code="E_REVIEW_INVALID",
        )
        return True
    if start_sec is not None:
        if (
            isinstance(start_sec, bool)
            or isinstance(end_sec, bool)
            or not isinstance(start_sec, (int, float))
            or not isinstance(end_sec, (int, float))
            or float(start_sec) < 0
            or float(end_sec) <= float(start_sec)
        ):
            _json_error(
                handler,
                400,
                "review range must satisfy 0 <= start_sec < end_sec",
                code="E_REVIEW_INVALID",
            )
            return True
        start_sec = float(start_sec)
        end_sec = float(end_sec)

    expected_revision = body.get("expected_project_revision")
    if expected_revision is not None and (
        isinstance(expected_revision, bool)
        or not isinstance(expected_revision, int)
        or expected_revision < 0
    ):
        _json_error(
            handler,
            400,
            "expected_project_revision must be a non-negative integer",
            code="E_REVIEW_INVALID",
        )
        return True

    watched_full_video = body.get("watched_full_video")
    creative_checks = body.get("creative_checks")
    if action == "approve":
        try:
            current_run = get_manager().get_run(project_id, run_id)
            acceptance = (
                (current_run.get("reality_contract") or {}).get("acceptance") or {}
            )
            dimensions = tuple(
                str(value)
                for value in (acceptance.get("creative_dimensions") or [])
                if str(value)
            )
        except Exception:
            dimensions = ()
        if not dimensions:
            dimensions = ("story", "pacing", "visual", "sound", "publishable")
        if watched_full_video is not True:
            _json_error(
                handler,
                400,
                "approve requires an explicit full-video watch confirmation",
                code="E_REVIEW_INVALID",
            )
            return True
        if not isinstance(creative_checks, dict) or any(
            type(creative_checks.get(name)) is not bool for name in dimensions
        ):
            _json_error(
                handler,
                400,
                "approve requires boolean creative checks: " + ", ".join(dimensions),
                code="E_REVIEW_INVALID",
            )
            return True
        failed_dimensions = [name for name in dimensions if creative_checks[name] is False]
        if failed_dimensions:
            _json_error(
                handler,
                400,
                "failed creative checks require request_changes: "
                + ", ".join(failed_dimensions),
                code="E_REVIEW_INVALID",
            )
            return True
        creative_checks = {name: creative_checks[name] for name in dimensions}
    else:
        watched_full_video = None
        creative_checks = None

    try:
        from gemia import identity

        reviewer_account_id = identity.resolve_account_id(handler)
    except Exception:
        reviewer_account_id = None
    try:
        result = get_manager().review_run(
            project_id,
            run_id,
            action=action,
            note=note,
            start_sec=start_sec,
            end_sec=end_sec,
            expected_project_revision=expected_revision,
            reviewer_account_id=reviewer_account_id,
            watched_full_video=watched_full_video,
            creative_checks=creative_checks,
        )
    except FileNotFoundError as exc:
        _json_error(handler, 404, str(exc) or f"unknown production run: {run_id}")
        return True
    except ValueError as exc:
        code = str(getattr(exc, "code", None) or "E_PRODUCTION_STATE")
        _json_error(handler, 409, str(exc), code=code)
        return True
    except Exception as exc:
        if _respond_production_error(handler, exc):
            return True
        raise
    public = _public_payload(result)
    if not isinstance(public, dict):
        _json_error(handler, 500, "production review returned an invalid payload")
        return True
    public.setdefault("project_id", project_id)
    public.setdefault("run_id", run_id)
    public.setdefault("action", action)
    _json_response(handler, 200, public)
    return True


def _submit_turn(handler, runner: SessionRunner) -> bool:
    body = _read_json_body(handler)
    if body is None:
        return True
    message = body.get("message")
    if not isinstance(message, str) or not message.strip():
        _json_error(handler, 400, "request body must include non-empty 'message' string")
        return True
    client_turn_id = body.get("client_turn_id")
    if client_turn_id is not None and not _valid_client_turn_id(client_turn_id):
        _json_error(handler, 400, "invalid client_turn_id", code="E_INPUT")
        return True
    expected_revision = body.get("expected_project_revision")
    if expected_revision is not None and (
        isinstance(expected_revision, bool)
        or not isinstance(expected_revision, int)
        or expected_revision < 0
    ):
        _json_error(
            handler,
            400,
            "expected_project_revision must be a non-negative integer",
            code="E_INPUT",
        )
        return True
    submit_kwargs: dict[str, Any] = {}
    if client_turn_id is not None:
        submit_kwargs["client_turn_id"] = str(client_turn_id)
    if expected_revision is not None:
        submit_kwargs["expected_project_revision"] = expected_revision
    try:
        submit_request = getattr(runner, "submit_turn_request", None)
        if callable(submit_request):
            result = submit_request(message, **submit_kwargs)
        else:
            # v1 runner fallback for rolling upgrades.  v2 runners always expose
            # submit_turn_request and enforce revision/idempotency host-side.
            result = runner.submit_turn(message)
    except Exception as exc:
        if _respond_production_error(handler, exc):
            return True
        raise
    if result is False:
        _json_error(handler, 409, "turn already in progress for this session")
        return True
    if isinstance(result, dict) and not bool(result.get("accepted", True)):
        code = str(result.get("code") or "E_BUSY")
        message_text = str(result.get("error") or "turn was not accepted")
        _json_error(handler, 409, message_text, code=code)
        return True
    payload: dict[str, Any] = {"session_id": runner.session_id, "accepted": True}
    if isinstance(result, dict):
        for key in (
            "client_turn_id",
            "scheduled",
            "duplicate",
            "turn_status",
            "idempotent",
            "project_id",
            "run_id",
            "project_revision",
            "production_state",
        ):
            if key in result:
                payload[key] = result[key]
    _json_response(handler, 202, payload)
    return True


def _steer_turn(handler, runner: SessionRunner) -> bool:
    body = _read_json_body(handler)
    if body is None:
        return True
    message = body.get("message")
    if not isinstance(message, str) or not message.strip():
        _json_error(handler, 400, "request body must include non-empty 'message' string")
        return True
    if not runner.steer_turn(message):
        _json_error(handler, 409, "no active turn to guide for this session")
        return True
    _json_response(handler, 202, {
        "session_id": runner.session_id,
        "accepted": True,
        "mode": "steer",
    })
    return True


def _stop_turn(handler, runner: SessionRunner) -> bool:
    if not runner.stop_turn():
        _json_error(handler, 409, "no active turn to stop for this session")
        return True
    _json_response(handler, 202, {
        "session_id": runner.session_id,
        "accepted": True,
        "mode": "stop",
    })
    return True


def _retract_turn(handler, runner: SessionRunner) -> bool:
    """Retract the last completed user turn. The body's optional
    ``expected_message`` is the frontend's view of that turn's text; the
    runner refuses on mismatch so a stale UI can never delete the wrong turn."""
    body = _read_json_body(handler)
    if body is None:
        return True
    expected = body.get("expected_message")
    if expected is not None and not isinstance(expected, str):
        _json_error(handler, 400, "'expected_message' must be a string when present")
        return True
    result = runner.retract_turn(expected)
    if not result.get("ok"):
        reason = result.get("reason", "nothing_to_retract")
        message = (
            "a turn is still running — stop it before retracting"
            if reason == "turn_in_progress"
            else "no retractable turn (already retracted, rewritten, or the session moved on)"
        )
        _json_error(handler, 409, message, code=reason)
        return True
    _json_response(handler, 200, {
        "session_id": runner.session_id,
        "retracted": True,
        "message": result["message"],
    })
    return True


def _ask_response(handler, runner: SessionRunner) -> bool:
    """Deliver a user's answer to a pending ``elicit`` question.

    Validates the answer against the question schema BEFORE resolving the
    bridge future. On failure the future stays pending and the user can retry.
    """
    body = _read_json_body(handler)
    if body is None:
        return True
    question_id = body.get("question_id")
    answers = body.get("answers")
    if not isinstance(question_id, str) or not question_id:
        _json_error(handler, 400, "request body must include 'question_id' string")
        return True
    if not isinstance(answers, dict):
        _json_error(handler, 400, "request body must include 'answers' object")
        return True

    question_dict = runner.get_pending_question(question_id)
    if question_dict is None:
        _json_error(handler, 404, f"no pending question: {question_id}")
        return True

    from gemia.tools.ask import AskAnswer, AskQuestion, validate_ask_answer_all

    try:
        question_obj = AskQuestion.from_dict(question_dict)
    except Exception:
        _json_response(handler, 422, {
            "error": "pending question schema is invalid",
            "code": "E_ASK_INVALID_SCHEMA",
            "question_id": question_id,
        })
        return True
    else:
        answer_obj = AskAnswer(question_id=question_id, answers=answers)
        field_errors = validate_ask_answer_all(question_obj, answer_obj)
        if field_errors:
            _json_response(handler, 422, {
                "error": "answer validation failed",
                "code": "E_ASK_INVALID_ANSWER",
                "question_id": question_id,
                "field_errors": field_errors,
            })
            return True

    delivered = runner.deliver_ask_answer(question_id, answers)
    if not delivered:
        _json_error(handler, 404, f"no pending question: {question_id}")
        return True
    _json_response(handler, 200, {"question_id": question_id, "delivered": True})
    return True


def _set_plan_mode(handler, runner: SessionRunner) -> bool:
    """Toggle the session's plan mode. The agent broadcasts a
    ``plan_mode_changed`` SSE event so every connected client stays in sync."""
    body = _read_json_body(handler)
    if body is None:
        return True
    enabled = body.get("enabled")
    if not isinstance(enabled, bool):
        _json_error(handler, 400, "request body must include boolean 'enabled'")
        return True
    state = runner.set_plan_mode(enabled)
    _json_response(handler, 200, {"session_id": runner.session_id, "plan_mode": state})
    return True


def _upload_asset(handler, runner: SessionRunner) -> bool:
    try:
        length = int(handler.headers.get("Content-Length") or "0")
    except ValueError:
        _json_error(handler, 400, "Content-Length must be an integer")
        return True
    if length <= 0:
        _json_error(handler, 400, "Content-Length required and must be > 0")
        return True
    cap = _max_upload_bytes()
    if length > cap:
        _json_error(handler, 413, f"upload too large: {length} > {cap} bytes")
        return True
    conn = getattr(handler, "connection", None)
    if conn is not None and hasattr(conn, "settimeout"):
        try:
            conn.settimeout(float(os.environ.get("LUMERI_V3_UPLOAD_TIMEOUT_SEC") or 60))
        except Exception:
            pass

    filename_raw = handler.headers.get("X-Filename") or "upload.bin"
    filename = Path(unquote(filename_raw)).name or "upload.bin"

    uploads_dir = runner.output_dir / "uploads"
    uploads_dir.mkdir(parents=True, exist_ok=True)
    temp_path = uploads_dir / f"upload-{uuid.uuid4().hex[:12]}{Path(filename).suffix}"

    bytes_read = 0
    with temp_path.open("wb") as f:
        while bytes_read < length:
            chunk = handler.rfile.read(min(_CHUNK, length - bytes_read))
            if not chunk:
                break
            f.write(chunk)
            bytes_read += len(chunk)

    if bytes_read != length:
        temp_path.unlink(missing_ok=True)
        _json_error(handler, 400, f"upload truncated: got {bytes_read} of {length} bytes")
        return True

    try:
        asset_id = runner.add_external_asset(temp_path, summary=f"user-uploaded {filename}")
    except Exception as exc:
        temp_path.unlink(missing_ok=True)
        _json_error(handler, 400, f"failed to register asset: {exc}")
        return True

    library_asset = None
    if runner.account_id:
        try:
            from gemia.media_library import import_media

            library_asset = import_media(
                runner.account_id,
                temp_path,
                original_name=filename,
                project_id=runner.project_id,
            )
        except Exception:
            # Session upload remains usable if account-library indexing fails;
            # the response makes that absence explicit instead of failing the
            # already-completed session registration.
            library_asset = None

    _json_response(handler, 201, {
        "asset_id": asset_id,
        "library_asset_id": library_asset.get("asset_id") if library_asset else None,
        "library_asset": library_asset,
        "filename": filename,
        "size_bytes": bytes_read,
        "preview_url": f"/sessions/{runner.session_id}/assets/{asset_id}",
    })
    return True


def _close_session(handler, runner: SessionRunner) -> bool:
    sid = runner.session_id
    get_manager().close_session(sid)
    _json_response(handler, 200, {"session_id": sid, "closed": True})
    return True


def _auto_title(handler, runner: SessionRunner) -> bool:
    """Generate a one-line session title from conversation messages via a
    lightweight model call. The frontend calls this after user message 1
    and 5 to auto-name sessions."""
    body = _read_json_body(handler)
    if body is None:
        return True
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        _json_error(handler, 400, "request body must include non-empty 'messages' list")
        return True

    import threading

    def _generate():
        try:
            from gemia.gemini_client import GeminiClientV3

            client = GeminiClientV3()
            digest = []
            for msg in messages[-10:]:
                role = msg.get("role", "")
                content = str(msg.get("content") or "")[:200]
                if role in ("user", "status", "assistant") and content.strip():
                    digest.append(f"{role}: {content}")
            conversation = "\n".join(digest)

            import json as _json
            import ssl
            import urllib.request

            import certifi

            api_body = {
                "model": client.model,
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "你是一个会话标题生成器。根据对话内容生成一个简短的中文标题（8-15字），"
                            "概括会话的主要主题。只输出标题本身，不要引号、标点、解释。"
                        ),
                    },
                    {"role": "user", "content": conversation},
                ],
                "stream": False,
                "temperature": 0.3,
                "max_tokens": 40,
            }

            bearer = client.api_key
            if client.provider == "vertex":
                from gemia.gemini_client import _vertex_access_token

                bearer = _vertex_access_token(client.proxy)

            headers = {
                "Authorization": f"Bearer {bearer}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://local-lumeri-desktop",
                "X-Title": "lumeri-v3-title",
            }
            req = urllib.request.Request(
                client.api_url,
                data=_json.dumps(api_body).encode("utf-8"),
                headers=headers,
                method="POST",
            )
            ssl_context = ssl.create_default_context(cafile=certifi.where())
            https_handler = urllib.request.HTTPSHandler(context=ssl_context)
            if client.proxy:
                opener = urllib.request.build_opener(
                    urllib.request.ProxyHandler({"https": client.proxy, "http": client.proxy}),
                    https_handler,
                )
            else:
                opener = urllib.request.build_opener(
                    urllib.request.ProxyHandler({}),
                    https_handler,
                )
            resp = opener.open(req, timeout=15)
            raw = resp.read().decode("utf-8")
            data = _json.loads(raw)
            choices = data.get("choices") or []
            if choices:
                title = (choices[0].get("message") or {}).get("content", "").strip()
                title = title.strip("\"'""''「」")[:60]
                if title:
                    return title
            return None
        except Exception:
            return None

    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(_generate)
        try:
            title = future.result(timeout=20)
        except Exception:
            title = None

    if title:
        _json_response(handler, 200, {"title": title})
    else:
        _json_response(handler, 200, {"title": None})
    return True


# ── GET handlers ──────────────────────────────────────────────────────


_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_CLIENT_TURN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,191}$")


def _valid_resource_id(value: Any) -> bool:
    return isinstance(value, str) and bool(_SESSION_ID_RE.fullmatch(value))


def _valid_client_turn_id(value: Any) -> bool:
    return isinstance(value, str) and bool(_CLIENT_TURN_ID_RE.fullmatch(value))


def _runner_snapshot(runner: SessionRunner) -> dict[str, Any]:
    """Return the durable public runtime snapshot when available.

    The fallback preserves v1 compatibility while the durable runtime rolls
    out.  It deliberately contains no local paths.
    """
    snapshot_fn = getattr(runner, "snapshot", None)
    if callable(snapshot_fn):
        raw = snapshot_fn()
        if isinstance(raw, dict):
            return _public_payload(raw)

    snapshot: dict[str, Any] = {}
    for key in (
        "project_id",
        "run_id",
        "project_revision",
        "production_state",
        "budget",
        "blockers",
        "delivery",
        "acceptance",
    ):
        value = getattr(runner, key, None)
        if value is not None:
            snapshot[key] = _public_payload(value)
    try:
        project = runner.agent.project
        snapshot.setdefault("project_id", project.project_id)
        meta = project.store.load_meta(project.project_id)
        snapshot.setdefault("project_revision", int(meta.get("patch_seq") or 0))
    except Exception:
        pass
    return snapshot


def _session_response_payload(runner: SessionRunner) -> dict[str, Any]:
    sid = runner.session_id
    payload: dict[str, Any] = {
        # Keep every v1 field byte-for-byte compatible for Web/CLI callers.
        "session_id": sid,
        "stream_url": f"/sessions/{sid}/stream",
        "turn_url": f"/sessions/{sid}/turn",
        "assets_url": f"/sessions/{sid}/assets",
        "close_url": f"/sessions/{sid}/close",
        "resume_url": f"/sessions/{sid}/resume",
        "protocol_version": PROTOCOL_VERSION,
    }
    payload.update(_runner_snapshot(runner))
    project_id = payload.get("project_id")
    if project_id:
        payload["project_url"] = f"/projects/{project_id}"
        payload["artifacts_url"] = f"/projects/{project_id}/artifacts"
    return payload


def _public_payload(value: Any) -> Any:
    """Make store snapshots JSON-safe without leaking host filesystem paths."""
    if isinstance(value, Path):
        return value.name
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            name = str(key)
            normalized_name = name.lower()
            if (
                normalized_name in {
                    "path",
                    "cwd",
                    "workdir",
                    "serverpath",
                    "output_dir",
                    "project_root",
                    "source_root",
                    "edit_root",
                }
                or normalized_name.endswith("_path")
                or normalized_name.endswith("_paths")
            ):
                continue
            result[name] = _public_payload(item)
        return result
    if isinstance(value, (list, tuple, set)):
        return [_public_payload(item) for item in value]
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return _public_payload(to_dict())
    return value


_PRODUCTION_ERROR_STATUS = {
    "E_NOT_FOUND": 404,
    "E_BAD_ARG": 400,
    "E_REVIEW_INVALID": 400,
    "E_REVISION_CONFLICT": 409,
    "E_IDEMPOTENCY_CONFLICT": 409,
    "E_STATE_TRANSITION": 409,
    "E_PRODUCTION_STATE": 409,
    "E_PROJECT_BUSY": 409,
    "E_BUDGET": 409,
}


def _respond_production_error(handler, exc: Exception) -> bool:
    code = str(getattr(exc, "code", "") or "")
    status = _PRODUCTION_ERROR_STATUS.get(code)
    if status is None:
        return False
    current_revision = getattr(exc, "current_revision", None)
    extra = (
        {"project_revision": int(current_revision)}
        if isinstance(current_revision, int)
        and not isinstance(current_revision, bool)
        and current_revision >= 0
        else None
    )
    _json_error(handler, status, str(exc) or code, code=code, extra=extra)
    return True


def _session_transcript(handler, session_id: str, query: dict, *, body: bool) -> bool:
    """Serve the durable event transcript (NDJSON, one {seq, ts, event} per
    line). Works for CLOSED sessions too — the transcript outlives the runner
    and the 200-event SSE replay buffer; this is the resync source for a
    client that attached late or reconnected after a restart.

    ``?since_seq=N`` skips lines with seq <= N (incremental catch-up).
    """
    if not _SESSION_ID_RE.match(session_id):
        _json_error(handler, 400, "invalid session id")
        return True
    path = get_manager().sessions_root / session_id / "transcript.jsonl"
    if not path.exists():
        _json_error(handler, 404, f"no transcript for session: {session_id}")
        return True

    since_seq = 0
    raw_since = query.get("since_seq")
    if raw_since:
        try:
            since_seq = max(0, int(raw_since[0]))
        except (TypeError, ValueError):
            _json_error(handler, 400, "since_seq must be an integer")
            return True

    handler.send_response(200)
    handler.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    if not body:
        return True
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                if since_seq:
                    try:
                        if int(json.loads(line).get("seq") or 0) <= since_seq:
                            continue
                    except (json.JSONDecodeError, ValueError):
                        continue
                handler.wfile.write(line.encode("utf-8"))
        handler.wfile.flush()
    except (BrokenPipeError, ConnectionResetError):
        pass
    return True


def _session_info(handler, session_id: str) -> bool:
    runner = _session_runner(handler, session_id)
    if runner is None:
        return True
    payload: dict[str, Any] = {
        "session_id": session_id,
        "assets": runner.list_assets(),
        "tasks": runner.list_tasks(),
        "latest_event_id": SSE_REGISTRY.latest_event_id(session_id),
        "plan_mode": runner.plan_mode,
        "turn_in_progress": runner.turn_in_progress,
        "protocol_version": PROTOCOL_VERSION,
    }
    payload.update(_runner_snapshot(runner))
    _json_response(handler, 200, payload)
    return True


def _list_sessions(handler, *, compact: bool = False) -> bool:
    """Read-only snapshot of live runners + pending async jobs (background panel).

    Avoids manager.get() on purpose — get() touches last_used_at, and a
    monitoring read must not keep sessions alive. Runner internals are read
    defensively (the agent loop is being refactored in a parallel worktree),
    so a missing attribute degrades to an empty jobs list, never a 500.
    """
    manager = get_manager()
    try:
        with manager._lock:
            runners = list(manager._runners.values())
    except AttributeError:
        runners = [r for r in (manager.get(sid) for sid in manager.list_sessions()) if r]
    sessions = []
    for runner in runners:
        jobs: list[dict[str, Any]] = []
        subagents: list[dict[str, str]] = []
        try:
            registry = runner.agent._tool_ctx.jobs
            jobs = [rec.to_dict() for rec in registry.list_pending()]
        except Exception:
            jobs = []
        try:
            active_subagents = getattr(runner.agent, "_active_subagents", {})
            subagents = [
                {
                    "agent_id": str(item.get("agent_id") or ""),
                    "goal": str(item.get("goal") or ""),
                    "tool_profile": str(item.get("tool_profile") or "full"),
                }
                for item in active_subagents.values()
                if isinstance(item, dict)
            ]
        except Exception:
            subagents = []
        item = {
            "session_id": getattr(runner, "session_id", ""),
            "project_id": getattr(runner, "project_id", "") or "",
            "account_id": getattr(runner, "account_id", "") or "",
            "created_at": getattr(runner, "created_at", None),
            "last_used_at": getattr(runner, "last_used_at", None),
            "turn_in_progress": bool(getattr(runner, "turn_in_progress", False)),
            "plan_mode": bool(getattr(runner, "plan_mode", False)),
            "pending_jobs": jobs,
            "active_subagents": subagents,
        }
        if not compact:
            item.update(_runner_snapshot(runner))
        sessions.append(item)
    sessions.sort(key=lambda s: s.get("last_used_at") or 0, reverse=True)
    _json_response(handler, 200, {"sessions": sessions})
    return True


def _list_projects(handler) -> bool:
    if not _require_local_project_access(handler):
        return True
    projects = get_manager().list_projects()
    _json_response(handler, 200, {"projects": projects})
    return True


def _project_info(handler, project_id: str) -> bool:
    if not _valid_resource_id(project_id):
        _json_error(handler, 400, "invalid project_id", code="E_INPUT")
        return True
    try:
        payload = get_manager().get_project(project_id)
    except FileNotFoundError as exc:
        _json_error(handler, 404, str(exc) or f"unknown project: {project_id}")
        return True
    except Exception as exc:
        if _respond_production_error(handler, exc):
            return True
        raise
    if payload is None:
        _json_error(handler, 404, f"unknown project: {project_id}")
        return True
    public = _public_payload(payload)
    if not isinstance(public, dict):
        _json_error(handler, 500, "project store returned an invalid payload")
        return True
    public.setdefault("project_id", project_id)
    _json_response(handler, 200, public)
    return True


def _project_run_info(handler, project_id: str, run_id: str) -> bool:
    if not _valid_resource_id(project_id) or not _valid_resource_id(run_id):
        _json_error(handler, 400, "invalid project_id or run_id", code="E_INPUT")
        return True
    try:
        payload = get_manager().get_run(project_id, run_id)
    except FileNotFoundError as exc:
        _json_error(handler, 404, str(exc) or f"unknown production run: {run_id}")
        return True
    except Exception as exc:
        if _respond_production_error(handler, exc):
            return True
        raise
    if payload is None:
        _json_error(handler, 404, f"unknown production run: {run_id}")
        return True
    public = _public_payload(payload)
    if not isinstance(public, dict):
        _json_error(handler, 500, "production store returned an invalid payload")
        return True
    public.setdefault("project_id", project_id)
    public.setdefault("run_id", run_id)
    _json_response(handler, 200, public)
    return True


def _serve_project_artifact(
    handler,
    project_id: str,
    asset_id: str,
    *,
    body: bool,
) -> bool:
    if not _valid_resource_id(project_id) or not _valid_resource_id(asset_id):
        _json_error(handler, 400, "invalid project_id or asset_id", code="E_INPUT")
        return True
    try:
        path = get_manager().artifact_path(project_id, asset_id)
    except FileNotFoundError:
        path = None
    except Exception as exc:
        if _respond_production_error(handler, exc):
            return True
        raise
    if path is None:
        _json_error(handler, 404, f"unknown project artifact: {asset_id}")
        return True
    resolved = Path(path).expanduser().resolve()
    if not resolved.is_file():
        _json_error(handler, 404, f"project artifact is unavailable: {asset_id}")
        return True
    _serve_file_with_range(handler, resolved, body=body)
    return True


def _timeline_payload_dict(session_id: str, project_id: str, project: dict, meta: dict) -> dict[str, Any]:
    """Build the compact timeline JSON payload (shared by GET timeline + POST op)."""
    timeline = project.get("timeline") if isinstance(project.get("timeline"), dict) else {}
    assets_list = project.get("assets") or []
    asset_map = {
        str(a.get("id") or a.get("asset_id") or ""): a
        for a in assets_list
        if isinstance(a, dict)
    }
    tracks_raw = timeline.get("tracks") or []
    clips_raw = timeline.get("clips") or []
    clips_by_track: dict[str, list[dict]] = {}
    for clip in clips_raw:
        if not isinstance(clip, dict):
            continue
        tid = str(clip.get("track_id") or "")
        asset = asset_map.get(str(clip.get("asset_id") or "")) or {}
        clips_by_track.setdefault(tid, []).append({
            "id": str(clip.get("id") or ""),
            # asset_id is surfaced so the frontend can fetch the clip's source
            # media (/projects/{pid}/artifacts/{asset_id}; session alias kept)
            # for filmstrip + waveform.
            "asset_id": str(clip.get("asset_id") or ""),
            "name": str(clip.get("name") or asset.get("name") or "clip"),
            "start": float(clip.get("start") or 0.0),
            "duration": float(clip.get("duration") or 0.1),
            "source_in": float(clip.get("source_in") or 0.0),
            "source_out": float(clip.get("source_out") or 0.0),
            "media_kind": str(clip.get("media_kind") or "video"),
            "track_id": tid,
            "enabled": bool(clip.get("enabled", True)),
            "effects": clip.get("effects") if isinstance(clip.get("effects"), dict) else {},
            "text_config": clip.get("text_config") if isinstance(clip.get("text_config"), dict) else None,
            # lumerai stores the outgoing transition as clip["transition_after"]
            # (patches.py _op_add_transition); the payload key stays "transition"
            # for both frontends. Reading the old "transition" key surfaced
            # nothing, ever — add_transition looked applied but was invisible.
            "transition": clip.get("transition_after") if isinstance(clip.get("transition_after"), dict) else None,
            # Keep the structural handle visible next to the flattened preview
            # so the client can open the same Clip as an editable container.
            "segment_ref": str(clip.get("segment_ref") or "") or None,
            "provenance": clip.get("provenance") if isinstance(clip.get("provenance"), dict) else None,
            "asset_metadata": asset.get("metadata") if isinstance(asset.get("metadata"), dict) else {},
        })
    for clips in clips_by_track.values():
        clips.sort(key=lambda c: float(c.get("start") or 0.0))

    tracks = []
    emitted_track_ids: set[str] = set()
    for track in tracks_raw:
        if not isinstance(track, dict):
            continue
        tid = str(track.get("id") or "")
        if not tid:
            continue
        emitted_track_ids.add(tid)
        tracks.append({
            "id": tid,
            "kind": str(track.get("kind") or "video"),
            "name": str(track.get("name") or tid),
            "duck_under": track.get("duck_under") if isinstance(track.get("duck_under"), str) else None,
            "clips": clips_by_track.get(tid, []),
        })
    for tid in sorted(clips_by_track):
        if tid in emitted_track_ids:
            continue
        clips = clips_by_track[tid]
        kinds = {str(c.get("media_kind") or "") for c in clips if isinstance(c, dict)}
        if "audio" in kinds:
            kind = "audio"
        else:
            kind = "video"
        label = {"audio": "Audio"}.get(kind, "Video")
        tracks.append({
            "id": tid,
            "kind": kind,
            "name": f"{label} {tid}",
            "duck_under": None,
            "clips": clips,
        })

    try:
        from gemia.segment_document import segment_manifest
        structural_manifest = [
            segment_manifest(project, clip)
            for clip in clips_raw
            if isinstance(clip, dict)
        ]
    except Exception:
        structural_manifest = []
    return {
        "session_id": session_id,
        "project_id": project_id,
        "patch_seq": int(meta.get("patch_seq") or 0),
        "duration": float(timeline.get("duration") or 0.0),
        "fps": float(timeline.get("fps") or 30.0),
        "width": int(timeline.get("width") or 1920),
        "height": int(timeline.get("height") or 1080),
        "can_undo": int(meta.get("patch_seq") or 0) > 0,
        "can_redo": bool(meta.get("redo_stack") or []),
        "tracks": tracks,
        "segment_manifest": structural_manifest,
        # The storyboard IR rides the timeline payload so the web outline
        # panel refreshes through the existing poll + timeline_op force-fetch
        # (set_shotlist/update_shot land in the same patch log as clip ops).
        "shotlist": project.get("shotlist") if isinstance(project.get("shotlist"), dict) else None,
    }


def _session_timeline(handler, session_id: str) -> bool:
    """Return the current project timeline as a JSON payload for the frontend."""
    runner = _session_runner(handler, session_id)
    if runner is None:
        return True
    try:
        project = runner.agent.project.load()
        meta = runner.agent.project.store.load_meta(runner.agent.project.project_id)
    except Exception as exc:
        _json_error(handler, 500, f"could not load project: {exc}")
        return True
    payload = _timeline_payload_dict(
        session_id, runner.agent.project.project_id, project, meta,
    )
    cached_revision = getattr(runner, "cached_project_revision", None)
    if cached_revision is not None:
        payload["project_revision"] = int(cached_revision)
    _json_response(handler, 200, payload)
    return True


def _segment_clip(project: dict[str, Any], segment_key: str, clip_id: str | None = None) -> tuple[dict[str, Any], str]:
    """Resolve either a Clip id or a SegmentDocument ref without writing."""
    clips = (project.get("timeline") or {}).get("clips") or []
    wanted = str(clip_id or segment_key or "")
    for clip in clips:
        if not isinstance(clip, dict):
            continue
        if str(clip.get("id") or "") == wanted or str(clip.get("segment_ref") or "") == wanted:
            return clip, str(clip.get("id") or "")
    raise KeyError(wanted)


def _segment_payload(runner: SessionRunner, project: dict[str, Any], segment_key: str, *, clip_id: str | None = None) -> dict[str, Any]:
    from gemia.segment_document import persisted_segment, segment_manifest

    clip, resolved_clip_id = _segment_clip(project, segment_key, clip_id)
    doc = persisted_segment(project, clip, create=False)
    meta = runner.agent.project.store.load_meta(runner.agent.project.project_id)
    return {
        "session_id": runner.session_id,
        "project_id": runner.agent.project.project_id,
        "clip_id": resolved_clip_id,
        "segment_ref": str(clip.get("segment_ref") or "") or None,
        "project_revision": int(meta.get("patch_seq") or 0),
        "segment": doc,
        "manifest": segment_manifest(project, clip),
        "agent_status": {
            "message": "你正在编辑" if doc.get("reservations") else "可以细化",
            "reservations": [
                {"entity_ref": str(ref), "owner": str(value.get("owner") or "")}
                for ref, value in (doc.get("reservations") or {}).items()
                if isinstance(value, dict)
            ],
        },
    }


def _session_segment(handler, session_id: str, segment_key: str) -> bool:
    runner = _session_runner(handler, session_id)
    if runner is None:
        return True
    try:
        project = runner.agent.project.load()
        payload = _segment_payload(runner, project, segment_key)
    except KeyError:
        _json_error(handler, 404, "片段不存在", code="E_NOT_FOUND")
        return True
    except Exception as exc:
        _json_error(handler, 500, f"could not load segment: {exc}")
        return True
    _json_response(handler, 200, payload, headers={"Cache-Control": "no-store"})
    return True


def _session_segment_post(handler, runner: SessionRunner, segment_key: str, action: str) -> bool:
    body = _read_json_body(handler)
    if body is None:
        return True
    try:
        project = runner.agent.project.load()
        clip, clip_id = _segment_clip(project, segment_key, body.get("clip_id"))
    except KeyError:
        _json_error(handler, 404, "片段不存在", code="E_NOT_FOUND")
        return True
    except Exception as exc:
        _json_error(handler, 500, f"could not resolve segment: {exc}")
        return True
    if action == "view":
        # View state is deliberately outside creative history. The client
        # instance id prevents a second window from echoing its own update.
        event = {
            "kind": "segment_view",
            "session_id": runner.session_id,
            "clip_id": clip_id,
            "segment_id": str(clip.get("segment_ref") or segment_key),
            "client_instance_id": str(body.get("client_instance_id") or ""),
            "view": body.get("view") if isinstance(body.get("view"), dict) else {},
        }
        SSE_REGISTRY.emit(runner.session_id, event)
        _json_response(handler, 200, {"ok": True, "view": event["view"]})
        return True
    client_op_id = str(body.get("client_op_id") or "").strip() or None
    if not client_op_id:
        _json_error(handler, 400, "片段创作操作需要唯一 client_op_id", code="E_BAD_ARG")
        return True
    expected_project_revision = body.get("expected_project_revision")
    if expected_project_revision is not None and (isinstance(expected_project_revision, bool) or not isinstance(expected_project_revision, int) or expected_project_revision < 0):
        _json_error(handler, 400, "expected_project_revision must be a non-negative integer", code="E_BAD_ARG")
        return True
    current_doc = _segment_payload(runner, project, segment_key, clip_id=clip_id)["segment"]
    if "expected_segment_revision" not in body:
        _json_error(handler, 400, "片段创作操作需要 expected_segment_revision", code="E_BAD_ARG")
        return True
    expected_segment_revision = body.get("expected_segment_revision")
    edit = dict(body)
    edit.update({
        "op": "segment_edit",
        "clip_id": clip_id,
        "action": "save" if action == "save" else ("branch" if action == "branch" else str(body.get("action") or body.get("edit") or "set_layer")),
        "expected_segment_revision": expected_segment_revision,
        "client_op_id": client_op_id,
        "actor": str(body.get("actor") or "human"),
    })
    if action == "branch" and not edit.get("branch_id"):
        edit["branch_id"] = f"branch_{uuid.uuid4().hex[:8]}"
    try:
        def _apply():
            return runner.agent.project.apply_ops(
                [edit], label=f"segment:{edit.get('action')}", client_op_id=client_op_id,
            )
        runner.run_project_edit(_apply, expected_project_revision=expected_project_revision)
    except TimelinePatchError as exc:
        status = 409 if exc.code in {"E_SEGMENT_REVISION_CONFLICT", "E_ENTITY_RESERVED", "E_REVISION_CONFLICT"} else (404 if exc.code == "E_NOT_FOUND" else 400)
        _json_error(handler, status, exc.message, code=exc.code)
        return True
    except Exception as exc:
        if _respond_production_error(handler, exc):
            return True
        _json_error(handler, 500, f"failed to apply segment edit: {exc}")
        return True
    try:
        updated = runner.agent.project.load()
        payload = _segment_payload(runner, updated, segment_key, clip_id=clip_id)
        if action == "branch":
            branch_id = str(edit.get("branch_id") or "")
            payload["branch_id"] = branch_id
            payload["branch"] = (updated.get("segments") or {}).get(branch_id)
        payload["saved"] = action == "save"
        payload["branch_ready"] = action == "branch"
        if action == "save":
            SSE_REGISTRY.emit(runner.session_id, {"kind": "segment_saved", "clip_id": clip_id, "segment_id": payload.get("segment_ref") or segment_key})
        if action == "branch":
            SSE_REGISTRY.emit(runner.session_id, {"kind": "segment_branch_ready", "clip_id": clip_id, "segment_id": payload.get("segment_ref") or segment_key, "branch_id": edit.get("branch_id")})
    except Exception as exc:
        _json_error(handler, 500, f"segment edit landed but could not reload: {exc}")
        return True
    _json_response(handler, 200, payload, headers={"Cache-Control": "no-store"})
    return True


# ── LumenFrame direct-manipulation canvas ─────────────────────────────


def _query_int(query: dict | None, key: str, default: int = 0) -> int:
    raw = ((query or {}).get(key) or [default])[0]
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key} must be an integer") from exc


def _session_lumenframe_canvas(
    handler, session_id: str, query: dict | None = None,
) -> bool:
    runner = _session_runner(handler, session_id)
    if runner is None:
        return True
    try:
        from gemia.lumenframe_canvas import canvas_metadata

        payload = canvas_metadata(runner.agent._tool_ctx, frame=_query_int(query, "frame"))
    except ValueError as exc:
        code = str(getattr(exc, "code", "E_BAD_ARG"))
        status = 404 if code == "E_NOT_FOUND" else 400
        _json_error(handler, status, str(getattr(exc, "message", exc)), code=code)
        return True
    except Exception as exc:
        _json_error(handler, 500, f"could not render lumenframe canvas metadata: {exc}")
        return True
    _json_response(handler, 200, payload, headers={"Cache-Control": "no-store"})
    return True


def _session_lumenframe_frame(
    handler,
    session_id: str,
    query: dict | None = None,
    *,
    body: bool = True,
) -> bool:
    runner = _session_runner(handler, session_id)
    if runner is None:
        return True
    params = query or {}
    mode = str((params.get("mode") or ["composite"])[0] or "composite")
    layer_id = str((params.get("layer_id") or [""])[0] or "") or None
    revision = str((params.get("revision") or [""])[0] or "") or None
    try:
        from gemia.lumenframe_canvas import render_canvas_png
        from gemia.tools.layer import LumenframeRevisionConflict

        actual_frame, current_revision, png = render_canvas_png(
            runner.agent._tool_ctx,
            frame=_query_int(params, "frame"),
            mode=mode,
            layer_id=layer_id,
            revision=revision,
        )
    except LumenframeRevisionConflict as exc:
        _json_error(
            handler,
            409,
            str(exc),
            code=exc.code,
            extra={"current_revision": exc.current_revision},
        )
        return True
    except ValueError as exc:
        code = str(getattr(exc, "code", "E_BAD_ARG"))
        status = 404 if code == "E_NOT_FOUND" else 400
        _json_error(handler, status, str(getattr(exc, "message", exc)), code=code)
        return True
    except Exception as exc:
        _json_error(handler, 500, f"could not render lumenframe frame: {exc}")
        return True

    immutable = revision is not None and revision == current_revision
    _bytes_response(
        handler,
        200,
        png,
        content_type="image/png",
        body=body,
        headers={
            "Cache-Control": (
                "private, max-age=31536000, immutable" if immutable else "no-store"
            ),
            "ETag": f'"{current_revision}"',
            "X-Lumeri-Frame": str(actual_frame),
        },
    )
    return True


def _session_lumenframe_op(handler, runner: SessionRunner) -> bool:
    body = _read_json_body(handler)
    if body is None:
        return True
    try:
        from gemia.lumenframe_canvas import apply_canvas_operation
        from gemia.tools.layer import LumenframeRevisionConflict

        payload = runner.run_project_edit(
            lambda: apply_canvas_operation(runner.agent._tool_ctx, body),
        )
    except LumenframeRevisionConflict as exc:
        _json_error(
            handler,
            409,
            str(exc),
            code=exc.code,
            extra={"current_revision": exc.current_revision},
        )
        return True
    except FuturesTimeoutError:
        _json_error(
            handler,
            503,
            "edit is queued behind a long-running step; refresh before retrying",
            code="E_BUSY",
        )
        return True
    except OSError as exc:
        _json_error(handler, 500, str(exc), code="E_PERSIST")
        return True
    except ValueError as exc:
        code = str(getattr(exc, "code", "E_BAD_ARG"))
        status = 404 if code == "E_NOT_FOUND" else (409 if code == "E_LAYER_LOCKED" else 400)
        _json_error(handler, status, str(getattr(exc, "message", exc)), code=code)
        return True
    except Exception as exc:
        code = str(getattr(exc, "code", "E_UNKNOWN"))
        status = 400 if code.startswith("E_") and code != "E_UNKNOWN" else 500
        _json_error(handler, status, str(getattr(exc, "message", exc)), code=code)
        return True
    _json_response(handler, 200, payload, headers={"Cache-Control": "no-store"})
    return True


def _session_quanta(handler, session_id: str) -> bool:
    """Return the current Project's canonical discrete state tree."""
    runner = _session_runner(handler, session_id)
    if runner is None:
        return True
    try:
        project = runner.agent.project.load()
        meta = runner.agent.project.store.load_meta(runner.agent.project.project_id)
        from gemia.quanta.traverse import lift_flat_quanta

        quanta = lift_flat_quanta(project.get("quanta"))
    except Exception as exc:
        _json_error(handler, 500, f"could not load quanta: {exc}")
        return True
    payload = {
        "session_id": session_id,
        "project_id": runner.agent.project.project_id,
        "patch_seq": int(meta.get("patch_seq") or 0),
        "quanta": quanta,
    }
    cached_revision = getattr(runner, "cached_project_revision", None)
    if cached_revision is not None:
        payload["project_revision"] = int(cached_revision)
    _json_response(handler, 200, payload)
    return True


# User direct-edit op tokens -> the same patches.py ops the model's verbs emit.
# ``set_effects`` also carries the direct-UI BLEND op (effects.blend_mode) and the
# PIP op (effects.scale/x/y); ``add_transition`` carries the CROSSFADE op.
_USER_EDIT_OPS = {
    "move", "trim", "trim_head", "split", "delete", "set_time",
    "set_effects", "add_transition",
}


def _expected_edit_revision(body: dict[str, Any]) -> int | None:
    if "expected_project_revision" not in body:
        return None
    value = body.get("expected_project_revision")
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("expected_project_revision must be a non-negative integer")
    return value


def _build_user_edit_op(op_name: str, clip_id: str, body: dict) -> dict[str, Any]:
    """Map a structured user edit to one patches.py op dict.

    Raises ValueError for a malformed request (bad/missing params) -> 400; the
    op's own E_* validation happens later in apply_ops (TimelinePatchError).
    """
    prov = {"source": "user_direct_edit"}
    ripple = bool(body.get("ripple", False))

    def _num(key: str) -> float:
        try:
            return float(body[key])
        except (TypeError, ValueError):
            raise ValueError(f"{op_name}.{key} must be a number") from None

    if op_name == "move":
        op: dict[str, Any] = {"op": "move_clip", "clip_id": clip_id, "ripple": ripple, "provenance": prov}
        if body.get("start") is not None:
            op["start"] = _num("start")
        if body.get("track_id"):
            op["track_id"] = str(body["track_id"])
        return op
    if op_name == "trim":
        op = {"op": "trim_clip", "clip_id": clip_id, "ripple": ripple, "provenance": prov}
        if body.get("source_in") is not None:
            op["source_in"] = _num("source_in")
        if body.get("source_out") is not None:
            op["source_out"] = _num("source_out")
        return op
    if op_name == "trim_head":
        if body.get("start") is None or body.get("source_in") is None:
            raise ValueError("trim_head requires 'start' and 'source_in'")
        # The route expands this compatibility token into one atomic patch;
        # returning the values here keeps number validation in one place.
        return {
            "op": "trim_head",
            "clip_id": clip_id,
            "start": _num("start"),
            "source_in": _num("source_in"),
            "provenance": prov,
        }
    if op_name == "split":
        if body.get("at_time") is None:
            raise ValueError("split requires 'at_time'")
        return {"op": "split_clip", "clip_id": clip_id, "at_time": _num("at_time"), "provenance": prov}
    if op_name == "delete":
        return {"op": "delete_clip", "clip_id": clip_id, "ripple": ripple, "provenance": prov}
    if op_name == "set_time":
        op = {"op": "set_clip_time", "clip_id": clip_id, "ripple": ripple, "provenance": prov}
        if body.get("start") is not None:
            op["start"] = _num("start")
        if body.get("duration") is not None:
            op["duration"] = _num("duration")
        return op
    if op_name == "set_effects":
        effects = body.get("effects")
        if not isinstance(effects, dict):
            raise ValueError("set_effects requires an 'effects' object")
        return {"op": "set_clip_effects", "clip_id": clip_id, "effects": effects, "provenance": prov}
    if op_name == "add_transition":
        # Direct-UI CROSSFADE op -> lumerai _op_add_transition. The patch op's own
        # E_BAD_ARG validation covers adjacency/duration; we only pre-check that the
        # kind is a known transition so a typo fails fast as a 400 here.
        kind = str(body.get("kind") or "")
        if kind not in _TRANSITION_KINDS:
            raise ValueError(
                f"add_transition.kind must be one of {sorted(_TRANSITION_KINDS)}, got {kind!r}"
            )
        op = {"op": "add_transition", "clip_id": clip_id, "kind": kind, "provenance": prov}
        if body.get("duration_sec") is not None:
            op["duration_sec"] = _num("duration_sec")
        return op
    raise ValueError(f"unhandled op '{op_name}'")


def _session_timeline_op(handler, runner: SessionRunner) -> bool:
    """Apply ONE user direct-edit op through the same ProjectStore/patch path as
    the model's verbs. Emits a ``timeline_op`` SSE event (via ProjectHandle's
    on_patch) and returns the post-state in the GET /timeline shape."""
    body = _read_json_body(handler)
    if body is None:
        return True
    try:
        expected_revision = _expected_edit_revision(body)
    except ValueError as exc:
        _json_error(handler, 400, str(exc), code="E_BAD_ARG")
        return True
    op_name = str(body.get("op") or "")
    if op_name in {"undo", "redo"}:
        return _apply_user_history(handler, runner, body, direction=op_name)
    if op_name not in _USER_EDIT_OPS:
        _json_error(handler, 400, f"unknown op '{op_name}'; valid: {sorted(_USER_EDIT_OPS)} or undo/redo")
        return True
    clip_id = str(body.get("clip_id") or "")
    if not clip_id:
        _json_error(handler, 400, "timeline op requires 'clip_id'")
        return True
    try:
        patch_op = _build_user_edit_op(op_name, clip_id, body)
    except ValueError as exc:
        _json_error(handler, 400, str(exc), code="E_BAD_ARG")
        return True

    project = runner.agent.project
    client_op_id = str(body.get("client_op_id") or "").strip() or None
    # A response may be lost after the durable patch has landed.  Replaying the
    # same gesture must return the authoritative post-state before checking the
    # now-stale revision, rather than applying it twice or surfacing a conflict.
    if client_op_id and project.store.client_op_seen(project.project_id, client_op_id):
        project_state = project.load()
        meta = project.store.load_meta(project.project_id)
        payload = _timeline_payload_dict(
            runner.session_id, project.project_id, project_state, meta,
        )
        cached_revision = getattr(runner, "cached_project_revision", None)
        if cached_revision is not None:
            payload["project_revision"] = int(cached_revision)
        payload["duplicate"] = True
        _json_response(handler, 200, payload)
        return True
    try:
        # SAME path as the verbs: ProjectStore append-only patch log + undo,
        # and ProjectHandle.on_patch emits the timeline_op SSE event. Hopped
        # onto the session loop (run_project_edit) so user edits serialize
        # with agent verbs and their SSE emits stay ordered.
        edit_kwargs = (
            {"expected_project_revision": expected_revision}
            if expected_revision is not None
            else {}
        )
        def _apply_edit():
            if op_name != "trim_head":
                return project.apply_ops(
                    [patch_op], label=f"user_edit:{op_name}",
                    client_op_id=client_op_id,
                )
            current = project.load()
            clip = next(
                (
                    item for item in (current.get("timeline", {}).get("clips") or [])
                    if str(item.get("id") or "") == clip_id
                ),
                None,
            )
            if clip is None:
                raise TimelinePatchError("E_NOT_FOUND", f"clip not found: {clip_id}")
            move = {
                "op": "move_clip", "clip_id": clip_id,
                "start": patch_op["start"], "ripple": False,
                "provenance": patch_op["provenance"],
            }
            trim = {
                "op": "trim_clip", "clip_id": clip_id,
                "source_in": patch_op["source_in"], "ripple": False,
                "provenance": patch_op["provenance"],
            }
            # Expanding left must move first to free the old start; shrinking
            # right must trim first to free the new start. Both ops persist as
            # one patch and therefore one undo/redo step.
            ops = [move, trim] if patch_op["start"] < float(clip.get("start") or 0.0) else [trim, move]
            return project.apply_ops(
                ops, label="user_edit:trim_head", client_op_id=client_op_id,
            )

        runner.run_project_edit(
            _apply_edit,
            **edit_kwargs,
        )
    except TimelinePatchError as exc:
        _json_error(handler, 400, exc.message, code=exc.code)
        return True
    except FuturesTimeoutError:
        _json_error(
            handler, 503,
            "edit is queued behind a long-running step and has not applied yet — "
            "refresh the timeline to see whether it landed",
            code="E_BUSY",
        )
        return True
    except Exception as exc:
        if _respond_production_error(handler, exc):
            return True
        _json_error(handler, 500, f"failed to apply edit: {exc}")
        return True

    try:
        project_state = project.load()
        meta = project.store.load_meta(project.project_id)
    except Exception as exc:
        _json_error(handler, 500, f"applied, but could not reload project: {exc}")
        return True
    payload = _timeline_payload_dict(
        runner.session_id, project.project_id, project_state, meta,
    )
    cached_revision = getattr(runner, "cached_project_revision", None)
    if cached_revision is not None:
        payload["project_revision"] = int(cached_revision)
    # Export honesty (docs/timeline-canonical-plan.md §4): the 200 response
    # carries write-time warnings when the edit stored fields the exporter
    # will not render today. Warn, never reject.
    warnings = _user_edit_warnings(op_name, clip_id, body, project_state)
    if warnings:
        payload["warnings"] = warnings
    _json_response(handler, 200, payload)
    return True


def _user_edit_warnings(
    op_name: str, clip_id: str, body: dict, project_state: dict
) -> list[str]:
    """Write-time export-honesty warnings for one applied user edit."""
    if op_name == "add_transition":
        return transition_warnings(str(body.get("kind") or ""))
    if op_name == "set_effects":
        clip = next(
            (
                c
                for c in (project_state.get("timeline", {}).get("clips") or [])
                if str(c.get("id")) == clip_id
            ),
            None,
        )
        media_kind = str((clip or {}).get("media_kind") or "video")
        effects = body.get("effects")
        return effects_warnings(media_kind, effects if isinstance(effects, dict) else {})
    return []


def _apply_user_history(
    handler, runner: SessionRunner, body: dict, *, direction: str,
) -> bool:
    """Undo/redo timeline patches through the serialized Project edit path."""
    try:
        steps = int(body.get("steps") or 1)
    except (TypeError, ValueError):
        _json_error(handler, 400, f"{direction} 'steps' must be an integer")
        return True
    try:
        expected_revision = _expected_edit_revision(body)
    except ValueError as exc:
        _json_error(handler, 400, str(exc), code="E_BAD_ARG")
        return True
    project = runner.agent.project
    try:
        edit_kwargs = (
            {"expected_project_revision": expected_revision}
            if expected_revision is not None
            else {}
        )
        runner.run_project_edit(
            lambda: getattr(project, direction)(max(1, min(steps, 50))),
            **edit_kwargs,
        )
    except FuturesTimeoutError:
        _json_error(
            handler, 503,
            f"{direction} is queued behind a long-running step and has not applied yet — "
            "refresh the timeline to see whether it landed",
            code="E_BUSY",
        )
        return True
    except Exception as exc:
        if _respond_production_error(handler, exc):
            return True
        _json_error(handler, 400, f"{direction} failed: {exc}")
        return True
    try:
        project_state = project.load()
        meta = project.store.load_meta(project.project_id)
    except Exception as exc:
        _json_error(handler, 500, f"{direction} applied, but could not reload project: {exc}")
        return True
    payload = _timeline_payload_dict(
        runner.session_id, project.project_id, project_state, meta,
    )
    cached_revision = getattr(runner, "cached_project_revision", None)
    if cached_revision is not None:
        payload["project_revision"] = int(cached_revision)
    _json_response(handler, 200, payload)
    return True


def _list_assets(handler, session_id: str) -> bool:
    runner = _session_runner(handler, session_id)
    if runner is None:
        return True
    _json_response(handler, 200, {"assets": runner.list_assets()})
    return True


def _list_tasks(handler, session_id: str) -> bool:
    runner = _session_runner(handler, session_id)
    if runner is None:
        return True
    _json_response(handler, 200, {"tasks": runner.list_tasks()})
    return True


def _kill_task(handler, runner: SessionRunner, job_id: str) -> bool:
    """Kill a background shell job. Idempotent: killing an already-finished
    job returns its terminal state rather than erroring."""
    try:
        result = runner.kill_task(job_id)
    except KeyError:
        _json_error(handler, 404, f"unknown job: {job_id}")
        return True
    except ValueError as exc:
        # A real job whose kind has no local process (e.g. a video LRO): a
        # client error, not an internal fault — surface it as 400 instead of
        # letting it escape to the generic 500 handler.
        _json_error(handler, 400, str(exc))
        return True
    _json_response(handler, 200, {"session_id": runner.session_id, **result})
    return True


def _serve_asset(handler, session_id: str, asset_id: str, *, body: bool) -> bool:
    runner = _session_runner(handler, session_id)
    if runner is None:
        return True
    path = runner.asset_path(asset_id)
    if path is None or not Path(path).exists():
        _json_error(handler, 404, f"unknown asset: {asset_id}")
        return True
    _serve_file_with_range(handler, Path(path), body=body)
    return True


def _sse_stream(handler, session_id: str, query: dict, *, body: bool) -> bool:
    # Idle cleanup unregisters the old SSE buffer together with the sleeping
    # runner.  Wake the durable session before opening the stream so reconnect
    # never attaches to a missing registry and silently looks expired.
    if _session_runner(handler, session_id) is None:
        return True
    last_id_raw = handler.headers.get("Last-Event-ID")
    if last_id_raw is None:
        q_last = query.get("last_event_id")
        last_id_raw = q_last[0] if q_last else None
    try:
        last_id = int(last_id_raw) if last_id_raw is not None else None
    except (TypeError, ValueError):
        last_id = None
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("X-Accel-Buffering", "no")
    handler.end_headers()
    if not body:
        return True
    try:
        # Per-connection hello frame. Deliberately NOT pushed through the SSE
        # registry (it would consume a replay-buffer event id and be replayed
        # out of order on reconnect) and deliberately id-LESS, so neither the
        # browser EventSource nor the CLI parser advances Last-Event-ID on it.
        # Plain data frame (no `event:` name): both frontends dispatch it like
        # any other kind and warn (non-blocking) on version mismatch.
        hello = json.dumps(
            {
                "kind": "protocol_hello",
                "protocol_version": PROTOCOL_VERSION,
                "server_instance_id": _SERVER_INSTANCE_ID,
            },
            ensure_ascii=False,
        )
        handler.wfile.write(f"data: {hello}\n\n".encode("utf-8"))
        handler.wfile.flush()
        for chunk in iter_events(session_id, last_event_id=last_id):
            handler.wfile.write(chunk)
            handler.wfile.flush()
    except (BrokenPipeError, ConnectionResetError):
        pass
    return True


# ── helpers ───────────────────────────────────────────────────────────


def _serve_file_with_range(handler, path: Path, *, body: bool) -> None:
    file_size = path.stat().st_size
    mime, _ = mimetypes.guess_type(str(path))
    mime = mime or "application/octet-stream"

    range_header = (handler.headers.get("Range") or "").strip()
    start: int
    end: int
    use_range = False
    if range_header and range_header.startswith("bytes="):
        spec = range_header[len("bytes="):]
        if "," in spec:
            # HTTP servers may ignore multi-range requests instead of
            # generating multipart/byteranges. Serving the full body keeps
            # media playback compatible without pretending the range failed.
            spec = ""
        try:
            if spec:
                start_s, end_s = spec.split("-", 1)
                if start_s:
                    start = int(start_s)
                    end = int(end_s) if end_s else file_size - 1
                elif end_s:
                    suffix = int(end_s)
                    if suffix <= 0:
                        raise ValueError
                    start = max(0, file_size - suffix)
                    end = file_size - 1
                else:
                    raise ValueError
            else:
                start = 0
                end = file_size - 1
        except ValueError:
            handler.send_response(416)
            handler.send_header("Content-Range", f"bytes */{file_size}")
            handler.send_header("Accept-Ranges", "bytes")
            handler.end_headers()
            return
        if file_size <= 0 or start < 0 or start >= file_size or start > end:
            handler.send_response(416)
            handler.send_header("Content-Range", f"bytes */{file_size}")
            handler.send_header("Accept-Ranges", "bytes")
            handler.end_headers()
            return
        end = min(end, file_size - 1)
        use_range = bool(spec)
    else:
        start = 0
        end = file_size - 1

    content_length = end - start + 1
    handler.send_response(206 if use_range else 200)
    handler.send_header("Content-Type", mime)
    handler.send_header("Content-Length", str(content_length))
    handler.send_header("Accept-Ranges", "bytes")
    if use_range:
        handler.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
    handler.end_headers()
    if not body:
        return
    with path.open("rb") as f:
        if start:
            f.seek(start)
        remaining = content_length
        while remaining > 0:
            chunk = f.read(min(_CHUNK, remaining))
            if not chunk:
                break
            handler.wfile.write(chunk)
            remaining -= len(chunk)


def _read_json_body(handler) -> dict[str, Any] | None:
    try:
        length = int(handler.headers.get("Content-Length") or "0")
    except ValueError:
        _json_error(handler, 400, "Content-Length must be an integer")
        return None
    if length <= 0:
        _json_error(handler, 400, "missing JSON body")
        return None
    raw = handler.rfile.read(length)
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        _json_error(handler, 400, f"invalid JSON: {exc}")
        return None
    if not isinstance(data, dict):
        _json_error(handler, 400, "request body must be a JSON object")
        return None
    return data


def _read_optional_json_body(handler) -> dict[str, Any] | None:
    """Read an object body when present; treat a zero-length POST as ``{}``.

    POST /sessions has accepted an empty body since v1 and the CLI depends on
    that shape.  Protocol v2 adds optional identifiers without making the old
    request invalid.
    """
    raw_length = handler.headers.get("Content-Length")
    if raw_length in (None, ""):
        return {}
    try:
        length = int(raw_length)
    except (TypeError, ValueError):
        _json_error(handler, 400, "Content-Length must be an integer")
        return None
    if length == 0:
        return {}
    if length < 0:
        _json_error(handler, 400, "Content-Length must be non-negative")
        return None
    return _read_json_body(handler)


def _bytes_response(
    handler,
    status: int,
    data: bytes,
    *,
    content_type: str,
    body: bool = True,
    headers: dict[str, str] | None = None,
) -> None:
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(data)))
    for key, value in (headers or {}).items():
        handler.send_header(str(key), str(value))
    handler.end_headers()
    if body:
        handler.wfile.write(data)


def _json_response(
    handler,
    status: int,
    payload: dict[str, Any],
    *,
    headers: dict[str, str] | None = None,
) -> None:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    _bytes_response(
        handler,
        status,
        data,
        content_type="application/json; charset=utf-8",
        headers=headers,
    )


def _json_error(
    handler,
    status: int,
    message: str,
    *,
    code: str | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    payload: dict[str, Any] = {"error": message}
    if code:
        payload["code"] = code
    if extra:
        payload.update(extra)
    _json_response(handler, status, payload)


__all__ = ["try_handle"]
