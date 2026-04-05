from __future__ import annotations

import json
import os
import ssl
import textwrap
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import certifi


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


class GeminiAdapter:
    """Minimal OpenRouter-backed Gemini adapter for structured plan generation."""

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        api_url: str = "https://openrouter.ai/api/v1/chat/completions",
        log_dir: str | Path = "logs/gemini",
    ) -> None:
        self.api_key = (
            api_key
            or os.environ.get("OPENROUTER_API_KEY")
            or _read_config_key("openrouter_api_key")
        )
        self.model = model or os.environ.get("OPENROUTER_MODEL", "google/gemini-3-flash")
        self.api_url = api_url
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.ssl_verify = os.environ.get("GEMIA_SSL_VERIFY", "1") != "0"
        if not self.api_key:
            raise RuntimeError("OPENROUTER_API_KEY is missing")

    async def generate_plan_json(self, system_prompt: str, user_payload: dict[str, Any], tag: str) -> dict[str, Any]:
        payload = {
            "model": self.model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
            ],
        }
        body = self._post_json(payload)
        self._write_log(tag=tag, request_payload=payload, response_body=body)
        content = ((body.get("choices") or [{}])[0].get("message") or {}).get("content", "")
        if not content:
            raise RuntimeError(f"Gemini returned empty content: {body}")
        return self._extract_json(content)

    def _post_json(self, payload: dict[str, Any]) -> dict[str, Any]:
        req = urllib.request.Request(
            self.api_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://local-gemia-mvp",
                "X-Title": "gemia-mvp",
            },
            method="POST",
        )
        try:
            if self.ssl_verify:
                context = ssl.create_default_context(cafile=certifi.where())
            else:
                context = ssl.create_default_context()
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE
            with urllib.request.urlopen(req, timeout=90, context=context) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="ignore")
            raise RuntimeError(f"OpenRouter HTTP {exc.code}: {error_body}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"OpenRouter request failed: {exc}") from exc

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
        path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n")

    @staticmethod
    def _extract_json(text: str) -> dict[str, Any]:
        text = text.strip()
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start : end + 1])
        raise ValueError("No JSON object found in model response")


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
        Return an Ask JSON (max 3 questions, concise):
        {
          "ask": true,
          "questions": ["What style or look are you going for?", "How long should the output be?"]
        }

        When to ask:
        - Style/effect is totally unspecified ("make it cool", "do something interesting")
        - Duration is ambiguous for trim/subtitle
        - Multi-step request references tools not yet available (Nano Banana, Veo, etc.)

        When NOT to ask:
        - Simple clear operations: "裁前10秒", "加速2倍", "color grade warm"
        - User provides enough context even if brief

        If "clarifications" are provided in the payload, use them to generate the Plan — do not ask again.
        """
    ).strip()


def build_primitive_plan_system_prompt() -> str:
    """System prompt for Plan v2 — references actual primitive functions."""
    from gemia.registry import catalog_for_prompt
    catalog = catalog_for_prompt()
    return textwrap.dedent(
        f"""
        You are Gemia's AI planner for video/image/audio workflows.
        Return JSON only. No markdown.

        You have access to these primitive functions:

        {catalog}

        Analyze the user's request. Choose one of two responses:

        === CASE A: Request is specific enough to execute ===
        Return a Plan v2 JSON:
        {{
          "version": "2.0",
          "goal": "string describing the intent",
          "steps": [
            {{
              "id": "step_1",
              "function": "gemia.picture.color.color_grade",
              "args": {{"preset": "cyberpunk"}},
              "input": "$input",
              "output": "$output"
            }}
          ]
        }}

        Variable references:
        - "$input" = user's input video file
        - "$output" = desired output file (use ONLY on the last step)
        - "$step_N" = output of step with id "step_N"

        IMPORTANT rules:
        - If a gemia.picture.* function is used and the input is a video,
          the engine applies it to every frame automatically. Do NOT add
          extract_frames/frames_to_video steps for this — just use the
          picture function directly with "$input".
        - For video timeline operations (trim, speed, reverse, concat),
          use gemia.video.timeline.* functions.
        - Keep plans minimal — prefer fewer steps.
        - Omit "input" for intermediate steps (defaults to previous step output).
        - Omit "output" for non-final steps (auto-generates temp path).
        - "args" must only contain JSON-serializable values (no arrays or objects
          that represent numpy data).

        IMPORTANT notes on generative functions:
        - gemia.picture.generative.generate_image: generates a new image from scratch (no input image needed). Use when creating title cards, backgrounds, or standalone images. Args: prompt (str), aspect_ratio (str), style (str), model_tier (str).
        - gemia.picture.generative.style_transfer: applies a visual style to each video frame. Use for "赛博朋克", "水墨画", "油画" style requests on videos. Args: img (auto from pipeline), style_prompt (str), model_tier (str).
        - gemia.picture.generative.edit_image: edits each frame with an instruction. Use for "remove background", "add rain effect" on videos. Args: img (auto from pipeline), instruction (str), model_tier (str).
        - gemia.picture.generative.blend_images: blends current frame with another image file (requires img_b_path arg pointing to a local file). Args: img (auto from pipeline), img_b_path (str), prompt (str), model_tier (str).
        - gemia.video.generative.generate_video: generates a new video from text prompt. No input needed. Args: prompt (str), duration (float), aspect_ratio (str).
        - gemia.video.generative.generate_video_from_image: animates a still image into video. Input is an image file path. Args: image_path (auto from pipeline), prompt (str), duration (float).
        - gemia.video.generative.extend_video: extends the end of a video. Input is a video file path. Args: video_path (auto from pipeline), prompt (str), duration (float).

        IMPORTANT notes on non-generative functions:
        - gemia.video.frames.stabilize: stabilizes shaky video. Args: video_path (auto), output_path (auto), smoothness (int, default 30).
        - gemia.video.frames.retime: variable speed retiming. Args: video_path (auto), output_path (auto), speed_map (list of [timestamp_sec, speed_factor] pairs, e.g. [[0,1.0],[3,2.0]]).
        - gemia.picture.color.lift_gamma_gain: adjusts lift/gamma/gain per channel. Args: img (auto), lift/gamma/gain as floats.
        - gemia.picture.color.log_to_linear: converts log-encoded footage to linear. Args: img (auto), log_format ('slog2'|'slog3'|'logc'|'log3g10').
        - gemia.picture.color.color_space_convert: converts between color spaces. Args: img (auto), src/dst as strings.
        - gemia.picture.analysis.waveform_monitor: returns waveform analysis image (use for QC, not in pipeline).
        - gemia.video.analysis.detect_scenes: returns list of scene change timestamps. Args: path (video), threshold (float).
        - gemia.video.analysis.get_metadata: returns dict with duration/width/height/fps. Args: path (video).

        For analysis functions (detect_scenes, get_metadata, waveform_monitor) that return data rather than media files, do NOT include them in a pipeline — only use them when the user explicitly asks for analysis results.

        === CASE B: Request is too vague or missing key parameters ===
        Return an Ask JSON (max 3 questions, concise):
        {{
          "ask": true,
          "questions": ["What style or look?", "How long should the output be?"]
        }}

        When to ask: style/effect is totally unspecified, duration is ambiguous.
        When NOT to ask: clear operations like "裁前10秒", "加速2倍", "warm调色".
        If "clarifications" are provided in the payload, generate the Plan — do not ask again.
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
