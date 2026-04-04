"""Gemini image generation client (Nano Banana).

Supports two backends:
- **Native Gemini REST API** (``GEMINI_API_KEY``) — preferred for image output reliability.
- **OpenRouter API** (``OPENROUTER_API_KEY``) — fallback when Gemini API key is absent.

Environment variables
---------------------
GEMINI_API_KEY              : Preferred API key for native Gemini REST.
OPENROUTER_API_KEY          : Fallback API key for OpenRouter.
GEMINI_NB_FLASH_MODEL       : Override flash model for native Gemini.
GEMINI_NB_PRO_MODEL         : Override pro model for native Gemini.
OPENROUTER_NB_FLASH_MODEL   : Override flash model for OpenRouter.
OPENROUTER_NB_PRO_MODEL     : Override pro model for OpenRouter.
GEMIA_SSL_VERIFY            : Set to "0" to disable SSL verification.
"""
from __future__ import annotations

import base64
import json
import os
import ssl
import urllib.error
import urllib.request
from typing import Any

import certifi
import cv2
import numpy as np

from gemia.primitives_common import ensure_float32, to_uint8

# ── Model name constants ─────────────────────────────────────────────────

_NATIVE_FLASH_DEFAULT = "gemini-2.0-flash-exp-image-generation"
_NATIVE_PRO_DEFAULT = "gemini-2.5-pro-preview-05-06"
_OPENROUTER_FLASH_DEFAULT = "google/gemini-2.0-flash-exp"
_OPENROUTER_PRO_DEFAULT = "google/gemini-2.5-pro-preview-05-06"

_GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models"
_OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


class GenerativeClient:
    """Client for Gemini image generation (Nano Banana NB2 / Pro).

    Selects backend automatically:
    - If ``GEMINI_API_KEY`` is set (and ``prefer_native=True``), uses native Gemini REST.
    - Otherwise falls back to OpenRouter with ``OPENROUTER_API_KEY``.

    Args:
        model_tier: ``"flash"`` (NB2) or ``"pro"`` (NB Pro). Default ``"flash"``.
        prefer_native: Prefer native Gemini API when key is available. Default ``True``.

    Raises:
        RuntimeError: If neither ``GEMINI_API_KEY`` nor ``OPENROUTER_API_KEY`` is set.
    """

    def __init__(self, model_tier: str = "flash", prefer_native: bool = True) -> None:
        self.model_tier = model_tier
        self.ssl_verify = os.environ.get("GEMIA_SSL_VERIFY", "1") != "0"

        gemini_key = os.environ.get("GEMINI_API_KEY", "")
        openrouter_key = os.environ.get("OPENROUTER_API_KEY", "")

        if prefer_native and gemini_key:
            self._backend = "native"
            self._api_key = gemini_key
            self._model = self._resolve_native_model(model_tier)
        elif openrouter_key:
            self._backend = "openrouter"
            self._api_key = openrouter_key
            self._model = self._resolve_openrouter_model(model_tier)
        elif gemini_key:
            self._backend = "native"
            self._api_key = gemini_key
            self._model = self._resolve_native_model(model_tier)
        else:
            raise RuntimeError(
                "Set GEMINI_API_KEY (preferred) or OPENROUTER_API_KEY for Nano Banana image generation."
            )

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
        if self._backend == "native":
            return self._native_text_to_image(prompt)
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
        if self._backend == "native":
            return self._native_image_and_text(img, prompt)
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
        if self._backend == "native":
            return self._native_blend(img_a, img_b, prompt)
        return self._openrouter_blend(img_a, img_b, prompt)

    # ── Native Gemini REST ────────────────────────────────────────────────

    def _native_text_to_image(self, prompt: str) -> np.ndarray:
        url = f"{_GEMINI_BASE_URL}/{self._model}:generateContent?key={self._api_key}"
        payload: dict[str, Any] = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"responseModalities": ["TEXT", "IMAGE"]},
        }
        body = self._post_json(url, payload, auth_header=False)
        return self._extract_image_from_native_response(body)

    def _native_image_and_text(self, img: np.ndarray, prompt: str) -> np.ndarray:
        url = f"{_GEMINI_BASE_URL}/{self._model}:generateContent?key={self._api_key}"
        b64 = _ndarray_to_b64(img)
        payload: dict[str, Any] = {
            "contents": [{
                "parts": [
                    {"inlineData": {"mimeType": "image/png", "data": b64}},
                    {"text": prompt},
                ]
            }],
            "generationConfig": {"responseModalities": ["TEXT", "IMAGE"]},
        }
        body = self._post_json(url, payload, auth_header=False)
        return self._extract_image_from_native_response(body)

    def _native_blend(self, img_a: np.ndarray, img_b: np.ndarray, prompt: str) -> np.ndarray:
        url = f"{_GEMINI_BASE_URL}/{self._model}:generateContent?key={self._api_key}"
        b64_a = _ndarray_to_b64(img_a)
        b64_b = _ndarray_to_b64(img_b)
        payload: dict[str, Any] = {
            "contents": [{
                "parts": [
                    {"inlineData": {"mimeType": "image/png", "data": b64_a}},
                    {"inlineData": {"mimeType": "image/png", "data": b64_b}},
                    {"text": prompt},
                ]
            }],
            "generationConfig": {"responseModalities": ["TEXT", "IMAGE"]},
        }
        body = self._post_json(url, payload, auth_header=False)
        return self._extract_image_from_native_response(body)

    def _extract_image_from_native_response(self, body: dict) -> np.ndarray:
        """Extract the first inlineData image from a native Gemini response."""
        try:
            parts = body["candidates"][0]["content"]["parts"]
        except (KeyError, IndexError) as exc:
            raise RuntimeError(f"Unexpected Gemini response structure: {body}") from exc
        for part in parts:
            if "inlineData" in part:
                data = part["inlineData"]["data"]
                mime = part["inlineData"].get("mimeType", "image/png")
                return _b64_to_ndarray(data, mime)
        raise RuntimeError(
            "Gemini returned no image in response. "
            "Check that the model supports image generation and GEMINI_API_KEY is valid."
        )

    # ── OpenRouter API ────────────────────────────────────────────────────

    def _openrouter_text_to_image(self, prompt: str) -> np.ndarray:
        payload: dict[str, Any] = {
            "model": self._model,
            "modalities": ["text", "image"],
            "messages": [
                {"role": "user", "content": [{"type": "text", "text": prompt}]},
            ],
        }
        body = self._post_json(_OPENROUTER_URL, payload, auth_header=True)
        return self._extract_image_from_openrouter_response(body)

    def _openrouter_image_and_text(self, img: np.ndarray, prompt: str) -> np.ndarray:
        b64 = _ndarray_to_b64(img)
        payload: dict[str, Any] = {
            "model": self._model,
            "modalities": ["text", "image"],
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                    {"type": "text", "text": prompt},
                ],
            }],
        }
        body = self._post_json(_OPENROUTER_URL, payload, auth_header=True)
        return self._extract_image_from_openrouter_response(body)

    def _openrouter_blend(self, img_a: np.ndarray, img_b: np.ndarray, prompt: str) -> np.ndarray:
        b64_a = _ndarray_to_b64(img_a)
        b64_b = _ndarray_to_b64(img_b)
        payload: dict[str, Any] = {
            "model": self._model,
            "modalities": ["text", "image"],
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64_a}"}},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64_b}"}},
                    {"type": "text", "text": prompt},
                ],
            }],
        }
        body = self._post_json(_OPENROUTER_URL, payload, auth_header=True)
        return self._extract_image_from_openrouter_response(body)

    def _extract_image_from_openrouter_response(self, body: dict) -> np.ndarray:
        """Extract the first image_url part from an OpenRouter response."""
        try:
            content = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError) as exc:
            raise RuntimeError(f"Unexpected OpenRouter response structure: {body}") from exc

        # content may be a string (text only) or a list of parts
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "image_url":
                    url = part.get("image_url", {}).get("url", "")
                    if url.startswith("data:"):
                        # data:image/png;base64,<data>
                        header, data = url.split(",", 1)
                        mime = header.split(":")[1].split(";")[0]
                        return _b64_to_ndarray(data, mime)

        raise RuntimeError(
            "OpenRouter returned no image in response. "
            "For reliable image generation, set GEMINI_API_KEY and use the native Gemini API instead."
        )

    # ── HTTP helpers ──────────────────────────────────────────────────────

    def _post_json(self, url: str, payload: dict, auth_header: bool = True) -> dict:
        """POST JSON payload, return parsed response body."""
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if auth_header:
            headers["Authorization"] = f"Bearer {self._api_key}"
            headers["HTTP-Referer"] = "https://local-gemia-mvp"
            headers["X-Title"] = "gemia-mvp"

        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            if self.ssl_verify:
                context = ssl.create_default_context(cafile=certifi.where())
            else:
                context = ssl.create_default_context()
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE
            with urllib.request.urlopen(req, timeout=120, context=context) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="ignore")
            raise RuntimeError(f"Gemini API HTTP {exc.code}: {error_body}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Gemini API request failed: {exc}") from exc

    # ── Model resolution ──────────────────────────────────────────────────

    @staticmethod
    def _resolve_native_model(tier: str) -> str:
        if tier == "pro":
            return os.environ.get("GEMINI_NB_PRO_MODEL", _NATIVE_PRO_DEFAULT)
        return os.environ.get("GEMINI_NB_FLASH_MODEL", _NATIVE_FLASH_DEFAULT)

    @staticmethod
    def _resolve_openrouter_model(tier: str) -> str:
        if tier == "pro":
            return os.environ.get("OPENROUTER_NB_PRO_MODEL", _OPENROUTER_PRO_DEFAULT)
        return os.environ.get("OPENROUTER_NB_FLASH_MODEL", _OPENROUTER_FLASH_DEFAULT)


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
