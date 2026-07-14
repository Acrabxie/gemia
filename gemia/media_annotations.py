"""Persistent account-scoped annotations for media-library assets."""
from __future__ import annotations

import json
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from gemia.media_library import MediaLibraryError, get_asset, library_path, media_root
from gemia.media_search import fts_normalize

_VALID_SCOPE = {"asset", "time_range", "frame"}
_VALID_SOURCE = {"gemini", "user", "import", "system", "gemini_vision", "heuristic"}


class MediaAnnotationError(ValueError):
    """Raised when a media annotation operation cannot be completed."""


def annotation_summary(account_id: str, asset_id: str) -> dict[str, Any]:
    """Return a compact annotation summary suitable for asset list payloads."""
    with _connect(account_id) as conn:
        rows = conn.execute(
            """
            SELECT label, tags_json, category, updated_at
            FROM media_annotations
            WHERE asset_id = ?
            ORDER BY updated_at DESC
            """,
            (_safe_asset_id(asset_id),),
        ).fetchall()
    tags: list[str] = []
    labels: list[str] = []
    categories: list[str] = []
    for row in rows:
        label = str(row["label"] or "").strip()
        if label and label not in labels:
            labels.append(label)
        category = str(row["category"] or "").strip()
        if category and category not in categories:
            categories.append(category)
        for tag in _json_list(row["tags_json"]):
            text = str(tag or "").strip()
            if text and text not in tags:
                tags.append(text)
    return {
        "count": len(rows),
        "labels": labels[:6],
        "tags": tags[:12],
        "categories": categories[:8],
    }


def list_annotations(account_id: str, asset_id: str) -> list[dict[str, Any]]:
    _require_asset(account_id, asset_id)
    with _connect(account_id) as conn:
        rows = conn.execute(
            """
            SELECT * FROM media_annotations
            WHERE asset_id = ?
            ORDER BY
              CASE WHEN start_sec IS NULL THEN 1 ELSE 0 END,
              start_sec ASC,
              created_at ASC
            """,
            (_safe_asset_id(asset_id),),
        ).fetchall()
    return [_public_annotation(dict(row)) for row in rows]


def create_annotation(account_id: str, asset_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    asset = _require_asset(account_id, asset_id)
    row = _normalize_payload(account_id, asset, payload, existing=None)
    row["annotation_id"] = _annotation_id()
    now = _utc_now()
    row["created_at"] = now
    row["updated_at"] = now
    with _connect(account_id) as conn:
        conn.execute(
            """
            INSERT INTO media_annotations (
              annotation_id, asset_id, account_id, scope, start_sec, end_sec,
              frame, label, note, tags_json, category, confidence, source,
              language, metadata_json, search_text, created_at, updated_at
            ) VALUES (
              :annotation_id, :asset_id, :account_id, :scope, :start_sec, :end_sec,
              :frame, :label, :note, :tags_json, :category, :confidence, :source,
              :language, :metadata_json, :search_text, :created_at, :updated_at
            )
            """,
            row,
        )
    return get_annotation(account_id, asset_id, row["annotation_id"])


def upsert_annotations(
    account_id: str,
    asset_id: str,
    annotations: list[dict[str, Any]],
    *,
    replace_source: str | None = None,
) -> list[dict[str, Any]]:
    _require_asset(account_id, asset_id)
    if replace_source:
        delete_source_annotations(account_id, asset_id, replace_source)
    created = [create_annotation(account_id, asset_id, item) for item in annotations]
    return created


def get_annotation(account_id: str, asset_id: str, annotation_id: str) -> dict[str, Any]:
    _require_asset(account_id, asset_id)
    with _connect(account_id) as conn:
        row = conn.execute(
            "SELECT * FROM media_annotations WHERE asset_id = ? AND annotation_id = ?",
            (_safe_asset_id(asset_id), _safe_annotation_id(annotation_id)),
        ).fetchone()
    if not row:
        raise MediaAnnotationError("annotation not found")
    return _public_annotation(dict(row))


def update_annotation(
    account_id: str,
    asset_id: str,
    annotation_id: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    current = get_annotation(account_id, asset_id, annotation_id)
    asset = _require_asset(account_id, asset_id)
    merged = {**current, **payload}
    row = _normalize_payload(account_id, asset, merged, existing=current)
    row["annotation_id"] = _safe_annotation_id(annotation_id)
    row["updated_at"] = _utc_now()
    with _connect(account_id) as conn:
        conn.execute(
            """
            UPDATE media_annotations SET
              scope = :scope,
              start_sec = :start_sec,
              end_sec = :end_sec,
              frame = :frame,
              label = :label,
              note = :note,
              tags_json = :tags_json,
              category = :category,
              confidence = :confidence,
              source = :source,
              language = :language,
              metadata_json = :metadata_json,
              search_text = :search_text,
              updated_at = :updated_at
            WHERE asset_id = :asset_id AND annotation_id = :annotation_id
            """,
            row,
        )
    return get_annotation(account_id, asset_id, annotation_id)


def delete_annotation(account_id: str, asset_id: str, annotation_id: str) -> dict[str, Any]:
    annotation = get_annotation(account_id, asset_id, annotation_id)
    with _connect(account_id) as conn:
        conn.execute(
            "DELETE FROM media_annotations WHERE asset_id = ? AND annotation_id = ?",
            (_safe_asset_id(asset_id), _safe_annotation_id(annotation_id)),
        )
    return annotation


def delete_source_annotations(account_id: str, asset_id: str, source: str) -> int:
    clean_source = _source(source)
    with _connect(account_id) as conn:
        cur = conn.execute(
            "DELETE FROM media_annotations WHERE asset_id = ? AND source = ?",
            (_safe_asset_id(asset_id), clean_source),
        )
        return int(cur.rowcount or 0)


def annotate_asset_heuristic(
    account_id: str,
    asset_id: str,
    *,
    mode: str = "quick",
    language: str = "auto",
    tags: list[str] | None = None,
    replace_existing: bool = True,
) -> dict[str, Any]:
    """Create practical local annotations while the Gemini vision pass is pluggable.

    This gives long/bulk media management immediate value: duration buckets,
    sampled time ranges, and asset-level tags are persistent and searchable.
    A later Gemini sampler can call the same storage API with richer labels.
    """
    asset = _require_asset(account_id, asset_id)
    media_kind = str(asset.get("media_kind") or "video")
    duration = max(float(asset.get("duration") or 0.0), 0.0)
    base_tags = _dedupe_tags([media_kind, *(tags or [])])
    annotations: list[dict[str, Any]] = [
        {
            "scope": "asset",
            "label": _label("素材摘要", "Asset summary", language),
            "note": _asset_note(asset, language),
            "tags": base_tags,
            "category": "summary",
            "confidence": 0.55,
            "source": "heuristic",
            "language": language,
            "metadata": {"mode": mode, "strategy": "local_heuristic"},
        }
    ]
    if media_kind in {"video", "audio"} and duration > 0:
        samples = 3 if mode != "detailed" else 6
        for index, (start, end) in enumerate(_sample_ranges(duration, samples), start=1):
            annotations.append(
                {
                    "scope": "time_range",
                    "start_sec": start,
                    "end_sec": end,
                    "label": _label(f"片段 {index}", f"Segment {index}", language),
                    "note": _label(
                        f"待复核的素材区间 {start:.1f}s-{end:.1f}s。",
                        f"Reviewable source range {start:.1f}s-{end:.1f}s.",
                        language,
                    ),
                    "tags": _dedupe_tags([*base_tags, "review", f"segment-{index}"]),
                    "category": "segment",
                    "confidence": 0.45,
                    "source": "heuristic",
                    "language": language,
                    "metadata": {"mode": mode, "strategy": "local_heuristic"},
                }
            )
    created = upsert_annotations(
        account_id,
        asset_id,
        annotations,
        replace_source="heuristic" if replace_existing else None,
    )
    return {
        "asset_id": asset_id,
        "annotation_count": len(created),
        "annotations": created,
        "summary": f"annotated {asset.get('name') or asset_id} with {len(created)} marker(s)",
    }


def search_annotation_text(account_id: str, asset_id: str) -> str:
    try:
        annotations = list_annotations(account_id, asset_id)
    except Exception:
        return ""
    chunks: list[str] = []
    for item in annotations:
        chunks.extend(
            [
                str(item.get("label") or ""),
                str(item.get("note") or ""),
                str(item.get("category") or ""),
                " ".join(str(tag) for tag in item.get("tags") or []),
            ]
        )
    return " ".join(chunk for chunk in chunks if chunk)


@contextmanager
def _connect(account_id: str) -> Iterator[sqlite3.Connection]:
    media_root(account_id).mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(library_path(account_id))
    conn.row_factory = sqlite3.Row
    try:
        _ensure_schema(conn)
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS media_annotations (
            annotation_id TEXT PRIMARY KEY,
            asset_id TEXT NOT NULL,
            account_id TEXT NOT NULL,
            scope TEXT NOT NULL,
            start_sec REAL,
            end_sec REAL,
            frame INTEGER,
            label TEXT NOT NULL,
            note TEXT NOT NULL DEFAULT '',
            tags_json TEXT NOT NULL DEFAULT '[]',
            category TEXT NOT NULL DEFAULT '',
            confidence REAL,
            source TEXT NOT NULL DEFAULT 'user',
            language TEXT NOT NULL DEFAULT 'auto',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_media_annotations_asset ON media_annotations(asset_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_media_annotations_updated ON media_annotations(updated_at)")
    _ensure_search_index(conn)
    conn.commit()


def _ensure_search_index(conn: sqlite3.Connection) -> None:
    """FTS5 external-content index over media_annotations + the index-state table.

    Idempotent and cheap: the column migration is guarded by table_info, the FTS
    table and triggers use IF NOT EXISTS, and the one-time backfill only runs while
    unnormalized rows remain. Triggers are created AFTER the backfill so the
    external-content index is never fed 'delete' commands for rows it never held.
    See docs/semantic-search-media-plan.md §4.2/§7.1.
    """
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(media_annotations)")}
    if "search_text" not in cols:
        conn.execute("ALTER TABLE media_annotations ADD COLUMN search_text TEXT NOT NULL DEFAULT ''")

    conn.execute(
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS media_annotations_fts USING fts5(
            search_text,
            content='media_annotations',
            tokenize='unicode61'
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS media_index_state (
            asset_id       TEXT PRIMARY KEY,
            fingerprint    TEXT NOT NULL,
            strategy       TEXT NOT NULL,
            prompt_version INTEGER NOT NULL DEFAULT 0,
            model          TEXT NOT NULL DEFAULT '',
            frames_used    INTEGER NOT NULL DEFAULT 0,
            annotated_at   TEXT NOT NULL,
            status         TEXT NOT NULL,
            error          TEXT
        )
        """
    )

    stale = conn.execute(
        "SELECT count(*) FROM media_annotations WHERE search_text = '' AND label != ''"
    ).fetchone()
    if stale and int(stale[0]) > 0:
        rows = conn.execute(
            "SELECT annotation_id, label, note, category, tags_json FROM media_annotations "
            "WHERE search_text = '' AND label != ''"
        ).fetchall()
        for row in rows:
            tags = _json_list(row["tags_json"])
            text = fts_normalize(
                " ".join(
                    [
                        str(row["label"] or ""),
                        str(row["note"] or ""),
                        str(row["category"] or ""),
                        *[str(t) for t in tags],
                    ]
                )
            )
            conn.execute(
                "UPDATE media_annotations SET search_text = ? WHERE annotation_id = ?",
                (text, str(row["annotation_id"])),
            )
        conn.execute("INSERT INTO media_annotations_fts(media_annotations_fts) VALUES('rebuild')")

    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS media_annotations_ai AFTER INSERT ON media_annotations BEGIN
          INSERT INTO media_annotations_fts(rowid, search_text) VALUES (new.rowid, new.search_text);
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS media_annotations_ad AFTER DELETE ON media_annotations BEGIN
          INSERT INTO media_annotations_fts(media_annotations_fts, rowid, search_text)
          VALUES('delete', old.rowid, old.search_text);
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS media_annotations_au AFTER UPDATE ON media_annotations BEGIN
          INSERT INTO media_annotations_fts(media_annotations_fts, rowid, search_text)
          VALUES('delete', old.rowid, old.search_text);
          INSERT INTO media_annotations_fts(rowid, search_text) VALUES (new.rowid, new.search_text);
        END
        """
    )


def _require_asset(account_id: str, asset_id: str) -> dict[str, Any]:
    asset = get_asset(account_id, _safe_asset_id(asset_id))
    if not asset:
        raise MediaAnnotationError("media asset not found")
    return asset


def _normalize_payload(
    account_id: str,
    asset: dict[str, Any],
    payload: dict[str, Any],
    *,
    existing: dict[str, Any] | None,
) -> dict[str, Any]:
    asset_id = _safe_asset_id(str(asset.get("asset_id") or ""))
    fallback_scope = existing.get("scope") if existing else ""
    scope = str(payload.get("scope") or fallback_scope or "").strip() or "asset"
    if scope not in _VALID_SCOPE:
        raise MediaAnnotationError(f"invalid annotation scope: {scope}")
    duration = max(float(asset.get("duration") or 0.0), 0.0)
    start = _optional_float(payload.get("start_sec"))
    end = _optional_float(payload.get("end_sec"))
    if scope == "time_range":
        if start is None:
            start = 0.0
        start = max(start, 0.0)
        if duration > 0:
            start = min(start, duration)
        if end is None:
            end = duration if duration > 0 else start
        end = max(end, start)
        if duration > 0:
            end = min(end, duration)
    else:
        start = None
        end = None
    frame = payload.get("frame")
    frame_value = None if frame in (None, "") else max(0, int(frame))
    label = str(payload.get("label") or "").strip()
    if not label:
        raise MediaAnnotationError("annotation label is required")
    confidence = _optional_float(payload.get("confidence"))
    if confidence is not None:
        confidence = max(0.0, min(1.0, confidence))
    tags = _dedupe_tags(payload.get("tags"))
    note = str(payload.get("note") or "")[:5000]
    category = str(payload.get("category") or "")[:80]
    label = label[:200]
    return {
        "asset_id": asset_id,
        "account_id": account_id,
        "scope": scope,
        "start_sec": start,
        "end_sec": end,
        "frame": frame_value,
        "label": label,
        "note": note,
        "tags_json": json.dumps(tags, ensure_ascii=False),
        "category": category,
        "confidence": confidence,
        "source": _source(str(payload.get("source") or "user")),
        "language": str(payload.get("language") or "auto")[:32],
        "metadata_json": json.dumps(_json_dict(payload.get("metadata")), ensure_ascii=False, sort_keys=True),
        "search_text": fts_normalize(" ".join([label, note, category, *tags])),
    }


def _public_annotation(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "annotation_id": str(row.get("annotation_id") or ""),
        "asset_id": str(row.get("asset_id") or ""),
        "scope": str(row.get("scope") or "asset"),
        "start_sec": row.get("start_sec"),
        "end_sec": row.get("end_sec"),
        "frame": row.get("frame"),
        "label": str(row.get("label") or ""),
        "note": str(row.get("note") or ""),
        "tags": _json_list(row.get("tags_json")),
        "category": str(row.get("category") or ""),
        "confidence": row.get("confidence"),
        "source": str(row.get("source") or "user"),
        "language": str(row.get("language") or "auto"),
        "metadata": _json_dict(row.get("metadata_json")),
        "created_at": str(row.get("created_at") or ""),
        "updated_at": str(row.get("updated_at") or ""),
    }


def _sample_ranges(duration: float, count: int) -> list[tuple[float, float]]:
    if duration <= 0:
        return []
    count = max(1, min(count, 12))
    window = min(max(duration / (count * 3), 1.0), 10.0)
    ranges: list[tuple[float, float]] = []
    for index in range(count):
        center = duration * (index + 0.5) / count
        start = max(0.0, center - window / 2)
        end = min(duration, center + window / 2)
        ranges.append((round(start, 3), round(max(start, end), 3)))
    return ranges


def _asset_note(asset: dict[str, Any], language: str) -> str:
    name = str(asset.get("name") or "media")
    kind = str(asset.get("media_kind") or "media")
    duration = float(asset.get("duration") or 0.0)
    width = int(asset.get("width") or 0)
    height = int(asset.get("height") or 0)
    if str(language).lower().startswith("zh"):
        return f"{name}：{kind} 素材，时长 {duration:.1f}s，尺寸 {width}x{height}。"
    return f"{name}: {kind} asset, {duration:.1f}s, {width}x{height}."


def _label(zh: str, en: str, language: str) -> str:
    return zh if str(language).lower().startswith("zh") else en


def _source(value: str) -> str:
    source = str(value or "user").strip().lower()
    return source if source in _VALID_SOURCE else "user"


def _dedupe_tags(value: Any) -> list[str]:
    if isinstance(value, str):
        raw_items = re.split(r"[,，\s]+", value)
    elif isinstance(value, list):
        raw_items = value
    else:
        raw_items = []
    out: list[str] = []
    for item in raw_items:
        text = str(item or "").strip()
        if text and text not in out:
            out.append(text[:60])
    return out[:30]


def _optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    return float(value)


def _json_dict(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        value = json.loads(str(raw))
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def _json_list(raw: Any) -> list[Any]:
    if isinstance(raw, list):
        return raw
    if not raw:
        return []
    try:
        value = json.loads(str(raw))
    except Exception:
        return []
    return value if isinstance(value, list) else []


def _safe_asset_id(value: str) -> str:
    text = str(value or "")
    if not re.fullmatch(r"asset_[0-9a-f]{24}", text):
        raise MediaAnnotationError("invalid media asset id")
    return text


def _safe_annotation_id(value: str) -> str:
    text = str(value or "")
    if not re.fullmatch(r"ann_[0-9a-f]{16}", text):
        raise MediaAnnotationError("invalid annotation id")
    return text


def _annotation_id() -> str:
    return f"ann_{uuid.uuid4().hex[:16]}"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = [
    "MediaAnnotationError",
    "annotate_asset_heuristic",
    "annotation_summary",
    "create_annotation",
    "delete_annotation",
    "get_annotation",
    "list_annotations",
    "search_annotation_text",
    "update_annotation",
    "upsert_annotations",
]
