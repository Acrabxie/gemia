"""Public-build local-workspace identity compatibility layer.

The open-source Lumeri runtime is intentionally single-user and local-only.
Hosted registration, OAuth, email delivery, cloud account state, billing, and
subscriptions live outside this repository. This module preserves the small
identity/path interface used by the editing core without exposing or
pretending to provide those private services.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any, NoReturn

from gemia.errors import GemiaError

AUTH_AVAILABLE = False
ACCOUNT_SCHEMA_VERSION = 1
LOCAL_WORKSPACE_ID = "local_public"
ACCOUNTS_ROOT = Path.home() / ".gemia" / "public-workspaces"
ACTIVE_ACCOUNT_PATH = ACCOUNTS_ROOT / ".unused-active.json"
CONFIG_PATH = Path.home() / ".gemia" / "config.json"
_PENDING_OAUTH_STATES: dict[str, dict[str, Any]] = {}


class AuthError(GemiaError, ValueError):
    """Raised when a private account-service operation is requested."""

    code = "E_AUTH_UNAVAILABLE"


def _auth_unavailable() -> NoReturn:
    raise AuthError(
        "Account services are not included in the public build. "
        "The open-source runtime uses one local workspace."
    )


def _local_profile() -> dict[str, Any]:
    return {
        "version": ACCOUNT_SCHEMA_VERSION,
        "account_id": LOCAL_WORKSPACE_ID,
        "provider": "local",
        "email": "",
        "email_verified": False,
        "name": "Local workspace",
        "picture": "",
        "created_at": "",
        "updated_at": "",
        "memory_root": str(account_memory_root(LOCAL_WORKSPACE_ID)),
        "session_root": str(account_session_root(LOCAL_WORKSPACE_ID)),
    }


def current_account_id() -> str:
    """Return the deterministic local workspace identifier."""

    return LOCAL_WORKSPACE_ID


def current_account() -> dict[str, Any]:
    return _local_profile()


def list_accounts() -> list[dict[str, Any]]:
    return [_local_profile()]


def auth_session_payload() -> dict[str, Any]:
    """Describe local mode without claiming that authentication exists."""

    return {
        "account": _local_profile(),
        "accounts": [_local_profile()],
        "auth_available": False,
        "mode": "local",
        "google_client_id": "",
        "has_google_client_id": False,
    }


def sign_out() -> None:
    """Local mode has no sign-in state, so sign-out is a no-op."""


def switch_account(account_id: str) -> dict[str, Any]:
    if account_id != LOCAL_WORKSPACE_ID:
        _auth_unavailable()
    return _local_profile()


def google_client_id() -> str:
    return ""


def google_client_secret() -> str:
    return ""


def save_google_client_id(client_id: str) -> NoReturn:
    _auth_unavailable()


def start_google_oauth() -> NoReturn:
    _auth_unavailable()


def finish_google_oauth(state: str, code: str) -> NoReturn:
    _auth_unavailable()


def sign_in_with_google(credential: str, *, client_id: str | None = None) -> NoReturn:
    _auth_unavailable()


def verify_google_id_token(id_token: str, *, client_id: str | None = None) -> NoReturn:
    _auth_unavailable()


def account_id_for(provider: str, subject: str) -> str:
    """Create a stable non-secret identifier for compatibility consumers."""

    provider_slug = re.sub(r"[^a-zA-Z0-9]+", "_", provider.strip().lower()).strip("_") or "workspace"
    digest = hashlib.sha256(f"{provider}:{subject}".encode("utf-8")).hexdigest()[:24]
    return f"{provider_slug}_{digest}"


def _validate_account_id(account_id: str) -> str:
    value = account_id.strip() if isinstance(account_id, str) else ""
    if not re.fullmatch(r"[a-zA-Z0-9_-]{8,80}", value):
        raise AuthError("Invalid workspace id")
    return value


def account_root(account_id: str) -> Path:
    return ACCOUNTS_ROOT / _validate_account_id(account_id)


def account_profile_path(account_id: str) -> Path:
    return account_root(account_id) / "profile.json"


def account_memory_root(account_id: str) -> Path:
    return account_root(account_id) / "memory"


def account_session_root(account_id: str) -> Path:
    return account_root(account_id) / "sessions"


def load_account(account_id: str | None) -> dict[str, Any] | None:
    return _local_profile() if account_id == LOCAL_WORKSPACE_ID else None


__all__ = [
    "ACCOUNTS_ROOT",
    "ACTIVE_ACCOUNT_PATH",
    "AUTH_AVAILABLE",
    "AuthError",
    "LOCAL_WORKSPACE_ID",
    "account_id_for",
    "account_memory_root",
    "account_root",
    "account_session_root",
    "auth_session_payload",
    "current_account",
    "current_account_id",
    "finish_google_oauth",
    "list_accounts",
    "sign_in_with_google",
    "sign_out",
    "start_google_oauth",
    "switch_account",
    "verify_google_id_token",
]
