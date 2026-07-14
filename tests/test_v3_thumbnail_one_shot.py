from __future__ import annotations

import asyncio
import base64
import copy
import json
from pathlib import Path
from typing import Any, AsyncIterator

import gemia.agent_loop_v3 as loop_mod
import pytest
from gemia.agent_loop_v3 import AgentLoopV3


_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8"
    "z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


class _Client:
    model = "fake"

    def __init__(self) -> None:
        self.calls = 0
        self.seen: list[list[dict[str, Any]]] = []

    async def stream_turn(
        self, messages: list[dict[str, Any]], *, tools=None, temperature=0.7
    ) -> AsyncIterator[dict[str, Any]]:
        del tools, temperature
        self.calls += 1
        self.seen.append(copy.deepcopy(messages))
        if self.calls == 1:
            yield {
                "kind": "tool_call_start",
                "index": 0,
                "id": "inspect-1",
                "name": "analyze_media",
            }
            yield {
                "kind": "tool_call_args_delta",
                "index": 0,
                "delta": '{"asset_id":"v_001"}',
            }
            yield {"kind": "finish", "reason": "tool_calls"}
            return
        yield {"kind": "text_delta", "text": "reviewed"}
        yield {"kind": "finish", "reason": "stop"}


class _RaisingAfterThumbnailClient(_Client):
    async def stream_turn(
        self, messages: list[dict[str, Any]], *, tools=None, temperature=0.7
    ) -> AsyncIterator[dict[str, Any]]:
        if self.calls == 0:
            async for event in super().stream_turn(
                messages, tools=tools, temperature=temperature
            ):
                yield event
            return
        self.calls += 1
        self.seen.append(copy.deepcopy(messages))
        raise RuntimeError("iterator failed before yielding")
        if False:  # pragma: no cover - keeps this an async generator
            yield {}


def test_ordinary_tool_thumbnail_is_consumed_by_exactly_one_model_call(
    tmp_path: Path, monkeypatch
) -> None:
    thumbnail = tmp_path / "thumb.png"
    thumbnail.write_bytes(_PNG)

    async def fake_analyze(args, ctx):
        return {
            "status": "ok",
            "thumbnail_for_next_message": True,
            "thumbnail_path": str(thumbnail),
            "summary": "reviewed source",
        }

    monkeypatch.setitem(loop_mod.DISPATCHER, "analyze_media", fake_analyze)
    client = _Client()
    loop = AgentLoopV3(
        session_id="thumbnail_once",
        output_dir=tmp_path,
        gemini_client=client,  # type: ignore[arg-type]
        emit_event=lambda event: None,
    )

    asyncio.run(loop.run_turn("分析素材"))

    serialized_calls = [json.dumps(call, ensure_ascii=False) for call in client.seen]
    calls_with_base64 = [text for text in serialized_calls if "data:image" in text]
    assert len(calls_with_base64) == 1
    assert "data:image" not in json.dumps(loop._messages, ensure_ascii=False)
    assert any(
        message.get("role") == "user" and "从上下文回收" in str(message.get("content"))
        for message in loop._messages
    )


def test_thumbnail_is_reclaimed_when_stream_iterator_raises(
    tmp_path: Path, monkeypatch
) -> None:
    thumbnail = tmp_path / "thumb.png"
    thumbnail.write_bytes(_PNG)

    async def fake_analyze(args, ctx):
        return {
            "status": "ok",
            "thumbnail_for_next_message": True,
            "thumbnail_path": str(thumbnail),
        }

    monkeypatch.setitem(loop_mod.DISPATCHER, "analyze_media", fake_analyze)
    client = _RaisingAfterThumbnailClient()
    loop = AgentLoopV3(
        session_id="thumbnail_raise",
        output_dir=tmp_path,
        gemini_client=client,  # type: ignore[arg-type]
        emit_event=lambda event: None,
    )

    with pytest.raises(RuntimeError, match="iterator failed"):
        asyncio.run(loop.run_turn("分析素材"))

    assert "data:image" in json.dumps(client.seen[-1], ensure_ascii=False)
    assert "data:image" not in json.dumps(loop._messages, ensure_ascii=False)
