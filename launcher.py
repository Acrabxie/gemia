"""Gemia desktop launcher.

Starts the local HTTP server in a background thread, then opens a native
webview window (via pywebview) so the app feels like a real desktop app
without requiring a browser. Falls back to the system browser if pywebview
is unavailable.

Usage:
  python launcher.py            # dev
  ./Gemia.app                   # after PyInstaller build
"""
from __future__ import annotations

import sys
import threading
import time
import webbrowser
from pathlib import Path

HOST = "127.0.0.1"
PORT = 8765  # distinct from dev port 8000 to avoid conflicts


def _start_server() -> None:
    # Ensure imports resolve when run from a PyInstaller bundle
    root = Path(__file__).resolve().parent
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

    from server import main as server_main  # noqa: PLC0415
    server_main(host=HOST, port=PORT)


def main() -> None:
    t = threading.Thread(target=_start_server, daemon=True)
    t.start()

    # Give the server a moment to bind
    time.sleep(1.2)

    url = f"http://{HOST}:{PORT}"

    try:
        import webview  # noqa: PLC0415
        webview.create_window(
            "Gemia",
            url,
            width=1440,
            height=900,
            resizable=True,
            min_size=(900, 600),
        )
        webview.start()
    except ImportError:
        # pywebview not available — open system browser
        webbrowser.open(url)
        # Keep the process alive so the server thread stays up
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
