"""A genuine user decision is the only completion-related hard wait."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, AsyncIterator

import gemia.agent_loop_v3 as loop_mod

from gemia.agent_loop_v3 import AgentLoopV3


class _AskThenActThenReply:
    model = "fake"

    def __init__(self) -> None:
        self.calls = 0

    async def stream_turn(
        self,
        messages: list[dict[str, Any]],
        *,
        tools=None,
        temperature: float = 0.7,
    ) -> AsyncIterator[dict[str, Any]]:
        del messages, tools, temperature
        self.calls += 1
        if self.calls == 1:
            args = {
                "reason": "missing_source",
                "title": "Choose the source",
                "controls": {
                    "source": {
                        "type": "select",
                        "options": ["camera-a", "camera-b"],
                    }
                },
            }
            yield {
                "kind": "tool_call_start",
                "index": 0,
                "id": "ask_source",
                "name": "elicit",
            }
            yield {
                "kind": "tool_call_args_delta",
                "index": 0,
                "delta": json.dumps(args),
            }
            yield {"kind": "finish", "reason": "tool_calls"}
            return
        if self.calls == 2:
            yield {
                "kind": "tool_call_start",
                "index": 0,
                "id": "apply_choice",
                "name": "fake_mutation",
            }
            yield {"kind": "tool_call_args_delta", "index": 0, "delta": "{}"}
            yield {"kind": "finish", "reason": "tool_calls"}
            return
        yield {"kind": "text_delta", "text": "已按你选的素材处理。"}
        yield {"kind": "finish", "reason": "stop"}


def test_required_decision_blocks_mutation_until_answer_then_ends_naturally(
    tmp_path: Path,
    monkeypatch,
) -> None:
    mutations: list[str] = []

    async def mutate(args: dict[str, Any], ctx) -> dict[str, Any]:
        del args, ctx
        mutations.append("applied")
        return {"status": "success", "applied": True}

    monkeypatch.setitem(loop_mod.DISPATCHER, "fake_mutation", mutate)
    client = _AskThenActThenReply()
    events: list[dict[str, Any]] = []
    loop = AgentLoopV3(
        session_id="required_decision",
        output_dir=tmp_path,
        gemini_client=client,  # type: ignore[arg-type]
        emit_event=events.append,
    )

    async def exercise() -> None:
        task = asyncio.create_task(loop.run_turn("用我的镜头素材完成剪辑"))
        for _ in range(100):
            asks = [event for event in events if event.get("kind") == "ask_question"]
            if asks:
                break
            await asyncio.sleep(0.005)
        else:
            raise AssertionError("required decision was never emitted")

        question_id = asks[0]["question"]["question_id"]
        await asyncio.sleep(0.03)
        assert not task.done()
        assert mutations == []
        assert loop.deliver_ask_answer(question_id, {"source": "camera-a"}) is True
        await task

    asyncio.run(exercise())

    assert mutations == ["applied"]
    assert client.calls == 3
    assert [event for event in events if event.get("kind") == "turn_complete"]
    assert not [event for event in events if event.get("kind") == "completion_check"]
    assert not [event for event in events if event.get("reason") == "incomplete_goal"]
