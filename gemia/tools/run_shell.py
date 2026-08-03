"""run_shell — execute a host-shell command in an isolated sandbox.

The workspace directory is fully writable. Outside the workspace, files can
only be created, not modified/deleted. Credentials (~/.ssh, ~/.config/gcloud,
~/.gemia/config.json) are not readable. Network access is denied.

Wraps the command with sandbox-exec and build_v4_sandbox_command() from the
M1 isolation layer (gemia/sandbox_v4.py). Enforces sandbox_enforced=True or
raises RuntimeError.

Dispatcher signature: async def dispatch(args: dict, ctx: ToolContext) -> dict.
Returns {exit_code, stdout_tail, stderr_tail, timed_out, sandbox_enforced, workspace_dir}.
"""
from __future__ import annotations

import asyncio
import subprocess
from typing import Any

from gemia.host_execution import (
    minimal_subprocess_env,
    sandbox_unavailable_message,
    shell_command,
)
from gemia.sandbox_v4 import (
    build_v4_sandbox_command,
    is_sandbox_disabled,
    native_sandbox_available,
)
from gemia.tools._context import ToolContext


def _minimal_env() -> dict[str, str]:
    """Backward-compatible alias used by older tests and callers."""

    return minimal_subprocess_env()


async def dispatch(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Run a native host-shell command in an isolated sandbox.

    Args:
        command: required, bash command string
        timeout_sec: optional, timeout in seconds (default 30, max 120)

    Returns:
        {
            exit_code: int (124 if timed out),
            stdout_tail: str (last ~4000 chars),
            stderr_tail: str (last ~4000 chars),
            timed_out: bool,
            sandbox_enforced: bool (always True or else RuntimeError),
            workspace_dir: str (absolute path),
        }
    """
    command = str(args.get("command") or "").strip()
    if not command:
        raise ValueError("run_shell requires a non-empty 'command' argument")

    timeout_sec = args.get("timeout_sec", 30)
    try:
        timeout_sec = float(timeout_sec)
    except (TypeError, ValueError):
        raise ValueError(f"timeout_sec must be a number, got {timeout_sec!r}") from None

    if timeout_sec <= 0 or timeout_sec > 120:
        raise ValueError(f"timeout_sec must be in (0, 120], got {timeout_sec}")

    # Build sandbox command using M1 isolation layer.
    # When the user has explicitly disabled the sandbox (POST /settings/sandbox),
    # run the raw command with full system access and report enforced=False
    # honestly — do NOT raise.
    sandbox_disabled = is_sandbox_disabled()
    if not sandbox_disabled and not native_sandbox_available():
        raise RuntimeError(sandbox_unavailable_message())
    raw_cmd, shell_name = shell_command(command)
    if sandbox_disabled:
        cmd = raw_cmd
        enforced = False
    else:
        cmd, enforced = build_v4_sandbox_command(
            raw_cmd,
            workspace_dir=ctx.output_dir,
        )
        if not enforced:
            raise RuntimeError(sandbox_unavailable_message())

    # Run in subprocess with minimal environment
    exit_code = None
    stdout_data = ""
    stderr_data = ""
    timed_out = False

    try:
        result = await asyncio.to_thread(
            subprocess.run,
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            cwd=str(ctx.output_dir),
            env=_minimal_env(),
        )
        exit_code = result.returncode
        stdout_data = result.stdout or ""
        stderr_data = result.stderr or ""
    except subprocess.TimeoutExpired as exc:
        exit_code = 124  # Standard timeout exit code
        timed_out = True
        stdout_data = exc.stdout or ""
        stderr_data = exc.stderr or ""

    # Tail last ~4000 chars
    tail_size = 4000
    stdout_tail = stdout_data[-tail_size:] if len(stdout_data) > tail_size else stdout_data
    stderr_tail = stderr_data[-tail_size:] if len(stderr_data) > tail_size else stderr_data

    return {
        "exit_code": exit_code,
        "stdout_tail": stdout_tail,
        "stderr_tail": stderr_tail,
        "timed_out": timed_out,
        "sandbox_enforced": enforced,
        "shell": shell_name,
        "workspace_dir": str(ctx.output_dir),
    }


__all__ = ["dispatch"]
