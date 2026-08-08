from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from gemia.model_strength import is_model_unavailable_error, media_model_failover_chain, strongest_media_model

from .common import cleared_proxy_env, get_config_value


class GeminiMediaClient:
    """Native Gemini media generation for unattended stock creation."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        image_model: str | None = None,
        video_model: str | None = None,
    ) -> None:
        try:
            from gemia import cloud_accounts

            cloud_mode = cloud_accounts.enabled()
        except Exception:
            cloud_mode = os.environ.get("LUMERI_CLOUD_ACCOUNTS", "").strip().lower() in {"1", "true", "yes"}
        if cloud_mode:
            raise RuntimeError(
                "Unattended Gemini media creation is unavailable in cloud-account mode."
            )
        self.api_key = (
            api_key or get_config_value("gemini_api_key", "GEMINI_API_KEY")
        ).strip()
        if not self.api_key:
            raise RuntimeError("GEMINI_API_KEY is required for native Gemini media generation.")
        self.image_model = strongest_media_model(
            "image", "imagen", (image_model, os.environ.get("GEMIA_GEMINI_IMAGE_MODEL"))
        )
        self.video_model = strongest_media_model(
            "video", "gemini", (video_model, os.environ.get("GEMIA_GEMINI_VIDEO_MODEL"))
        )

    @staticmethod
    def _sdk() -> tuple[Any, Any]:
        try:
            from google import genai
            from google.genai import types
        except ImportError as exc:  # pragma: no cover - depends on local runtime
            raise RuntimeError(
                "google-genai is not installed. Use the wrapper scripts or "
                "`uv run --no-project --with google-genai ...`."
            ) from exc
        return genai, types

    def generate_images(
        self,
        *,
        prompt: str,
        output_dir: Path,
        task_id: str,
        count: int = 4,
        aspect_ratio: str = "16:9",
        image_size: str = "1K",
    ) -> list[str]:
        genai, types = self._sdk()
        output_dir.mkdir(parents=True, exist_ok=True)
        with cleared_proxy_env():
            client = genai.Client(api_key=self.api_key)
            response = None
            chain = media_model_failover_chain("image", "imagen", (self.image_model,))
            for index, model in enumerate(chain):
                try:
                    response = client.models.generate_images(
                        model=model,
                        prompt=prompt,
                        config=types.GenerateImagesConfig(
                            number_of_images=max(int(count), 1),
                            aspect_ratio=aspect_ratio,
                            image_size=image_size,
                            output_mime_type="image/png",
                        ),
                    )
                    self.image_model = model
                    break
                except Exception as exc:
                    if index + 1 >= len(chain) or not is_model_unavailable_error(exc):
                        raise
            assert response is not None
        generated = list(response.generated_images or [])
        if not generated:
            raise RuntimeError(f"Gemini returned no images for task {task_id}.")
        outputs: list[str] = []
        for index, item in enumerate(generated, start=1):
            image = item.image
            if image is None:
                continue
            path = output_dir / f"{task_id}_{index:02d}.png"
            image.save(path)
            outputs.append(str(path.resolve()))
        if not outputs:
            raise RuntimeError(f"Gemini returned empty image payloads for task {task_id}.")
        return outputs

    def generate_video(
        self,
        *,
        prompt: str,
        output_path: Path,
        duration_seconds: int = 5,
        aspect_ratio: str = "16:9",
        resolution: str = "720p",
        poll_interval_sec: int = 10,
        image_path: str | None = None,
        video_path: str | None = None,
    ) -> str:
        genai, types = self._sdk()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        image_arg = types.Image.from_file(location=image_path) if image_path else None
        video_arg = types.Video.from_file(location=video_path) if video_path else None
        with cleared_proxy_env():
            client = genai.Client(api_key=self.api_key)
            operation = None
            chain = media_model_failover_chain("video", "gemini", (self.video_model,))
            for index, model in enumerate(chain):
                try:
                    operation = client.models.generate_videos(
                        model=model,
                        prompt=prompt,
                        image=image_arg,
                        video=video_arg,
                        config=types.GenerateVideosConfig(
                            duration_seconds=max(int(duration_seconds), 1),
                            aspect_ratio=aspect_ratio,
                            resolution=resolution,
                        ),
                    )
                    self.video_model = model
                    break
                except Exception as exc:
                    if index + 1 >= len(chain) or not is_model_unavailable_error(exc):
                        raise
            assert operation is not None
            while not operation.done:
                time.sleep(max(int(poll_interval_sec), 5))
                operation = client.operations.get(operation)
            response = operation.response or operation.result
            generated = list((response.generated_videos if response else None) or [])
            if not generated or generated[0].video is None:
                raise RuntimeError("Gemini returned no generated video.")
            video = generated[0].video
            client.files.download(file=video)
            video.save(output_path)
        return str(output_path.resolve())
