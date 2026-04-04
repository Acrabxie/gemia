#!/usr/bin/env python3
"""Entry point for the packaged Gemia server sidecar.

When running as a PyInstaller binary:
- Sets up data directories at ~/.gemia/workspace/
- Reads API keys from ~/.gemia/config.json
- Starts the HTTP server on port 7788
- If port 7788 is already in use, exits cleanly (existing server handles it)

When running in development (not packaged):
- Uses the current directory as _BASE_DIR
- Still reads config from ~/.gemia/config.json
"""
from __future__ import annotations

import errno
import json
import os
import socket
import sys
from pathlib import Path


def _port_in_use(port: int) -> bool:
    """Return True if TCP port is already bound."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
            return False
        except OSError:
            return True


def main() -> None:
    port = 7788

    # ── Detect if running as a PyInstaller bundle ──────────────────────
    if getattr(sys, "frozen", False):
        workspace = Path.home() / ".gemia" / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        for subdir in ("inputs", "outputs", "skills", "skills_v2",
                       "tasks", "plans", "temp", "logs"):
            (workspace / subdir).mkdir(exist_ok=True)
        os.chdir(str(workspace))
        if hasattr(sys, "_MEIPASS"):
            sys.path.insert(0, sys._MEIPASS)

    # ── If port already in use, an existing server is handling things ──
    if _port_in_use(port):
        print(
            f"Port {port} already in use — existing server will handle requests.",
            file=sys.stderr,
        )
        # Exit cleanly; Tauri's waitForServer will connect to the existing one.
        sys.exit(0)

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

    # ── Import and patch server module paths ───────────────────────────
    import importlib.util

    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        server_path = Path(sys._MEIPASS) / "server.py"
        spec = importlib.util.spec_from_file_location("server", server_path)
        server = importlib.util.module_from_spec(spec)  # type: ignore[assignment]
        spec.loader.exec_module(server)  # type: ignore[union-attr]

        # Patch all module-level paths to point at the workspace, not _MEIPASS.
        # server.py uses Path(__file__).resolve().parent for _BASE_DIR,
        # which in the frozen binary resolves to the temp extraction dir.
        workspace_path = Path.home() / ".gemia" / "workspace"
        server._BASE_DIR = workspace_path
        server._SKILLS_DIR = workspace_path / "skills"
        server._STATIC_DIR = workspace_path / "static"
        server._INPUTS_DIR = workspace_path / "inputs"
        server._TASKS_DIR = workspace_path / "tasks"
        server._PLANS_DIR = workspace_path / "plans"
    else:
        import server  # type: ignore

    # ── Start server ───────────────────────────────────────────────────
    try:
        server.main(host="127.0.0.1", port=port)
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE:
            print(
                f"Port {port} taken by another process — exiting cleanly.",
                file=sys.stderr,
            )
            sys.exit(0)
        raise


if __name__ == "__main__":
    main()
