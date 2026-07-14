"""M4-C end-to-end: timeline verb → SSE event → timeline API route.

These tests cover the full stack from AgentLoopV3 through to the
GET /sessions/{id}/timeline route handler:

  1. Scripted model calls timeline_insert_clip (video) → timeline_op SSE
     event fired, project state updated, turn_complete emitted cleanly.
  2. Scripted model calls timeline_insert_clip (text overlay) → overlay
     track auto-created, timeline_op fired.
  3. After a scripted insert, the route handler returns correct JSON with
     the clip present in the right track.
  4. Route returns 404 for an unknown session_id.
  5. Route returns empty timeline (duration=0, no clips) for a fresh session.

No API keys. The video-path tests require ffmpeg/ffprobe for clip duration
probing; text-only tests run without ffmpeg.
"""
from __future__ import annotations

import asyncio
import io
import json
import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Any, AsyncIterator

import pytest

from gemia.agent_loop_v3 import AgentLoopV3
from gemia import v3_routes
from gemia.tools import DISPATCHER


# ── Scripted model clients ───────────────────────────────────────────────


class _ScriptedInsertVideo:
    """Inserts once, then renders sampled timeline frames before stopping."""

    model = "fake"

    def __init__(self, asset_id: str = "") -> None:
        self.asset_id = asset_id
        self.calls = 0

    async def stream_turn(
        self, messages: list[dict[str, Any]], *, tools=None, temperature: float = 0.7
    ) -> AsyncIterator[dict[str, Any]]:
        del messages, tools, temperature
        self.calls += 1
        if self.calls == 1:
            yield {"kind": "tool_call_start", "index": 0, "id": "c1",
                   "name": "timeline_insert_clip"}
            yield {"kind": "tool_call_args_delta", "index": 0,
                   "delta": json.dumps({"asset_id": self.asset_id})}
            yield {"kind": "finish", "reason": "tool_calls"}
            return
        if self.calls == 2:
            yield {"kind": "tool_call_start", "index": 0, "id": "c2",
                   "name": "inspect_timeline"}
            yield {"kind": "tool_call_args_delta", "index": 0, "delta": "{}"}
            yield {"kind": "finish", "reason": "tool_calls"}
            return
        yield {"kind": "text_delta", "text": "Inserted and visually reviewed the clip in V1."}
        yield {"kind": "finish", "reason": "stop"}


class _ScriptedInsertText:
    """Inserts a text overlay, then renders sampled frames before stopping."""

    model = "fake"

    def __init__(self) -> None:
        self.calls = 0

    async def stream_turn(
        self, messages: list[dict[str, Any]], *, tools=None, temperature: float = 0.7
    ) -> AsyncIterator[dict[str, Any]]:
        del messages, tools, temperature
        self.calls += 1
        if self.calls == 1:
            yield {"kind": "tool_call_start", "index": 0, "id": "c1",
                   "name": "timeline_insert_clip"}
            yield {"kind": "tool_call_args_delta", "index": 0,
                   "delta": json.dumps({
                       "text": {"content": "Hello World", "color": "#ff0000"},
                       "duration": 2.5,
                   })}
            yield {"kind": "finish", "reason": "tool_calls"}
            return
        if self.calls == 2:
            yield {"kind": "tool_call_start", "index": 0, "id": "c2",
                   "name": "inspect_timeline"}
            yield {"kind": "tool_call_args_delta", "index": 0, "delta": "{}"}
            yield {"kind": "finish", "reason": "tool_calls"}
            return
        yield {"kind": "text_delta", "text": "Added and visually reviewed the text overlay."}
        yield {"kind": "finish", "reason": "stop"}


# ── Minimal fake HTTP handler (mirrors test_v3_infra_regressions.py) ────


class _FakeHandler:
    def __init__(self) -> None:
        self.headers: dict[str, str] = {}
        self.path: str = "/"
        self.wfile = io.BytesIO()
        self.status: int | None = None
        self.response_headers: dict[str, str] = {}

    def send_response(self, status: int) -> None:
        self.status = status

    def send_header(self, key: str, value: str) -> None:
        self.response_headers[key.lower()] = value

    def end_headers(self) -> None:
        pass

    @property
    def body_json(self) -> dict:
        return json.loads(self.wfile.getvalue().decode("utf-8"))


# ── Tests requiring ffmpeg (video asset probing) ─────────────────────────

_needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg + ffprobe required",
)


@_needs_ffmpeg
def test_insert_video_clip_emits_timeline_op_and_updates_project(
    sample_video_path: str, tmp_path: Path
) -> None:
    """Full loop: scripted model inserts a video clip → timeline_op event,
    project updated, turn completes cleanly."""
    events: list[dict[str, Any]] = []
    client = _ScriptedInsertVideo()
    loop = AgentLoopV3(
        session_id="m4c-e2e-01",
        output_dir=tmp_path,
        gemini_client=client,
        emit_event=events.append,
    )
    client.asset_id = loop.add_external_asset(Path(sample_video_path), summary="clip")

    asyncio.run(loop.run_turn("insert the clip into the timeline"))

    # timeline_op event was emitted once, with correct payload
    tl_ops = [e for e in events if e.get("kind") == "timeline_op"]
    assert len(tl_ops) == 1
    assert "insert_clip" in tl_ops[0]["ops"]
    assert tl_ops[0]["clip_count"] == 1
    assert tl_ops[0]["seq"] == 1

    # Project state reflects the insert
    proj = loop.project.load()
    clips = proj["timeline"]["clips"]
    assert len(clips) == 1
    assert clips[0]["asset_id"] == client.asset_id
    assert clips[0]["track_id"] == "V1"
    assert clips[0]["media_kind"] == "video"

    # Turn completed without errors
    assert any(e.get("kind") == "turn_complete" for e in events)
    assert not any(e.get("kind") == "turn_error" for e in events)


@_needs_ffmpeg
def test_timeline_route_reflects_inserted_clip(
    sample_video_path: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After a scripted insert, GET /sessions/{id}/timeline returns the clip
    in the correct track."""
    SID = "m4c-route-01"
    events: list[dict[str, Any]] = []
    client = _ScriptedInsertVideo()
    loop = AgentLoopV3(
        session_id=SID,
        output_dir=tmp_path,
        gemini_client=client,
        emit_event=events.append,
    )
    client.asset_id = loop.add_external_asset(Path(sample_video_path), summary="clip")
    asyncio.run(loop.run_turn("insert the clip"))

    # Wire a fake manager that returns this loop's project
    fake_runner = SimpleNamespace(agent=loop)
    monkeypatch.setattr(
        v3_routes, "get_manager",
        lambda: SimpleNamespace(get=lambda sid: fake_runner if sid == SID else None),
    )

    handler = _FakeHandler()
    ok = v3_routes._session_timeline(handler, SID)

    assert ok is True
    assert handler.status == 200
    data = handler.body_json
    assert data["session_id"] == SID
    assert data["patch_seq"] == 1
    assert data["fps"] == pytest.approx(30.0)

    v1 = next((t for t in data["tracks"] if t["id"] == "V1"), None)
    assert v1 is not None, "V1 track missing from response"
    assert len(v1["clips"]) == 1
    clip = v1["clips"][0]
    assert clip["media_kind"] == "video"
    assert clip["track_id"] == "V1"
    assert clip["enabled"] is True


# ── Tests that do not require ffmpeg (text clips, empty state) ───────────


@_needs_ffmpeg
def test_insert_text_clip_emits_timeline_op_with_overlay_track(
    sample_video_path: str, tmp_path: Path,
) -> None:
    """Scripted model inserts a text clip → OV1 auto-created, timeline_op fired."""
    events: list[dict[str, Any]] = []
    client = _ScriptedInsertText()
    loop = AgentLoopV3(
        session_id="m4c-e2e-02",
        output_dir=tmp_path,
        gemini_client=client,
        emit_event=events.append,
    )
    base_asset = loop.add_external_asset(Path(sample_video_path), summary="base clip")
    asyncio.run(
        DISPATCHER["timeline_insert_clip"](
            {"asset_id": base_asset}, loop._tool_ctx
        )
    )
    events.clear()

    asyncio.run(loop.run_turn("add a title overlay"))

    tl_ops = [e for e in events if e.get("kind") == "timeline_op"]
    assert len(tl_ops) == 1
    assert "insert_clip" in tl_ops[0]["ops"]

    proj = loop.project.load()
    clips = proj["timeline"]["clips"]
    assert len(clips) == 2
    text_clip = next(clip for clip in clips if clip["media_kind"] == "text")
    assert text_clip["track_id"] == "OV1"
    assert text_clip["text_config"]["content"] == "Hello World"
    assert text_clip["duration"] == pytest.approx(2.5)

    tracks = {t["id"]: t for t in proj["timeline"]["tracks"]}
    assert "OV1" in tracks
    assert tracks["OV1"]["kind"] == "overlay"

    assert any(e.get("kind") == "turn_complete" for e in events)
    assert not any(e.get("kind") == "turn_error" for e in events)


def test_timeline_route_returns_404_for_unknown_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Route returns HTTP 404 when the session_id doesn't exist."""
    monkeypatch.setattr(
        v3_routes, "get_manager",
        lambda: SimpleNamespace(get=lambda sid: None),
    )
    handler = _FakeHandler()
    ok = v3_routes._session_timeline(handler, "does-not-exist")
    assert ok is True
    assert handler.status == 404
    assert "error" in handler.body_json


def test_timeline_route_returns_empty_state_for_fresh_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fresh session with no clips → duration=0, all tracks have empty clip lists."""
    SID = "m4c-route-fresh"
    loop = AgentLoopV3(
        session_id=SID,
        output_dir=tmp_path,
        gemini_client=SimpleNamespace(model="fake"),
        emit_event=lambda _e: None,
    )
    fake_runner = SimpleNamespace(agent=loop)
    monkeypatch.setattr(
        v3_routes, "get_manager",
        lambda: SimpleNamespace(get=lambda sid: fake_runner if sid == SID else None),
    )

    handler = _FakeHandler()
    ok = v3_routes._session_timeline(handler, SID)

    assert ok is True
    assert handler.status == 200
    data = handler.body_json
    assert data["duration"] == pytest.approx(0.0)
    assert data["patch_seq"] == 0
    all_clips = [c for t in data["tracks"] for c in t["clips"]]
    assert all_clips == []
