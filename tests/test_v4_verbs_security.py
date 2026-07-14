"""M3 INTEGRATED security probe (rerunnable) — owned by the security gate.

M1 (`test_sandbox_v4_isolation.py`) proves the *profile* holds. This file proves
the boundary still holds once the real verbs are wired on top of it: it drives the
actual ``run_shell`` / ``fetch`` dispatchers (not an ad-hoc profile) and asserts
the worst-case injection paths are sealed.

Acrab's required probe matrix (must show DENIED, not a verbal "it's wired"):
  * run_shell `cat ~/.gemia/config.json`            → DENIED, secret never in stdout
  * run_shell `cat ~/.config/gcloud/...`            → DENIED
  * run_shell network connect (curl external)       → DENIED (exfil only on host fetch)
  * run_shell overwrite / delete out-of-zone file   → DENIED, original byte-intact
  * run_shell create new file under ~/.ssh          → DENIED (validates M1 half-step)
  * run_shell workspace write (control)             → OK (didn't break legit use)
  * web_search / web_open return compact text only  → no raw HTML reaches SSE
  * fetch return payload never carries raw bytes    → only path/metadata reach SSE
"""
from __future__ import annotations

import asyncio
import json
import shutil
import sys
import uuid
from pathlib import Path

import pytest

def _sandbox_exec_usable(path: str) -> bool:
    import subprocess
    try:
        r = subprocess.run([path, "-p", "(version 1)(allow default)"], capture_output=True, timeout=3)
        return r.returncode == 0
    except Exception:
        return False
from gemia.tools import fetch as fetch_mod
from gemia.tools import run_shell as run_shell_mod
from gemia.tools import web_search as web_search_mod
from gemia.tools._context import AssetRegistry, ToolContext

_SANDBOX_EXEC = shutil.which("sandbox-exec")
_USABLE = bool(_SANDBOX_EXEC) and sys.platform == "darwin" and _sandbox_exec_usable(_SANDBOX_EXEC or "")
_needs_sandbox = pytest.mark.skipif(not _USABLE, reason="sandbox-exec not usable on this host")


@pytest.fixture(autouse=True)
def _force_sandbox_enabled():
    """Security tests always need the real sandbox, regardless of POST /settings/sandbox toggle."""
    from gemia.sandbox_v4 import is_sandbox_disabled, set_sandbox_disabled
    was_disabled = is_sandbox_disabled()
    set_sandbox_disabled(False)
    yield
    set_sandbox_disabled(was_disabled)


def _ctx(workspace: Path) -> ToolContext:
    workspace.mkdir(parents=True, exist_ok=True)
    return ToolContext(
        session_id="sec_probe",
        output_dir=workspace,
        registry=AssetRegistry(),
        emit_progress=lambda _u: None,
    )


def _run_shell(workspace: Path, command: str, *, timeout_sec: float = 20) -> dict:
    return asyncio.run(
        run_shell_mod.dispatch({"command": command, "timeout_sec": timeout_sec}, _ctx(workspace))
    )


# --------------------------------------------------------------------------- #
# credential reads via the real run_shell verb                                #
# --------------------------------------------------------------------------- #

@_needs_sandbox
def test_run_shell_cat_gemia_config_denied(tmp_path: Path) -> None:
    if not (Path.home() / ".gemia" / "config.json").exists():
        pytest.skip("~/.gemia/config.json not present")
    r = _run_shell(tmp_path / "ws", "cat ~/.gemia/config.json")
    assert r["exit_code"] != 0, r
    assert r["stdout_tail"].strip() == "", f"secret leaked to stdout: {r['stdout_tail']!r}"
    # any real key material would contain these substrings; none must appear
    for marker in ("api_key", "refresh_token", "proxy", "openrouter", "vertex_project"):
        assert marker not in r["stdout_tail"].lower(), f"leaked {marker}"


@_needs_sandbox
def test_run_shell_cat_gcloud_adc_denied(tmp_path: Path) -> None:
    gcloud = Path.home() / ".config" / "gcloud"
    if not gcloud.exists():
        pytest.skip("~/.config/gcloud not present")
    # both a directory listing and reading the ADC file must be denied
    r = _run_shell(tmp_path / "ws", "ls ~/.config/gcloud && cat ~/.config/gcloud/application_default_credentials.json")
    assert r["exit_code"] != 0, r
    assert "refresh_token" not in r["stdout_tail"].lower(), "ADC leaked"


# --------------------------------------------------------------------------- #
# network egress via the real run_shell verb (must be sealed; fetch is host)   #
# --------------------------------------------------------------------------- #

@_needs_sandbox
def test_run_shell_network_denied(tmp_path: Path) -> None:
    # curl to a raw IP (no DNS dependency); deny network* must kill the connect.
    r = _run_shell(tmp_path / "ws", "curl -sS --max-time 6 https://1.1.1.1/ ; echo EXIT=$?")
    assert r["exit_code"] != 0 or "EXIT=0" not in r["stdout_tail"], r
    # belt-and-suspenders: python socket connect from inside the sandbox
    r2 = _run_shell(
        tmp_path / "ws2",
        "python3 -c \"import socket; socket.create_connection(('1.1.1.1',443),3)\"",
    )
    assert r2["exit_code"] != 0, f"sandbox opened a socket: {r2}"


# --------------------------------------------------------------------------- #
# out-of-zone existing files immutable via the real run_shell verb             #
# --------------------------------------------------------------------------- #

@_needs_sandbox
def test_run_shell_cannot_overwrite_or_delete_outside_existing(tmp_path: Path) -> None:
    outside = Path("/private/tmp") / f"v4sec_{uuid.uuid4().hex[:8]}"
    outside.mkdir(parents=True)
    victim = outside / "existing.txt"
    victim.write_text("ORIGINAL", encoding="utf-8")
    try:
        ws = tmp_path / "ws"
        over = _run_shell(ws, f"echo HACKED > {victim}")
        assert over["exit_code"] != 0, over
        assert victim.read_text(encoding="utf-8") == "ORIGINAL", "outside file was overwritten!"

        rm = _run_shell(ws, f"rm -f {victim}")
        assert rm["exit_code"] != 0, rm
        assert victim.exists(), "outside file was deleted!"

        # creating a NEW file out-of-zone is allowed (tier-2 create-only) — control
        newf = outside / "fresh.txt"
        created = _run_shell(ws, f"echo hi > {newf}")
        assert created["exit_code"] == 0, created
        assert newf.read_text(encoding="utf-8").strip() == "hi"
    finally:
        shutil.rmtree(outside, ignore_errors=True)


# --------------------------------------------------------------------------- #
# credential dir not writable (validates the M1 defense-in-depth half-step)    #
# --------------------------------------------------------------------------- #

@_needs_sandbox
def test_run_shell_cannot_write_into_ssh(tmp_path: Path) -> None:
    marker = Path.home() / ".ssh" / f"v4_probe_{uuid.uuid4().hex[:8]}"
    assert not marker.exists()
    try:
        r = _run_shell(tmp_path / "ws", f"touch {marker}")
        assert r["exit_code"] != 0, r
        assert not marker.exists(), "wrote a new file into ~/.ssh (authorized_keys vector open!)"
    finally:
        # defensive: never leave a probe file in ~/.ssh (it should never exist)
        try:
            marker.unlink()
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# control: workspace is genuinely usable (we didn't just break everything)     #
# --------------------------------------------------------------------------- #

@_needs_sandbox
def test_run_shell_workspace_is_writable_control(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    r = _run_shell(ws, "echo ok > out.txt && cat out.txt")
    assert r["exit_code"] == 0, r
    assert r["stdout_tail"].strip() == "ok"
    assert (ws / "out.txt").read_text(encoding="utf-8").strip() == "ok"
    assert r["sandbox_enforced"] is True


# --------------------------------------------------------------------------- #
# web_search / web_open: host-side internet, no raw HTML in return dict        #
# --------------------------------------------------------------------------- #

def test_web_search_return_dict_never_carries_raw_html(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    html = b"""
    <html><body>
      <!-- RAW_HTML_ONLY_MARKER -->
      <a class="result__a" href="https://example.com/current">Current facts</a>
      <div class="result__snippet">One useful snippet.</div>
    </body></html>
    """

    class _Headers:
        def get(self, k, default=None):
            return "text/html" if k == "Content-Type" else default

    class _Resp:
        def read(self, *_a, **_k):
            return html

        headers = _Headers()

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

    class _Opener:
        def open(self, _req, timeout=None):
            return _Resp()

    monkeypatch.setattr(web_search_mod.urllib.request, "build_opener", lambda *a, **k: _Opener())

    ws = tmp_path / "ws"
    ctx = _ctx(ws)
    result = asyncio.run(web_search_mod.dispatch({"query": "current facts"}, ctx))
    blob = json.dumps(result, default=str)

    assert result["result_count"] == 1
    assert "RAW_HTML_ONLY_MARKER" not in blob
    assert "result__a" not in blob
    assert "<html" not in blob
    assert not any(isinstance(v, (bytes, bytearray)) for v in result.values())


def test_web_open_return_dict_never_carries_raw_html(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    html = b"""
    <html><head><title>Readable</title><script>RAW_SCRIPT_MARKER</script></head>
    <body><h1>Visible title</h1><p>Readable paragraph.</p></body></html>
    """

    class _Headers:
        def get(self, k, default=None):
            return "text/html" if k == "Content-Type" else default

    class _Resp:
        def read(self, *_a, **_k):
            return html

        headers = _Headers()

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

    class _Opener:
        def open(self, _req, timeout=None):
            return _Resp()

    monkeypatch.setattr(web_search_mod.urllib.request, "build_opener", lambda *a, **k: _Opener())

    ws = tmp_path / "ws"
    ctx = _ctx(ws)
    result = asyncio.run(
        web_search_mod.dispatch_open({"url": "https://example.com/readable"}, ctx)
    )
    blob = json.dumps(result, default=str)

    assert "Visible title" in result["content"]
    assert "Readable paragraph." in result["content"]
    assert "RAW_SCRIPT_MARKER" not in blob
    assert "<html" not in blob
    assert "<h1>" not in blob
    assert not any(isinstance(v, (bytes, bytearray)) for v in result.values())


# --------------------------------------------------------------------------- #
# fetch: downloaded bytes land in the workspace, never in the returned dict    #
# (the returned dict is what becomes a tool_result / SSE payload)              #
# --------------------------------------------------------------------------- #

def test_fetch_payload_never_enters_return_dict(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    payload = b"SECRETPAYLOAD-" + b"\x89PNG\r\n" + b"x" * 2048

    class _Headers:
        def get(self, k, default=None):
            return "image/png" if k == "Content-Type" else default

    class _Resp:
        def read(self):
            return payload

        headers = _Headers()

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

    class _Opener:
        def open(self, _req, timeout=None):
            return _Resp()

    monkeypatch.setattr(fetch_mod.urllib.request, "build_opener", lambda *a, **k: _Opener())

    ws = tmp_path / "ws"
    ws.mkdir()
    ctx = _ctx(ws)
    result = asyncio.run(
        fetch_mod.dispatch({"url": "https://example.com/pic.png", "dest_name": "pic.png"}, ctx)
    )

    # bytes physically landed in the workspace
    assert (ws / "pic.png").read_bytes() == payload

    # the return dict (→ tool_result → SSE) carries only path/metadata, no bytes
    assert set(result).issubset({"asset_id", "path", "size_bytes", "content_type", "summary"})
    assert result["path"] == "pic.png"
    assert result["size_bytes"] == len(payload)
    blob = json.dumps(result, default=str)
    assert "SECRETPAYLOAD" not in blob, "raw bytes leaked into the SSE-bound return dict"
    assert not any(isinstance(v, (bytes, bytearray)) for v in result.values())
