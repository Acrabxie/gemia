#!/usr/bin/env python3
import argparse
import json
import os
import re
import subprocess
import sys
import textwrap
import urllib.error
import urllib.request
from pathlib import Path


DEFAULT_MODEL = os.environ.get("OPENROUTER_MODEL", "google/gemini-2.5-flash")
OUTPUT_DIR = Path("outputs")
PLAN_SCHEMA_HINT = {
    "version": "1.0",
    "input_path": "string",
    "output_path": "string",
    "operations": [
        {
            "op": "trim | resize | color",
            "enabled": True,
        }
    ],
}


def run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=check, text=True, capture_output=True)


def ffprobe_video(input_path: Path) -> dict:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height",
        "-show_entries",
        "format=duration",
        "-of",
        "json",
        str(input_path),
    ]
    result = run(cmd)
    data = json.loads(result.stdout or "{}")
    stream = (data.get("streams") or [{}])[0]
    fmt = data.get("format") or {}
    return {
        "width": int(stream.get("width") or 0),
        "height": int(stream.get("height") or 0),
        "duration": float(fmt.get("duration") or 0.0),
    }


def extract_json_block(text: str) -> dict:
    text = text.strip()
    fenced = re.search(r"```json\s*(\{.*?\})\s*```", text, re.S)
    if fenced:
        return json.loads(fenced.group(1))
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        return json.loads(text[start : end + 1])
    raise ValueError("No JSON object found in model response")


def call_openrouter(prompt: str, video_meta: dict, input_path: Path, output_path: Path, model: str) -> dict:
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY is missing")

    system_prompt = textwrap.dedent(
        f"""
        You are a video edit planner for a Python + ffmpeg CLI MVP.
        Return JSON only.
        Keep the plan minimal and executable.
        Allowed operations:
        - trim: {{"op":"trim","enabled":true,"start":number,"end":number}}
        - resize: {{"op":"resize","enabled":true,"width":int,"height":int}}
        - color: {{"op":"color","enabled":true,"eq":{{"brightness":number,"contrast":number,"saturation":number}}}}

        Requirements:
        - Choose at most 2 operations.
        - If the user prompt is vague, prefer one safe operation only.
        - Preserve audio when possible.
        - Output must match this shape:
        {json.dumps(PLAN_SCHEMA_HINT, ensure_ascii=True)}
        """
    ).strip()

    user_prompt = {
        "user_prompt": prompt,
        "input_path": str(input_path),
        "output_path": str(output_path),
        "video_meta": video_meta,
    }
    payload = {
        "model": model,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(user_prompt, ensure_ascii=True)},
        ],
    }

    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://local-gemia-mvp",
            "X-Title": "gemia-mvp",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"OpenRouter HTTP {exc.code}: {error_body}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"OpenRouter request failed: {exc}") from exc

    choices = body.get("choices") or []
    if not choices:
        raise RuntimeError(f"OpenRouter returned no choices: {body}")
    content = choices[0].get("message", {}).get("content", "")
    if not content:
        raise RuntimeError(f"OpenRouter returned empty content: {body}")
    return extract_json_block(content)


def local_fallback_plan(prompt: str, video_meta: dict, input_path: Path, output_path: Path) -> dict:
    p = prompt.lower()
    ops = []
    duration = video_meta["duration"]
    width = video_meta["width"] or 640
    height = video_meta["height"] or 360

    if any(word in p for word in ["trim", "cut", "short", "clip", "first"]):
        end = min(3.0, duration) if duration > 0 else 3.0
        ops.append({"op": "trim", "enabled": True, "start": 0.0, "end": max(1.0, end)})

    if any(word in p for word in ["resize", "smaller", "720", "540", "vertical", "square"]):
        target_w, target_h = 640, 360
        if "720" in p:
            target_w, target_h = 1280, 720
        elif "540" in p:
            target_w, target_h = 960, 540
        elif "square" in p:
            target_w, target_h = 720, 720
        elif "vertical" in p:
            target_w, target_h = 720, 1280
        if target_w != width or target_h != height:
            ops.append({"op": "resize", "enabled": True, "width": target_w, "height": target_h})

    if any(word in p for word in ["bright", "brighter", "color", "contrast", "saturation", "vivid"]):
        ops.append(
            {
                "op": "color",
                "enabled": True,
                "eq": {"brightness": 0.05, "contrast": 1.08, "saturation": 1.15},
            }
        )

    if not ops:
        ops.append({"op": "trim", "enabled": True, "start": 0.0, "end": max(1.0, min(3.0, duration or 3.0))})

    return {
        "version": "1.0",
        "input_path": str(input_path),
        "output_path": str(output_path),
        "operations": ops[:2],
    }


def build_filter_chain(operations: list[dict]) -> str:
    filters = []
    for op in operations:
        if not op.get("enabled", True):
            continue
        if op["op"] == "resize":
            filters.append(f"scale={int(op['width'])}:{int(op['height'])}")
        elif op["op"] == "color":
            eq = op.get("eq") or {}
            brightness = float(eq.get("brightness", 0))
            contrast = float(eq.get("contrast", 1.0))
            saturation = float(eq.get("saturation", 1.0))
            filters.append(f"eq=brightness={brightness}:contrast={contrast}:saturation={saturation}")
    return ",".join(filters)


def execute_plan(plan: dict, output_path: Path) -> None:
    input_path = Path(plan["input_path"])
    operations = [op for op in plan.get("operations", []) if op.get("enabled", True)]
    trim_op = next((op for op in operations if op.get("op") == "trim"), None)
    vf = build_filter_chain(operations)

    cmd = ["ffmpeg", "-y"]
    if trim_op:
        cmd += ["-ss", str(trim_op.get("start", 0.0))]
    cmd += ["-i", str(input_path)]
    if trim_op and trim_op.get("end") is not None:
        duration = max(0.1, float(trim_op["end"]) - float(trim_op.get("start", 0.0)))
        cmd += ["-t", str(duration)]
    if vf:
        cmd += ["-vf", vf]
    cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-movflags", "+faststart", str(output_path)]

    proc = subprocess.run(cmd, text=True, capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}")


def ensure_test_video(path: Path) -> Path:
    if path.exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg",
        "-y",
        "-f",
        "lavfi",
        "-i",
        "testsrc=size=1280x720:rate=30",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=880:sample_rate=44100",
        "-t",
        "5",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        str(path),
    ]
    proc = subprocess.run(cmd, text=True, capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError(f"Failed to generate test video: {proc.stderr}")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="Minimal Gemia CLI MVP")
    parser.add_argument("--input", required=True, help="Local video file path")
    parser.add_argument("--prompt", required=True, help="Natural language edit prompt")
    parser.add_argument("--output", help="Output file path")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="OpenRouter model id")
    parser.add_argument("--save-plan", help="Optional path to save plan JSON")
    parser.add_argument("--allow-fallback", action="store_true", default=True)
    parser.add_argument("--no-fallback", dest="allow_fallback", action="store_false")
    parser.add_argument("--create-test-video-if-missing", action="store_true")
    args = parser.parse_args()

    input_path = Path(args.input).expanduser().resolve()
    if args.create_test_video_if_missing:
        input_path = ensure_test_video(input_path)
    elif not input_path.exists():
        raise SystemExit(f"Input file does not exist: {input_path}")

    OUTPUT_DIR.mkdir(exist_ok=True)
    output_path = Path(args.output).expanduser().resolve() if args.output else (OUTPUT_DIR / f"{input_path.stem}_edited.mp4").resolve()
    video_meta = ffprobe_video(input_path)

    plan_source = "openrouter"
    try:
        plan = call_openrouter(args.prompt, video_meta, input_path, output_path, args.model)
    except Exception as exc:
        if not args.allow_fallback:
            raise
        plan_source = f"fallback ({exc})"
        plan = local_fallback_plan(args.prompt, video_meta, input_path, output_path)

    plan["input_path"] = str(input_path)
    plan["output_path"] = str(output_path)

    if args.save_plan:
        Path(args.save_plan).write_text(json.dumps(plan, indent=2, ensure_ascii=False) + "\n")

    execute_plan(plan, output_path)

    print(json.dumps({"plan_source": plan_source, "plan": plan, "result_file": str(output_path)}, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
