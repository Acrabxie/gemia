"""Project-scoped media asset library for Gemia.

The sqlite file and blobs remain underneath the signed-in account, but every
public record belongs to exactly one Project.  Older account-wide rows are kept
in a private compatibility scope and are never surfaced to a real Project
unless they are explicitly copied/imported there.
"""
from __future__ import annotations

import hashlib
import json
import mimetypes
import re
import shutil
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from gemia.accounts import account_root
from gemia.asset_identity import asset_identity_for_record, attach_asset_identity
from gemia.media_ingest import probe_still_metadata
from gemia.project_model import IMAGE_DURATION
from gemia.video.timeline_assets import (
    SUPPORTED_MEDIA_EXTENSIONS,
    extract_waveform_peaks,
    generate_timeline_thumbnails,
    media_kind_for_path,
    probe_media,
)

MEDIA_LIBRARY_SCHEMA_VERSION = 2
LEGACY_PROJECT_SCOPE = "__legacy_account_scope__"


class MediaLibraryError(ValueError):
    """Raised when a media library operation cannot be completed."""


def media_root(account_id: str) -> Path:
    return account_root(account_id) / "media"


def library_path(account_id: str) -> Path:
    return media_root(account_id) / "library.sqlite3"


def originals_root(account_id: str) -> Path:
    return media_root(account_id) / "originals"


def cache_root(account_id: str) -> Path:
    return media_root(account_id) / "cache"


def asset_cache_root(account_id: str, asset_id: str) -> Path:
    return cache_root(account_id) / _safe_asset_id(asset_id)


def ensure_library(account_id: str) -> None:
    """Create/migrate the account container without exposing any Project."""
    _ensure_library_dirs(account_id)


def import_media(
    account_id: str,
    source_path: str | Path,
    *,
    original_name: str | None = None,
    project_id: str | None = None,
) -> dict[str, Any]:
    """Copy media into one Project and return its public asset record.

    ``project_id=None`` is retained only for old direct Python callers.  It is
    isolated in :data:`LEGACY_PROJECT_SCOPE`; HTTP and agent surfaces always
    pass their active Project id.
    """
    project_scope = _project_scope(project_id)
    source = Path(source_path)
    if not source.exists() or not source.is_file():
        raise MediaLibraryError(f"media file not found: {source_path}")
    ext = source.suffix.lower()
    if ext not in SUPPORTED_MEDIA_EXTENSIONS:
        allowed = ", ".join(sorted(SUPPORTED_MEDIA_EXTENSIONS))
        raise MediaLibraryError(f"unsupported media type: {ext or 'unknown'}; allowed: {allowed}")

    _ensure_library_dirs(account_id)
    fingerprint = _sha256_file(source)
    scoped_fingerprint = hashlib.sha256(
        f"{project_scope}\0{fingerprint}".encode()
    ).hexdigest()
    asset_id = f"asset_{scoped_fingerprint[:24]}"
    display_name = Path(original_name or source.name).name or f"{asset_id}{ext}"
    storage_path = originals_root(account_id) / f"{asset_id}{ext}"
    if not storage_path.exists() or not _same_file(source, storage_path):
        shutil.copy2(source, storage_path)
    try:
        storage_path.chmod(0o600)
    except OSError:
        pass

    now = _utc_now()
    metadata: dict[str, Any] = {}
    thumbnails: list[str] = []
    waveform_peaks: list[float] = []
    media_kind = media_kind_for_path(storage_path)
    mime_type = mimetypes.guess_type(str(display_name))[0] or mimetypes.guess_type(str(storage_path))[0] or "application/octet-stream"
    duration = IMAGE_DURATION if media_kind == "image" else 0.0
    status = "ready"
    error: str | None = None
    try:
        metadata = probe_media(str(storage_path))
        if media_kind_for_path(storage_path) == "image":
            metadata["image_ingest"] = probe_still_metadata(storage_path)
        media_kind = media_kind_for_path(storage_path)
        metadata["media_kind"] = media_kind
        metadata["mime_type"] = metadata.get("mime_type") or mime_type
        mime_type = str(metadata.get("mime_type") or mime_type)
        duration = IMAGE_DURATION if media_kind == "image" else max(float(metadata.get("duration") or 0.0), 0.0)
        thumbnails = generate_timeline_thumbnails(str(storage_path), asset_cache_root(account_id, asset_id), count=8)
        waveform_peaks = extract_waveform_peaks(str(storage_path), samples=512)
    except Exception as exc:
        status = "error"
        error = str(exc)
        metadata = {
            "media_kind": media_kind,
            "mime_type": mime_type,
            "file_size_bytes": storage_path.stat().st_size,
            "duration": duration,
            "width": 0,
            "height": 0,
            "fps": 0.0,
            "codec": "",
            "audio_codec": "",
            "has_audio": False,
        }

    row = {
        "asset_id": asset_id,
        "account_id": account_id,
        "project_id": project_scope,
        "name": display_name,
        "media_kind": media_kind,
        "mime_type": mime_type,
        "fingerprint": fingerprint,
        "original_path": str(storage_path),
        "storage_path": str(storage_path),
        "source_path": str(storage_path),
        "duration": duration,
        "width": int(metadata.get("width") or 0),
        "height": int(metadata.get("height") or 0),
        "fps": float(metadata.get("fps") or 0.0),
        "codec": str(metadata.get("codec") or ""),
        "audio_codec": str(metadata.get("audio_codec") or ""),
        "has_audio": 1 if metadata.get("has_audio") else 0,
        "file_size_bytes": int(metadata.get("file_size_bytes") or storage_path.stat().st_size),
        "metadata_json": json.dumps(metadata, ensure_ascii=False, sort_keys=True),
        "thumbnails_json": json.dumps([str(Path(item)) for item in thumbnails], ensure_ascii=False),
        "waveform_peaks_json": json.dumps(waveform_peaks, ensure_ascii=False),
        "status": status,
        "error": error,
        "created_at": now,
        "updated_at": now,
    }
    metadata["asset_identity"] = asset_identity_for_record(row)
    row["metadata_json"] = json.dumps(metadata, ensure_ascii=False, sort_keys=True)
    with _connect(account_id) as conn:
        existing = _row_by_id(conn, asset_id, project_scope)
        if existing:
            row["created_at"] = str(existing["created_at"] or now)
        _upsert_asset(conn, row)
    return get_asset(account_id, asset_id, project_id=project_scope) or _public_asset(row)


def copy_asset_to_project(
    account_id: str,
    asset_id: str,
    *,
    source_project_id: str,
    target_project_id: str,
) -> dict[str, Any]:
    """Explicitly copy one source-Project asset into a target Project record."""
    source_scope = _project_scope(source_project_id)
    target_scope = _project_scope(target_project_id)
    if source_scope == target_scope:
        asset = get_asset(account_id, asset_id, project_id=source_scope)
        if not asset:
            raise MediaLibraryError("media asset not found")
        return asset
    source = get_asset(account_id, asset_id, project_id=source_scope)
    if not source:
        raise MediaLibraryError("media asset not found")
    return import_media(
        account_id,
        str(source.get("storage_path") or source.get("source_path") or ""),
        original_name=str(source.get("name") or "") or None,
        project_id=target_scope,
    )


def list_assets(
    account_id: str,
    *,
    kind: str | None = None,
    q: str | None = None,
    limit: int = 200,
    include_deleted: bool = False,
    project_id: str | None = None,
) -> list[dict[str, Any]]:
    _ensure_library_dirs(account_id)
    project_scope = _project_scope(project_id)
    clauses = ["project_id = ?"]
    params: list[Any] = [project_scope]
    if not include_deleted:
        clauses.append("deleted_at IS NULL")
    if kind in {"video", "image", "audio", "lottie"}:
        clauses.append("media_kind = ?")
        params.append(kind)
    query = str(q or "").strip().lower()
    params.append(1000 if query else max(1, min(int(limit or 200), 1000)))
    sql = f"""
        SELECT * FROM media_assets
        WHERE {' AND '.join(clauses)}
        ORDER BY updated_at DESC, created_at DESC
        LIMIT ?
    """
    with _connect(account_id) as conn:
        assets = [_public_asset(_dict_from_row(row)) for row in conn.execute(sql, params).fetchall()]
    assets = [_with_annotation_summary(account_id, asset, project_scope) for asset in assets]
    if query:
        filtered: list[dict[str, Any]] = []
        for asset in assets:
            annotation_text = ""
            try:
                from gemia.media_annotations import search_annotation_text

                annotation_text = search_annotation_text(
                    account_id,
                    str(asset.get("asset_id") or ""),
                    project_id=project_scope,
                )
            except Exception:
                annotation_text = ""
            haystack = " ".join(
                [
                    str(asset.get("name") or ""),
                    str(asset.get("mime_type") or ""),
                    str(asset.get("media_kind") or ""),
                    annotation_text,
                ]
            ).lower()
            if query in haystack:
                filtered.append(asset)
            if len(filtered) >= max(1, min(int(limit or 200), 1000)):
                break
        assets = filtered
    return assets


def get_asset(
    account_id: str,
    asset_id: str,
    *,
    include_deleted: bool = False,
    project_id: str | None = None,
) -> dict[str, Any] | None:
    project_scope = _project_scope(project_id)
    with _connect(account_id) as conn:
        row = _row_by_id(conn, _safe_asset_id(asset_id), project_scope)
        if not row:
            return None
        payload = _dict_from_row(row)
        if payload.get("deleted_at") and not include_deleted:
            return None
        return _with_annotation_summary(
            account_id,
            attach_asset_identity(_public_asset(payload)),
            project_scope,
        )


def soft_delete_asset(account_id: str, asset_id: str, *, project_id: str | None = None) -> dict[str, Any]:
    project_scope = _project_scope(project_id)
    now = _utc_now()
    with _connect(account_id) as conn:
        row = _row_by_id(conn, _safe_asset_id(asset_id), project_scope)
        if not row:
            raise MediaLibraryError("media asset not found")
        conn.execute(
            "UPDATE media_assets SET deleted_at = ?, updated_at = ?, status = ? WHERE asset_id = ? AND project_id = ?",
            (now, now, "deleted", _safe_asset_id(asset_id), project_scope),
        )
    asset = get_asset(account_id, asset_id, include_deleted=True, project_id=project_scope)
    if not asset:
        raise MediaLibraryError("media asset not found")
    return asset


def resolve_asset_file(
    account_id: str,
    asset_id: str,
    area: str,
    filename: str | None = None,
    *,
    project_id: str | None = None,
) -> Path:
    asset = get_asset(account_id, asset_id, project_id=project_id)
    if not asset:
        raise MediaLibraryError("media asset not found")
    if area == "original":
        return Path(str(asset.get("storage_path") or asset.get("source_path") or ""))
    if area == "cache" and filename:
        cache_dir = asset_cache_root(account_id, asset_id).resolve()
        filename_path = Path(filename)
        if filename_path.is_absolute() or filename_path.name != filename or ".." in filename_path.parts:
            raise MediaLibraryError("invalid media cache path")
        potential_path = (cache_dir / filename).resolve()
        if not potential_path.is_relative_to(cache_dir):
            raise MediaLibraryError("invalid media cache path")
        return potential_path
    raise MediaLibraryError("invalid media file route")


def default_clip_for_asset(asset: dict[str, Any]) -> dict[str, Any]:
    media_kind = str(asset.get("media_kind") or "video")
    duration = IMAGE_DURATION if media_kind == "image" else max(float(asset.get("duration") or 0.0), 0.1)
    return {
        "id": f"clip_{uuid.uuid4().hex[:12]}",
        "assetId": str(asset.get("asset_id") or asset.get("id") or ""),
        "trackId": "A1" if media_kind == "audio" else ("OV1" if media_kind in {"image", "lottie"} else "V1"),
        "mediaKind": media_kind,
        "mimeType": str(asset.get("mime_type") or ""),
        "name": str(asset.get("name") or "media"),
        "serverPath": str(asset.get("source_path") or ""),
        "previewSrc": str(asset.get("preview_src") or ""),
        "duration": duration,
        "inPoint": 0.0,
        "outPoint": duration,
        "keep": True,
        "summary": _default_summary(media_kind, duration),
        "thumbnailSrc": str(asset.get("thumbnail_src") or ""),
        "thumbnailStrip": list(asset.get("thumbnails") or []),
        "waveformPeaks": list(asset.get("waveform_peaks") or []),
        "metadata": asset.get("metadata") if isinstance(asset.get("metadata"), dict) else {},
        "effects": {"rotation": 0, "mirrored": False, "muted": False, "audioDetached": False, "speed": 1},
    }


def upload_response_for_asset(asset: dict[str, Any]) -> dict[str, Any]:
    """Return the legacy upload response shape plus v1 asset/clip payloads."""
    clip = default_clip_for_asset(asset)
    return {
        "asset": asset,
        "clip": clip,
        "asset_id": asset.get("asset_id") or asset.get("id"),
        "name": asset.get("name"),
        "path": asset.get("source_path"),
        "preview_src": asset.get("preview_src"),
        "duration": asset.get("duration"),
        "media_kind": asset.get("media_kind"),
        "mime_type": asset.get("mime_type"),
        "metadata": asset.get("metadata") if isinstance(asset.get("metadata"), dict) else {},
        "thumbnails": list(asset.get("thumbnails") or []),
        "waveform_peaks": list(asset.get("waveform_peaks") or []),
    }


def _ensure_library_dirs(account_id: str) -> None:
    for path in (media_root(account_id), originals_root(account_id), cache_root(account_id)):
        path.mkdir(parents=True, exist_ok=True)
        try:
            path.chmod(0o700)
        except OSError:
            pass
    with _connect(account_id):
        pass


@contextmanager
def _connect(account_id: str) -> Iterator[sqlite3.Connection]:
    media_root(account_id).mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(library_path(account_id))
    conn.row_factory = sqlite3.Row
    try:
        _ensure_schema(conn)
        try:
            library_path(account_id).chmod(0o600)
        except OSError:
            pass
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
        CREATE TABLE IF NOT EXISTS media_assets (
            asset_id TEXT PRIMARY KEY,
            account_id TEXT NOT NULL,
            project_id TEXT NOT NULL,
            name TEXT NOT NULL,
            media_kind TEXT NOT NULL,
            mime_type TEXT NOT NULL,
            fingerprint TEXT NOT NULL,
            original_path TEXT NOT NULL,
            storage_path TEXT NOT NULL,
            source_path TEXT NOT NULL,
            duration REAL NOT NULL DEFAULT 0,
            width INTEGER NOT NULL DEFAULT 0,
            height INTEGER NOT NULL DEFAULT 0,
            fps REAL NOT NULL DEFAULT 0,
            codec TEXT NOT NULL DEFAULT '',
            audio_codec TEXT NOT NULL DEFAULT '',
            has_audio INTEGER NOT NULL DEFAULT 0,
            file_size_bytes INTEGER NOT NULL DEFAULT 0,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            thumbnails_json TEXT NOT NULL DEFAULT '[]',
            waveform_peaks_json TEXT NOT NULL DEFAULT '[]',
            status TEXT NOT NULL DEFAULT 'ready',
            error TEXT,
            deleted_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(media_assets)")}
    if "project_id" not in columns:
        conn.execute("ALTER TABLE media_assets ADD COLUMN project_id TEXT NOT NULL DEFAULT ''")
    conn.execute(
        "UPDATE media_assets SET project_id = ? WHERE project_id = ''",
        (LEGACY_PROJECT_SCOPE,),
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_media_assets_kind ON media_assets(media_kind)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_media_assets_project ON media_assets(project_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_media_assets_updated ON media_assets(updated_at)")
    conn.execute(f"PRAGMA user_version = {MEDIA_LIBRARY_SCHEMA_VERSION}")
    conn.commit()


def _upsert_asset(conn: sqlite3.Connection, row: dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO media_assets (
            asset_id, account_id, project_id, name, media_kind, mime_type, fingerprint,
            original_path, storage_path, source_path, duration, width, height,
            fps, codec, audio_codec, has_audio, file_size_bytes, metadata_json,
            thumbnails_json, waveform_peaks_json, status, error, deleted_at,
            created_at, updated_at
        ) VALUES (
            :asset_id, :account_id, :project_id, :name, :media_kind, :mime_type, :fingerprint,
            :original_path, :storage_path, :source_path, :duration, :width, :height,
            :fps, :codec, :audio_codec, :has_audio, :file_size_bytes, :metadata_json,
            :thumbnails_json, :waveform_peaks_json, :status, :error, NULL,
            :created_at, :updated_at
        )
        ON CONFLICT(asset_id) DO UPDATE SET
            name = excluded.name,
            media_kind = excluded.media_kind,
            mime_type = excluded.mime_type,
            original_path = excluded.original_path,
            storage_path = excluded.storage_path,
            source_path = excluded.source_path,
            duration = excluded.duration,
            width = excluded.width,
            height = excluded.height,
            fps = excluded.fps,
            codec = excluded.codec,
            audio_codec = excluded.audio_codec,
            has_audio = excluded.has_audio,
            file_size_bytes = excluded.file_size_bytes,
            metadata_json = excluded.metadata_json,
            thumbnails_json = excluded.thumbnails_json,
            waveform_peaks_json = excluded.waveform_peaks_json,
            status = excluded.status,
            error = excluded.error,
            deleted_at = NULL,
            updated_at = excluded.updated_at
        """,
        row,
    )
    conn.commit()


def _row_by_id(conn: sqlite3.Connection, asset_id: str, project_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM media_assets WHERE asset_id = ? AND project_id = ?",
        (_safe_asset_id(asset_id), _project_scope(project_id)),
    ).fetchone()


def _dict_from_row(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    return dict(row)


def _public_asset(row: dict[str, Any]) -> dict[str, Any]:
    asset_id = str(row.get("asset_id") or row.get("id") or "")
    metadata = _json_dict(row.get("metadata_json"), row.get("metadata"))
    thumbnails_abs = _json_list(row.get("thumbnails_json"), row.get("thumbnails"))
    waveform_peaks = [float(item or 0) for item in _json_list(row.get("waveform_peaks_json"), row.get("waveform_peaks"))]
    thumbnails = [_asset_file_url(asset_id, "cache", Path(str(item)).name) for item in thumbnails_abs]
    preview_src = _asset_file_url(asset_id, "original")
    duration = IMAGE_DURATION if row.get("media_kind") == "image" else max(float(row.get("duration") or 0.0), 0.0)
    return attach_asset_identity({
        "id": asset_id,
        "asset_id": asset_id,
        "account_id": str(row.get("account_id") or ""),
        "project_id": str(row.get("project_id") or LEGACY_PROJECT_SCOPE),
        "name": str(row.get("name") or "media"),
        "media_kind": str(row.get("media_kind") or "video"),
        "mime_type": str(row.get("mime_type") or metadata.get("mime_type") or ""),
        "fingerprint": str(row.get("fingerprint") or ""),
        "original_path": str(row.get("original_path") or row.get("storage_path") or ""),
        "storage_path": str(row.get("storage_path") or row.get("source_path") or ""),
        "source_path": str(row.get("source_path") or row.get("storage_path") or ""),
        "preview_src": preview_src,
        "thumbnail_src": thumbnails[0] if thumbnails else "",
        "thumbnails": thumbnails,
        "waveform_peaks": waveform_peaks,
        "duration": duration,
        "width": int(row.get("width") or metadata.get("width") or 0),
        "height": int(row.get("height") or metadata.get("height") or 0),
        "fps": float(row.get("fps") or metadata.get("fps") or 0.0),
        "codec": str(row.get("codec") or metadata.get("codec") or ""),
        "audio_codec": str(row.get("audio_codec") or metadata.get("audio_codec") or ""),
        "has_audio": bool(row.get("has_audio") or metadata.get("has_audio")),
        "file_size_bytes": int(row.get("file_size_bytes") or metadata.get("file_size_bytes") or 0),
        "metadata": metadata,
        "status": str(row.get("status") or "ready"),
        "error": row.get("error"),
        "deleted_at": row.get("deleted_at"),
        "created_at": str(row.get("created_at") or _utc_now()),
        "updated_at": str(row.get("updated_at") or _utc_now()),
    })


def _with_annotation_summary(account_id: str, asset: dict[str, Any], project_id: str) -> dict[str, Any]:
    try:
        from gemia.media_annotations import annotation_summary

        asset["annotation_summary"] = annotation_summary(
            account_id,
            str(asset.get("asset_id") or ""),
            project_id=project_id,
        )
    except Exception:
        asset["annotation_summary"] = {"count": 0, "labels": [], "tags": [], "categories": []}
    return asset


def _asset_file_url(asset_id: str, area: str, filename: str | None = None) -> str:
    if area == "cache" and filename:
        return f"/media-library/file/{asset_id}/cache/{filename}"
    return f"/media-library/file/{asset_id}/original"


def _default_summary(media_kind: str, duration: float) -> dict[str, Any] | None:
    if media_kind == "video":
        return None
    if media_kind == "image":
        return {
            "duration": duration,
            "summary": "图片素材，默认按 3 秒静态视频处理。",
            "mood": "image",
            "key_frame": "still frame",
            "suggested_use": "可作为 3 秒静态画面、背景或转场素材。",
            "keep": True,
        }
    if media_kind == "lottie":
        return {
            "duration": duration,
            "summary": "Lottie 动画素材，已读取动画时长和首帧缩略图。",
            "mood": "motion graphics",
            "key_frame": "animated vector frame",
            "suggested_use": "可作为动态图标、片头标识、贴纸或 motion graphics overlay。",
            "keep": True,
        }
    return {
        "duration": duration,
        "summary": "音频素材，已导入时间轴并提取波形。",
        "mood": "audio",
        "key_frame": "waveform",
        "suggested_use": "可作为配乐、旁白或音效轨道素材。",
        "keep": True,
    }


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _same_file(left: Path, right: Path) -> bool:
    try:
        return left.resolve() == right.resolve() or left.samefile(right)
    except OSError:
        return False


def _json_dict(raw: Any, fallback: Any = None) -> dict[str, Any]:
    if isinstance(fallback, dict):
        default = fallback
    else:
        default = {}
    if isinstance(raw, dict):
        return raw
    if not raw:
        return default
    try:
        value = json.loads(str(raw))
    except Exception:
        return default
    return value if isinstance(value, dict) else default


def _json_list(raw: Any, fallback: Any = None) -> list[Any]:
    if isinstance(fallback, list):
        default = fallback
    else:
        default = []
    if isinstance(raw, list):
        return raw
    if not raw:
        return default
    try:
        value = json.loads(str(raw))
    except Exception:
        return default
    return value if isinstance(value, list) else default


def _safe_asset_id(value: str) -> str:
    text = str(value or "")
    if not re.fullmatch(r"asset_[0-9a-f]{24}", text):
        raise MediaLibraryError("invalid media asset id")
    return text


def _project_scope(value: str | None) -> str:
    text = str(value or "").strip()
    if not text:
        return LEGACY_PROJECT_SCOPE
    if len(text) > 160 or not re.fullmatch(r"[A-Za-z0-9_.:-]+", text):
        raise MediaLibraryError("invalid project id")
    return text


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = [
    "MEDIA_LIBRARY_SCHEMA_VERSION",
    "MediaLibraryError",
    "LEGACY_PROJECT_SCOPE",
    "asset_cache_root",
    "asset_identity_for_record",
    "cache_root",
    "copy_asset_to_project",
    "default_clip_for_asset",
    "ensure_library",
    "get_asset",
    "import_media",
    "library_path",
    "list_assets",
    "media_root",
    "originals_root",
    "resolve_asset_file",
    "soft_delete_asset",
    "upload_response_for_asset",
]
