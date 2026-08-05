"""Revision-bound production acceptance over the actual preview/export graph."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from gemia.errors import RECOVERY_FIX_ARGS, RECOVERY_SWITCH_TOOL, ToolError
from gemia.production_acceptance import evaluate_delivery
from gemia.reality_contract import MAX_MEDIA_BUDGET_USD, normalize_reality_contract
from gemia.render_receipt import (
    CANONICAL_RENDER_SEMANTICS_VERSION,
    canonical_render_semantics,
    file_sha256,
)
from gemia.tools._context import AssetRecord, ToolContext


def _record(ctx: ToolContext, asset_id: str, *, kind: str) -> AssetRecord:
    try:
        record = ctx.registry.get(str(asset_id))
    except KeyError as exc:
        raise ToolError(
            f"delivery evidence asset is missing: {asset_id}",
            code="E_NOT_FOUND",
            recovery=RECOVERY_FIX_ARGS,
        ) from exc
    if record.kind != kind:
        raise ToolError(
            f"delivery evidence asset {asset_id} must be {kind}, got {record.kind}",
            code="E_BAD_ARG",
            recovery=RECOVERY_FIX_ARGS,
        )
    return record


def _receipt(record: AssetRecord, *, role: str) -> dict[str, Any]:
    value = record.source.get("render_receipt")
    if not isinstance(value, dict):
        raise ToolError(
            f"{role} asset is not backed by a canonical RenderReceipt",
            code="E_EVIDENCE_MISSING",
            recovery=RECOVERY_SWITCH_TOOL,
            hint="Render it again through render_preview/project_export.",
        )
    return dict(value)


def _receipt_revision(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return -1


def _validate_receipt_before_review(
    record: AssetRecord,
    receipt: dict[str, Any],
    *,
    role: str,
    project_revision: int,
    require_machine_pass: bool,
) -> None:
    """Fail closed before subjective evidence can advance production state."""

    semantics_version = _receipt_revision(receipt.get("render_semantics_version"))
    if (
        semantics_version != CANONICAL_RENDER_SEMANTICS_VERSION
        or receipt.get("render_semantics") != canonical_render_semantics()
    ):
        raise ToolError(
            f"{role} receipt was produced by stale render semantics",
            code="E_STALE_EVIDENCE",
            recovery=RECOVERY_SWITCH_TOOL,
            hint="Render the current project again before formal review.",
        )

    receipt_revision = _receipt_revision(receipt.get("project_revision"))
    if receipt_revision != project_revision:
        raise ToolError(
            f"{role} receipt is stale: expected revision {project_revision}, "
            f"got {receipt.get('project_revision')!r}",
            code="E_STALE_EVIDENCE",
            recovery=RECOVERY_SWITCH_TOOL,
            hint="Render the current project revision again before formal review.",
        )

    machine_status = str(receipt.get("machine_status") or "")
    machine_blockers = receipt.get("machine_blockers")
    if not isinstance(machine_blockers, list) or machine_blockers:
        raise ToolError(
            f"{role} receipt has unresolved machine blockers",
            code="E_DELIVERY_GATE",
            recovery=RECOVERY_SWITCH_TOOL,
        )
    if require_machine_pass and machine_status != "passed":
        raise ToolError(
            f"{role} machine gate did not pass: {machine_status or 'missing'}",
            code="E_DELIVERY_GATE",
            recovery=RECOVERY_SWITCH_TOOL,
        )
    # Draft previews intentionally skip the full decode and can therefore be
    # provisional.  A rejected or malformed preview is never review evidence.
    if not require_machine_pass and machine_status not in {"passed", "provisional"}:
        raise ToolError(
            f"{role} machine status is not reviewable: {machine_status or 'missing'}",
            code="E_DELIVERY_GATE",
            recovery=RECOVERY_SWITCH_TOOL,
        )

    output = record.path.expanduser()
    if not output.exists() or not output.is_file():
        raise ToolError(
            f"{role} file is missing: {output}",
            code="E_EVIDENCE_MISSING",
            recovery=RECOVERY_SWITCH_TOOL,
            hint="Render the current project again before formal review.",
        )
    try:
        record_path = output.resolve(strict=True)
        receipt_path_raw = str(receipt.get("output_path") or "").strip()
        receipt_path = (
            Path(receipt_path_raw).expanduser().resolve(strict=True)
            if receipt_path_raw
            else None
        )
        actual_sha256 = file_sha256(record_path)
    except OSError as exc:
        raise ToolError(
            f"{role} file cannot be read for integrity verification",
            code="E_EVIDENCE_MISSING",
            recovery=RECOVERY_SWITCH_TOOL,
            detail=f"{type(exc).__name__}: {exc}",
        ) from exc
    if receipt_path is None or receipt_path != record_path:
        raise ToolError(
            f"{role} receipt and AssetRecord point to different files",
            code="E_STALE_EVIDENCE",
            recovery=RECOVERY_SWITCH_TOOL,
        )
    receipt_sha256 = str(receipt.get("output_sha256") or "").lower()
    asset_sha256 = str(record.sha256 or "").lower()
    if (
        not receipt_sha256
        or not asset_sha256
        or not (actual_sha256 == receipt_sha256 == asset_sha256)
    ):
        raise ToolError(
            f"{role} file hash no longer matches its RenderReceipt and AssetRecord",
            code="E_STALE_EVIDENCE",
            recovery=RECOVERY_SWITCH_TOOL,
            hint="Render the current project again; do not reuse modified output bytes.",
        )


def _commit_review_master(
    *,
    store: Any,
    project_id: str,
    run_id: str,
    project_revision: int,
    export_record: AssetRecord,
    export_receipt: dict[str, Any],
    evidence_id: str,
    trace_id: str,
) -> dict[str, Any]:
    probe = export_receipt.get("probe")
    duration = probe.get("duration") if isinstance(probe, dict) else None
    return store.record_deliverable(
        project_id,
        run_id,
        asset_id=export_record.asset_id,
        project_revision=project_revision,
        sha256=str(export_record.sha256 or ""),
        graph_hash=str(export_receipt.get("graph_hash") or ""),
        render_id=str(export_receipt.get("render_id") or ""),
        render_semantics_version=int(
            export_receipt.get("render_semantics_version") or 0
        ),
        evidence_id=evidence_id,
        duration_sec=float(duration) if duration is not None else None,
        trace_id=trace_id,
    )


def backfill_current_delivery(
    *,
    store: Any,
    registry: Any,
    project_id: str,
    run_id: str,
    trace_id: str = "delivery-backfill",
) -> dict[str, Any]:
    """Bind an already-passed current export without re-rendering or charging.

    This is intentionally explicit: it reads the current complete acceptance
    Evidence, then re-validates the exact AssetRecord, RenderReceipt, path and
    bytes before allowing ``record_deliverable`` to advance only the run's
    production-fact revision.
    """

    project_revision = int(store.load_project(project_id).get("revision") or 0)
    run = store.load_run(project_id, run_id)
    if str(run.get("state") or "") not in {"ready_for_review", "accepted"}:
        raise ToolError(
            "delivery backfill requires an already reviewable ProductionRun",
            code="E_STATE_TRANSITION",
            recovery=RECOVERY_SWITCH_TOOL,
        )
    evidence = store._current_machine_evidence(  # noqa: SLF001 - same boundary
        project_id,
        run_id,
        run=run,
    )
    if not isinstance(evidence, dict):
        raise ToolError(
            "delivery backfill requires complete current production acceptance evidence",
            code="E_EVIDENCE_MISSING",
            recovery=RECOVERY_SWITCH_TOOL,
        )
    payload = evidence.get("payload")
    if not isinstance(payload, dict):
        raise ToolError(
            "delivery acceptance evidence is malformed",
            code="E_EVIDENCE_MISSING",
            recovery=RECOVERY_SWITCH_TOOL,
        )
    export_record = _record_from_registry(
        registry,
        str(payload.get("export_asset_id") or ""),
        kind="video",
    )
    export_receipt = _receipt(export_record, role="export")
    _validate_receipt_before_review(
        export_record,
        export_receipt,
        role="export",
        project_revision=project_revision,
        require_machine_pass=True,
    )
    return _commit_review_master(
        store=store,
        project_id=project_id,
        run_id=run_id,
        project_revision=project_revision,
        export_record=export_record,
        export_receipt=export_receipt,
        evidence_id=str(evidence.get("evidence_id") or ""),
        trace_id=trace_id,
    )


def _record_from_registry(registry: Any, asset_id: str, *, kind: str) -> AssetRecord:
    try:
        record = registry.get(str(asset_id))
    except KeyError as exc:
        raise ToolError(
            f"delivery evidence asset is missing: {asset_id}",
            code="E_NOT_FOUND",
            recovery=RECOVERY_FIX_ARGS,
        ) from exc
    if record.kind != kind:
        raise ToolError(
            f"delivery evidence asset {asset_id} must be {kind}, got {record.kind}",
            code="E_BAD_ARG",
            recovery=RECOVERY_FIX_ARGS,
        )
    return record


async def dispatch(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    store = ctx.extra.get("production_store")
    project_id = str(ctx.extra.get("project_id") or "")
    run_id = str(ctx.extra.get("run_id") or "")
    if store is None or not project_id or not run_id or ctx.project is None:
        raise ToolError(
            "verify_delivery requires a durable ProductionRun",
            code="E_PRODUCTION_REQUIRED",
            recovery=RECOVERY_SWITCH_TOOL,
        )

    project_record = store.load_project(project_id)
    project_revision = int(project_record.get("revision") or 0)
    run = store.load_run(project_id, run_id)
    contract = normalize_reality_contract(
        run.get("reality_contract")
        if isinstance(run.get("reality_contract"), dict)
        else None,
        hard_cap_usd=MAX_MEDIA_BUDGET_USD,
    )
    acceptance = contract.get("acceptance") or {}
    inspection_min = int(acceptance.get("review_sample_frames_min") or 1)
    review_names = tuple(
        str(value)
        for value in (acceptance.get("agent_review_checks") or [])
        if str(value)
    )
    if str(run.get("state") or "") != "verifying":
        raise ToolError(
            f"delivery can only be verified in state 'verifying', got {run.get('state')!r}",
            code="E_STATE_TRANSITION",
            recovery=RECOVERY_SWITCH_TOOL,
        )

    def transition_to(target: str, *, trace_id: str) -> dict[str, Any]:
        transition = ctx.extra.get("transition_production")
        if callable(transition):
            return transition(target, trace_id=trace_id)
        return store.transition_run(project_id, run_id, target, trace_id=trace_id)

    export_record = _record(ctx, str(args.get("export_asset_id") or ""), kind="video")
    preview_record = _record(ctx, str(args.get("preview_asset_id") or ""), kind="video")
    export_receipt: dict[str, Any] = {}
    preview_receipt: dict[str, Any] = {}
    try:
        export_receipt = _receipt(export_record, role="export")
        preview_receipt = _receipt(preview_record, role="preview")
        _validate_receipt_before_review(
            export_record,
            export_receipt,
            role="export",
            project_revision=project_revision,
            require_machine_pass=True,
        )
        _validate_receipt_before_review(
            preview_record,
            preview_receipt,
            role="preview",
            project_revision=project_revision,
            require_machine_pass=False,
        )
    except ToolError as exc:
        # A bad receipt is a formal machine-gate failure, not an exceptional
        # limbo state.  Persist it and move to revising without running the
        # subjective review evaluator over stale or modified bytes.
        preflight_report = {
            "schema": "lumeri.production-acceptance",
            "version": 1,
            "project_revision": project_revision,
            "render_id": str(export_receipt.get("render_id") or ""),
            "graph_hash": str(export_receipt.get("graph_hash") or ""),
            "ready_for_review": False,
            "checks": [
                {
                    "code": "receipt_preflight",
                    "ok": False,
                    "actual": exc.user_message,
                    "expected": "current, passed, revision-bound, hash-matching media",
                }
            ],
            "blockers": [
                {
                    "code": exc.code,
                    "phase": "receipt_preflight",
                    "detail": exc.user_message,
                }
            ],
            "human_review_required": True,
        }
        evidence = store.add_evidence(
            project_id,
            run_id,
            kind="production_acceptance",
            payload={
                "acceptance_report": preflight_report,
                "export_asset_id": export_record.asset_id,
                "preview_asset_id": preview_record.asset_id,
                "inspection_asset_ids": [],
            },
            project_revision=project_revision,
            trace_id=str(ctx.extra.get("active_trace_id") or ""),
        )
        transitioned = transition_to("revising", trace_id=evidence["evidence_id"])
        exc.detail = (
            f"{exc.detail}; acceptance_evidence_id={evidence['evidence_id']}; "
            f"production_state={transitioned.get('state')}"
        )
        raise

    inspection_ids = [
        str(value) for value in (args.get("inspection_asset_ids") or []) if str(value)
    ]
    if len(set(inspection_ids)) < inspection_min:
        raise ToolError(
            "formal review requires at least "
            f"{inspection_min} distinct frames sampled from the canonical graph",
            code="E_EVIDENCE_MISSING",
            recovery=RECOVERY_FIX_ARGS,
        )
    graph_hash = str(export_receipt.get("graph_hash") or "")
    for asset_id in inspection_ids:
        frame = _record(ctx, asset_id, kind="image")
        if (
            str(frame.source.get("graph_hash") or "") != graph_hash
            or int(frame.source.get("project_revision") or -1) != project_revision
        ):
            raise ToolError(
                f"inspection frame {asset_id} is stale or belongs to another render graph",
                code="E_STALE_EVIDENCE",
                recovery=RECOVERY_SWITCH_TOOL,
            )

    review_checks = args.get("review_checks")
    review_notes = args.get("review_notes")
    if not isinstance(review_checks, dict) or not isinstance(review_notes, dict):
        raise ToolError(
            "review_checks and review_notes must be objects",
            code="E_BAD_ARG",
            recovery=RECOVERY_FIX_ARGS,
        )
    structured_checks: dict[str, dict[str, Any]] = {}
    for name in review_names:
        note = str(review_notes.get(name) or "").strip()
        if name not in review_checks or not note:
            raise ToolError(
                f"review evidence for {name!r} needs a boolean result and a concrete note",
                code="E_EVIDENCE_MISSING",
                recovery=RECOVERY_FIX_ARGS,
            )
        structured_checks[name] = {
            "status": "passed" if review_checks.get(name) is True else "failed",
            "note": note[:1000],
            "inspection_asset_ids": inspection_ids,
        }

    report = evaluate_delivery(
        project=ctx.project.load(),
        render_receipt=export_receipt,
        asset_records=ctx.registry.to_dict(),
        budget_snapshot=store.media_budget(project_id, run_id).snapshot(),
        evidence={"review_checks": structured_checks},
        preview_receipt=preview_receipt,
        reality_contract=contract,
    )
    evidence = store.add_evidence(
        project_id,
        run_id,
        kind="production_acceptance",
        payload={
            "acceptance_report": report,
            "export_asset_id": export_record.asset_id,
            "preview_asset_id": preview_record.asset_id,
            "inspection_asset_ids": inspection_ids,
        },
        project_revision=project_revision,
        trace_id=str(ctx.extra.get("active_trace_id") or ""),
    )

    target = "ready_for_review" if report["ready_for_review"] else "revising"
    delivery_result: dict[str, Any] | None = None
    if report["ready_for_review"]:
        delivery_result = _commit_review_master(
            store=store,
            project_id=project_id,
            run_id=run_id,
            project_revision=project_revision,
            export_record=export_record,
            export_receipt=export_receipt,
            evidence_id=evidence["evidence_id"],
            trace_id=str(ctx.extra.get("active_trace_id") or ""),
        )
    transitioned = transition_to(target, trace_id=evidence["evidence_id"])

    payload: dict[str, Any] = {
        "acceptance_report": report,
        "evidence_id": evidence["evidence_id"],
        "project_revision": project_revision,
        "production_state": transitioned.get("state"),
        "export_asset_id": export_record.asset_id,
    }
    if delivery_result is not None:
        payload["delivery"] = store.public_delivery(project_id, run_id)
    if not report["ready_for_review"]:
        payload.update(
            {
                "status": "failed",
                "error": "the current revision failed the formal delivery gate",
                "error_code": "E_DELIVERY_GATE",
                "recovery": RECOVERY_SWITCH_TOOL,
            }
        )
    return payload


__all__ = ["backfill_current_delivery", "dispatch"]
