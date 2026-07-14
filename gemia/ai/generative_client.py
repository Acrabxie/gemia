"""Nano Banana image generation client via OpenRouter chat completions.

Environment variables
---------------------
OPENROUTER_API_KEY          : Preferred API key for OpenRouter.
GEMIA_OPENROUTER_API_KEY    : Alternate OpenRouter API key.
GEMIA_IMAGE_API_KEY         : Alternate image-only OpenRouter key.
OPENROUTER_IMAGE_URL        : Optional base URL, default https://openrouter.ai/api/v1.
GEMIA_IMAGE_MODEL           : Override image model, default google/gemini-2.5-flash-image.
GEMIA_IMAGE_PRO_MODEL       : Optional pro-tier override, default google/gemini-3.1-flash-image-preview.
GEMIA_SSL_VERIFY            : Set to "0" to disable SSL verification.

OPENAI_API_KEY is only used as a compatibility fallback when the effective
base URL explicitly points at openrouter.ai. For every other base URL we
require an explicit OpenRouter image key so a real OpenAI key is not silently
forwarded to an unrelated OpenAI-compatible service.
"""
from __future__ import annotations

import base64
import json
import os
import ssl
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

import certifi
import cv2
import numpy as np

from gemia.primitives_common import ensure_float32, to_uint8

# ── Defaults ─────────────────────────────────────────────────────────────

_DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
_DEFAULT_MODEL = "google/gemini-2.5-flash-image"
_DEFAULT_PRO_MODEL = "google/gemini-3.1-flash-image-preview"
_DEFAULT_TIMEOUT_SEC = 180


class GenerativeClient:
    """Client for Nano Banana image generation/editing via OpenRouter.

    Args:
        model_tier: ``"flash"`` or ``"pro"``. Flash defaults to Nano Banana
            (Gemini 2.5 Flash Image) through OpenRouter; pro can be overridden
            independently.

    Raises:
        RuntimeError: If no OpenRouter API key is configured.
    """

    def __init__(self, model_tier: str = "flash") -> None:
        self.model_tier = model_tier
        self.ssl_verify = os.environ.get("GEMIA_SSL_VERIFY", "1") != "0"
        self.base_url, self._base_source = _resolve_base_url()
        self._api_key = _resolve_api_key(self.base_url, self._base_source)
        if not self._api_key:
            raise RuntimeError(
                "Set OPENROUTER_API_KEY or ~/.gemia/config.json:openrouter_api_key "
                "for Nano Banana image generation."
            )
        self._backend = "openrouter"
        self._model = _resolve_image_model(model_tier)
        self.proxy = os.environ.get("GEMIA_PROXY") or _read_config_key("proxy") or ""
        self.timeout_sec = int(os.environ.get("GEMIA_IMAGE_TIMEOUT_SEC", str(_DEFAULT_TIMEOUT_SEC)))

    # ── Public API ────────────────────────────────────────────────────────

    def generate_image_from_text(self, prompt: str) -> np.ndarray:
        """Generate an image from a text prompt.

        Args:
            prompt: Text description of the image to generate.

        Returns:
            float32 BGR ndarray, shape (H, W, 3), values in [0, 1].

        Raises:
            RuntimeError: If the API call fails or returns no image.
        """
        return self._openrouter_text_to_image(prompt)

    def generate_image_from_image_and_text(self, img: np.ndarray, prompt: str) -> np.ndarray:
        """Edit an image based on a natural-language instruction.

        Args:
            img: Input float32 BGR ndarray.
            prompt: Editing instruction, e.g. "make the sky purple".

        Returns:
            float32 BGR ndarray with the edit applied.

        Raises:
            RuntimeError: If the API call fails or returns no image.
        """
        return self._openrouter_image_and_text(img, prompt)

    def blend_two_images(self, img_a: np.ndarray, img_b: np.ndarray, prompt: str) -> np.ndarray:
        """Blend two images guided by a text prompt.

        Args:
            img_a: First input float32 BGR ndarray.
            img_b: Second input float32 BGR ndarray.
            prompt: Blending guidance, e.g. "blend these two images seamlessly".

        Returns:
            Blended float32 BGR ndarray.

        Raises:
            RuntimeError: If the API call fails or returns no image.
        """
        return self._openrouter_blend(img_a, img_b, prompt)

    # ── OpenRouter chat-completions image API ─────────────────────────────

    def _openrouter_text_to_image(self, prompt: str) -> np.ndarray:
        body = self._post_json(
            f"{self.base_url}/chat/completions",
            self._chat_payload(prompt),
        )
        return self._extract_image_from_response(body)

    def _openrouter_image_and_text(self, img: np.ndarray, prompt: str) -> np.ndarray:
        image_data_uri = f"data:image/png;base64,{_ndarray_to_b64(img)}"
        body = self._post_json(
            f"{self.base_url}/chat/completions",
            self._chat_payload(prompt, image_data_uri=image_data_uri),
        )
        return self._extract_image_from_response(body)

    def _openrouter_blend(self, img_a: np.ndarray, img_b: np.ndarray, prompt: str) -> np.ndarray:
        collage = _side_by_side_collage(img_a, img_b)
        blend_prompt = (
            "The uploaded reference image is a side-by-side collage. "
            "Use the left half as the primary image and the right half as the secondary image. "
            "Create one final blended image, not a side-by-side collage. "
            f"Blend guidance: {prompt}"
        )
        return self._openrouter_image_and_text(collage, blend_prompt)

    def _chat_payload(self, prompt: str, *, image_data_uri: str | None = None) -> dict[str, Any]:
        content: str | list[dict[str, Any]]
        if image_data_uri:
            content = [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": image_data_uri}},
            ]
        else:
            content = prompt
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": [{"role": "user", "content": content}],
            "modalities": ["image", "text"],
        }
        size = os.environ.get("GEMIA_IMAGE_SIZE", "").strip()
        if size:
            payload["image_config"] = {"size": size}
        return payload

    def _extract_image_from_response(self, body: dict[str, Any]) -> np.ndarray:
        """Extract image bytes from OpenRouter, OpenAI Images, or Responses payloads."""
        for choice in body.get("choices", []) or []:
            if not isinstance(choice, dict):
                continue
            message = choice.get("message") or {}
            if not isinstance(message, dict):
                continue
            for image in message.get("images", []) or []:
                url = _extract_image_url(image)
                if url:
                    return self._image_url_to_ndarray(url)
            content = message.get("content")
            if isinstance(content, list):
                for part in content:
                    url = _extract_image_url(part)
                    if url:
                        return self._image_url_to_ndarray(url)
            elif isinstance(content, str):
                url = _find_data_image_url(content)
                if url:
                    return self._image_url_to_ndarray(url)

        for item in body.get("data", []) or []:
            if not isinstance(item, dict):
                continue
            if item.get("b64_json"):
                return _b64_to_ndarray(str(item["b64_json"]))
            if item.get("url"):
                return self._url_to_ndarray(str(item["url"]))

        for item in body.get("output", []) or []:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "image_generation_call" and item.get("result"):
                return _b64_to_ndarray(str(item["result"]))
            for part in item.get("content", []) or []:
                if not isinstance(part, dict):
                    continue
                if part.get("type") in {"output_image", "image"} and part.get("b64_json"):
                    return _b64_to_ndarray(str(part["b64_json"]))
                image_url = part.get("image_url")
                if isinstance(image_url, dict) and image_url.get("url"):
                    url = str(image_url["url"])
                    if url.startswith("data:"):
                        return self._image_url_to_ndarray(url)
                    return self._url_to_ndarray(url)

        raise RuntimeError(
            "Nano Banana/OpenRouter returned no image in response. "
            f"Response keys: {sorted(body.keys())}"
        )

    def _image_url_to_ndarray(self, url: str) -> np.ndarray:
        if url.startswith("data:"):
            header, data = url.split(",", 1)
            mime = header.split(":")[1].split(";")[0]
            return _b64_to_ndarray(data, mime)
        return self._url_to_ndarray(url)

    # ── HTTP helpers ──────────────────────────────────────────────────────

    def _post_json(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        """POST JSON payload, return parsed response body."""
        headers: dict[str, str] = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._api_key}",
            "User-Agent": "Lumeri/desktop-image-client",
        }
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        return self._open_json(req)

    def _post_multipart(
        self,
        url: str,
        fields: dict[str, str],
        files: list[tuple[str, str, str, bytes]],
    ) -> dict[str, Any]:
        boundary = f"lumeri-{uuid.uuid4().hex}"
        body = _encode_multipart(fields, files, boundary)
        headers = {
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Content-Length": str(len(body)),
            "Authorization": f"Bearer {self._api_key}",
            "User-Agent": "Lumeri/desktop-image-client",
        }
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        return self._open_json(req)

    def _open_json(self, req: urllib.request.Request) -> dict[str, Any]:
        try:
            opener = self._opener()
            with opener.open(req, timeout=self.timeout_sec) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="ignore")
            raise RuntimeError(f"Nano Banana OpenRouter API HTTP {exc.code}: {error_body}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Nano Banana OpenRouter API request failed: {exc}") from exc

    def _url_to_ndarray(self, url: str) -> np.ndarray:
        req = urllib.request.Request(url, headers={"User-Agent": "Lumeri/desktop-image-client"})
        try:
            with self._opener().open(req, timeout=self.timeout_sec) as resp:
                raw = resp.read()
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Nano Banana image download failed: {exc}") from exc
        arr = np.frombuffer(raw, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is None:
            raise RuntimeError("cv2.imdecode failed for Nano Banana URL response.")
        return ensure_float32(img)

    def _opener(self) -> urllib.request.OpenerDirector:
        if self.ssl_verify:
            context = ssl.create_default_context(cafile=certifi.where())
        else:
            context = ssl.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        handlers: list[Any] = [urllib.request.HTTPSHandler(context=context)]
        if self.proxy:
            handlers.append(urllib.request.ProxyHandler({"http": self.proxy, "https": self.proxy}))
        return urllib.request.build_opener(*handlers)

    # ── Model resolution ──────────────────────────────────────────────────

    @staticmethod
    def _resolve_openrouter_model(tier: str) -> str:
        """Backward-compatible alias for old tests/callers."""
        return _resolve_image_model(tier)


# ── Helper functions ──────────────────────────────────────────────────────

def _ndarray_to_b64(img: np.ndarray) -> str:
    """Convert a float32 BGR ndarray to a base64-encoded PNG string.

    Args:
        img: float32 BGR ndarray, values in [0, 1].

    Returns:
        Base64-encoded PNG as a plain string (no data URI prefix).
    """
    img_f32 = ensure_float32(img)
    img_u8 = to_uint8(img_f32)
    # Convert BGR → RGB for standard PNG output
    if img_u8.ndim == 3 and img_u8.shape[2] == 3:
        img_rgb = cv2.cvtColor(img_u8, cv2.COLOR_BGR2RGB)
    else:
        img_rgb = img_u8
    success, buf = cv2.imencode(".png", img_rgb)
    if not success:
        raise RuntimeError("cv2.imencode failed while converting ndarray to PNG.")
    return base64.b64encode(buf.tobytes()).decode("ascii")


def _ndarray_to_png_bytes(img: np.ndarray) -> bytes:
    return base64.b64decode(_ndarray_to_b64(img))


def _b64_to_ndarray(data: str, mime: str = "image/png") -> np.ndarray:
    """Decode a base64 image string to a float32 BGR ndarray.

    Args:
        data: Base64-encoded image data (no data URI prefix).
        mime: MIME type of the image (e.g. ``"image/png"``, ``"image/jpeg"``).

    Returns:
        float32 BGR ndarray, shape (H, W, 3), values in [0, 1].

    Raises:
        RuntimeError: If decoding fails.
    """
    raw = base64.b64decode(data)
    arr = np.frombuffer(raw, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)  # Returns BGR uint8
    if img is None:
        raise RuntimeError(f"cv2.imdecode failed for MIME type {mime}.")
    return ensure_float32(img)


def _encode_multipart(
    fields: dict[str, str],
    files: list[tuple[str, str, str, bytes]],
    boundary: str,
) -> bytes:
    chunks: list[bytes] = []
    boundary_bytes = boundary.encode("ascii")
    for name, value in fields.items():
        chunks.extend([
            b"--" + boundary_bytes + b"\r\n",
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"),
            str(value).encode("utf-8"),
            b"\r\n",
        ])
    for field_name, filename, content_type, payload in files:
        chunks.extend([
            b"--" + boundary_bytes + b"\r\n",
            (
                f'Content-Disposition: form-data; name="{field_name}"; '
                f'filename="{filename}"\r\n'
            ).encode("utf-8"),
            f"Content-Type: {content_type}\r\n\r\n".encode("utf-8"),
            payload,
            b"\r\n",
        ])
    chunks.append(b"--" + boundary_bytes + b"--\r\n")
    return b"".join(chunks)


def _side_by_side_collage(img_a: np.ndarray, img_b: np.ndarray) -> np.ndarray:
    a = ensure_float32(img_a)
    b = ensure_float32(img_b)
    height = max(a.shape[0], b.shape[0])
    a_resized = _resize_to_height(a, height)
    b_resized = _resize_to_height(b, height)
    return np.concatenate([a_resized, b_resized], axis=1)


def _resize_to_height(img: np.ndarray, height: int) -> np.ndarray:
    if img.shape[0] == height:
        return img
    width = max(1, int(round(img.shape[1] * (height / img.shape[0]))))
    return ensure_float32(cv2.resize(img, (width, height), interpolation=cv2.INTER_AREA))


def _resolve_image_model(tier: str) -> str:
    if tier == "pro":
        return (
            os.environ.get("GEMIA_IMAGE_PRO_MODEL")
            or os.environ.get("NANO_BANANA_PRO_MODEL")
            or os.environ.get("OPENROUTER_IMAGE_PRO_MODEL")
            or _read_config_key("image_pro_model")
            or _read_config_key("nano_banana_pro_model")
            or _read_config_key("openrouter_image_pro_model")
            or _model_or_empty(_read_config_key("image_model"))
            or _DEFAULT_PRO_MODEL
        )
    return (
        os.environ.get("GEMIA_IMAGE_MODEL")
        or os.environ.get("NANO_BANANA_MODEL")
        or os.environ.get("OPENROUTER_IMAGE_MODEL")
        or _model_or_empty(_read_config_key("image_model"))
        or _read_config_key("nano_banana_model")
        or _read_config_key("openrouter_image_model")
        or _DEFAULT_MODEL
    )


def _resolve_base_url() -> tuple[str, str]:
    for source, value in (
        ("env:OPENROUTER_IMAGE_URL", os.environ.get("OPENROUTER_IMAGE_URL")),
        ("env:GEMIA_OPENROUTER_IMAGE_URL", os.environ.get("GEMIA_OPENROUTER_IMAGE_URL")),
        ("env:GEMIA_IMAGE_BASE_URL", os.environ.get("GEMIA_IMAGE_BASE_URL")),
        ("config:image_base_url", _read_config_key("image_base_url")),
        ("config:openrouter_image_url", _read_config_key("openrouter_image_url")),
        ("config:openai_base_url", _read_config_key("openai_base_url")),
    ):
        cleaned = _clean_url(value)
        if cleaned and "sisyphusx.com" not in cleaned:
            return cleaned, source
    openai_base = _clean_url(os.environ.get("OPENAI_BASE_URL"))
    if "openrouter.ai" in openai_base:
        return openai_base, "env:OPENAI_BASE_URL"
    return _DEFAULT_BASE_URL, "default"


def _resolve_api_key(base_url: str = "", base_source: str = "") -> str:
    """Return an OpenRouter image key from env/config, with a narrow compat fallback."""
    for name in ("OPENROUTER_API_KEY", "GEMIA_OPENROUTER_API_KEY", "GEMIA_IMAGE_API_KEY"):
        value = str(os.environ.get(name) or "").strip()
        if value:
            return value
    if base_source == "env:OPENAI_BASE_URL" and "openrouter.ai" in str(base_url):
        openai_compat_key = str(os.environ.get("OPENAI_API_KEY") or "").strip()
        if openai_compat_key:
            return openai_compat_key
    for key in ("openrouter_api_key", "openrouter_key", "image_api_key", "nano_banana_api_key"):
        value = _read_config_key(key)
        if value:
            return value
    return ""


def _model_or_empty(value: str) -> str:
    lowered = value.strip().lower()
    if not lowered:
        return ""
    if lowered in {"gpt-image-2", "gpt_image2", "gpt image2"}:
        return ""
    return value.strip()


def _extract_image_url(item: Any) -> str:
    if not isinstance(item, dict):
        return ""
    if isinstance(item.get("url"), str):
        return str(item["url"])
    image_url = item.get("image_url")
    if isinstance(image_url, str):
        return image_url
    if isinstance(image_url, dict) and isinstance(image_url.get("url"), str):
        return str(image_url["url"])
    if isinstance(item.get("b64_json"), str):
        return f"data:image/png;base64,{item['b64_json']}"
    return ""


def _find_data_image_url(text: str) -> str:
    marker = "data:image/"
    start = text.find(marker)
    if start < 0:
        return ""
    end = len(text)
    for sep in ("\n", "\"", "'", ")", "]", " "):
        pos = text.find(sep, start)
        if pos > start:
            end = min(end, pos)
    return text[start:end]


def _clean_url(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip().rstrip("/")


def _read_config_key(field: str) -> str:
    try:
        path = Path.home() / ".gemia" / "config.json"
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            return str(data.get(field, "") or "").strip()
    except Exception:
        pass
    return ""
