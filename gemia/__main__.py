"""Lumeri Video CLI entry point.

Usage:
    python -m gemia serve [--host HOST] [--port PORT]
    python -m gemia inspect <project-dir>
"""
from __future__ import annotations

import argparse
import sys


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m gemia", description="Lumeri Video")
    sub = parser.add_subparsers(dest="command")

    # ── serve ─────────────────────────────────────────────────────────
    p_serve = sub.add_parser("serve", help="Start the Lumeri Video server")
    p_serve.add_argument("--host", default=None)
    p_serve.add_argument("--port", type=int, default=None)

    # ── inspect ───────────────────────────────────────────────────────
    p_inspect = sub.add_parser("inspect", help="Inspect a project directory")
    p_inspect.add_argument("project_dir", help="Path to project directory")

    args = parser.parse_args()

    if args.command == "serve" or args.command is None:
        from server import main as serve_main
        serve_main(
            host=getattr(args, "host", None),
            port=getattr(args, "port", None),
        )
    elif args.command == "inspect":
        from gemia.project_inspect import inspect_project, render_text
        result = inspect_project(args.project_dir)
        print(render_text(result))
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
