# Lumeri

**Lumeri** is a family of AI creative tools that share one idea: give the model a small vocabulary of clean, composable primitives, and let it plan the work.

**Lumeri Video** is the first product in the family — a programmable engine where an AI plans and executes video, image, and audio operations by calling primitive Python functions.

> The public product and GitHub repository name is **Lumeri**. The Python package and some internal paths still use the historical engineering name `gemia`.

## The Lumeri family

| Product | What it does | Status |
|---|---|---|
| **Lumeri Video** | AI plans a pipeline of primitive video/image/audio ops and executes it | in this repo |
| **Lumeri Audio** | Music and sound-design workflows on the same primitive/plan/skill model | early |
| **Lumeri Image** | Still-image workflows — grading, retouching, style transfer as a plan | early |
| **Lumeri PPT** | Deck generation and edit as a plan of slide-level primitives | early |
| **Lumeri CAD** | Parametric CAD workflows driven by natural-language plans | exploration |

Each product ships as its own surface, but they share the same three ideas below.

---

## Core ideas

### 1. Primitive API

Around ninety pure Python functions across three domains — color grading, blur, keying, optical flow, audio repair, and more. Each function has a clear signature and docstring. The model reads those docstrings to know what's available.

```
gemia.picture.color.color_grade(image, preset="cyberpunk")
gemia.video.timeline.cut(input_path, output_path, start_sec=0, end_sec=3)
gemia.audio.frequency.eq(audio, bands={...})
```

Picture functions automatically work on video — the engine extracts frames, applies the operation per-frame, and re-encodes with the original audio. The model doesn't need to know this; it just picks the right function.

### 2. Skills

A Skill is a saved execution plan. Run a prompt, get a good result, save it as a Skill. Apply it to any video later — no AI call needed.

```
run → save-skill → run-skill → done
```

Skills are JSON templates with `$input` / `$output` variable binding. Concrete paths are stripped; the pipeline is portable.

Skills record which models were used and expose adjustable parameters:

```json
{
  "name": "赛博朋克调色",
  "parameters": [
    {"step_id": "step_1", "arg": "preset", "type": "str", "current_value": "cyberpunk"}
  ]
}
```

### 3. Orchestrator

Describe what you want in natural language. Lumeri sends your prompt plus the full function catalog to the planner. The planner returns a structured plan (not code, not ffmpeg commands). The engine executes it.

If the prompt is vague, the model asks clarifying questions first (Ask mechanism).

---

## Models

Lumeri Video runs on Google's models. Nothing else is wired into the default path.

| Role | Model |
|---|---|
| Planner | Google Gemini |
| Image generation / editing | Nano Banana (Gemini image) |
| Video generation | Veo |
| Music / sound generation | Lyria |

You point Lumeri at Google via one of two auth paths:

- **Gemini API key** — set `GEMINI_API_KEY` (from Google AI Studio).
- **Vertex AI** — set `VERTEX_PROJECT` (uses your local `gcloud` ADC).

That's the whole configuration surface. No third-party gateways, no key marketplaces.

---

## Install

```bash
git clone https://github.com/Acrabxie/lumeri.git && cd lumeri

# Python 3.12+, ffmpeg required
pip install -e .

# Point at Google
export GEMINI_API_KEY="..."
```

Verify:

```bash
python3 -m pytest tests/ -v    # ~258 tests, no GPU needed
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
Asking the planner for a plan...
Plan: Apply cyberpunk color grading (1 step)
  step_1: gemia.picture.color.color_grade({"preset": "cyberpunk"})

Executing...
Done!
```

The engine sees `gemia.picture.*` applied to a video and auto-wraps it with per-frame processing.

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

### Demo 3 — Save & reuse as Skill

```bash
python3 -m gemia save-skill --name "裁切加速vintage"
python3 -m gemia list-skills
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
| `gemia.picture.color` | 8 | `color_grade`, `lift_gamma_gain`, `apply_3d_lut`, `log_to_linear` |
| `gemia.picture.pixel` | 5 | `blur`, `sharpen`, `denoise`, `add_grain` |
| `gemia.picture.geometry` | 4 | `resize`, `crop`, `rotate` |
| `gemia.picture.composite` | 19 | `blend_multiply/screen/overlay…`, `chroma_key`, `luma_key`, `create_edge_mask` |
| `gemia.picture.analysis` | 7 | `histogram`, `waveform_monitor`, `vectorscope`, `check_clipping` |
| `gemia.audio.basics` | 5 | `load`, `save`, `trim`, `concat`, `mix` |
| `gemia.audio.dynamics` | 3 | `normalize`, `compress`, `adjust_gain` |
| `gemia.audio.frequency` | 3 | `eq`, `highpass`, `lowpass` |
| `gemia.audio.time_pitch` | 3 | `time_stretch`, `pitch_shift`, `detect_bpm` |
| `gemia.audio.repair` | 4 | `reduce_noise`, `remove_hum`, `de_ess`, `remove_reverb` |
| `gemia.audio.mixer` | 3 | `create_bus`, `sidechain_compress`, `auto_duck` |
| `gemia.video.frames` | 6 | `extract_frames`, `optical_flow_interpolate`, `retime`, `stabilize` |
| `gemia.video.timeline` | 4 | `cut`, `concat`, `speed`, `reverse` |
| `gemia.video.compositing` | 2 | `overlay`, `add_audio_track` |
| `gemia.video.analysis` | 4 | `get_metadata`, `detect_scenes`, `track_point`, `track_plane` |
| `gemia.video.keyframe` | 2 | `KeyframeTrack`, `apply_animated_op` |
| `gemia.picture.generative` | 4 | `generate_image`, `style_transfer`, `edit_image`, `blend_images` |
| `gemia.video.generative` | 3 | `generate_video`, `generate_video_from_image`, `extend_video` |

---

## Architecture

```
User prompt
    │
    ▼
┌──────────────────────┐
│  Planner (Gemini)     │  reads every primitive's docstring
└──────────┬───────────┘
           │ Plan JSON
           ▼
┌──────────────────────┐
│  PlanEngine           │  auto-bridges picture ↔ video
└──────────┬───────────┘
           │
           ▼
┌──────────────────────────────────────────────────────────┐
│  gemia.picture    gemia.audio    gemia.video              │
│  (OpenCV/numpy)   (librosa)      (ffmpeg)                 │
│  + Nano Banana    + Lyria        + Veo                    │
└──────────────────────────────────────────────────────────┘
           │
           ▼
       Output file
```

---

## Roadmap

- ✅ Generative primitives — image (Nano Banana), video (Veo), music (Lyria)
- ✅ Skills v2 — model tracking, parameterization
- Skills UI — visual skill browser in the web interface
- Desktop app — standalone macOS / Windows app
- Sibling products — Lumeri Audio / Image / PPT / CAD on the same core

---

## Environment

- Python 3.12+
- ffmpeg / ffprobe in PATH
- One of: `GEMINI_API_KEY` (Google AI Studio) or `VERTEX_PROJECT` (Vertex AI)

## Contributors

See [CONTRIBUTORS.md](CONTRIBUTORS.md).

## License

MIT
