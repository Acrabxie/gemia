# Lumeri Agent Instructions

## Repository Structure

This is **Lumeri Video** — an agentic video project workspace.

### Core Architecture (v3)

- `server.py` — HTTP server entry point, serves web UI and delegates to v3 routes
- `gemia/v3_routes.py` — Session HTTP API (`/sessions/*`)
- `gemia/agent_loop_v3.py` — Multi-turn model ↔ tool conversation loop
- `gemia/tools/` — 18 media tools using FFmpeg directly and Google GenAI
- `gemia/gemini_client.py` — Gemini API client for the agent loop
- `gemia/ai/google_genai_client.py` — Google GenAI client for generative tools
- `gemia/budget_guard.py` — Step and cost budget enforcement
- `gemia/errors.py` — Structured errors with recovery protocol
- `gemia/session_manager.py` — Session lifecycle and concurrency
- `gemia/session_telemetry.py` — Telemetry and observability
- `gemia/transport/sse.py` — Server-Sent Events for real-time UI updates

### Project Model

- `gemia/project_model.py` — Timeline data model (tracks, clips, effects)
- `gemia/project_store.py` — Persistent project state (JSON files)
- `gemia/project_render.py` — FFmpeg-based preview rendering
- `gemia/project_export.py` — Full-quality export (MP4, OTIO)
- `gemia/project_inspect.py` — Project introspection utilities

### Patch System

- `lumerai/patches.py` — TimelinePatch vocabulary (shared by AI and human editing)
- `lumerai/otio_adapter.py` — OpenTimelineIO ↔ Lumeri project round-trip
- `lumerai/sandbox.py` — Sandboxed script execution

### Frontend

- `static/v3/` — Web UI (HTML/CSS/JS) with multi-track timeline, chat, preview

## Rules

1. All timeline modifications — whether from the AI agent or human direct editing — MUST go through the TimelinePatch vocabulary in `lumerai/patches.py`.
2. The v3 tool protocol is the only agent path. Do not add alternative agent loops.
3. Media processing uses FFmpeg via `gemia/tools/_ffmpeg.py`. Do not add alternative media backends.
4. Do not store secrets, API keys, or credentials in code or config files in the repo.
5. Run `python -m pytest tests/ -v` before committing changes.
