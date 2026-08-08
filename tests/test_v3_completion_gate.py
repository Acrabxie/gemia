"""The host records execution but never owns the completion verdict."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, AsyncIterator

import gemia.agent_loop_v3 as loop_mod

from gemia.agent_loop_v3 import AgentLoopV3, _relevant_existing_jobs


class _TextOnlyClient:
    model = "fake"

    def __init__(self, text: str = "自然回答。") -> None:
        self.text = text
        self.calls = 0
        self.system_prompts: list[str] = []
        self.tool_names_seen: list[set[str]] = []

    async def stream_turn(
        self,
        messages: list[dict[str, Any]],
        *,
        tools=None,
        temperature: float = 0.7,
    ) -> AsyncIterator[dict[str, Any]]:
        del temperature
        self.calls += 1
        self.system_prompts.append(str(messages[0]["content"]))
        self.tool_names_seen.append(
            {
                str(schema["function"]["name"])
                for schema in (tools or [])
            }
        )
        yield {"kind": "text_delta", "text": self.text}
        yield {"kind": "finish", "reason": "stop"}


class _ToolThenTextClient(_TextOnlyClient):
    def __init__(self, tool_name: str, text: str = "这是实际结果。") -> None:
        super().__init__(text)
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
        self.system_prompts.append(str(messages[0]["content"]))
        self.tool_names_seen.append(
            {
                str(schema["function"]["name"])
                for schema in (tools or [])
            }
        )
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
        yield {"kind": "text_delta", "text": self.text}
        yield {"kind": "finish", "reason": "stop"}


class _ManualImportThenActClient(_TextOnlyClient):
    def __init__(self, media_path: Path, *, refusal_includes_path: bool = True) -> None:
        super().__init__()
        self.media_path = media_path
        self.refusal_includes_path = refusal_includes_path
        self.messages_seen: list[list[dict[str, Any]]] = []

    async def stream_turn(
        self,
        messages: list[dict[str, Any]],
        *,
        tools=None,
        temperature: float = 0.7,
    ) -> AsyncIterator[dict[str, Any]]:
        del temperature
        self.calls += 1
        self.messages_seen.append(list(messages))
        self.tool_names_seen.append(
            {str(schema["function"]["name"]) for schema in (tools or [])}
        )
        if self.calls == 1:
            refusal = (
                f"请在素材库中手动导入：\n`{self.media_path}`"
                if self.refusal_includes_path
                else "当前会话没有提供可将本地路径注册为项目素材的导入能力。"
            )
            yield {
                "kind": "text_delta",
                "text": refusal,
            }
            yield {"kind": "finish", "reason": "stop"}
            return
        if self.calls == 2:
            yield {
                "kind": "tool_call_start",
                "index": 0,
                "id": "call_import",
                "name": "copy_in",
            }
            yield {
                "kind": "tool_call_args_delta",
                "index": 0,
                "delta": json.dumps({"path": str(self.media_path)}),
            }
            yield {"kind": "finish", "reason": "tool_calls"}
            return
        yield {"kind": "text_delta", "text": "已直接导入并取得素材身份。"}
        yield {"kind": "finish", "reason": "stop"}


def _events_of(events: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    return [event for event in events if event.get("kind") == kind]


def test_only_explicitly_continued_session_jobs_bind_to_current_turn() -> None:
    pending = {"job-old": "running", "job-other": "queued"}
    assert _relevant_existing_jobs("查看当前时间线", pending) == {}
    assert _relevant_existing_jobs("检查 job-old 的状态", pending) == {
        "job-old": "running"
    }
    assert _relevant_existing_jobs("继续等待结果", pending) == pending


def test_no_tool_response_is_the_natural_end_of_turn(tmp_path: Path) -> None:
    client = _TextOnlyClient("我会根据我们的对话诚实回答。")
    events: list[dict[str, Any]] = []
    loop = AgentLoopV3(
        session_id="natural_prose",
        output_dir=tmp_path,
        gemini_client=client,  # type: ignore[arg-type]
        emit_event=events.append,
    )

    asyncio.run(loop.run_turn("你对我的了解"))

    assert client.calls == 1
    assert _events_of(events, "model_text_delta")
    assert _events_of(events, "turn_complete")
    assert not _events_of(events, "completion_check")
    assert not _events_of(events, "turn_error")
    assert not _events_of(events, "turn_wrapup")
    assert loop._turn_ledger is None


def test_local_manual_import_deflection_recovers_into_copy_in(tmp_path: Path) -> None:
    media = tmp_path / "outside" / "new-animation.mp4"
    media.parent.mkdir()
    media.write_bytes(b"media")
    client = _ManualImportThenActClient(media)
    events: list[dict[str, Any]] = []
    loop = AgentLoopV3(
        session_id="manual_import_recovery",
        output_dir=tmp_path / "workspace",
        gemini_client=client,  # type: ignore[arg-type]
        emit_event=events.append,
    )

    asyncio.run(loop.run_turn("再试试"))

    assert client.calls == 3
    assert "copy_in" in client.tool_names_seen[0]
    recovery_messages = client.messages_seen[1]
    assert "Do not ask the user" in str(recovery_messages[-1]["content"])
    imported = loop.registry.list_records()
    assert len(imported) == 1
    assert imported[0].path.name == "new-animation.mp4"
    results = _events_of(events, "tool_exec_result")
    assert results[-1]["tool_name"] == "copy_in"
    assert results[-1]["result"]["asset_registered"] is True


def test_import_capability_refusal_uses_exact_path_from_current_request(
    tmp_path: Path,
) -> None:
    media = tmp_path / "outside" / "new animation.mp4"
    media.parent.mkdir()
    media.write_bytes(b"media")
    client = _ManualImportThenActClient(media, refusal_includes_path=False)
    events: list[dict[str, Any]] = []
    loop = AgentLoopV3(
        session_id="capability_refusal_recovery",
        output_dir=tmp_path / "workspace",
        gemini_client=client,  # type: ignore[arg-type]
        emit_event=events.append,
    )

    asyncio.run(loop.run_turn(f"直接导入 {media}，不要让我手动拖入。"))

    assert client.calls == 3
    assert "copy_in" in client.tool_names_seen[0]
    imported = loop.registry.list_records()
    assert len(imported) == 1
    assert imported[0].path.name == "new animation.mp4"
    results = _events_of(events, "tool_exec_result")
    assert results[-1]["tool_name"] == "copy_in"
    assert results[-1]["result"]["asset_registered"] is True


def test_information_classifier_cannot_withhold_tools_or_activity(
    tmp_path: Path,
    monkeypatch,
) -> None:
    async def inspect(args: dict[str, Any], ctx) -> dict[str, Any]:
        del args, ctx
        return {"status": "success", "summary": "inspected"}

    monkeypatch.setitem(loop_mod.DISPATCHER, "probe_media", inspect)
    client = _ToolThenTextClient("probe_media", "我已经看过，再直接回答你。")
    events: list[dict[str, Any]] = []
    loop = AgentLoopV3(
        session_id="information_can_act",
        output_dir=tmp_path,
        gemini_client=client,  # type: ignore[arg-type]
        emit_event=events.append,
    )

    # This is deliberately classified INFORMATION by the legacy presentation
    # heuristic. It must still expose tools and record an actual tool call.
    asyncio.run(loop.run_turn("你是谁"))

    assert "probe_media" in client.tool_names_seen[0]
    assert loop._turn_ledger is not None
    assert loop._turn_ledger.sequence == 1
    assert _events_of(events, "turn_complete")
    assert not _events_of(events, "completion_check")


def test_open_activity_record_cannot_override_model_stop(
    tmp_path: Path,
    monkeypatch,
) -> None:
    async def fail_tool(args: dict[str, Any], ctx) -> dict[str, Any]:
        del args, ctx
        return {
            "error": "not available",
            "error_code": "E_TEST_UNAVAILABLE",
            "recovery": "none",
        }

    monkeypatch.setitem(loop_mod.DISPATCHER, "fake_failure", fail_tool)
    client = _ToolThenTextClient(
        "fake_failure",
        "这个工具现在不可用，所以我没有把它说成成功。",
    )
    events: list[dict[str, Any]] = []
    loop = AgentLoopV3(
        session_id="open_record_natural_stop",
        output_dir=tmp_path,
        gemini_client=client,  # type: ignore[arg-type]
        emit_event=events.append,
    )

    asyncio.run(loop.run_turn("做一个当前不可用的操作"))

    assert client.calls == 2
    assert loop._turn_ledger is not None
    assert loop._turn_ledger.open_observations()
    assert "Tool activity (observational)" in client.system_prompts[1]
    assert "completion=" not in client.system_prompts[1]
    assert "blockers=" not in client.system_prompts[1]
    assert _events_of(events, "model_text_delta")[-1]["delta"].startswith("这个工具")
    assert _events_of(events, "turn_complete")
    assert not _events_of(events, "completion_check")
    assert not [
        event
        for event in events
        if event.get("kind") == "turn_error"
        and event.get("reason") == "incomplete_goal"
    ]
    assert not _events_of(events, "turn_wrapup")


def test_activity_record_still_projects_actual_final_asset(
    tmp_path: Path,
    monkeypatch,
) -> None:
    async def make_image(args: dict[str, Any], ctx) -> dict[str, Any]:
        del args
        path = tmp_path / "final.png"
        path.write_bytes(b"image")
        ctx.registry.register_output(
            "img-final", kind="image", path=path, summary="final image"
        )
        return {"status": "success", "asset_id": "img-final", "kind": "image"}

    monkeypatch.setitem(loop_mod.DISPATCHER, "fake_make_image", make_image)
    events: list[dict[str, Any]] = []
    loop = AgentLoopV3(
        session_id="activity_asset_projection",
        output_dir=tmp_path,
        gemini_client=_ToolThenTextClient("fake_make_image", "图片在这里。"),  # type: ignore[arg-type]
        emit_event=events.append,
    )

    asyncio.run(loop.run_turn("生成一张图片"))

    completed = _events_of(events, "turn_complete")
    assert len(completed) == 1
    assert completed[0]["final_asset_ids"] == ["img-final"]
    assert completed[0]["outcome"] == "progressed"
    assert not _events_of(events, "completion_check")
    assert not [
        event
        for event in events
        if event.get("reason") == "incomplete_goal"
    ]
