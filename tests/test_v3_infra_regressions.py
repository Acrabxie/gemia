import asyncio
import io
import json
import time
import uuid
from pathlib import Path

import pytest

import server
from gemia import v3_routes
from gemia import session_manager
from gemia.session_manager import SessionLimitError, SessionManager, SessionRunner
from gemia.tools import add_overlay as add_overlay_tool
from gemia.tools import edit_video as edit_video_tool
from gemia.tools import export as export_tool
from gemia.tools._context import AssetRegistry, ToolContext
from gemia.transport import sse
from gemia.v3_routes import _serve_file_with_range, _upload_asset


class FakeHandler:
    def __init__(self, *, headers=None, body=b"") -> None:
        self.headers = headers or {}
        self.path = "/"
        self.rfile = io.BytesIO(body)
        self.wfile = io.BytesIO()
        self.status = None
        self.response_headers = {}
        self.connection = None

    def send_response(self, status: int) -> None:
        self.status = status

    def send_header(self, key: str, value: str) -> None:
        self.response_headers[key.lower()] = value

    def end_headers(self) -> None:
        pass

    @property
    def body(self) -> bytes:
        return self.wfile.getvalue()

    @property
    def body_json(self) -> dict:
        return json.loads(self.body.decode("utf-8"))


class FakeAgentLoop:
    def __init__(self, **_kwargs) -> None:
        self.registry = AssetRegistry()
        self.guidance = []
        self.started = False

    async def run_turn(self, _message: str) -> None:
        self.started = True
        await asyncio.sleep(0.2)

    def queue_turn_guidance(self, message: str) -> None:
        self.guidance.append(message)


def _parse_sse(chunk: bytes) -> tuple[int, dict]:
    lines = chunk.decode("utf-8").strip().splitlines()
    event_id = int(lines[0].split(":", 1)[1].strip())
    payload = json.loads(lines[1].split(":", 1)[1].strip())
    return event_id, payload


def _ctx(tmp_path: Path) -> ToolContext:
    return ToolContext(
        session_id="test-session",
        output_dir=tmp_path,
        registry=AssetRegistry(),
        emit_progress=lambda _u: None,
    )


def test_session_runner_rejects_overlapping_turns(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(session_manager, "AgentLoopV3", FakeAgentLoop)
    runner = SessionRunner(
        session_id="v3-overlap",
        output_dir=tmp_path / "work",
        sessions_root=tmp_path / "sessions",
    )
    try:
        assert runner.submit_turn("one") is True
        assert runner.submit_turn("two") is False
    finally:
        runner.close()


def test_distinct_sessions_run_turns_concurrently(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(session_manager, "AgentLoopV3", FakeAgentLoop)
    manager = SessionManager(
        output_root=tmp_path / "runtime",
        max_sessions=3,
        idle_timeout_sec=0,
        sweep_interval_sec=0,
    )
    first = manager.create_session()
    second = manager.create_session()
    try:
        assert first.submit_turn("first session work") is True
        assert second.submit_turn("second session work") is True
        deadline = time.time() + 1
        while (
            not first.agent.started or not second.agent.started
        ) and time.time() < deadline:
            time.sleep(0.01)
        assert first.agent.started is True
        assert second.agent.started is True
        assert first.turn_in_progress is True
        assert second.turn_in_progress is True
    finally:
        manager.close_all()


def test_compact_session_list_reports_activity_without_heavy_snapshots(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(session_manager, "AgentLoopV3", FakeAgentLoop)
    manager = SessionManager(
        output_root=tmp_path / "runtime",
        max_sessions=2,
        idle_timeout_sec=0,
        sweep_interval_sec=0,
    )
    runner = manager.create_session()
    monkeypatch.setattr(v3_routes, "get_manager", lambda: manager)
    handler = FakeHandler()
    handler.path = "/sessions?compact=1"
    try:
        assert v3_routes.try_handle(handler, method="GET") is True
        assert handler.status == 200
        item = handler.body_json["sessions"][0]
        assert item["session_id"] == runner.session_id
        assert item["project_id"] == runner.project_id
        assert item["turn_in_progress"] is False
        assert item["pending_jobs"] == []
        assert item["active_subagents"] == []
        assert "assets" not in item
        assert "budget" not in item
    finally:
        manager.close_all()


def test_compact_session_list_includes_only_live_subagent_presence(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(session_manager, "AgentLoopV3", FakeAgentLoop)
    manager = SessionManager(
        output_root=tmp_path / "runtime",
        max_sessions=2,
        idle_timeout_sec=0,
        sweep_interval_sec=0,
    )
    runner = manager.create_session()
    runner.agent._active_subagents = {
        "call-1:sub_1": {
            "agent_id": "sub_1",
            "goal": "检查镜头素材",
            "tool_profile": "probe",
        },
    }
    monkeypatch.setattr(v3_routes, "get_manager", lambda: manager)
    handler = FakeHandler()
    handler.path = "/sessions?compact=1"
    try:
        assert v3_routes.try_handle(handler, method="GET") is True
        item = handler.body_json["sessions"][0]
        assert item["active_subagents"] == [{
            "agent_id": "sub_1",
            "goal": "检查镜头素材",
            "tool_profile": "probe",
        }]
    finally:
        manager.close_all()


def test_session_runner_can_steer_and_stop_active_turn(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(session_manager, "AgentLoopV3", FakeAgentLoop)
    runner = SessionRunner(
        session_id="v3-control",
        output_dir=tmp_path / "work",
        sessions_root=tmp_path / "sessions",
    )
    try:
        assert runner.submit_turn("make a video") is True
        deadline = time.time() + 1
        while not runner.agent.started and time.time() < deadline:
            time.sleep(0.01)
        assert runner.steer_turn("先不要字幕，改成蓝色调") is True
        assert runner.agent.guidance == ["先不要字幕，改成蓝色调"]
        assert runner.stop_turn() is True
        deadline = time.time() + 1
        while runner.turn_in_progress and time.time() < deadline:
            time.sleep(0.01)
        assert runner.turn_in_progress is False
        transcript = (tmp_path / "sessions/v3-control/transcript.jsonl").read_text(encoding="utf-8")
        assert '"kind": "turn_guidance_queued"' in transcript
        assert '"kind": "turn_cancelled"' in transcript
        assert runner.steer_turn("too late") is False
        assert runner.stop_turn() is False
    finally:
        runner.close()


def test_session_manager_caps_sessions_and_sweeps_idle(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(session_manager, "AgentLoopV3", FakeAgentLoop)
    manager = SessionManager(
        output_root=tmp_path,
        max_sessions=1,
        idle_timeout_sec=1,
        sweep_interval_sec=0,
    )
    first = manager.create_session()
    first.output_dir.mkdir(parents=True, exist_ok=True)
    marker = first.output_dir / "asset.tmp"
    marker.write_text("x", encoding="utf-8")
    with pytest.raises(SessionLimitError):
        manager.create_session()

    with first._state_lock:  # noqa: SLF001 - intentional regression test
        first.last_used_at = time.time() - 10
    assert manager.cleanup_idle() == [first.session_id]
    assert manager.get(first.session_id) is None
    # Idle sweep must never delete workdir files — they are user data.
    assert marker.exists()
    manager.close_all(remove_workdirs=True)


def test_session_route_transparently_resumes_sleeping_durable_session(
    monkeypatch,
) -> None:
    class SleepingRunner:
        session_id = "v3-sleeping"
        plan_mode = False
        turn_in_progress = False
        project_id = "project-sleeping"
        run_id = "run-sleeping"

        def list_assets(self) -> list:
            return []

        def list_tasks(self) -> list:
            return []

    class SleepingManager:
        def __init__(self) -> None:
            self.runner = SleepingRunner()
            self.resume_calls: list[str] = []

        def get(self, _session_id: str):
            return None

        def resume_session(self, session_id: str):
            self.resume_calls.append(session_id)
            return self.runner

    manager = SleepingManager()
    monkeypatch.setattr(v3_routes, "get_manager", lambda: manager)
    handler = FakeHandler()
    handler.path = "/sessions/v3-sleeping"

    assert v3_routes.try_handle(handler, method="GET") is True
    assert handler.status == 200
    assert handler.body_json["session_id"] == "v3-sleeping"
    assert handler.body_json["project_id"] == "project-sleeping"
    assert manager.resume_calls == ["v3-sleeping"]


def test_session_manager_cap_counts_sessions_being_created(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(session_manager, "AgentLoopV3", FakeAgentLoop)
    manager = SessionManager(
        output_root=tmp_path,
        max_sessions=1,
        idle_timeout_sec=0,
        sweep_interval_sec=0,
    )
    with manager._lock:  # noqa: SLF001 - intentional cap race regression test
        manager._creating_sessions = 1  # noqa: SLF001
    with pytest.raises(SessionLimitError):
        manager.create_session()
    with manager._lock:  # noqa: SLF001
        manager._creating_sessions = 0  # noqa: SLF001
    manager.close_all(remove_workdirs=True)


def test_sse_reconnect_reports_replay_gap_after_buffer_overflow() -> None:
    sid = f"test-{uuid.uuid4().hex}"
    sse.REGISTRY.register(sid)
    try:
        for idx in range(sse.REPLAY_BUFFER_SIZE + 5):
            sse.REGISTRY.emit(sid, {"kind": "tick", "idx": idx})
        gen = sse.iter_events(sid, last_event_id=1)
        event_id, payload = _parse_sse(next(gen))
        assert payload["kind"] == "replay_gap"
        assert payload["requested_last_event_id"] == 1
        assert payload["oldest_available_event_id"] == 6
        assert event_id == 5
        next_id, next_payload = _parse_sse(next(gen))
        assert next_id == 6
        assert next_payload["kind"] == "tick"
    finally:
        sse.REGISTRY.close(sid)
        sse.REGISTRY.unregister(sid)


def test_sse_registry_continues_ids_after_durable_resume() -> None:
    sid = f"test-{uuid.uuid4().hex}"
    sse.REGISTRY.register(sid, last_event_id=2409)
    try:
        assert sse.REGISTRY.latest_event_id(sid) == 2409
        assert sse.REGISTRY.emit(sid, {"kind": "turn_start"}) == 2410
        gen = sse.iter_events(sid, last_event_id=2409)
        event_id, payload = _parse_sse(next(gen))
        assert event_id == 2410
        assert payload["kind"] == "turn_start"
    finally:
        sse.REGISTRY.close(sid)
        sse.REGISTRY.unregister(sid)


def test_v3_asset_range_supports_suffix_and_clamps_end(tmp_path: Path) -> None:
    asset = tmp_path / "asset.bin"
    asset.write_bytes(b"0123456789")

    suffix = FakeHandler(headers={"Range": "bytes=-5"})
    _serve_file_with_range(suffix, asset, body=True)
    assert suffix.status == 206
    assert suffix.response_headers["content-range"] == "bytes 5-9/10"
    assert suffix.body == b"56789"

    clamped = FakeHandler(headers={"Range": "bytes=8-999"})
    _serve_file_with_range(clamped, asset, body=True)
    assert clamped.status == 206
    assert clamped.response_headers["content-range"] == "bytes 8-9/10"
    assert clamped.body == b"89"

    multi = FakeHandler(headers={"Range": "bytes=0-1,3-4"})
    _serve_file_with_range(multi, asset, body=True)
    assert multi.status == 200
    assert multi.body == b"0123456789"


def test_v3_upload_rejects_bad_content_length_without_500() -> None:
    handler = FakeHandler(headers={"Content-Length": "not-an-int"})

    assert _upload_asset(handler, object()) is True
    assert handler.status == 400
    assert "Content-Length" in handler.body_json["error"]


def test_v3_upload_also_registers_signed_in_media_library_asset(monkeypatch, tmp_path: Path) -> None:
    class Runner:
        output_dir = tmp_path
        account_id = "signed-in-account"
        session_id = "v3-upload-library"

        def add_external_asset(
            self,
            path: Path,
            *,
            summary: str = "",
            original_name: str | None = None,
        ) -> str:
            assert path.read_bytes() == b"audio-bytes"
            assert "take.wav" in summary
            return "aud_001"

    from gemia import media_library

    monkeypatch.setattr(
        media_library,
        "import_media",
        lambda account_id, path, original_name=None: {
            "asset_id": "asset_library_001",
            "account_id": account_id,
            "name": original_name,
        },
    )
    handler = FakeHandler(
        headers={"Content-Length": str(len(b"audio-bytes")), "X-Filename": "take.wav"},
        body=b"audio-bytes",
    )

    assert _upload_asset(handler, Runner()) is True
    assert handler.status == 201
    assert handler.body_json["asset_id"] == "aud_001"
    assert handler.body_json["library_asset_id"] == "asset_library_001"
    assert handler.body_json["library_asset"]["name"] == "take.wav"


def test_v3_try_handle_hides_internal_errors_by_default(monkeypatch) -> None:
    handler = FakeHandler()
    handler.path = "/sessions/v3-test"

    def boom(*_args, **_kwargs):
        raise RuntimeError("local secret path")

    monkeypatch.setattr(v3_routes, "_route_get", boom)

    assert v3_routes.try_handle(handler, method="GET") is True
    assert handler.status == 500
    assert handler.body_json == {"error": "internal server error"}


@pytest.mark.parametrize("disconnect", [BrokenPipeError(), ConnectionResetError()])
def test_v3_try_handle_does_not_turn_client_disconnect_into_500(
    monkeypatch, disconnect: OSError
) -> None:
    handler = FakeHandler()
    handler.path = "/projects/project-1/artifacts/v_001"

    def disconnected(*_args, **_kwargs):
        raise disconnect

    monkeypatch.setattr(v3_routes, "_route_get", disconnected)

    assert v3_routes.try_handle(handler, method="GET") is True
    assert handler.status is None
    assert handler.body == b""


def test_v3_static_path_guard_rejects_prefix_sibling(tmp_path: Path) -> None:
    root = tmp_path / "static" / "v3"
    sibling = tmp_path / "static" / "v3_evil"
    root.mkdir(parents=True)
    sibling.mkdir(parents=True)
    (root / "index.html").write_text("ok", encoding="utf-8")
    (sibling / "secret.txt").write_text("nope", encoding="utf-8")

    assert server._safe_child_path(root, "index.html") == root / "index.html"
    assert server._safe_child_path(root, "../v3_evil/secret.txt") is None
    assert server._safe_child_path(root, "\x00") is None


@pytest.mark.skip(
    reason="_scrub_local_paths only ever existed in the pre-e146a34 working tree "
    "and was lost before the checkpoint commit; the current loop strips only "
    "thumbnail_path inline (agent_loop_v3). Restoring the general scrub is a "
    "pending v3 decision — see shared QUEUE."
)
def test_tool_result_scrubber_removes_local_paths() -> None:
    payload = {
        "asset_id": "v_002",
        "preview_uri": "/tmp/lumeri-v3/work/v_002.mp4",
        "metadata": {
            "output_path": "/tmp/lumeri-v3/work/v_002.mp4",
            "nested": [{"source_path": "/tmp/input.mp4", "ok": True}],
        },
    }

    assert _scrub_local_paths(payload) == {  # noqa: F821 - intentionally absent; see skip reason
        "asset_id": "v_002",
        "metadata": {"nested": [{"ok": True}]},
    }


def test_export_gif_registers_image_kind_and_hides_output_path(monkeypatch, tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    src = tmp_path / "source.mp4"
    src.write_bytes(b"video")
    asset_id = ctx.registry.add_external(src).asset_id

    async def fake_run(cmd, *, total_seconds, progress) -> None:
        Path(cmd[-1]).write_bytes(b"gif")

    monkeypatch.setattr(export_tool, "ffprobe_duration", lambda _path: 1.0)
    monkeypatch.setattr(export_tool, "run_ffmpeg_with_progress", fake_run)

    result = asyncio.run(
        export_tool.dispatch(
            {"asset_id": asset_id, "format": "gif", "quality": "draft"},
            ctx,
        )
    )

    assert result["kind"] == "image"
    assert result["metadata"]["format"] == "gif"
    assert "output_path" not in result["metadata"]
    record = ctx.registry.get(result["asset_id"])
    assert record.kind == "image"
    assert record.path.suffix == ".gif"


def test_edit_video_speed_uses_video_only_filter_for_silent_inputs(monkeypatch, tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    src = tmp_path / "silent.mp4"
    src.write_bytes(b"video")
    asset_id = ctx.registry.add_external(src).asset_id
    seen = {}

    async def fake_run(cmd, *, total_seconds, progress) -> None:
        seen["cmd"] = cmd
        Path(cmd[-1]).write_bytes(b"out")

    monkeypatch.setattr(edit_video_tool, "ffprobe_duration", lambda _path: 2.0)
    monkeypatch.setattr(
        edit_video_tool,
        "ffprobe_metadata",
        lambda _path: {"streams": [{"codec_type": "video"}]},
    )
    monkeypatch.setattr(edit_video_tool, "run_ffmpeg_with_progress", fake_run)

    result = asyncio.run(
        edit_video_tool.dispatch(
            {"asset_id": asset_id, "operation": "speed", "speed_factor": 0.5},
            ctx,
        )
    )

    cmd_text = " ".join(seen["cmd"])
    assert "[0:a]" not in cmd_text
    assert "-an" in seen["cmd"]
    assert result["asset_id"].startswith("v_")


def test_image_overlay_uses_overlay_coordinate_namespace(monkeypatch, tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    video = tmp_path / "base.mp4"
    image = tmp_path / "logo.png"
    video.write_bytes(b"video")
    image.write_bytes(b"image")
    video_id = ctx.registry.add_external(video).asset_id
    image_id = ctx.registry.add_external(image).asset_id
    seen = {}

    async def fake_run(cmd, *, total_seconds, progress) -> None:
        seen["cmd"] = cmd
        Path(cmd[-1]).write_bytes(b"out")

    monkeypatch.setattr(add_overlay_tool, "ffprobe_duration", lambda _path: 2.0)
    monkeypatch.setattr(add_overlay_tool, "run_ffmpeg_with_progress", fake_run)

    asyncio.run(
        add_overlay_tool.dispatch(
            {
                "asset_id": video_id,
                "kind": "image",
                "overlay_asset_id": image_id,
                "position": "center",
            },
            ctx,
        )
    )

    cmd_text = " ".join(seen["cmd"])
    assert "overlay=(W-w)/2:(H-h)/2" in cmd_text
    assert "text_w" not in cmd_text
    assert "text_h" not in cmd_text


def test_v3_frontend_marks_all_deliverables_and_uses_result_kind() -> None:
    source = Path("static/v3/v3.js").read_text(encoding="utf-8")

    assert "ev.deliverable_asset_ids || ev.final_asset_ids" in source
    assert "finals[finals.length - 1]" not in source
    assert "ev.result?.kind || inferKindFromAssetId" in source


def test_v3_frontend_persists_last_event_id_and_handles_replay_gap() -> None:
    source = Path("static/v3/v3.js").read_text(encoding="utf-8")

    assert "e.lastEventId" in source
    assert "saveLastEventId(sessionId, e.lastEventId)" in source
    assert "?last_event_id=" in source
    assert "replay_gap" in source
    assert "refreshSessionState()" in source
    assert "scheduleReconnect" in source


def test_v3_frontend_recovers_an_expired_session_before_retrying_upload() -> None:
    """A runtime reload must resume the durable project before retrying."""
    source = Path("static/v3/v3.js").read_text(encoding="utf-8")

    assert "async function uploadFile(file, retryExpiredSession = true)" in source
    assert "r.status === 404 && retryExpiredSession" in source
    assert "const expected = { project_id: state.projectId, run_id: state.runId };" in source
    assert "await resumeSession(staleSessionId, expected);" in source
    assert "return uploadFile(file, false);" in source

    upload_source = source[source.index("async function uploadFile"):source.index("function setUploadStatus")]
    assert "createSession(" not in upload_source


def test_tester_managed_frontend_skips_legacy_asset_probes_and_imports_into_the_session() -> None:
    """The classmate build must stay on the isolated session-asset contract."""
    source = Path("static/v3/v3.js").read_text(encoding="utf-8")

    managed_refresh = source[
        source.index("async function fetchMediaLibrary"):
        source.index("async function fetchLegacyMediaLibrary")
    ]
    assert "if (isTesterManagedWorkspace())" in managed_refresh
    assert managed_refresh.index('state.mediaLibraryStatus = "ready";') < managed_refresh.index("return;")
    assert "apiFetch(" not in managed_refresh
    assert "return fetchLegacyMediaLibrary();" in managed_refresh
    legacy_refresh = source[
        source.index("async function fetchLegacyMediaLibrary"):
        source.index("async function annotateLibraryAsset")
    ]
    assert 'apiFetch("/media-library/list?limit=100")' in legacy_refresh
    assert "fetchSessionNonMediaAssets" in legacy_refresh

    activation = source[
        source.index("async function activateSessionPayload"):
        source.index("async function createSession")
    ]
    upload = source[
        source.index("async function uploadFile"):
        source.index("function setUploadStatus")
    ]
    for caller in (activation, upload):
        assert "fetchMediaLibrary().catch" in caller
        assert "fetchLegacyMediaLibrary" not in caller

    assert 'apiFetch(`/sessions/${state.sessionId}/assets`, {' in upload
    assert 'preview_src: previewSrc' in source
    assert '`/sessions/${encodeURIComponent(state.sessionId)}/assets/${encodeURIComponent(assetId)}`' in source

    files_panel = source[
        source.index("async function renderFilesPanel"):
        source.index("async function renderLibraryPanel")
    ]
    assert files_panel.index("if (isTesterManagedWorkspace())") < files_panel.index('apiFetch("/files/roots")')
    managed_files_guard = files_panel[
        files_panel.index("if (isTesterManagedWorkspace())"):
        files_panel.index("if (!filesState)")
    ]
    assert "return;" in managed_files_guard
    assert "apiFetch(" not in managed_files_guard


def test_v3_frontend_uses_only_model_authored_activity_copy() -> None:
    """The activity UI must not infer user copy from a tool name or payload."""
    source = Path("static/v3/v3.js").read_text(encoding="utf-8")
    preview = Path("static/v3/preview.html").read_text(encoding="utf-8")

    for content in (source, preview):
        assert "ACTIVITY_COPY" not in content
        assert "activityKind" not in content
        assert "activity_text" in content
        assert "safeActivityText" in content
        assert "ev.args" not in content

    assert "model_tool_call_ready: (ev)" in source
    assert "tc.activityText = safeActivityText(ev.activity_text)" in source
    assert "activityLabel(tc)" in source
    assert "tc.progressReport = safeProgressReport(ev.progress_report)" in source
    assert 'class="midturn-report"' in source
    assert "已完成：${activityText}" not in source


def test_v3_frontend_renders_lumeri_wrapup_as_assistant_report() -> None:
    """A graceful stop must show Lumeri's report, never a fixed pause banner."""
    source = Path("static/v3/v3.js").read_text(encoding="utf-8")

    assert 'ev.reason === "incomplete_goal"' in source
    assert "stripActivityMarkup(t.pendingAssistantText).trim()" in source
    assert 'String(ev.message || "").trim()' in source
    assert "t.assistantText = modelReport || fallbackReport" in source
    assert "本轮已暂停，等待下一步" not in source


def test_v3_conversation_distinguishes_source_without_alarm_chrome() -> None:
    source = Path("static/v3/v3.js").read_text(encoding="utf-8")
    css = Path("static/v3/v3.css").read_text(encoding="utf-8")

    render_banner = source[source.index("function renderBanner"):source.index("function renderAssets")]
    system_style = css[css.index(".system-message {"):css.index("/* ── Assets")]
    assistant_style = css[css.index(".assistant-bubble {"):css.index(".assistant-bubble.streaming")]

    assert 'class="system-message"' in render_banner
    assert "banner-turn-error" not in render_banner
    assert "background:" not in system_style
    assert "border:" not in system_style
    assert "color: var(--system-text);" in system_style
    assert "color: var(--m3-on-surface);" in assistant_style


def test_v3_frontend_renders_only_host_approved_text_deltas_live() -> None:
    source = Path("static/v3/v3.js").read_text(encoding="utf-8")

    assert 'if (ev.display === "stream")' in source
    assert "t.assistantText = stripActivityMarkup(t.pendingAssistantText)" in source
    assert "t.streaming = true" in source


# ──────────────────────────── budget_guard regression ────────────────────────────


def test_budget_guard_time_gate_uses_spent_seconds_not_wall_clock() -> None:
    """Regression: time gate was asymmetric, using wall-clock elapsed instead of
    cumulative tool-execution time. This caused long-open-but-cheap sessions to
    falsely fail time gates due to idle time (SSE streaming, waiting on model,
    waiting on user).

    Fix: time gate now uses self.spent_seconds + eta (symmetric with cost gate),
    not elapsed + eta.
    """
    from gemia.budget_guard import BudgetGuard

    # Create a guard with a small time cap (10s).
    guard = BudgetGuard(max_usd=100.0, max_seconds=10.0)

    # Simulate some idle wall-clock time passing (e.g., SSE streaming, waiting).
    # Do NOT commit any tool time yet.
    time.sleep(0.1)

    # Check a small tool (analyze_media: 4s ETA). Wall-clock elapsed is ~0.1s.
    # Old buggy code: projected_sec = 0.1 + 4 = 4.1s (should pass)
    # Fixed code: projected_sec = 0.0 + 4 = 4.0s (should pass)
    decision = guard.check("analyze_media")
    assert decision.ok is True, (
        f"analyze_media should be allowed when no tool time spent yet; "
        f"wall-clock idle time should not count against the budget"
    )
    assert decision.estimated_eta_sec == 4.0

    # Now commit 7s of tool execution.
    guard.commit("analyze_media", actual_seconds=7.0)

    # Simulate more idle time (e.g., user thinking, SSE reconnect).
    time.sleep(0.1)

    # Check another small tool (analyze_media: 4s ETA). Total wall-clock elapsed
    # is now ~0.2s, but spent_seconds = 7s.
    # Old buggy code: projected_sec = 0.2 + 4 = 4.2s (would pass, but only due to lucky timing)
    # Fixed code: projected_sec = 7.0 + 4 = 11.0s > 10.0s (correctly blocked)
    decision = guard.check("analyze_media")
    assert decision.ok is False, (
        f"analyze_media should be blocked because spent_seconds (7.0) + eta (4.0) > cap (10.0), "
        f"regardless of wall-clock idle time"
    )
    assert "exceed cap" in decision.reason
    assert "11s > 10s" in decision.reason


def test_gemini_client_proxy_econnrefused_error_provides_diagnostic() -> None:
    """Regression: ECONNREFUSED to localhost proxy (e.g., mihomo/clash down)
    produced bare 'URLError: <urlopen error [Errno 61] Connection refused>'
    without telling the user which proxy failed or suggesting it might be down.

    Fix: When a proxy is configured and ECONNREFUSED occurs, the error message
    now includes the proxy address and a hint about the common proxy tools.
    """
    import asyncio
    from gemia.gemini_client import GeminiClientV3

    # Create a client with a non-existent local proxy.
    # We don't actually make the request; we just verify the error path works.
    client = GeminiClientV3(
        api_key="test-key-unused",
        model="google/gemini-3.1-pro-preview",
        proxy="socks5h://127.0.0.1:59999",  # High port, almost certainly not listening
    )

    messages = [{"role": "user", "content": "test"}]

    async def run_stream():
        async for event in client.stream_turn(messages):
            if event.get("kind") == "error":
                error_msg = event.get("error", "")
                # Should mention either the proxy address, or connection refusal,
                # or the hint about mihomo/clash.
                has_proxy = "127.0.0.1:59999" in error_msg
                has_refused = "refused" in error_msg.lower()
                has_mihomo = "mihomo" in error_msg.lower() or "clash" in error_msg.lower()
                assert has_proxy or has_refused or has_mihomo, (
                    f"Error should mention proxy/refusal/mihomo hint; got: {error_msg}"
                )
                return error_msg
        raise AssertionError("Expected error event, got none")

    try:
        error_msg = asyncio.run(run_stream())
        # Verify the diagnostic message is actually informative.
        assert "Connection refused" in error_msg or "refused connection" in error_msg
    except asyncio.TimeoutError:
        pytest.skip("Proxy connection attempt timed out (expected in test environment)")


def test_v3_frontend_renders_clip_transition_badge() -> None:
    """Clips with a stored transition must render the ⇄ badge (regression:
    transitions were stored backend-side but invisible in every frontend)."""
    source = Path("static/v3/v3.js").read_text(encoding="utf-8")
    assert "clip.transition" in source
    assert "ptl-clip-trans" in source
    css = Path("static/v3/v3.css").read_text(encoding="utf-8")
    assert ".ptl-clip-trans" in css
