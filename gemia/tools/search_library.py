"""search_library -- find usable media in the current session/library.

This is intentionally a real, non-throwing tool. The model may call it as a
cheap preflight; an empty library should return an empty result set, not abort a
turn. Results from the account media library are registered into the session
AssetRegistry so follow-up creative tools can use the returned session asset_id.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from gemia.tools._context import ToolContext
from gemia.tools._library_session import (
    account_id_for as _account_id,
    ensure_session_asset as _session_id_for_library_asset,
)

_VALID_KINDS = {"video", "image", "audio", "any"}
_DEFAULT_LIMIT = 10
_MAX_LIMIT = 50


def _clamp_limit(value: Any) -> int:
    if value in (None, ""):
        return _DEFAULT_LIMIT
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"search_library limit must be an integer, got {value!r}") from exc
    return max(1, min(_MAX_LIMIT, parsed))


def _tokens(query: str) -> list[str]:
    return [t for t in re.split(r"[^a-z0-9\u4e00-\u9fff]+", query.lower()) if t]


def _score(text: str, tokens: list[str]) -> int:
    lowered = text.lower()
    if not tokens:
        return 1
    return sum(1 for token in tokens if token in lowered)


def _session_registry_matches(
    ctx: ToolContext,
    *,
    query: str,
    kind: str,
    limit: int,
) -> list[dict[str, Any]]:
    tokens = _tokens(query)
    matches: list[tuple[int, dict[str, Any]]] = []
    for record in ctx.registry.list_records():
        if kind != "any" and record.kind != kind:
            continue
        haystack = " ".join(
            [
                record.asset_id,
                record.kind,
                record.path.name,
                record.summary,
            ]
        )
        score = _score(haystack, tokens)
        if score <= 0 and tokens:
            continue
        matches.append(
            (
                score,
                {
                    "asset_id": record.asset_id,
                    "kind": record.kind,
                    "name": record.path.name,
                    "summary": record.summary,
                    "source": "session",
                },
            )
        )
    matches.sort(key=lambda item: item[0], reverse=True)
    return [item[1] for item in matches[:limit]]


def _library_matches(
    ctx: ToolContext,
    *,
    query: str,
    kind: str,
    limit: int,
) -> list[dict[str, Any]]:
    account_id = _account_id(ctx)
    if not account_id:
        return []

    try:
        from gemia.media_library import list_assets

        assets = list_assets(
            account_id,
            kind=None if kind == "any" else kind,
            q=query,
            limit=limit,
        )
    except Exception:
        return []

    results: list[dict[str, Any]] = []
    for asset in assets:
        session_asset_id = _session_id_for_library_asset(ctx, asset)
        if not session_asset_id:
            continue
        results.append(
            {
                "asset_id": session_asset_id,
                "library_asset_id": asset.get("asset_id") or asset.get("id"),
                "kind": asset.get("media_kind") or "video",
                "name": asset.get("name") or "media",
                "duration": asset.get("duration"),
                "width": asset.get("width"),
                "height": asset.get("height"),
                "summary": (
                    f"{asset.get('name') or 'media'} "
                    f"({asset.get('media_kind') or 'media'}, "
                    f"{asset.get('width') or 0}x{asset.get('height') or 0})"
                ),
                "source": "media_library",
            }
        )
        if len(results) >= limit:
            break
    return results


async def dispatch(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    query = str(args.get("query") or "").strip()
    if not query:
        raise ValueError("search_library requires a non-empty 'query' argument")
    kind = str(args.get("kind") or "any").strip().lower()
    if kind not in _VALID_KINDS:
        raise ValueError(f"search_library kind must be one of {sorted(_VALID_KINDS)}")
    limit = _clamp_limit(args.get("limit"))

    session_results = _session_registry_matches(ctx, query=query, kind=kind, limit=limit)
    remaining = max(0, limit - len(session_results))
    library_results = (
        _library_matches(ctx, query=query, kind=kind, limit=remaining)
        if remaining
        else []
    )
    results = session_results + library_results
    return {
        "query": query,
        "kind": kind,
        "result_count": len(results),
        "results": results,
        "summary": (
            f"found {len(results)} matching assets"
            if results
            else "no matching session or media-library assets found"
        ),
    }


__all__ = ["dispatch"]
