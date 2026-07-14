"""Gemini tool-call protocol regressions for AgentLoopV3."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, AsyncIterator

from gemia.agent_loop_v3 import AgentLoopV3
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

    # The tool protocol remains intact; because the creative goal produced no
    # asset, the ledger performs one full-route retry before ending incomplete.
    assert client.calls == 3
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
