"""Async client for Google GenAI image models on **Vertex AI**.

Talks to Vertex AI's native ``:generateContent`` endpoint for Nano Banana 2
(Gemini 3.1 Flash Image). Distinct from ``gemia/gemini_client.py`` (which
drives the orchestration LLM over the OpenAI-compatible surface), but it
REUSES that module's Vertex auth: one authorized_user ADC, one minted+cached
OAuth bearer, one Google Cloud project / billing source. No AI Studio key,
no second credential, no env-var fallback.

Auth model (Vertex, reusing orchestration — see gemia/gemini_client.py):
    bearer   = gemia.gemini_client._vertex_access_token(proxy)   # ADC → OAuth
    project  = $VERTEX_PROJECT  | config["vertex_project"]
    location = $VERTEX_LOCATION | config["vertex_location"] | "global"

Network model:
    - POST https://{host}/{ver}/projects/{project}/locations/{loc}/
      publishers/google/models/{model}:generateContent
      host = aiplatform.googleapis.com (global) | {loc}-aiplatform.googleapis.com
    - Goes through ~/.gemia/config.json["proxy"] / $OPENROUTER_PROXY — the
      same FlClash hop orchestration uses, so the mainland-China route to
      aiplatform.googleapis.com does not drop mid-call.
    - urllib + run_in_executor — no httpx, no google SDK. Stdlib only,
      sandbox/v4 friendly.

Errors are NEVER swallowed. Non-2xx / transport failures raise
``VertexAPIError`` with the response body tail; the dispatcher turns that
into a ``tool_exec_error`` for the agent loop, exactly like ffmpeg failures.
The host does NOT auto-retry a paid generation (a retry after a charged-but-
dropped response would double-bill); the money-leak audit below makes any
submitted-without-completed call visible instead.

``generate_image`` uses ``:generateContent``. Veo and Lyria share the same
transport/auth helpers: Lyria uses ``:predict`` and Veo uses
``:predictLongRunning`` + ``:fetchPredictOperation``.
"""
from __future__ import annotations

import asyncio
import json
import os
import ssl
import sys
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import certifi

# Reuse the orchestration Vertex auth: same ADC, same minted+cached OAuth
# bearer, same project/billing. No new credential surface.
from gemia.gemini_client import _vertex_access_token, _ADC_PATH


_DEFAULT_IMAGE_MODEL = "gemini-3.1-flash-image"  # Nano Banana 2
_DEFAULT_API_VERSION = "v1beta1"  # Vertex; preview models + image config live here
_DEFAULT_LOCATION = "global"
_DEFAULT_TIMEOUT_SEC = 60.0


# ── Money-leak audit ──────────────────────────────────────────────────
#
# Every paid provider call writes a pre-call "submitted" record BEFORE the
# HTTP request leaves, then "completed" on a parsed result, or "failed" on
# any exception. A request_id with "submitted" but no "completed" is a
# suspected money leak (we may have been charged but did not receive/use
# the result). This is intentionally append-only JSONL, not a reconciliation
# system — it is the smallest mechanism that makes a leak visible instead of
# silently swallowed. Idempotent retry belongs to the later Veo async phase.


def _api_audit_path() -> Path:
    override = os.environ.get("GEMIA_V3_API_AUDIT", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".gemia" / "v3" / "api-calls.jsonl"


def _append_audit(record: dict[str, Any]) -> None:
    """Append one audit line. Never raises — but warns to stderr on failure
    so a broken audit log surfaces rather than hiding leaks."""
    try:
        path = _api_audit_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        line = {"ts": datetime.now(timezone.utc).isoformat(), **record}
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(line, ensure_ascii=False) + "\n")
    except Exception as exc:  # pragma: no cover - defensive
        print(f"[api_audit] failed to write audit record: {exc}", file=sys.stderr, flush=True)


def read_api_calls(path: str | Path | None = None) -> list[dict[str, Any]]:
    """Read all audit records (oldest first). Empty list if the log is absent."""
    p = Path(path) if path else _api_audit_path()
    if not p.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def find_suspected_leaks(path: str | Path | None = None) -> list[dict[str, Any]]:
    """Return request_ids that were submitted but never completed.

    A 'failed' record still counts as a suspect (submitted + failed, no
    completed) because a sync request can fail AFTER the provider charged.
    A submitted-only record (process died mid-call) is also a suspect.
    """
    by_id: dict[str, list[dict[str, Any]]] = {}
    order: list[str] = []
    for rec in read_api_calls(path):
        rid = str(rec.get("request_id") or "")
        if not rid:
            continue
        if rid not in by_id:
            by_id[rid] = []
            order.append(rid)
        by_id[rid].append(rec)
    leaks: list[dict[str, Any]] = []
    for rid in order:
        recs = by_id[rid]
        statuses = {r.get("status") for r in recs}
        if "submitted" in statuses and "completed" not in statuses:
            leaks.append({"request_id": rid, "records": recs})
    return leaks


class VertexAPIError(RuntimeError):
    """Raised on any non-2xx response or transport failure from Vertex."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        body_tail: str = "",
    ) -> None:
        super().__init__(message)
        self.status = status
        self.body_tail = body_tail


class VertexAuthMissingError(VertexAPIError):
    """Raised when Vertex auth prerequisites are missing.

    Either ``VERTEX_PROJECT`` is unset or the authorized_user ADC file is
    absent. Distinct class so the dispatcher can surface a clear,
    user-actionable error to the model (and the model to the user) rather
    than a generic auth failure that looks like a transient network issue.
    """


def _read_config(field: str) -> str:
    try:
        path = Path.home() / ".gemia" / "config.json"
        if not path.exists():
            return ""
        data = json.loads(path.read_text())
        value = data.get(field, "")
        return str(value or "").strip()
    except Exception:
        return ""


class GoogleGenAIClient:
    """Vertex AI image client. Async surface, blocking I/O under the hood.

    Project / location / proxy resolve the SAME way orchestration's
    ``GeminiClientV3`` does, so image generation and the LLM share one
    credential and one billing source.
    """

    def __init__(
        self,
        *,
        project: str | None = None,
        location: str | None = None,
        proxy: str | None = None,
        timeout_sec: float = _DEFAULT_TIMEOUT_SEC,
        api_version: str = _DEFAULT_API_VERSION,
    ) -> None:
        resolved_project = (
            project
            or os.environ.get("VERTEX_PROJECT")
            or _read_config("vertex_project")
        ).strip()
        if not resolved_project:
            raise VertexAuthMissingError(
                "VERTEX_PROJECT is not set (env VERTEX_PROJECT or "
                "~/.gemia/config.json:vertex_project). Vertex image generation "
                "needs the Google Cloud project that holds the billing/credit. "
                "It reuses the orchestration credential — there is no separate key."
            )
        if not _ADC_PATH.exists():
            raise VertexAuthMissingError(
                f"Application Default Credentials not found at {_ADC_PATH}. "
                "Run `gcloud auth application-default login` (authorized_user). "
                "Vertex image generation reuses the same ADC as orchestration."
            )
        resolved_location = (
            location
            or os.environ.get("VERTEX_LOCATION")
            or _read_config("vertex_location")
            or _DEFAULT_LOCATION
        ).strip()
        host = (
            "aiplatform.googleapis.com"
            if resolved_location == "global"
            else f"{resolved_location}-aiplatform.googleapis.com"
        )
        self.project = resolved_project
        self.location = resolved_location
        self.api_version = (api_version or _DEFAULT_API_VERSION).strip()
        self.base_url = (
            f"https://{host}/{self.api_version}/projects/{resolved_project}"
            f"/locations/{resolved_location}/publishers/google/models"
        )
        if proxy is None:
            proxy = os.environ.get("OPENROUTER_PROXY") or _read_config("proxy")
        self.proxy = (proxy or "").strip() or None
        self.timeout_sec = float(timeout_sec)

    # ── public surface ────────────────────────────────────────────────

    async def generate_image(
        self,
        *,
        prompt: str,
        model: str = _DEFAULT_IMAGE_MODEL,
        aspect_ratio: str | None = None,
        image_size: str = "2K",
        reference_images: list[bytes] | None = None,
        verb: str = "generate_image",
        estimated_cost_usd: float = 0.0,
        asset_id: str | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """Call Nano Banana 2 on Vertex (synchronous, LRO-free generateContent).

        Wraps the paid call in money-leak audit: a "submitted" record is
        written BEFORE the HTTP request leaves; "completed" on a parsed
        result; "failed" on any exception (then re-raised). The returned
        dict includes the ``request_id`` so the caller can correlate.

        Returns:
            {
              "image_bytes": bytes,        # decoded PNG/JPEG payload
              "mime_type": str,
              "model": str,
              "raw_response_meta": dict,   # candidates/safetyRatings minus inlineData
              "request_id": str,
            }

        Raises:
            VertexAPIError on HTTP failure or malformed response.
        """
        request_id = request_id or uuid.uuid4().hex
        _append_audit(
            {
                "request_id": request_id,
                "verb": verb,
                "provider": "vertex",
                "model": model,
                "location": self.location,
                "estimated_cost_usd": float(estimated_cost_usd),
                "status": "submitted",
            }
        )

        body: dict[str, Any] = {
            # Vertex requires an explicit role on each content entry
            # ("Please use a valid role: user, model") — AI Studio was lenient.
            "contents": [
                {
                    "role": "user",
                    "parts": _build_content_parts(prompt, reference_images or []),
                }
            ],
            "generationConfig": _build_image_generation_config(
                aspect_ratio=aspect_ratio, image_size=image_size
            ),
        }
        path = f"{model}:generateContent"
        try:
            response_json = await self._post_json(path, body)
            payload = _extract_image_payload(response_json, model=model)
        except Exception as exc:
            _append_audit(
                {
                    "request_id": request_id,
                    "status": "failed",
                    "error": f"{type(exc).__name__}: {exc}"[:500],
                }
            )
            raise

        _append_audit(
            {
                "request_id": request_id,
                "status": "completed",
                "actual_asset_id": asset_id,
                "size_bytes": len(payload["image_bytes"]),
                "finish_reason": payload["raw_response_meta"].get("finish_reason"),
            }
        )
        payload["request_id"] = request_id
        return payload

    async def predict(
        self,
        *,
        model: str,
        instances: list[dict[str, Any]],
        parameters: dict[str, Any] | None = None,
        verb: str,
        estimated_cost_usd: float = 0.0,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """Call Vertex model ``:predict`` for synchronous media models.

        Used by Lyria. The response JSON is returned to the dispatcher, which
        is responsible for decoding/writing bytes and scrubbing event payloads.
        """
        request_id = request_id or uuid.uuid4().hex
        _append_audit(
            {
                "request_id": request_id,
                "verb": verb,
                "provider": "vertex",
                "model": model,
                "location": self.location,
                "estimated_cost_usd": float(estimated_cost_usd),
                "status": "submitted",
            }
        )
        body: dict[str, Any] = {"instances": instances}
        if parameters:
            body["parameters"] = parameters
        try:
            response_json = await self._post_json(f"{model}:predict", body)
        except Exception as exc:
            _append_audit(
                {
                    "request_id": request_id,
                    "status": "failed",
                    "error": f"{type(exc).__name__}: {exc}"[:500],
                }
            )
            raise
        _append_audit({"request_id": request_id, "status": "completed"})
        response_json["_lumeri_request_id"] = request_id
        return response_json

    async def predict_long_running(
        self,
        *,
        model: str,
        instances: list[dict[str, Any]],
        parameters: dict[str, Any] | None = None,
        verb: str,
        estimated_cost_usd: float = 0.0,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """Submit a Vertex ``:predictLongRunning`` operation.

        Used by Veo. This audits the paid submission request only. Polling the
        operation is separate and not charged as a new generation.
        """
        request_id = request_id or uuid.uuid4().hex
        _append_audit(
            {
                "request_id": request_id,
                "verb": verb,
                "provider": "vertex",
                "model": model,
                "location": self.location,
                "estimated_cost_usd": float(estimated_cost_usd),
                "status": "submitted",
            }
        )
        body: dict[str, Any] = {"instances": instances}
        if parameters:
            body["parameters"] = parameters
        try:
            response_json = await self._post_json(f"{model}:predictLongRunning", body)
        except Exception as exc:
            _append_audit(
                {
                    "request_id": request_id,
                    "status": "failed",
                    "error": f"{type(exc).__name__}: {exc}"[:500],
                }
            )
            raise
        _append_audit(
            {
                "request_id": request_id,
                "status": "completed",
                "operation_name": response_json.get("name"),
            }
        )
        response_json["_lumeri_request_id"] = request_id
        return response_json

    async def fetch_predict_operation(
        self,
        *,
        model: str,
        operation_name: str,
    ) -> dict[str, Any]:
        """Poll a Vertex ``:predictLongRunning`` operation."""
        return await self._post_json(
            f"{model}:fetchPredictOperation",
            {"operationName": operation_name},
        )

    # ── transport ─────────────────────────────────────────────────────

    async def _post_json(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.base_url}/{path}"
        encoded = json.dumps(body).encode("utf-8")
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, self._post_json_blocking, url, encoded
        )

    def _post_json_blocking(self, url: str, data: bytes) -> dict[str, Any]:
        # Mint/refresh the Vertex bearer in the executor thread (cached;
        # refresh is rare). Reuses orchestration's ADC token + module cache.
        bearer = _vertex_access_token(self.proxy)
        headers = {
            "Authorization": f"Bearer {bearer}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
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
            resp = opener.open(req, timeout=self.timeout_sec)
        except urllib.error.HTTPError as exc:
            body_tail = exc.read().decode("utf-8", errors="ignore")[-1600:]
            raise VertexAPIError(
                f"Vertex HTTP {exc.code} on {url.rsplit('/', 1)[-1]}",
                status=exc.code,
                body_tail=body_tail,
            ) from exc
        except urllib.error.URLError as exc:
            raise VertexAPIError(
                f"Vertex transport error: {exc.reason}",
                status=None,
                body_tail="",
            ) from exc
        with resp:
            raw = resp.read()
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            tail = raw[-1600:].decode("utf-8", errors="ignore")
            raise VertexAPIError(
                f"Vertex returned non-JSON response: {exc}",
                status=getattr(resp, "status", None),
                body_tail=tail,
            ) from exc


# ── helpers ──────────────────────────────────────────────────────────


def _build_content_parts(
    prompt: str, reference_images: list[bytes]
) -> list[dict[str, Any]]:
    parts: list[dict[str, Any]] = [{"text": str(prompt or "")}]
    for ref in reference_images:
        import base64
        parts.append(
            {
                "inlineData": {
                    "mimeType": _sniff_image_mime(ref),
                    "data": base64.b64encode(ref).decode("ascii"),
                }
            }
        )
    return parts


def _build_image_generation_config(
    *, aspect_ratio: str | None, image_size: str
) -> dict[str, Any]:
    # Vertex generateContent puts image controls under ``imageConfig`` — NOT
    # ``responseFormat`` (an OpenAI-ism Vertex rejects with HTTP 400
    # "Unknown name responseFormat at generation_config: Cannot find field").
    image_config: dict[str, Any] = {"imageSize": image_size}
    if aspect_ratio:
        image_config["aspectRatio"] = aspect_ratio
    return {
        "responseModalities": ["TEXT", "IMAGE"],
        "imageConfig": image_config,
    }


def _extract_image_payload(
    response_json: dict[str, Any], *, model: str
) -> dict[str, Any]:
    import base64

    candidates = response_json.get("candidates") or []
    if not candidates:
        prompt_feedback = response_json.get("promptFeedback")
        raise VertexAPIError(
            f"Vertex returned no candidates (model={model}). "
            f"promptFeedback={prompt_feedback!r}",
            status=200,
            body_tail=json.dumps(response_json)[:1200],
        )
    candidate = candidates[0]
    finish_reason = candidate.get("finishReason")
    content = candidate.get("content") or {}
    parts = content.get("parts") or []

    image_bytes: bytes | None = None
    mime_type: str = ""
    text_summary_parts: list[str] = []

    for part in parts:
        inline = part.get("inlineData") if isinstance(part, dict) else None
        if inline and isinstance(inline.get("data"), str):
            try:
                image_bytes = base64.b64decode(inline["data"])
            except (ValueError, TypeError) as exc:
                raise VertexAPIError(
                    f"Vertex inlineData base64 decode failed: {exc}",
                    status=200,
                ) from exc
            mime_type = str(inline.get("mimeType") or "image/png")
            continue
        text = part.get("text") if isinstance(part, dict) else None
        if isinstance(text, str) and text:
            text_summary_parts.append(text)

    if image_bytes is None:
        raise VertexAPIError(
            f"Vertex response had no image inlineData "
            f"(finishReason={finish_reason!r}, text_only={text_summary_parts[:1]})",
            status=200,
            body_tail=json.dumps(response_json)[:1200],
        )

    # Strip inlineData from the meta we return — caller stores it as
    # asset metadata; base64 must NEVER ride the event channel.
    meta = {
        "model": model,
        "finish_reason": finish_reason,
        "model_text": " ".join(text_summary_parts).strip() or None,
        "safety_ratings": candidate.get("safetyRatings"),
        "usage_metadata": response_json.get("usageMetadata"),
    }
    return {
        "image_bytes": image_bytes,
        "mime_type": mime_type,
        "model": model,
        "raw_response_meta": meta,
    }


def _sniff_image_mime(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "application/octet-stream"


__all__ = [
    "GoogleGenAIClient",
    "VertexAPIError",
    "VertexAuthMissingError",
    "read_api_calls",
    "find_suspected_leaks",
]
