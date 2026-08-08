from __future__ import annotations

import os
from typing import Any

import pytest

from gemia.cloud_accounts import (
    CLIENT_USER_AGENT,
    CloudAccountClient,
    CloudAuthError,
    HttpTransport,
)


class MemoryStore:
    def __init__(self):
        self.value: str | None = None

    def get(self) -> str | None:
        return self.value

    def set(self, token: str) -> None:
        self.value = token

    def delete(self) -> None:
        self.value = None


class FakeTransport:
    def __init__(self):
        self.calls: list[tuple[str, str, dict[str, Any] | None, str]] = []
        self.approved = False

    def request(self, method, path, *, payload=None, access_token=""):
        self.calls.append((method, path, payload, access_token))
        if path == "/v1/device/authorizations":
            return 201, {
                "device_code": "dc_super_secret_device_code",
                "user_code": "ABCD-EFGH",
                "verification_uri": "https://accounts.lumeri.io/",
                "verification_uri_complete": "https://accounts.lumeri.io/?user_code=ABCD-EFGH",
                "expires_in": 600,
                "interval": 3,
            }
        if path == "/v1/device/token" and not self.approved:
            return 428, {"error": "authorization_pending", "message": "pending"}
        if path == "/v1/device/token":
            return 200, _tokens("access-one", "refresh-one")
        if path == "/v1/me" and access_token == "access-one":
            return 200, _account()
        if path == "/v1/auth/refresh":
            return 200, _tokens("access-two", "refresh-two")
        if path == "/v1/logout":
            return 200, {"ok": True}
        return 404, {"error": "not_found"}


class FakeResponse:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return b"{}"


def _account():
    return {
        "id": "cloud-account-1",
        "email": "creator@example.com",
        "display_name": "Creator",
        "picture_url": "",
    }


def _tokens(access: str, refresh: str):
    return {
        "access_token": access,
        "refresh_token": refresh,
        "expires_in": 900,
        "account": _account(),
    }


def test_device_code_never_returns_to_browser_and_refresh_uses_secure_store():
    transport = FakeTransport()
    store = MemoryStore()
    client = CloudAccountClient(transport, store)

    started = client.start_device_login(device_name="Mac", platform_name="macOS")
    assert "device_code" not in started
    assert started["user_code"] == "ABCD-EFGH"
    assert client.poll_device_login(started["attempt_id"])["pending"] is True

    transport.approved = True
    completed = client.poll_device_login(started["attempt_id"])
    assert completed["account"]["id"] == "cloud-account-1"
    assert store.value == "refresh-one"
    assert all("dc_super_secret_device_code" not in str(call[2]) for call in transport.calls[:-2])


def test_existing_refresh_token_rotates_and_logout_clears_store():
    transport = FakeTransport()
    store = MemoryStore()
    store.value = "refresh-old"
    client = CloudAccountClient(transport, store)

    assert client.current_account()["email"] == "creator@example.com"
    assert store.value == "refresh-two"
    client.logout()
    assert store.value is None


def test_http_transport_identifies_the_lumeri_client(monkeypatch):
    captured = {}

    def fake_urlopen(request, *, timeout):
        captured["user_agent"] = request.get_header("User-agent")
        captured["accept"] = request.get_header("Accept")
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    status, payload = HttpTransport(timeout=9).request("GET", "/v1/config")

    assert status == 200
    assert payload == {}
    assert captured == {
        "user_agent": CLIENT_USER_AGENT,
        "accept": "application/json",
        "timeout": 9,
    }


def test_missing_remote_route_is_reported_as_account_service_contract_error():
    with pytest.raises(CloudAuthError) as exc_info:
        CloudAccountClient._raise({"detail": "Not Found"}, 404)

    assert exc_info.value.code == "account_service_contract_missing"
    assert str(exc_info.value) == "Lumeri 账户服务尚未部署当前客户端所需接口"


class CredentialTransport:
    def __init__(self, *, provider: str = "openai", selected=None):
        self.calls: list[tuple[str, str, dict[str, Any] | None, str]] = []
        self.account = {
            "id": "credential-account",
            "email": "adult@example.com",
            "display_name": "Adult",
            "picture_url": "",
            "onboarding_completed": True,
            "age_band": "18_plus",
            "provider_mode": "byok",
            "provider": provider,
        }
        self.selected = selected
        self.auxiliary: dict[str, str] = {}

    def request(self, method, path, *, payload=None, access_token=""):
        self.calls.append((method, path, payload, access_token))
        if path == "/v1/device/authorizations":
            return 201, {
                "device_code": "credential-device-code-long-enough",
                "user_code": "ABCD-EFGH",
                "verification_uri": "https://accounts.lumeri.io/",
                "verification_uri_complete": "https://accounts.lumeri.io/?user_code=ABCD-EFGH",
                "expires_in": 600,
                "interval": 3,
            }
        if path == "/v1/device/token":
            return 200, {
                "access_token": "credential-access",
                "refresh_token": "credential-refresh",
                "account": dict(self.account),
            }
        if path == "/v1/auth/refresh":
            return 200, {
                "access_token": "credential-access",
                "refresh_token": "credential-refresh",
                "account": dict(self.account),
            }
        if path == "/v1/me":
            return 200, dict(self.account)
        if path == "/v1/credentials/selected":
            if self.selected is None:
                return 404, {
                    "error": "not_found",
                    "message": "selected BYOK credential is not configured",
                }
            return 200, dict(self.selected)
        if path.startswith("/v1/credentials/auxiliary/"):
            provider = path.rsplit("/", 1)[-1]
            if method == "PUT":
                self.auxiliary[provider] = str((payload or {}).get("secret") or "")
                return 200, {
                    "id": "auxiliary-credential",
                    "provider": f"search_{provider}",
                    "label": "default",
                }
            if method == "POST":
                secret = self.auxiliary.get(provider, "")
                if not secret:
                    return 404, {
                        "error": "not_found",
                        "message": "auxiliary credential is not configured",
                    }
                return 200, {
                    "provider": provider,
                    "label": "default",
                    "secret": secret,
                }
        if path == "/v1/provider-selection" and method == "PUT":
            provider = str((payload or {}).get("provider") or "")
            self.account["provider"] = provider
            self.account["provider_mode"] = (
                "managed" if provider in {"lumeri", "openai_subscription"} else "byok"
            )
            self.selected = None
            return 200, dict(self.account)
        if path.startswith("/v1/credentials/") and method == "PUT":
            provider = path.rsplit("/", 1)[-1]
            return 200, {"id": "credential", "provider": provider, "label": "default"}
        if path == "/v1/logout":
            return 200, {"ok": True}
        return 404, {"error": "not_found", "message": "not found"}


def test_selected_credential_is_fetched_on_login_and_bound_to_account():
    transport = CredentialTransport(
        provider="anthropic",
        selected={"provider": "anthropic", "label": "default", "secret": "account-secret"},
    )
    store = MemoryStore()
    store.value = "refresh-existing"
    client = CloudAccountClient(transport, store)

    assert client.current_account()["id"] == "credential-account"
    assert client.credential_snapshot() == {
        "account_id": "credential-account",
        "cloud_provider": "anthropic",
        "local_provider": "claude",
        "config_field": "anthropic_api_key",
        "secret": "account-secret",
    }
    assert ("POST", "/v1/credentials/selected", {}, "credential-access") in transport.calls


def test_selected_credential_is_fetched_when_device_login_completes():
    transport = CredentialTransport(
        provider="openrouter",
        selected={"provider": "openrouter", "label": "default", "secret": "device-secret"},
    )
    store = MemoryStore()
    client = CloudAccountClient(transport, store)

    started = client.start_device_login(device_name="Mac", platform_name="macOS")
    completed = client.poll_device_login(started["attempt_id"])
    assert completed["pending"] is False
    assert client.credential_snapshot()["secret"] == "device-secret"
    assert store.value == "credential-refresh"


def test_missing_selected_credential_keeps_login_valid_but_keyless():
    transport = CredentialTransport(provider="gemini", selected=None)
    store = MemoryStore()
    store.value = "refresh-existing"
    client = CloudAccountClient(transport, store)

    assert client.current_account()["provider"] == "gemini"
    assert client.credential_snapshot() == {
        "account_id": "credential-account",
        "cloud_provider": "gemini",
        "local_provider": "gemini",
        "config_field": "gemini_api_key",
        "secret": "",
    }


def test_logout_clears_only_client_snapshot_and_never_restores_legacy_env(monkeypatch):
    transport = CredentialTransport(
        selected={"provider": "openai", "label": "default", "secret": "cloud-secret"}
    )
    store = MemoryStore()
    store.value = "refresh-existing"
    monkeypatch.setenv("OPENAI_API_KEY", "legacy-machine-secret")
    client = CloudAccountClient(transport, store)

    client.current_account()
    assert client.credential_snapshot()["secret"] == "cloud-secret"
    assert os.environ["OPENAI_API_KEY"] == "legacy-machine-secret"
    client.logout()
    assert client.credential_snapshot() is None
    assert store.value is None
    assert os.environ["OPENAI_API_KEY"] == "legacy-machine-secret"


def test_selected_credential_response_cannot_cross_provider():
    transport = CredentialTransport(
        provider="openai",
        selected={"provider": "anthropic", "label": "default", "secret": "wrong-secret"},
    )
    store = MemoryStore()
    store.value = "refresh-existing"
    client = CloudAccountClient(transport, store)

    with pytest.raises(CloudAuthError, match="不匹配"):
        client.current_account()
    assert client.credential_snapshot() is None


def test_account_change_replaces_old_secret_with_keyless_new_context():
    transport = CredentialTransport(
        selected={"provider": "openai", "label": "default", "secret": "first-secret"}
    )
    store = MemoryStore()
    store.value = "refresh-existing"
    client = CloudAccountClient(transport, store)
    client.current_account()
    assert client.credential_snapshot()["secret"] == "first-secret"

    transport.account["id"] = "second-account"
    transport.selected = None
    assert client.current_account()["id"] == "second-account"
    assert client.credential_snapshot()["account_id"] == "second-account"
    assert client.credential_snapshot()["secret"] == ""


def test_credential_fetch_rejects_account_change_during_access_refresh():
    class SwitchingTransport(CredentialTransport):
        def request(self, method, path, *, payload=None, access_token=""):
            if path == "/v1/credentials/selected" and access_token == "credential-access":
                self.calls.append((method, path, payload, access_token))
                return 401, {"error": "unauthorized"}
            if path == "/v1/auth/refresh":
                self.calls.append((method, path, payload, access_token))
                self.account["id"] = "switched-account"
                return 200, {
                    "access_token": "switched-access",
                    "refresh_token": "switched-refresh",
                    "account": dict(self.account),
                }
            return super().request(
                method, path, payload=payload, access_token=access_token
            )

    transport = SwitchingTransport(
        selected={"provider": "openai", "label": "default", "secret": "wrong-account-secret"}
    )
    client = CloudAccountClient(transport, MemoryStore())
    started = client.start_device_login(device_name="Mac", platform_name="macOS")

    with pytest.raises(CloudAuthError) as exc_info:
        client.poll_device_login(started["attempt_id"])

    assert exc_info.value.code == "account_changed"
    assert client.credential_snapshot() is None


@pytest.mark.parametrize(
    ("provider", "config_field", "local_provider"),
    [
        ("openai", "openai_api_key", "openai"),
        ("anthropic", "anthropic_api_key", "claude"),
        ("gemini", "gemini_api_key", "gemini"),
        ("openrouter", "openrouter_api_key", "openrouter"),
        ("custom", "openai_api_key", "custom"),
    ],
)
def test_put_selected_credential_uses_cloud_provider_mapping(provider, config_field, local_provider):
    transport = CredentialTransport(provider=provider, selected=None)
    store = MemoryStore()
    store.value = "refresh-existing"
    client = CloudAccountClient(transport, store)
    client.current_account()

    client.put_selected_credential(provider, "new-secret")
    assert transport.calls[-1][0:3] == (
        "PUT",
        f"/v1/credentials/{provider}",
        {"label": "default", "secret": "new-secret"},
    )
    assert client.credential_snapshot()["config_field"] == config_field
    assert client.credential_snapshot()["local_provider"] == local_provider


def test_auxiliary_search_credential_round_trip_stays_account_bound_in_memory():
    transport = CredentialTransport(provider="openai_subscription", selected=None)
    transport.account["provider_mode"] = "managed"
    store = MemoryStore()
    store.value = "refresh-existing"
    client = CloudAccountClient(transport, store)
    client.current_account()

    client.put_auxiliary_credential("tavily", "search-secret")
    assert client.auxiliary_credential_snapshot()["tavily"] == {
        "account_id": "credential-account",
        "provider": "tavily",
        "config_field": "tavily_api_key",
        "secret": "search-secret",
    }
    client.clear_auxiliary_credentials()
    assert client.sync_auxiliary_credential("tavily") is True
    assert client.auxiliary_credential_snapshot()["tavily"]["secret"] == "search-secret"


def test_auxiliary_search_credential_missing_and_teen_account_supported():
    transport = CredentialTransport(provider="lumeri", selected=None)
    transport.account["provider_mode"] = "managed"
    store = MemoryStore()
    store.value = "refresh-existing"
    client = CloudAccountClient(transport, store)
    client.current_account()

    assert client.sync_auxiliary_credential("brave") is False
    transport.account["age_band"] = "13_17"
    client.current_account(sync_credential=False)
    client.put_auxiliary_credential("brave", "teen-secret")
    assert client.auxiliary_credential_snapshot()["brave"]["secret"] == "teen-secret"


@pytest.mark.parametrize(
    ("provider", "mode"),
    [
        ("lumeri", "managed"),
        ("openai_subscription", "managed"),
        ("anthropic", "byok"),
    ],
)
def test_select_provider_updates_account_and_account_bound_context(provider, mode):
    transport = CredentialTransport(provider="lumeri", selected=None)
    transport.account["provider_mode"] = "managed"
    store = MemoryStore()
    store.value = "refresh-existing"
    client = CloudAccountClient(transport, store)
    client.current_account()

    account = client.select_provider(provider)

    assert account["provider"] == provider
    assert account["provider_mode"] == mode
    assert (
        "PUT",
        "/v1/provider-selection",
        {"provider": provider},
    ) in [call[0:3] for call in transport.calls]
    snapshot = client.credential_snapshot()
    assert snapshot is not None
    assert snapshot["cloud_provider"] == provider
    assert snapshot["secret"] == ""
