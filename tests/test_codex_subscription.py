from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import pytest

from gemia import brain_config, codex_subscription
from gemia import gemini_client


def _completed(args: list[str], returncode: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args, returncode, stdout, stderr)


def test_subscription_status_accepts_only_chatgpt_login(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(codex_subscription, "_codex_executable", lambda: "codex")

    def fake_run(args: list[str], *, timeout: int = 8):
        del timeout
        if args == ["--version"]:
            return _completed(args, 0, "codex-cli 1.2.3\n")
        return _completed(args, 0, "Logged in using ChatGPT\n")

    monkeypatch.setattr(codex_subscription, "_run_codex", fake_run)
    status = codex_subscription.subscription_status()
    assert status == {
        "installed": True,
        "authenticated": True,
        "auth_method": "chatgpt",
        "version": "codex-cli 1.2.3",
        "message": "已登录 ChatGPT 订阅",
    }

    monkeypatch.setattr(
        codex_subscription,
        "_run_codex",
        lambda args, timeout=8: _completed(args, 0, "Logged in using an API key\n"),
    )
    status = codex_subscription.subscription_status()
    assert status["authenticated"] is False
    assert status["auth_method"] == "api_key"


def test_provider_catalog_exposes_local_codex_without_a_key(monkeypatch: pytest.MonkeyPatch) -> None:
    spec = brain_config.provider_spec("codex_subscription")
    assert spec is not None
    assert spec["label"] == "OpenAI 订阅（本机 Codex）"
    assert spec["key_field"] is None

    monkeypatch.setenv("LUMERI_V3_PROVIDER", "")
    config: dict = {}
    updated, changed = brain_config.apply_update(config, {"provider": "codex_subscription"})
    assert updated["lumeri_v3_provider"] == "codex_subscription"
    assert changed == ["lumeri_v3_provider"]
    assert brain_config.read_status(updated)["has_key"]["openai"] is False


def test_codex_provider_does_not_inherit_another_providers_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LUMERI_V3_PROVIDER", "codex_subscription")
    monkeypatch.delenv("LUMERI_V3_MODEL", raising=False)

    def fake_config(key: str) -> str:
        return {
            "lumeri_v3_provider": "openai",
            "lumeri_v3_model": "api-only-model",
        }.get(key, "")

    monkeypatch.setattr(gemini_client, "_read_config_key", fake_config)
    client = gemini_client.GeminiClientV3()
    assert client.provider == "codex_subscription"
    assert client.model == ""


def test_parse_result_filters_unknown_tools() -> None:
    text, calls = codex_subscription._parse_result(
        json.dumps(
            {
                "text": "处理中",
                "tool_calls": [
                    {"name": "inspect_project", "arguments_json": "{\"detail\":true}"},
                    {"name": "not_a_lumeri_tool", "arguments_json": "{}"},
                ],
            }
        ),
        {"inspect_project"},
    )
    assert text == "处理中"
    assert calls == [{"name": "inspect_project", "arguments": {"detail": True}}]


def test_process_error_does_not_echo_prompt_or_paths() -> None:
    stderr = b'user SECRET PROMPT /private/path\n{"error":{"code":"UsageLimitExceeded"}}'
    message = codex_subscription._safe_process_error(stderr, 1)
    assert "额度" in message
    assert "SECRET" not in message
    assert "/private/path" not in message


def test_inline_image_is_materialized_without_leaving_data_url(tmp_path: Path) -> None:
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "看图"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,aGVsbG8="}},
            ],
        }
    ]
    clean, images = codex_subscription._materialize_messages(messages, tmp_path)
    assert len(images) == 1
    assert images[0].read_bytes() == b"hello"
    assert clean[0]["content"][1] == {"type": "text", "text": "[已附加图片 1]"}
    assert "base64" not in json.dumps(clean)


def test_adapter_emits_v3_tool_protocol(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        codex_subscription,
        "subscription_status",
        lambda: {"authenticated": True, "message": "ok"},
    )
    monkeypatch.setattr(codex_subscription, "_codex_executable", lambda: "/usr/bin/codex")

    class FakeProcess:
        returncode = 0

        async def communicate(self, prompt: bytes):
            assert b"LUMERI_TOOLS" in prompt
            return b"", b""

        def kill(self):
            self.returncode = -9

        async def wait(self):
            return self.returncode

    async def fake_create(*args, **kwargs):
        del kwargs
        argv = list(args)
        output_path = Path(argv[argv.index("--output-last-message") + 1])
        output_path.write_text(
            json.dumps(
                {
                    "text": "",
                    "tool_calls": [{"name": "inspect_project", "arguments_json": "{\"detail\":true}"}],
                }
            ),
            encoding="utf-8",
        )
        return FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)
    client = codex_subscription.CodexSubscriptionClient(timeout=5)

    async def collect():
        return [
            event
            async for event in client.stream_turn(
                [{"role": "user", "content": "检查项目"}],
                tools=[
                    {
                        "type": "function",
                        "function": {
                            "name": "inspect_project",
                            "description": "Inspect",
                            "parameters": {"type": "object"},
                        },
                    }
                ],
            )
        ]

    events = asyncio.run(collect())
    assert [event["kind"] for event in events] == [
        "tool_call_start",
        "tool_call_args_delta",
        "finish",
    ]
    assert events[0]["name"] == "inspect_project"
    assert json.loads(events[1]["delta"]) == {"detail": True}
    assert events[2]["reason"] == "tool_calls"
