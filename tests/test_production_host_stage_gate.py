from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path

import pytest

from gemia import session_manager
from gemia.budget_guard import BudgetGuard
from gemia.project_store import ProjectHandle
from gemia.session_manager import SessionManager, VerbGateError
from gemia.tool_capability_registry import ToolCapability, ToolCapabilityRegistry
from gemia.tools._context import AssetRegistry, ToolContext


_HOST_TOOLS = (
    "probe_media",
    "stock_media",
    "get_shotlist",
    "timeline_insert_clip",
    "mix_audio",
    "vector_motion",
    "render_preview",
    "verify_delivery",
)


def _schema(name: str) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": f"test capability {name}",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    }


class _ProductionHostLoopDouble:
    """Minimal agent with real registry, project, budget and host dispatch."""

    def __init__(self, **kwargs) -> None:
        self.session_id = kwargs["session_id"]
        self.registry = kwargs.get("asset_registry") or AssetRegistry()
        self.project = ProjectHandle.open(
            kwargs["project_root"],
            kwargs["project_id"],
            session_id=self.session_id,
        )
        self._emit = kwargs["emit_event"]
        self._messages = list((kwargs.get("runtime_state") or {}).get("messages") or [])
        self.plan_mode = False
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
        self.dispatches: list[tuple[str, dict]] = []
        self.block_turn = False
        self.turn_started = threading.Event()
        self.release_turn = threading.Event()

        capabilities = []
        for tool_name in _HOST_TOOLS:

            async def dispatch(args, _ctx, *, name=tool_name):
                self.dispatches.append((name, dict(args)))
                return {"ok": True, "tool_name": name, "args": dict(args)}

            capabilities.append(
                ToolCapability(
                    name=tool_name,
                    schema=_schema(tool_name),
                    dispatcher=dispatch,
                    workflows=(),
                    plan_mode="blocked",
                    estimated_usd=0.0,
                    estimated_eta_sec=0.0,
                    paid_media=False,
                    uses_default_cost=True,
                    effect=(
                        "read"
                        if tool_name in {"probe_media", "get_shotlist"}
                        else "write"
                    ),
                    execution=(
                        "job"
                        if tool_name in {
                            "stock_media",
                            "vector_motion",
                            "render_preview",
                            "verify_delivery",
                        }
                        else "sync"
                    ),
                    surface="product",
                    exposed_via=("agent", "http", "mcp"),
                    requires_idempotency_key=tool_name not in {
                        "probe_media",
                        "get_shotlist",
                    },
                    requires_project_revision=tool_name not in {
                        "probe_media",
                        "get_shotlist",
                    },
                )
            )
        self.capabilities = ToolCapabilityRegistry(capabilities)

    async def run_turn(self, message: str) -> None:
        self._messages.append({"role": "user", "content": message})
        if self.block_turn:
            self.turn_started.set()
            while not self.release_turn.is_set():
                await asyncio.sleep(0.01)
        self._emit({"kind": "turn_complete", "outcome": "progressed"})

    def snapshot_runtime_state(self) -> dict:
        return {
            "messages": list(self._messages),
            "turn_count": 0,
            "plan_mode": self.plan_mode,
            "budget": self.budget.snapshot(),
        }

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


@pytest.fixture
def production_runner(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(session_manager, "AgentLoopV3", _ProductionHostLoopDouble)
    manager = SessionManager(
        output_root=tmp_path,
        max_sessions=2,
        idle_timeout_sec=0,
        sweep_interval_sec=0,
    )
    runner = manager.create_session()
    try:
        yield manager, runner
    finally:
        runner.agent.release_turn.set()
        deadline = time.time() + 2.0
        while runner.turn_in_progress and time.time() < deadline:
            time.sleep(0.01)
        manager.close_all()


def _receipt_files(manager: SessionManager, runner) -> list[Path]:
    return list(
        manager.production_store.tool_calls_dir(
            runner.project_id, runner.run_id
        ).glob("*.json")
    )


def _transition(manager: SessionManager, runner, state: str) -> None:
    manager.production_store.transition_run(runner.project_id, runner.run_id, state)


def _advance_to_accepted(
    manager: SessionManager, runner, monkeypatch: pytest.MonkeyPatch
) -> None:
    for state in (
        "preflight",
        "sourcing",
        "rough_cut",
        "sound_pass",
        "visual_pass",
        "rendering",
        "verifying",
    ):
        _transition(manager, runner, state)
    # This test exercises terminal-state host gating, not delivery evaluation;
    # the ProductionStore acceptance contract has its own adversarial tests.
    monkeypatch.setattr(
        manager.production_store,
        "_has_current_machine_evidence",
        lambda *_args, **_kwargs: True,
    )
    _transition(manager, runner, "ready_for_review")
    monkeypatch.setattr(
        manager.production_store,
        "_validate_delivery_candidate",
        lambda *_args, **_kwargs: None,
    )
    run = manager.production_store.load_run(runner.project_id, runner.run_id)
    run["deliverables"] = [{"role": "review_master"}]
    manager.production_store._write_json(  # noqa: SLF001 - terminal-state fixture
        manager.production_store.run_path(runner.project_id, runner.run_id),
        run,
    )
    manager.production_store.review_run(
        runner.project_id,
        runner.run_id,
        action="approve",
        watched_full_video=True,
        creative_checks={
            "story": True,
            "pacing": True,
            "visual": True,
            "sound": True,
            "publishable": True,
        },
    )


def test_active_agent_turn_is_rejected_before_receipt_claim(production_runner) -> None:
    manager, runner = production_runner
    runner.agent.block_turn = True
    submitted = runner.submit_turn_request("keep the agent busy", client_turn_id="busy")
    assert submitted["scheduled"] is True
    assert runner.agent.turn_started.wait(1.0)

    with pytest.raises(VerbGateError) as exc_info:
        runner.run_production_verb(
            "probe_media",
            {"asset_id": "image_001"},
            trace_id="trace-busy",
            idempotency_key="busy-probe",
        )

    assert exc_info.value.code == "E_BUSY"
    assert exc_info.value.payload["error_code"] == "E_BUSY"
    assert _receipt_files(manager, runner) == []


def test_wrong_stage_fails_closed_before_receipt_claim(production_runner) -> None:
    manager, runner = production_runner
    _transition(manager, runner, "preflight")

    with pytest.raises(VerbGateError) as exc_info:
        runner.run_production_verb(
            "mix_audio",
            {},
            trace_id="trace-wrong-stage",
            idempotency_key="wrong-stage-mix",
        )

    payload = exc_info.value.payload
    assert exc_info.value.code == "E_PRODUCTION_STATE"
    assert payload["reason"] == "tool_not_allowed"
    assert payload["production_state"] == "preflight"
    assert "probe_media" in payload["allowed_tools"]
    assert "stock_media" in payload["allowed_tools"]
    assert "mix_audio" not in payload["allowed_tools"]
    assert _receipt_files(manager, runner) == []
    assert runner.agent.dispatches == []


@pytest.mark.parametrize("terminal_state", ["accepted", "cancelled", "failed"])
def test_terminal_state_forbids_new_formal_calls(
    production_runner, terminal_state: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, runner = production_runner
    if terminal_state == "accepted":
        _advance_to_accepted(manager, runner, monkeypatch)
    else:
        _transition(manager, runner, terminal_state)

    with pytest.raises(VerbGateError) as exc_info:
        runner.run_production_verb(
            "probe_media",
            {},
            trace_id=f"trace-{terminal_state}",
            idempotency_key=f"terminal-{terminal_state}",
        )

    payload = exc_info.value.payload
    assert exc_info.value.code == "E_PRODUCTION_STATE"
    assert payload["reason"] == "terminal_state"
    assert payload["production_state"] == terminal_state
    assert _receipt_files(manager, runner) == []
    assert runner.agent.dispatches == []


def test_each_active_stage_keeps_its_formal_capability_lane(production_runner) -> None:
    manager, runner = production_runner
    stage_calls = (
        ("preflight", ("probe_media", "stock_media")),
        ("sourcing", ("probe_media", "stock_media")),
        ("rough_cut", ("get_shotlist", "timeline_insert_clip")),
        ("sound_pass", ("mix_audio",)),
        ("visual_pass", ("vector_motion", "timeline_insert_clip")),
        ("rendering", ("render_preview", "verify_delivery")),
        ("verifying", ("verify_delivery",)),
    )

    expected_dispatches = []
    call_index = 0
    for state, tool_names in stage_calls:
        _transition(manager, runner, state)
        for tool_name in tool_names:
            result = runner.run_production_verb(
                tool_name,
                {"index": call_index},
                trace_id=f"trace-{state}-{call_index}",
                idempotency_key=f"stage-{state}-{call_index}",
            )
            assert result["ok"] is True
            assert result["tool_name"] == tool_name
            assert result["production_duplicate"] is False
            expected_dispatches.append(tool_name)
            call_index += 1

    assert [name for name, _args in runner.agent.dispatches] == expected_dispatches


def test_successful_formal_call_duplicate_replays_without_dispatch(
    production_runner,
) -> None:
    _manager, runner = production_runner
    _transition(_manager, runner, "preflight")
    kwargs = {
        "trace_id": "trace-replay",
        "idempotency_key": "replay-probe",
    }

    first = runner.run_production_verb("probe_media", {"asset_id": "image_001"}, **kwargs)
    replay = runner.run_production_verb("probe_media", {"asset_id": "image_001"}, **kwargs)

    assert first["production_duplicate"] is False
    assert replay["production_duplicate"] is True
    assert replay["production_tool_call_id"] == first["production_tool_call_id"]
    assert runner.agent.dispatches == [
        ("probe_media", {"asset_id": "image_001"})
    ]
