from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

from gemia import host_execution, sandbox_v4
from gemia.audio.effects import text_to_speech
from gemia.tools import build, run_shell
from gemia.tools._context import AssetRegistry, ToolContext
from gemia.video import blender_link


def _ctx(root: Path) -> ToolContext:
    root.mkdir(parents=True, exist_ok=True)
    return ToolContext(
        session_id="host_execution",
        output_dir=root,
        registry=AssetRegistry(),
        emit_progress=lambda _update: None,
    )


@pytest.fixture
def raw_execution(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(sandbox_v4, "_SANDBOX_DISABLED", True)
    yield
    for proc, _deadline in list(build._PROCESSES.values()):
        host_execution.terminate_process_tree(proc)
    build._PROCESSES.clear()


def test_minimal_environment_excludes_provider_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-leak")
    monkeypatch.setenv("GEMINI_API_KEY", "must-not-leak")

    env = host_execution.minimal_subprocess_env()

    assert env["PATH"]
    assert env["HOME"] == str(Path.home())
    assert "OPENAI_API_KEY" not in env
    assert "GEMINI_API_KEY" not in env


def test_python_interpreter_uses_current_runtime(tmp_path: Path) -> None:
    command, runtime = host_execution.interpreter_command("python3", tmp_path / "script.py")

    assert command[0] == sys.executable
    assert runtime == "python"


def test_raw_shell_executes_on_current_host(tmp_path: Path, raw_execution) -> None:
    command = 'Write-Output "windows-shell-ok"' if os.name == "nt" else "printf 'posix-shell-ok\\n'"

    result = asyncio.run(run_shell.dispatch({"command": command}, _ctx(tmp_path)))

    expected = "windows-shell-ok" if os.name == "nt" else "posix-shell-ok"
    assert result["exit_code"] == 0, result
    assert expected in result["stdout_tail"]
    assert result["sandbox_enforced"] is False
    assert result["shell"] == ("powershell" if os.name == "nt" else "bash")


def test_raw_python_build_executes_and_finishes(tmp_path: Path, raw_execution) -> None:
    ctx = _ctx(tmp_path)
    submitted = asyncio.run(
        build.dispatch(
            {"code": 'print("cross-platform-build-ok")', "timeout_sec": 10},
            ctx,
        )
    )

    result = asyncio.run(
        build.dispatch_wait({"job_id": submitted["job_id"], "max_wait_sec": 20}, ctx)
    )

    assert submitted["runtime"] == "python"
    assert submitted["sandbox_enforced"] is False
    assert result["status"] == "done", result
    assert "cross-platform-build-ok" in result["stdout_tail"]


def test_blender_discovers_windows_program_files(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    executable = tmp_path / "Blender Foundation" / "Blender 4.5" / "blender.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"")
    monkeypatch.setenv("ProgramFiles", str(tmp_path))
    monkeypatch.delenv("LUMERI_BLENDER_PATH", raising=False)
    monkeypatch.delenv("GEMIA_BLENDER_PATH", raising=False)
    monkeypatch.setattr(blender_link.shutil, "which", lambda _name: None)

    assert blender_link._find_blender() == str(executable)


@pytest.mark.skipif(os.name != "nt", reason="Windows SAPI acceptance")
def test_windows_sapi_creates_wave_file(tmp_path: Path) -> None:
    output = tmp_path / "speech.wav"

    text_to_speech("Lumeri Windows voice check", str(output))

    assert output.read_bytes()[:4] == b"RIFF"
    assert output.stat().st_size > 44


@pytest.mark.skipif(os.name != "nt", reason="Windows font acceptance")
def test_windows_lumenframe_resolves_scalable_system_font() -> None:
    from lumenframe.resolve import _resolve_font

    _font, source, scalable = _resolve_font(None, 48)

    assert scalable is True
    assert "Windows/Fonts" in source.replace("\\", "/")
