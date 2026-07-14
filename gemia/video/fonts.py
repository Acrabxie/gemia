"""Font catalog and resolution helpers for text layers."""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from PIL import ImageFont

FONT_EXTENSIONS = {".ttf", ".otf", ".ttc", ".otc"}
GOOGLE_FONTS_API_URL = "https://www.googleapis.com/webfonts/v1/webfonts"
_REMOTE_CACHE_TTL_SEC = 24 * 60 * 60
_DEFAULT_FONT_KEYWORDS = (
    "pingfang",
    "hiragino sans gb",
    "stheiti",
    "heiti",
    "songti",
    "source han",
    "noto sans cjk",
    "noto serif cjk",
    "microsoft yahei",
    "simhei",
    "helvetica",
    "arial",
    "dejavu",
)


@dataclass(frozen=True)
class FontRecord:
    id: str
    family: str
    style: str
    name: str
    path: str
    source: str
    is_default: bool = False
    supports_cjk_hint: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class GoogleFontRecord:
    family: str
    category: str
    variants: list[str]
    subsets: list[str]
    files: dict[str, str]
    version: str = ""
    last_modified: str = ""
    source: str = "google_fonts"

    def to_dict(self) -> dict[str, Any]:
        return {
            "family": self.family,
            "category": self.category,
            "variants": self.variants,
            "subsets": self.subsets,
            "files": self.files,
            "version": self.version,
            "last_modified": self.last_modified,
            "source": self.source,
        }


def font_roots(*, repo_root: str | Path | None = None, include_system: bool = True) -> list[Path]:
    """Return configured font roots in scan order."""
    roots: list[Path] = []
    env_roots = os.environ.get("GEMIA_FONT_ROOTS", "").strip()
    if env_roots:
        roots.extend(Path(item).expanduser() for item in env_roots.split(os.pathsep) if item.strip())

    roots.append(Path.home() / ".gemia" / "fonts")
    if repo_root is not None:
        root = Path(repo_root).expanduser()
        roots.extend([root / "assets" / "fonts", root / "fonts", root / "static" / "fonts"])

    if include_system:
        roots.extend(
            [
                Path.home() / "Library" / "Fonts",
                Path("/Library/Fonts"),
                Path("/System/Library/Fonts"),
                Path("/System/Library/Fonts/Supplemental"),
                Path("/usr/share/fonts"),
                Path("/usr/local/share/fonts"),
            ]
        )

    seen: set[str] = set()
    unique: list[Path] = []
    for root in roots:
        try:
            key = str(root.resolve())
        except OSError:
            key = str(root)
        if key in seen:
            continue
        seen.add(key)
        unique.append(root)
    return unique


def get_font_catalog(*, refresh: bool = False) -> list[FontRecord]:
    """Return the cached font catalog, scanning configured roots on demand."""
    if refresh:
        _cached_font_catalog.cache_clear()
    return _cached_font_catalog()


def font_catalog_payload(
    *,
    limit: int | None = None,
    refresh: bool = False,
    include_remote: bool = False,
    remote_limit: int = 120,
) -> dict[str, Any]:
    catalog = get_font_catalog(refresh=refresh)
    default_font = next((font for font in catalog if font.is_default), catalog[0] if catalog else None)
    fonts = catalog[:limit] if limit is not None else catalog
    payload: dict[str, Any] = {
        "fonts": [font.to_dict() for font in fonts],
        "count": len(catalog),
        "default_font": default_font.to_dict() if default_font else None,
        "roots": [str(root) for root in font_roots(repo_root=_repo_root())],
    }
    if include_remote:
        try:
            payload["google_fonts"] = google_fonts_payload(limit=remote_limit, refresh=refresh)
        except Exception as exc:
            payload["google_fonts"] = {"available": False, "error": str(exc), "fonts": []}
    return payload


def prompt_font_catalog(*, limit: int = 12) -> dict[str, Any]:
    """Return a compact planner-facing font catalog."""
    catalog = get_font_catalog()
    chosen = sorted(catalog, key=_font_sort_key)[:limit]
    payload: dict[str, Any] = {
        "default_font_id": next((font.id for font in catalog if font.is_default), None),
        "fonts": [
            {
                "font_id": font.id,
                "family": font.family,
                "style": font.style,
                "supports_cjk_hint": font.supports_cjk_hint,
            }
            for font in chosen
        ],
        "font_config_schema": {
            "font_id": "preferred stable id from this catalog",
            "family": "optional family fallback",
            "google_family": "optional Google Fonts family; backend downloads regular TTF when API key is configured",
            "path": "optional absolute font path",
            "size": "positive integer",
            "color": "RGBA floats in [0,1]",
            "padding": "integer >= 0",
        },
    }
    try:
        remote = get_google_fonts(sort="popularity")[:limit]
        if remote:
            payload["google_fonts"] = [
                {
                    "google_family": font.family,
                    "category": font.category,
                    "subsets": font.subsets[:8],
                    "variants": font.variants[:8],
                }
                for font in remote
            ]
    except Exception:
        pass
    return payload


def resolve_font_path(font_config: dict[str, Any] | None = None) -> str | None:
    """Resolve font_config path/id/family/name into an existing local font path."""
    cfg = dict(font_config or {})
    direct_path = str(cfg.get("path") or "").strip()
    if direct_path:
        path = Path(direct_path).expanduser()
        if path.exists() and path.is_file():
            return str(path.resolve())

    catalog = get_font_catalog()
    google_family = str(cfg.get("google_family") or cfg.get("googleFontFamily") or "").strip()
    if google_family:
        downloaded = download_google_font(
            google_family,
            variant=str(cfg.get("variant") or "regular"),
        )
        if downloaded:
            return downloaded

    if not catalog:
        return None

    queries = [
        str(cfg.get("font_id") or cfg.get("id") or "").strip(),
        str(cfg.get("family") or "").strip(),
        str(cfg.get("name") or "").strip(),
    ]
    for query in [item for item in queries if item]:
        query_key = _normalize(query)
        for font in catalog:
            if query_key in {
                _normalize(font.id),
                _normalize(font.family),
                _normalize(font.name),
                _normalize(Path(font.path).stem),
            }:
                return font.path
        for font in catalog:
            haystack = " ".join([font.id, font.family, font.name, Path(font.path).stem])
            if query_key and query_key in _normalize(haystack):
                return font.path

    default_font = next((font for font in catalog if font.is_default), catalog[0])
    return default_font.path


def google_fonts_payload(
    *,
    limit: int | None = 120,
    refresh: bool = False,
    sort: str = "popularity",
    subset: str | None = None,
    category: str | None = None,
    family: str | None = None,
) -> dict[str, Any]:
    """Return Google Fonts metadata when GEMIA_GOOGLE_FONTS_API_KEY is configured."""
    fonts = get_google_fonts(
        refresh=refresh,
        sort=sort,
        subset=subset,
        category=category,
        family=family,
    )
    selected = fonts[:limit] if limit is not None else fonts
    return {
        "available": True,
        "source": "google_fonts_developer_api",
        "count": len(fonts),
        "fonts": [font.to_dict() for font in selected],
    }


def get_google_fonts(
    *,
    refresh: bool = False,
    sort: str = "popularity",
    subset: str | None = None,
    category: str | None = None,
    family: str | None = None,
) -> list[GoogleFontRecord]:
    api_key = _google_fonts_api_key()
    if not api_key:
        raise RuntimeError("GEMIA_GOOGLE_FONTS_API_KEY is not configured")

    params = {
        "sort": sort,
        "subset": subset or "",
        "category": category or "",
        "family": family or "",
    }
    cache_path = _google_cache_path(params)
    if not refresh:
        cached = _read_google_cache(cache_path)
        if cached is not None:
            return cached

    query = {key: value for key, value in params.items() if value}
    query["key"] = api_key
    url = f"{GOOGLE_FONTS_API_URL}?{urllib.parse.urlencode(query)}"
    request = urllib.request.Request(url, headers={"Accept": "application/json"}, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"Google Fonts API HTTP {exc.code}: {body}") from exc
    items = payload.get("items")
    if not isinstance(items, list):
        raise RuntimeError("Google Fonts API response did not include items")
    records = [_google_record(item) for item in items if isinstance(item, dict)]
    _write_google_cache(cache_path, records)
    return records


def download_google_font(family: str, *, variant: str = "regular", refresh: bool = False) -> str | None:
    """Download a Google Fonts TTF/OTF file into ~/.gemia/fonts/google and return its path."""
    family = str(family or "").strip()
    if not family:
        return None
    records = get_google_fonts(refresh=refresh, family=family)
    record = next((item for item in records if item.family.lower() == family.lower()), records[0] if records else None)
    if record is None:
        return None
    url = record.files.get(variant) or record.files.get("regular") or next(iter(record.files.values()), "")
    if not url:
        return None
    suffix = Path(urllib.parse.urlparse(url).path).suffix.lower()
    if suffix not in {".ttf", ".otf"}:
        raise RuntimeError(f"Google font file is not TTF/OTF and cannot be rendered by Pillow: {url}")
    target_dir = Path.home() / ".gemia" / "fonts" / "google"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{_slug(record.family)}-{_slug(variant)}{suffix}"
    if target.exists() and not refresh:
        return str(target.resolve())
    request = urllib.request.Request(url.replace("http://", "https://"), method="GET")
    with urllib.request.urlopen(request, timeout=60) as response:
        target.write_bytes(response.read())
    get_font_catalog(refresh=True)
    return str(target.resolve())


@lru_cache(maxsize=1)
def _cached_font_catalog() -> list[FontRecord]:
    records: list[FontRecord] = []
    seen_paths: set[str] = set()
    for root in font_roots(repo_root=_repo_root()):
        if not root.exists() or not root.is_dir():
            continue
        source = _source_for_root(root)
        for path in sorted(_iter_font_files(root)):
            resolved = str(path.resolve())
            if resolved in seen_paths:
                continue
            seen_paths.add(resolved)
            record = _record_for_path(path, source=source)
            if record is not None:
                records.append(record)

    default_path = _choose_default_font_path(records)
    with_default = [
        FontRecord(
            id=font.id,
            family=font.family,
            style=font.style,
            name=font.name,
            path=font.path,
            source=font.source,
            is_default=font.path == default_path,
            supports_cjk_hint=font.supports_cjk_hint,
        )
        for font in records
    ]
    return sorted(with_default, key=_font_sort_key)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _google_fonts_api_key() -> str:
    key = os.environ.get("GEMIA_GOOGLE_FONTS_API_KEY", "").strip()
    if key:
        return key
    try:
        config_path = Path.home() / ".gemia" / "config.json"
        if config_path.exists():
            config = json.loads(config_path.read_text(encoding="utf-8"))
            return str(config.get("google_fonts_api_key") or "").strip()
    except Exception:
        pass
    return ""


def _google_cache_path(params: dict[str, str]) -> Path:
    sanitized = json.dumps({key: value for key, value in params.items() if value}, sort_keys=True)
    digest = hashlib.sha1(sanitized.encode("utf-8")).hexdigest()[:12]
    cache_dir = Path.home() / ".gemia" / "font-cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir / f"google-webfonts-{digest}.json"


def _read_google_cache(path: Path) -> list[GoogleFontRecord] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if time.time() - float(payload.get("fetched_at", 0.0)) > _REMOTE_CACHE_TTL_SEC:
            return None
        items = payload.get("items")
        if not isinstance(items, list):
            return None
        return [_google_record(item) for item in items if isinstance(item, dict)]
    except Exception:
        return None


def _write_google_cache(path: Path, records: list[GoogleFontRecord]) -> None:
    payload = {
        "fetched_at": time.time(),
        "items": [record.to_dict() for record in records],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _google_record(item: dict[str, Any]) -> GoogleFontRecord:
    files = item.get("files") if isinstance(item.get("files"), dict) else {}
    return GoogleFontRecord(
        family=str(item.get("family") or ""),
        category=str(item.get("category") or ""),
        variants=[str(value) for value in item.get("variants", []) if value],
        subsets=[str(value) for value in item.get("subsets", []) if value],
        files={str(key): str(value) for key, value in files.items()},
        version=str(item.get("version") or ""),
        last_modified=str(item.get("lastModified") or item.get("last_modified") or ""),
    )


def _iter_font_files(root: Path) -> list[Path]:
    return [path for path in root.rglob("*") if path.is_file() and path.suffix.lower() in FONT_EXTENSIONS]


def _record_for_path(path: Path, *, source: str) -> FontRecord | None:
    try:
        font = ImageFont.truetype(str(path), 18)
        family, style = font.getname()
    except OSError:
        return None
    family = str(family or path.stem).strip() or path.stem
    style = str(style or "Regular").strip() or "Regular"
    name = f"{family} {style}".strip()
    font_id = f"{_slug(name)}-{hashlib.sha1(str(path.resolve()).encode('utf-8')).hexdigest()[:8]}"
    supports_cjk_hint = _looks_cjk_capable(" ".join([family, style, path.name]))
    return FontRecord(
        id=font_id,
        family=family,
        style=style,
        name=name,
        path=str(path.resolve()),
        source=source,
        supports_cjk_hint=supports_cjk_hint,
    )


def _choose_default_font_path(records: list[FontRecord]) -> str | None:
    if not records:
        return None
    for keyword in _DEFAULT_FONT_KEYWORDS:
        keyword_key = _normalize(keyword)
        for record in records:
            text = _normalize(" ".join([record.family, record.name, Path(record.path).name]))
            if keyword_key in text:
                return record.path
    return records[0].path


def _font_sort_key(font: FontRecord) -> tuple[int, int, str, str]:
    return (
        0 if font.is_default else 1,
        0 if font.supports_cjk_hint else 1,
        font.family.lower(),
        font.style.lower(),
    )


def _source_for_root(root: Path) -> str:
    raw = str(root.expanduser())
    home = str(Path.home())
    if raw.startswith(home):
        return "user"
    if "System/Library/Fonts" in raw or raw == "/Library/Fonts":
        return "system"
    if str(_repo_root()) in raw:
        return "project"
    return "configured"


def _looks_cjk_capable(text: str) -> bool:
    key = _normalize(text)
    return any(
        marker in key
        for marker in (
            "pingfang",
            "hiragino",
            "heiti",
            "songti",
            "kaiti",
            "noto sans cjk",
            "source han",
            "yahei",
            "simhei",
            "stfangsong",
            "stkaiti",
            "stheiti",
            "stxihei",
            "stsong",
        )
    )


def _normalize(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().lower())


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "font"


__all__ = [
    "FontRecord",
    "GoogleFontRecord",
    "download_google_font",
    "font_catalog_payload",
    "font_roots",
    "get_font_catalog",
    "get_google_fonts",
    "google_fonts_payload",
    "prompt_font_catalog",
    "resolve_font_path",
]
