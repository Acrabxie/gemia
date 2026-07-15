# Lumeri

**Lumeri** is a family of AI creative tools built around a small vocabulary of
clean, composable primitives that a model can plan and execute.

**Lumeri Video** is the first product in the family. It is an agentic video
workspace where the model works over a persistent timeline using structured
tools while you watch, edit, and correct the result.

> The public product and GitHub repository name is **Lumeri**. The Python
> package and some engineering paths still use the historical name `gemia`.

## Product loop

```text
Import media
→ Persist project and timeline
→ Model calls media tools over multiple turns
→ TimelinePatch updates the project
→ Render and inspect a preview
→ Revise from structured feedback
→ Export MP4 or OTIO
```

## Architecture

| Layer | Responsibility |
|---|---|
| `server.py` | Local HTTP entry point |
| `gemia/v3_routes.py` | Session API and streaming |
| `gemia/agent_loop_v3.py` | Multi-turn model/tool loop |
| `gemia/tools/` | Media tools built on FFmpeg and Python |
| `gemia/project_model.py` | Persistent timeline model |
| `gemia/project_render.py` | Preview renderer |
| `gemia/project_export.py` | Full-quality export |
| `lumerai/patches.py` | Shared timeline patch vocabulary |
| `lumerai/otio_adapter.py` | OpenTimelineIO interchange |
| `static/v3/` | Local web interface |

## Install

Python 3.12+ and FFmpeg are required.

```bash
git clone https://github.com/Acrabxie/lumeri.git
cd lumeri
python -m pip install -e ".[dev]"

# macOS
brew install ffmpeg

# Ubuntu
sudo apt-get install ffmpeg
```

Configure a supported model provider through environment variables or the
local setup UI, then start Lumeri:

```bash
python server.py
# Open http://127.0.0.1:7788/v3/
```

The open-source build uses one local workspace. Hosted sign-in, email delivery,
cloud account management, billing, and subscriptions are not included in this
repository.

## Tests

```bash
python -m pytest tests/ -q
```

The suite covers tool contracts, timeline patches, render/export behavior,
OpenTimelineIO interchange, self-correction, sandboxing, sessions, and the web
server.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) and [SECURITY.md](SECURITY.md).

## Contributors

See [CONTRIBUTORS.md](CONTRIBUTORS.md).

## License

MIT
