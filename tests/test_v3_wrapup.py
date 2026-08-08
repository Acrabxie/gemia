"""Graceful non-success wrap-up for AgentLoopV3 (opencode pattern #5).

When the budget is exhausted or the doom-loop guard fires, ``_drive_turn``
emits a short
``turn_wrapup`` event whose ``message`` explains the actual stop reason in one
natural sentence, synthesized LOCALLY (no extra model call). Tool / asset counts
stay as structured event telemetry instead of becoming a canned status report.

Pinned here:
  * a retryable model stream error reconnects six times with increasing delay,
    then emits one ``turn_error`` and no explanatory ``turn_wrapup``;
  * a normal successful turn does NOT emit a spurious ``turn_wrapup``;
  * non-retryable model errors fail immediately.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, AsyncIterator

import gemia.agent_loop_v3 as loop_mod
from gemia.agent_loop_v3 import AgentLoopV3


class _StreamErrors:
    """Fake model that surfaces a stream error immediately."""

    model = "fake"

    def __init__(self) -> None:
        self.calls = 0

    async def stream_turn(
        self, messages: list[dict[str, Any]], *, tools=None, temperature: float = 0.7
    ) -> AsyncIterator[dict[str, Any]]:
        del messages, tools, temperature
        self.calls += 1
        yield {"kind": "error", "error": "simulated stream failure"}


class _PartialThenErrors:
    model = "fake"

    def __init__(self) -> None:
        self.calls = 0

    async def stream_turn(
        self, messages: list[dict[str, Any]], *, tools=None, temperature: float = 0.7
    ) -> AsyncIterator[dict[str, Any]]:
        del messages, tools, temperature
        self.calls += 1
        yield {"kind": "text_delta", "text": "starting"}
        yield {
            "kind": "tool_call_start",
            "index": 0,
            "id": "partial_call",
            "name": "partial_tool",
        }
        yield {"kind": "tool_call_args_delta", "index": 0, "delta": '{"x":'}
        yield {"kind": "error", "error": "upstream failed mid-frame"}


def test_stream_error_retries_six_times_then_emits_only_error(
    tmp_path: Path, monkeypatch
) -> None:
    delays: list[float] = []

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr(loop_mod.asyncio, "sleep", fake_sleep)
    client = _StreamErrors()
    events: list[dict[str, Any]] = []
    loop = AgentLoopV3(
        session_id="wrapup_stream_error",
        output_dir=tmp_path,
        gemini_client=client,  # type: ignore[arg-type]
        emit_event=events.append,
    )

    asyncio.run(loop.run_turn("build something broken"))

    assert client.calls == 7  # initial attempt + six reconnects
    assert delays == [2.0, 4.0, 8.0, 16.0, 32.0, 64.0]
    resets = [e for e in events if e.get("kind") == "model_stream_reset"]
    assert [event["retry"] for event in resets] == [1, 2, 3, 4, 5, 6]
    assert [event["delay_sec"] for event in resets] == delays
    assert {event["error_class"] for event in resets} == {"stream_failure"}

    turn_errors = [e for e in events if e.get("kind") == "turn_error"]
    assert len(turn_errors) == 1
    assert turn_errors[0]["reason"] == "stream_error"
    assert "simulated stream failure" in turn_errors[0]["error"]
    assert not [e for e in events if e.get("kind") == "turn_wrapup"]


class _RecoversAfterThreeDisconnects:
    model = "fake"

    def __init__(self) -> None:
        self.calls = 0

    async def stream_turn(
        self, messages: list[dict[str, Any]], *, tools=None, temperature: float = 0.7
    ) -> AsyncIterator[dict[str, Any]]:
        del messages, tools, temperature
        self.calls += 1
        if self.calls <= 3:
            yield {"kind": "text_delta", "text": "discard this partial"}
            yield {"kind": "error", "error": "connection reset"}
            return
        yield {"kind": "text_delta", "text": "recovered response"}
        yield {"kind": "finish", "reason": "stop"}


def test_stream_reconnect_recovers_without_final_error(
    tmp_path: Path, monkeypatch
) -> None:
    delays: list[float] = []

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr(loop_mod.asyncio, "sleep", fake_sleep)
    client = _RecoversAfterThreeDisconnects()
    events: list[dict[str, Any]] = []
    loop = AgentLoopV3(
        session_id="stream_recovers",
        output_dir=tmp_path,
        gemini_client=client,  # type: ignore[arg-type]
        emit_event=events.append,
    )

    asyncio.run(loop.run_turn("build something"))

    assert client.calls == 4
    assert delays == [2.0, 4.0, 8.0]
    assert len([e for e in events if e.get("kind") == "model_stream_reset"]) == 3
    assert {
        e["error_class"]
        for e in events
        if e.get("kind") == "model_stream_reset"
    } == {"connection_reset"}
    assert not [e for e in events if e.get("kind") == "turn_error"]
    assert [e for e in events if e.get("kind") == "turn_complete"]
    assert any(
        event.get("kind") == "model_text_delta"
        and event.get("delta") == "recovered response"
        for event in events
    )


def test_partial_text_then_error_never_dispatches_or_completes(
    tmp_path: Path, monkeypatch
) -> None:
    dispatched = False

    async def forbidden_dispatch(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
        nonlocal dispatched
        dispatched = True
        return {"ok": True}

    async def no_wait(delay: float) -> None:
        del delay

    monkeypatch.setattr(loop_mod.asyncio, "sleep", no_wait)
    monkeypatch.setitem(loop_mod.DISPATCHER, "partial_tool", forbidden_dispatch)
    events: list[dict[str, Any]] = []
    client = _PartialThenErrors()
    loop = AgentLoopV3(
        session_id="wrapup_partial_error",
        output_dir=tmp_path,
        gemini_client=client,  # type: ignore[arg-type]
        emit_event=events.append,
    )

    asyncio.run(loop.run_turn("make an asset"))

    assert dispatched is False
    assert client.calls == 7
    assert len([e for e in events if e.get("kind") == "model_stream_reset"]) == 6
    assert len([e for e in events if e.get("kind") == "turn_error"]) == 1
    assert not [e for e in events if e.get("kind") == "turn_wrapup"]
    assert not [e for e in events if e.get("kind") == "turn_complete"]
    assert not [e for e in events if e.get("kind") == "tool_exec_start"]


class _AuthErrors:
    model = "fake"

    def __init__(self) -> None:
        self.calls = 0

    async def stream_turn(
        self, messages: list[dict[str, Any]], *, tools=None, temperature: float = 0.7
    ) -> AsyncIterator[dict[str, Any]]:
        del messages, tools, temperature
        self.calls += 1
        yield {"kind": "error", "error": "HTTP 401: invalid API key"}


def test_non_retryable_stream_error_fails_immediately(tmp_path: Path) -> None:
    client = _AuthErrors()
    events: list[dict[str, Any]] = []
    loop = AgentLoopV3(
        session_id="wrapup_auth_error",
        output_dir=tmp_path,
        gemini_client=client,  # type: ignore[arg-type]
        emit_event=events.append,
    )

    asyncio.run(loop.run_turn("build something"))

    assert client.calls == 1
    assert not [e for e in events if e.get("kind") == "model_stream_reset"]
    turn_errors = [e for e in events if e.get("kind") == "turn_error"]
    assert len(turn_errors) == 1
    assert turn_errors[0]["error"] == "HTTP 401: invalid API key"
    assert not [e for e in events if e.get("kind") == "turn_wrapup"]


class _AlwaysSucceeds:
    """Dispatcher that always returns a successful (non-raising) result."""

    def __init__(self) -> None:
        self.n = 0

    async def __call__(self, args: dict[str, Any], ctx: Any) -> dict[str, Any]:
        self.n += 1
        return {"ok": True, "n": self.n}


class _CallsToolThenStops:
    """Fake model: calls ``tool_name`` with DISTINCT args ``call_times`` times,
    then ends with text — a normal, healthy turn that completes successfully."""

    model = "fake"

    def __init__(self, tool_name: str, call_times: int) -> None:
        self.calls = 0
        self._tool = tool_name
        self._call_times = call_times

    async def stream_turn(
        self, messages: list[dict[str, Any]], *, tools=None, temperature: float = 0.7
    ) -> AsyncIterator[dict[str, Any]]:
        del messages, tools, temperature
        self.calls += 1
        if self.calls <= self._call_times:
            yield {"kind": "tool_call_start", "index": 0, "id": f"c{self.calls}", "name": self._tool}
            # Distinct args each call → no doom loop, real progress.
            yield {"kind": "tool_call_args_delta", "index": 0, "delta": f'{{"q": "step-{self.calls}"}}'}
            yield {"kind": "finish", "reason": "tool_calls"}
            return
        yield {"kind": "text_delta", "text": "all done"}
        yield {"kind": "finish", "reason": "stop"}


def test_no_wrapup_on_successful_turn(tmp_path: Path, monkeypatch) -> None:
    """Control: a normal successful turn (tools succeed, turn_complete) must NOT
    emit a spurious ``turn_wrapup`` — the wrap-up is only for non-success exits."""
    disp = _AlwaysSucceeds()
    monkeypatch.setitem(loop_mod.DISPATCHER, "good_tool", disp)

    client = _CallsToolThenStops("good_tool", call_times=2)
    events: list[dict[str, Any]] = []
    loop = AgentLoopV3(
        session_id="wrapup_success",
        output_dir=tmp_path,
        gemini_client=client,  # type: ignore[arg-type]
        emit_event=events.append,
    )

    asyncio.run(loop.run_turn("do two clean steps"))

    # The turn completed honestly.
    assert [e for e in events if e.get("kind") == "turn_complete"]
    assert not [e for e in events if e.get("kind") == "turn_error"]
    # And produced NO graceful wrap-up — that is only for the failure exits.
    assert not [e for e in events if e.get("kind") == "turn_wrapup"]


def test_synthesize_wrapup_message_pure_helper() -> None:
    """The LOCAL fallback names the stop naturally without a report template."""
    # Doom loop, work partially done.
    msg = AgentLoopV3._synthesize_wrapup_message(
        "doom_loop",
        tools_succeeded=2,
        tools_failed=5,
        assets_produced=1,
        tool_name="echo_tool",
    )
    assert "陷入了重复" in msg
    assert "echo_tool" not in msg
    assert "已完成：" not in msg
    assert "仍待处理：" not in msg
    assert "你让我继续" not in msg

    # Budget exhaustion, nothing done.
    msg2 = AgentLoopV3._synthesize_wrapup_message(
        "budget_exhausted",
        tools_succeeded=0,
        tools_failed=0,
        assets_produced=0,
    )
    assert "执行预算已经用完" in msg2
    assert "未完成的部分没有被算作成功" in msg2

    # Doom-loop reporting stays human-facing and does not leak tool names.
    msg3 = AgentLoopV3._synthesize_wrapup_message(
        "doom_loop",
        tools_succeeded=0,
        tools_failed=0,
        assets_produced=0,
        tool_name="echo_tool",
    )
    assert "陷入了重复" in msg3
    assert "echo_tool" not in msg3

    # Unknown mechanical stops use a generic fallback; there is no host-owned
    # incomplete-goal verdict or fixed "cannot count as complete" sentence.
    msg5 = AgentLoopV3._synthesize_wrapup_message(
        "unknown_stop",
        tools_succeeded=15,
        tools_failed=4,
        assets_produced=0,
    )
    assert msg5 == "这轮执行没有完整结束。"
    assert "还不能算完成" not in msg5
    assert "我先停在这里" not in msg5
    assert "你让我继续" not in msg5

    source = Path(loop_mod.__file__).read_text(encoding="utf-8")
    assert "执行过程中模型连接中断了，未完成的部分没有被算作成功。" not in source

    static_root = Path(loop_mod.__file__).resolve().parent.parent / "static" / "v3"
    frontend = (static_root / "v3.js").read_text(encoding="utf-8")
    preview = (static_root / "preview.html").read_text(encoding="utf-8")
    assert "Error: ${errorReason}" in frontend
    assert "Error: ${errorReason}" in preview
