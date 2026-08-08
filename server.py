"""Minimal local HTTP server for Gemia MVP.

Endpoints:
  GET  /                        → intentionally blank (reserved for the Lumeri family portal)
  GET  /video/<rel-path>        → Lumeri Video web UI (static/v3/ on disk); /v3 → 301 /video
  GET  /sessions, POST /sessions/… → Lumeri v3 session surface (gemia/v3_routes.py)
  GET  /file/<rel-path>         → serve project files from approved media roots
  GET  /config                  → {"has_key": bool}
  GET  /session-history         → active account's current UI session
  GET  /session-history/list    → active account's recent UI sessions
  GET  /session-history/<id>    → open one previous UI session snapshot
  POST /session-history         → save active account's current UI session
  POST /upload-media            → import video/image/audio assets
  GET  /media-library/list      → account-scoped media assets
  GET  /media-library/<id>      → one media asset
  GET  /media-library/file/<id>/<area>/<file?> → media original/cache file
  POST /media-library/import    → import a server-local media path
  POST /media-library/<id>/add-to-project → make a default timeline clip
  DELETE /media-library/<id>    → soft-delete one media asset
  GET  /agent-links/status      → codex-lumeri/gemini-lumeri link status and recent relay messages
  GET  /agent-links/messages    → recent local relay messages
  POST /agent-links/link        → mark codex-lumeri or gemini-lumeri linked in the top bar
  POST /agent-links/message     → send one local relay message, optionally invoking target CLI
  POST /agent-links/relay       → run one codex-lumeri ↔ gemini-lumeri relay round through Lumeri
  POST /config                  → save API keys to ~/.gemia/config.json
  POST /runtime/dev/workspace   → gated Creative Dev Sandbox workspace
  POST /runtime/dev/workspace/<id>/run → gated Creative Dev Sandbox command runner
  GET  /skills
"""

from __future__ import annotations

import json
import hashlib
import mimetypes
import os
import re
import shutil
import socket
import subprocess
import threading
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

from gemia.model_strength import strongest_media_model


_DEFAULT_CONFIG_PATH = Path.home() / ".gemia" / "config.json"
_DEFAULT_LOCAL_CONFIG_PATH = Path.home() / ".lumeri" / "config.toml"
_CONFIG_PATH = _DEFAULT_CONFIG_PATH
_LOCAL_CONFIG_PATH = _DEFAULT_LOCAL_CONFIG_PATH
_DEFAULT_IMAGE_MODEL = strongest_media_model("image", "openrouter")
_DEFAULT_IMAGE_BASE_URL = "https://openrouter.ai/api/v1"
_VALIDATED_CLOUD_ACCOUNT_ID = ""
_VALIDATED_CLOUD_ACCOUNT_LOCK = threading.RLock()


def _public_config_path() -> Path:
    override = os.environ.get("LUMERI_LOCAL_CONFIG_PATH", "").strip()
    if override:
        return Path(override).expanduser()
    # Test/embedded hosts commonly redirect the legacy config. Keep their
    # public authority beside that isolated root instead of touching real HOME.
    if _CONFIG_PATH != _DEFAULT_CONFIG_PATH and _LOCAL_CONFIG_PATH == _DEFAULT_LOCAL_CONFIG_PATH:
        return _CONFIG_PATH.parent / "lumeri-config.toml"
    return _LOCAL_CONFIG_PATH


def _validated_cloud_account_id() -> str:
    with _VALIDATED_CLOUD_ACCOUNT_LOCK:
        return _VALIDATED_CLOUD_ACCOUNT_ID


def _mark_cloud_account_validated(cloud_account: dict[str, object]) -> None:
    global _VALIDATED_CLOUD_ACCOUNT_ID
    cloud_id = str(cloud_account.get("id") or "").strip()
    with _VALIDATED_CLOUD_ACCOUNT_LOCK:
        _VALIDATED_CLOUD_ACCOUNT_ID = cloud_id


def _clear_cloud_account_validation(*, clear_credential: bool = True) -> None:
    global _VALIDATED_CLOUD_ACCOUNT_ID
    with _VALIDATED_CLOUD_ACCOUNT_LOCK:
        _VALIDATED_CLOUD_ACCOUNT_ID = ""
    if clear_credential:
        try:
            cloud_client = cloud_accounts.client()
            clear = getattr(cloud_client, "clear_credential", None)
            if callable(clear):
                clear()
            clear_auxiliary = getattr(cloud_client, "clear_auxiliary_credentials", None)
            if callable(clear_auxiliary):
                clear_auxiliary()
        except Exception:
            pass


def _close_live_cloud_sessions() -> None:
    """Drop account-bound runners while preserving every durable workdir."""
    try:
        from gemia.session_manager import get_manager

        manager = get_manager()
        for session_id in manager.list_sessions():
            try:
                manager.close_session(session_id, remove_workdir=False)
            except Exception:
                # close_session removes the runner from the live registry
                # before shutting its loop down, so it cannot be reused.
                pass
    except Exception:
        pass


def _legacy_image_model(value: object) -> bool:
    lowered = str(value or "").strip().lower()
    return lowered in {"gpt-image-2", "gpt_image2", "gpt image2"}


def _configured_image_model() -> str:
    value = os.environ.get("GEMIA_IMAGE_MODEL") or ""
    candidate = "" if _legacy_image_model(value) else value
    return strongest_media_model("image", "openrouter", (candidate, _DEFAULT_IMAGE_MODEL))


def _configured_image_base_url() -> str:
    value = os.environ.get("GEMIA_IMAGE_BASE_URL") or os.environ.get("OPENROUTER_IMAGE_URL") or ""
    if value and "sisyphusx.com" not in value:
        return value
    return _DEFAULT_IMAGE_BASE_URL


def _configured_server_host(default: str = "0.0.0.0") -> str:
    return os.environ.get("LUMERI_HOST") or os.environ.get("GEMIA_HOST") or default


def _lan_addresses() -> list[str]:
    addresses: set[str] = set()

    def add_candidate(value: str) -> None:
        value = str(value or "").strip()
        try:
            import ipaddress

            address = ipaddress.ip_address(value)
            benchmark_net = ipaddress.ip_network("198.18.0.0/15")
            if (
                address.version == 4
                and not address.is_loopback
                and not address.is_link_local
                and not address.is_multicast
                and not address.is_unspecified
                and address not in benchmark_net
            ):
                addresses.add(value)
        except Exception:
            return

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            add_candidate(sock.getsockname()[0])
    except Exception:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            add_candidate(str(info[4][0]))
    except Exception:
        pass
    try:
        output = subprocess.check_output(["/sbin/ifconfig"], text=True, stderr=subprocess.DEVNULL, timeout=1.5)
        for line in output.splitlines():
            parts = line.strip().split()
            if len(parts) >= 2 and parts[0] == "inet":
                add_candidate(parts[1])
    except Exception:
        pass
    return sorted(addresses)


def _server_urls(host: str, port: int) -> list[str]:
    if host in {"0.0.0.0", "::", ""}:
        urls = [f"http://127.0.0.1:{port}"]
        urls.extend(f"http://{address}:{port}" for address in _lan_addresses())
        return urls
    return [f"http://{host}:{port}"]


# ── Security gate helpers ────────────────────────────────────────────────
# Defends 7788 against (a) DNS rebinding from a browser tab pointing at a
# malicious page that resolves to 127.0.0.1, and (b) cross-origin POSTs from
# arbitrary local apps. Local CLIs/Tauri/native mobile callers don't send
# Origin/Referer, so they are still allowed; only browser callers with a
# foreign Origin are blocked.

_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}
# Tauri / native shells use these schemes; allow them when Origin is present.
_NATIVE_ORIGIN_SCHEMES = {"tauri", "lumeri", "app"}
_LAN_ADDRESS_CACHE: tuple[float, list[str]] | None = None
_LAN_ADDRESS_TTL_SEC = 30.0


def _cached_lan_addresses() -> list[str]:
    """Cheap LAN-address lookup; the real call shells out to ifconfig."""
    global _LAN_ADDRESS_CACHE
    import time as _time
    now = _time.time()
    if _LAN_ADDRESS_CACHE is not None and now - _LAN_ADDRESS_CACHE[0] < _LAN_ADDRESS_TTL_SEC:
        return list(_LAN_ADDRESS_CACHE[1])
    addrs = _lan_addresses()
    _LAN_ADDRESS_CACHE = (now, list(addrs))
    return list(addrs)


def _host_allowed(host_header: str) -> bool:
    """Return True if the Host header points at this server."""
    raw = (host_header or "").strip().lower()
    if not raw:
        # No Host header: most CLIs and Python urllib still send one, so an
        # empty Host is unusual and easier to block than to defend against.
        return False
    host_only = raw.split("]")[-1].split(":")[0] if raw.startswith("[") else raw.split(":")[0]
    if host_only in _LOOPBACK_HOSTS:
        return True
    return host_only in _cached_lan_addresses()


def _require_account(handler: BaseHTTPRequestHandler) -> str | None:
    """Return the acting account_id or send 401 and return None.

    Cloud mode trusts only the profile written by the boot-time remote session
    check. Legacy mode retains gemia.identity's per-request account pin."""
    if cloud_accounts.enabled():
        # /auth/session performs the remote token validation once at boot.
        # Workspace requests use the resulting local profile so timeline and
        # media interactions never inherit cloud round-trip latency.
        profile = accounts.current_account()
        if not profile:
            _json_response(handler, 401, {"error": "not signed in"})
            return None
        if profile.get("onboarding_completed") is not True:
            _json_response(handler, 403, {"error": "account onboarding required"})
            return None
        cloud_id = str(profile.get("cloud_account_id") or "").strip()
        if not cloud_id or cloud_id != _validated_cloud_account_id():
            _json_response(handler, 401, {"error": "cloud session not validated"})
            return None
        account_id = str(profile.get("account_id") or "").strip()
    else:
        account_id = identity.resolve_account_id(handler)
    if not account_id:
        _json_response(handler, 401, {"error": "not signed in"})
        return None
    return account_id


def _normalized_byok_provider(value: object) -> str:
    provider = str(value or "").strip().lower()
    return {"anthropic": "claude"}.get(provider, provider)


def _cloud_provider_name(value: object) -> str:
    provider = _normalized_byok_provider(value)
    return {"claude": "anthropic"}.get(provider, provider)


_CLOUD_KEYLESS_PROVIDERS = ("lumeri", "openai_subscription")
_CLOUD_BYOK_PROVIDERS = ("openai", "claude", "gemini", "openrouter", "custom")


def _allowed_cloud_providers(profile: dict[str, object]) -> tuple[str, ...]:
    if profile.get("age_band") == "18_plus":
        return (*_CLOUD_KEYLESS_PROVIDERS, *_CLOUD_BYOK_PROVIDERS)
    return _CLOUD_KEYLESS_PROVIDERS


_MODEL_KEY_PROVIDER = {
    "openrouter_api_key": "openrouter",
    "gemini_api_key": "gemini",
    "anthropic_api_key": "claude",
    "openai_api_key": "openai",
}
_CLOUD_AUXILIARY_SECRET_FIELDS = {
    "image_api_key",
    "nano_banana_api_key",
}
_CLOUD_SEARCH_SECRET_FIELDS = {
    "tavily_api_key",
    "brave_api_key",
    "serper_api_key",
    "exa_api_key",
    "bing_api_key",
    "google_cse_key",
    "searxng_api_key",
}
_CLOUD_SEARCH_PROVIDER_SECRET_FIELD = {
    "tavily": "tavily_api_key",
    "brave": "brave_api_key",
    "serper": "serper_api_key",
    "exa": "exa_api_key",
    "bing": "bing_api_key",
    "google_cse": "google_cse_key",
    "searxng": "searxng_api_key",
}
_CLOUD_KEYLESS_SEARCH_PROVIDERS = {"", "auto", "duckduckgo", "searxng"}
_CLOUD_SEARCH_PROVIDERS = {
    *_CLOUD_KEYLESS_SEARCH_PROVIDERS,
    *_CLOUD_SEARCH_PROVIDER_SECRET_FIELD,
}


def _prepare_cloud_config_body(body: dict[str, object]) -> tuple[dict[str, object] | None, str]:
    """Validate a cloud Setup update against the account's age policy."""
    profile = accounts.current_account() or {}
    selected = _normalized_byok_provider(profile.get("model_provider"))
    candidate = _normalized_byok_provider(body.get("provider")) or selected
    cloud_provider = _cloud_provider_name(candidate)
    if not selected or not cloud_provider:
        return None, "account provider is not configured"
    if candidate not in _allowed_cloud_providers(profile):
        return None, "provider is not available for this account"
    profile_id = str(body.get("profile_id") or "").strip()
    if profile_id:
        profile_provider = "custom" if profile_id.startswith("custom:") else _normalized_byok_provider(profile_id)
        if profile_provider != candidate:
            return None, "profile does not match selected provider"
    for field, provider in _MODEL_KEY_PROVIDER.items():
        if field not in body:
            continue
        expected = "openai" if candidate == "custom" else candidate
        if provider != expected:
            return None, "credential field does not match selected provider"
    forbidden = sorted(field for field in _CLOUD_AUXILIARY_SECRET_FIELDS if field in body)
    if forbidden:
        return None, "auxiliary credentials are unavailable in cloud-account mode"
    search_provider = str(body.get("search_provider") or "").strip().lower()
    if search_provider not in _CLOUD_SEARCH_PROVIDERS:
        return None, "search provider is unavailable in cloud-account mode"
    supplied_search_secrets = {
        field for field in _CLOUD_SEARCH_SECRET_FIELDS if field in body
    }
    expected_search_secret = _CLOUD_SEARCH_PROVIDER_SECRET_FIELD.get(search_provider, "")
    if supplied_search_secrets - ({expected_search_secret} if expected_search_secret else set()):
        return None, "search credential does not match selected provider"
    prepared = dict(body)
    prepared["provider"] = candidate
    if candidate != "custom":
        prepared.pop("base_url", None)
    if profile_id:
        prepared["profile_id"] = profile_id
    return prepared, cloud_provider


def _require_provider_access(
    handler: BaseHTTPRequestHandler,
    *,
    requested_provider: object = "",
) -> bool:
    """Allow only providers permitted by the signed-in account's age band.

    Legacy/local-only mode keeps its existing first-run behavior.  In cloud
    mode the account service remains authoritative. Lumeri Credits and OpenAI
    subscription are available to both age bands; external keys remain 18+.
    """
    if cloud_accounts.enabled():
        if _require_account(handler) is None:
            return False
        profile = accounts.current_account() or {}
        requested = _normalized_byok_provider(requested_provider)
        if requested and requested not in _allowed_cloud_providers(profile):
            _json_response(handler, 403, {"error": "provider is not available for this account"})
            return False
        return True
    if accounts.list_accounts() and _require_account(handler) is None:
        return False
    return True


def _require_workspace_account(handler: BaseHTTPRequestHandler) -> str | None:
    """Resolve an account for workspace/media routes in both runtime modes."""
    if cloud_accounts.enabled():
        return _require_account(handler)
    account_id = identity.resolve_account_id(handler)
    if not account_id:
        _json_response(handler, 401, {"error": "not signed in"})
        return None
    return account_id


def _active_media_project_id(account_id: str) -> str:
    """Return the active Project scope without accepting caller-selected ids."""
    from gemia.media_library import LEGACY_PROJECT_SCOPE
    from gemia.session_history import load_current_session

    try:
        session = load_current_session(account_id=account_id)
    except Exception:
        return LEGACY_PROJECT_SCOPE
    project_id = str(session.get("project_id") or "").strip()
    if project_id:
        return project_id
    project = session.get("project") if isinstance(session.get("project"), dict) else {}
    return str(project.get("project_id") or project.get("id") or "").strip() or LEGACY_PROJECT_SCOPE


def _video_path_allowed(account_id: str | None, video: str) -> bool:
    """Reject media paths that don't live in this account's library or in
    the project-local input/output staging dirs. Used by /video-summary to
    keep unauthenticated callers (or swapped accounts) from coercing ffmpeg
    into reading another user's media originals.
    """
    if not video:
        return False
    try:
        resolved = Path(video).expanduser().resolve()
    except Exception:
        return False
    base = _BASE_DIR.resolve()
    candidates: list[Path] = [
        (base / name).resolve()
        for name in ("inputs", "outputs", "frames", "styled", "demo", "uploads", "temp")
    ]
    if account_id:
        try:
            from gemia.media_library import cache_root, originals_root
            candidates.append(originals_root(account_id).resolve())
            candidates.append(cache_root(account_id).resolve())
        except Exception:
            pass
    for root in candidates:
        try:
            resolved.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def _origin_allowed(origin_or_referer: str) -> bool:
    """Return True if Origin/Referer points at this loopback or our LAN host."""
    value = (origin_or_referer or "").strip()
    if not value:
        return True  # absent header → not a browser cross-origin call
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


def _is_loopback_client(handler: BaseHTTPRequestHandler) -> bool:
    try:
        import ipaddress

        return ipaddress.ip_address(str(handler.client_address[0])).is_loopback
    except Exception:
        return False


def _load_config_keys() -> None:
    """Load API keys from ~/.gemia/config.json into env vars (if not already set)."""
    if cloud_accounts.enabled():
        return
    if _CONFIG_PATH.exists():
        try:
            cfg = json.loads(_CONFIG_PATH.read_text())
            if key := cfg.get("openrouter_api_key"):
                os.environ.setdefault("OPENROUTER_API_KEY", key)
            if key := cfg.get("gemini_api_key"):
                os.environ.setdefault("GEMINI_API_KEY", key)
            if key := cfg.get("laozhang_api_key"):
                os.environ.setdefault("LAOZHANG_API_KEY", key)
            if value := cfg.get("image_base_url"):
                os.environ.setdefault("GEMIA_IMAGE_BASE_URL", value)
            if value := cfg.get("openrouter_image_url"):
                os.environ.setdefault("OPENROUTER_IMAGE_URL", value)
            if value := cfg.get("image_model"):
                if not _legacy_image_model(value):
                    os.environ.setdefault("GEMIA_IMAGE_MODEL", value)
        except Exception:
            pass


def _has_valid_key() -> bool:
    key = os.environ.get("OPENROUTER_API_KEY", "")
    return bool(key) and key not in ("test", "sk-or-...") and len(key) > 10


def _cloud_credential_snapshot() -> dict[str, str]:
    if not cloud_accounts.enabled():
        return {}
    try:
        snapshot = cloud_accounts.client().credential_snapshot()
        return dict(snapshot or {})
    except Exception:
        return {}


def _has_valid_image_key() -> bool:
    # OPENAI_API_KEY is intentionally NOT consulted; see GenerativeClient docstring.
    try:
        cfg = json.loads(_CONFIG_PATH.read_text()) if _CONFIG_PATH.exists() else {}
    except Exception:
        cfg = {}
    key = (
        os.environ.get("OPENROUTER_API_KEY")
        or os.environ.get("GEMIA_OPENROUTER_API_KEY")
        or os.environ.get("GEMIA_IMAGE_API_KEY")
        or str(cfg.get("openrouter_api_key") or cfg.get("image_api_key") or cfg.get("nano_banana_api_key") or "")
    )
    return bool(key) and key not in ("test", "sk-...") and len(key) > 10


def _model_profile_payload() -> dict:
    from gemia.memory import public_model_profile

    return public_model_profile()

from gemia import accounts
from gemia import cloud_accounts
from gemia import identity
from gemia.artifacts import artifact_outputs as _artifact_outputs
from gemia.artifacts import is_document_artifact_output as _is_document_artifact_output
from gemia.artifacts import is_media_output as _is_media_output
from gemia.artifacts import is_video_output as _is_video_output
from gemia.artifacts import media_outputs as _media_outputs
from gemia.artifacts import output_paths as _output_paths
from gemia.stability import (
    TASK_STATUSES,
    error_envelope as _stability_error_envelope,
    error_event as _stability_error_event,
    normalize_task_status as _normalize_task_status,
    stability_gate_enabled as _stability_gate_enabled,
)
from gemia.ai.sub_agents import SubAgentRegistry
from gemia.sandbox_v4 import set_sandbox_disabled as _set_v4_sandbox_disabled, is_sandbox_disabled as _is_v4_sandbox_disabled


def _auth_session_payload() -> dict[str, object]:
    if not cloud_accounts.enabled():
        return accounts.auth_session_payload()
    previous_cloud_id = _validated_cloud_account_id()
    # Revalidation is the only operation allowed to arm workspace access.
    # Clear first so an account/token change cannot race with the old marker.
    _clear_cloud_account_validation()
    try:
        cloud_client = cloud_accounts.client()
        cloud_account = cloud_client.current_account(sync_credential=False)
        if cloud_account:
            next_cloud_id = str(cloud_account.get("id") or "").strip()
            if previous_cloud_id and previous_cloud_id != next_cloud_id:
                _close_live_cloud_sessions()
            sync_credential = getattr(cloud_client, "sync_selected_credential", None)
            if callable(sync_credential):
                sync_credential(cloud_account)
    except cloud_accounts.CloudAuthError as exc:
        accounts.sign_out()
        if previous_cloud_id:
            _close_live_cloud_sessions()
        return {
            "account": None,
            "accounts": [],
            "cloud_login_enabled": True,
            "account_service_available": False,
            "service_error": str(exc),
        }
    profile = accounts.activate_cloud_account(cloud_account) if cloud_account else None
    if not profile:
        accounts.sign_out()
        if previous_cloud_id:
            _close_live_cloud_sessions()
    else:
        _mark_cloud_account_validated(cloud_account)
        try:
            stored = json.loads(_CONFIG_PATH.read_text()) if _CONFIG_PATH.exists() else {}
            search_provider = str(stored.get("search_provider") or "").strip().lower()
            sync_auxiliary = getattr(cloud_client, "sync_auxiliary_credential", None)
            if search_provider in _CLOUD_SEARCH_PROVIDER_SECRET_FIELD and callable(sync_auxiliary):
                sync_auxiliary(search_provider)
        except Exception:
            # Search is optional and has a built-in DuckDuckGo fallback. A
            # missing/stale auxiliary credential must not invalidate login.
            pass
    return {
        "account": profile,
        "accounts": [profile] if profile else [],
        "cloud_login_enabled": True,
        "account_service_available": True,
    }

# In-memory store for pending ask sessions. Each entry MUST carry account_id
# and created_at so that account-switch cannot let user B answer user A's ask.
# In-memory store for task execution progress {task_id: {current_step, total_steps, current_function}}
_BASE_DIR = Path(__file__).resolve().parent
_SKILLS_DIR = _BASE_DIR / "skills"
_SKILLS_V2_DIR = _BASE_DIR / "skills_v2"
_STATIC_DIR = _BASE_DIR / "static"
_INPUTS_DIR = _BASE_DIR / "inputs"
# Directories that may be served via /file/. Keep this in sync with the
# frontend's project-relative output path resolver.
_ALLOWED_ROOTS = {"outputs", "frames", "styled", "demo", "inputs", "uploads", "temp", "timeline"}
_TASKS_DIR = _BASE_DIR / "tasks"
_PLANS_DIR = _BASE_DIR / "plans"


def _json_response(handler: BaseHTTPRequestHandler, status: int, body: object) -> None:
    data = json.dumps(body, ensure_ascii=False).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def _html_response(handler: BaseHTTPRequestHandler, status: int, html: str, *, body: bool = True) -> None:
    data = html.encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    if body:
        handler.wfile.write(data)


def _empty_response(handler: BaseHTTPRequestHandler, status: int = 204) -> None:
    handler.send_response(status)
    handler.send_header("Content-Length", "0")
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()


def _error_payload(exc: Exception, *, context: str = "") -> dict[str, object]:
    return _stability_error_envelope(exc, context=context)


def _human_error_message(exc: Exception) -> str:
    return str(_error_payload(exc).get("user_message") or str(exc))


def _read_json_body(handler: BaseHTTPRequestHandler) -> dict:
    length = int(handler.headers.get("Content-Length", 0))
    raw = handler.rfile.read(length) if length else b"{}"
    payload = json.loads(raw or b"{}")
    if not isinstance(payload, dict):
        raise ValueError("JSON body must be an object")
    return payload


def _auth_callback_html(*, ok: bool, message: str) -> str:
    color = "#7dd3c7" if ok else "#ff6b82"
    title = "Lumeri 登录完成" if ok else "Lumeri 登录失败"
    safe_message = (
        message.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{title}</title>
  <style>
    body {{
      margin: 0;
      min-height: 100vh;
      display: grid;
      place-items: center;
      background: #080b0f;
      color: #edf3f7;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }}
    main {{
      width: min(420px, calc(100vw - 40px));
      border-radius: 14px;
      background: #11161d;
      padding: 24px;
      box-shadow: 0 24px 80px rgba(0, 0, 0, 0.45);
    }}
    h1 {{ margin: 0 0 10px; font-size: 18px; color: {color}; }}
    p {{ margin: 0; color: #9aa7b4; font-size: 14px; line-height: 1.7; }}
  </style>
</head>
<body>
  <main>
    <h1>{title}</h1>
    <p>{safe_message}</p>
  </main>
  <script>
    try {{ window.opener && window.opener.postMessage({{ type: "lumeri-auth-complete", ok: {str(ok).lower()} }}, "*"); }} catch (_) {{}}
    setTimeout(() => {{ try {{ window.close(); }} catch (_) {{}} }}, 1400);
  </script>
</body>
</html>"""


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
            handler.send_header("Content-Type", content_type)
            handler.send_header("Content-Range", f"bytes */{size}")
            handler.send_header("Accept-Ranges", "bytes")
            handler.send_header("Cache-Control", "no-store")
            handler.send_header("Connection", "close")
            handler.end_headers()
            handler.close_connection = True
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
            with path.open("rb") as file_obj:
                file_obj.seek(start)
                remaining = content_length
                while remaining > 0:
                    chunk = file_obj.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    handler.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass
    handler.close_connection = True


def _vnext_enabled() -> bool:
    return os.environ.get("LUMERAI_VNEXT", "0") == "1"


def _creative_sandbox_service():
    from gemia.creative_sandbox import CreativeSandboxService

    return CreativeSandboxService(_BASE_DIR)


def _creative_sandbox_error_response(handler: BaseHTTPRequestHandler, exc: Exception) -> None:
    from gemia.creative_sandbox import creative_sandbox_error_payload

    status, payload = creative_sandbox_error_payload(exc)
    _json_response(handler, status, payload)


def _creative_sandbox_preview_payload(service, session_id: str) -> dict:
    payload = service.latest_preview(session_id)
    preview = payload.get("preview")
    if isinstance(preview, dict):
        path = str(preview.get("path") or "")
        kind = str(preview.get("kind") or "previews")
        rel_path = path.split("/", 1)[1] if path.startswith(f"{kind}/") else path
        payload["raw_url"] = (
            f"/runtime/dev/workspace/{quote(session_id)}/files"
            f"?raw=1&kind={quote(kind)}&path={quote(rel_path)}"
        )
    return payload


def _safe_child_path(root: Path, rel: str) -> Path | None:
    """Return a resolved file below root, or None when rel escapes root."""
    try:
        if "\x00" in rel:
            return None
        resolved_root = Path(root).resolve()
        candidate = (resolved_root / rel.lstrip("/")).resolve()
        candidate.relative_to(resolved_root)
    except (OSError, ValueError):
        return None
    return candidate if candidate.exists() and candidate.is_file() else None


def _health_payload() -> dict:
    checks: list[dict[str, object]] = []

    def add(name: str, ok: bool, detail: str = "", *, required: bool = True, extra: dict | None = None) -> None:
        item: dict[str, object] = {"name": name, "ok": bool(ok), "required": bool(required)}
        if detail:
            item["detail"] = detail
        if extra:
            item.update(extra)
        checks.append(item)

    if cloud_accounts.enabled():
        add(
            "config.cloud_account",
            True,
            "Provider credentials are resolved after account login",
            required=False,
        )
    else:
        add("config.openrouter", _has_valid_key(), "OpenRouter key present" if _has_valid_key() else "OpenRouter key missing", required=False)
        add("config.image", _has_valid_image_key(), "image provider key present" if _has_valid_image_key() else "image provider key missing", required=False)
    for name, path in (("outputs_dir", _BASE_DIR / "outputs"), ("tasks_dir", _TASKS_DIR)):
        add(name, _dir_is_writable(path), str(path))

    frontend_ok, frontend_detail = _frontend_dist_health()
    add("frontend_dist", frontend_ok, frontend_detail)

    session_ok, session_detail = _session_health()
    add("session_video_refs", session_ok, session_detail, required=False)

    blender = _lightweight_blender_status()
    add(
        "blender_lumerilink",
        bool(blender.get("available")),
        str(blender.get("blender_path") or "Blender not found; local fallback remains available"),
        required=False,
        extra={"available": bool(blender.get("available"))},
    )

    input_log_dir = Path.home() / "Desktop" / "Lumeri Gemini Inputs"
    add(
        "model_input_observability",
        bool(os.environ.get("GEMIA_INPUT_TXT_LOG")) or input_log_dir.exists(),
        str(input_log_dir),
        required=False,
    )
    add("stability_gate", _stability_gate_enabled(), "stable-first capability gate")
    ok = all(item["ok"] for item in checks if item.get("required", True))
    return {
        "ok": ok,
        "status": "ok" if ok else "degraded",
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "task_statuses": sorted(TASK_STATUSES),
        "checks": checks,
    }


def _lightweight_blender_status() -> dict[str, object]:
    """Probe Blender without importing the heavy video runtime during startup."""
    candidates = (
        os.environ.get("LUMERI_BLENDER_PATH", ""),
        os.environ.get("GEMIA_BLENDER_PATH", ""),
        shutil.which("blender") or "",
        "/Applications/Blender.app/Contents/MacOS/Blender",
    )
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return {"available": True, "blender_path": str(Path(candidate))}
    return {"available": False, "blender_path": None}


def _dir_is_writable(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / f".lumeri-health-{uuid.uuid4().hex[:8]}"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except Exception:
        return False


def _frontend_dist_health() -> tuple[bool, str]:
    v3_root = _STATIC_DIR / "v3"
    missing = [name for name in ("index.html", "v3.js", "v3.css") if not (v3_root / name).exists()]
    if missing:
        return False, "missing v3 frontend files: " + ", ".join(missing)
    return True, str(v3_root / "index.html")


def _session_health() -> tuple[bool, str]:
    account_id = accounts.current_account_id()
    if not account_id:
        return True, "no active account session"
    try:
        from gemia.session_history import load_current_session

        payload = load_current_session(account_id=account_id)
    except Exception as exc:
        return False, _human_error_message(exc)
    bad_paths: list[str] = []

    def visit(value: object, key: str = "") -> None:
        if isinstance(value, dict):
            for nested_key, nested_value in value.items():
                visit(nested_value, str(nested_key))
        elif isinstance(value, list):
            for nested in value:
                visit(nested, key)
        elif key in {"video_src", "server_video_path", "serverVideoPath", "previewSrc", "serverPath"}:
            text = str(value or "")
            if text and not _is_session_playable_media_ref(text):
                bad_paths.append(text)

    visit(payload)
    if bad_paths:
        return False, f"non-media video refs: {bad_paths[:3]}"
    return True, "session video refs are media-only"


def _is_session_playable_media_ref(value: object) -> bool:
    text = str(value or "").strip()
    if not text:
        return True
    if _is_media_output(text):
        return True
    try:
        parsed = urlparse(text)
    except ValueError:
        parsed = None
    path = unquote(parsed.path if parsed and parsed.scheme in {"http", "https"} else text)
    parts = [part for part in path.split("/") if part]
    if len(parts) >= 4 and parts[0] == "media-library" and parts[1] == "file":
        return parts[3] == "original" or parts[3] == "cache"
    return False


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: object) -> None:  # quieter logs
        print(f"  {self.address_string()} {fmt % args}")

    def _security_gate(self, *, mutating: bool) -> bool:
        """Return True if the request should be rejected (and a response sent).

        Always validates the Host header. For mutating verbs (POST/DELETE)
        also validates Origin/Referer to block DNS-rebinding from a browser
        page that resolves to 127.0.0.1.
        """
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

    def _handle_get_like(self, *, body: bool) -> None:
        parsed_url = urlparse(self.path)
        path = unquote(parsed_url.path).rstrip("/") or "/"

        if path == "/api/internal/v1" or path.startswith("/api/internal/v1/"):
            if cloud_accounts.enabled() and _require_account(self) is None:
                return
            from gemia.api_v1_routes import try_handle as _api_v1_try

            if _api_v1_try(self, method=("GET" if body else "HEAD"), body=body):
                return
        if path == "/api/v1" or path.startswith("/api/v1/"):
            from gemia.api_v1_routes import legacy_path

            previous_path = self.path
            suffix = legacy_path(path)
            self.path = suffix + (
                f"?{parsed_url.query}" if parsed_url.query else ""
            )
            try:
                self._handle_get_like(body=body)
            finally:
                self.path = previous_path
            return

        # Root: remote visitors go straight to Video; local stays blank
        # (reserved for a future Lumeri family portal).
        if path == "/":
            if self.headers.get("X-Lumeri-Remote"):
                self.send_response(302)
                self.send_header("Location", "/video")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            _html_response(
                self,
                200,
                '<!doctype html><html lang="en"><head><meta charset="utf-8">'
                "<title>Lumeri</title></head><body></body></html>",
                body=body,
            )
            return

        if path == "/favicon.ico":
            _empty_response(self)
            return

        if path == "/health":
            _json_response(self, 200, _health_payload())
            return

        if path == "/settings/sandbox":
            if cloud_accounts.enabled() and _require_account(self) is None:
                return
            _json_response(self, 200, {"sandbox_disabled": _is_v4_sandbox_disabled()})
            return
        # Lumeri v3 protocol surface (live sessions + durable projects/runs).
        if (
            path == "/sessions"
            or path.startswith("/sessions/")
            or path == "/projects"
            or path.startswith("/projects/")
        ):
            if cloud_accounts.enabled() and _require_account(self) is None:
                return
            from gemia.v3_routes import try_handle as _v3_try
            if _v3_try(self, method=("GET" if body else "HEAD")):
                return
        # Read-only file browsing for the web UI (whitelisted roots only).
        if path.startswith("/files/"):
            if cloud_accounts.enabled() and _require_account(self) is None:
                return
            from gemia.file_browse_routes import try_handle as _files_try
            if _files_try(
                self,
                method=("GET" if body else "HEAD"),
                serve_file=lambda p: _file_response(self, p, body=body),
            ):
                return
        # Quanta uses the shared Lumeri workspace shell.  Keep the standalone
        # kernel demonstration available as evidence at /quanta/demo.
        if path == "/quanta" or path.startswith("/quanta/"):
            if path in ("/quanta", "/quanta/"):
                target = (Path(__file__).resolve().parent / "static" / "v3" / "index.html").resolve()
                _file_response(self, target, body=body)
                return
            rel = "index.html" if path in ("/quanta/demo", "/quanta/demo/") else path[len("/quanta/"):]
            quanta_root = (Path(__file__).resolve().parent / "static" / "v3" / "quanta").resolve()
            target = _safe_child_path(quanta_root, rel)
            if target is None:
                _json_response(self, 404, {"error": "quanta asset not found"})
                return
            _file_response(self, target, body=body)
            return

        # Lumeri Video frontend (vanilla HTML/JS, on disk at static/v3/).
        if path == "/video" or path == "/video/" or path.startswith("/video/"):
            rel = "index.html" if path in ("/video", "/video/") else path[len("/video/"):]
            v3_root = (Path(__file__).resolve().parent / "static" / "v3").resolve()
            target = _safe_child_path(v3_root, rel)
            if target is None:
                _json_response(self, 404, {"error": "video asset not found"})
                return
            _file_response(self, target, body=body)
            return

        # Legacy /v3 URLs — permanently moved to /video (old links keep working).
        if path == "/v3" or path == "/v3/" or path.startswith("/v3/"):
            suffix = "" if path in ("/v3", "/v3/") else "/" + path[len("/v3/"):]
            self.send_response(301)
            self.send_header("Location", "/video" + suffix)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if path == "/config/codex-login-status":
            if not _require_provider_access(self, requested_provider="openai_subscription"):
                return
            from gemia import brain_config

            status, payload = brain_config.codex_login_bridge("GET")
            _json_response(self, status, payload)
            return

        # Config status (for first-run key check). Network topology fields
        # (bind host, port, LAN URLs) are gated behind a signed-in account so
        # the first-run check stays anonymous but a logged-in user can still
        # retrieve LAN pairing info.
        if path == "/config":
            if cloud_accounts.enabled() and _require_account(self) is None:
                return
            cloud_profile = accounts.current_account() or {}
            cloud_snapshot = _cloud_credential_snapshot()
            cloud_byok_allowed = cloud_profile.get("age_band") == "18_plus"
            allowed_cloud_providers = (
                list(_allowed_cloud_providers(cloud_profile))
                if cloud_accounts.enabled()
                else []
            )
            payload = {
                "has_key": bool(cloud_snapshot.get("secret"))
                if cloud_accounts.enabled()
                else _has_valid_key(),
                "has_image_key": bool(
                    cloud_snapshot.get("secret")
                    and cloud_snapshot.get("local_provider") == "openrouter"
                )
                if cloud_accounts.enabled()
                else _has_valid_image_key(),
                "image_provider": "openrouter/nano-banana",
                "stability_gate": _stability_gate_enabled(),
                "health_url": "/health",
                "byok_allowed": not cloud_accounts.enabled()
                or cloud_byok_allowed,
            }
            try:
                from gemia import local_config

                if not _is_loopback_client(self) and not identity.resolve_account_id(self):
                    raise PermissionError("public local config is loopback-only")
                _public_source = {}
                if _CONFIG_PATH.exists():
                    loaded = json.loads(_CONFIG_PATH.read_text())
                    _public_source = loaded if isinstance(loaded, dict) else {}
                if cloud_accounts.enabled():
                    _public_source = cloud_accounts.runtime_model_config(_public_source)
                payload["local_config"] = local_config.load_or_create(
                    _public_source, path=_public_config_path()
                )
            except Exception:
                pass
            if cloud_accounts.enabled():
                payload.update(
                    {
                        "allowed_providers": allowed_cloud_providers,
                        "selected_provider": _normalized_byok_provider(
                            cloud_profile.get("model_provider")
                        ),
                        "provider_mode": cloud_profile.get("provider_mode") or "",
                    }
                )
            if (
                (cloud_accounts.enabled() and _validated_cloud_account_id())
                or identity.resolve_account_id(self)
            ):
                bind_host = _configured_server_host()
                try:
                    bind_port = int(os.environ.get("LUMERI_PORT") or os.environ.get("GEMIA_PORT") or "7788")
                except ValueError:
                    bind_port = 7788
                payload.update(
                    {
                        "image_model": _configured_image_model(),
                        "image_base_url": _configured_image_base_url(),
                        "server_bind_host": bind_host,
                        "server_port": bind_port,
                        "server_urls": _server_urls(bind_host, bind_port),
                    }
                )
                # 搜索引擎配置状态（密钥脱敏，供 Setup 面板渲染）。
                try:
                    _cfg_search = {}
                    if _CONFIG_PATH.exists():
                        _cfg_search = json.loads(_CONFIG_PATH.read_text())
                    if cloud_accounts.enabled():
                        provider = str(_cfg_search.get("search_provider") or "auto").lower()
                        auxiliary_snapshot = {}
                        try:
                            snapshot = getattr(
                                cloud_accounts.client(),
                                "auxiliary_credential_snapshot",
                                None,
                            )
                            auxiliary_snapshot = snapshot() if callable(snapshot) else {}
                        except Exception:
                            auxiliary_snapshot = {}
                        payload["search"] = {
                            "provider": provider
                            if provider in _CLOUD_SEARCH_PROVIDERS
                            else "auto",
                            "has_key": {
                                name: bool((auxiliary_snapshot.get(name) or {}).get("secret"))
                                for name in _CLOUD_SEARCH_PROVIDER_SECRET_FIELD
                            },
                            "allowed_providers": sorted(
                                _CLOUD_SEARCH_PROVIDERS - {""}
                            ),
                        }
                    else:
                        payload["search"] = {
                            "provider": _cfg_search.get("search_provider", "auto"),
                            "has_key": {
                                "tavily": bool(_cfg_search.get("tavily_api_key")),
                                "brave": bool(_cfg_search.get("brave_api_key")),
                                "serper": bool(_cfg_search.get("serper_api_key")),
                                "exa": bool(_cfg_search.get("exa_api_key")),
                                "bing": bool(_cfg_search.get("bing_api_key")),
                                "google_cse": bool(_cfg_search.get("google_cse_key") and _cfg_search.get("google_cse_id")),
                                "searxng": bool(_cfg_search.get("searxng_url")),
                            },
                        }
                except Exception:
                    pass
                # 大脑 provider 现状（密钥脱敏，供 Setup 面板渲染）。
                try:
                    from gemia import brain_config
                    _cfg = {}
                    if _CONFIG_PATH.exists():
                        _cfg = json.loads(_CONFIG_PATH.read_text())
                    if cloud_accounts.enabled():
                        _cfg = cloud_accounts.runtime_model_config(_cfg)
                    else:
                        from gemia import local_config

                        _cfg = local_config.merge_with_secret_config(
                            _cfg, path=_public_config_path()
                        )
                    brain = brain_config.read_status(_cfg)
                    if isinstance(payload.get("local_config"), dict):
                        brain["fast_mode"] = payload["local_config"].get(
                            "features", {}
                        ).get("fast_mode", {})
                    if cloud_accounts.enabled():
                        lumeri_credits = {
                            "id": "lumeri",
                            "label": "Lumeri Credits",
                            "hint": "保存为账户级供应商偏好；Credits 结算功能后续开放",
                            "fields": [],
                            "key_field": None,
                            "recommended_model": "",
                            "model_presets": [],
                        }
                        allowed = set(allowed_cloud_providers)
                        providers = [lumeri_credits, *brain.get("providers", [])]
                        brain["providers"] = [
                            provider
                            for provider in providers
                            if _normalized_byok_provider(provider.get("id")) in allowed
                        ]
                        brain["allowed_providers"] = allowed_cloud_providers
                        brain["cloud_account_mode"] = True
                    payload["brain"] = brain
                except Exception:
                    pass
            _json_response(self, 200, payload)
            return

        if path == "/model":
            if cloud_accounts.enabled() and _require_account(self) is None:
                return
            from gemia.memory import model_selection_payload

            _json_response(self, 200, model_selection_payload("planner"))
            return

        if path == "/auth/session":
            _json_response(self, 200, _auth_session_payload())
            return

        if path == "/accounts":
            session_payload = _auth_session_payload()
            if cloud_accounts.enabled():
                _json_response(self, 200, session_payload)
            else:
                _json_response(self, 200, {"accounts": accounts.list_accounts(), **session_payload})
            return

        if path == "/agent-links/status":
            if cloud_accounts.enabled() and _require_account(self) is None:
                return
            from gemia.agent_links import status_payload

            _json_response(self, 200, status_payload())
            return

        if path == "/agent-links/messages":
            if cloud_accounts.enabled() and _require_account(self) is None:
                return
            from gemia.agent_links import list_messages

            query = parse_qs(parsed_url.query)
            try:
                limit = int(query.get("limit", ["80"])[0] or 80)
            except ValueError:
                limit = 80
            _json_response(self, 200, {"ok": True, "messages": list_messages(limit=limit)})
            return

        if path.startswith("/runtime/dev/workspace"):
            if cloud_accounts.enabled() and _require_account(self) is None:
                return
            if not _vnext_enabled():
                _json_response(self, 404, {"error": "vNext runtime is disabled"})
                return
            try:
                parts = path.split("/")
                service = _creative_sandbox_service()
                if len(parts) >= 5 and parts[1:4] == ["runtime", "dev", "workspace"]:
                    session_id = unquote(parts[4]).strip()
                    action = parts[5] if len(parts) >= 6 else ""
                    if not action:
                        _json_response(self, 200, service.get_workspace(session_id))
                        return
                    if action == "artifacts":
                        _json_response(self, 200, service.list_artifacts(session_id))
                        return
                    if action == "preview":
                        _json_response(self, 200, _creative_sandbox_preview_payload(service, session_id))
                        return
                    if action == "report":
                        report = service.report(session_id)
                        preview = report.get("preview")
                        if isinstance(preview, dict):
                            report["preview"] = _creative_sandbox_preview_payload(service, session_id)
                        _json_response(self, 200, report)
                        return
                    if action == "logs":
                        _json_response(self, 200, service.list_logs(session_id))
                        return
                    if action == "files":
                        query = parse_qs(urlparse(self.path).query)
                        if str((query.get("raw") or [""])[0]).strip().lower() in {"1", "true", "yes"}:
                            target = service.file_path(
                                session_id,
                                {
                                    "kind": (query.get("kind") or ["scripts"])[0],
                                    "path": (query.get("path") or [""])[0],
                                },
                            )
                            _file_response(self, target)
                            return
                        _json_response(
                            self,
                            200,
                            service.read_file(
                                session_id,
                                {
                                    "kind": (query.get("kind") or ["scripts"])[0],
                                    "path": (query.get("path") or [""])[0],
                                },
                            ),
                        )
                        return
                _json_response(self, 404, {"error": "creative sandbox route not found"})
            except Exception as exc:
                _creative_sandbox_error_response(self, exc)
            return

        if path == "/auth/google/callback":
            query = parse_qs(parsed_url.query)
            google_error = (query.get("error") or [""])[0]
            if google_error:
                _html_response(
                    self,
                    400,
                    _auth_callback_html(ok=False, message=f"Google 返回错误：{google_error}。您可以回到 Lumeri 重试。"),
                    body=body,
                )
                return
            state = (query.get("state") or [""])[0]
            code = (query.get("code") or [""])[0]
            try:
                profile = accounts.finish_google_oauth(state, code)
                name = str(profile.get("name") or profile.get("email") or "当前账号")
                _html_response(
                    self,
                    200,
                    _auth_callback_html(ok=True, message=f"{name} 已登录。您现在可以返回 Lumeri 了。"),
                    body=body,
                )
            except Exception as exc:
                _html_response(
                    self,
                    400,
                    _auth_callback_html(ok=False, message=f"{exc}。您现在可以返回 Lumeri 重试。"),
                    body=body,
                )
            return

        if path == "/project/current":
            from gemia.project_model import normalize_project
            from gemia.session_history import load_current_session

            account_id = _require_workspace_account(self)
            if account_id is None:
                return
            session = load_current_session(account_id=account_id)
            project = normalize_project(
                session.get("project") if isinstance(session.get("project"), dict) else None,
                project_state=session.get("project_state") if isinstance(session.get("project_state"), dict) else None,
                account_id=account_id,
            )
            _json_response(self, 200, {"project": project})
            return

        if path == "/session-history":
            from gemia.session_history import load_current_session

            if cloud_accounts.enabled():
                account_id = _require_account(self)
                if account_id is None:
                    return
            else:
                try:
                    account_id = identity.resolve_account_id(self)
                except Exception:
                    account_id = None
            _json_response(self, 200, load_current_session(account_id=account_id))
            return

        if path == "/session-history/list":
            from gemia.session_history import list_session_snapshots
            from gemia.session_manager import get_manager

            if cloud_accounts.enabled():
                account_id = _require_account(self)
                if account_id is None:
                    return
            else:
                try:
                    account_id = identity.resolve_account_id(self)
                except Exception:
                    account_id = None
            query = parse_qs(parsed_url.query)
            try:
                limit = int(query.get("limit", ["30"])[0] or 30)
            except ValueError:
                limit = 30
            snapshots = list_session_snapshots(limit=limit, account_id=account_id)
            records = get_manager().list_persisted_sessions(include_deleted=True)
            session_meta = {
                str(record.get("session_id") or ""): record
                for record in records
                if (
                    not account_id
                    or str(record.get("account_id") or "") == str(account_id)
                )
            }
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

            if cloud_accounts.enabled():
                account_id = _require_account(self)
                if account_id is None:
                    return
            else:
                try:
                    account_id = identity.resolve_account_id(self)
                except Exception:
                    account_id = None
            snapshot_id = unquote(path.removeprefix("/session-history/")).strip()
            try:
                _json_response(self, 200, load_session_snapshot(snapshot_id, account_id=account_id, activate=True))
            except FileNotFoundError:
                _json_response(self, 404, {"error": "session not found"})
            except Exception as exc:
                _json_response(self, 500, {"error": str(exc)})
            return

        if path == "/media-library/list":
            from gemia.media_library import list_assets

            account_id = _require_workspace_account(self)
            if account_id is None:
                return
            query = parse_qs(parsed_url.query)
            kind = str(query.get("kind", [""])[0] or "")
            q = str(query.get("q", [""])[0] or "")
            try:
                limit = int(query.get("limit", ["200"])[0] or 200)
            except ValueError:
                limit = 200
            project_id = _active_media_project_id(account_id)
            _json_response(
                self,
                200,
                {"assets": list_assets(account_id, kind=kind, q=q, limit=limit, project_id=project_id)},
            )
            return

        if path.startswith("/media-library/prepare/"):
            from gemia.roughcut import RoughcutError, get_prepare_job

            account_id = _require_workspace_account(self)
            if account_id is None:
                return
            job_id = path.removeprefix("/media-library/prepare/").strip()
            try:
                _json_response(
                    self,
                    200,
                    get_prepare_job(
                        account_id,
                        job_id,
                        project_id=_active_media_project_id(account_id),
                    ),
                )
            except RoughcutError as exc:
                _json_response(self, 404, {"error": str(exc)})
            return

        if path.startswith("/media-library/") and path.endswith("/roughcut"):
            from gemia.roughcut import RoughcutError, load_roughcut

            account_id = _require_workspace_account(self)
            if account_id is None:
                return
            parts = path.split("/")
            asset_id = parts[2] if len(parts) >= 4 else ""
            try:
                _json_response(
                    self,
                    200,
                    {"manifest": load_roughcut(
                        account_id,
                        asset_id,
                        project_id=_active_media_project_id(account_id),
                    )},
                )
            except RoughcutError as exc:
                _json_response(self, 404, {"error": str(exc)})
            return

        if path.startswith("/media-library/") and path.endswith("/annotations"):
            from gemia.media_annotations import MediaAnnotationError, list_annotations

            account_id = _require_workspace_account(self)
            if account_id is None:
                return
            parts = path.split("/")
            asset_id = parts[2] if len(parts) >= 4 else ""
            try:
                _json_response(
                    self,
                    200,
                    {"annotations": list_annotations(account_id, asset_id, project_id=_active_media_project_id(account_id))},
                )
            except MediaAnnotationError as exc:
                _json_response(self, 404, {"error": str(exc)})
            return

        if path.startswith("/media-library/file/"):
            from gemia.media_library import MediaLibraryError, resolve_asset_file

            account_id = _require_workspace_account(self)
            if account_id is None:
                return
            parts = path.split("/")
            try:
                asset_id = parts[3] if len(parts) >= 5 else ""
                area = parts[4] if len(parts) >= 5 else ""
                filename = parts[5] if len(parts) >= 6 else None
                _file_response(
                    self,
                    resolve_asset_file(
                        account_id,
                        asset_id,
                        area,
                        filename,
                        project_id=_active_media_project_id(account_id),
                    ),
                    body=body,
                )
            except MediaLibraryError as exc:
                _json_response(self, 404, {"error": str(exc)})
            return

        if path.startswith("/media-library/"):
            from gemia.media_library import get_asset

            account_id = _require_workspace_account(self)
            if account_id is None:
                return
            parts = path.split("/")
            asset_id = parts[2] if len(parts) >= 3 else ""
            asset = get_asset(account_id, asset_id, project_id=_active_media_project_id(account_id))
            if not asset:
                _json_response(self, 404, {"error": "media asset not found"})
                return
            _json_response(self, 200, {"asset": asset})
            return

        # Safe file serving: /file/outputs/..., /file/demo/...
        if path.startswith("/file/"):
            if cloud_accounts.enabled() and _require_account(self) is None:
                return
            rel = path[len("/file/"):]
            # Reject traversal attempts
            parts_rel = Path(rel).parts
            if not parts_rel or parts_rel[0] not in _ALLOWED_ROOTS or ".." in parts_rel:
                _json_response(self, 403, {"error": "forbidden"})
                return
            resolved = (_BASE_DIR / rel).resolve()
            try:
                resolved.relative_to(_BASE_DIR.resolve())
            except ValueError:
                # Symlink (or otherwise) escaped the project root — refuse.
                _json_response(self, 403, {"error": "forbidden"})
                return
            _file_response(self, resolved, body=body)
            return

        if path == "/agents":
            if cloud_accounts.enabled() and _require_account(self) is None:
                return
            _json_response(self, 200, {"agents": SubAgentRegistry.list_agents()})
            return

        if path == "/skill-cloud/artifacts":
            if not cloud_accounts.enabled():
                _json_response(
                    self,
                    503,
                    {"error": "Skill Cloud is not enabled on this Lumeri runtime"},
                )
                return
            if _require_account(self) is None:
                return
            try:
                artifacts = cloud_accounts.client().list_skill_artifacts()
                _json_response(self, 200, {"artifacts": artifacts})
            except cloud_accounts.CloudAuthError as exc:
                _json_response(
                    self,
                    exc.status,
                    {"error": exc.code, "user_message": str(exc)},
                )
            return

        if path == "/skills":
            if cloud_accounts.enabled() and _require_account(self) is None:
                return
            # Load from skills_v2/ (preferred) with name+description from JSON
            skills_v2 = []
            for p in sorted(_SKILLS_V2_DIR.glob("*.json")):
                try:
                    data = json.loads(p.read_text())
                    skills_v2.append({
                        "id": p.stem,
                        "name": data.get("name", p.stem),
                        "description": data.get("description", ""),
                        "file": str(p),
                    })
                except Exception:
                    pass
            # Fallback: legacy skills/ dir
            legacy_skills = [
                {"id": p.stem, "name": p.stem, "description": "", "file": str(p)}
                for p in sorted(_SKILLS_DIR.glob("*.json"))
            ] if _SKILLS_DIR.exists() else []
            all_skills = skills_v2 + legacy_skills
            inputs = sorted(
                {
                    p.name: str(p.resolve())
                    for p in _INPUTS_DIR.glob("**/*")
                    if p.is_file()
                }.items()
            )
            _json_response(self, 200, {
                "skills": all_skills,
                "inputs": [
                    {"name": name, "path": abs_path}
                    for name, abs_path in inputs
                ]
            })
            return

        _json_response(self, 404, {"error": "not found"})

    def do_GET(self) -> None:  # noqa: N802
        if self._security_gate(mutating=False):
            return
        self._handle_get_like(body=True)

    def do_HEAD(self) -> None:  # noqa: N802
        if self._security_gate(mutating=False):
            return
        self._handle_get_like(body=False)

    def do_DELETE(self) -> None:  # noqa: N802
        if self._security_gate(mutating=True):
            return
        parsed_url = urlparse(self.path)
        route = unquote(parsed_url.path).rstrip("/")
        if route == "/api/v1" or route.startswith("/api/v1/"):
            from gemia.api_v1_routes import legacy_path

            previous_path = self.path
            self.path = legacy_path(route) + (
                f"?{parsed_url.query}" if parsed_url.query else ""
            )
            try:
                self.do_DELETE()
            finally:
                self.path = previous_path
            return
        if route.startswith("/sessions/") or route.startswith("/projects/"):
            if cloud_accounts.enabled() and _require_account(self) is None:
                return
            from gemia.v3_routes import try_handle as _v3_try

            if _v3_try(self, method="DELETE"):
                return
        if route.startswith("/media-library/") and "/annotations/" in route:
            try:
                from gemia.media_annotations import MediaAnnotationError, delete_annotation

                account_id = _require_workspace_account(self)
                if account_id is None:
                    return
                parts = route.split("/")
                asset_id = parts[2] if len(parts) >= 5 else ""
                annotation_id = parts[4] if len(parts) >= 5 else ""
                _json_response(
                    self,
                    200,
                    {"annotation": delete_annotation(
                        account_id,
                        asset_id,
                        annotation_id,
                        project_id=_active_media_project_id(account_id),
                    )},
                )
            except MediaAnnotationError as exc:
                _json_response(self, 404, {"error": str(exc)})
            except Exception as exc:
                _json_response(self, 500, {"error": str(exc)})
            return
        if route.startswith("/media-library/"):
            try:
                from gemia.media_library import MediaLibraryError, soft_delete_asset

                account_id = _require_workspace_account(self)
                if account_id is None:
                    return
                parts = route.split("/")
                asset_id = parts[2] if len(parts) >= 3 else ""
                _json_response(
                    self,
                    200,
                    {"asset": soft_delete_asset(
                        account_id,
                        asset_id,
                        project_id=_active_media_project_id(account_id),
                    )},
                )
            except MediaLibraryError as exc:
                _json_response(self, 404, {"error": str(exc)})
            except Exception as exc:
                _json_response(self, 500, {"error": str(exc)})
            return
        _json_response(self, 404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if self._security_gate(mutating=True):
            return
        parsed_url = urlparse(self.path)
        route = unquote(parsed_url.path).rstrip("/")
        if route == "/api/internal/v1" or route.startswith("/api/internal/v1/"):
            if cloud_accounts.enabled() and _require_account(self) is None:
                return
            from gemia.api_v1_routes import try_handle as _api_v1_try

            if _api_v1_try(self, method="POST"):
                return
        if route == "/api/v1" or route.startswith("/api/v1/"):
            from gemia.api_v1_routes import legacy_path

            previous_path = self.path
            self.path = legacy_path(route) + (
                f"?{parsed_url.query}" if parsed_url.query else ""
            )
            try:
                self.do_POST()
            finally:
                self.path = previous_path
            return
        # Client-owned workspaces (the personal iPad build) use this as a
        # one-shot subscription call.  It must stay above the session router:
        # no SessionManager/ProjectStore is created or written here.
        if route == "/local-chat":
            if cloud_accounts.enabled():
                if _require_account(self) is None:
                    return
                _json_response(
                    self,
                    403,
                    {"error": "online managed creation is not available yet"},
                )
                return
            try:
                from gemia.stateless_chat import StatelessChatError, complete

                _json_response(self, 200, complete(_read_json_body(self)))
            except StatelessChatError as exc:
                _json_response(self, 400, {"error": str(exc)})
            return
        # Lumeri v3 protocol surface (live sessions + durable projects/runs).
        if (
            route == "/sessions"
            or route.startswith("/sessions/")
            or route == "/projects"
            or route.startswith("/projects/")
        ):
            if cloud_accounts.enabled() and _require_account(self) is None:
                return
            from gemia.v3_routes import try_handle as _v3_try
            if _v3_try(self, method="POST"):
                return
        if route == "/auth/google/start":
            if cloud_accounts.enabled():
                _json_response(self, 409, {"error": "use cloud device login"})
                return
            try:
                _json_response(self, 200, accounts.start_google_oauth())
            except Exception as exc:
                _json_response(self, 400, _error_payload(exc))
            return

        if route == "/auth/email/start":
            if cloud_accounts.enabled():
                _json_response(self, 409, {"error": "use cloud device login"})
                return
            try:
                payload = _read_json_body(self)
                _json_response(self, 200, accounts.start_email_login(payload.get("email", "")))
            except Exception as exc:
                _json_response(self, 400, _error_payload(exc))
            return

        if route == "/auth/email/verify":
            if cloud_accounts.enabled():
                _json_response(self, 409, {"error": "use cloud device login"})
                return
            try:
                payload = _read_json_body(self)
                profile = accounts.verify_email_login(payload.get("email", ""), payload.get("code", ""))
                _json_response(self, 200, {"ok": True, "account": profile, **accounts.auth_session_payload()})
            except Exception as exc:
                _json_response(self, 400, _error_payload(exc))
            return

        if route == "/auth/device/start":
            if not cloud_accounts.enabled():
                _json_response(self, 404, {"error": "cloud login is disabled"})
                return
            try:
                import platform as _platform

                device_name = _platform.node().strip() or f"Lumeri {_platform.system()}"
                payload = cloud_accounts.client().start_device_login(
                    device_name=device_name,
                    platform_name=_platform.system() or "Desktop",
                )
                _json_response(self, 200, payload)
            except cloud_accounts.CloudAuthError as exc:
                _json_response(self, exc.status, {"error": exc.code, "user_message": str(exc)})
            return

        if route == "/auth/device/token":
            if not cloud_accounts.enabled():
                _json_response(self, 404, {"error": "cloud login is disabled"})
                return
            previous_cloud_id = ""
            try:
                payload = _read_json_body(self)
                cloud_client = cloud_accounts.client()
                result = cloud_client.poll_device_login(
                    str(payload.get("attempt_id") or ""),
                    sync_credential=False,
                )
                if result.get("pending"):
                    _json_response(self, 202, result)
                    return
                previous_cloud_id = _validated_cloud_account_id()
                _clear_cloud_account_validation()
                next_cloud_id = str(result["account"].get("id") or "").strip()
                if previous_cloud_id and previous_cloud_id != next_cloud_id:
                    _close_live_cloud_sessions()
                sync_credential = getattr(cloud_client, "sync_selected_credential", None)
                if callable(sync_credential):
                    sync_credential(result["account"])
                profile = accounts.activate_cloud_account(result["account"])
                _mark_cloud_account_validated(result["account"])
                _json_response(self, 200, {"ok": True, "account": profile, **_auth_session_payload()})
            except cloud_accounts.CloudAuthError as exc:
                _clear_cloud_account_validation()
                if previous_cloud_id:
                    _close_live_cloud_sessions()
                accounts.sign_out()
                _json_response(self, exc.status, {"error": exc.code, "user_message": str(exc)})
            return

        if route == "/model":
            if cloud_accounts.enabled() and _require_account(self) is None:
                return
            try:
                from gemia.memory import apply_model_selection, model_selection_payload

                payload = _read_json_body(self) or {}
                apply_model_selection(payload, "planner")
                _json_response(self, 200, {"ok": True, **model_selection_payload("planner")})
            except ValueError as exc:
                _json_response(self, 400, {"error": str(exc)})
            except Exception as exc:
                _json_response(self, 500, _error_payload(exc))
            return

        if route == "/model/add":
            if cloud_accounts.enabled() and _require_account(self) is None:
                return
            try:
                from gemia import brain_config
                from gemia.memory import add_model_to_catalog, model_selection_payload

                payload = _read_json_body(self) or {}
                model_id = payload.get("id", "").strip()
                if not model_id:
                    _json_response(self, 400, {"error": "missing model id"})
                    return
                add_model_to_catalog(
                    model_id,
                    label=payload.get("label", ""),
                    provider=payload.get("provider", "")
                    or brain_config.read_status(
                        json.loads(_CONFIG_PATH.read_text()) if _CONFIG_PATH.exists() else {}
                    ).get("provider", ""),
                    slot="planner",
                )
                _json_response(self, 200, {"ok": True, **model_selection_payload("planner")})
            except ValueError as exc:
                _json_response(self, 400, {"error": str(exc)})
            except Exception as exc:
                _json_response(self, 500, _error_payload(exc))
            return

        if route == "/model/remove":
            if cloud_accounts.enabled() and _require_account(self) is None:
                return
            try:
                from gemia.memory import model_selection_payload, remove_model_from_catalog

                payload = _read_json_body(self) or {}
                model_id = payload.get("id", "").strip()
                if not model_id:
                    _json_response(self, 400, {"error": "missing model id"})
                    return
                remove_model_from_catalog(model_id, slot="planner")
                _json_response(self, 200, {"ok": True, **model_selection_payload("planner")})
            except ValueError as exc:
                _json_response(self, 400, {"error": str(exc)})
            except Exception as exc:
                _json_response(self, 500, _error_payload(exc))
            return

        if route == "/auth/logout":
            if _validated_cloud_account_id():
                _close_live_cloud_sessions()
            _clear_cloud_account_validation()
            if cloud_accounts.enabled():
                try:
                    cloud_accounts.client().logout()
                except cloud_accounts.CloudAuthError:
                    pass
            accounts.sign_out()
            _json_response(self, 200, {"ok": True, **_auth_session_payload()})
            return

        if route.startswith("/runtime/"):
            if cloud_accounts.enabled() and _require_account(self) is None:
                return
            if not _vnext_enabled():
                _json_response(self, 404, {"error": "vNext runtime is disabled"})
                return
            try:
                post_query = parse_qs(urlparse(self.path).query)
                payload = _read_json_body(self)
                if route == "/runtime/dev/workspace":
                    service = _creative_sandbox_service()
                    _json_response(self, 200, service.create_workspace(payload, account_id=identity.resolve_account_id(self)))
                    return
                if route.startswith("/runtime/dev/workspace/"):
                    parts = route.split("/")
                    service = _creative_sandbox_service()
                    if len(parts) >= 6 and parts[1:4] == ["runtime", "dev", "workspace"]:
                        session_id = unquote(parts[4]).strip()
                        action = parts[5]
                        if action == "files":
                            _json_response(self, 200, service.write_file(session_id, payload))
                            return
                        if action == "command-events":
                            _json_response(self, 200, service.record_command_event(session_id, payload))
                            return
                        if action == "run":
                            command_id = f"cmd_{uuid.uuid4().hex[:12]}"
                            args = payload.get("args") or payload.get("command")
                            if isinstance(args, str):
                                args = [args]
                            if not isinstance(args, list) or not all(isinstance(item, str) for item in args):
                                _json_response(
                                    self,
                                    400,
                                    {
                                        "status": "failed",
                                        "error": {
                                            "code": "invalid_command_args",
                                            "message": "args must be an argv array of strings",
                                        },
                                    },
                                )
                                return
                            service.append_event(
                                session_id,
                                "dev_command_started",
                                {
                                    "command_id": command_id,
                                    "command": " ".join(args)[:240],
                                    "label": str(payload.get("label") or ""),
                                    "executed": True,
                                },
                            )
                            from gemia.creative_sandbox_runner import CreativeSandboxRunner

                            runner = CreativeSandboxRunner(_BASE_DIR, session_id=session_id)
                            result = runner.run(
                                args,
                                cwd=payload.get("cwd"),
                                timeout_sec=float(payload.get("timeout_sec") or 30),
                                declared_artifact_paths=payload.get("declared_artifact_paths") or (),
                                command_id=command_id,
                            ).to_dict()
                            service.append_event(
                                session_id,
                                "dev_command_finished",
                                {
                                    "command_id": result.get("command_id"),
                                    "status": result.get("status"),
                                    "exit_code": result.get("exit_code"),
                                    "duration_ms": result.get("duration_ms"),
                                    "stdout_tail": result.get("stdout_tail"),
                                    "stderr_tail": result.get("stderr_tail"),
                                    "artifact_count": len(result.get("artifacts") or []),
                                    "executed": True,
                                },
                            )
                            for artifact in result.get("artifacts") or []:
                                service.append_event(
                                    session_id,
                                    "dev_artifact_ready",
                                    {
                                        "path": artifact.get("rel_path") or artifact.get("path"),
                                        "size": artifact.get("size"),
                                        "declared": artifact.get("declared"),
                                        "command_id": result.get("command_id"),
                                    },
                                )
                            _json_response(
                                self,
                                200,
                                {
                                    "status": "succeeded" if result.get("status") == "succeeded" else result.get("status"),
                                    "session_id": session_id,
                                    "result": result,
                                    "workspace": service.get_workspace(session_id).get("workspace"),
                                    "events": service.read_events(session_id),
                                    "artifacts": service.list_artifacts(session_id).get("artifacts", []),
                                    "preview": _creative_sandbox_preview_payload(service, session_id),
                                    "report": service.report(session_id),
                                },
                            )
                            return
                    _json_response(self, 404, {"error": "creative sandbox route not found"})
                    return
                _json_response(self, 404, {"error": "runtime route not found"})
            except Exception as exc:
                _creative_sandbox_error_response(self, exc)
            return

        if route == "/agent-links/link":
            if _require_account(self) is None:
                return
            try:
                from gemia.agent_links import link_agent

                payload = _read_json_body(self)
                agent_id = str(payload.get("agent_id") or payload.get("agent") or "").strip()
                linked = bool(payload.get("linked", True))
                _json_response(self, 200, link_agent(agent_id, linked=linked))
            except Exception as exc:
                _json_response(self, 400, {"error": str(exc)})
            return

        if route == "/agent-links/message":
            if _require_account(self) is None:
                return
            try:
                from gemia.agent_links import send_message

                payload = _read_json_body(self)
                result = send_message(
                    sender=str(payload.get("sender") or "lumeri"),
                    target=str(payload.get("target") or ""),
                    message=str(payload.get("message") or ""),
                    invoke=bool(payload.get("invoke", False)),
                    cwd=_BASE_DIR,
                    timeout_seconds=int(payload.get("timeout_seconds") or 180),
                )
                _json_response(self, 200, result)
            except Exception as exc:
                _json_response(self, 400, {"error": str(exc)})
            return

        if route == "/agent-links/relay":
            if _require_account(self) is None:
                return
            try:
                from gemia.agent_links import relay_round

                payload = _read_json_body(self)
                result = relay_round(
                    message=str(payload.get("message") or ""),
                    first=str(payload.get("first") or "codex-lumeri"),
                    second=str(payload.get("second") or "gemini-lumeri"),
                    cwd=_BASE_DIR,
                    timeout_seconds=int(payload.get("timeout_seconds") or 180),
                )
                _json_response(self, 200, result)
            except Exception as exc:
                _json_response(self, 400, {"error": str(exc)})
            return

        if route in ("/upload-video", "/upload-media"):
            from gemia.media_library import MediaLibraryError, import_media, upload_response_for_asset
            from gemia.video.timeline_assets import SUPPORTED_MEDIA_EXTENSIONS

            account_id = _require_workspace_account(self)
            if account_id is None:
                return
            filename = (self.headers.get("X-Filename") or "upload").strip()
            safe_name = Path(filename).name.strip() or "upload"
            ext = Path(safe_name).suffix.lower()
            if not ext:
                content_type = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip()
                guessed = mimetypes.guess_extension(content_type) or ""
                if guessed == ".jpe":
                    guessed = ".jpg"
                if guessed in SUPPORTED_MEDIA_EXTENSIONS:
                    ext = guessed
                    safe_name = f"{safe_name}{ext}"
            if ext not in SUPPORTED_MEDIA_EXTENSIONS:
                allowed = ", ".join(sorted(SUPPORTED_MEDIA_EXTENSIONS))
                _json_response(self, 400, {"error": f"unsupported media type: {ext or 'unknown'}", "allowed_extensions": allowed})
                return
            length = int(self.headers.get("Content-Length", 0))
            if length <= 0:
                _json_response(self, 400, {"error": "empty upload"})
                return
            incoming_dir = _INPUTS_DIR / ".incoming"
            incoming_dir.mkdir(parents=True, exist_ok=True)
            dest = incoming_dir / f"{uuid.uuid4().hex}{ext}"
            with dest.open("wb") as f:
                remaining = length
                while remaining > 0:
                    chunk = self.rfile.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    f.write(chunk)
                    remaining -= len(chunk)
            try:
                actual = dest.stat().st_size
                if actual != length:
                    # Reject truncated uploads instead of silently importing a
                    # half-written file that the user thinks is intact.
                    _json_response(self, 400, {
                        "error": "incomplete upload",
                        "expected_bytes": length,
                        "received_bytes": actual,
                    })
                    return
                asset = import_media(
                    account_id,
                    dest,
                    original_name=safe_name,
                    project_id=_active_media_project_id(account_id),
                )
                _json_response(self, 200, upload_response_for_asset(asset))
            except MediaLibraryError as exc:
                _json_response(self, 400, {"error": str(exc)})
            except Exception as exc:
                _json_response(self, 500, {"error": str(exc)})
            finally:
                try:
                    dest.unlink()
                except OSError:
                    pass
            return

        if route == "/config":
            # Save API keys to ~/.gemia/config.json and reload into env. The
            # first-run UI lets a logged-out user paste a key, so we only
            # require auth once any account has been provisioned locally.
            if not _require_provider_access(self):
                return
            try:
                length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(length) if length else b"{}"
                body = json.loads(raw)
                if not isinstance(body, dict):
                    _json_response(self, 400, {"error": "request body must be an object"})
                    return
                if "fast_mode" in body and not isinstance(body.get("fast_mode"), bool):
                    _json_response(self, 400, {"error": "fast_mode must be a boolean"})
                    return
                if cloud_accounts.enabled():
                    provider_requested = bool(str(body.get("provider") or "").strip())
                    provider_body = dict(body)
                    provider_body.pop("fast_mode", None)
                    prepared, cloud_provider = _prepare_cloud_config_body(provider_body)
                    if prepared is None:
                        _json_response(self, 403, {"error": cloud_provider})
                        return
                    if not _require_provider_access(
                        self, requested_provider=prepared.get("provider")
                    ):
                        return
                    selected = _normalized_byok_provider(prepared.get("provider"))
                    updated_account: dict[str, object] | None = None
                    if provider_requested:
                        updated_account = cloud_accounts.client().select_provider(
                            cloud_provider
                        )
                        accounts.activate_cloud_account(updated_account)
                    expected_key_field = {
                        "openrouter": "openrouter_api_key",
                        "gemini": "gemini_api_key",
                        "claude": "anthropic_api_key",
                        "openai": "openai_api_key",
                        "custom": "openai_api_key",
                    }.get(selected, "")
                    secret = str(body.get(expected_key_field) or "").strip() if expected_key_field else ""
                    if secret:
                        cloud_accounts.client().put_selected_credential(
                            cloud_provider, secret
                        )
                    search_provider = str(prepared.get("search_provider") or "").strip().lower()
                    search_secret_field = _CLOUD_SEARCH_PROVIDER_SECRET_FIELD.get(
                        search_provider, ""
                    )
                    search_secret = (
                        str(body.get(search_secret_field) or "").strip()
                        if search_secret_field
                        else ""
                    )
                    if search_secret:
                        cloud_accounts.client().put_auxiliary_credential(
                            search_provider, search_secret
                        )
                    elif search_secret_field:
                        sync_auxiliary = getattr(
                            cloud_accounts.client(),
                            "sync_auxiliary_credential",
                            None,
                        )
                        if callable(sync_auxiliary):
                            sync_auxiliary(search_provider)
                    cfg_dir = _CONFIG_PATH.parent
                    cfg_dir.mkdir(parents=True, exist_ok=True)
                    existing: dict[str, object] = {}
                    if _CONFIG_PATH.exists():
                        try:
                            loaded = json.loads(_CONFIG_PATH.read_text())
                            existing = loaded if isinstance(loaded, dict) else {}
                        except Exception:
                            existing = {}
                    existing = cloud_accounts.strip_model_credentials(existing)
                    for field in (
                        *_CLOUD_AUXILIARY_SECRET_FIELDS,
                        *_CLOUD_SEARCH_SECRET_FIELDS,
                    ):
                        existing.pop(field, None)
                    if str(existing.get("search_provider") or "").lower() not in _CLOUD_SEARCH_PROVIDERS:
                        existing["search_provider"] = "auto"
                    safe_body = dict(prepared)
                    for field in _MODEL_KEY_PROVIDER:
                        safe_body.pop(field, None)
                    for field in _CLOUD_SEARCH_SECRET_FIELDS:
                        safe_body.pop(field, None)
                    # Search selection and non-secret connection metadata stay
                    # local; account-bound credentials never touch this file.
                    for field in ("search_provider", "searxng_url", "google_cse_id"):
                        if field in safe_body:
                            value = str(safe_body.get(field) or "").strip()
                            if value:
                                existing[field] = value
                            else:
                                existing.pop(field, None)
                    from gemia import brain_config
                    from gemia import local_config

                    brain_config.apply_update(existing, safe_body, sync_env=False)
                    existing = cloud_accounts.bind_model_metadata(existing)
                    public_body = dict(safe_body)
                    if "fast_mode" in body:
                        public_body["fast_mode"] = body["fast_mode"]
                    local_config.write_public_update(
                        existing, public_body, path=_public_config_path()
                    )
                    _CONFIG_PATH.write_text(
                        json.dumps(existing, indent=2, ensure_ascii=False)
                    )
                    _json_response(
                        self,
                        200,
                        {
                            "ok": True,
                            "selected_provider": selected,
                            "provider_mode": (
                                str((updated_account or {}).get("provider_mode") or "")
                                or str((accounts.current_account() or {}).get("provider_mode") or "")
                            ),
                        },
                    )
                    return
                requested_provider = body.get("provider")
                if not requested_provider and body.get("openrouter_api_key"):
                    requested_provider = "openrouter"
                if not requested_provider and body.get("gemini_api_key"):
                    requested_provider = "gemini"
                if not _require_provider_access(self, requested_provider=requested_provider):
                    return
                cfg_dir = _CONFIG_PATH.parent
                cfg_dir.mkdir(parents=True, exist_ok=True)
                existing = {}
                if _CONFIG_PATH.exists():
                    try:
                        existing = json.loads(_CONFIG_PATH.read_text())
                    except Exception:
                        pass
                if not body.get("provider") and (
                    key := body.get("openrouter_api_key", "").strip()
                ):
                    existing["openrouter_api_key"] = key
                    os.environ["OPENROUTER_API_KEY"] = key
                if not body.get("provider") and (
                    key := body.get("gemini_api_key", "").strip()
                ):
                    existing["gemini_api_key"] = key
                    os.environ["GEMINI_API_KEY"] = key
                if key := body.get("image_api_key", "").strip():
                    existing["image_api_key"] = key
                    os.environ["GEMIA_IMAGE_API_KEY"] = key
                if key := body.get("nano_banana_api_key", "").strip():
                    existing["nano_banana_api_key"] = key
                    os.environ["GEMIA_IMAGE_API_KEY"] = key
                if value := body.get("image_base_url", "").strip():
                    existing["image_base_url"] = value
                    os.environ["GEMIA_IMAGE_BASE_URL"] = value
                if value := body.get("openrouter_image_url", "").strip():
                    existing["openrouter_image_url"] = value
                    os.environ["OPENROUTER_IMAGE_URL"] = value
                if value := body.get("image_model", "").strip():
                    if _legacy_image_model(value):
                        existing.pop("image_model", None)
                        os.environ.pop("GEMIA_IMAGE_MODEL", None)
                    else:
                        strongest = strongest_media_model("image", "openrouter", (value,))
                        existing["image_model"] = strongest
                        os.environ["GEMIA_IMAGE_MODEL"] = strongest
                # 搜索引擎字段（白名单合并）。
                _SEARCH_CONFIG_KEYS = (
                    "search_provider", "tavily_api_key", "brave_api_key",
                    "serper_api_key", "exa_api_key", "bing_api_key",
                    "google_cse_key", "google_cse_id",
                    "searxng_url", "searxng_api_key",
                )
                for sk in _SEARCH_CONFIG_KEYS:
                    if sk in body:
                        v = str(body[sk]).strip() if body[sk] else ""
                        if v:
                            existing[sk] = v
                        else:
                            existing.pop(sk, None)
                # 大脑 provider 字段（白名单合并 + 即时设 env）。
                try:
                    from gemia import brain_config
                    brain_config.apply_update(existing, body)
                except Exception:
                    pass
                from gemia import local_config

                local_config.write_public_update(
                    existing, body, path=_public_config_path()
                )
                _CONFIG_PATH.write_text(json.dumps(existing, indent=2, ensure_ascii=False))
                _json_response(self, 200, {"ok": True})
            except cloud_accounts.CloudAuthError as exc:
                _json_response(self, exc.status, {"error": exc.code, "user_message": str(exc)})
            except Exception as exc:
                _json_response(self, 500, {"error": str(exc)})
            return

        if route == "/config/list-models":
            if not _require_provider_access(self):
                return
            try:
                from gemia import brain_config
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                if cloud_accounts.enabled():
                    prepared, error = _prepare_cloud_config_body(body)
                    if prepared is None:
                        _json_response(self, 403, {"error": error})
                        return
                    if any(field in body for field in _MODEL_KEY_PROVIDER):
                        _json_response(self, 403, {"error": "save the selected credential before listing models"})
                        return
                    body = prepared
                if not _require_provider_access(self, requested_provider=body.get("provider")):
                    return
                cfg = {}
                if _CONFIG_PATH.exists():
                    cfg = json.loads(_CONFIG_PATH.read_text())
                if cloud_accounts.enabled():
                    cfg = cloud_accounts.runtime_model_config(cfg)
                else:
                    from gemia import local_config

                    cfg = local_config.merge_with_secret_config(
                        cfg, path=_public_config_path()
                    )
                if body:
                    brain_config.apply_update(cfg, body, sync_env=False)
                proxy = "" if cloud_accounts.enabled() else os.environ.get("HTTPS_PROXY") or ""
                if not proxy and not cloud_accounts.enabled():
                    try:
                        proxy = json.loads(_CONFIG_PATH.read_text()).get("proxy") or ""
                    except Exception:
                        proxy = ""
                pv = (
                    body.get("provider")
                    or brain_config.read_status(cfg).get("provider")
                    or "openai"
                )
                result = brain_config.list_models(pv, cfg, proxy=proxy or None)
                _json_response(self, 200, result)
            except Exception as exc:
                _json_response(self, 500, {"ok": False, "error": str(exc), "models": []})
            return

        if route == "/config/codex-login":
            if not _require_provider_access(self, requested_provider="openai_subscription"):
                return
            from gemia import brain_config

            status, payload = brain_config.codex_login_bridge("POST")
            _json_response(self, status, payload)
            return

        if route == "/config/test-brain":
            # 用当前配置发极小探针，验证 provider 连通与鉴权（Setup 面板的"测试连接"）。
            if not _require_provider_access(self):
                return
            try:
                from gemia import brain_config
                length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(length) if length else b"{}"
                body = json.loads(raw) if raw.strip() else {}
                if cloud_accounts.enabled():
                    prepared, error = _prepare_cloud_config_body(body)
                    if prepared is None:
                        _json_response(self, 403, {"error": error})
                        return
                    if any(field in body for field in _MODEL_KEY_PROVIDER):
                        _json_response(self, 403, {"error": "save the selected credential before testing"})
                        return
                    body = prepared
                if not _require_provider_access(self, requested_provider=body.get("provider")):
                    return
                cfg = {}
                if _CONFIG_PATH.exists():
                    try:
                        cfg = json.loads(_CONFIG_PATH.read_text())
                    except Exception:
                        cfg = {}
                if cloud_accounts.enabled():
                    cfg = cloud_accounts.runtime_model_config(cfg)
                else:
                    from gemia import local_config

                    cfg = local_config.merge_with_secret_config(
                        cfg, path=_public_config_path()
                    )
                # 未保存字段只合并到临时快照，不污染运行进程的 provider/model。
                if body:
                    brain_config.apply_update(cfg, body, sync_env=False)
                proxy = "" if cloud_accounts.enabled() else os.environ.get("HTTPS_PROXY") or ""
                if not proxy and not cloud_accounts.enabled() and _CONFIG_PATH.exists():
                    try:
                        proxy = json.loads(_CONFIG_PATH.read_text()).get("proxy") or ""
                    except Exception:
                        proxy = ""
                test_kwargs = {"proxy": proxy or None, "config": cfg}
                if _normalized_byok_provider(body.get("provider")) == "openai_subscription":
                    test_kwargs["provider_override"] = "openai_subscription"
                result = brain_config.test_provider(**test_kwargs)
                _json_response(self, 200, result)
            except Exception as exc:
                _json_response(self, 500, {"ok": False, "error": str(exc)})
            return

        if route == "/dev-feedback":
            if _require_account(self) is None:
                return
            try:
                length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(length) if length else b"{}"
                body = json.loads(raw)
                feedback = str(body.get("feedback", "")).strip()
                if not feedback:
                    _json_response(self, 400, {"error": "feedback is empty"})
                    return
                ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S %z")
                dev_file = Path(__file__).parent / "dev_feedback.txt"
                with dev_file.open("a", encoding="utf-8") as f:
                    f.write(f"[PENDING] {ts}\n{feedback}\n---\n")
                _json_response(self, 200, {"ok": True})
            except Exception as exc:
                _json_response(self, 500, {"error": str(exc)})
            return

        if route == "/session-history":
            try:
                from gemia.session_history import save_current_session

                payload = _read_json_body(self)
                if cloud_accounts.enabled():
                    account_id = _require_account(self)
                    if account_id is None:
                        return
                else:
                    try:
                        account_id = identity.resolve_account_id(self)
                    except Exception:
                        account_id = None
                _json_response(self, 200, save_current_session(payload, account_id=account_id))
            except Exception as exc:
                _json_response(self, 500, {"error": str(exc)})
            return

        if route == "/project/normalize":
            account_id = _require_account(self)
            if account_id is None:
                return
            try:
                from gemia.project_model import normalize_project

                payload = _read_json_body(self)
                project = normalize_project(
                    payload.get("project") if isinstance(payload.get("project"), dict) else None,
                    project_state=payload.get("project_state") if isinstance(payload.get("project_state"), dict) else None,
                    account_id=account_id,
                )
                _json_response(self, 200, {"project": project})
            except Exception as exc:
                _json_response(self, 500, {"error": str(exc)})
            return

        if route == "/media-library/copy":
            try:
                from gemia.media_library import MediaLibraryError, copy_asset_to_project, upload_response_for_asset

                payload = _read_json_body(self)
                account_id = _require_workspace_account(self)
                if account_id is None:
                    return
                source_project_id = str(payload.get("source_project_id") or "").strip()
                asset_id = str(payload.get("asset_id") or "").strip()
                if not source_project_id or not asset_id:
                    _json_response(self, 400, {"error": "source_project_id and asset_id are required"})
                    return
                asset = copy_asset_to_project(
                    account_id,
                    asset_id,
                    source_project_id=source_project_id,
                    target_project_id=_active_media_project_id(account_id),
                )
                _json_response(self, 200, upload_response_for_asset(asset))
            except MediaLibraryError as exc:
                _json_response(self, 404, {"error": str(exc)})
            except Exception as exc:
                _json_response(self, 500, {"error": str(exc)})
            return

        if route == "/media-library/import":
            try:
                from gemia.media_library import MediaLibraryError, import_media, upload_response_for_asset

                payload = _read_json_body(self)
                account_id = _require_workspace_account(self)
                if account_id is None:
                    return
                source_path = str(payload.get("path") or payload.get("source_path") or "")
                original_name = str(payload.get("name") or "") or None
                asset = import_media(
                    account_id,
                    source_path,
                    original_name=original_name,
                    project_id=_active_media_project_id(account_id),
                )
                _json_response(self, 200, upload_response_for_asset(asset))
            except MediaLibraryError as exc:
                _json_response(self, 400, {"error": str(exc)})
            except Exception as exc:
                _json_response(self, 500, {"error": str(exc)})
            return

        if route == "/media-library/prepare":
            try:
                from gemia.roughcut import RoughcutError, prepare_roughcut, start_prepare_job

                payload = _read_json_body(self)
                account_id = _require_workspace_account(self)
                if account_id is None:
                    return
                if bool(payload.get("background", True)):
                    _json_response(
                        self,
                        202,
                        start_prepare_job(
                            account_id,
                            payload,
                            project_id=_active_media_project_id(account_id),
                        ),
                    )
                else:
                    asset_ids = payload.get("asset_ids") or payload.get("assets") or []
                    if isinstance(asset_ids, str):
                        asset_ids = [asset_ids]
                    result = prepare_roughcut(
                        account_id,
                        [str(item) for item in asset_ids],
                        all_assets=bool(payload.get("all") or payload.get("all_assets")),
                        language=str(payload.get("language") or "auto"),
                        create_proxies=bool(payload.get("create_proxies", True)),
                        proxy_resolution=int(payload.get("proxy_resolution") or 540),
                        resume=bool(payload.get("resume", True)),
                        max_assets=int(payload.get("max_assets") or 100),
                        project_id=_active_media_project_id(account_id),
                    )
                    _json_response(self, 200, result)
            except RoughcutError as exc:
                _json_response(self, 400, {"error": str(exc)})
            except Exception as exc:
                _json_response(self, 500, {"error": str(exc)})
            return

        if route.startswith("/media-library/prepare/") and route.endswith("/resume"):
            try:
                from gemia.roughcut import RoughcutError, resume_prepare_job

                account_id = _require_workspace_account(self)
                if account_id is None:
                    return
                job_id = route.removeprefix("/media-library/prepare/").removesuffix("/resume").strip("/")
                _json_response(
                    self,
                    202,
                    resume_prepare_job(
                        account_id,
                        job_id,
                        project_id=_active_media_project_id(account_id),
                    ),
                )
            except RoughcutError as exc:
                _json_response(self, 400, {"error": str(exc)})
            return

        if route.startswith("/media-library/") and route.endswith("/roughcut/review"):
            try:
                from gemia.roughcut import RoughcutError, apply_roughcut_review

                payload = _read_json_body(self)
                account_id = _require_workspace_account(self)
                if account_id is None:
                    return
                parts = route.split("/")
                asset_id = parts[2] if len(parts) >= 5 else ""
                _json_response(
                    self,
                    200,
                    apply_roughcut_review(
                        account_id,
                        asset_id,
                        payload,
                        project_id=_active_media_project_id(account_id),
                    ),
                )
            except RoughcutError as exc:
                _json_response(self, 400, {"error": str(exc)})
            return

        if route == "/media-library/annotate":
            try:
                from gemia.media_annotations import MediaAnnotationError, annotate_asset_heuristic
                from gemia.media_library import list_assets

                payload = _read_json_body(self)
                account_id = _require_workspace_account(self)
                if account_id is None:
                    return
                asset_ids = payload.get("asset_ids") or payload.get("assets") or []
                if isinstance(asset_ids, str):
                    asset_ids = [asset_ids]
                if not isinstance(asset_ids, list):
                    _json_response(self, 400, {"error": "asset_ids must be a list"})
                    return
                if not asset_ids and payload.get("all"):
                    asset_ids = [
                        asset.get("asset_id")
                        for asset in list_assets(
                            account_id,
                            kind="video",
                            limit=int(payload.get("max_assets") or 20),
                            project_id=_active_media_project_id(account_id),
                        )
                    ]
                if not asset_ids:
                    _json_response(self, 400, {"error": "asset_ids is required"})
                    return
                max_assets = max(1, min(int(payload.get("max_assets") or len(asset_ids)), 100))
                results = []
                for asset_id in [str(item) for item in asset_ids[:max_assets]]:
                    results.append(
                        annotate_asset_heuristic(
                            account_id,
                            asset_id,
                            mode=str(payload.get("mode") or "quick"),
                            language=str(payload.get("language") or "auto"),
                            tags=payload.get("tags") if isinstance(payload.get("tags"), list) else None,
                            replace_existing=bool(payload.get("replace_existing", True)),
                            project_id=_active_media_project_id(account_id),
                        )
                    )
                _json_response(self, 200, {"results": results, "asset_count": len(results)})
            except MediaAnnotationError as exc:
                _json_response(self, 404, {"error": str(exc)})
            except Exception as exc:
                _json_response(self, 500, {"error": str(exc)})
            return

        if route.startswith("/media-library/") and "/annotations" in route:
            try:
                from gemia.media_annotations import MediaAnnotationError, create_annotation, update_annotation

                payload = _read_json_body(self)
                account_id = _require_workspace_account(self)
                if account_id is None:
                    return
                parts = route.split("/")
                asset_id = parts[2] if len(parts) >= 4 else ""
                if len(parts) >= 5 and parts[3] == "annotations" and parts[4]:
                    _json_response(
                        self,
                        200,
                        {"annotation": update_annotation(
                            account_id,
                            asset_id,
                            parts[4],
                            payload,
                            project_id=_active_media_project_id(account_id),
                        )},
                    )
                    return
                if len(parts) >= 4 and parts[3] == "annotations":
                    _json_response(
                        self,
                        200,
                        {"annotation": create_annotation(
                            account_id,
                            asset_id,
                            payload,
                            project_id=_active_media_project_id(account_id),
                        )},
                    )
                    return
                _json_response(self, 404, {"error": "not found"})
            except MediaAnnotationError as exc:
                _json_response(self, 404, {"error": str(exc)})
            except Exception as exc:
                _json_response(self, 500, {"error": str(exc)})
            return

        if route.startswith("/media-library/") and route.endswith("/add-to-project"):
            try:
                from gemia.media_library import default_clip_for_asset, get_asset

                account_id = _require_workspace_account(self)
                if account_id is None:
                    return
                parts = route.split("/")
                asset_id = parts[2] if len(parts) >= 4 else ""
                asset = get_asset(account_id, asset_id, project_id=_active_media_project_id(account_id))
                if not asset:
                    _json_response(self, 404, {"error": "media asset not found"})
                    return
                _json_response(self, 200, {"asset": asset, "clip": default_clip_for_asset(asset)})
            except Exception as exc:
                _json_response(self, 500, {"error": str(exc)})
            return

        if route in ("/video-summary", "/video-summary/batch"):
            account_id = _require_account(self)
            if account_id is None:
                return
            try:
                from gemia.video.summary import batch_summarize, video_summarize

                payload = _read_json_body(self)
                if route == "/video-summary":
                    video_path = str(payload.get("video_path") or payload.get("video") or "").strip()
                    if not video_path:
                        _json_response(self, 400, {"error": "video_path is required"})
                        return
                    if not _video_path_allowed(account_id, video_path):
                        _json_response(self, 403, {"error": "video_path is outside this account's media library"})
                        return
                    _json_response(self, 200, video_summarize(video_path))
                    return
                videos = payload.get("videos") or payload.get("video_list") or []
                if not isinstance(videos, list):
                    _json_response(self, 400, {"error": "videos is required"})
                    return
                cleaned = [str(item) for item in videos]
                bad = [v for v in cleaned if not _video_path_allowed(account_id, v)]
                if bad:
                    _json_response(self, 403, {"error": "video_list contains paths outside this account's media library", "rejected": bad})
                    return
                _json_response(self, 200, {"summaries": batch_summarize(cleaned)})
            except json.JSONDecodeError as exc:
                _json_response(self, 400, {"error": f"invalid JSON: {exc}"})
            except Exception as exc:
                _json_response(self, 500, {"error": str(exc)})
            return

        # /dev/claude and /dev/gemini-code were removed: both shelled out to a
        # CLI agent with full filesystem write powers and no auth/origin gate.
        # Use the local `claude` / `codex` CLI directly during development.

        if route == "/settings/sandbox":
            if cloud_accounts.enabled() and _require_account(self) is None:
                return
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) if length else b"{}")
            _set_v4_sandbox_disabled(bool(body.get("disabled", False)))
            _json_response(self, 200, {"sandbox_disabled": _is_v4_sandbox_disabled()})
            return

        if route != "/accounts/switch":
            _json_response(self, 404, {"error": "not found"})
            return

        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            _json_response(self, 400, {"error": f"invalid JSON: {exc}"})
            return

        if route == "/accounts/switch":
            if cloud_accounts.enabled():
                _json_response(self, 409, {"error": "account switching is managed by Lumeri Accounts"})
                return
            account_id = str(payload.get("account_id") or "").strip()
            if not account_id:
                _json_response(self, 400, {"error": "account_id is required"})
                return
            try:
                account = accounts.switch_account(account_id)
                _json_response(self, 200, {"ok": True, "account": account, **accounts.auth_session_payload()})
            except Exception as exc:
                _json_response(self, 400, _error_payload(exc))
            return

        _json_response(self, 404, {"error": "not found"})


def main(
    host: str | None = None,
    port: int | None = None,
    *,
    allow_unconfigured: bool = False,
) -> None:
    # First-run onboarding: if no usable model provider is configured, prompt
    # interactively (TTY) or print instructions and exit cleanly (headless).
    # When a provider is already configured this is a no-op, so existing
    # startup behaviour is unchanged.
    if not cloud_accounts.enabled():
        from gemia.onboarding import ensure_onboarded

        if not ensure_onboarded() and not allow_unconfigured:
            # Headless + unconfigured: instructions already printed. Do NOT bind a
            # brain-less server.
            return
        _load_config_keys()  # Legacy mode keeps its machine-local credentials.
    host = host or _configured_server_host()
    port = int(port or os.environ.get("LUMERI_PORT") or os.environ.get("GEMIA_PORT") or 7788)
    os.environ["GEMIA_HOST"] = host
    os.environ["GEMIA_PORT"] = str(port)
    server = ThreadingHTTPServer((host, port), _Handler)
    server.daemon_threads = True
    print(f"Lumeri server listening on http://{host}:{port}")
    for url in _server_urls(host, port):
        print(f"  available at {url}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Gemia MVP local HTTP server")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args()
    main(args.host, args.port)
