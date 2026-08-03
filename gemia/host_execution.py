"""Cross-platform host process helpers for local Lumeri tools.

The macOS build uses ``sandbox-exec`` for untrusted commands.  Windows does
not provide an equivalent primitive that Lumeri can safely assume is present,
so command/code execution stays fail-closed while the sandbox is enabled.  If
the owner explicitly disables the sandbox in the local UI, these helpers make
the raw execution path work with PowerShell and native Windows interpreters.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Sequence


IS_WINDOWS = os.name == "nt"


def minimal_subprocess_env() -> dict[str, str]:
    """Return a small, usable environment without provider credentials."""

    common = ("PATH", "LANG", "LC_ALL")
    windows = ("PATHEXT", "SystemRoot", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP")
    posix = ("HOME", "TMPDIR")
    allowed = (*common, *(windows if IS_WINDOWS else posix))
    env = {key: os.environ[key] for key in allowed if key in os.environ}

    if "PATH" not in env:
        env["PATH"] = os.defpath
    home = str(Path.home())
    env.setdefault("HOME", home)
    if IS_WINDOWS:
        env.setdefault("USERPROFILE", home)
        system_root = os.environ.get("SystemRoot") or os.environ.get("SYSTEMROOT")
        if system_root:
            env.setdefault("SystemRoot", system_root)
            env.setdefault("SYSTEMROOT", system_root)
            env.setdefault("WINDIR", system_root)
    return env


def shell_command(command: str) -> tuple[list[str], str]:
    """Return the native host shell argv and a user-facing shell name."""

    if IS_WINDOWS:
        executable = shutil.which("pwsh") or shutil.which("powershell") or shutil.which("powershell.exe")
        if not executable:
            raise EnvironmentError("PowerShell was not found on PATH")
        return [executable, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command], "powershell"

    executable = shutil.which("bash") or "/bin/bash"
    if not Path(executable).exists():
        raise EnvironmentError("bash was not found on PATH")
    return [executable, "-c", command], "bash"


def interpreter_command(
    language: str,
    script_path: Path,
    script_args: Sequence[str] = (),
) -> tuple[list[str], str]:
    """Resolve one build language to native argv without POSIX assumptions."""

    normalized = language.strip().lower()
    if normalized in {"python", "python3"}:
        return [sys.executable, str(script_path), *script_args], "python"
    if normalized in {"powershell", "pwsh", "ps1"}:
        if not IS_WINDOWS:
            executable = shutil.which("pwsh")
        else:
            executable = shutil.which("pwsh") or shutil.which("powershell") or shutil.which("powershell.exe")
        if not executable:
            raise EnvironmentError("PowerShell was not found on PATH")
        return [executable, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(script_path), *script_args], "powershell"
    if normalized in {"bash", "shell", "sh"}:
        if IS_WINDOWS:
            raise EnvironmentError("Bash builds are unavailable on native Windows; use language='powershell'")
        executable = shutil.which("bash") or "/bin/bash"
        if not Path(executable).exists():
            raise EnvironmentError("bash was not found on PATH")
        return [executable, str(script_path), *script_args], "bash"

    binary_name = {
        "node": "node",
        "go": "go",
        "ruby": "ruby",
        "rust": "rustc",
    }.get(normalized)
    if not binary_name:
        raise ValueError(f"Unsupported language '{language}'")
    executable = shutil.which(binary_name)
    if not executable:
        raise EnvironmentError(f"{binary_name} was not found on PATH")
    prefix = [executable, "run"] if normalized == "go" else [executable]
    return [*prefix, str(script_path), *script_args], normalized


def process_group_kwargs() -> dict[str, Any]:
    """Return platform-native isolated process-group creation flags."""

    if IS_WINDOWS:
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def terminate_process_tree(proc: subprocess.Popen[Any]) -> None:
    """Best-effort termination of a build and any children it spawned."""

    if proc.poll() is not None:
        return
    if IS_WINDOWS:
        taskkill = shutil.which("taskkill") or shutil.which("taskkill.exe")
        if taskkill:
            subprocess.run(
                [taskkill, "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True,
                timeout=8,
                check=False,
                env=minimal_subprocess_env(),
            )
        if proc.poll() is None:
            proc.kill()
        return

    try:
        os.killpg(os.getpgid(proc.pid), 9)
    except OSError:
        proc.kill()


def sandbox_unavailable_message() -> str:
    if IS_WINDOWS:
        return (
            "secure command sandbox unavailable on native Windows; refusing to run code. "
            "The computer owner can explicitly disable Sandbox in the local Lumeri menu "
            "to allow unsandboxed PowerShell execution."
        )
    return "sandbox-exec unavailable or failed on this host; refusing to run code without sandbox enforcement"


__all__ = [
    "IS_WINDOWS",
    "interpreter_command",
    "minimal_subprocess_env",
    "process_group_kwargs",
    "sandbox_unavailable_message",
    "shell_command",
    "terminate_process_tree",
]
