from __future__ import annotations

import io
import json
from pathlib import Path

from gemia import session_history, v3_contract, v3_routes
from gemia.production_store import RevisionConflictError


class Handler:
    def __init__(self, path: str, payload: dict | None = None, *, empty: bool = False) -> None:
        raw = b"" if empty else json.dumps(payload or {}).encode("utf-8")
        self.path = path
        self.headers = {"Content-Length": str(len(raw))}
        self.rfile = io.BytesIO(raw)
        self.wfile = io.BytesIO()
        self.status: int | None = None
        self.response_headers: dict[str, str] = {}
        self.connection = None

    def send_response(self, status: int) -> None:
        self.status = status

    def send_header(self, key: str, value: str) -> None:
        self.response_headers[key.lower()] = value

    def end_headers(self) -> None:
        pass

    @property
    def json(self) -> dict:
        return json.loads(self.wfile.getvalue().decode("utf-8"))


class Runner:
    session_id = "v3-resume"
    plan_mode = False
    turn_in_progress = False

    def snapshot(self) -> dict:
        return {
            "project_id": "project-1",
            "run_id": "run-1",
            "project_revision": 7,
            "production_state": "rough_cut",
            "budget": {"limit_usd": 15.0, "spent_usd": 1.5, "reserved_usd": 2.8},
            "blockers": [],
        }

    def list_assets(self) -> list[dict]:
        return []

    def list_tasks(self) -> list[dict]:
        return []

    def submit_turn_request(self, message: str, **kwargs) -> dict:
        self.submission = (message, kwargs)
        return {
            "accepted": True,
            "scheduled": True,
            "duplicate": False,
            "client_turn_id": kwargs.get("client_turn_id"),
            "project_id": "project-1",
            "run_id": "run-1",
            "project_revision": 7,
        }


class Manager:
    def __init__(self, artifact: Path | None = None) -> None:
        self.runner = Runner()
        self.artifact = artifact
        self.created_with = None
        self.reviewed_with = None
        self.pinned_with = None
        self.deleted_session = None

    def create_session(self, **kwargs):
        self.created_with = kwargs
        return self.runner

    def resume_session(self, session_id: str):
        assert session_id == "v3-resume"
        return self.runner

    def get(self, session_id: str):
        return self.runner if session_id == self.runner.session_id else None

    def get_project(self, project_id: str) -> dict:
        return {
            "project_id": project_id,
            "project_revision": 7,
            "path": "/private/root",
            "project_state": {
                "assets": [
                    {
                        "id": "v_001",
                        "source_path": "/tmp/input.mp4",
                        "serverPath": "/private/tmp/input.mp4",
                    }
                ]
            },
            "budget": {"ledger_path": "/private/root/budget.json"},
        }

    def get_run(self, project_id: str, run_id: str) -> dict:
        return {"project_id": project_id, "run_id": run_id, "production_state": "rough_cut"}

    def review_run(self, project_id: str, run_id: str, **kwargs) -> dict:
        self.reviewed_with = (project_id, run_id, kwargs)
        return {"production_state": "revising", "project_revision": 7}

    def artifact_path(self, project_id: str, asset_id: str) -> Path | None:
        assert (project_id, asset_id) == ("project-1", "v_001")
        return self.artifact

    def handoff_session_assets(self, source_session_id: str, target_session_id: str) -> dict:
        self.handoff_with = (source_session_id, target_session_id)
        return {
            "source_session_id": source_session_id,
            "target_session_id": target_session_id,
            "transferred": [{"asset_id": "v_002", "kind": "video", "summary": "teaser"}],
            "already_available": [],
            "unavailable": [],
        }

    def create_project(self, **kwargs) -> dict:
        self.project_created_with = kwargs
        return {
            "project_id": "project-new",
            "name": kwargs.get("name") or "Creator Folder",
            "source_root": str(kwargs.get("source_root") or ""),
            "edit_root": "/private/lumeri/projects/project-new/design",
            "sessions": [],
        }

    def list_projects(self) -> list[dict]:
        return [{
            "project_id": "project-1",
            "name": "Film",
            "source_root": "/Users/a/Film",
            "edit_root": "/Users/a/.lumeri/projects/project-1/design",
            "sessions": [{"session_id": "v3-resume"}],
        }]

    def undo_project_files(self, project_id: str) -> dict:
        return {"project_id": project_id, "status": "undone"}

    def redo_project_files(self, project_id: str) -> dict:
        return {"project_id": project_id, "status": "redone"}

    def set_session_pinned(self, session_id: str, pinned: bool) -> dict:
        self.pinned_with = (session_id, pinned)
        return {"session_id": session_id, "pinned": pinned}

    def delete_session(self, session_id: str) -> dict:
        self.deleted_session = session_id
        return {"session_id": session_id, "deleted_at": "2026-07-28T00:00:00+00:00"}


class RevisionConflictRunner(Runner):
    cached_project_revision = 8

    def submit_turn_request(self, message: str, **kwargs) -> dict:
        raise RevisionConflictError(
            "project revision mismatch: expected 7, current 8",
            current_revision=self.cached_project_revision,
        )


class RevisionConflictManager(Manager):
    def __init__(self) -> None:
        super().__init__()
        self.runner = RevisionConflictRunner()


def test_protocol_v2_declares_durable_production_events() -> None:
    assert v3_contract.PROTOCOL_VERSION == 2
    assert {
        "production_state_changed",
        "project_revision_committed",
        "budget_updated",
        "delivery_ready",
        "acceptance_updated",
    } <= v3_contract.EVENT_KINDS


def test_empty_post_sessions_remains_compatible(monkeypatch) -> None:
    manager = Manager()
    monkeypatch.setattr(v3_routes, "get_manager", lambda: manager)
    handler = Handler("/sessions", empty=True)

    assert v3_routes.try_handle(handler, method="POST") is True
    assert handler.status == 201
    assert handler.json["session_id"] == "v3-resume"
    assert handler.json["protocol_version"] == 2
    assert "project_id" not in manager.created_with
    assert "run_id" not in manager.created_with


def test_post_sessions_accepts_durable_project_and_run(monkeypatch) -> None:
    manager = Manager()
    monkeypatch.setattr(v3_routes, "get_manager", lambda: manager)
    handler = Handler("/sessions", {"project_id": "project-1", "run_id": "run-1"})

    assert v3_routes.try_handle(handler, method="POST") is True
    assert handler.status == 201
    assert manager.created_with["project_id"] == "project-1"
    assert manager.created_with["run_id"] == "run-1"
    assert handler.json["resume_url"] == "/sessions/v3-resume/resume"


def test_post_sessions_accepts_independent_production_fork(monkeypatch) -> None:
    manager = Manager()
    monkeypatch.setattr(v3_routes, "get_manager", lambda: manager)
    handler = Handler(
        "/sessions",
        {"fork_from_project_id": "project-1"},
    )

    assert v3_routes.try_handle(handler, method="POST") is True
    assert handler.status == 201
    assert manager.created_with["fork_from_project_id"] == "project-1"


def test_session_pin_and_recoverable_delete_routes(monkeypatch) -> None:
    manager = Manager()
    monkeypatch.setattr(v3_routes, "get_manager", lambda: manager)

    pinned = Handler("/sessions/v3-resume/pin", {"pinned": True})
    assert v3_routes.try_handle(pinned, method="POST") is True
    assert pinned.status == 200
    assert pinned.json["pinned"] is True
    assert manager.pinned_with == ("v3-resume", True)

    deleted = Handler("/sessions/v3-resume", empty=True)
    assert v3_routes.try_handle(deleted, method="DELETE") is True
    assert deleted.status == 200
    assert deleted.json["deleted"] is True
    assert manager.deleted_session == "v3-resume"


def test_local_project_create_list_and_file_history_routes(monkeypatch) -> None:
    manager = Manager()
    monkeypatch.setattr(v3_routes, "get_manager", lambda: manager)

    created = Handler("/projects", {"source_root": "/Users/a/Film", "name": "Film"})
    assert v3_routes.try_handle(created, method="POST") is True
    assert created.status == 201
    assert manager.project_created_with == {"name": "Film", "source_root": "/Users/a/Film"}

    internal = Handler("/projects", {"name": "No Folder"})
    assert v3_routes.try_handle(internal, method="POST") is True
    assert internal.status == 201
    assert manager.project_created_with == {"name": "No Folder", "source_root": None}
    assert internal.json["source_root"] == ""

    listed = Handler("/projects", empty=True)
    assert v3_routes.try_handle(listed, method="GET") is True
    assert listed.status == 200
    assert listed.json["projects"][0]["sessions"][0]["session_id"] == "v3-resume"

    undo = Handler("/projects/project-1/undo", {})
    assert v3_routes.try_handle(undo, method="POST") is True
    assert undo.json["status"] == "undone"
    redo = Handler("/projects/project-1/redo", {})
    assert v3_routes.try_handle(redo, method="POST") is True
    assert redo.json["status"] == "redone"


def test_remote_project_folder_routes_fail_closed(monkeypatch) -> None:
    monkeypatch.setattr(v3_routes, "get_manager", lambda: Manager())
    handler = Handler("/projects", empty=True)
    handler.headers["X-Lumeri-Remote"] = "1"
    assert v3_routes.try_handle(handler, method="GET") is True
    assert handler.status == 403


def test_resume_and_turn_revision_idempotency_routes(monkeypatch) -> None:
    manager = Manager()
    monkeypatch.setattr(v3_routes, "get_manager", lambda: manager)

    resume = Handler("/sessions/v3-resume/resume", empty=True)
    assert v3_routes.try_handle(resume, method="POST") is True
    assert resume.status == 200
    assert resume.json["project_revision"] == 7

    turn = Handler(
        "/sessions/v3-resume/turn",
        {
            "message": "continue the cut",
            "client_turn_id": "turn-123",
            "expected_project_revision": 7,
        },
    )
    assert v3_routes.try_handle(turn, method="POST") is True
    assert turn.status == 202
    assert manager.runner.submission == (
        "continue the cut",
        {"client_turn_id": "turn-123", "expected_project_revision": 7},
    )


def test_turn_revision_conflict_returns_authoritative_admission_revision(
    monkeypatch,
) -> None:
    manager = RevisionConflictManager()
    monkeypatch.setattr(v3_routes, "get_manager", lambda: manager)
    turn = Handler(
        "/sessions/v3-resume/turn",
        {
            "message": "continue the cut",
            "client_turn_id": "turn-123",
            "expected_project_revision": 7,
        },
    )

    assert v3_routes.try_handle(turn, method="POST") is True
    assert turn.status == 409
    assert turn.json == {
        "error": "project revision mismatch: expected 7, current 8",
        "code": "E_REVISION_CONFLICT",
        "project_revision": 8,
    }


def test_project_run_review_and_persistent_artifact_routes(monkeypatch, tmp_path: Path) -> None:
    artifact = tmp_path / "final.mp4"
    artifact.write_bytes(b"video-bytes")
    manager = Manager(artifact)
    monkeypatch.setattr(v3_routes, "get_manager", lambda: manager)

    project = Handler("/projects/project-1", empty=True)
    assert v3_routes.try_handle(project, method="GET") is True
    assert project.status == 200
    assert project.json["project_revision"] == 7
    assert "path" not in project.json
    asset = project.json["project_state"]["assets"][0]
    assert "source_path" not in asset
    assert "serverPath" not in asset
    assert "ledger_path" not in project.json["budget"]

    run = Handler("/projects/project-1/runs/run-1", empty=True)
    assert v3_routes.try_handle(run, method="GET") is True
    assert run.status == 200
    assert run.json["production_state"] == "rough_cut"

    review = Handler(
        "/projects/project-1/runs/run-1/review",
        {
            "action": "request_changes",
            "note": "tighten this beat",
            "start_sec": 25.0,
            "end_sec": 31.0,
            "expected_project_revision": 7,
        },
    )
    assert v3_routes.try_handle(review, method="POST") is True
    assert review.status == 200
    assert manager.reviewed_with[2]["action"] == "request_changes"
    assert manager.reviewed_with[2]["start_sec"] == 25.0

    media = Handler("/projects/project-1/artifacts/v_001", empty=True)
    assert v3_routes.try_handle(media, method="GET") is True
    assert media.status == 200
    assert media.wfile.getvalue() == b"video-bytes"
    assert media.response_headers["accept-ranges"] == "bytes"


def test_post_session_handoff_routes_only_explicit_sibling_target(monkeypatch) -> None:
    manager = Manager()
    monkeypatch.setattr(v3_routes, "get_manager", lambda: manager)

    handler = Handler(
        "/sessions/v3-resume/handoff", {"target_session_id": "v3-target"}
    )
    assert v3_routes.try_handle(handler, method="POST") is True
    assert handler.status == 200
    assert manager.handoff_with == ("v3-resume", "v3-target")
    assert handler.json["transferred"][0]["asset_id"] == "v_002"

    invalid = Handler("/sessions/v3-resume/handoff", {})
    assert v3_routes.try_handle(invalid, method="POST") is True
    assert invalid.status == 400
    assert invalid.json["code"] == "E_INPUT"


def test_review_rejects_unbounded_or_empty_revision_request(monkeypatch) -> None:
    manager = Manager()
    monkeypatch.setattr(v3_routes, "get_manager", lambda: manager)
    handler = Handler(
        "/projects/project-1/runs/run-1/review",
        {"action": "request_changes", "note": "", "start_sec": 3.0},
    )

    assert v3_routes.try_handle(handler, method="POST") is True
    assert handler.status == 400
    assert handler.json["code"] == "E_REVIEW_INVALID"
    assert manager.reviewed_with is None


def test_approve_requires_and_forwards_full_watch_and_five_creative_checks(
    monkeypatch,
) -> None:
    manager = Manager()
    monkeypatch.setattr(v3_routes, "get_manager", lambda: manager)
    creative = {
        "story": True,
        "pacing": True,
        "visual": True,
        "sound": True,
        "publishable": True,
    }
    missing = Handler(
        "/projects/project-1/runs/run-1/review",
        {"action": "approve", "expected_project_revision": 7},
    )
    assert v3_routes.try_handle(missing, method="POST") is True
    assert missing.status == 400
    assert missing.json["code"] == "E_REVIEW_INVALID"
    assert manager.reviewed_with is None

    approved = Handler(
        "/projects/project-1/runs/run-1/review",
        {
            "action": "approve",
            "expected_project_revision": 7,
            "watched_full_video": True,
            "creative_checks": creative,
        },
    )
    assert v3_routes.try_handle(approved, method="POST") is True
    assert approved.status == 200
    assert manager.reviewed_with[2]["watched_full_video"] is True
    assert manager.reviewed_with[2]["creative_checks"] == creative


def test_session_history_v2_keeps_runtime_reference_and_marks_legacy_chat_only(
    monkeypatch, tmp_path: Path
) -> None:
    root = tmp_path / "sessions"
    monkeypatch.setattr(session_history, "SESSION_ROOT", root)
    monkeypatch.setattr(session_history, "CURRENT_SESSION_PATH", root / "current.json")
    monkeypatch.setattr(session_history, "SNAPSHOT_ROOT", root / "history")

    saved = session_history.save_current_session(
        {
            "v3_session_id": "v3-resume",
            "project_id": "project-1",
            "run_id": "run-1",
            "project_revision": 7,
            "production_state": "rough_cut",
            "messages": [{"role": "user", "content": "keep the real project"}],
        }
    )
    assert saved["version"] == 2
    assert saved["chat_only"] is False
    snapshot_id = session_history.list_session_snapshots()[0]["id"]
    restored = session_history.load_session_snapshot(snapshot_id)
    assert restored["session_id"] == snapshot_id
    assert restored["v3_session_id"] == "v3-resume"
    assert restored["project_id"] == "project-1"

    legacy = session_history.save_current_session(
        {"messages": [{"role": "user", "content": "old chat only"}]}
    )
    assert legacy["chat_only"] is True


def test_web_reattaches_history_and_exposes_production_truth() -> None:
    root = Path(__file__).resolve().parents[1]
    source = (root / "static/v3/v3.js").read_text(encoding="utf-8")
    html = (root / "static/v3/index.html").read_text(encoding="utf-8")
    server_source = (root / "server.py").read_text(encoding="utf-8")

    assert "restoreCurrentSessionOrCreate" in source
    assert "/resume`" in source
    assert "v3_session_id" in source
    assert "if (!messages.length && !state.projectId) return;" in source
    assert "await autoSaveSession();\n    return activated;" in source
    assert "仅聊天记录 · 工程未保存" in source
    assert "client_turn_id: turn.clientTurnId" in source
    assert "expected_project_revision: state.projectRevision" in source
    submit_turn = source.split("async function submitTurn(message)", 1)[1].split(
        "async function steerTurn(message)", 1
    )[0]
    assert submit_turn.count("const turn = newTurn(message);") == 1
    assert "for (let attempt = 0; attempt < 2; attempt++)" in submit_turn
    assert 'data.code !== "E_REVISION_CONFLICT"' in submit_turn
    assert "client_turn_id: turn.clientTurnId" in submit_turn
    assert "applyProductionSnapshot(data);" in submit_turn
    assert "data.project_revision !== undefined" in submit_turn
    assert "await refreshSessionState();" in submit_turn
    assert "state.turnInProgress = true;" in submit_turn
    assert "await autoSaveSession();" in submit_turn
    assert "请确认后再次发送" not in submit_turn
    assert "state.productionRevision = Math.floor(revision)" in source
    assert "data.production_revision !== undefined && data.project_revision === undefined" not in source
    assert "/artifacts/${encodeURIComponent(assetId)}`" in source
    for kind in (
        "production_state_changed",
        "project_revision_committed",
        "budget_updated",
        "delivery_ready",
        "acceptance_updated",
    ):
        assert f"{kind}:" in source
    assert 'id="production-strip"' in html
    assert 'id="request-changes-btn"' in html
    assert 'id="approve-production-btn"' in html
    assert 'id="review-watched-full-video"' in html
    assert 'id="delivery-review-master"' in html
    assert 'id="delivery-review-video"' in html
    assert html.count("data-review-dimension=") == 5
    assert "payload.watched_full_video = true" in source
    assert "payload.creative_checks = creativeChecks" in source
    assert "function currentReviewMaster()" in source
    assert 'state.productionDelivery = ev.delivery || null' in source
    assert 'els.approveProductionBtn.disabled = reviewBusy || !deliveryReady' in source
    assert 'generated_video: "生成视频"' in source
    assert server_source.count('path.startswith("/projects/")') == 1
    assert server_source.count('route.startswith("/projects/")') == 1
    assert server_source.count('path == "/projects"') == 1
    assert server_source.count('route == "/projects"') == 1
