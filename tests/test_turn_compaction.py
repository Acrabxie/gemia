from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, AsyncIterator

from gemia.agent_loop_v3 import AgentLoopV3
from gemia.turn_compaction import (
    compact_settled_tool_blocks,
    estimate_message_tokens,
    settled_tool_blocks,
)
from gemia.turn_ledger import TurnLedger


def _history(count: int = 20, *, payload_size: int = 5_000):
    messages = [{"role": "user", "content": "make a seven second animation"}]
    for index in range(count):
        call_id = f"call_{index}"
        messages.extend(
            [
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": "build",
                                "arguments": json.dumps({"source": "x" * payload_size}),
                            },
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": json.dumps(
                        {"status": "ok", "asset_id": f"v_{index:03d}", "stdout": "y" * payload_size}
                    ),
                },
            ]
        )
    return messages


def test_twenty_large_blocks_compact_atomically_and_cut_traffic() -> None:
    messages = _history()
    result = compact_settled_tool_blocks(messages)

    assert result.removed_blocks == 16
    assert len(settled_tool_blocks(result.messages)) == 4
    assert result.estimated_tokens_after < result.estimated_tokens_before * 0.5
    assert len(result.summaries) == 16

    assistant_ids = {
        call["id"]
        for message in result.messages
        for call in (message.get("tool_calls") or [])
    }
    result_ids = {
        message["tool_call_id"]
        for message in result.messages
        if message.get("role") == "tool"
    }
    assert assistant_ids == result_ids


def test_recent_protected_and_signature_blocks_remain_byte_identical() -> None:
    messages = _history(12, payload_size=1_000)
    messages[1]["tool_calls"][0]["extra_content"] = {"thought_signature": "sig"}
    protected_before = json.dumps(messages[3:5], sort_keys=True)

    result = compact_settled_tool_blocks(
        messages,
        protected_call_ids={"call_1"},
        max_estimated_tokens=1,
    )

    assert messages[1] in result.messages
    retained_pair = [
        message
        for message in result.messages
        if message.get("tool_call_id") == "call_1"
        or any(call.get("id") == "call_1" for call in message.get("tool_calls", []))
    ]
    assert json.dumps(retained_pair, sort_keys=True) == protected_before
    assert estimate_message_tokens(result.messages) < estimate_message_tokens(messages)


def test_compaction_never_removes_a_user_message_between_settled_blocks() -> None:
    messages = _history(2, payload_size=1_000)
    messages.extend(
        [
            {"role": "user", "content": "CURRENT USER REQUEST"},
            *_history(5, payload_size=1_000)[1:],
        ]
    )

    result = compact_settled_tool_blocks(
        messages, max_estimated_tokens=1, keep_recent=4
    )

    assert any(
        message.get("role") == "user"
        and message.get("content") == "CURRENT USER REQUEST"
        for message in result.messages
    )


class _CaptureConversationClient:
    model = "fake"

    def __init__(self) -> None:
        self.seen: list[list[dict[str, Any]]] = []

    async def stream_turn(
        self, messages: list[dict[str, Any]], *, tools=None, temperature=0.7
    ) -> AsyncIterator[dict[str, Any]]:
        del tools, temperature
        self.seen.append(messages)
        yield {"kind": "text_delta", "text": "你好"}
        yield {"kind": "finish", "reason": "stop"}


def test_compacted_summaries_survive_into_the_next_turn_prompt(tmp_path: Path) -> None:
    client = _CaptureConversationClient()
    loop = AgentLoopV3(
        session_id="compact_cross_turn",
        output_dir=tmp_path,
        gemini_client=client,  # type: ignore[arg-type]
        emit_event=lambda event: None,
    )
    loop._messages = _history()
    loop._turn_ledger = TurnLedger("build", workflow="motion_graphics")
    loop._compact_turn_history()
    assert loop._compacted_history
    preserved = loop._compacted_history[-1][:80]

    asyncio.run(loop.run_turn("你好"))

    system_prompt = str(client.seen[0][0]["content"])
    assert preserved in system_prompt
