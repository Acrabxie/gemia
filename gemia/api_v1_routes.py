"""Versioned first-party HTTP contracts.

``/api/v1`` is the stable domain prefix used by Lumeri's own clients. During
the migration it is a compatibility prefix over the existing domain routes.

``/api/internal/v1`` is a localhost-only diagnostics and capability surface.
It intentionally exposes only registry entries classified as ``product``;
host execution, arbitrary host filesystem access, credentials and private
agent plumbing are absent from its generated manifest and cannot be invoked.
"""
from __future__ import annotations

import json
import re
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from gemia.session_manager import VerbGateError, get_manager
from gemia.tool_capability_registry import build_default_registry
from gemia.transport.sse import REGISTRY as SSE_REGISTRY
from gemia.v3_contract import PROTOCOL_VERSION
from gemia.v3_routes import _public_payload


API_VERSION = 1
INTERNAL_PREFIX = "/api/internal/v1"
DOMAIN_PREFIX = "/api/v1"
_RESOURCE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


def is_domain_path(path: str) -> bool:
    return path == DOMAIN_PREFIX or path.startswith(f"{DOMAIN_PREFIX}/")


def legacy_path(path: str) -> str:
    """Map a v1 domain URL to its compatibility route."""

    if not is_domain_path(path):
        return path
    suffix = path[len(DOMAIN_PREFIX):]
    return suffix or "/"


def try_handle(handler, *, method: str, body: bool = True) -> bool:
    parsed = urlparse(handler.path)
    path = unquote(parsed.path).rstrip("/") or "/"
    if path != INTERNAL_PREFIX and not path.startswith(f"{INTERNAL_PREFIX}/"):
        return False
    if not _require_local(handler):
        return True
    if method not in {"GET", "HEAD", "POST"}:
        _error(handler, 405, "E_INPUT", "method not allowed")
        return True

    query = parse_qs(parsed.query)
    if method in {"GET", "HEAD"}:
        return _route_get(handler, path, query, body=body and method == "GET")
    return _route_post(handler, path)


def _require_local(handler) -> bool:
    if str(handler.headers.get("X-Lumeri-Remote") or "").strip():
        _error(
            handler,
            403,
            "E_REMOTE_BLOCKED",
            "the internal API is disabled for remote requests",
            recovery="none",
        )
        return False
    client_address = getattr(handler, "client_address", None)
    if client_address:
        host = str(client_address[0] or "")
        if host not in {"127.0.0.1", "::1", "localhost"}:
            _error(
                handler,
                403,
                "E_REMOTE_BLOCKED",
                "the internal API is localhost-only",
                recovery="none",
            )
            return False
    return True


def _route_get(handler, path: str, query: dict[str, list[str]], *, body: bool) -> bool:
    if path == f"{INTERNAL_PREFIX}/capabilities":
        registry = build_default_registry()
        _json(
            handler,
            200,
            {
                "api_version": API_VERSION,
                "protocol_version": PROTOCOL_VERSION,
                "capabilities": registry.manifest(
                    surface="product", exposed_via="http"
                ),
            },
            body=body,
        )
        return True

    if path == f"{INTERNAL_PREFIX}/doctor":
        from gemia import brain_config
        from gemia.memory import read_user_config, resolve_model_with_source
        from gemia.tools.web_search import search_provider_status

        config = read_user_config()
        status = brain_config.read_status(config)
        _json(
            handler,
            200,
            {
                "api_version": API_VERSION,
                "protocol_version": PROTOCOL_VERSION,
                "config": {
                    "provider": status.get("provider"),
                    "model": status.get("model"),
                    "effort": status.get("effort"),
                    "location": status.get("location"),
                    "has_key": status.get("has_key"),
                    "resolved_models": {
                        slot: resolve_model_with_source(slot)
                        for slot in ("planner", "image", "video", "audio")
                    },
                    "search": search_provider_status(),
                    "credentials": "redacted",
                },
            },
            body=body,
        )
        return True

    match = re.fullmatch(rf"{re.escape(INTERNAL_PREFIX)}/capabilities/([^/]+)", path)
    if match:
        name = match.group(1)
        registry = build_default_registry()
        try:
            capability = registry.get(name)
        except KeyError:
            _error(handler, 404, "E_TOOL", f"unknown capability: {name}")
            return True
        if capability.surface != "product" or "http" not in capability.exposed_via:
            _error(handler, 404, "E_TOOL", f"unknown capability: {name}")
            return True
        _json(handler, 200, capability.manifest_record(), body=body)
        return True

    match = re.fullmatch(
        rf"{re.escape(INTERNAL_PREFIX)}/sessions/([^/]+)/(snapshot|events)", path
    )
    if match:
        session_id, resource = match.groups()
        if not _valid_id(session_id):
            _error(handler, 400, "E_INPUT", "invalid session id")
            return True
        if resource == "snapshot":
            runner = _get_or_resume_runner(session_id)
            if runner is None:
                _error(handler, 404, "E_NOT_FOUND", f"unknown session: {session_id}")
                return True
            snapshot = runner.snapshot()
            snapshot["latest_event_seq"] = _latest_event_seq(session_id)
            _json(handler, 200, _public_payload(snapshot), body=body)
            return True
        return _events(handler, session_id, query, body=body)

    _error(handler, 404, "E_NOT_FOUND", "internal API resource not found")
    return True


def _route_post(handler, path: str) -> bool:
    match = re.fullmatch(
        rf"{re.escape(INTERNAL_PREFIX)}/sessions/([^/]+)/capabilities/([^/:]+):invoke",
        path,
    )
    if not match:
        _error(handler, 404, "E_NOT_FOUND", "internal API resource not found")
        return True
    session_id, name = match.groups()
    if not _valid_id(session_id):
        _error(handler, 400, "E_INPUT", "invalid session id")
        return True
    runner = _get_or_resume_runner(session_id)
    if runner is None:
        _error(handler, 404, "E_NOT_FOUND", f"unknown session: {session_id}")
        return True

    payload = _read_json(handler)
    if payload is None:
        return True
    arguments = payload.get("arguments", {})
    if not isinstance(arguments, dict):
        _error(handler, 400, "E_INPUT", "arguments must be an object")
        return True
    request_id = str(
        handler.headers.get("X-Request-ID")
        or payload.get("request_id")
        or f"req-{uuid.uuid4().hex}"
    )
    call_id = f"call-{uuid.uuid4().hex}"
    idempotency_key = payload.get("idempotency_key")
    expected_revision = payload.get("expected_project_revision")
    if expected_revision is not None:
        try:
            expected_revision = int(expected_revision)
        except (TypeError, ValueError):
            _error(
                handler,
                400,
                "E_INPUT",
                "expected_project_revision must be an integer",
                request_id=request_id,
            )
            return True

    try:
        capability = runner.agent.capabilities.get(name)
        if capability.surface != "product" or "http" not in capability.exposed_via:
            raise KeyError(name)
    except KeyError:
        _error(
            handler,
            404,
            "E_TOOL",
            f"unknown capability: {name}",
            request_id=request_id,
        )
        return True

    if capability.requires_idempotency_key and not str(idempotency_key or "").strip():
        _error(
            handler,
            409,
            "E_IDEMPOTENCY_CONFLICT",
            "idempotency_key is required for write and paid capabilities",
            request_id=request_id,
        )
        return True
    if capability.requires_project_revision and expected_revision is None:
        _error(
            handler,
            409,
            "E_REVISION_CONFLICT",
            "expected_project_revision is required for project writes",
            details={"project_revision": runner.project_revision},
            request_id=request_id,
        )
        return True

    try:
        if capability.paid_media:
            result = runner.run_production_verb(
                name,
                arguments,
                trace_id=request_id,
                idempotency_key=str(idempotency_key or ""),
                expected_project_revision=expected_revision,
            )
            call_id = str(result.get("production_tool_call_id") or call_id)
        else:
            result = runner.execute_capability(
                name,
                arguments,
                origin="internal_http",
                call_id=call_id,
                request_id=request_id,
                idempotency_key=(
                    str(idempotency_key) if idempotency_key is not None else None
                ),
                expected_project_revision=expected_revision,
                require_mutation_tokens=True,
            )
    except VerbGateError as exc:
        status = {
            "E_TOOL": 404,
            "E_BUSY": 409,
            "E_BUDGET": 409,
            "E_PLAN_MODE": 409,
            "E_REVISION_CONFLICT": 409,
            "E_IDEMPOTENCY_CONFLICT": 409,
        }.get(exc.code, 400)
        _error(
            handler,
            status,
            exc.code,
            exc.message,
            details=exc.payload,
            request_id=request_id,
        )
        return True
    except Exception as exc:
        from gemia.errors import GemiaError

        if isinstance(exc, GemiaError):
            error_payload = exc.to_payload()
            code = str(error_payload.get("error_code") or exc.code)
            message = str(error_payload.get("error") or str(exc))
            recovery = str(error_payload.get("recovery") or "none")
            status = 400
        else:
            error_payload = {}
            code = str(getattr(exc, "code", "") or "E_TOOL")
            message = str(exc) or type(exc).__name__
            recovery = "none"
            status = 500
        _error(
            handler,
            status,
            code,
            message,
            recovery=recovery,
            details=error_payload,
            request_id=request_id,
        )
        return True

    project_revision = runner.project_revision
    response = {
        "request_id": request_id,
        "call_id": call_id,
        "status": (
            "accepted"
            if capability.execution == "job" and result.get("job_id")
            else "completed"
        ),
        "result": _public_payload(result),
        "project_revision": project_revision,
        "latest_event_seq": _latest_event_seq(session_id),
    }
    _json(handler, 202 if response["status"] == "accepted" else 200, response)
    return True


def _events(
    handler,
    session_id: str,
    query: dict[str, list[str]],
    *,
    body: bool,
) -> bool:
    raw_after = (query.get("after") or query.get("since_seq") or ["0"])[0]
    try:
        after = max(0, int(raw_after))
    except (TypeError, ValueError):
        _error(handler, 400, "E_INPUT", "after must be an integer")
        return True
    transcript = get_manager().sessions_root / session_id / "transcript.jsonl"
    if not transcript.exists():
        _error(handler, 404, "E_NOT_FOUND", f"no events for session: {session_id}")
        return True
    events: list[dict[str, Any]] = []
    for line in transcript.read_text(encoding="utf-8").splitlines():
        try:
            record = json.loads(line)
            seq = int(record.get("seq") or 0)
            event = record.get("event")
            if seq <= after or not isinstance(event, dict):
                continue
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
        stable_keys = {
            "kind", "origin", "request_id", "trace_id",
            "project_revision", "production_revision",
        }
        events.append(
            {
                "seq": seq,
                "ts": record.get("ts"),
                "session_id": session_id,
                "request_id": event.get("request_id") or event.get("trace_id"),
                "origin": event.get("origin") or "agent",
                "kind": event.get("kind") or "unknown",
                "project_revision": event.get("project_revision"),
                "production_revision": event.get("production_revision"),
                "data": {
                    key: value
                    for key, value in event.items()
                    if key not in stable_keys
                },
            }
        )
    replay_gap = bool(events and after > 0 and events[0]["seq"] > after + 1)
    _json(
        handler,
        200,
        {
            "session_id": session_id,
            "events": _public_payload(events),
            "latest_event_seq": _latest_event_seq(session_id),
            "replay_gap": replay_gap,
            "snapshot_required": replay_gap,
        },
        body=body,
    )
    return True


def _get_or_resume_runner(session_id: str):
    manager = get_manager()
    runner = manager.get(session_id)
    if runner is not None:
        return runner
    try:
        return manager.resume_session(session_id)
    except Exception:
        return None


def _latest_event_seq(session_id: str) -> int:
    live = SSE_REGISTRY.latest_event_id(session_id)
    if live is not None:
        return int(live)
    transcript = get_manager().sessions_root / session_id / "transcript.jsonl"
    latest = 0
    if transcript.exists():
        for line in transcript.read_text(encoding="utf-8").splitlines():
            try:
                latest = max(latest, int(json.loads(line).get("seq") or 0))
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
    return latest


def _valid_id(value: str) -> bool:
    return bool(_RESOURCE_ID.fullmatch(str(value or "")))


def _read_json(handler) -> dict[str, Any] | None:
    try:
        length = int(handler.headers.get("Content-Length") or 0)
    except ValueError:
        _error(handler, 400, "E_INPUT", "invalid Content-Length")
        return None
    try:
        payload = json.loads(handler.rfile.read(length) or b"{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        _error(handler, 400, "E_INPUT", "request body must be valid JSON")
        return None
    if not isinstance(payload, dict):
        _error(handler, 400, "E_INPUT", "request body must be an object")
        return None
    return payload


def _json(handler, status: int, payload: dict[str, Any], *, body: bool = True) -> None:
    data = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    if body:
        handler.wfile.write(data)


def _error(
    handler,
    status: int,
    code: str,
    message: str,
    *,
    recovery: str = "none",
    details: dict[str, Any] | None = None,
    request_id: str | None = None,
) -> None:
    payload: dict[str, Any] = {
        "error": {
            "code": code,
            "message": message,
            "recovery": recovery,
            "details": _public_payload(details or {}),
        }
    }
    if request_id:
        payload["request_id"] = request_id
    _json(handler, status, payload)


def contract_document() -> dict[str, Any]:
    """Deterministic fixture exported to first-party clients."""

    registry = build_default_registry()
    return {
        "api_version": API_VERSION,
        "protocol_version": PROTOCOL_VERSION,
        "capabilities": registry.manifest(surface="product", exposed_via="http"),
        "event_envelope": {
            "required": ["seq", "ts", "session_id", "origin", "kind", "data"],
            "optional": [
                "request_id", "project_revision", "production_revision",
            ],
        },
        "error_envelope": {
            "required": ["code", "message", "recovery", "details"],
        },
    }


__all__ = [
    "API_VERSION",
    "DOMAIN_PREFIX",
    "INTERNAL_PREFIX",
    "contract_document",
    "is_domain_path",
    "legacy_path",
    "try_handle",
]
