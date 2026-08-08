from __future__ import annotations

import importlib
import json

import pytest


MODEL_ENV_VARS = (
    "GEMIA_PLANNER_MODEL",
    "GEMINI_MODEL",
    "GEMIA_IMAGE_MODEL",
    "GEMIA_GEMINI_IMAGE_MODEL",
    "GEMIA_IMAGE_PRO_MODEL",
    "GEMIA_VIDEO_MODEL",
    "GEMIA_GEMINI_VIDEO_MODEL",
    "VEO_MODEL",
    "GEMIA_AUDIO_MODEL",
    "LYRIA_MODEL",
    "OPENROUTER_API_KEY",
    "OPENROUTER_MODEL",
    "GEMIA_OPENROUTER_MODEL",
    "GEMIA_AI_PROVIDER",
    "GEMIA_PLANNER_PROVIDER",
    "GEMIA_OPENROUTER_API_KEY",
    "SISYPHUS_API_KEY",
    "GEMIA_SISYPHUS_API_KEY",
    "GEMIA_IMAGE_API_KEY",
    "SISYPHUS_BASE_URL",
    "GEMIA_IMAGE_BASE_URL",
    "SISYPHUS_IMAGE_MODEL",
    "NANO_BANANA_MODEL",
    "NANO_BANANA_PRO_MODEL",
    "OPENROUTER_IMAGE_MODEL",
    "OPENROUTER_IMAGE_PRO_MODEL",
    "OPENROUTER_IMAGE_URL",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
)


def _clear_model_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in MODEL_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def test_bootstrap_memory_creates_expected_files(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    from gemia.memory import bootstrap_memory

    paths = bootstrap_memory(day="2026-04-27")

    for key in ("root", "roles", "queue", "memory", "model_profile", "daily"):
        assert paths[key]
    assert (tmp_path / ".gemia" / "memory" / "ROLES.md").exists()
    assert (tmp_path / ".gemia" / "memory" / "QUEUE.md").exists()
    assert (tmp_path / ".gemia" / "memory" / "MEMORY.md").exists()
    assert (tmp_path / ".gemia" / "memory" / "MODEL_PROFILE.json").exists()
    assert (tmp_path / ".gemia" / "memory" / "daily" / "2026-04-27.md").exists()


def test_model_profile_contains_gemia_defaults(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    _clear_model_env(monkeypatch)
    from gemia.memory import load_model_profile

    profile = load_model_profile()
    models = profile["models"]

    assert models["planner"]["default"] == "google/gemini-3.1-pro-preview"
    assert models["planner"]["provider"] == "openrouter"
    assert "GeminiFlash3" not in models["planner"]["aliases"]
    assert models["planner"]["variants"]["fast"] == "google/gemini-3-flash-preview"
    assert models["image"]["default"] == "google/gemini-3.1-flash-image-preview"
    assert models["image"]["provider"] == "openrouter/nano-banana"
    assert models["video"]["default"] == "veo-3.1-generate-preview"
    assert models["audio"]["default"] == "lyria-3-pro-preview"


def test_memory_writer_rejects_secret_fields(tmp_path) -> None:
    from gemia.memory import write_memory_json

    with pytest.raises(ValueError):
        write_memory_json(tmp_path / "bad.json", {"openrouter_api_key": "sk-test"})


def test_resolve_model_priority_env_config_profile(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    _clear_model_env(monkeypatch)
    from gemia.memory import bootstrap_memory, resolve_model

    bootstrap_memory()
    cfg_path = tmp_path / ".gemia" / "config.json"
    cfg_path.write_text(json.dumps({"planner_model": "config-planner"}, indent=2), encoding="utf-8")

    assert resolve_model("planner") == "config-planner"

    monkeypatch.setenv("GEMIA_PLANNER_MODEL", "env-planner")
    assert resolve_model("planner") == "env-planner"

    monkeypatch.delenv("GEMIA_PLANNER_MODEL")
    cfg_path.write_text("{}", encoding="utf-8")
    assert resolve_model("planner") == "google/gemini-3.1-pro-preview"


def test_clients_use_model_profile_defaults(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    _clear_model_env(monkeypatch)
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-test-key")
    monkeypatch.setenv("OPENROUTER_API_KEY", "openrouter-test-key")

    from gemia.ai.gemini_adapter import GeminiAdapter
    from gemia.ai.generative_client import GenerativeClient
    from gemia.ai.veo_client import VeoClient

    adapter = GeminiAdapter(log_dir=tmp_path / "logs")
    image_client = GenerativeClient()
    video_client = VeoClient()

    assert adapter.gemini_model == "gemini-3.1-pro-preview"
    assert adapter.model == "google/gemini-3.1-pro-preview"
    assert image_client._model == "google/gemini-3.1-flash-image-preview"
    assert image_client.base_url == "https://openrouter.ai/api/v1"
    assert video_client.model == "google/veo-3.1"


def test_model_profile_endpoint_payload_does_not_expose_keys(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    _clear_model_env(monkeypatch)
    cfg_dir = tmp_path / ".gemia"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "config.json").write_text(
        json.dumps(
            {
                "openrouter_api_key": "sk-private",
                "gemini_api_key": "gemini-private",
                "planner_model": "config-planner",
            }
        ),
        encoding="utf-8",
    )

    import server

    server = importlib.reload(server)
    payload = server._model_profile_payload()
    dumped = json.dumps(payload, ensure_ascii=False)

    assert payload["resolved_models"]["planner"]["model"] == "config-planner"
    assert "sk-private" not in dumped
    assert "gemini-private" not in dumped
    assert "openrouter_api_key" not in dumped
    assert "gemini_api_key" not in dumped


def test_server_image_key_status_uses_openrouter_image_keys(tmp_path, monkeypatch) -> None:
    _clear_model_env(monkeypatch)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)

    import server

    server = importlib.reload(server)
    monkeypatch.setattr(server, "_CONFIG_PATH", tmp_path / "config.json")
    (tmp_path / "config.json").write_text(
        json.dumps({"nano_banana_api_key": "openrouter-private-key", "openrouter_image_url": "https://openrouter.ai/api/v1"}),
        encoding="utf-8",
    )

    assert server._has_valid_image_key()

    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("OPENROUTER_API_KEY", "openrouter-private-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://openrouter.ai/api/v1")
    assert server._has_valid_image_key()


# ── /model priority catalog + active selection ──────────────────────────────


def _clear_selection_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("LUMERI_V3_MODEL", "LUMERI_V3_EFFORT"):
        monkeypatch.delenv(name, raising=False)


def test_model_catalog_is_priority_ordered(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    _clear_model_env(monkeypatch)
    _clear_selection_env(monkeypatch)
    from gemia import memory

    memory = importlib.reload(memory)
    catalog = memory.model_catalog("planner")
    assert len(catalog) >= 2
    assert catalog[0]["id"] == "google/gemini-3.1-pro-preview"
    # index 0 is the default
    assert memory.active_model_selection("planner")["default_model"] == catalog[0]["id"]


def test_active_selection_defaults_when_no_override(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    _clear_model_env(monkeypatch)
    _clear_selection_env(monkeypatch)
    from gemia import memory

    memory = importlib.reload(memory)
    active = memory.active_model_selection("planner")
    assert active["is_default_model"] is True
    assert active["is_default_effort"] is True
    assert active["effort"] == "medium"


def test_set_model_selection_by_index_and_effort(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    _clear_model_env(monkeypatch)
    _clear_selection_env(monkeypatch)
    from gemia import memory

    memory = importlib.reload(memory)
    catalog = memory.model_catalog("planner")
    result = memory.apply_model_selection({"model": "2", "effort": "high"})
    assert result["model"] == catalog[1]["id"]
    assert result["effort"] == "high"
    assert result["is_default_model"] is False
    # persisted to config.json (not memory), key preserved on disk
    cfg = json.loads((tmp_path / ".gemia" / "config.json").read_text())
    assert cfg["lumeri_v3_model"] == catalog[1]["id"]
    assert cfg["lumeri_v3_effort"] == "high"


def test_reset_model_keeps_effort(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    _clear_model_env(monkeypatch)
    _clear_selection_env(monkeypatch)
    from gemia import memory

    memory = importlib.reload(memory)
    memory.apply_model_selection({"model": "3", "effort": "high"})
    result = memory.apply_model_selection({"model": "default"})
    assert result["is_default_model"] is True
    assert result["effort"] == "high"  # effort untouched


def test_set_model_selection_rejects_unknown(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    _clear_model_env(monkeypatch)
    _clear_selection_env(monkeypatch)
    from gemia import memory

    memory = importlib.reload(memory)
    with pytest.raises(ValueError):
        memory.apply_model_selection({"model": "no-such-model"})
    with pytest.raises(ValueError):
        memory.apply_model_selection({"effort": "ultra"})


def test_config_override_reflected_in_active(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    _clear_model_env(monkeypatch)
    _clear_selection_env(monkeypatch)
    from gemia import memory

    memory = importlib.reload(memory)
    cfg_dir = tmp_path / ".gemia"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "config.json").write_text(
        json.dumps({"lumeri_v3_model": "google/gemini-3.5-flash"}), encoding="utf-8"
    )
    active = memory.active_model_selection("planner")
    assert active["model"] == "google/gemini-3.5-flash"
    assert active["label"] == "Gemini 3.5 Flash"
    assert active["is_default_model"] is False


def test_manual_weaker_model_is_not_blocked_by_legacy_strongest_keys(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    _clear_model_env(monkeypatch)
    _clear_selection_env(monkeypatch)
    from gemia import memory

    memory = importlib.reload(memory)
    memory.write_user_config(
        {
            "lumeri_v3_force_strongest": True,
            "lumeri_v3_strongest_model": "gpt-5.6-sol",
            "lumeri_v3_strongest_provider": "openai",
            "lumeri_v3_strongest_effort": "max",
            "lumeri_v3_provider": "openai",
            "lumeri_v3_model": "gpt-5.6-sol",
            "lumeri_v3_effort": "high",
        }
    )

    active = memory.apply_model_selection({"model": "gpt-5.5", "effort": "low"})
    assert active["model"] == "gpt-5.5"
    assert active["provider"] == "openai"
    assert active["effort"] == "low"
    assert "locked" not in active


def test_saved_setup_provider_wins_over_stale_daemon_environment(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    _clear_model_env(monkeypatch)
    _clear_selection_env(monkeypatch)
    monkeypatch.setenv("LUMERI_V3_PROVIDER", "openai")
    monkeypatch.setenv("LUMERI_V3_MODEL", "gpt-5.6-sol")
    monkeypatch.setenv("LUMERI_V3_EFFORT", "max")
    from gemia import memory

    memory = importlib.reload(memory)
    memory.write_user_config(
        {
            "vertex_project": "project-1",
            "lumeri_v3_provider": "vertex",
            "lumeri_v3_model": "google/gemini-2.5-pro",
            "lumeri_v3_effort": "high",
            "lumeri_v3_force_strongest": True,
            "lumeri_v3_strongest_model": "gpt-5.6-sol",
            "lumeri_v3_strongest_provider": "openai",
            "lumeri_v3_strongest_effort": "max",
        }
    )

    from gemia.gemini_client import GeminiClientV3

    client = GeminiClientV3(proxy="")
    assert client.provider == "vertex"
    assert client.model == "google/gemini-2.5-pro"
    assert client.reasoning_effort == "high"
