import json
import tomllib

import server
from gemia import local_config
from tests_http_harness import create_raw_request, run_server_handler

CAPABILITIES = {
    "codex_cli": {"available": True},
    "openai_subscription_bridge": {"available": True},
    "fast_mode": {
        "available": True,
        "provider": "openai_subscription",
        "quality_policy": "reasoning_unchanged",
    },
}


def _secret_config() -> dict:
    return {
        "brain_active_profile": "openai_subscription",
        "brain_provider_profiles": {
            "openai_subscription": {
                "provider": "openai_subscription",
                "name": "OpenAI subscription",
                "model": "gpt-5.6-sol",
                "effort": "high",
                "auth_mode": "subscription",
                "base_url": "http://127.0.0.1:7808/v1/chat/completions",
            },
            "openai": {
                "provider": "openai",
                "model": "gpt-5.5",
                "effort": "medium",
                "api_key": "must-never-leak",
            },
        },
    }


def test_public_config_is_secret_free_and_capability_based(tmp_path) -> None:
    path = tmp_path / ".lumeri" / "config.toml"
    snapshot = local_config.load_or_create(
        _secret_config(), path=path, capabilities=CAPABILITIES
    )

    text = path.read_text(encoding="utf-8")
    assert snapshot["schema_version"] == 1
    assert snapshot["model"]["active_profile"] == "openai_subscription"
    assert snapshot["features"]["fast_mode"]["available"] is True
    assert "must-never-leak" not in text
    assert "api_key" not in text
    assert "base_url" not in text


def test_fast_mode_is_independent_of_reasoning_and_subscription_only(tmp_path) -> None:
    path = tmp_path / "config.toml"
    secret = _secret_config()
    snapshot = local_config.write_public_update(
        secret,
        {"fast_mode": True},
        path=path,
        capabilities=CAPABILITIES,
    )

    active = snapshot["model"]["profiles"]["openai_subscription"]
    assert active["effort"] == "high"
    assert snapshot["features"]["fast_mode"]["enabled"] is True
    assert snapshot["features"]["fast_mode"]["effective"] is True

    stored = tomllib.loads(path.read_text(encoding="utf-8"))
    stored["model"]["active_profile"] = "openai"
    path.write_text(local_config._toml_dump(stored), encoding="utf-8")
    switched = local_config.load_or_create(
        secret, path=path, capabilities=CAPABILITIES
    )
    assert switched["features"]["fast_mode"]["enabled"] is True
    assert switched["features"]["fast_mode"]["effective"] is False


def test_public_selection_overrides_legacy_but_preserves_secret(tmp_path) -> None:
    path = tmp_path / "config.toml"
    secret = _secret_config()
    stored = local_config.build_snapshot(secret, capabilities=CAPABILITIES)
    stored["model"]["profiles"]["openai"]["model"] = "gpt-5.4"
    stored["model"]["active_profile"] = "openai"
    path.write_text(local_config._toml_dump(stored), encoding="utf-8")

    merged = local_config.merge_with_secret_config(secret, path=path)

    assert merged["brain_active_profile"] == "openai"
    assert merged["brain_provider_profiles"]["openai"]["model"] == "gpt-5.4"
    assert (
        merged["brain_provider_profiles"]["openai"]["api_key"]
        == "must-never-leak"
    )


def test_bridge_capability_requires_explicit_fast_mode_advertisement(monkeypatch) -> None:
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return b'{"ok":true,"capabilities":{"fast_mode":true}}'

    class Opener:
        def open(self, request, timeout):
            assert request.full_url == "http://127.0.0.1:7808/health"
            assert timeout == 0.35
            return Response()

    monkeypatch.setattr(
        local_config.urllib.request, "build_opener", lambda *_args: Opener()
    )
    assert local_config._bridge_capability_snapshot() == (True, True)


def _request(method: str, path: str, body=None):
    raw = create_raw_request(method, path, body=body)
    response = run_server_handler(server._Handler, raw)
    return response["status"], json.loads(response["body"].decode("utf-8"))


def test_localhost_config_api_reads_and_writes_public_authority(monkeypatch, tmp_path) -> None:
    secret_path = tmp_path / ".gemia" / "config.json"
    public_path = tmp_path / ".lumeri" / "config.toml"
    secret_path.parent.mkdir(parents=True)
    secret_path.write_text(json.dumps(_secret_config()), encoding="utf-8")
    monkeypatch.setattr(server, "_CONFIG_PATH", secret_path)
    monkeypatch.setattr(server, "_LOCAL_CONFIG_PATH", public_path)
    monkeypatch.setattr(server, "_require_provider_access", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(server.cloud_accounts, "enabled", lambda: False)
    monkeypatch.setattr(local_config, "detect_capabilities", lambda: CAPABILITIES)

    status, saved = _request("POST", "/config", {"fast_mode": True})
    assert status == 200
    assert saved == {"ok": True}

    status, payload = _request("GET", "/config")
    assert status == 200
    fast = payload["local_config"]["features"]["fast_mode"]
    assert fast["enabled"] is True
    assert fast["quality_policy"] == "reasoning_unchanged"
    assert "must-never-leak" not in public_path.read_text(encoding="utf-8")
    assert "must-never-leak" in secret_path.read_text(encoding="utf-8")


def test_model_api_writes_same_public_config(monkeypatch, tmp_path) -> None:
    secret_path = tmp_path / ".gemia" / "config.json"
    public_path = tmp_path / ".lumeri" / "config.toml"
    secret_path.parent.mkdir(parents=True)
    secret_path.write_text(json.dumps(_secret_config()), encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LUMERI_LOCAL_CONFIG_PATH", str(public_path))
    monkeypatch.setattr(server, "_CONFIG_PATH", secret_path)
    monkeypatch.setattr(server, "_LOCAL_CONFIG_PATH", public_path)
    monkeypatch.setattr(server.cloud_accounts, "enabled", lambda: False)
    monkeypatch.setattr(local_config, "detect_capabilities", lambda: CAPABILITIES)

    status, payload = _request("POST", "/model", {"fast_mode": True})

    assert status == 200
    assert payload["fast_mode"]["enabled"] is True
    assert payload["active"]["effort"] == "high"
    assert tomllib.loads(public_path.read_text())["features"]["fast_mode"]["enabled"] is True
