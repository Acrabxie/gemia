from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import pytest

from gemia.errors import ToolError
from gemia.production_store import ProductionStore
from gemia.project_store import ProjectHandle
from gemia.render_receipt import (
    CANONICAL_RENDER_SEMANTICS_VERSION,
    canonical_render_semantics,
)
from gemia.tool_capability_registry import build_default_registry
from gemia.tools import DISPATCHER
from gemia.tools._context import AssetRegistry, ToolContext


def _advance_to_verifying(store: ProductionStore, project_id: str, run_id: str) -> None:
    for state in (
        "preflight",
        "sourcing",
        "rough_cut",
        "sound_pass",
        "visual_pass",
        "rendering",
        "verifying",
    ):
        store.transition_run(project_id, run_id, state)


def _render_receipt(
    path: Path,
    *,
    project_id: str,
    project_revision: int,
    render_id: str,
    graph_hash: str,
    machine_status: str,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "project_id": project_id,
        "project_revision": project_revision,
        "render_id": render_id,
        "graph_hash": graph_hash,
        "render_semantics_version": CANONICAL_RENDER_SEMANTICS_VERSION,
        "render_semantics": canonical_render_semantics(),
        "output_path": str(path.resolve()),
        "output_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "machine_status": machine_status,
        "machine_blockers": [],
    }


def test_formal_capability_registry_owns_delivery_verifier() -> None:
    registry = build_default_registry()
    assert "verify_delivery" in registry.names
    assert registry.dispatcher("verify_delivery") is DISPATCHER["verify_delivery"]
    assert any(
        row["name"] == "verify_delivery" and row["paid_media"] is False
        for row in registry.help_rows()
    )


def test_formal_timeline_rejects_unrendered_effect_before_patch(tmp_path: Path) -> None:
    handle = ProjectHandle.open(
        tmp_path / "project", "project-formal", session_id="session-formal"
    )
    ctx = ToolContext(
        session_id="session-formal",
        output_dir=tmp_path,
        registry=AssetRegistry(),
        emit_progress=lambda _update: None,
        project=handle,
        extra={
            "production_store": object(),
            "project_id": "project-formal",
            "run_id": "run-formal",
        },
    )
    inserted = asyncio.run(
        DISPATCHER["timeline_insert_clip"](
            {"text": {"content": "Acrab"}, "duration": 2.0}, ctx
        )
    )
    before = handle.store.load_meta(handle.project_id)["patch_seq"]
    with pytest.raises(ToolError) as exc:
        asyncio.run(
            DISPATCHER["timeline_set_clip_effects"](
                {"clip_id": inserted["clip_id"], "effects": {"scale": 2.0}},
                ctx,
            )
        )
    assert exc.value.code == "E_RENDER_UNSUPPORTED"
    assert handle.store.load_meta(handle.project_id)["patch_seq"] == before


def _delivery_context(tmp_path: Path):
    project_id, run_id = "project-delivery", "run-delivery"
    store = ProductionStore(tmp_path / "durable")
    store.create_project(project_id)
    store.observe_project_state(project_id, state_hash="state-1", timeline_patch_seq=0)
    store.create_run(project_id, run_id)
    _advance_to_verifying(store, project_id, run_id)
    revision = int(store.load_project(project_id)["revision"])
    handle = ProjectHandle.open(
        tmp_path / "timeline", project_id, session_id="session-delivery"
    )
    registry = AssetRegistry()

    graph_hash = "a" * 64
    export_path = tmp_path / "export.mp4"
    preview_path = tmp_path / "preview.mp4"
    export_path.write_bytes(b"export")
    preview_path.write_bytes(b"preview")
    receipt = _render_receipt(
        export_path,
        project_id=project_id,
        project_revision=revision,
        render_id="render-current",
        graph_hash=graph_hash,
        machine_status="passed",
    )
    preview_receipt = _render_receipt(
        preview_path,
        project_id=project_id,
        project_revision=revision,
        render_id="preview-current",
        graph_hash=graph_hash,
        machine_status="provisional",
    )
    export_id = registry.allocate_id("video")
    preview_id = registry.allocate_id("video")
    registry.register_output(
        export_id,
        kind="video",
        path=export_path,
        summary="export",
        source={"kind": "derived_export", "render_receipt": receipt},
    )
    registry.register_output(
        preview_id,
        kind="video",
        path=preview_path,
        summary="preview",
        source={"kind": "derived_preview", "render_receipt": preview_receipt},
    )
    frame_ids: list[str] = []
    for index in range(12):
        path = tmp_path / f"frame-{index}.png"
        path.write_bytes(f"frame-{index}".encode())
        asset_id = registry.allocate_id("image")
        registry.register_output(
            asset_id,
            kind="image",
            path=path,
            summary=f"frame {index}",
            source={
                "kind": "derived_inspection_frame",
                "graph_hash": graph_hash,
                "project_revision": revision,
            },
        )
        frame_ids.append(asset_id)
    registry.save(store.asset_registry_path(project_id))

    transitions: list[str] = []

    def transition(state: str, *, trace_id: str | None = None):
        transitions.append(state)
        return store.transition_run(project_id, run_id, state, trace_id=trace_id)

    ctx = ToolContext(
        session_id="session-delivery",
        output_dir=tmp_path,
        registry=registry,
        emit_progress=lambda _update: None,
        project=handle,
        extra={
            "production_store": store,
            "project_id": project_id,
            "run_id": run_id,
            "transition_production": transition,
            "active_trace_id": "turn-1",
        },
    )
    args = {
        "export_asset_id": export_id,
        "preview_asset_id": preview_id,
        "inspection_asset_ids": frame_ids,
        "review_checks": {
            "black_frames": True,
            "watermarks": True,
            "text_integrity": True,
            "character_continuity": True,
            "real_motion": True,
        },
        "review_notes": {
            name: f"observed {name} across the current contact sheet"
            for name in (
                "black_frames",
                "watermarks",
                "text_integrity",
                "character_continuity",
                "real_motion",
            )
        },
    }
    return store, project_id, run_id, ctx, args, transitions, receipt


def test_verify_delivery_records_current_evidence_and_moves_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import gemia.tools.verify_delivery as verifier

    store, project_id, run_id, ctx, args, transitions, receipt = _delivery_context(
        tmp_path
    )
    report = {
        "schema": "lumeri.production-acceptance",
        "version": 1,
        "project_revision": receipt["project_revision"],
        "render_id": receipt["render_id"],
        "graph_hash": receipt["graph_hash"],
        "ready_for_review": True,
        "checks": [{"code": "formal", "ok": True}],
        "blockers": [],
        "human_review_required": True,
    }
    monkeypatch.setattr(verifier, "evaluate_delivery", lambda **_kwargs: report)
    monkeypatch.setattr(store, "_has_current_machine_evidence", lambda *_a, **_k: True)
    result = asyncio.run(verifier.dispatch(args, ctx))

    assert result["production_state"] == "ready_for_review"
    assert transitions == ["ready_for_review"]
    assert store.load_run(project_id, run_id)["state"] == "ready_for_review"
    assert result["evidence_id"] in store.load_run(project_id, run_id)["evidence_ids"]
    run = store.load_run(project_id, run_id)
    assert [item["asset_id"] for item in run["deliverables"]] == [
        args["export_asset_id"]
    ]
    assert result["delivery"]["review_master"]["asset_id"] == args["export_asset_id"]
    assert "output_path" not in json.dumps(result["delivery"])


def test_backfill_existing_ready_run_records_only_delivery_fact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import gemia.tools.verify_delivery as verifier

    store, project_id, run_id, ctx, args, _transitions, receipt = _delivery_context(
        tmp_path
    )
    report = {
        "schema": "lumeri.production-acceptance",
        "version": 1,
        "project_revision": receipt["project_revision"],
        "render_id": receipt["render_id"],
        "graph_hash": receipt["graph_hash"],
        "ready_for_review": True,
        "checks": [{"code": "formal", "ok": True}],
        "blockers": [],
        "human_review_required": True,
    }
    monkeypatch.setattr(verifier, "evaluate_delivery", lambda **_kwargs: report)
    monkeypatch.setattr(store, "_has_current_machine_evidence", lambda *_a, **_k: True)
    verified = asyncio.run(verifier.dispatch(args, ctx))
    evidence_path = (
        store.evidence_dir(project_id, run_id) / f"{verified['evidence_id']}.json"
    )
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))

    # Model a pre-delivery-fact build: acceptance and bytes are real/current,
    # but the run had no active review-master identity.
    legacy_run = store.load_run(project_id, run_id)
    legacy_run["deliverables"] = []
    store._write_json(store.run_path(project_id, run_id), legacy_run)  # noqa: SLF001
    before_revision = int(legacy_run["revision"])
    before_project_revision = int(legacy_run["project_revision"])
    before_budget = store.media_budget(project_id, run_id).snapshot()
    monkeypatch.setattr(
        store,
        "_current_machine_evidence",
        lambda *_a, **_k: evidence,
    )

    backfilled = verifier.backfill_current_delivery(
        store=store,
        registry=ctx.registry,
        project_id=project_id,
        run_id=run_id,
    )
    current = store.load_run(project_id, run_id)
    assert backfilled["duplicate"] is False
    assert current["state"] == "ready_for_review"
    assert current["project_revision"] == before_project_revision
    assert current["revision"] == before_revision + 1
    assert current["deliverables"][0]["asset_id"] == args["export_asset_id"]
    assert store.media_budget(project_id, run_id).snapshot() == before_budget


def test_verify_delivery_failure_moves_to_revising(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import gemia.tools.verify_delivery as verifier

    store, project_id, run_id, ctx, args, transitions, receipt = _delivery_context(
        tmp_path
    )
    report = {
        "schema": "lumeri.production-acceptance",
        "version": 1,
        "project_revision": receipt["project_revision"],
        "render_id": receipt["render_id"],
        "graph_hash": receipt["graph_hash"],
        "ready_for_review": False,
        "checks": [{"code": "audio_roles_complete", "ok": False}],
        "blockers": [{"code": "audio_roles_complete"}],
        "human_review_required": True,
    }
    monkeypatch.setattr(verifier, "evaluate_delivery", lambda **_kwargs: report)
    result = asyncio.run(verifier.dispatch(args, ctx))

    assert result["status"] == "failed"
    assert result["error_code"] == "E_DELIVERY_GATE"
    assert transitions == ["revising"]
    assert store.load_run(project_id, run_id)["state"] == "revising"


def _delivery_asset(ctx: ToolContext, args: dict[str, object], role: str):
    return ctx.registry.get(str(args[f"{role}_asset_id"]))


def _assert_preflight_rejected(
    *,
    store: ProductionStore,
    project_id: str,
    run_id: str,
    ctx: ToolContext,
    args: dict[str, object],
    transitions: list[str],
    monkeypatch: pytest.MonkeyPatch,
    expected_code: str,
) -> None:
    import gemia.tools.verify_delivery as verifier

    def should_not_evaluate(**_kwargs):
        pytest.fail("formal acceptance ran before receipt preflight completed")

    monkeypatch.setattr(verifier, "evaluate_delivery", should_not_evaluate)
    with pytest.raises(ToolError) as exc:
        asyncio.run(verifier.dispatch(args, ctx))
    assert exc.value.code == expected_code
    assert transitions == ["revising"]
    run = store.load_run(project_id, run_id)
    assert run["state"] == "revising"
    assert len(run["evidence_ids"]) == 1
    evidence_path = (
        store.evidence_dir(project_id, run_id) / f"{run['evidence_ids'][0]}.json"
    )
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    report = evidence["payload"]["acceptance_report"]
    assert report["ready_for_review"] is False
    assert report["blockers"][0]["code"] == expected_code
    assert report["blockers"][0]["phase"] == "receipt_preflight"


@pytest.mark.parametrize("role", ["export", "preview"])
@pytest.mark.parametrize("mutation", ["version", "payload"])
def test_verify_delivery_rejects_stale_render_semantics_before_acceptance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    role: str,
    mutation: str,
) -> None:
    store, project_id, run_id, ctx, args, transitions, _receipt = _delivery_context(
        tmp_path
    )
    receipt = _delivery_asset(ctx, args, role).source["render_receipt"]
    if mutation == "version":
        receipt["render_semantics_version"] = CANONICAL_RENDER_SEMANTICS_VERSION - 1
    else:
        receipt["render_semantics"] = {
            "version": CANONICAL_RENDER_SEMANTICS_VERSION,
            "audio_master": {"delivery_duration_policy": "legacy"},
        }

    _assert_preflight_rejected(
        store=store,
        project_id=project_id,
        run_id=run_id,
        ctx=ctx,
        args=args,
        transitions=transitions,
        monkeypatch=monkeypatch,
        expected_code="E_STALE_EVIDENCE",
    )


@pytest.mark.parametrize(
    ("role", "mutation"),
    [
        ("export", "status"),
        ("export", "blocker"),
        ("preview", "status"),
        ("preview", "blocker"),
    ],
)
def test_verify_delivery_rejects_failed_machine_receipt_before_acceptance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    role: str,
    mutation: str,
) -> None:
    store, project_id, run_id, ctx, args, transitions, _receipt = _delivery_context(
        tmp_path
    )
    receipt = _delivery_asset(ctx, args, role).source["render_receipt"]
    if mutation == "status":
        receipt["machine_status"] = "rejected"
    else:
        receipt["machine_blockers"] = [{"code": "decode_failed"}]

    _assert_preflight_rejected(
        store=store,
        project_id=project_id,
        run_id=run_id,
        ctx=ctx,
        args=args,
        transitions=transitions,
        monkeypatch=monkeypatch,
        expected_code="E_DELIVERY_GATE",
    )


@pytest.mark.parametrize("role", ["export", "preview"])
def test_verify_delivery_rejects_receipt_from_another_project_revision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    role: str,
) -> None:
    store, project_id, run_id, ctx, args, transitions, _receipt = _delivery_context(
        tmp_path
    )
    receipt = _delivery_asset(ctx, args, role).source["render_receipt"]
    receipt["project_revision"] = int(receipt["project_revision"]) - 1

    _assert_preflight_rejected(
        store=store,
        project_id=project_id,
        run_id=run_id,
        ctx=ctx,
        args=args,
        transitions=transitions,
        monkeypatch=monkeypatch,
        expected_code="E_STALE_EVIDENCE",
    )


@pytest.mark.parametrize("role", ["export", "preview"])
@pytest.mark.parametrize(
    "mutation",
    ["bytes", "receipt_hash", "asset_hash", "missing", "output_path", "receipt"],
)
def test_verify_delivery_rejects_missing_or_changed_media_before_acceptance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    role: str,
    mutation: str,
) -> None:
    store, project_id, run_id, ctx, args, transitions, _receipt = _delivery_context(
        tmp_path
    )
    record = _delivery_asset(ctx, args, role)
    receipt = record.source["render_receipt"]
    if mutation == "receipt":
        record.source.pop("render_receipt")
    elif mutation == "bytes":
        record.path.write_bytes(b"changed after receipt")
    elif mutation == "receipt_hash":
        receipt["output_sha256"] = "0" * 64
    elif mutation == "asset_hash":
        record.sha256 = "0" * 64
    elif mutation == "missing":
        record.path.unlink()
    else:
        other = tmp_path / f"other-{role}.mp4"
        other.write_bytes(record.path.read_bytes())
        receipt["output_path"] = str(other.resolve())

    _assert_preflight_rejected(
        store=store,
        project_id=project_id,
        run_id=run_id,
        ctx=ctx,
        args=args,
        transitions=transitions,
        monkeypatch=monkeypatch,
        expected_code=(
            "E_EVIDENCE_MISSING"
            if mutation in {"missing", "receipt"}
            else "E_STALE_EVIDENCE"
        ),
    )
