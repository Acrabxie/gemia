"""search_media -- semantic search over persistent media annotations.

Unlike ``search_library`` (asset-level lexical preflight over the session +
library), ``search_media`` runs an FTS5 query over vision/heuristic/user
annotations and returns matching assets WITH time ranges, so the model can act on
the returned ``asset_id`` + ``start_sec``/``end_sec`` directly (insert clip, trim,
cut). Free and fast; never annotates (that costs money and stays the model's
explicit decision via ``annotate_media``).

Retrieval lives in :mod:`gemia.media_search`; session registration is shared with
``search_library`` via :mod:`gemia.tools._library_session`.
"""
from __future__ import annotations

from typing import Any

from gemia.tools._context import ToolContext
from gemia.tools._library_session import account_id_for, ensure_session_asset

_VALID_KINDS = {"video", "image", "audio", "any"}
_DEFAULT_LIMIT = 8
_MAX_LIMIT = 20


def _clamp_limit(value: Any) -> int:
    if value in (None, ""):
        return _DEFAULT_LIMIT
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return _DEFAULT_LIMIT
    return max(1, min(_MAX_LIMIT, parsed))


async def dispatch(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    query = str(args.get("query") or "").strip()
    if not query:
        raise ValueError("search_media requires a non-empty 'query' argument")
    kind = str(args.get("kind") or "any").strip().lower()
    if kind not in _VALID_KINDS:
        raise ValueError(f"search_media kind must be one of {sorted(_VALID_KINDS)}")
    limit = _clamp_limit(args.get("limit"))

    account_id = account_id_for(ctx)
    if not account_id:
        return {
            "query": query,
            "kind": kind,
            "fuzzy": False,
            "result_count": 0,
            "results": [],
            "unindexed_count": 0,
            "index_hint": "",
            "summary": "no account context; sign in to search the media library",
        }

    from gemia.media_library import get_asset
    from gemia.media_search import search_media_annotations

    raw = search_media_annotations(account_id, query, kind=kind, limit=limit)

    results: list[dict[str, Any]] = []
    for item in raw["results"]:
        library_asset_id = str(item.get("library_asset_id") or "")
        asset = get_asset(account_id, library_asset_id) if library_asset_id else None
        session_asset_id = ensure_session_asset(ctx, asset) if asset else None
        if not session_asset_id:
            # backing file gone: skip so the model never gets an unusable id
            continue
        results.append(
            {
                "asset_id": session_asset_id,
                "library_asset_id": library_asset_id,
                "name": item.get("name") or "media",
                "kind": item.get("kind") or "video",
                "duration": item.get("duration"),
                "score": item.get("score"),
                "matched_terms": item.get("matched_terms") or [],
                "asset_labels": item.get("asset_labels") or [],
                "time_ranges": item.get("time_ranges") or [],
            }
        )

    unindexed = int(raw.get("unindexed_count") or 0)
    index_hint = (
        f"{unindexed} asset(s) of this kind have no vision annotations; "
        f"annotate_media can index them (paid, ~$0.03/asset)."
        if unindexed > 0
        else ""
    )
    n_ranges = sum(len(r["time_ranges"]) for r in results)
    if results:
        summary = f"found {len(results)} asset(s), {n_ranges} time range(s) for '{query}'"
    elif unindexed > 0:
        summary = (
            f"no indexed media matched '{query}'; {unindexed} asset(s) can be "
            f"indexed with annotate_media"
        )
    else:
        summary = f"no media matched '{query}'"

    return {
        "query": query,
        "kind": kind,
        "fuzzy": bool(raw.get("fuzzy")),
        "result_count": len(results),
        "results": results,
        "unindexed_count": unindexed,
        "index_hint": index_hint,
        "summary": summary,
    }


__all__ = ["dispatch"]
