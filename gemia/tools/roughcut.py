"""Agent verb for local non-timeline rough-cut preparation."""
from __future__ import annotations

import asyncio
from typing import Any

from gemia.tools._context import ProgressUpdate, ToolContext


def _account_id(ctx: ToolContext) -> str:
    explicit = str(ctx.extra.get("account_id") or "").strip()
    if explicit:
        return explicit
    try:
        from gemia import accounts

        return str(accounts.current_account_id() or "").strip()
    except Exception:
        return ""


async def dispatch(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    account_id = _account_id(ctx)
    if not account_id:
        raise ValueError("prepare_roughcut requires a signed-in account")
    from gemia.roughcut import prepare_roughcut

    asset_ids = args.get("asset_ids") or []
    if isinstance(asset_ids, str):
        asset_ids = [asset_ids]
    if not isinstance(asset_ids, list):
        raise ValueError("prepare_roughcut asset_ids must be a list")

    def _progress(update: dict[str, Any]) -> None:
        ctx.emit_progress(
            ProgressUpdate(
                percent=float(update.get("percent") or 0.0),
                message=str(update.get("message") or "preparing rough cut"),
            )
        )

    return await asyncio.to_thread(
        prepare_roughcut,
        account_id,
        [str(item) for item in asset_ids],
        all_assets=bool(args.get("all")),
        language=str(args.get("language") or "auto"),
        create_proxies=bool(args.get("create_proxies", True)),
        proxy_resolution=int(args.get("proxy_resolution") or 540),
        resume=bool(args.get("resume", True)),
        max_assets=int(args.get("max_assets") or 100),
        progress=_progress,
        project_id=str(ctx.extra.get("project_id") or "").strip() or None,
    )


__all__ = ["dispatch"]
