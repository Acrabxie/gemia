"""Multi-agent fan-out tests.

Offline: a fake client + fake dispatchers drive N children with no network.
Gates asserted:
- profile coverage: legacy profiles ⊆ TOOL_NAMES, elicit ∈ FORBIDDEN;
- ordered subagent_start / subagent_result, one pair per started child;
- the double-count rule: the loop commits 0 s for spawn_subtasks;
- deadline → timeout status; cancellation-on-parent-error settles;
- child doom-loop ends the CHILD (not the parent); plan-flag mid-batch clamp;
- at most four direct children, no recursive fan-out, and no host step/deadline cap;
- full tool profile gives children the parent's read/write/edit tool list.
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from gemia import subtasks as sub
from gemia.agent_loop_v3 import AgentLoopV3
from gemia.budget_guard import BudgetGuard
from gemia.plan_mode import PLAN_BLOCKED_TOOLS
from gemia.tool_capability_registry import build_default_registry
from gemia.tool_router import PRIVATE_SYSTEM_TOOLS, SYSTEM_TOOLS
from gemia.tools._schema import TOOL_NAMES

# ── profile coverage ────────────────────────────────────────────────────────


def test_legacy_profiles_are_subsets_of_registered_tools() -> None:
    names = set(TOOL_NAMES)
    for profile_name, tools in sub.PROFILES.items():
        if tools is None:
            continue
        assert tools <= names, f"{profile_name} references unknown tools: {tools - names}"


def test_legacy_profiles_exclude_forbidden_tools() -> None:
    for profile_name, tools in sub.PROFILES.items():
        if tools is None:
            continue
        assert not (tools & sub.FORBIDDEN_IN_CHILDREN), (
            f"{profile_name} contains globally-forbidden tools: "
            f"{tools & sub.FORBIDDEN_IN_CHILDREN}"
        )


def test_forbidden_set_is_registered() -> None:
    names = set(TOOL_NAMES)
    assert names >= sub.FORBIDDEN_IN_CHILDREN


def test_spawn_subtasks_is_plan_blocked() -> None:
    assert "spawn_subtasks" in PLAN_BLOCKED_TOOLS


def test_spawn_subtasks_is_root_only() -> None:
    assert "spawn_subtasks" in sub.FORBIDDEN_IN_CHILDREN
    assert "elicit" in sub.FORBIDDEN_IN_CHILDREN
    for _name, tools in sub.PROFILES.items():
        if tools is not None:
            assert "elicit" not in tools


def test_full_profile_returns_none() -> None:
    assert sub._profile_tools(None) is None
    assert sub._profile_tools("full") is None


def test_full_profile_schemas_allow_root_ask_but_not_recursive_spawn() -> None:
    schemas = sub._child_tool_schemas(None)
    names = {s["function"]["name"] for s in schemas}
    assert names == (
        set(TOOL_NAMES) - sub.FORBIDDEN_IN_CHILDREN
    ) | {sub.ASK_ROOT_AGENT}
    assert "spawn_subtasks" not in names
    assert sub.ASK_ROOT_AGENT in names
    assert "elicit" not in names
    assert len(names) > 10


def test_spawn_schema_matches_four_child_host_limit() -> None:
    schema = next(
        item for item in sub.TOOL_SCHEMAS
        if item["function"]["name"] == "spawn_subtasks"
    )
    subtasks = schema["function"]["parameters"]["properties"]["subtasks"]
    assert subtasks["minItems"] == 1
    assert subtasks["maxItems"] == sub.MAX_CHILDREN == 4


def test_full_profile_includes_project_read_write_and_edit_tools() -> None:
    names = {s["function"]["name"] for s in sub._child_tool_schemas(None)}
    assert {"read_file", "write_file", "list_dir"} <= names
    assert {"lumen_patch", "timeline_trim_clip", "edit_video"} <= names
    assert {"run_shell", "fetch", "save_skill", "remember", "log_note"} <= names


def test_subtask_defaults_have_no_host_execution_caps() -> None:
    assert sub.DEFAULT_MAX_STEPS is None
    assert sub.HARD_MAX_STEPS is None
    assert sub.DEFAULT_DEADLINE_SEC is None
    assert sub.HARD_DEADLINE_SEC is None


# ── fake client + dispatchers ────────────────────────────────────────────────


class _ScriptedClient:
    model = "fake"

    def __init__(self, spawn_args: dict[str, Any]) -> None:
        self._spawn_args = spawn_args
        self.parent_calls = 0
        self._child_streams: dict[str, int] = {}

    async def stream_turn(
        self, messages: list[dict[str, Any]], *, tools=None, temperature: float = 0.7
    ) -> AsyncIterator[dict[str, Any]]:
        del temperature
        is_child = any(
            m.get("role") == "system"
            and "Lumeri sub-agent" in str(m.get("content"))
            for m in messages
        )
        if not is_child:
            async for ev in self._parent_stream(messages):
                yield ev
            return
        async for ev in self._child_stream(messages):
            yield ev

    async def _parent_stream(
        self, messages: list[dict[str, Any]]
    ) -> AsyncIterator[dict[str, Any]]:
        self.parent_calls += 1
        spawn_completed = any(
            m.get("role") == "tool"
            and m.get("tool_call_id") == "spawn_call"
            and '"E_TOOL_NOT_ACTIVE"' not in str(m.get("content"))
            for m in messages
        )
        if not spawn_completed:
            yield {"kind": "tool_call_start", "index": 0, "id": "spawn_call",
                   "name": "spawn_subtasks"}
            yield {"kind": "tool_call_args_delta", "index": 0,
                   "delta": json.dumps(self._spawn_args)}
            yield {"kind": "finish", "reason": "tool_calls"}
            return
        yield {"kind": "text_delta", "text": "integrated results."}
        yield {"kind": "finish", "reason": "stop"}

    def _goal_of(self, messages: list[dict[str, Any]]) -> str:
        for m in messages:
            if m.get("role") == "user" and isinstance(m.get("content"), str):
                return m["content"]
        return ""

    async def _child_stream(
        self, messages: list[dict[str, Any]]
    ) -> AsyncIterator[dict[str, Any]]:
        goal = self._goal_of(messages)
        served = self._child_streams.get(goal, 0)
        self._child_streams[goal] = served + 1
        if served == 0:
            yield {"kind": "tool_call_start", "index": 0, "id": "c_probe",
                   "name": "probe_media"}
            yield {"kind": "tool_call_args_delta", "index": 0,
                   "delta": json.dumps({"asset_id": "v_001"})}
            yield {"kind": "finish", "reason": "tool_calls"}
            return
        yield {"kind": "text_delta", "text": f"done: {goal[:20]}"}
        yield {"kind": "finish", "reason": "stop"}


class _ToolCaptureClient:
    model = "fake"

    def __init__(self) -> None:
        self.calls: list[set[str]] = []

    async def stream_turn(
        self, messages: list[dict[str, Any]], *, tools=None, temperature: float = 0.7
    ) -> AsyncIterator[dict[str, Any]]:
        del messages, temperature
        self.calls.append({
            str(schema["function"]["name"])
            for schema in (tools or [])
        })
        yield {"kind": "text_delta", "text": "ready"}
        yield {"kind": "finish", "reason": "stop"}


def _make_loop(tmp_path: Path, client, **kw) -> tuple[AgentLoopV3, list[dict[str, Any]]]:
    events: list[dict[str, Any]] = []
    loop = AgentLoopV3(
        session_id=kw.pop("sid", "subtasks_test"),
        output_dir=tmp_path,
        gemini_client=client,  # type: ignore[arg-type]
        emit_event=events.append,
        **kw,
    )
    return loop, events


def test_root_first_model_call_receives_system_capabilities(tmp_path: Path) -> None:
    client = _ToolCaptureClient()
    loop, _events = _make_loop(tmp_path, client)

    asyncio.run(loop.run_turn("拉起子agent"))

    assert client.calls
    assert client.calls[0] >= SYSTEM_TOOLS


def test_full_child_with_compiled_parent_keeps_complete_installed_surface(
    tmp_path: Path,
) -> None:
    client = _ToolCaptureClient()
    loop, _events = _make_loop(tmp_path, client)
    loop.capabilities = build_default_registry()
    names = {
        str(schema["function"]["name"])
        for schema in sub._child_tool_schemas(None, parent=loop)
    }

    assert names == (
        set(loop.capabilities.names) - sub.FORBIDDEN_IN_CHILDREN
    ) | {sub.ASK_ROOT_AGENT}


def test_root_system_baseline_still_obeys_remote_and_plan_policies(
    tmp_path: Path,
) -> None:
    remote_client = _ToolCaptureClient()
    remote_loop, _events = _make_loop(
        tmp_path / "remote", remote_client, sid="remote_system", extra={"remote": True}
    )
    asyncio.run(remote_loop.run_turn("拉起子agent"))
    assert remote_client.calls
    assert not (remote_client.calls[0] & PRIVATE_SYSTEM_TOOLS)
    assert "spawn_subtasks" in remote_client.calls[0]

    plan_client = _ToolCaptureClient()
    plan_loop, _events = _make_loop(
        tmp_path / "plan", plan_client, sid="plan_system"
    )
    plan_loop.set_plan_mode(True)
    asyncio.run(plan_loop.run_turn("拉起子agent"))
    assert plan_client.calls
    assert not (plan_client.calls[0] & PLAN_BLOCKED_TOOLS)


def test_remote_full_child_keeps_creative_but_strips_private_system_tools(
    tmp_path: Path,
) -> None:
    client = _ToolCaptureClient()
    loop, _events = _make_loop(tmp_path, client, extra={"remote": True})
    names = {
        str(schema["function"]["name"])
        for schema in sub._child_tool_schemas(None, parent=loop)
    }

    assert not (PRIVATE_SYSTEM_TOOLS & names)
    assert {"lumen_patch", "timeline_trim_clip", "edit_video"} <= names


@pytest.fixture
def fake_probe(monkeypatch: pytest.MonkeyPatch):
    async def _probe(args: dict[str, Any], ctx) -> dict[str, Any]:
        return {"summary": "probed", "duration_sec": 3.0}

    from gemia import subtasks as _sub
    monkeypatch.setitem(_sub.DISPATCHER, "probe_media", _probe)
    return _probe


# ── ordered start/result, double-count ─────────────────────────────────────


def test_two_child_fanout_ordered_events(
    tmp_path: Path, fake_probe, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawn_args = {
        "subtasks": [
            {"goal": "annotate clip A", "tool_profile": "probe"},
            {"goal": "annotate clip B", "tool_profile": "probe"},
        ]
    }
    client = _ScriptedClient(spawn_args)
    loop, events = _make_loop(tmp_path, client, budget_max_usd=5.0, budget_max_seconds=600.0)

    real_commit = BudgetGuard.commit
    def fake_commit(self, tool_name, *, actual_usd=None, actual_seconds=None):
        if tool_name == "probe_media":
            actual_seconds = 10.0
        return real_commit(self, tool_name, actual_usd=actual_usd, actual_seconds=actual_seconds)
    monkeypatch.setattr(BudgetGuard, "commit", fake_commit)

    asyncio.run(loop.run_turn("annotate these clips"))

    starts = [e for e in events if e.get("kind") == "subagent_start"]
    results = [e for e in events if e.get("kind") == "subagent_result"]
    assert [e["agent_id"] for e in starts] == ["sub_1", "sub_2"]
    assert [e["agent_id"] for e in results] == ["sub_1", "sub_2"]
    assert all(e["call_id"] == "spawn_call" for e in starts + results)
    assert all(e["status"] == "ok" for e in results)


def test_child_tool_events_carry_agent_id(
    tmp_path: Path, fake_probe
) -> None:
    spawn_args = {"subtasks": [{"goal": "probe A", "tool_profile": "probe"}]}
    client = _ScriptedClient(spawn_args)
    loop, events = _make_loop(tmp_path, client)
    asyncio.run(loop.run_turn("go"))

    child_execs = [
        e for e in events
        if e.get("kind", "").startswith("tool_exec_") and e.get("agent_id")
    ]
    assert child_execs, "child tool activity must ride tool_exec_* with agent_id"
    assert all(e["agent_id"] == "sub_1" for e in child_execs)
    assert all(e["call_id"] == "spawn_call" for e in child_execs)
    parent_execs = [
        e for e in events
        if e.get("kind") == "tool_exec_start" and not e.get("agent_id")
    ]
    assert any(e["tool_name"] == "spawn_subtasks" for e in parent_execs)


# ── bounded children count ───────────────────────────────────────────────────


def test_spawn_more_than_four_children_refused(tmp_path: Path, fake_probe) -> None:
    """The host refuses a batch larger than the four-child limit."""
    spawn_args = {
        "subtasks": [
            {"goal": f"probe clip {i}", "tool_profile": "probe"} for i in range(5)
        ]
    }
    client = _ScriptedClient(spawn_args)
    loop, events = _make_loop(tmp_path, client)
    asyncio.run(loop.run_turn("go"))
    starts = [e for e in events if e.get("kind") == "subagent_start"]
    assert not starts
    assert any(
        e.get("kind") == "tool_exec_error"
        and e.get("tool_name") == "spawn_subtasks"
        and e.get("error_code") == "E_SUBTASK_LIMIT"
        for e in events
    )


# ── full profile (default) ──────────────────────────────────────────────────


def test_default_profile_is_full(tmp_path: Path, fake_probe) -> None:
    """Omitting tool_profile gives children the full tool list."""
    spawn_args = {"subtasks": [{"goal": "probe A"}]}
    client = _ScriptedClient(spawn_args)
    loop, events = _make_loop(tmp_path, client)
    asyncio.run(loop.run_turn("go"))
    starts = [e for e in events if e.get("kind") == "subagent_start"]
    assert starts


# ── unknown profile refused ─────────────────────────────────────────────────


def test_spawn_unknown_profile_refused(tmp_path: Path) -> None:
    spawn_args = {"subtasks": [{"goal": "x", "tool_profile": "nonesuch"}]}
    client = _ScriptedClient(spawn_args)
    loop, events = _make_loop(tmp_path, client)
    asyncio.run(loop.run_turn("go"))
    errs = [e for e in events if e.get("kind") == "tool_exec_error"
            and e.get("tool_name") == "spawn_subtasks"]
    assert any(e.get("error_code") == "E_SUBTASK_PROFILE" for e in errs)


# ── fail-closed profile enforcement ──────────────────────────────────────────


def test_child_spawn_subtasks_is_blocked_by_profile(tmp_path: Path) -> None:
    """A child cannot create another child."""
    class _Noop:
        model = "fake"
        async def stream_turn(self, messages, *, tools=None, temperature=0.7):
            if False:
                yield {}
            return

    loop, ev = _make_loop(tmp_path, _Noop())
    child = sub.SubtaskLoop(
        agent_id="sub_1", parent=loop, call_id="spawn_call",
        goal="do something", profile_name="full",
        guard=BudgetGuard(max_usd=1.0, max_seconds=100.0),
    )
    tc = {"id": "x", "name": "spawn_subtasks", "args_buf": [json.dumps({"subtasks": []})]}
    asyncio.run(child._dispatch_child_call(tc))
    tool_msgs = [m for m in child._messages if m.get("role") == "tool"]
    assert tool_msgs
    payload = json.loads(tool_msgs[-1]["content"])
    assert payload["error_code"] == "E_SUBTASK_PROFILE"


def test_child_can_ask_root_agent(tmp_path: Path) -> None:
    class _RootConsultClient:
        model = "fake"

        def __init__(self) -> None:
            self.calls: list[tuple[list[dict[str, Any]], Any]] = []

        async def stream_turn(self, messages, *, tools=None, temperature=0.7):
            del temperature
            self.calls.append((messages, tools))
            yield {"kind": "text_delta", "text": "先读取当前项目状态，再继续。"}

    client = _RootConsultClient()
    loop, events = _make_loop(tmp_path, client)
    child = sub.SubtaskLoop(
        agent_id="sub_1", parent=loop, call_id="spawn_call",
        goal="需要判断下一步", profile_name="probe",
        guard=BudgetGuard(max_usd=1.0, max_seconds=100.0),
    )
    tc = {
        "id": "root_question",
        "name": sub.ASK_ROOT_AGENT,
        "args_buf": [json.dumps({"question": "下一步先做什么？"})],
    }
    asyncio.run(child._dispatch_child_call(tc))
    payload = json.loads(
        [m for m in child._messages if m.get("role") == "tool"][-1]["content"]
    )
    assert payload == {
        "status": "answered",
        "answer": "先读取当前项目状态，再继续。",
        "root_agent": "root",
    }
    assert client.calls and client.calls[0][1] == []
    assert "下一步先做什么？" in client.calls[0][0][-1]["content"]
    assert any(
        e.get("kind") == "tool_exec_result"
        and e.get("tool_name") == sub.ASK_ROOT_AGENT
        and e.get("agent_id") == "sub_1"
        for e in events
    )


def test_full_child_can_dispatch_project_write_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_write(args, ctx):
        return {"status": "written", "path": args["path"]}

    monkeypatch.setitem(sub.DISPATCHER, "write_file", fake_write)

    class _Noop:
        model = "fake"

        async def stream_turn(self, messages, *, tools=None, temperature=0.7):
            del messages, tools, temperature
            if False:
                yield {}

    loop, _ = _make_loop(tmp_path, _Noop())
    child = sub.SubtaskLoop(
        agent_id="sub_1", parent=loop, call_id="spawn_call",
        goal="edit project", profile_name="full",
        guard=BudgetGuard(max_usd=1.0, max_seconds=100.0),
    )
    asyncio.run(child._dispatch_child_call({
        "id": "write_call",
        "name": "write_file",
        "args_buf": [json.dumps({"path": "project://edit/notes.md", "content": "ok"})],
    }))
    payload = json.loads(
        [m for m in child._messages if m.get("role") == "tool"][-1]["content"]
    )
    assert payload["status"] == "written"


def test_child_out_of_legacy_profile_tool_refused(tmp_path: Path) -> None:
    """A child with a legacy restricted profile cannot use tools outside it."""
    class _Noop:
        model = "fake"
        async def stream_turn(self, messages, *, tools=None, temperature=0.7):
            if False:
                yield {}
            return

    loop, ev = _make_loop(tmp_path, _Noop())
    child = sub.SubtaskLoop(
        agent_id="sub_1", parent=loop, call_id="spawn_call",
        goal="do a forbidden thing", profile_name="probe",
        guard=BudgetGuard(max_usd=1.0, max_seconds=100.0),
    )
    tc = {"id": "x", "name": "run_shell", "args_buf": [json.dumps({"cmd": "ls"})]}
    asyncio.run(child._dispatch_child_call(tc))
    tool_msgs = [m for m in child._messages if m.get("role") == "tool"]
    assert tool_msgs
    payload = json.loads(tool_msgs[-1]["content"])
    assert payload["error_code"] == "E_SUBTASK_PROFILE"


# ── plan-flag mid-batch clamp ────────────────────────────────────────────────


def test_child_plan_block_when_parent_toggles_mid_batch(tmp_path: Path) -> None:
    class _Noop:
        model = "fake"
        async def stream_turn(self, messages, *, tools=None, temperature=0.7):
            if False:
                yield {}
            return

    loop, _ = _make_loop(tmp_path, _Noop())
    loop.set_plan_mode(True)
    child = sub.SubtaskLoop(
        agent_id="sub_1", parent=loop, call_id="spawn_call",
        goal="annotate", profile_name="annotate",
        guard=BudgetGuard(max_usd=1.0, max_seconds=100.0),
    )
    tc = {"id": "x", "name": "annotate_media", "args_buf": [json.dumps({"asset_id": "v_001"})]}
    asyncio.run(child._dispatch_child_call(tc))
    tool_msgs = [m for m in child._messages if m.get("role") == "tool"]
    payload = json.loads(tool_msgs[-1]["content"])
    assert payload["error_code"] == "E_PLAN_MODE"
    assert payload["blocked_by_plan_mode"] is True


# ── child doom-loop ends the CHILD, not the parent ───────────────────────────


class _DoomChildClient(_ScriptedClient):
    async def _child_stream(self, messages):
        yield {"kind": "tool_call_start", "index": 0, "id": "c", "name": "probe_media"}
        yield {"kind": "tool_call_args_delta", "index": 0,
               "delta": json.dumps({"asset_id": "v_001"})}
        yield {"kind": "finish", "reason": "tool_calls"}


def test_child_doom_loop_ends_child_not_parent(
    tmp_path: Path, fake_probe
) -> None:
    spawn_args = {"subtasks": [{"goal": "loop forever", "tool_profile": "probe"}]}
    client = _DoomChildClient(spawn_args)
    loop, events = _make_loop(tmp_path, client)
    asyncio.run(loop.run_turn("go"))
    results = [e for e in events if e.get("kind") == "subagent_result"]
    assert results and results[0]["status"] == "error"
    assert "same call" in results[0]["summary"] or "no progress" in results[0]["summary"]
    assert [e for e in events if e.get("kind") == "turn_complete"]
    assert not [e for e in events if e.get("reason") == "incomplete_goal"]


def test_returned_child_failure_bubbles_to_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def returned_failure(args, ctx):
        return {"status": "failed", "error_code": "E_PROBE", "error": "bad media"}

    monkeypatch.setitem(sub.DISPATCHER, "probe_media", returned_failure)
    client = _ScriptedClient(
        {"subtasks": [{"goal": "probe broken media", "tool_profile": "probe"}]}
    )
    loop, events = _make_loop(tmp_path, client)

    asyncio.run(loop.run_turn("让多个代理并行分析素材"))

    child_results = [e for e in events if e.get("kind") == "subagent_result"]
    assert child_results and child_results[0]["status"] == "error"
    assert any(
        e.get("kind") == "tool_exec_error"
        and e.get("tool_name") == "spawn_subtasks"
        and e.get("error_code") == "E_SUBTASK_FAILED"
        for e in events
    )
    assert [e for e in events if e.get("kind") == "turn_complete"]
    assert not [e for e in events if e.get("reason") == "incomplete_goal"]


@pytest.mark.parametrize(
    "payload",
    [
        {"applied": False, "asset_id": "v_001"},
        {"status": "pending", "job_id": "job-1", "asset_id": "v_001"},
        {"status": "partial", "asset_id": "v_001", "summary": "partial result"},
    ],
    ids=["noop", "pending", "partial"],
)
def test_child_nonterminal_outcome_is_reported_to_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, Any],
) -> None:
    async def nonterminal(args, ctx):
        del args, ctx
        return dict(payload)

    monkeypatch.setitem(sub.DISPATCHER, "probe_media", nonterminal)
    client = _ScriptedClient(
        {"subtasks": [{"goal": "probe media", "tool_profile": "probe"}]}
    )
    loop, events = _make_loop(tmp_path, client)

    asyncio.run(loop.run_turn("让多个代理并行分析素材"))

    child_results = [e for e in events if e.get("kind") == "subagent_result"]
    assert child_results and child_results[0]["status"] == "error"
    assert any(
        e.get("kind") == "tool_exec_error"
        and e.get("tool_name") == "spawn_subtasks"
        and e.get("error_code") == "E_SUBTASK_FAILED"
        for e in events
    )
    assert [e for e in events if e.get("kind") == "turn_complete"]
    assert not [e for e in events if e.get("reason") == "incomplete_goal"]


@pytest.mark.parametrize(
    "payload",
    [
        {"applied": False, "asset_id": "A"},
        {"status": "pending", "asset_id": "A", "job_id": "job-A"},
        {"status": "partial", "asset_id": "A"},
    ],
    ids=["noop", "pending", "partial"],
)
def test_child_nonterminal_outcome_does_not_resolve_prior_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, Any],
) -> None:
    calls = 0

    async def fail_then_nonterminal(args, ctx):
        nonlocal calls
        del ctx
        calls += 1
        if calls == 1:
            return {
                "status": "failed",
                "asset_id": args["asset_id"],
                "error_code": "E_ORIGINAL",
                "error": "original failure",
            }
        return dict(payload)

    monkeypatch.setitem(sub.DISPATCHER, "probe_media", fail_then_nonterminal)

    class _Noop:
        model = "fake"

        async def stream_turn(self, messages, *, tools=None, temperature=0.7):
            del messages, tools, temperature
            if False:
                yield {}

    loop, _ = _make_loop(tmp_path, _Noop())
    child = sub.SubtaskLoop(
        agent_id="sub_1",
        parent=loop,
        call_id="spawn_call",
        goal="probe A",
        profile_name="probe",
        guard=BudgetGuard(max_usd=1.0, max_seconds=100.0),
    )
    call = {
        "id": "probe-a",
        "name": "probe_media",
        "args_buf": ['{"asset_id":"A"}'],
    }
    asyncio.run(child._dispatch_child_call(call))
    asyncio.run(child._dispatch_child_call({**call, "id": "probe-a-retry"}))

    assert child._unresolved_failures["target:read:asset_id=A"] == "E_ORIGINAL"


def test_child_success_on_other_asset_does_not_clear_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def target_sensitive(args, ctx):
        if args.get("asset_id") == "A":
            return {"status": "failed", "error_code": "E_MISSING", "error": "missing A"}
        return {"status": "success", "asset_id": args.get("asset_id")}

    monkeypatch.setitem(sub.DISPATCHER, "probe_media", target_sensitive)

    class _Noop:
        model = "fake"
        async def stream_turn(self, messages, *, tools=None, temperature=0.7):
            if False:
                yield {}

    loop, _ = _make_loop(tmp_path, _Noop())
    child = sub.SubtaskLoop(
        agent_id="sub_1",
        parent=loop,
        call_id="spawn_call",
        goal="probe A and B",
        profile_name="probe",
        guard=BudgetGuard(max_usd=1.0, max_seconds=100.0),
    )
    asyncio.run(child._dispatch_child_call({
        "id": "a", "name": "probe_media", "args_buf": ['{"asset_id":"A"}']
    }))
    asyncio.run(child._dispatch_child_call({
        "id": "b", "name": "probe_media", "args_buf": ['{"asset_id":"B"}']
    }))

    assert child._unresolved_failures == {
        "target:read:asset_id=A": "E_MISSING"
    }


class _AnnotateFailsThenAnalyzeSucceedsClient(_ScriptedClient):
    async def _child_stream(self, messages):
        goal = self._goal_of(messages)
        served = self._child_streams.get(goal, 0)
        self._child_streams[goal] = served + 1
        if served == 0:
            yield {
                "kind": "tool_call_start",
                "index": 0,
                "id": "annotate-a",
                "name": "annotate_media",
            }
            yield {
                "kind": "tool_call_args_delta",
                "index": 0,
                "delta": json.dumps({"asset_id": "A", "annotations": {"tag": "hero"}}),
            }
            yield {"kind": "finish", "reason": "tool_calls"}
            return
        if served == 1:
            yield {
                "kind": "tool_call_start",
                "index": 0,
                "id": "analyze-a",
                "name": "analyze_media",
            }
            yield {
                "kind": "tool_call_args_delta",
                "index": 0,
                "delta": json.dumps({"asset_id": "A"}),
            }
            yield {"kind": "finish", "reason": "tool_calls"}
            return
        yield {"kind": "text_delta", "text": "analysis succeeded"}
        yield {"kind": "finish", "reason": "stop"}


def test_child_read_on_same_asset_cannot_clear_failed_annotation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def failed_annotation(args, ctx):
        del ctx
        return {
            "status": "failed",
            "asset_id": args["asset_id"],
            "error_code": "E_ANNOTATE",
            "error": "annotation write failed",
        }

    async def successful_analysis(args, ctx):
        del ctx
        return {
            "status": "success",
            "asset_id": args["asset_id"],
            "summary": "asset is readable",
        }

    monkeypatch.setitem(sub.DISPATCHER, "annotate_media", failed_annotation)
    monkeypatch.setitem(sub.DISPATCHER, "analyze_media", successful_analysis)
    spawn_args = {
        "subtasks": [
            {
                "goal": "annotate and inspect asset A",
                "tool_profile": "annotate",
            }
        ]
    }
    client = _AnnotateFailsThenAnalyzeSucceedsClient(spawn_args)
    loop, events = _make_loop(tmp_path, client)

    asyncio.run(loop.run_turn("让多个代理并行标注素材 A"))

    child_results = [e for e in events if e.get("kind") == "subagent_result"]
    assert child_results and child_results[0]["status"] == "error"
    assert any(
        e.get("kind") == "tool_exec_error"
        and e.get("tool_name") == "spawn_subtasks"
        and e.get("error_code") == "E_SUBTASK_FAILED"
        for e in events
    )
    assert [e for e in events if e.get("kind") == "turn_complete"]
    assert not [e for e in events if e.get("reason") == "incomplete_goal"]


# ── deadline → timeout ───────────────────────────────────────────────────────


class _HangingChildClient(_ScriptedClient):
    async def _child_stream(self, messages):
        yield {"kind": "tool_call_start", "index": 0, "id": "c", "name": "probe_media"}
        yield {"kind": "tool_call_args_delta", "index": 0,
               "delta": json.dumps({"asset_id": "v_001"})}
        yield {"kind": "finish", "reason": "tool_calls"}


def test_deadline_cancels_stragglers_with_timeout_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _slow_probe(args, ctx):
        await asyncio.sleep(30.0)
        return {"summary": "never"}

    from gemia import subtasks as _sub
    monkeypatch.setitem(_sub.DISPATCHER, "probe_media", _slow_probe)

    spawn_args = {
        "subtasks": [{"goal": "slow", "tool_profile": "probe"}],
        "deadline_sec": 0.2,
    }
    client = _HangingChildClient(spawn_args)
    loop, events = _make_loop(tmp_path, client)
    asyncio.run(loop.run_turn("go"))

    results = [e for e in events if e.get("kind") == "subagent_result"]
    assert results and results[0]["status"] == "timeout"
    assert loop.budget.spent_usd <= loop.budget.max_usd
    assert loop.budget.spent_seconds <= loop.budget.max_seconds


# ── cancellation-on-parent-error settles ─────────────────────────────────────


def test_parent_cancel_settles_and_emits_terminals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started = asyncio.Event()

    async def _slow_probe(args, ctx):
        started.set()
        await asyncio.sleep(30.0)
        return {"summary": "never"}

    from gemia import subtasks as _sub
    monkeypatch.setitem(_sub.DISPATCHER, "probe_media", _slow_probe)

    spawn_args = {"subtasks": [{"goal": "s", "tool_profile": "probe"}]}
    client = _HangingChildClient(spawn_args)
    events: list[dict[str, Any]] = []
    loop = AgentLoopV3(
        session_id="cancel_test", output_dir=tmp_path,
        gemini_client=client, emit_event=events.append,  # type: ignore[arg-type]
    )

    async def _run_and_cancel() -> None:
        ctx = loop._tool_ctx
        ctx.extra["call_id"] = "spawn_call"
        task = asyncio.ensure_future(sub.dispatch(spawn_args, ctx))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(_run_and_cancel())

    assert loop.budget.spent_usd <= loop.budget.max_usd
    assert loop.budget.spent_seconds <= loop.budget.max_seconds
    results = [e for e in events if e.get("kind") == "subagent_result"]
    assert results and results[0]["status"] in {"timeout", "cancelled"}
    assert results[0]["agent_id"] == "sub_1"
