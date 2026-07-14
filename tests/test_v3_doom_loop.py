"""Success-blind doom-loop guard for AgentLoopV3.

Ported from opencode's processor.ts (DOOM_LOOP_THRESHOLD=3). The existing
per-(tool, error_code) circuit breaker only trips on FAILURES. This adds a guard
that is independent of the result: if the last ``_DOOM_LOOP_THRESHOLD`` *dispatched*
tool calls in a turn are the SAME tool name with BYTE-IDENTICAL arguments, the turn
is repeating itself (an echo loop, not progress) and stops with a structured
``turn_error`` carrying ``reason: "doom_loop"``.

Two cases are pinned:
  * identical-args, always-succeeds  → trips at exactly _DOOM_LOOP_THRESHOLD calls
    (proves it does NOT loop forever and emits the doom-loop signal).
  * differing-args, always-succeeds  → never trips (proves real progress is safe).
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, AsyncIterator

import gemia.agent_loop_v3 as loop_mod
from gemia.agent_loop_v3 import AgentLoopV3, _DOOM_LOOP_THRESHOLD


class _AlwaysSucceeds:
    """Dispatcher that always returns a successful (non-raising) result, so the
    failure breaker never fires — isolating the success-blind doom-loop guard."""

    def __init__(self) -> None:
        self.n = 0

    async def __call__(self, args: dict[str, Any], ctx: Any) -> dict[str, Any]:
        self.n += 1
        return {"ok": True, "n": self.n}


class _CallsSameArgs:
    """Fake model that calls ``echo_tool`` with BYTE-IDENTICAL args every stream,
    up to a hard safety ceiling so a broken guard fails loudly instead of
    hanging the test."""

    model = "fake"

    def __init__(self, tool_name: str, *, ceiling: int = 30) -> None:
        self.calls = 0
        self._tool = tool_name
        self._ceiling = ceiling

    async def stream_turn(
        self, messages: list[dict[str, Any]], *, tools=None, temperature: float = 0.7
    ) -> AsyncIterator[dict[str, Any]]:
        del messages, tools, temperature
        self.calls += 1
        if self.calls > self._ceiling:  # safety net: guard should fire first
            yield {"kind": "text_delta", "text": "ceiling hit"}
            yield {"kind": "finish", "reason": "stop"}
            return
        yield {"kind": "tool_call_start", "index": 0, "id": f"c{self.calls}", "name": self._tool}
        # Identical args byte-for-byte on every call.
        yield {"kind": "tool_call_args_delta", "index": 0, "delta": '{"q": "same"}'}
        yield {"kind": "finish", "reason": "tool_calls"}


class _CallsDifferentArgs:
    """Fake model that calls ``echo_tool`` with DIFFERENT args each stream for a
    fixed number of turns, then ends with text — genuine progress, never a loop."""

    model = "fake"

    def __init__(self, tool_name: str, *, call_times: int) -> None:
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
            # Distinct args each call → never byte-identical.
            yield {"kind": "tool_call_args_delta", "index": 0, "delta": f'{{"q": "step-{self.calls}"}}'}
            yield {"kind": "finish", "reason": "tool_calls"}
            return
        yield {"kind": "text_delta", "text": "done"}
        yield {"kind": "finish", "reason": "stop"}


class _CallsSameArgsInOneBatch:
    model = "fake"

    async def stream_turn(
        self, messages: list[dict[str, Any]], *, tools=None, temperature: float = 0.7
    ) -> AsyncIterator[dict[str, Any]]:
        del messages, tools, temperature
        for index in range(4):
            yield {
                "kind": "tool_call_start",
                "index": index,
                "id": f"batch-{index}",
                "name": "echo_tool",
            }
            yield {
                "kind": "tool_call_args_delta",
                "index": index,
                "delta": '{"q": "same"}',
            }
        yield {"kind": "finish", "reason": "tool_calls"}


class _DiscoveryThenMutationAfterFullFallback:
    """The third discovery happens after full fallback and must be consumed."""

    model = "fake"

    def __init__(self) -> None:
        self.calls = 0

    async def stream_turn(
        self, messages: list[dict[str, Any]], *, tools=None, temperature: float = 0.7
    ) -> AsyncIterator[dict[str, Any]]:
        del messages, tools, temperature
        self.calls += 1
        if self.calls <= 3:
            yield {
                "kind": "tool_call_start",
                "index": 0,
                "id": f"search-{self.calls}",
                "name": "search_library",
            }
            yield {
                "kind": "tool_call_args_delta",
                "index": 0,
                "delta": f'{{"query":"candidate-{self.calls}"}}',
            }
            yield {"kind": "finish", "reason": "tool_calls"}
            return
        if self.calls == 4:
            yield {
                "kind": "tool_call_start",
                "index": 0,
                "id": "write-final",
                "name": "file_write",
            }
            yield {
                "kind": "tool_call_args_delta",
                "index": 0,
                "delta": '{"path":"result.txt","content":"done"}',
            }
            yield {"kind": "finish", "reason": "tool_calls"}
            return
        yield {"kind": "text_delta", "text": "done"}
        yield {"kind": "finish", "reason": "stop"}


def test_doom_loop_trips_after_threshold_identical_calls(tmp_path: Path, monkeypatch) -> None:
    """Identical (tool, byte-identical args) calls trip the doom-loop guard at
    exactly _DOOM_LOOP_THRESHOLD calls — even though every call SUCCEEDS, so the
    failure breaker is never involved. Proves the loop does NOT run forever."""
    disp = _AlwaysSucceeds()
    monkeypatch.setitem(loop_mod.DISPATCHER, "echo_tool", disp)

    client = _CallsSameArgs("echo_tool")
    events: list[dict[str, Any]] = []
    loop = AgentLoopV3(
        session_id="doom",
        output_dir=tmp_path,
        gemini_client=client,  # type: ignore[arg-type]
        emit_event=events.append,
    )

    asyncio.run(loop.run_turn("keep calling the same thing"))

    # Guard trips on the _DOOM_LOOP_THRESHOLD-th identical call: the model is
    # called exactly that many times and never reaches the safety ceiling.
    assert client.calls == _DOOM_LOOP_THRESHOLD == 3
    # The tool actually dispatched (succeeded) every time — failure breaker never
    # had anything to count.
    assert disp.n == _DOOM_LOOP_THRESHOLD
    # Exactly one structured doom-loop turn_error, with the doom signal.
    turn_errors = [e for e in events if e.get("kind") == "turn_error"]
    assert len(turn_errors) == 1
    err = turn_errors[0]
    assert err.get("reason") == "doom_loop"
    assert err.get("tool_name") == "echo_tool"
    assert err.get("repeat_count") == _DOOM_LOOP_THRESHOLD
    assert "doom loop" in err["error"].lower()
    # It is NOT the failure breaker (no "in a row this turn; stopping" failure msg
    # and the turn never reached turn_complete).
    assert not [e for e in events if e.get("kind") == "turn_complete"]


def test_distinct_args_do_not_bypass_objective_no_progress_guard(
    tmp_path: Path, monkeypatch
) -> None:
    """Different args avoid the byte-identical doom guard, but successful echo
    calls still cannot masquerade as objective ledger progress forever."""
    disp = _AlwaysSucceeds()
    monkeypatch.setitem(loop_mod.DISPATCHER, "echo_tool", disp)

    # Far more calls than the threshold, all with distinct args.
    n_calls = _DOOM_LOOP_THRESHOLD * 3
    client = _CallsDifferentArgs("echo_tool", call_times=n_calls)
    events: list[dict[str, Any]] = []
    loop = AgentLoopV3(
        session_id="no_doom",
        output_dir=tmp_path,
        gemini_client=client,  # type: ignore[arg-type]
        emit_event=events.append,
    )

    asyncio.run(loop.run_turn("do distinct steps"))

    assert client.calls == 5
    assert disp.n == 5 < n_calls
    # No byte-identical doom signal; the host instead stops after adjacent,
    # full-surface, then still-no-progress execution.
    assert not [e for e in events if e.get("reason") == "doom_loop"]
    assert any(e.get("reason") == "incomplete_goal" for e in events)
    assert not [e for e in events if e.get("kind") == "turn_complete"]


def test_full_fallback_result_is_consumed_before_incomplete_stop(
    tmp_path: Path, monkeypatch
) -> None:
    async def search(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
        del ctx
        return {"status": "success", "results": [args["query"]]}

    async def write(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
        del ctx
        path = tmp_path / args["path"]
        path.write_text(args["content"], encoding="utf-8")
        return {"status": "success", "output_path": str(path)}

    monkeypatch.setitem(loop_mod.DISPATCHER, "search_library", search)
    monkeypatch.setitem(loop_mod.DISPATCHER, "file_write", write)
    client = _DiscoveryThenMutationAfterFullFallback()
    events: list[dict[str, Any]] = []
    loop = AgentLoopV3(
        session_id="consume_full_result",
        output_dir=tmp_path,
        gemini_client=client,  # type: ignore[arg-type]
        emit_event=events.append,
    )

    asyncio.run(loop.run_turn("写一个 result.txt 文件"))

    # Three discovery rounds, the mutation, a stop, then the one-shot
    # completion-check confirmation round.
    assert client.calls == 6
    assert (tmp_path / "result.txt").read_text(encoding="utf-8") == "done"
    assert any(event.get("kind") == "turn_complete" for event in events)
    assert not any(event.get("reason") == "incomplete_goal" for event in events)


def test_doom_loop_settles_every_call_in_multi_call_batch(
    tmp_path: Path, monkeypatch
) -> None:
    disp = _AlwaysSucceeds()
    monkeypatch.setitem(loop_mod.DISPATCHER, "echo_tool", disp)
    events: list[dict[str, Any]] = []
    loop = AgentLoopV3(
        session_id="doom_batch",
        output_dir=tmp_path,
        gemini_client=_CallsSameArgsInOneBatch(),  # type: ignore[arg-type]
        emit_event=events.append,
    )

    asyncio.run(loop.run_turn("repeat in one batch"))

    assistant_ids = {
        call["id"]
        for message in loop._messages
        if message.get("role") == "assistant"
        for call in message.get("tool_calls", [])
    }
    result_ids = {
        message["tool_call_id"]
        for message in loop._messages
        if message.get("role") == "tool"
    }
    assert assistant_ids == result_ids == {f"batch-{index}" for index in range(4)}
    assert disp.n == _DOOM_LOOP_THRESHOLD
    assert any(
        event.get("kind") == "tool_exec_error"
        and event.get("call_id") == "batch-3"
        and event.get("error_code") == "E_DOOM_LOOP_CANCELLED"
        for event in events
    )


def test_is_doom_loop_pure_helper() -> None:
    """Unit-level proof of the comparison: byte-identical (name, args) repeated
    _DOOM_LOOP_THRESHOLD times is a loop; a differing tail or a too-short history
    is not."""
    same = ("echo_tool", '{"q": "same"}')
    # Exactly threshold identical → loop.
    assert AgentLoopV3._is_doom_loop([same] * _DOOM_LOOP_THRESHOLD) is True
    # One short → not yet.
    assert AgentLoopV3._is_doom_loop([same] * (_DOOM_LOOP_THRESHOLD - 1)) is False
    # Identical tool but ONE byte different in args → not a loop.
    diff_args = [("echo_tool", '{"q": "same"}')] * (_DOOM_LOOP_THRESHOLD - 1) + [
        ("echo_tool", '{"q": "same "}')  # trailing space → different bytes
    ]
    assert AgentLoopV3._is_doom_loop(diff_args) is False
    # Same args but a different tool name in the tail → not a loop.
    diff_name = [same] * (_DOOM_LOOP_THRESHOLD - 1) + [("other_tool", '{"q": "same"}')]
    assert AgentLoopV3._is_doom_loop(diff_name) is False
    # Only the LAST threshold entries matter: a noisy prefix then a clean run trips.
    assert (
        AgentLoopV3._is_doom_loop(
            [("a", "{}"), ("b", "{}")] + [same] * _DOOM_LOOP_THRESHOLD
        )
        is True
    )
