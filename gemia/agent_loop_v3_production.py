"""Read-only production and routing state for AgentLoopV3."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


class AgentLoopProductionMixin:
    def _routing_state(self) -> dict[str, Any]:
        pending = {
            record.job_id: record.last_polled_status
            for record in self._tool_ctx.jobs.list_pending()
        }
        try:
            lumen_text = self._get_lumenframe_prompt_text()
            has_lumenframe = bool(lumen_text and not lumen_text.startswith("("))
        except Exception:  # noqa: BLE001 — routing hints are best effort
            has_lumenframe = False
        try:
            project_state = self.project.load()
            timeline = (project_state or {}).get("timeline") or {}
            has_timeline = bool(timeline.get("clips") or float(timeline.get("duration") or 0) > 0)
        except Exception:  # noqa: BLE001
            has_timeline = False
        production = self._production_status()
        return {
            "has_assets": bool(self.registry.list_records()),
            "has_timeline": has_timeline,
            "has_lumenframe": has_lumenframe,
            "pending_jobs": pending,
            "production_state": production.get("production_state"),
            "production_blockers": production.get("blockers", []),
        }

    def _production_status(self) -> dict[str, Any]:
        store = self._tool_ctx.extra.get("production_store")
        project_id = str(self._tool_ctx.extra.get("project_id") or "")
        run_id = str(self._tool_ctx.extra.get("run_id") or "")
        if store is None or not project_id or not run_id:
            return {}
        try:
            project = store.load_project(project_id)
            run = store.load_run(project_id, run_id)
            from gemia.creative_ir import compact_creative_ir
            from gemia.production_evidence import stage_evidence_gaps
            from gemia.reality_contract import (
                MAX_MEDIA_BUDGET_USD,
                contract_gaps,
                normalize_reality_contract,
            )

            contract = normalize_reality_contract(
                run.get("reality_contract")
                if isinstance(run.get("reality_contract"), dict)
                else None,
                hard_cap_usd=MAX_MEDIA_BUDGET_USD,
            )
            creative_ir = store.load_creative_ir(project_id, run_id)
            facts = self._production_facts(
                store=store,
                project_id=project_id,
                run_id=run_id,
                project_revision=int(project.get("revision") or 0),
                creative_ir=creative_ir,
            )
            state = str(run.get("state") or "created")
            return {
                "project_id": project_id,
                "run_id": run_id,
                "project_revision": int(project.get("revision") or 0),
                "production_revision": int(run.get("revision") or 0),
                "production_state": state,
                "blockers": list(run.get("blockers") or []),
                "reality_contract": contract,
                "contract_gaps": contract_gaps(contract),
                "creative_ir": compact_creative_ir(creative_ir),
                "evidence_facts": facts,
                "evidence_gaps": stage_evidence_gaps(
                    state=state,
                    contract=contract,
                    creative_ir=creative_ir,
                    facts=facts,
                ),
                "review": run.get("review"),
            }
        except Exception:  # noqa: BLE001 — routing/telemetry must fail open
            return {
                "project_id": project_id or None,
                "run_id": run_id or None,
            }

    def _production_facts(
        self,
        *,
        store: Any,
        project_id: str,
        run_id: str,
        project_revision: int,
        creative_ir: Mapping[str, Any],
    ) -> dict[str, Any]:
        try:
            project_state = self.project.load()
        except Exception:
            project_state = {}
        timeline = (
            project_state.get("timeline") if isinstance(project_state.get("timeline"), dict) else {}
        )
        tracks = {
            str(track.get("id") or ""): str(track.get("kind") or "")
            for track in (timeline.get("tracks") or [])
            if isinstance(track, dict)
        }
        visual_clip_count = 0
        audio_clip_count = 0
        for clip in timeline.get("clips") or []:
            if not isinstance(clip, dict) or not bool(clip.get("enabled", True)):
                continue
            kind = tracks.get(str(clip.get("track_id") or ""))
            if kind == "video" and str(clip.get("media_kind") or "") in {"video", "image"}:
                visual_clip_count += 1
            elif kind == "audio" or str(clip.get("media_kind") or "") == "audio":
                audio_clip_count += 1

        current_preview = False
        current_export_passed = False
        records = self.registry.list_records()
        for record in records:
            receipt = record.source.get("render_receipt")
            if (
                not isinstance(receipt, dict)
                or int(receipt.get("project_revision") or -1) != project_revision
            ):
                continue
            source_kind = str(record.source.get("kind") or "")
            if source_kind == "derived_preview" and str(receipt.get("machine_status") or "") in {
                "passed",
                "provisional",
            }:
                current_preview = True
            if (
                source_kind == "derived_export"
                and str(receipt.get("machine_status") or "") == "passed"
                and not receipt.get("machine_blockers")
            ):
                current_export_passed = True

        entrypoint = str((creative_ir.get("program") or {}).get("entrypoint") or "")
        design_program_exists = False
        if entrypoint.startswith("project://design/"):
            relative = entrypoint[len("project://design/") :]
            candidate = (
                self.project.store.project_dir(self.project.project_id) / "design" / relative
            )
            try:
                design_program_exists = (
                    candidate.resolve().is_relative_to(
                        (
                            self.project.store.project_dir(self.project.project_id) / "design"
                        ).resolve()
                    )
                    and candidate.is_file()
                )
            except (OSError, RuntimeError, ValueError):
                design_program_exists = False
        return {
            "asset_count": len(records),
            "visual_clip_count": visual_clip_count,
            "audio_clip_count": audio_clip_count,
            "timeline_duration_sec": timeline.get("duration"),
            "has_lumenframe": bool(
                self._get_lumenframe_prompt_text()
                and not self._get_lumenframe_prompt_text().startswith("(")
            ),
            "design_program_exists": design_program_exists,
            "current_preview_receipt": current_preview,
            "current_export_machine_passed": current_export_passed,
            "current_acceptance_passed": bool(
                store._has_current_machine_evidence(project_id, run_id)  # noqa: SLF001
            ),
        }


__all__ = ["AgentLoopProductionMixin"]
