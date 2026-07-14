"""HTTP routes for Lumeri v3 sessions.

    POST   /sessions                            create session
    GET    /sessions/{id}                       info (assets, latest_event_id, plan_mode, protocol_version)
    POST   /sessions/{id}/turn                  submit user message (202)
    POST   /sessions/{id}/plan_mode             toggle plan mode {"enabled": bool}
    POST   /sessions/{id}/assets                upload asset (raw body + X-Filename)
    GET    /sessions/{id}/assets                list session assets
    GET    /sessions/{id}/assets/{asset_id}     serve asset file (Range supported)
    POST   /sessions/{id}/close                 close session
    GET    /sessions/{id}/stream                SSE event stream (Last-Event-ID)
    GET    /sessions/{id}/transcript            durable NDJSON transcript (?since_seq=N; works after close)

``try_handle(handler, method=...)`` is the single entrypoint server.py
calls. Returns True if the request was handled, False to let the host
server continue routing.

Uploads: raw body POST. ``X-Filename`` header carries the original
filename (URL-encoded; Unicode safe). Size capped by
``LUMERI_V3_UPLOAD_MAX_BYTES`` (default 500 MiB).

Asset URLs: per-session, e.g. ``/sessions/v3-abc/assets/v_002``. The
frontend constructs these from ``asset_id`` returned in SSE
``tool_exec_result.result.asset_id`` events; no separate global asset
table.
"""
from __future__ import annotations

import json
import mimetypes
import os
import re
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


def _max_upload_bytes() -> int:
    try:
        return int(os.environ.get("LUMERI_V3_UPLOAD_MAX_BYTES") or _DEFAULT_MAX_UPLOAD_BYTES)
    except ValueError:
        return _DEFAULT_MAX_UPLOAD_BYTES


def try_handle(handler, *, method: str) -> bool:
    parsed = urlparse(handler.path)
    path = unquote(parsed.path).rstrip("/") or "/"
    query = parse_qs(parsed.query)

    if path != "/sessions" and not path.startswith("/sessions/"):
        return False

    try:
        if method == "POST":
            return _route_post(handler, path, query)
        if method in {"GET", "HEAD"}:
            return _route_get(handler, path, query, body=(method == "GET"))
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

    # Direct-edit op endpoint (user drag/trim/split/delete) — same patch path
    # as the model's timeline_* verbs.
    m = re.match(r"^/sessions/([^/]+)/timeline/op$", path)
    if m:
        runner = get_manager().get(m.group(1))
        if runner is None:
            _json_error(handler, 404, f"unknown session: {m.group(1)}")
            return True
        return _session_timeline_op(handler, runner)

    m = re.match(r"^/sessions/([^/]+)/(turn|assets|close|ask_response|plan_mode)$", path)
    if not m:
        return False
    session_id, action = m.group(1), m.group(2)
    runner = get_manager().get(session_id)
    if runner is None:
        _json_error(handler, 404, f"unknown session: {session_id}")
        return True

    if action == "turn":
        return _submit_turn(handler, runner)
    if action == "assets":
        return _upload_asset(handler, runner)
    if action == "close":
        return _close_session(handler, runner)
    if action == "ask_response":
        return _ask_response(handler, runner)
    if action == "plan_mode":
        return _set_plan_mode(handler, runner)
    return False


def _route_get(handler, path: str, query: dict, *, body: bool) -> bool:
    if path == "/sessions":
        return _list_sessions(handler)

    m = re.match(r"^/sessions/([^/]+)/stream$", path)
    if m:
        return _sse_stream(handler, m.group(1), query, body=body)

    m = re.match(r"^/sessions/([^/]+)/transcript$", path)
    if m:
        return _session_transcript(handler, m.group(1), query, body=body)

    m = re.match(r"^/sessions/([^/]+)/timeline$", path)
    if m:
        return _session_timeline(handler, m.group(1))

    m = re.match(r"^/sessions/([^/]+)/assets/([^/]+)$", path)
    if m:
        return _serve_asset(handler, m.group(1), m.group(2), body=body)

    m = re.match(r"^/sessions/([^/]+)/assets$", path)
    if m:
        return _list_assets(handler, m.group(1))

    m = re.match(r"^/sessions/([^/]+)$", path)
    if m:
        return _session_info(handler, m.group(1))

    return False


# ── POST handlers ─────────────────────────────────────────────────────


def _create_session(handler) -> bool:
    try:
        from gemia import identity

        # Per-request pin honored: a client that pins X-Lumeri-Account gets
        # its session bound to THAT account even if another client flips the
        # global active.json mid-flight.
        account_id = identity.resolve_account_id(handler)
    except Exception:
        account_id = None
    try:
        runner = get_manager().create_session(account_id=account_id)
    except SessionLimitError as exc:
        _json_error(handler, 503, str(exc))
        return True
    sid = runner.session_id
    _json_response(handler, 201, {
        "session_id": sid,
        "stream_url": f"/sessions/{sid}/stream",
        "turn_url":   f"/sessions/{sid}/turn",
        "assets_url": f"/sessions/{sid}/assets",
        "close_url":  f"/sessions/{sid}/close",
    })
    return True


def _submit_turn(handler, runner: SessionRunner) -> bool:
    body = _read_json_body(handler)
    if body is None:
        return True
    message = body.get("message")
    if not isinstance(message, str) or not message.strip():
        _json_error(handler, 400, "request body must include non-empty 'message' string")
        return True
    if not runner.submit_turn(message):
        _json_error(handler, 409, "turn already in progress for this session")
        return True
    _json_response(handler, 202, {"session_id": runner.session_id, "accepted": True})
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
        # Schema changed between emit and answer — accept as-is to avoid wedge.
        pass
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

    _json_response(handler, 201, {
        "asset_id": asset_id,
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


# ── GET handlers ──────────────────────────────────────────────────────


_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


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
    runner = get_manager().get(session_id)
    if runner is None:
        _json_error(handler, 404, f"unknown session: {session_id}")
        return True
    _json_response(handler, 200, {
        "session_id": session_id,
        "assets": runner.list_assets(),
        "latest_event_id": SSE_REGISTRY.latest_event_id(session_id),
        "plan_mode": runner.plan_mode,
        "protocol_version": PROTOCOL_VERSION,
    })
    return True


def _list_sessions(handler) -> bool:
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
        try:
            registry = runner.agent._tool_ctx.jobs
            jobs = [rec.to_dict() for rec in registry.list_pending()]
        except Exception:
            jobs = []
        sessions.append({
            "session_id": getattr(runner, "session_id", ""),
            "account_id": getattr(runner, "account_id", "") or "",
            "created_at": getattr(runner, "created_at", None),
            "last_used_at": getattr(runner, "last_used_at", None),
            "turn_in_progress": bool(getattr(runner, "turn_in_progress", False)),
            "plan_mode": bool(getattr(runner, "plan_mode", False)),
            "pending_jobs": jobs,
        })
    sessions.sort(key=lambda s: s.get("last_used_at") or 0, reverse=True)
    _json_response(handler, 200, {"sessions": sessions})
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
            # media (/sessions/{id}/assets/{asset_id}) for filmstrip + waveform.
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
        elif "image" in kinds or "text" in kinds or tid.startswith("OV"):
            kind = "overlay"
        else:
            kind = "video"
        label = {"audio": "Audio", "overlay": "Overlay"}.get(kind, "Video")
        tracks.append({
            "id": tid,
            "kind": kind,
            "name": f"{label} {tid}",
            "duck_under": None,
            "clips": clips,
        })

    return {
        "session_id": session_id,
        "project_id": project_id,
        "patch_seq": int(meta.get("patch_seq") or 0),
        "duration": float(timeline.get("duration") or 0.0),
        "fps": float(timeline.get("fps") or 30.0),
        "width": int(timeline.get("width") or 1920),
        "height": int(timeline.get("height") or 1080),
        "tracks": tracks,
        # The storyboard IR rides the timeline payload so the web outline
        # panel refreshes through the existing poll + timeline_op force-fetch
        # (set_shotlist/update_shot land in the same patch log as clip ops).
        "shotlist": project.get("shotlist") if isinstance(project.get("shotlist"), dict) else None,
    }


def _session_timeline(handler, session_id: str) -> bool:
    """Return the current project timeline as a JSON payload for the frontend."""
    runner = get_manager().get(session_id)
    if runner is None:
        _json_error(handler, 404, f"unknown session: {session_id}")
        return True
    try:
        project = runner.agent.project.load()
        meta = runner.agent.project.store.load_meta(runner.agent.project.project_id)
    except Exception as exc:
        _json_error(handler, 500, f"could not load project: {exc}")
        return True
    _json_response(handler, 200, _timeline_payload_dict(
        session_id, runner.agent.project.project_id, project, meta,
    ))
    return True


# User direct-edit op tokens -> the same patches.py ops the model's verbs emit.
# ``set_effects`` also carries the direct-UI BLEND op (effects.blend_mode) and the
# PIP op (effects.scale/x/y); ``add_transition`` carries the CROSSFADE op.
_USER_EDIT_OPS = {"move", "trim", "split", "delete", "set_time", "set_effects", "add_transition"}


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
    op_name = str(body.get("op") or "")
    if op_name == "undo":
        return _apply_user_undo(handler, runner, body)
    if op_name not in _USER_EDIT_OPS:
        _json_error(handler, 400, f"unknown op '{op_name}'; valid: {sorted(_USER_EDIT_OPS)} or 'undo'")
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
    try:
        # SAME path as the verbs: ProjectStore append-only patch log + undo,
        # and ProjectHandle.on_patch emits the timeline_op SSE event. Hopped
        # onto the session loop (run_project_edit) so user edits serialize
        # with agent verbs and their SSE emits stay ordered.
        runner.run_project_edit(
            lambda: project.apply_ops([patch_op], label=f"user_edit:{op_name}")
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


def _apply_user_undo(handler, runner: SessionRunner, body: dict) -> bool:
    """Rewind the last N timeline patches via the same path as the timeline_undo
    verb (ProjectStore.undo_to_seq). Returns the post-state."""
    try:
        steps = int(body.get("steps") or 1)
    except (TypeError, ValueError):
        _json_error(handler, 400, "undo 'steps' must be an integer")
        return True
    project = runner.agent.project
    try:
        runner.run_project_edit(lambda: project.undo(max(1, min(steps, 50))))
    except FuturesTimeoutError:
        _json_error(
            handler, 503,
            "undo is queued behind a long-running step and has not applied yet — "
            "refresh the timeline to see whether it landed",
            code="E_BUSY",
        )
        return True
    except Exception as exc:
        _json_error(handler, 400, f"undo failed: {exc}")
        return True
    try:
        project_state = project.load()
        meta = project.store.load_meta(project.project_id)
    except Exception as exc:
        _json_error(handler, 500, f"undone, but could not reload project: {exc}")
        return True
    _json_response(handler, 200, _timeline_payload_dict(
        runner.session_id, project.project_id, project_state, meta,
    ))
    return True


def _list_assets(handler, session_id: str) -> bool:
    runner = get_manager().get(session_id)
    if runner is None:
        _json_error(handler, 404, f"unknown session: {session_id}")
        return True
    _json_response(handler, 200, {"assets": runner.list_assets()})
    return True


def _serve_asset(handler, session_id: str, asset_id: str, *, body: bool) -> bool:
    runner = get_manager().get(session_id)
    if runner is None:
        _json_error(handler, 404, f"unknown session: {session_id}")
        return True
    path = runner.asset_path(asset_id)
    if path is None or not Path(path).exists():
        _json_error(handler, 404, f"unknown asset: {asset_id}")
        return True
    _serve_file_with_range(handler, Path(path), body=body)
    return True


def _sse_stream(handler, session_id: str, query: dict, *, body: bool) -> bool:
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
            {"kind": "protocol_hello", "protocol_version": PROTOCOL_VERSION},
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


def _json_response(handler, status: int, payload: dict[str, Any]) -> None:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def _json_error(handler, status: int, message: str, *, code: str | None = None) -> None:
    payload: dict[str, Any] = {"error": message}
    if code:
        payload["code"] = code
    _json_response(handler, status, payload)


__all__ = ["try_handle"]
