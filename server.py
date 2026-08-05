"""Lumeri Video server — single entry point.

Endpoints:
  GET  /                        → v3 web UI (static/v3/index.html)
  GET  /v3/<path>               → v3 web UI assets
  GET  /health                  → server health
  GET  /config                  → {has_key: bool}
  POST /config                  → save API keys to ~/.gemia/config.json
  GET  /settings/sandbox        → sandbox status
  POST /settings/sandbox        → toggle sandbox
  GET  /file/<rel-path>         → serve project files from approved dirs
  *    /sessions/*              → v3 session routes (delegated to v3_routes)
"""

from __future__ import annotations

import json
import mimetypes
import os
import socket
import subprocess
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

_CONFIG_PATH = Path.home() / ".gemia" / "config.json"
_BASE_DIR = Path(__file__).resolve().parent
_STATIC_V3_DIR = _BASE_DIR / "static" / "v3"
_INPUTS_DIR = _BASE_DIR / "inputs"
_LOCAL_WORKSPACE_ID = "local"

# Directories that may be served via /file/.
_ALLOWED_ROOTS = {"outputs", "frames", "styled", "demo", "inputs", "uploads", "temp", "timeline"}


# ── Config ───────────────────────────────────────────────────────────────

def _load_config_keys() -> None:
    """Load API keys from ~/.gemia/config.json into env vars (if not already set)."""
    if _CONFIG_PATH.exists():
        try:
            cfg = json.loads(_CONFIG_PATH.read_text())
            if key := cfg.get("gemini_api_key"):
                os.environ.setdefault("GEMINI_API_KEY", key)
            if value := cfg.get("vertex_project"):
                os.environ.setdefault("VERTEX_PROJECT", value)
        except Exception:
            pass


def _has_valid_key() -> bool:
    return bool(os.environ.get("GEMINI_API_KEY") or os.environ.get("VERTEX_PROJECT"))


def _configured_server_host(default: str = "127.0.0.1") -> str:
    return os.environ.get("LUMERI_HOST") or os.environ.get("GEMIA_HOST") or default


def _lan_addresses() -> list[str]:
    addresses: set[str] = set()
    try:
        import ipaddress
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            addr = sock.getsockname()[0]
            ip = ipaddress.ip_address(addr)
            if ip.version == 4 and not ip.is_loopback:
                addresses.add(addr)
    except Exception:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            addresses.add(str(info[4][0]))
    except Exception:
        pass
    return sorted(addresses)


def _server_urls(host: str, port: int) -> list[str]:
    if host in {"0.0.0.0", "::", ""}:
        urls = [f"http://127.0.0.1:{port}"]
        urls.extend(f"http://{address}:{port}" for address in _lan_addresses())
        return urls
    return [f"http://{host}:{port}"]


# ── Security ─────────────────────────────────────────────────────────────

_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}
_NATIVE_ORIGIN_SCHEMES = {"tauri", "lumeri", "app"}
_LAN_CACHE: tuple[float, list[str]] | None = None


def _cached_lan_addresses() -> list[str]:
    global _LAN_CACHE
    import time as _time
    now = _time.time()
    if _LAN_CACHE is not None and now - _LAN_CACHE[0] < 30.0:
        return list(_LAN_CACHE[1])
    addrs = _lan_addresses()
    _LAN_CACHE = (now, list(addrs))
    return list(addrs)


def _host_allowed(host_header: str) -> bool:
    raw = (host_header or "").strip().lower()
    if not raw:
        return False
    host_only = raw.split("]")[-1].split(":")[0] if raw.startswith("[") else raw.split(":")[0]
    if host_only in _LOOPBACK_HOSTS:
        return True
    return host_only in _cached_lan_addresses()


def _origin_allowed(origin_or_referer: str) -> bool:
    value = (origin_or_referer or "").strip()
    if not value:
        return True
    try:
        parsed = urlparse(value)
    except Exception:
        return False
    scheme = (parsed.scheme or "").lower()
    if scheme in _NATIVE_ORIGIN_SCHEMES:
        return True
    if scheme not in {"http", "https"}:
        return False
    netloc = (parsed.netloc or "").lower()
    if not netloc:
        return False
    host_only = netloc.split("]")[-1].split(":")[0] if netloc.startswith("[") else netloc.split(":")[0]
    if host_only in _LOOPBACK_HOSTS:
        return True
    return host_only in _cached_lan_addresses()


# ── HTTP helpers ─────────────────────────────────────────────────────────

def _json_response(handler: BaseHTTPRequestHandler, status: int, body: object) -> None:
    data = json.dumps(body, ensure_ascii=False).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def _empty_response(handler: BaseHTTPRequestHandler, status: int = 204) -> None:
    handler.send_response(status)
    handler.send_header("Content-Length", "0")
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()


def _file_response(handler: BaseHTTPRequestHandler, path: Path, *, body: bool = True) -> None:
    if not path.exists() or not path.is_file():
        _json_response(handler, 404, {"error": "file not found"})
        return
    content_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    size = path.stat().st_size

    range_header = handler.headers.get("Range", "").strip()
    start = 0
    end = size - 1
    partial = False
    if range_header.startswith("bytes="):
        requested = range_header.removeprefix("bytes=").split(",", 1)[0].strip()
        try:
            raw_start, _, raw_end = requested.partition("-")
            if raw_start:
                start = int(raw_start)
                end = int(raw_end) if raw_end else size - 1
            elif raw_end:
                suffix = int(raw_end)
                start = max(0, size - suffix)
                end = size - 1
            if size <= 0 or start < 0 or end < start or start >= size:
                raise ValueError
            end = min(end, size - 1)
            partial = True
        except ValueError:
            handler.send_response(416)
            handler.send_header("Content-Range", f"bytes */{size}")
            handler.send_header("Accept-Ranges", "bytes")
            handler.end_headers()
            return

    content_length = max(0, end - start + 1)
    handler.send_response(206 if partial else 200)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(content_length))
    handler.send_header("Accept-Ranges", "bytes")
    if partial:
        handler.send_header("Content-Range", f"bytes {start}-{end}/{size}")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Connection", "close")
    handler.end_headers()
    if body:
        try:
            with path.open("rb") as f:
                f.seek(start)
                remaining = content_length
                while remaining > 0:
                    chunk = f.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    handler.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass
    handler.close_connection = True


def _safe_child_path(root: Path, rel: str) -> Path | None:
    """Resolve *rel* under *root*, returning None if it escapes."""
    root = root.resolve()
    try:
        target = (root / rel).resolve()
        target.relative_to(root)
        return target
    except (ValueError, OSError):
        return None


def _read_json_body(handler: BaseHTTPRequestHandler) -> dict:
    length = int(handler.headers.get("Content-Length", 0))
    raw = handler.rfile.read(length) if length else b"{}"
    payload = json.loads(raw or b"{}")
    if not isinstance(payload, dict):
        raise ValueError("JSON body must be an object")
    return payload


# ── Handler ──────────────────────────────────────────────────────────────

class _Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: object) -> None:
        print(f"  {self.address_string()} {fmt % args}")

    def _security_gate(self, *, mutating: bool) -> bool:
        if not _host_allowed(self.headers.get("Host", "")):
            _json_response(self, 403, {"error": "host not allowed"})
            return True
        if mutating:
            origin = self.headers.get("Origin")
            referer = self.headers.get("Referer")
            if origin and not _origin_allowed(origin):
                _json_response(self, 403, {"error": "origin not allowed"})
                return True
            if referer and not _origin_allowed(referer):
                _json_response(self, 403, {"error": "referer not allowed"})
                return True
        return False

    def _handle_get(self) -> None:
        parsed_url = urlparse(self.path)
        path = unquote(parsed_url.path).rstrip("/") or "/"

        # ── Web UI: serve static/v3/ ──
        if path == "/" or path == "/v3" or path == "/v3/":
            _file_response(self, _STATIC_V3_DIR / "index.html")
            return

        if path.startswith("/v3/"):
            rel = path[len("/v3/"):]
            target = _safe_child_path(_STATIC_V3_DIR, rel)
            if target is None:
                _json_response(self, 404, {"error": "not found"})
                return
            _file_response(self, target)
            return

        if path == "/favicon.ico":
            _empty_response(self)
            return

        # ── Health ──
        if path == "/health":
            _json_response(self, 200, {"status": "ok", "has_key": _has_valid_key()})
            return

        # ── File browser ──
        if path.startswith("/files/"):
            from gemia.file_browse_routes import try_handle as _files_try
            if _files_try(self, method="GET", serve_file=lambda p: _file_response(self, p)):
                return

        # ── Config ──
        if path == "/config":
            cfg = {}
            if _CONFIG_PATH.exists():
                try:
                    cfg = json.loads(_CONFIG_PATH.read_text())
                except Exception:
                    pass
            from gemia import brain_config
            status = brain_config.read_status(cfg)
            # The v3 Setup panel consumes ``brain`` as a nested object.  Keep
            # the top-level ``has_key`` for the small first-run status check,
            # but provide the same public shape to both web clients.
            _json_response(self, 200, {"has_key": _has_valid_key(), "brain": status})
            return

        if path == "/config/codex-subscription":
            from gemia.codex_subscription import subscription_status

            _json_response(self, 200, subscription_status())
            return

        # ── Model selection ──
        # The v3 command palette fetches this route directly.  It is local-only
        # state and contains no provider secrets.
        if path == "/model":
            from gemia.memory import model_selection_payload
            _json_response(self, 200, model_selection_payload("planner"))
            return

        # ── Sandbox settings ──
        if path == "/settings/sandbox":
            from gemia.sandbox_v4 import sandbox_status
            _json_response(self, 200, sandbox_status())
            return

        if path == "/projects":
            from gemia.local_projects import list_projects
            _json_response(self, 200, {"projects": list_projects()})
            return

        # ── v3 session routes ──
        if path == "/sessions" or path.startswith("/sessions/"):
            from gemia.v3_routes import try_handle as _v3_try
            if _v3_try(self, method="GET"):
                return

        # ── Local session history (single-user, no account namespace) ──
        if path == "/session-history":
            from gemia.session_history import load_current_session
            _json_response(self, 200, load_current_session())
            return

        if path == "/session-history/list":
            from gemia.session_history import list_session_snapshots
            from gemia.session_manager import get_manager
            query = parse_qs(parsed_url.query)
            try:
                limit = int(query.get("limit", ["30"])[0] or 30)
            except ValueError:
                limit = 30
            snapshots = list_session_snapshots(limit=limit)
            manager = get_manager()
            list_persisted = getattr(manager, "list_persisted_sessions", None)
            records = list_persisted(include_deleted=True) if callable(list_persisted) else []
            session_meta = {str(item.get("session_id") or ""): item for item in records}
            if not session_meta:
                from gemia.local_projects import session_metadata
                session_meta = session_metadata()
            visible = []
            for snapshot in snapshots:
                meta = session_meta.get(str(snapshot.get("v3_session_id") or ""))
                if meta and meta.get("deleted_at"):
                    continue
                visible.append({**snapshot, "pinned": bool(meta and meta.get("pinned"))})
            visible.sort(key=lambda item: bool(item.get("pinned")), reverse=True)
            _json_response(self, 200, {"sessions": visible})
            return

        if path.startswith("/session-history/"):
            from gemia.session_history import load_session_snapshot
            snapshot_id = path.removeprefix("/session-history/").strip()
            try:
                _json_response(self, 200, load_session_snapshot(snapshot_id, activate=True))
            except FileNotFoundError:
                _json_response(self, 404, {"error": "session not found"})
            return

        # ── Local media library ──
        if path == "/media-library/list":
            from gemia.media_library import list_assets
            query = parse_qs(parsed_url.query)
            try:
                limit = int(query.get("limit", ["200"])[0] or 200)
            except ValueError:
                limit = 200
            _json_response(self, 200, {"assets": list_assets(
                _LOCAL_WORKSPACE_ID,
                kind=str(query.get("kind", [""])[0] or ""),
                q=str(query.get("q", [""])[0] or ""),
                limit=limit,
            )})
            return

        if path.startswith("/media-library/prepare/"):
            from gemia.roughcut import RoughcutError, get_prepare_job
            try:
                _json_response(self, 200, get_prepare_job(
                    _LOCAL_WORKSPACE_ID,
                    path.removeprefix("/media-library/prepare/").strip(),
                ))
            except RoughcutError as exc:
                _json_response(self, 404, {"error": str(exc)})
            return

        if path.startswith("/media-library/") and path.endswith("/roughcut"):
            from gemia.roughcut import RoughcutError, load_roughcut
            asset_id = path.split("/")[2]
            try:
                _json_response(self, 200, {"manifest": load_roughcut(_LOCAL_WORKSPACE_ID, asset_id)})
            except RoughcutError as exc:
                _json_response(self, 404, {"error": str(exc)})
            return

        if path.startswith("/media-library/") and path.endswith("/annotations"):
            from gemia.media_annotations import MediaAnnotationError, list_annotations
            asset_id = path.split("/")[2]
            try:
                _json_response(self, 200, {"annotations": list_annotations(_LOCAL_WORKSPACE_ID, asset_id)})
            except MediaAnnotationError as exc:
                _json_response(self, 404, {"error": str(exc)})
            return

        if path.startswith("/media-library/file/"):
            from gemia.media_library import MediaLibraryError, resolve_asset_file
            parts = path.split("/")
            try:
                target = resolve_asset_file(
                    _LOCAL_WORKSPACE_ID,
                    parts[3] if len(parts) >= 5 else "",
                    parts[4] if len(parts) >= 5 else "",
                    parts[5] if len(parts) >= 6 else None,
                )
                _file_response(self, target)
            except MediaLibraryError as exc:
                _json_response(self, 404, {"error": str(exc)})
            return

        if path.startswith("/media-library/"):
            from gemia.media_library import get_asset
            asset = get_asset(_LOCAL_WORKSPACE_ID, path.split("/")[2])
            if asset:
                _json_response(self, 200, {"asset": asset})
            else:
                _json_response(self, 404, {"error": "media asset not found"})
            return

        # ── File serving ──
        if path.startswith("/file/"):
            rel = path[len("/file/"):]
            parts = rel.split("/", 1)
            if not parts or parts[0] not in _ALLOWED_ROOTS:
                _json_response(self, 403, {"error": "forbidden"})
                return
            target = _safe_child_path(_BASE_DIR, rel)
            if target is None:
                _json_response(self, 403, {"error": "forbidden"})
                return
            _file_response(self, target)
            return

        _json_response(self, 404, {"error": "not found"})

    def _handle_post(self) -> None:
        parsed_url = urlparse(self.path)
        path = unquote(parsed_url.path).rstrip("/") or "/"

        if path == "/projects":
            try:
                from gemia.local_projects import create_project
                payload = _read_json_body(self)
                project = create_project(
                    str(payload.get("name") or ""),
                    str(payload.get("source_root") or payload.get("path") or ""),
                )
                _json_response(self, 201, project)
            except ValueError as exc:
                _json_response(self, 400, {"error": str(exc)})
            return

        if path == "/projects/pick-folder":
            if os.name != "nt":
                _json_response(self, 200, {"path": "", "cancelled": True})
                return
            result = subprocess.run(
                [
                    "powershell.exe", "-NoProfile", "-STA", "-Command",
                    "Add-Type -AssemblyName System.Windows.Forms; "
                    "$d=New-Object System.Windows.Forms.FolderBrowserDialog; "
                    "$d.Description='选择 Lumeri Project 文件夹'; "
                    "if($d.ShowDialog() -eq 'OK'){$d.SelectedPath}",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            selected = result.stdout.strip()
            _json_response(self, 200, {"path": selected, "cancelled": not bool(selected)})
            return

        if path == "/sessions":
            try:
                from gemia.local_projects import link_session
                from gemia.session_manager import get_manager
                from gemia.transport.sse import REGISTRY as sse_registry
                from gemia.v3_contract import PROTOCOL_VERSION
                payload = _read_json_body(self)
                project_id = str(payload.get("project_id") or "").strip()
                runner = get_manager().create_session()
                link_session(project_id, runner.session_id)
                _json_response(self, 201, {
                    "session_id": runner.session_id,
                    "project_id": project_id or None,
                    "run_id": runner.session_id if project_id else None,
                    "assets": runner.list_assets(),
                    "latest_event_id": sse_registry.latest_event_id(runner.session_id),
                    "plan_mode": runner.plan_mode,
                    "protocol_version": PROTOCOL_VERSION,
                    "stream_url": f"/sessions/{runner.session_id}/stream",
                    "turn_url": f"/sessions/{runner.session_id}/turn",
                    "assets_url": f"/sessions/{runner.session_id}/assets",
                })
            except Exception as exc:
                _json_response(self, 500, {"error": str(exc)})
            return

        # ── v3 session routes ──
        if path.startswith("/sessions/"):
            from gemia.v3_routes import try_handle as _v3_try
            if _v3_try(self, method="POST"):
                return

        if path == "/session-history":
            try:
                from gemia.session_history import save_current_session
                _json_response(self, 200, save_current_session(_read_json_body(self)))
            except Exception as exc:
                _json_response(self, 400, {"error": str(exc)})
            return

        # ── Config save ──
        if path == "/config":
            try:
                payload = _read_json_body(self)
                _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
                existing = {}
                if _CONFIG_PATH.exists():
                    try:
                        existing = json.loads(_CONFIG_PATH.read_text())
                    except Exception:
                        pass
                from gemia import brain_config
                existing, changed = brain_config.apply_update(existing, payload)
                _CONFIG_PATH.write_text(json.dumps(existing, indent=2))
                _load_config_keys()
                _json_response(self, 200, {"saved": True, "has_key": _has_valid_key()})
            except Exception as exc:
                _json_response(self, 400, {"error": str(exc)})
            return

        # ── Config list models ──
        if path == "/config/list-models":
            try:
                payload = _read_json_body(self)
                provider = payload.get("provider", "")
                existing = {}
                if _CONFIG_PATH.exists():
                    try:
                        existing = json.loads(_CONFIG_PATH.read_text())
                    except Exception:
                        pass
                proxy = os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy") or existing.get("proxy")
                from gemia import brain_config
                res = brain_config.list_models(provider, existing, proxy=proxy or None)
                _json_response(self, 200, res)
            except Exception as exc:
                _json_response(self, 400, {"error": str(exc)})
            return

        # ── Config test brain ──
        if path == "/config/test-brain":
            try:
                payload = _read_json_body(self)
                existing = {}
                if _CONFIG_PATH.exists():
                    try:
                        existing = json.loads(_CONFIG_PATH.read_text())
                    except Exception:
                        pass
                from gemia import brain_config
                # Temporarily apply keys in payload to test client
                brain_config.apply_update({}, payload)
                proxy = os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy") or existing.get("proxy")
                res = brain_config.test_provider(proxy=proxy or None)
                _json_response(self, 200, res)
            except Exception as exc:
                _json_response(self, 400, {"error": str(exc)})
            return

        if path == "/config/codex-subscription/login":
            from gemia.codex_subscription import launch_login

            result = launch_login()
            _json_response(self, 200 if result.get("ok") else 400, result)
            return

        # ── Model selection ──
        if path == "/model":
            try:
                from gemia.memory import apply_model_selection, model_selection_payload
                payload = _read_json_body(self)
                apply_model_selection(payload, "planner")
                _json_response(self, 200, {"ok": True, **model_selection_payload("planner")})
            except ValueError as exc:
                _json_response(self, 400, {"error": str(exc)})
            except Exception as exc:
                _json_response(self, 500, {"error": str(exc)})
            return

        if path == "/model/add":
            try:
                from gemia import brain_config
                from gemia.memory import add_model_to_catalog, model_selection_payload
                payload = _read_json_body(self)
                model_id = str(payload.get("id") or "").strip()
                if not model_id:
                    raise ValueError("missing model id")
                cfg = json.loads(_CONFIG_PATH.read_text()) if _CONFIG_PATH.exists() else {}
                add_model_to_catalog(
                    model_id,
                    label=str(payload.get("label") or ""),
                    provider=str(payload.get("provider") or brain_config.read_status(cfg).get("provider") or ""),
                    slot="planner",
                )
                _json_response(self, 200, {"ok": True, **model_selection_payload("planner")})
            except ValueError as exc:
                _json_response(self, 400, {"error": str(exc)})
            return

        if path == "/model/remove":
            try:
                from gemia.memory import model_selection_payload, remove_model_from_catalog
                payload = _read_json_body(self)
                model_id = str(payload.get("id") or "").strip()
                if not model_id:
                    raise ValueError("missing model id")
                remove_model_from_catalog(model_id, slot="planner")
                _json_response(self, 200, {"ok": True, **model_selection_payload("planner")})
            except ValueError as exc:
                _json_response(self, 400, {"error": str(exc)})
            return

        if path == "/media-library/annotate":
            try:
                from gemia.media_annotations import annotate_asset_heuristic
                from gemia.media_library import list_assets
                payload = _read_json_body(self)
                asset_ids = payload.get("asset_ids") or payload.get("assets") or []
                if isinstance(asset_ids, str):
                    asset_ids = [asset_ids]
                if not asset_ids and payload.get("all"):
                    asset_ids = [item["asset_id"] for item in list_assets(
                        _LOCAL_WORKSPACE_ID,
                        kind=str(payload.get("kind") or "video"),
                        limit=int(payload.get("max_assets") or 20),
                    )]
                results = [annotate_asset_heuristic(
                    _LOCAL_WORKSPACE_ID,
                    str(asset_id),
                    mode=str(payload.get("mode") or "quick"),
                    language=str(payload.get("language") or "auto"),
                    tags=payload.get("tags") if isinstance(payload.get("tags"), list) else None,
                    replace_existing=bool(payload.get("replace_existing", True)),
                ) for asset_id in asset_ids]
                _json_response(self, 200, {"results": results, "asset_count": len(results)})
            except Exception as exc:
                _json_response(self, 400, {"error": str(exc)})
            return

        if path == "/media-library/prepare":
            try:
                from gemia.roughcut import prepare_roughcut, start_prepare_job
                payload = _read_json_body(self)
                if bool(payload.get("background", True)):
                    _json_response(self, 202, start_prepare_job(_LOCAL_WORKSPACE_ID, payload))
                else:
                    ids = payload.get("asset_ids") or payload.get("assets") or []
                    if isinstance(ids, str):
                        ids = [ids]
                    _json_response(self, 200, prepare_roughcut(
                        _LOCAL_WORKSPACE_ID,
                        [str(item) for item in ids],
                        all_assets=bool(payload.get("all") or payload.get("all_assets")),
                        language=str(payload.get("language") or "auto"),
                        create_proxies=bool(payload.get("create_proxies", True)),
                        proxy_resolution=int(payload.get("proxy_resolution") or 540),
                        resume=bool(payload.get("resume", True)),
                        max_assets=int(payload.get("max_assets") or 100),
                    ))
            except Exception as exc:
                _json_response(self, 400, {"error": str(exc)})
            return

        if path.startswith("/media-library/prepare/") and path.endswith("/resume"):
            try:
                from gemia.roughcut import resume_prepare_job
                job_id = path.removeprefix("/media-library/prepare/").removesuffix("/resume").strip("/")
                _json_response(self, 202, resume_prepare_job(_LOCAL_WORKSPACE_ID, job_id))
            except Exception as exc:
                _json_response(self, 400, {"error": str(exc)})
            return

        if path.startswith("/media-library/") and path.endswith("/roughcut/review"):
            try:
                from gemia.roughcut import apply_roughcut_review
                _json_response(self, 200, apply_roughcut_review(
                    _LOCAL_WORKSPACE_ID, path.split("/")[2], _read_json_body(self)
                ))
            except Exception as exc:
                _json_response(self, 400, {"error": str(exc)})
            return

        if path.startswith("/media-library/") and "/annotations" in path:
            try:
                from gemia.media_annotations import create_annotation, update_annotation
                parts = path.split("/")
                payload = _read_json_body(self)
                if len(parts) >= 5 and parts[4]:
                    result = update_annotation(_LOCAL_WORKSPACE_ID, parts[2], parts[4], payload)
                else:
                    result = create_annotation(_LOCAL_WORKSPACE_ID, parts[2], payload)
                _json_response(self, 200, {"annotation": result})
            except Exception as exc:
                _json_response(self, 400, {"error": str(exc)})
            return

        if path.startswith("/media-library/") and path.endswith("/add-to-project"):
            from gemia.media_library import default_clip_for_asset, get_asset
            asset = get_asset(_LOCAL_WORKSPACE_ID, path.split("/")[2])
            if not asset:
                _json_response(self, 404, {"error": "media asset not found"})
            else:
                _json_response(self, 200, {"asset": asset, "clip": default_clip_for_asset(asset)})
            return

        # ── Sandbox toggle ──
        if path == "/settings/sandbox":
            try:
                payload = _read_json_body(self)
                from gemia.sandbox_v4 import set_sandbox_disabled
                set_sandbox_disabled(bool(payload.get("disabled", False)))
                from gemia.sandbox_v4 import sandbox_status
                _json_response(self, 200, sandbox_status())
            except Exception as exc:
                _json_response(self, 400, {"error": str(exc)})
            return

        _json_response(self, 404, {"error": "not found"})

    def _handle_delete(self) -> None:
        parsed_url = urlparse(self.path)
        path = unquote(parsed_url.path).rstrip("/") or "/"

        # ── v3 session routes ──
        if path.startswith("/sessions/"):
            from gemia.v3_routes import try_handle as _v3_try
            if _v3_try(self, method="DELETE"):
                return

        if path.startswith("/media-library/") and "/annotations/" in path:
            try:
                from gemia.media_annotations import delete_annotation
                parts = path.split("/")
                _json_response(self, 200, {"annotation": delete_annotation(
                    _LOCAL_WORKSPACE_ID, parts[2], parts[4]
                )})
            except Exception as exc:
                _json_response(self, 404, {"error": str(exc)})
            return

        if path.startswith("/media-library/"):
            try:
                from gemia.media_library import soft_delete_asset
                _json_response(self, 200, {"asset": soft_delete_asset(
                    _LOCAL_WORKSPACE_ID, path.split("/")[2]
                )})
            except Exception as exc:
                _json_response(self, 404, {"error": str(exc)})
            return

        _json_response(self, 404, {"error": "not found"})

    def do_GET(self) -> None:  # noqa: N802
        if self._security_gate(mutating=False):
            return
        self._handle_get()

    def do_HEAD(self) -> None:  # noqa: N802
        if self._security_gate(mutating=False):
            return
        self._handle_get()

    def do_POST(self) -> None:  # noqa: N802
        if self._security_gate(mutating=True):
            return
        self._handle_post()

    def do_DELETE(self) -> None:  # noqa: N802
        if self._security_gate(mutating=True):
            return
        self._handle_delete()


# ── Entry point ──────────────────────────────────────────────────────────

def main(host: str | None = None, port: int | None = None) -> None:
    _load_config_keys()
    host = host or _configured_server_host()
    port = int(port or os.environ.get("LUMERI_PORT") or os.environ.get("GEMIA_PORT") or 7788)
    os.environ["GEMIA_HOST"] = host
    os.environ["GEMIA_PORT"] = str(port)
    server = ThreadingHTTPServer((host, port), _Handler)
    server.daemon_threads = True
    print(f"Lumeri Video server listening on http://{host}:{port}")
    for url in _server_urls(host, port):
        print(f"  available at {url}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Lumeri Video server")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args()
    main(args.host, args.port)
