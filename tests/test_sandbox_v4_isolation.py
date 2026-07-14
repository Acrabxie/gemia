"""M1 isolation probe (rerunnable) for the v4 build two-tier sandbox.

These tests ARE the security probe Acrab asked for: they drive the *shipped*
``build_two_tier_profile`` through a real ``sandbox-exec`` wrapper and assert the
two-tier boundary actually holds at the kernel level. Unlike a throwaway script
they live in the repo and re-run with the suite, so a future profile regression
fails loudly.

Coverage:
  * profile string: paths resolved (canonical), deny-after-allow ordering.
  * workspace tier: create / modify / append / delete all OK.
  * outside tier: create-new OK, but overwrite / append / O_RDWR / unlink /
    rename of a PRE-EXISTING file all DENIED, and existing content unchanged.
  * credentials: read DENIED + write DENIED (synthetic, and real ~/.gemia etc.).
  * network: outbound connect DENIED.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from gemia.sandbox_v4 import (
    DEFAULT_CREDENTIAL_DENY,
    build_two_tier_profile,
)
def _sandbox_exec_usable(path: str) -> bool:
    import subprocess
    try:
        r = subprocess.run([path, "-p", "(version 1)(allow default)"], capture_output=True, timeout=3)
        return r.returncode == 0
    except Exception:
        return False

_SANDBOX_EXEC = shutil.which("sandbox-exec")
_USABLE = bool(_SANDBOX_EXEC) and sys.platform == "darwin" and _sandbox_exec_usable(_SANDBOX_EXEC or "")
_needs_sandbox = pytest.mark.skipif(not _USABLE, reason="sandbox-exec not usable on this host")


def _run(profile: str, code: str, *, timeout: float = 12) -> subprocess.CompletedProcess[str]:
    """Run ``python3 -c code`` confined by ``profile`` via sandbox-exec."""
    return subprocess.run(
        [_SANDBOX_EXEC, "-p", profile, sys.executable, "-c", textwrap.dedent(code)],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _matrix(stdout: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in stdout.splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip()
    return out


# --------------------------------------------------------------------------- #
# 1. profile string structure (no sandbox needed)                             #
# --------------------------------------------------------------------------- #

def test_profile_resolves_symlinked_paths(tmp_path: Path) -> None:
    # /tmp is a symlink to /private/tmp; the profile must contain the resolved
    # form, never the bare /tmp (the silent-failure pitfall §1.4).
    profile = build_two_tier_profile(
        tmp_path,
        outside_create_roots=["/tmp"],
        credential_deny=[],
    )
    assert '(subpath "/private/tmp")' in profile
    assert '(subpath "/tmp")' not in profile


def test_profile_deny_after_allow_and_workspace_last(tmp_path: Path) -> None:
    cred = tmp_path / "creds" / "secret.json"
    profile = build_two_tier_profile(
        tmp_path / "ws",
        outside_create_roots=[tmp_path / "out"],
        credential_deny=[cred],
    )
    allow_read = profile.index("(allow file-read*)")
    deny_read = profile.index("(deny file-read*")
    assert allow_read < deny_read, "credential read-deny must come AFTER allow file-read*"

    create_rule = profile.index("(allow file-write-create")
    ws_rule = profile.rindex("(allow file-write* ")
    assert create_rule < ws_rule, "workspace full-write must be emitted LAST (last-match-wins)"
    # default = network denied inside sandbox
    assert "(deny network*)" in profile


# --------------------------------------------------------------------------- #
# 2. workspace tier — full read/write/create/delete                           #
# --------------------------------------------------------------------------- #

@_needs_sandbox
def test_workspace_full_write(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "existing.txt").write_text("ORIGINAL", encoding="utf-8")
    profile = build_two_tier_profile(ws, outside_create_roots=[], credential_deny=[])
    proc = _run(
        profile,
        f"""
        import os
        base = {str(ws)!r}
        def t(label, fn):
            try:
                fn(); print(label + "=OK")
            except OSError as e:
                print(label + "=DENIED(%s)" % e.errno)
        t("create", lambda: open(os.path.join(base, "new.txt"), "w").write("x"))
        t("overwrite", lambda: open(os.path.join(base, "existing.txt"), "w").write("CHANGED"))
        t("append", lambda: open(os.path.join(base, "existing.txt"), "a").write("!"))
        t("mkdir", lambda: os.mkdir(os.path.join(base, "sub")))
        t("unlink", lambda: os.unlink(os.path.join(base, "new.txt")))
        """,
    )
    m = _matrix(proc.stdout)
    assert m == {
        "create": "OK",
        "overwrite": "OK",
        "append": "OK",
        "mkdir": "OK",
        "unlink": "OK",
    }, proc.stdout + proc.stderr


# --------------------------------------------------------------------------- #
# 3. outside tier — create-new only, existing immutable                       #
# --------------------------------------------------------------------------- #

@_needs_sandbox
def test_outside_create_only_existing_immutable(tmp_path: Path) -> None:
    # Outside root must live under a real default create root for a faithful
    # probe; /private/tmp is one. Use a unique dir there.
    import os
    import uuid

    outside = Path("/private/tmp") / f"v4probe_{uuid.uuid4().hex[:8]}"
    outside.mkdir(parents=True)
    existing = outside / "existing.txt"
    existing.write_text("ORIGINAL", encoding="utf-8")
    try:
        profile = build_two_tier_profile(
            tmp_path / "ws",  # workspace elsewhere (under /private/var/folders)
            outside_create_roots=[outside],
            credential_deny=[],
        )
        proc = _run(
            profile,
            f"""
            import os
            base = {str(outside)!r}
            existing = os.path.join(base, "existing.txt")
            def t(label, fn):
                try:
                    fn(); print(label + "=OK")
                except OSError as e:
                    print(label + "=DENIED(%s)" % e.errno)
            t("A_create_new", lambda: open(os.path.join(base, "fresh.txt"), "w").write("new"))
            t("G_mkdir", lambda: os.mkdir(os.path.join(base, "freshdir")))
            t("B_overwrite", lambda: open(existing, "w").write("HACKED"))
            t("C_append", lambda: open(existing, "a").write("HACKED"))
            t("D_rdwr", lambda: os.close(os.open(existing, os.O_RDWR)))
            t("E_unlink", lambda: os.unlink(existing))
            t("F_rename", lambda: os.rename(existing, existing + ".moved"))
            """,
        )
        m = _matrix(proc.stdout)
        assert m.get("A_create_new") == "OK", proc.stdout + proc.stderr
        assert m.get("G_mkdir") == "OK", proc.stdout + proc.stderr
        for op in ("B_overwrite", "C_append", "D_rdwr", "E_unlink", "F_rename"):
            assert m.get(op, "").startswith("DENIED"), f"{op}: {proc.stdout}{proc.stderr}"
        # existing content physically unchanged
        assert existing.read_text(encoding="utf-8") == "ORIGINAL"
    finally:
        shutil.rmtree(outside, ignore_errors=True)


# --------------------------------------------------------------------------- #
# 4. credentials — read DENIED + write DENIED                                 #
# --------------------------------------------------------------------------- #

@_needs_sandbox
def test_credentials_read_and_write_denied_synthetic(tmp_path: Path) -> None:
    cred_dir = tmp_path / "fakehome"
    cred_dir.mkdir()
    cred_file = cred_dir / "config.json"
    cred_file.write_text("TOPSECRET", encoding="utf-8")
    sibling = cred_dir / "public.txt"
    sibling.write_text("PUBLIC", encoding="utf-8")

    profile = build_two_tier_profile(
        tmp_path / "ws",
        outside_create_roots=[cred_dir],  # otherwise even cred write would be deny-default
        credential_deny=[cred_file],
    )
    proc = _run(
        profile,
        f"""
        import os
        cred = {str(cred_file)!r}
        sibling = {str(sibling)!r}
        def rd(p):
            try:
                with open(p) as f: return "READ:" + f.read()
            except OSError as e: return "DENIED(%s)" % e.errno
        def wr(p):
            try:
                with open(p, "w") as f: f.write("X"); return "OK"
            except OSError as e: return "DENIED(%s)" % e.errno
        print("cred_read=" + rd(cred))
        print("cred_write=" + wr(cred))
        print("sibling_read=" + rd(sibling))
        """,
    )
    m = _matrix(proc.stdout)
    assert m.get("cred_read", "").startswith("DENIED"), proc.stdout + proc.stderr
    assert m.get("cred_write", "").startswith("DENIED"), proc.stdout + proc.stderr
    assert m.get("sibling_read") == "READ:PUBLIC", proc.stdout + proc.stderr
    # the secret never left the sandbox
    assert "TOPSECRET" not in proc.stdout


@_needs_sandbox
def test_credential_dir_create_denied(tmp_path: Path) -> None:
    # Regression: a credential DIRECTORY sits under an outside create-root (~).
    # macOS SBPL picks the most-specific operation, so a broad file-write* deny
    # does NOT block the create op shadowed by (allow file-write-create $HOME).
    # The builder must also deny file-write-create on credential paths, else a
    # new file (e.g. ~/.ssh/authorized_keys) could be planted.
    home_sim = tmp_path / "home"
    cred_dir = home_sim / ".ssh"
    cred_dir.mkdir(parents=True)

    profile = build_two_tier_profile(
        tmp_path / "ws",
        outside_create_roots=[home_sim],  # create allowed in home, but NOT in ~/.ssh
        credential_deny=[cred_dir],
    )
    proc = _run(
        profile,
        f"""
        import os
        def t(label, fn):
            try:
                fn(); print(label + "=OK")
            except OSError as e:
                print(label + "=DENIED(%s)" % e.errno)
        t("cred_create", lambda: os.close(os.open({str(cred_dir / "authorized_keys")!r}, os.O_CREAT | os.O_WRONLY | os.O_EXCL)))
        t("cred_mkdir", lambda: os.mkdir({str(cred_dir / "evil")!r}))
        t("outside_create_control", lambda: open({str(home_sim / "ok.txt")!r}, "w").write("x"))
        """,
    )
    m = _matrix(proc.stdout)
    assert m.get("cred_create", "").startswith("DENIED"), proc.stdout + proc.stderr
    assert m.get("cred_mkdir", "").startswith("DENIED"), proc.stdout + proc.stderr
    assert m.get("outside_create_control") == "OK", proc.stdout + proc.stderr
    # nothing was actually planted in the credential dir
    assert not (cred_dir / "authorized_keys").exists()
    assert not (cred_dir / "evil").exists()


@_needs_sandbox
def test_real_credentials_denied() -> None:
    real_config = Path.home() / ".gemia" / "config.json"
    if not real_config.exists():
        pytest.skip("~/.gemia/config.json not present on this host")
    profile = build_two_tier_profile(
        Path.home() / "Lumeri" / "workspace",
        credential_deny=DEFAULT_CREDENTIAL_DENY,
    )
    proc = _run(
        profile,
        f"""
        import os
        def rd(p):
            try:
                with open(p) as f: return "READ"
            except OSError as e: return "DENIED(%s)" % e.errno
        def ls(p):
            try:
                os.listdir(p); return "READ"
            except OSError as e: return "DENIED(%s)" % e.errno
        print("gemia_config=" + rd({str(real_config)!r}))
        print("gcloud=" + ls({str(Path.home() / '.config' / 'gcloud')!r}))
        print("ssh=" + ls({str(Path.home() / '.ssh')!r}))
        print("control_etc_hosts=" + rd("/etc/hosts"))
        """,
    )
    m = _matrix(proc.stdout)
    assert m.get("gemia_config", "").startswith("DENIED"), proc.stdout + proc.stderr
    assert m.get("gcloud", "").startswith("DENIED"), proc.stdout + proc.stderr
    assert m.get("ssh", "").startswith("DENIED"), proc.stdout + proc.stderr
    # control: a non-credential read still works (proves we didn't just break reads)
    assert m.get("control_etc_hosts") == "READ", proc.stdout + proc.stderr


# --------------------------------------------------------------------------- #
# 5. network — outbound denied inside sandbox                                 #
# --------------------------------------------------------------------------- #

@_needs_sandbox
def test_network_denied_inside_sandbox(tmp_path: Path) -> None:
    profile = build_two_tier_profile(tmp_path / "ws", outside_create_roots=[], credential_deny=[])
    proc = _run(
        profile,
        """
        import socket
        try:
            s = socket.socket(); s.settimeout(3); s.connect(("1.1.1.1", 443))
            print("net=OPEN"); s.close()
        except OSError as e:
            print("net=DENIED(%s)" % e.errno)
        """,
    )
    assert _matrix(proc.stdout).get("net", "").startswith("DENIED"), proc.stdout + proc.stderr


@_needs_sandbox
def test_network_allowed_when_opted_in(tmp_path: Path) -> None:
    # Sanity: the deny is from our profile, not the environment. We only assert
    # the syscall is permitted by the sandbox (socket created), not reachability.
    profile = build_two_tier_profile(
        tmp_path / "ws", outside_create_roots=[], credential_deny=[], allow_network=True
    )
    proc = _run(
        profile,
        """
        import socket
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM); s.close()
            print("sock=OK")
        except OSError as e:
            print("sock=DENIED(%s)" % e.errno)
        """,
    )
    assert _matrix(proc.stdout).get("sock") == "OK", proc.stdout + proc.stderr
