#!/usr/bin/env python3
"""Entry point for the packaged Gemia server sidecar.

When running as a PyInstaller binary:
- Sets up data directories at ~/.gemia/workspace/
- Reads API keys from ~/.gemia/config.json
- Starts the HTTP server on port 7788

When running in development (not packaged):
- Uses the current directory as _BASE_DIR
- Still reads config from ~/.gemia/config.json
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def main() -> None:
    # ── Detect if running as a PyInstaller bundle ──────────────────────
    if getattr(sys, "frozen", False):
        # Running as bundled binary — use ~/.gemia/workspace/ as the working dir
        workspace = Path.home() / ".gemia" / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        # Create subdirectories expected by server.py
        for subdir in ("inputs", "outputs", "skills", "skills_v2", "tasks", "plans", "temp", "logs"):
            (workspace / subdir).mkdir(exist_ok=True)
        # Change working directory so server.py's relative paths work
        os.chdir(str(workspace))
        # Make the bundled gemia package importable
        if hasattr(sys, "_MEIPASS"):
            sys.path.insert(0, sys._MEIPASS)

    # ── Load API keys from config ──────────────────────────────────────
    config_path = Path.home() / ".gemia" / "config.json"
    if config_path.exists():
        try:
            config = json.loads(config_path.read_text())
            if key := config.get("openrouter_api_key"):
                os.environ.setdefault("OPENROUTER_API_KEY", key)
            if key := config.get("gemini_api_key"):
                os.environ.setdefault("GEMINI_API_KEY", key)
            if key := config.get("laozhang_api_key"):
                os.environ.setdefault("LAOZHANG_API_KEY", key)
        except Exception as e:
            print(f"Warning: could not read config: {e}", file=sys.stderr)

    # ── Start server on port 7788 ──────────────────────────────────────
    # Import server module — works both in dev and bundled
    import importlib.util

    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        server_path = Path(sys._MEIPASS) / "server.py"
        spec = importlib.util.spec_from_file_location("server", server_path)
        server = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(server)
    else:
        import server  # type: ignore

    server.main(host="127.0.0.1", port=7788)


if __name__ == "__main__":
    main()
