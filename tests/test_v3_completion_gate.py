"""RC4 one-shot completion-check gate for AgentLoopV3.

When the model stops emitting tool_calls and the gate is enabled
(COMPLETION_CHECK_ENABLED=True), the loop injects ONE user message
prompting goal completion verification, then re-runs the model once more.
On the second no-tool-call, the loop respects honest stop and emits turn_complete.
The gate prevents infinite loops while allowing the model a final chance to
course-correct.
"""
from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any, AsyncIterator

import gemia.agent_loop_v3 as loop_mod
import pytest
from gemia.agent_loop_v3 import AgentLoopV3, COMPLETION_CHECK_ENABLED
from gemia.agent_loop_v3 import _relevant_existing_jobs


class _ModelStopsImmediately:
    """Model that emits no tool_calls — just text, then stop."""

    model = "fake"

    def __init__(self) -> None:
        self.calls = 0

    async def stream_turn(
        self, messages: list[dict[str, Any]], *, tools=None, temperature: float = 0.7
    ) -> AsyncIterator[dict[str, Any]]:
        del messages, tools, temperature
        self.calls += 1
        yield {"kind": "text_delta", "text": "Done processing."}
        yield {"kind": "finish", "reason": "stop"}


def test_only_explicitly_continued_session_jobs_bind_to_current_turn() -> None:
    pending = {"job-old": "running", "job-other": "queued"}
    assert _relevant_existing_jobs("查看当前时间线", pending) == {}
    assert _relevant_existing_jobs("检查 job-old 的状态", pending) == {
        "job-old": "running"
    }
    assert _relevant_existing_jobs("继续等待结果", pending) == pending


class _AlwaysAsksInProse(_ModelStopsImmediately):
    async def stream_turn(
        self, messages: list[dict[str, Any]], *, tools=None, temperature: float = 0.7
    ) -> AsyncIterator[dict[str, Any]]:
        del messages, tools, temperature
        self.calls += 1
        yield {"kind": "text_delta", "text": "请告诉我你喜欢什么动画风格？"}
        yield {"kind": "finish", "reason": "stop"}


class _WorksThenActsAfterGate:
    """Does real work first (a tool call), stops → the gate fires because work
    was done → then course-corrects with a SECOND tool call after seeing the
    gate → stops. Exercises both 'gate fires after work' and 'post-gate tool
    dispatch still works'."""

    model = "fake"

    def __init__(self) -> None:
        self.calls = 0

    async def stream_turn(
        self, messages: list[dict[str, Any]], *, tools=None, temperature: float = 0.7
    ) -> AsyncIterator[dict[str, Any]]:
        del tools, temperature
        self.calls += 1
        if self.calls == 1:
            # Real work — a tool call marks the turn as having done something.
            yield {"kind": "tool_call_start", "index": 0, "id": "c1", "name": "fake_ok"}
            yield {"kind": "tool_call_args_delta", "index": 0, "delta": "{}"}
            yield {"kind": "finish", "reason": "tool_calls"}
            return
        if self.calls == 2:
            # Stop after work → the gate should fire (work was done).
            yield {"kind": "text_delta", "text": "第一步做完了。"}
            yield {"kind": "finish", "reason": "stop"}
            return
        if self.calls == 3:
            # Post-gate: only act if the goal-check nudge was actually injected.
            has_nudge = any(
                msg.get("role") == "user"
                and isinstance(msg.get("content"), str)
                and "目标核对" in msg["content"]
                for msg in messages
            )
            if has_nudge:
                yield {"kind": "tool_call_start", "index": 0, "id": "c2", "name": "fake_ok"}
                yield {"kind": "tool_call_args_delta", "index": 0, "delta": "{}"}
                yield {"kind": "finish", "reason": "tool_calls"}
                return
            yield {"kind": "text_delta", "text": "No nudge found; error."}
            yield {"kind": "finish", "reason": "stop"}
            return
        # After the post-gate tool result: stop for good (gate is one-shot).
        yield {"kind": "text_delta", "text": "都做完了。"}
        yield {"kind": "finish", "reason": "stop"}


class _RetreatsThenActsAfterGate:
    """First asks the user to supply creative details in prose, then obeys the
    host completion gate and uses a tool.  This is the concrete regression for
    actionable first-response retreat: the gate is based on the current request,
    even when no tool has run yet."""

    model = "fake"

    def __init__(self) -> None:
        self.calls = 0

    async def stream_turn(
        self, messages: list[dict[str, Any]], *, tools=None, temperature: float = 0.7
    ) -> AsyncIterator[dict[str, Any]]:
        del tools, temperature
        self.calls += 1
        if self.calls == 1:
            yield {"kind": "text_delta", "text": "请先告诉我动画风格和更多参数。"}
            yield {"kind": "finish", "reason": "stop"}
            return
        if self.calls == 2 and any(
            message.get("role") == "user"
            and "直接制作完整7秒动画" in str(message.get("content"))
            and "目标核对" in str(message.get("content"))
            for message in messages
        ):
            yield {
                "kind": "tool_call_start",
                "index": 0,
                "id": "retreat_generate",
                "name": "fake_generate_video",
            }
            yield {"kind": "tool_call_args_delta", "index": 0, "delta": "{}"}
            yield {"kind": "finish", "reason": "tool_calls"}
            return
        if self.calls == 3:
            yield {
                "kind": "tool_call_start",
                "index": 0,
                "id": "retreat_review",
                "name": "analyze_media",
            }
            yield {
                "kind": "tool_call_args_delta",
                "index": 0,
                "delta": '{"asset_id":"v_001"}',
            }
            yield {"kind": "finish", "reason": "tool_calls"}
            return
        if self.calls == 4:
            yield {"kind": "text_delta", "text": "7 秒动画已完成并复验。"}
            yield {"kind": "finish", "reason": "stop"}
            return
        yield {"kind": "text_delta", "text": "无法再推进。"}
        yield {"kind": "finish", "reason": "stop"}


class _DeniedCreativeElicitThenWrites:
    """A policy-refused creative ask must not poison later valid work."""

    model = "fake"

    def __init__(self) -> None:
        self.calls = 0

    async def stream_turn(
        self, messages: list[dict[str, Any]], *, tools=None, temperature: float = 0.7
    ) -> AsyncIterator[dict[str, Any]]:
        del messages, tools, temperature
        self.calls += 1
        if self.calls == 1:
            args = {
                "reason": "creative_preference",
                "title": "Choose a file format",
                "controls": {"format": {"type": "text"}},
            }
            yield {
                "kind": "tool_call_start",
                "index": 0,
                "id": "ask-style",
                "name": "elicit",
            }
            yield {
                "kind": "tool_call_args_delta",
                "index": 0,
                "delta": json.dumps(args),
            }
            yield {"kind": "finish", "reason": "tool_calls"}
            return
        if self.calls == 2:
            yield {
                "kind": "tool_call_start",
                "index": 0,
                "id": "write-result",
                "name": "file_write",
            }
            yield {
                "kind": "tool_call_args_delta",
                "index": 0,
                "delta": json.dumps(
                    {"path": "result.txt", "content": "safe default result"}
                ),
            }
            yield {"kind": "finish", "reason": "tool_calls"}
            return
        yield {"kind": "text_delta", "text": "已按安全默认值完成。"}
        yield {"kind": "finish", "reason": "stop"}


class _BudgetBlockedThenStops:
    """Request one over-cap host tool, then stop without asking approval."""

    model = "fake"

    def __init__(self) -> None:
        self.calls = 0

    async def stream_turn(
        self, messages: list[dict[str, Any]], *, tools=None, temperature: float = 0.7
    ) -> AsyncIterator[dict[str, Any]]:
        del messages, tools, temperature
        self.calls += 1
        if self.calls == 1:
            yield {
                "kind": "tool_call_start",
                "index": 0,
                "id": "budgeted_read",
                "name": "get_timeline",
            }
            yield {"kind": "tool_call_args_delta", "index": 0, "delta": "{}"}
            yield {"kind": "finish", "reason": "tool_calls"}
            return
        yield {"kind": "text_delta", "text": "预算上限无法由批准解除。"}
        yield {"kind": "finish", "reason": "stop"}


class _ReferenceThenFinalVideo:
    model = "fake"

    def __init__(self) -> None:
        self.calls = 0

    async def stream_turn(
        self, messages: list[dict[str, Any]], *, tools=None, temperature: float = 0.7
    ) -> AsyncIterator[dict[str, Any]]:
        del messages, tools, temperature
        self.calls += 1
        scripted = {
            1: ("make_reference", "reference"),
            2: ("make_final_video", "final"),
            3: ("analyze_media", "review"),
        }
        if self.calls in scripted:
            name, call_id = scripted[self.calls]
            args = {"asset_id": "v-final"} if name == "analyze_media" else {}
            yield {
                "kind": "tool_call_start",
                "index": 0,
                "id": call_id,
                "name": name,
            }
            yield {
                "kind": "tool_call_args_delta",
                "index": 0,
                "delta": json.dumps(args),
            }
            yield {"kind": "finish", "reason": "tool_calls"}
            return
        yield {"kind": "text_delta", "text": "最终视频已完成。"}
        yield {"kind": "finish", "reason": "stop"}


def test_no_gate_for_pure_conversation(tmp_path: Path) -> None:
    """A pure conversational turn — the model answers in plain text and does no
    work (no tool calls, no assets, no failures) — must NOT trigger the
    pre-delivery gate. The gate reviews work; with nothing done, firing it only
    forces a redundant second reply (the "已完成…" report / rule meta-commentary
    we want gone). So a hello/identity/thanks turn is a single natural reply."""
    if not COMPLETION_CHECK_ENABLED:
        return

    client = _ModelStopsImmediately()
    events: list[dict[str, Any]] = []
    loop = AgentLoopV3(
        session_id="pure_conversation_no_gate",
        output_dir=tmp_path,
        gemini_client=client,  # type: ignore[arg-type]
        emit_event=events.append,
    )

    asyncio.run(loop.run_turn("你是谁"))

    # Exactly ONE model call — no gate round, no forced second reply.
    assert client.calls == 1, f"Expected 1 call, got {client.calls}"

    # No completion_check event: the gate never fired.
    completion_checks = [e for e in events if e.get("kind") == "completion_check"]
    assert (
        len(completion_checks) == 0
    ), f"Expected 0 completion_check, got {len(completion_checks)}"

    # Turn completes cleanly on the first stop.
    turn_completes = [e for e in events if e.get("kind") == "turn_complete"]
    assert len(turn_completes) == 1, f"Expected 1 turn_complete, got {len(turn_completes)}"


def test_parent_budget_gate_is_a_non_overridable_blocker_not_an_ask(
    tmp_path: Path,
) -> None:
    client = _BudgetBlockedThenStops()
    events: list[dict[str, Any]] = []
    loop = AgentLoopV3(
        session_id="budget_is_not_approval",
        output_dir=tmp_path,
        gemini_client=client,  # type: ignore[arg-type]
        emit_event=events.append,
    )
    loop.budget.max_usd = 0.0
    loop.budget.max_seconds = 0.0

    asyncio.run(loop.run_turn("读取当前时间线；若预算不够就诚实停止，不要询问批准"))

    tool_payloads = [
        json.loads(message["content"])
        for message in loop._messages
        if message.get("role") == "tool"
    ]
    budget_payload = next(
        payload for payload in tool_payloads if payload.get("error_code") == "E_BUDGET"
    )
    assert budget_payload["blocked_by_budget"] is True
    assert budget_payload["approval_cannot_override"] is True
    assert "needs_approval" not in budget_payload
    assert not [event for event in events if event.get("kind") == "ask_question"]


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg required")
def test_turn_complete_projects_ledger_finals_not_intermediate_assets(
    tmp_path: Path, monkeypatch
) -> None:
    async def make_reference(args: dict[str, Any], ctx) -> dict[str, Any]:
        del args
        path = tmp_path / "reference.png"
        path.write_bytes(b"not-needed-for-review")
        ctx.registry.register_output(
            "img-ref", kind="image", path=path, summary="reference image"
        )
        return {"status": "success", "asset_id": "img-ref", "kind": "image"}

    async def make_final_video(args: dict[str, Any], ctx) -> dict[str, Any]:
        del args
        path = tmp_path / "final.mp4"
        subprocess.run(
            [
                "ffmpeg", "-y", "-f", "lavfi", "-i",
                "color=c=black:s=16x16:r=1:d=1",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path),
            ],
            check=True,
            capture_output=True,
        )
        ctx.registry.register_output(
            "v-final", kind="video", path=path, summary="final video"
        )
        return {"status": "success", "asset_id": "v-final", "kind": "video"}

    async def review(args: dict[str, Any], ctx) -> dict[str, Any]:
        del ctx
        return {"status": "success", "asset_id": args["asset_id"]}

    monkeypatch.setitem(loop_mod.DISPATCHER, "make_reference", make_reference)
    monkeypatch.setitem(loop_mod.DISPATCHER, "make_final_video", make_final_video)
    monkeypatch.setitem(loop_mod.DISPATCHER, "analyze_media", review)
    events: list[dict[str, Any]] = []
    loop = AgentLoopV3(
        session_id="final_projection",
        output_dir=tmp_path,
        gemini_client=_ReferenceThenFinalVideo(),  # type: ignore[arg-type]
        emit_event=events.append,
    )

    asyncio.run(loop.run_turn("生成一个视频"))

    complete = [event for event in events if event.get("kind") == "turn_complete"]
    assert len(complete) == 1
    assert complete[0]["final_asset_ids"] == ["v-final"]


def test_completion_gate_disabled_still_cannot_bypass_host_ledger(tmp_path: Path) -> None:
    """Disabling the visual/text gate does not disable objective completion."""
    # Temporarily disable the gate.
    original_enabled = loop_mod.COMPLETION_CHECK_ENABLED
    loop_mod.COMPLETION_CHECK_ENABLED = False

    try:
        client = _ModelStopsImmediately()
        events: list[dict[str, Any]] = []
        loop = AgentLoopV3(
            session_id="completion_gate_disabled",
            output_dir=tmp_path,
            gemini_client=client,  # type: ignore[arg-type]
            emit_event=events.append,
        )

        asyncio.run(loop.run_turn("Do something"))

        # Adjacent route, then full route, then an honest incomplete stop.
        assert client.calls == 3, f"Expected 3 calls, got {client.calls}"

        # No completion_check event should be emitted.
        completion_checks = [e for e in events if e.get("kind") == "completion_check"]
        assert (
            len(completion_checks) == 0
        ), f"Expected 0 completion_check events, got {len(completion_checks)}"

        assert not [e for e in events if e.get("kind") == "turn_complete"]
        assert any(
            e.get("kind") == "turn_error" and e.get("reason") == "incomplete_goal"
            for e in events
        )
    finally:
        loop_mod.COMPLETION_CHECK_ENABLED = original_enabled


def test_agent_loop_passes_all_routed_deliverables_to_host_ledger(
    tmp_path: Path,
) -> None:
    loop = AgentLoopV3(
        session_id="multi_deliverable_ledger",
        output_dir=tmp_path,
        gemini_client=_ModelStopsImmediately(),  # type: ignore[arg-type]
        emit_event=lambda _event: None,
    )

    asyncio.run(loop.run_turn("生成一张图片和一段音频"))

    assert loop._turn_ledger is not None
    assert loop._turn_ledger.workflows == ("image", "audio")
    blockers = loop._turn_ledger.completion_decision().blockers
    assert "final_asset_kind:image:missing" in blockers
    assert "final_asset_kind:audio:missing" in blockers


def test_gate_fires_after_work_and_allows_post_gate_tool(
    tmp_path: Path, monkeypatch
) -> None:
    """When the turn actually did work (a tool ran), the gate DOES fire once —
    and the model may course-correct with another tool call after it, which is
    dispatched normally. Proves the gate still guards real work turns and that
    post-gate tool dispatch survives the new conversational carve-out."""
    if not COMPLETION_CHECK_ENABLED:
        return

    async def fake_ok(args: dict[str, Any], ctx) -> dict[str, Any]:
        del args, ctx
        return {"status": "ok"}

    monkeypatch.setitem(loop_mod.DISPATCHER, "fake_ok", fake_ok)

    client = _WorksThenActsAfterGate()
    events: list[dict[str, Any]] = []
    loop = AgentLoopV3(
        session_id="gate_after_work",
        output_dir=tmp_path,
        gemini_client=client,  # type: ignore[arg-type]
        emit_event=events.append,
    )

    asyncio.run(loop.run_turn("做点活儿"))

    # call1 tool → call2 stop → gate → call3 tool → call4 stop.
    assert client.calls == 4, f"Expected 4 calls, got {client.calls}"

    # Exactly one completion_check event (one-shot, even with post-gate work).
    completion_checks = [e for e in events if e.get("kind") == "completion_check"]
    assert len(completion_checks) == 1, f"Expected 1 completion_check, got {len(completion_checks)}"

    # Both the pre-gate and post-gate tool calls dispatched.
    readys = [e for e in events if e.get("kind") == "model_tool_call_ready"]
    assert len(readys) == 2, f"Expected 2 dispatched tool calls, got {len(readys)}"
    assert all(e.get("tool_name") == "fake_ok" for e in readys)


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg required")
def test_actionable_first_text_retreat_triggers_gate_and_tool_recovery(
    tmp_path: Path, monkeypatch
) -> None:
    if not COMPLETION_CHECK_ENABLED:
        return

    async def fake_generate(args: dict[str, Any], ctx) -> dict[str, Any]:
        del args
        output = tmp_path / "recovered.mp4"
        subprocess.run(
            [
                "ffmpeg", "-y", "-f", "lavfi", "-i",
                "color=c=black:s=16x16:r=1:d=7",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", str(output),
            ],
            check=True,
            capture_output=True,
        )
        ctx.registry.register_output(
            "v_001", kind="video", path=output, summary="recovered animation"
        )
        return {
            "status": "ok",
            "asset_id": "v_001",
            "kind": "video",
            # Deliberately omit duration: the host must obtain it via ffprobe.
        }

    async def fake_analyze(args: dict[str, Any], ctx) -> dict[str, Any]:
        del ctx
        return {"status": "ok", "asset_id": args["asset_id"], "summary": "reviewed"}

    monkeypatch.setitem(loop_mod.DISPATCHER, "fake_generate_video", fake_generate)
    monkeypatch.setitem(loop_mod.DISPATCHER, "analyze_media", fake_analyze)
    client = _RetreatsThenActsAfterGate()
    events: list[dict[str, Any]] = []
    loop = AgentLoopV3(
        session_id="actionable_retreat_gate",
        output_dir=tmp_path,
        gemini_client=client,  # type: ignore[arg-type]
        emit_event=events.append,
    )

    asyncio.run(loop.run_turn("直接制作完整7秒动画"))

    kinds = [(event.get("kind"), event.get("call_id")) for event in events]
    assert sum(kind == "completion_check" for kind, _ in kinds) == 1
    assert kinds.index(("completion_check", None)) < kinds.index(
        ("tool_exec_start", "retreat_generate")
    )
    assert kinds.index(("tool_exec_start", "retreat_generate")) < kinds.index(
        ("tool_exec_result", "retreat_generate")
    )
    assert kinds.index(("tool_exec_result", "retreat_generate")) < kinds.index(
        ("tool_exec_start", "retreat_review")
    )
    assert any(event.get("kind") == "turn_complete" for event in events)
    assert not any(
        event.get("kind") == "turn_error"
        and event.get("reason") == "incomplete_goal"
        for event in events
    )
    assert client.calls == 4


def test_actionable_repeated_prose_questions_are_not_exposed(tmp_path: Path) -> None:
    client = _AlwaysAsksInProse()
    events: list[dict[str, Any]] = []
    loop = AgentLoopV3(
        session_id="prose_question_guard",
        output_dir=tmp_path,
        gemini_client=client,  # type: ignore[arg-type]
        emit_event=events.append,
    )

    asyncio.run(loop.run_turn("直接制作完整7秒动画，创作偏好使用默认值"))

    assert not [event for event in events if event.get("kind") == "model_text_delta"]
    assert any(
        event.get("kind") == "turn_error"
        and event.get("reason") == "incomplete_goal"
        for event in events
    )
    assert loop._tool_ctx.extra["clarification_guard"].asks_used == 0


def test_policy_refused_creative_elicit_does_not_block_later_valid_work(
    tmp_path: Path, monkeypatch
) -> None:
    async def fake_file_write(args: dict[str, Any], ctx) -> dict[str, Any]:
        target = ctx.output_dir / str(args["path"])
        target.write_text(str(args["content"]), encoding="utf-8")
        return {"status": "success", "path": str(target)}

    monkeypatch.setitem(loop_mod.DISPATCHER, "file_write", fake_file_write)
    client = _DeniedCreativeElicitThenWrites()
    events: list[dict[str, Any]] = []
    loop = AgentLoopV3(
        session_id="creative_elicit_nonblocking",
        output_dir=tmp_path,
        gemini_client=client,  # type: ignore[arg-type]
        emit_event=events.append,
    )

    asyncio.run(loop.run_turn("写入结果文件，创作格式你决定"))

    assert (tmp_path / "result.txt").read_text(encoding="utf-8") == "safe default result"
    assert not [event for event in events if event.get("kind") == "ask_question"]
    assert any(
        event.get("kind") == "tool_exec_error"
        and event.get("call_id") == "ask-style"
        and event.get("error_code") == "E_CLARIFICATION_POLICY"
        for event in events
    )
    failure = loop._turn_ledger.unresolved_failures["ask-style"]
    assert failure.blocking is False
    assert any(event.get("kind") == "turn_complete" for event in events)
    assert not any(
        event.get("kind") == "turn_error"
        and event.get("reason") == "incomplete_goal"
        for event in events
    )


def test_completion_gate_no_infinite_loop_without_tools(tmp_path: Path) -> None:
    """Regression test: ensure the one-shot guard prevents infinite loops.
    A model that always stops should result in exactly 2 model calls
    and then turn_complete."""
    if not COMPLETION_CHECK_ENABLED:
        return

    client = _ModelStopsImmediately()
    events: list[dict[str, Any]] = []
    loop = AgentLoopV3(
        session_id="completion_gate_no_loop",
        output_dir=tmp_path,
        gemini_client=client,  # type: ignore[arg-type]
        emit_event=events.append,
    )

    # This should not hang or loop forever.
    asyncio.run(loop.run_turn("Stop immediately"))

    # _ModelStopsImmediately does no work, so the conversational carve-out
    # applies: no gate round, exactly ONE model call, then honest stop.
    assert client.calls == 1, f"Expected 1 call, got {client.calls}"

    # No completion_check (gate skipped for a zero-work turn).
    completion_checks = [e for e in events if e.get("kind") == "completion_check"]
    assert len(completion_checks) == 0, f"Expected 0 completion_check, got {len(completion_checks)}"

    # Verify we end with turn_complete (no error or infinite loop).
    turn_errors = [e for e in events if e.get("kind") == "turn_error"]
    assert len(turn_errors) == 0, f"Should have no errors, got {len(turn_errors)}"

    turn_completes = [e for e in events if e.get("kind") == "turn_complete"]
    assert len(turn_completes) == 1, f"Expected 1 turn_complete, got {len(turn_completes)}"
