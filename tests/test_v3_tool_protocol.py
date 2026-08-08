"""Gemini tool-call protocol regressions for AgentLoopV3."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, AsyncIterator

import gemia.agent_loop_v3 as loop_mod
import pytest
from gemia.agent_loop_v3 import (
    AgentLoopV3,
    _DisplayStreamGate,
    _activity_text_from_model_preamble,
    _progress_report_from_model_preamble,
    _strip_activity_markup,
)
from gemia.gemini_client import _parse_chunk


class _FakeGeminiClient:
    model = "fake-gemini"

    def __init__(self) -> None:
        self.seen_messages: list[list[dict[str, Any]]] = []
        self.calls = 0

    async def stream_turn(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.7,
    ) -> AsyncIterator[dict[str, Any]]:
        del tools, temperature
        self.calls += 1
        self.seen_messages.append(json.loads(json.dumps(messages)))
        if self.calls == 1:
            yield {
                "kind": "tool_call_start",
                "index": 0,
                "id": "call_search",
                "name": "search_library",
                "extra_content": {"thought_signature": "sig-123"},
            }
            yield {
                "kind": "tool_call_args_delta",
                "index": 0,
                "delta": '{"query":"motion graphics","kind":"any"}',
            }
            yield {"kind": "finish", "reason": "tool_calls"}
            return
        yield {"kind": "text_delta", "text": "没有找到现成素材，我会从空画布开始。"}
        yield {"kind": "finish", "reason": "stop"}


class _ActivityPreambleClient:
    """One model-authored activity label followed by a root tool call."""

    model = "fake-gemini"

    def __init__(self, preamble: str) -> None:
        self.preamble = preamble
        self.calls = 0
        self.seen_messages: list[list[dict[str, Any]]] = []

    async def stream_turn(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.7,
    ) -> AsyncIterator[dict[str, Any]]:
        del tools, temperature
        self.calls += 1
        self.seen_messages.append(json.loads(json.dumps(messages)))
        if self.calls == 1:
            yield {"kind": "text_delta", "text": self.preamble}
            yield {
                "kind": "tool_call_start",
                "index": 0,
                "id": "activity-call",
                "name": "activity_fake",
            }
            yield {"kind": "tool_call_args_delta", "index": 0, "delta": "{}"}
            yield {"kind": "finish", "reason": "tool_calls"}
            return
        yield {"kind": "text_delta", "text": "这一步已经完成。"}
        yield {"kind": "finish", "reason": "stop"}


def test_model_activity_label_is_attached_before_tool_execution(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def fake_activity(_args: dict[str, Any], _ctx: Any) -> dict[str, Any]:
        return {"status": "ok"}

    monkeypatch.setitem(loop_mod.DISPATCHER, "activity_fake", fake_activity)
    client = _ActivityPreambleClient(
        "<report>我已经理顺了开场素材，主体现在更容易看清。接下来会收紧字幕节奏。</report>"
        "\n<activity>正在把开场节奏剪得更利落</activity>"
    )
    events: list[dict[str, Any]] = []
    loop = AgentLoopV3(
        session_id="model_activity",
        output_dir=tmp_path,
        gemini_client=client,  # type: ignore[arg-type]
        emit_event=events.append,
    )

    asyncio.run(loop.run_turn("把开场节奏调紧一些"))

    ready = next(
        event
        for event in events
        if event.get("kind") == "model_tool_call_ready"
        and event.get("call_id") == "activity-call"
    )
    start = next(
        event
        for event in events
        if event.get("kind") == "tool_exec_start"
        and event.get("call_id") == "activity-call"
    )
    assert ready["activity_text"] == "正在把开场节奏剪得更利落"
    assert ready["progress_report"] == (
        "我已经理顺了开场素材，主体现在更容易看清。接下来会收紧字幕节奏。"
    )
    assert events.index(ready) < events.index(start)
    assert not [
        event
        for event in events
        if event.get("kind") == "model_text_delta"
        and "<activity>" in str(event.get("delta") or "")
        and event.get("display") == "stream"
    ]
    final_delta = next(
        event
        for event in events
        if event.get("kind") == "model_text_delta"
        and event.get("delta") == "这一步已经完成。"
    )
    assert final_delta["display"] == "stream"
    assert events.index(final_delta) < next(
        index
        for index, event in enumerate(events)
        if event.get("kind") == "turn_complete"
    )

    # The UI-only protocol text must not re-enter provider history.
    assistant = next(
        message
        for message in client.seen_messages[1]
        if message.get("role") == "assistant" and message.get("tool_calls")
    )
    assert assistant["content"] is None


def test_display_stream_gate_withholds_split_tool_ui_prefix() -> None:
    gate = _DisplayStreamGate()
    assert gate.feed(" \n<re") == []
    assert gate.feed("port>阶段进展") == []
    assert gate.feed("</report><activity>继续处理</activity>") == []
    assert gate.withheld is True
    assert gate.emitted is False


def test_display_stream_gate_releases_real_model_chunks() -> None:
    gate = _DisplayStreamGate()
    assert gate.feed("这") == ["这"]
    assert gate.feed("一步") == ["一步"]
    assert gate.feed("已经完成。") == ["已经完成。"]
    assert gate.streamable is True
    assert gate.emitted is True


@pytest.mark.parametrize(
    ("preamble", "tool_names"),
    [
        ("<activity>正在查看 /tmp/secret.py</activity>", ()),
        ("<activity>{\"code\":\"do not show\"}</activity>", ()),
        ("<activity>先调用 activity_fake 完成这一步</activity>", ("activity_fake",)),
        ("<activity>第一行\n第二行</activity>", ()),
        ("正在调整开场节奏", ()),
    ],
)
def test_model_activity_label_rejects_code_and_unstructured_preamble(
    preamble: str, tool_names: tuple[str, ...]
) -> None:
    assert _activity_text_from_model_preamble(preamble, tool_names=tool_names) is None


def test_activity_markup_is_not_saved_as_assistant_prose() -> None:
    assert _strip_activity_markup("<activity>正在调整开场节奏</activity>") == ""
    assert _strip_activity_markup("完成了。\n<activity>正在调整开场节奏</activity>") == "完成了。"
    assert _strip_activity_markup(
        "<report>已经理顺素材。</report>\n<activity>正在调整节奏</activity>"
    ) == ""


def test_progress_report_requires_safe_structured_preamble() -> None:
    good = (
        "<report>画面主体已经明确，下一步会检查字幕的手机端可读性。</report>"
        "<activity>正在收紧字幕节奏</activity>"
    )
    assert _progress_report_from_model_preamble(good) == (
        "画面主体已经明确，下一步会检查字幕的手机端可读性。"
    )
    assert _progress_report_from_model_preamble(
        "<report>调用 activity_fake 继续</report><activity>继续处理</activity>",
        tool_names=("activity_fake",),
    ) is None
    assert _progress_report_from_model_preamble(
        "<report>泄露 /tmp/secret.py</report><activity>继续处理</activity>"
    ) is None
    assert _activity_text_from_model_preamble(
        "<report>泄露 /tmp/secret.py</report><activity>继续处理</activity>"
    ) == "继续处理"


def test_agent_loop_preserves_gemini_tool_call_extra_content(tmp_path: Path) -> None:
    client = _FakeGeminiClient()
    events: list[dict[str, Any]] = []
    loop = AgentLoopV3(
        session_id="tool_protocol",
        output_dir=tmp_path,
        gemini_client=client,  # type: ignore[arg-type]
        emit_event=events.append,
    )

    asyncio.run(loop.run_turn("做个mg动画"))

    # The tool protocol remains intact and the model's next no-tool response
    # ends naturally; the activity record does not trigger a host retry.
    assert client.calls == 2
    second_messages = client.seen_messages[1]
    assistant = next(
        msg for msg in second_messages
        if msg.get("role") == "assistant" and msg.get("tool_calls")
    )
    tool_call = assistant["tool_calls"][0]
    assert tool_call["id"] == "call_search"
    assert tool_call["function"]["name"] == "search_library"
    assert tool_call["extra_content"] == {"thought_signature": "sig-123"}

    tool_msg = next(msg for msg in second_messages if msg.get("role") == "tool")
    assert "no matching session or media-library assets found" in tool_msg["content"]
    assert not [event for event in events if event.get("kind") == "tool_exec_error"]
    assert [event for event in events if event.get("kind") == "turn_complete"]


def test_parse_chunk_forwards_tool_call_extra_content() -> None:
    chunk = {
        "choices": [
            {
                "delta": {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call_search",
                            "function": {
                                "name": "search_library",
                                "arguments": '{"query":',
                            },
                            "extra_content": {"thought_signature": "sig-abc"},
                        }
                    ]
                }
            }
        ]
    }

    events = list(_parse_chunk(chunk))

    assert events[0] == {
        "kind": "tool_call_start",
        "index": 0,
        "id": "call_search",
        "name": "search_library",
        "extra_content": {"thought_signature": "sig-abc"},
    }
    assert events[1] == {
        "kind": "tool_call_args_delta",
        "index": 0,
        "delta": '{"query":',
    }


def test_parse_chunk_forwards_late_tool_call_extra_content() -> None:
    chunk = {
        "choices": [
            {
                "delta": {
                    "tool_calls": [
                        {
                            "index": 0,
                            "function": {},
                            "extra_content": {"thought_signature": "sig-late"},
                        }
                    ]
                }
            }
        ]
    }

    assert list(_parse_chunk(chunk)) == [
        {
            "kind": "tool_call_extra",
            "index": 0,
            "extra_content": {"thought_signature": "sig-late"},
        }
    ]


def test_parse_chunk_top_level_error_is_terminal_before_choices() -> None:
    chunk = {
        "error": {
            "message": "upstream failed",
            "type": "upstream_error",
            "code": "response_failed",
        },
        "choices": [
            {
                "delta": {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "partial",
                            "function": {"name": "run_shell", "arguments": "{"},
                        }
                    ]
                },
                "finish_reason": "stop",
            }
        ],
    }

    assert list(_parse_chunk(chunk)) == [
        {"kind": "error", "error": "upstream failed (response_failed)"}
    ]


def test_parse_chunk_accepts_string_error_without_choices() -> None:
    assert list(_parse_chunk({"error": "connection reset"})) == [
        {"kind": "error", "error": "connection reset"}
    ]
