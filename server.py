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


def _configured_server_host(default: str = "0.0.0.0") -> str:
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
            _json_response(self, 200, status)
            return

        # ── Sandbox settings ──
        if path == "/settings/sandbox":
            from gemia.sandbox_v4 import is_sandbox_disabled
            _json_response(self, 200, {"sandbox_disabled": is_sandbox_disabled()})
            return

        # ── v3 session routes ──
        if path == "/sessions" or path.startswith("/sessions/"):
            from gemia.v3_routes import try_handle as _v3_try
            if _v3_try(self, method="GET"):
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

        # ── v3 session routes ──
        if path == "/sessions" or path.startswith("/sessions/"):
            from gemia.v3_routes import try_handle as _v3_try
            if _v3_try(self, method="POST"):
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

        # ── Sandbox toggle ──
        if path == "/settings/sandbox":
            try:
                payload = _read_json_body(self)
                from gemia.sandbox_v4 import set_sandbox_disabled
                set_sandbox_disabled(bool(payload.get("disabled", False)))
                from gemia.sandbox_v4 import is_sandbox_disabled
                _json_response(self, 200, {"sandbox_disabled": is_sandbox_disabled()})
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
