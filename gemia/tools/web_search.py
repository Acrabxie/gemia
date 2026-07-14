"""web_search / web_open -- controlled host-side web access.

The sandbox remains network-denied. These verbs run on the host, use the same
proxy config convention as fetch, and return compact structured data only. Raw
HTML is parsed and discarded before the result dict reaches the SSE/tool stream.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import certifi

from gemia.tools._context import ToolContext

_SEARCH_ENGINE = "duckduckgo_lite"
_SEARCH_URL = "https://lite.duckduckgo.com/lite/"
_MAX_SEARCH_BYTES = 2 * 1024 * 1024
_MAX_PAGE_BYTES = 3 * 1024 * 1024

# ── BYOK search-provider framework ───────────────────────────────────────────
# web_search is pluggable. Each provider reads its key(s) from ~/.gemia/config.json
# (via the existing _read_config) and issues a single request through the existing
# _build_opener() (proxy-aware). Adapters return a NORMALIZED list of
# {title, url, snippet} dicts (extra fields allowed). On ANY provider error the
# dispatcher falls back to the no-key duckduckgo scraper and records a note.
_USER_AGENT = "Lumeri/4.0 (+https://lumeri.local)"
_MAX_PROVIDER_BYTES = 2 * 1024 * 1024

# Order auto-detect probes config for a present key/URL. Paid BYOK engines rank
# first (a present key means the operator opted into that engine), then searxng —
# the self-hosted, keyless free default, auto-detected only when searxng_url is
# configured. duckduckgo needs no config and is the universal last-resort
# fallback, so it is never auto-detected (only the explicit/last-resort path).
_AUTO_DETECT_ORDER = ("tavily", "serper", "brave", "exa", "google_cse", "bing", "searxng")
# Providers selectable via the schema enum / search_provider config / per-call arg.
_VALID_PROVIDERS = (
    "auto",
    "tavily",
    "serper",
    "brave",
    "exa",
    "google_cse",
    "bing",
    "searxng",
    "duckduckgo",
)
# DuckDuckGo Lite returns ~10 results per page. Default to a full page (the
# previous default of 5 silently discarded half of it). Going past one page
# would require following the Lite "next page" form (extra round-trips), so
# the single-page maximum is the ceiling here.
_MAX_SEARCH_LIMIT = 10
_DEFAULT_SEARCH_LIMIT = _MAX_SEARCH_LIMIT
_DEFAULT_PAGE_CHARS = 6000
_MAX_PAGE_CHARS = 12000


def _read_config(key: str) -> str | None:
    config_path = Path.home() / ".gemia" / "config.json"
    if not config_path.exists():
        return None
    try:
        with open(config_path, encoding="utf-8") as f:
            data = json.load(f)
        value = data.get(key)
        return str(value) if value else None
    except Exception:
        return None


def _build_opener() -> urllib.request.OpenerDirector:
    proxy = os.environ.get("OPENROUTER_PROXY") or _read_config("proxy")
    ssl_context = ssl.create_default_context(cafile=certifi.where())
    https_handler = urllib.request.HTTPSHandler(context=ssl_context)

    if proxy:
        return urllib.request.build_opener(
            urllib.request.ProxyHandler({"https": proxy, "http": proxy}),
            https_handler,
        )
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        https_handler,
    )


def _clamp_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    if value is None or value == "":
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"expected integer value, got {value!r}") from exc
    return max(minimum, min(maximum, parsed))


def _slug(text: str, *, max_len: int = 48) -> str:
    lowered = text.lower()
    slug = re.sub(r"[^a-z0-9]+", "-", lowered).strip("-")
    if not slug:
        slug = "web"
    return slug[:max_len].strip("-") or "web"


def _trim(text: str, limit: int) -> str:
    compact = _space(text)
    if len(compact) <= limit:
        return compact
    return compact[: max(0, limit - 3)].rstrip() + "..."


def _space(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _host(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    return parsed.netloc.lower().removeprefix("www.")


def _class_set(attrs: list[tuple[str, str | None]]) -> set[str]:
    for key, value in attrs:
        if key == "class" and value:
            return set(value.split())
    return set()


def _attr(attrs: list[tuple[str, str | None]], name: str) -> str | None:
    for key, value in attrs:
        if key == name:
            return value
    return None


def _normalize_result_url(href: str | None) -> str | None:
    if not href:
        return None
    candidate = href.strip()
    if candidate.startswith("//"):
        candidate = "https:" + candidate
    elif candidate.startswith("/"):
        candidate = urllib.parse.urljoin("https://duckduckgo.com", candidate)

    parsed = urllib.parse.urlparse(candidate)
    if parsed.netloc.endswith("duckduckgo.com") and parsed.path.startswith("/l/"):
        uddg = urllib.parse.parse_qs(parsed.query).get("uddg")
        if uddg:
            candidate = uddg[0]
            parsed = urllib.parse.urlparse(candidate)

    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return urllib.parse.urlunparse(parsed)


class _DDGHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[dict[str, str]] = []
        self._in_title = False
        self._title_parts: list[str] = []
        self._pending_url: str | None = None
        self._snippet_depth = 0
        self._snippet_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        classes = _class_set(attrs)
        if tag == "a" and ({"result__a", "result-link"} & classes):
            self._in_title = True
            self._title_parts = []
            self._pending_url = _normalize_result_url(_attr(attrs, "href"))
            return
        if {"result__snippet", "result-snippet"} & classes:
            self._snippet_depth = 1
            self._snippet_parts = []
            return
        if self._snippet_depth:
            self._snippet_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if self._in_title and tag == "a":
            title = _space("".join(self._title_parts))
            if title and self._pending_url:
                self.results.append(
                    {
                        "title": _trim(title, 200),
                        "url": _trim(self._pending_url, 1000),
                        "snippet": "",
                        "source": _host(self._pending_url),
                    }
                )
            self._in_title = False
            self._title_parts = []
            self._pending_url = None
            return

        if self._snippet_depth:
            self._snippet_depth -= 1
            if self._snippet_depth == 0 and self.results:
                snippet = _trim("".join(self._snippet_parts), 500)
                if snippet and not self.results[-1].get("snippet"):
                    self.results[-1]["snippet"] = snippet
                self._snippet_parts = []

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._title_parts.append(data)
        elif self._snippet_depth:
            self._snippet_parts.append(data)


class _ReadableHTMLParser(HTMLParser):
    _SKIP_TAGS = {"script", "style", "noscript", "svg", "canvas", "template"}
    _BLOCK_TAGS = {
        "article", "aside", "blockquote", "br", "dd", "div", "dl", "dt",
        "figcaption", "footer", "h1", "h2", "h3", "h4", "h5", "h6",
        "header", "hr", "li", "main", "nav", "ol", "p", "pre", "section",
        "table", "td", "th", "tr", "ul",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.text_parts: list[str] = []
        self._skip_depth = 0
        self._in_title = False
        self._title_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1
            return
        if tag == "title":
            self._in_title = True
            self._title_parts = []
            return
        if not self._skip_depth and tag in self._BLOCK_TAGS:
            self.text_parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1
            return
        if tag == "title" and self._in_title:
            self.title = _trim("".join(self._title_parts), 200)
            self._in_title = False
            return
        if not self._skip_depth and tag in self._BLOCK_TAGS:
            self.text_parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._in_title:
            self._title_parts.append(data)
            return
        text = _space(data)
        if text:
            self.text_parts.append(text)
            self.text_parts.append(" ")

    def readable_text(self) -> str:
        lines = []
        for line in "".join(self.text_parts).splitlines():
            compact = _space(line)
            if compact:
                lines.append(compact)
        return "\n".join(lines)


def _write_json(ctx: ToolContext, prefix: str, key: str, payload: dict[str, Any]) -> str:
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:10]
    path = ctx.output_dir / "web" / f"{prefix}-{_slug(key)}-{digest}.json"
    rel_path = str(path.relative_to(ctx.output_dir))
    disk_payload = dict(payload)
    disk_payload["path"] = rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(disk_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return rel_path


def _read_response(resp: Any, *, max_bytes: int) -> tuple[bytes, str]:
    with resp:
        data = resp.read(max_bytes + 1)
        content_type = resp.headers.get("Content-Type", "application/octet-stream")
    if len(data) > max_bytes:
        raise ValueError(f"web response exceeded size limit: {max_bytes / 1024 / 1024:.0f} MB")
    return data, content_type


# ── provider resolution ──────────────────────────────────────────────────────


def _resolve_provider(args: dict[str, Any]) -> tuple[str, dict[str, str]]:
    """Decide which search engine serves this call and gather its credentials.

    Precedence:
      1. per-call ``args["provider"]`` (when given and not "auto")
      2. config ``search_provider``
      3. AUTO-DETECT: first provider in ``_AUTO_DETECT_ORDER`` whose key(s) are
         present in config
      4. "duckduckgo" (no key, universal fallback)

    Returns ``(provider_name, creds)``. ``creds`` is the (possibly empty) dict of
    resolved keys for that provider; duckduckgo always resolves to ``{}``.
    """
    requested = str(args.get("provider") or "").strip().lower()
    if requested and requested != "auto":
        if requested not in _VALID_PROVIDERS:
            raise ValueError(
                f"unknown search provider {requested!r}; "
                f"valid: {', '.join(p for p in _VALID_PROVIDERS if p != 'auto')}"
            )
        return requested, _provider_creds(requested)

    configured = (_read_config("search_provider") or "").strip().lower()
    if configured and configured != "auto":
        if configured in _VALID_PROVIDERS:
            return configured, _provider_creds(configured)
        # Unknown configured value: ignore and fall through to auto-detect.

    for name in _AUTO_DETECT_ORDER:
        creds = _provider_creds(name)
        if creds:
            return name, creds

    return "duckduckgo", {}


def _provider_creds(provider: str) -> dict[str, str]:
    """Read a provider's key(s) from config. Empty dict => not configured."""
    if provider == "duckduckgo":
        return {}
    if provider == "searxng":
        # SearXNG is keyless but needs a self-hosted instance URL. An optional
        # bearer token covers reverse-proxy-protected instances.
        url = _read_config("searxng_url")
        if not url:
            return {}
        creds = {"url": url}
        key = _read_config("searxng_api_key")
        if key:
            creds["key"] = key
        return creds
    if provider == "google_cse":
        key = _read_config("google_cse_key")
        cx = _read_config("google_cse_id")
        if key and cx:
            return {"key": key, "cx": cx}
        return {}
    config_key = {
        "tavily": "tavily_api_key",
        "brave": "brave_api_key",
        "serper": "serper_api_key",
        "exa": "exa_api_key",
        "bing": "bing_api_key",
    }.get(provider)
    if not config_key:
        return {}
    value = _read_config(config_key)
    return {"key": value} if value else {}


# ── provider HTTP helper ─────────────────────────────────────────────────────


def _request_json(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: dict[str, Any] | None = None,
    timeout: float = 20,
) -> Any:
    """Issue one proxy-aware request and parse the JSON response.

    Uses the existing _build_opener() so provider calls honour the configured
    proxy. Raises ValueError on transport/HTTP/parse failure so the dispatcher
    can fall back uniformly.
    """
    opener = _build_opener()
    req_headers = {"User-Agent": _USER_AGENT, "Accept": "application/json"}
    if headers:
        req_headers.update(headers)
    data: bytes | None = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        req_headers.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
    try:
        resp = opener.open(req, timeout=timeout)
    except urllib.error.HTTPError as exc:
        raise ValueError(f"HTTP {exc.code}: {exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise ValueError(f"transport error: {exc.reason}") from exc
    raw, _content_type = _read_response(resp, max_bytes=_MAX_PROVIDER_BYTES)
    try:
        return json.loads(raw.decode("utf-8", errors="replace"))
    except (ValueError, UnicodeError) as exc:
        raise ValueError(f"invalid JSON from provider: {exc}") from exc


def _norm(value: Any) -> str:
    return str(value) if value is not None else ""


# ── provider adapters: each returns a normalized list[{title, url, snippet}] ──


def _search_tavily(query: str, limit: int, creds: dict[str, str]) -> list[dict[str, str]]:
    payload = _request_json(
        "https://api.tavily.com/search",
        method="POST",
        body={"api_key": creds["key"], "query": query, "max_results": limit},
    )
    results = []
    for item in (payload.get("results") or [])[:limit]:
        results.append(
            {
                "title": _norm(item.get("title")),
                "url": _norm(item.get("url")),
                "snippet": _norm(item.get("content")),
            }
        )
    return results


def _search_brave(query: str, limit: int, creds: dict[str, str]) -> list[dict[str, str]]:
    url = "https://api.search.brave.com/res/v1/web/search?" + urllib.parse.urlencode(
        {"q": query, "count": limit}
    )
    payload = _request_json(
        url,
        method="GET",
        headers={
            "X-Subscription-Token": creds["key"],
            "Accept": "application/json",
        },
    )
    web = payload.get("web") or {}
    results = []
    for item in (web.get("results") or [])[:limit]:
        results.append(
            {
                "title": _norm(item.get("title")),
                "url": _norm(item.get("url")),
                "snippet": _norm(item.get("description")),
            }
        )
    return results


def _search_serper(query: str, limit: int, creds: dict[str, str]) -> list[dict[str, str]]:
    payload = _request_json(
        "https://google.serper.dev/search",
        method="POST",
        headers={"X-API-KEY": creds["key"], "Content-Type": "application/json"},
        body={"q": query, "num": limit},
    )
    results = []
    for item in (payload.get("organic") or [])[:limit]:
        results.append(
            {
                "title": _norm(item.get("title")),
                "url": _norm(item.get("link")),
                "snippet": _norm(item.get("snippet")),
            }
        )
    return results


def _search_google_cse(
    query: str, limit: int, creds: dict[str, str]
) -> list[dict[str, str]]:
    num = max(1, min(10, limit))
    url = "https://www.googleapis.com/customsearch/v1?" + urllib.parse.urlencode(
        {"key": creds["key"], "cx": creds["cx"], "q": query, "num": num}
    )
    payload = _request_json(url, method="GET")
    results = []
    for item in (payload.get("items") or [])[:limit]:
        results.append(
            {
                "title": _norm(item.get("title")),
                "url": _norm(item.get("link")),
                "snippet": _norm(item.get("snippet")),
            }
        )
    return results


def _search_bing(query: str, limit: int, creds: dict[str, str]) -> list[dict[str, str]]:
    url = "https://api.bing.microsoft.com/v7.0/search?" + urllib.parse.urlencode(
        {"q": query, "count": limit}
    )
    payload = _request_json(
        url,
        method="GET",
        headers={"Ocp-Apim-Subscription-Key": creds["key"]},
    )
    web_pages = payload.get("webPages") or {}
    results = []
    for item in (web_pages.get("value") or [])[:limit]:
        results.append(
            {
                "title": _norm(item.get("name")),
                "url": _norm(item.get("url")),
                "snippet": _norm(item.get("snippet")),
            }
        )
    return results


def _search_exa(query: str, limit: int, creds: dict[str, str]) -> list[dict[str, str]]:
    payload = _request_json(
        "https://api.exa.ai/search",
        method="POST",
        headers={"x-api-key": creds["key"], "Content-Type": "application/json"},
        body={"query": query, "numResults": limit},
    )
    results = []
    for item in (payload.get("results") or [])[:limit]:
        snippet = item.get("text") or item.get("snippet") or ""
        results.append(
            {
                "title": _norm(item.get("title")),
                "url": _norm(item.get("url")),
                "snippet": _norm(snippet),
            }
        )
    return results


def _search_searxng(query: str, limit: int, creds: dict[str, str]) -> list[dict[str, str]]:
    """Query a self-hosted SearXNG instance via its JSON API.

    SearXNG is a keyless metasearch engine — it aggregates Google/Bing/DuckDuckGo/
    etc. and returns JSON at ``{base}/search?format=json``. ``creds["url"]`` is the
    instance base URL; ``creds["key"]`` (optional) adds a bearer token for
    reverse-proxy-protected instances. Each result carries ``content`` as snippet.
    """
    base = creds["url"].rstrip("/")
    endpoint = base if base.endswith("/search") else base + "/search"
    url = endpoint + "?" + urllib.parse.urlencode(
        {"q": query, "format": "json", "safesearch": "1"}
    )
    headers = {"Accept": "application/json"}
    key = creds.get("key")
    if key:
        headers["Authorization"] = f"Bearer {key}"
    payload = _request_json(url, method="GET", headers=headers)
    results = []
    for item in (payload.get("results") or [])[:limit]:
        results.append(
            {
                "title": _norm(item.get("title")),
                "url": _norm(item.get("url")),
                "snippet": _norm(item.get("content")),
            }
        )
    return results


_PROVIDER_ADAPTERS = {
    "tavily": _search_tavily,
    "brave": _search_brave,
    "serper": _search_serper,
    "google_cse": _search_google_cse,
    "bing": _search_bing,
    "exa": _search_exa,
    "searxng": _search_searxng,
}


def _search_duckduckgo(query: str, limit: int) -> list[dict[str, str]]:
    """The no-key DuckDuckGo Lite scraper (existing behaviour)."""
    opener = _build_opener()
    url = _SEARCH_URL + "?" + urllib.parse.urlencode({"q": query})
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": _USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
        },
    )
    try:
        resp = opener.open(req, timeout=20)
    except urllib.error.HTTPError as exc:
        raise ValueError(f"web_search HTTP {exc.code}: {exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise ValueError(f"web_search transport error: {exc.reason}") from exc
    data, _content_type = _read_response(resp, max_bytes=_MAX_SEARCH_BYTES)
    parser = _DDGHTMLParser()
    parser.feed(data.decode("utf-8", errors="replace"))
    return parser.results[:limit]


async def dispatch(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Search the public web (BYOK pluggable) and return compact results."""
    query = str(args.get("query") or "").strip()
    if not query:
        raise ValueError("web_search requires a non-empty 'query' argument")
    limit = _clamp_int(
        args.get("limit"),
        default=_DEFAULT_SEARCH_LIMIT,
        minimum=1,
        maximum=_MAX_SEARCH_LIMIT,
    )

    provider, creds = _resolve_provider(args)
    served_by = provider
    fallback_note: str | None = None
    results: list[dict[str, str]] = []

    if provider != "duckduckgo":
        adapter = _PROVIDER_ADAPTERS[provider]

        def _run_provider() -> list[dict[str, str]]:
            return adapter(query, limit, creds)

        try:
            results = await asyncio.to_thread(_run_provider)
        except Exception as exc:  # network/auth/parse -> fall back, never hard-fail
            fallback_note = (
                f"provider {provider!r} failed ({exc}); fell back to duckduckgo"
            )
            served_by = "duckduckgo"

    if served_by == "duckduckgo":
        def _run_ddg() -> list[dict[str, str]]:
            return _search_duckduckgo(query, limit)

        try:
            results = await asyncio.to_thread(_run_ddg)
        except Exception as exc:
            raise ValueError(f"web_search failed for query {query!r}: {exc}") from exc

    engine = _SEARCH_ENGINE if served_by == "duckduckgo" else served_by
    payload = {
        "query": query,
        "engine": engine,
        "provider": served_by,
        "result_count": len(results),
        "results": results,
        "summary": f"found {len(results)} web results for {query!r}",
    }
    if fallback_note:
        payload["fallback"] = fallback_note
    payload["path"] = _write_json(ctx, "web-search", query, payload)
    return payload


async def dispatch_open(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Open an https web page and return readable text, not raw HTML."""
    url = str(args.get("url") or "").strip()
    if not url:
        raise ValueError("web_open requires a non-empty 'url' argument")
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError("web_open requires an https:// URL")

    max_chars = _clamp_int(
        args.get("max_chars"),
        default=_DEFAULT_PAGE_CHARS,
        minimum=500,
        maximum=_MAX_PAGE_CHARS,
    )

    opener = _build_opener()

    def _blocking() -> tuple[bytes, str]:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Lumeri/4.0 (+https://lumeri.local)",
                "Accept": "text/html,text/plain,application/xhtml+xml;q=0.9,*/*;q=0.1",
            },
        )
        try:
            resp = opener.open(req, timeout=25)
        except urllib.error.HTTPError as exc:
            raise ValueError(f"web_open HTTP {exc.code}: {exc.reason}") from exc
        except urllib.error.URLError as exc:
            raise ValueError(f"web_open transport error: {exc.reason}") from exc
        return _read_response(resp, max_bytes=_MAX_PAGE_BYTES)

    try:
        body, content_type = await asyncio.to_thread(_blocking)
    except Exception as exc:
        raise ValueError(f"web_open failed for {url[:80]}: {exc}") from exc

    lowered_type = content_type.lower().split(";", 1)[0].strip()
    decoded = body.decode("utf-8", errors="replace")
    if lowered_type in {"text/plain", "text/markdown"}:
        title = ""
        text = decoded
    elif lowered_type in {"", "text/html", "application/xhtml+xml"} or "html" in lowered_type:
        parser = _ReadableHTMLParser()
        parser.feed(decoded)
        title = parser.title
        text = parser.readable_text()
    else:
        raise ValueError(
            f"web_open only reads text/html or text/plain pages; got {content_type!r}. "
            "Use fetch for downloadable files."
        )

    content = _trim(text, max_chars)
    payload = {
        "url": url,
        "source": _host(url),
        "title": title,
        "content_type": content_type,
        "content": content,
        "content_chars": len(content),
    }
    title_part = f" ({title})" if title else ""
    payload["summary"] = f"read {len(content)} chars from {_host(url)}{title_part}"
    payload["path"] = _write_json(ctx, "web-open", url, payload)
    return payload


__all__ = ["dispatch", "dispatch_open", "_resolve_provider"]
