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

4. dispatch_save_skill (verb `save_skill`):
   - Host-side only: copy source to skills dir, write JSON metadata.
   - Validates path containment, slugifies name, prevents overwrite.
   - Returns skill, path, summary.

Module-level `_PROCESSES` dict tracks (Popen, deadline_monotonic) for
each job_id. Limits: max 3 pending build jobs.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from gemia.sandbox_v4 import build_v4_sandbox_command, is_sandbox_disabled
from gemia.tools._context import ToolContext
from gemia.tools.run_shell import _minimal_env


# Module-level process tracking: job_id -> (Popen, deadline_monotonic)
_PROCESSES: dict[str, tuple[subprocess.Popen[str], float]] = {}


# ────────────────────────────────────────────────────────────────────────────
# verb `build` — async submit Python code to sandbox
# ────────────────────────────────────────────────────────────────────────────


async def dispatch(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Async submit code to sandbox (supports multiple languages).

    Args:
        code: required, source code string (language determined by 'language' param)
        language: optional, default "python3". Supported: "python3", "node", "bash", etc.
        filename: optional, default "script.py", must be simple name (no path separators)
        args: optional, list of string arguments to pass to script
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
        ValueError: if >3 pending builds, or filename has path separators,
                   or timeout out of range, or code empty
        RuntimeError: if sandbox not enforced
    """
    code = str(args.get("code") or "").strip()
    if not code:
        raise ValueError("build requires non-empty 'code' argument")

    language = str(args.get("language") or "python3").strip().lower()
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
    if "/" in filename or "\\" in filename:
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
        # Default to first line of code or first 60 chars
        note = (code.split("\n")[0][:60]) if code else "Python build"

    # Check pending limit: max 3 concurrent builds
    pending_count = len([p for p, _ in _PROCESSES.values() if p.poll() is None])
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

    # Write script
    script_path = job_dir / filename
    script_path.write_text(code, encoding="utf-8")

    # Prepare log files
    stdout_log = job_dir / "stdout.log"
    stderr_log = job_dir / "stderr.log"

    # Build sandboxed command. When the user has explicitly disabled the
    # sandbox (POST /settings/sandbox), run the raw command with full system
    # access and report enforced=False honestly — do NOT raise.
    # Map language to interpreter
    interpreters = {
        "python3": "/usr/bin/env python3",
        "python": "/usr/bin/env python3",
        "node": "/usr/bin/env node",
        "bash": "/bin/bash",
        "go": "/usr/bin/env go run",
        "ruby": "/usr/bin/env ruby",
        "rust": "/usr/bin/env rustc",
    }
    interpreter = interpreters.get(language, "/usr/bin/env python3")
    
    if is_sandbox_disabled():
        cmd = interpreter.split() + [str(script_path), *script_args]
        enforced = False
    else:
        cmd, enforced = build_v4_sandbox_command(
            interpreter.split() + [str(script_path), *script_args],
            workspace_dir=ctx.output_dir,
        )
        if not enforced:
            raise RuntimeError(
                "sandbox-exec unavailable or failed on this host; refusing to run "
                "code without sandbox enforcement"
            )

    # Start process with new session (process group isolation)
    deadline = time.monotonic() + timeout_sec
    try:
        with open(stdout_log, "w") as out_f, open(stderr_log, "w") as err_f:
            proc = subprocess.Popen(
                cmd,
                stdout=out_f,
                stderr=err_f,
                cwd=str(ctx.output_dir),
                env=_minimal_env(),
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
        "script_path": str(script_path.relative_to(ctx.output_dir)),
        "stdout_log": str(stdout_log.relative_to(ctx.output_dir)),
        "stderr_log": str(stderr_log.relative_to(ctx.output_dir)),
        "sandbox_enforced": enforced,
        "summary": note,
    }


# ────────────────────────────────────────────────────────────────────────────
# verb `check_job` — poll a pending build job
# ────────────────────────────────────────────────────────────────────────────


def _check_job_impl(job_id: str, ctx: ToolContext) -> dict[str, Any]:
    """Synchronous job check logic (reused by both check_job and wait_for_job)."""
    record = ctx.jobs.get(job_id)  # Raises KeyError if not found
    job_dir = Path(ctx.output_dir) / "builds" / job_id

    stdout_log = job_dir / "stdout.log"
    stderr_log = job_dir / "stderr.log"

    stdout_tail = ""
    stderr_tail = ""
    exit_code = None
    status = record.last_polled_status

    # Read tails if logs exist
    if stdout_log.exists():
        with open(stdout_log) as f:
            content = f.read()
            stdout_tail = content[-4000:] if len(content) > 4000 else content

    if stderr_log.exists():
        with open(stderr_log) as f:
            content = f.read()
            stderr_tail = content[-4000:] if len(content) > 4000 else content

    # Poll process if still tracked
    if job_id in _PROCESSES:
        proc, deadline = _PROCESSES[job_id]
        rc = proc.poll()

        if rc is None:
            # Still running
            now = time.monotonic()
            if now > deadline:
                # Timeout: kill process group
                try:
                    os.killpg(os.getpgid(proc.pid), 9)
                except OSError:
                    pass  # Already gone
                exit_code = proc.wait(timeout=1)
                timeout_sec = record.estimated_eta_sec
                ctx.jobs.update_from_poll(
                    job_id,
                    "failed",
                    error=f"timeout after {int(timeout_sec)}s",
                )
                status = "failed"
                _PROCESSES.pop(job_id, None)
            else:
                status = "running"
                ctx.jobs.update_from_poll(job_id, "running")
        elif rc == 0:
            # Success
            status = "done"
            exit_code = rc
            # The job dir's single script is the durable artifact; fall back
            # to the stdout log if the model named the file unconventionally.
            scripts = sorted(job_dir.glob("*.py"))
            final = scripts[0] if scripts else stdout_log
            ctx.jobs.update_from_poll(job_id, "done", final_path=final)
            _PROCESSES.pop(job_id, None)
        else:
            # Failure
            status = "failed"
            exit_code = rc
            error_msg = f"exit code {rc}"
            if stderr_tail:
                error_msg += f"; last stderr: {stderr_tail[-200:]}"
            ctx.jobs.update_from_poll(job_id, "failed", error=error_msg)
            _PROCESSES.pop(job_id, None)

    return {
        "job_id": job_id,
        "status": status,
        "stdout_tail": stdout_tail,
        "stderr_tail": stderr_tail,
        "exit_code": exit_code,
        "summary": record.summary,
    }


async def dispatch_check(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Poll a pending job by job_id (build or Veo video job).

    Args:
        job_id: required, job identifier returned by build or generate_video

    Returns for build jobs:
        { job_id, status, stdout_tail, stderr_tail, exit_code, summary }
    Returns for video jobs:
        { job_id, status, summary } + asset_id/metadata when done

    Raises:
        KeyError: if job_id not found in any registry
    """
    job_id = str(args.get("job_id") or "").strip()
    if not job_id:
        raise ValueError("check_job requires 'job_id' argument")

    # Dispatch by job kind: video jobs use Veo LRO polling via JobRegistry.
    try:
        record = ctx.jobs.get(job_id)
        if record.kind == "video":
            from gemia.tools import generate_video as _gv
            return await _gv.resolve_veo_job(job_id, ctx)
    except KeyError:
        pass  # Not a JobRegistry job; fall through to build/_PROCESSES path.

    return _check_job_impl(job_id, ctx)


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
    "dispatch_save_skill",
]
