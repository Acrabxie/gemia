# Lumeri Video

An agentic video project workspace. The AI model drives a multi-turn loop over your timeline using structured tools; you watch, edit, and correct in real time.

## Product Loop

```
Import media
→ Persist Project / Timeline
→ Model calls media tools (multi-turn)
→ Unified TimelinePatch modifies project
→ Render low-res preview
→ Analyze timing, visuals, audio
→ Self-correct from structured errors
→ Accept user feedback or direct timeline edits
→ Undo / fork
→ Export MP4 or OTIO
```

## Architecture

| Layer | What it does |
|---|---|
| `server.py` | Single HTTP entry point |
| `gemia/v3_routes.py` | Session API (`/sessions/*`) |
| `gemia/agent_loop_v3.py` | Multi-turn model ↔ tool loop |
| `gemia/tools/` | 18 media tools (FFmpeg, Gemini GenAI) |
| `gemia/budget_guard.py` | Cost and step limits |
| `gemia/errors.py` | Structured error + recovery protocol |
| `gemia/session_manager.py` | Session lifecycle |
| `gemia/session_telemetry.py` | Observability |
| `gemia/transport/sse.py` | Server-Sent Events |
| `gemia/project_model.py` | Timeline data model |
| `gemia/project_store.py` | Persistent project state |
| `gemia/project_render.py` | FFmpeg-based preview renderer |
| `gemia/project_export.py` | Full-quality export |
| `lumerai/patches.py` | TimelinePatch vocabulary (shared by AI and human) |
| `lumerai/otio_adapter.py` | OpenTimelineIO ↔ Lumeri round-trip |
| `lumerai/sandbox.py` | Sandboxed script execution |
| `static/v3/` | Web UI (timeline, chat, preview) |

## Install

```bash
git clone https://github.com/Acrabxie/lumeri.git && cd lumeri
pip install -e ".[dev]"

# FFmpeg is required
brew install ffmpeg   # macOS
# or: apt install ffmpeg  # Linux

# Configure a Google model provider
export GEMINI_API_KEY="..."       # from Google AI Studio
# or: export VERTEX_PROJECT="..."  # uses gcloud ADC
```

## Quick Start

```bash
# Start the server
python server.py
# Open http://127.0.0.1:7788 in your browser
```

## Run Tests

```bash
python -m pytest tests/ -v
```

Tests cover: tool protocol, timeline patches, direct edit, project render/export, OTIO interchange, self-correction, sandbox security, session/SSE, build verb, and verbs functional.

## Current Limitations

- Preview rendering requires FFmpeg on PATH.
- Generative tools (image/video/audio generation) require a valid Gemini API key or Vertex AI project.
- The web UI is functional but early-stage; no offline/PWA support.
- No GPU acceleration; all processing runs on CPU via FFmpeg.

## License

MIT
