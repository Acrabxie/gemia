"""build verb family — async Python code execution in sandbox + skill persistence.

Four dispatchers:

1. dispatch (verb `build`):
   - Async submit of Python code to sandbox with Popen, no wait.
   - Returns immediately with job_id, script_path, stdout/stderr log paths.
   - Uses build_v4_sandbox_command() from sandbox_v4.py for two-tier isolation.

2. dispatch_check (verb `check_job`):
   - Poll a pending build job by job_id.
   - Checks process status, kills on timeout, reads log tails.
   - Returns job_id, status, exit_code, stdout_tail, stderr_tail, summary.

3. dispatch_wait (verb `wait_for_job`):
   - Block (with async.sleep) until job done or max_wait_sec exceeded.
   - Same return shape as check_job + waited_sec, timed_out.

4. dispatch_kill (verb `kill_job`):
   - SIGKILL the whole process group of a running build/shell job.
   - Registry maps result to failed + error="killed by kill_job".

5. dispatch_save_skill (verb `save_skill`):
   - Host-side only: copy source to skills dir, write JSON metadata.
   - Validates path containment, slugifies name, prevents overwrite.
   - Returns skill, path, summary.

Module-level `_PROCESSES` dict tracks (Popen, deadline_monotonic) for each
job_id — build verb AND background run_shell jobs share it (check/wait/kill
work uniformly), with per-family pending caps keyed on the job_id prefix.
Limits: max 3 pending build jobs, max 3 pending shell jobs.
"""
from __future__ import annotations

import asyncio
import atexit
import json
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from gemia.device_capabilities import resolve_device_capabilities
from gemia.sandbox_v4 import build_v4_sandbox_command, is_sandbox_disabled
from gemia.tools._context import ToolContext
from gemia.tools.run_shell import _minimal_env


# Module-level process tracking: job_id -> (Popen, deadline_monotonic).
# Shared by the build verb AND background run_shell jobs (job_id prefixes
# "build_" / "shell_" keep the per-family pending caps separate).
_PROCESSES: dict[str, tuple[subprocess.Popen[str], float]] = {}

# Log reading limits (see _read_log_slice). The cap kills runaway jobs whose
# output would otherwise grow without bound.
_LOG_TAIL_BYTES = 4000
_LOG_SLICE_MAX_BYTES = 16384
_LOG_CAP_BYTES = 10 * 1024 * 1024


def _kill_all_tracked_processes() -> None:
    """Interpreter-exit backstop: SIGKILL every process group still tracked.

    SessionRunner.close() does the polite per-session SIGTERM→SIGKILL sweep;
    this only matters when the whole server dies with jobs running, so
    background children never outlive the host as orphans.
    """
    for _job_id, (proc, _deadline) in list(_PROCESSES.items()):
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass
    _PROCESSES.clear()


atexit.register(_kill_all_tracked_processes)


# ────────────────────────────────────────────────────────────────────────────
# verb `build` — async submit Python code to sandbox
# ────────────────────────────────────────────────────────────────────────────


async def dispatch(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Async submit a project entrypoint or compatibility code blob.

    Args:
        entrypoint: preferred, existing relative path in the project workspace
        code: compatibility source string; mutually exclusive with entrypoint
        language: optional, inferred from entrypoint or defaults to "python3"
        filename: optional compatibility-code filename, with no path separators
        args: optional, list of string arguments to pass to script
        device_capabilities: optional host capability requests
        timeout_sec: optional, default 120, clamped to (0, 600]
        note: optional, human-readable description

    Returns:
        {
            job_id: str,
            status: "submitted",
            script_path: str (relative to workspace),
            stdout_log: str (relative to workspace),
            stderr_log: str (relative to workspace),
            sandbox_enforced: True,
            summary: str
        }

    Raises:
        ValueError: if >3 pending builds, the entrypoint escapes the project,
                   both/neither code and entrypoint are provided, or arguments
                   are invalid
        RuntimeError: if sandbox not enforced
    """
    code = str(args.get("code") or "").strip()
    entrypoint = str(args.get("entrypoint") or "").strip()
    if bool(code) == bool(entrypoint):
        raise ValueError("build requires exactly one of 'entrypoint' or non-empty 'code'")

    workspace = Path(ctx.output_dir).resolve()
    project_program_root: Path | None = None
    if ctx.project is not None:
        project_program_root = (
            ctx.project.store.project_dir(ctx.project.project_id) / "design"
        ).resolve()
    entrypoint_path: Path | None = None
    project_revision_commit: dict[str, Any] | None = None
    if entrypoint:
        if entrypoint.startswith("project://"):
            relative = entrypoint[len("project://") :].lstrip("/")
            if project_program_root is None or not relative.startswith("design/"):
                raise ValueError(
                    "project entrypoint must use project://design/... in a persistent project"
                )
            candidate = project_program_root / relative[len("design/") :]
            allowed_root = project_program_root
        else:
            raw_entrypoint = Path(entrypoint).expanduser()
            if raw_entrypoint.is_absolute():
                raise ValueError(
                    "entrypoint must be relative or use project://design/..."
                )
            candidate = workspace / raw_entrypoint
            allowed_root = workspace
        try:
            entrypoint_path = candidate.resolve(strict=True)
            entrypoint_path.relative_to(allowed_root)
        except (FileNotFoundError, OSError, RuntimeError, ValueError) as exc:
            raise ValueError(
                "entrypoint must resolve to an existing file inside its project "
                f"workspace: {entrypoint!r}"
            ) from exc
        if not entrypoint_path.is_file():
            raise ValueError(f"entrypoint must be a file, got {entrypoint!r}")
        if project_program_root is not None:
            production_store = ctx.extra.get("production_store")
            durable_project_id = str(ctx.extra.get("project_id") or "")
            run_id = str(ctx.extra.get("run_id") or "")
            if production_store is not None and durable_project_id and run_id:
                project_revision_commit = production_store.observe_design_program(
                    durable_project_id,
                    run_id,
                    design_root=project_program_root,
                    trace_id=str(ctx.extra.get("active_trace_id") or "") or None,
                )

    requested_language = str(args.get("language") or "").strip().lower()
    suffix_languages = {
        ".py": "python3",
        ".js": "node",
        ".mjs": "node",
        ".sh": "bash",
        ".go": "go",
        ".rb": "ruby",
        ".rs": "rust",
    }
    language = requested_language
    if not language and entrypoint_path is not None:
        language = suffix_languages.get(entrypoint_path.suffix.lower(), "")
        if not language:
            raise ValueError(
                "cannot infer language from entrypoint suffix; pass 'language' explicitly"
            )
    if not language:
        language = "python3"
    # Normalize language names
    if language in ("node", "nodejs", "javascript", "js"):
        language = "node"
    elif language in ("bash", "shell", "sh"):
        language = "bash"
    elif language not in ("python3", "python", "go", "ruby", "rust"):
        # Strict validation: only known languages pass through
        raise ValueError(
            f"Unsupported language '{language}'. Supported: python3, node, bash, go, ruby, rust"
        )

    filename = str(args.get("filename") or "script.py").strip()
    if code and ("/" in filename or "\\" in filename):
        raise ValueError(f"filename must not contain path separators, got {filename!r}")

    script_args = args.get("args")
    if script_args is None:
        script_args = []
    elif isinstance(script_args, list):
        script_args = [str(a) for a in script_args]
    else:
        raise ValueError(f"args must be a list, got {type(script_args)}")

    timeout_sec = args.get("timeout_sec", 120)
    try:
        timeout_sec = float(timeout_sec)
    except (TypeError, ValueError):
        raise ValueError(f"timeout_sec must be a number, got {timeout_sec!r}") from None

    if timeout_sec <= 0 or timeout_sec > 600:
        raise ValueError(f"timeout_sec must be in (0, 600], got {timeout_sec}")

    note = str(args.get("note") or "").strip()
    if not note:
        note = (
            f"Run design program {entrypoint}"
            if entrypoint
            else code.split("\n")[0][:60]
        )

    requested_capabilities = args.get("device_capabilities")
    if requested_capabilities is None:
        requested_capabilities = []
    elif not isinstance(requested_capabilities, list):
        raise ValueError("device_capabilities must be a list")
    unrestricted_host = is_sandbox_disabled()
    capability_plan = resolve_device_capabilities(
        (str(value) for value in requested_capabilities),
        unrestricted_host=unrestricted_host,
    )

    # Check pending limit: max 3 concurrent builds. _PROCESSES also holds
    # background shell jobs — count only this family's, by job_id prefix.
    # Snapshot before polling: proc.poll() releases the GIL (os.waitpid), so a
    # concurrent pop from another session's watcher thread would raise
    # "dictionary changed size during iteration" against the live view.
    pending_count = len([
        jid for jid, (p, _) in list(_PROCESSES.items())
        if jid.startswith("build_") and p.poll() is None
    ])
    if pending_count >= 3:
        raise ValueError(
            f"Too many pending builds (max 3). Currently pending: {pending_count}"
        )

    # JobRegistry generates "build_<uuid hex[:8]>" — collision-free, unlike
    # a wallclock-derived id. Builds produce workspace files, not registry
    # assets, so there is no pending asset to pre-allocate.
    record = ctx.jobs.submit(
        kind="build",
        provider=f"local:{language}-sandbox",
        operation_name="pending",
        pending_asset_id="-",
        estimated_eta_sec=timeout_sec,
        summary=note,
    )
    job_id = record.job_id

    # Create job directory
    job_dir = Path(ctx.output_dir) / "builds" / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    # Compatibility code is still supported, but a real design program lives
    # as incrementally editable project files and runs by entrypoint.
    if entrypoint_path is None:
        script_path = job_dir / filename
        script_path.write_text(code, encoding="utf-8")
        source_mode = "inline_code"
    else:
        script_path = entrypoint_path
        source_mode = "project_entrypoint"

    # Prepare log files
    stdout_log = job_dir / "stdout.log"
    stderr_log = job_dir / "stderr.log"

    # Build sandboxed command. When the user has explicitly disabled the
    # sandbox (POST /settings/sandbox), run the raw command with full system
    # access and report enforced=False honestly — do NOT raise.
    # Map language to interpreter
    interpreters: dict[str, list[str]] = {
        "python3": [sys.executable],
        "python": [sys.executable],
        "node": ["/usr/bin/env", "node"],
        "bash": ["/bin/bash"],
        "go": ["/usr/bin/env", "go", "run"],
        "ruby": ["/usr/bin/env", "ruby"],
        "rust": ["/usr/bin/env", "rustc"],
    }
    interpreter = interpreters.get(language, [sys.executable])
    program_cmd = [*interpreter, str(script_path), *script_args]

    if unrestricted_host:
        cmd = program_cmd
        enforced = False
    else:
        cmd, enforced = build_v4_sandbox_command(
            program_cmd,
            workspace_dir=ctx.output_dir,
            allow_gpu=capability_plan.allow_gpu,
        )
        if not enforced:
            raise RuntimeError(
                "sandbox-exec unavailable or failed on this host; refusing to run "
                "code without sandbox enforcement"
            )

    # Start process with new session (process group isolation)
    deadline = time.monotonic() + timeout_sec
    child_env = _minimal_env()
    if language in ("python", "python3"):
        repository_root = Path(__file__).resolve().parents[2]
        python_paths = [
            script_path.parent,
            workspace,
            workspace / ".site-packages",
            repository_root,
        ]
        child_env["PYTHONPATH"] = os.pathsep.join(
            str(path) for path in python_paths if path.exists()
        )
        child_env["PYTHONDONTWRITEBYTECODE"] = "1"
        child_env["LUMERI_DESIGN_PROGRAM"] = "1"
        child_env["LUMERI_PROJECT_ROOT"] = str(
            project_program_root.parent if project_program_root is not None else workspace
        )
        child_env["LUMERI_RUN_WORKSPACE"] = str(workspace)
    try:
        with open(stdout_log, "w") as out_f, open(stderr_log, "w") as err_f:
            proc = subprocess.Popen(
                cmd,
                stdout=out_f,
                stderr=err_f,
                cwd=str(ctx.output_dir),
                env=child_env,
                start_new_session=True,  # Process group isolation
            )
    except Exception as e:
        stdout_log.unlink(missing_ok=True)
        stderr_log.unlink(missing_ok=True)
        ctx.jobs.update_from_poll(job_id, "failed", error=f"failed to start: {e}")
        raise RuntimeError(f"failed to start build process: {e}") from e

    # Track process
    _PROCESSES[job_id] = (proc, deadline)
    record.operation_name = str(proc.pid)

    return {
        "job_id": job_id,
        "status": "submitted",
        "script_path": (
            "project://design/" + str(script_path.resolve().relative_to(project_program_root))
            if project_program_root is not None
            and script_path.resolve().is_relative_to(project_program_root)
            else str(script_path.resolve().relative_to(workspace))
        ),
        "stdout_log": str(stdout_log.resolve().relative_to(workspace)),
        "stderr_log": str(stderr_log.resolve().relative_to(workspace)),
        "sandbox_enforced": enforced,
        "source_mode": source_mode,
        "device_capabilities": capability_plan.to_receipt(),
        "project_revision_commit": project_revision_commit,
        "summary": note,
    }


# ────────────────────────────────────────────────────────────────────────────
# verb `check_job` — poll a pending build job
# ────────────────────────────────────────────────────────────────────────────


def _read_log_slice(path: Path, since_offset: int | None) -> tuple[str, int, bool]:
    """Read an incremental slice of a log without loading the whole file.

    Returns (text, next_offset, truncated). With no offset: the last
    _LOG_TAIL_BYTES bytes (truncated=True means earlier output was skipped).
    With an offset: up to _LOG_SLICE_MAX_BYTES from there (truncated=True
    means more bytes remain past next_offset — call again with it).
    """
    if not path.exists():
        return "", 0, False
    size = path.stat().st_size
    with open(path, "rb") as f:
        if since_offset is None:
            start = max(0, size - _LOG_TAIL_BYTES)
            f.seek(start)
            data = f.read(size - start)
            return data.decode("utf-8", errors="replace"), size, start > 0
        start = min(max(0, int(since_offset)), size)
        f.seek(start)
        data = f.read(min(size - start, _LOG_SLICE_MAX_BYTES))
        next_offset = start + len(data)
        return data.decode("utf-8", errors="replace"), next_offset, next_offset < size


def _job_log_paths(record: Any, ctx: ToolContext) -> tuple[Path, Path | None]:
    """(primary log, separate stderr log or None) for a job by kind."""
    if record.kind == "shell":
        # Background run_shell merges stdout+stderr into one file.
        return Path(ctx.output_dir) / "tasks" / f"{record.job_id}.log", None
    job_dir = Path(ctx.output_dir) / "builds" / record.job_id
    return job_dir / "stdout.log", job_dir / "stderr.log"


def _check_job_impl(
    job_id: str,
    ctx: ToolContext,
    since_offset: int | None = None,
    *,
    mark_announced: bool = True,
) -> dict[str, Any]:
    """Synchronous job check logic for build and shell jobs.

    Shared by check_job, wait_for_job AND the session background watcher.
    mark_announced=True (model-facing polls) records that the model has
    seen a terminal state, so the watcher won't notify it again; the
    watcher itself passes False.
    """
    record = ctx.jobs.get(job_id)  # Raises KeyError if not found
    is_shell = record.kind == "shell"
    primary_log, stderr_log = _job_log_paths(record, ctx)

    exit_code = None
    status = record.last_polled_status

    # Poll process if still tracked (do this BEFORE reading log slices so a
    # just-finished job's final output is included in this response).
    if job_id in _PROCESSES:
        proc, deadline = _PROCESSES[job_id]
        rc = proc.poll()

        if rc is None:
            now = time.monotonic()
            log_size = primary_log.stat().st_size if primary_log.exists() else 0
            # Build jobs write stdout/stderr to two files; a runaway stream on
            # stderr must count toward the cap too (shell jobs merge into one
            # file, so stderr_log is None there and this is a no-op).
            if stderr_log is not None and stderr_log.exists():
                log_size += stderr_log.stat().st_size
            if now > deadline or log_size > _LOG_CAP_BYTES:
                # Timeout or runaway output: kill the whole process group.
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except OSError:
                    pass  # Already gone
                try:
                    exit_code = proc.wait(timeout=1)
                except Exception:
                    # A SIGKILL'd child can be momentarily unreapable (D-state on
                    # slow I/O). Finalize the job as failed anyway — leaving it
                    # non-terminal here would strand it (the watcher's except
                    # handler would keep polling, but never resolve it).
                    exit_code = None
                if log_size > _LOG_CAP_BYTES:
                    error = f"log exceeded {_LOG_CAP_BYTES // (1024 * 1024)}MB cap"
                else:
                    error = f"timeout after {int(record.estimated_eta_sec)}s"
                ctx.jobs.update_from_poll(job_id, "failed", error=error)
                status = "failed"
                _PROCESSES.pop(job_id, None)
            else:
                status = "running"
                ctx.jobs.update_from_poll(job_id, "running")
        elif rc == 0:
            status = "done"
            exit_code = rc
            if is_shell:
                final = primary_log
            else:
                # The job dir's single script is the durable artifact; fall
                # back to the stdout log if the model named the file
                # unconventionally. Skip exFAT AppleDouble "._*" sidecars.
                job_dir = primary_log.parent
                scripts = sorted(
                    p for p in job_dir.glob("*.py") if not p.name.startswith("._")
                )
                final = scripts[0] if scripts else primary_log
            ctx.jobs.update_from_poll(job_id, "done", final_path=final)
            _PROCESSES.pop(job_id, None)
        else:
            status = "failed"
            exit_code = rc
            error_msg = f"exit code {rc}"
            err_source = stderr_log if stderr_log is not None else primary_log
            err_tail, _, _ = _read_log_slice(err_source, None)
            if err_tail:
                error_msg += f"; last output: {err_tail[-200:]}"
            ctx.jobs.update_from_poll(job_id, "failed", error=error_msg)
            _PROCESSES.pop(job_id, None)

    stdout_tail, next_offset, truncated = _read_log_slice(primary_log, since_offset)
    stderr_tail = ""
    if stderr_log is not None:
        stderr_tail, _, _ = _read_log_slice(stderr_log, None)

    if mark_announced and status in ("done", "failed"):
        record.announced = True

    result = {
        "job_id": job_id,
        "status": status,
        "stdout_tail": stdout_tail,
        "stderr_tail": stderr_tail,
        "exit_code": exit_code,
        "summary": record.summary,
        "next_offset": next_offset,
        "truncated": truncated,
    }
    if is_shell:
        result["log_path"] = str(primary_log)
        if record.final_error:
            result["error"] = record.final_error
    return result


# ── restart reconciliation for background shell jobs ─────────────────────────
# After a process restart the Popen handles in _PROCESSES are gone, but the
# persisted registry (jobs.json) still lists shell jobs that were mid-flight.
# We can no longer manage those OS processes (no handle; re-adoption is a later
# batch), so on load we reconcile them to an honest terminal state. The one hard
# rule: reconcile NEVER kills — a reused pid would point at an unrelated process,
# and a verified-live orphan is left to finish on its own.

# A still-running orphan's start time matches started_epoch within a few seconds
# (spawn latency + `ps` 1s resolution); a reused pid started much later. This
# tolerance only sharpens the error MESSAGE — reconcile never kills either way.
_RECONCILE_IDENTITY_TOLERANCE_SEC = 5.0
_RECONCILE_TAIL_CHARS = 2000


def _process_alive(pid: int) -> bool:
    """True if `pid` maps to a live process. signal 0 probes without delivering:
    ESRCH → gone, EPERM → alive but owned by someone else."""
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def _parse_ps_etime(s: str) -> float | None:
    """Parse `ps -o etime` elapsed time — `[[dd-]hh:]mm:ss` — into seconds.
    Portable across BSD (macOS) and GNU ps, both of which support etime (the
    GNU-only `etimes` raw-seconds column is not available on macOS)."""
    s = s.strip()
    if not s:
        return None
    days = 0
    if "-" in s:
        d, _, s = s.partition("-")
        try:
            days = int(d)
        except ValueError:
            return None
    try:
        nums = [float(p) for p in s.split(":")]
    except ValueError:
        return None
    sec = 0.0
    for n in nums:  # mm:ss or hh:mm:ss, most-significant first
        sec = sec * 60 + n
    return days * 86400 + sec


def _process_start_epoch(pid: int) -> float | None:
    """Best-effort wall-clock epoch when `pid` started, via `ps -o etime=`
    (elapsed since start). None when ps is unavailable or the pid is gone. Used
    only for the pid-reuse identity check; a None result degrades to a
    conservative 'could not verify' outcome — never a kill."""
    try:
        out = subprocess.run(
            ["ps", "-p", str(pid), "-o", "etime="],
            capture_output=True,
            text=True,
            timeout=3,
        )
    except Exception:
        return None
    if out.returncode != 0:
        return None
    elapsed = _parse_ps_etime(out.stdout or "")
    if elapsed is None:
        return None
    return time.time() - elapsed


def shell_job_output_tail(record: Any, ctx: ToolContext, *, limit: int = _RECONCILE_TAIL_CHARS) -> str:
    """The tail of a shell job's merged log — for rebuilding a completion notice
    on restart (an already-terminal job the model never got to see)."""
    primary_log, _ = _job_log_paths(record, ctx)
    tail, _, _ = _read_log_slice(primary_log, None)
    return tail[-limit:]


def reconcile_orphan_shell_job(record: Any, ctx: ToolContext) -> dict[str, Any]:
    """Resolve a background shell job that outlived its session's process.

    Called on session load for a shell job whose persisted status was still
    non-terminal. The Popen handle is gone; classify by pid liveness + a
    start-time identity check and mark the job failed with an honest reason.
    NEVER kills: a reused pid belongs to an unrelated process, and a verified
    live orphan is left running (re-adoption is a later batch).

    Returns a notice-shaped dict so the caller can disclose the job's fate to
    the model on the next turn.
    """
    tail = shell_job_output_tail(record, ctx)
    pid = record.pid

    if not pid:
        error = "lost across restart (no pid recorded)"
    elif not _process_alive(pid):
        error = "process ended during restart; final state unknown"
    else:
        start = _process_start_epoch(pid)
        if start is None:
            error = (
                f"still-running pid {pid} across restart; identity unverifiable "
                "— left running, not re-adopted"
            )
        elif (
            record.started_epoch is not None
            and abs(start - record.started_epoch) <= _RECONCILE_IDENTITY_TOLERANCE_SEC
        ):
            error = (
                f"orphaned across restart — pid {pid} may still be running; not "
                "re-adopted (its result will not be collected)"
            )
        else:
            error = f"identity lost — pid {pid} was reused across restart"

    ctx.jobs.update_from_poll(record.job_id, "failed", error=error)
    elapsed = None
    if record.started_epoch is not None:
        elapsed = max(0.0, time.time() - record.started_epoch)
    return {
        "job_id": record.job_id,
        "status": "failed",
        "exit_code": None,
        "summary": record.summary,
        "error": error,
        "elapsed_sec": elapsed,
        "output_tail": tail,
    }


async def dispatch_check(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Poll a pending job by job_id (build, background shell, or Veo video job).

    Args:
        job_id: required, job identifier returned by build/run_shell/generate_video
        since_offset: optional int (build/shell only) — byte offset from a
            previous check_job's next_offset; returns only NEW log output
            since then instead of the default tail

    Returns for build/shell jobs:
        { job_id, status, stdout_tail, stderr_tail, exit_code, summary,
          next_offset, truncated }
    Returns for video jobs:
        { job_id, status, summary } + asset_id/metadata when done

    Raises:
        KeyError: if job_id not found in any registry
    """
    job_id = str(args.get("job_id") or "").strip()
    if not job_id:
        raise ValueError("check_job requires 'job_id' argument")

    since_offset = args.get("since_offset")
    if since_offset is not None:
        try:
            since_offset = int(since_offset)
        except (TypeError, ValueError):
            raise ValueError(
                f"since_offset must be an integer, got {since_offset!r}"
            ) from None
        if since_offset < 0:
            since_offset = 0

    # Dispatch by job kind: video jobs use Veo LRO polling via JobRegistry.
    try:
        record = ctx.jobs.get(job_id)
        if record.kind == "video":
            from gemia.tools import generate_video as _gv
            return await _gv.resolve_veo_job(job_id, ctx)
    except KeyError:
        pass  # Not a JobRegistry job; fall through to build/_PROCESSES path.

    return _check_job_impl(job_id, ctx, since_offset)


# ────────────────────────────────────────────────────────────────────────────
# verb `wait_for_job` — block until job done or timeout
# ────────────────────────────────────────────────────────────────────────────


async def dispatch_wait(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Wait for a job to complete or timeout (build or Veo video job).

    Args:
        job_id: required, job identifier returned by build or generate_video
        max_wait_sec: optional, default 60 for build / 300 for video,
                      clamped to (0, 300]

    Returns: same shape as check_job plus waited_sec and timed_out fields.

    Raises:
        KeyError: if job_id not found
    """
    job_id = str(args.get("job_id") or "").strip()
    if not job_id:
        raise ValueError("wait_for_job requires 'job_id' argument")

    max_wait_sec = args.get("max_wait_sec", 60)
    try:
        max_wait_sec = float(max_wait_sec)
    except (TypeError, ValueError):
        raise ValueError(f"max_wait_sec must be a number, got {max_wait_sec!r}") from None

    if max_wait_sec <= 0 or max_wait_sec > 300:
        raise ValueError(f"max_wait_sec must be in (0, 300], got {max_wait_sec}")

    # Dispatch by job kind: Veo video jobs poll via resolve_veo_job.
    try:
        record = ctx.jobs.get(job_id)
        if record.kind == "video":
            from gemia.tools import generate_video as _gv
            start = time.monotonic()
            while True:
                result = await _gv.resolve_veo_job(job_id, ctx)
                elapsed = time.monotonic() - start
                if result["status"] in ("done", "failed"):
                    result["waited_sec"] = elapsed
                    result["timed_out"] = False
                    return result
                if elapsed >= max_wait_sec:
                    result["waited_sec"] = elapsed
                    result["timed_out"] = True
                    return result
                await asyncio.sleep(10.0)  # Veo operations change slowly
        if record.kind == "shell":
            # A long blocking wait on a shell job re-creates the stuck-loop
            # problem backgrounding exists to solve; the watcher notifies on
            # completion, so cap the busy-wait and let the model move on.
            max_wait_sec = min(max_wait_sec, 60.0)
    except KeyError:
        pass  # Not a JobRegistry job; fall through to build/_PROCESSES path.

    start = time.monotonic()
    while True:
        result = _check_job_impl(job_id, ctx)
        elapsed = time.monotonic() - start

        if result["status"] in ("done", "failed"):
            result["waited_sec"] = elapsed
            result["timed_out"] = False
            return result

        if elapsed >= max_wait_sec:
            result["waited_sec"] = elapsed
            result["timed_out"] = True
            return result

        await asyncio.sleep(1)


# ────────────────────────────────────────────────────────────────────────────
# verb `kill_job` — stop a running background job
# ────────────────────────────────────────────────────────────────────────────


async def dispatch_kill(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Kill a running build/shell job by job_id (verb `kill_job`).

    SIGKILLs the whole process group (grandchildren included) using the pgid
    persisted at spawn — getpgid() on a dead group leader would raise. The
    registry maps the result to failed + error="killed by kill_job" (v1 has
    no separate "killed" status: list_pending only treats done/failed as
    terminal, so a new status would pend forever). Idempotent on jobs that
    already finished.

    Returns:
        { job_id, status, killed, summary } (+ already_finished when a no-op)

    Raises:
        KeyError: if job_id not found
        ValueError: for job kinds without a local process (e.g. video LROs)
    """
    job_id = str(args.get("job_id") or "").strip()
    if not job_id:
        raise ValueError("kill_job requires 'job_id' argument")

    record = ctx.jobs.get(job_id)  # Raises KeyError if not found
    if record.kind not in ("build", "shell"):
        raise ValueError(
            f"kill_job only supports build/shell jobs; {job_id} is kind "
            f"{record.kind!r} (a remote operation that cannot be killed locally)"
        )

    if record.last_polled_status in ("done", "failed"):
        return {
            "job_id": job_id,
            "status": record.last_polled_status,
            "killed": False,
            "already_finished": True,
            "summary": record.summary,
        }

    entry = _PROCESSES.get(job_id)
    proc = entry[0] if entry is not None else None
    # If we still hold a handle but the OS process already exited (e.g. reaped by
    # a cap-count poll before the watcher marked it terminal), do NOT killpg its
    # pgid — the OS may have recycled it onto an unrelated process group. Reap the
    # handle and record the terminal state instead.
    already_exited = proc is not None and proc.poll() is not None
    pgid = record.pgid or (proc.pid if proc is not None else None)
    killed = False
    if pgid and not already_exited:
        try:
            os.killpg(pgid, signal.SIGKILL)
            killed = True
        except (ProcessLookupError, PermissionError):
            killed = False  # already dead (or pid identity lost) — still mark terminal
    if proc is not None:
        try:
            proc.wait(timeout=2)  # reap so no zombie lingers
        except Exception:
            pass
        _PROCESSES.pop(job_id, None)
    record.announced = True  # the model initiated this; no notification needed
    ctx.jobs.update_from_poll(job_id, "failed", error="killed by kill_job")

    return {
        "job_id": job_id,
        "status": "failed",
        "killed": killed,
        "error": "killed by kill_job",
        "summary": record.summary,
    }


# ────────────────────────────────────────────────────────────────────────────
# verb `save_skill` — persist a build artifact as a reusable skill
# ────────────────────────────────────────────────────────────────────────────


async def dispatch_save_skill(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Save a built script as a reusable skill.

    Args:
        source: required, workspace-relative path to source file
        name: required, human-readable skill name
        description: optional, skill description
        overwrite: optional bool, default False; if False, error on name conflict

    Returns:
        {
            skill: str (slug name),
            path: str (absolute path to saved skill),
            summary: str
        }

    Raises:
        ValueError: if source outside workspace, file not found, name slug
                   conflicts and not overwrite, invalid name
        FileNotFoundError: if source file doesn't exist
    """
    source = str(args.get("source") or "").strip()
    if not source:
        raise ValueError("save_skill requires 'source' argument")

    name = str(args.get("name") or "").strip()
    if not name:
        raise ValueError("save_skill requires 'name' argument")

    description = str(args.get("description") or "").strip()
    overwrite = bool(args.get("overwrite", False))

    # Resolve source path relative to workspace
    source_path = (Path(ctx.output_dir) / source).resolve()

    # Validate containment (prevent path traversal)
    try:
        source_path.relative_to(ctx.output_dir.resolve())
    except ValueError:
        raise ValueError(
            f"source path {source_path} is outside workspace {ctx.output_dir}"
        ) from None

    if not source_path.exists():
        raise FileNotFoundError(f"source file does not exist: {source_path}")

    # Slugify name: lowercase, spaces to hyphens, only [a-z0-9_-]
    slug = name.lower()
    slug = re.sub(r"\s+", "-", slug)
    slug = re.sub(r"[^a-z0-9_-]", "", slug)
    if not slug:
        raise ValueError(f"name produces empty slug after slugification: {name!r}")

    # Determine skills root
    output_parent = Path(ctx.output_dir).parent
    if output_parent.name == "workdirs":
        # Production: skills at ../../skills relative to workdir parent
        skills_root = output_parent.parent / "skills"
    else:
        # Test: skills at output_dir/skills
        skills_root = Path(ctx.output_dir) / "skills"

    skills_root.mkdir(parents=True, exist_ok=True)

    # Target skill path
    skill_file = skills_root / f"{slug}.py"
    skill_meta = skills_root / f"{slug}.json"

    # Check overwrite
    if skill_file.exists() and not overwrite:
        raise ValueError(
            f"skill {slug!r} already exists at {skill_file}; set overwrite=true to replace"
        )

    # Copy source
    skill_file.write_bytes(source_path.read_bytes())

    # Write metadata
    meta = {
        "name": name,
        "slug": slug,
        "description": description,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "origin_session": ctx.session_id,
        "source": source,  # Relative path from workspace for traceability
    }
    skill_meta.write_text(json.dumps(meta, indent=2, ensure_ascii=False))

    return {
        "skill": slug,
        "path": str(skill_file),
        "summary": f"Saved skill '{slug}' from {source}",
    }


__all__ = [
    "dispatch",
    "dispatch_check",
    "dispatch_wait",
    "dispatch_kill",
    "dispatch_save_skill",
]
