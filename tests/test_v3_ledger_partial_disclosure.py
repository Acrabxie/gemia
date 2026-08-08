"""A recoverable library failure reaches an honest model-authored answer.

The lived bug: an editing turn hard-errored with "host acceptance ledger
remains incomplete" and DISCARDED the model's honest partial explanation ("I
removed the title but couldn't remove the original shape"), leaving the user an
opaque halt. The tool result is evidence for the model; the host does not add a
second completion verdict. This test drives a real ``AgentLoopV3`` where a
mutating tool fails recoverably and the model then explains honestly.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, AsyncIterator

import gemia.agent_loop_v3 as loop_mod
from gemia.agent_loop_v3 import AgentLoopV3


_HONEST_PARTIAL = (
    "我删掉了标题图层，但原始 demo 形状因为图层 ID 的问题没能删除——"
    "要我换个方式重试吗？"
)


async def _fake_edit_recoverable_failure(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    """A mutating verb that fails with a typed, recoverable error (not a crash)."""
    del args, ctx
    return {
        "applied": False,
        "error_code": "E_NOT_FOUND",
        "error_message": "delete_layer: layer not found",
        "recovery": "none",
    }


class _FailsThenExplainsHonestly:
    """call 1 → a mutating tool call that fails recoverably; every call after →
    an honest partial explanation in prose, no tool calls (the model has done
    what it could and is reporting the shortfall)."""

    model = "fake"

    def __init__(self) -> None:
        self.calls = 0

    async def stream_turn(
        self, messages: list[dict[str, Any]], *, tools=None, temperature: float = 0.7
    ) -> AsyncIterator[dict[str, Any]]:
        del messages, tools, temperature
        self.calls += 1
        if self.calls == 1:
            yield {"kind": "tool_call_start", "index": 0, "id": "e1", "name": "fake_edit"}
            yield {"kind": "tool_call_args_delta", "index": 0, "delta": "{}"}
            yield {"kind": "finish", "reason": "tool_calls"}
            return
        yield {"kind": "text_delta", "text": _HONEST_PARTIAL}
        yield {"kind": "finish", "reason": "stop"}


def test_recoverable_library_failure_degrades_to_partial(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setitem(loop_mod.DISPATCHER, "fake_edit", _fake_edit_recoverable_failure)

    client = _FailsThenExplainsHonestly()
    events: list[dict[str, Any]] = []
    loop = AgentLoopV3(
        session_id="fm3_partial_disclosure",
        output_dir=tmp_path,
        gemini_client=client,  # type: ignore[arg-type]
        emit_event=events.append,
    )

    # The first tool call creates an observational activity record.
    asyncio.run(loop.run_turn("删除标题图层并重新导出"))

    assert not [e for e in events if e.get("reason") == "incomplete_goal"]
    assert [e for e in events if e.get("kind") == "turn_complete"]

    # …AND the model's honest partial explanation was DELIVERED, not discarded.
    delivered = "".join(
        e.get("delta", "") for e in events if e.get("kind") == "model_text_delta"
    )
    assert _HONEST_PARTIAL in delivered, (
        "the model's honest partial explanation was discarded — the user only got "
        f"an opaque halt. delivered text deltas = {delivered!r}"
    )

    # No host-authored verdict is appended after the model's own explanation.
    assert not [e for e in events if e.get("kind") == "turn_wrapup"]
