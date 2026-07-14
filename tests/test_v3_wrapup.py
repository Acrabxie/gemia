"""Graceful non-success wrap-up for AgentLoopV3 (opencode pattern #5).

When the budget is exhausted, the doom-loop guard fires, or the model stream
errors out, ``_drive_turn`` used to emit a bare turn_error and return — the user
got an unexplained stop. This adds an ADDITIVE graceful wrap-up: at each of those
non-success exit points, *in addition to* the existing turn_error event, the loop emits a short
``turn_wrapup`` event whose ``message`` explains 'stopped because X; here's what
was / wasn't done', synthesized LOCALLY (no extra model call) from the turn's
tool / asset counts.

Pinned here:
  * a fake client that emits a model stream error → ``turn_wrapup`` is emitted
    with the stop reason, alongside the existing ``turn_error``;
  * a normal successful turn does NOT emit a spurious ``turn_wrapup``;
  * an exception raised inside wrap-up synthesis does not break the turn (the
    original turn_error still returns cleanly).
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

    async def stream_turn(
        self, messages: list[dict[str, Any]], *, tools=None, temperature: float = 0.7
    ) -> AsyncIterator[dict[str, Any]]:
        del messages, tools, temperature
        yield {"kind": "text_delta", "text": "starting"}
        yield {
            "kind": "tool_call_start",
            "index": 0,
            "id": "partial_call",
            "name": "partial_tool",
        }
        yield {"kind": "tool_call_args_delta", "index": 0, "delta": '{"x":'}
        yield {"kind": "error", "error": "upstream failed mid-frame"}


def test_wrapup_emitted_on_stream_error(tmp_path: Path) -> None:
    """When the model stream errors, a ``turn_wrapup`` event is emitted with
    the stop reason — IN ADDITION to the existing ``turn_error``."""
    client = _StreamErrors()
    events: list[dict[str, Any]] = []
    loop = AgentLoopV3(
        session_id="wrapup_stream_error",
        output_dir=tmp_path,
        gemini_client=client,  # type: ignore[arg-type]
        emit_event=events.append,
    )

    asyncio.run(loop.run_turn("build something broken"))

    # The existing turn_error is still emitted (not replaced).
    turn_errors = [e for e in events if e.get("kind") == "turn_error"]
    assert len(turn_errors) == 1
    assert "simulated stream failure" in turn_errors[0]["error"]

    # The ADDITIVE wrap-up event is emitted with the stop reason and a message.
    wrapups = [e for e in events if e.get("kind") == "turn_wrapup"]
    assert len(wrapups) == 1, "expected exactly one graceful wrap-up event"
    wrap = wrapups[0]
    assert wrap["reason"] == "stream_error"
    # The message explains the stop AND what was / wasn't done.
    msg = wrap["message"]
    assert "Stopped because" in msg
    assert "stream" in msg.lower()
    assert wrap["tools_failed"] == 0
    assert wrap["tools_succeeded"] == 0
    assert wrap["assets_produced"] == 0

    # Ordering: the wrap-up comes AFTER the turn_error (explains it).
    ti_err = next(i for i, e in enumerate(events) if e.get("kind") == "turn_error")
    ti_wrap = next(i for i, e in enumerate(events) if e.get("kind") == "turn_wrapup")
    assert ti_wrap > ti_err


def test_partial_text_then_error_never_dispatches_or_completes(
    tmp_path: Path, monkeypatch
) -> None:
    dispatched = False

    async def forbidden_dispatch(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
        nonlocal dispatched
        dispatched = True
        return {"ok": True}

    monkeypatch.setitem(loop_mod.DISPATCHER, "partial_tool", forbidden_dispatch)
    events: list[dict[str, Any]] = []
    loop = AgentLoopV3(
        session_id="wrapup_partial_error",
        output_dir=tmp_path,
        gemini_client=_PartialThenErrors(),  # type: ignore[arg-type]
        emit_event=events.append,
    )

    asyncio.run(loop.run_turn("make an asset"))

    assert dispatched is False
    assert len([e for e in events if e.get("kind") == "turn_error"]) == 1
    assert len([e for e in events if e.get("kind") == "turn_wrapup"]) == 1
    assert not [e for e in events if e.get("kind") == "turn_complete"]
    assert not [e for e in events if e.get("kind") == "tool_exec_start"]


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


def test_wrapup_synthesis_exception_does_not_break_turn(
    tmp_path: Path, monkeypatch
) -> None:
    """If the wrap-up message synthesis raises, the turn must not break: the
    original stream error is still emitted, no exception escapes, and no wrap-up
    event leaks. This proves the try/except contract — wrap-up failures are
    swallowed."""

    def _boom(*args: Any, **kwargs: Any) -> str:
        raise RuntimeError("synthesis blew up")

    # Make the LOCAL synthesis explode at the exact point the wrap-up runs.
    monkeypatch.setattr(
        AgentLoopV3, "_synthesize_wrapup_message", staticmethod(_boom)
    )

    client = _StreamErrors()
    events: list[dict[str, Any]] = []
    loop = AgentLoopV3(
        session_id="wrapup_boom",
        output_dir=tmp_path,
        gemini_client=client,  # type: ignore[arg-type]
        emit_event=events.append,
    )

    # Must NOT raise — the wrap-up try/except swallows the synthesis failure.
    asyncio.run(loop.run_turn("build something broken"))

    # The existing turn_error is still emitted (the loop still stopped cleanly
    # via its normal stream-error path).
    turn_errors = [e for e in events if e.get("kind") == "turn_error"]
    assert len(turn_errors) == 1
    # The wrap-up emission was attempted but its synthesis raised, so no
    # turn_wrapup event leaks (it was swallowed before emit).
    assert not [e for e in events if e.get("kind") == "turn_wrapup"]


def test_synthesize_wrapup_message_pure_helper() -> None:
    """Unit-level proof that the LOCAL synthesis builds a sensible explanatory
    summary from the stop reason + counts, with no model call involved."""
    # Doom loop, work partially done.
    msg = AgentLoopV3._synthesize_wrapup_message(
        "doom_loop",
        tools_succeeded=2,
        tools_failed=5,
        assets_produced=1,
        tool_name="echo_tool",
    )
    assert "Stopped because" in msg
    assert "echo_tool" in msg
    assert "doom loop" in msg.lower()
    assert "1 asset" in msg
    assert "2 tool calls succeeded" in msg
    assert "5 tool calls failed" in msg

    # Budget exhaustion, nothing done.
    msg2 = AgentLoopV3._synthesize_wrapup_message(
        "budget_exhausted",
        tools_succeeded=0,
        tools_failed=0,
        assets_produced=0,
    )
    assert "budget" in msg2.lower()
    assert "nothing was completed" in msg2
    assert "no failures were recorded" in msg2

    # Doom loop names the tool.
    msg3 = AgentLoopV3._synthesize_wrapup_message(
        "doom_loop",
        tools_succeeded=0,
        tools_failed=0,
        assets_produced=0,
        tool_name="echo_tool",
    )
    assert "doom loop" in msg3.lower()
    assert "echo_tool" in msg3

    # Stream error path.
    msg4 = AgentLoopV3._synthesize_wrapup_message(
        "stream_error",
        tools_succeeded=1,
        tools_failed=0,
        assets_produced=0,
    )
    assert "stream errored" in msg4.lower()
    assert "1 tool call succeeded" in msg4
