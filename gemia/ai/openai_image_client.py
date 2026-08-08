"""OpenAI subscription image generation through the local Codex bridge.

The subscription bridge exposes a narrow OpenAI-compatible images endpoint.
It owns the OAuth/session material; this client only sends prompt/reference
bytes and receives the generated image bytes.  It is intentionally separate
from ``GeminiClientV3`` so an API-key or Vertex route cannot be selected by
accident.
"""
from __future__ import annotations

import asyncio
import base64
import json
import ssl
import urllib.error
import urllib.request
from typing import Any

import certifi


DEFAULT_ENDPOINT = "http://127.0.0.1:7808/v1/images/generations"
DEFAULT_TIMEOUT_SEC = 180.0


class OpenAIImageAPIError(RuntimeError):
    """Raised when the subscription bridge cannot produce an image."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class OpenAIImageClient:
    def __init__(
        self,
        *,
        endpoint: str = DEFAULT_ENDPOINT,
        proxy: str | None = None,
        timeout_sec: float = DEFAULT_TIMEOUT_SEC,
    ) -> None:
        self.endpoint = str(endpoint or DEFAULT_ENDPOINT).strip()
        self.proxy = str(proxy or "").strip()
        self.timeout_sec = float(timeout_sec)

    async def generate_image(
        self,
        *,
        prompt: str,
        model: str = "gpt-image-2",
        size: str | None = None,
        quality: str = "high",
        reference_images: list[bytes] | None = None,
        verb: str = "generate_image",
        estimated_cost_usd: float = 0.0,
        asset_id: str | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        del verb, estimated_cost_usd, asset_id
        payload: dict[str, Any] = {
            "model": model,
            "prompt": str(prompt),
            "quality": str(quality or "high"),
        }
        if size:
            payload["size"] = str(size)
        encoded_refs = [
            f"data:image/png;base64,{base64.b64encode(value).decode('ascii')}"
            for value in (reference_images or [])
        ]
        if encoded_refs:
            payload["input_images"] = encoded_refs
        body = await asyncio.to_thread(self._post_json, payload)
        data = body.get("data") if isinstance(body, dict) else None
        first = data[0] if isinstance(data, list) and data else {}
        b64 = str(first.get("b64_json") or "") if isinstance(first, dict) else ""
        if not b64:
            raise OpenAIImageAPIError("OpenAI subscription image response contained no image")
        try:
            image_bytes = base64.b64decode(b64, validate=True)
        except (ValueError, TypeError) as exc:
            raise OpenAIImageAPIError("OpenAI subscription image response was not valid base64") from exc
        return {
            "image_bytes": image_bytes,
            "mime_type": "image/png",
            "model": str(first.get("model") or model) if isinstance(first, dict) else model,
            "request_id": request_id,
            "raw_response_meta": {
                "model_text": None,
                "finish_reason": "completed",
                "usage_metadata": body.get("usage") if isinstance(body, dict) else None,
            },
        }

    def _post_json(self, payload: dict[str, Any]) -> dict[str, Any]:
        req = urllib.request.Request(
            self.endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "Lumeri/openai-subscription-image",
            },
            method="POST",
        )
        try:
            with self._opener().open(req, timeout=self.timeout_sec) as response:
                raw = response.read()
                status = int(getattr(response, "status", 200) or 200)
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            status = int(exc.code)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise OpenAIImageAPIError(f"OpenAI subscription image bridge unavailable: {exc}") from exc
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise OpenAIImageAPIError(
                f"OpenAI subscription image bridge returned invalid JSON (HTTP {status})",
                status=status,
            ) from exc
        if status >= 400 or not isinstance(body, dict):
            error = body.get("error") if isinstance(body, dict) else None
            message = error.get("message") if isinstance(error, dict) else None
            raise OpenAIImageAPIError(
                str(message or f"OpenAI subscription image request failed (HTTP {status})"),
                status=status,
            )
        return body

    def _opener(self) -> urllib.request.OpenerDirector:
        handlers: list[Any] = []
        if self.proxy:
            handlers.append(urllib.request.ProxyHandler({"http": self.proxy, "https": self.proxy}))
        else:
            handlers.append(urllib.request.ProxyHandler({}))
        context = ssl.create_default_context(cafile=certifi.where())
        handlers.append(urllib.request.HTTPSHandler(context=context))
        return urllib.request.build_opener(*handlers)


def endpoint_from_chat_url(value: str | None) -> str:
    """Convert the subscription bridge's chat URL to its image endpoint."""
    url = str(value or "").strip().rstrip("/")
    suffix = "/v1/chat/completions"
    if url.endswith(suffix):
        return f"{url[:-len(suffix)]}/v1/images/generations"
    return DEFAULT_ENDPOINT


__all__ = [
    "DEFAULT_ENDPOINT",
    "OpenAIImageAPIError",
    "OpenAIImageClient",
    "endpoint_from_chat_url",
]
