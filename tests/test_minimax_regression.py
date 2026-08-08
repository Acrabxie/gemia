"""MiniMax reasoning stays private while tool protocol continuity remains intact."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, AsyncIterator

import gemia.agent_loop_v3 as loop_mod
from gemia.agent_loop_v3 import AgentLoopV3
from gemia.gemini_client import _MiniMaxThinkTagFilter


class _RawThinkingToolClient:
    model = "fake-minimax"

    def __init__(self) -> None:
        self.calls = 0
        self.seen_messages: list[list[dict[str, Any]]] = []

    async def stream_turn(
        self, messages: list[dict[str, Any]], *, tools=None, temperature: float = 0.7
    ) -> AsyncIterator[dict[str, Any]]:
        del tools, temperature
        self.calls += 1
        self.seen_messages.append(json.loads(json.dumps(messages)))
        if self.calls == 1:
            yield {
                "kind": "text_delta",
                "text": "正在检索素材。",
                "raw_text": "<think>private chain of thought</think>正在检索素材。",
            }
            yield {
                "kind": "tool_call_start",
                "index": 0,
                "id": "lookup",
                "name": "search_library",
            }
            yield {
                "kind": "tool_call_args_delta",
                "index": 0,
                "delta": '{"query":"intro","kind":"any"}',
            }
            yield {"kind": "finish", "reason": "tool_calls"}
            return
        yield {"kind": "text_delta", "text": "没有找到匹配素材。"}
        yield {"kind": "finish", "reason": "stop"}


def test_minimax_filter_hides_split_think_tags() -> None:
    gate = _MiniMaxThinkTagFilter()

    assert gate.feed("公开内容<th") == "公开内容"
    assert gate.feed("ink>内部推理") == ""
    assert gate.feed("继续推理</th") == ""
    assert gate.feed("ink>结论") == "结论"
    assert gate.finish() == ""


def test_raw_thinking_is_only_used_for_immediate_tool_continuation(
    tmp_path: Path, monkeypatch
) -> None:
    # The older Windows loop retains a one-shot completion check; it is not part
    # of this protocol assertion and would add an unrelated third fake call.
    if hasattr(loop_mod, "COMPLETION_CHECK_ENABLED"):
        monkeypatch.setattr(loop_mod, "COMPLETION_CHECK_ENABLED", False)

    client = _RawThinkingToolClient()
    events: list[dict[str, Any]] = []
    loop = AgentLoopV3(
        session_id="minimax_private_thinking",
        output_dir=tmp_path,
        gemini_client=client,  # type: ignore[arg-type]
        emit_event=events.append,
    )

    asyncio.run(loop.run_turn("找开场素材"))

    assert client.calls == 2
    continued_assistant = next(
        message
        for message in client.seen_messages[1]
        if message.get("role") == "assistant" and message.get("tool_calls")
    )
    assert "<think>private chain of thought</think>" in continued_assistant["content"]

    persisted_assistant = next(
        message
        for message in loop._messages
        if message.get("role") == "assistant" and message.get("tool_calls")
    )
    assert persisted_assistant["content"] == "正在检索素材。"
    assert "<think>" not in json.dumps(loop._messages, ensure_ascii=False)
    assert "<think>" not in json.dumps(loop.render_messages(), ensure_ascii=False)
    delivered = "".join(
        str(event.get("delta") or "")
        for event in events
        if event.get("kind") == "model_text_delta"
    )
    assert "private chain of thought" not in delivered
