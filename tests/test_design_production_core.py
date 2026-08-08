from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from gemia.device_capabilities import (
    DeviceCapabilityPermissionError,
    resolve_device_capabilities,
)
from gemia.production_evidence import stage_evidence_gaps
from gemia.production_store import ProductionStore, ProductionValidationError
from gemia.project_store import ProjectHandle
from gemia.reality_contract import (
    contract_gaps,
    default_reality_contract,
    media_policy_decision,
    normalize_reality_contract,
)
from gemia.tools import files
from gemia.tools._context import AssetRegistry, ToolContext


def _bound_contract() -> dict:
    return normalize_reality_contract(
        {
            "brief": "A real 42-second product story",
            "deliverable": {"duration_sec": 42},
        }
    )


def test_contract_is_unbound_until_a_real_brief_supplies_duration() -> None:
    neutral = default_reality_contract()
    assert contract_gaps(neutral) == [
        "reality_contract.brief",
        "reality_contract.deliverable.duration_sec",
    ]
    assert contract_gaps(_bound_contract()) == []


def test_generation_is_a_recorded_exception_not_the_default() -> None:
    contract = _bound_contract()
    snapshot = {"tool_reserved_calls": {}}
    blocked = media_policy_decision(contract, {}, snapshot, "generate_video")
    assert blocked["allowed"] is False

    video_ir = {
        "asset_strategy": {
            "generated_video": {
                "blocker": "No owned, licensed, or local source can express this one shot"
            }
        }
    }
    assert media_policy_decision(
        contract, video_ir, snapshot, "generate_video"
    )["allowed"] is True

    wrong_image_ir = {
        "asset_strategy": {
            "generated_image": {
                "blocker": "Need another background",
                "blocker_type": "convenience",
            }
        }
    }
    assert media_policy_decision(
        contract, wrong_image_ir, snapshot, "generate_image"
    )["allowed"] is False
    continuity_ir = {
        "asset_strategy": {
            "generated_image": {
                "blocker": "Existing sources break the established character identity",
                "blocker_type": "character_continuity",
            }
        }
    }
    assert media_policy_decision(
        contract, continuity_ir, snapshot, "generate_image"
    )["allowed"] is True


def test_project_design_program_write_commits_revision_and_survives_context(
    tmp_path: Path,
) -> None:
    production = ProductionStore(tmp_path / "v3")
    project_id, run_id = "project-design", "run-design"
    production.create_project(project_id)
    production.create_run(project_id, run_id, reality_contract=_bound_contract())
    project = ProjectHandle.open(
        production.projects_root,
        project_id,
        session_id="session-one",
    )
    context = ToolContext(
        session_id="session-one",
        output_dir=tmp_path / "session-one",
        registry=AssetRegistry(),
        emit_progress=lambda _update: None,
        project=project,
        extra={
            "production_store": production,
            "project_id": project_id,
            "run_id": run_id,
        },
    )
    context.output_dir.mkdir(parents=True)

    first = asyncio.run(
        files.dispatch_write_file(
            {"path": "project://design/main.py", "content": "print('v1')\n"},
            context,
        )
    )
    first_revision = first["project_revision_commit"]["project_revision"]
    assert first_revision > 0
    assert production.load_run(project_id, run_id)["project_revision"] == first_revision

    reopened = ProjectHandle.open(
        production.projects_root,
        project_id,
        session_id="session-two",
    )
    second_context = ToolContext(
        session_id="session-two",
        output_dir=tmp_path / "session-two",
        registry=AssetRegistry(),
        emit_progress=lambda _update: None,
        project=reopened,
        extra={
            "production_store": production,
            "project_id": project_id,
            "run_id": run_id,
        },
    )
    read_back = asyncio.run(
        files.dispatch_read_file({"path": "project://design/main.py"}, second_context)
    )
    assert read_back["text"] == "print('v1')\n"


def test_revision_scope_cannot_be_cleared_before_real_project_change(
    tmp_path: Path,
) -> None:
    store = ProductionStore(tmp_path)
    project_id, run_id = "project-revise", "run-revise"
    store.create_project(project_id)
    store.create_run(project_id, run_id, reality_contract=_bound_contract())
    project = store.load_project(project_id)
    scope = {
        "request": "tighten 10-12 seconds",
        "base_timeline_patch_seq": int(project.get("timeline_patch_seq") or 0),
        "base_design_program_hash": str(project.get("design_program_hash") or ""),
    }
    store.patch_design_state(
        project_id,
        run_id,
        document="creative_ir",
        operation="set",
        path="/active_revision_scope",
        value=scope,
        expected_revision=0,
    )
    with pytest.raises(ProductionValidationError, match="only after"):
        store.patch_design_state(
            project_id,
            run_id,
            document="creative_ir",
            operation="set",
            path="/active_revision_scope",
            value=None,
            expected_revision=1,
        )

    design_root = store.project_dir(project_id) / "design"
    design_root.mkdir(parents=True, exist_ok=True)
    (design_root / "main.py").write_text("print('revised')\n", encoding="utf-8")
    store.observe_design_program(project_id, run_id)
    cleared = store.patch_design_state(
        project_id,
        run_id,
        document="creative_ir",
        operation="set",
        path="/active_revision_scope",
        value=None,
        expected_revision=1,
    )
    assert cleared["value"]["active_revision_scope"] is None


def test_runtime_toolchains_and_renders_do_not_advance_design_revision(
    tmp_path: Path,
) -> None:
    store = ProductionStore(tmp_path)
    project_id, run_id = "project-digest", "run-digest"
    store.create_project(project_id)
    store.create_run(project_id, run_id, reality_contract=_bound_contract())
    design_root = store.project_dir(project_id) / "design"
    design_root.mkdir(parents=True, exist_ok=True)
    (design_root / "main.py").write_text("print('source')\n", encoding="utf-8")
    first = store.observe_design_program(project_id, run_id)
    assert first["changed"] is True

    tool_file = design_root / "toolchain" / "venv" / "cache.bin"
    tool_file.parent.mkdir(parents=True)
    tool_file.write_bytes(b"runtime-v1")
    render_file = design_root / "renders" / "preview.mp4"
    render_file.parent.mkdir(parents=True)
    render_file.write_bytes(b"render-v1")
    unchanged = store.observe_design_program(project_id, run_id)
    assert unchanged["changed"] is False
    assert unchanged["project_revision"] == first["project_revision"]

    tool_file.write_bytes(b"runtime-v2")
    render_file.write_bytes(b"render-v2")
    assert store.observe_design_program(project_id, run_id)["changed"] is False


def test_legacy_design_digest_migrates_without_advancing_revision(
    tmp_path: Path,
) -> None:
    store = ProductionStore(tmp_path)
    project_id, run_id = "project-legacy-digest", "run-legacy-digest"
    store.create_project(project_id)
    store.create_run(project_id, run_id, reality_contract=_bound_contract())
    design_root = store.project_dir(project_id) / "design"
    (design_root / "main.py").write_text("print('source')\n", encoding="utf-8")

    project = store.load_project(project_id)
    project["revision"] = 7
    project["design_program_hash"] = "a" * 64
    project.pop("design_program_digest_scope", None)
    project.pop("design_program_source_hash", None)
    store._write_json(store.project_record_path(project_id), project)

    migrated = store.observe_design_program(project_id, run_id)
    assert migrated["changed"] is False
    assert migrated["digest_scope_migrated"] is True
    assert migrated["project_revision"] == 7
    reopened = store.load_project(project_id)
    assert reopened["revision"] == 7
    assert reopened["design_program_hash"] == "a" * 64
    assert reopened["design_program_source_hash"]

    assert store.observe_design_program(project_id, run_id)["changed"] is False
    (design_root / "main.py").write_text("print('changed')\n", encoding="utf-8")
    changed = store.observe_design_program(project_id, run_id)
    assert changed["changed"] is True
    assert changed["project_revision"] == 8


def test_stage_gates_compile_missing_facts_and_host_keeps_os_authority() -> None:
    contract = _bound_contract()
    assert stage_evidence_gaps(
        state="preflight", contract=contract, creative_ir={}, facts={"asset_count": 0}
    ) == ["creative_ir.asset_strategy_or_existing_assets"]

    accelerated = resolve_device_capabilities(
        ["compute.gpu", "media.hardware_encode"], unrestricted_host=False
    )
    assert accelerated.allow_gpu is True
    with pytest.raises(DeviceCapabilityPermissionError):
        resolve_device_capabilities(["capture.camera"], unrestricted_host=False)
    host = resolve_device_capabilities(["capture.camera"], unrestricted_host=True)
    assert host.os_managed == ("capture.camera",)
