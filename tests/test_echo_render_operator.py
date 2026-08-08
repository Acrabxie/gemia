from __future__ import annotations

from copy import deepcopy
import hashlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import pytest

from scripts import render_echo_protocol_v1 as operator

GRAPH_HASH = "a" * 64


class _Record:
    def __init__(
        self,
        asset_id: str,
        *,
        kind: str,
        path: Path,
        source: dict[str, Any],
    ) -> None:
        self.asset_id = asset_id
        self.kind = kind
        self.path = path.resolve()
        self.source = deepcopy(source)
        self.sha256 = hashlib.sha256(self.path.read_bytes()).hexdigest()


class _Registry:
    def __init__(self) -> None:
        self.records: dict[str, _Record] = {}

    def add(
        self,
        asset_id: str,
        *,
        kind: str,
        path: Path,
        source: dict[str, Any],
    ) -> None:
        self.records[asset_id] = _Record(
            asset_id,
            kind=kind,
            path=path,
            source=source,
        )

    def get(self, asset_id: str) -> _Record:
        try:
            return self.records[asset_id]
        except KeyError:
            raise KeyError(asset_id) from None


class _Manager:
    def __init__(self) -> None:
        self.state = "rendering"
        self.project_revision = 7
        self.production_revision = 30
        self.budget = {
            "limit_usd": 15.0,
            "spent_usd": 1.525,
            "reserved_usd": 0.0,
            "remaining_usd": 13.475,
            "duplicate_billing_count": 0,
            "veo_reserved_calls": 0,
            "veo_reserved_duration_sec": 0.0,
        }
        self.evidence: dict[str, dict[str, Any]] = {}
        self.evidence_calls: list[dict[str, Any]] = []
        self.transitions: list[dict[str, Any]] = []

    def get_run(self, project_id: str, run_id: str) -> dict[str, Any]:
        assert project_id == "project-echo"
        assert run_id == operator.RUN_ID
        return {
            "state": self.state,
            "production_state": self.state,
            "revision": self.production_revision,
            "production_revision": self.production_revision,
            "project_revision": self.project_revision,
            "budget": deepcopy(self.budget),
            "evidence_ids": list(self.evidence),
        }

    def get_project(self, project_id: str) -> dict[str, Any]:
        assert project_id == "project-echo"
        return {
            "project_id": project_id,
            "revision": self.project_revision,
            "project_revision": self.project_revision,
        }

    def record_evidence(self, project_id: str, run_id: str, **kwargs: Any):
        assert project_id == "project-echo"
        assert run_id == operator.RUN_ID
        evidence_id = kwargs["evidence_id"]
        canonical = deepcopy(kwargs)
        if evidence_id in self.evidence:
            assert self.evidence[evidence_id] == canonical
            return {"evidence_id": evidence_id, "duplicate": True}
        self.evidence[evidence_id] = canonical
        self.evidence_calls.append(canonical)
        self.production_revision += 1
        return {"evidence_id": evidence_id, "duplicate": False}

    def transition_run(
        self,
        project_id: str,
        run_id: str,
        state: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        assert project_id == "project-echo"
        assert run_id == operator.RUN_ID
        assert kwargs["expected_revision"] == self.production_revision
        if state == "verifying":
            assert self.state == "rendering"
        elif state == "rendering":
            assert self.state == "revising"
        else:
            raise AssertionError(f"unexpected test transition: {state}")
        self.transitions.append({"state": state, **deepcopy(kwargs)})
        self.state = state
        self.production_revision += 1
        return self.get_run(project_id, run_id)


class _Runner:
    def __init__(
        self,
        manager: _Manager,
        registry: _Registry,
        results: dict[str, dict[str, Any]],
        *,
        on_dispatch: Callable[[str], None] | None = None,
    ) -> None:
        self.project_id = "project-echo"
        self.run_id = operator.RUN_ID
        self.registry = registry
        self.agent = SimpleNamespace(registry=registry)
        self.manager = manager
        self.results = deepcopy(results)
        self.on_dispatch = on_dispatch
        self.receipts: dict[str, dict[str, Any]] = {}
        self.calls: list[dict[str, Any]] = []
        self.dispatches: list[str] = []

    def run_production_verb(
        self,
        tool_name: str,
        args: dict[str, Any],
        **kwargs: Any,
    ) -> dict[str, Any]:
        key = kwargs["idempotency_key"]
        call = {
            "tool_name": tool_name,
            "args": deepcopy(args),
            **deepcopy(kwargs),
        }
        self.calls.append(call)
        if key in self.receipts:
            return {
                **deepcopy(self.receipts[key]),
                "production_duplicate": True,
            }
        self.dispatches.append(tool_name)
        if self.on_dispatch is not None:
            self.on_dispatch(tool_name)
        tool_call_id = f"tool-{hashlib.sha256(key.encode()).hexdigest()[:16]}"
        result = {
            **deepcopy(self.results[tool_name]),
            "production_duplicate": False,
            "production_tool_call_id": tool_call_id,
            "production_status": "succeeded",
        }
        self.receipts[key] = deepcopy(result)
        return result


def _safe_root(tmp_path: Path) -> Path:
    token = hashlib.sha256(str(tmp_path).encode()).hexdigest()[:16]
    root = Path.cwd() / ".pytest_cache" / "echo_render_operator" / token
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve()


def _write(path: Path, value: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value)
    return path.resolve()


def _receipt(
    path: Path,
    *,
    render_id: str,
    graph_hash: str = GRAPH_HASH,
    machine_status: str = "passed",
    machine_blockers: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "project_id": "project-echo",
        "project_revision": 7,
        "render_id": render_id,
        "graph_hash": graph_hash,
        "output_path": str(path),
        "output_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "dropped_fields": [],
        "machine_status": machine_status,
        "machine_blockers": list(machine_blockers or []),
    }


def _case(tmp_path: Path) -> tuple[_Manager, _Runner]:
    root = _safe_root(tmp_path)
    manager = _Manager()
    registry = _Registry()

    preview_path = _write(root / "preview.mp4", b"preview")
    preview_receipt = _receipt(
        preview_path,
        render_id="preview-render",
        machine_status="provisional",
    )
    registry.add(
        "preview-1",
        kind="video",
        path=preview_path,
        source={
            "kind": "derived_inspection_preview",
            "project_revision": 7,
            "graph_hash": GRAPH_HASH,
            "render_receipt": preview_receipt,
        },
    )
    frame_ids: list[str] = []
    for index in range(12):
        asset_id = f"frame-{index + 1:02d}"
        frame_ids.append(asset_id)
        registry.add(
            asset_id,
            kind="image",
            path=_write(root / f"{asset_id}.png", asset_id.encode()),
            source={
                "kind": "derived_inspection_frame",
                "project_revision": 7,
                "graph_hash": GRAPH_HASH,
            },
        )
    registry.add(
        "contact-1",
        kind="image",
        path=_write(root / "contact.png", b"contact"),
        source={
            "kind": "derived_inspection_contact_sheet",
            "project_revision": 7,
            "graph_hash": GRAPH_HASH,
        },
    )

    export_path = _write(root / "echo-protocol-v1.mp4", b"final")
    export_receipt = _receipt(export_path, render_id="final-render")
    registry.add(
        "export-1",
        kind="video",
        path=export_path,
        source={
            "kind": "derived_export",
            "project_revision": 7,
            "graph_hash": GRAPH_HASH,
            "render_receipt": export_receipt,
        },
    )
    results = {
        "inspect_timeline": {
            "preview_asset_id": "preview-1",
            "frame_asset_ids": frame_ids,
            "contact_sheet_asset_id": "contact-1",
            "graph_hash": GRAPH_HASH,
            "render_receipt": preview_receipt,
            "dropped_fields": [],
        },
        "project_export": {
            "asset_id": "export-1",
            "export_path": str(export_path),
            "duration": 120.0,
            "width": 1920,
            "height": 1080,
            "has_audio": True,
            "graph_hash": GRAPH_HASH,
            "render_receipt": export_receipt,
            "machine_status": "passed",
            "machine_blockers": [],
            "dropped_fields": [],
        },
    }
    return manager, _Runner(manager, registry, results)


def _review() -> tuple[dict[str, bool], dict[str, str]]:
    checks = {name: True for name in operator.FORMAL_REQUIRED_REVIEW_CHECKS}
    notes = {
        name: f"Reviewed all twelve sampled frames for {name}."
        for name in operator.FORMAL_REQUIRED_REVIEW_CHECKS
    }
    return checks, notes


def test_render_orders_canonical_inspection_before_export_and_stops_verifying(
    tmp_path: Path,
) -> None:
    manager, runner = _case(tmp_path)

    result = operator.execute_render(manager, runner, attempt=2)

    assert runner.dispatches == ["inspect_timeline", "project_export"]
    assert [call["tool_name"] for call in runner.calls] == runner.dispatches
    assert runner.calls[0]["args"] == {
        "start_sec": 0.0,
        "end_sec": 120.0,
        "max_frames": 12,
        "label": "echo-v1-r7-a2-inspect",
    }
    assert runner.calls[1]["args"] == {
        "quality": "1080p",
        "label": "echo-v1-r7-a2-final",
    }
    assert all(call["timeout"] >= 1800 for call in runner.calls)
    assert runner.calls[0]["idempotency_key"] == "echo-v1-r7-a2-inspect"
    assert runner.calls[1]["idempotency_key"] == "echo-v1-r7-a2-export"
    assert result["production_state"] == "verifying"
    assert result["transitioned"] is True
    assert result["project_revision"] == 7
    assert result["graph_hash"] == GRAPH_HASH
    assert result["machine_status"] == "passed"
    assert result["duration"] == 120.0
    assert result["width"] == 1920 and result["height"] == 1080
    assert result["has_audio"] is True
    assert result["dropped_fields"] == []
    assert len(result["inspection_asset_ids"]) == 12
    assert manager.state == "verifying"
    assert len(manager.evidence_calls) == 1
    assert manager.evidence_calls[0]["project_revision"] == 7
    assert manager.evidence_calls[0]["payload"]["checks"] == {
        "inspect_before_export": True,
        "twelve_distinct_frames": True,
        "preview_export_graph_parity": True,
        "revision_bound": True,
        "machine_status_passed": True,
        "no_dropped_fields": True,
        "no_tmp_outputs": True,
        "budget_unchanged": True,
        "ai_video_generation_calls": 0,
    }
    assert [item["state"] for item in manager.transitions] == ["verifying"]
    assert manager.budget["spent_usd"] == 1.525
    assert manager.budget["veo_reserved_calls"] == 0


def test_render_crash_replay_uses_duplicate_receipts_and_no_duplicate_fact(
    tmp_path: Path,
) -> None:
    manager, runner = _case(tmp_path)
    first = operator.execute_render(manager, runner)
    evidence_count = len(manager.evidence_calls)
    transition_count = len(manager.transitions)

    replay = operator.execute_render(manager, runner)

    assert first["replayed"] is False
    assert replay["replayed"] is True
    assert replay["transitioned"] is False
    assert runner.dispatches == ["inspect_timeline", "project_export"]
    assert len(runner.calls) == 4
    assert len(manager.evidence_calls) == evidence_count == 1
    assert len(manager.transitions) == transition_count == 1
    assert manager.state == "verifying"


def test_verifying_state_without_render_receipts_fails_closed(tmp_path: Path) -> None:
    manager, runner = _case(tmp_path)
    manager.state = "verifying"

    with pytest.raises(
        operator.EchoRenderOperatorError,
        match="completed revision-bound inspection receipt",
    ):
        operator.execute_render(manager, runner)

    assert runner.dispatches == ["inspect_timeline"]
    assert manager.evidence_calls == []
    assert manager.transitions == []
    assert manager.state == "verifying"


def test_graph_mismatch_fails_without_evidence_or_transition(tmp_path: Path) -> None:
    manager, runner = _case(tmp_path)
    wrong = "b" * 64
    runner.results["project_export"]["graph_hash"] = wrong
    runner.results["project_export"]["render_receipt"]["graph_hash"] = wrong

    with pytest.raises(
        operator.EchoRenderOperatorError,
        match="preview/export canonical graph hash mismatch",
    ):
        operator.execute_render(manager, runner)

    assert runner.dispatches == ["inspect_timeline", "project_export"]
    assert manager.evidence_calls == []
    assert manager.transitions == []
    assert manager.state == "rendering"


def test_machine_failure_does_not_advance(tmp_path: Path) -> None:
    manager, runner = _case(tmp_path)
    runner.results["project_export"]["machine_status"] = "rejected"
    runner.results["project_export"]["machine_blockers"] = [{"code": "audio_missing"}]
    runner.results["project_export"]["render_receipt"]["machine_status"] = "rejected"
    runner.results["project_export"]["render_receipt"]["machine_blockers"] = [
        {"code": "audio_missing"}
    ]

    with pytest.raises(operator.EchoRenderOperatorError, match="machine delivery gate"):
        operator.execute_render(manager, runner)

    assert manager.evidence_calls == []
    assert manager.transitions == []
    assert manager.state == "rendering"


def test_budget_mutation_fails_closed_before_evidence_or_transition(
    tmp_path: Path,
) -> None:
    manager, base_runner = _case(tmp_path)

    def mutate_budget(tool_name: str) -> None:
        if tool_name == "project_export":
            manager.budget["spent_usd"] = 1.526
            manager.budget["remaining_usd"] = 13.474

    runner = _Runner(
        manager,
        base_runner.registry,
        base_runner.results,
        on_dispatch=mutate_budget,
    )
    with pytest.raises(
        operator.EchoRenderOperatorError, match="changed the media budget"
    ):
        operator.execute_render(manager, runner)

    assert manager.evidence_calls == []
    assert manager.transitions == []
    assert manager.state == "rendering"


def test_verify_uses_render_assets_and_never_accepts_human_gate(tmp_path: Path) -> None:
    manager, render_runner = _case(tmp_path)
    render_result = operator.execute_render(manager, render_runner)
    checks, notes = _review()

    def transition_ready(tool_name: str) -> None:
        if tool_name == "verify_delivery":
            assert manager.state == "verifying"
            manager.state = "ready_for_review"
            manager.production_revision += 2  # verifier evidence + state transition

    verify_runner = _Runner(
        manager,
        render_runner.registry,
        {
            "verify_delivery": {
                "production_state": "ready_for_review",
                "acceptance_report": {
                    "ready_for_review": True,
                    "human_review_required": True,
                },
                "evidence_id": "ev-production-acceptance",
                "export_asset_id": render_result["export_asset_id"],
            }
        },
        on_dispatch=transition_ready,
    )

    verified = operator.execute_verify(
        manager,
        verify_runner,
        render_result,
        review_checks=checks,
        review_notes=notes,
    )

    assert verify_runner.dispatches == ["verify_delivery"]
    call = verify_runner.calls[0]
    assert call["idempotency_key"] == "echo-v1-r7-a1-verify-s3"
    assert call["timeout"] >= 1800
    assert call["args"]["preview_asset_id"] == render_result["preview_asset_id"]
    assert call["args"]["export_asset_id"] == render_result["export_asset_id"]
    assert call["args"]["inspection_asset_ids"] == render_result["inspection_asset_ids"]
    assert call["args"]["review_checks"] == checks
    assert call["args"]["review_notes"] == notes
    assert verified["production_state"] == "ready_for_review"
    assert verified["human_review_required"] is True
    assert verified["accepted"] is False
    assert manager.state == "ready_for_review"

    replay = operator.execute_verify(
        manager,
        verify_runner,
        render_result,
        review_checks=checks,
        review_notes=notes,
    )
    assert replay["production_duplicate"] is True
    assert verify_runner.dispatches == ["verify_delivery"]
    assert manager.state == "ready_for_review"


def test_audio_tail_remediation_reopens_same_revision_without_cost(
    tmp_path: Path,
) -> None:
    manager, runner = _case(tmp_path)
    manager.state = "revising"
    manager.evidence["ev-failed-acceptance"] = {"kind": "production_acceptance"}
    export_record = runner.registry.get("export-1")
    export_record.source["render_receipt"].update(
        {
            "render_semantics_version": 2,
            "project_revision": 7,
        }
    )
    render_result = {
        "project_revision": 7,
        "export_asset_id": "export-1",
    }
    budget_before = deepcopy(manager.budget)

    result = operator.execute_prepare_audio_tail_rerender(
        manager,
        runner,
        render_result,
        attempt=3,
        probe_fn=lambda _path: {
            "container_duration": 120.0,
            "video_duration": 120.0,
            "audio_duration": 117.4,
        },
    )

    assert result["production_state"] == "rendering"
    assert result["project_revision"] == 7
    assert result["failed_audio_duration"] == 117.4
    assert result["veo_calls"] == 0
    assert manager.project_revision == 7
    assert manager.budget == budget_before
    assert manager.evidence_calls[-1]["kind"] == "renderer_remediation"
    assert (
        manager.evidence_calls[-1]["payload"]["remediation"]["project_patch"] is False
    )

    evidence_count = len(manager.evidence_calls)
    replay = operator.execute_prepare_audio_tail_rerender(
        manager,
        runner,
        render_result,
        attempt=3,
        probe_fn=lambda _path: (_ for _ in ()).throw(
            AssertionError("must not reprobe")
        ),
    )
    assert replay["replayed"] is True
    assert len(manager.evidence_calls) == evidence_count
    assert manager.state == "rendering"


def test_verify_rejects_unstructured_review_before_tool_call(tmp_path: Path) -> None:
    manager, runner = _case(tmp_path)
    render_result = operator.execute_render(manager, runner)
    checks, notes = _review()
    notes["watermarks"] = ""

    with pytest.raises(operator.EchoRenderOperatorError, match="watermarks"):
        operator.execute_verify(
            manager,
            runner,
            render_result,
            review_checks=checks,
            review_notes=notes,
        )

    assert runner.dispatches == ["inspect_timeline", "project_export"]


def test_timeout_below_production_floor_is_rejected_before_render(
    tmp_path: Path,
) -> None:
    manager, runner = _case(tmp_path)

    with pytest.raises(operator.EchoRenderOperatorError, match="at least 1800s"):
        operator.execute_render(manager, runner, timeout_sec=1799)

    assert runner.calls == []
    assert manager.evidence_calls == []
    assert manager.transitions == []
