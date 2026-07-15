# Lumeri v3-A branch — what changed and how to use it

> Branch: `claude/jolly-clarke-JO7E3`
> Target audience: Acrab a month from now, or anyone picking this up
> from a cold start.

---

## 简介

This branch turns Lumeri's "v3" agent loop — built in a separate
phase as `claude/jolly-clarke-JO7E3` *up through v3-alive* — into
something you can actually drive from a browser, end to end. The v2
flow (`gemia/agent_workflow.py`, 4573 lines) was a one-shot Plan-JSON
generator followed by Python pretending to narrate live agent
activity with hardcoded Chinese status strings. The model spoke once
and never again. The "progress" the UI showed was theatre.

v3 (already on this branch before A) replaced that with a real
function-calling agent loop: Gemini 3.1 Pro on OpenRouter, SSE
streaming, 5 ffmpeg verb dispatchers, real ffprobe progress, no
host-synthesized status text. A.M1 through A.M3 (this batch) adds
the surface around it — HTTP endpoints (create session, submit turn,
upload asset, fetch asset with Range, SSE stream with reconnect),
per-session worker threads, and a vanilla HTML/JS frontend at
`/v3`. It's the smallest amount of code that gets the v3 loop into a
user's hands.

What you can do today: upload a video, type a Chinese prompt, watch
the model pick verbs and execute them, see real ffmpeg progress,
play the resulting MP4 in-browser. What you cannot do: use the 10
not-yet-implemented verbs (generate_image / generate_video / etc.),
resume a session after a process restart, or expose this directly to
the public internet without auth/quota/monitoring. The v2 Tauri UI
at `/` is untouched and still works; v3 lives at `/v3` as a parallel
surface.

---

## 架构变更

### 旧 (v2, still present, untouched)

```
User prompt
   │
   ▼
gemia/agent_workflow.run_agent_workflow() (4573 lines)
   │
   ├─ ONE call to Gemini → JSON plan
   │
   ├─ Python executes plan (no model in the loop)
   │
   └─ Python emits hardcoded Chinese status strings as if narrating
      ("我先确认这次要处理什么素材...")
```

### 新 (v3-alive + A)

```
Browser /v3 (vanilla HTML+JS, native EventSource)
   │
   ▼ POST /sessions, /turn, /assets       ▲ SSE: id:N\ndata:{...}\n\n
server.py _Handler (ThreadingHTTPServer)─┘
   │
   ▼ try_handle(self, method=…)  (one dispatch call per verb)
gemia/v3_routes.py
   │
   ▼
gemia/session_manager.SessionManager
   │   dict[session_id → SessionRunner]
   ▼
SessionRunner  (one daemon thread + one asyncio loop per session)
   │
   ▼ run_coroutine_threadsafe(agent.run_turn(msg))
gemia/agent_loop_v3.AgentLoopV3.drive_turn()
   │
   ├─► gemia/gemini_client.GeminiClientV3.stream_turn(messages, tools)
   │      real SSE from OpenRouter, parses chunked function-calling
   │      deltas: text_delta, tool_call_start, tool_call_args_delta,
   │      finish.
   │
   ├─► gemia/tools/DISPATCHER[tool_name](args, ctx)
   │      ctx is a per-session ToolContext with AssetRegistry +
   │      output_dir + progress callback.
   │      Real handlers: analyze_media, edit_video, color_grade,
   │      add_overlay, export. Stubs (10): NotImplementedError.
   │      Each real handler invokes ffmpeg via Popen + parses
   │      out_time_us from stdout for true progress; emits via the
   │      progress callback the loop wires in.
   │
   └─► self._emit({"kind": "...", ...})  for every visible event
              │
              ▼
gemia/transport/sse.REGISTRY.emit(session_id, event)
   monotonic per-session event_id, append to 200-entry ring buffer,
   notify the stream condition variable.
              │
              ▼ consumed by
iter_events(session_id, last_event_id=N) in /sessions/{id}/stream
   replays buffer (id > N), emits replay_gap if N fell off the ring,
   then waits for new events.
              │
              ▼ SSE chunks back to Browser EventSource
              │
              ▼ browser persists lastEventId and reconnects with
                ?last_event_id=N.
```

### What's different in one sentence

The model is in the loop on every step; progress is the FFmpeg's own
clock; the host narrates nothing.

---

## 代码组织

Read order if you're approaching the agent loop cold (top is most
important):

| File | One-line job | Read priority |
|---|---|---|
| `gemia/agent_loop_v3.py` | The loop. `drive_turn` is the heart — read it. | **core** |
| `gemia/tools/__init__.py` | Maps verb name → async dispatcher. 5 real, 10 stub. | **core** |
| `gemia/tools/_schema.py` | All 15 verb schemas (JSON sent to Gemini). Pure data. | **core** |
| `gemia/tools/_context.py` | `AssetRegistry` (per-session, asset_id → path), `ToolContext`, `ProgressUpdate`. | **core** |
| `gemia/transport/sse.py` | Per-session 200-entry ring buffer + condition-backed SSE replay. | **core** |
| `gemia/gemini_client.py` | Streams from OpenRouter with `stream:true`, reassembles fragmented tool-call args. | **core** |
| `gemia/budget_guard.py` | Per-session $ and time cap. Returns `needs_approval`; model decides. | **core** |
| `gemia/session_manager.py` | `SessionRunner` (thread + asyncio loop per session) and `SessionManager` (process-wide dict). | **core** |
| `gemia/v3_routes.py` | All HTTP endpoints. `try_handle` is the single entrypoint server.py calls. | **core** |
| `gemia/prompts/system_v3.md` | The system prompt. Creative-collaborator framing. Has `{{asset_registry}}` and `{{pinned_intent}}` placeholders. | **core** |
| `gemia/tools/_ffmpeg.py` | `run_ffmpeg_with_progress` (Popen + `-progress pipe:1` parsing), `ffprobe_metadata`. | edge |
| `gemia/tools/edit_video.py` | trim / concat / reverse / speed via the ffmpeg helper. | edge |
| `gemia/tools/color_grade.py` | Named looks (warm/cool/vintage/cinematic/teal_orange/neutral) + intensity blend. | edge |
| `gemia/tools/add_overlay.py` | text (drawtext) / image (overlay) / subtitle. Needs libfreetype-enabled ffmpeg. | edge |
| `gemia/tools/analyze_media.py` | ffprobe + 512×512 thumbnail. Returns `thumbnail_for_next_message=True` to trigger Plan-B visual feedback. | edge |
| `gemia/tools/export.py` | mp4/mov/webm/gif × 4k/1080p/720p/480p/draft × 6 platform presets. | edge |
| `static/v3/index.html` | Page skeleton. 1.5 KB. | edge |
| `static/v3/v3.js` | The frontend. 11 event handlers, no silent drop. ~400 lines. | edge |
| `static/v3/v3.css` | All styling. ~250 lines. | edge |
| `scripts/smoke_*.py` | Per-component smoke tests. Read when one breaks. | reference |
| `scripts/milestone_v3.py` | v3-alive end-to-end without HTTP. | reference |
| `scripts/integration_v3_m1.py` | A.M1 HTTP integration test (real server, real Gemini). | reference |
| `scripts/m3_real_task.py` | A.M3 real task driver (Playwright headless). | reference |

### Server-side modifications to v2 files

| File | What | Net lines |
|---|---|---|
| `server.py` | Two `try_handle` dispatchers in `_handle_get_like` + `do_POST`; static `/v3` route. Nothing else touched. | +24 |

### v2 assets we deliberately did NOT touch (still loadable)

| File | Lines | Why kept |
|---|---:|---|
| `gemia/agent_workflow.py` | 4573 | Rollback path. Still serves `/` UI via opencode_compat routes. |
| `gemia/orchestrator.py` | 674 | v2 dispatcher; called by agent_workflow. |
| `gemia/opencode_compat.py` | 509 | OpenCode-shape compat layer that the Tauri 7788 UI talks to. |
| `gemia/runtime_vnext.py` | 766 | Earlier vNext sandbox attempt. |
| `gemia/creative_sandbox*.py` | 1502 | Sandbox layer. |
| `gemia/stability.py` | 196 | stability_gate. |
| `lumerai/sandbox.py` | 276 | AST blacklist sandbox. |
| `gemia/agent_loop.py` | 297 | Earlier (non-v3) agent loop experiment. |
| Total kept untouched | **~8793** | All survive `git checkout main`. |

### v3 production code total

| Bucket | Lines |
|---|---:|
| Core agent loop | 1287 (agent_loop_v3 + gemini_client + budget_guard + system_v3.md) |
| Tools layer | 1413 (5 dispatchers + schema + context + ffmpeg helper) |
| HTTP/session/transport | 671 (v3_routes + session_manager + sse) |
| Frontend | 777 (index.html + v3.js + v3.css) |
| `server.py` net additions | 24 |
| Total v3 production | **~4172** |

Plus ~1500 lines of test/integration scripts and ~1000 lines of
evidence logs/docs.

---

## 怎么跑

### One-time setup

1. `~/.gemia/config.json` must contain at least
   `{"openrouter_api_key": "sk-or-v1-...", "proxy": "http://127.0.0.1:7890"}`
   (proxy optional but recommended in the user's location).
2. ffmpeg must be the homebrew-ffmpeg/ffmpeg tap build (8.1.1+), not
   the standard homebrew formula — the latter lacks `libfreetype`,
   which breaks `add_overlay`. Verify:
   `ffmpeg -filters 2>&1 | grep drawtext` should return a line.
3. Python deps: standard lib only for the agent loop. ffprobe/ffmpeg
   must be on PATH.

### Run

```sh
cd /path/to/lumeri
python3 server.py --port 7788 --host 127.0.0.1
```

Then open `http://127.0.0.1:7788/v3` in any modern browser (Chrome,
Safari, the Tauri app's webview).

A new session is created automatically on page load. The UI shows
the session_id, a `live` connection pill, an upload button, and a
prompt textarea. Drop a clip, type what you want, click `send`.

### 5-minute reproduction (Acrab cold start)

If you forget how it works:

```sh
cd /path/to/lumeri
git checkout claude/jolly-clarke-JO7E3
python3 scripts/m3_real_task.py
```

This spawns the server on a free port, generates a test clip,
drives the UI via headless Playwright, and writes
`docs/v3-A-M3-real-task.md` + screenshots. If that report shows
"turn completed = true" and the ffprobe of v_006 is 1920×1080, the
whole pipeline is healthy.

### Configuration knobs

| Env var | Default | Effect |
|---|---|---|
| `OPENROUTER_API_KEY` | (config file) | Overrides `~/.gemia/config.json` key. |
| `LUMERI_V3_MODEL` | `google/gemini-3.1-pro-preview` | Which OpenRouter model. |
| `OPENROUTER_PROXY` | (config file) | HTTP proxy. |
| `LUMERI_V3_OUTPUT_ROOT` | `/tmp/lumeri-v3` | Where session workdirs + meta live. |
| `LUMERI_V3_UPLOAD_MAX_BYTES` | `524288000` (500 MiB) | Upload size cap. |
| `LUMERI_V3_UPLOAD_TIMEOUT_SEC` | `60` | Socket read timeout while receiving an upload. |
| `LUMERI_V3_MAX_SESSIONS` | `20` | Process-wide active or creating v3 session cap. |
| `LUMERI_V3_IDLE_TIMEOUT_SEC` | `7200` | Idle session TTL; `0` disables idle cleanup. |
| `LUMERI_V3_SWEEP_INTERVAL_SEC` | `60` | Background idle sweeper interval; `0` disables the sweeper thread. |
| `LUMERI_V3_KEEP_CLOSED_WORKDIRS` | unset | Set to `1` to keep idle-closed workdirs for debugging. |
| `LUMERI_PORT` / `GEMIA_PORT` | `7788` | Server port. |

---

## 已知问题

### Fixed in Codex post-review hardening

These were found during `codex exec review --base main` plus a manual pass,
then fixed in this branch before handoff:

- **F4 [P1]** Session OOM/disk leak: fixed with
  `LUMERI_V3_MAX_SESSIONS`, `last_used_at`, idle sweeper, optional
  workdir cleanup, and 503 when the cap is reached.
- **F5 [P1]** Overlapping turns in one session: fixed with a per-session
  in-progress guard; direct concurrent `/turn` calls now get 409.
- **F6 [P2]** Silent SSE replay overflow: fixed with `replay_gap`.
  The frontend refreshes `/sessions/{id}` and reconnects with
  `?last_event_id=N`.
- **F7 [P2]** Range boundaries: fixed for suffix ranges, open-ended
  ranges, EOF clamping, invalid ranges, and multi-range full-body fallback.
- **F8 [P2]** `/v3` static path guard: fixed with resolved
  `relative_to()` containment, not string-prefix checks.
- **F9 [P2]** Local path leakage: fixed by scrubbing `*_path`, `path`,
  and `preview_uri` from model/SSE-visible tool results; frontend uses
  `/sessions/{id}/assets/{asset_id}` only.
- **F10 [P2]** Malformed `Content-Length` 500: fixed with 400 responses
  and a socket read timeout for uploads. The 500 MiB cap still rejects
  oversized honest uploads before reading the body.
- **F11 [P2]** Silent-video speed/reverse: fixed by probing for audio and
  using video-only FFmpeg graphs with `-an` when needed.
- **F12 [P2]** Image overlay coordinates: fixed by using overlay
  `W/H/w/h` variables instead of drawtext `text_w/text_h`.
- **F13 [P2]** Multi-output final semantics: fixed by emitting
  `deliverable_asset_ids` and `intermediate_asset_ids`; frontend marks
  every deliverable final.
- **F14 [P2]** GIF export kind: fixed by registering GIF exports as
  `kind="image"` and rendering from result/registry kind.
- **F15 [P2]** `color_grade` schema mismatch: fixed by narrowing the
  schema description to video until image grading exists.

### Still open

- **A3** `SessionRunner.close()` cancels pending loop tasks and then joins
  the daemon thread for up to 5s. That is acceptable for local dev, but a
  production daemon still needs process-level job supervision for stuck
  FFmpeg children.
- **A5** The 10 stub verbs are still visible to the model. If Gemini calls
  `generate_video`, the user sees a real red tool error. Decide next
  whether to hide stub schemas or implement the verbs.
- **A7** `/v3` still auto-creates a fresh session on page load. Session
  cleanup now exists, but there is no "resume existing live session" UX.
- **A8** There is still no entry point from the main Tauri UI at `/` into
  `/v3`. Acrab has to know the URL.
- **Production hardening** Auth, per-user quota, durable session store,
  request logging, and external process supervision are still absent.

---

## 回滚路径

### Switch back to v2 (zero v3 dependency)

```sh
git checkout main
python3 server.py
```

The `claude/jolly-clarke-JO7E3` branch is additive on the codebase
side: it adds `/sessions` and `/v3` routing without removing the old
v2 routes. Every v2 file in the "kept untouched" table above is
intended to remain behaviorally unchanged.

### Read a v3 session file using v2 code

Verified in `docs/v3-alive-evidence.log` and re-verified at the time
of writing this doc:

```sh
python3 -c "
from gemia.session_store import SessionStore
from pathlib import Path
s = SessionStore(Path('/tmp/lumeri-v3/sessions'))
import json
for sid_dir in s.root.iterdir():
    meta = json.loads(s.meta_path(sid_dir.name).read_text())
    print(meta)
"
```

v3 writes `meta.json` with all v2-`SessionStore`-expected fields
plus `loop_version: "v3"` (which v2 ignores).

### What's actually safe to delete later, when no longer rolling back

Once you trust v3 fully (months of real use), the legacy-deletion
candidates are documented in the original v3 spec:

- `gemia/agent_workflow.py` → move to `gemia/legacy/`
- `gemia/orchestrator.py` (slim to thin facade or remove)
- `gemia/opencode_compat.py` (kept while old UI at `/` is in use;
  remove after v3 fully replaces the main UI)
- `gemia/creative_sandbox*.py`, `lumerai/sandbox.py`,
  `gemia/runtime_vnext.py`, `gemia/stability.py`,
  `gemia/agent_loop.py`

Total deletable when you cut over: ~8793 lines.

---

## 下一步建议方向

You asked for my honest take, including whether I disagree with the
"M4 polish 不开,等用户反馈" decision. Here it is.

### Where I agree with you

M4 polish ("fix UI nits, refine status text, prettify") **is** a
trap right now. Acrab hasn't actually used the thing yet. Polish
needs feedback from real use, not me guessing.

### Where I disagree

There's a category of work that isn't "polish" but also isn't a new
milestone — it's **what stands between Acrab and actually using
v3**. Without it, you'll never get the feedback that justifies M4.
Specifically:

1. **A8 (no link from main UI to /v3) is the #1 thing.** Right now
   the v3 page is reachable only if you type `/v3` into the URL bar.
   Acrab will open the Tauri app, see the old UI, and either use
   that (and not exercise v3) or close the app frustrated. Adding a
   single button or tab in the existing nav that opens `/v3` is
   ~5 lines of edit in the Tauri main app — not a milestone, just
   a wiring fix. Do this before anything else.

2. **A7 (no live-session resume UX) is next.** The backend now has
   caps and idle cleanup, but refresh still creates a new session.
   Add a session picker or "reconnect to last session" path before
   asking Acrab to run long jobs.

3. **A5 (stub verbs) should be decided explicitly.** Either hide the
   10 unimplemented schemas for the first user test, or implement the
   cheap ones first. Letting the model call red-card stubs makes the
   product feel broken even when the transport is healthy.

That is not M4 polish. It is "make the entry path usable enough to
collect real feedback."

### After Acrab starts using it

Then wait for feedback. The first real-use bugs will tell you what
M4 should be. My guess based on the code: it'll be about stub-verb
expectations, missing live-session resume, or the gap between `/v3`
and the mature `/` editing UI. Don't guess; let him tell you.

### When (eventually) doing milestone B (15-verb expansion)

When you do scale to 15 verbs, the most impactful additions are
`generate_image` (Nano Banana — cheap, fast, opens the "give me a
title card" use case) and `arrange_timeline` (multi-clip composition
— the unlock for actual editing workflows). `generate_video` (Veo)
is the most expensive, slowest, and least likely to be needed for
the festival-video case. Build the cheap visual ones first.

### When (eventually) cleaning legacy (option C)

Don't do this until v3 fully replaces the `/` Tauri UI. Otherwise
you break the rollback path that keeps you sleeping at night.

---

*Generated on 2026-05-27 after A.M3 passed. Last reviewed and hardened
by Codex at commit `ee3c433` plus the current uncommitted fix batch.*
