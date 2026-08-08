from __future__ import annotations

import io
import json
import time
from pathlib import Path

import pytest

from gemia import session_history, session_manager, v3_routes
from gemia.budget_guard import BudgetGuard
from gemia.production_store import ProductionNotFoundError, RevisionConflictError
from gemia.project_store import ProjectHandle
from gemia.session_manager import SessionManager
from gemia.tools._context import AssetRegistry, ToolContext


class _DurableLoopDouble:
    """Small loop double that exercises SessionRunner's real persistence."""

    def __init__(self, **kwargs) -> None:
        self.session_id = kwargs["session_id"]
        registry = kwargs.get("asset_registry")
        self.registry = registry if registry is not None else AssetRegistry()
        self.project = ProjectHandle.open(
            kwargs["project_root"],
            kwargs["project_id"],
            session_id=self.session_id,
        )
        self._emit = kwargs["emit_event"]
        self._messages = list((kwargs.get("runtime_state") or {}).get("messages") or [])
        self._turn_count = int((kwargs.get("runtime_state") or {}).get("turn_count") or 0)
        self.plan_mode = bool((kwargs.get("runtime_state") or {}).get("plan_mode", False))
        self.budget = BudgetGuard(
            max_usd=1.0e100,
            max_seconds=None,
            production_media_budget=kwargs.get("production_media_budget"),
        )
        self._tool_ctx = ToolContext(
            session_id=self.session_id,
            output_dir=kwargs["output_dir"],
            registry=self.registry,
            emit_progress=lambda _update: None,
            extra=dict(kwargs.get("extra") or {}),
            project=self.project,
        )

    async def run_turn(self, message: str) -> None:
        self._messages.append({"role": "user", "content": message})
        self._messages.append({"role": "assistant", "content": "progress saved"})
        self._turn_count += 1
        self._emit({"kind": "turn_complete", "outcome": "progressed"})

    def snapshot_runtime_state(self) -> dict:
        return {
            "messages": list(self._messages),
            "turn_count": self._turn_count,
            "plan_mode": self.plan_mode,
            "budget": self.budget.snapshot(),
        }

    def add_external_asset(self, path: Path, *, summary: str = "") -> str:
        return self.registry.add_external(path, summary=summary or None).asset_id

    def persist_jobs(self) -> None:
        return None

    def poll_background_jobs(self) -> dict:
        return {"pending": 0, "had_fast_fail": False}

    def has_pending_background_notifications(self) -> bool:
        return False

    async def run_background_resume_turn(self) -> bool:
        return False

    def queue_turn_guidance(self, _message: str) -> None:
        return None

    def set_plan_mode(self, enabled: bool) -> bool:
        self.plan_mode = bool(enabled)
        return self.plan_mode


def _wait_idle(runner, timeout: float = 2.0) -> None:
    deadline = time.time() + timeout
    while runner.turn_in_progress and time.time() < deadline:
        time.sleep(0.01)
    assert runner.turn_in_progress is False


def _patch_history_roots(monkeypatch, tmp_path: Path) -> Path:
    root = tmp_path / "ui-history"
    monkeypatch.setattr(session_history, "SESSION_ROOT", root)
    monkeypatch.setattr(session_history, "CURRENT_SESSION_PATH", root / "current.json")
    monkeypatch.setattr(session_history, "SNAPSHOT_ROOT", root / "history")
    return root


def test_session_pin_orders_project_and_delete_is_recoverable(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(session_manager, "AgentLoopV3", _DurableLoopDouble)
    manager = SessionManager(
        output_root=tmp_path,
        max_sessions=3,
        idle_timeout_sec=0,
        sweep_interval_sec=0,
    )
    try:
        project = manager.create_project(name="Film")
        first = manager.create_session(project_id=project["project_id"])
        second = manager.create_session(fork_from_project_id=project["project_id"])

        manager.set_session_pinned(first.session_id, True)
        visible = manager.list_projects()[0]["sessions"]
        assert [item["session_id"] for item in visible[:2]] == [
            first.session_id,
            second.session_id,
        ]

        deleted = manager.delete_session(first.session_id)
        assert deleted["deleted_at"]
        assert first.session_id not in {
            item["session_id"] for item in manager.list_projects()[0]["sessions"]
        }
        retained = manager.list_persisted_sessions(include_deleted=True)
        assert any(
            item["session_id"] == first.session_id and item["deleted_at"]
            for item in retained
        )
        with pytest.raises(ProductionNotFoundError):
            manager.resume_session(first.session_id)
    finally:
        manager.close_all()


def test_close_resume_restores_project_assets_messages_and_turn_idempotency(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(session_manager, "AgentLoopV3", _DurableLoopDouble)
    manager = SessionManager(
        output_root=tmp_path,
        max_sessions=3,
        idle_timeout_sec=0,
        sweep_interval_sec=0,
    )
    first = manager.create_session()
    session_id, project_id, run_id = first.session_id, first.project_id, first.run_id
    media = tmp_path / "source.mp4"
    media.write_bytes(b"motion")
    asset_id = first.add_external_asset(media, summary="source footage")
    scheduled = first.submit_turn_request("make a rough cut", client_turn_id="turn-a")
    assert scheduled["scheduled"] is True
    _wait_idle(first)
    before = first.snapshot()
    before_revision = before["project_revision"]
    transcript_path = manager.sessions_root / session_id / "transcript.jsonl"
    before_seq = max(json.loads(line)["seq"] for line in transcript_path.read_text().splitlines())
    assert session_manager.SSE_REGISTRY.latest_event_id(session_id) == before_seq

    manager.close_session(session_id)
    assert manager.get(session_id) is None

    restarted = SessionManager(
        output_root=tmp_path,
        max_sessions=3,
        idle_timeout_sec=0,
        sweep_interval_sec=0,
    )
    resumed = restarted.resume_session(session_id)
    assert session_manager.SSE_REGISTRY.latest_event_id(session_id) == before_seq
    assert (resumed.project_id, resumed.run_id) == (project_id, run_id)
    assert resumed.project_revision == before_revision
    assert resumed.agent.registry.get(asset_id).sha256
    assert resumed.agent._messages[-1]["content"] == "progress saved"

    with pytest.raises(RevisionConflictError) as stale:
        resumed.submit_turn_request(
            "must not start",
            client_turn_id="turn-stale-after-restart",
            expected_project_revision=max(0, before_revision - 1),
        )
    assert stale.value.current_revision == before_revision
    assert all(
        message.get("content") != "must not start"
        for message in resumed.agent._messages
    )

    duplicate = resumed.submit_turn_request(
        "make a rough cut", client_turn_id="turn-a"
    )
    assert duplicate["duplicate"] is True
    assert duplicate["scheduled"] is False
    next_turn = resumed.submit_turn_request("local revision", client_turn_id="turn-b")
    assert next_turn["scheduled"] is True
    _wait_idle(resumed)
    after_seq = max(json.loads(line)["seq"] for line in transcript_path.read_text().splitlines())
    assert after_seq > before_seq
    assert resumed.project_revision == before_revision
    restarted.close_all()


def test_idle_swept_session_is_transparently_resumed_by_session_route(
    monkeypatch, tmp_path: Path
) -> None:
    class Handler:
        headers: dict[str, str] = {}

        def __init__(self) -> None:
            self.wfile = io.BytesIO()
            self.status: int | None = None

        def send_response(self, status: int) -> None:
            self.status = status

        def send_header(self, _key: str, _value: str) -> None:
            return None

        def end_headers(self) -> None:
            return None

    monkeypatch.setattr(session_manager, "AgentLoopV3", _DurableLoopDouble)
    manager = SessionManager(
        output_root=tmp_path,
        max_sessions=3,
        idle_timeout_sec=1,
        sweep_interval_sec=0,
    )
    first = manager.create_session()
    session_id = first.session_id
    project_id = first.project_id
    with first._state_lock:  # noqa: SLF001 - intentional idle-expiry regression
        first.last_used_at = time.time() - 10
    assert manager.cleanup_idle() == [session_id]
    assert manager.get(session_id) is None

    monkeypatch.setattr(v3_routes, "get_manager", lambda: manager)
    handler = Handler()
    assert v3_routes._session_info(handler, session_id) is True
    assert handler.status == 200
    payload = json.loads(handler.wfile.getvalue())
    assert payload["session_id"] == session_id
    assert payload["project_id"] == project_id
    assert manager.get(session_id) is not None
    manager.close_all()


def test_new_session_in_existing_project_isolates_context_and_inherits_assets(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(session_manager, "AgentLoopV3", _DurableLoopDouble)
    manager = SessionManager(
        output_root=tmp_path / "runtime",
        max_sessions=3,
        idle_timeout_sec=0,
        sweep_interval_sec=0,
    )
    project = manager.create_project(name="Shared", source_root=None)
    first = manager.create_session(project_id=project["project_id"])
    media = tmp_path / "shared.mp4"
    media.write_bytes(b"shared motion")
    asset_id = first.add_external_asset(media, summary="shared source")

    second = manager.create_session(project_id=project["project_id"])
    assert second.project_id != first.project_id
    assert second.run_id != first.run_id
    assert second.agent.registry.get(asset_id).summary == "shared source"
    assert (
        manager.production_store.load_project(second.project_id)[
            "forked_from_project_id"
        ]
        == first.project_id
    )
    visible = manager.list_projects()
    assert [item["project_id"] for item in visible] == [first.project_id]
    assert {
        item["session_id"] for item in visible[0]["sessions"]
    } == {first.session_id, second.session_id}
    manager.close_all()


def test_forked_production_inherits_assets_but_not_timeline_or_design(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(session_manager, "AgentLoopV3", _DurableLoopDouble)
    manager = SessionManager(
        output_root=tmp_path / "runtime",
        max_sessions=3,
        idle_timeout_sec=0,
        sweep_interval_sec=0,
    )
    original = manager.create_project(name="Brand Film", source_root=None)
    first = manager.create_session(project_id=original["project_id"])
    media = tmp_path / "logo.mp4"
    media.write_bytes(b"brand source")
    asset_id = first.add_external_asset(media, summary="shared logo source")
    original_title = first.agent.project.store.load(first.project_id)["title"]
    design_file = (
        manager.production_store.project_dir(first.project_id)
        / "design"
        / "old_melt.py"
    )
    design_file.write_text("print('old melt')\n", encoding="utf-8")

    forked = manager.create_session(fork_from_project_id=first.project_id)

    assert forked.project_id != first.project_id
    assert forked.run_id != first.run_id
    assert forked.agent.registry.get(asset_id).summary == "shared logo source"
    assert forked.agent.project.store.load(forked.project_id)["title"] != original_title
    assert not (
        manager.production_store.project_dir(forked.project_id)
        / "design"
        / "old_melt.py"
    ).exists()
    record = manager.production_store.load_project(forked.project_id)
    assert record["forked_from_project_id"] == first.project_id
    visible_projects = manager.list_projects()
    assert [item["project_id"] for item in visible_projects] == [first.project_id]
    assert {
        item["session_id"] for item in visible_projects[0]["sessions"]
    } == {first.session_id, forked.session_id}
    manager.close_all()


def test_project_session_handoff_transfers_assets_without_context_or_timeline(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(session_manager, "AgentLoopV3", _DurableLoopDouble)
    manager = SessionManager(
        output_root=tmp_path / "runtime",
        max_sessions=3,
        idle_timeout_sec=0,
        sweep_interval_sec=0,
    )
    try:
        project = manager.create_project(name="Campaign")
        source = manager.create_session(project_id=project["project_id"])
        target = manager.create_session(fork_from_project_id=source.project_id)
        completed = tmp_path / "completed.mp4"
        completed.write_bytes(b"completed-video")
        source_asset_id = source.add_external_asset(
            completed, summary="approved teaser"
        )

        # A receiving session may be asleep.  The handoff must persist its
        # result without waking or inheriting the source conversation.
        manager.close_session(target.session_id)
        result = manager.handoff_session_assets(source.session_id, target.session_id)
        resumed_target = manager.resume_session(target.session_id)

        assert result["source_session_id"] == source.session_id
        assert result["target_session_id"] == target.session_id
        assert len(result["transferred"]) == 1
        imported_id = result["transferred"][0]["asset_id"]
        imported = resumed_target.agent.registry.get(imported_id)
        assert imported.summary == "approved teaser"
        assert imported.source["handoff"] == {
            "from_session_id": source.session_id,
            "from_project_id": source.project_id,
            "source_asset_id": source_asset_id,
        }
        assert resumed_target.agent.project.store.load(target.project_id)["timeline"]["clips"] == []
        assert resumed_target.agent._messages == []

        repeat = manager.handoff_session_assets(source.session_id, target.session_id)
        assert repeat["transferred"] == []
        assert len(repeat["already_available"]) == 1

        unrelated = manager.create_session()
        with pytest.raises(ValueError, match="same Project"):
            manager.handoff_session_assets(source.session_id, unrelated.session_id)
    finally:
        manager.close_all()


def test_linked_history_fresh_manager_restores_same_run_budget_and_assets(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(session_manager, "AgentLoopV3", _DurableLoopDouble)
    history_root = _patch_history_roots(monkeypatch, tmp_path)
    runtime_root = tmp_path / "runtime"
    manager = SessionManager(
        output_root=runtime_root,
        max_sessions=2,
        idle_timeout_sec=0,
        sweep_interval_sec=0,
    )
    runner = manager.create_session()
    media = tmp_path / "licensed-source.mp4"
    media.write_bytes(b"licensed motion")
    asset_id = runner.add_external_asset(media, summary="licensed source footage")
    ledger = manager.production_store.media_budget(runner.project_id, runner.run_id)
    ledger.import_baseline(
        import_key="echo-protocol-existing-spend",
        amount_usd="1.525",
        evidence={"basis": "known pre-ledger provider spend"},
    )
    production_state = runner.snapshot()["production_state"]
    linked = session_history.save_current_session(
        {
            "session_id": runner.session_id,
            "v3_session_id": runner.session_id,
            "project_id": runner.project_id,
            "run_id": runner.run_id,
            "project_revision": runner.project_revision,
            "production_state": production_state,
            "messages": [{"role": "user", "content": "keep this production"}],
        }
    )
    snapshot_meta = session_history.list_session_snapshots()[0]
    raw_snapshot = json.loads(Path(snapshot_meta["path"]).read_text(encoding="utf-8"))
    assert raw_snapshot["session_id"] == runner.session_id
    assert raw_snapshot["project_id"] == runner.project_id
    assert raw_snapshot["run_id"] == runner.run_id
    assert linked["chat_only"] is False

    expected_ids = (runner.session_id, runner.project_id, runner.run_id)
    manager.close_all()
    restored_history = session_history.load_session_snapshot(snapshot_meta["id"])
    restarted = SessionManager(
        output_root=runtime_root,
        max_sessions=2,
        idle_timeout_sec=0,
        sweep_interval_sec=0,
    )
    resumed = restarted.resume_session(restored_history["v3_session_id"])
    snapshot = resumed.snapshot()

    assert (resumed.session_id, resumed.project_id, resumed.run_id) == expected_ids
    assert snapshot["budget"]["spent_usd"] == pytest.approx(1.525)
    assert snapshot["budget"]["reserved_usd"] == pytest.approx(0.0)
    assert resumed.agent.registry.get(asset_id).sha256
    assert history_root.joinpath("current.json").exists()
    restarted.close_all()


def test_resume_missing_run_id_fails_closed_without_creating_a_run(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(session_manager, "AgentLoopV3", _DurableLoopDouble)
    manager = SessionManager(
        output_root=tmp_path,
        max_sessions=2,
        idle_timeout_sec=0,
        sweep_interval_sec=0,
    )
    runner = manager.create_session()
    session_id, project_id = runner.session_id, runner.project_id
    before_run_ids = list(manager.production_store.load_project(project_id)["run_ids"])
    manager.close_all()

    meta_path = manager.production_store.session_meta_path(session_id)
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta.pop("run_id")
    meta_path.write_text(json.dumps(meta), encoding="utf-8")

    restarted = SessionManager(
        output_root=tmp_path,
        max_sessions=2,
        idle_timeout_sec=0,
        sweep_interval_sec=0,
    )
    with pytest.raises(ProductionNotFoundError, match="no durable project/run"):
        restarted.resume_session(session_id)
    assert restarted.production_store.load_project(project_id)["run_ids"] == before_run_ids
    assert restarted.list_sessions() == []
    restarted.close_all()


def test_restart_without_edit_does_not_advance_project_revision(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(session_manager, "AgentLoopV3", _DurableLoopDouble)
    manager = SessionManager(
        output_root=tmp_path,
        max_sessions=2,
        idle_timeout_sec=0,
        sweep_interval_sec=0,
    )
    runner = manager.create_session()
    ids = runner.session_id, runner.project_id
    revisions = [runner.snapshot()["project_revision"] for _ in range(4)]
    assert revisions == [revisions[0]] * 4
    runner.agent.registry.allocate_id("image")
    assert runner.project_revision == revisions[0]
    manager.close_session(ids[0])
    closed_revision = manager.production_store.load_project(ids[1])["revision"]

    restarted = SessionManager(
        output_root=tmp_path,
        max_sessions=2,
        idle_timeout_sec=0,
        sweep_interval_sec=0,
    )
    resumed = restarted.resume_session(ids[0])
    assert [resumed.snapshot()["project_revision"] for _ in range(4)] == [
        closed_revision
    ] * 4
    restarted.close_all()


def test_read_only_snapshot_skips_design_program_verification(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(session_manager, "AgentLoopV3", _DurableLoopDouble)
    manager = SessionManager(
        output_root=tmp_path,
        max_sessions=2,
        idle_timeout_sec=0,
        sweep_interval_sec=0,
    )
    runner = manager.create_session()
    design_root = manager.production_store.project_dir(runner.project_id) / "design"
    design_root.mkdir(parents=True, exist_ok=True)
    (design_root / "composition.py").write_text("FRAME = 1\n", encoding="utf-8")

    calls = 0
    original = manager.production_store.observe_design_program

    def counted_observe(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(
        manager.production_store, "observe_design_program", counted_observe
    )

    runner.snapshot()
    assert calls == 0

    _ = runner.project_revision
    assert calls == 1
    manager.close_all()


def test_budget_sse_and_transcript_use_same_spent_reserved_view(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(session_manager, "AgentLoopV3", _DurableLoopDouble)
    manager = SessionManager(
        output_root=tmp_path,
        max_sessions=2,
        idle_timeout_sec=0,
        sweep_interval_sec=0,
    )
    runner = manager.create_session()
    ledger = manager.production_store.media_budget(runner.project_id, runner.run_id)
    decision = ledger.reserve(
        idempotency_key="image-a",
        tool_name="generate_image",
        estimated_usd=0.101,
        provider="vertex",
        model="image-model",
    )
    assert decision.ok
    event = {"kind": "budget_updated", "budget": {"committed_usd": 999}}
    runner._emit_event(event)  # noqa: SLF001 - verifies the ordered event sink
    assert event["budget"]["spent_usd"] == 0.0
    assert event["budget"]["reserved_usd"] == 0.101
    assert "ledger_path" not in event["budget"]
    transcript = manager.sessions_root / runner.session_id / "transcript.jsonl"
    persisted = json.loads(transcript.read_text(encoding="utf-8").splitlines()[-1])
    assert persisted["event"]["budget"] == event["budget"]
    manager.close_all()
