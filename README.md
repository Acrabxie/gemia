<p align="center">
  <a href="https://lumeri.io/">
    <img src="docs/assets/lumeri-working.gif" width="240" alt="Lumeri working animation" />
  </a>
</p>

<h1 align="center">Lumeri</h1>

<p align="center">
  <strong>the LUI for creation</strong>
</p>

<p align="center">
  An AI Creative Workflow Engine for turning an idea into a real, editable media project.
</p>

<p align="center">
  <a href="https://lumeri.io/"><strong>Official website</strong></a>
  ·
  <a href="#product-loop">Product loop</a>
  ·
  <a href="#install">Install</a>
  ·
  <a href="#architecture">Architecture</a>
  ·
  <a href="CONTRIBUTING.md">Contribute</a>
</p>

<p align="center">
  <a href="https://lumeri.io/">
    <img src="https://img.shields.io/badge/website-lumeri.io-5FC6DE?style=for-the-badge" alt="Lumeri official website" />
  </a>
  <img src="https://img.shields.io/badge/Python-3.12%2B-1F2937?style=for-the-badge&amp;logo=python&amp;logoColor=white" alt="Python 3.12 or newer" />
  <a href="LICENSE">
    <img src="https://img.shields.io/badge/license-MIT-1F2937?style=for-the-badge" alt="MIT license" />
  </a>
</p>

**Lumeri** is a family of AI creative tools built around a small vocabulary of
clean, composable primitives that a model can plan and execute.

**Lumeri Video** is the first product in the family. It is an agentic video
workspace where the model works over a persistent timeline using structured
tools while you watch, edit, and correct the result.

More than a prompt box, Lumeri keeps the creative process inspectable: projects
persist, tool calls are structured, timeline changes remain editable, and every
preview can become the starting point for the next revision.

> The public product and GitHub repository name is **Lumeri**. The Python
> package and some engineering paths still use the historical name `gemia`.

## Why Lumeri

- **Project-native** — work lives in a persistent project and timeline, not a
  disposable chat response.
- **Structured by design** — the model plans with explicit media tools and
  applies reviewable timeline patches instead of emitting opaque editor macros.
- **Built for iteration** — preview, inspect, revise, and export from the same
  creative loop.
- **Open foundation** — the public engine, media tools, project model, and local
  workspace are available here under the MIT license.

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
switching, hosted email, or Lumeri billing. It always opens one local workspace
stored on the current computer. Model-provider configuration remains local to
that computer and is never committed to Git.

To use an existing ChatGPT plan instead of an API key, choose **OpenAI 订阅
（本机 Codex）** in `/setup`. This option invokes the Codex CLI on the same
computer and only accepts a local **Sign in with ChatGPT** session. Lumeri never
reads, copies, or stores Codex login credentials, and every person must sign in
with their own OpenAI account.

On Windows, install and authenticate the Codex CLI before selecting that option:

```powershell
winget install OpenJS.NodeJS.LTS
npm install -g @openai/codex
codex login
codex login status
```

If Node.js was just installed, reopen PowerShell before running `npm`. The
provider setup panel can open the `codex login` window and refresh the local
status. API-key authentication shown by `codex login status` does not count as
subscription access; complete **Sign in with ChatGPT** instead.

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
