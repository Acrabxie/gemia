from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .base import LocalExecutor


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    proc = subprocess.run(cmd, text=True, capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError(f"Command failed: {' '.join(cmd)}\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}")
    return proc


class ExtractKeyframesExecutor(LocalExecutor):
    step_type = "extract_keyframes"

    async def validate(self, step: dict[str, Any], context: dict[str, Any]) -> None:
        if not context.get("input_path"):
            raise ValueError("input_path missing in context")

    async def submit(self, step: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        await super().submit(step, context)
        input_path = Path(context["input_path"])
        out_dir = Path(step.get("params", {}).get("output_dir", "frames"))
        fps = float(step.get("params", {}).get("fps", 0.2))
        out_dir.mkdir(parents=True, exist_ok=True)
        output_pattern = out_dir / "keyframe-%02d.png"
        _run([
            "ffmpeg", "-y", "-i", str(input_path), "-vf", f"fps={fps}", str(output_pattern)
        ])
        files = sorted(str(p.resolve()) for p in out_dir.glob("keyframe-*.png"))
        return {"status": "submitted", "step": step, "files": files, "output_dir": str(out_dir.resolve())}

    async def finalize(self, execution: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        return {"status": "done", "assets": execution.get("files", []), "meta": {"output_dir": execution.get("output_dir")}}


class StylizeImagesExecutor(LocalExecutor):
    step_type = "stylize_images"

    async def submit(self, step: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        await super().submit(step, context)
        inputs = context.get("step_outputs", {}).get(step.get("depends_on", [None])[0], [])
        if not inputs:
            raise ValueError("stylize_images requires extracted frames from previous step")
        output_dir = Path(step.get("params", {}).get("output_dir", "styled"))
        suffix = step.get("params", {}).get("suffix", "styled")
        output_dir.mkdir(parents=True, exist_ok=True)
        outputs = []
        for src in inputs:
            src_path = Path(src)
            dest = output_dir / f"{src_path.stem}-{suffix}.jpg"
            shutil.copyfile(src_path, dest)
            outputs.append(str(dest.resolve()))
        record = {
            "style_prompt": step.get("params", {}).get("style", ""),
            "mode": "minimal-copy-placeholder",
            "inputs": inputs,
            "outputs": outputs,
        }
        (output_dir / "stylize-run.json").write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n")
        return {"status": "submitted", "step": step, "files": outputs, "output_dir": str(output_dir.resolve()), "record": record}

    async def finalize(self, execution: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        return {"status": "done", "assets": execution.get("files", []), "meta": execution.get("record", {})}


class ComposePreviewVideoExecutor(LocalExecutor):
    step_type = "compose_preview_video"

    async def submit(self, step: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        await super().submit(step, context)
        extracted = context.get("step_outputs", {}).get(step.get("depends_on", [None])[0], [])
        if not extracted:
            raise ValueError("compose_preview_video requires stylized frames from previous step")
        original_video = Path(context["input_path"])
        output_path = Path(step.get("params", {}).get("output_path") or context["output_path"])
        output_path.parent.mkdir(parents=True, exist_ok=True)
        style_note = step.get("params", {}).get("style_note", "preview")
        drawtext = f"drawtext=text={style_note!r}:x=20:y=20:fontsize=28:fontcolor=white:box=1:boxcolor=black@0.5"
        _run([
            "ffmpeg", "-y", "-i", str(original_video), "-vf", drawtext, "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(output_path)
        ])
        return {"status": "submitted", "step": step, "file": str(output_path.resolve())}

    async def finalize(self, execution: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        return {"status": "done", "assets": [execution["file"]]}
