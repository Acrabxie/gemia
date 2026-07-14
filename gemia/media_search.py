"""FTS5 retrieval over persistent media annotations (bilingual zh/en).

This module is import-independent of :mod:`gemia.media_annotations`: that module
imports :func:`fts_normalize` from here, so the dependency is one-way and there is
no cycle. Retrieval opens the account library sqlite file directly (annotations,
assets, and the FTS index all live in one ``library.sqlite3`` — see
``docs/semantic-search-media-plan.md`` §0/§4).

Tokenizer decision (D4, verified against sqlite 3.50.4): ``unicode61`` over an
external-content FTS5 table, with CJK char-bigram normalization applied in Python
at both ingest and query time. Trigram was rejected because it cannot MATCH the
2-character words that dominate Chinese (``日落`` → 0 rows on trigram). See the
design doc §4.1 for the full transcript.
"""
from __future__ import annotations

import re
import sqlite3
from typing import Any

from gemia.media_library import library_path, media_root

# BMP CJK ranges: CJK Unified + Ext-A, CJK Compat Ideographs, Hiragana/Katakana,
# Hangul syllables. Ingest and query MUST use the same class or recall breaks.
_CJK = re.compile("[㐀-䶿一-鿿豈-﫿぀-ヿ가-힯]")


def fts_normalize(text: str) -> str:
    """CJK runs -> overlapping char bigrams (a len-1 run stays a single char);
    latin/digit runs -> lowercase words; everything else is a separator."""
    out: list[str] = []
    cjk: list[str] = []
    word: list[str] = []

    def flush_word() -> None:
        if word:
            out.append("".join(word))
            word.clear()

    def flush_cjk() -> None:
        if len(cjk) == 1:
            out.append(cjk[0])
        elif cjk:
            out.extend(cjk[i] + cjk[i + 1] for i in range(len(cjk) - 1))
        cjk.clear()

    for ch in str(text or ""):
        if _CJK.match(ch):
            flush_word()
            cjk.append(ch)
        elif ch.isalnum():
            flush_cjk()
            word.append(ch.lower())
        else:
            flush_word()
            flush_cjk()
    flush_word()
    flush_cjk()
    return " ".join(out)


def build_match_expr(query: str) -> str:
    """AND of terms; CJK bigrams verbatim, single CJK char and latin words get prefix ``*``.

    The normalized output only ever contains ``[a-z0-9]`` and CJK characters, so FTS5
    operators cannot survive from user input; the only ``*`` is the one we append. Always
    bind the result as a SQL parameter — never f-string it into the statement.
    """
    parts: list[str] = []
    for tok in fts_normalize(query).split():
        if _CJK.match(tok[0]) and len(tok) >= 2:
            parts.append(tok)  # bigram, exact
        else:
            parts.append(tok + "*")  # single CJK char or latin word: prefix
    return " ".join(parts)  # implicit AND


def _connect(account_id: str) -> sqlite3.Connection:
    media_root(account_id).mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(library_path(account_id))
    conn.row_factory = sqlite3.Row
    return conn


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name = ?",
        (name,),
    ).fetchone()
    return row is not None


_CORE_SQL = """
SELECT a.rowid AS rowid, a.annotation_id AS annotation_id, a.asset_id AS asset_id,
       a.scope AS scope, a.start_sec AS start_sec, a.end_sec AS end_sec,
       a.label AS label, a.category AS category, a.confidence AS confidence,
       a.search_text AS search_text,
       m.name AS name, m.media_kind AS media_kind, m.duration AS duration,
       bm25(media_annotations_fts) AS rank
FROM media_annotations_fts f
JOIN media_annotations a ON a.rowid = f.rowid
JOIN media_assets m ON m.asset_id = a.asset_id AND m.deleted_at IS NULL
WHERE media_annotations_fts MATCH :expr
  AND (:kind = '' OR m.media_kind = :kind)
ORDER BY rank
LIMIT 400
"""


def _run_match(conn: sqlite3.Connection, expr: str, kind_filter: str) -> list[sqlite3.Row]:
    if not expr:
        return []
    return conn.execute(_CORE_SQL, {"expr": expr, "kind": kind_filter}).fetchall()


def _unindexed_count(conn: sqlite3.Connection, kind_filter: str) -> int:
    """Assets of the requested kind (not deleted) with no vision-index 'ok' state row.

    Matches design doc §4.4 / §7.1: index-worthiness is tracked in ``media_index_state``.
    Until the vision pipeline (day-2) writes 'ok' rows, every asset counts as unindexed,
    which is honest — nothing is vision-annotated yet.
    """
    if not _table_exists(conn, "media_assets"):
        return 0
    has_state = _table_exists(conn, "media_index_state")
    clauses = ["deleted_at IS NULL"]
    params: list[Any] = []
    if kind_filter:
        clauses.append("media_kind = ?")
        params.append(kind_filter)
    if has_state:
        clauses.append(
            "asset_id NOT IN (SELECT asset_id FROM media_index_state WHERE status = 'ok')"
        )
    sql = f"SELECT COUNT(*) FROM media_assets WHERE {' AND '.join(clauses)}"
    row = conn.execute(sql, params).fetchone()
    return int(row[0] if row else 0)


def search_media_annotations(
    account_id: str,
    query: str,
    *,
    kind: str = "any",
    limit: int = 8,
) -> dict[str, Any]:
    """Library-level semantic search returning assets WITH time ranges.

    Returns a plain dict (no session context). The tool layer
    (:mod:`gemia.tools.search_media`) registers hits into the session registry and
    adds the ``index_hint``/``summary`` fields. Never raises on an empty or
    unindexed library — returns ``result_count: 0``.
    """
    kind = str(kind or "any").strip().lower() or "any"
    kind_filter = "" if kind == "any" else kind
    try:
        limit = max(1, min(20, int(limit)))
    except (TypeError, ValueError):
        limit = 8

    expr = build_match_expr(query)
    fuzzy = False
    empty = {
        "query": str(query or ""),
        "kind": kind,
        "fuzzy": False,
        "result_count": 0,
        "results": [],
        "unindexed_count": 0,
    }
    if not expr:
        return empty

    conn = _connect(account_id)
    try:
        if not _table_exists(conn, "media_annotations_fts"):
            return empty
        rows = _run_match(conn, expr, kind_filter)
        # OR fallback: 0 rows AND the expression has >= 2 top-level terms.
        terms = expr.split()
        if not rows and len(terms) >= 2:
            or_expr = " OR ".join(terms)
            rows = _run_match(conn, or_expr, kind_filter)
            if rows:
                fuzzy = True
        unindexed = _unindexed_count(conn, kind_filter)
    finally:
        conn.close()

    # Group annotation rows by asset. SQLite bm25 is smaller-is-better; we negate.
    user_terms = [t for t in str(query or "").split() if t.strip()]
    user_norm = [(t, fts_normalize(t)) for t in user_terms]

    by_asset: dict[str, dict[str, Any]] = {}
    for row in rows:
        asset_id = str(row["asset_id"])
        entry = by_asset.setdefault(
            asset_id,
            {
                "library_asset_id": asset_id,
                "name": str(row["name"] or "media"),
                "kind": str(row["media_kind"] or "video"),
                "duration": row["duration"],
                "best_rank": None,
                "has_asset_scope": False,
                "asset_labels": [],
                "matched_norms": set(),
                "time_rows": [],
            },
        )
        rank = float(row["rank"]) if row["rank"] is not None else 0.0
        if entry["best_rank"] is None or rank < entry["best_rank"]:
            entry["best_rank"] = rank
        search_text = str(row["search_text"] or "")
        for original, norm in user_norm:
            if norm and norm in search_text:
                entry["matched_norms"].add(original)
        label = str(row["label"] or "")
        scope = str(row["scope"] or "")
        if scope == "asset":
            entry["has_asset_scope"] = True
            if label and label not in entry["asset_labels"]:
                entry["asset_labels"].append(label)
        if scope == "time_range" and row["start_sec"] is not None:
            entry["time_rows"].append(
                {
                    "start_sec": round(float(row["start_sec"]), 3),
                    "end_sec": round(float(row["end_sec"]), 3)
                    if row["end_sec"] is not None
                    else round(float(row["start_sec"]), 3),
                    "label": label,
                    "category": str(row["category"] or ""),
                    "confidence": row["confidence"],
                    "annotation_id": str(row["annotation_id"] or ""),
                    "_rank": rank,
                }
            )

    scored: list[tuple[float, dict[str, Any]]] = []
    for entry in by_asset.values():
        base = -(entry["best_rank"] if entry["best_rank"] is not None else 0.0)
        if entry["has_asset_scope"]:
            base += 0.5
        scored.append((base, entry))
    scored.sort(key=lambda item: item[0], reverse=True)

    results: list[dict[str, Any]] = []
    for score, entry in scored[:limit]:
        time_rows = sorted(entry["time_rows"], key=lambda r: r["_rank"])[:6]
        for tr in time_rows:
            tr.pop("_rank", None)
        results.append(
            {
                "library_asset_id": entry["library_asset_id"],
                "name": entry["name"],
                "kind": entry["kind"],
                "duration": entry["duration"],
                "score": round(score, 3),
                "matched_terms": sorted(entry["matched_norms"]),
                "asset_labels": entry["asset_labels"][:3],
                "time_ranges": time_rows,
            }
        )

    return {
        "query": str(query or ""),
        "kind": kind,
        "fuzzy": fuzzy,
        "result_count": len(results),
        "results": results,
        "unindexed_count": unindexed,
    }


def asset_ids_matching(account_id: str, q: str, *, limit: int = 1000) -> list[str]:
    """Distinct library asset ids whose annotations match ``q`` (day-2 N+1 kill for
    ``media_library.list_assets(q=)``). Read-only, never raises."""
    expr = build_match_expr(q)
    if not expr:
        return []
    conn = _connect(account_id)
    try:
        if not _table_exists(conn, "media_annotations_fts"):
            return []
        rows = conn.execute(
            """
            SELECT DISTINCT a.asset_id
            FROM media_annotations_fts f
            JOIN media_annotations a ON a.rowid = f.rowid
            WHERE media_annotations_fts MATCH :expr
            LIMIT :limit
            """,
            {"expr": expr, "limit": int(limit)},
        ).fetchall()
        return [str(r["asset_id"]) for r in rows]
    finally:
        conn.close()


__all__ = [
    "fts_normalize",
    "build_match_expr",
    "search_media_annotations",
    "asset_ids_matching",
]
