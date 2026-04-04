# Gemia

**AI creative workflow engine** — not an editor, not a plugin. A programmable layer where AI plans and executes video/image/audio operations through composable primitive functions.

Codex brought AI into the programmer's workflow. Gemia does the same for creative work.

---

## Core ideas

### 1. Primitive API

45+ pure Python functions across three domains — color grading, blur, trim, speed, EQ, and more. Each function has a clear signature and docstring. The AI reads these docstrings to understand what's available.

```
gemia.picture.color.color_grade(image, preset="cyberpunk")
gemia.video.timeline.cut(input_path, output_path, start_sec=0, end_sec=3)
gemia.audio.frequency.eq(audio, bands={...})
```

Picture functions automatically work on video — the engine extracts frames, applies the operation per-frame, and re-encodes with original audio. The AI doesn't need to know this; it just picks the right function.

### 2. Skills

A Skill is a saved execution plan. Run a prompt, get a good result, save it as a Skill. Apply it to any video later — no AI call needed.

```
run → save-skill → run-skill → done
```

Skills are JSON templates with `$input` / `$output` variable binding. Concrete paths are stripped; the pipeline is portable.

### 3. Orchestrator

Describe what you want in natural language. Gemia sends your prompt + the full function catalog to Gemini. The AI returns a structured plan (not code, not ffmpeg commands). The engine executes it.

If the prompt is vague, the AI asks clarifying questions first (Ask mechanism).

---

## Install

```bash
git clone https://github.com/nicekate/gemia.git && cd gemia

# Python 3.12+, ffmpeg required
pip install -e .

# API key (Gemini via OpenRouter)
export OPENROUTER_API_KEY="sk-or-..."
```

Verify:

```bash
python3 -m pytest tests/ -v    # 93 tests, no GPU needed
```

---

## Demo

### Generate a test video (if you don't have one)

```bash
ffmpeg -y -f lavfi -i testsrc2=duration=5:size=1280x720:rate=30 \
  -c:v libx264 -pix_fmt yuv420p inputs/demo.mp4
```

### Demo 1 — Cyberpunk color grade

```bash
python3 -m gemia run \
  --video inputs/demo.mp4 \
  --prompt "赛博朋克风格，冷色调"
```

```
Asking AI for a plan...
Plan: Apply cyberpunk color grading (1 step)
  step_1: gemia.picture.color.color_grade({"preset": "cyberpunk"})

Executing...
Done!
```

The engine sees `gemia.picture.*` applied to a video and auto-wraps with per-frame processing.

### Demo 2 — Multi-step pipeline

```bash
python3 -m gemia run \
  --video inputs/demo.mp4 \
  --prompt "裁前3秒，加速2倍，调色vintage"
```

```
Plan: Trim + speed + vintage (3 steps)
  step_1: gemia.video.timeline.cut({"start_sec": 0.0, "end_sec": 3.0})
  step_2: gemia.video.timeline.speed({"factor": 2.0})
  step_3: gemia.picture.color.color_grade({"preset": "vintage"})

Executing...
Done!
```

5s input → 3s (trim) → 1.5s (2x speed) → vintage color graded.

### Demo 3 — Save & reuse as Skill

```bash
# Save last run
python3 -m gemia save-skill --name "裁切加速vintage"

# List skills
python3 -m gemia list-skills

# Apply to another video — no AI call
python3 -m gemia run-skill "裁切加速vintage" --video inputs/another.mp4
```

---

## CLI reference

```bash
# AI-driven execution
python3 -m gemia run --video FILE --prompt "..." [--output FILE]

# Skills
python3 -m gemia save-skill --name "NAME" [--from-task TASK_ID]
python3 -m gemia list-skills
python3 -m gemia run-skill "NAME" --video FILE [--output FILE]

# Web UI
python3 server.py    # http://127.0.0.1:8000
```

---

## Primitive modules

| Module | Count | Examples |
|--------|-------|---------|
| `gemia.picture.color` | 4 | `color_grade`, `adjust_exposure`, `adjust_temperature` |
| `gemia.picture.pixel` | 5 | `blur`, `sharpen`, `denoise`, `add_grain` |
| `gemia.picture.geometry` | 4 | `resize`, `crop`, `rotate` |
| `gemia.picture.composite` | 3 | `create_mask`, `blend`, `composite` |
| `gemia.picture.analysis` | 3 | `histogram`, `dominant_colors`, `edge_detect` |
| `gemia.audio.basics` | 5 | `load`, `save`, `trim`, `concat`, `mix` |
| `gemia.audio.dynamics` | 3 | `normalize`, `compress`, `adjust_gain` |
| `gemia.audio.frequency` | 3 | `eq`, `highpass`, `lowpass` |
| `gemia.audio.time_pitch` | 3 | `time_stretch`, `pitch_shift`, `detect_bpm` |
| `gemia.video.frames` | 3 | `extract_frames`, `frames_to_video`, `apply_picture_op_to_video` |
| `gemia.video.timeline` | 4 | `cut`, `concat`, `speed`, `reverse` |
| `gemia.video.compositing` | 2 | `overlay`, `add_audio_track` |
| `gemia.video.analysis` | 2 | `get_metadata`, `detect_scenes` |

---

## Architecture

```
User prompt
    │
    ▼
┌──────────────────────┐
│  Gemini (OpenRouter)  │  sees all 45 function docstrings
└──────────┬───────────┘
           │ Plan v2 JSON
           ▼
┌──────────────────────┐
│  PlanEngine           │  auto-bridges picture↔video
└──────────┬───────────┘
           │
           ▼
┌──────────────────────────────────────────────┐
│  gemia.picture    gemia.audio    gemia.video  │
│  (OpenCV/numpy)   (librosa)      (ffmpeg)     │
└──────────────────────────────────────────────┘
           │
           ▼
       Output file
```

---

## Roadmap

- **Skills UI** — visual skill browser in the web interface, drag-and-drop parameter editing
- **Nano Banana** — lightweight local model for simple operations, reduce API dependency
- **Veo integration** — AI-generated video clips as a primitive function
- **Desktop app** — standalone macOS / Windows app via pywebview + PyInstaller

---

## Environment

- Python 3.12+
- ffmpeg / ffprobe in PATH
- `OPENROUTER_API_KEY` (required for AI planning)
- `OPENROUTER_MODEL` (optional, default `google/gemini-2.5-flash`)

## License

MIT
