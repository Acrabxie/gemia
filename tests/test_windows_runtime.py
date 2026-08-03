from __future__ import annotations

from pathlib import Path, PureWindowsPath

import gemia.runtime_paths as runtime_paths
import server
from gemia.video import fonts


def test_server_defaults_to_loopback() -> None:
    assert server._configured_server_host() == "127.0.0.1"


def test_output_root_uses_platform_temp_directory(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("LUMERI_V3_OUTPUT_ROOT", raising=False)
    monkeypatch.setattr(runtime_paths.tempfile, "gettempdir", lambda: str(tmp_path))

    assert runtime_paths.output_root() == tmp_path / "lumeri-v3"


def test_output_root_accepts_windows_style_override(monkeypatch) -> None:
    monkeypatch.setenv("LUMERI_V3_OUTPUT_ROOT", r"C:\\LumeriData\\runtime")

    assert PureWindowsPath(str(runtime_paths.output_root())) == PureWindowsPath(
        r"C:\\LumeriData\\runtime"
    )


def test_windows_font_directory_is_in_system_roots(monkeypatch) -> None:
    monkeypatch.setenv("WINDIR", r"C:\\Windows")

    roots = fonts.font_roots(include_system=True)

    assert PureWindowsPath(str(roots[1])) == PureWindowsPath(r"C:\\Windows\\Fonts")
