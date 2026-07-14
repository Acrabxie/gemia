"""Plan mode: per-session read-only planning gate for AgentLoopV3.

Covers the four load-bearing behaviors:
- classification completeness: every registered tool is explicitly allowed or
  blocked (a newly added tool must be classified before it ships), fail-closed
  for unknown names;
- the dispatch gate: while plan mode is ON a blocked tool never dispatches
  (plan_gate event + blocked_by_plan_mode tool_result), an allowed tool
  dispatches normally, and toggling OFF restores dispatch;
- the runaway stop: gated calls are invisible to BudgetGuard and the
  doom-loop guard, so PLAN_GATE_TURN_LIMIT must hard-stop a turn that keeps
  hammering blocked tools (turn_error reason=plan_gate_limit + turn_wrapup);
- surface plumbing: plan_mode_changed SSE on toggle, {{plan_mode}} system
  prompt injection, recency-digest reinforcement line, meta.json field, and
  the POST /sessions/{id}/plan_mode route + GET info field.
"""
from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, AsyncIterator

from gemia import v3_routes
from gemia.agent_loop_v3 import AgentLoopV3
from gemia.plan_mode import (
    PLAN_ALLOWED_TOOLS,
    PLAN_BLOCKED_TOOLS,
    PLAN_GATE_TURN_LIMIT,
    is_plan_safe,
)
from gemia.tools._schema import TOOL_NAMES


# ── classification completeness ──────────────────────────────────────────────


def test_every_registered_tool_is_classified() -> None:
    """allowed ∪ blocked must exactly cover TOOL_NAMES and stay disjoint, so a
    newly registered tool fails HERE until someone classifies it."""
    names = set(TOOL_NAMES)
    classified = PLAN_ALLOWED_TOOLS | PLAN_BLOCKED_TOOLS
    assert classified == names, (
        f"unclassified: {sorted(names - classified)}; "
        f"stale entries: {sorted(classified - names)}"
    )
    assert not (PLAN_ALLOWED_TOOLS & PLAN_BLOCKED_TOOLS)


def test_unknown_tool_fails_closed() -> None:
    assert not is_plan_safe("some_future_tool_nobody_classified")


def test_known_mutators_are_blocked() -> None:
    for name in ("generate_video", "export", "run_shell", "timeline_insert_clip",
                 "lumen_patch", "write_file", "remember"):
        assert not is_plan_safe(name), name
    for name in ("get_timeline", "probe_media", "analyze_media", "elicit"):
        assert is_plan_safe(name), name


# ── fake model clients (mirrors test_v3_completion_gate.py) ──────────────────


class _CallsOneToolThenStops:
    """First stream: one tool call (given name/args). Later streams: text stop."""

    model = "fake"

    def __init__(self, tool_name: str, args: str = "{}") -> None:
        self.tool_name = tool_name
        self.args = args
        self.calls = 0

    async def stream_turn(
        self, messages: list[dict[str, Any]], *, tools=None, temperature: float = 0.7
    ) -> AsyncIterator[dict[str, Any]]:
        del messages, tools, temperature
        self.calls += 1
        if self.calls == 1:
            yield {"kind": "tool_call_start", "index": 0, "id": "call_1",
                   "name": self.tool_name}
            yield {"kind": "tool_call_args_delta", "index": 0, "delta": self.args}
            yield {"kind": "finish", "reason": "tool_calls"}
            return
        yield {"kind": "text_delta", "text": "Here is the plan."}
        yield {"kind": "finish", "reason": "stop"}


class _AlwaysCallsBlockedTool:
    """Pathological model: every stream re-issues a blocked tool call."""

    model = "fake"

    def __init__(self) -> None:
        self.calls = 0

    async def stream_turn(
        self, messages: list[dict[str, Any]], *, tools=None, temperature: float = 0.7
    ) -> AsyncIterator[dict[str, Any]]:
        del messages, tools, temperature
        self.calls += 1
        yield {"kind": "tool_call_start", "index": 0, "id": f"call_{self.calls}",
               "name": "timeline_add_track"}
        yield {"kind": "tool_call_args_delta", "index": 0,
               "delta": json.dumps({"kind": "video", "n": self.calls})}
        yield {"kind": "finish", "reason": "tool_calls"}


class _BlockedBatchAtLimit:
    """Reach the gate limit, then include one later call in the same batch."""

    model = "fake"

    def __init__(self) -> None:
        self.calls = 0

    async def stream_turn(
        self, messages: list[dict[str, Any]], *, tools=None, temperature: float = 0.7
    ) -> AsyncIterator[dict[str, Any]]:
        del messages, tools, temperature
        self.calls += 1
        count = 2 if self.calls == PLAN_GATE_TURN_LIMIT else 1
        for index in range(count):
            yield {
                "kind": "tool_call_start",
                "index": index,
                "id": f"call_{self.calls}_{index}",
                "name": "timeline_add_track",
            }
            yield {
                "kind": "tool_call_args_delta",
                "index": index,
                "delta": json.dumps({"kind": "video", "index": index}),
            }
        yield {"kind": "finish", "reason": "tool_calls"}


def _make_loop(tmp_path: Path, client, sid: str) -> tuple[AgentLoopV3, list[dict[str, Any]]]:
    events: list[dict[str, Any]] = []
    loop = AgentLoopV3(
        session_id=sid,
        output_dir=tmp_path,
        gemini_client=client,  # type: ignore[arg-type]
        emit_event=events.append,
    )
    return loop, events


# ── the dispatch gate ────────────────────────────────────────────────────────


def test_blocked_tool_is_gated_while_plan_mode_on(tmp_path: Path) -> None:
    client = _CallsOneToolThenStops("timeline_add_track", '{"kind":"video"}')
    loop, events = _make_loop(tmp_path, client, "plan_gate_blocks")
    loop.set_plan_mode(True)

    asyncio.run(loop.run_turn("把片头做一个转场"))

    gates = [e for e in events if e.get("kind") == "plan_gate"]
    assert len(gates) == 1
    assert gates[0]["tool_name"] == "timeline_add_track"
    assert "blocked" in gates[0]["message"]
    # The gated call never dispatched.
    assert not [e for e in events if e.get("kind") == "tool_exec_start"]
    # The model saw a structured blocked_by_plan_mode tool_result.
    tool_msgs = [m for m in loop._messages if m.get("role") == "tool"]
    assert tool_msgs and "blocked_by_plan_mode" in tool_msgs[-1]["content"]
    # Turn still ends normally (the model presents the plan as text).
    assert [e for e in events if e.get("kind") == "turn_complete"]


def test_allowed_tool_dispatches_while_plan_mode_on(tmp_path: Path) -> None:
    client = _CallsOneToolThenStops(
        "search_library", '{"query":"sunset","kind":"any"}'
    )
    loop, events = _make_loop(tmp_path, client, "plan_gate_allows")
    loop.set_plan_mode(True)

    asyncio.run(loop.run_turn("找找可用素材"))

    assert not [e for e in events if e.get("kind") == "plan_gate"]
    starts = [e for e in events if e.get("kind") == "tool_exec_start"]
    assert starts and starts[0]["tool_name"] == "search_library"


def test_blocked_tool_dispatches_again_after_plan_mode_off(tmp_path: Path) -> None:
    client = _CallsOneToolThenStops("timeline_add_track", '{"kind":"video"}')
    loop, events = _make_loop(tmp_path, client, "plan_gate_off")
    loop.set_plan_mode(True)
    loop.set_plan_mode(False)

    asyncio.run(loop.run_turn("加一条视频轨"))

    assert not [e for e in events if e.get("kind") == "plan_gate"]
    # Dispatch was attempted (success or typed tool error — either proves the
    # gate stood down).
    assert [e for e in events if e.get("kind") == "tool_exec_start"]


def test_plan_gate_limit_hard_stops_a_hammering_turn(tmp_path: Path) -> None:
    client = _AlwaysCallsBlockedTool()
    loop, events = _make_loop(tmp_path, client, "plan_gate_limit")
    loop.set_plan_mode(True)

    asyncio.run(loop.run_turn("执行剪辑"))

    gates = [e for e in events if e.get("kind") == "plan_gate"]
    assert len(gates) == PLAN_GATE_TURN_LIMIT
    errors = [e for e in events if e.get("kind") == "turn_error"]
    assert errors and errors[-1]["reason"] == "plan_gate_limit"
    wrapups = [e for e in events if e.get("kind") == "turn_wrapup"]
    assert wrapups and wrapups[-1]["reason"] == "plan_gate_limit"
    # The model was never streamed more times than the limit allows.
    assert client.calls == PLAN_GATE_TURN_LIMIT
    assert not [e for e in events if e.get("kind") == "tool_exec_start"]


def test_plan_gate_limit_settles_later_calls_in_same_batch(tmp_path: Path) -> None:
    client = _BlockedBatchAtLimit()
    loop, events = _make_loop(tmp_path, client, "plan_gate_batch")
    loop.set_plan_mode(True)

    asyncio.run(loop.run_turn("执行剪辑"))

    assistant_ids = {
        call["id"]
        for message in loop._messages
        if message.get("role") == "assistant"
        for call in message.get("tool_calls") or []
    }
    result_ids = {
        message.get("tool_call_id")
        for message in loop._messages
        if message.get("role") == "tool"
    }
    assert assistant_ids == result_ids
    assert any(
        event.get("kind") == "tool_exec_error"
        and event.get("error_code") == "E_PLAN_GATE_CANCELLED"
        for event in events
    )


# ── prompt + state surfaces ──────────────────────────────────────────────────


def test_toggle_emits_changed_event_once_per_transition(tmp_path: Path) -> None:
    client = _CallsOneToolThenStops("search_library")
    loop, events = _make_loop(tmp_path, client, "plan_toggle_events")

    assert loop.set_plan_mode(True) is True
    assert loop.set_plan_mode(True) is True   # no-op repeat
    assert loop.set_plan_mode(False) is False

    changed = [e for e in events if e.get("kind") == "plan_mode_changed"]
    assert [e["enabled"] for e in changed] == [True, False]


def test_system_prompt_and_digest_reflect_plan_mode(tmp_path: Path) -> None:
    client = _CallsOneToolThenStops("search_library")
    loop, _ = _make_loop(tmp_path, client, "plan_prompt")

    off_prompt = loop.render_messages()[0]["content"]
    assert "PLAN MODE" not in off_prompt
    assert "{{plan_mode}}" not in off_prompt

    loop.set_plan_mode(True)
    on_prompt = loop.render_messages()[0]["content"]
    assert "PLAN MODE" in on_prompt
    assert "blocked_by_plan_mode" in on_prompt
    assert "Plan mode: ON" in loop._env_recency_digest()


def test_meta_json_records_plan_mode(tmp_path: Path) -> None:
    client = _CallsOneToolThenStops("search_library")
    events: list[dict[str, Any]] = []
    loop = AgentLoopV3(
        session_id="plan_meta",
        output_dir=tmp_path / "out",
        sessions_root=tmp_path / "sessions",
        gemini_client=client,  # type: ignore[arg-type]
        emit_event=events.append,
    )
    meta_path = tmp_path / "sessions" / "plan_meta" / "meta.json"
    assert json.loads(meta_path.read_text())["plan_mode"] is False
    loop.set_plan_mode(True)
    assert json.loads(meta_path.read_text())["plan_mode"] is True


# ── HTTP route (mirrors the test_timeline_direct_edit harness) ───────────────


class _PostHandler:
    def __init__(self, body: bytes) -> None:
        self.headers = {"Content-Length": str(len(body))}
        self.path = "/"
        self.rfile = io.BytesIO(body)
        self.wfile = io.BytesIO()
        self.status: int | None = None
        self.connection = None

    def send_response(self, status: int) -> None:
        self.status = status

    def send_header(self, key: str, value: str) -> None:
        pass

    def end_headers(self) -> None:
        pass

    @property
    def body_json(self) -> dict:
        return json.loads(self.wfile.getvalue().decode("utf-8"))


def test_plan_mode_route_toggles_and_validates(tmp_path: Path) -> None:
    client = _CallsOneToolThenStops("search_library")
    loop, events = _make_loop(tmp_path, client, "plan_route")
    runner = SimpleNamespace(
        agent=loop,
        session_id="plan_route",
        set_plan_mode=loop.set_plan_mode,
    )

    h = _PostHandler(json.dumps({"enabled": True}).encode())
    assert v3_routes._set_plan_mode(h, runner) is True
    assert h.status == 200
    assert h.body_json == {"session_id": "plan_route", "plan_mode": True}
    assert loop.plan_mode is True

    # Non-boolean payloads are rejected without touching the flag.
    h_bad = _PostHandler(json.dumps({"enabled": "yes"}).encode())
    v3_routes._set_plan_mode(h_bad, runner)
    assert h_bad.status == 400
    assert loop.plan_mode is True

    h_off = _PostHandler(json.dumps({"enabled": False}).encode())
    v3_routes._set_plan_mode(h_off, runner)
    assert h_off.status == 200
    assert loop.plan_mode is False
    assert [e["enabled"] for e in events if e.get("kind") == "plan_mode_changed"] == [
        True, False,
    ]


def test_prompt_tool_list_is_generated_from_allowed_set() -> None:
    """The prose tool list inside PLAN_MODE_PROMPT must be the sorted
    frozenset, so classifying a tool differently can never leave the prompt
    promising a stale list."""
    from gemia.plan_mode import PLAN_MODE_PROMPT

    expected = ", ".join(sorted(PLAN_ALLOWED_TOOLS))
    assert expected in PLAN_MODE_PROMPT
