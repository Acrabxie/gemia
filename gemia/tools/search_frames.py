"""search_frames -- semantic footage search over raw material for shotlist fill ("先搜真素材").

Unlike ``search_library`` (a cheap lexical match over asset_id/filename/summary),
this wraps ``gemia.video.intellisearch``: it builds a label index over real
footage (filename + probed visual/dialog labels) and scores each clip against
the query. Use it to fill a shot's footage from existing material before
falling back to ``generate_video``.

Candidates come from (in order): the session AssetRegistry (registered video
assets), the account media library, and any explicit ``paths``. Top matches are
registered into the session registry so a shot can reference the returned
``asset_id`` directly. Non-throwing: no candidates → empty results (the model
should then generate), never an aborted turn.

The label index is cached per candidate set on ``ctx.extra`` so repeated
searches in one session don't re-probe the same footage.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from gemia.tools._context import ToolContext

_DEFAULT_LIMIT = 5
_MAX_LIMIT = 20
_MAX_CANDIDATES = 60  # cap probing cost per session index build
_INDEX_SAMPLES = 3     # frames probed per clip; low = fast, enough for labels
_VIDEO_LIKE = {"video", "image"}


def _clamp_limit(value: Any) -> int:
    try:
        parsed = int(value) if value not in (None, "") else _DEFAULT_LIMIT
    except (TypeError, ValueError):
        parsed = _DEFAULT_LIMIT
    return max(1, min(_MAX_LIMIT, parsed))


def _account_id(ctx: ToolContext) -> str:
    explicit = str(ctx.extra.get("account_id") or "").strip()
    if explicit:
        return explicit
    try:
        from gemia import public_identity as accounts

        return str(accounts.current_account_id() or "").strip()
    except Exception:
        return ""


def _candidate_paths(ctx: ToolContext, extra_paths: list[str], kind: str) -> list[Path]:
    """Real footage paths to index: session registry + account library + explicit."""
    seen: set[str] = set()
    out: list[Path] = []

    def _add(raw: Any) -> None:
        if not raw:
            return
        p = Path(str(raw)).expanduser()
        try:
            p = p.resolve()
        except OSError:
            return
        key = str(p)
        if key in seen or not p.exists():
            return
        seen.add(key)
        out.append(p)

    for record in ctx.registry.list_records():
        if kind == "any" or record.kind == kind or record.kind in _VIDEO_LIKE:
            _add(record.path)

    account_id = _account_id(ctx)
    if account_id:
        try:
            from gemia.media_library import list_assets

            for asset in list_assets(
                account_id, kind=None if kind == "any" else kind, limit=_MAX_CANDIDATES
            ):
                _add(asset.get("source_path") or asset.get("storage_path") or asset.get("original_path"))
        except Exception:
            pass

    for raw in extra_paths:
        _add(raw)

    return out[:_MAX_CANDIDATES]


def _index_for(ctx: ToolContext, paths: list[Path]):
    """Build (or reuse a cached) intellisearch index over ``paths``."""
    from gemia.video.intellisearch import index_real_media

    key = hashlib.sha1("\n".join(str(p) for p in paths).encode("utf-8")).hexdigest()[:16]
    cache = ctx.extra.setdefault("search_media_indexes", {})
    if isinstance(cache, dict) and key in cache and Path(cache[key]).exists():
        return cache[key]
    index_path = Path(ctx.output_dir) / ".intellisearch" / f"index_{key}.json"
    index_real_media(
        [str(p) for p in paths], str(index_path), max_samples=_INDEX_SAMPLES
    )
    if isinstance(cache, dict):
        cache[key] = str(index_path)
    return str(index_path)


def _register_match(ctx: ToolContext, path: Path) -> str | None:
    """Register a matched file into the session registry (reusing prior id)."""
    mapping = ctx.extra.setdefault("library_asset_session_ids", {})
    if not isinstance(mapping, dict):
        mapping = {}
        ctx.extra["library_asset_session_ids"] = mapping
    key = str(path)
    existing = mapping.get(key)
    if isinstance(existing, str) and ctx.registry.contains(existing):
        return existing
    try:
        record = ctx.registry.add_external(path, summary=f"searched footage: {path.name}")
    except Exception:
        return None
    mapping[key] = record.asset_id
    return record.asset_id


async def dispatch(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    query = str(args.get("query") or "").strip()
    if not query:
        raise ValueError("search_media requires a non-empty 'query' argument")
    kind = str(args.get("kind") or "video").strip().lower()
    if kind not in {"video", "image", "any"}:
        raise ValueError("search_media kind must be one of video, image, any")
    limit = _clamp_limit(args.get("limit"))
    extra_paths = [str(p) for p in (args.get("paths") or []) if p]

    candidates = _candidate_paths(ctx, extra_paths, kind)
    if not candidates:
        return {
            "query": query,
            "result_count": 0,
            "results": [],
            "summary": "no indexable footage found — generate this shot instead of searching",
        }

    from gemia.video.intellisearch import search_media_index

    index_path = _index_for(ctx, candidates)
    query_result = search_media_index(index_path, query, limit=limit)

    results: list[dict[str, Any]] = []
    for match in query_result.matches:
        path = Path(str(match.get("path") or ""))
        if not path.exists():
            continue
        asset_id = _register_match(ctx, path)
        if not asset_id:
            continue
        labels = list(match.get("matched_terms") or []) + list(match.get("matched_phrases") or [])
        results.append(
            {
                "asset_id": asset_id,
                "name": path.name,
                "score": match.get("score"),
                "matched": labels[:8],
                "time_ranges": match.get("time_ranges") or [],
                "source": "search_frames",
            }
        )
        if len(results) >= limit:
            break

    return {
        "query": query,
        "kind": kind,
        "indexed_count": len(candidates),
        "result_count": len(results),
        "results": results,
        "summary": (
            f"found {len(results)} matching clips (indexed {len(candidates)})"
            if results
            else f"no semantic match among {len(candidates)} clips — consider generate_video"
        ),
    }


__all__ = ["dispatch"]
