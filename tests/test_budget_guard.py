"""Budget reservation / settlement API (docs/multi-agent-plan.md §5, WP1).

Ports the reservation-math assertions from the stranded RC3 parallel-dispatch
branch (d7f941c) onto the current BudgetGuard, plus the new amount-based
reserve_amount used by the subtask fan-out. Covers:
- reserve() adds estimates up front (so N concurrent launches can't all pass a
  stale check), settle returns the delta, and an unspent reservation credits
  the totals back down;
- reserve_amount() with the same atomic check-then-add semantics, plus refusal
  when a slice would breach either cap;
- existing commit() behavior stays intact.
"""
from __future__ import annotations

import pytest

from gemia.budget_guard import BudgetGuard, BudgetReservation
from gemia.production_budget import ProductionMediaBudget


# ── ported d7f941c reservation math ──────────────────────────────────────────


def test_reserve_adds_estimate_up_front() -> None:
    """reserve() must add the estimate to spent_* immediately so a second
    concurrent reserve sees the first one's claim (no stale-total overrun)."""
    g = BudgetGuard(max_usd=5.0, max_seconds=600.0)
    decision, res = g.reserve("generate_image")  # $0.101 / 10s
    assert decision.ok
    assert isinstance(res, BudgetReservation)
    assert res.tool_name == "generate_image"
    assert g.spent_usd == res.estimated_cost_usd == 0.101
    assert g.spent_seconds == res.estimated_eta_sec == 10.0


def test_reserve_refuses_over_cap_and_reserves_nothing() -> None:
    g = BudgetGuard(max_usd=0.05, max_seconds=600.0)
    decision, res = g.reserve("generate_image")  # $0.101 > $0.05 cap
    assert not decision.ok
    assert res is None
    assert g.spent_usd == 0.0  # nothing claimed on refusal


def test_reserve_prevents_stale_total_overrun() -> None:
    """Three $2.80 video reservations against a $5 cap: only the first can be
    reserved because each reserve() adds its estimate before the next checks."""
    g = BudgetGuard(max_usd=5.0, max_seconds=100_000.0)
    d0, r0 = g.reserve("generate_video")
    d1, r1 = g.reserve("generate_video")
    assert d0.ok and r0 is not None
    assert not d1.ok and r1 is None
    assert g.spent_usd == 2.80


def test_commit_reserved_settles_actual_and_returns_unspent() -> None:
    """settlement is spent += actual - estimated; a lower actual returns the
    unspent slice automatically (negative delta)."""
    g = BudgetGuard(max_usd=5.0, max_seconds=600.0)
    _, res = g.reserve("generate_video")  # reserves $2.80 / 120s
    assert g.spent_usd == 2.80 and g.spent_seconds == 120.0
    # Actual came in cheaper/faster than the estimate.
    g.commit_reserved(res, actual_usd=1.50, actual_seconds=40.0)
    assert round(g.spent_usd, 4) == 1.50
    assert round(g.spent_seconds, 2) == 40.0


def test_commit_reserved_charges_overrun() -> None:
    g = BudgetGuard(max_usd=50.0, max_seconds=600.0)
    _, res = g.reserve("generate_video")  # $2.80 / 120s
    g.commit_reserved(res, actual_usd=4.00, actual_seconds=200.0)
    assert round(g.spent_usd, 4) == 4.00
    assert round(g.spent_seconds, 2) == 200.0


def test_commit_reserved_defaults_to_estimate_when_actual_missing() -> None:
    g = BudgetGuard(max_usd=5.0, max_seconds=600.0)
    _, res = g.reserve("generate_image")  # $0.101 / 10s
    g.commit_reserved(res)  # no actuals → settles at the estimate, net zero delta
    assert round(g.spent_usd, 4) == 0.101
    assert round(g.spent_seconds, 2) == 10.0


# ── new amount-based reservation (subtask slices) ────────────────────────────


def test_reserve_amount_adds_and_returns_reservation() -> None:
    g = BudgetGuard(max_usd=5.0, max_seconds=600.0)
    decision, res = g.reserve_amount("spawn_subtasks:sub_1", usd=1.0, seconds=100.0)
    assert decision.ok and res is not None
    assert res.tool_name == "spawn_subtasks:sub_1"
    assert g.spent_usd == 1.0 and g.spent_seconds == 100.0


def test_reserve_amount_refuses_over_usd_cap() -> None:
    g = BudgetGuard(max_usd=1.0, max_seconds=600.0)
    decision, res = g.reserve_amount("slice", usd=2.0, seconds=10.0)
    assert not decision.ok and res is None
    assert "cost" in decision.reason
    assert g.spent_usd == 0.0 and g.spent_seconds == 0.0


def test_reserve_amount_refuses_over_seconds_cap() -> None:
    g = BudgetGuard(max_usd=5.0, max_seconds=100.0)
    decision, res = g.reserve_amount("slice", usd=0.0, seconds=200.0)
    assert not decision.ok and res is None
    assert "time" in decision.reason
    assert g.spent_seconds == 0.0


def test_reserve_amount_slice_roundtrip_returns_unspent() -> None:
    """The subtask flow: reserve a slice, the child spends less, settle → the
    session totals credit back exactly the unspent portion."""
    g = BudgetGuard(max_usd=5.0, max_seconds=600.0)
    _, res = g.reserve_amount("spawn_subtasks:sub_1", usd=2.0, seconds=200.0)
    assert g.spent_usd == 2.0 and g.spent_seconds == 200.0
    # Child actually spent only a fraction of its slice.
    g.commit_reserved(res, actual_usd=0.0, actual_seconds=71.2)
    assert round(g.spent_usd, 4) == 0.0
    assert round(g.spent_seconds, 2) == 71.2


def test_two_slices_sum_and_settle_independently() -> None:
    g = BudgetGuard(max_usd=5.0, max_seconds=600.0)
    _, r1 = g.reserve_amount("sub_1", usd=1.0, seconds=100.0)
    _, r2 = g.reserve_amount("sub_2", usd=1.0, seconds=100.0)
    assert g.spent_usd == 2.0 and g.spent_seconds == 200.0
    g.commit_reserved(r1, actual_usd=0.5, actual_seconds=40.0)
    g.commit_reserved(r2, actual_usd=0.5, actual_seconds=60.0)
    assert round(g.spent_usd, 4) == 1.0
    assert round(g.spent_seconds, 2) == 100.0


# ── existing behavior intact ─────────────────────────────────────────────────


def test_plain_commit_still_uses_estimate_or_actual() -> None:
    g = BudgetGuard(max_usd=5.0, max_seconds=600.0)
    g.commit("generate_image")  # estimate path
    assert round(g.spent_usd, 4) == 0.101
    g.commit("analyze_media", actual_seconds=2.0)  # actual path
    assert round(g.spent_seconds, 2) == 12.0  # 10.0 (image eta) + 2.0


def test_spawn_subtasks_cost_row_is_near_free() -> None:
    g = BudgetGuard(max_usd=5.0, max_seconds=600.0)
    usd, eta = g.estimate("spawn_subtasks")
    assert usd == 0.0
    assert eta == 1.0


def test_production_run_has_no_legacy_time_stop_or_five_dollar_paid_gate(tmp_path) -> None:
    ledger = ProductionMediaBudget(tmp_path / "budget.json", run_id="formal")
    ledger.import_baseline(import_key="echo-old-spend", amount_usd="1.525")
    guard = BudgetGuard.for_production_run(ledger)
    assert guard.max_seconds is None
    guard.spent_seconds = 100_000.0
    assert guard.check("edit_video").ok
    # Three formal 8-second Veo reservations fit the run policy even though
    # their $8.40 total would have tripped the legacy $5 session guard.
    for index in range(3):
        decision = guard.reserve_paid_media(
            "generate_video",
            idempotency_key=f"veo-{index}",
            provider="vertex",
            model="veo-3.1-fast",
            requested_duration_sec=8,
        )
        assert decision.ok
    assert ledger.snapshot()["veo_reserved_duration_sec"] == 24.0
    with pytest.raises(RuntimeError, match="reserve_paid_media"):
        guard.reserve("generate_video")
