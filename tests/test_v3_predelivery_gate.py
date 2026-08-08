"""There is no host-authored pre-delivery completion gate."""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, AsyncIterator

import gemia.agent_loop_v3 as loop_mod

from gemia.agent_loop_v3 import AgentLoopV3


class _ToolThenNaturalReply:
    model = "fake"

    def __init__(self, tool_name: str, reply: str) -> None:
        self.tool_name = tool_name
        self.reply = reply
        self.calls = 0
        self.messages_seen: list[list[dict[str, Any]]] = []

    async def stream_turn(
        self,
        messages: list[dict[str, Any]],
        *,
        tools=None,
        temperature: float = 0.7,
    ) -> AsyncIterator[dict[str, Any]]:
        del tools, temperature
        self.calls += 1
        self.messages_seen.append(messages)
        if self.calls == 1:
            yield {
                "kind": "tool_call_start",
                "index": 0,
                "id": "call_1",
                "name": self.tool_name,
            }
            yield {"kind": "tool_call_args_delta", "index": 0, "delta": "{}"}
            yield {"kind": "finish", "reason": "tool_calls"}
            return
        yield {"kind": "text_delta", "text": self.reply}
        yield {"kind": "finish", "reason": "stop"}


def test_visual_output_does_not_trigger_a_synthetic_review_round(
    tmp_path: Path,
    monkeypatch,
) -> None:
    async def make_visual(args: dict[str, Any], ctx) -> dict[str, Any]:
        del args
        path = tmp_path / "visual.png"
        path.write_bytes(b"visual")
        ctx.registry.register_output(
            "img-visual", kind="image", path=path, summary="visual result"
        )
        return {"status": "success", "asset_id": "img-visual", "kind": "image"}

    monkeypatch.setitem(loop_mod.DISPATCHER, "fake_visual", make_visual)
    client = _ToolThenNaturalReply("fake_visual", "图片已生成。")
    events: list[dict[str, Any]] = []
    loop = AgentLoopV3(
        session_id="no_predelivery_visual_gate",
        output_dir=tmp_path,
        gemini_client=client,  # type: ignore[arg-type]
        emit_event=events.append,
    )

    asyncio.run(loop.run_turn("生成一张图片"))

    assert client.calls == 2
    assert not [event for event in events if event.get("kind") == "completion_check"]
    assert [event for event in events if event.get("kind") == "turn_complete"]
    assert not any(
        "目标核对" in str(message.get("content"))
        or "主机验收账本" in str(message.get("content"))
        for batch in client.messages_seen
        for message in batch
    )


def test_failed_tool_is_reported_by_model_without_host_wrapup(
    tmp_path: Path,
    monkeypatch,
) -> None:
    async def fail(args: dict[str, Any], ctx) -> dict[str, Any]:
        del args, ctx
        return {"error": "failed", "error_code": "E_TEST", "recovery": "none"}

    monkeypatch.setitem(loop_mod.DISPATCHER, "fake_failed_tool", fail)
    reply = "这次调用失败了，我没有把它写成成功。"
    client = _ToolThenNaturalReply("fake_failed_tool", reply)
    events: list[dict[str, Any]] = []
    loop = AgentLoopV3(
        session_id="no_predelivery_failure_gate",
        output_dir=tmp_path,
        gemini_client=client,  # type: ignore[arg-type]
        emit_event=events.append,
    )

    asyncio.run(loop.run_turn("执行这个失败的工具"))

    assert client.calls == 2
    assert any(
        event.get("kind") == "model_text_delta" and event.get("delta") == reply
        for event in events
    )
    assert [event for event in events if event.get("kind") == "turn_complete"]
    assert not [event for event in events if event.get("kind") == "turn_wrapup"]
    assert not [event for event in events if event.get("reason") == "incomplete_goal"]
