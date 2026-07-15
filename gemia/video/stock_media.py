"""Stock media search and download primitives for Pexels/Pixabay."""
from __future__ import annotations

import json
import mimetypes
import os
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import certifi


PEXELS_VIDEO_SEARCH_URL = "https://api.pexels.com/videos/search"
PEXELS_IMAGE_SEARCH_URL = "https://api.pexels.com/v1/search"
PIXABAY_VIDEO_SEARCH_URL = "https://pixabay.com/api/videos/"
PIXABAY_IMAGE_SEARCH_URL = "https://pixabay.com/api/"
STOCK_MEDIA_TIMEOUT_SEC = 30


class StockMediaError(ValueError):
    """Raised when stock media search or fetch cannot be completed."""


def search_stock_media(
    *,
    query: str,
    provider: str = "auto",
    media_type: str = "video",
    limit: int = 5,
    orientation: str | None = None,
    safe_search: bool = True,
) -> dict[str, Any]:
    """Search Pexels/Pixabay for stock media and return compact metadata."""
    query_text = _clean_query(query)
    provider_value = _normalize_provider(provider)
    kind = _normalize_media_type(media_type)
    per_page = max(1, min(int(limit or 5), 20))
    results: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []

    providers = ["pexels", "pixabay"] if provider_value == "auto" else [provider_value]
    for source in providers:
        try:
            if source == "pexels":
                results.extend(_search_pexels(query_text, media_type=kind, limit=per_page, orientation=orientation))
            elif source == "pixabay":
                results.extend(
                    _search_pixabay(
                        query_text,
                        media_type=kind,
                        limit=per_page,
                        orientation=orientation,
                        safe_search=safe_search,
                    )
                )
        except Exception as exc:
            errors.append({"provider": source, "error": str(exc)})

    return {
        "query": query_text,
        "provider": provider_value,
        "media_type": kind,
        "results": results[:per_page],
        "errors": errors,
        "searched_at": _utc_now(),
    }


def fetch_stock_media(
    input_path: str,
    output_path: str,
    *,
    query: str,
    provider: str = "auto",
    media_type: str = "video",
    limit: int = 8,
    orientation: str | None = None,
    safe_search: bool = True,
    import_to_media_library: bool = True,
) -> str:
    """Search and download the first matching stock asset.

    ``input_path`` is accepted for PlanEngine compatibility and is not read.
    """
    del input_path
    search = search_stock_media(
        query=query,
        provider=provider,
        media_type=media_type,
        limit=limit,
        orientation=orientation,
        safe_search=safe_search,
    )
    results = [item for item in search.get("results") or [] if isinstance(item, dict) and item.get("download_url")]
    if not results:
        raise StockMediaError(f"No downloadable stock media found for query: {query}")
    selected = _choose_best_result(results)
    downloaded = _download_result(selected, output_path)
    imported_asset = _maybe_import_to_media_library(downloaded, selected, import_to_media_library)
    _write_sidecar(downloaded, selected, search, imported_asset)
    return str(downloaded)


def fetch_pexels_media(
    input_path: str,
    output_path: str,
    *,
    query: str,
    media_type: str = "video",
    limit: int = 8,
    orientation: str | None = None,
    import_to_media_library: bool = True,
) -> str:
    """Search and download one Pexels video/photo for the plan."""
    return fetch_stock_media(
        input_path,
        output_path,
        query=query,
        provider="pexels",
        media_type=media_type,
        limit=limit,
        orientation=orientation,
        import_to_media_library=import_to_media_library,
    )


def fetch_pixabay_media(
    input_path: str,
    output_path: str,
    *,
    query: str,
    media_type: str = "video",
    limit: int = 8,
    orientation: str | None = None,
    safe_search: bool = True,
    import_to_media_library: bool = True,
) -> str:
    """Search and download one Pixabay video/photo for the plan."""
    return fetch_stock_media(
        input_path,
        output_path,
        query=query,
        provider="pixabay",
        media_type=media_type,
        limit=limit,
        orientation=orientation,
        safe_search=safe_search,
        import_to_media_library=import_to_media_library,
    )


def _search_pexels(query: str, *, media_type: str, limit: int, orientation: str | None) -> list[dict[str, Any]]:
    key = _api_key("pexels")
    params: dict[str, Any] = {"query": query, "per_page": limit}
    if orientation:
        params["orientation"] = orientation
    url = (PEXELS_VIDEO_SEARCH_URL if media_type == "video" else PEXELS_IMAGE_SEARCH_URL) + "?" + urllib.parse.urlencode(params)
    payload = _urlopen_json(url, headers={"Authorization": key})
    if media_type == "video":
        return [_pexels_video_result(item) for item in payload.get("videos") or [] if isinstance(item, dict)]
    return [_pexels_image_result(item) for item in payload.get("photos") or [] if isinstance(item, dict)]


def _search_pixabay(
    query: str,
    *,
    media_type: str,
    limit: int,
    orientation: str | None,
    safe_search: bool,
) -> list[dict[str, Any]]:
    key = _api_key("pixabay")
    api_limit = max(3, limit)
    params: dict[str, Any] = {
        "key": key,
        "q": query,
        "per_page": api_limit,
        "safesearch": "true" if safe_search else "false",
    }
    if orientation in {"horizontal", "vertical"}:
        params["orientation"] = orientation
    if media_type != "video":
        params["image_type"] = "photo"
    url = (PIXABAY_VIDEO_SEARCH_URL if media_type == "video" else PIXABAY_IMAGE_SEARCH_URL) + "?" + urllib.parse.urlencode(params)
    payload = _urlopen_json(url)
    if media_type == "video":
        return [_pixabay_video_result(item) for item in payload.get("hits") or [] if isinstance(item, dict)]
    return [_pixabay_image_result(item) for item in payload.get("hits") or [] if isinstance(item, dict)]


def _pexels_video_result(item: dict[str, Any]) -> dict[str, Any]:
    files = [entry for entry in item.get("video_files") or [] if isinstance(entry, dict) and entry.get("link")]
    best = _largest_media_file(files)
    return {
        "provider": "pexels",
        "id": str(item.get("id") or ""),
        "title": str(item.get("url") or "Pexels video"),
        "media_type": "video",
        "width": int(item.get("width") or best.get("width") or 0),
        "height": int(item.get("height") or best.get("height") or 0),
        "duration": float(item.get("duration") or 0.0),
        "page_url": str(item.get("url") or ""),
        "thumbnail_url": str(item.get("image") or ""),
        "download_url": str(best.get("link") or ""),
        "license": "Pexels License",
        "attribution": _pexels_user(item),
    }


def _pexels_image_result(item: dict[str, Any]) -> dict[str, Any]:
    src = item.get("src") if isinstance(item.get("src"), dict) else {}
    return {
        "provider": "pexels",
        "id": str(item.get("id") or ""),
        "title": str(item.get("alt") or item.get("url") or "Pexels photo"),
        "media_type": "image",
        "width": int(item.get("width") or 0),
        "height": int(item.get("height") or 0),
        "duration": 0.0,
        "page_url": str(item.get("url") or ""),
        "thumbnail_url": str(src.get("medium") or src.get("small") or ""),
        "download_url": str(src.get("large2x") or src.get("large") or src.get("original") or ""),
        "license": "Pexels License",
        "attribution": str(item.get("photographer") or ""),
    }


def _pixabay_video_result(item: dict[str, Any]) -> dict[str, Any]:
    videos = item.get("videos") if isinstance(item.get("videos"), dict) else {}
    best = _pixabay_best_video(videos)
    return {
        "provider": "pixabay",
        "id": str(item.get("id") or ""),
        "title": str(item.get("tags") or "Pixabay video"),
        "media_type": "video",
        "width": int(best.get("width") or 0),
        "height": int(best.get("height") or 0),
        "duration": float(item.get("duration") or 0.0),
        "page_url": str(item.get("pageURL") or ""),
        "thumbnail_url": str(item.get("picture_id") or ""),
        "download_url": str(best.get("url") or ""),
        "license": "Pixabay Content License",
        "attribution": str(item.get("user") or ""),
    }


def _pixabay_image_result(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "provider": "pixabay",
        "id": str(item.get("id") or ""),
        "title": str(item.get("tags") or "Pixabay image"),
        "media_type": "image",
        "width": int(item.get("imageWidth") or item.get("webformatWidth") or 0),
        "height": int(item.get("imageHeight") or item.get("webformatHeight") or 0),
        "duration": 0.0,
        "page_url": str(item.get("pageURL") or ""),
        "thumbnail_url": str(item.get("previewURL") or ""),
        "download_url": str(item.get("largeImageURL") or item.get("webformatURL") or ""),
        "license": "Pixabay Content License",
        "attribution": str(item.get("user") or ""),
    }


def _largest_media_file(files: list[dict[str, Any]]) -> dict[str, Any]:
    if not files:
        return {}
    return sorted(files, key=lambda item: int(item.get("width") or 0) * int(item.get("height") or 0), reverse=True)[0]


def _pixabay_best_video(videos: dict[str, Any]) -> dict[str, Any]:
    candidates = [value for key, value in videos.items() if key in {"large", "medium", "small", "tiny"} and isinstance(value, dict)]
    return _largest_media_file(candidates)


def _choose_best_result(results: list[dict[str, Any]]) -> dict[str, Any]:
    return sorted(
        results,
        key=lambda item: (
            int(item.get("width") or 0) * int(item.get("height") or 0),
            float(item.get("duration") or 0.0),
        ),
        reverse=True,
    )[0]


def _download_result(result: dict[str, Any], output_path: str) -> Path:
    url = str(result.get("download_url") or "").strip()
    if not url:
        raise StockMediaError("Selected stock media has no download URL")
    output = _output_path_for_url(output_path, url, str(result.get("media_type") or "video"))
    output.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": "Lumeri/stock-media"})
    context = ssl.create_default_context(cafile=certifi.where())
    with urllib.request.urlopen(req, timeout=STOCK_MEDIA_TIMEOUT_SEC, context=context) as resp:
        data = resp.read()
        content_type = resp.headers.get("Content-Type", "")
    if not data:
        raise StockMediaError("Downloaded stock media is empty")
    if not output.suffix:
        output = output.with_suffix(_extension_for_content_type(content_type, result.get("media_type")) or ".mp4")
    output.write_bytes(data)
    try:
        output.chmod(0o600)
    except OSError:
        pass
    return output


def _maybe_import_to_media_library(path: Path, result: dict[str, Any], enabled: bool) -> dict[str, Any] | None:
    if not enabled:
        return None
    try:
        from gemia.public_identity import current_account_id
        from gemia.media_library import import_media

        account_id = current_account_id()
        if not account_id:
            return None
        original_name = f"{result.get('provider')}-{result.get('id')}{path.suffix}"
        return import_media(account_id, path, original_name=original_name)
    except Exception:
        return None


def _write_sidecar(path: Path, result: dict[str, Any], search: dict[str, Any], imported_asset: dict[str, Any] | None) -> None:
    sidecar = path.with_suffix(path.suffix + ".stock.json")
    payload = {
        "source": result,
        "search": {key: value for key, value in search.items() if key != "results"},
        "imported_asset_id": imported_asset.get("asset_id") if isinstance(imported_asset, dict) else None,
        "downloaded_path": str(path),
        "created_at": _utc_now(),
    }
    sidecar.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _output_path_for_url(output_path: str, url: str, media_type: str) -> Path:
    output = Path(output_path).expanduser()
    suffix = _extension_for_url(url) or (".mp4" if media_type == "video" else ".jpg")
    if output.suffix.lower() not in _allowed_suffixes(media_type):
        output = output.with_suffix(suffix)
    return output


def _extension_for_url(url: str) -> str:
    suffix = Path(urllib.parse.urlparse(url).path).suffix.lower()
    if suffix in {".mp4", ".mov", ".webm", ".jpg", ".jpeg", ".png", ".webp"}:
        return suffix
    return ""


def _extension_for_content_type(content_type: str, media_type: Any) -> str:
    guessed = mimetypes.guess_extension(str(content_type).split(";", 1)[0].strip())
    if guessed:
        return guessed
    return ".mp4" if media_type == "video" else ".jpg"


def _allowed_suffixes(media_type: str) -> set[str]:
    if media_type == "image":
        return {".jpg", ".jpeg", ".png", ".webp"}
    return {".mp4", ".mov", ".webm", ".m4v"}


def _urlopen_json(url: str, *, headers: dict[str, str] | None = None) -> dict[str, Any]:
    request_headers = {"User-Agent": "Lumeri/stock-media"}
    request_headers.update(headers or {})
    req = urllib.request.Request(url, headers=request_headers)
    context = ssl.create_default_context(cafile=certifi.where())
    try:
        with urllib.request.urlopen(req, timeout=STOCK_MEDIA_TIMEOUT_SEC, context=context) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="ignore")[:500]
        raise StockMediaError(f"HTTP {exc.code}: {body}") from exc


def _api_key(provider: str) -> str:
    if provider == "pexels":
        value = _first_secret("PEXELS_API_KEY", "GEMIA_PEXELS_API_KEY", config_keys=("pexels_api_key", "pexels_key"))
    elif provider == "pixabay":
        value = _first_secret("PIXABAY_API_KEY", "GEMIA_PIXABAY_API_KEY", config_keys=("pixabay_api_key", "pixabay_key"))
    else:
        value = ""
    if not value:
        raise StockMediaError(f"{provider} API key is not configured")
    return value


def _first_secret(*env_names: str, config_keys: tuple[str, ...]) -> str:
    for name in env_names:
        value = str(os.environ.get(name) or "").strip()
        if value:
            return value
    cfg = _read_config()
    for key in config_keys:
        value = str(cfg.get(key) or "").strip()
        if value:
            return value
    return ""


def _read_config() -> dict[str, Any]:
    try:
        path = Path.home() / ".gemia" / "config.json"
        payload = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _normalize_provider(provider: str) -> str:
    value = str(provider or "auto").strip().lower()
    if value in {"auto", "pexels", "pixabay"}:
        return value
    raise StockMediaError("provider must be auto, pexels, or pixabay")


def _normalize_media_type(media_type: str) -> str:
    value = str(media_type or "video").strip().lower()
    if value in {"photo", "photos", "image", "images", "picture"}:
        return "image"
    if value in {"video", "videos", "broll", "b-roll"}:
        return "video"
    raise StockMediaError("media_type must be video or image")


def _clean_query(query: str) -> str:
    text = re.sub(r"\s+", " ", str(query or "")).strip()
    if not text:
        raise StockMediaError("query is required")
    return text[:160]


def _pexels_user(item: dict[str, Any]) -> str:
    user = item.get("user")
    if isinstance(user, dict):
        return str(user.get("name") or "")
    return ""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stock_filename(prefix: str = "stock") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"
