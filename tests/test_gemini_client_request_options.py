from __future__ import annotations

import asyncio
from typing import Any

import gemia.gemini_client as gemini_client
import pytest
from gemia.gemini_client import GeminiClientV3, _parse_optional_bool


def _capture_body(
    *,
    provider: str = "openai",
    effort: str = "medium",
    parallel: bool | None = None,
    tools: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    client = object.__new__(GeminiClientV3)
    client.provider = provider
    client.model = "test-model"
    client.reasoning_effort = effort
    client.parallel_tool_calls = parallel
    client.orchestration_temperature = 0.2
    captured: dict[str, Any] = {}

    def fake_stream(body: dict[str, Any]):
        captured.update(body)
        yield {"kind": "finish", "reason": "stop"}

    client._stream_blocking = fake_stream
    client._stream_blocking_claude = fake_stream

    async def consume() -> None:
        async for _ in client.stream_turn(
            [{"role": "user", "content": "hello"}], tools=tools
        ):
            pass

    asyncio.run(consume())
    return captured


def test_reasoning_effort_and_parallel_are_sent_exactly() -> None:
    body = _capture_body(
        effort="max",
        parallel=True,
        tools=[{"type": "function", "function": {"name": "inspect"}}],
    )
    assert body["reasoning"] == {"effort": "high"}
    assert body["parallel_tool_calls"] is True


def test_parallel_false_is_not_lost() -> None:
    body = _capture_body(
        parallel=False,
        tools=[{"type": "function", "function": {"name": "inspect"}}],
    )
    assert body["parallel_tool_calls"] is False


def test_parallel_unset_no_tools_and_claude_are_omitted() -> None:
    assert "parallel_tool_calls" not in _capture_body(parallel=None, tools=[])
    assert "parallel_tool_calls" not in _capture_body(
        provider="claude",
        parallel=True,
        tools=[{"type": "function", "function": {"name": "inspect"}}],
    )


def test_tri_state_parser_rejects_invalid_without_truthiness_bug() -> None:
    assert _parse_optional_bool("true", source="test") is True
    assert _parse_optional_bool("false", source="test") is False
    assert _parse_optional_bool(0, source="test") is False
    assert _parse_optional_bool("maybe", source="test") is None


def test_openai_subscription_bridge_needs_no_api_key(monkeypatch) -> None:
    monkeypatch.setenv("LUMERI_V3_PROVIDER", "openai")
    monkeypatch.setenv("LUMERI_V3_MODEL", "gpt-5.5")
    monkeypatch.setenv("LUMERI_OPENAI_AUTH_MODE", "subscription")
    monkeypatch.setenv(
        "LUMERI_OPENAI_BASE_URL",
        "http://127.0.0.1:7808/v1/chat/completions",
    )
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    client = GeminiClientV3(
        proxy="",
        config={
            "lumeri_v3_provider": "openai",
            "lumeri_v3_model": "gpt-5.5",
            "lumeri_openai_auth_mode": "subscription",
            "lumeri_openai_base_url": "http://127.0.0.1:7808/v1/chat/completions",
        },
    )

    assert client.provider == "openai"
    assert client.api_url == "http://127.0.0.1:7808/v1/chat/completions"


def test_subscription_fast_mode_adds_priority_without_lowering_reasoning(monkeypatch) -> None:
    client = GeminiClientV3(
        config={
            "lumeri_v3_provider": "openai",
            "lumeri_v3_model": "gpt-5.6-sol",
            "lumeri_v3_effort": "high",
            "lumeri_openai_auth_mode": "subscription",
            "lumeri_openai_base_url": "http://127.0.0.1:7808/v1/chat/completions",
            "lumeri_fast_mode": True,
        },
        proxy="",
    )
    captured = []
    monkeypatch.setattr(
        "gemia.local_config.fast_mode_preference", lambda **_kwargs: True
    )
    monkeypatch.setattr(
        client,
        "_stream_blocking",
        lambda body: captured.append(body) or iter([{"kind": "finish", "reason": "stop"}]),
    )

    async def collect() -> None:
        events = [event async for event in client.stream_turn([{"role": "user", "content": "hi"}])]
        assert events == [{"kind": "finish", "reason": "stop"}]

    import asyncio

    asyncio.run(collect())
    assert captured[0]["service_tier"] == "priority"
    assert captured[0]["reasoning"] == {"effort": "high"}
    assert client.api_key == "unused"


def test_openai_api_still_requires_key(monkeypatch) -> None:
    monkeypatch.setenv("LUMERI_V3_PROVIDER", "openai")
    monkeypatch.setenv("LUMERI_OPENAI_AUTH_MODE", "api_key")
    monkeypatch.setenv("LUMERI_OPENAI_BASE_URL", "https://api.openai.com/v1/chat/completions")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(gemini_client, "_read_config_key", lambda _field: "")

    with pytest.raises(RuntimeError, match="OPENAI_API_KEY required"):
        GeminiClientV3(
            proxy="",
            config={
                "lumeri_v3_provider": "openai",
                "lumeri_openai_auth_mode": "api_key",
                "lumeri_openai_base_url": "https://api.openai.com/v1/chat/completions",
            },
        )


def test_custom_provider_keeps_identity_but_uses_openai_compatible_transport(monkeypatch) -> None:
    monkeypatch.setenv("LUMERI_V3_PROVIDER", "openai")
    client = GeminiClientV3(
        proxy="",
        config={
            "lumeri_v3_provider": "custom",
            "lumeri_v3_model": "gateway-model",
            "lumeri_openai_auth_mode": "api_key",
            "lumeri_openai_base_url": "https://gateway.example/v1/chat/completions",
            "openai_api_key": "gateway-secret",
        },
    )

    assert client.provider == "custom"
    assert client.api_url == "https://gateway.example/v1/chat/completions"
    assert client.api_key == "gateway-secret"
    assert client.model == "gateway-model"
