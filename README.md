<p align="center">
  <img src="docs/assets/lumeri-working.gif" width="180" alt="Lumeri working animation" />
</p>

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
# Open http://127.0.0.1:7788/
```

### Windows 10/11

Install 64-bit Python 3.12 or newer, Git, and a complete FFmpeg package whose
`ffmpeg` and `ffprobe` commands are on `PATH`. Then open PowerShell in the
cloned repository:

```powershell
.\scripts\windows\setup.ps1
.\scripts\windows\start.ps1
```

The start script runs the source checkout directly on
`http://127.0.0.1:7788/` and opens the browser workspace. It does not build or
install an EXE. Run `doctor.ps1` for a non-destructive prerequisite and port
check, or pass `-Port 7790` to both doctor/start when 7788 is already occupied.

PowerShell execution policy is left unchanged. If your machine blocks local
scripts, review the scripts first and invoke them for the current process only:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

The public build has no account system: no registration, login, account
switching, hosted email, billing, or subscriptions. It always opens one local
workspace stored on the current computer. Model-provider configuration remains
local to that computer and is never committed to Git.

Media editing, rendering, export, local voiceover, Windows fonts, Blender
discovery, and OpenTimelineIO bundles run natively on Windows. Arbitrary
model-generated `build`/`run_shell` code stays locked by default because native
Windows does not ship the macOS kernel sandbox used by Lumeri. The local owner
may explicitly disable **Sandbox** in the Lumeri menu to allow PowerShell and
Python execution with full computer access; Lumeri never enables that unsafe
mode automatically.

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
