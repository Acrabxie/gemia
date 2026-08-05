"""Permission checks for the hidden Creative Dev Sandbox command runner.

The runner is intentionally argv-only and conservative. It is meant to let the
vNext UI run local Python/ffmpeg-style work inside ``workspaces/<session_id>``
without turning the old Plan-v2 process into a general shell.
"""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence


class CreativeSandboxPermissionError(ValueError):
    """Raised when a command is outside the v0 sandbox policy."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class CreativeSandboxPolicy:
    allow_network: bool = False
    allowed_binaries: tuple[str, ...] = (
        "python",
        "python3",
        "ffmpeg",
        "ffprobe",
    )
    blocked_binaries: tuple[str, ...] = (
        "bash",
        "brew",
        "curl",
        "git",
        "npx",
        "npm",
        "pip",
        "pip3",
        "pnpm",
        "rsync",
        "scp",
        "sh",
        "ssh",
        "wget",
        "yarn",
    )
    blocked_python_modules: tuple[str, ...] = (
        "aiohttp",
        "ensurepip",
        "ftplib",
        "http",
        "httpx",
        "os",
        "pip",
        "requests",
        "shutil",
        "socket",
        "subprocess",
        "urllib",
        "venv",
    )
    allowed_python_imports: tuple[str, ...] = (
        "csv",
        "datetime",
        "json",
        "lumerai",
        "math",
        "pathlib",
        "random",
        "statistics",
        "time",
    )
    declared_output_paths: tuple[Path, ...] = field(default_factory=tuple)


_URL_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")
_SECRET_RE = re.compile(r"(api[_-]?key|token|secret|password|passwd|bearer)", re.IGNORECASE)
_PATH_SUFFIXES = (
    ".avi",
    ".gif",
    ".jpeg",
    ".jpg",
    ".json",
    ".m4a",
    ".mov",
    ".mp3",
    ".mp4",
    ".png",
    ".py",
    ".txt",
    ".wav",
    ".webm",
)
_BLOCKED_CALLS = {"eval", "exec", "__import__", "compile", "input", "breakpoint"}
_WRITE_METHODS = {"write_text", "write_bytes", "touch", "mkdir"}
_MUTATING_METHODS = _WRITE_METHODS | {"unlink", "rename", "replace", "rmdir"}


def safe_args(args: Sequence[str]) -> list[str]:
    redacted: list[str] = []
    for arg in args:
        value = str(arg)
        if _SECRET_RE.search(value):
            if "=" in value:
                key, _sep, _rest = value.partition("=")
                redacted.append(f"{key}=<redacted>")
            else:
                redacted.append("<redacted>")
        else:
            redacted.append(value)
    return redacted


def validate_command(
    args: Sequence[str],
    *,
    workspace_dir: Path,
    cwd: Path,
    policy: CreativeSandboxPolicy | None = None,
) -> None:
    policy = policy or CreativeSandboxPolicy()
    if not args:
        raise CreativeSandboxPermissionError("empty_command", "command args are required")
    workspace_dir = workspace_dir.resolve()
    cwd = cwd.resolve()
    if not _is_relative_to(cwd, workspace_dir):
        raise CreativeSandboxPermissionError("cwd_outside_workspace", "cwd must stay inside the session workspace")

    binary = Path(str(args[0])).name
    if binary in policy.blocked_binaries:
        raise CreativeSandboxPermissionError("blocked_command", f"command is blocked in v0 sandbox: {binary}")
    if not _binary_allowed(binary, policy.allowed_binaries):
        raise CreativeSandboxPermissionError("command_not_allowed", f"command is not allowed in v0 sandbox: {binary}")

    _reject_network_or_install_args(args, policy=policy)
    _reject_outside_path_args(args, workspace_dir=workspace_dir, cwd=cwd, policy=policy)
    if _is_python_binary(binary):
        _validate_python_command(args, workspace_dir=workspace_dir, cwd=cwd, policy=policy)


def _reject_network_or_install_args(args: Sequence[str], *, policy: CreativeSandboxPolicy) -> None:
    lowered = [str(a).lower() for a in args]
    if not policy.allow_network:
        for arg in lowered:
            if _URL_RE.match(arg):
                raise CreativeSandboxPermissionError("network_blocked", "network URLs are blocked in v0 sandbox")
    binary = Path(str(args[0])).name
    if _is_python_binary(binary):
        for index, arg in enumerate(lowered[:-1]):
            if arg == "-m" and lowered[index + 1].split(".", 1)[0] in {"pip", "ensurepip", "venv"}:
                raise CreativeSandboxPermissionError("package_install_blocked", "Python package/install modules are blocked")
    if binary in {"ffmpeg", "ffprobe"} and any(a in {"http", "https", "tcp", "udp", "rtmp", "rtsp"} for a in lowered):
        raise CreativeSandboxPermissionError("network_blocked", "network protocols are blocked in v0 sandbox")


def _reject_outside_path_args(
    args: Sequence[str],
    *,
    workspace_dir: Path,
    cwd: Path,
    policy: CreativeSandboxPolicy,
) -> None:
    allowed = _allowed_write_roots(workspace_dir, policy.declared_output_paths)
    for raw in args[1:]:
        arg = str(raw)
        if arg.startswith("-") and "=" not in arg:
            continue
        value = arg.split("=", 1)[1] if arg.startswith("-") and "=" in arg else arg
        if not _looks_like_path(value):
            continue
        resolved = _resolve_candidate_path(value, cwd)
        if resolved is None:
            continue
        if not any(_is_relative_to(resolved, root) or resolved == root for root in allowed):
            raise CreativeSandboxPermissionError(
                "path_outside_workspace",
                f"path argument must stay in workspace or declared outputs: {value}",
            )


def _validate_python_command(
    args: Sequence[str],
    *,
    workspace_dir: Path,
    cwd: Path,
    policy: CreativeSandboxPolicy,
) -> None:
    lowered = [str(a).lower() for a in args]
    if "-c" in lowered:
        index = lowered.index("-c")
        try:
            source = str(args[index + 1])
        except IndexError as exc:
            raise CreativeSandboxPermissionError("invalid_python_command", "python -c requires source") from exc
        validate_python_source(source, workspace_dir=workspace_dir, cwd=cwd, policy=policy)
        return

    script_path: Path | None = None
    for raw in args[1:]:
        value = str(raw)
        if value.startswith("-"):
            continue
        candidate = _resolve_candidate_path(value, cwd)
        if candidate and candidate.suffix == ".py":
            script_path = candidate
            break
    if script_path is None:
        return
    if not _is_relative_to(script_path, workspace_dir):
        raise CreativeSandboxPermissionError("script_outside_workspace", "Python scripts must live inside the workspace")
    try:
        source = script_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CreativeSandboxPermissionError("script_read_failed", f"cannot read script: {exc}") from exc
    validate_python_source(source, workspace_dir=workspace_dir, cwd=cwd, policy=policy)


def validate_python_source(
    source: str,
    *,
    workspace_dir: Path,
    cwd: Path,
    policy: CreativeSandboxPolicy | None = None,
) -> None:
    policy = policy or CreativeSandboxPolicy()
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise CreativeSandboxPermissionError("python_syntax_error", f"Python syntax error: {exc.msg}") from exc
    allowed_writes = _allowed_write_roots(workspace_dir, policy.declared_output_paths)
    blocked_modules = set(policy.blocked_python_modules)
    allowed_imports = set(policy.allowed_python_imports)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                _validate_import(alias.name, blocked_modules=blocked_modules, allowed_imports=allowed_imports)
        elif isinstance(node, ast.ImportFrom):
            _validate_import(node.module or "", blocked_modules=blocked_modules, allowed_imports=allowed_imports)
        elif isinstance(node, ast.Call):
            _validate_python_call(node, cwd=cwd, allowed_writes=allowed_writes)
        elif isinstance(node, ast.Attribute) and node.attr.startswith("__"):
            raise CreativeSandboxPermissionError("python_attribute_blocked", f"dunder attribute is blocked: {node.attr}")


def _validate_import(module: str, *, blocked_modules: set[str], allowed_imports: set[str]) -> None:
    root = (module or "").split(".", 1)[0]
    if root in blocked_modules:
        raise CreativeSandboxPermissionError("python_import_blocked", f"Python import is blocked: {module}")
    if root and root not in allowed_imports:
        raise CreativeSandboxPermissionError("python_import_not_allowed", f"Python import is not allowed in v0: {module}")


def _validate_python_call(node: ast.Call, *, cwd: Path, allowed_writes: tuple[Path, ...]) -> None:
    func = node.func
    if isinstance(func, ast.Name):
        if func.id in _BLOCKED_CALLS:
            raise CreativeSandboxPermissionError("python_call_blocked", f"Python call is blocked: {func.id}")
        if func.id == "open":
            _validate_open_call(node, cwd=cwd, allowed_writes=allowed_writes)
    elif isinstance(func, ast.Attribute):
        if func.attr in _MUTATING_METHODS:
            target = _literal_path_from_expr(func.value, cwd)
            if target is None:
                raise CreativeSandboxPermissionError(
                    "python_dynamic_write_blocked",
                    f"dynamic filesystem write is blocked: {func.attr}",
                )
            if not any(_is_relative_to(target, root) or target == root for root in allowed_writes):
                raise CreativeSandboxPermissionError("python_write_outside_workspace", f"write outside workspace is blocked: {target}")


def _validate_open_call(node: ast.Call, *, cwd: Path, allowed_writes: tuple[Path, ...]) -> None:
    if not node.args:
        return
    mode = "r"
    if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
        mode = str(node.args[1].value)
    for kw in node.keywords:
        if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
            mode = str(kw.value.value)
    if not any(flag in mode for flag in ("w", "a", "x", "+")):
        return
    target = _literal_path_from_expr(node.args[0], cwd)
    if target is None:
        raise CreativeSandboxPermissionError("python_dynamic_write_blocked", "dynamic open(..., write-mode) is blocked")
    if not any(_is_relative_to(target, root) or target == root for root in allowed_writes):
        raise CreativeSandboxPermissionError("python_write_outside_workspace", f"write outside workspace is blocked: {target}")


def _literal_path_from_expr(expr: ast.AST, cwd: Path) -> Path | None:
    if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
        return _resolve_candidate_path(expr.value, cwd)
    if isinstance(expr, ast.Call) and isinstance(expr.func, ast.Name) and expr.func.id == "Path" and expr.args:
        return _literal_path_from_expr(expr.args[0], cwd)
    return None


def _allowed_write_roots(workspace_dir: Path, declared: Iterable[Path]) -> tuple[Path, ...]:
    roots = [workspace_dir.resolve()]
    for path in declared:
        roots.append(Path(path).expanduser().resolve())
    return tuple(roots)


def _looks_like_path(value: str) -> bool:
    if not value or _URL_RE.match(value):
        return False
    if "/" in value or value.startswith(".") or value.startswith("~"):
        return True
    return value.lower().endswith(_PATH_SUFFIXES)


def _resolve_candidate_path(value: str, cwd: Path) -> Path | None:
    try:
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = cwd / path
        return path.resolve()
    except (OSError, RuntimeError, ValueError):
        return None


def _is_python_binary(binary: str) -> bool:
    return binary == "python" or binary.startswith("python")


def _binary_allowed(binary: str, allowed_binaries: Iterable[str]) -> bool:
    if binary in allowed_binaries:
        return True
    if _is_python_binary(binary) and any(_is_python_binary(item) for item in allowed_binaries):
        return True
    return False


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False
