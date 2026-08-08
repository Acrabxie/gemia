"""Execution commitment regressions for conversational completion."""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, AsyncIterator, Callable

import gemia.agent_loop_v3 as loop_mod
import pytest

from gemia.agent_loop_v3 import AgentLoopV3
from gemia.turn_control import TurnIntent, classify_turn_intent


class _ProseOnlyClient:
    model = "fake"

    def __init__(
        self,
        answer: str = "我会根据当前记忆诚实回答。",
        *,
        before_response: Callable[[], None] | None = None,
    ) -> None:
        self.answer = answer
        self.before_response = before_response
        self.calls = 0
        self.tool_counts: list[int] = []
        self.system_prompts: list[str] = []

    def _run_before_response(self) -> None:
        if self.before_response is not None:
            before_response, self.before_response = self.before_response, None
            before_response()

    async def stream_turn(
        self,
        messages: list[dict[str, Any]],
        *,
        tools=None,
        temperature: float = 0.7,
    ) -> AsyncIterator[dict[str, Any]]:
        del temperature
        self.calls += 1
        self.tool_counts.append(len(tools or []))
        self.system_prompts.append(str(messages[0]["content"]))
        self._run_before_response()
        yield {"kind": "text_delta", "text": self.answer}
        yield {"kind": "finish", "reason": "stop"}


class _ToolThenProseClient(_ProseOnlyClient):
    def __init__(self, tool_name: str = "fake_action") -> None:
        super().__init__()
        self.tool_name = tool_name

    async def stream_turn(
        self,
        messages: list[dict[str, Any]],
        *,
        tools=None,
        temperature: float = 0.7,
    ) -> AsyncIterator[dict[str, Any]]:
        del temperature
        self.calls += 1
        self.tool_counts.append(len(tools or []))
        self.system_prompts.append(str(messages[0]["content"]))
        if self.calls == 1:
            yield {
                "kind": "tool_call_start",
                "index": 0,
                "id": "call_1",
                "name": self.tool_name,
            }
            yield {"kind": "tool_call_args_delta", "index": 0, "delta": "{}"}
            yield {"kind": "finish", "reason": "tool_calls"}
            return
        self._run_before_response()
        yield {"kind": "text_delta", "text": "执行结果已经确认。"}
        yield {"kind": "finish", "reason": "stop"}


def _run_prose_turn(
    tmp_path: Path,
    request: str,
    *,
    pinned_intent: str | None = None,
    answer: str = "我会根据当前记忆诚实回答。",
) -> tuple[_ProseOnlyClient, list[dict[str, Any]], AgentLoopV3]:
    client = _ProseOnlyClient(answer)
    events: list[dict[str, Any]] = []
    loop = AgentLoopV3(
        session_id="execution_commitment",
        output_dir=tmp_path,
        gemini_client=client,  # type: ignore[arg-type]
        emit_event=events.append,
    )
    loop._pinned_intent = pinned_intent
    asyncio.run(loop.run_turn(request))
    return client, events, loop


@pytest.mark.parametrize(
    "turn_text",
    [
        "你对我的了解",
        "谈谈你眼里的我",
        "Across our conversations, what impression have I left?",
    ],
)
def test_uncommitted_prose_is_never_forced_into_completion_ledger(
    tmp_path: Path,
    turn_text: str,
) -> None:
    client, events, loop = _run_prose_turn(
        tmp_path,
        turn_text,
        pinned_intent="制作一支产品宣传片",
    )

    assert classify_turn_intent(turn_text) is TurnIntent.ACTIONABLE
    assert client.calls == 1
    assert client.tool_counts[0] > 0
    assert "(no tool activity in this turn)" in client.system_prompts[0]
    assert "step:act:open" not in client.system_prompts[0]
    assert [event["kind"] for event in events] == [
        "turn_start",
        "model_text_delta",
        "turn_complete",
    ]
    assert loop._turn_ledger is None


@pytest.mark.parametrize(
    ("turn_text", "plan_mode"),
    [
        ("只规划一份宣传片方案，不要执行", False),
        ("直接制作完整7秒动画", True),
    ],
)
def test_planning_prose_never_activates_production_ledger(
    tmp_path: Path,
    turn_text: str,
    plan_mode: bool,
) -> None:
    client = _ProseOnlyClient("这是完整方案；当前不执行。")
    events: list[dict[str, Any]] = []
    loop = AgentLoopV3(
        session_id="planning_prose",
        output_dir=tmp_path,
        gemini_client=client,  # type: ignore[arg-type]
        emit_event=events.append,
    )
    loop.plan_mode = plan_mode

    asyncio.run(loop.run_turn(turn_text))

    if not plan_mode:
        assert classify_turn_intent(turn_text) is TurnIntent.PLAN
    assert client.calls == 1
    assert not [event for event in events if event["kind"] == "completion_check"]
    assert not [event for event in events if event["kind"] == "turn_error"]
    assert [event for event in events if event["kind"] == "turn_complete"]
    assert loop._turn_ledger is None


def test_plan_research_tool_does_not_create_production_obligation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    search_started = asyncio.Event()
    upload_done = asyncio.Event()

    async def fake_search(args: dict[str, Any], ctx) -> dict[str, Any]:
        del args, ctx
        search_started.set()
        await upload_done.wait()
        return {"status": "success", "results": []}

    monkeypatch.setitem(loop_mod.DISPATCHER, "search_library", fake_search)
    client = _ToolThenProseClient("search_library")
    events: list[dict[str, Any]] = []
    loop = AgentLoopV3(
        session_id="planning_research",
        output_dir=tmp_path,
        gemini_client=client,  # type: ignore[arg-type]
        emit_event=events.append,
    )
    turn_text = "只规划一份宣传片方案，不要执行"
    external_path = tmp_path / "external-during-plan.png"
    external_path.write_bytes(b"external")

    async def run_with_concurrent_upload() -> None:
        async def upload() -> None:
            await search_started.wait()
            loop.registry.register_output(
                "img-plan-external",
                kind="image",
                path=external_path,
                summary="concurrent upload, not a search result",
            )
            upload_done.set()

        uploader = asyncio.create_task(upload())
        await loop.run_turn(turn_text)
        await uploader

    asyncio.run(run_with_concurrent_upload())

    assert classify_turn_intent(turn_text) is TurnIntent.PLAN
    assert client.calls == 2
    assert not [event for event in events if event["kind"] == "completion_check"]
    assert not [event for event in events if event["kind"] == "turn_error"]
    completed = [event for event in events if event["kind"] == "turn_complete"]
    assert len(completed) == 1
    assert completed[0]["final_asset_ids"] == []
    assert completed[0]["outcome"] == "progressed"
    assert loop.registry.contains("img-plan-external")
    assert loop._turn_ledger is not None
    assert loop._turn_ledger.sequence == 1


def test_pending_job_prose_does_not_preactivate_ledger(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _ProseOnlyClient("任务应该还在运行。")
    events: list[dict[str, Any]] = []
    loop = AgentLoopV3(
        session_id="pending_job_prose",
        output_dir=tmp_path,
        gemini_client=client,  # type: ignore[arg-type]
        emit_event=events.append,
    )
    monkeypatch.setattr(
        loop,
        "_routing_state",
        lambda: {
            "has_assets": False,
            "has_timeline": False,
            "has_lumenframe": False,
            "pending_jobs": {"job-1": "running"},
        },
    )

    asyncio.run(loop.run_turn("继续等待 job-1 的结果"))

    assert client.calls == 1
    assert not [event for event in events if event["kind"] == "completion_check"]
    assert not [event for event in events if event["kind"] == "turn_error"]
    assert [event for event in events if event["kind"] == "turn_complete"]
    assert loop._turn_ledger is None


def test_external_asset_race_cannot_commit_a_prose_turn(tmp_path: Path) -> None:
    client = _ProseOnlyClient()
    events: list[dict[str, Any]] = []
    loop = AgentLoopV3(
        session_id="external_asset_race",
        output_dir=tmp_path,
        gemini_client=client,  # type: ignore[arg-type]
        emit_event=events.append,
    )
    external_path = tmp_path / "external.png"
    external_path.write_bytes(b"external")
    client.before_response = lambda: loop.registry.register_output(
        "img-external",
        kind="image",
        path=external_path,
        summary="concurrently registered external asset",
    )

    asyncio.run(loop.run_turn("你对我的了解"))

    assert client.calls == 1
    assert not [event for event in events if event["kind"] == "completion_check"]
    assert not [event for event in events if event["kind"] == "turn_error"]
    completed = [event for event in events if event["kind"] == "turn_complete"]
    assert len(completed) == 1
    assert completed[0]["final_asset_ids"] == []
    assert completed[0]["outcome"] == "no_change"
    assert loop.registry.contains("img-external")
    assert loop._turn_ledger is None


def test_first_tool_call_records_activity_without_owning_completion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_action(args: dict[str, Any], ctx) -> dict[str, Any]:
        del args, ctx
        return {
            "error": "not available",
            "error_code": "E_TEST_UNAVAILABLE",
            "recovery": "none",
        }

    monkeypatch.setitem(loop_mod.DISPATCHER, "fake_action", fake_action)
    client = _ToolThenProseClient()
    events: list[dict[str, Any]] = []
    loop = AgentLoopV3(
        session_id="execution_commitment_tool",
        output_dir=tmp_path,
        gemini_client=client,  # type: ignore[arg-type]
        emit_event=events.append,
    )

    asyncio.run(loop.run_turn("做点活儿"))

    assert "(no tool activity in this turn)" in client.system_prompts[0]
    assert any(
        "Tool activity (observational)" in prompt
        for prompt in client.system_prompts[1:]
    )
    assert loop._turn_ledger is not None
    assert loop._turn_ledger.open_observations()
    assert not [event for event in events if event["kind"] == "completion_check"]
    assert [event for event in events if event["kind"] == "turn_complete"]
    assert not [event for event in events if event["kind"] == "turn_error"]
    assert not [event for event in events if event["kind"] == "turn_wrapup"]
