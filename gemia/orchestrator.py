from __future__ import annotations

import asyncio
import json
import math
import shutil
import subprocess
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from .ai.ai_client import AIClient
from .registry import resolve as registry_resolve, get_info as registry_get_info


class GemiaOrchestrator:
    def __init__(self, root_dir: str | Path | None = None) -> None:
        this_file = Path(__file__).resolve()
        self.root_dir = Path(root_dir) if root_dir else this_file.parent.parent
        self.tasks_dir = self.root_dir / "tasks"
        self.skills_dir = self.root_dir / "skills"
        self.plans_dir = self.root_dir / "plans"
        self.outputs_dir = self.root_dir / "outputs"
        self.frames_dir = self.root_dir / "frames"
        self.styled_dir = self.root_dir / "styled"
        self.temp_dir = self.root_dir / "temp"
        for p in [self.tasks_dir, self.skills_dir, self.plans_dir, self.outputs_dir, self.frames_dir, self.styled_dir, self.temp_dir]:
            p.mkdir(parents=True, exist_ok=True)

    def run_plan(self, plan_path: str) -> str:
        plan_file = Path(plan_path)
        if not plan_file.is_absolute():
            plan_file = (self.root_dir / plan_file).resolve()
        plan = json.loads(plan_file.read_text())
        task_id = self._new_task_id()
        plan_id = plan.get("plan_id") or plan_file.stem
        output_path = self._execute_plan(plan, task_id)
        task = {
            "task_id": task_id,
            "status": "succeeded",
            "plan_id": plan_id,
            "outputs": [output_path],
            "created_at": datetime.now().isoformat(),
        }
        self._save_task(task)
        return task_id

    def run_skill(self, skill_id: str, inputs: dict) -> str:
        skill_path = self.skills_dir / f"{skill_id}.json"
        if not skill_path.exists():
            raise FileNotFoundError(f"Skill not found: {skill_path}")
        skill = json.loads(skill_path.read_text())
        task_id = self._new_task_id()
        plan = self._expand_skill_to_plan(skill, inputs, task_id)
        plan_path = self.plans_dir / f"{task_id}_plan.json"
        plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n")
        output_path = self._execute_plan(plan, task_id)
        task = {
            "task_id": task_id,
            "status": "succeeded",
            "plan_id": plan["plan_id"],
            "outputs": [output_path],
            "created_at": datetime.now().isoformat(),
        }
        self._save_task(task)
        return task_id

    def run_plan_dict(self, plan: dict, progress_callback=None) -> str:
        """Execute a plan dict directly (no file needed)."""
        task_id = self._new_task_id()
        plan.setdefault("plan_id", f"plan_{task_id}")
        plan_path = self.plans_dir / f"{task_id}_plan.json"
        plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n")
        output_path = self._execute_plan(plan, task_id, progress_callback=progress_callback)
        task = {
            "task_id": task_id,
            "status": "succeeded",
            "plan_id": plan["plan_id"],
            "outputs": [output_path],
            "created_at": datetime.now().isoformat(),
        }
        self._save_task(task)
        return task_id

    def plan_or_ask(self, request: str, *, input_path: str, output_path: str, answers: dict | None = None, agent: str | None = None) -> dict:
        """Return {"ask": true, "questions": [...]} or a Plan dict."""
        client = AIClient()
        return asyncio.run(client.plan_or_ask(
            request,
            input_path=input_path,
            output_path=output_path,
            answers=answers,
            agent=agent,
        ))

    def plan_from_prompt(self, request: str, *, input_path: str, agent: str | None = None) -> dict:
        output_path = str((self.outputs_dir / f"plan_preview_{uuid.uuid4().hex[:8]}.mp4").resolve())
        client = AIClient()
        plan = asyncio.run(client.plan_from_prompt(request, input_path=input_path, output_path=output_path, agent=agent))
        return plan

    def plan_from_primitives(self, request: str, *, input_path: str, output_path: str, answers: dict | None = None, agent: str | None = None) -> dict:
        """Return {"ask": true, "questions": [...]} or a Plan v2 dict using the primitive catalog."""
        client = AIClient()
        return asyncio.run(client.plan_from_primitives(
            request,
            input_path=input_path,
            output_path=output_path,
            answers=answers,
            agent=agent,
        ))

    def get_task(self, task_id: str) -> dict:
        path = self.tasks_dir / f"{task_id}.json"
        return json.loads(path.read_text())

    def get_assets(self, task_id: str) -> dict:
        task = self.get_task(task_id)
        assets = []
        for output in task.get("outputs", []):
            p = Path(output)
            # Try to make path relative to _BASE_DIR so /file/ route can serve it
            try:
                rel = p.relative_to(self.root_dir)
                serve_path = str(rel)
            except ValueError:
                serve_path = str(p)
            assets.append({
                "path": serve_path,
                "abs_path": str(p),
                "exists": p.exists(),
                "size_bytes": p.stat().st_size if p.exists() else None,
                "kind": p.suffix.lower().lstrip("."),
            })
        return {
            "task_id": task_id,
            "assets": assets,
        }

    def _new_task_id(self) -> str:
        return f"task_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"

    def _save_task(self, task: dict[str, Any]) -> None:
        path = self.tasks_dir / f"{task['task_id']}.json"
        path.write_text(json.dumps(task, ensure_ascii=False, indent=2) + "\n")

    def _expand_skill_to_plan(self, skill: dict[str, Any], inputs: dict[str, Any], task_id: str) -> dict[str, Any]:
        pipeline = skill.get("pipeline", [])
        input_video = inputs.get("video") or ((inputs.get("videos") or [None])[0])
        output_ext = ".mp4"
        output_path = str((self.outputs_dir / f"{task_id}_preview{output_ext}").resolve())

        def resolve_value(value: Any) -> Any:
            if isinstance(value, str) and value.startswith("{{parameters.") and value.endswith("}}"):
                key = value[len("{{parameters."):-2]
                return inputs.get(key)
            if isinstance(value, list):
                return [resolve_value(v) for v in value]
            if isinstance(value, dict):
                return {k: resolve_value(v) for k, v in value.items()}
            return value

        steps = []
        for raw_step in pipeline:
            step = {
                "id": raw_step["id"],
                "type": raw_step["type"],
                "params": resolve_value(raw_step.get("params", {})),
            }
            if raw_step.get("depends_on"):
                step["depends_on"] = raw_step["depends_on"]
            if step["type"] == "extract_keyframes":
                step["params"].setdefault("output_dir", str((self.frames_dir / task_id).resolve()))
            if step["type"] == "stylize_images":
                style = step["params"].get("style_prompt", "styled")
                step["params"].setdefault("output_dir", str((self.styled_dir / task_id).resolve()))
                step["params"].setdefault("suffix", self._slug(str(style)))
            if step["type"] in {"compose_preview_video", "trim_clip", "change_speed", "add_subtitle", "color_grade", "merge_clips"}:
                step["params"].setdefault("output_path", output_path)
            steps.append(step)

        return {
            "plan_id": f"plan_{task_id}",
            "skill_id": skill["skill_id"],
            "goal": f"Run skill {skill['skill_id']}",
            "input_path": input_video,
            "output_path": output_path,
            "inputs": inputs,
            "steps": steps,
        }

    def _execute_plan(self, plan: dict[str, Any], task_id: str,
                      progress_callback: "Callable[[int, int, str], None] | None" = None) -> str:
        context = {
            "input_path": plan["input_path"],
            "output_path": plan["output_path"],
            "step_outputs": {},
        }
        steps = plan.get("steps", [])
        for i, step in enumerate(steps):
            if progress_callback is not None:
                fqn = step.get("function", step.get("type", ""))
                progress_callback(i + 1, len(steps), fqn)
            if "function" in step:
                assets = self._step_primitive_v2(step, context)
                context["step_outputs"][step["id"]] = assets
                if assets:
                    context["input_path"] = assets[0]
                continue
            step_type = step["type"]
            if step_type == "extract_keyframes":
                assets = self._step_extract_keyframes(step, context)
            elif step_type == "stylize_images":
                assets = self._step_stylize_images(step, context)
            elif step_type == "compose_preview_video":
                assets = self._step_compose_preview_video(step, context)
            elif step_type == "trim_clip":
                assets = self._step_trim_clip(step, context)
            elif step_type == "change_speed":
                assets = self._step_change_speed(step, context)
            elif step_type == "add_subtitle":
                assets = self._step_add_subtitle(step, context)
            elif step_type == "color_grade":
                assets = self._step_color_grade(step, context)
            elif step_type == "merge_clips":
                assets = self._step_merge_clips(step, context)
            else:
                raise ValueError(f"Unsupported step type: {step_type}")
            context["step_outputs"][step["id"]] = assets
            if assets:
                context["input_path"] = assets[0]
        return context["input_path"]

    def _step_extract_keyframes(self, step: dict[str, Any], context: dict[str, Any]) -> list[str]:
        input_path = Path(context["input_path"])
        out_dir = Path(step.get("params", {}).get("output_dir", self.frames_dir))
        requested = int(step.get("params", {}).get("num_keyframes", 1) or 1)
        duration = self._probe_duration(input_path)
        fps = max(requested / duration, 0.001) if duration > 0 else 0.2
        fps = min(fps, 2.0)
        out_dir.mkdir(parents=True, exist_ok=True)
        output_pattern = out_dir / "keyframe-%02d.png"
        self._run([
            "ffmpeg", "-y", "-i", str(input_path), "-vf", f"fps={fps}", str(output_pattern)
        ])
        outputs = [str(p.resolve()) for p in sorted(out_dir.glob("keyframe-*.png"))]
        return outputs[:requested]

    def _step_stylize_images(self, step: dict[str, Any], context: dict[str, Any]) -> list[str]:
        depends_on = step.get("depends_on", [])
        inputs = context.get("step_outputs", {}).get(depends_on[0], []) if depends_on else []
        if not inputs:
            raise ValueError("stylize_images requires extracted frames")
        output_dir = Path(step.get("params", {}).get("output_dir", self.styled_dir))
        suffix = step.get("params", {}).get("suffix", "styled")
        style_prompt = step.get("params", {}).get("style_prompt", "")
        output_dir.mkdir(parents=True, exist_ok=True)
        outputs = []
        reference = self._find_reference_stylized_image(style_prompt)
        mode = "reference-reuse" if reference else "minimal-copy-placeholder"
        for src in inputs[:1]:
            src_path = Path(src)
            dest = output_dir / f"{src_path.stem}-{suffix}.jpg"
            shutil.copyfile(reference or src_path, dest)
            outputs.append(str(dest.resolve()))
        record = {
            "mode": mode,
            "style_prompt": style_prompt,
            "reference_image": str(reference.resolve()) if reference else None,
            "inputs": inputs,
            "outputs": outputs,
        }
        (output_dir / "stylize-run.json").write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n")
        return outputs

    def _step_compose_preview_video(self, step: dict[str, Any], context: dict[str, Any]) -> list[str]:
        depends_on = step.get("depends_on", [])
        original_assets = context.get("step_outputs", {}).get(depends_on[0], []) if len(depends_on) > 0 else []
        styled_assets = context.get("step_outputs", {}).get(depends_on[1], []) if len(depends_on) > 1 else []
        if not original_assets or not styled_assets:
            raise ValueError("compose_preview_video requires original and styled frame assets")
        output_format = step.get("params", {}).get("output_format", "before_after_hstack")
        if output_format != "before_after_hstack":
            raise ValueError(f"Unsupported output_format: {output_format}")
        orig = original_assets[0]
        styled = styled_assets[0]
        output_path = Path(step.get("params", {}).get("output_path") or context["output_path"])
        output_path.parent.mkdir(parents=True, exist_ok=True)
        filter_complex = (
            "[0:v]scale=1080:1080:force_original_aspect_ratio=decrease,"
            "pad=1080:1080:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1[v0];"
            "[1:v]scale=1080:1080:force_original_aspect_ratio=decrease,"
            "pad=1080:1080:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1[v1];"
            "[v0][v1]hstack=inputs=2,format=yuv420p[v]"
        )
        self._run([
            "ffmpeg", "-y",
            "-loop", "1", "-t", "3", "-i", str(orig),
            "-loop", "1", "-t", "3", "-i", str(styled),
            "-filter_complex", filter_complex,
            "-map", "[v]", "-r", "30", "-c:v", "libx264", "-pix_fmt", "yuv420p",
            str(output_path),
        ])
        return [str(output_path.resolve())]

    def _step_trim_clip(self, step: dict[str, Any], context: dict[str, Any]) -> list[str]:
        input_path = Path(context["input_path"])
        params = step.get("params", {})
        start_sec = float(params.get("start_sec", 0) or 0)
        end_sec = float(params.get("end_sec", 0) or 0)
        output_path = Path(params.get("output_path") or context["output_path"])
        output_path.parent.mkdir(parents=True, exist_ok=True)
        self._run([
            "ffmpeg", "-y", "-ss", str(start_sec), "-to", str(end_sec), "-i", str(input_path),
            "-c:v", "libx264", "-c:a", "aac", str(output_path)
        ])
        return [str(output_path.resolve())]

    def _step_change_speed(self, step: dict[str, Any], context: dict[str, Any]) -> list[str]:
        input_path = Path(context["input_path"])
        params = step.get("params", {})
        speed = float(params.get("speed", 1.0) or 1.0)
        if speed <= 0:
            raise ValueError("speed must be > 0")
        output_path = Path(params.get("output_path") or context["output_path"])
        output_path.parent.mkdir(parents=True, exist_ok=True)
        video_filter = f"setpts={1/speed}*PTS"
        if 0.5 <= speed <= 2.0:
            audio_filter = f"atempo={speed}"
            self._run([
                "ffmpeg", "-y", "-i", str(input_path),
                "-filter:v", video_filter,
                "-filter:a", audio_filter,
                "-c:v", "libx264", "-c:a", "aac", str(output_path)
            ])
        else:
            self._run([
                "ffmpeg", "-y", "-i", str(input_path),
                "-filter:v", video_filter,
                "-an", "-c:v", "libx264", str(output_path)
            ])
        return [str(output_path.resolve())]

    def _step_add_subtitle(self, step: dict[str, Any], context: dict[str, Any]) -> list[str]:
        input_path = Path(context["input_path"])
        params = step.get("params", {})
        text = str(params.get("text", "")).strip() or "Subtitle"
        duration_sec = float(params.get("duration_sec", 3) or 3)
        output_path = Path(params.get("output_path") or context["output_path"])
        output_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            escaped = text.replace("'", "\\'").replace(":", "\\:")
            drawtext = (
                f"drawtext=text='{escaped}':x=(w-text_w)/2:y=h-(text_h*2):fontsize=42:fontcolor=white:"
                f"box=1:boxcolor=black@0.45:boxborderw=12:enable='between(t,0,{duration_sec})'"
            )
            self._run([
                "ffmpeg", "-y", "-i", str(input_path), "-vf", drawtext,
                "-c:v", "libx264", "-c:a", "aac", str(output_path)
            ])
        except RuntimeError as exc:
            if "No such filter: 'drawtext'" not in str(exc):
                raise
            fallback = f"subtitled: {self._slug(text)}"
            self._run([
                "ffmpeg", "-y", "-i", str(input_path),
                "-metadata", f"comment={fallback}",
                "-c:v", "libx264", "-c:a", "aac", str(output_path)
            ])
        return [str(output_path.resolve())]

    def _step_color_grade(self, step: dict[str, Any], context: dict[str, Any]) -> list[str]:
        input_path = Path(context["input_path"])
        params = step.get("params", {})
        preset = str(params.get("preset", "warm")).lower()
        filters = {
            "warm": "eq=saturation=1.15:contrast=1.05:brightness=0.03,colorbalance=rs=.08:gs=.02:bs=-.03",
            "cool": "eq=saturation=0.95:contrast=1.03:brightness=-0.01,colorbalance=rs=-.04:gs=.01:bs=.08",
            "vintage": "curves=vintage,eq=saturation=0.85:contrast=0.95",
            "cyberpunk": "eq=saturation=1.4:contrast=1.15, colorbalance=rs=.12:gs=-.02:bs=.16"
        }
        vf = filters.get(preset, filters["warm"])
        output_path = Path(params.get("output_path") or context["output_path"])
        output_path.parent.mkdir(parents=True, exist_ok=True)
        self._run([
            "ffmpeg", "-y", "-i", str(input_path), "-vf", vf,
            "-c:v", "libx264", "-c:a", "aac", str(output_path)
        ])
        return [str(output_path.resolve())]

    def _step_merge_clips(self, step: dict[str, Any], context: dict[str, Any]) -> list[str]:
        params = step.get("params", {})
        videos = params.get("videos") or []
        if not videos:
            raise ValueError("merge_clips requires videos list")
        output_path = Path(params.get("output_path") or context["output_path"])
        output_path.parent.mkdir(parents=True, exist_ok=True)
        list_file = self.temp_dir / f"merge-{uuid.uuid4().hex[:8]}.txt"
        list_file.write_text("\n".join([f"file '{Path(v).resolve()}'" for v in videos]) + "\n")
        self._run([
            "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(list_file),
            "-c:v", "libx264", "-c:a", "aac", str(output_path)
        ])
        return [str(output_path.resolve())]

    def _resolve_step_var(self, value: str, context: dict[str, Any]) -> str:
        """Resolve a variable reference like $input, $output, or $step_N."""
        if value == "$input":
            return context["input_path"]
        if value == "$output":
            return context["output_path"]
        if value.startswith("$"):
            step_ref = value[1:]  # e.g. "step_1"
            step_assets = context["step_outputs"].get(step_ref)
            if step_assets:
                return step_assets[0]
            raise ValueError(f"Step reference {value!r} not found in context")
        return value

    def _step_primitive_v2(self, step: dict[str, Any], context: dict[str, Any]) -> list[str]:
        """Execute a v2 plan step with a 'function' key (FQN dispatch)."""
        fqn = step["function"]
        args = dict(step.get("args") or {})

        # Resolve input
        raw_input = step.get("input")
        if raw_input is not None:
            resolved_input = self._resolve_step_var(raw_input, context)
        else:
            resolved_input = context["input_path"]

        # Resolve output
        raw_output = step.get("output")
        if raw_output is not None:
            resolved_output = self._resolve_step_var(raw_output, context)
        else:
            resolved_output = str(self.temp_dir / f"step_{step['id']}_{uuid.uuid4().hex[:6]}.mp4")

        # Ensure parent dir exists
        Path(resolved_output).parent.mkdir(parents=True, exist_ok=True)

        fn = registry_resolve(fqn)
        info = registry_get_info(fqn)

        # Determine call style from the first parameter
        import inspect
        params = list(info.params.items())
        is_picture_fn = False
        if params:
            first_param_name, first_param_info = params[0]
            annotation = first_param_info.get("annotation", "")
            if first_param_name in ("img",) or annotation in ("Image", "ndarray", "np.ndarray"):
                is_picture_fn = True
            elif first_param_name in ("video_path", "input_path", "path") or annotation == "str":
                is_picture_fn = False

        if is_picture_fn:
            from gemia.video.frames import apply_picture_op_to_video
            apply_picture_op_to_video(resolved_input, resolved_output, op=lambda img: fn(img, **args))
        else:
            fn(resolved_input, resolved_output, **args)

        return [resolved_output]

    def _run(self, cmd: list[str]) -> None:
        proc = subprocess.run(cmd, text=True, capture_output=True)
        if proc.returncode != 0:
            raise RuntimeError(
                f"Command failed: {' '.join(cmd)}\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
            )

    def _probe_duration(self, input_path: Path) -> float:
        proc = subprocess.run([
            "ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(input_path)
        ], text=True, capture_output=True)
        if proc.returncode != 0:
            return 0.0
        try:
            payload = json.loads(proc.stdout or "{}")
            return max(float((payload.get("format") or {}).get("duration") or 0.0), 0.0)
        except Exception:
            return 0.0

    def _find_reference_stylized_image(self, style_prompt: str) -> Path | None:
        style = style_prompt.lower()
        candidates = [
            ("cyberpunk", self.styled_dir / "keyframe-01-cyberpunk-openrouter.jpg"),
            ("vintage", self.styled_dir / "keyframe-01-vintage_test_1774745090.jpg"),
        ]
        for keyword, path in candidates:
            if keyword in style and path.exists():
                return path
        return None

    def _slug(self, text: str) -> str:
        cleaned = ''.join(ch.lower() if ch.isalnum() else '_' for ch in text).strip('_')
        while '__' in cleaned:
            cleaned = cleaned.replace('__', '_')
        return cleaned or 'styled'


def run_plan(plan_path: str) -> str:
    return GemiaOrchestrator().run_plan(plan_path)


def run_skill(skill_id: str, inputs: dict) -> str:
    return GemiaOrchestrator().run_skill(skill_id, inputs)


def get_task(task_id: str) -> dict:
    return GemiaOrchestrator().get_task(task_id)


def get_assets(task_id: str) -> dict:
    return GemiaOrchestrator().get_assets(task_id)


def plan_from_primitives(request: str, *, input_path: str, output_path: str, answers: dict | None = None) -> dict:
    return GemiaOrchestrator().plan_from_primitives(request, input_path=input_path, output_path=output_path, answers=answers)
