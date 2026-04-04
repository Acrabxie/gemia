# Task: Tauri DMG — Gemia Desktop App for macOS

## Goal
Package Gemia as a native macOS .dmg using Tauri, with Python backend as sidecar.

## Context
- Backend: existing Python `gemia` module at repo root
- Frontend: minimal Tauri/React UI (see UI spec below)
- Distribution: double-click .dmg → install Gemia.app → runs on macOS

## Prerequisites (install before starting)
```bash
# Rust toolchain
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh

# Tauri CLI
cargo install tauri-cli --version "^2"
# or: npm install -g @tauri-apps/cli@next

# Node.js + package manager
brew install node
```

## Architecture: Python Sidecar

Tauri sidecar = a bundled binary that Tauri spawns and communicates with via stdout/stdin or local HTTP.

Use local HTTP approach:
1. Python Flask/HTTP server starts when Tauri launches (sidecar binary)
2. Tauri frontend calls `http://127.0.0.1:7788/` for all operations
3. The Python server is `server.py` (already exists in the repo)

Python sidecar packaging:
- Use PyInstaller to package `server.py` + all dependencies into a single binary
- Binary goes into `src-tauri/binaries/`
- Tauri runs it as a sidecar

## Minimum UI Spec

4 components only:
1. **Video upload zone** — drag-and-drop or click to select .mp4
2. **AI chat input** — text field + submit button (Enter to send)
3. **Video preview** — `<video>` element showing the output
4. **Status bar** — text: "Ready" / "Planning..." / "Executing..." / "Done!"

State machine:
- idle → planning (show "Planning...") → executing (show "Executing...") → done (show output video)
- Error state: show error message in red

## First-run API Key Setup

On first launch:
- Check for `~/.gemia/config.json`
- If missing, show a modal dialog: "Enter your OpenRouter API Key" + "Enter your Gemini API Key (optional)"
- Store to `~/.gemia/config.json`: `{"openrouter_api_key": "...", "gemini_api_key": "..."}`
- Set env vars before starting the Python server

## API Endpoints (Python server, already exists at server.py)

Review `server.py` and ensure these endpoints work:
- `POST /run` — body: `{video_path, prompt}` → returns task_id
- `GET /task/{task_id}` — returns task status + output path
- `GET /skills` — list saved skills
- `POST /run-skill` — body: `{skill_name, video_path}`

If server.py is missing endpoints, add them.

## Tauri Project Structure

```
gemia-mvp/
  tauri/
    src-tauri/
      Cargo.toml
      tauri.conf.json
      src/
        main.rs
        lib.rs
    src/               # Frontend (React + Vite)
      App.tsx
      main.tsx
      index.html
    package.json
    vite.config.ts
```

## Build Steps

```bash
# 1. Package Python sidecar
cd /path/to/gemia-mvp
pip install pyinstaller
pyinstaller --onefile --name gemia-server server.py

# 2. Copy to Tauri binaries
mkdir -p tauri/src-tauri/binaries
cp dist/gemia-server "tauri/src-tauri/binaries/gemia-server-aarch64-apple-darwin"

# 3. Build Tauri app
cd tauri
npm install
npm run tauri build

# Output: tauri/src-tauri/target/release/bundle/dmg/Gemia_*.dmg
```

## tauri.conf.json Key Settings

```json
{
  "tauri": {
    "bundle": {
      "identifier": "ai.gemia.app",
      "icon": ["icons/icon.png"],
      "externalBin": ["binaries/gemia-server"]
    },
    "allowlist": {
      "http": {"all": true, "request": true},
      "shell": {"all": false, "sidecar": true}
    }
  }
}
```

## Estimated Steps for CC (Claude Code)

1. Review existing `server.py` — add missing API endpoints if needed
2. Create `tauri/` directory with Tauri v2 project scaffold
3. Write minimal React frontend (4 components, < 200 lines)
4. Configure `tauri.conf.json` for sidecar + DMG bundle
5. Write PyInstaller spec for `server.py`
6. Test: `npm run tauri dev` → verify UI works with live Python server
7. Build: `npm run tauri build` → produces Gemia.dmg
8. Test DMG: mount, install, run, verify API key dialog, verify video processing

## Notes
- Do not implement audio features in the UI for now
- The Python server already handles all business logic
- Keep the frontend under 300 lines total (HTML+CSS+JS or TSX)
- macOS only for now (aarch64-apple-darwin target)
