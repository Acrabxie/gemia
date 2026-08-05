"""Evidence-gap compiler for the durable production state machine."""
from __future__ import annotations

from typing import Any, Mapping

from gemia.reality_contract import contract_gaps


NEXT_STAGE = {
    "created": "preflight",
    "preflight": "sourcing",
    "sourcing": "rough_cut",
    "rough_cut": "sound_pass",
    "sound_pass": "visual_pass",
    "visual_pass": "rendering",
    "rendering": "verifying",
}


def stage_evidence_gaps(
    *,
    state: str,
    contract: Mapping[str, Any],
    creative_ir: Mapping[str, Any],
    facts: Mapping[str, Any],
) -> list[str]:
    """Return missing *facts* required to leave ``state``.

    Tool names are intentionally absent.  A successful API call is not proof
    that a source strategy, sequence, sound design, preview or export exists.
    """

    state = str(state or "created")
    if state == "created":
        gaps = list(contract_gaps(contract))
        if not creative_ir:
            gaps.append("creative_ir.present")
        return gaps
    if state == "preflight":
        strategy = creative_ir.get("asset_strategy")
        if not strategy and int(facts.get("asset_count") or 0) <= 0:
            return ["creative_ir.asset_strategy_or_existing_assets"]
        return []
    if state == "sourcing":
        beats = creative_ir.get("beats")
        beat_order = creative_ir.get("beat_order")
        if (
            (not isinstance(beats, Mapping) or not beats or not beat_order)
            and int(facts.get("visual_clip_count") or 0) <= 0
        ):
            return ["creative_ir.ordered_beats_or_timeline_sequence"]
        return []
    if state == "rough_cut":
        deliverable = contract.get("deliverable") if isinstance(contract.get("deliverable"), Mapping) else {}
        audio = deliverable.get("audio") if isinstance(deliverable.get("audio"), Mapping) else {}
        if not bool(audio.get("required", True)):
            return []
        systems = creative_ir.get("systems") if isinstance(creative_ir.get("systems"), Mapping) else {}
        if not systems.get("audio") and int(facts.get("audio_clip_count") or 0) <= 0:
            return ["creative_ir.systems.audio_or_timeline_audio"]
        return []
    if state == "sound_pass":
        systems = creative_ir.get("systems") if isinstance(creative_ir.get("systems"), Mapping) else {}
        visual_systems = (
            "edit",
            "motion",
            "composition",
            "typography",
            "color",
            "spatial",
            "continuity",
        )
        if (
            not any(systems.get(name) for name in visual_systems)
            and not bool(facts.get("has_lumenframe"))
            and not bool(facts.get("design_program_exists"))
        ):
            return ["creative_ir.visual_system_or_design_program"]
        return []
    if state == "visual_pass":
        if not bool(facts.get("current_preview_receipt")):
            return ["evidence.current_preview_receipt"]
        return []
    if state == "rendering":
        if not bool(facts.get("current_export_machine_passed")):
            return ["evidence.current_export_machine_passed"]
        return []
    if state == "verifying":
        if not bool(facts.get("current_acceptance_passed")):
            return ["evidence.current_acceptance_passed"]
        return []
    if state == "revising":
        if creative_ir.get("active_revision_scope"):
            return ["creative_ir.active_revision_scope_unresolved"]
        return []
    return []


def next_evidence_stage(state: str) -> str | None:
    return NEXT_STAGE.get(str(state or ""))


__all__ = ["NEXT_STAGE", "next_evidence_stage", "stage_evidence_gaps"]
