"""Budget guard for the Lumeri v3 agent loop.

The only host-side gate. Tracks per-session cumulative cost and elapsed
time; returns either ``ok`` or a fixed-limit block. The model chooses an
in-budget alternative or reports the blocker — the host does not auto-pick a
cheaper tool and an approval cannot raise the cap.

There is no capability gate, no stability gate, no approval stub. The
model holds the wheel; the host only reports real money and real time.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


_TOOL_COSTS: dict[str, dict[str, float]] = {
    # Provider-backed (real money). Numbers verified in doc 07 from
    # Google's public pricing pages (2026-05-30 snapshot).
    "generate_image":     {"usd": 0.101, "eta_sec": 10.0},   # Nano Banana 2, 2K
    "generate_video":     {"usd": 2.80,  "eta_sec": 120.0},  # Veo 3.1 fast, 8s
    "generate_audio":     {"usd": 0.00,  "eta_sec": 45.0},   # Lyria 3 clip — preview, currently free
    "analyze_media":      {"usd": 0.01,  "eta_sec": 4.0},    # rough Gemini text estimate
    # Audio analysis (zero money, time only).
    "align_audio":        {"usd": 0.00, "eta_sec": 2.0},    # cross-correlation
    "detect_beats":       {"usd": 0.00, "eta_sec": 2.0},    # librosa beat detection
    # Local ffmpeg (zero money, time only).
    "edit_image":         {"usd": 0.00, "eta_sec": 4.0},
    "edit_video":         {"usd": 0.00, "eta_sec": 10.0},
    "composite":          {"usd": 0.00, "eta_sec": 8.0},
    "color_grade":        {"usd": 0.00, "eta_sec": 8.0},
    "adjust_media":       {"usd": 0.00, "eta_sec": 6.0},
    "paint_overlay":      {"usd": 0.00, "eta_sec": 1.0},
    "paint_mask_effect":  {"usd": 0.00, "eta_sec": 12.0},
    "add_overlay":        {"usd": 0.00, "eta_sec": 6.0},
    "arrange_timeline":   {"usd": 0.00, "eta_sec": 12.0},
    "mix_audio":          {"usd": 0.00, "eta_sec": 6.0},
    "edit_audio":         {"usd": 0.00, "eta_sec": 4.0},
    "transform_geometry": {"usd": 0.00, "eta_sec": 5.0},
    "smart_reframe":      {"usd": 0.00, "eta_sec": 8.0},
    "extract_frame":      {"usd": 0.00, "eta_sec": 1.0},
    "probe_media":        {"usd": 0.00, "eta_sec": 0.2},
    "inspect_lottie":     {"usd": 0.00, "eta_sec": 1.0},
    "search_library":     {"usd": 0.00, "eta_sec": 0.5},
    "search_media":       {"usd": 0.00, "eta_sec": 0.5},
    "search_frames":      {"usd": 0.00, "eta_sec": 3.0},
    "draft_shotlist":     {"usd": 0.00, "eta_sec": 0.3},
    "set_shotlist":       {"usd": 0.00, "eta_sec": 0.2},
    "update_shot":        {"usd": 0.00, "eta_sec": 0.2},
    "get_shotlist":       {"usd": 0.00, "eta_sec": 0.1},
    "refine_shot":        {"usd": 0.00, "eta_sec": 3.0},
    "assemble_shotlist":  {"usd": 0.00, "eta_sec": 8.0},
    "narrate":            {"usd": 0.00, "eta_sec": 4.0},
    "subtitle":           {"usd": 0.00, "eta_sec": 4.0},
    "animate_captions":   {"usd": 0.00, "eta_sec": 5.0},
    "annotate_media":     {"usd": 0.00, "eta_sec": 8.0},
    "get_media_annotations": {"usd": 0.00, "eta_sec": 0.2},
    "write_media_annotation": {"usd": 0.00, "eta_sec": 0.2},
    "export":             {"usd": 0.00, "eta_sec": 20.0},
    # M3 verbs (host-side internet + sandbox bash).
    "web_search":         {"usd": 0.00, "eta_sec": 3.0},     # Host-side search, compact results
    "web_open":           {"usd": 0.00, "eta_sec": 5.0},     # Host-side page text extraction
    "fetch":              {"usd": 0.00, "eta_sec": 5.0},     # Host-side https download
    "run_shell":          {"usd": 0.00, "eta_sec": 10.0},    # Sandboxed bash execution
    "file_list":          {"usd": 0.00, "eta_sec": 0.2},     # Host-side file inspection
    "file_read":          {"usd": 0.00, "eta_sec": 0.2},
    "file_write":         {"usd": 0.00, "eta_sec": 0.2},
    "file_copy":          {"usd": 0.00, "eta_sec": 0.5},
    "file_move":          {"usd": 0.00, "eta_sec": 0.5},
    "file_delete":        {"usd": 0.00, "eta_sec": 0.2},
    # M4 verbs (async code execution + job polling + skill persistence).
    "build":              {"usd": 0.00, "eta_sec": 15.0},    # Async Python sandbox submit
    "check_job":          {"usd": 0.00, "eta_sec": 0.5},     # Job status poll
    "wait_for_job":       {"usd": 0.00, "eta_sec": 30.0},    # Blocking job wait
    "save_skill":         {"usd": 0.00, "eta_sec": 0.5},     # Skill file copy + metadata
    # Timeline v1 verbs (document mutation = pure in-process patch; near-free).
    "get_timeline":             {"usd": 0.00, "eta_sec": 0.2},
    "timeline_insert_clip":     {"usd": 0.00, "eta_sec": 0.5},
    "timeline_delete_clip":     {"usd": 0.00, "eta_sec": 0.2},
    "timeline_move_clip":       {"usd": 0.00, "eta_sec": 0.2},
    "timeline_trim_clip":       {"usd": 0.00, "eta_sec": 0.2},
    "timeline_split_clip":      {"usd": 0.00, "eta_sec": 0.2},
    "timeline_set_clip_time":   {"usd": 0.00, "eta_sec": 0.2},
    "timeline_add_transition":  {"usd": 0.00, "eta_sec": 0.2},
    "timeline_set_clip_effects": {"usd": 0.00, "eta_sec": 0.2},
    "timeline_add_track":       {"usd": 0.00, "eta_sec": 0.2},
    "timeline_set_track":       {"usd": 0.00, "eta_sec": 0.2},
    "timeline_undo":            {"usd": 0.00, "eta_sec": 0.2},
    "inspect_timeline":         {"usd": 0.00, "eta_sec": 12.0},  # render proxy + sampled frames
    "get_safe_areas":           {"usd": 0.00, "eta_sec": 0.1},
    "render_preview":           {"usd": 0.00, "eta_sec": 20.0},  # ffmpeg low-res proxy
    "project_export":           {"usd": 0.00, "eta_sec": 60.0},  # full-quality multi-track export
    # Lumenframe layer/time verbs (pure document patch unless rendering).
    "get_lumenframe":           {"usd": 0.00, "eta_sec": 0.2},
    "lumen_patch":              {"usd": 0.00, "eta_sec": 0.2},
    "lumen_add_layer":          {"usd": 0.00, "eta_sec": 0.2},
    "lumen_set_transform":      {"usd": 0.00, "eta_sec": 0.2},
    "lumen_set_opacity":        {"usd": 0.00, "eta_sec": 0.2},
    "lumen_delete_layer":       {"usd": 0.00, "eta_sec": 0.2},
    "lumen_move_layer":         {"usd": 0.00, "eta_sec": 0.2},
    "lumen_set_visibility":     {"usd": 0.00, "eta_sec": 0.2},
    "lumen_select":             {"usd": 0.00, "eta_sec": 0.2},
    "lumen_set_mask":           {"usd": 0.00, "eta_sec": 0.2},
    "lumen_key":                {"usd": 0.00, "eta_sec": 0.2},
    "lumen_set_range":          {"usd": 0.00, "eta_sec": 0.2},
    "lumen_set_lane":           {"usd": 0.00, "eta_sec": 0.2},
    "lumen_retime_segment":     {"usd": 0.00, "eta_sec": 0.2},
    "lumen_reverse":            {"usd": 0.00, "eta_sec": 0.2},
    "lumen_time_remap":         {"usd": 0.00, "eta_sec": 0.2},
    "lumen_speed_ramp":         {"usd": 0.00, "eta_sec": 0.2},
    "lumen_ripple_delete":      {"usd": 0.00, "eta_sec": 0.2},
    "lumen_merge_compositions": {"usd": 0.00, "eta_sec": 0.2},
    "lumen_set_work_area":      {"usd": 0.00, "eta_sec": 0.2},
    "lumen_render":             {"usd": 0.00, "eta_sec": 20.0},
    "lumen_seek":               {"usd": 0.00, "eta_sec": 1.0},
    "lumen_render_range":       {"usd": 0.00, "eta_sec": 5.0},
    # Multi-agent fan-out: the verb itself is near-free orchestration; the real
    # cost of children flows through per-child budget reservations (see
    # gemia/subtasks.py + docs/multi-agent-plan.md §5). The loop special-cases
    # spawn_subtasks to commit actual_seconds=0.0 so the batch wall-clock is not
    # double-counted on top of the children's settled seconds (§5.3).
    "spawn_subtasks":           {"usd": 0.00, "eta_sec": 1.0},
}


@dataclass
class BudgetDecision:
    ok: bool
    estimated_cost_usd: float
    estimated_eta_sec: float
    reason: str = ""
    alternatives: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "estimated_cost_usd": self.estimated_cost_usd,
            "estimated_eta_sec": self.estimated_eta_sec,
            "reason": self.reason,
            "alternatives": list(self.alternatives),
        }


@dataclass(frozen=True)
class BudgetReservation:
    """A budget slice claimed up front by ``reserve()`` / ``reserve_amount()``.

    Salvaged from the stranded RC3 parallel-dispatch branch
    (``d7f941c:gemia/budget_guard.py:84``). ``tool_name`` is a free-form label
    for snapshots/logs (it is a real tool name for ``reserve()``, and a label
    like ``"spawn_subtasks:sub_1"`` for ``reserve_amount()``); the settlement
    math in ``commit_reserved`` never looks at it.
    """

    tool_name: str
    estimated_cost_usd: float
    estimated_eta_sec: float


class BudgetGuard:
    """Per-session cumulative cost + time tracker."""

    def __init__(self, *, max_usd: float = 5.0, max_seconds: float = 600.0) -> None:
        self.max_usd = float(max_usd)
        self.max_seconds = float(max_seconds)
        self.spent_usd = 0.0
        self.spent_seconds = 0.0
        self._started_at = time.monotonic()

    def estimate(self, tool_name: str) -> tuple[float, float]:
        entry = _TOOL_COSTS.get(tool_name)
        if entry is None:
            return 0.0, 5.0
        return float(entry["usd"]), float(entry["eta_sec"])

    def check(self, tool_name: str) -> BudgetDecision:
        cost, eta = self.estimate(tool_name)
        projected_usd = self.spent_usd + cost
        # Use cumulative tool-execution time (spent_seconds), not wall-clock elapsed time.
        # This is symmetric with cost accounting: both measure actual committed resources,
        # not idle/streaming/waiting time. Idle time is borne by the host, not the session.
        projected_sec = self.spent_seconds + eta
        if projected_usd > self.max_usd:
            return BudgetDecision(
                ok=False,
                estimated_cost_usd=cost,
                estimated_eta_sec=eta,
                reason=f"session cost would exceed cap: ${projected_usd:.2f} > ${self.max_usd:.2f}",
                alternatives=_cheaper(tool_name),
            )
        if projected_sec > self.max_seconds:
            return BudgetDecision(
                ok=False,
                estimated_cost_usd=cost,
                estimated_eta_sec=eta,
                reason=f"session time would exceed cap: {projected_sec:.0f}s > {self.max_seconds:.0f}s",
                alternatives=_cheaper(tool_name),
            )
        return BudgetDecision(ok=True, estimated_cost_usd=cost, estimated_eta_sec=eta)

    def reserve(self, tool_name: str) -> tuple[BudgetDecision, BudgetReservation | None]:
        """Atomically check and reserve the estimated budget for a future dispatch.

        Parallel tool execution must account for estimates before launching work,
        otherwise N concurrent calls can all pass ``check()`` against the same
        stale totals and collectively exceed the session caps. The caller settles
        the reservation with ``commit_reserved()`` after the call finishes.

        No lock is needed: all callers live on the session event loop and there is
        no ``await`` between the ``check()`` read and the ``spent_*`` add, so the
        check-then-add is atomic (see docs/multi-agent-plan.md §5.1).
        """
        decision = self.check(tool_name)
        if not decision.ok:
            return decision, None
        reservation = BudgetReservation(
            tool_name=tool_name,
            estimated_cost_usd=decision.estimated_cost_usd,
            estimated_eta_sec=decision.estimated_eta_sec,
        )
        self.spent_usd += reservation.estimated_cost_usd
        self.spent_seconds += reservation.estimated_eta_sec
        return decision, reservation

    def reserve_amount(
        self, label: str, *, usd: float, seconds: float
    ) -> tuple[BudgetDecision, BudgetReservation | None]:
        """Amount-based reservation for host capabilities (e.g. subtask slices)
        that are not a single ``_TOOL_COSTS`` row.

        Same atomic check-then-add as ``reserve()``, but the caller supplies the
        amounts directly. Refuses (and reserves nothing) if the requested slice
        would push either axis past the cap. ``label`` is echoed onto the
        reservation for snapshots/logs only.
        """
        usd = float(usd)
        seconds = float(seconds)
        projected_usd = self.spent_usd + usd
        projected_sec = self.spent_seconds + seconds
        if projected_usd > self.max_usd:
            return (
                BudgetDecision(
                    ok=False,
                    estimated_cost_usd=usd,
                    estimated_eta_sec=seconds,
                    reason=(
                        f"session cost would exceed cap: "
                        f"${projected_usd:.2f} > ${self.max_usd:.2f}"
                    ),
                ),
                None,
            )
        if projected_sec > self.max_seconds:
            return (
                BudgetDecision(
                    ok=False,
                    estimated_cost_usd=usd,
                    estimated_eta_sec=seconds,
                    reason=(
                        f"session time would exceed cap: "
                        f"{projected_sec:.0f}s > {self.max_seconds:.0f}s"
                    ),
                ),
                None,
            )
        reservation = BudgetReservation(
            tool_name=label,
            estimated_cost_usd=usd,
            estimated_eta_sec=seconds,
        )
        self.spent_usd += usd
        self.spent_seconds += seconds
        return (
            BudgetDecision(ok=True, estimated_cost_usd=usd, estimated_eta_sec=seconds),
            reservation,
        )

    def commit_reserved(
        self,
        reservation: BudgetReservation,
        *,
        actual_usd: float | None = None,
        actual_seconds: float | None = None,
    ) -> None:
        """Settle a prior reservation with actual observed resource usage.

        ``spent += actual - estimated`` on each axis. An unspent slice returns
        automatically: a lower actual produces a negative delta that credits the
        session totals back down. Falls back to the reserved estimate when an
        axis's actual is not supplied.
        """
        actual_cost = (
            float(actual_usd)
            if actual_usd is not None
            else reservation.estimated_cost_usd
        )
        actual_sec = (
            float(actual_seconds)
            if actual_seconds is not None
            else reservation.estimated_eta_sec
        )
        self.spent_usd += actual_cost - reservation.estimated_cost_usd
        self.spent_seconds += actual_sec - reservation.estimated_eta_sec

    def commit(
        self,
        tool_name: str,
        *,
        actual_usd: float | None = None,
        actual_seconds: float | None = None,
    ) -> None:
        cost, eta = self.estimate(tool_name)
        self.spent_usd += float(actual_usd) if actual_usd is not None else cost
        self.spent_seconds += float(actual_seconds) if actual_seconds is not None else eta

    def snapshot(self) -> dict[str, Any]:
        return {
            "max_usd": self.max_usd,
            "max_seconds": self.max_seconds,
            "spent_usd": round(self.spent_usd, 4),
            "spent_seconds": round(self.spent_seconds, 2),
            "elapsed_seconds": round(time.monotonic() - self._started_at, 2),
        }


def tool_cost_usd(tool_name: str) -> float:
    """Module-level cost lookup for callers that need the estimate without a
    BudgetGuard instance (e.g. the generate_image dispatcher's audit record)."""
    entry = _TOOL_COSTS.get(tool_name)
    return float(entry["usd"]) if entry else 0.0


def _cheaper(tool_name: str) -> list[str]:
    if tool_name == "generate_video":
        return ["search_library", "generate_image"]
    if tool_name in {"generate_image", "generate_audio"}:
        return ["search_library"]
    return []


__all__ = ["BudgetGuard", "BudgetDecision", "BudgetReservation", "tool_cost_usd"]
