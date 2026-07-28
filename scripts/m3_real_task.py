"""M3 — drive a real creative task through the v3 frontend, collect evidence.

Source clip: 20s testsrc2 1920x1080 + 220Hz sine at /tmp/festival_clip.mp4
Prompt: "做一个 5 秒的开场片段：从视频中取前 5 秒,放慢到 0.5 倍速,
        在画面下方加一行标题文字「艺术节 2026」,整体调成暖色调,
        最后导出 1080p MP4"

Drives /v3 with Playwright headless chromium:
  - upload clip via setInputFiles
  - type prompt + click send
  - takes screenshots at key UI transitions
  - dumps window.__lumeriEvents (raw SSE log) at the end
  - ffprobes the final asset on disk

Writes:
  - docs/v3-A-M3-real-task.md         master report (Acrabxie-readable)
  - docs/v3-A-M3-screenshots/*.png    UI screenshots
  - docs/v3-A-M3-sse.json             raw event log
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from http.client import HTTPConnection
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

PROMPT = (
    "做一个 5 秒的开场片段：从视频中取前 5 秒,放慢到 0.5 倍速,"
    "在画面下方加一行标题文字「艺术节 2026」,整体调成暖色调,"
    "最后导出 1080p MP4"
)
CLIP = Path("/tmp/festival_clip.mp4")
SCREENSHOT_DIR = REPO_ROOT / "docs" / "v3-A-M3-screenshots"
SSE_OUT = REPO_ROOT / "docs" / "v3-A-M3-sse.json"
REPORT = REPO_ROOT / "docs" / "v3-A-M3-real-task.md"
DRIVER_DIR = Path("/tmp/v3-m3-driver")


DRIVER_JS = r"""
import { chromium } from 'playwright';
import fs from 'node:fs/promises';
import path from 'node:path';

const serverUrl = process.argv[2];
const clipPath = process.argv[3];
const promptText = process.argv[4];
const screenshotDir = process.argv[5];
const sseOut = process.argv[6];

function shot(page, name) {
  return page.screenshot({ path: path.join(screenshotDir, name), fullPage: true });
}

async function main() {
  await fs.mkdir(screenshotDir, { recursive: true });
  const browser = await chromium.launch({ headless: true });
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await ctx.newPage();

  const consoleLog = [];
  page.on('console', m => consoleLog.push(`[${m.type()}] ${m.text()}`));
  page.on('pageerror', e => consoleLog.push(`[pageerror] ${e.message}`));

  console.log('navigating to', serverUrl + '/v3');
  await page.goto(serverUrl + '/v3', { waitUntil: 'load' });

  await page.waitForFunction(() => {
    const lbl = document.querySelector('#session-id-label');
    return lbl && lbl.textContent && lbl.textContent !== '—';
  }, { timeout: 15000 });
  const sessionId = await page.$eval('#session-id-label', el => el.textContent);
  console.log('session_id =', sessionId);

  await shot(page, '01-initial-idle.png');

  console.log('uploading', clipPath);
  await page.setInputFiles('#upload-input', clipPath);
  await page.waitForFunction(() => {
    const s = document.querySelector('.upload-status');
    return s && /^uploaded as/.test(s.textContent);
  }, { timeout: 120000 });
  await shot(page, '02-after-upload.png');

  console.log('typing prompt and sending');
  await page.fill('#prompt-input', promptText);
  await page.click('#send-btn');

  await page.waitForSelector('.tool-card', { timeout: 60000 });
  await shot(page, '03-first-tool-card.png');

  // Take a screenshot whenever a new tool card transitions to 'running'
  // or 'done', and a periodic snapshot every 8s as a fallback.
  const startTs = Date.now();
  let lastShot = Date.now();
  let lastDoneCount = 0;
  let stepCount = 4;
  let completed = false;
  const maxWait = 300_000;

  while (Date.now() - startTs < maxWait) {
    const snapshot = await page.evaluate(() => {
      const btn = document.querySelector('#send-btn');
      const cards = Array.from(document.querySelectorAll('.tool-card .tool-status'))
        .map(s => s.className.replace('tool-status ', ''));
      const doneCount = cards.filter(c => c === 'done').length;
      const assets = Array.from(document.querySelectorAll('.asset-card.final')).length;
      const banners = Array.from(document.querySelectorAll('.banner')).map(b => b.className);
      const composerReady = btn && !btn.disabled;
      return { composerReady, doneCount, assets, banners, cards };
    });

    if (snapshot.doneCount > lastDoneCount) {
      await shot(page, `${String(stepCount).padStart(2,'0')}-after-tool-${snapshot.doneCount}.png`);
      lastDoneCount = snapshot.doneCount;
      lastShot = Date.now();
      stepCount++;
    } else if (Date.now() - lastShot > 8000) {
      await shot(page, `${String(stepCount).padStart(2,'0')}-progress.png`);
      lastShot = Date.now();
      stepCount++;
    }

    completed = snapshot.composerReady;
    if (completed) break;
    await new Promise(r => setTimeout(r, 500));
  }

  // small wait for any trailing text deltas after composer re-enables
  await new Promise(r => setTimeout(r, 1500));
  await shot(page, `${String(stepCount).padStart(2,'0')}-final.png`);

  console.log('turn completed =', completed);

  const finalState = await page.evaluate(() => {
    const cards = Array.from(document.querySelectorAll('.tool-card')).map(c => ({
      tool: c.querySelector('.tool-name')?.textContent || '',
      status: c.querySelector('.tool-status')?.textContent || '',
      args: c.querySelector('.tool-args')?.textContent || '',
      summary: c.querySelector('.tool-summary')?.textContent || '',
      error: c.querySelector('.tool-error')?.textContent || '',
      preview: c.querySelector('.tool-preview-link')?.getAttribute('href') || '',
    }));
    const assistant = Array.from(document.querySelectorAll('.assistant-bubble'))
      .map(b => b.textContent).join('\n---\n');
    const assets = Array.from(document.querySelectorAll('.asset-card')).map(c => ({
      meta: c.querySelector('.asset-meta')?.textContent.trim() || '',
      final: c.classList.contains('final'),
      src: c.querySelector('video, img')?.getAttribute('src') || '',
    }));
    const banners = Array.from(document.querySelectorAll('.banner')).map(b => ({
      cls: b.className,
      text: b.textContent,
    }));
    const sessionId = document.querySelector('#session-id-label')?.textContent || '';
    return { sessionId, cards, assistant, assets, banners };
  });

  const rawEvents = await page.evaluate(() => window.__lumeriEvents || []);
  await fs.writeFile(sseOut, JSON.stringify({ finalState, events: rawEvents, consoleLog }, null, 2));

  console.log('events captured:', rawEvents.length);
  console.log('final state cards:', finalState.cards.length, '/ assets:', finalState.assets.length);

  await browser.close();
}

main().catch(err => { console.error('DRIVER FAILED:', err); process.exit(1); });
"""


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def wait_ready(host, port, timeout=45):
    deadline = time.monotonic() + timeout
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            c = HTTPConnection(host, port, timeout=1)
            c.request("GET", "/health", headers={"Host": f"{host}:{port}"})
            r = c.getresponse()
            r.read()
            if r.status == 200:
                return
        except Exception as e:
            last = e
        time.sleep(0.3)
    raise RuntimeError(f"server not ready: {last!r}")


def ensure_driver_dir() -> None:
    DRIVER_DIR.mkdir(parents=True, exist_ok=True)
    pkg = DRIVER_DIR / "package.json"
    if not pkg.exists():
        pkg.write_text('{"name":"v3-m3-driver","version":"1.0.0","type":"module"}\n')
    if not (DRIVER_DIR / "node_modules" / "playwright").exists():
        print(f"[setup] installing playwright in {DRIVER_DIR}", flush=True)
        subprocess.run(["npm", "install", "playwright"], cwd=DRIVER_DIR,
                       capture_output=True, check=True)
    (DRIVER_DIR / "driver.mjs").write_text(DRIVER_JS, encoding="utf-8")


def ffprobe(path: Path) -> dict:
    out = subprocess.check_output([
        "ffprobe", "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", str(path),
    ], text=True)
    return json.loads(out)


def main() -> int:
    if not CLIP.exists():
        raise RuntimeError(f"source clip missing: {CLIP}")
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    SSE_OUT.parent.mkdir(parents=True, exist_ok=True)
    ensure_driver_dir()

    port = free_port()
    work_root = Path(tempfile.mkdtemp(prefix="v3-A-M3-"))
    print(f"[1/5] server on port {port}, output_root={work_root}", flush=True)
    server = subprocess.Popen(
        ["python3", "server.py", "--port", str(port), "--host", "127.0.0.1"],
        cwd=str(REPO_ROOT),
        env={**os.environ, "LUMERI_V3_OUTPUT_ROOT": str(work_root)},
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )

    try:
        wait_ready("127.0.0.1", port)

        print(f"[2/5] running browser driver (Playwright headless)", flush=True)
        r = subprocess.run(
            ["node", str(DRIVER_DIR / "driver.mjs"),
             f"http://127.0.0.1:{port}",
             str(CLIP),
             PROMPT,
             str(SCREENSHOT_DIR),
             str(SSE_OUT)],
            capture_output=True, text=True, timeout=360,
        )
        print(r.stdout)
        if r.returncode != 0:
            print(r.stderr)
            raise RuntimeError(f"driver returncode={r.returncode}")

        print(f"[3/5] reading captured state", flush=True)
        captured = json.loads(SSE_OUT.read_text())
        final_state = captured["finalState"]
        events = captured["events"]
        sid = final_state.get("sessionId", "?")

        print(f"[4/5] locating final asset on disk", flush=True)
        workdir = work_root / "workdirs" / sid
        final_aid = None
        for c in final_state["cards"]:
            if "export" in c["tool"]:
                href = c["preview"]
                if href:
                    final_aid = href.rsplit("/", 1)[-1]
        if final_aid is None and final_state["assets"]:
            last_final = [a for a in final_state["assets"] if a.get("final")]
            target = (last_final or final_state["assets"])[-1]
            final_aid = (target["src"].rsplit("/", 1)[-1] or "").split("?")[0]
        final_mp4 = (workdir / f"{final_aid}.mp4") if final_aid else None
        probe = ffprobe(final_mp4) if (final_mp4 and final_mp4.exists()) else None

        print(f"[5/5] writing master report", flush=True)
        write_report(final_state, events, probe, final_mp4, port, sid, work_root)
        print(f"\nreport written: {REPORT}", flush=True)
        return 0
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()


def write_report(final_state, events, probe, final_mp4, port, sid, work_root):
    by_kind: dict[str, int] = {}
    for ev in events:
        by_kind[ev.get("kind", "?")] = by_kind.get(ev.get("kind", "?"), 0) + 1

    with REPORT.open("w", encoding="utf-8") as f:
        f.write(f"# Lumeri v3-A.M3 — real creative task evidence\n\n")
        f.write(f"- generated: {datetime.now(timezone.utc).isoformat()}\n")
        f.write(f"- source clip: `{CLIP}` (20s testsrc2 1920x1080 + 220Hz sine)\n")
        f.write(f"- session id: `{sid}`\n")
        f.write(f"- server port: {port}\n")
        f.write(f"- output root: `{work_root}`\n\n")

        f.write(f"## Prompt\n\n> {PROMPT}\n\n")

        f.write(f"## SSE event totals (captured client-side from `window.__lumeriEvents`)\n\n")
        for k, v in sorted(by_kind.items()):
            f.write(f"- `{k}`: {v}\n")
        f.write(f"\nTotal events: **{len(events)}**\n\n")

        f.write(f"## Tool calls\n\n")
        for i, c in enumerate(final_state["cards"], 1):
            f.write(f"### {i}. `{c['tool']}` — `{c['status']}`\n\n")
            if c["args"]:
                f.write(f"```json\n{c['args']}\n```\n\n")
            if c["summary"]:
                f.write(f"summary: {c['summary']}\n\n")
            if c["error"]:
                f.write(f"**error:** {c['error']}\n\n")
            if c["preview"]:
                f.write(f"preview: `{c['preview']}`\n\n")

        if final_state.get("assistant"):
            f.write(f"## Model's user-facing reply\n\n")
            f.write(f"```\n{final_state['assistant']}\n```\n\n")

        if final_state.get("banners"):
            f.write(f"## Banners (errors / budget gates)\n\n")
            for b in final_state["banners"]:
                f.write(f"- `{b['cls']}`: {b['text']}\n")
            f.write("\n")

        f.write(f"## Final assets\n\n")
        for a in final_state["assets"]:
            mark = " **FINAL**" if a.get("final") else ""
            f.write(f"- `{a['src']}`{mark} — {a['meta']}\n")
        f.write("\n")

        f.write(f"## ffprobe of final asset on disk\n\n")
        if probe:
            vs = next((s for s in probe["streams"] if s["codec_type"] == "video"), None)
            if vs:
                f.write(f"- file: `{final_mp4}` ({Path(final_mp4).stat().st_size} bytes)\n")
                f.write(f"- video: **{vs['width']}x{vs['height']} {vs['codec_name']}**, duration `{vs.get('duration', '?')}`s\n")
                af = next((s for s in probe["streams"] if s["codec_type"] == "audio"), None)
                if af:
                    f.write(f"- audio: {af['codec_name']}, {af.get('channels', '?')}ch, duration `{af.get('duration', '?')}`s\n")
        else:
            f.write(f"- final asset not found on disk\n")
        f.write("\n")

        f.write(f"## Screenshots\n\n")
        shots = sorted(Path(SCREENSHOT_DIR).glob("*.png"))
        for s in shots:
            rel = s.relative_to(REPO_ROOT)
            f.write(f"- `{rel}` ({s.stat().st_size} bytes)\n")
        f.write("\n")

        f.write(f"## Raw event log\n\n")
        f.write(f"Full log at `docs/v3-A-M3-sse.json`. First and last 5 events:\n\n")
        f.write("```json\n")
        for ev in events[:5]:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")
        if len(events) > 10:
            f.write(f"... ({len(events) - 10} more events) ...\n")
        for ev in events[-5:]:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")
        f.write("```\n")


if __name__ == "__main__":
    sys.exit(main())
