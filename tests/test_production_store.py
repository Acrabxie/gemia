from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
from pathlib import Path

import pytest

from gemia.production_store import (
    IdempotencyConflictError,
    ProductionStore,
    ProductionStoreError,
    RevisionConflictError,
    StateTransitionError,
)
from gemia.reality_contract import (
    normalize_reality_contract,
    required_acceptance_check_codes,
)
from gemia.tools._context import AssetRegistry


_TEST_CONTRACT = normalize_reality_contract(
    {
        "brief": "A 120-second durable production fixture",
        "deliverable": {
            "duration_sec": 120,
            "audio": {"required_roles": ["music", "narration", "sfx"]},
        },
        "acceptance": {
            "edit_units": {"min": 36, "max": 48},
            "median_shot_duration_max_sec": 3,
            "verified_motion_min_sec": 60,
            "licensed_public_motion_assets_min": 10,
            "static_shot_max_sec": 3,
            "review_sample_frames_min": 12,
        },
    }
)


def _claim_and_hold_worker(
    root: str,
    start_event,
    release_event,
    result_queue,
) -> None:
    store = ProductionStore(root)
    start_event.wait(10)
    receipt = store.claim_tool_call(
        "project-a",
        "run-a",
        tool_name="stock_media",
        args={"action": "search", "query": "rainy city"},
        trace_id=f"trace-{os.getpid()}",
        idempotency_key="cross-process-key",
        project_revision=1,
    )
    result_queue.put(
        {
            "duplicate": bool(receipt["duplicate"]),
            "status": receipt["status"],
            "pid": os.getpid(),
        }
    )
    release_event.wait(15)


def _crash_after_budget_seam_worker(
    root: str,
    *,
    idempotency_key: str,
    submitted: bool,
    marker_path: str,
) -> None:
    store = ProductionStore(root)
    receipt = store.claim_tool_call(
        "project-a",
        "run-a",
        tool_name="generate_image",
        args={"prompt": idempotency_key},
        trace_id=f"trace-{idempotency_key}",
        idempotency_key=idempotency_key,
        project_revision=1,
    )
    ledger = store.media_budget("project-a", "run-a")
    decision = ledger.reserve(
        idempotency_key=idempotency_key,
        tool_name="generate_image",
        estimated_usd="0.101",
        provider="crash-seam-provider",
        model="test-model",
    )
    assert decision.ok and decision.reservation is not None
    store.bind_tool_call_reservation(
        "project-a",
        "run-a",
        idempotency_key,
        reservation_id=decision.reservation.reservation_id,
    )
    if submitted:
        assert ledger.claim_submission(decision.reservation.reservation_id)
    Path(marker_path).write_text(
        json.dumps(
            {
                "tool_call_id": receipt["tool_call_id"],
                "reservation_id": decision.reservation.reservation_id,
            }
        ),
        encoding="utf-8",
    )
    # Model an uncatchable host loss at the exact durable seam.  The OS must
    # release the advisory execution lease; no Python cleanup is allowed.
    os._exit(23)


def _store_with_run(tmp_path: Path) -> tuple[ProductionStore, str, str]:
    store = ProductionStore(tmp_path)
    project_id, run_id = "project-a", "run-a"
    store.create_project(project_id)
    store.observe_project_state(project_id, state_hash="state-a", timeline_patch_seq=0)
    store.create_run(project_id, run_id, reality_contract=_TEST_CONTRACT)
    return store, project_id, run_id


_FORMAL_CHECK_CODES = tuple(sorted(required_acceptance_check_codes(_TEST_CONTRACT)))
_CREATIVE_APPROVAL = {
    "story": True,
    "pacing": True,
    "visual": True,
    "sound": True,
    "publishable": True,
}


def _formal_acceptance_payload(project_revision: int) -> dict:
    inspection_ids = [f"frame-{index:02d}" for index in range(12)]
    checks = []
    for code in _FORMAL_CHECK_CODES:
        actual: object = True
        if code.startswith("review_"):
            actual = {
                "status": "passed",
                "note": f"inspected {code}",
                "inspection_asset_ids": inspection_ids,
            }
        checks.append(
            {"code": code, "ok": True, "actual": actual, "expected": "passed"}
        )
    return {
        "acceptance_report": {
            "schema": "lumeri.production-acceptance",
            "version": 1,
            "project_revision": project_revision,
            "ready_for_review": True,
            "render_id": "render-a",
            "graph_hash": "a" * 64,
            "checks": checks,
            "blockers": [],
            "human_review_required": True,
            "human_review_dimensions": [
                "story",
                "pacing",
                "visual",
                "sound",
                "publishable",
            ],
        },
        "export_asset_id": "export-a",
        "preview_asset_id": "preview-a",
        "inspection_asset_ids": inspection_ids,
    }


def _record_review_master(
    store: ProductionStore,
    project_id: str,
    run_id: str,
    *,
    project_revision: int,
    evidence_id: str,
) -> Path:
    media = store.project_dir(project_id) / "review-master.mp4"
    media.write_bytes(b"verified-review-master")
    digest = hashlib.sha256(media.read_bytes()).hexdigest()
    receipt = {
        "project_revision": project_revision,
        "render_id": "render-a",
        "graph_hash": "a" * 64,
        "render_semantics_version": 3,
        "output_path": str(media.resolve()),
        "output_sha256": digest,
        "machine_status": "passed",
        "machine_blockers": [],
        "probe": {"duration": 120.0},
    }
    registry = AssetRegistry()
    registry.register_output(
        "export-a",
        kind="video",
        path=media,
        summary="formal review master",
        source={"kind": "derived_export", "render_receipt": receipt},
    )
    registry.save(store.asset_registry_path(project_id))
    store.record_deliverable(
        project_id,
        run_id,
        asset_id="export-a",
        project_revision=project_revision,
        sha256=digest,
        graph_hash="a" * 64,
        render_id="render-a",
        render_semantics_version=3,
        evidence_id=evidence_id,
        duration_sec=120.0,
    )
    return media


def test_project_revision_is_monotonic_and_turn_claim_is_idempotent(tmp_path: Path) -> None:
    store, project_id, run_id = _store_with_run(tmp_path)
    assert store.load_project(project_id)["revision"] == 1
    assert store.observe_project_state(
        project_id, state_hash="state-a", timeline_patch_seq=0
    )["revision"] == 1
    assert store.observe_project_state(
        project_id, state_hash="state-b", timeline_patch_seq=0
    )["revision"] == 2

    claimed = store.claim_turn(
        project_id,
        run_id,
        session_id="session-a",
        client_turn_id="client-turn-a",
        message="continue",
        project_revision=2,
        expected_project_revision=2,
    )
    assert claimed["duplicate"] is False
    duplicate = store.claim_turn(
        project_id,
        run_id,
        session_id="session-a",
        client_turn_id="client-turn-a",
        message="continue",
        project_revision=2,
        expected_project_revision=2,
    )
    assert duplicate["duplicate"] is True
    store.observe_project_state(project_id, state_hash="state-c", timeline_patch_seq=1)
    retry_after_revision_advance = store.claim_turn(
        project_id,
        run_id,
        session_id="session-a",
        client_turn_id="client-turn-a",
        message="continue",
        project_revision=3,
        expected_project_revision=2,
    )
    assert retry_after_revision_advance["duplicate"] is True
    with pytest.raises(IdempotencyConflictError):
        store.claim_turn(
            project_id,
            run_id,
            session_id="session-a",
            client_turn_id="client-turn-a",
            message="different request",
            project_revision=2,
        )
    with pytest.raises(RevisionConflictError) as conflict:
        store.claim_turn(
            project_id,
            run_id,
            session_id="session-a",
            client_turn_id="client-turn-b",
            message="continue",
            project_revision=3,
            expected_project_revision=2,
        )
    assert conflict.value.current_revision == 3


def test_ready_and_approval_require_complete_current_production_acceptance(
    tmp_path: Path,
) -> None:
    store, project_id, run_id = _store_with_run(tmp_path)
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

    with pytest.raises(StateTransitionError):
        store.transition_run(project_id, run_id, "ready_for_review")

    project_revision = store.load_project(project_id)["revision"]
    store.add_evidence(
        project_id,
        run_id,
        kind="machine_acceptance",
        project_revision=project_revision,
        payload={
            "schema": "lumeri.production-acceptance",
            "project_revision": project_revision,
            "ready_for_review": True,
            "render_id": "render-a",
            "graph_hash": "a" * 64,
            "checks": [{"code": "formal_delivery", "ok": True}],
            "blockers": [],
        },
    )
    with pytest.raises(StateTransitionError):
        store.transition_run(project_id, run_id, "ready_for_review")

    # Renaming the same one-row claim does not turn it into formal Evidence.
    store.add_evidence(
        project_id,
        run_id,
        kind="production_acceptance",
        project_revision=project_revision,
        payload={
            "acceptance_report": {
                "schema": "lumeri.production-acceptance",
                "version": 1,
                "project_revision": project_revision,
                "ready_for_review": True,
                "render_id": "render-a",
                "graph_hash": "a" * 64,
                "checks": [
                    {
                        "code": "formal_delivery",
                        "ok": True,
                        "actual": True,
                        "expected": True,
                    }
                ],
                "blockers": [],
                "human_review_required": True,
                "human_review_dimensions": list(_CREATIVE_APPROVAL),
            },
            "export_asset_id": "export-a",
            "preview_asset_id": "preview-a",
            "inspection_asset_ids": [f"frame-{index:02d}" for index in range(12)],
        },
    )
    with pytest.raises(StateTransitionError):
        store.transition_run(project_id, run_id, "ready_for_review")

    formal_evidence = store.add_evidence(
        project_id,
        run_id,
        kind="production_acceptance",
        project_revision=project_revision,
        payload=_formal_acceptance_payload(project_revision),
    )
    ready = store.transition_run(project_id, run_id, "ready_for_review")
    assert ready["state"] == "ready_for_review"
    media = _record_review_master(
        store,
        project_id,
        run_id,
        project_revision=project_revision,
        evidence_id=formal_evidence["evidence_id"],
    )
    public_delivery = store.public_delivery(project_id, run_id)
    assert public_delivery is not None
    assert public_delivery["review_master"]["asset_id"] == "export-a"
    assert public_delivery["review_master"]["url"].endswith(
        "/artifacts/export-a"
    )
    assert "output_path" not in json.dumps(public_delivery)

    with pytest.raises(ProductionStoreError, match="full-video watch"):
        store.review_run(project_id, run_id, action="approve")
    with pytest.raises(ProductionStoreError, match="explicit boolean"):
        store.review_run(
            project_id,
            run_id,
            action="approve",
            watched_full_video=True,
            creative_checks={"story": True},
        )
    with pytest.raises(ProductionStoreError, match="failed creative"):
        store.review_run(
            project_id,
            run_id,
            action="approve",
            watched_full_video=True,
            creative_checks={**_CREATIVE_APPROVAL, "sound": False},
        )
    with pytest.raises(ProductionStoreError, match="non_boolean"):
        store.review_run(
            project_id,
            run_id,
            action="approve",
            watched_full_video=True,
            creative_checks={**_CREATIVE_APPROVAL, "publishable": "yes"},
        )

    media.write_bytes(b"tampered-after-review")
    with pytest.raises(ProductionStoreError, match="bytes changed"):
        store.review_run(
            project_id,
            run_id,
            action="approve",
            watched_full_video=True,
            creative_checks=_CREATIVE_APPROVAL,
        )
    media.write_bytes(b"verified-review-master")

    accepted = store.review_run(
        project_id,
        run_id,
        action="approve",
        watched_full_video=True,
        creative_checks=_CREATIVE_APPROVAL,
    )
    assert accepted["state"] == "accepted"
    assert accepted["review"]["watched_full_video"] is True
    assert accepted["review"]["creative_checks"] == _CREATIVE_APPROVAL

    # A project edit invalidates both receipt and human approval, even if the
    # timeline patch sequence later moves backwards through undo.
    changed = store.observe_project_state(
        project_id, state_hash="state-after-review", timeline_patch_seq=0
    )
    invalidated = store.sync_run_project_revision(
        project_id, run_id, int(changed["revision"])
    )
    assert invalidated["state"] == "revising"
    assert invalidated["review"]["invalidated_by_project_revision"] == 2
    assert invalidated["deliverables"] == []
    assert store.public_delivery(project_id, run_id) is None


def test_request_changes_remains_compatible_without_approval_confirmation(
    tmp_path: Path,
) -> None:
    store, project_id, run_id = _store_with_run(tmp_path)
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
    project_revision = store.load_project(project_id)["revision"]
    evidence = store.add_evidence(
        project_id,
        run_id,
        kind="production_acceptance",
        project_revision=project_revision,
        payload=_formal_acceptance_payload(project_revision),
    )
    store.transition_run(project_id, run_id, "ready_for_review")
    _record_review_master(
        store,
        project_id,
        run_id,
        project_revision=project_revision,
        evidence_id=evidence["evidence_id"],
    )

    requested = store.review_run(
        project_id,
        run_id,
        action="request_changes",
        note="tighten the 25-31 second beat",
        start_sec=25.0,
        end_sec=31.0,
    )

    assert requested["state"] == "revising"
    assert requested["review"]["action"] == "request_changes"
    assert requested["deliverables"] == []
    assert store.public_delivery(project_id, run_id) is None
    assert "watched_full_video" not in requested["review"]
    assert "creative_checks" not in requested["review"]


def test_formal_acceptance_for_previous_project_revision_cannot_reach_ready(
    tmp_path: Path,
) -> None:
    store, project_id, run_id = _store_with_run(tmp_path)
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
    evidence_revision = store.load_project(project_id)["revision"]
    store.add_evidence(
        project_id,
        run_id,
        kind="production_acceptance",
        project_revision=evidence_revision,
        payload=_formal_acceptance_payload(evidence_revision),
    )

    current = store.observe_project_state(
        project_id,
        state_hash="state-after-formal-acceptance",
        timeline_patch_seq=1,
    )
    store.sync_run_project_revision(project_id, run_id, int(current["revision"]))

    with pytest.raises(StateTransitionError, match="current project revision"):
        store.transition_run(project_id, run_id, "ready_for_review")


def test_only_canonical_media_budget_can_authorize_spend(tmp_path: Path) -> None:
    store, project_id, run_id = _store_with_run(tmp_path)
    ledger = store.media_budget(project_id, run_id)
    decision = ledger.reserve(
        idempotency_key="paid-a",
        tool_name="generate_image",
        estimated_usd=0.101,
        provider="vertex",
        model="image-model",
    )
    assert decision.ok and decision.created
    document = json.loads(ledger.path.read_text(encoding="utf-8"))
    assert decision.reservation is not None
    assert decision.reservation.reservation_id in document["reservations"]
    with pytest.raises(ProductionStoreError, match="disabled"):
        store._legacy_float_reserve_budget_do_not_use(  # noqa: SLF001
            project_id,
            run_id,
            idempotency_key="legacy",
            tool_name="generate_image",
            estimated_usd=0.101,
            trace_id="trace",
        )


def test_asset_metadata_update_is_identity_safe_and_survives_reload(tmp_path: Path) -> None:
    media = tmp_path / "motion.mp4"
    media.write_bytes(b"fake-video-content")
    registry_path = tmp_path / "assets.json"
    registry = AssetRegistry(on_change=lambda value: value.save(registry_path))
    original = registry.add_external(
        media,
        source={"kind": "stock", "provider": "pixabay"},
        license={"name": "Pixabay Content License"},
    )
    updated = registry.update_record(
        original.asset_id,
        source_patch={"real_motion_verified": True},
        license_patch={"source_url": "https://example.invalid/item"},
        summary="verified city motion",
    )
    assert (updated.asset_id, updated.path, updated.sha256, updated.kind) == (
        original.asset_id,
        original.path,
        original.sha256,
        original.kind,
    )
    restored = AssetRegistry.load(registry_path).get(original.asset_id)
    assert restored.summary == "verified city motion"
    assert restored.source["real_motion_verified"] is True
    assert restored.license["source_url"].endswith("/item")


def test_formal_tool_receipt_is_claimed_bound_and_replayed_without_reexecution(
    tmp_path: Path,
) -> None:
    store, project_id, run_id = _store_with_run(tmp_path)
    claimed = store.claim_tool_call(
        project_id,
        run_id,
        tool_name="stock_media",
        args={"action": "search", "query": "rainy city"},
        trace_id="trace-stock-search",
        idempotency_key="echo:stock:search:rainy-city",
        project_revision=1,
    )
    assert claimed["duplicate"] is False
    store.bind_tool_call_reservation(
        project_id,
        run_id,
        "echo:stock:search:rainy-city",
        reservation_id="pmr-test",
    )
    completed = store.complete_tool_call(
        project_id,
        run_id,
        "echo:stock:search:rainy-city",
        status="succeeded",
        project_revision=1,
        result={"result_count": 4},
        reservation_id="pmr-test",
    )
    assert completed["result"] == {"result_count": 4}
    replay = store.claim_tool_call(
        project_id,
        run_id,
        tool_name="stock_media",
        args={"action": "search", "query": "rainy city"},
        trace_id="trace-stock-search-replay",
        idempotency_key="echo:stock:search:rainy-city",
        project_revision=2,
    )
    assert replay["duplicate"] is True
    assert replay["status"] == "succeeded"
    with pytest.raises(IdempotencyConflictError):
        store.claim_tool_call(
            project_id,
            run_id,
            tool_name="stock_media",
            args={"action": "search", "query": "different"},
            trace_id="trace-conflict",
            idempotency_key="echo:stock:search:rainy-city",
            project_revision=1,
        )


def test_resume_does_not_reconcile_a_tool_call_owned_by_this_live_store(
    tmp_path: Path,
) -> None:
    store, project_id, run_id = _store_with_run(tmp_path)
    store.claim_tool_call(
        project_id,
        run_id,
        tool_name="stock_media",
        args={"action": "fetch", "query": "orbit", "result_id": "42"},
        trace_id="trace-interrupted",
        idempotency_key="echo:stock:fetch:orbit:42",
        project_revision=1,
    )
    reconciled = store.reconcile_inflight_tool_calls(
        project_id, run_id, project_revision=1
    )
    assert reconciled == []
    assert store.get_tool_call(
        project_id, run_id, "echo:stock:fetch:orbit:42"
    )["status"] == "claimed"
    store.complete_tool_call(
        project_id,
        run_id,
        "echo:stock:fetch:orbit:42",
        status="failed",
        project_revision=1,
        error="test cleanup",
    )


def test_two_processes_cannot_both_claim_and_live_owner_is_not_reconciled(
    tmp_path: Path,
) -> None:
    root = tmp_path / "cross-process"
    _store_with_run(root)
    context = multiprocessing.get_context("spawn")
    start_event = context.Event()
    release_event = context.Event()
    result_queue = context.Queue()
    processes = [
        context.Process(
            target=_claim_and_hold_worker,
            args=(str(root), start_event, release_event, result_queue),
        )
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    start_event.set()
    results = [result_queue.get(timeout=15) for _ in processes]
    assert sum(not row["duplicate"] for row in results) == 1
    assert sum(row["duplicate"] for row in results) == 1

    observer = ProductionStore(root)
    assert observer.reconcile_inflight_tool_calls(
        "project-a", "run-a", project_revision=1
    ) == []
    receipt = observer.get_tool_call(
        "project-a", "run-a", "cross-process-key"
    )
    assert receipt["status"] == "claimed"
    with pytest.raises(IdempotencyConflictError, match="live execution owner"):
        observer.bind_tool_call_reservation(
            "project-a",
            "run-a",
            "cross-process-key",
            reservation_id="foreign-reservation",
        )
    with pytest.raises(IdempotencyConflictError, match="live execution owner"):
        observer.complete_tool_call(
            "project-a",
            "run-a",
            "cross-process-key",
            status="failed",
            project_revision=1,
            error="foreign completion must be rejected",
        )

    release_event.set()
    for process in processes:
        process.join(timeout=15)
        assert process.exitcode == 0

    reconciled = observer.reconcile_inflight_tool_calls(
        "project-a", "run-a", project_revision=1
    )
    assert len(reconciled) == 1
    assert reconciled[0]["status"] == "failed"
    assert reconciled[0]["reconciliation"]["action"] == "orphan_reconciled"


@pytest.mark.parametrize(
    ("submitted", "expected_receipt_status", "expected_budget_status", "expected_action"),
    [
        (False, "failed", "released", "released_unsubmitted"),
        (True, "uncertain", "uncertain", "retained_estimate_uncertain"),
    ],
)
def test_crash_reconciliation_applies_auditable_budget_policy(
    tmp_path: Path,
    submitted: bool,
    expected_receipt_status: str,
    expected_budget_status: str,
    expected_action: str,
) -> None:
    root = tmp_path / ("submitted" if submitted else "reserved")
    _store_with_run(root)
    key = f"crash-{'submitted' if submitted else 'reserved'}"
    marker = root / "crash-marker.json"
    context = multiprocessing.get_context("spawn")
    process = context.Process(
        target=_crash_after_budget_seam_worker,
        kwargs={
            "root": str(root),
            "idempotency_key": key,
            "submitted": submitted,
            "marker_path": str(marker),
        },
    )
    process.start()
    process.join(timeout=20)
    assert process.exitcode == 23
    crash_facts = json.loads(marker.read_text(encoding="utf-8"))

    observer = ProductionStore(root)
    reconciled = observer.reconcile_inflight_tool_calls(
        "project-a", "run-a", project_revision=1
    )
    assert len(reconciled) == 1
    receipt = reconciled[0]
    assert receipt["status"] == expected_receipt_status
    budget_reconciliation = receipt["reconciliation"]["budget"]
    assert budget_reconciliation["action"] == expected_action
    assert budget_reconciliation["audit"]["tool_call_id"] == crash_facts["tool_call_id"]

    ledger = observer.media_budget("project-a", "run-a")
    reservation = ledger.get(crash_facts["reservation_id"])
    assert reservation.status == expected_budget_status
    document = json.loads(ledger.path.read_text(encoding="utf-8"))
    history = document["reservations"][reservation.reservation_id][
        "reconciliation_history"
    ]
    assert len(history) == 1
    assert history[0]["action"] == expected_action
    assert observer.reconcile_inflight_tool_calls(
        "project-a", "run-a", project_revision=1
    ) == []

    if submitted:
        assert ledger.snapshot()["reserved_usd"] == pytest.approx(0.101)
    else:
        assert ledger.snapshot()["reserved_usd"] == 0.0


def test_evidence_id_is_idempotent_only_for_identical_canonical_content(
    tmp_path: Path,
) -> None:
    store, project_id, run_id = _store_with_run(tmp_path)
    original = store.add_evidence(
        project_id,
        run_id,
        kind="media_probe",
        payload={"duration": 120, "streams": {"video": True, "audio": True}},
        project_revision=1,
        trace_id="trace-evidence",
        evidence_id="evidence-fixed",
    )
    replay = store.add_evidence(
        project_id,
        run_id,
        kind="media_probe",
        payload={"streams": {"audio": True, "video": True}, "duration": 120},
        project_revision=1,
        trace_id="trace-evidence",
        evidence_id="evidence-fixed",
    )
    assert replay == original
    assert store.load_run(project_id, run_id)["evidence_ids"] == ["evidence-fixed"]

    conflicts = [
        {"kind": "different_kind"},
        {"payload": {"duration": 119}},
        {"project_revision": 2},
        {"trace_id": "different-trace"},
    ]
    baseline = {
        "kind": "media_probe",
        "payload": {"duration": 120, "streams": {"video": True, "audio": True}},
        "project_revision": 1,
        "trace_id": "trace-evidence",
    }
    for override in conflicts:
        with pytest.raises(IdempotencyConflictError):
            store.add_evidence(
                project_id,
                run_id,
                evidence_id="evidence-fixed",
                **{**baseline, **override},
            )
