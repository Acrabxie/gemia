"""End-to-end milestone D for Lumeri v3 — drives the loop and captures evidence.

Generates /tmp/clip.mp4 (10s testsrc + 440Hz sine), creates an
AgentLoopV3, registers the clip as the first asset, sends the
milestone prompt, and captures every SSE event into
docs/v3-alive-evidence.log. After turn_complete, ffprobes each
final asset and appends the report.

No HTTP server in the loop — the test reads through the same
gemia.transport.sse.iter_events generator that server.py /sessions/
{id}/stream uses, so the wire-up the route exercises is identical.
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gemia.agent_loop_v3 import AgentLoopV3  # noqa: E402
from gemia.transport.sse import REGISTRY, iter_events  # noqa: E402


WORK = Path("/tmp/lumeri-v3-milestone")
CLIP = Path("/tmp/clip.mp4")
PROMPT = "把 /tmp/clip.mp4 前 5 秒裁掉,加暖色调,导出 1080p"
EVIDENCE_LOG = Path(__file__).resolve().parent.parent / "docs" / "v3-alive-evidence.log"


def generate_test_clip(out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "lavfi", "-i", "testsrc=duration=10:size=1280x720:rate=30",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=10",
        "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k",
        "-shortest",
        str(out),
    ]
    subprocess.run(cmd, check=True, capture_output=True)


def ffprobe_full(path: Path) -> str:
    cmd = [
        "ffprobe", "-v", "error",
        "-print_format", "json",
        "-show_format", "-show_streams",
        str(path),
    ]
    return subprocess.check_output(cmd, text=True)


def main() -> int:
    WORK.mkdir(parents=True, exist_ok=True)
    EVIDENCE_LOG.parent.mkdir(parents=True, exist_ok=True)
    session_id = f"milestone-v3-{int(time.time())}"
    session_workdir = WORK / session_id
    sessions_root = WORK / "sessions"

    print(f"generating test clip → {CLIP}", flush=True)
    generate_test_clip(CLIP)
    print(f"clip size: {CLIP.stat().st_size} bytes", flush=True)

    REGISTRY.register(session_id)

    log = EVIDENCE_LOG.open("w", encoding="utf-8")
    log.write(f"# Lumeri v3 milestone evidence — session {session_id}\n")
    log.write(f"# generated:  {datetime.now(timezone.utc).isoformat()}\n")
    log.write(f"# prompt:     {PROMPT}\n")
    log.write(f"# input clip: {CLIP} ({CLIP.stat().st_size} bytes)\n")
    log.write(f"# sessions_root: {sessions_root}\n\n")
    log.flush()

    agent_error: list[BaseException] = []

    def run_agent() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            agent = AgentLoopV3(
                session_id=session_id,
                output_dir=session_workdir,
                sessions_root=sessions_root,
            )
            asset_id = agent.add_external_asset(
                CLIP, summary=f"user-provided 10s test clip ({CLIP.name})"
            )
            print(f"registered input as {asset_id}", flush=True)
            loop.run_until_complete(agent.run_turn(PROMPT))
        except BaseException as exc:  # capture for main thread; do not silence
            agent_error.append(exc)
        finally:
            REGISTRY.close(session_id)
            loop.close()

    print(f"spawning agent thread for session {session_id}", flush=True)
    t0 = time.monotonic()
    thread = threading.Thread(target=run_agent, daemon=False)
    thread.start()

    event_count = 0
    final_asset_ids: list[str] = []
    saw_turn_complete = False

    for chunk in iter_events(session_id):
        event_count += 1
        text = chunk.decode("utf-8")
        ts = time.monotonic() - t0
        log.write(f"# t+{ts:7.3f}s\n")
        log.write(text)
        log.flush()
        line = text.strip()
        if line.startswith("data:"):
            try:
                ev = json.loads(line[5:].strip())
                kind = ev.get("kind")
                if kind == "turn_complete":
                    saw_turn_complete = True
                    final_asset_ids = ev.get("final_asset_ids", []) or []
                elif kind == "turn_error":
                    print(f"  ⚠ turn_error: {ev.get('error')}", flush=True)
            except json.JSONDecodeError:
                pass
            print(f"  [{ts:7.3f}s] event #{event_count}: {line[:120]}", flush=True)

    thread.join(timeout=15)
    elapsed = time.monotonic() - t0

    log.write(f"\n# ──────────────────────────────────────────────────────────\n")
    log.write(f"# total SSE events: {event_count}\n")
    log.write(f"# elapsed: {elapsed:.2f}s\n")
    log.write(f"# saw turn_complete: {saw_turn_complete}\n")
    log.write(f"# final_asset_ids: {final_asset_ids}\n")
    if agent_error:
        log.write(f"# AGENT THREAD ERROR: {type(agent_error[0]).__name__}: {agent_error[0]}\n")

    log.write("\n# ── ffprobe of final assets ───────────────────────────────\n")
    for aid in final_asset_ids:
        candidates = [
            c for c in session_workdir.glob(f"{aid}.*")
            if c.suffix not in (".txt", ".png") and not c.name.endswith(".concat.txt")
        ]
        if not candidates:
            log.write(f"\n## {aid}: no file found in {session_workdir}\n")
            continue
        target = candidates[0]
        log.write(f"\n## {aid} → {target} ({target.stat().st_size} bytes)\n")
        try:
            log.write(ffprobe_full(target))
        except subprocess.CalledProcessError as exc:
            log.write(f"ffprobe failed: exit {exc.returncode}\n")

    log.close()
    print(f"\nevidence log written: {EVIDENCE_LOG}", flush=True)

    if agent_error:
        raise agent_error[0]
    return 0


if __name__ == "__main__":
    sys.exit(main())
