from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from gemia.production_budget import ProductionBudgetError, ProductionMediaBudget


def _ledger(
    tmp_path: Path, *, import_echo_spend: bool = True, **kwargs
) -> ProductionMediaBudget:
    ledger = ProductionMediaBudget(tmp_path / "production-budget.json", run_id="run-1", **kwargs)
    if import_echo_spend:
        ledger.import_baseline(
            import_key="echo-protocol-existing-spend",
            amount_usd="1.525",
            evidence={"basis": "minimum known pre-ledger provider spend"},
        )
    return ledger


def test_new_run_starts_at_zero_then_explicitly_imports_old_spend(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path, import_echo_spend=False)
    snapshot = ledger.snapshot()
    assert snapshot["baseline_spend_usd"] == 0.0
    assert snapshot["committed_usd"] == 0.0
    ledger.import_baseline(
        import_key="echo-protocol-existing-spend",
        amount_usd="1.525",
        evidence={"basis": "minimum known pre-ledger provider spend"},
    )
    snapshot = ledger.snapshot()
    assert snapshot["baseline_spend_usd"] == pytest.approx(1.525)
    assert snapshot["committed_usd"] == pytest.approx(1.525)
    assert snapshot["warning_usd"] == 12.0
    assert snapshot["cap_usd"] == 15.0
    assert snapshot["veo_max_calls"] == 3
    assert snapshot["veo_max_duration_sec"] == 24.0


def test_warning_at_twelve_and_hard_reject_over_fifteen(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    first = ledger.reserve(
        idempotency_key="first", tool_name="generate_image", estimated_usd="10.475"
    )
    assert first.ok and first.warning
    assert ledger.snapshot()["committed_usd"] == pytest.approx(12.0)

    exact_cap = ledger.reserve(
        idempotency_key="exact", tool_name="generate_image", estimated_usd="3.0"
    )
    assert exact_cap.ok
    assert ledger.snapshot()["committed_usd"] == pytest.approx(15.0)

    rejected = ledger.reserve(
        idempotency_key="over", tool_name="generate_image", estimated_usd="0.000001"
    )
    assert not rejected.ok
    assert "cap" in rejected.reason
    assert ledger.snapshot()["committed_usd"] == pytest.approx(15.0)


def test_duplicate_key_is_idempotent_and_only_one_submission_claim_wins(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    created = ledger.reserve(
        idempotency_key="shot-7", tool_name="generate_image", estimated_usd="0.101"
    )
    replay = ledger.reserve(
        idempotency_key="shot-7", tool_name="generate_image", estimated_usd="0.101"
    )
    assert created.ok and created.created and created.reservation is not None
    assert replay.ok and not replay.created and replay.reservation is not None
    assert replay.reservation.reservation_id == created.reservation.reservation_id
    assert ledger.claim_submission(created.reservation.reservation_id)
    assert not ledger.claim_submission(created.reservation.reservation_id)


def test_idempotency_key_collision_fails_closed(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    ledger.reserve(idempotency_key="same", tool_name="generate_image", estimated_usd="0.101")
    collision = ledger.reserve(
        idempotency_key="same", tool_name="generate_video", estimated_usd="2.8",
        requested_duration_sec=8,
    )
    assert not collision.ok
    assert "different paid call" in collision.reason


def test_parallel_reservations_cannot_overspend(tmp_path: Path) -> None:
    path = tmp_path / "parallel.json"

    def reserve(index: int) -> bool:
        ledger = ProductionMediaBudget(
            path, run_id="parallel", cap_usd=3.0, warning_usd=2.5
        )
        ledger.import_baseline(import_key="old", amount_usd="1.525")
        return ledger.reserve(
            idempotency_key=f"call-{index}",
            tool_name="generate_image",
            estimated_usd=0.5,
        ).ok

    with ThreadPoolExecutor(max_workers=10) as pool:
        accepted = list(pool.map(reserve, range(10)))
    # $3 cap - $1.525 baseline leaves room for exactly two $0.50 calls.
    assert sum(accepted) == 2
    snapshot = ProductionMediaBudget.open(path).snapshot()
    assert snapshot["committed_usd"] == pytest.approx(2.525)
    assert snapshot["committed_usd"] <= snapshot["cap_usd"]


def test_veo_is_limited_to_three_calls_and_twenty_four_seconds(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    reservations = []
    for index in range(3):
        result = ledger.reserve(
            idempotency_key=f"veo-{index}",
            tool_name="generate_video",
            estimated_usd=2.8,
            requested_duration_sec=8,
        )
        assert result.ok and result.reservation is not None
        reservations.append(result.reservation)
    fourth = ledger.reserve(
        idempotency_key="veo-3",
        tool_name="generate_video",
        estimated_usd=2.8,
        requested_duration_sec=1,
    )
    assert not fourth.ok
    assert "call limit" in fourth.reason
    snapshot = ledger.snapshot()
    assert snapshot["veo_reserved_calls"] == 3
    assert snapshot["veo_reserved_duration_sec"] == 24.0


def test_veo_duration_limit_is_independent_of_call_limit(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    first = ledger.reserve(
        idempotency_key="long-1", tool_name="generate_video", estimated_usd=1,
        requested_duration_sec=20,
    )
    assert first.ok
    second = ledger.reserve(
        idempotency_key="long-2", tool_name="generate_video", estimated_usd=1,
        requested_duration_sec=5,
    )
    assert not second.ok
    assert "duration limit" in second.reason


def test_veo_reservation_requires_duration(tmp_path: Path) -> None:
    result = _ledger(tmp_path).reserve(
        idempotency_key="missing-duration",
        tool_name="generate_video",
        estimated_usd=2.8,
    )
    assert not result.ok
    assert "requires requested_duration_sec" in result.reason


def test_uncertain_request_keeps_estimate_and_cannot_be_released(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    decision = ledger.reserve(
        idempotency_key="uncertain", tool_name="generate_image", estimated_usd=0.101
    )
    assert decision.reservation is not None
    reservation_id = decision.reservation.reservation_id
    assert ledger.claim_submission(reservation_id)
    ledger.mark_uncertain(reservation_id, error="transport ended after send")
    assert ledger.snapshot()["committed_usd"] == pytest.approx(1.626)
    with pytest.raises(ProductionBudgetError, match="cannot be released"):
        ledger.release(reservation_id)


def test_release_only_pre_submit_work_and_persists_across_reload(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    decision = ledger.reserve(
        idempotency_key="cancelled", tool_name="generate_image", estimated_usd=0.101
    )
    assert decision.reservation is not None
    ledger.release(decision.reservation.reservation_id, reason="cancelled before dispatch")
    reloaded = ProductionMediaBudget.open(ledger.path)
    assert reloaded.snapshot()["committed_usd"] == pytest.approx(1.525)
    replay = reloaded.reserve(
        idempotency_key="cancelled", tool_name="generate_image", estimated_usd=0.101
    )
    assert not replay.ok


def test_orphan_reconciliation_is_idempotent_and_keeps_submitted_estimate(
    tmp_path: Path,
) -> None:
    ledger = _ledger(tmp_path)
    decision = ledger.reserve(
        idempotency_key="orphaned-submission",
        tool_name="generate_image",
        estimated_usd="0.101",
    )
    assert decision.reservation is not None
    reservation_id = decision.reservation.reservation_id
    assert ledger.claim_submission(reservation_id)

    first = ledger.reconcile_orphaned_reservation(
        reservation_id,
        tool_call_id="tool-orphaned",
        owner_id="dead-owner",
        reason="crash seam",
    )
    replay = ledger.reconcile_orphaned_reservation(
        reservation_id,
        tool_call_id="tool-orphaned",
        owner_id="dead-owner",
        reason="crash seam",
    )
    assert first == replay
    assert first["action"] == "retained_estimate_uncertain"
    assert ledger.get(reservation_id).status == "uncertain"
    assert ledger.snapshot()["committed_usd"] == pytest.approx(1.626)
    document = json.loads(ledger.path.read_text(encoding="utf-8"))
    assert len(document["reservations"][reservation_id]["reconciliation_history"]) == 1


def test_charged_but_unmaterialized_asset_is_a_durable_reconciliation_blocker(
    tmp_path: Path,
) -> None:
    ledger = _ledger(tmp_path)
    decision = ledger.reserve(
        idempotency_key="charged-image", tool_name="generate_image", estimated_usd="0.101"
    )
    assert decision.reservation is not None
    reservation_id = decision.reservation.reservation_id
    ledger.claim_submission(reservation_id)
    ledger.settle(reservation_id, result_asset_id="img_001")
    blockers = ledger.snapshot()["reconciliation_blockers"]
    assert blockers == [
        {
            "code": "charged_asset_missing",
            "reservation_id": reservation_id,
            "request_id": decision.reservation.request_id,
            "result_asset_id": "img_001",
        }
    ]

    asset = tmp_path / "img_001.png"
    asset.write_bytes(b"materialized")
    ledger.mark_asset_materialized(
        reservation_id,
        result_asset_id="img_001",
        asset_path=asset,
        asset_sha256=hashlib.sha256(asset.read_bytes()).hexdigest(),
    )
    assert ledger.snapshot()["reconciliation_blockers"] == []


def test_open_snapshot_and_get_are_strictly_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = _ledger(tmp_path)
    decision = ledger.reserve(
        idempotency_key="read-only-receipt",
        tool_name="generate_image",
        estimated_usd="0.101",
    )
    assert decision.reservation is not None

    writes: list[dict] = []
    monkeypatch.setattr(
        ProductionMediaBudget,
        "_write_atomic",
        lambda self, document: writes.append(dict(document)),
    )
    reopened = ProductionMediaBudget.open(ledger.path)
    snapshot = reopened.snapshot()
    reopened.get(decision.reservation.reservation_id)

    assert writes == []
    assert snapshot["spent_usd"] == pytest.approx(1.525)
    assert snapshot["reserved_usd"] == pytest.approx(0.101)
    assert snapshot["remaining_usd"] == pytest.approx(13.374)


def test_one_budget_mutation_performs_one_atomic_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = _ledger(tmp_path)
    real_write = ProductionMediaBudget._write_atomic
    writes: list[str] = []

    def counted_write(self, document):
        writes.append(str(document.get("updated_at") or ""))
        return real_write(self, document)

    monkeypatch.setattr(ProductionMediaBudget, "_write_atomic", counted_write)
    decision = ledger.reserve(
        idempotency_key="single-write",
        tool_name="generate_image",
        estimated_usd="0.101",
    )
    assert decision.ok
    assert len(writes) == 1
