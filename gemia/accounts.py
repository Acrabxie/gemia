"""Local Google-backed account storage for Gemia."""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import tempfile
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from gemia.errors import GemiaError

ACCOUNT_SCHEMA_VERSION = 1
ACCOUNTS_ROOT = Path.home() / ".gemia" / "accounts"
ACTIVE_ACCOUNT_PATH = ACCOUNTS_ROOT / "active.json"
MACHINE_WORKSPACE_ID = "local_machine_workspace"
CONFIG_PATH = Path.home() / ".gemia" / "config.json"
GOOGLE_TOKENINFO_URL = "https://oauth2.googleapis.com/tokeninfo"
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_OAUTH_REDIRECT_URI = "http://127.0.0.1:7788/auth/google/callback"
GOOGLE_OAUTH_SCOPE = "openid email profile"
PENDING_OAUTH_TTL_SECONDS = 10 * 60
_PENDING_OAUTH_STATES: dict[str, dict[str, Any]] = {}

# Email one-time-code login (coexists with Google sign-in).
EMAIL_CODE_TTL_SECONDS = 10 * 60
EMAIL_CODE_LENGTH = 6
EMAIL_CODE_MAX_ATTEMPTS = 5
EMAIL_CODE_RESEND_COOLDOWN_SECONDS = 60
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_PENDING_EMAIL_CODES: dict[str, dict[str, Any]] = {}


class AuthError(GemiaError, ValueError):
    """Raised when a local auth operation cannot be completed safely.

    Inherits from both ``GemiaError`` (for unified server-side error
    serialization) and ``ValueError`` (for backward-compatible callers).
    """

    code = "E_AUTH"


def google_client_id() -> str:
    """Return the configured public Google OAuth client id."""
    for name in ("GEMIA_GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_CLIENT_ID"):
        value = _clean_string(os.environ.get(name))
        if value:
            return value
    cfg = _read_json(CONFIG_PATH, {})
    if isinstance(cfg, dict):
        for key in ("google_oauth_client_id", "google_client_id", "oauth_client_id"):
            value = _clean_string(cfg.get(key))
            if value:
                return value
    return ""


def google_client_secret() -> str:
    """Return an optional OAuth client secret without exposing it to public APIs."""
    for name in ("GEMIA_GOOGLE_OAUTH_CLIENT_SECRET", "GOOGLE_OAUTH_CLIENT_SECRET"):
        value = _clean_string(os.environ.get(name))
        if value:
            return value
    cfg = _read_json(CONFIG_PATH, {})
    if isinstance(cfg, dict):
        return _clean_string(cfg.get("google_oauth_client_secret"))
    return ""


def save_google_client_id(client_id: str) -> str:
    """Persist the public Google OAuth client id to local config."""
    value = _clean_string(client_id)
    if not value:
        raise AuthError("Google OAuth Client ID is required")
    cfg = _read_json(CONFIG_PATH, {})
    if not isinstance(cfg, dict):
        cfg = {}
    cfg["google_oauth_client_id"] = value
    _atomic_write_json(CONFIG_PATH, cfg)
    os.environ["GEMIA_GOOGLE_OAUTH_CLIENT_ID"] = value
    return value


def start_google_oauth() -> dict[str, Any]:
    """Create a one-time OAuth URL for browser-based Google sign-in."""
    client_id = google_client_id()
    if not client_id:
        raise AuthError("Google OAuth Client ID is not configured")
    _prune_pending_oauth()
    state = secrets.token_urlsafe(24)
    verifier = _pkce_verifier()
    challenge = _pkce_challenge(verifier)
    expires_at = time.time() + PENDING_OAUTH_TTL_SECONDS
    _PENDING_OAUTH_STATES[state] = {
        "client_id": client_id,
        "code_verifier": verifier,
        "redirect_uri": GOOGLE_OAUTH_REDIRECT_URI,
        "expires_at": expires_at,
    }
    params = {
        "client_id": client_id,
        "redirect_uri": GOOGLE_OAUTH_REDIRECT_URI,
        "response_type": "code",
        "scope": GOOGLE_OAUTH_SCOPE,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "prompt": "select_account",
        "include_granted_scopes": "false",
    }
    return {
        "authorization_url": f"{GOOGLE_AUTH_URL}?{urllib.parse.urlencode(params)}",
        "state": state,
        "redirect_uri": GOOGLE_OAUTH_REDIRECT_URI,
        "expires_at": int(expires_at),
    }


def finish_google_oauth(state: str, code: str) -> dict[str, Any]:
    """Exchange a Google OAuth code, verify its ID token, and activate the account."""
    state_value = _clean_string(state)
    pending = _PENDING_OAUTH_STATES.pop(state_value, None)
    if not pending:
        raise AuthError("Google login session was not found or already used")
    if float(pending.get("expires_at") or 0) <= time.time():
        raise AuthError("Google login session has expired")
    code_value = _clean_string(code)
    if not code_value:
        raise AuthError("Google login code is missing")
    token_payload = _exchange_google_oauth_code(
        code_value,
        client_id=str(pending["client_id"]),
        code_verifier=str(pending["code_verifier"]),
        redirect_uri=str(pending["redirect_uri"]),
    )
    id_token = _clean_string(token_payload.get("id_token"))
    if not id_token:
        raise AuthError("Google did not return an ID token")
    return sign_in_with_google(id_token, client_id=str(pending["client_id"]))


def current_account_id() -> str | None:
    payload = _read_json(ACTIVE_ACCOUNT_PATH, {})
    if not isinstance(payload, dict):
        return None
    account_id = _clean_string(payload.get("account_id"))
    if not account_id:
        return None
    return account_id if account_profile_path(account_id).exists() else None


def current_account() -> dict[str, Any] | None:
    account_id = current_account_id()
    return load_account(account_id) if account_id else None


def list_accounts() -> list[dict[str, Any]]:
    _ensure_root()
    accounts: list[dict[str, Any]] = []
    for path in sorted(ACCOUNTS_ROOT.glob("*/profile.json")):
        payload = _read_json(path, {})
        if isinstance(payload, dict) and payload.get("account_id"):
            accounts.append(_public_profile(payload))
    return sorted(accounts, key=lambda item: str(item.get("updated_at") or ""), reverse=True)


def auth_session_payload() -> dict[str, Any]:
    from gemia import email_delivery

    return {
        "account": current_account(),
        "accounts": list_accounts(),
        "google_client_id": google_client_id(),
        "has_google_client_id": bool(google_client_id()),
        "email_login_enabled": email_delivery.is_configured(),
    }


def sign_out() -> None:
    try:
        ACTIVE_ACCOUNT_PATH.unlink()
    except FileNotFoundError:
        pass


def activate_cloud_account(cloud_account: dict[str, Any]) -> dict[str, Any]:
    """Bind a verified cloud identity to this machine's one shared workspace."""
    cloud_id = _clean_string(cloud_account.get("id"))
    email = normalize_email(_clean_string(cloud_account.get("email")))
    if not cloud_id:
        raise AuthError("Cloud account id is missing")
    workspace_id = _machine_workspace_id()
    now = _utc_now()
    existing = _read_json(account_profile_path(workspace_id), {})
    profile = {
        "version": ACCOUNT_SCHEMA_VERSION,
        "account_id": workspace_id,
        "cloud_account_id": cloud_id,
        "provider": "lumeri-cloud",
        "onboarding_completed": cloud_account.get("onboarding_completed") is True,
        "age_band": _clean_string(cloud_account.get("age_band")),
        "provider_mode": _clean_string(cloud_account.get("provider_mode")),
        "model_provider": _clean_string(cloud_account.get("provider")),
        "provider_subject": cloud_id,
        "email": email,
        "email_verified": True,
        "name": _clean_string(cloud_account.get("display_name")) or email.split("@", 1)[0],
        "picture": _clean_string(cloud_account.get("picture_url")),
        "created_at": existing.get("created_at") if isinstance(existing, dict) else now,
        "updated_at": now,
    }
    _ensure_account_dirs(workspace_id)
    _atomic_write_json(account_profile_path(workspace_id), profile)
    _bootstrap_account_memory(profile)
    _write_active(workspace_id)
    return _public_profile(profile)


def switch_account(account_id: str) -> dict[str, Any]:
    profile = load_account(account_id)
    if not profile:
        raise AuthError("Account not found")
    _write_active(account_id)
    return profile


def sign_in_with_google(credential: str, *, client_id: str | None = None) -> dict[str, Any]:
    """Verify a Google ID token and make its local account active."""
    claims = verify_google_id_token(credential, client_id=client_id)
    sub = _clean_string(claims.get("sub"))
    if not sub:
        raise AuthError("Google account subject is missing")
    email_verified = _truthy(claims.get("email_verified"))
    if not email_verified:
        raise AuthError("Google email is not verified")

    return _activate_account(
        provider="google",
        subject=sub,
        email=_clean_string(claims.get("email")),
        name=_clean_string(claims.get("name")),
        picture=_clean_string(claims.get("picture")),
        email_verified=email_verified,
    )


def _activate_account(
    *,
    provider: str,
    subject: str,
    email: str,
    name: str,
    picture: str = "",
    email_verified: bool = True,
) -> dict[str, Any]:
    """Create or update a local account for ``provider`` and make it active."""
    account_id = account_id_for(provider, subject)
    now = _utc_now()
    existing = _read_json(account_profile_path(account_id), {})
    created_at = existing.get("created_at") if isinstance(existing, dict) else None
    profile = {
        "version": ACCOUNT_SCHEMA_VERSION,
        "account_id": account_id,
        "provider": provider,
        "provider_subject": subject,
        "email": _clean_string(email),
        "email_verified": bool(email_verified),
        "name": _clean_string(name),
        "picture": _clean_string(picture),
        "created_at": created_at or now,
        "updated_at": now,
    }
    _ensure_account_dirs(account_id)
    _atomic_write_json(account_profile_path(account_id), profile)
    _bootstrap_account_memory(profile)
    _write_active(account_id)
    return _public_profile(profile)


def normalize_email(email: str) -> str:
    """Lower-case and validate an email address, or raise ``AuthError``."""
    value = _clean_string(email).lower()
    if not value or len(value) > 254 or not _EMAIL_RE.match(value):
        raise AuthError("邮箱地址格式不正确")
    return value


def start_email_login(email: str) -> dict[str, Any]:
    """Generate a one-time login code, email it, and remember only its hash."""
    from gemia import email_delivery

    address = normalize_email(email)
    _prune_pending_email_codes()
    now = time.time()
    existing = _PENDING_EMAIL_CODES.get(address)
    if existing:
        next_allowed = float(existing.get("sent_at") or 0) + EMAIL_CODE_RESEND_COOLDOWN_SECONDS
        if next_allowed > now:
            wait = int(next_allowed - now) + 1
            raise AuthError(f"验证码发送过于频繁，请 {wait} 秒后再试")

    code = _generate_email_code()
    expires_at = now + EMAIL_CODE_TTL_SECONDS
    # Send first; only remember the pending code if delivery succeeded.
    email_delivery.send_login_code(address, code, ttl_minutes=EMAIL_CODE_TTL_SECONDS // 60)
    _PENDING_EMAIL_CODES[address] = {
        "code_hash": _hash_email_code(address, code),
        "expires_at": expires_at,
        "sent_at": now,
        "attempts": 0,
    }
    return {
        "ok": True,
        "email": address,
        "expires_at": int(expires_at),
        "ttl_seconds": EMAIL_CODE_TTL_SECONDS,
        "resend_after": int(now + EMAIL_CODE_RESEND_COOLDOWN_SECONDS),
    }


def verify_email_login(email: str, code: str) -> dict[str, Any]:
    """Validate a one-time code and activate (or create) the email account."""
    address = normalize_email(email)
    submitted = re.sub(r"\D", "", _clean_string(code))
    _prune_pending_email_codes()
    pending = _PENDING_EMAIL_CODES.get(address)
    if not pending:
        raise AuthError("验证码不存在或已过期，请重新获取")
    if float(pending.get("expires_at") or 0) <= time.time():
        _PENDING_EMAIL_CODES.pop(address, None)
        raise AuthError("验证码已过期，请重新获取")
    if int(pending.get("attempts") or 0) >= EMAIL_CODE_MAX_ATTEMPTS:
        _PENDING_EMAIL_CODES.pop(address, None)
        raise AuthError("验证码尝试次数过多，请重新获取")
    if len(submitted) != EMAIL_CODE_LENGTH:
        pending["attempts"] = int(pending.get("attempts") or 0) + 1
        raise AuthError(f"请输入 {EMAIL_CODE_LENGTH} 位数字验证码")

    expected = str(pending.get("code_hash") or "")
    if not secrets.compare_digest(expected, _hash_email_code(address, submitted)):
        pending["attempts"] = int(pending.get("attempts") or 0) + 1
        remaining = EMAIL_CODE_MAX_ATTEMPTS - int(pending["attempts"])
        if remaining <= 0:
            _PENDING_EMAIL_CODES.pop(address, None)
            raise AuthError("验证码错误次数过多，请重新获取")
        raise AuthError(f"验证码不正确，还可尝试 {remaining} 次")

    _PENDING_EMAIL_CODES.pop(address, None)
    return _activate_account(
        provider="email",
        subject=address,
        email=address,
        name=address.split("@", 1)[0],
        email_verified=True,
    )


def _generate_email_code() -> str:
    return str(secrets.randbelow(10 ** EMAIL_CODE_LENGTH)).zfill(EMAIL_CODE_LENGTH)


def _hash_email_code(email: str, code: str) -> str:
    return hashlib.sha256(f"{email}:{code}".encode("utf-8")).hexdigest()


def _prune_pending_email_codes() -> None:
    now = time.time()
    for key, pending in list(_PENDING_EMAIL_CODES.items()):
        if float(pending.get("expires_at") or 0) <= now:
            _PENDING_EMAIL_CODES.pop(key, None)


def verify_google_id_token(id_token: str, *, client_id: str | None = None) -> dict[str, Any]:
    """Verify a Google ID token and return its claims.

    Prefer google-auth when it is installed. The stdlib fallback asks Google's
    tokeninfo endpoint to validate the token, then still checks local claims.
    """
    token = _clean_string(id_token)
    audience = _clean_string(client_id) or google_client_id()
    if not token:
        raise AuthError("Google credential is missing")
    if not audience:
        raise AuthError("Google OAuth Client ID is not configured")

    try:
        from google.auth.transport import requests as google_requests  # type: ignore
        from google.oauth2 import id_token as google_id_token  # type: ignore

        claims = google_id_token.verify_oauth2_token(token, google_requests.Request(), audience)
    except ImportError:
        claims = _verify_google_id_token_via_tokeninfo(token)
    except Exception as exc:
        raise AuthError(f"Google credential verification failed: {exc}") from exc

    if not isinstance(claims, dict):
        raise AuthError("Google credential verification returned invalid claims")
    return _validate_google_claims(claims, audience)


def _exchange_google_oauth_code(
    code: str,
    *,
    client_id: str,
    code_verifier: str,
    redirect_uri: str,
) -> dict[str, Any]:
    body = {
        "client_id": client_id,
        "code": code,
        "code_verifier": code_verifier,
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
    }
    secret = google_client_secret()
    if secret:
        body["client_secret"] = secret
    data = urllib.parse.urlencode(body).encode("utf-8")
    request = urllib.request.Request(
        GOOGLE_TOKEN_URL,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            error_payload = json.loads(exc.read().decode("utf-8"))
        except Exception:
            error_payload = {}
        message = error_payload.get("error_description") or error_payload.get("error") or str(exc)
        raise AuthError(f"Google token exchange failed: {message}") from exc
    except Exception as exc:
        raise AuthError(f"Google token exchange failed: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("error"):
        raise AuthError(str(payload.get("error_description") or payload.get("error") or "Google token exchange failed"))
    return payload


def _pkce_verifier() -> str:
    return secrets.token_urlsafe(64)[:96]


def _pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return _base64_url_no_padding(digest)


def _base64_url_no_padding(data: bytes) -> str:
    import base64

    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _prune_pending_oauth() -> None:
    now = time.time()
    for state, pending in list(_PENDING_OAUTH_STATES.items()):
        if float(pending.get("expires_at") or 0) <= now:
            _PENDING_OAUTH_STATES.pop(state, None)


def account_id_for(provider: str, subject: str) -> str:
    provider_slug = _slug(provider) or "account"
    digest = hashlib.sha256(f"{provider}:{subject}".encode("utf-8")).hexdigest()[:24]
    return f"{provider_slug}_{digest}"


def account_root(account_id: str) -> Path:
    safe_id = _validate_account_id(account_id)
    binding = _read_json(ACCOUNTS_ROOT / "workspace.json", {})
    if isinstance(binding, dict):
        bound_id = _clean_string(binding.get("workspace_id"))
        if re.fullmatch(r"[a-zA-Z0-9_-]{8,80}", bound_id):
            return ACCOUNTS_ROOT / bound_id
    return ACCOUNTS_ROOT / safe_id


def account_profile_path(account_id: str) -> Path:
    return ACCOUNTS_ROOT / _validate_account_id(account_id) / "profile.json"


def account_memory_root(account_id: str) -> Path:
    return account_root(account_id) / "memory"


def account_session_root(account_id: str) -> Path:
    return account_root(account_id) / "sessions"


def load_account(account_id: str | None) -> dict[str, Any] | None:
    if not account_id:
        return None
    payload = _read_json(account_profile_path(account_id), {})
    if not isinstance(payload, dict) or not payload.get("account_id"):
        return None
    return _public_profile(payload)


def _verify_google_id_token_via_tokeninfo(id_token: str) -> dict[str, Any]:
    url = f"{GOOGLE_TOKENINFO_URL}?{urllib.parse.urlencode({'id_token': id_token})}"
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        raise AuthError(f"Google credential verification failed: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("error"):
        raise AuthError(str(payload.get("error_description") or payload.get("error") or "Google credential rejected"))
    return payload


def _validate_google_claims(claims: dict[str, Any], audience: str) -> dict[str, Any]:
    if _clean_string(claims.get("aud")) != audience:
        raise AuthError("Google credential audience does not match this app")
    issuer = _clean_string(claims.get("iss"))
    if issuer not in {"accounts.google.com", "https://accounts.google.com"}:
        raise AuthError("Google credential issuer is invalid")
    try:
        expires_at = int(claims.get("exp") or 0)
    except (TypeError, ValueError):
        expires_at = 0
    if expires_at <= int(time.time()):
        raise AuthError("Google credential has expired")
    if not _clean_string(claims.get("sub")):
        raise AuthError("Google credential subject is missing")
    return claims


def _public_profile(profile: dict[str, Any]) -> dict[str, Any]:
    account_id = _clean_string(profile.get("account_id"))
    return {
        "version": ACCOUNT_SCHEMA_VERSION,
        "account_id": account_id,
        "cloud_account_id": _clean_string(profile.get("cloud_account_id")),
        "provider": _clean_string(profile.get("provider")) or "google",
        "onboarding_completed": profile.get("onboarding_completed") is True,
        "age_band": _clean_string(profile.get("age_band")),
        "provider_mode": _clean_string(profile.get("provider_mode")),
        "model_provider": _clean_string(profile.get("model_provider")),
        "email": _clean_string(profile.get("email")),
        "email_verified": bool(profile.get("email_verified")),
        "name": _clean_string(profile.get("name")),
        "picture": _clean_string(profile.get("picture")),
        "created_at": _clean_string(profile.get("created_at")),
        "updated_at": _clean_string(profile.get("updated_at")),
        "memory_root": str(account_memory_root(account_id)) if account_id else "",
        "session_root": str(account_session_root(account_id)) if account_id else "",
    }


def _write_active(account_id: str) -> None:
    _ensure_root()
    _atomic_write_json(ACTIVE_ACCOUNT_PATH, {"account_id": _validate_account_id(account_id), "updated_at": _utc_now()})


def _bootstrap_account_memory(profile: dict[str, Any]) -> None:
    account_id = _clean_string(profile.get("account_id"))
    if not account_id:
        return
    memory_root = account_memory_root(account_id)
    daily_dir = memory_root / "daily"
    daily_path = daily_dir / f"{date.today().isoformat()}.md"
    memory_root.mkdir(parents=True, exist_ok=True)
    daily_dir.mkdir(parents=True, exist_ok=True)
    for path in (memory_root, daily_dir):
        try:
            path.chmod(0o700)
        except OSError:
            pass
    _write_text_if_missing(
        memory_root / "README.md",
        "# Lumeri Device Memory\n\n"
        "This local memory belongs to this device's shared creative workspace.\n\n"
        "Do not store secrets, tokens, passwords, or raw private conversations here.\n",
    )
    _write_text_if_missing(
        memory_root / "MEMORY.md",
        "# Device Workspace Memory\n\n"
        "- Creative data is shared by signed-in accounts on this device.\n"
        "- Cloud account data, provider credentials, and credits remain account-isolated.\n",
    )
    _write_text_if_missing(daily_path, f"# {daily_path.stem}\n\n- Account memory initialized.\n")


def _ensure_account_dirs(account_id: str) -> None:
    root = account_root(account_id)
    session_history = account_session_root(account_id) / "history"
    memory_root = account_memory_root(account_id)
    for path in (
        ACCOUNTS_ROOT,
        account_profile_path(account_id).parent,
        root,
        account_session_root(account_id),
        session_history,
        memory_root,
    ):
        path.mkdir(parents=True, exist_ok=True)
        try:
            path.chmod(0o700)
        except OSError:
            pass


def _ensure_root() -> None:
    ACCOUNTS_ROOT.mkdir(parents=True, exist_ok=True)
    try:
        ACCOUNTS_ROOT.chmod(0o700)
    except OSError:
        pass


def _machine_workspace_id() -> str:
    """Return a stable local workspace id without moving existing creative data."""
    _ensure_root()
    binding_path = ACCOUNTS_ROOT / "workspace.json"
    binding = _read_json(binding_path, {})
    if isinstance(binding, dict):
        bound = _clean_string(binding.get("workspace_id"))
        if re.fullmatch(r"[a-zA-Z0-9_-]{8,80}", bound):
            return bound
    active = _read_json(ACTIVE_ACCOUNT_PATH, {})
    candidate = _clean_string(active.get("account_id")) if isinstance(active, dict) else ""
    if not re.fullmatch(r"[a-zA-Z0-9_-]{8,80}", candidate):
        candidate = MACHINE_WORKSPACE_ID
    _atomic_write_json(binding_path, {"workspace_id": candidate, "created_at": _utc_now()})
    return candidate


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


def _write_text_if_missing(path: Path, text: str) -> None:
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _validate_account_id(account_id: str) -> str:
    value = _clean_string(account_id)
    if not re.fullmatch(r"[a-zA-Z0-9_-]{8,80}", value):
        raise AuthError("Invalid account id")
    return value


def _clean_string(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()


def _slug(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "_", value.strip().lower()).strip("_")


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).lower() in {"1", "true", "yes"}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = [
    "ACCOUNTS_ROOT",
    "ACTIVE_ACCOUNT_PATH",
    "AuthError",
    "GOOGLE_OAUTH_REDIRECT_URI",
    "MACHINE_WORKSPACE_ID",
    "activate_cloud_account",
    "account_id_for",
    "account_memory_root",
    "account_session_root",
    "auth_session_payload",
    "current_account",
    "current_account_id",
    "google_client_id",
    "google_client_secret",
    "finish_google_oauth",
    "list_accounts",
    "normalize_email",
    "save_google_client_id",
    "start_email_login",
    "start_google_oauth",
    "sign_in_with_google",
    "sign_out",
    "switch_account",
    "verify_email_login",
    "verify_google_id_token",
]
