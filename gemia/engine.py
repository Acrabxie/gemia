"""Plan v2 execution engine.

Executes structured plans where each step references a primitive function
by its fully-qualified name. Handles I/O type bridging automatically:
picture functions applied to video paths are wrapped with
``apply_picture_op_to_video``.

Usage::

    from gemia.engine import PlanEngine
    engine = PlanEngine()
    output = engine.execute(plan_dict, "input.mp4", "output.mp4")
"""
from __future__ import annotations

import inspect
import json
import subprocess
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np

from gemia.compat import ffmpeg_path
from gemia.errors import UserInputError
from gemia.plan_contract import normalize_plan_for_execution
from gemia.registry import get_info, resolve


_VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm"}
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp"}
_AUDIO_EXTS = {".wav", ".mp3", ".aac", ".flac", ".ogg", ".m4a"}
_MEDIA_EXTS = _VIDEO_EXTS | _IMAGE_EXTS | _AUDIO_EXTS
_NON_MEDIA_ARTIFACT_EXTS = {".json", ".md", ".txt", ".html", ".htm", ".csv", ".yaml", ".yml"}


class PlanEngine:
    """Execute v2 plans that reference primitive functions directly."""

    def __init__(self, root_dir: str | Path | None = None) -> None:
        this_file = Path(__file__).resolve()
        self.root_dir = Path(root_dir) if root_dir else this_file.parent.parent
        self.temp_dir = self.root_dir / "temp"
        self.outputs_dir = self.root_dir / "outputs"
        self.tasks_dir = self.root_dir / "tasks"
        self.plans_dir = self.root_dir / "plans"
        for d in (self.temp_dir, self.outputs_dir, self.tasks_dir, self.plans_dir):
            d.mkdir(parents=True, exist_ok=True)

    def execute(self, plan: dict, input_path: str, output_path: str,
                on_step: "Callable[[int, int, str], None] | None" = None) -> str:
        """Execute a v2 plan and return the final output path.

        Args:
            plan: Plan dict with ``version: "2.0"`` and ``steps``.
            input_path: User's input video/audio file.
            output_path: Desired output file path.
            on_step: Optional callback called before each step with
                (current_step_index, total_steps, function_name).

        Returns:
            Path to the output file.
        """
        plan = normalize_plan_for_execution(
            plan,
            input_path=input_path,
            output_path=output_path,
            strict_functions=False,
        )
        bindings: dict[str, Any] = {
            "$input": input_path,
            "$output": output_path,
            "$last_media": input_path,
        }
        steps = plan.get("steps", [])
        if not steps:
            raise ValueError("执行计划中没有任何步骤，请重试")

        # Validate input file exists if it's a path
        if isinstance(input_path, str) and input_path.strip():
            from pathlib import Path as _Path
            p = _Path(input_path).expanduser()
            if not p.exists():
                raise FileNotFoundError(f"找不到输入文件：{input_path}")
            if p.is_dir():
                raise ValueError(f"输入路径是文件夹，不是媒体文件：{input_path}")
            # Check video format for video-extension files
            if p.suffix.lower() not in _MEDIA_EXTS and p.suffix.lower() != "":
                raise ValueError(
                    f"不支持的文件格式：{p.suffix}，视频请使用 mp4/mov/avi/mkv/webm"
                )

        for i, step in enumerate(steps):
            step_id = step["id"]
            fqn = step["function"]
            if on_step is not None:
                on_step(i + 1, len(steps), fqn)
            args = dict(step.get("args", {}))
            is_last = (i == len(steps) - 1)

            # Resolve input reference
            input_ref = step.get("input")
            if input_ref is None:
                previous_ref = f"${steps[i - 1]['id']}" if i > 0 else "$input"
                previous_val = self._resolve_ref(previous_ref, bindings)
                input_ref = "$last_media" if _is_non_media_artifact(previous_val) else previous_ref
            input_val = self._resolve_ref(input_ref, bindings)
            if self._video_step_should_skip_non_media_artifact(fqn, input_val):
                input_val = bindings.get("$last_media", "")

            # Resolve output path
            output_ref = step.get("output")
            if output_ref == "$output":
                out_path = output_path
            elif isinstance(output_ref, str) and output_ref.startswith("$") and output_ref not in bindings:
                # Self-reference like "$step_1" on step_1 — treat as auto temp
                out_path = str(self.temp_dir / f"{step_id}_{uuid.uuid4().hex[:8]}.mp4")
            elif output_ref is not None:
                out_path = self._resolve_ref(output_ref, bindings)
            elif is_last:
                out_path = output_path
            else:
                out_path = str(self.temp_dir / f"{step_id}_{uuid.uuid4().hex[:8]}.mp4")

            # Execute with auto-bridging
            try:
                result = self._execute_step(fqn, args, input_val, out_path)
            except FileNotFoundError as exc:
                raise FileNotFoundError(f"找不到输入文件：{exc}") from exc
            except ValueError as exc:
                msg = str(exc)
                if "Unresolved reference" in msg:
                    ref = msg.split("Unresolved reference:")[-1].strip()
                    raise ValueError(f"执行计划出错：步骤引用了不存在的变量 {ref}") from exc
                raise
            except Exception as exc:
                raise RuntimeError(f"第 {step_id} 步执行失败：{exc}") from exc
            bindings[f"${step_id}"] = result
            if _is_media_artifact(result):
                bindings["$last_media"] = result

        last_id = steps[-1]["id"]
        return bindings[f"${last_id}"]

    def run_with_task(self, plan: dict, input_path: str, output_path: str | None = None) -> str:
        """Execute plan, persist as a task, return task_id."""
        task_id = f"task_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
        if output_path is None:
            output_path = str((self.outputs_dir / f"{task_id}_out.mp4").resolve())

        # Save plan
        plan_path = self.plans_dir / f"{task_id}_plan.json"
        plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n")

        # Execute
        final_output = self.execute(plan, input_path, output_path)

        # Detect which models were used by the plan steps
        models_used = _detect_models_used(plan.get("steps", []))

        # Save task
        task = {
            "task_id": task_id,
            "status": "succeeded",
            "plan_id": plan.get("plan_id", f"plan_{task_id}"),
            "goal": plan.get("goal", ""),
            "outputs": [final_output],
            "created_at": datetime.now().isoformat(),
            "version": "2.0",
            "models_used": models_used,
        }
        task_path = self.tasks_dir / f"{task_id}.json"
        task_path.write_text(json.dumps(task, ensure_ascii=False, indent=2) + "\n")
        return task_id

    # ── Internal ───────────────────────────────────────────────────────

    def _resolve_ref(self, ref: Any, bindings: dict[str, Any]) -> Any:
        """Resolve a variable reference like ``$input`` or ``$step_1``."""
        if isinstance(ref, str) and ref.startswith("$"):
            val = bindings.get(ref)
            if val is None:
                raise ValueError(f"Unresolved reference: {ref}")
            return val
        if isinstance(ref, list):
            return [self._resolve_ref(item, bindings) for item in ref]
        if isinstance(ref, dict):
            return {key: self._resolve_ref(value, bindings) for key, value in ref.items()}
        return ref

    def _execute_step(self, fqn: str, args: dict, input_val: Any, output_path: str) -> Any:
        """Execute a single step with automatic I/O bridging."""
        info = get_info(fqn)
        func = info.func
        domain = info.domain

        input_is_path = isinstance(input_val, str)
        input_is_path_list = isinstance(input_val, list) and all(isinstance(item, str) for item in input_val)
        input_is_frames = isinstance(input_val, list) and not input_is_path_list

        # ── Generative picture: generate_image (no image input) ───────
        if domain == "picture" and info.name == "generate_image":
            result_img = func(**args)
            return _save_image_to_path(result_img, output_path)

        # ── Picture function on a video file → auto-wrap ──────────────
        if domain == "picture" and input_is_path:
            from gemia.video.frames import apply_picture_op_to_video
            op = _make_picture_op(func, args)
            return apply_picture_op_to_video(input_val, output_path, op=op)

        # ── Picture function on frame list → @batchable handles it ────
        if domain == "picture" and input_is_frames:
            return func(input_val, **args)

        # ── Video function on path list → pass to timeline/transition router ─
        if domain == "video" and input_is_path_list:
            return self._call_video_func(func, info, input_val, output_path, args)

        # ── Video function on frame list → write frames first ─────────
        if domain == "video" and input_is_frames:
            from gemia.video.frames import frames_to_video
            temp = str(self.temp_dir / f"frames_{uuid.uuid4().hex[:8]}.mp4")
            frames_to_video(input_val, output_path=temp)
            return self._call_video_func(func, info, temp, output_path, args)

        # ── Video function on file path → direct call ─────────────────
        if domain == "video" and input_is_path:
            return self._call_video_func(func, info, input_val, output_path, args)

        # ── Audio function ────────────────────────────────────────────
        if domain == "audio":
            return self._call_audio_func(func, info, input_val, args)

        raise ValueError(f"Cannot execute {fqn}: domain={domain}, input type={type(input_val).__name__}")

    def _video_step_should_skip_non_media_artifact(self, fqn: str, input_val: Any) -> bool:
        if not _is_non_media_artifact(input_val):
            return False
        try:
            return get_info(fqn).domain == "video"
        except KeyError:
            return False

    def _call_video_func(self, func: Any, info: Any, input_path: Any,
                         output_path: str, args: dict) -> str:
        """Route video functions based on their signature."""
        sig = inspect.signature(func)
        param_names = list(sig.parameters.keys())

        # Generative video functions: generate_video (no input_path needed)
        if info.name == "generate_video":
            return func(**args)

        # Generative video functions: take input_path as first positional arg
        if info.name in ("generate_video_from_image", "extend_video"):
            return func(input_path, **args)

        # Analysis functions: func(path, **kwargs) — no output_path
        if info.name in ("get_metadata", "detect_scenes"):
            return func(input_path, **args)

        # Stock search returns metadata; stock fetch primitives keep the
        # standard input/output signature for pipeline compatibility.
        if info.name == "search_stock_media":
            return func(**args)

        # generate_broll is text/script driven: it downloads public B-roll
        # into an output directory and does not consume the pipeline media path.
        if info.name == "generate_broll":
            call_args = dict(args)
            script_text = (
                call_args.pop("script_text", None)
                or call_args.pop("prompt", None)
                or call_args.pop("query", None)
                or "cinematic b-roll"
            )
            output_dir = call_args.pop("output_dir", None) or str(Path(output_path).with_suffix(""))
            call_args.pop("duration", None)
            return func(str(script_text), str(output_dir), **call_args)

        # extract_frames: func(path, **kwargs) → list[Image]
        if info.name == "extract_frames":
            return func(input_path, **args)

        # frames_to_video: func(frames, *, output_path, ...)
        if info.name == "frames_to_video":
            return func(input_path, output_path=output_path, **args)

        # concat: func(paths, output_path)
        if param_names and param_names[0] == "paths":
            paths = input_path if isinstance(input_path, list) else [input_path]
            normalized_paths = self._normalize_concat_inputs(paths)
            result = func(normalized_paths, output_path, **args)
            return result or output_path

        # Two-input compositing/transition functions: func(input_a, input_b, output_path, ...)
        if "input_a" in param_names and "input_b" in param_names:
            call_args = dict(args)
            if "input_a" not in call_args:
                call_args["input_a"] = input_path[0] if isinstance(input_path, list) and input_path else input_path
            if "input_b" not in call_args:
                if isinstance(input_path, list) and len(input_path) >= 2:
                    call_args["input_b"] = input_path[1]
                else:
                    raise UserInputError("双输入视频操作需要 input_a 和 input_b 两段素材。")
            input_a = call_args.pop("input_a")
            input_b = call_args.pop("input_b")
            result = func(input_a, input_b, output_path, **call_args)
            return result or output_path

        # apply_picture_op_to_video: skip (handled by picture auto-bridge)
        if info.name == "apply_picture_op_to_video":
            raise ValueError("apply_picture_op_to_video should not be called directly in plans; "
                             "use a picture function instead.")

        # Standard: func(input_path, output_path, **kwargs)
        if isinstance(input_path, list):
            if len(input_path) != 1:
                raise UserInputError("这个视频能力一次只能接收一段输入；多段素材请先用 timeline.concat 或 transition 组合。")
            input_path = input_path[0]
        result = func(input_path, output_path, **args)
        return result or output_path

    def _normalize_concat_inputs(self, paths: list[str]) -> list[str]:
        """Convert still images in a concat list into 3-second video clips."""
        normalized: list[str] = []
        for path in paths:
            source = Path(path).expanduser()
            if source.suffix.lower() in _IMAGE_EXTS:
                output = self.temp_dir / f"still_{uuid.uuid4().hex[:8]}.mp4"
                self._image_to_video_for_concat(source, output)
                normalized.append(str(output))
            else:
                normalized.append(str(source))
        return normalized

    def _image_to_video_for_concat(self, source: Path, output: Path, *, duration_sec: float = 3.0) -> None:
        output.parent.mkdir(parents=True, exist_ok=True)
        cmd = [
            ffmpeg_path(), "-y",
            "-loop", "1",
            "-t", str(duration_sec),
            "-i", str(source),
            "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2,format=yuv420p",
            "-r", "30",
            "-c:v", "libx264",
            "-an",
            str(output),
        ]
        proc = subprocess.run(cmd, text=True, capture_output=True)
        if proc.returncode != 0:
            raise RuntimeError(f"image-to-video concat preparation failed for {source}: {proc.stderr}")

    def _call_audio_func(self, func: Any, info: Any, input_val: Any, args: dict) -> Any:
        """Route audio functions.

        Auto-bridging: if input_val is a file path string, auto-load it via
        gemia.audio.basics.load, apply the function, and return the result array.
        The caller is responsible for saving if needed (via a subsequent save step).
        """
        import numpy as np

        # load: func(path, **kwargs) → (audio, sr)
        if info.name == "load":
            return func(input_val, **args)
        # save: func(path, audio, **kwargs)
        if info.name == "save":
            return func(input_val, **args)

        # Auto-bridge: if input is a file path, load it first
        if isinstance(input_val, str):
            from gemia.audio.basics import load as _audio_load
            audio_tuple = _audio_load(input_val)
            # load returns (audio_array, sr) tuple
            if isinstance(audio_tuple, tuple):
                audio_arr, sr = audio_tuple
            else:
                audio_arr, sr = audio_tuple, 22050
            # Inject sr into args if the function accepts it and it's not already set
            import inspect as _inspect
            sig = _inspect.signature(func)
            if "sr" in sig.parameters and "sr" not in args:
                args = {**args, "sr": sr}
            return func(audio_arr, **args)

        # input_val is already an ndarray or (ndarray, sr) tuple
        if isinstance(input_val, tuple):
            audio_arr, sr = input_val
            import inspect as _inspect
            sig = _inspect.signature(func)
            if "sr" in sig.parameters and "sr" not in args:
                args = {**args, "sr": sr}
            return func(audio_arr, **args)

        return func(input_val, **args)


def _make_picture_op(func: Any, args: dict) -> Any:
    """Create a closure for use with apply_picture_op_to_video."""
    def op(frame: Any) -> Any:
        return func(frame, **args)
    return op


def _path_suffix(value: str) -> str:
    return Path(value).expanduser().suffix.lower()


def _is_media_path(value: str) -> bool:
    return bool(str(value or "").strip()) and _path_suffix(value) in _MEDIA_EXTS


def _is_media_artifact(value: Any) -> bool:
    if isinstance(value, str):
        return _is_media_path(value)
    if isinstance(value, list) and value:
        return all(isinstance(item, str) and _is_media_path(item) for item in value)
    return False


def _is_non_media_artifact(value: Any) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    suffix = _path_suffix(value)
    if suffix in _MEDIA_EXTS:
        return False
    if suffix in _NON_MEDIA_ARTIFACT_EXTS:
        return True
    path = Path(value).expanduser()
    return path.exists() and path.is_file() and suffix not in _MEDIA_EXTS


def _save_image_to_path(img: np.ndarray, path: str) -> str:
    """Save a float32 BGR ndarray to disk. Creates parent dirs. Returns actual path.

    If ``path`` ends with ``.mp4`` the extension is changed to ``.png`` so the
    image is written as a valid image file rather than a video container.

    Args:
        img: float32 BGR ndarray to save.
        path: Desired output path (may be ``.mp4``; will be rewritten to ``.png``).

    Returns:
        Actual path where the image was written.

    Raises:
        RuntimeError: If ``cv2.imwrite`` fails.
    """
    import cv2 as _cv2
    from gemia.primitives_common import to_uint8
    actual_path = path[:-4] + ".png" if path.endswith(".mp4") else path
    Path(actual_path).parent.mkdir(parents=True, exist_ok=True)
    img_u8 = to_uint8(img)
    if not _cv2.imwrite(actual_path, img_u8):
        raise RuntimeError(f"Failed to write image to {actual_path}")
    return actual_path


def _detect_models_used(steps: list[dict]) -> list[str]:
    """Detect which backend models are used by a plan's steps.

    Args:
        steps: List of step dicts from a v2 plan.

    Returns:
        Sorted list of model/backend identifiers, e.g.
        ``["ffmpeg", "nano_banana_flash", "opencv"]``.
    """
    models: set[str] = set()
    for step in steps:
        fqn = step.get("function", "")
        if "generative" in fqn and "picture" in fqn:
            tier = step.get("args", {}).get("model_tier", "flash")
            models.add(f"nano_banana_{tier}")
        elif "generative" in fqn and "video" in fqn:
            models.add("veo")
        elif "picture" in fqn or "audio" in fqn:
            models.add("opencv")
        elif "video" in fqn:
            models.add("ffmpeg")
    return sorted(models)
