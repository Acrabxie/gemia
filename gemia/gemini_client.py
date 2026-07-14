"""Multi-provider streaming client for Lumeri v3.

Supports five providers: vertex, gemini, claude, openrouter, openai.
Provider is resolved in this order:
  1. LUMERI_V3_PROVIDER env / config.json:lumeri_v3_provider  (explicit)
  2. First provider whose credentials exist in env or config.json (auto-probe)
  3. Falls through to openrouter (will raise if key is also absent)

Credential keys (env or ~/.gemia/config.json):
  vertex     — VERTEX_PROJECT / vertex_project  +  GCP ADC at ~/.config/gcloud/...
  gemini     — GEMINI_API_KEY  / gemini_api_key
  claude     — ANTHROPIC_API_KEY / anthropic_api_key
  openrouter — OPENROUTER_API_KEY / openrouter_api_key
  openai     — OPENAI_API_KEY / openai_api_key

Vertex, Gemini, OpenRouter, and OpenAI all use the OpenAI-compatible SSE
path.  Claude uses the Anthropic Messages API and is converted transparently:
outgoing tool defs and message history are rewritten to Claude format; incoming
SSE events are mapped back to the shared delta protocol.
"""
from __future__ import annotations

import asyncio
import json
import os
import ssl
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, AsyncIterator, Iterator

import certifi


_DEFAULT_URL = "https://openrouter.ai/api/v1/chat/completions"
_DEFAULT_MODEL = "google/gemini-3.1-pro"

# Per-provider default models (used when LUMERI_V3_MODEL is not set)
_DEFAULT_VERTEX_MODEL    = "google/gemini-3.1-pro"
_DEFAULT_GEMINI_MODEL    = "gemini-3.1-pro"
_DEFAULT_CLAUDE_MODEL    = "claude-3-5-sonnet"
_DEFAULT_OPENROUTER_MODEL = _DEFAULT_MODEL
_DEFAULT_OPENAI_MODEL    = "o1"

# Auto-probe priority: first provider with credentials wins
_PROVIDER_PRIORITY = ("vertex", "gemini", "claude", "openrouter", "openai")


def _read_config_key(field: str) -> str:
    try:
        path = Path.home() / ".gemia" / "config.json"
        if path.exists():
            data = json.loads(path.read_text())
            return data.get(field, "") or ""
    except Exception:
        return ""
    return ""


_OPEN_ATTEMPTS = 3
_OPEN_TIMEOUT = 20.0  # per attempt; a hung proxy/TLS handshake fails fast, then retries
_RETRY_BACKOFF = 1.2


def _open_with_retry(opener, req, *, timeout=_OPEN_TIMEOUT, attempts=_OPEN_ATTEMPTS, proxy: str | None = None):
    """Open a request, retrying transient transport failures.

    The proxy hop to Google occasionally drops a TLS handshake
    (UNEXPECTED_EOF / handshake timeout). Without this, one blip fails the
    whole turn. HTTP 4xx (other than 429) is surfaced immediately; connection
    errors and 429/5xx are retried with a short backoff.

    Args:
        opener: urllib opener with proxy configuration.
        req: urllib request.
        timeout: per-attempt timeout in seconds.
        attempts: number of retry attempts.
        proxy: optional proxy URL for diagnostic error messages.
    """
    import time
    import errno

    last_exc: Exception | None = None
    for attempt in range(attempts):
        try:
            return opener.open(req, timeout=timeout)
        except urllib.error.HTTPError as exc:
            if exc.code in (429, 500, 502, 503, 504) and attempt < attempts - 1:
                last_exc = exc
                time.sleep(_RETRY_BACKOFF * (attempt + 1))
                continue
            raise
        except (urllib.error.URLError, ssl.SSLError, TimeoutError, OSError) as exc:
            last_exc = exc
            if attempt < attempts - 1:
                time.sleep(_RETRY_BACKOFF * (attempt + 1))
                continue
            raise
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("unreachable")


_ADC_PATH = Path.home() / ".config" / "gcloud" / "application_default_credentials.json"
_vertex_token_cache: dict[str, Any] = {"access": None, "exp": 0.0}


def _probe_provider() -> str | None:
    """Return the first provider whose credentials exist, or None."""
    if (
        (os.environ.get("VERTEX_PROJECT") or _read_config_key("vertex_project"))
        and _ADC_PATH.exists()
    ):
        return "vertex"
    if os.environ.get("GEMINI_API_KEY") or _read_config_key("gemini_api_key"):
        return "gemini"
    if os.environ.get("ANTHROPIC_API_KEY") or _read_config_key("anthropic_api_key"):
        return "claude"
    if os.environ.get("OPENROUTER_API_KEY") or _read_config_key("openrouter_api_key"):
        return "openrouter"
    if os.environ.get("OPENAI_API_KEY") or _read_config_key("openai_api_key"):
        return "openai"
    return None


def _vertex_access_token(proxy: str | None) -> str:
    """Mint/refresh a GCP access token from authorized_user ADC.

    Stdlib only (no google-auth). Caches the token until ~5 min before
    expiry, so a long-running sidecar keeps a valid bearer without holding
    a static credential. Goes through the same proxy as the model calls.
    """
    import time
    from urllib.parse import urlencode

    tok = _vertex_token_cache
    if tok["access"] and time.time() < tok["exp"] - 300:
        return tok["access"]
    adc = json.loads(_ADC_PATH.read_text())
    data = urlencode({
        "grant_type": "refresh_token",
        "client_id": adc["client_id"],
        "client_secret": adc["client_secret"],
        "refresh_token": adc["refresh_token"],
    }).encode("utf-8")
    ctx = ssl.create_default_context(cafile=certifi.where())
    proxy_handler = (
        urllib.request.ProxyHandler({"https": proxy, "http": proxy})
        if proxy else urllib.request.ProxyHandler({})
    )
    opener = urllib.request.build_opener(
        proxy_handler, urllib.request.HTTPSHandler(context=ctx)
    )
    req = urllib.request.Request(
        "https://oauth2.googleapis.com/token", data=data, method="POST"
    )
    payload = json.loads(_open_with_retry(opener, req, timeout=30, proxy=proxy).read())
    tok["access"] = payload["access_token"]
    tok["exp"] = time.time() + float(payload.get("expires_in", 3600))
    return tok["access"]


# ── Claude format helpers ──────────────────────────────────────────────────

def _tools_to_claude(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """OpenAI tool defs → Anthropic tool defs."""
    result = []
    for t in tools:
        func = t.get("function") or {}
        result.append({
            "name": func.get("name", ""),
            "description": func.get("description", ""),
            "input_schema": func.get("parameters") or {"type": "object", "properties": {}},
        })
    return result


def _messages_to_claude(messages: list[dict[str, Any]]) -> tuple[str | None, list[dict[str, Any]]]:
    """Convert OpenAI-format messages to Claude format.

    Returns (system_prompt_or_None, claude_messages).
    Tool-result messages (role="tool") are folded into a single preceding
    user message so Claude's alternating-role contract is satisfied.
    """
    system: str | None = None
    out: list[dict[str, Any]] = []

    i = 0
    while i < len(messages):
        msg = messages[i]
        role = msg.get("role", "")

        if role == "system":
            system = msg.get("content") or ""
            i += 1
            continue

        if role == "user":
            content = msg.get("content")
            if isinstance(content, list):
                out.append({"role": "user", "content": content})
            else:
                out.append({"role": "user", "content": str(content or "")})
            i += 1
            continue

        if role == "assistant":
            tool_calls = msg.get("tool_calls")
            text = msg.get("content") or ""
            if tool_calls:
                parts: list[dict[str, Any]] = []
                if text:
                    parts.append({"type": "text", "text": str(text)})
                for tc in tool_calls:
                    func = tc.get("function") or {}
                    args_str = func.get("arguments") or "{}"
                    try:
                        input_obj = json.loads(args_str)
                    except Exception:
                        input_obj = {}
                    parts.append({
                        "type": "tool_use",
                        "id": tc.get("id") or "",
                        "name": func.get("name") or "",
                        "input": input_obj,
                    })
                out.append({"role": "assistant", "content": parts})
            else:
                out.append({"role": "assistant", "content": str(text)})
            i += 1
            continue

        if role == "tool":
            # Gather all consecutive tool results into one user message.
            tool_results: list[dict[str, Any]] = []
            while i < len(messages) and messages[i].get("role") == "tool":
                tr = messages[i]
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tr.get("tool_call_id") or "",
                    "content": str(tr.get("content") or ""),
                })
                i += 1
            out.append({"role": "user", "content": tool_results})
            continue

        i += 1  # skip unknown roles

    return system, out


def _parse_claude_stream(resp: Any) -> Iterator[dict[str, Any]]:
    """Parse Anthropic SSE stream → shared delta protocol."""
    # stop_reason → OpenAI finish_reason
    _STOP_MAP = {
        "end_turn": "stop",
        "tool_use": "tool_calls",
        "max_tokens": "length",
        "stop_sequence": "stop",
    }
    while True:
        line = resp.readline()
        if not line:
            break
        line = line.rstrip(b"\r\n")
        if not line or line.startswith(b"event:") or line.startswith(b":"):
            continue
        if not line.startswith(b"data:"):
            continue
        payload = line[5:].lstrip()
        try:
            chunk = json.loads(payload.decode("utf-8"))
        except json.JSONDecodeError:
            continue

        ctype = chunk.get("type", "")

        if ctype == "content_block_start":
            block = chunk.get("content_block") or {}
            if block.get("type") == "tool_use":
                yield {
                    "kind": "tool_call_start",
                    "index": int(chunk.get("index", 0)),
                    "id": block.get("id") or "",
                    "name": block.get("name") or "",
                }

        elif ctype == "content_block_delta":
            idx = int(chunk.get("index", 0))
            delta = chunk.get("delta") or {}
            if delta.get("type") == "text_delta":
                text = delta.get("text") or ""
                if text:
                    yield {"kind": "text_delta", "text": text}
            elif delta.get("type") == "input_json_delta":
                partial = delta.get("partial_json") or ""
                if partial:
                    yield {"kind": "tool_call_args_delta", "index": idx, "delta": partial}

        elif ctype == "message_delta":
            stop_reason = (chunk.get("delta") or {}).get("stop_reason")
            if stop_reason:
                yield {"kind": "finish", "reason": _STOP_MAP.get(stop_reason, stop_reason)}

        elif ctype == "error":
            err = chunk.get("error") or {}
            yield {"kind": "error", "error": f"Anthropic {err.get('type','error')}: {err.get('message','')}"}
            return


class GeminiClientV3:
    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        api_url: str = _DEFAULT_URL,
        proxy: str | None = None,
        timeout: float = 120.0,
    ) -> None:
        proxy_value = (
            proxy
            if proxy is not None
            else os.environ.get("OPENROUTER_PROXY")
            or _read_config_key("proxy")
            or ""
        ).strip()
        self.proxy = proxy_value or None
        self.timeout = float(timeout)

        # Provider resolution: explicit → auto-probe → openrouter (fail at key check)
        self.provider = (
            (os.environ.get("LUMERI_V3_PROVIDER") or _read_config_key("lumeri_v3_provider") or "").strip().lower()
            or _probe_provider()
            or "openrouter"
        )

        # Shared model override (highest priority across all providers)
        model_override = (
            model
            or os.environ.get("LUMERI_V3_MODEL")
            or _read_config_key("lumeri_v3_model")
        )

        if self.provider == "vertex":
            project = (
                os.environ.get("VERTEX_PROJECT") or _read_config_key("vertex_project")
            ).strip()
            if not project:
                raise RuntimeError("VERTEX_PROJECT required for vertex provider (env or config.json:vertex_project).")
            location = (
                os.environ.get("VERTEX_LOCATION") or _read_config_key("vertex_location") or "global"
            ).strip()
            host = (
                "aiplatform.googleapis.com"
                if location == "global"
                else f"{location}-aiplatform.googleapis.com"
            )
            self.api_url = (
                f"https://{host}/v1beta1/projects/{project}"
                f"/locations/{location}/endpoints/openapi/chat/completions"
            )
            self.model = model_override or _DEFAULT_VERTEX_MODEL
            self.api_key = ""  # Vertex uses per-call minted OAuth bearer

        elif self.provider == "gemini":
            self.api_key = (
                os.environ.get("GEMINI_API_KEY") or _read_config_key("gemini_api_key")
            ).strip()
            if not self.api_key:
                raise RuntimeError("GEMINI_API_KEY required for gemini provider (env or config.json:gemini_api_key).")
            self.api_url = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
            self.model = model_override or _DEFAULT_GEMINI_MODEL

        elif self.provider == "claude":
            self.api_key = (
                os.environ.get("ANTHROPIC_API_KEY") or _read_config_key("anthropic_api_key")
            ).strip()
            if not self.api_key:
                raise RuntimeError("ANTHROPIC_API_KEY required for claude provider (env or config.json:anthropic_api_key).")
            self.api_url = "https://api.anthropic.com/v1/messages"
            self.model = model_override or _DEFAULT_CLAUDE_MODEL

        elif self.provider == "openai":
            self.api_key = (
                os.environ.get("OPENAI_API_KEY") or _read_config_key("openai_api_key")
            ).strip()
            if not self.api_key:
                raise RuntimeError("OPENAI_API_KEY required for openai provider (env or config.json:openai_api_key).")
            self.api_url = "https://api.openai.com/v1/chat/completions"
            self.model = model_override or _DEFAULT_OPENAI_MODEL

        else:  # openrouter (default)
            self.api_key = (
                api_key
                or os.environ.get("OPENROUTER_API_KEY")
                or _read_config_key("openrouter_api_key")
            ).strip()
            if not self.api_key:
                raise RuntimeError(
                    "No AI provider credentials found. Set one of: "
                    "VERTEX_PROJECT+ADC, GEMINI_API_KEY, ANTHROPIC_API_KEY, "
                    "OPENROUTER_API_KEY, OPENAI_API_KEY "
                    "(env or ~/.gemia/config.json)."
                )
            self.model = model_override or _read_config_key("openrouter_model") or _DEFAULT_OPENROUTER_MODEL
            self.api_url = api_url

    async def stream_turn(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.7,
    ) -> AsyncIterator[dict[str, Any]]:
        """Stream one turn. Yields delta dicts:

        - ``{"kind": "text_delta", "text": str}``
        - ``{"kind": "tool_call_start", "index": int, "id": str, "name": str}``
        - ``{"kind": "tool_call_args_delta", "index": int, "delta": str}``
        - ``{"kind": "finish", "reason": str}``
        - ``{"kind": "error", "error": str}``
        """
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "temperature": temperature,
        }
        if tools:
            body["tools"] = tools

        loop = asyncio.get_running_loop()
        q: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

        _blocker = self._stream_blocking_claude if self.provider == "claude" else self._stream_blocking

        def producer() -> None:
            try:
                for ev in _blocker(body):
                    loop.call_soon_threadsafe(q.put_nowait, ev)
            except Exception as exc:
                loop.call_soon_threadsafe(
                    q.put_nowait,
                    {"kind": "error", "error": f"{type(exc).__name__}: {exc}"},
                )
            finally:
                loop.call_soon_threadsafe(q.put_nowait, None)

        threading.Thread(target=producer, daemon=True).start()

        while True:
            item = await q.get()
            if item is None:
                return
            yield item

    def _stream_blocking(self, body: dict[str, Any]) -> Iterator[dict[str, Any]]:
        bearer = (
            _vertex_access_token(self.proxy)
            if self.provider == "vertex"
            else self.api_key
        )
        headers = {
            "Authorization": f"Bearer {bearer}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "HTTP-Referer": "https://local-lumeri-desktop",
            "X-Title": "lumeri-v3",
        }
        req = urllib.request.Request(
            self.api_url,
            data=json.dumps(body).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        ssl_context = ssl.create_default_context(cafile=certifi.where())
        https_handler = urllib.request.HTTPSHandler(context=ssl_context)
        if self.proxy:
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({"https": self.proxy, "http": self.proxy}),
                https_handler,
            )
        else:
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({}),
                https_handler,
            )

        try:
            # Streaming: this one socket timeout governs BOTH the open (response
            # headers / time-to-first-byte) AND every subsequent body read (urllib
            # reuses it for each recv). It must therefore tolerate LLM TTFB and
            # inter-chunk gaps — use the configurable stream timeout, NOT the 20s
            # handshake-fastfail constant (that 20s caused "read operation timed
            # out (after 3 transport attempts)" on slow first tokens).
            resp = _open_with_retry(opener, req, timeout=self.timeout, proxy=self.proxy)
        except urllib.error.HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="ignore")
            yield {"kind": "error", "error": f"HTTP {exc.code}: {error_body[:600]}"}
            return
        except (urllib.error.URLError, ssl.SSLError, TimeoutError, OSError) as exc:
            # ECONNREFUSED to a localhost proxy (mihomo/clash down or restarting) is
            # the common cause; surface an actionable message instead of a bare
            # URLError. The errno lives on URLError.reason (the wrapped OSError), not
            # exc.__cause__ (which is None here), and ECONNREFUSED is platform-specific
            # (61 macOS/BSD, 111 Linux) — so key off errno.ECONNREFUSED, with a string
            # match as a belt-and-suspenders fallback.
            import errno as _errno

            underlying = getattr(exc, "reason", exc)
            refused = (
                getattr(underlying, "errno", None) == _errno.ECONNREFUSED
                or "Connection refused" in str(exc)
            )
            if self.proxy and refused:
                error_msg = (
                    f"local proxy {self.proxy} refused connection — is the proxy "
                    f"(mihomo/clash) running? ({exc})"
                )
            else:
                error_msg = str(exc)
            yield {
                "kind": "error",
                "error": (
                    f"{type(exc).__name__}: {error_msg} "
                    f"(after {_OPEN_ATTEMPTS} transport attempts)"
                ),
            }
            return

        with resp:
            while True:
                line = resp.readline()
                if not line:
                    break
                line = line.rstrip(b"\r\n")
                if not line:
                    continue
                if line.startswith(b":"):
                    continue
                if not line.startswith(b"data:"):
                    continue
                payload = line[5:].lstrip()
                if payload == b"[DONE]":
                    return
                try:
                    chunk = json.loads(payload.decode("utf-8"))
                except json.JSONDecodeError:
                    continue
                for event in _parse_chunk(chunk):
                    yield event


    def _stream_blocking_claude(self, body_openai: dict[str, Any]) -> Iterator[dict[str, Any]]:
        """Stream a turn via the Anthropic Messages API.

        Accepts the same OpenAI-format body that _stream_blocking uses, converts
        it to Claude format transparently, and yields the same delta protocol.
        """
        system, claude_messages = _messages_to_claude(body_openai.get("messages", []))
        tools = body_openai.get("tools")

        body: dict[str, Any] = {
            "model": self.model,
            "max_tokens": 8096,
            "messages": claude_messages,
            "stream": True,
        }
        if system:
            body["system"] = system
        if tools:
            body["tools"] = _tools_to_claude(tools)
        temp = body_openai.get("temperature")
        if temp is not None:
            body["temperature"] = temp

        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        req = urllib.request.Request(
            self.api_url,
            data=json.dumps(body).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        ssl_context = ssl.create_default_context(cafile=certifi.where())
        https_handler = urllib.request.HTTPSHandler(context=ssl_context)
        if self.proxy:
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({"https": self.proxy, "http": self.proxy}),
                https_handler,
            )
        else:
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({}),
                https_handler,
            )

        try:
            # Streaming: this one socket timeout governs BOTH the open (response
            # headers / time-to-first-byte) AND every subsequent body read (urllib
            # reuses it for each recv). It must therefore tolerate LLM TTFB and
            # inter-chunk gaps — use the configurable stream timeout, NOT the 20s
            # handshake-fastfail constant (that 20s caused "read operation timed
            # out (after 3 transport attempts)" on slow first tokens).
            resp = _open_with_retry(opener, req, timeout=self.timeout, proxy=self.proxy)
        except urllib.error.HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="ignore")
            yield {"kind": "error", "error": f"HTTP {exc.code}: {error_body[:600]}"}
            return
        except (urllib.error.URLError, ssl.SSLError, TimeoutError, OSError) as exc:
            import errno as _errno
            underlying = getattr(exc, "reason", exc)
            refused = (
                getattr(underlying, "errno", None) == _errno.ECONNREFUSED
                or "Connection refused" in str(exc)
            )
            if self.proxy and refused:
                error_msg = f"local proxy {self.proxy} refused connection — is the proxy running? ({exc})"
            else:
                error_msg = str(exc)
            yield {"kind": "error", "error": f"{type(exc).__name__}: {error_msg} (after {_OPEN_ATTEMPTS} transport attempts)"}
            return

        with resp:
            yield from _parse_claude_stream(resp)


def _parse_chunk(chunk: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """Parse one OpenAI-compatible streaming chunk into delta events."""
    choices = chunk.get("choices") or []
    if not choices:
        return
    choice = choices[0]
    delta = choice.get("delta") or {}
    content = delta.get("content")
    if isinstance(content, str) and content:
        yield {"kind": "text_delta", "text": content}
    tool_calls = delta.get("tool_calls")
    if isinstance(tool_calls, list):
        for tc in tool_calls:
            idx = int(tc.get("index", 0))
            func = tc.get("function") or {}
            name = func.get("name")
            args_delta = func.get("arguments")
            if name:
                start_event = {
                    "kind": "tool_call_start",
                    "index": idx,
                    "id": str(tc.get("id") or ""),
                    "name": str(name),
                }
                # Vertex/Gemini returns thought_signature here; pass it through
                # so the agent loop can echo it back on the next turn.
                extra = tc.get("extra_content")
                if extra is not None:
                    start_event["extra_content"] = extra
                yield start_event
            elif tc.get("extra_content") is not None:
                yield {
                    "kind": "tool_call_extra",
                    "index": idx,
                    "extra_content": tc.get("extra_content"),
                }
            if isinstance(args_delta, str) and args_delta:
                yield {"kind": "tool_call_args_delta", "index": idx, "delta": args_delta}
    finish_reason = choice.get("finish_reason")
    if finish_reason:
        yield {"kind": "finish", "reason": str(finish_reason)}


__all__ = ["GeminiClientV3"]
