"""Read and atomically patch RealityContract / Creative IR production facts."""
from __future__ import annotations

from typing import Any

from gemia.creative_ir import compact_creative_ir
from gemia.errors import RECOVERY_FIX_ARGS, RECOVERY_SWITCH_TOOL, ToolError
from gemia.reality_contract import MAX_MEDIA_BUDGET_USD, normalize_reality_contract
from gemia.tools._context import ToolContext


def _scope(ctx: ToolContext) -> tuple[Any, str, str]:
    store = ctx.extra.get("production_store")
    project_id = str(ctx.extra.get("project_id") or "")
    run_id = str(ctx.extra.get("run_id") or "")
    if store is None or not project_id or not run_id:
        raise ToolError(
            "design state requires a durable ProductionRun",
            code="E_PRODUCTION_REQUIRED",
            recovery=RECOVERY_SWITCH_TOOL,
        )
    return store, project_id, run_id


async def dispatch_get(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    store, project_id, run_id = _scope(ctx)
    run = store.load_run(project_id, run_id)
    contract = normalize_reality_contract(
        run.get("reality_contract")
        if isinstance(run.get("reality_contract"), dict)
        else None,
        hard_cap_usd=MAX_MEDIA_BUDGET_USD,
    )
    creative_ir = store.load_creative_ir(project_id, run_id)
    document = str(args.get("document") or "both").strip().lower()
    if document not in {"both", "reality_contract", "creative_ir"}:
        raise ToolError(
            "document must be both, reality_contract, or creative_ir",
            code="E_BAD_ARG",
            recovery=RECOVERY_FIX_ARGS,
        )
    payload: dict[str, Any] = {
        "project_id": project_id,
        "run_id": run_id,
        "project_revision": int(run.get("project_revision") or 0),
        "production_state": str(run.get("state") or "created"),
    }
    if document in {"both", "reality_contract"}:
        payload["reality_contract"] = contract
        payload["contract_revision"] = int(run.get("contract_revision") or 0)
    if document in {"both", "creative_ir"}:
        payload["creative_ir"] = compact_creative_ir(creative_ir)
    return payload


async def dispatch_patch(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    store, project_id, run_id = _scope(ctx)
    expected = args.get("expected_revision")
    if expected is not None and (
        isinstance(expected, bool) or not isinstance(expected, int) or expected < 0
    ):
        raise ToolError(
            "expected_revision must be a non-negative integer",
            code="E_BAD_ARG",
            recovery=RECOVERY_FIX_ARGS,
        )
    return store.patch_design_state(
        project_id,
        run_id,
        document=str(args.get("document") or ""),
        operation=str(args.get("operation") or ""),
        path=str(args.get("path") or ""),
        value=args.get("value"),
        expected_revision=expected,
        trace_id=str(ctx.extra.get("active_trace_id") or ""),
    )


__all__ = ["dispatch_get", "dispatch_patch"]
