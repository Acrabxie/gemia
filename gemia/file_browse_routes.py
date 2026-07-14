"""Read-only file browsing for the web UI's "文件" panel.

    GET /files/roots                                    browsable named roots
    GET /files/list?root=<key>&path=<rel>[&session=<sid>]  list one directory
    GET /files/get?root=<key>&path=<rel>[&session=<sid>]   serve one file

``try_handle(handler, method=..., serve_file=...)`` is the single entrypoint
server.py calls; ``serve_file(path)`` is injected so file bytes go out through
the host's ``_file_response`` (Range support) without a circular import.

Security model: the server is reachable from the LAN (server.py _host_allowed),
so this surface never accepts absolute paths or user-supplied roots. Roots are
a fixed whitelist:

  session    <LUMERI_V3_OUTPUT_ROOT>/workdirs/<sid> — a session's working
             files (requires ?session=; same id alphabet as v3_routes)
  outputs / frames / styled / demo / inputs / uploads / temp / timeline
             the repo-relative roots /file/ already serves one-by-one

Traversal guards mirror server.py._safe_child_path: reject dot/dot-dot parts,
then resolve() + relative_to() containment (which also rejects symlinks that
escape the root).
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, unquote, urlparse

_REPO_ROOT = Path(__file__).resolve().parent.parent
# Keep in lockstep with server.py _ALLOWED_ROOTS (the /file/ whitelist).
_REPO_KEYS = ("outputs", "frames", "styled", "demo", "inputs", "uploads", "temp", "timeline")
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_MAX_ENTRIES = 500


def try_handle(handler, *, method: str, serve_file: Callable[[Path], None]) -> bool:
    parsed = urlparse(handler.path)
    path = unquote(parsed.path).rstrip("/")
    if path not in {"/files/roots", "/files/list", "/files/get"}:
        return False
    if method not in {"GET", "HEAD"}:
        _json_error(handler, 405, f"method {method} not allowed on {path}")
        return True
    query = parse_qs(parsed.query)

    if path == "/files/roots":
        _json_response(handler, 200, {"roots": _roots_payload()})
        return True

    root_key = (query.get("root") or [""])[0]
    rel = (query.get("path") or [""])[0]
    session = (query.get("session") or [""])[0]
    base = _resolve_root(root_key, session)
    if base is None:
        _json_error(handler, 404, f"unknown or empty root: {root_key}")
        return True
    target = _safe_child(base, rel)
    if target is None or not target.exists():
        _json_error(handler, 404, "path not found")
        return True

    if path == "/files/list":
        if not target.is_dir():
            _json_error(handler, 400, "not a directory")
            return True
        _json_response(handler, 200, _list_payload(root_key, session, base, target))
        return True

    # /files/get
    if not target.is_file():
        _json_error(handler, 400, "not a file")
        return True
    serve_file(target)
    return True


def _roots_payload() -> list[dict[str, Any]]:
    roots = []
    for key in _REPO_KEYS:
        if (_REPO_ROOT / key).is_dir():
            roots.append({"key": key, "label": key})
    return roots


def _workdirs_root() -> Path:
    base = os.environ.get("LUMERI_V3_OUTPUT_ROOT") or "/tmp/lumeri-v3"
    return Path(base) / "workdirs"


def _resolve_root(root: str, session: str) -> Path | None:
    if root == "session":
        if not _SESSION_ID_RE.match(session or ""):
            return None
        p = _workdirs_root() / session
        return p if p.is_dir() else None
    if root in _REPO_KEYS:
        p = _REPO_ROOT / root
        return p if p.is_dir() else None
    return None


def _safe_child(base: Path, rel: str) -> Path | None:
    parts = [p for p in (rel or "").strip().split("/") if p]
    if any(p in {".", ".."} for p in parts):
        return None
    try:
        resolved_base = base.resolve()
        candidate = (resolved_base.joinpath(*parts)).resolve() if parts else resolved_base
        candidate.relative_to(resolved_base)
    except (OSError, ValueError):
        return None
    return candidate


def _list_payload(root_key: str, session: str, base: Path, target: Path) -> dict[str, Any]:
    entries = []
    truncated = False
    try:
        children = sorted(
            target.iterdir(),
            key=lambda p: (not p.is_dir(), p.name.lower()),
        )
    except OSError:
        children = []
    for child in children:
        if child.name.startswith("."):
            continue
        if len(entries) >= _MAX_ENTRIES:
            truncated = True
            break
        try:
            stat = child.stat()
            is_dir = child.is_dir()
        except OSError:
            continue
        entries.append({
            "name": child.name,
            "is_dir": is_dir,
            "size": 0 if is_dir else int(stat.st_size),
            "mtime": stat.st_mtime,
        })
    rel = str(target.relative_to(base.resolve())) if target != base.resolve() else ""
    return {
        "root": root_key,
        "session": session or None,
        "path": "" if rel == "." else rel,
        "entries": entries,
        "truncated": truncated,
    }


def _json_response(handler, status: int, payload: dict[str, Any]) -> None:
    import json

    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def _json_error(handler, status: int, message: str) -> None:
    _json_response(handler, status, {"error": message})


__all__ = ["try_handle"]
