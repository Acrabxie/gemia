"""Gemini/Lyria audio generation client via the official Gemini API.

Environment variables
---------------------
GEMINI_API_KEY              : Required. API key for the official Gemini API.
GEMIA_AUDIO_MODEL           : Override default audio model.
GEMIA_AUDIO_PRO_MODEL       : Override pro-tier audio model.
GEMIA_SSL_VERIFY            : Set to "0" to disable SSL verification.
"""
from __future__ import annotations

import base64
import json
import os
import ssl
import subprocess
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Literal, TypedDict

import certifi

from gemia.audio.effects import text_to_speech as local_text_to_speech
from gemia.model_strength import is_model_unavailable_error, media_model_failover_chain, strongest_media_model
from gemia.primitives_common import ensure_path_exists

# ── Type Definitions ─────────────────────────────────────────────────────

class SpeechGenerationArtifact(TypedDict):
    """Artifact detailing a speech generation request and its result."""
    text: str
    voice: str
    performance: str
    timing_metadata_requested: bool
    dry_run_activated: bool
    output_audio_path: str
    generated_at: str
    model: str
    # Future: timing metadata, phoneme data, etc.


# ── Configuration ────────────────────────────────────────────────────────

_GEMINI_LYRIA_DEFAULT = "lyria-3-pro-preview" # Default model for audio generation
_GEMINI_GENERATE_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

# ── Client Implementation ────────────────────────────────────────────────

def _read_config_key(field: str) -> str:
    try:
        path = Path.home() / ".gemia" / "config.json"
        if path.exists():
            data = json.loads(path.read_text())
            return data.get(field, "") or ""
    except Exception:
        pass
    return ""

class AudioClient:
    """Client for Gemini/Lyria audio generation via Gemini API.

    Args:
        model_tier: ``"pro"`` for Lyria-3-Pro. Default ``"pro"``.
        dry_run: If True, API calls will be skipped and local TTS will be used.

    Raises:
        RuntimeError: If ``GEMINI_API_KEY`` is not set and not in dry-run mode.
    """

    def __init__(self, model_tier: Literal["pro"] = "pro", dry_run: bool = False) -> None:
        self.model_tier = model_tier
        self.dry_run = dry_run
        self.ssl_verify = os.environ.get("GEMIA_SSL_VERIFY", "1") != "0"

        try:
            from gemia import cloud_accounts

            cloud_key = cloud_accounts.credential_for_provider("gemini")
        except Exception:
            cloud_key = "" if os.environ.get("LUMERI_CLOUD_ACCOUNTS", "").strip().lower() in {"1", "true", "yes"} else None
        gemini_key = (
            cloud_key
            if cloud_key is not None
            else os.environ.get("GEMINI_API_KEY", "") or _read_config_key("gemini_api_key")
        )
        self._backend = "gemini_native"
        self._api_key = gemini_key

        if not self.dry_run and not self._api_key:
            raise RuntimeError(
                "Set GEMINI_API_KEY for Lyria audio generation or enable dry-run mode."
            )
        self._model = self._resolve_gemini_model(model_tier)

    # ── Public API ────────────────────────────────────────────────────────

    def generate_speech_from_text(
        self,
        text: str,
        output_dir: str,
        *,
        voice: str = "default",  # e.g., "male-1", "female-2"
        performance: str = "neutral",  # e.g., "neutral", "expressive", "whisper"
        timing_metadata_requested: bool = False, # Request phoneme/word timing
    ) -> SpeechGenerationArtifact:
        """Generate speech audio from text using Lyria/Gemini API or local fallback.

        Args:
            text: The text to convert to speech.
            output_dir: Directory to save the generated audio file and artifact.
            voice: Specifies the desired voice characteristics.
            performance: Specifies the emotional or stylistic performance.
            timing_metadata_requested: If True, request detailed timing metadata.

        Returns:
            A SpeechGenerationArtifact detailing the request and result.
        """
        output_dir_path = ensure_path_exists(output_dir)
        artifact_path = output_dir_path / f"speech_artifact_{Path(output_dir).name}.json"
        audio_output_path = output_dir_path / f"speech_{Path(output_dir).name}.mp3"

        if self.dry_run:
            # Fallback to local TTS in dry-run mode
            local_text_to_speech(text, str(audio_output_path), voice="Samantha", rate=175) # Using a default macOS voice for dry-run
            # Ensure the output audio file exists, even if local_text_to_speech is mocked
            if not audio_output_path.exists():
                audio_output_path.write_bytes(b"dummy_audio_content_dry_run")
            artifact: SpeechGenerationArtifact = {
                "text": text,
                "voice": voice,
                "performance": performance,
                "timing_metadata_requested": timing_metadata_requested,
                "dry_run_activated": True,
                "output_audio_path": str(audio_output_path),
                "generated_at": str(os.getenv("TODAYS_DATE", "unknown")),
                "model": "dry-run-local-tts",
            }
            Path(artifact_path).write_text(json.dumps(artifact, indent=2))
            return artifact
        
        # Real API call
        payload: dict[str, Any] = {
            "contents": [
                {"role": "user", "parts": [{"text": text}]},
            ],
            "generationConfig": {
                "responseModalities": ["AUDIO", "TEXT"],
                "audioConfig": {
                    "voice": voice,
                    "performance": performance,
                    "enableTimingMetadata": timing_metadata_requested,
                }
            },
        }
        body: dict[str, Any] | None = None
        chain = media_model_failover_chain("audio", "gemini", (self._model,))
        for index, model in enumerate(chain):
            try:
                body = self._post_json(_GEMINI_GENERATE_URL.format(model=model), payload)
                self._model = model
                break
            except RuntimeError as exc:
                if index + 1 >= len(chain) or not is_model_unavailable_error(exc):
                    raise
        assert body is not None
        audio_data_b64 = self._extract_audio_from_gemini_response(body)
        
        audio_data = base64.b64decode(audio_data_b64)
        Path(audio_output_path).write_bytes(audio_data)

        artifact: SpeechGenerationArtifact = {
            "text": text,
            "voice": voice,
            "performance": performance,
            "timing_metadata_requested": timing_metadata_requested,
            "dry_run_activated": False,
            "output_audio_path": str(audio_output_path),
            "generated_at": str(os.getenv("TODAYS_DATE", "unknown")),
            "model": self._model,
            # Future: add actual timing metadata from response
        }
        Path(artifact_path).write_text(json.dumps(artifact, indent=2))
        return artifact


    def _extract_audio_from_gemini_response(self, body: dict) -> str:
        """Extract the first inline audio part from a Gemini response."""
        for candidate in body.get("candidates", []) or []:
            content = candidate.get("content", {}) or {}
            for part in content.get("parts", []) or []:
                inline = part.get("inlineData") or part.get("inline_data")
                if isinstance(inline, dict) and inline.get("data"):
                    mime = inline.get("mimeType") or inline.get("mime_type") or "audio/mpeg" # Default to MP3
                    if mime.startswith("audio/"):
                        return inline["data"]

        raise RuntimeError(
            f"Gemini returned no audio in response: {body}"
        )

    # ── HTTP helpers ──────────────────────────────────────────────────────

    def _post_json(self, url: str, payload: dict) -> dict:
        """POST JSON payload, return parsed response body."""
        headers: dict[str, str] = {
            "Content-Type": "application/json",
            "x-goog-api-key": self._api_key,
        }

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
    def _resolve_gemini_model(tier: str) -> str:
        candidates = (
            os.environ.get("GEMIA_AUDIO_PRO_MODEL"),
            os.environ.get("GEMIA_AUDIO_MODEL"),
            os.environ.get("GEMIA_GEMINI_AUDIO_MODEL"),
            _read_config_key("audio_pro_model"),
            _read_config_key("lyria_pro_model"),
            _read_config_key("audio_model"),
            _read_config_key("lyria_model"),
            _read_config_key("gemini_audio_model"),
            _GEMINI_LYRIA_DEFAULT,
        )
        return strongest_media_model("audio", "gemini", candidates)


# ── Primitive for Registry ────────────────────────────────────────────────

def resolve21_ai_speech_generator(
    text: str,
    output_dir: str,
    *,
    voice: str = "default",
    performance: str = "neutral",
    timing_metadata_requested: bool = False,
    dry_run: bool = False,
    model_tier: Literal["pro"] = "pro",
) -> SpeechGenerationArtifact:
    """Planner-visible AI Speech Generator primitive.

    Generates speech audio from text using Lyria/Gemini API,
    or a local TTS fallback in dry-run mode.

    Creates a repeatable speech-generation request artifact.

    Args:
        text: The text to convert to speech.
        output_dir: Directory to save the generated audio file and artifact.
        voice: Specifies the desired voice characteristics (e.g., "male-1", "female-2").
        performance: Specifies the emotional or stylistic performance (e.g., "neutral", "expressive").
        timing_metadata_requested: If True, requests detailed timing metadata (e.g., phoneme/word timings).
        dry_run: If True, skips actual API calls and uses local TTS.
        model_tier: The tier of the Lyria/Gemini model to use ("pro").

    Returns:
        A SpeechGenerationArtifact detailing the request and result.
    """
    client = AudioClient(model_tier=model_tier, dry_run=dry_run)
    artifact = client.generate_speech_from_text(
        text=text,
        output_dir=output_dir,
        voice=voice,
        performance=performance,
        timing_metadata_requested=timing_metadata_requested,
    )
    return artifact
