"""Immediate tool-failure direction guidance for AgentLoopV3.

There is no fixed cap on total tool steps per turn. Every failed tool call asks
the model to evaluate whether a retry has evidence behind it or whether it
should change direction. This guidance never hard-stops the turn.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, AsyncIterator

import gemia.agent_loop_v3 as loop_mod
from gemia.agent_loop_v3 import AgentLoopV3, _REPEATED_FAILURE_NUDGE_THRESHOLD
from gemia.errors import ToolError
from gemia.skill_store import SKILL_RECALL_GUIDANCE_STATE_KEY


class _RepeatsBuildThenStops:
    """Fake model that fails once, then stops with text after direction guidance.

    This proves the first-failure nudge does not hard-stop the turn; the model
    remains in control.
    """

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


def test_first_failure_direction_check_does_not_stop_turn(tmp_path: Path) -> None:
    client = _RepeatsBuildThenStops()
    events: list[dict[str, Any]] = []
    loop = AgentLoopV3(
        session_id="failure_nudge",
        output_dir=tmp_path,
        gemini_client=client,  # type: ignore[arg-type]
        emit_event=events.append,
    )

    asyncio.run(loop.run_turn("build something broken"))

    # The first build failure appends a model-facing direction check. The host
    # does not stop; the fake model's next no-tool response ends naturally.
    assert client.calls == _REPEATED_FAILURE_NUDGE_THRESHOLD + 1
    assert not any(e.get("reason") == "incomplete_goal" for e in events)
    assert [e for e in events if e.get("kind") == "turn_complete"]
    # Every attempt surfaced an error to the model — none silently dropped.
    assert sum(1 for e in events if e.get("kind") == "tool_exec_error") == 1
    nudges = [
        m for m in loop._messages
        if m.get("role") == "user"
        and "Tool failure direction check" in str(m.get("content"))
    ]
    assert len(nudges) == 1
    assert "build" in nudges[0]["content"]


def test_failure_direction_check_can_audit_recent_skill_recall(
    tmp_path: Path,
) -> None:
    loop = AgentLoopV3(
        session_id="skill_audit_nudge",
        output_dir=tmp_path,
        gemini_client=_RepeatsBuildThenStops(),  # type: ignore[arg-type]
        emit_event=lambda _event: None,
    )
    loop._tool_ctx.extra[SKILL_RECALL_GUIDANCE_STATE_KEY] = {
        "version": 1,
        "last_query": "portrait",
        "entries": {
            "portrait": {
                "revision": 0,
                "scope_query": "portrait",
                "last_result_names": [
                    "Portrait crop recipe",
                    "Generic timeline",
                ],
            },
        },
    }

    loop._append_repeated_failure_nudge("stabilize_video", "E_UNSUPPORTED", 1)

    content = str(loop._messages[-1]["content"])
    assert "Portrait crop recipe" in content
    assert "routing_audit" in content
    assert "failure_evidence" in content
    assert "avoid_skills" in content


def test_skill_recall_audit_survives_runtime_snapshot(tmp_path: Path) -> None:
    entry = {
        "revision": 2,
        "scope_query": "portrait",
        "failure_evidence": "crop did not stabilize motion",
        "guidance": "prefer optical stabilization",
        "avoid_skills": ["Portrait crop recipe"],
        "previous_skills": ["Portrait crop recipe"],
        "last_result_names": ["Optical stabilization"],
    }
    state = {
        "version": 1,
        "last_query": "portrait",
        "entries": {"portrait": entry},
    }
    first = AgentLoopV3(
        session_id="skill_audit_snapshot",
        output_dir=tmp_path / "first",
        gemini_client=_RepeatsBuildThenStops(),  # type: ignore[arg-type]
        emit_event=lambda _event: None,
    )
    first._tool_ctx.extra[SKILL_RECALL_GUIDANCE_STATE_KEY] = state

    restored = AgentLoopV3(
        session_id="skill_audit_snapshot",
        output_dir=tmp_path / "restored",
        runtime_state=first.snapshot_runtime_state(),
        gemini_client=_RepeatsBuildThenStops(),  # type: ignore[arg-type]
        emit_event=lambda _event: None,
    )

    assert restored._tool_ctx.extra[SKILL_RECALL_GUIDANCE_STATE_KEY] == state


class _AuditsSkillRecallAfterFailure:
    model = "fake"

    def __init__(self) -> None:
        self.calls = 0

    async def stream_turn(
        self, messages: list[dict[str, Any]], *, tools=None, temperature: float = 0.7
    ) -> AsyncIterator[dict[str, Any]]:
        del messages, tools, temperature
        self.calls += 1
        if self.calls == 1:
            name = "recall_skills"
            args = {"query": "portrait", "include_library": False}
        elif self.calls == 2:
            name = "probe_media"
            args = {"asset_id": "v_missing"}
        elif self.calls == 3:
            name = "recall_skills"
            args = {
                "query": "portrait",
                "include_library": False,
                "routing_audit": {
                    "failure_evidence": (
                        "the crop recipe did not address missing motion metadata"
                    ),
                    "guidance": "prefer optical stabilization and motion analysis",
                    "avoid_skills": ["Portrait crop recipe"],
                },
            }
        else:
            yield {"kind": "text_delta", "text": "rerouted"}
            yield {"kind": "finish", "reason": "stop"}
            return
        yield {
            "kind": "tool_call_start",
            "index": 0,
            "id": f"c{self.calls}",
            "name": name,
        }
        yield {
            "kind": "tool_call_args_delta",
            "index": 0,
            "delta": json.dumps(args),
        }
        yield {"kind": "finish", "reason": "tool_calls"}


def test_agent_audits_failed_skill_route_end_to_end(
    tmp_path: Path, monkeypatch
) -> None:
    from gemia.skill_store import DistilledSkillStore

    store_dir = tmp_path / "skills"
    monkeypatch.setenv("GEMIA_SKILL_STORE_DIR", str(store_dir))
    store = DistilledSkillStore()
    store.distill(
        "Portrait crop recipe",
        when_to_use="portrait social crop",
        steps=["crop the portrait"],
        tags=["portrait", "crop"],
    )
    store.distill(
        "Optical stabilization",
        when_to_use="stabilize handheld footage with optical flow",
        steps=["analyze motion", "stabilize"],
        tags=["stabilize", "optical"],
    )
    client = _AuditsSkillRecallAfterFailure()
    loop = AgentLoopV3(
        session_id="skill_audit_e2e",
        output_dir=tmp_path / "work",
        gemini_client=client,  # type: ignore[arg-type]
        emit_event=lambda _event: None,
    )

    asyncio.run(loop.run_turn("portrait"))

    tool_payloads = {
        str(message.get("tool_call_id")): json.loads(str(message["content"]))
        for message in loop._messages
        if message.get("role") == "tool"
    }
    assert [item["name"] for item in tool_payloads["c1"]["skills"]] == [
        "Portrait crop recipe"
    ]
    assert [item["name"] for item in tool_payloads["c3"]["skills"]] == [
        "Optical stabilization"
    ]
    assert tool_payloads["c3"]["routing"]["audit_revision"] == 1
    assert client.calls == 4
    assert any(
        "routing_audit" in str(message.get("content"))
        for message in loop._messages
        if message.get("role") == "user"
    )


class _Flaky:
    """Stateful dispatcher: raises on calls 1-4, succeeds on call 5, then raises
    on calls 6-9."""

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


def test_immediate_failure_guidance_does_not_stop_after_success(
    tmp_path: Path, monkeypatch
) -> None:
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

    # Each of the 8 failures receives an immediate direction check. The success
    # in the middle remains a success, and none of the guidance becomes a host
    # completion verdict or hard stop.
    assert client.calls == 10
    assert not any(e.get("reason") == "incomplete_goal" for e in events)
    assert [e for e in events if e.get("kind") == "turn_complete"]
    nudges = [
        message
        for message in loop._messages
        if message.get("role") == "user"
        and "Tool failure direction check" in str(message.get("content"))
    ]
    assert len(nudges) == 8


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
    """Always raises, alternating the structured error code each call."""

    _codes = ["E_BAD_ARG", "E_UNSUPPORTED"]

    def __init__(self) -> None:
        self.n = 0

    async def __call__(self, args: dict[str, Any], ctx: Any) -> dict[str, Any]:
        code = self._codes[self.n % len(self._codes)]
        self.n += 1
        raise ToolError(f"failure #{self.n}", code=code, recovery="fix_args")


def test_error_code_changes_do_not_suppress_direction_check(
    tmp_path: Path, monkeypatch
) -> None:
    """Every failure triggers a direction check even when error codes change."""
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

    # Nine tool turns plus closing text. Changing error codes does not suppress
    # the immediate first-failure direction check.
    assert client.calls == 10
    assert not any(e.get("reason") == "incomplete_goal" for e in events)
    assert [e for e in events if e.get("kind") == "turn_complete"]
    assert sum(1 for e in events if e.get("kind") == "tool_exec_error") == 9
    nudges = [
        m for m in loop._messages
        if m.get("role") == "user"
        and "Tool failure direction check" in str(m.get("content"))
    ]
    assert len(nudges) == 9


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
        '"error_code": "E_PROCESS_EXIT"' in str(message.get("content"))
        for message in loop._messages
        if message.get("role") == "tool"
    )
    assert [event for event in events if event.get("kind") == "turn_complete"]


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
        and "Tool failure direction check" in str(message.get("content"))
    ]
    assert len(nudges) == 5
