"""v4 build — two-tier sandbox profile (M1 isolation layer).

This is the security命门 of the v4 build effort (networking / bash / code-writing).
It builds the macOS ``sandbox-exec`` SBPL profile that enforces Acrab's two-tier
permission model. The kernel-level ``sandbox-exec`` boundary is the *only* real
wall (see ``docs/v4-build-readiness/11-sandbox-impl-plan.md`` §1.5); the legacy
``creative_sandbox_permissions`` AST/argv allowlist is downgraded to advisory
pre-flight for v4 and is intentionally NOT consulted here.

Tier model
----------
* **workspace**  — full read / write / create / modify / delete (``file-write*``).
* **outside workspace** — readable (except credentials) + may CREATE new files
  and directories, but may NOT modify, append, truncate, delete or rename any
  *pre-existing* file. macOS distinguishes a freshly-created vnode from an
  existing one at ``open()``: writing the new vnode is covered by
  ``file-write-create``; touching an existing one needs ``file-write-data`` /
  ``file-write-unlink`` which we never grant outside the workspace.
* **credentials** — neither readable nor writable (defense in depth).
* **network** — denied inside the sandbox by default. Fetch lives on the host
  (``generate_image``-style verb), so injected sandbox code has no exfil path.

Two silent-failure pitfalls this module guards against (empirically hit
2026-06-06, see the impl-plan doc §1.4 / §1.2):

1. ``sandbox-exec`` matches the *canonical, fully symlink-resolved* real path.
   ``/tmp`` is a symlink to ``/private/tmp``; a rule written as ``/tmp`` silently
   never matches. Every path here is ``Path(...).resolve()``-d before it enters
   the profile.
2. SBPL is *last-match-wins*. A credential ``(deny file-read* ...)`` only works
   if it is emitted AFTER the broad ``(allow file-read*)``. Likewise the
   workspace ``file-write*`` rule is emitted LAST so it wins over the broader
   create-only rule for paths that live under ``$HOME``.

NOTE: This module only *builds* the profile and wraps a command. It is NOT yet
wired into the live dispatch path — the v4 verbs (fetch / run_shell / build) are
M3. Correctness is proven by ``tests/test_sandbox_v4_isolation.py``, which is the
rerunnable isolation probe.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from typing import Sequence

_SANDBOX_EXEC_USABLE: bool | None = None


def _escape_profile_path(path: Path) -> str:
    return str(path).replace("\\", "\\\\").replace('"', '\\"')


def _sandbox_exec_usable(sandbox_exec: str) -> bool:
    global _SANDBOX_EXEC_USABLE
    if _SANDBOX_EXEC_USABLE is not None:
        return _SANDBOX_EXEC_USABLE
    try:
        proc = subprocess.run(
            [sandbox_exec, "-p", "(version 1)\n(allow default)", "/usr/bin/true"],
            capture_output=True,
            text=True,
            timeout=3,
        )
    except Exception:
        _SANDBOX_EXEC_USABLE = False
    else:
        _SANDBOX_EXEC_USABLE = proc.returncode == 0
    return _SANDBOX_EXEC_USABLE

__all__ = [
    "DEFAULT_OUTSIDE_CREATE_ROOTS",
    "DEFAULT_CREDENTIAL_DENY",
    "build_two_tier_profile",
    "build_v4_sandbox_command",
    "ensure_packages",
    "set_sandbox_disabled",
    "is_sandbox_disabled",
]

# Sandbox kill switch. When True, run_shell/build run commands WITHOUT the
# sandbox-exec wrapper (raw system access — e.g. so Blender can reach the GPU).
# Toggled by the server's POST /settings/sandbox endpoint.
#
# A plain module global (not a ContextVar) so it is visible across the v3
# SessionRunner worker threads, which run their own asyncio loops. The value is
# persisted per-machine to ``~/.gemia/sandbox_state.json`` and reloaded on
# import, so the choice survives server restarts.
_SANDBOX_STATE_PATH = Path.home() / ".gemia" / "sandbox_state.json"


def _load_sandbox_disabled() -> bool:
    try:
        import json as _json
        data = _json.loads(_SANDBOX_STATE_PATH.read_text(encoding="utf-8"))
        return bool(data.get("disabled", False))
    except (OSError, ValueError):
        return False


_SANDBOX_DISABLED: bool = _load_sandbox_disabled()


def set_sandbox_disabled(value: bool) -> None:
    global _SANDBOX_DISABLED
    _SANDBOX_DISABLED = bool(value)
    try:
        import json as _json
        _SANDBOX_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _SANDBOX_STATE_PATH.write_text(
            _json.dumps({"disabled": _SANDBOX_DISABLED}), encoding="utf-8"
        )
    except OSError:
        pass  # in-memory toggle still works even if persistence fails


def is_sandbox_disabled() -> bool:
    return _SANDBOX_DISABLED

# Acrab decision #2 (2026-06-07): out-of-zone create roots.
# $HOME + /private/tmp + /Volumes/Extreme SSD (Acrab's external-disk output habit).
DEFAULT_OUTSIDE_CREATE_ROOTS: tuple[str, ...] = (
    str(Path.home()),
    "/private/tmp",
    "/Volumes/Extreme SSD",
)

# Credentials excluded from BOTH read and write. ~/.gemia/config.json holds
# OpenRouter / Pexels / OAuth / Gemini keys; ~/.config/gcloud holds the Vertex
# ADC refresh token; ~/.ssh holds SSH private keys + authorized_keys.
DEFAULT_CREDENTIAL_DENY: tuple[str, ...] = (
    str(Path.home() / ".ssh"),
    str(Path.home() / ".config" / "gcloud"),
    str(Path.home() / ".gemia" / "config.json"),
)


def _resolve(path: str | Path) -> Path:
    return Path(path).expanduser().resolve()


def _path_rule(path: Path) -> str:
    """``(subpath "...")`` for directories, ``(literal "...")`` for files.

    When the path exists we trust the filesystem; otherwise we fall back to the
    suffix heuristic (a ``.json`` etc. is a file, a bare name is a directory).
    """
    if path.exists():
        kind = "subpath" if path.is_dir() else "literal"
    else:
        kind = "literal" if path.suffix else "subpath"
    return f'({kind} "{_escape_profile_path(path)}")'


def build_two_tier_profile(
    workspace_dir: str | Path,
    *,
    outside_create_roots: Sequence[str | Path] = DEFAULT_OUTSIDE_CREATE_ROOTS,
    credential_deny: Sequence[str | Path] = DEFAULT_CREDENTIAL_DENY,
    allow_network: bool = False,
    allow_network_outbound_only: bool = False,
    allow_gpu: bool = False,
) -> str:
    """Build the two-tier SBPL profile string.

    All paths are resolved to their canonical form before insertion. Rule
    ordering is load-bearing (last-match-wins) — do not reorder casually.
    """
    workspace = _resolve(workspace_dir)
    creds = [_resolve(p) for p in credential_deny]
    roots = [_resolve(p) for p in outside_create_roots]

    lines: list[str] = [
        "(version 1)",
        "(deny default)",
        "",
        "; ---- process / system basics ----",
        "(allow process*)",
        "(allow sysctl*)",
        "; mach-lookup is required for macOS frameworks (Quartz, CoreGraphics) to",
        "; resolve WindowServer — without it CGMainDisplayID() returns 0 (invalid).",
        "(allow mach-lookup)",
        "",
        "; ---- read: everything readable EXCEPT credentials ----",
        "; deny MUST follow the broad allow (SBPL last-match-wins).",
        "(allow file-read*)",
    ]
    lines += [f"(deny file-read* {_path_rule(c)})" for c in creds]

    lines += [
        "",
        "; ---- write tier 2: outside workspace = create-new only ----",
        "; create-only (no file-write-data / no file-write-unlink) => new files OK,",
        "; modify / append / truncate / delete / rename of existing = DENIED.",
    ]
    lines += [
        f'(allow file-write-create (subpath "{_escape_profile_path(r)}"))'
        for r in roots
    ]

    lines += [
        "",
        "; ---- credentials: not writable either. ----",
        "; macOS SBPL resolves by MOST-SPECIFIC operation, not pure last-match: a broad",
        "; (deny file-write* ...) does NOT override the (allow file-write-create $HOME)",
        "; above for the create op, so a new file could still be made inside ~/.ssh.",
        "; We therefore deny the create operation explicitly (same specificity as the",
        "; allow, emitted after it => deny wins) AND deny the rest via file-write*.",
        "; (empirically verified 2026-06-07; see RULES.md R3.)",
    ]
    for c in creds:
        rule = _path_rule(c)
        lines.append(f"(deny file-write-create {rule})")
        lines.append(f"(deny file-write* {rule})")

    lines += [
        "",
        "; ---- write tier 1: workspace = full r/w/create/delete ----",
        "; emitted LAST so it wins over the create-only rule for paths under $HOME.",
        f'(allow file-write* (subpath "{_escape_profile_path(workspace)}"))',
        "",
        "; ---- network ----",
        "; allow_network=True  → full bidirectional (legacy flag)",
        "; allow_network_outbound_only=True → outbound-only (safer, enough for pip/curl)",
        "(allow network*)" if allow_network else (
            "(allow network-outbound)" if allow_network_outbound_only else "(deny network*)"
        ),
    ]

    if allow_gpu:
        lines += [
            "",
            "; ---- GPU / Metal: allow IOKit so Blender / Metal apps can init GPU ----",
            "; Risk: sandboxed code can use GPU (crypto mining, GPU mem reads).",
            "; Only enable for trusted callers that explicitly pass allow_gpu=True.",
            "(allow iokit-open)",
            "(allow iokit-get-properties)",
        ]

    return "\n".join(lines)


def ensure_packages(
    workspace_dir: str | Path,
    packages: Sequence[str],
    *,
    quiet: bool = True,
) -> Path:
    """Host-side pip install into workspace/.site-packages.

    Runs outside the sandbox so network is available. The installed path is
    automatically picked up by lumerai/sandbox.py _child_env via PYTHONPATH.
    Returns the site-packages directory path.
    """
    site = Path(workspace_dir) / ".site-packages"
    site.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, "-m", "pip", "install", "--target", str(site)]
    if quiet:
        cmd.append("--quiet")
    cmd.extend(packages)
    subprocess.run(cmd, check=True)
    return site


def build_v4_sandbox_command(
    args: Sequence[str],
    *,
    workspace_dir: str | Path,
    outside_create_roots: Sequence[str | Path] = DEFAULT_OUTSIDE_CREATE_ROOTS,
    credential_deny: Sequence[str | Path] = DEFAULT_CREDENTIAL_DENY,
    allow_network: bool = False,
    allow_network_outbound_only: bool = False,
    allow_gpu: bool = False,
) -> tuple[list[str], bool]:
    """Wrap ``args`` with ``sandbox-exec -p <two-tier profile>``.

    Returns ``(command, sandbox_enforced)``. On a host without a usable
    ``sandbox-exec`` (non-macOS, or the binary refuses a trivial profile) the
    original args are returned with ``sandbox_enforced=False`` so the caller can
    refuse to expose the capability rather than run it unconfined.
    """
    argv = [str(a) for a in args]
    sandbox_exec = shutil.which("sandbox-exec")
    if not sandbox_exec or sys.platform != "darwin" or not _sandbox_exec_usable(sandbox_exec):
        return argv, False
    profile = build_two_tier_profile(
        workspace_dir,
        outside_create_roots=outside_create_roots,
        credential_deny=credential_deny,
        allow_network=allow_network,
        allow_network_outbound_only=allow_network_outbound_only,
        allow_gpu=allow_gpu,
    )
    return [sandbox_exec, "-p", profile, *argv], True
