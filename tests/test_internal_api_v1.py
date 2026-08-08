from __future__ import annotations

import io
import json
import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from gemia import api_v1_routes
from gemia import v3_routes
from gemia.session_manager import SessionManager, VerbGateError
import server
from tests_http_harness import create_raw_request, run_server_handler


class Handler:
    def __init__(
        self,
        path: str,
        *,
        payload: dict | None = None,
        headers: dict[str, str] | None = None,
        client_address=("127.0.0.1", 43123),
    ) -> None:
        data = (
            json.dumps(payload, ensure_ascii=False).encode("utf-8")
            if payload is not None
            else b""
        )
        self.path = path
        self.headers = {"Content-Length": str(len(data)), **(headers or {})}
        self.client_address = client_address
        self.rfile = io.BytesIO(data)
        self.wfile = io.BytesIO()
        self.status = None
        self.response_headers: dict[str, str] = {}

    def send_response(self, status: int) -> None:
        self.status = status

    def send_header(self, key: str, value: str) -> None:
        self.response_headers[key] = value

    def end_headers(self) -> None:
        pass

    def json(self) -> dict:
        return json.loads(self.wfile.getvalue() or b"{}")


@pytest.fixture()
def manager(tmp_path: Path):
    value = SessionManager(
        output_root=tmp_path / "root",
        sweep_interval_sec=0,
        idle_timeout_sec=0,
    )
    try:
        yield value
    finally:
        value.close_all()


def test_generated_manifest_is_complete_but_excludes_host_escape_hatches() -> None:
    handler = Handler("/api/internal/v1/capabilities")
    assert api_v1_routes.try_handle(handler, method="GET") is True
    assert handler.status == 200
    records = {row["name"]: row for row in handler.json()["capabilities"]}
    assert "timeline_insert_clip" in records
    assert records["timeline_insert_clip"]["effect"] == "write"
    assert records["timeline_insert_clip"]["requires_idempotency_key"] is True
    assert records["web_search"]["effect"] == "read"
    assert "http" in records["web_search"]["exposed_via"]
    assert "web_open" not in records
    assert "run_shell" not in records
    assert "read_file" not in records
    assert "project_import_otio" not in records
    assert "remember" not in records


def test_internal_api_is_closed_to_remote_requests() -> None:
    remote = Handler(
        "/api/internal/v1/capabilities",
        headers={"X-Lumeri-Remote": "1"},
    )
    assert api_v1_routes.try_handle(remote, method="GET") is True
    assert remote.status == 403
    assert remote.json()["error"]["code"] == "E_REMOTE_BLOCKED"


def test_doctor_returns_sources_and_redaction_without_credentials() -> None:
    handler = Handler("/api/internal/v1/doctor")
    assert api_v1_routes.try_handle(handler, method="GET") is True
    assert handler.status == 200
    config = handler.json()["config"]
    assert config["credentials"] == "redacted"
    assert set(config["has_key"]) == {"openrouter", "gemini", "anthropic", "openai"}
    assert "api_key" not in json.dumps(config).lower().replace("has_key", "")
    assert config["resolved_models"]["planner"]["source"]
    assert config["search"]["effective_provider"]
    assert config["search"]["built_in"]["provider"] == "duckduckgo"
    assert config["search"]["built_in"]["available"] is True


def test_invoke_requires_revision_and_idempotency_then_replays(
    manager: SessionManager,
    monkeypatch,
) -> None:
    monkeypatch.setattr(api_v1_routes, "get_manager", lambda: manager)
    runner = manager.create_session()
    path = (
        f"/api/internal/v1/sessions/{runner.session_id}"
        "/capabilities/timeline_insert_clip:invoke"
    )

    missing = Handler(path, payload={"arguments": {"text": {"content": "A"}}})
    assert api_v1_routes.try_handle(missing, method="POST") is True
    assert missing.status == 409
    assert missing.json()["error"]["code"] == "E_IDEMPOTENCY_CONFLICT"

    revision = runner.project_revision
    payload = {
        "arguments": {"text": {"content": "A"}},
        "idempotency_key": "insert-title-a",
        "expected_project_revision": revision,
    }
    first = Handler(path, payload=payload)
    assert api_v1_routes.try_handle(first, method="POST") is True
    assert first.status == 200
    first_body = first.json()
    assert first_body["status"] == "completed"
    assert first_body["result"]["clip_id"]
    assert first_body["project_revision"] > revision

    replay = Handler(path, payload=payload)
    assert api_v1_routes.try_handle(replay, method="POST") is True
    assert replay.status == 200
    assert replay.json()["result"]["clip_id"] == first_body["result"]["clip_id"]

    conflict = Handler(
        path,
        payload={
            **payload,
            "arguments": {"text": {"content": "different"}},
        },
    )
    assert api_v1_routes.try_handle(conflict, method="POST") is True
    assert conflict.status == 409
    assert conflict.json()["error"]["code"] == "E_IDEMPOTENCY_CONFLICT"


def test_snapshot_and_durable_events_use_stable_envelopes(
    manager: SessionManager,
    monkeypatch,
) -> None:
    monkeypatch.setattr(api_v1_routes, "get_manager", lambda: manager)
    runner = manager.create_session()
    runner._emit_event(
        {
            "kind": "tool_exec_start",
            "origin": "internal_http",
            "request_id": "req-fixture",
            "call_id": "call-fixture",
            "project_revision": runner.project_revision,
        }
    )

    snapshot = Handler(
        f"/api/internal/v1/sessions/{runner.session_id}/snapshot"
    )
    assert api_v1_routes.try_handle(snapshot, method="GET") is True
    assert snapshot.status == 200
    assert snapshot.json()["session_id"] == runner.session_id
    assert "project_revision" in snapshot.json()

    events = Handler(
        f"/api/internal/v1/sessions/{runner.session_id}/events?after=0"
    )
    assert api_v1_routes.try_handle(events, method="GET") is True
    body = events.json()
    event = next(row for row in body["events"] if row["request_id"] == "req-fixture")
    assert set(
        {
            "seq",
            "ts",
            "session_id",
            "request_id",
            "origin",
            "kind",
            "project_revision",
            "data",
        }
    ).issubset(event)
    assert event["data"]["call_id"] == "call-fixture"
    assert body["replay_gap"] is False
    assert body["snapshot_required"] is False


def test_snapshot_resumes_sleeping_runner_and_event_gap_requires_snapshot(
    manager: SessionManager,
    monkeypatch,
) -> None:
    monkeypatch.setattr(api_v1_routes, "get_manager", lambda: manager)
    runner = manager.create_session()
    session_id = runner.session_id
    for index in range(3):
        runner._emit_event({"kind": "fixture", "index": index})
    manager.close_session(session_id)
    assert manager.get(session_id) is None

    snapshot = Handler(f"/api/internal/v1/sessions/{session_id}/snapshot")
    assert api_v1_routes.try_handle(snapshot, method="GET") is True
    assert snapshot.status == 200
    assert snapshot.json()["session_id"] == session_id
    assert snapshot.json()["latest_event_seq"] >= 3
    assert manager.get(session_id) is not None

    events = Handler(f"/api/internal/v1/sessions/{session_id}/events?after=1")
    assert api_v1_routes.try_handle(events, method="GET") is True
    body = events.json()
    assert body["events"][0]["seq"] == 2
    assert body["replay_gap"] is False

    transcript = manager.sessions_root / session_id / "transcript.jsonl"
    records = [
        json.loads(line)
        for line in transcript.read_text(encoding="utf-8").splitlines()
    ]
    transcript.write_text(
        "\n".join(
            json.dumps(record)
            for record in records
            if int(record.get("seq") or 0) >= 3
        )
        + "\n",
        encoding="utf-8",
    )
    gap = Handler(f"/api/internal/v1/sessions/{session_id}/events?after=1")
    assert api_v1_routes.try_handle(gap, method="GET") is True
    assert gap.json()["replay_gap"] is True
    assert gap.json()["snapshot_required"] is True


def test_domain_prefix_maps_to_legacy_routes() -> None:
    assert api_v1_routes.is_domain_path("/api/v1/projects")
    assert api_v1_routes.legacy_path("/api/v1/projects") == "/projects"
    assert api_v1_routes.legacy_path("/api/v1/sessions/s1/turn") == "/sessions/s1/turn"


def test_vendored_contract_fixture_matches_generated_document() -> None:
    fixture = (
        Path(__file__).resolve().parents[1]
        / "contracts"
        / "internal-api-v1.json"
    )
    assert json.loads(fixture.read_text(encoding="utf-8")) == api_v1_routes.contract_document()


def test_headless_creator_loop_fixture_uses_only_product_capabilities() -> None:
    root = Path(__file__).resolve().parents[1]
    fixture = json.loads(
        (root / "contracts" / "creator-loop-v1.json").read_text(encoding="utf-8")
    )
    manifest = {
        row["name"]: row
        for row in api_v1_routes.contract_document()["capabilities"]
    }
    steps = [step for step in fixture["steps"] if step["kind"] == "capability"]
    assert [step["name"] for step in steps] == [
        "analyze_media",
        "timeline_insert_clip",
        "render_preview",
        "get_timeline",
        "timeline_trim_clip",
        "project_export",
        "verify_delivery",
    ]
    for step in steps:
        assert manifest[step["name"]]["surface"] == "product"
        assert manifest[step["name"]]["effect"] == step["effect"]


def test_headless_http_project_edit_snapshot_and_recovery_loop(
    manager: SessionManager,
    monkeypatch,
) -> None:
    monkeypatch.setattr(v3_routes, "get_manager", lambda: manager)
    monkeypatch.setattr(api_v1_routes, "get_manager", lambda: manager)

    def call(method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {"Content-Type": "application/json"} if body is not None else None
        response = run_server_handler(
            server._Handler,
            create_raw_request(method, path, headers, body),
        )
        return response["status"], json.loads(response["body"] or b"{}")

    status, project = call("POST", "/api/v1/projects", {"name": "API Loop"})
    assert status == 201
    status, session = call(
        "POST",
        "/api/v1/sessions",
        {"project_id": project["project_id"]},
    )
    assert status == 201
    session_id = session["session_id"]

    status, snapshot = call(
        "GET", f"/api/internal/v1/sessions/{session_id}/snapshot"
    )
    assert status == 200
    status, edit = call(
        "POST",
        (
            f"/api/internal/v1/sessions/{session_id}"
            "/capabilities/timeline_insert_clip:invoke"
        ),
        {
            "arguments": {"text": {"content": "Headless Lumeri"}},
            "idempotency_key": "headless-title-v1",
            "expected_project_revision": snapshot["project_revision"],
        },
    )
    assert status == 200
    assert edit["status"] == "completed"
    assert edit["project_revision"] > snapshot["project_revision"]

    status, timeline = call("GET", f"/api/v1/sessions/{session_id}/timeline")
    assert status == 200
    assert any(
        clip.get("text_config", {}).get("content") == "Headless Lumeri"
        for track in timeline["tracks"]
        for clip in track["clips"]
    )

    status, recovered = call(
        "GET", f"/api/internal/v1/sessions/{session_id}/events?after=0"
    )
    assert status == 200
    correlated = [
        event
        for event in recovered["events"]
        if event["request_id"] == edit["request_id"]
    ]
    assert [event["kind"] for event in correlated] == [
        "tool_exec_start",
        "tool_exec_result",
    ]


def test_agent_http_and_mcp_share_one_registered_dispatcher_seam(
    manager: SessionManager,
) -> None:
    runner = manager.create_session()
    original = runner.agent._tool_ctx.extra["execute_registered_capability"]
    calls: list[str] = []

    async def observed(name, arguments, context):
        calls.append(name)
        return await original(name, arguments, context)

    runner.agent._tool_ctx.extra["execute_registered_capability"] = observed
    agent_future = asyncio.run_coroutine_threadsafe(
        observed("get_timeline", {}, runner.agent._tool_ctx),
        runner._loop,
    )
    agent_result = agent_future.result(timeout=10)
    http_result = runner.execute_capability(
        "get_timeline",
        {},
        origin="internal_http",
        require_mutation_tokens=True,
    )
    mcp_result = runner.run_verb("get_timeline", {})

    assert calls == ["get_timeline", "get_timeline", "get_timeline"]
    assert http_result["timeline"] == agent_result["timeline"]
    assert mcp_result["timeline"] == agent_result["timeline"]


def test_http_and_mcp_share_plan_and_budget_gate_codes(
    manager: SessionManager,
    monkeypatch,
) -> None:
    monkeypatch.setattr(api_v1_routes, "get_manager", lambda: manager)
    runner = manager.create_session()
    runner.set_plan_mode(True)
    revision = runner.project_revision
    invoke_path = (
        f"/api/internal/v1/sessions/{runner.session_id}"
        "/capabilities/timeline_insert_clip:invoke"
    )
    http_plan = Handler(
        invoke_path,
        payload={
            "arguments": {"text": {"content": "blocked"}},
            "idempotency_key": "plan-block",
            "expected_project_revision": revision,
        },
    )
    api_v1_routes.try_handle(http_plan, method="POST")
    with pytest.raises(VerbGateError) as mcp_plan:
        runner.run_verb(
            "timeline_insert_clip",
            {"text": {"content": "blocked"}},
        )
    assert http_plan.json()["error"]["code"] == mcp_plan.value.code == "E_PLAN_MODE"

    runner.set_plan_mode(False)
    runner.agent.budget.max_usd = 0.0
    http_budget = Handler(
        (
            f"/api/internal/v1/sessions/{runner.session_id}"
            "/capabilities/analyze_media:invoke"
        ),
        payload={"arguments": {"asset_id": "missing"}},
    )
    api_v1_routes.try_handle(http_budget, method="POST")
    with pytest.raises(VerbGateError) as mcp_budget:
        runner.run_verb("analyze_media", {"asset_id": "missing"})
    assert http_budget.json()["error"]["code"] == mcp_budget.value.code == "E_BUDGET"


def test_http_and_mcp_share_schema_validation_errors(
    manager: SessionManager,
    monkeypatch,
) -> None:
    from gemia.errors import ToolError

    monkeypatch.setattr(api_v1_routes, "get_manager", lambda: manager)
    runner = manager.create_session()
    http_invalid = Handler(
        (
            f"/api/internal/v1/sessions/{runner.session_id}"
            "/capabilities/probe_media:invoke"
        ),
        payload={"arguments": {}},
    )
    api_v1_routes.try_handle(http_invalid, method="POST")
    with pytest.raises(ToolError) as mcp_invalid:
        runner.run_verb("probe_media", {})
    assert (
        http_invalid.json()["error"]["code"]
        == mcp_invalid.value.code
        == "E_BAD_ARG"
    )
    assert http_invalid.json()["error"]["recovery"] == "fix_args"


def test_concurrent_duplicate_write_executes_once_and_replays(
    manager: SessionManager,
) -> None:
    runner = manager.create_session()
    original = runner.agent._tool_ctx.extra["execute_registered_capability"]

    async def slow(name, arguments, context):
        if name == "timeline_insert_clip":
            await asyncio.sleep(0.1)
        return await original(name, arguments, context)

    runner.agent._tool_ctx.extra["execute_registered_capability"] = slow
    revision = runner.project_revision

    def invoke():
        return runner.execute_capability(
            "timeline_insert_clip",
            {"text": {"content": "Once"}},
            origin="internal_http",
            idempotency_key="concurrent-once",
            expected_project_revision=revision,
            require_mutation_tokens=True,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(invoke) for _ in range(2)]
        outcomes = []
        for future in futures:
            try:
                outcomes.append(("ok", future.result()))
            except VerbGateError as exc:
                outcomes.append((exc.code, exc.payload))

    assert sorted(kind for kind, _ in outcomes) == ["E_BUSY", "ok"]
    completed = next(payload for kind, payload in outcomes if kind == "ok")
    replay = invoke()
    assert replay["clip_id"] == completed["clip_id"]
    state = runner.agent.project.load()
    assert len(state["timeline"]["clips"]) == 1


def test_idempotency_receipt_survives_runner_restart(
    manager: SessionManager,
) -> None:
    runner = manager.create_session()
    session_id = runner.session_id
    revision = runner.project_revision
    arguments = {"text": {"content": "Persistent once"}}
    first = runner.execute_capability(
        "timeline_insert_clip",
        arguments,
        origin="internal_http",
        idempotency_key="persistent-once",
        expected_project_revision=revision,
        require_mutation_tokens=True,
    )
    manager.close_session(session_id)
    resumed = manager.resume_session(session_id)
    replay = resumed.execute_capability(
        "timeline_insert_clip",
        arguments,
        origin="internal_http",
        idempotency_key="persistent-once",
        expected_project_revision=revision,
        require_mutation_tokens=True,
    )
    assert replay["clip_id"] == first["clip_id"]
    assert len(resumed.agent.project.load()["timeline"]["clips"]) == 1
