"""Client for the Lumeri cloud account service.

Native clients use OAuth 2.0-style device authorization. Refresh tokens are
kept only in the operating system credential store; browser JavaScript never
receives them.
"""
from __future__ import annotations

import ctypes
import json
import os
import platform
import re
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Protocol

DEFAULT_ORIGIN = "https://accounts.lumeri.io"
CLIENT_USER_AGENT = "Lumeri/1.0"
KEYCHAIN_SERVICE = "io.lumeri.accounts"
KEYCHAIN_ACCOUNT = "refresh-token"
_CLOUD_BYOK_PROVIDERS = {"openai", "anthropic", "gemini", "openrouter", "custom"}
_MODEL_CONFIG_FIELDS = {
    "openrouter_api_key",
    "gemini_api_key",
    "anthropic_api_key",
    "openai_api_key",
}
_CLOUD_MODEL_METADATA_KEY = "cloud_account_model_profiles"
_AUXILIARY_CREDENTIAL_FIELDS = {
    "tavily": "tavily_api_key",
    "brave": "brave_api_key",
    "serper": "serper_api_key",
    "exa": "exa_api_key",
    "bing": "bing_api_key",
    "google_cse": "google_cse_key",
    "searxng": "searxng_api_key",
}


def _cloud_provider(value: object) -> str:
    provider = str(value or "").strip().lower()
    provider = {"claude": "anthropic"}.get(provider, provider)
    return (
        provider
        if provider in _CLOUD_BYOK_PROVIDERS
        or provider in {"lumeri", "openai_subscription"}
        else ""
    )


def _local_provider(value: object) -> str:
    provider = _cloud_provider(value)
    return {"anthropic": "claude"}.get(provider, provider)


def _credential_config_field(value: object) -> str:
    return {
        "openai": "openai_api_key",
        "anthropic": "anthropic_api_key",
        "gemini": "gemini_api_key",
        "openrouter": "openrouter_api_key",
        "custom": "openai_api_key",
    }.get(_cloud_provider(value), "")


def _account_allows_byok(account: dict[str, Any], *, provider: str) -> bool:
    return bool(
        account.get("onboarding_completed") is True
        and account.get("age_band") == "18_plus"
        and account.get("provider_mode") == "byok"
        and provider in _CLOUD_BYOK_PROVIDERS
        and _cloud_provider(account.get("provider")) == provider
    )


class CloudAuthError(RuntimeError):
    def __init__(self, message: str, *, code: str = "cloud_auth_error", status: int = 400):
        super().__init__(message)
        self.code = code
        self.status = status


class TokenStore(Protocol):
    def get(self) -> str | None: ...

    def set(self, token: str) -> None: ...

    def delete(self) -> None: ...


class Transport(Protocol):
    def request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        access_token: str = "",
    ) -> tuple[int, dict[str, Any]]: ...


class SystemTokenStore:
    """Store one refresh token in macOS Keychain or Windows Credential Manager."""

    def __init__(self, *, service: str = KEYCHAIN_SERVICE, account: str = KEYCHAIN_ACCOUNT):
        self.service = service
        self.account = account

    def get(self) -> str | None:
        system = platform.system()
        if system == "Darwin":
            return self._mac_get()
        if system == "Windows":
            return self._windows_get()
        raise CloudAuthError("系统安全凭据存储不可用", code="secure_store_unavailable", status=503)

    def set(self, token: str) -> None:
        value = str(token or "")
        if not value:
            raise CloudAuthError("刷新凭据为空", code="invalid_token")
        system = platform.system()
        if system == "Darwin":
            self._mac_set(value)
            return
        if system == "Windows":
            self._windows_set(value)
            return
        raise CloudAuthError("系统安全凭据存储不可用", code="secure_store_unavailable", status=503)

    def delete(self) -> None:
        system = platform.system()
        if system == "Darwin":
            self._mac_delete()
            return
        if system == "Windows":
            self._windows_delete()
            return

    def _mac_security(self):
        library = ctypes.cdll.LoadLibrary(
            "/System/Library/Frameworks/Security.framework/Security"
        )
        library.SecKeychainFindGenericPassword.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_char_p,
            ctypes.c_uint32,
            ctypes.c_char_p,
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p),
        ]
        library.SecKeychainFindGenericPassword.restype = ctypes.c_int32
        library.SecKeychainItemFreeContent.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        library.SecKeychainItemFreeContent.restype = ctypes.c_int32
        library.SecKeychainAddGenericPassword.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_char_p,
            ctypes.c_uint32,
            ctypes.c_char_p,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        library.SecKeychainAddGenericPassword.restype = ctypes.c_int32
        library.SecKeychainItemModifyAttributesAndData.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_void_p,
        ]
        library.SecKeychainItemModifyAttributesAndData.restype = ctypes.c_int32
        library.SecKeychainItemDelete.argtypes = [ctypes.c_void_p]
        library.SecKeychainItemDelete.restype = ctypes.c_int32
        return library

    @staticmethod
    def _mac_release(item: ctypes.c_void_p) -> None:
        if not item:
            return
        core_foundation = ctypes.cdll.LoadLibrary(
            "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
        )
        core_foundation.CFRelease.argtypes = [ctypes.c_void_p]
        core_foundation.CFRelease(item)

    def _mac_find(self):
        security = self._mac_security()
        service = self.service.encode("utf-8")
        account = self.account.encode("utf-8")
        length = ctypes.c_uint32()
        data = ctypes.c_void_p()
        item = ctypes.c_void_p()
        status = security.SecKeychainFindGenericPassword(
            None,
            len(service),
            service,
            len(account),
            account,
            ctypes.byref(length),
            ctypes.byref(data),
            ctypes.byref(item),
        )
        return security, status, length, data, item

    def _mac_get(self) -> str | None:
        security, status, length, data, item = self._mac_find()
        if status == -25300:  # errSecItemNotFound
            return None
        if status != 0:
            raise CloudAuthError("无法读取系统钥匙串", code="secure_store_read_failed", status=503)
        try:
            return ctypes.string_at(data, length.value).decode("utf-8")
        finally:
            security.SecKeychainItemFreeContent(None, data)
            self._mac_release(item)

    def _mac_set(self, token: str) -> None:
        security, status, _, data, item = self._mac_find()
        if status == 0:
            security.SecKeychainItemFreeContent(None, data)
            raw = token.encode("utf-8")
            result = security.SecKeychainItemModifyAttributesAndData(
                item, None, len(raw), raw
            )
            self._mac_release(item)
        elif status == -25300:
            service = self.service.encode("utf-8")
            account = self.account.encode("utf-8")
            raw = token.encode("utf-8")
            result = security.SecKeychainAddGenericPassword(
                None,
                len(service),
                service,
                len(account),
                account,
                len(raw),
                raw,
                None,
            )
        else:
            result = status
        if result != 0:
            raise CloudAuthError("无法写入系统钥匙串", code="secure_store_write_failed", status=503)

    def _mac_delete(self) -> None:
        security, status, _, data, item = self._mac_find()
        if status == -25300:
            return
        if status != 0:
            raise CloudAuthError("无法读取系统钥匙串", code="secure_store_read_failed", status=503)
        security.SecKeychainItemFreeContent(None, data)
        result = security.SecKeychainItemDelete(item)
        self._mac_release(item)
        if result != 0:
            raise CloudAuthError("无法清除系统钥匙串", code="secure_store_delete_failed", status=503)

    class _WindowsFileTime(ctypes.Structure):
        _fields_ = [("dwLowDateTime", ctypes.c_uint32), ("dwHighDateTime", ctypes.c_uint32)]

    class _WindowsCredential(ctypes.Structure):
        pass

    _WindowsCredential._fields_ = [
        ("Flags", ctypes.c_uint32),
        ("Type", ctypes.c_uint32),
        ("TargetName", ctypes.c_wchar_p),
        ("Comment", ctypes.c_wchar_p),
        ("LastWritten", _WindowsFileTime),
        ("CredentialBlobSize", ctypes.c_uint32),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", ctypes.c_uint32),
        ("AttributeCount", ctypes.c_uint32),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", ctypes.c_wchar_p),
        ("UserName", ctypes.c_wchar_p),
    ]

    def _windows_get(self) -> str | None:
        advapi = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
        pointer = ctypes.POINTER(self._WindowsCredential)()
        if not advapi.CredReadW(self.service, 1, 0, ctypes.byref(pointer)):
            error = ctypes.get_last_error()
            if error == 1168:  # ERROR_NOT_FOUND
                return None
            raise CloudAuthError("无法读取 Windows 凭据管理器", code="secure_store_read_failed", status=503)
        try:
            credential = pointer.contents
            raw = ctypes.string_at(credential.CredentialBlob, credential.CredentialBlobSize)
            return raw.decode("utf-8")
        finally:
            advapi.CredFree(pointer)

    def _windows_set(self, token: str) -> None:
        advapi = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
        raw = token.encode("utf-8")
        blob = (ctypes.c_ubyte * len(raw)).from_buffer_copy(raw)
        credential = self._WindowsCredential()
        credential.Type = 1
        credential.TargetName = self.service
        credential.CredentialBlobSize = len(raw)
        credential.CredentialBlob = ctypes.cast(blob, ctypes.POINTER(ctypes.c_ubyte))
        credential.Persist = 2
        credential.UserName = self.account
        if not advapi.CredWriteW(ctypes.byref(credential), 0):
            raise CloudAuthError("无法写入 Windows 凭据管理器", code="secure_store_write_failed", status=503)

    def _windows_delete(self) -> None:
        advapi = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
        if advapi.CredDeleteW(self.service, 1, 0):
            return
        error = ctypes.get_last_error()
        if error != 1168:
            raise CloudAuthError("无法清除 Windows 凭据管理器", code="secure_store_delete_failed", status=503)


class HttpTransport:
    def __init__(self, origin: str = DEFAULT_ORIGIN, *, timeout: float = 15.0):
        normalized = str(origin or "").rstrip("/")
        parsed = urllib.parse.urlparse(normalized)
        allow_insecure = os.environ.get("LUMERI_ACCOUNTS_ALLOW_INSECURE", "").lower() in {
            "1",
            "true",
            "yes",
        }
        if parsed.scheme != "https" and not (
            allow_insecure and parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost"}
        ):
            raise CloudAuthError("账户服务必须使用 HTTPS", code="insecure_account_origin")
        if not parsed.netloc or parsed.username or parsed.password:
            raise CloudAuthError("账户服务地址无效", code="invalid_account_origin")
        self.origin = normalized
        self.timeout = timeout

    def request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        access_token: str = "",
    ) -> tuple[int, dict[str, Any]]:
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {
            "Accept": "application/json",
            "User-Agent": CLIENT_USER_AGENT,
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
        if access_token:
            headers["Authorization"] = f"Bearer {access_token}"
        request = urllib.request.Request(
            f"{self.origin}{path}", data=body, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
                status = response.status
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            status = exc.code
        except Exception as exc:
            raise CloudAuthError(
                "无法连接 Lumeri 账户服务", code="account_service_unavailable", status=503
            ) from exc
        try:
            decoded = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:
            decoded = {}
        return status, decoded if isinstance(decoded, dict) else {}


@dataclass
class PendingDevice:
    device_code: str
    expires_at: float
    interval: int


class CloudAccountClient:
    def __init__(self, transport: Transport, token_store: TokenStore):
        self.transport = transport
        self.token_store = token_store
        self._pending: dict[str, PendingDevice] = {}
        self._access_token = ""
        self._account: dict[str, Any] | None = None
        self._credential: dict[str, str] | None = None
        self._auxiliary_credentials: dict[str, dict[str, str]] = {}
        self._lock = threading.RLock()

    def start_device_login(self, *, device_name: str, platform_name: str) -> dict[str, Any]:
        status, data = self.transport.request(
            "POST",
            "/v1/device/authorizations",
            payload={"device_name": device_name[:120], "platform": platform_name[:40]},
        )
        if status != 201:
            self._raise(data, status)
        device_code = str(data.get("device_code") or "")
        if not device_code:
            raise CloudAuthError("账户服务未返回设备授权", code="invalid_account_response", status=502)
        attempt_id = secrets.token_urlsafe(24)
        interval = max(2, min(int(data.get("interval") or 3), 10))
        expires_in = max(30, min(int(data.get("expires_in") or 600), 900))
        with self._lock:
            self._prune_pending()
            self._pending[attempt_id] = PendingDevice(
                device_code=device_code,
                expires_at=time.time() + expires_in,
                interval=interval,
            )
        return {
            "attempt_id": attempt_id,
            "user_code": str(data.get("user_code") or ""),
            "verification_uri": str(data.get("verification_uri") or ""),
            "verification_uri_complete": str(data.get("verification_uri_complete") or ""),
            "expires_in": expires_in,
            "interval": interval,
        }

    def poll_device_login(
        self, attempt_id: str, *, sync_credential: bool = True
    ) -> dict[str, Any]:
        with self._lock:
            self._prune_pending()
            pending = self._pending.get(str(attempt_id or ""))
        if not pending:
            raise CloudAuthError("登录请求不存在或已过期", code="authorization_expired")
        status, data = self.transport.request(
            "POST", "/v1/device/token", payload={"device_code": pending.device_code}
        )
        if status == 428 and data.get("error") == "authorization_pending":
            return {"pending": True, "interval": pending.interval}
        if status != 200:
            with self._lock:
                self._pending.pop(attempt_id, None)
            self._raise(data, status)
        with self._lock:
            self._pending.pop(attempt_id, None)
        account = self._accept_tokens(data)
        if sync_credential:
            self._sync_selected_credential(account)
        return {"pending": False, "account": account}

    def current_account(self, *, sync_credential: bool = True) -> dict[str, Any] | None:
        with self._lock:
            access = self._access_token
        if access:
            status, data = self.transport.request("GET", "/v1/me", access_token=access)
            if status == 200:
                with self._lock:
                    prior_id = str((self._account or {}).get("id") or "")
                    next_id = str(data.get("id") or "")
                    if prior_id and prior_id != next_id:
                        self._credential = None
                        self._auxiliary_credentials = {}
                    self._account = dict(data)
                if sync_credential:
                    self._sync_selected_credential(data)
                return dict(data)
        refresh = self.token_store.get()
        if not refresh:
            self._clear_memory()
            return None
        status, data = self.transport.request(
            "POST", "/v1/auth/refresh", payload={"refresh_token": refresh}
        )
        if status != 200:
            self.token_store.delete()
            self._clear_memory()
            return None
        account = self._accept_tokens(data)
        if sync_credential:
            self._sync_selected_credential(account)
        return account

    def sync_selected_credential(self, account: dict[str, Any]) -> None:
        """Fetch the selected key after the caller has handled account change."""
        self._sync_selected_credential(account)

    def select_provider(self, provider: str) -> dict[str, Any]:
        """Select one account provider and refresh its account-bound context."""
        cloud_provider = _cloud_provider(provider)
        if not cloud_provider:
            raise CloudAuthError("供应商无效", code="invalid_provider")
        current = self.current_account(sync_credential=False)
        expected_id = str((current or {}).get("id") or "")
        if not expected_id:
            raise CloudAuthError("请先登录 Lumeri", code="unauthorized", status=401)
        status, data = self._authorized_account_request(
            "PUT",
            "/v1/provider-selection",
            payload={"provider": cloud_provider},
        )
        if status != 200:
            self._raise(data, status)
        account_id = str(data.get("id") or "")
        selected = _cloud_provider(data.get("provider"))
        if not expected_id or account_id != expected_id or selected != cloud_provider:
            self.clear_credential()
            raise CloudAuthError(
                "账户服务返回的供应商不匹配",
                code="invalid_account_response",
                status=502,
            )
        with self._lock:
            self._account = dict(data)
            self._credential = None
        self._sync_selected_credential(data)
        return dict(data)

    def put_selected_credential(self, provider: str, secret: str) -> dict[str, Any]:
        """Save and activate the signed-in account's selected BYOK credential."""
        cloud_provider = _cloud_provider(provider)
        secret_value = str(secret or "").strip()
        if not cloud_provider or not secret_value:
            raise CloudAuthError("供应商凭证无效", code="invalid_credential")
        with self._lock:
            account = dict(self._account or {})
        if not _account_allows_byok(account, provider=cloud_provider):
            raise CloudAuthError(
                "凭证供应商与账户设置不匹配",
                code="provider_mismatch",
                status=403,
            )
        status, data = self._authorized_request(
            "PUT",
            f"/v1/credentials/{urllib.parse.quote(cloud_provider, safe='')}",
            payload={"label": "default", "secret": secret_value},
        )
        self._require_same_account(account, cloud_provider)
        if status != 200:
            self._raise(data, status)
        if str(data.get("provider") or "") != cloud_provider or str(data.get("label") or "") != "default":
            raise CloudAuthError("账户服务返回的凭证不匹配", code="invalid_account_response", status=502)
        self._set_credential(account, cloud_provider, secret_value)
        return dict(data)

    def credential_snapshot(self) -> dict[str, str] | None:
        """Return the active account-bound credential for local in-memory use."""
        with self._lock:
            return dict(self._credential) if self._credential else None

    def clear_credential(self) -> None:
        """Forget only the cloud credential held by this client."""
        with self._lock:
            self._credential = None

    def put_auxiliary_credential(self, provider: str, secret: str) -> dict[str, Any]:
        """Save one account-isolated search credential and retain it only in memory."""

        clean_provider = str(provider or "").strip().lower()
        config_field = _AUXILIARY_CREDENTIAL_FIELDS.get(clean_provider, "")
        secret_value = str(secret or "").strip()
        if not config_field or not secret_value:
            raise CloudAuthError("搜索凭证无效", code="invalid_credential")
        with self._lock:
            account = dict(self._account or {})
        status, data = self._authorized_account_request(
            "PUT",
            f"/v1/credentials/auxiliary/{urllib.parse.quote(clean_provider, safe='')}",
            payload={"label": "default", "secret": secret_value},
        )
        self._require_same_account(account, _cloud_provider(account.get("provider")))
        if status != 200:
            self._raise(data, status)
        if (
            str(data.get("provider") or "") != f"search_{clean_provider}"
            or str(data.get("label") or "") != "default"
        ):
            raise CloudAuthError(
                "账户服务返回的搜索凭证不匹配",
                code="invalid_account_response",
                status=502,
            )
        self._set_auxiliary_credential(account, clean_provider, secret_value)
        return dict(data)

    def sync_auxiliary_credential(self, provider: str) -> bool:
        """Fetch exactly one selected search credential into account-bound memory."""

        clean_provider = str(provider or "").strip().lower()
        if clean_provider not in _AUXILIARY_CREDENTIAL_FIELDS:
            self.clear_auxiliary_credentials()
            return False
        with self._lock:
            account = dict(self._account or {})
        status, data = self._authorized_account_request(
            "POST",
            f"/v1/credentials/auxiliary/{urllib.parse.quote(clean_provider, safe='')}",
            payload={},
        )
        self._require_same_account(account, _cloud_provider(account.get("provider")))
        if status == 404 and str(data.get("error") or "") == "not_found":
            with self._lock:
                self._auxiliary_credentials.pop(clean_provider, None)
            return False
        if status != 200:
            self._raise(data, status)
        secret_value = str(data.get("secret") or "").strip()
        if (
            str(data.get("provider") or "") != clean_provider
            or str(data.get("label") or "") != "default"
            or not secret_value
        ):
            raise CloudAuthError(
                "账户服务返回的搜索凭证不匹配",
                code="invalid_account_response",
                status=502,
            )
        self._set_auxiliary_credential(account, clean_provider, secret_value)
        return True

    def auxiliary_credential_snapshot(self) -> dict[str, dict[str, str]]:
        """Return a copy of account-bound search credentials currently held in memory."""

        with self._lock:
            return {key: dict(value) for key, value in self._auxiliary_credentials.items()}

    def clear_auxiliary_credentials(self) -> None:
        with self._lock:
            self._auxiliary_credentials = {}

    def publish_skill_artifact(self, artifact: dict[str, Any]) -> dict[str, Any]:
        """Upload one validated Skill/Workflow through the signed-in account."""
        status, data = self._authorized_account_request(
            "POST",
            "/v1/skill-cloud/artifacts",
            payload=dict(artifact),
        )
        if status != 201:
            self._raise(data, status)
        item = data.get("artifact")
        if not isinstance(item, dict):
            raise CloudAuthError(
                "Skill Cloud 返回的数据不完整",
                code="invalid_account_response",
                status=502,
            )
        return {"artifact": dict(item), "created": bool(data.get("created"))}

    def list_skill_artifacts(self, *, kind: str = "") -> list[dict[str, Any]]:
        """List account-owned and public Skill/Workflow metadata."""
        clean_kind = str(kind or "").strip().lower()
        if clean_kind not in {"", "skill", "workflow", "point_library"}:
            raise CloudAuthError("Skill 类型无效", code="invalid_skill_kind", status=422)
        query = f"?kind={urllib.parse.quote(clean_kind, safe='')}" if clean_kind else ""
        status, data = self._authorized_account_request(
            "GET", f"/v1/skill-cloud/artifacts{query}"
        )
        if status != 200:
            self._raise(data, status)
        items = data.get("artifacts")
        if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
            raise CloudAuthError(
                "Skill Cloud 返回的数据不完整",
                code="invalid_account_response",
                status=502,
            )
        return [dict(item) for item in items]

    def load_skill_artifact(
        self,
        *,
        kind: str,
        artifact_id: str,
        version: str,
        content_sha256: str = "",
    ) -> dict[str, Any]:
        """Load one exact account-owned or public version."""
        clean_kind = str(kind or "").strip().lower()
        if clean_kind not in {"skill", "workflow", "point_library"}:
            raise CloudAuthError("Skill 类型无效", code="invalid_skill_kind", status=422)
        path = "/v1/skill-cloud/artifacts/{}/{}/{}".format(
            urllib.parse.quote(clean_kind, safe=""),
            urllib.parse.quote(str(artifact_id or "").strip(), safe=""),
            urllib.parse.quote(str(version or "").strip(), safe=""),
        )
        clean_hash = str(content_sha256 or "").strip().lower()
        if clean_hash:
            if not re.fullmatch(r"[a-f0-9]{64}", clean_hash):
                raise CloudAuthError(
                    "Skill 内容摘要无效", code="invalid_skill_hash", status=422
                )
            path += f"?content_sha256={urllib.parse.quote(clean_hash, safe='')}"
        status, data = self._authorized_account_request("GET", path)
        if status != 200:
            self._raise(data, status)
        return dict(data)

    def logout(self) -> None:
        with self._lock:
            access = self._access_token
        try:
            if access:
                self.transport.request("POST", "/v1/logout", access_token=access)
        finally:
            self.token_store.delete()
            self._clear_memory()

    def _accept_tokens(self, data: dict[str, Any]) -> dict[str, Any]:
        access = str(data.get("access_token") or "")
        refresh = str(data.get("refresh_token") or "")
        account = data.get("account")
        if not access or not refresh or not isinstance(account, dict) or not account.get("id"):
            raise CloudAuthError("账户服务返回的数据不完整", code="invalid_account_response", status=502)
        self.token_store.set(refresh)
        with self._lock:
            prior_id = str((self._account or {}).get("id") or "")
            next_id = str(account.get("id") or "")
            if prior_id and prior_id != next_id:
                self._credential = None
                self._auxiliary_credentials = {}
            self._access_token = access
            self._account = dict(account)
        return dict(account)

    def _authorized_request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
    ) -> tuple[int, dict[str, Any]]:
        with self._lock:
            access = self._access_token
        if not access:
            self._refresh_access()
            with self._lock:
                access = self._access_token
        status, data = self.transport.request(method, path, payload=payload, access_token=access)
        if status != 401:
            return status, data
        self._refresh_access()
        with self._lock:
            access = self._access_token
        return self.transport.request(method, path, payload=payload, access_token=access)

    def _authorized_account_request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
    ) -> tuple[int, dict[str, Any]]:
        account = self.current_account(sync_credential=False)
        expected_id = str((account or {}).get("id") or "")
        if not expected_id:
            raise CloudAuthError("请先登录 Lumeri", code="unauthorized", status=401)
        status, data = self._authorized_request(method, path, payload=payload)
        with self._lock:
            current_id = str((self._account or {}).get("id") or "")
        if current_id != expected_id:
            raise CloudAuthError(
                "账户已切换，请重新执行 Skill Cloud 操作",
                code="account_changed",
                status=409,
            )
        return status, data

    def _refresh_access(self) -> dict[str, Any]:
        refresh = self.token_store.get()
        if not refresh:
            self._clear_memory()
            raise CloudAuthError("登录已过期", code="unauthorized", status=401)
        status, data = self.transport.request(
            "POST", "/v1/auth/refresh", payload={"refresh_token": refresh}
        )
        if status != 200:
            self.token_store.delete()
            self._clear_memory()
            self._raise(data, status)
        return self._accept_tokens(data)

    def _sync_selected_credential(self, account: dict[str, Any]) -> None:
        cloud_provider = _cloud_provider(account.get("provider"))
        with self._lock:
            current = dict(self._credential or {})
        account_id = str(account.get("id") or "")
        if (
            current.get("account_id") != account_id
            or current.get("cloud_provider") != cloud_provider
        ):
            self.clear_credential()
        self._set_credential(account, cloud_provider, "")
        if not _account_allows_byok(account, provider=cloud_provider):
            return
        status, data = self._authorized_request(
            "POST", "/v1/credentials/selected", payload={}
        )
        self._require_same_account(account, cloud_provider)
        if status == 404 and str(data.get("error") or "") == "not_found":
            return
        if status != 200:
            self._raise(data, status)
        provider = _cloud_provider(data.get("provider"))
        label = str(data.get("label") or "")
        secret = str(data.get("secret") or "").strip()
        if provider != cloud_provider or label != "default" or not secret:
            self.clear_credential()
            raise CloudAuthError("账户服务返回的凭证不匹配", code="invalid_account_response", status=502)
        self._set_credential(account, provider, secret)

    def _set_credential(self, account: dict[str, Any], provider: str, secret: str) -> None:
        with self._lock:
            self._credential = {
                "account_id": str(account.get("id") or ""),
                "cloud_provider": provider,
                "local_provider": _local_provider(provider),
                "config_field": _credential_config_field(provider),
                "secret": secret,
            }

    def _set_auxiliary_credential(
        self,
        account: dict[str, Any],
        provider: str,
        secret: str,
    ) -> None:
        with self._lock:
            self._auxiliary_credentials[provider] = {
                "account_id": str(account.get("id") or ""),
                "provider": provider,
                "config_field": _AUXILIARY_CREDENTIAL_FIELDS[provider],
                "secret": secret,
            }

    def _require_same_account(self, expected: dict[str, Any], provider: str) -> None:
        """Fail closed if an access-token refresh crossed account context."""
        with self._lock:
            current = dict(self._account or {})
        if (
            str(current.get("id") or "") != str(expected.get("id") or "")
            or _cloud_provider(current.get("provider")) != provider
        ):
            self.clear_credential()
            raise CloudAuthError(
                "账户已切换，请重新确认供应商凭证",
                code="account_changed",
                status=409,
            )

    def _clear_memory(self) -> None:
        with self._lock:
            self._access_token = ""
            self._account = None
            self._credential = None
            self._auxiliary_credentials = {}

    def _prune_pending(self) -> None:
        now = time.time()
        for key, value in list(self._pending.items()):
            if value.expires_at <= now:
                self._pending.pop(key, None)

    @staticmethod
    def _raise(data: dict[str, Any], status: int) -> None:
        remote_code = str(data.get("error") or "").strip()
        message = str(
            data.get("message")
            or data.get("user_message")
            or data.get("detail")
            or "Lumeri 账户请求失败"
        ).strip()
        # FastAPI's default 404 shape is {"detail": "Not Found"}. A missing
        # route means the desktop client and the deployed account service are
        # on different releases; preserve that diagnosis instead of reducing
        # it to the opaque cloud_auth_error fallback.
        code = remote_code or (
            "account_service_contract_missing"
            if status == 404 and message.lower() == "not found"
            else "cloud_auth_error"
        )
        if code == "account_service_contract_missing":
            message = "Lumeri 账户服务尚未部署当前客户端所需接口"
        raise CloudAuthError(message, code=code, status=status)


_CLIENT: CloudAccountClient | None = None


def enabled() -> bool:
    return os.environ.get("LUMERI_CLOUD_ACCOUNTS", "").strip().lower() in {"1", "true", "yes"}


def client() -> CloudAccountClient:
    global _CLIENT
    if _CLIENT is None:
        origin = os.environ.get("LUMERI_ACCOUNTS_ORIGIN", DEFAULT_ORIGIN)
        _CLIENT = CloudAccountClient(HttpTransport(origin), SystemTokenStore())
    return _CLIENT


def strip_model_credentials(config: dict[str, Any]) -> dict[str, Any]:
    """Return config metadata without any machine-global model credential."""
    safe = deepcopy(config) if isinstance(config, dict) else {}
    for field in (*_MODEL_CONFIG_FIELDS, "sisyphus_api_key"):
        safe.pop(field, None)
    profiles = safe.get("brain_provider_profiles")
    if isinstance(profiles, dict):
        for profile in profiles.values():
            if isinstance(profile, dict):
                profile.pop("api_key", None)
    cloud_profiles = safe.get(_CLOUD_MODEL_METADATA_KEY)
    if isinstance(cloud_profiles, dict):
        for profile in cloud_profiles.values():
            if isinstance(profile, dict):
                profile.pop("api_key", None)
    return safe


def bind_model_metadata(config: dict[str, Any]) -> dict[str, Any]:
    """Bind the selected non-secret local model metadata to this cloud account."""
    safe = strip_model_credentials(config)
    if not enabled():
        return safe
    snapshot = client().credential_snapshot() or {}
    account_id = str(snapshot.get("account_id") or "")
    local_provider = str(snapshot.get("local_provider") or "")
    if not account_id or not local_provider:
        return safe
    profiles = safe.get("brain_provider_profiles")
    active = str(safe.get("brain_active_profile") or "")
    profile = dict(profiles.get(active) or {}) if isinstance(profiles, dict) else {}
    if str(profile.get("provider") or "") != local_provider:
        profile = {"provider": local_provider}
    profile.pop("api_key", None)
    bound = safe.get(_CLOUD_MODEL_METADATA_KEY)
    bound = dict(bound) if isinstance(bound, dict) else {}
    bound[account_id] = profile
    safe[_CLOUD_MODEL_METADATA_KEY] = bound
    return safe


def runtime_model_config(
    config: dict[str, Any],
    *,
    provider_override: str | None = None,
) -> dict[str, Any]:
    """Build a cloud-account-bound model config without file/env fallback.

    ``provider_override`` is only for a non-mutating Setup connection test:
    the creator may test the provider currently shown in the panel before
    pressing Save, while normal runtime calls remain account-authoritative.
    """
    safe = strip_model_credentials(config)
    if not enabled():
        return safe
    safe.pop("proxy", None)
    snapshot = client().credential_snapshot() or {}
    account_id = str(snapshot.get("account_id") or "").strip()
    local_provider = str(snapshot.get("local_provider") or "").strip()
    requested_provider = _local_provider(provider_override)
    if requested_provider:
        local_provider = requested_provider
    secret = str(snapshot.get("secret") or "").strip()
    profiles = safe.get("brain_provider_profiles")
    profiles = dict(profiles) if isinstance(profiles, dict) else {}
    active = str(safe.get("brain_active_profile") or "")
    selected_id = ""
    if local_provider in {"openai", "openai_subscription", "claude", "gemini", "openrouter"}:
        selected_id = local_provider
    elif local_provider == "custom":
        active_profile = profiles.get(active)
        if isinstance(active_profile, dict) and active_profile.get("provider") == "custom":
            selected_id = active
        else:
            selected_id = next(
                (
                    str(profile_id)
                    for profile_id, profile in profiles.items()
                    if isinstance(profile, dict) and profile.get("provider") == "custom"
                ),
                "cloud:custom",
            )
    else:
        selected_id = f"cloud:{local_provider or 'signed-out'}"
    bound = safe.get(_CLOUD_MODEL_METADATA_KEY)
    selected = (
        dict(bound.get(account_id) or {})
        if account_id and isinstance(bound, dict)
        else {}
    )
    if str(selected.get("provider") or "") != local_provider:
        selected = {}
    if not selected and requested_provider:
        active_profile = profiles.get(active)
        if isinstance(active_profile, dict) and str(active_profile.get("provider") or "") == local_provider:
            selected = dict(active_profile)
            selected.pop("api_key", None)
    selected.pop("api_key", None)
    if local_provider != "custom":
        # Fixed cloud providers always use their official endpoint.  A stale
        # account profile must never redirect an account-bound credential to a
        # machine-global or previously configured compatible API host.
        selected.pop("base_url", None)
        selected.pop("auth_mode", None)
    if local_provider == "openai_subscription":
        from gemia.brain_config import (
            OPENAI_SUBSCRIPTION_BASE_URL,
            OPENAI_SUBSCRIPTION_MODE,
        )

        selected["base_url"] = OPENAI_SUBSCRIPTION_BASE_URL
        selected["auth_mode"] = OPENAI_SUBSCRIPTION_MODE
    selected["provider"] = local_provider or "cloud_signed_out"
    custom_ready = local_provider != "custom" or bool(
        str(selected.get("base_url") or "").strip()
        and str(selected.get("model") or "").strip()
    )
    if secret and custom_ready:
        selected["api_key"] = secret
    profiles[selected_id] = selected
    safe["brain_provider_profiles"] = profiles
    safe["brain_active_profile"] = selected_id
    return safe


def credential_for_provider(provider: str) -> str | None:
    """Return None in legacy mode, or the exact active cloud key in cloud mode."""
    if not enabled():
        return None
    snapshot = client().credential_snapshot() or {}
    requested = _local_provider(provider)
    selected = str(snapshot.get("local_provider") or "")
    if not requested or requested != selected:
        return ""
    return str(snapshot.get("secret") or "")


__all__ = [
    "CloudAccountClient",
    "CloudAuthError",
    "HttpTransport",
    "SystemTokenStore",
    "bind_model_metadata",
    "client",
    "credential_for_provider",
    "enabled",
    "runtime_model_config",
    "strip_model_credentials",
]
