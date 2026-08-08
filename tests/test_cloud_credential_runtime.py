from __future__ import annotations

import pytest

from gemia import brain_config, cloud_accounts
from gemia.ai.audio_client import AudioClient
from gemia.ai.gemini_adapter import GeminiAdapter
from gemia.ai.generative_client import GenerativeClient
from gemia.ai.google_genai_client import GoogleGenAIClient, VertexAuthMissingError
from gemia.ai.veo_client import VeoClient
from gemia.automation.gemini_media import GeminiMediaClient
from gemia.gemini_client import GeminiClientV3
from gemia.tools import web_search


class SnapshotClient:
    def __init__(self, snapshot=None, auxiliary=None):
        self.snapshot = snapshot
        self.auxiliary = auxiliary or {}

    def credential_snapshot(self):
        return dict(self.snapshot) if self.snapshot else None

    def auxiliary_credential_snapshot(self):
        return {key: dict(value) for key, value in self.auxiliary.items()}


def _snapshot(provider: str, local_provider: str, field: str, secret: str = "cloud-secret"):
    return {
        "account_id": "cloud-account",
        "cloud_provider": provider,
        "local_provider": local_provider,
        "config_field": field,
        "secret": secret,
    }


def test_cloud_mode_never_falls_back_to_stale_text_image_video_audio_keys(monkeypatch, tmp_path):
    monkeypatch.setattr(cloud_accounts, "enabled", lambda: True)
    monkeypatch.setattr(cloud_accounts, "client", lambda: SnapshotClient())
    monkeypatch.setenv("OPENAI_API_KEY", "stale-openai")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "stale-anthropic")
    monkeypatch.setenv("OPENROUTER_API_KEY", "stale-openrouter")
    monkeypatch.setenv("GEMINI_API_KEY", "stale-gemini")
    stale_config = {
        "lumeri_v3_provider": "openai",
        "openai_api_key": "stale-file-openai",
        "openrouter_api_key": "stale-file-openrouter",
        "gemini_api_key": "stale-file-gemini",
        "anthropic_api_key": "stale-file-anthropic",
    }

    with pytest.raises(RuntimeError):
        GeminiClientV3(config=stale_config)
    with pytest.raises(RuntimeError):
        GeminiAdapter(log_dir=tmp_path / "logs")
    with pytest.raises(RuntimeError):
        GenerativeClient()
    with pytest.raises(RuntimeError):
        VeoClient()
    with pytest.raises(RuntimeError):
        AudioClient()
    with pytest.raises(RuntimeError):
        GeminiMediaClient(api_key="stale-explicit-key")
    with pytest.raises(VertexAuthMissingError, match="unavailable"):
        GoogleGenAIClient()


def test_cloud_text_resolver_failure_never_uses_stale_machine_config(monkeypatch):
    monkeypatch.setattr(cloud_accounts, "enabled", lambda: True)
    monkeypatch.setattr(
        cloud_accounts,
        "runtime_model_config",
        lambda _config: (_ for _ in ()).throw(RuntimeError("resolver offline")),
    )
    monkeypatch.setenv("OPENROUTER_API_KEY", "stale-env-secret")

    with pytest.raises(RuntimeError, match="credential resolver is unavailable"):
        GeminiClientV3(
            config={
                "lumeri_v3_provider": "openrouter",
                "openrouter_api_key": "stale-file-secret",
            }
        )


def test_cloud_openai_subscription_uses_local_bridge_without_api_key(monkeypatch):
    from gemia.brain_config import OPENAI_SUBSCRIPTION_BASE_URL

    monkeypatch.setattr(cloud_accounts, "enabled", lambda: True)
    subscription = SnapshotClient(
        {
            "account_id": "teen-cloud-account",
            "cloud_provider": "openai_subscription",
            "local_provider": "openai_subscription",
            "config_field": "",
            "secret": "",
        }
    )
    monkeypatch.setattr(cloud_accounts, "client", lambda: subscription)
    monkeypatch.setenv("OPENAI_API_KEY", "stale-api-key-must-not-be-used")

    client = GeminiClientV3(config={})

    assert client.api_url == OPENAI_SUBSCRIPTION_BASE_URL
    assert client.api_key == "unused"


def test_setup_test_can_probe_openai_subscription_before_account_switch(monkeypatch):
    from gemia.brain_config import OPENAI_SUBSCRIPTION_BASE_URL

    monkeypatch.setattr(cloud_accounts, "enabled", lambda: True)
    account = SnapshotClient(
        _snapshot("lumeri", "lumeri", "", secret="")
    )
    monkeypatch.setattr(cloud_accounts, "client", lambda: account)

    client = GeminiClientV3(
        config={
            "brain_provider_profiles": {
                "openai_subscription": {
                    "provider": "openai_subscription",
                    "model": "gpt-5.6-sol",
                    "auth_mode": "subscription",
                }
            },
            "brain_active_profile": "openai_subscription",
        },
        cloud_provider_override="openai_subscription",
    )

    assert client.provider == "openai"
    assert client.model == "gpt-5.6-sol"
    assert client.api_url == OPENAI_SUBSCRIPTION_BASE_URL
    assert client.api_key == "unused"


def test_cloud_text_and_planner_ignore_poisoned_global_endpoint_key_and_proxy(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(cloud_accounts, "enabled", lambda: True)
    snapshot = SnapshotClient(
        _snapshot("openrouter", "openrouter", "openrouter_api_key")
    )
    monkeypatch.setattr(cloud_accounts, "client", lambda: snapshot)
    monkeypatch.setenv("OPENROUTER_API_KEY", "stale-env-secret")
    monkeypatch.setenv("OPENROUTER_PROXY", "http://127.0.0.1:65530")
    monkeypatch.setenv("GEMIA_PROXY", "http://127.0.0.1:65531")
    monkeypatch.setenv("LUMERI_OPENAI_BASE_URL", "https://poison.example/v1/chat/completions")
    config = {
        "proxy": "http://127.0.0.1:65532",
        "cloud_account_model_profiles": {
            "cloud-account": {
                "provider": "openrouter",
                "model": "google/gemini-3.1-pro-preview",
                "base_url": "https://poison.example/v1/chat/completions",
            }
        },
    }

    client = GeminiClientV3(
        api_key="stale-explicit-secret",
        api_url="https://poison.example/v1/chat/completions",
        config=config,
    )
    assert client.provider == "openrouter"
    assert client.api_key == "cloud-secret"
    assert client.api_url == "https://openrouter.ai/api/v1/chat/completions"
    assert client.proxy is None

    planner = GeminiAdapter(
        api_key="stale-explicit-secret",
        api_url="https://poison.example/v1/chat/completions",
        log_dir=tmp_path / "planner-logs",
    )
    assert planner.provider == "openrouter"
    assert planner.openrouter_api_key == "cloud-secret"
    assert planner.api_url == "https://openrouter.ai/api/v1/chat/completions"
    assert planner.proxy == ""

    image = GenerativeClient()
    assert image._api_key == "cloud-secret"
    assert image.base_url == "https://openrouter.ai/api/v1"
    assert image.proxy == ""


def test_selected_openrouter_and_gemini_keys_feed_only_matching_media_clients(monkeypatch):
    monkeypatch.setattr(cloud_accounts, "enabled", lambda: True)
    openrouter = SnapshotClient(
        _snapshot("openrouter", "openrouter", "openrouter_api_key")
    )
    monkeypatch.setattr(cloud_accounts, "client", lambda: openrouter)
    image = GenerativeClient()
    video = VeoClient()
    assert image._api_key == "cloud-secret"
    assert image.base_url == "https://openrouter.ai/api/v1"
    assert video.api_key == "cloud-secret"
    assert video.base_url == "https://openrouter.ai/api/v1"
    with pytest.raises(RuntimeError):
        AudioClient()

    gemini = SnapshotClient(_snapshot("gemini", "gemini", "gemini_api_key"))
    monkeypatch.setattr(cloud_accounts, "client", lambda: gemini)
    audio = AudioClient()
    assert audio._api_key == "cloud-secret"
    with pytest.raises(RuntimeError, match="unavailable"):
        GeminiMediaClient()
    with pytest.raises(RuntimeError):
        GenerativeClient()


def test_runtime_text_config_uses_account_bound_metadata_and_custom_fails_without_it(monkeypatch):
    monkeypatch.setattr(cloud_accounts, "enabled", lambda: True)
    custom = SnapshotClient(_snapshot("custom", "custom", "openai_api_key"))
    monkeypatch.setattr(cloud_accounts, "client", lambda: custom)
    stale = {
        "brain_provider_profiles": {
            "custom:old": {
                "provider": "custom",
                "model": "old-model",
                "base_url": "https://old-account.example/v1/chat/completions",
                "api_key": "old-secret",
            }
        },
        "brain_active_profile": "custom:old",
    }

    with pytest.raises(RuntimeError):
        GeminiClientV3(config=stale)

    bound = dict(stale)
    bound["cloud_account_model_profiles"] = {
        "cloud-account": {
            "provider": "custom",
            "model": "account-model",
            "base_url": "https://account.example/v1/chat/completions",
            "effort": "medium",
        }
    }
    client = GeminiClientV3(config=bound)
    assert client.provider == "custom"
    assert client.model == "account-model"
    assert client.api_url == "https://account.example/v1/chat/completions"
    assert client.api_key == "cloud-secret"


def test_cloud_search_uses_only_account_bound_auxiliary_credentials(monkeypatch):
    monkeypatch.setattr(cloud_accounts, "enabled", lambda: True)
    client = SnapshotClient(
        auxiliary={
            "tavily": {
                "account_id": "cloud-account",
                "provider": "tavily",
                "config_field": "tavily_api_key",
                "secret": "account-search-secret",
            }
        }
    )
    monkeypatch.setattr(cloud_accounts, "client", lambda: client)
    original_read_config = web_search._read_config
    monkeypatch.setattr(
        web_search,
        "_read_config",
        lambda key: "tavily" if key == "search_provider" else original_read_config(key),
    )

    assert web_search._resolve_provider({}) == ("tavily", {"key": "account-search-secret"})
    assert web_search._resolve_provider({"provider": "tavily"}) == (
        "tavily",
        {"key": "account-search-secret"},
    )
    assert original_read_config("brave_api_key") is None


def test_cloud_model_listing_rejects_poisoned_vertex_profile_before_adc(monkeypatch):
    monkeypatch.setattr(cloud_accounts, "enabled", lambda: True)
    snapshot = SnapshotClient(_snapshot("openai", "openai", "openai_api_key"))
    monkeypatch.setattr(cloud_accounts, "client", lambda: snapshot)
    monkeypatch.setenv("VERTEX_PROJECT", "poisoned-project")
    monkeypatch.setattr(
        brain_config,
        "_list_vertex_models",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("cloud model listing reached ADC")
        ),
    )
    config = {
        "brain_provider_profiles": {
            "vertex": {
                "provider": "vertex",
                "vertex_project": "poisoned-project",
            }
        },
        "brain_active_profile": "vertex",
    }

    result = brain_config.list_models("vertex", config)

    assert result["ok"] is False
    assert result["models"] == []
    assert "does not match" in result["error"]
