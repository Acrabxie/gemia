"""First-class Pexels/Pixabay sourcing for the v3 production loop.

The lower-level implementation already knows how to search, download, and
write a provenance sidecar.  This adapter keeps those operations inside the
session/project workspace and registers downloaded media as an addressable
asset instead of making the model fall back to shell scripts.
"""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

from gemia.errors import RECOVERY_FIX_ARGS, ToolError
from gemia.production_budget import claim_paid_media_call
from gemia.tools._context import ToolContext
from gemia.video.stock_media import StockMediaError, fetch_stock_media, search_stock_media


_ACTIONS = {"search", "fetch"}
_PROVIDERS = {"auto", "pexels", "pixabay"}
_MEDIA_TYPES = {"video", "image"}


def _safe_stem(query: str) -> str:
    stem = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff._-]+", "-", query).strip("-._")
    return (stem or "stock-media")[:72]


async def dispatch(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    query = str(args.get("query") or "").strip()
    if not query:
        raise ValueError("stock_media requires a non-empty query")

    action = str(args.get("action") or "search").strip().lower()
    provider = str(args.get("provider") or "auto").strip().lower()
    media_type = str(args.get("media_type") or "video").strip().lower()
    orientation = str(args.get("orientation") or "").strip().lower() or None
    try:
        limit = max(1, min(int(args.get("limit") or 8), 20))
    except (TypeError, ValueError):
        limit = 8
    result_id = str(args.get("result_id") or "").strip() or None
    result_index = args.get("result_index")
    formal = bool(
        ctx.extra.get("production_store")
        and ctx.extra.get("project_id")
        and ctx.extra.get("run_id")
    )
    call_context = ctx.extra.get("tool_call_context")
    if formal and (
        not isinstance(call_context, dict)
        or not str(call_context.get("trace_id") or "")
        or not str(call_context.get("idempotency_key") or "")
    ):
        raise ToolError(
            "formal stock-media calls require a host trace and idempotency key",
            code="E_TRACE_REQUIRED",
            recovery=RECOVERY_FIX_ARGS,
        )

    if action not in _ACTIONS:
        raise ValueError(f"stock_media action must be one of {sorted(_ACTIONS)}")
    if provider not in _PROVIDERS:
        raise ValueError(f"stock_media provider must be one of {sorted(_PROVIDERS)}")
    if media_type not in _MEDIA_TYPES:
        raise ValueError(f"stock_media media_type must be one of {sorted(_MEDIA_TYPES)}")

    if formal and action == "fetch" and (
        provider not in {"pexels", "pixabay"} or not result_id
    ):
        raise ToolError(
            "formal stock fetch requires the exact provider and result_id returned by search",
            code="E_IDEMPOTENCY_REQUIRED",
            recovery=RECOVERY_FIX_ARGS,
        )

    paid = claim_paid_media_call(ctx.extra if isinstance(ctx.extra, dict) else None)
    paid_call, paid_ledger = paid if paid is not None else (None, None)
    if formal and (paid_call is None or paid_ledger is None):
        raise ToolError(
            "formal stock-media calls require a persisted zero-dollar budget reservation",
            code="E_BUDGET_RESERVATION_REQUIRED",
            recovery=RECOVERY_FIX_ARGS,
        )

    if formal and action == "fetch":
        for existing in ctx.registry.list_records():
            source = existing.source
            if (
                source.get("kind") == "public_stock"
                and str(source.get("provider") or "").lower() == provider
                and str(source.get("provider_asset_id") or "") == result_id
            ):
                if paid_call is not None and paid_ledger is not None:
                    paid_ledger.settle(paid_call.reservation_id, actual_usd=0)
                return {
                    "status": "noop",
                    "asset_id": existing.asset_id,
                    "provider": provider,
                    "provider_asset_id": result_id,
                    "summary": "exact stock result is already materialized in this project",
                }

    if action == "search":
        try:
            result = await asyncio.to_thread(
                search_stock_media,
                query=query,
                provider=provider,
                media_type=media_type,
                limit=limit,
                orientation=orientation,
                safe_search=True,
            )
        except BaseException as exc:
            if paid_call is not None and paid_ledger is not None:
                paid_ledger.mark_uncertain(
                    paid_call.reservation_id,
                    error=f"{type(exc).__name__}: {exc}",
                )
            raise
        if paid_call is not None and paid_ledger is not None:
            paid_ledger.settle(paid_call.reservation_id, actual_usd=0)
        items = [item for item in result.get("results") or [] if isinstance(item, dict)]
        return {
            **result,
            "result_count": len(items),
            "trace_id": str((call_context or {}).get("trace_id") or ""),
            "idempotency_key": str((call_context or {}).get("idempotency_key") or ""),
            "summary": f"found {len(items)} licensed stock {media_type} result(s) for '{query}'",
        }

    asset_id = ctx.registry.allocate_id(media_type)
    suffix = ".mp4" if media_type == "video" else ".jpg"
    stock_dir = ctx.output_dir / "stock"
    stock_dir.mkdir(parents=True, exist_ok=True)
    output_path = stock_dir / f"{asset_id}-{_safe_stem(query)}{suffix}"
    try:
        downloaded_value = await asyncio.to_thread(
            fetch_stock_media,
            "",
            str(output_path),
            query=query,
            provider=provider,
            media_type=media_type,
            limit=limit,
            orientation=orientation,
            safe_search=True,
            import_to_media_library=False,
            result_id=result_id,
            result_index=result_index,
        )
    except BaseException as exc:
        if paid_call is not None and paid_ledger is not None:
            paid_ledger.mark_uncertain(
                paid_call.reservation_id,
                error=f"{type(exc).__name__}: {exc}",
            )
        raise

    downloaded = Path(downloaded_value).expanduser().resolve()
    try:
        downloaded.relative_to(ctx.output_dir.resolve())
    except ValueError as exc:
        raise ValueError("stock_media download escaped the project workspace") from exc
    if not downloaded.is_file() or downloaded.stat().st_size <= 0:
        raise ValueError("stock_media did not produce a non-empty file")

    provenance: dict[str, Any] = {}
    sidecar = downloaded.with_suffix(downloaded.suffix + ".stock.json")
    if sidecar.is_file():
        try:
            payload = json.loads(sidecar.read_text(encoding="utf-8"))
            source = payload.get("source")
            if isinstance(source, dict):
                provenance = source
        except (OSError, ValueError, TypeError):
            provenance = {}

    provider_name = str(provenance.get("provider") or provider)
    license_name = str(provenance.get("license") or "")
    source_url = str(provenance.get("source_url") or provenance.get("page_url") or "")
    summary = f"licensed {provider_name} {media_type} for '{query}'"
    if license_name:
        summary += f" ({license_name})"
    license_url = {
        "pexels": "https://www.pexels.com/license/",
        "pixabay": "https://pixabay.com/service/license-summary/",
    }.get(provider_name.lower(), "")
    if paid_call is not None and paid_ledger is not None:
        paid_ledger.settle(
            paid_call.reservation_id,
            actual_usd=0,
            result_asset_id=asset_id,
        )
    ctx.registry.register_output(
        asset_id,
        path=downloaded,
        kind=media_type,
        summary=summary,
        source={
            "kind": "public_stock",
            "provider": provider_name,
            "provider_asset_id": str(provenance.get("id") or ""),
            "url": source_url,
            "query": query,
            "attribution": str(provenance.get("attribution") or ""),
            "trace_id": str((call_context or {}).get("trace_id") or ""),
            "idempotency_key": str((call_context or {}).get("idempotency_key") or ""),
            "request_id": paid_call.request_id if paid_call is not None else None,
            "reservation_id": paid_call.reservation_id if paid_call is not None else None,
            # Motion must be established by a revision-bound inspection; a
            # video container alone is not evidence of meaningful motion.
            "real_motion_verified": False,
        },
        license={
            "name": license_name,
            "url": license_url,
            "attribution": str(provenance.get("attribution") or ""),
        },
    )
    if paid_call is not None and paid_ledger is not None:
        registered = ctx.registry.get(asset_id)
        paid_ledger.mark_asset_materialized(
            paid_call.reservation_id,
            result_asset_id=asset_id,
            asset_path=registered.path,
            asset_sha256=registered.sha256,
        )
    return {
        "asset_id": asset_id,
        "path": str(downloaded.relative_to(ctx.output_dir)),
        "query": query,
        "provider": provider_name,
        "provider_asset_id": str(provenance.get("id") or ""),
        "media_type": media_type,
        "license": license_name,
        "source_url": source_url,
        "attribution": str(provenance.get("attribution") or ""),
        "trace_id": str((call_context or {}).get("trace_id") or ""),
        "idempotency_key": str((call_context or {}).get("idempotency_key") or ""),
        "provenance_sidecar": str(sidecar.relative_to(ctx.output_dir)) if sidecar.is_file() else "",
        "size_bytes": downloaded.stat().st_size,
        "summary": summary,
    }


__all__ = ["dispatch"]
