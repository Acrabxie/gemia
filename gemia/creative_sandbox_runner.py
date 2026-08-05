"""Auditable local command runner for Lumeri Creative Dev Sandbox v0."""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Sequence

from .creative_sandbox_permissions import (
    CreativeSandboxPermissionError,
    CreativeSandboxPolicy,
    safe_args,
    validate_command,
)


@dataclass
class SandboxArtifact:
    path: str
    rel_path: str | None
    size: int
    modified_at: str
    declared: bool = False


@dataclass
class CreativeCommandResult:
    command_id: str
    status: str
    command_line: str
    safe_args: list[str]
    cwd: str
    workspace_dir: str
    start_time: str
    end_time: str
    duration_ms: int
    exit_code: int | None
    stdout_tail: str
    stderr_tail: str
    artifacts: list[SandboxArtifact] = field(default_factory=list)
    error: dict[str, str] | None = None
    timed_out: bool = False
    sandbox_enforced: bool = False

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["artifacts"] = [asdict(item) for item in self.artifacts]
        return payload


class CreativeSandboxRunner:
    """Run one vetted command inside ``workspaces/<session_id>``.

    The primary enforcement is path/command validation plus macOS
    ``sandbox-exec`` when available. On non-macOS hosts the result reports
    ``sandbox_enforced=False`` so callers can decide whether to expose it.
    """

    def __init__(self, root_dir: str | Path, *, session_id: str, policy: CreativeSandboxPolicy | None = None) -> None:
        self.root_dir = Path(root_dir).expanduser().resolve()
        self.session_id = _safe_session_id(session_id)
        self.workspace_dir = (self.root_dir / "workspaces" / self.session_id).resolve()
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        self.policy = policy or CreativeSandboxPolicy()

    def run(
        self,
        args: Sequence[str],
        *,
        cwd: str | Path | None = None,
        timeout_sec: float = 30,
        declared_artifact_paths: Sequence[str | Path] | None = None,
        command_id: str | None = None,
    ) -> CreativeCommandResult:
        command_id = _safe_optional_command_id(command_id) or f"cmd_{uuid.uuid4().hex[:12]}"
        start = _now()
        safe = safe_args([str(a) for a in args])
        command_line = shlex.join(safe)
        run_cwd = self._resolve_cwd(cwd)
        declared_items: list[Path] = []
        for item in declared_artifact_paths or ():
            path = Path(item).expanduser()
            if not path.is_absolute():
                path = self.workspace_dir / path
            declared_items.append(path.resolve())
        declared = tuple(declared_items)
        for path in declared:
            path.parent.mkdir(parents=True, exist_ok=True)
        policy = CreativeSandboxPolicy(
            allow_network=self.policy.allow_network,
            allowed_binaries=self.policy.allowed_binaries,
            blocked_binaries=self.policy.blocked_binaries,
            blocked_python_modules=self.policy.blocked_python_modules,
            allowed_python_imports=self.policy.allowed_python_imports,
            declared_output_paths=declared,
        )
        before = _snapshot_files(self.workspace_dir, declared)
        try:
            validate_command([str(a) for a in args], workspace_dir=self.workspace_dir, cwd=run_cwd, policy=policy)
        except CreativeSandboxPermissionError as exc:
            return self._result(
                command_id=command_id,
                status="blocked",
                command_line=command_line,
                safe=safe,
                cwd=run_cwd,
                start=start,
                exit_code=None,
                stdout="",
                stderr="",
                artifacts=[],
                error={"code": exc.code, "message": str(exc)},
            )

        sandbox_cmd, sandbox_enforced = _sandbox_command(
            [str(a) for a in args],
            workspace_dir=self.workspace_dir,
            declared_paths=declared,
            allow_network=policy.allow_network,
        )
        env = _command_env(
            root_dir=self.root_dir,
            workspace_dir=self.workspace_dir,
            session_id=self.session_id,
            args=[str(a) for a in args],
        )
        try:
            proc = subprocess.run(
                sandbox_cmd,
                cwd=str(run_cwd),
                env=env,
                text=True,
                capture_output=True,
                timeout=timeout_sec,
            )
            status = "succeeded" if proc.returncode == 0 else "failed"
            artifacts = _discover_artifacts(self.workspace_dir, declared, before)
            return self._result(
                command_id=command_id,
                status=status,
                command_line=command_line,
                safe=safe,
                cwd=run_cwd,
                start=start,
                exit_code=proc.returncode,
                stdout=proc.stdout,
                stderr=proc.stderr,
                artifacts=artifacts,
                sandbox_enforced=sandbox_enforced,
            )
        except subprocess.TimeoutExpired as exc:
            artifacts = _discover_artifacts(self.workspace_dir, declared, before)
            return self._result(
                command_id=command_id,
                status="timeout",
                command_line=command_line,
                safe=safe,
                cwd=run_cwd,
                start=start,
                exit_code=124,
                stdout=exc.stdout or "",
                stderr=exc.stderr or f"Command timed out after {timeout_sec}s",
                artifacts=artifacts,
                error={"code": "timeout", "message": f"Command timed out after {timeout_sec}s"},
                timed_out=True,
                sandbox_enforced=sandbox_enforced,
            )
        except Exception as exc:  # pragma: no cover - defensive traceback firewall
            return self._result(
                command_id=command_id,
                status="failed",
                command_line=command_line,
                safe=safe,
                cwd=run_cwd,
                start=start,
                exit_code=None,
                stdout="",
                stderr="",
                artifacts=[],
                error={"code": "runner_failed", "message": f"{type(exc).__name__}: {exc}"},
                sandbox_enforced=sandbox_enforced,
            )

    def _resolve_cwd(self, cwd: str | Path | None) -> Path:
        if cwd is None:
            return self.workspace_dir
        path = Path(cwd).expanduser()
        if not path.is_absolute():
            path = self.workspace_dir / path
        return path.resolve()

    def _result(
        self,
        *,
        command_id: str,
        status: str,
        command_line: str,
        safe: list[str],
        cwd: Path,
        start: datetime,
        exit_code: int | None,
        stdout: str,
        stderr: str,
        artifacts: list[SandboxArtifact],
        error: dict[str, str] | None = None,
        timed_out: bool = False,
        sandbox_enforced: bool = False,
    ) -> CreativeCommandResult:
        end = _now()
        return CreativeCommandResult(
            command_id=command_id,
            status=status,
            command_line=command_line,
            safe_args=safe,
            cwd=str(cwd),
            workspace_dir=str(self.workspace_dir),
            start_time=start.isoformat(),
            end_time=end.isoformat(),
            duration_ms=int((end - start).total_seconds() * 1000),
            exit_code=exit_code,
            stdout_tail=_tail(stdout),
            stderr_tail=_tail(_strip_traceback(stderr)),
            artifacts=artifacts,
            error=error,
            timed_out=timed_out,
            sandbox_enforced=sandbox_enforced,
        )


def run_creative_command(
    args: Sequence[str],
    *,
    root_dir: str | Path,
    session_id: str,
    cwd: str | Path | None = None,
    timeout_sec: float = 30,
    declared_artifact_paths: Sequence[str | Path] | None = None,
    allow_network: bool = False,
) -> dict[str, Any]:
    policy = CreativeSandboxPolicy(allow_network=allow_network)
    runner = CreativeSandboxRunner(root_dir, session_id=session_id, policy=policy)
    return runner.run(
        args,
        cwd=cwd,
        timeout_sec=timeout_sec,
        declared_artifact_paths=declared_artifact_paths,
    ).to_dict()


def _sandbox_command(
    args: list[str],
    *,
    workspace_dir: Path,
    declared_paths: Sequence[Path],
    allow_network: bool,
) -> tuple[list[str], bool]:
    sandbox_exec = shutil.which("sandbox-exec")
    if not sandbox_exec or os.name != "posix" or sys.platform != "darwin" or not _sandbox_exec_usable(sandbox_exec):
        return args, False
    profile = _sandbox_profile(workspace_dir, declared_paths=declared_paths, allow_network=allow_network)
    return [sandbox_exec, "-p", profile, *args], True


_SANDBOX_EXEC_USABLE: bool | None = None


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


def _sandbox_profile(workspace_dir: Path, *, declared_paths: Sequence[Path], allow_network: bool) -> str:
    write_rules = [f'(subpath "{_escape_profile_path(workspace_dir)}")']
    for path in declared_paths:
        if path.suffix:
            write_rules.append(f'(literal "{_escape_profile_path(path)}")')
        else:
            write_rules.append(f'(subpath "{_escape_profile_path(path)}")')
    network_rule = "(allow network*)" if allow_network else "(deny network*)"
    return "\n".join(
        [
            "(version 1)",
            "(deny default)",
            "(allow process*)",
            "(allow sysctl*)",
            "(allow file-read*)",
            f"(allow file-write* {' '.join(write_rules)})",
            network_rule,
        ]
    )


def _escape_profile_path(path: Path) -> str:
    return str(path).replace("\\", "\\\\").replace('"', '\\"')


def _command_env(
    *,
    root_dir: Path,
    workspace_dir: Path,
    session_id: str,
    args: Sequence[str],
) -> dict[str, str]:
    script_path, script_hash = _python_script_context(args, workspace_dir=workspace_dir)
    package_root = Path(__file__).resolve().parent.parent
    python_path = os.pathsep.join(
        part
        for part in [
            str(root_dir),
            str(package_root),
            os.environ.get("PYTHONPATH", ""),
        ]
        if part
    )
    keep = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": python_path,
        "DYLD_LIBRARY_PATH": os.environ.get("DYLD_LIBRARY_PATH", ""),
        "FFMPEG_BINARY": os.environ.get("FFMPEG_BINARY", ""),
        "PYTHONDONTWRITEBYTECODE": "1",
        "LUMERAI_SCRIPT_MODE": "1",
        "LUMERAI_PROJECT_ROOT": str(root_dir),
        "LUMERAI_OUTPUT_DIR": str(workspace_dir / "previews"),
        "LUMERAI_WORKSPACE_DIR": str(workspace_dir),
        "LUMERAI_SESSION_ID": session_id,
        "LUMERAI_AI_MODEL": "creative-sandbox",
    }
    if script_path:
        keep["LUMERAI_SCRIPT_PATH"] = str(script_path)
    if script_hash:
        keep["LUMERAI_SCRIPT_HASH"] = script_hash
    return {k: v for k, v in keep.items() if v}


def _python_script_context(args: Sequence[str], *, workspace_dir: Path) -> tuple[Path | None, str]:
    if not args:
        return None, ""
    binary = Path(str(args[0])).name
    if not (binary == "python" or binary.startswith("python")):
        return None, ""
    for raw in args[1:]:
        value = str(raw)
        if value.startswith("-"):
            continue
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = workspace_dir / path
        try:
            resolved = path.resolve()
            resolved.relative_to(workspace_dir)
        except (OSError, RuntimeError, ValueError):
            continue
        if resolved.suffix != ".py" or not resolved.exists():
            continue
        try:
            digest = sha256(resolved.read_bytes()).hexdigest()
        except OSError:
            digest = ""
        return resolved, digest
    return None, ""


def _snapshot_files(workspace_dir: Path, declared: Sequence[Path]) -> dict[str, tuple[int, int]]:
    roots = [workspace_dir, *declared]
    snapshot: dict[str, tuple[int, int]] = {}
    for root in roots:
        paths = [root] if root.is_file() else list(root.rglob("*")) if root.exists() and root.is_dir() else []
        for path in paths:
            if path.is_file():
                try:
                    stat = path.stat()
                except OSError:
                    continue
                snapshot[str(path.resolve())] = (stat.st_size, stat.st_mtime_ns)
    return snapshot


def _discover_artifacts(workspace_dir: Path, declared: Sequence[Path], before: dict[str, tuple[int, int]]) -> list[SandboxArtifact]:
    discovered: list[SandboxArtifact] = []
    seen: set[str] = set()
    roots = [workspace_dir, *declared]
    declared_resolved = {str(p.resolve()) for p in declared}
    for root in roots:
        paths = [root] if root.is_file() else list(root.rglob("*")) if root.exists() and root.is_dir() else []
        for path in sorted(paths):
            if not path.is_file():
                continue
            try:
                resolved = path.resolve()
                stat = resolved.stat()
            except OSError:
                continue
            key = str(resolved)
            if key in seen:
                continue
            previous = before.get(key)
            if previous == (stat.st_size, stat.st_mtime_ns) and key not in declared_resolved:
                continue
            seen.add(key)
            rel: str | None
            try:
                rel = str(resolved.relative_to(workspace_dir))
            except ValueError:
                rel = None
            discovered.append(
                SandboxArtifact(
                    path=key,
                    rel_path=rel,
                    size=stat.st_size,
                    modified_at=datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
                    declared=key in declared_resolved,
                )
            )
    return discovered


def _tail(text: str, *, limit: int = 4000) -> str:
    if len(text) <= limit:
        return text
    return text[-limit:]


def _strip_traceback(stderr: str) -> str:
    lines = (stderr or "").splitlines()
    if not any(line.startswith("Traceback ") for line in lines):
        return stderr or ""
    kept: list[str] = []
    in_traceback = False
    last_error = ""
    for line in lines:
        if line.startswith("Traceback "):
            in_traceback = True
            continue
        if in_traceback:
            if line and not line.startswith(" ") and not line.lstrip().startswith(("File ", "^", "~")):
                last_error = line.strip()
            continue
        kept.append(line)
    if last_error:
        kept.append(last_error)
    return "\n".join(kept)


def _safe_session_id(session_id: str) -> str:
    value = str(session_id or "").strip()
    if not value or not all(ch.isalnum() or ch in {"_", "-"} for ch in value) or len(value) > 80:
        raise ValueError("invalid session_id")
    return value


def _safe_optional_command_id(command_id: str | None) -> str:
    value = str(command_id or "").strip()
    if not value:
        return ""
    if not all(ch.isalnum() or ch in {"_", "-"} for ch in value) or len(value) > 80:
        return ""
    return value


def _now() -> datetime:
    return datetime.now(timezone.utc)


def write_result_json(path: str | Path, payload: CreativeCommandResult | dict[str, Any]) -> None:
    target = Path(path)
    data = payload.to_dict() if isinstance(payload, CreativeCommandResult) else payload
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
