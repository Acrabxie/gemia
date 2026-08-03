"""Shared helpers for registering media-library assets into the session registry.

Extracted from ``tools/search_library`` so ``search_media`` and ``search_library``
register library hits identically — do not copy-paste this logic into either tool.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from gemia.tools._context import ToolContext


def workspace_id_for(ctx: ToolContext) -> str:
    """Resolve the one local workspace used by the public runtime."""
    explicit = str(ctx.extra.get("workspace_id") or "").strip()
    if explicit:
        return explicit
    try:
        from gemia.local_workspace import current_workspace_id

        return current_workspace_id()
    except Exception:
        return ""


def ensure_session_asset(ctx: ToolContext, asset: dict[str, Any]) -> str | None:
    """Register a media-library asset into the session registry, returning its
    session ``asset_id`` (memoized per resolved path). Returns ``None`` when the
    backing file is missing or registration fails — callers skip such hits."""
    source_path = str(
        asset.get("source_path")
        or asset.get("storage_path")
        or asset.get("original_path")
        or ""
    ).strip()
    if not source_path:
        return None
    path = Path(source_path)
    if not path.exists():
        return None

    mapping = ctx.extra.setdefault("library_asset_session_ids", {})
    if not isinstance(mapping, dict):
        mapping = {}
        ctx.extra["library_asset_session_ids"] = mapping
    key = str(path.resolve())
    existing = mapping.get(key)
    if isinstance(existing, str) and ctx.registry.contains(existing):
        return existing

    try:
        record = ctx.registry.add_external(
            path,
            summary=f"library asset: {asset.get('name') or path.name}",
        )
    except Exception:
        return None
    mapping[key] = record.asset_id
    return record.asset_id


__all__ = ["ensure_session_asset", "workspace_id_for"]
