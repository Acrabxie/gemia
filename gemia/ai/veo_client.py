"""Veo 3.1 video generation client via laozhang.ai.

Environment variables
---------------------
LAOZHANG_API_KEY  : Required. API key for laozhang.ai.
                    Falls back to OPENROUTER_API_KEY if unset.
LAOZHANG_API_URL  : Optional. Base URL (default: https://api.laozhang.ai/v1).
VEO_MODEL         : Optional. Model name (default: veo-3).
GEMIA_SSL_VERIFY  : Set to "0" to disable SSL certificate verification.
"""
from __future__ import annotations

import base64
import json
import os
import ssl
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import certifi

# ── Defaults ─────────────────────────────────────────────────────────────
_DEFAULT_BASE_URL = "https://api.laozhang.ai/v1"
_DEFAULT_MODEL = "veo-3"
_POLL_INTERVAL_SEC = 5
_MAX_POLL_SEC = 600  # 10 minutes


class VeoClient:
    """Client for Veo 3.1 video generation via laozhang.ai.

    Submits a generation job, polls until completion, downloads the video
    to a local temp file, and returns the file path.

    Attributes:
        api_key: API key used for authentication.
        base_url: Base URL of the laozhang.ai API.
        model: Model name, e.g. ``"veo-3"``.
        ssl_verify: Whether to verify SSL certificates.

    Raises:
        RuntimeError: If neither ``LAOZHANG_API_KEY`` nor ``OPENROUTER_API_KEY``
            is set when instantiated.
    """

    def __init__(self) -> None:
        self.api_key = (
            os.environ.get("LAOZHANG_API_KEY")
            or os.environ.get("OPENROUTER_API_KEY")
        )
        if not self.api_key:
            raise RuntimeError(
                "Set LAOZHANG_API_KEY for Veo video generation. "
                "OPENROUTER_API_KEY is also accepted as a fallback."
            )
        self.base_url = os.environ.get("LAOZHANG_API_URL", _DEFAULT_BASE_URL).rstrip("/")
        self.model = os.environ.get("VEO_MODEL", _DEFAULT_MODEL)
        self.ssl_verify = os.environ.get("GEMIA_SSL_VERIFY", "1") != "0"

        # Local directory for downloaded videos
        this_file = Path(__file__).resolve()
        repo_root = this_file.parent.parent.parent
        self._temp_dir = repo_root / "temp" / "veo"
        self._temp_dir.mkdir(parents=True, exist_ok=True)

    # ── Public API ────────────────────────────────────────────────────────

    def generate(self, prompt: str, duration: float = 5.0, aspect_ratio: str = "16:9") -> str:
        """Generate a video from a text description.

        Args:
            prompt: Text description of the video to generate.
            duration: Duration in seconds (1–60). Default 5.
            aspect_ratio: ``"16:9"``, ``"9:16"``, or ``"1:1"``. Default ``"16:9"``.

        Returns:
            Absolute path to the downloaded MP4 video file.

        Raises:
            RuntimeError: If the API call fails or generation is rejected.
        """
        body: dict[str, Any] = {
            "model": self.model,
            "prompt": prompt,
            "duration": duration,
            "aspect_ratio": aspect_ratio,
        }
        job_id = self._submit_job(body)
        video_url = self._poll_until_done(job_id)
        return self._download_video(video_url, job_id)

    def generate_from_image(self, image_path: str, prompt: str, duration: float = 5.0) -> str:
        """Generate a video from a still image and a text description.

        Args:
            image_path: Path to the input image (JPEG or PNG).
            prompt: Motion description, e.g. ``"camera slowly zooms out"``.
            duration: Duration in seconds (1–60). Default 5.

        Returns:
            Absolute path to the downloaded MP4 video file.

        Raises:
            FileNotFoundError: If ``image_path`` does not exist.
            RuntimeError: If the API call fails.
        """
        img_data = _image_path_to_data_uri(image_path)
        body: dict[str, Any] = {
            "model": self.model,
            "prompt": prompt,
            "image": img_data,
            "duration": duration,
        }
        job_id = self._submit_job(body)
        video_url = self._poll_until_done(job_id)
        return self._download_video(video_url, job_id)

    def extend(self, video_path: str, prompt: str, duration: float = 3.0) -> str:
        """Extend an existing video with an AI-generated continuation.

        Args:
            video_path: Path to the input video to extend.
            prompt: Description of the continuation, e.g. ``"fade to black slowly"``.
            duration: Duration of the extension in seconds. Default 3.

        Returns:
            Absolute path to the downloaded extended MP4 video file.

        Raises:
            FileNotFoundError: If ``video_path`` does not exist.
            RuntimeError: If the API call fails.
        """
        video_b64 = _file_to_b64(video_path)
        body: dict[str, Any] = {
            "model": self.model,
            "prompt": prompt,
            "video": f"data:video/mp4;base64,{video_b64}",
            "duration": duration,
        }
        job_id = self._submit_job(body)
        video_url = self._poll_until_done(job_id)
        return self._download_video(video_url, job_id)

    # ── Internal ──────────────────────────────────────────────────────────

    def _submit_job(self, body: dict[str, Any], max_retries: int = 3) -> str:
        """POST a generation request and return the job ID.

        Retries up to ``max_retries`` times with 2-second backoff on HTTP errors.

        Args:
            body: Request payload dict.
            max_retries: Maximum retry attempts. Default 3.

        Returns:
            Job ID string from the API response.

        Raises:
            RuntimeError: If all retries fail.
        """
        url = f"{self.base_url}/video/generations"
        last_exc: Exception | None = None
        for attempt in range(1, max_retries + 1):
            try:
                resp = self._post_json(url, body)
                # Response may contain job_id directly or nested
                job_id = resp.get("id") or resp.get("job_id") or resp.get("task_id")
                if not job_id:
                    # Some APIs return status+url immediately if fast
                    if resp.get("status") == "completed" and resp.get("url"):
                        # Synthetic job ID for polling bypass
                        return f"_immediate_{resp['url']}"
                    raise RuntimeError(f"Veo API returned no job ID: {resp}")
                return str(job_id)
            except RuntimeError as exc:
                last_exc = exc
                if attempt < max_retries:
                    time.sleep(2)
        raise RuntimeError(f"Veo job submission failed after {max_retries} attempts: {last_exc}") from last_exc

    def _poll_until_done(self, job_id: str) -> str:
        """Poll job status until completed or timeout.

        Args:
            job_id: Job ID returned by ``_submit_job``.

        Returns:
            URL of the completed video file.

        Raises:
            RuntimeError: If the job fails or times out.
        """
        # Handle immediate-completion jobs (bypass polling)
        if job_id.startswith("_immediate_"):
            return job_id[len("_immediate_"):]

        url = f"{self.base_url}/video/generations/{job_id}"
        elapsed = 0.0
        while elapsed < _MAX_POLL_SEC:
            resp = self._get_json(url)
            status = resp.get("status", "pending")
            if status == "completed":
                video_url = resp.get("url") or resp.get("video_url") or resp.get("output_url")
                if not video_url:
                    raise RuntimeError(f"Veo job completed but no URL in response: {resp}")
                return video_url
            if status == "failed":
                error_msg = resp.get("error") or resp.get("message") or str(resp)
                raise RuntimeError(f"Veo generation failed: {error_msg}")
            # Still pending/processing — wait and retry
            time.sleep(_POLL_INTERVAL_SEC)
            elapsed += _POLL_INTERVAL_SEC

        raise RuntimeError(
            f"Veo generation timed out after {_MAX_POLL_SEC}s for job {job_id}."
        )

    def _download_video(self, video_url: str, job_id: str) -> str:
        """Download a video from URL to a local temp file.

        Args:
            video_url: HTTP(S) URL of the video file.
            job_id: Used to name the output file.

        Returns:
            Absolute path to the downloaded file.

        Raises:
            RuntimeError: If download fails.
        """
        safe_id = job_id.replace("/", "_").replace(":", "_")[:64]
        out_path = self._temp_dir / f"{safe_id}.mp4"

        try:
            if self.ssl_verify:
                context = ssl.create_default_context(cafile=certifi.where())
            else:
                context = ssl.create_default_context()
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE
            with urllib.request.urlopen(video_url, timeout=300, context=context) as resp:
                out_path.write_bytes(resp.read())
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Failed to download Veo video from {video_url}: {exc}") from exc

        return str(out_path.resolve())

    def _post_json(self, url: str, payload: dict) -> dict:
        """POST JSON payload and return parsed response."""
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            ctx = self._ssl_context()
            with urllib.request.urlopen(req, timeout=60, context=ctx) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="ignore")
            raise RuntimeError(f"Veo API HTTP {exc.code}: {body}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Veo API request failed: {exc}") from exc

    def _get_json(self, url: str) -> dict:
        """GET JSON from URL and return parsed response."""
        req = urllib.request.Request(
            url,
            headers={"Authorization": f"Bearer {self.api_key}"},
            method="GET",
        )
        try:
            ctx = self._ssl_context()
            with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="ignore")
            raise RuntimeError(f"Veo API HTTP {exc.code}: {body}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Veo API request failed: {exc}") from exc

    def _ssl_context(self) -> ssl.SSLContext:
        """Return an appropriate SSL context based on ``ssl_verify``."""
        if self.ssl_verify:
            return ssl.create_default_context(cafile=certifi.where())
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx


# ── Helper functions ──────────────────────────────────────────────────────

def _file_to_b64(path: str) -> str:
    """Read a file from disk and return its base64-encoded content.

    Args:
        path: Path to the file.

    Returns:
        Base64-encoded string.

    Raises:
        FileNotFoundError: If the file does not exist.
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"File not found: {path}")
    return base64.b64encode(p.read_bytes()).decode("ascii")


def _image_path_to_data_uri(path: str) -> str:
    """Convert an image file to a base64 data URI.

    Args:
        path: Path to a JPEG or PNG image.

    Returns:
        Data URI string, e.g. ``"data:image/png;base64,..."``.

    Raises:
        FileNotFoundError: If the file does not exist.
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Image not found: {path}")
    suffix = p.suffix.lower()
    mime = "image/jpeg" if suffix in (".jpg", ".jpeg") else "image/png"
    b64 = base64.b64encode(p.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{b64}"
