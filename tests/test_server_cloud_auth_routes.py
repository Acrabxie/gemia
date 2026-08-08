from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import server
from gemia import accounts, cloud_accounts
from tests_http_harness import create_raw_request, run_server_handler


def request(method: str, path: str, body: dict[str, Any] | None = None):
    headers = {"Content-Type": "application/json"} if body is not None else {}
    raw = create_raw_request(method, path, headers=headers, body=body)
    response = run_server_handler(server._Handler, raw)
    return response["status"], json.loads(response["body"].decode("utf-8"))


class FakeCloudClient:
    def __init__(self):
        self.account: dict[str, Any] | None = None
        self.logged_out = False
        self.current_account_calls = 0
        self.selected_secret = ""
        self.credential: dict[str, str] | None = None
        self.credential_uploads: list[tuple[str, str]] = []
        self.auxiliary_credentials: dict[str, dict[str, str]] = {}
        self.auxiliary_uploads: list[tuple[str, str]] = []

    def current_account(self, *, sync_credential: bool = True):
        self.current_account_calls += 1
        if self.account and sync_credential:
            self._set_credential_state()
        return self.account

    def _set_credential_state(self):
        if self.account:
            provider = str(self.account.get("provider") or "")
            self.credential = {
                "account_id": str(self.account.get("id") or ""),
                "cloud_provider": provider,
                "local_provider": {"anthropic": "claude"}.get(provider, provider),
                "config_field": {
                    "openai": "openai_api_key",
                    "anthropic": "anthropic_api_key",
                    "gemini": "gemini_api_key",
                    "openrouter": "openrouter_api_key",
                    "custom": "openai_api_key",
                }.get(provider, ""),
                "secret": self.selected_secret,
            }

    def sync_selected_credential(self, account):
        assert account is self.account
        self._set_credential_state()

    def credential_snapshot(self):
        return dict(self.credential) if self.credential else None

    def clear_credential(self):
        self.credential = None

    def clear_auxiliary_credentials(self):
        self.auxiliary_credentials = {}

    def put_auxiliary_credential(self, provider: str, secret: str):
        assert self.account is not None
        item = {
            "account_id": str(self.account["id"]),
            "provider": provider,
            "config_field": {
                "tavily": "tavily_api_key",
                "brave": "brave_api_key",
                "serper": "serper_api_key",
                "exa": "exa_api_key",
                "bing": "bing_api_key",
                "google_cse": "google_cse_key",
                "searxng": "searxng_api_key",
            }[provider],
            "secret": secret,
        }
        self.auxiliary_credentials[provider] = item
        self.auxiliary_uploads.append((provider, secret))
        return {"provider": f"search_{provider}", "label": "default"}

    def sync_auxiliary_credential(self, provider: str):
        return provider in self.auxiliary_credentials

    def auxiliary_credential_snapshot(self):
        return {key: dict(value) for key, value in self.auxiliary_credentials.items()}

    def put_selected_credential(self, provider: str, secret: str):
        assert self.account is not None
        assert provider == self.account["provider"]
        self.selected_secret = secret
        self.credential_uploads.append((provider, secret))
        self.current_account()
        return {"provider": provider, "label": "default"}

    def select_provider(self, provider: str):
        assert self.account is not None
        self.account["provider"] = provider
        self.account["provider_mode"] = (
            "managed" if provider in {"lumeri", "openai_subscription"} else "byok"
        )
        self.selected_secret = ""
        self._set_credential_state()
        return dict(self.account)

    def start_device_login(self, *, device_name: str, platform_name: str):
        return {
            "attempt_id": "local-attempt",
            "user_code": "ABCD-EFGH",
            "verification_uri": "https://accounts.lumeri.io/",
            "verification_uri_complete": "https://accounts.lumeri.io/?user_code=ABCD-EFGH",
            "expires_in": 600,
            "interval": 3,
        }

    def poll_device_login(self, attempt_id: str, *, sync_credential: bool = True):
        assert attempt_id == "local-attempt"
        self.account = {
            "id": "cloud-account-1",
            "email": "creator@example.com",
            "display_name": "Creator",
            "picture_url": "",
            "onboarding_completed": True,
            "age_band": "18_plus",
            "provider_mode": "byok",
            "provider": "openai",
        }
        if sync_credential:
            self.sync_selected_credential(self.account)
        return {"pending": False, "account": self.account}

    def logout(self):
        self.logged_out = True
        self.account = None
        self.credential = None

    def list_skill_artifacts(self, *, kind: str = ""):
        assert kind == ""
        return [
            {
                "kind": "skill",
                "id": "creative-brief",
                "version": "1.0.0",
                "title": "创作目标解析",
                "description": "把目标整理为可执行创作约束。",
                "visibility": "public",
                "access": "public",
                "publisher": "Lumeri",
                "content_sha256": "a" * 64,
                "step_count": 3,
            }
        ]


def test_cloud_device_login_activates_one_machine_workspace(monkeypatch, tmp_path: Path):
    root = tmp_path / "accounts"
    monkeypatch.setattr(accounts, "ACCOUNTS_ROOT", root)
    monkeypatch.setattr(accounts, "ACTIVE_ACCOUNT_PATH", root / "active.json")
    monkeypatch.setattr(cloud_accounts, "enabled", lambda: True)
    fake = FakeCloudClient()
    monkeypatch.setattr(cloud_accounts, "client", lambda: fake)

    status, payload = request("GET", "/auth/session")
    assert status == 200
    assert payload["account"] is None
    assert payload["cloud_login_enabled"] is True

    status, started = request("POST", "/auth/device/start", {})
    assert status == 200
    assert started["attempt_id"] == "local-attempt"
    assert "device_code" not in started

    status, completed = request("POST", "/auth/device/token", {"attempt_id": "local-attempt"})
    assert status == 200
    assert completed["account"]["email"] == "creator@example.com"
    assert completed["account"]["account_id"] == accounts.MACHINE_WORKSPACE_ID
    assert completed["account"]["cloud_account_id"] == "cloud-account-1"
    assert completed["account"]["onboarding_completed"] is True
    assert completed["account"]["age_band"] == "18_plus"
    assert completed["account"]["provider_mode"] == "byok"
    assert completed["account"]["model_provider"] == "openai"

    status, logged_out = request("POST", "/auth/logout", {})
    assert status == 200
    assert logged_out["account"] is None
    assert fake.logged_out is True


def _set_cloud_account(fake: FakeCloudClient, *, age_band: str, completed: bool = True) -> None:
    fake.account = {
        "id": "cloud-account-policy",
        "email": "policy@example.com",
        "display_name": "Policy",
        "picture_url": "",
        "onboarding_completed": completed,
        "age_band": age_band,
        "provider_mode": "managed" if age_band == "13_17" else "byok",
        "provider": "lumeri" if age_band == "13_17" else "openai",
    }


def test_cloud_workspace_routes_fail_closed_before_local_initialization(monkeypatch, tmp_path: Path):
    root = tmp_path / "accounts"
    monkeypatch.setattr(accounts, "ACCOUNTS_ROOT", root)
    monkeypatch.setattr(accounts, "ACTIVE_ACCOUNT_PATH", root / "active.json")
    monkeypatch.setattr(cloud_accounts, "enabled", lambda: True)
    fake = FakeCloudClient()
    monkeypatch.setattr(cloud_accounts, "client", lambda: fake)

    for method, path, body in (
        ("GET", "/sessions", None),
        ("GET", "/projects", None),
        ("GET", "/session-history", None),
        ("GET", "/media-library/list", None),
        ("GET", "/files/outputs", None),
        ("GET", "/file/outputs/example.mp4", None),
        ("GET", "/model", None),
        ("GET", "/agents", None),
        ("GET", "/skills", None),
        ("GET", "/skill-cloud/artifacts", None),
        ("GET", "/agent-links/status", None),
        ("GET", "/agent-links/messages", None),
        ("GET", "/runtime/dev/workspace/example", None),
        ("POST", "/sessions", {}),
        ("POST", "/model", {}),
        ("POST", "/runtime/dev/workspace", {}),
        ("POST", "/settings/sandbox", {"disabled": True}),
        ("POST", "/local-chat", {"message": "test"}),
    ):
        status, payload = request(method, path, body)
        assert status == 401, (method, path, payload)
        assert payload["error"] == "not signed in"


def test_skill_space_lists_account_and_public_cloud_guides(monkeypatch, tmp_path: Path):
    root = tmp_path / "accounts"
    monkeypatch.setattr(accounts, "ACCOUNTS_ROOT", root)
    monkeypatch.setattr(accounts, "ACTIVE_ACCOUNT_PATH", root / "active.json")
    monkeypatch.setattr(cloud_accounts, "enabled", lambda: True)
    fake = FakeCloudClient()
    _set_cloud_account(fake, age_band="18_plus")
    monkeypatch.setattr(cloud_accounts, "client", lambda: fake)

    assert request("GET", "/auth/session")[0] == 200
    status, payload = request("GET", "/skill-cloud/artifacts")

    assert status == 200
    assert payload["artifacts"][0]["title"] == "创作目标解析"
    assert payload["artifacts"][0]["access"] == "public"


def test_cloud_workspace_rejects_accounts_before_onboarding(monkeypatch, tmp_path: Path):
    root = tmp_path / "accounts"
    monkeypatch.setattr(accounts, "ACCOUNTS_ROOT", root)
    monkeypatch.setattr(accounts, "ACTIVE_ACCOUNT_PATH", root / "active.json")
    monkeypatch.setattr(cloud_accounts, "enabled", lambda: True)
    fake = FakeCloudClient()
    _set_cloud_account(fake, age_band="18_plus", completed=False)
    monkeypatch.setattr(cloud_accounts, "client", lambda: fake)

    # /auth/session is the one remote validation point and writes the local
    # fail-closed profile used by subsequent low-latency workspace requests.
    status, session = request("GET", "/auth/session")
    assert status == 200
    assert session["account"]["onboarding_completed"] is False

    for method, path, body in (
        ("GET", "/sessions", None),
        ("GET", "/media-library/list", None),
        ("POST", "/sessions", {}),
    ):
        status, payload = request(method, path, body)
        assert status == 403, (method, path, payload)
        assert payload["error"] == "account onboarding required"


def test_cloud_teen_account_can_switch_credits_and_subscription_but_not_byok(monkeypatch, tmp_path: Path):
    root = tmp_path / "accounts"
    monkeypatch.setattr(accounts, "ACCOUNTS_ROOT", root)
    monkeypatch.setattr(accounts, "ACTIVE_ACCOUNT_PATH", root / "active.json")
    monkeypatch.setattr(cloud_accounts, "enabled", lambda: True)
    fake = FakeCloudClient()
    _set_cloud_account(fake, age_band="13_17")
    monkeypatch.setattr(cloud_accounts, "client", lambda: fake)

    status, session = request("GET", "/auth/session")
    assert status == 200
    assert session["account"]["age_band"] == "13_17"

    status, config = request("GET", "/config")
    assert status == 200
    assert config["byok_allowed"] is False
    assert config["allowed_providers"] == ["lumeri", "openai_subscription"]
    assert [provider["id"] for provider in config["brain"]["providers"]] == [
        "lumeri",
        "openai_subscription",
    ]

    status, payload = request("POST", "/config", {"provider": "openai"})
    assert status == 403
    assert payload["error"] == "provider is not available for this account"

    status, payload = request(
        "POST",
        "/config",
        {"provider": "openai_subscription", "model": "gpt-5.6-sol"},
    )
    assert status == 200
    assert payload["selected_provider"] == "openai_subscription"
    assert fake.account["provider"] == "openai_subscription"


def test_cloud_byok_requires_account_mode_and_matching_provider(monkeypatch, tmp_path: Path):
    root = tmp_path / "accounts"
    monkeypatch.setattr(accounts, "ACCOUNTS_ROOT", root)
    monkeypatch.setattr(accounts, "ACTIVE_ACCOUNT_PATH", root / "active.json")
    monkeypatch.setattr(cloud_accounts, "enabled", lambda: True)
    fake = FakeCloudClient()
    _set_cloud_account(fake, age_band="18_plus")
    monkeypatch.setattr(cloud_accounts, "client", lambda: fake)

    status, _ = request("GET", "/auth/session")
    assert status == 200

    status, payload = request("POST", "/config", {"provider": "claude"})
    assert status == 200
    assert payload["selected_provider"] == "claude"
    assert fake.account["provider"] == "anthropic"

    fake.account["provider_mode"] = "managed"
    fake.account["provider"] = "lumeri"
    status, _ = request("GET", "/auth/session")
    assert status == 200
    status, payload = request("POST", "/config", {"provider": "openai"})
    assert status == 200
    assert payload["selected_provider"] == "openai"

    assert server._normalized_byok_provider("anthropic") == "claude"
    assert server._normalized_byok_provider("claude") == "claude"


def test_workspace_requests_do_not_add_cloud_round_trips_after_boot(monkeypatch, tmp_path: Path):
    root = tmp_path / "accounts"
    monkeypatch.setattr(accounts, "ACCOUNTS_ROOT", root)
    monkeypatch.setattr(accounts, "ACTIVE_ACCOUNT_PATH", root / "active.json")
    monkeypatch.setattr(cloud_accounts, "enabled", lambda: True)
    fake = FakeCloudClient()
    _set_cloud_account(fake, age_band="18_plus")
    monkeypatch.setattr(cloud_accounts, "client", lambda: fake)

    status, _ = request("GET", "/auth/session")
    assert status == 200
    assert fake.current_account_calls == 1

    def unexpected_cloud_call():
        raise AssertionError("workspace route called the cloud account service")

    fake.current_account = unexpected_cloud_call  # type: ignore[method-assign]
    status, payload = request("GET", "/settings/sandbox")
    assert status == 200
    assert "sandbox_disabled" in payload


def test_persisted_cloud_profile_and_internal_api_require_process_validation(monkeypatch, tmp_path: Path):
    root = tmp_path / "accounts"
    monkeypatch.setattr(accounts, "ACCOUNTS_ROOT", root)
    monkeypatch.setattr(accounts, "ACTIVE_ACCOUNT_PATH", root / "active.json")
    monkeypatch.setattr(cloud_accounts, "enabled", lambda: True)
    fake = FakeCloudClient()
    _set_cloud_account(fake, age_band="18_plus")
    monkeypatch.setattr(cloud_accounts, "client", lambda: fake)

    accounts.activate_cloud_account(fake.account or {})
    server._clear_cloud_account_validation()
    status, payload = request("GET", "/api/internal/v1/capabilities")
    assert status == 401
    assert payload["error"] == "cloud session not validated"
    status, payload = request(
        "POST",
        "/api/internal/v1/sessions/missing/capabilities/inspect_timeline:invoke",
        {"arguments": {}},
    )
    assert status == 401
    assert payload["error"] == "cloud session not validated"

    status, _ = request("GET", "/auth/session")
    assert status == 200
    status, payload = request("GET", "/api/internal/v1/capabilities")
    assert status == 200
    assert payload["api_version"] == 1


def test_cloud_config_uploads_secret_without_persisting_it(monkeypatch, tmp_path: Path):
    root = tmp_path / "accounts"
    config_path = tmp_path / "config.json"
    root.mkdir()
    config_path.write_text(
        json.dumps(
            {
                "openai_api_key": "stale-flat-secret",
                "image_api_key": "stale-image-secret",
                "brain_provider_profiles": {
                    "openai": {"provider": "openai", "api_key": "stale-profile-secret"}
                },
            }
        )
    )
    monkeypatch.setattr(accounts, "ACCOUNTS_ROOT", root)
    monkeypatch.setattr(accounts, "ACTIVE_ACCOUNT_PATH", root / "active.json")
    monkeypatch.setattr(server, "_CONFIG_PATH", config_path)
    monkeypatch.setattr(cloud_accounts, "enabled", lambda: True)
    fake = FakeCloudClient()
    _set_cloud_account(fake, age_band="18_plus")
    monkeypatch.setattr(cloud_accounts, "client", lambda: fake)

    assert request("GET", "/auth/session")[0] == 200
    secret = "new-account-bound-secret"
    status, payload = request(
        "POST",
        "/config",
        {
            "provider": "openai",
            "model": "gpt-5.6-sol",
            "openai_api_key": secret,
        },
    )
    assert status == 200
    assert payload == {
        "ok": True,
        "selected_provider": "openai",
        "provider_mode": "byok",
    }
    assert fake.credential_uploads == [("openai", secret)]
    persisted = config_path.read_text()
    assert secret not in persisted
    assert "stale-flat-secret" not in persisted
    assert "stale-profile-secret" not in persisted
    assert "stale-image-secret" not in persisted
    config = json.loads(persisted)
    assert config["cloud_account_model_profiles"]["cloud-account-policy"]["model"] == "gpt-5.6-sol"


def test_cloud_config_rejects_cross_profile_and_machine_global_auxiliary_keys_but_saves_search(
    monkeypatch, tmp_path: Path
):
    root = tmp_path / "accounts"
    monkeypatch.setattr(accounts, "ACCOUNTS_ROOT", root)
    monkeypatch.setattr(accounts, "ACTIVE_ACCOUNT_PATH", root / "active.json")
    monkeypatch.setattr(server, "_CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(cloud_accounts, "enabled", lambda: True)
    fake = FakeCloudClient()
    _set_cloud_account(fake, age_band="18_plus")
    monkeypatch.setattr(cloud_accounts, "client", lambda: fake)
    assert request("GET", "/auth/session")[0] == 200

    status, payload = request(
        "POST",
        "/config",
        {"profile_id": "claude", "anthropic_api_key": "wrong-provider-secret"},
    )
    assert status == 403
    assert "match selected provider" in payload["error"]
    status, payload = request("POST", "/config", {"image_api_key": "image-secret"})
    assert status == 403
    assert "auxiliary credentials" in payload["error"]
    status, payload = request(
        "POST", "/config", {"search_provider": "tavily", "brave_api_key": "wrong-search-secret"}
    )
    assert status == 403
    assert "does not match" in payload["error"]

    status, payload = request(
        "POST", "/config", {"search_provider": "tavily", "tavily_api_key": "search-secret"}
    )
    assert status == 200
    assert payload["ok"] is True
    assert fake.credential_uploads == []
    assert fake.auxiliary_uploads == [("tavily", "search-secret")]
    on_disk = json.loads((tmp_path / "config.json").read_text())
    assert on_disk["search_provider"] == "tavily"
    assert "tavily_api_key" not in on_disk
    assert "search-secret" not in (tmp_path / "config.json").read_text()
    status, config_payload = request("GET", "/config")
    assert status == 200
    assert config_payload["search"]["provider"] == "tavily"
    assert config_payload["search"]["has_key"]["tavily"] is True
    assert "tavily" in config_payload["search"]["allowed_providers"]


def test_cloud_search_credentials_are_available_to_teen_accounts(
    monkeypatch, tmp_path: Path
):
    root = tmp_path / "accounts"
    monkeypatch.setattr(accounts, "ACCOUNTS_ROOT", root)
    monkeypatch.setattr(accounts, "ACTIVE_ACCOUNT_PATH", root / "active.json")
    monkeypatch.setattr(server, "_CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(cloud_accounts, "enabled", lambda: True)
    fake = FakeCloudClient()
    _set_cloud_account(fake, age_band="13_17")
    monkeypatch.setattr(cloud_accounts, "client", lambda: fake)
    assert request("GET", "/auth/session")[0] == 200

    status, config_payload = request("GET", "/config")
    assert status == 200
    assert "tavily" in config_payload["search"]["allowed_providers"]
    status, payload = request(
        "POST",
        "/config",
        {"search_provider": "tavily", "tavily_api_key": "teen-search-secret"},
    )
    assert status == 200
    assert payload["ok"] is True
    assert fake.auxiliary_uploads == [("tavily", "teen-search-secret")]
    on_disk = (tmp_path / "config.json").read_text()
    assert "teen-search-secret" not in on_disk
    assert "tavily_api_key" not in on_disk


def test_cloud_model_probes_ignore_global_secrets_proxy_and_fixed_provider_base(
    monkeypatch, tmp_path: Path
):
    from gemia import brain_config

    root = tmp_path / "accounts"
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "proxy": "http://127.0.0.1:65530",
                "openai_api_key": "stale-file-secret",
                "brain_provider_profiles": {
                    "openai": {
                        "provider": "openai",
                        "api_key": "stale-profile-secret",
                        "base_url": "https://poison.example/v1/chat/completions",
                    }
                },
            }
        )
    )
    monkeypatch.setattr(accounts, "ACCOUNTS_ROOT", root)
    monkeypatch.setattr(accounts, "ACTIVE_ACCOUNT_PATH", root / "active.json")
    monkeypatch.setattr(server, "_CONFIG_PATH", config_path)
    monkeypatch.setattr(cloud_accounts, "enabled", lambda: True)
    fake = FakeCloudClient()
    _set_cloud_account(fake, age_band="18_plus")
    fake.selected_secret = "cloud-selected-secret"
    monkeypatch.setattr(cloud_accounts, "client", lambda: fake)
    monkeypatch.setenv("OPENAI_API_KEY", "stale-env-secret")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:65531")
    monkeypatch.setenv("LUMERI_OPENAI_BASE_URL", "https://poison-env.example/v1/chat/completions")
    captured: list[tuple[str, dict[str, Any], str | None]] = []

    def capture_models(provider, config, proxy=None):
        captured.append((provider, dict(config), proxy))
        return {"ok": True, "models": []}

    def capture_test(*, proxy=None, config=None):
        captured.append(("test", dict(config or {}), proxy))
        return {"ok": True}

    monkeypatch.setattr(brain_config, "list_models", capture_models)
    monkeypatch.setattr(brain_config, "test_provider", capture_test)

    assert request("GET", "/auth/session")[0] == 200
    status, payload = request(
        "POST",
        "/config/list-models",
        {
            "provider": "openai",
            "base_url": "https://poison-request.example/v1/chat/completions",
        },
    )
    assert status == 200
    assert payload["ok"] is True
    status, payload = request(
        "POST",
        "/config/test-brain",
        {
            "provider": "openai",
            "base_url": "https://poison-request.example/v1/chat/completions",
        },
    )
    assert status == 200
    assert payload["ok"] is True

    assert len(captured) == 2
    for _provider, runtime_config, proxy in captured:
        resolved = brain_config.resolve_runtime_config(runtime_config)
        assert proxy is None
        assert resolved["openai_api_key"] == "cloud-selected-secret"
        assert resolved.get("proxy") in (None, "")
        assert resolved.get("lumeri_openai_base_url") in (None, "")
        assert "stale-file-secret" not in json.dumps(runtime_config)
        assert "stale-profile-secret" not in json.dumps(runtime_config)
        assert "poison" not in json.dumps(runtime_config)
    assert os.environ["OPENAI_API_KEY"] == "stale-env-secret"


def test_cloud_get_config_lists_switchable_providers_for_adult_managed_account(monkeypatch, tmp_path: Path):
    root = tmp_path / "accounts"
    monkeypatch.setattr(accounts, "ACCOUNTS_ROOT", root)
    monkeypatch.setattr(accounts, "ACTIVE_ACCOUNT_PATH", root / "active.json")
    monkeypatch.setattr(cloud_accounts, "enabled", lambda: True)
    fake = FakeCloudClient()
    _set_cloud_account(fake, age_band="18_plus")
    fake.account["provider_mode"] = "managed"
    fake.account["provider"] = "lumeri"
    monkeypatch.setattr(cloud_accounts, "client", lambda: fake)

    assert request("GET", "/auth/session")[0] == 200
    status, payload = request("GET", "/config")
    assert status == 200
    assert payload["byok_allowed"] is True
    assert payload["selected_provider"] == "lumeri"
    assert payload["allowed_providers"] == [
        "lumeri",
        "openai_subscription",
        "openai",
        "claude",
        "gemini",
        "openrouter",
        "custom",
    ]
    assert payload["brain"]["providers"][0]["id"] == "lumeri"


def test_subscription_connection_test_uses_panel_provider_without_switching_account(
    monkeypatch, tmp_path: Path
):
    from gemia import brain_config

    root = tmp_path / "accounts"
    monkeypatch.setattr(accounts, "ACCOUNTS_ROOT", root)
    monkeypatch.setattr(accounts, "ACTIVE_ACCOUNT_PATH", root / "active.json")
    monkeypatch.setattr(cloud_accounts, "enabled", lambda: True)
    fake = FakeCloudClient()
    _set_cloud_account(fake, age_band="13_17")
    monkeypatch.setattr(cloud_accounts, "client", lambda: fake)
    captured: list[dict[str, Any]] = []

    def capture_test(**kwargs):
        captured.append(kwargs)
        return {"ok": True, "provider": "openai", "model": "gpt-5.6-sol", "sample": "ok"}

    monkeypatch.setattr(brain_config, "test_provider", capture_test)

    assert request("GET", "/auth/session")[0] == 200
    status, payload = request(
        "POST",
        "/config/test-brain",
        {"provider": "openai_subscription", "model": "gpt-5.6-sol"},
    )

    assert status == 200
    assert payload["ok"] is True
    assert captured[0]["provider_override"] == "openai_subscription"
    assert fake.account["provider"] == "lumeri"


def test_cloud_main_skips_legacy_first_run_and_machine_key_loading(monkeypatch):
    from gemia import onboarding

    class FakeServer:
        daemon_threads = False

        def __init__(self, address, handler):
            self.address = address
            self.handler = handler

        def serve_forever(self):
            raise KeyboardInterrupt

    monkeypatch.setattr(cloud_accounts, "enabled", lambda: True)
    monkeypatch.setattr(
        onboarding,
        "ensure_onboarded",
        lambda: (_ for _ in ()).throw(AssertionError("ran legacy first-run onboarding")),
    )
    monkeypatch.setattr(
        server,
        "_load_config_keys",
        lambda: (_ for _ in ()).throw(AssertionError("loaded legacy model keys")),
    )
    monkeypatch.setattr(server, "ThreadingHTTPServer", FakeServer)

    server.main(host="127.0.0.1", port=7799)


def test_cloud_health_does_not_inspect_or_report_machine_credentials(monkeypatch):
    monkeypatch.setattr(cloud_accounts, "enabled", lambda: True)
    monkeypatch.setattr(
        server,
        "_has_valid_key",
        lambda: (_ for _ in ()).throw(AssertionError("inspected machine text key")),
    )
    monkeypatch.setattr(
        server,
        "_has_valid_image_key",
        lambda: (_ for _ in ()).throw(AssertionError("inspected machine media key")),
    )

    payload = server._health_payload()
    checks = {item["name"]: item for item in payload["checks"]}

    assert "config.cloud_account" in checks
    assert "config.openrouter" not in checks
    assert "config.image" not in checks


def test_cloud_local_chat_does_not_use_subscription_bridge(monkeypatch, tmp_path: Path):
    root = tmp_path / "accounts"
    monkeypatch.setattr(accounts, "ACCOUNTS_ROOT", root)
    monkeypatch.setattr(accounts, "ACTIVE_ACCOUNT_PATH", root / "active.json")
    monkeypatch.setattr(cloud_accounts, "enabled", lambda: True)
    fake = FakeCloudClient()
    _set_cloud_account(fake, age_band="18_plus")
    monkeypatch.setattr(cloud_accounts, "client", lambda: fake)

    assert request("GET", "/auth/session")[0] == 200
    status, payload = request("POST", "/local-chat", {"message": "hello"})
    assert status == 403
    assert payload["error"] == "online managed creation is not available yet"


def test_cloud_account_change_or_logout_closes_live_runners_but_same_account_does_not(monkeypatch, tmp_path: Path):
    root = tmp_path / "accounts"
    monkeypatch.setattr(accounts, "ACCOUNTS_ROOT", root)
    monkeypatch.setattr(accounts, "ACTIVE_ACCOUNT_PATH", root / "active.json")
    monkeypatch.setattr(cloud_accounts, "enabled", lambda: True)
    fake = FakeCloudClient()
    _set_cloud_account(fake, age_band="18_plus")
    monkeypatch.setattr(cloud_accounts, "client", lambda: fake)
    closed: list[str] = []
    monkeypatch.setattr(server, "_close_live_cloud_sessions", lambda: closed.append("closed"))

    assert request("GET", "/auth/session")[0] == 200
    assert request("GET", "/auth/session")[0] == 200
    assert closed == []

    fake.account["id"] = "different-cloud-account"
    assert request("GET", "/auth/session")[0] == 200
    assert closed == ["closed"]

    assert request("POST", "/auth/logout", {})[0] == 200
    assert closed == ["closed", "closed"]


def test_close_live_cloud_sessions_preserves_durable_workdirs(monkeypatch):
    from gemia import session_manager

    calls: list[tuple[str, bool]] = []

    class Manager:
        def list_sessions(self):
            return ["session-a", "session-b"]

        def close_session(self, session_id, *, remove_workdir=False):
            calls.append((session_id, remove_workdir))

    monkeypatch.setattr(session_manager, "get_manager", lambda: Manager())
    server._close_live_cloud_sessions()
    assert calls == [("session-a", False), ("session-b", False)]
