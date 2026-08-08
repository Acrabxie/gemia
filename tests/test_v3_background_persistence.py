"""Restart persistence + reconciliation for background shell jobs.

Covers the second-commit half of the background-task chain: a job registry that
survives a process restart, and honest reconciliation of any shell job that was
mid-flight when the previous process stopped.

  * reconcile_orphan_shell_job classifies by pid liveness + start-time identity:
    dead pid → failed "process ended"; live + matching start → "orphaned"
    (NEVER killed); live + mismatched start → "identity lost" (NEVER killed);
    no pid → "lost (no pid)". The invariant under test is that reconcile never
    kills — a reused pid must not take an unrelated process down with it.
  * AgentLoopV3, constructed with a sessions_root that already holds a jobs.json,
    restores shell jobs on load: a mid-flight job is reconciled to failed and a
    completion notice is queued; an already-terminal un-announced job re-queues
    its notice; an already-announced one stays silent; a video LRO is skipped.
    The reconciled terminal states are persisted straight back to jobs.json.

Real child processes run raw bash so the pid checks exercise real kernel state;
the model is a fake client so no network is touched.
"""
from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Any, AsyncIterator

import pytest

from gemia.agent_loop_v3 import AgentLoopV3
from gemia.tools import build
from gemia.tools._context import AssetRegistry, ToolContext
from gemia.tools._jobs import JobRegistry


# ── helpers ──────────────────────────────────────────────────────────────────


class _TextStopClient:
    """Fake model: one text delta then stop. Never touches the network."""

    model = "fake"

    def __init__(self) -> None:
        self.calls = 0

    async def stream_turn(
        self, messages: list[dict[str, Any]], *, tools=None, temperature: float = 0.7
    ) -> AsyncIterator[dict[str, Any]]:
        del messages, tools, temperature
        self.calls += 1
        yield {"kind": "text_delta", "text": "ack"}
        yield {"kind": "finish", "reason": "stop"}


@pytest.fixture(autouse=True)
def _reap_children():
    """Kill any sleeper a test spawned to stand in for a live orphan."""
    spawned: list[subprocess.Popen] = []
    yield spawned
    for proc in spawned:
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass
        try:
            proc.wait(timeout=1)
        except Exception:
            pass


def _live_sleeper(spawned: list[subprocess.Popen]) -> subprocess.Popen:
    proc = subprocess.Popen(
        ["/bin/sleep", "30"], start_new_session=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    spawned.append(proc)
    return proc


def _ctx(tmp_path: Path) -> ToolContext:
    return ToolContext(
        session_id="persist_test",
        output_dir=tmp_path,
        registry=AssetRegistry(),
        emit_progress=lambda _u: None,
        jobs=JobRegistry(),
    )


def _submit_shell(ctx: ToolContext, job_id: str, *, pid: int | None,
                  started_epoch: float | None, status: str = "running") -> Any:
    record = ctx.jobs.submit(
        kind="shell", provider="local:bash-raw", operation_name=str(pid or "-"),
        pending_asset_id="-", estimated_eta_sec=600.0, summary=f"cmd {job_id}",
        job_id=job_id,
    )
    record.pid = pid
    record.pgid = pid
    record.started_epoch = started_epoch
    if status != "submitted":
        ctx.jobs.update_from_poll(job_id, status)
    return record


# ── reconcile_orphan_shell_job classification ────────────────────────────────


def test_reconcile_dead_pid_marks_failed(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    # 999999 is virtually never a live pid → os.kill(pid, 0) raises ESRCH.
    rec = _submit_shell(ctx, "shell_dead", pid=999_999, started_epoch=time.time())

    notice = build.reconcile_orphan_shell_job(rec, ctx)

    assert ctx.jobs.get("shell_dead").last_polled_status == "failed"
    assert "process ended" in notice["error"]
    assert notice["status"] == "failed"
    assert notice["job_id"] == "shell_dead"


def test_reconcile_no_pid_marks_failed(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    rec = _submit_shell(ctx, "shell_nopid", pid=None, started_epoch=time.time())

    notice = build.reconcile_orphan_shell_job(rec, ctx)

    assert ctx.jobs.get("shell_nopid").last_polled_status == "failed"
    assert "no pid" in notice["error"]


def test_reconcile_live_matching_start_is_orphaned_and_never_killed(
    tmp_path: Path, _reap_children: list[subprocess.Popen]
) -> None:
    """A still-running process whose start time matches started_epoch is a
    genuine orphan: marked failed 'orphaned', but LEFT RUNNING (reconcile must
    never kill — this is the safety invariant)."""
    ctx = _ctx(tmp_path)
    proc = _live_sleeper(_reap_children)
    rec = _submit_shell(ctx, "shell_orphan", pid=proc.pid, started_epoch=time.time())

    notice = build.reconcile_orphan_shell_job(rec, ctx)

    assert ctx.jobs.get("shell_orphan").last_polled_status == "failed"
    assert "orphan" in notice["error"].lower()
    assert proc.poll() is None, "reconcile must NOT kill a live orphan"


def test_reconcile_reused_pid_is_identity_lost_and_never_killed(
    tmp_path: Path, _reap_children: list[subprocess.Popen]
) -> None:
    """A live pid whose real start time is far from started_epoch is a REUSED
    pid pointing at an unrelated process: marked 'identity lost' and, critically,
    NEVER killed (killing it would take down that innocent process)."""
    ctx = _ctx(tmp_path)
    proc = _live_sleeper(_reap_children)
    # started_epoch far in the past → the live pid's ps start won't match.
    rec = _submit_shell(
        ctx, "shell_reused", pid=proc.pid, started_epoch=time.time() - 100_000
    )

    notice = build.reconcile_orphan_shell_job(rec, ctx)

    assert ctx.jobs.get("shell_reused").last_polled_status == "failed"
    assert "identity lost" in notice["error"]
    assert proc.poll() is None, "reconcile must NOT kill a reused-pid process"


# ── AgentLoopV3._load_and_reconcile_jobs on construction ─────────────────────


def _write_jobs_json(sessions_root: Path, sid: str) -> JobRegistry:
    """Seed a prior-process jobs.json: one mid-flight shell job (dead pid), one
    terminal un-announced shell job, one terminal announced shell job, and one
    video LRO (which load must skip)."""
    reg = JobRegistry()
    # mid-flight shell job → must be reconciled to failed + notified
    r1 = reg.submit(kind="shell", provider="local:bash-raw", operation_name="999999",
                    pending_asset_id="-", estimated_eta_sec=600.0,
                    summary="find / -name x", job_id="shell_midflight")
    r1.pid = 999_999
    r1.pgid = 999_999
    r1.started_epoch = time.time()
    reg.update_from_poll("shell_midflight", "running")
    # terminal, un-announced → notice must be re-queued
    r2 = reg.submit(kind="shell", provider="local:bash-raw", operation_name="1",
                    pending_asset_id="-", estimated_eta_sec=1.0,
                    summary="echo done", job_id="shell_done_unseen")
    reg.update_from_poll("shell_done_unseen", "done")
    r2.announced = False
    # terminal, already announced → must stay silent
    r3 = reg.submit(kind="shell", provider="local:bash-raw", operation_name="1",
                    pending_asset_id="-", estimated_eta_sec=1.0,
                    summary="echo seen", job_id="shell_done_seen")
    reg.update_from_poll("shell_done_seen", "done")
    r3.announced = True
    # a video LRO → load must SKIP it (no local process to reconcile)
    reg.submit(kind="video", provider="ai_studio:veo-3.1-fast",
               operation_name="operations/deadbeef", pending_asset_id="v_1",
               estimated_eta_sec=120.0, summary="a veo job", job_id="video_x")

    reg.save(sessions_root / sid / "jobs.json")
    return reg


def test_load_reconciles_midflight_and_replays_unseen_notices(tmp_path: Path) -> None:
    sid = "sess_restore"
    sessions_root = tmp_path / "sessions"
    workdir = tmp_path / "workdirs" / sid
    workdir.mkdir(parents=True, exist_ok=True)
    _write_jobs_json(sessions_root, sid)

    loop = AgentLoopV3(
        session_id=sid,
        output_dir=workdir,
        sessions_root=sessions_root,
        gemini_client=_TextStopClient(),
        emit_event=lambda _e: None,
    )

    jobs = loop._tool_ctx.jobs
    # mid-flight → reconciled to failed
    assert jobs.get("shell_midflight").last_polled_status == "failed"
    assert "process ended" in (jobs.get("shell_midflight").final_error or "")
    # both terminal jobs restored as-is
    assert jobs.get("shell_done_unseen").last_polled_status == "done"
    assert jobs.get("shell_done_seen").last_polled_status == "done"
    # Provider LROs outlive the local process and remain pollable after resume.
    restored_video = jobs.get("video_x")
    assert restored_video.kind == "video"
    assert restored_video.last_polled_status not in ("done", "failed")

    # notices: mid-flight + terminal-unseen (2), NOT the already-announced one
    assert loop.has_pending_background_notifications()
    note = loop._drain_background_notifications()
    assert "shell_midflight" in note
    assert "shell_done_unseen" in note
    assert "shell_done_seen" not in note

    # reconciled terminal state was persisted straight back to jobs.json
    reloaded = JobRegistry.load(sessions_root / sid / "jobs.json")
    assert reloaded.get("shell_midflight").last_polled_status == "failed"
    assert reloaded.get("shell_midflight").announced is True


def test_load_no_jobs_json_is_a_noop(tmp_path: Path) -> None:
    """A fresh session (no prior jobs.json) loads clean: no records, no notices,
    no crash."""
    sid = "sess_fresh"
    sessions_root = tmp_path / "sessions"
    workdir = tmp_path / "workdirs" / sid
    workdir.mkdir(parents=True, exist_ok=True)

    loop = AgentLoopV3(
        session_id=sid,
        output_dir=workdir,
        sessions_root=sessions_root,
        gemini_client=_TextStopClient(),
        emit_event=lambda _e: None,
    )

    assert loop._tool_ctx.jobs.list_records() == []
    assert not loop.has_pending_background_notifications()


# ── model-observed completion must persist (no restart resurrection) ─────────


def _register_real_shell_job(loop, workdir: Path, spawned: list, cmd: list[str]):
    """Spawn a real short-lived shell job into loop's registry + _PROCESSES and
    persist the submit-time snapshot, mirroring run_shell._submit_background."""
    tasks_dir = workdir / "tasks"
    tasks_dir.mkdir(parents=True, exist_ok=True)
    record = loop._tool_ctx.jobs.submit(
        kind="shell", provider="local:bash-raw", operation_name="pending",
        pending_asset_id="-", estimated_eta_sec=600.0, summary="printf hi",
    )
    log_path = tasks_dir / f"{record.job_id}.log"
    with open(log_path, "w", encoding="utf-8") as log_f:
        proc = subprocess.Popen(
            cmd, stdout=log_f, stderr=subprocess.STDOUT, cwd=str(workdir),
            start_new_session=True,
        )
    spawned.append(proc)
    build._PROCESSES[record.job_id] = (proc, time.monotonic() + 600.0)
    record.pid = proc.pid
    record.pgid = proc.pid
    record.started_epoch = time.time()
    loop.persist_jobs()  # submit-time snapshot (non-terminal)
    return record.job_id, proc


def test_model_observed_completion_persists_so_restart_does_not_resurrect(
    tmp_path: Path, _reap_children: list[subprocess.Popen]
) -> None:
    """Regression: when a model-facing check_job/wait_for_job observes a shell
    job finish first — setting announced=True WITHOUT persisting — the watcher
    tick must still persist the terminal state. Otherwise jobs.json stays at the
    submit-time non-terminal snapshot and a restart reconciles the SUCCEEDED job
    into a false 'failed' with a bogus completion notice."""
    sid = "sess_model_race"
    sessions_root = tmp_path / "sessions"
    workdir = tmp_path / "workdirs" / sid
    workdir.mkdir(parents=True, exist_ok=True)

    loop = AgentLoopV3(
        session_id=sid, output_dir=workdir, sessions_root=sessions_root,
        gemini_client=_TextStopClient(), emit_event=lambda _e: None,
    )
    job_id, proc = _register_real_shell_job(
        loop, workdir, _reap_children, ["/bin/sh", "-c", "printf hi; exit 0"]
    )
    proc.wait(timeout=5)  # the OS process has exited before any poll

    jobs_path = sessions_root / sid / "jobs.json"
    # Submit-time snapshot is non-terminal.
    assert JobRegistry.load(jobs_path).get(job_id).last_polled_status not in ("done", "failed")

    # Model-facing poll wins the race: marks done + announced, pops _PROCESSES,
    # but does NOT persist (the exact gap this test guards).
    res = build._check_job_impl(job_id, loop._tool_ctx)  # mark_announced=True default
    assert res["status"] == "done"
    assert loop._tool_ctx.jobs.get(job_id).announced is True
    assert JobRegistry.load(jobs_path).get(job_id).last_polled_status not in ("done", "failed")

    # The watcher tick must persist the terminal state despite announced=True.
    loop.poll_background_jobs()
    reloaded = JobRegistry.load(jobs_path)
    assert reloaded.get(job_id).last_polled_status == "done"
    assert reloaded.get(job_id).announced is True

    # A fresh session (simulated restart) restores it as done, NEVER resurrects
    # it as failed, and queues NO false-failure completion notice.
    fresh = AgentLoopV3(
        session_id=sid, output_dir=workdir, sessions_root=sessions_root,
        gemini_client=_TextStopClient(), emit_event=lambda _e: None,
    )
    assert fresh._tool_ctx.jobs.get(job_id).last_polled_status == "done"
    assert not fresh.has_pending_background_notifications()
