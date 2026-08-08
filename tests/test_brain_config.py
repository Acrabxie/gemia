"""brain_config 金标准：脱敏、白名单、custom→openai、env 即时生效。"""
import os

import pytest

from gemia import brain_config as bc


def test_read_status_masks_keys():
    cfg = {
        "lumeri_v3_provider": "openai",
        "lumeri_v3_model": "gpt-5.5",
        "lumeri_v3_effort": "high",
        "openai_api_key": "sk-super-secret-value",
        "anthropic_api_key": "",
        "openrouter_api_key": "or-key",
        "vertex_project": "proj-1",
    }
    st = bc.read_status(cfg)
    # 现状字段透传
    assert st["provider"] == "openai"
    assert st["model"] == "gpt-5.5"
    assert st["effort"] == "high"
    assert st["vertex_project"] == "proj-1"
    # 密钥只给布尔
    assert st["has_key"] == {
        "openrouter": True,
        "gemini": False,
        "anthropic": False,
        "openai": True,
    }
    # 绝不泄漏任何明文密钥
    blob = str(st)
    assert "sk-super-secret-value" not in blob
    assert "or-key" not in blob
    # 供前端渲染的目录齐全
    assert [p["id"] for p in st["providers"]] == [
        "vertex", "gemini", "openai", "openai_subscription", "claude", "openrouter", "custom",
    ]
    assert st["efforts"] == bc.EFFORTS


def test_apply_update_whitelist_only():
    previous_openrouter_env = os.environ.get("OPENROUTER_API_KEY")
    cfg = {"smtp": {"password": "keep-me"}, "cloudflare_email": {"api_token": "keep"}}
    body = {
        "provider": "openrouter",
        "model": "anthropic/claude-fable-5",
        "effort": "medium",
        "openrouter_api_key": "or-new",
        # 恶意/越界字段——必须被忽略
        "smtp": "HACK",
        "cloudflare_email": "HACK",
        "google_oauth_client_secret": "HACK",
    }
    out, changed = bc.apply_update(cfg, body)
    assert out["lumeri_v3_provider"] == "openrouter"
    assert out["lumeri_v3_model"] == "anthropic/claude-fable-5"
    assert out["brain_provider_profiles"]["openrouter"]["api_key"] == "or-new"
    assert "openrouter_api_key" not in out
    # 敏感块原样保留、未被越界写入覆盖
    assert out["smtp"] == {"password": "keep-me"}
    assert out["cloudflare_email"] == {"api_token": "keep"}
    assert "google_oauth_client_secret" not in out
    assert os.environ.get("OPENROUTER_API_KEY") == previous_openrouter_env
    assert os.environ.get("LUMERI_V3_PROVIDER") == "openrouter"


def test_custom_persists_independently_with_base_url():
    out, _ = bc.apply_update({}, {
        "provider": "custom",
        "base_url": "https://gw.example/v1/chat/completions",
        "model": "my-model",
        "openai_api_key": "sk-x",
    })
    assert out["lumeri_v3_provider"] == "custom"
    assert out["lumeri_openai_base_url"] == "https://gw.example/v1/chat/completions"
    active = out["brain_active_profile"]
    assert out["brain_provider_profiles"][active]["api_key"] == "sk-x"
    assert "openai_api_key" not in out
    assert os.environ.get("LUMERI_OPENAI_BASE_URL") == "https://gw.example/v1/chat/completions"
    assert bc.read_status(out)["provider"] == "custom"


def test_custom_model_scan_uses_saved_endpoint_without_httpx(monkeypatch):
    class Response:
        def read(self):
            return b'{"data":[{"id":"gateway-model"}]}'

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    class Opener:
        def open(self, request, **kwargs):
            calls.append((request, kwargs))
            return Response()

    calls = []
    monkeypatch.setattr(bc.urllib.request, "build_opener", lambda *_args: Opener())
    result = bc.list_models(
        "custom",
        {
            "lumeri_openai_base_url": "https://gateway.example/v1/chat/completions",
            "openai_api_key": "gateway-secret",
        },
    )
    assert result["ok"] is True
    assert result["models"] == [{"id": "gateway-model"}]
    assert calls[0][0].full_url == "https://gateway.example/v1/models"
    assert calls[0][0].headers["Authorization"] == "Bearer gateway-secret"


def test_openai_subscription_maps_to_local_bridge_without_storing_a_key():
    out, changed = bc.apply_update({}, {
        "provider": "openai_subscription",
        "model": "gpt-5.5",
    })
    assert out["lumeri_v3_provider"] == "openai"
    assert out["lumeri_openai_auth_mode"] == "subscription"
    assert out["lumeri_openai_base_url"] == bc.OPENAI_SUBSCRIPTION_BASE_URL
    assert "openai_api_key" not in out
    assert "lumeri_openai_auth_mode" in changed
    assert bc.read_status(out)["provider"] == "openai_subscription"


def test_switching_from_subscription_to_openai_api_clears_bridge_url():
    cfg, _ = bc.apply_update({}, {"provider": "openai_subscription"})
    out, _ = bc.apply_update(cfg, {
        "provider": "openai",
        "base_url": "",
        "openai_api_key": "sk-test",
    })
    assert out["lumeri_openai_auth_mode"] == "api_key"
    assert out["lumeri_openai_base_url"] == ""
    assert bc.read_status(out)["provider"] == "openai"


def test_openai_subscription_model_list_scans_local_bridge(monkeypatch):
    class Response:
        status = 200

        def getcode(self):
            return self.status

        def read(self):
            return b'{"data":[{"id":"gpt-5.5","name":"GPT-5.5"}]}'

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    class Opener:
        def open(self, request, **kwargs):
            calls.append((request, kwargs))
            return Response()

    calls = []
    monkeypatch.setattr(bc.urllib.request, "build_opener", lambda *_args: Opener())
    result = bc.list_models("openai_subscription", {}, proxy="http://127.0.0.1:7890")
    assert result == {
        "ok": True,
        "models": [{"id": "gpt-5.5", "name": "GPT-5.5"}],
        "recommended_model": "gpt-5.5",
    }
    assert calls[0][0].full_url == "http://127.0.0.1:7808/v1/models"


def test_provider_switch_recommends_only_inside_new_provider(monkeypatch):
    monkeypatch.setenv("LUMERI_V3_PROVIDER", "openai")
    cfg = {
        "lumeri_v3_provider": "openai",
        "lumeri_v3_model": "gpt-5.6-sol",
        "lumeri_v3_force_strongest": True,
        "lumeri_v3_strongest_model": "gpt-5.6-sol",
        "lumeri_v3_strongest_provider": "openai",
    }

    out, _ = bc.apply_update(cfg, {"provider": "vertex"}, sync_env=False)

    assert out["lumeri_v3_provider"] == "vertex"
    assert out["lumeri_v3_model"] == "google/gemini-3.5-flash"
    assert os.environ["LUMERI_V3_PROVIDER"] == "openai"
    assert not any(key in out for key in bc._LEGACY_STRONGEST_KEYS)


def test_manual_weaker_model_remains_selectable_inside_provider():
    out, _ = bc.apply_update(
        {"lumeri_v3_provider": "vertex", "lumeri_v3_model": "google/gemini-3.5-flash"},
        {"provider": "vertex", "model": "google/gemini-2.5-pro"},
        sync_env=False,
    )
    assert out["lumeri_v3_model"] == "google/gemini-2.5-pro"


def test_cross_provider_model_is_replaced_by_local_recommendation():
    out, _ = bc.apply_update(
        {"lumeri_v3_provider": "openai", "lumeri_v3_model": "gpt-5.6-sol"},
        {"provider": "vertex", "model": "gpt-5.6-sol"},
        sync_env=False,
    )
    assert out["lumeri_v3_model"] == "google/gemini-3.5-flash"


def test_vertex_model_scan_follows_all_pages(monkeypatch):
    pages = [
        {
            "publisherModels": [
                {"name": "publishers/google/models/gemini-2.5-pro", "displayName": "Gemini 2.5 Pro"},
                {"name": "publishers/meta/models/llama-4"},
            ],
            "nextPageToken": "page two",
        },
        {
            "publisherModels": [
                {"name": "publishers/google/models/gemini-3.5-flash", "displayName": "Gemini 3.5 Flash"},
            ],
        },
        {
            "publisherModels": [
                {"name": "publishers/anthropic/models/claude-opus-5", "displayName": "Claude Opus 5"},
            ],
        },
    ]
    calls = []

    class Response:
        def __init__(self, payload):
            self.payload = payload

        def read(self):
            import json
            return json.dumps(self.payload).encode()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    class Opener:
        def open(self, request, **kwargs):
            calls.append((request, kwargs))
            return Response(pages[len(calls) - 1])

    import gemia.gemini_client as client_module

    monkeypatch.setattr(client_module, "_vertex_access_token", lambda _proxy: "access-token")
    monkeypatch.setattr(bc.urllib.request, "build_opener", lambda *_args: Opener())
    result = bc.list_models(
        "vertex",
        {"vertex_project": "project-1", "vertex_location": "global"},
    )

    assert result["ok"] is True
    assert result["from_catalog"] is True
    assert result["pages"] == 3
    assert result["recommended_model"] == "google/gemini-3.5-flash"
    assert [item["id"] for item in result["models"]] == [
        "google/gemini-3.5-flash",
        "anthropic/claude-opus-5",
        "google/gemini-2.5-pro",
    ]
    assert "/publishers/google/models?" in calls[0][0].full_url
    assert "pageToken=page+two" in calls[1][0].full_url
    assert "/publishers/anthropic/models?" in calls[2][0].full_url
    assert calls[0][0].get_header("Authorization") == "Bearer access-token"
    assert calls[0][0].get_header("X-goog-user-project") == "project-1"


def test_codex_login_bridge_starts_and_checks_loopback_oauth(monkeypatch):
    class Response:
        status = 200

        def __init__(self, state):
            self.state = state

        def getcode(self):
            return self.status

        def read(self):
            return ('{"state":"' + self.state + '"}').encode()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    calls = []

    class Opener:
        def open(self, request, **kwargs):
            calls.append((request.get_method(), request.full_url, kwargs))
            return Response("waiting" if request.get_method() == "POST" else "success")

    monkeypatch.setattr(bc.urllib.request, "build_opener", lambda *_args: Opener())
    assert bc.codex_login_bridge("POST") == (200, {"state": "waiting"})
    assert bc.codex_login_bridge("GET") == (200, {"state": "success"})
    assert [(method, url) for method, url, _ in calls] == [
        ("POST", "http://127.0.0.1:7808/v1/auth/login"),
        ("GET", "http://127.0.0.1:7808/v1/auth/status"),
    ]


def test_blank_key_does_not_clobber():
    cfg = {"openai_api_key": "sk-existing"}
    out, changed = bc.apply_update(cfg, {"provider": "openai", "openai_api_key": ""})
    # 留空表单不清已存 key
    assert out["openai_api_key"] == "sk-existing"
    assert "openai_api_key" not in changed


def test_unknown_provider_rejected():
    out, _ = bc.apply_update({}, {"provider": "definitely-not-a-provider"})
    assert "lumeri_v3_provider" not in out


def test_provider_profiles_do_not_leak_credentials_or_endpoints():
    cfg, _ = bc.apply_update(
        {},
        {
            "provider": "openai",
            "model": "gpt-5.5",
            "base_url": "https://api.openai.example/v1/chat/completions",
            "openai_api_key": "openai-secret",
        },
        sync_env=False,
    )
    cfg, _ = bc.apply_update(
        cfg,
        {
            "provider": "custom",
            "profile_id": "custom:new",
            "profile_name": "Gateway A",
            "model": "gateway-model",
            "base_url": "https://gateway-a.example/v1/chat/completions",
            "openai_api_key": "gateway-secret",
        },
        sync_env=False,
    )

    custom_id = cfg["brain_active_profile"]
    openai_runtime = bc.resolve_runtime_config(cfg, profile_id="openai")
    custom_runtime = bc.resolve_runtime_config(cfg, profile_id=custom_id)

    assert openai_runtime["openai_api_key"] == "openai-secret"
    assert openai_runtime["lumeri_openai_base_url"] == "https://api.openai.example/v1/chat/completions"
    assert custom_runtime["openai_api_key"] == "gateway-secret"
    assert custom_runtime["lumeri_openai_base_url"] == "https://gateway-a.example/v1/chat/completions"
    assert openai_runtime["openai_api_key"] != custom_runtime["openai_api_key"]


def test_multiple_named_custom_profiles_are_independent():
    cfg, _ = bc.apply_update(
        {},
        {
            "provider": "custom",
            "profile_id": "custom:new",
            "profile_name": "Gateway A",
            "model": "model-a",
            "base_url": "https://a.example/v1/chat/completions",
            "openai_api_key": "key-a",
        },
        sync_env=False,
    )
    first_id = cfg["brain_active_profile"]
    cfg, _ = bc.apply_update(
        cfg,
        {
            "provider": "custom",
            "profile_id": "custom:new",
            "profile_name": "Gateway B",
            "model": "model-b",
            "base_url": "https://b.example/v1/chat/completions",
            "openai_api_key": "key-b",
        },
        sync_env=False,
    )
    second_id = cfg["brain_active_profile"]

    assert first_id != second_id
    assert bc.resolve_runtime_config(cfg, profile_id=first_id)["lumeri_v3_model"] == "model-a"
    assert bc.resolve_runtime_config(cfg, profile_id=second_id)["lumeri_v3_model"] == "model-b"
    status = bc.read_status(cfg)
    assert status["profiles"][first_id]["name"] == "Gateway A"
    assert status["profiles"][second_id]["name"] == "Gateway B"
    assert "api_key" not in status["profiles"][first_id]
