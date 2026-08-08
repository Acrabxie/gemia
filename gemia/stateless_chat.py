"""One-shot model calls for a client-owned workspace.

This module deliberately has no ProjectStore, SessionManager, or filesystem
dependency.  It turns a bounded chat transcript into one subscription-bridge
request and returns the resulting text; the caller owns persistence.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any


_BRIDGE_URL = "http://127.0.0.1:7808/v1/chat/completions"
_MAX_MESSAGES = 80
_MAX_CONTENT_CHARS = 120_000


class StatelessChatError(RuntimeError):
    pass


def _messages(value: object) -> list[dict[str, str]]:
    if not isinstance(value, list):
        raise StatelessChatError("messages must be a list")
    result: list[dict[str, str]] = []
    total = 0
    for item in value[-_MAX_MESSAGES:]:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "").strip()
        content = item.get("content")
        if role not in {"system", "user", "assistant"} or not isinstance(content, str):
            continue
        text = content.strip()
        if not text:
            continue
        total += len(text)
        if total > _MAX_CONTENT_CHARS:
            raise StatelessChatError("message context is too large")
        result.append({"role": role, "content": text})
    if not result:
        raise StatelessChatError("messages must contain text")
    return result


def complete(payload: object) -> dict[str, str]:
    """Call the local subscription bridge without creating a Lumeri session."""
    if not isinstance(payload, dict):
        raise StatelessChatError("request body must be an object")
    messages = _messages(payload.get("messages"))
    model = str(payload.get("model") or os.environ.get("LUMERI_IPAD_MODEL") or "gpt-5.6-sol").strip()
    if not model or len(model) > 128:
        raise StatelessChatError("invalid model")
    # The local Codex bridge deliberately exposes SSE for every completion.
    # Consume it here and return one ordinary response to the iPad gateway.
    request_body = json.dumps({"model": model, "messages": messages, "stream": True}).encode("utf-8")
    request = urllib.request.Request(
        _BRIDGE_URL,
        data=request_body,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=180) as response:  # nosec B310: fixed loopback bridge
            chunks: list[str] = []
            failure = ""
            for raw_line in response:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line.startswith("data: "):
                    continue
                event = line[6:]
                if event == "[DONE]":
                    break
                try:
                    frame = json.loads(event)
                except json.JSONDecodeError:
                    continue
                error = frame.get("error")
                if error:
                    failure = str(error.get("message") if isinstance(error, dict) else error)
                    continue
                delta = ((frame.get("choices") or [{}])[0].get("delta") or {})
                text = delta.get("content")
                if isinstance(text, str):
                    chunks.append(text)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:400]
        raise StatelessChatError(f"model request failed ({exc.code}): {detail}") from exc
    except OSError as exc:
        raise StatelessChatError("local model bridge is unavailable") from exc
    try:
        text = "".join(chunks)
    except (KeyError, IndexError, TypeError) as exc:
        raise StatelessChatError("model bridge returned an invalid response") from exc
    if failure:
        raise StatelessChatError(f"model request failed: {failure[:400]}")
    if not text.strip():
        raise StatelessChatError("model returned no text")
    return {"text": text.strip(), "model": model}
