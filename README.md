# Lumeri

**AI creative workflow engine** — not an editor, not a plugin. A programmable layer where AI plans and executes video/image/audio operations through composable primitive functions.

Codex brought AI into the programmer's workflow. Lumeri does the same for creative work.

The public product and GitHub repository name is **Lumeri**. The Python package and some internal paths still use the historical engineering name `gemia`.

---

## Core ideas

### 1. Primitive API

88+ pure Python functions across three domains — color grading, blur, keying, optical flow, audio repair, and more. Each function has a clear signature and docstring. The AI reads these docstrings to understand what's available.

```
gemia.picture.color.color_grade(image, preset="cyberpunk")
gemia.video.timeline.cut(input_path, output_path, start_sec=0, end_sec=3)
gemia.audio.frequency.eq(audio, bands={...})
```

Picture functions automatically work on video — the engine extracts frames, applies the operation per-frame, and re-encodes with original audio. The AI doesn't need to know this; it just picks the right function.

## GPT Image 2 — AI Image Generation

Lumeri integrates GPT Image 2 through the Sisyphus/OpenAI-compatible image API as native primitives. The AI planner can call these the same way it calls any other primitive.

| Function | Description |
|----------|-------------|
| `generate_image(prompt, aspect_ratio, style)` | Text → image |
| `edit_image(image, instruction)` | Edit a frame with natural language |
| `style_transfer(image, style_prompt)` | Apply a visual style to each frame |
| `blend_images(image_a, img_b_path, prompt)` | Blend two images with AI guidance |

When applied to a video, `style_transfer` and `edit_image` are automatically applied per-frame (same auto-bridge as other picture primitives).

Requires `SISYPHUS_API_KEY` or `~/.gemia/config.json` field `sisyphus_api_key`.

### Example

```bash
python3 -m gemia run --video input.mp4 --prompt "把每一帧做成赛博朋克风格"
```

---

## Veo 3.1 — AI Video Generation

Generate and extend video clips with Veo 3.1 via laozhang.ai.

| Function | Description |
|----------|-------------|
| `generate_video(prompt, duration, aspect_ratio)` | Text → video |
| `generate_video_from_image(image_path, prompt, duration)` | Image → video |
| `extend_video(video_path, prompt, duration)` | Extend video end |

Requires `LAOZHANG_API_KEY`.

### Example

```bash
python3 -m gemia run --video input.mp4 --prompt "给结尾生成3秒延伸镜头"
```

---

### 2. Skills

A Skill is a saved execution plan. Run a prompt, get a good result, save it as a Skill. Apply it to any video later — no AI call needed.

```
run → save-skill → run-skill → done
```

Skills are JSON templates with `$input` / `$output` variable binding. Concrete paths are stripped; the pipeline is portable.

### Parameterized Skills

Skills now record which models were used and expose adjustable parameters:

```json
{
  "name": "赛博朋克调色",
  "models_used": ["opencv", "gpt_image2_flash"],
  "parameters": [
    {"step_id": "step_1", "arg": "preset", "type": "str", "current_value": "cyberpunk"}
  ]
}
```

### 3. Orchestrator

Describe what you want in natural language. Lumeri sends your prompt + the full function catalog to Gemini. The AI returns a structured plan (not code, not ffmpeg commands). The engine executes it.

If the prompt is vague, the AI asks clarifying questions first (Ask mechanism).

---

## Install

```bash
git clone https://github.com/Acrabxie/lumeri.git && cd lumeri

# Python 3.12+, ffmpeg required
pip install -e .

# API key (Gemini via OpenRouter)
export OPENROUTER_API_KEY="sk-or-..."
```

Verify:

```bash
python3 -m pytest tests/ -v    # 258 tests, no GPU needed
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
│  Gemini (OpenRouter)  │  sees all 88+ function docstrings
└──────────┬───────────┘
           │ Plan v2 JSON
           ▼
┌──────────────────────┐
│  PlanEngine           │  auto-bridges picture↔video
└──────────┬───────────┘
           │
           ▼
┌──────────────────────────────────────────────────────────┐
│  gemia.picture    gemia.audio    gemia.video              │
│  (OpenCV/numpy)   (librosa)      (ffmpeg)                 │
│  + GPT Image 2    —              + Veo 3.1                │
│  (Sisyphus API)                  (laozhang.ai)            │
└──────────────────────────────────────────────────────────┘
           │
           ▼
       Output file
```

---

## Roadmap

- ✅ **GPT Image 2** — image generation/editing integrated as primitives through Sisyphus
- ✅ **Veo integration** — AI-generated video clips via laozhang.ai (Veo 3.1)
- ✅ **Skills v2** — model tracking, parameterization, `parameters` field
- **Skills UI** — visual skill browser in the web interface
- **Desktop app** — standalone macOS / Windows app via Tauri

---

## Environment

- Python 3.12+
- ffmpeg / ffprobe in PATH
- `OPENROUTER_API_KEY` (required for AI planning)
- `SISYPHUS_API_KEY` (required for GPT Image 2 image generation/editing)
- `LAOZHANG_API_KEY` (required for Veo video generation)
- `OPENROUTER_MODEL` (optional, default `google/gemini-2.5-flash`)

## Contributors

See [CONTRIBUTORS.md](CONTRIBUTORS.md).

- Claude
- ChatGPT
- Acrabxie

## License

MIT
