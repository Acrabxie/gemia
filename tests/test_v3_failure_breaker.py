"""Per-tool consecutive-failure guidance for AgentLoopV3.

There is no longer a fixed cap on total tool steps per turn. The only
host behavior for repeated failures is: if the SAME tool fails to dispatch
``_REPEATED_FAILURE_NUDGE_THRESHOLD`` (=5) times in a row, the loop prompts
Gemini to change approach; a successful dispatch of that tool resets its streak.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, AsyncIterator

import gemia.agent_loop_v3 as loop_mod
from gemia.agent_loop_v3 import AgentLoopV3, _REPEATED_FAILURE_NUDGE_THRESHOLD
from gemia.errors import ToolError


class _RepeatsBuildThenStops:
    """Fake model that calls ``build`` with empty code until the nudge threshold,
    then stops with text. This proves repeated failures do not hard-stop the
    turn; the model remains in control."""

    model = "fake"

    def __init__(self, repeat_count: int = _REPEATED_FAILURE_NUDGE_THRESHOLD) -> None:
        self.calls = 0
        self._repeat_count = repeat_count

    async def stream_turn(
        self, messages: list[dict[str, Any]], *, tools=None, temperature: float = 0.7
    ) -> AsyncIterator[dict[str, Any]]:
        del messages, tools, temperature
        self.calls += 1
        if self.calls > self._repeat_count:
            yield {"kind": "text_delta", "text": "switching approach"}
            yield {"kind": "finish", "reason": "stop"}
            return
        yield {"kind": "tool_call_start", "index": 0, "id": f"c{self.calls}", "name": "build"}
        yield {"kind": "tool_call_args_delta", "index": 0, "delta": "{}"}
        yield {"kind": "finish", "reason": "tool_calls"}


def test_repeated_failure_nudge_does_not_stop_turn(tmp_path: Path) -> None:
    client = _RepeatsBuildThenStops()
    events: list[dict[str, Any]] = []
    loop = AgentLoopV3(
        session_id="failure_nudge",
        output_dir=tmp_path,
        gemini_client=client,  # type: ignore[arg-type]
        emit_event=events.append,
    )

    asyncio.run(loop.run_turn("build something broken"))

    # 5 consecutive build failures → a model-facing nudge is appended, but the
    # host does not stop AT the threshold. The fake model remains in control,
    # emits text, then the completion ledger widens adjacent/full routes before
    # honestly ending incomplete because the failures were never repaired.
    assert client.calls == _REPEATED_FAILURE_NUDGE_THRESHOLD + 3
    assert any(e.get("reason") == "incomplete_goal" for e in events)
    assert not [e for e in events if e.get("kind") == "turn_complete"]
    # Every attempt surfaced an error to the model — none silently dropped.
    assert sum(1 for e in events if e.get("kind") == "tool_exec_error") == 5
    nudges = [
        m for m in loop._messages
        if m.get("role") == "user"
        and "Repeated tool failure guidance" in str(m.get("content"))
    ]
    assert len(nudges) == 1
    assert "build" in nudges[0]["content"]


class _Flaky:
    """Stateful dispatcher: raises on calls 1-4, succeeds on call 5, raises on
    6-9. A working streak tracker (with success-reset) never reaches 5-in-a-row."""

    def __init__(self) -> None:
        self.n = 0

    async def __call__(self, args: dict[str, Any], ctx: Any) -> dict[str, Any]:
        self.n += 1
        if self.n == 5:
            return {"ok": True}
        raise RuntimeError(f"flaky failure #{self.n}")


class _CallsFlaky:
    """Fake model: calls ``flaky`` for the first 9 turns, then ends with text."""

    model = "fake"

    def __init__(self) -> None:
        self.calls = 0

    async def stream_turn(
        self, messages: list[dict[str, Any]], *, tools=None, temperature: float = 0.7
    ) -> AsyncIterator[dict[str, Any]]:
        del messages, tools, temperature
        self.calls += 1
        if self.calls <= 9:
            yield {"kind": "tool_call_start", "index": 0, "id": f"c{self.calls}", "name": "flaky"}
            yield {"kind": "tool_call_args_delta", "index": 0, "delta": "{}"}
            yield {"kind": "finish", "reason": "tool_calls"}
            return
        yield {"kind": "text_delta", "text": "done"}
        yield {"kind": "finish", "reason": "stop"}


def test_failure_nudge_streak_resets_on_success(tmp_path: Path, monkeypatch) -> None:
    flaky = _Flaky()
    monkeypatch.setitem(loop_mod.DISPATCHER, "flaky", flaky)

    client = _CallsFlaky()
    events: list[dict[str, Any]] = []
    loop = AgentLoopV3(
        session_id="failure_nudge_reset",
        output_dir=tmp_path,
        gemini_client=client,  # type: ignore[arg-type]
        emit_event=events.append,
    )

    asyncio.run(loop.run_turn("exercise flaky"))

    # 4 fails, 1 success (resets streak), 4 fails — never 5 in a row. The turn
    # runs to natural completion (call #10 with no tool calls). Without the
    # reset, the streak would hit 5 on the 6th flaky call and produce a nudge.
    # The final four failures remain unresolved, so after the one-shot gate and
    # full-route retry the ledger ends incomplete (the success still reset the
    # repeated-failure streak as asserted below).
    assert client.calls == 12
    assert any(e.get("reason") == "incomplete_goal" for e in events)
    assert not [e for e in events if e.get("kind") == "turn_complete"]


class _RaisesToolError:
    """Dispatcher that raises a typed ToolError with rich, actionable fields —
    the fuel the model needs to self-correct precisely."""

    async def __call__(self, args: dict[str, Any], ctx: Any) -> dict[str, Any]:
        raise ToolError(
            "'black and white' is not an available look.",
            code="E_UNSUPPORTED",
            recovery="fix_args",
            valid_options=["warm", "cool", "neutral"],
            hint="Pick a named look.",
        )


class _CallsToolThenStops:
    """Fake model: calls ``tool_name`` ``call_times`` times (reacting to each
    failure), then ends with text — the model explaining / moving on."""

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
            yield {"kind": "tool_call_args_delta", "index": 0, "delta": "{}"}
            yield {"kind": "finish", "reason": "tool_calls"}
            return
        yield {"kind": "text_delta", "text": "no such look — telling the user."}
        yield {"kind": "finish", "reason": "stop"}


def test_tool_error_surfaces_structured_fields(tmp_path: Path, monkeypatch) -> None:
    """A raised ToolError must reach BOTH the SSE stream and the model with its
    structure intact — not flattened to a bare string."""
    monkeypatch.setitem(loop_mod.DISPATCHER, "demo_tool", _RaisesToolError())
    client = _CallsToolThenStops("demo_tool", call_times=1)
    events: list[dict[str, Any]] = []
    loop = AgentLoopV3(
        session_id="typed_err",
        output_dir=tmp_path,
        gemini_client=client,  # type: ignore[arg-type]
        emit_event=events.append,
    )

    asyncio.run(loop.run_turn("make it black and white"))

    # (a) the SSE tool_exec_error event carries the typed fields.
    errs = [e for e in events if e.get("kind") == "tool_exec_error"]
    assert len(errs) == 1
    ev = errs[0]
    assert ev["error_code"] == "E_UNSUPPORTED"
    assert ev["recovery"] == "fix_args"
    assert ev["valid_options"] == ["warm", "cool", "neutral"]
    assert ev["hint"]

    # (b) the model-facing tool_result message carries the same structure.
    tool_msgs = [m for m in loop._messages if m.get("role") == "tool"]
    assert tool_msgs, "expected a tool_result fed back to the model"
    payload = json.loads(tool_msgs[-1]["content"])
    assert payload["error_code"] == "E_UNSUPPORTED"
    assert payload["recovery"] == "fix_args"
    assert payload["valid_options"] == ["warm", "cool", "neutral"]


class _RaisesAlternatingCodes:
    """Always raises, but alternates error_code each call. A model 'adapting'
    (different failure class) must not look like runaway."""

    _codes = ["E_BAD_ARG", "E_UNSUPPORTED"]

    def __init__(self) -> None:
        self.n = 0

    async def __call__(self, args: dict[str, Any], ctx: Any) -> dict[str, Any]:
        code = self._codes[self.n % len(self._codes)]
        self.n += 1
        raise ToolError(f"failure #{self.n}", code=code, recovery="fix_args")


def test_failure_nudge_soft_resets_when_error_code_changes(tmp_path: Path, monkeypatch) -> None:
    """Strictly alternating error codes never form a 5-long same-(tool,code)
    streak, so no repeated-failure nudge is appended even across 9 failures."""
    monkeypatch.setitem(loop_mod.DISPATCHER, "adapt_tool", _RaisesAlternatingCodes())
    client = _CallsToolThenStops("adapt_tool", call_times=9)
    events: list[dict[str, Any]] = []
    loop = AgentLoopV3(
        session_id="soft_reset",
        output_dir=tmp_path,
        gemini_client=client,  # type: ignore[arg-type]
        emit_event=events.append,
    )

    asyncio.run(loop.run_turn("keep adapting"))

    # 9 tool turns + closing text + gate + full-route retry = 12; unresolved
    # failures end incomplete, but alternating codes still never trigger the
    # repeated-same-error guidance.
    assert client.calls == 12
    assert any(e.get("reason") == "incomplete_goal" for e in events)
    assert not [e for e in events if e.get("kind") == "turn_complete"]
    assert sum(1 for e in events if e.get("kind") == "tool_exec_error") == 9
    assert not [
        m for m in loop._messages
        if m.get("role") == "user"
        and "Repeated tool failure guidance" in str(m.get("content"))
    ]


class _ReturnsFailure:
    async def __call__(self, args: dict[str, Any], ctx: Any) -> dict[str, Any]:
        return {
            "status": "failed",
            "error": "renderer exited",
            "exit_code": 7,
        }


def test_returned_failure_is_error_not_success(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setitem(loop_mod.DISPATCHER, "returned_failure", _ReturnsFailure())
    client = _CallsToolThenStops("returned_failure", call_times=1)
    events: list[dict[str, Any]] = []
    loop = AgentLoopV3(
        session_id="returned_failure",
        output_dir=tmp_path,
        gemini_client=client,  # type: ignore[arg-type]
        emit_event=events.append,
    )

    asyncio.run(loop.run_turn("render this"))

    errors = [e for e in events if e.get("kind") == "tool_exec_error"]
    results = [e for e in events if e.get("kind") == "tool_exec_result"]
    assert len(errors) == 1
    assert errors[0]["error_code"] == "E_PROCESS_EXIT"
    assert results == []
    assert any(
        "returned_failure" in str(message.get("content"))
        for message in loop._messages
        if message.get("role") == "user"
    )


class _ReturnedFailuresAroundNoop:
    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, args: dict[str, Any], ctx: Any) -> dict[str, Any]:
        self.calls += 1
        if self.calls == 5:
            return {"status": "ok", "applied": False}
        return {"status": "failed", "error_code": "E_RENDER"}


def test_noop_does_not_clear_an_unresolved_failure_streak(
    tmp_path: Path, monkeypatch
) -> None:
    dispatcher = _ReturnedFailuresAroundNoop()
    monkeypatch.setitem(loop_mod.DISPATCHER, "failure_noop_failure", dispatcher)
    client = _CallsToolThenStops("failure_noop_failure", call_times=6)
    loop = AgentLoopV3(
        session_id="failure_noop_failure",
        output_dir=tmp_path,
        gemini_client=client,  # type: ignore[arg-type]
        emit_event=lambda event: None,
    )

    asyncio.run(loop.run_turn("render this"))

    nudges = [
        message
        for message in loop._messages
        if message.get("role") == "user"
        and "Repeated tool failure guidance" in str(message.get("content"))
    ]
    assert len(nudges) == 1
