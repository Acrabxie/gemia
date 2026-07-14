from __future__ import annotations

import base64
import json
import mimetypes
import os
import ssl
import textwrap
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import certifi

from .provider_audit import audit_provider_payload


def _read_config_key(field: str) -> str:
    """Read a single key from ~/.gemia/config.json, returning '' on any error."""
    try:
        path = Path.home() / ".gemia" / "config.json"
        if path.exists():
            data = json.loads(path.read_text())
            return data.get(field, "") or ""
    except Exception:
        pass
    return ""


_GEMINI_NATIVE_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
_GEMINI_UPLOAD_URL = "https://generativelanguage.googleapis.com/upload/v1beta/files"
_GEMINI_FILE_URL = "https://generativelanguage.googleapis.com/v1beta/{name}"
_DEFAULT_GEMINI_MODEL = "gemini-3.1-pro-preview"
_DEFAULT_OPENROUTER_MODEL = "google/gemini-3.1-pro-preview"
_DEFAULT_INLINE_VIDEO_MAX_BYTES = 20 * 1024 * 1024
_DEFAULT_UPLOAD_VIDEO_MAX_BYTES = 512 * 1024 * 1024
_VIDEO_MIME_TYPES = {
    ".mp4": "video/mp4",
    ".mpeg": "video/mpeg",
    ".mpg": "video/mpg",
    ".mov": "video/quicktime",
    ".qt": "video/quicktime",
    ".avi": "video/avi",
    ".flv": "video/x-flv",
    ".webm": "video/webm",
    ".wmv": "video/wmv",
    ".3gp": "video/3gpp",
    ".3gpp": "video/3gpp",
}


class GeminiAdapter:
    """Planner adapter for native Gemini or OpenRouter-hosted Gemini."""

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        api_url: str = "https://openrouter.ai/api/v1/chat/completions",
        log_dir: str | Path = "logs/gemini",
    ) -> None:
        self.gemini_api_key = (
            api_key
            or os.environ.get("GEMINI_API_KEY")
            or _read_config_key("gemini_api_key")
        )
        self.openrouter_api_key = (
            os.environ.get("OPENROUTER_API_KEY")
            or _read_config_key("openrouter_api_key")
        )
        configured_provider = (
            os.environ.get("GEMIA_AI_PROVIDER")
            or _read_config_key("ai_provider")
            or ""
        ).strip().lower()
        self.gemini_model = (
            model
            or os.environ.get("GEMIA_PLANNER_MODEL")
            or os.environ.get("GEMINI_MODEL")
            or _read_config_key("planner_model")
            or _read_config_key("gemini_model")
            or _DEFAULT_GEMINI_MODEL
        )
        self.openrouter_model = (
            model
            or os.environ.get("GEMIA_PLANNER_MODEL")
            or os.environ.get("GEMIA_OPENROUTER_MODEL")
            or os.environ.get("OPENROUTER_MODEL")
            or _read_config_key("planner_model")
            or _read_config_key("openrouter_model")
            or _DEFAULT_OPENROUTER_MODEL
        )
        if configured_provider in {"openrouter", "gemini_native"}:
            self.provider = configured_provider
        elif self.openrouter_api_key:
            self.provider = "openrouter"
        else:
            self.provider = "gemini_native"
        self.model = self.openrouter_model if self.provider == "openrouter" else self.gemini_model
        self.api_url = api_url
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.ssl_verify = os.environ.get("GEMIA_SSL_VERIFY", "1") != "0"
        # Proxy for native Gemini (e.g. http://127.0.0.1:7890).
        # Empty by default — only set GEMIA_PROXY / config.proxy when an actual
        # local HTTP proxy exists; otherwise every native call burns ~90s on
        # connection refused before falling back to OpenRouter.
        self.proxy = (
            os.environ.get("GEMIA_PROXY")
            or _read_config_key("proxy")
            or ""
        )
        if self.provider == "openrouter" and not self.openrouter_api_key:
            raise RuntimeError("OPENROUTER_API_KEY is required for OpenRouter planning")
        if self.provider == "gemini_native" and not self.gemini_api_key and not self.openrouter_api_key:
            raise RuntimeError("GEMINI_API_KEY or OPENROUTER_API_KEY is required")

    async def generate_plan_json(
        self,
        system_prompt: str,
        user_payload: dict[str, Any],
        tag: str,
        *,
        attach_video: bool = False,
        dynamic_system_prompt: str | None = None,
    ) -> dict[str, Any]:
        # Native Gemini is still supported, but OpenRouter is the primary
        # planner when configured. OpenRouter plan calls stay text-only.
        if self.provider == "gemini_native" and self.gemini_api_key:
            try:
                body, request_meta = self._post_gemini_native(
                    _combine_system_prompts(system_prompt, dynamic_system_prompt),
                    user_payload,
                    attach_video=attach_video,
                )
                self._write_log(
                    tag=tag,
                    request_payload={"backend": "gemini_native", "model": self.gemini_model, **request_meta},
                    response_body=body,
                )
                content = body.get("candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text", "")
                if content:
                    plan = self._extract_json(content)
                    if not plan.get("ask"):
                        if "steps" not in plan:
                            raise RuntimeError("AI 生成的计划格式有误，请重试（缺少 steps 字段）")
                        if "version" not in plan:
                            raise RuntimeError("AI 生成的计划格式有误，请重试（缺少 version 字段）")
                    return plan
            except RuntimeError as exc:
                if str(exc).startswith("AI 生成的计划格式有误"):
                    raise
                if not self.openrouter_api_key:
                    raise RuntimeError(f"Gemini native API 请求失败：{exc}") from exc
            except Exception as exc:
                # Native failed, fall through to OpenRouter if available
                if not self.openrouter_api_key:
                    raise RuntimeError(f"Gemini native API 请求失败：{exc}") from exc

        if dynamic_system_prompt:
            body, request_meta = self._post_openrouter(
                system_prompt,
                user_payload,
                tag=tag,
                dynamic_system_prompt=dynamic_system_prompt,
            )
        else:
            body, request_meta = self._post_openrouter(system_prompt, user_payload, tag=tag)
        self._write_log(
            tag=tag,
            request_payload={"backend": "openrouter", "model": self.openrouter_model, **request_meta},
            response_body=body,
        )
        content = ((body.get("choices") or [{}])[0].get("message") or {}).get("content", "")
        if not content:
            raise RuntimeError(f"Gemini returned empty content: {body}")
        plan = self._extract_json(content)
        if not plan.get("ask"):
            if "steps" not in plan:
                raise RuntimeError("AI 生成的计划格式有误，请重试（缺少 steps 字段）")
            if "version" not in plan:
                raise RuntimeError("AI 生成的计划格式有误，请重试（缺少 version 字段）")
        return plan

    async def generate_video_context_json(
        self,
        system_prompt: str,
        user_payload: dict[str, Any],
        tag: str = "video-context",
    ) -> dict[str, Any]:
        """Ask native Gemini to inspect attached video and return JSON context."""
        if not self.gemini_api_key:
            raise RuntimeError("GEMINI_API_KEY is required for native video understanding.")
        if not self.can_read_video(user_payload.get("input_path")):
            raise RuntimeError("No readable video input_path was provided for Gemini video understanding.")
        body, request_meta = self._post_gemini_native(
            system_prompt,
            user_payload,
            attach_video=True,
        )
        self._write_log(
            tag=tag,
            request_payload={"backend": "gemini_native", "model": self.gemini_model, **request_meta},
            response_body=body,
        )
        content = body.get("candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text", "")
        if not content:
            raise RuntimeError(f"Gemini returned empty video context: {body}")
        return self._extract_json(content)

    async def generate_text(
        self,
        system_prompt: str,
        user_payload: dict[str, Any],
        tag: str = "generate-text",
        *,
        dynamic_system_prompt: str | None = None,
    ) -> str:
        """Generate plain text instead of a Plan JSON object."""
        if self.provider == "gemini_native" and self.gemini_api_key:
            try:
                body, request_meta = self._post_gemini_native(
                    _combine_system_prompts(system_prompt, dynamic_system_prompt),
                    user_payload,
                    attach_video=False,
                )
                self._write_log(
                    tag=tag,
                    request_payload={"backend": "gemini_native", "model": self.gemini_model, **request_meta},
                    response_body=body,
                )
                content = body.get("candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text", "")
                if content:
                    return content
            except Exception as exc:
                if not self.openrouter_api_key:
                    raise RuntimeError(f"Gemini native API 请求失败：{exc}") from exc

        messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]
        if dynamic_system_prompt:
            messages.append({"role": "system", "content": dynamic_system_prompt})
        messages.append(
            {
                "role": "user",
                "content": json.dumps(user_payload, ensure_ascii=False, sort_keys=True),
            }
        )
        payload = {
            "model": self.openrouter_model,
            "temperature": 0,
            "max_tokens": 448,
            "messages": messages,
        }
        body, request_meta = self._post_openrouter(payload, tag=tag)
        self._write_log(
            tag=tag,
            request_payload={"backend": "openrouter", "model": self.openrouter_model, **request_meta},
            response_body=body,
        )
        content = ((body.get("choices") or [{}])[0].get("message") or {}).get("content", "")
        if not content:
            raise RuntimeError(f"Gemini returned empty content: {body}")
        return str(content)

    def can_read_video(self, input_path: Any) -> bool:
        """Return whether native Gemini can receive at least one local video path."""
        if self.provider != "gemini_native" or not self.gemini_api_key:
            return False
        for path in _iter_video_paths(input_path):
            if Path(path).expanduser().exists():
                return True
        return False

    def _post_gemini_native(
        self,
        system_prompt: str,
        user_payload: dict[str, Any],
        *,
        attach_video: bool = False,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Call Gemini API directly via proxy."""
        url = _GEMINI_NATIVE_URL.format(model=self.gemini_model) + f"?key={self.gemini_api_key}"
        parts, request_meta = self._build_native_parts(user_payload, attach_video=attach_video)
        body = {
            "system_instruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {"temperature": 0, "responseMimeType": "application/json"},
        }
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        proxy_handler = urllib.request.ProxyHandler({
            "https": self.proxy,
            "http": self.proxy,
        }) if self.proxy else urllib.request.ProxyHandler({})
        opener = urllib.request.build_opener(proxy_handler)

        last_error: Exception | None = None
        for attempt in range(3):
            try:
                with opener.open(req, timeout=90) as resp:
                    return json.loads(resp.read().decode("utf-8")), request_meta
            except urllib.error.HTTPError as exc:
                error_body = exc.read().decode("utf-8", errors="ignore")
                if 400 <= exc.code < 500:
                    raise RuntimeError(f"Gemini API HTTP {exc.code}: {error_body}") from exc
                last_error = RuntimeError(f"Gemini API HTTP {exc.code}: {error_body}")
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = RuntimeError(f"Gemini native 请求失败（第{attempt + 1}次）：{exc}")
            if attempt < 2:
                time.sleep(2 ** attempt)
        raise last_error  # type: ignore[misc]

    def _build_native_parts(
        self,
        user_payload: dict[str, Any],
        *,
        attach_video: bool,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        parts: list[dict[str, Any]] = []
        media_inputs: list[dict[str, Any]] = []
        if attach_video:
            max_items = _env_int("GEMIA_GEMINI_MAX_VIDEO_ATTACHMENTS", 3)
            for raw_path in list(_iter_video_paths(user_payload.get("input_path")))[:max(max_items, 1)]:
                path = Path(raw_path).expanduser()
                if not path.exists() or not path.is_file():
                    continue
                part, meta = self._video_part_for_path(path)
                parts.append(part)
                media_inputs.append(meta)
        parts.append({"text": json.dumps(user_payload, ensure_ascii=False)})
        return parts, {
            "attached_media": media_inputs,
            "attached_media_count": len(media_inputs),
        }

    def _video_part_for_path(self, path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
        mime_type = _video_mime_type(path)
        size_bytes = path.stat().st_size
        inline_max = _env_int("GEMIA_GEMINI_INLINE_VIDEO_MAX_BYTES", _DEFAULT_INLINE_VIDEO_MAX_BYTES)
        upload_max = _env_int("GEMIA_GEMINI_UPLOAD_VIDEO_MAX_BYTES", _DEFAULT_UPLOAD_VIDEO_MAX_BYTES)
        fps = os.environ.get("GEMIA_GEMINI_VIDEO_FPS", "").strip()
        metadata: dict[str, Any] = {
            "path": str(path.resolve()),
            "mime_type": mime_type,
            "size_bytes": size_bytes,
        }
        if size_bytes <= inline_max:
            raw = path.read_bytes()
            part: dict[str, Any] = {
                "inline_data": {
                    "mime_type": mime_type,
                    "data": base64.b64encode(raw).decode("ascii"),
                }
            }
            metadata["mode"] = "inline_data"
        else:
            if size_bytes > upload_max:
                raise RuntimeError(
                    f"Video is too large for Gemia's current Gemini upload guard "
                    f"({size_bytes} bytes > {upload_max} bytes): {path}"
                )
            file_info = self._upload_video_file(path, mime_type)
            part = {
                "file_data": {
                    "mime_type": mime_type,
                    "file_uri": file_info["uri"],
                }
            }
            metadata.update({
                "mode": "file_api",
                "file_name": file_info.get("name", ""),
                "file_uri": file_info["uri"],
            })
        if fps:
            try:
                fps_value = float(fps)
            except ValueError:
                fps_value = 0.0
            if fps_value > 0:
                part["video_metadata"] = {"fps": fps_value}
                metadata["fps"] = fps_value
        return part, metadata

    def _upload_video_file(self, path: Path, mime_type: str) -> dict[str, str]:
        """Upload a video through Gemini Files API and return its URI metadata."""
        opener = self._native_opener()
        size_bytes = path.stat().st_size
        start_req = urllib.request.Request(
            _GEMINI_UPLOAD_URL,
            data=json.dumps({"file": {"display_name": path.name}}).encode("utf-8"),
            headers={
                "x-goog-api-key": self.gemini_api_key,
                "Content-Type": "application/json",
                "X-Goog-Upload-Protocol": "resumable",
                "X-Goog-Upload-Command": "start",
                "X-Goog-Upload-Header-Content-Length": str(size_bytes),
                "X-Goog-Upload-Header-Content-Type": mime_type,
            },
            method="POST",
        )
        with opener.open(start_req, timeout=60) as resp:
            upload_url = resp.headers.get("x-goog-upload-url") or resp.headers.get("X-Goog-Upload-URL")
        if not upload_url:
            raise RuntimeError("Gemini Files API did not return an upload URL.")

        upload_req = urllib.request.Request(
            upload_url,
            data=path.read_bytes(),
            headers={
                "Content-Length": str(size_bytes),
                "X-Goog-Upload-Offset": "0",
                "X-Goog-Upload-Command": "upload, finalize",
            },
            method="POST",
        )
        with opener.open(upload_req, timeout=300) as resp:
            uploaded = json.loads(resp.read().decode("utf-8"))
        file_payload = uploaded.get("file") or {}
        file_payload = self._wait_for_file_active(file_payload, opener)
        uri = file_payload.get("uri")
        if not uri:
            raise RuntimeError(f"Gemini Files API returned no file uri: {uploaded}")
        return {
            "name": file_payload.get("name", ""),
            "uri": uri,
            "mime_type": file_payload.get("mimeType") or file_payload.get("mime_type") or mime_type,
        }

    def _wait_for_file_active(
        self,
        file_payload: dict[str, Any],
        opener: urllib.request.OpenerDirector,
    ) -> dict[str, Any]:
        state = str(file_payload.get("state", "")).upper()
        name = str(file_payload.get("name", "")).strip()
        if not name or state in {"", "ACTIVE"}:
            return file_payload
        for _ in range(30):
            if state == "FAILED":
                raise RuntimeError(f"Gemini file processing failed: {file_payload}")
            time.sleep(2)
            req = urllib.request.Request(
                _GEMINI_FILE_URL.format(name=name),
                headers={"x-goog-api-key": self.gemini_api_key},
                method="GET",
            )
            with opener.open(req, timeout=60) as resp:
                file_payload = json.loads(resp.read().decode("utf-8"))
            state = str(file_payload.get("state", "")).upper()
            if state in {"", "ACTIVE"}:
                return file_payload
        raise RuntimeError(f"Gemini file processing did not become ACTIVE: {file_payload}")

    def _native_opener(self) -> urllib.request.OpenerDirector:
        proxy_handler = urllib.request.ProxyHandler({
            "https": self.proxy,
            "http": self.proxy,
        }) if self.proxy else urllib.request.ProxyHandler({})
        return urllib.request.build_opener(proxy_handler)

    def _openrouter_payload(
        self,
        system_prompt: str,
        user_payload: dict[str, Any],
        *,
        dynamic_system_prompt: str | None = None,
        use_cache_control: bool = True,
    ) -> dict[str, Any]:
        """Build the OpenRouter chat-completions payload.

        The system prompt is the stable block; OpenRouter's prompt caching
        contract uses ``cache_control`` on a content block, not a top-level key.
        """
        system_block: dict[str, Any] = {"type": "text", "text": system_prompt}
        if use_cache_control:
            system_block["cache_control"] = {"type": "ephemeral"}
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": [system_block]},
        ]
        if dynamic_system_prompt:
            messages.append({"role": "system", "content": dynamic_system_prompt})
        messages.append(
            {
                "role": "user",
                "content": json.dumps(user_payload, ensure_ascii=False, sort_keys=True),
            }
        )
        return {
            "model": self.openrouter_model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": messages,
        }

    def _post_openrouter(
        self,
        system_prompt: str | dict[str, Any],
        user_payload: dict[str, Any] | None = None,
        tag: str = "openrouter",
        *,
        dynamic_system_prompt: str | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        if isinstance(system_prompt, dict) and user_payload is None:
            payload = system_prompt
        else:
            payload = self._openrouter_payload(
                str(system_prompt),
                user_payload or {},
                dynamic_system_prompt=dynamic_system_prompt,
            )
        headers = {
            "Authorization": f"Bearer {self.openrouter_api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://local-lumeri-desktop",
            "X-Title": "lumeri-desktop",
        }
        provider_payload_audit = audit_provider_payload(payload)
        desktop_txt = self._write_desktop_input_txt(
            tag=tag,
            endpoint=self.api_url,
            request_body=payload,
            headers=headers,
            request_meta={
                "provider": "openrouter",
                "model": self.openrouter_model,
                "message_count": len(payload.get("messages") or []),
                "provider_payload_audit": provider_payload_audit,
            },
        )
        req = urllib.request.Request(
            self.api_url,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        if self.ssl_verify:
            context = ssl.create_default_context(cafile=certifi.where())
        else:
            context = ssl.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE

        last_error: Exception | None = None
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=90, context=context) as resp:
                    body = json.loads(resp.read().decode("utf-8"))
                    return body, {
                        "message_count": len(payload.get("messages") or []),
                        "desktop_input_txt": desktop_txt,
                        "provider_payload_audit": provider_payload_audit,
                        "usage": body.get("usage") if isinstance(body, dict) else None,
                    }
            except urllib.error.HTTPError as exc:
                error_body = exc.read().decode("utf-8", errors="ignore")
                if 400 <= exc.code < 500:
                    raise RuntimeError(f"OpenRouter HTTP {exc.code}: {error_body}") from exc
                last_error = RuntimeError(f"OpenRouter HTTP {exc.code}: {error_body}")
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = RuntimeError(f"AI 接口请求失败（第{attempt + 1}次）：{exc}")
            if attempt < 2:
                time.sleep(2 ** attempt)
        raise last_error  # type: ignore[misc]

    def _write_desktop_input_txt(
        self,
        *,
        tag: str,
        endpoint: str,
        request_body: dict[str, Any],
        headers: dict[str, Any],
        request_meta: dict[str, Any] | None = None,
    ) -> str | None:
        if os.environ.get("GEMIA_INPUT_TXT_LOG", "0") != "1":
            return None
        out_dir = Path(
            os.environ.get("GEMIA_INPUT_TXT_DIR")
            or (Path.home() / "Desktop" / "Lumeri Gemini Inputs")
        ).expanduser()
        out_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        safe_tag = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in tag)[:80] or "input"
        path = out_dir / f"{ts}-{safe_tag}.txt"
        record = {
            "timestamp": ts,
            "tag": tag,
            "endpoint": endpoint,
            "headers": _redact(headers),
            "request_meta": _redact(request_meta or {}),
            "request_body": _redact(request_body),
        }
        text = json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        path.write_text(text, encoding="utf-8")
        latest = out_dir / "latest.txt"
        latest.write_text(text, encoding="utf-8")
        return str(path)

    def _write_log(self, tag: str, request_payload: dict[str, Any], response_body: dict[str, Any]) -> None:
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = self.log_dir / f"{ts}-{tag}.json"
        record = {
            "timestamp": ts,
            "tag": tag,
            "model": self.model,
            "request": request_payload,
            "response": response_body,
        }
        if isinstance(response_body, dict) and isinstance(response_body.get("usage"), dict):
            record["usage"] = response_body["usage"]
        path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n")

    @staticmethod
    def _extract_json(text: str) -> dict[str, Any]:
        text = text.strip()
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start : end + 1])
        raise ValueError("No JSON object found in model response")


def _iter_video_paths(input_path: Any) -> list[str]:
    if not input_path:
        return []
    if isinstance(input_path, (list, tuple)):
        raw_paths = [str(item) for item in input_path if item]
    else:
        raw_paths = [str(input_path)]
    paths: list[str] = []
    for raw in raw_paths:
        path = Path(raw).expanduser()
        if path.suffix.lower() in _VIDEO_MIME_TYPES:
            paths.append(str(path))
    return paths


def _video_mime_type(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in _VIDEO_MIME_TYPES:
        return _VIDEO_MIME_TYPES[suffix]
    guessed = mimetypes.guess_type(str(path))[0]
    if guessed and guessed.startswith("video/"):
        return guessed
    return "video/mp4"


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return max(int(raw), 0)
    except ValueError:
        return default


def _redact(value: Any) -> Any:
    secret_fragments = ("authorization", "api_key", "apikey", "access_token", "refresh_token", "secret")
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key).lower()
            if any(fragment in key_text for fragment in secret_fragments):
                cleaned[key] = "<redacted>"
            else:
                cleaned[key] = _redact(item)
        return cleaned
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def _combine_system_prompts(system_prompt: str, dynamic_system_prompt: str | None = None) -> str:
    if not dynamic_system_prompt:
        return system_prompt
    return f"{system_prompt}\n\n{dynamic_system_prompt}"


def build_plan_system_prompt() -> str:
    return textwrap.dedent(
        """
        You are a planner for Gemia, a minimal media workflow runtime.
        Return JSON only.

        Generate a minimal executable Plan JSON using this shape:
        {
          "version": "1.0",
          "goal": "string",
          "input_path": "string",
          "output_path": "string",
          "steps": [
            {
              "id": "step id string",
              "type": "extract_keyframes | stylize_images | compose_preview_video | trim_clip | change_speed | add_subtitle | color_grade | merge_clips",
              "params": {"any": "json"},
              "depends_on": ["optional step ids"]
            }
          ]
        }

        Step param rules:
        - trim_clip: {"start_sec": number, "end_sec": number}
        - change_speed: {"speed": number}
        - add_subtitle: {"text": string, "duration_sec": number}
        - color_grade: {"preset": "warm|cool|vintage|cyberpunk"}
        - merge_clips: {"videos": ["abs path 1", "abs path 2"]}
        - extract_keyframes: {"num_keyframes": number}
        - stylize_images: {"style_prompt": string}
        - compose_preview_video: {"output_format": "before_after_hstack"}

        Rules:
        - Keep steps minimal and executable.
        - For edit requests like trim + speed + subtitle, generate those exact step types in order.
        - Use depends_on so later steps consume earlier outputs.
        - The output must be valid JSON object only.
        - Do not include markdown.
        """
    ).strip()


def build_plan_or_ask_system_prompt() -> str:
    return textwrap.dedent(
        """
        You are Gemia's AI planner for video editing workflows.
        Return JSON only. No markdown.

        Analyze the user's request. Choose one of two responses:

        === CASE A: Request is specific enough to execute ===
        Return a Plan JSON:
        {
          "version": "1.0",
          "goal": "string",
          "input_path": "string (use the provided input_path exactly)",
          "output_path": "string (use the provided output_path exactly)",
          "steps": [
            {
              "id": "step_1",
              "type": "trim_clip | change_speed | add_subtitle | color_grade | merge_clips | extract_keyframes | stylize_images | compose_preview_video",
              "params": {},
              "depends_on": []
            }
          ]
        }

        Step param rules:
        - trim_clip: {"start_sec": number, "end_sec": number}
        - change_speed: {"speed": number}  (1.0=normal, 2.0=2x fast, 0.5=half speed)
        - add_subtitle: {"text": string, "duration_sec": number}
        - color_grade: {"preset": "warm|cool|vintage|cyberpunk"}
        - extract_keyframes: {"num_keyframes": number}
        - stylize_images: {"style_prompt": string}
        - compose_preview_video: {"output_format": "before_after_hstack"}

        === CASE B: Request is too vague or missing key parameters ===
        Return an Ask JSON (max 3 questions). Each question must include "id", "text", and "input_type".
        Choose the most appropriate input_type for each question:
        - "choices": when there are a small set of discrete options (include "choices" array)
        - "slider": when the answer is a numeric range (include "min", "max", "default", "step", "unit")
        - "text": when a free-form answer is needed (optionally include "placeholder")

        Example:
        {
          "ask": true,
          "questions": [
            {"id": "q0", "text": "What visual style?", "input_type": "choices", "choices": ["warm", "cool", "vintage", "cyberpunk"]},
            {"id": "q1", "text": "How long should the output be?", "input_type": "slider", "min": 1, "max": 60, "default": 10, "step": 1, "unit": "s"},
            {"id": "q2", "text": "Any other notes?", "input_type": "text", "placeholder": "Optional details..."}
          ]
        }

        When to ask:
        - Style/effect is totally unspecified ("make it cool", "do something interesting")
        - Duration is ambiguous for trim/subtitle
        - Multi-step request references tools not yet available (GPT Image 2, Veo, etc.)

        When NOT to ask:
        - Simple clear operations: "裁前10秒", "加速2倍", "color grade warm"
        - User provides enough context even if brief

        If "clarifications" are provided in the payload, use them to generate the Plan — do not ask again.
        """
    ).strip()


def build_primitive_plan_system_prompt(
    categories: list[str] | tuple[str, ...] | None = None,
    *,
    has_video_context: bool = False,
) -> str:
    """System prompt for Plan v2 with an on-demand primitive catalog."""
    from gemia.registry import catalog_for_categories

    catalog = catalog_for_categories(categories or ["core"])
    video_context_note = ""
    if has_video_context:
        video_context_note = textwrap.dedent(
            """
            Video context:
            - payload.video_context is a compact local summary of the relevant video clip.
            - Use duration, mood, key_frame, and suggested_use as planning hints.
            - Do not request native video upload/read; plan from this metadata and the project state.
            """
        ).strip()

    rules_table = textwrap.dedent(
        """
        | trigger keywords | preferred primitive | extra args |
        |---|---|---|
        | layered edit, overlay, graph, keyframed opacity/position | gemia.video.layer_flow.render_layer_workflow | use args.overlay_layers; avoid preview/compositing internals |
        | focus, aperture, rack focus, click-to-focus | gemia.video.cinefocus.render_cinefocus_plan | normalized focus keyframes 0..1 |
        | motion blur, blurry action, deblur | gemia.video.motion_deblur.render_motion_deblur_plan | strength or quality if given |
        | ultra sharpen, recover detail | gemia.video.ultrasharpen.render_ultrasharpen_plan | keep natural detail |
        | age, younger, older face | gemia.video.face_age.render_face_age_plan | preserve identity unless user asks otherwise |
        | face shape, jaw, eyes, mouth | gemia.video.face_reshaper.render_face_reshaper_plan | subtle=True unless requested |
        | blemish, acne, skin cleanup | gemia.video.blemish.render_blemish_removal_plan | preserve texture |
        | slate id, scene metadata, AI slate | gemia.video.slate_id.render_slate_id_metadata_plan | analysis/report output |
        | HTML, Lottie, lower third, data panel, OGraf | gemia.video.html_graphics.render_html_graphics_plan | html/html_source/lottie_source/overlay_layers |
        | LumeriLink, Blender, spatial, 3D, parallax | gemia.video.blender_link.render_blender_link_operation | operation/style/intensity |
        """
    ).strip()

    return textwrap.dedent(
        f"""
        You are Lumeri's Plan v2 planner for video/image/audio workflows.
        Return one valid JSON object only. No markdown in the answer.

        {video_context_note}

        Available primitives:
        {catalog}

        Routing rules:
        {rules_table}

        CASE A: enough information to execute.
        Return:
        {{
          "version": "2.0",
          "goal": "short intent",
          "assistant_message": "用中文给用户一两句自然语言说明：你理解了什么、准备怎么处理，不要写思维链",
          "steps": [
            {{
              "id": "step_1",
              "function": "fully.qualified.primitive",
              "args": {{}},
              "input": "$input",
              "output": "$output",
              "assistant_message": "用中文给用户一句话说明这一步会做什么，不要写思维链"
            }}
          ]
        }}

        References:
        - "$input" is the provided input_path.
        - "$output" is the provided output_path and should appear only on the final media-producing step.
        - "$step_N" is the output of an earlier step.
        - Omit input for intermediate steps when it consumes the previous step.
        - Omit output for non-final media-producing steps.

        Planning constraints:
        - Use only primitives shown in the catalog unless the rule table names an exact preferred primitive.
        - Include top-level assistant_message for every executable plan.
        - Include assistant_message on every step, written as one concise Chinese user-visible status sentence for what is happening now. Do not write private reasoning or chain-of-thought.
        - For timeline edit, trim, speed, reverse, concat, or rotate, prefer gemia.video.timeline.*.
        - If a gemia.picture.* function is applied to video, Lumeri runs it on frames automatically; do not add extract/frames-to-video steps.
        - Keep simple requests to 1-2 steps; richer edits should usually be 2-6 clear steps.
        - Analysis primitives that return metadata are not media pipeline steps unless the user explicitly asks for analysis.
        - args must be JSON-serializable. Never include numpy arrays, binary data, or local thumbnails.

        CASE B: missing required information.
        Return:
        {{
          "ask": true,
          "questions": [
            {{"id": "q0", "text": "question", "input_type": "choices|slider|text"}}
          ]
        }}
        Ask at most 3 questions. Ask only for required execution details such as effect type, target material, or duration.
        If payload.clarifications exists, treat it as authoritative and generate a plan instead of asking again.
        Clear short requests like "裁前10秒", "加速2倍", "暖色调色", or "加字幕" should execute without asking.
        """
    ).strip()


def build_video_context_system_prompt() -> str:
    return textwrap.dedent(
        """
        You are Gemia's native Gemini video analyst.
        Return JSON only. No markdown.

        Read the attached source video directly. Use both visual and audio
        evidence when available. Produce concise planning context, not a final
        edit plan and not private chain-of-thought.

        Shape:
        {
          "available": true,
          "summary": "one or two sentences",
          "duration_hint_sec": number|null,
          "timeline": [
            {
              "timestamp": "MM:SS",
              "visual": "what is visible",
              "audio": "speech/music/noise if notable",
              "edit_note": "why this moment matters"
            }
          ],
          "subjects": ["short labels"],
          "visual_style": "short description",
          "audio_notes": ["short notes"],
          "quality_notes": ["shake/exposure/noise/framing/etc"],
          "edit_opportunities": [
            {
              "timestamp": "MM:SS",
              "action": "trim|stabilize|caption|color|retime|highlight|other",
              "reason": "short reason"
            }
          ]
        }

        Rules:
        - Keep timeline to at most 8 important moments.
        - Prefer concrete timestamps when you can infer them.
        - If content is hard to inspect, still return available=true with the
          uncertainty in quality_notes.
        """
    ).strip()


def build_revise_system_prompt() -> str:
    return textwrap.dedent(
        """
        You are revising an existing Gemia Plan JSON based on user feedback.
        Return JSON only.

        Keep the same overall plan structure, but adjust step params as needed.
        Preserve valid prior steps when possible.
        The output must be a valid Plan JSON object only.
        """
    ).strip()
