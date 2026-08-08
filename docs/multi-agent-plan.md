# Multi-Agent Capability v1 — Bounded Subtask Fan-Out (`spawn_subtasks`)

状态: 已定稿，待实施（Phase 1 未开始）
制定日期: 2026-07-06
适用范围: `/Volumes/Extreme SSD/gemia`（后端 + web v3）与 `~/Code/lumeri-cli`（TUI 客户端）
风格基准: `docs/timeline-canonical-plan.md` / `docs/protocol-parity-plan.md`
先读: `gemia/agent_loop_v3.py`, `gemia/budget_guard.py`, `gemia/session_manager.py`,
`gemia/transport/sse.py`, `gemia/v3_contract.py`, `gemia/plan_mode.py`,
以及搁浅分支提交 `d7f941c`（`git show d7f941c:gemia/budget_guard.py`）

---

## 0. Decisions locked (quick index)

| # | Decision | Verdict |
|---|----------|---------|
| D1 | Product shape | **Bounded sub-task fan-out via ONE host tool `spawn_subtasks`** — at most 4 direct children per call; children can ask the root for internal guidance but cannot fan out again. Legacy profiles remain opt-in; full profile exposes the parent's project-scoped read/write/edit surface. |
| D2 | Child architecture | **New lightweight `SubtaskLoop` class** (AgentLoopV3-lite in new file `gemia/subtasks.py`), NOT a second `AgentLoopV3` instance, NOT tool-batch-only. Shares parent's `GeminiClientV3`, `AssetRegistry`, `JobRegistry`, `ProjectHandle` (reads only in P1/P2 profiles). |
| D3 | Concurrency | **`asyncio` tasks on the session's existing single event loop.** No new threads, ever. |
| D4 | Budget | **Children share the PARENT session `BudgetGuard`**. No per-child budget slice or fresh budget pool is created; existing paid-media production policy remains authoritative. |
| D5 | Protocol | **Two new SSE kinds only: `subagent_start`, `subagent_result`.** No `subagent_progress` kind — child activity rides EXISTING `tool_exec_*` kinds with a new OPTIONAL `agent_id` field. Contract (`gemia/v3_contract.py`) updated FIRST, both frontends + drift tests same commit. |
| D6 | Child text | Child `model_text_delta` is **never emitted to SSE**. Child final text folds into the structured result `summary`. |
| D7 | Plan mode | `spawn_subtasks` classified **BLOCKED** in `PLAN_BLOCKED_TOOLS`; children additionally re-read the parent's live `plan_mode` flag per dispatch (defense in depth for mid-turn toggles). |
| D8 | Mutation rule | **Full-profile children may read/write/edit the project through the existing project-scoped tools.** Project journals and the single event loop remain the write-coordination mechanism. |
| D9 | Rails | Max 4 children per call; max depth 1; no `elicit` in any child profile; child-only `ask_root_agent`; optional caller step/deadline values; per-child doom-loop + failure-nudge state; cancellation via task-tree `try/finally` + existing `_cancel_pending`. |
| D10 | Phasing | P1 = annotate/probe fan-out (fixed read+annotate profile). P2 = per-beat assembly workers (needs `docs/outline-editing-plan.md`). P3 = A/B render variants. |

中文小结：产品形态是"有界子任务扇出"，一个新 host 工具 `spawn_subtasks`；每批最多 4 个
直接子代理，子代理是轻量循环（不是完整 AgentLoopV3），跑在会话已有的单事件循环上；
预算共享父会话的守卫；子代理可通过 `ask_root_agent` 请求 root 内部指导，但不能继续 spawn
或向用户提问；full profile 子代理可以通过项目范围工具读写和修改项目。

---

## 1. Problem and product shape

### 1.1 What the video domain actually needs

Today `AgentLoopV3._drive_turn` dispatches every tool call **sequentially**
(`gemia/agent_loop_v3.py:1154` — `for tc in accum.tool_calls:`). Four workloads are
embarrassingly parallel and dominated by wall-clock waits (ffmpeg, ffprobe, Gemini
vision, Veo polling), so serialization is pure user-visible latency:

1. **Parallel media annotation / indexing** — running the Gemini-vision annotation
   pass of `docs/semantic-search-media-plan.md` §2–§3 over N imported clips. Each
   clip's annotate cycle is independent (probe → sample frames → vision call →
   `write_media_annotation`). §7.3 of that plan already calls for async bulk
   indexing; this is its natural engine.
2. **Per-beat rough-cut workers** — one worker per outline beat
   (`docs/outline-editing-plan.md` §2 outline IR): search library, probe candidates,
   return a ranked clip proposal per beat.
3. **Parallel search/probe sweeps** — "find every clip with the dog on the beach" =
   fan out `search_library` / `probe_media` / `analyze_media` across the library.
4. **A/B render variants** — two-to-four candidate grades/cuts rendered as
   low-res proxies for the user to choose from.

### 1.2 What this is NOT

- **Not free-form agent spawning.** There is no `create_agent(persona=...)`. The model
  gets exactly one new verb, `spawn_subtasks`, whose children run a host-selected tool
  surface: full mode inherits the parent's project-scoped local tools, while legacy
  `tool_profile` values remain restricted. The model picks goals; the host picks
  capabilities.
- **Not a replacement for d7f941c's same-batch parallel dispatch.** That branch
  parallelized the tool calls of a *single model message*
  (`d7f941c` loop `_dispatch_tool_calls_parallel`, loop_d7f line 815). A subtask is a
  *multi-step child loop with its own model calls*. Same-batch parallelism can be
  revived later independently; this plan only salvages its budget APIs (§5) and its
  safe-set/max-parallel judgment (§8).
- **Not user-interactive.** Children cannot `elicit`. A child that needs a human
  decision returns `status:"needs_user"` in its structured result and the PARENT
  decides whether to ask (it has the `AskBridge`, `gemia/agent_loop_v3.py:306`).

**Alternatives considered**
- *Free-form `spawn_agent` with arbitrary system prompt + full host access*: still
  rejected. The full child profile uses the existing project-scoped read/write/edit
  tools and project journal; host credential paths and remote-session host tools
  remain protected.
- *Host-side batch APIs per workload* (e.g. a monolithic `annotate_all` tool):
  rejected — it hard-codes choreography the model should own (the same reasoning as
  `docs/outline-editing-plan.md` §8's ADR against a host-side `assemble_from_outline`),
  and each new workload would need a new host tool. One generic fan-out verb +
  profiles covers all four; larger workloads require separate root-owned calls.

中文小结：目标是四类视频领域真实需要的并行工作（批量标注、逐 beat 粗剪工人、检索/探测
扫射、A/B 渲染变体）。不做自由 spawn，不做工作负载专用大工具；一个受限扇出动词 + 固定
工具档案覆盖全部场景。子代理不能问用户。

---

## 2. Prior art: the `d7f941c` salvage report

`git show d7f941c --stat`: `parallelize safe agent loop tool calls` (2026-06-21) touched
`gemia/agent_loop_v3.py` (+753/−217), `gemia/budget_guard.py` (+50), and added
`tests/test_agent_loop_parallel_dispatch.py` (335 lines). Never merged.

### 2.1 Salvage (adopt nearly verbatim)

- **`BudgetReservation`** (`d7f941c:gemia/budget_guard.py:84`) — frozen dataclass
  `(tool_name, estimated_cost_usd, estimated_eta_sec)`.
- **`BudgetGuard.reserve(tool_name)`** (`:131`) — atomic check-and-reserve: runs
  `check()`, then adds the *estimates* to `spent_usd`/`spent_seconds` up front so N
  concurrent launches cannot all pass `check()` against stale totals. Returns
  `(BudgetDecision, BudgetReservation | None)`.
- **`BudgetGuard.commit_reserved(reservation, *, actual_usd, actual_seconds)`**
  (`:151`) — settlement: `spent += actual − estimated`. **Unspent reservation returns
  automatically** because a lower actual produces a negative delta.
- Judgment values: `_MAX_PARALLEL_TOOL_CALLS = 4` (loop_d7f:84) and the instinct that
  `run_shell` needs a per-call isolated workspace (`_child_tool_context`,
  loop_d7f:412 — per-call `output_dir / "run_shell_workspaces" / <call_id>`).
- Test shapes from `tests/test_agent_loop_parallel_dispatch.py` (reservation math,
  over-cap refusal, settlement return) — port the assertions, not the harness.

### 2.2 Bit-rot (do NOT rebase; re-implement against HEAD)

The branch's dispatch code predates five loop features that now live in
`gemia/agent_loop_v3.py`: the plan-mode gate (`:1206`), the success-blind doom-loop
guard (`:136`, `:1407`), `turn_wrapup` synthesis (`:919`), the RC4+ pre-delivery gate
(`:1100`), and the lumen post-state digest (`:1457`). Its `_apply_dispatch_outcome`
refactor of `_drive_turn` conflicts with all of them. Its `should_stop` semantics also
implement the OLD hard circuit-breaker (`_MAX_CONSECUTIVE_TOOL_FAILURES` as a stop,
loop_d7f:80) which HEAD deliberately replaced with nudges (`agent_loop_v3.py:109-124`).
Salvage = the budget module diff + the constants; leave the loop diff dead.

中文小结：d7f941c 的预算预留/结算 API（`BudgetReservation`/`reserve`/`commit_reserved`）
原样采纳；并发上限 4 和 run_shell 隔离工作区的判断保留。它的循环改动落后 HEAD 五个特性
（plan gate、doom loop、wrapup、预交付门、lumen 摘要），废弃不 rebase。

---

## 3. Architecture: child loop + concurrency model

### 3.1 Child = `SubtaskLoop`, a restricted AgentLoopV3-lite (new file `gemia/subtasks.py`)

A child needs multi-step model-driven behavior (probe → look → decide → annotate), so a
pure tool batch is not enough. But a full second `AgentLoopV3` is wrong:

- `AgentLoopV3.__init__` opens its own `ProjectHandle`
  (`gemia/agent_loop_v3.py:296-301`) — a second open per child breaks the
  single-writer discipline landed in commit `35f61b2`.
- It constructs an `AskBridge` (`:306`) — children must not ask users (D9).
- It writes v2-compatible session meta (`:321`, `:323-344`) — children are not
  sessions and must not pollute `sessions_root`.
- It carries the pre-delivery gate / visual self-check machinery (`:1100-1151`) —
  a delivery-to-user concept; children deliver to the parent.

`SubtaskLoop` therefore reuses only the *streaming and dispatch primitives*:

```
class SubtaskLoop:                     # gemia/subtasks.py (new)
    agent_id: str                      # e.g. "sub_1" (unique within the spawn call)
    parent: AgentLoopV3 | SubtaskLoop   # live context; nested fan-out is refused
    goal: str                          # child system+user prompt payload
    profile: SubtaskProfile            # frozenset of tool names + flags (§4)
    guard: BudgetGuard                 # shared parent guard (§5)
    max_steps: int | None              # optional caller limit; omitted = unlimited
    _messages: list[dict]              # own rolling transcript, never parent's
```

- **Model calls**: `parent.client.stream_turn(...)` — the shared `GeminiClientV3`
  instance (`gemia/agent_loop_v3.py:284`) is stateless per call (messages passed in),
  so sharing is safe on one loop. Full-profile child schemas expose the parent's
  project-scoped file read/write/edit and creative tools; legacy restricted profiles
  remain opt-in.
- **Dispatch**: same `DISPATCHER` table (`gemia/tools/__init__.py:198-200`), guarded by
  an explicit `if name not in profile.tools: -> E_SUBTASK_PROFILE tool_result`
  (fail-closed, mirroring `plan_mode.is_plan_safe`, `gemia/plan_mode.py:83-86`) —
  schemas-subset alone is not a security boundary because a model can hallucinate
  tool names.
- **Child ToolContext**: a NEW `ToolContext` (`gemia/tools/_context.py:157-169`) per
  child, sharing the parent's `registry`, `jobs`, and `project` fields but with:
  - `output_dir = parent.output_dir / "subtasks" / agent_id` (asset files land in a
    per-child dir; `AssetRegistry` records absolute paths so downstream tools are
    unaffected);
  - its own `emit_progress` bound to the child's current `call_id`+`agent_id` — this
    also fixes, for children, the shared-mutable hazard the parent still has (the
    parent rebinds ONE shared `self._tool_ctx.emit_progress` per call,
    `gemia/agent_loop_v3.py:1337`, which is only safe because it is serial);
  - `extra` = copy of parent extra **with `ask_bridge` removed** (the parent seeds it
    at `gemia/agent_loop_v3.py:308-309`; absence makes `elicit` structurally
    impossible). A child receives the separate child-only `ask_root_agent` schema;
    `agent_loop` points to the current child so the dispatcher can identify the
    caller, while `spawn_subtasks` remains forbidden there.
- **No SSE text**: child text deltas accumulate into the child transcript only (D6).
  Child tool events emit through `parent._emit` with `agent_id` attached (§6).

**Alternatives considered**
- *Full `AgentLoopV3` per child*: rejected for the four bullets above.
- *Single-purpose hard-coded loops per workload* (e.g. an `AnnotateWorker` class with a
  fixed probe→vision→write script): rejected — every workload variation (bilingual
  annotation policy, per-beat ranking criteria) would become host code; the model
  should own strategy inside the existing project-scoped capability envelope. The
  profile mechanism gives legacy narrow modes while full mode keeps throughput high.
- *Child talks to a cheaper/smaller model*: deferred, not rejected. `SubtaskLoop`
  takes the client from the parent today; a `model=` knob on the profile is a
  one-line extension once a second provider config exists. Not in P1 scope.

### 3.2 Concurrency: asyncio tasks on the session's single loop — no threads

`SessionRunner` gives each session exactly one daemon thread running one dedicated
asyncio loop (`gemia/session_manager.py:67-83`, `_run_loop` `:94-106`), and the whole
codebase leans on that confinement:

- `AssetRegistry` is a plain dict with no lock (`gemia/tools/_context.py:71-144`);
  `register_output` raises on duplicate ids (`:109-110`) and `_next_id` is a
  non-atomic read-increment (`:142-144`). Safe iff all mutation happens on one thread.
- User timeline edits are deliberately hopped ONTO the session loop
  (`run_project_edit`, `gemia/session_manager.py:151-173`) precisely so their
  `timeline_op` emits serialize with the turn's event stream.
- `AskBridge.deliver` resolves futures via `call_soon_threadsafe` back onto this loop
  (`gemia/tools/_ask_bridge.py:77-93`).

Children as `asyncio.create_task(...)` coroutines on this same loop preserve every one
of those invariants: all Python-level state mutation interleaves only at `await`
boundaries; ffmpeg/HTTP waits overlap for the actual speedup; and session shutdown
already cancels every task on the loop (`SessionRunner._cancel_pending`,
`gemia/session_manager.py:226-232`) so closed-session cleanup is free.

Known cost, accepted: a synchronous CPU-bound stretch inside any dispatcher blocks
parent AND children alike. That is today's behavior for the parent (documented at
`gemia/session_manager.py:161-164`); fan-out does not worsen the class of problem,
only its blast radius, and the heavy tools are subprocess/network-bound.

**Alternatives considered**
- *Thread pool per child*: rejected — breaks `AssetRegistry`/message-list thread
  confinement, would require locking a dozen structures, and buys nothing because
  child workloads are await-bound, not CPU-bound.
- *Separate process per child*: rejected for v1 — serializing `ToolContext`/project
  handles across processes is a project on its own; revisit only if real CPU-bound
  child work appears.

中文小结：子代理是新文件 `gemia/subtasks.py` 里的 `SubtaskLoop`：复用父的 Gemini 客户端、
DISPATCHER、注册表/任务表/项目句柄，但有自己的消息列表、自己的 ToolContext（独立
output_dir、独立 emit_progress、剥掉 ask_bridge）、按档案裁剪的 schema 子集 + 派发前的
fail-closed 白名单校验。并发用会话现有单事件循环上的 asyncio task——AssetRegistry 无锁、
用户编辑跳环、关会话统一取消，这些既有约定全部依赖单线程模型，开新线程全会破。

---

## 4. The `spawn_subtasks` verb and tool profiles

### 4.1 Schema (exact `_schema.py` entry; paste before the closing `]` at `gemia/tools/_schema.py`)

```python
_tool(
    "spawn_subtasks",
    "Fan out up to 4 direct sub-agents that work IN PARALLEL on independent goals and "
    "return structured results. Full-profile children inherit project-scoped "
    "read/write/edit tools; children can ask the root for internal guidance but "
    "cannot recursively fan out or ask the user, and draw cost/time from THIS "
    "session's budget. Use for: bulk media annotation/indexing, per-beat rough-cut "
    "candidate scouting, parallel library search/probe sweeps, A/B preview variants. "
    "Do NOT use for a single sequential task — call the tools directly instead.",
    {
        "subtasks": {
            "type": "array",
            "minItems": 1,
            "maxItems": 4,
            "items": {
                "type": "object",
                "properties": {
                    "goal": {"type": "string",
                             "description": "Self-contained instruction for this child; include asset_ids explicitly."},
                    "tool_profile": {"type": "string",
                                     "enum": ["full", "annotate", "probe"],
                                     "description": "Omit or use 'full' for the parent's full local tool set; legacy narrow modes remain available."},
                    "asset_ids": {"type": "array", "items": {"type": "string"},
                                  "description": "Assets this child is scoped to (informational; echoed into the child prompt)."},
                },
                "required": ["goal"],
            },
        },
        "deadline_sec": {"type": "number",
                         "description": "Optional caller-requested shared wall-clock deadline; omit for unlimited wait."},
    },
    ["subtasks"],
),
```

Budget table entry (`gemia/budget_guard.py:18` `_TOOL_COSTS`):
`"spawn_subtasks": {"usd": 0.00, "eta_sec": 1.0}` — the verb itself is near-free;
real child work is charged through the shared parent guard.

### 4.2 Profiles (legacy narrow modes plus full mode)

Derived by reading dispatcher behavior, same doctrine as
`gemia/plan_mode.py:10-19` — names below cross-checked against `DISPATCHER`
(`gemia/tools/__init__.py:97-195`):

```python
PROFILE_ANNOTATE = frozenset({
    "probe_media", "analyze_media", "extract_frame", "search_library",
    "get_media_annotations", "annotate_media", "write_media_annotation",
})
PROFILE_PROBE = frozenset({
    "probe_media", "analyze_media", "search_library",
    "get_media_annotations", "get_timeline", "get_lumenframe", "get_safe_areas",
})
# Full mode is represented by ``None`` and inherits the parent's local tool set,
# including project-scoped reads, writes, file edits, and creative edits.
# P2: PROFILE_BEAT_SCOUT = PROFILE_PROBE | {"get_shotlist"}   (returns proposals only)
# P3: PROFILE_RENDER_VARIANT = {"render_preview", "lumen_render", "extract_frame",
#                               "probe_media", "get_timeline", "get_lumenframe"}
FORBIDDEN_IN_CHILDREN = frozenset({"elicit", "spawn_subtasks"})
```

Notes:
- `annotate_media` / `write_media_annotation` are PLAN-BLOCKED tools
  (`gemia/plan_mode.py:52,74`) but are the whole point of the annotate profile in
  normal mode; plan-mode inheritance (§7) still blocks them when plan mode is ON.
- `extract_frame` registers derived assets — acceptable: children register into the
  shared registry deliberately (results must be addressable by the parent). Asset-id
  allocation is loop-confined, so no races (§3.2).
- Test gate (mirrors `tests/test_plan_mode.py` exact-coverage style): every legacy
  profile ⊆ `TOOL_NAMES`, `elicit` and `spawn_subtasks` are unavailable to children,
  child schemas include `ask_root_agent` and project file/edit tools, and
  `"spawn_subtasks" in PLAN_BLOCKED_TOOLS` — all asserted in `tests/test_subtasks.py`.

### 4.3 Structured result (what the parent's tool_result contains)

`spawn_subtasks` returns, per child, exactly:

```json
{"agent_id": "sub_1", "status": "ok|error|timeout|cancelled|needs_user",
 "summary": "<=1200 chars, the child's final text>",
 "asset_ids": ["img_007", "..."], "data": {"...profile-specific..."},
 "steps": 6, "spent_usd": 0.04, "spent_seconds": 71.2}
```

Total tool_result capped at 16 KB (truncate `summary`s round-robin past the cap) so a
4-child fan-out cannot flood the rolling context window
(`_ROLLING_USER_TURNS = 8`, `gemia/agent_loop_v3.py:82`).

中文小结：`spawn_subtasks` 的 schema、预算表条目、两个 P1 档案（annotate/probe）与
子任务结果仍为结构化 JSON；full profile 对项目读写和创作编辑开放，只有用户交互由父代理
承担。

---

## 5. Budget and execution limits

Children share the parent session's `BudgetGuard`; the subtask module does not
reserve a per-child slice or create a fresh budget pool. This keeps sibling work
honest against the same session accounting and leaves paid-media production
policy as the authoritative spend gate.

Execution is bounded at the fan-out boundary:

- each `spawn_subtasks` call accepts **at most 4 direct children**;
- children cannot recursively call `spawn_subtasks`;
- omitted `max_steps` means the child may continue until it finishes, is cancelled,
  or hits a real provider/session failure;
- omitted `deadline_sec` means the dispatcher waits for every child;
- an explicit caller-provided step or deadline value is honored;
- cancellation owns the four direct child tasks and gathers every child;
- the per-child identical-call doom-loop guard remains a no-progress detector, not
  a workload-size cap.

Children may call the child-only `ask_root_agent` consultation tool when they need
guidance about the root task or a decision. The root answers with text only; this
does not open a user-facing question or another fan-out branch.

The parent loop's existing production budget, credential protection, remote-session
host restrictions, and plan-mode gate remain in force. Those are session/product
boundaries, not artificial sub-agent throughput limits.

中文小结：子代理共享父会话预算，不切片、不新建预算池；每次最多 4 个直接子代理，子代理
不能继续扇出。默认不限制子代理步数和批次等待，调用方若明确传入 `max_steps` 或
`deadline_sec` 则按调用方要求执行。子代理遇到真正阻塞时可通过 `ask_root_agent` 向 root
请求内部指导，但不会转成用户提问。

---

## 6. Protocol: SSE kinds, `agent_id` threading, parity checklist

### 6.1 New kinds — minimal set decision

Add exactly two kinds to `EVENT_KINDS` (`gemia/v3_contract.py:27`) **before any emit
code exists** (contract-first rule, `docs/protocol-parity-plan.md` 规则 3):

```
subagent_start   {kind, call_id, agent_id, goal, tool_profile,
                  budget: {max_usd, max_seconds}, index, total}
subagent_result  {kind, call_id, agent_id, status, summary, asset_ids,
                  steps, spent_usd, spent_seconds, elapsed_seconds}
```

`call_id` is the spawning `spawn_subtasks` call's id — the anchor both frontends
already track (`static/v3/v3.js:75,442` `toolCalls` Map;
`~/Code/lumeri-cli/src/App.js:171` `callsById`).

**`subagent_progress` is deliberately NOT added.** Child activity is real tool
execution, and the transport already has honest vocabulary for it: children emit the
EXISTING `tool_exec_start` / `tool_exec_progress` / `tool_exec_result` /
`tool_exec_error` kinds (via `parent._emit`, which is thread-agnostic anyway —
`gemia/transport/sse.py:65-75`). A synthetic "child is 60% done with its goal" percent
would be fabricated narration, which `transport/sse.py:5-16` invariant 1 forbids.
(Alternative — one `subagent_progress` wrapping child tool events: rejected; it
duplicates every `tool_exec_*` payload shape and both frontends' renderers.)

`PROTOCOL_VERSION` stays 1: additive kinds are non-breaking by the contract's own rule
(`gemia/v3_contract.py:20-22`); both frontends banner unknown kinds rather than drop.

### 6.2 `agent_id` threading rule (optional field on existing kinds)

- Any `tool_exec_start|progress|result|error` and `model_tool_call_ready` event MAY
  carry `agent_id: str`. **Absence means the root/parent loop** — existing emit sites
  change zero bytes, replay compatibility is total.
- Children never emit `model_text_delta`, `turn_*`, `completion_check`, `plan_gate`,
  `budget_gate` kinds. A child's budget refusal is internal (its tool_result); a
  child's terminal state is exactly one `subagent_result`.
- `agent_id` format: `"sub_{n}"`, unique within the spawn call; global uniqueness
  comes from pairing with `call_id` (frontends key children by
  `subagent_start.call_id + agent_id`).

### 6.3 Rendering (both frontends, same commit)

- **web `static/v3/v3.js`**: handlers table (`:419`, dispatched at `:621`) gains
  `subagent_start` / `subagent_result`. Rendering rule: a child renders as an
  indented group UNDER the spawning `spawn_subtasks` call card; `tool_exec_*` events
  carrying `agent_id` route into that child group instead of the top-level call list
  (one `if (ev.agent_id)` branch at the head of the four tool_exec handlers).
  `subagent_result` closes the group with status chip + summary.
- **CLI `~/Code/lumeri-cli/src/App.js`**: `handleEvent` (`:140`) mirrors the same:
  child lines render indented (`  ├─ sub_1 …`) under the spawn call, statuses via
  the existing chip/label conventions in `src/format.js`.
- Unknown-kind banner behavior already covers older clients mid-rollout.

### 6.4 Parity checklist (same commit — `docs/protocol-parity-plan.md` 规则 1)

1. `gemia/v3_contract.py`: add both kinds to `EVENT_KINDS` (FIRST change of the commit).
2. `scripts/export_contract.py` rerun → `static/v3/contract.json` +
   `~/Code/lumeri-cli/src/contract.json` regenerate.
3. Emit sites: `gemia/subtasks.py` emits with literal kind strings only, and
   `tests/test_v3_contract.py:26` `EMIT_FILES` gains `"gemia/subtasks.py"` (the
   extraction regex `:33` is literal-string based — non-literal kinds blind it).
4. Web: `static/v3/v3.js` handlers for both kinds + `agent_id` routing in the four
   `tool_exec_*` handlers.
5. CLI: `src/App.js` handleEvent cases + rendering; `test/contract.mjs` stays green
   (it asserts App.js covers contract EVENT_KINDS).
6. Backend tests: `python3 -m pytest tests/test_v3_contract.py tests/test_subtasks.py`.
7. CLI tests: `cd ~/Code/lumeri-cli && npm test`.
8. Verification recorded in QUEUE.md with BOTH ends' results (规则 2).

中文小结：只加 `subagent_start`/`subagent_result` 两个 kind；子代理的工具活动复用现有
`tool_exec_*` + 可选 `agent_id` 字段（缺省=父循环，零破坏）。不加 subagent_progress——
子进度就是真实工具进度，合成百分比违反 sse.py 不变量 1。渲染规则：两端都把带 agent_id
的事件缩进归组到 spawn 调用卡片下。八步同 commit 对等清单照 parity plan 执行，
test_v3_contract 的 EMIT_FILES 必须加入 gemia/subtasks.py。

---

## 7. Plan mode

### 7.1 Classification: `spawn_subtasks` → `PLAN_BLOCKED_TOOLS`

Per the derivation doctrine (`gemia/plan_mode.py:10-19`, classify by reading the
dispatcher): the spawn dispatcher reserves budget, launches model-driven children
whose profiles include mutating tools (`annotate_media`, `write_media_annotation`),
and registers derived assets (`extract_frame`) — three independently disqualifying
behaviors. Planning-quality answers ("what would you fan out?") need no execution.
Fail-closed would block an unclassified name anyway (`gemia/plan_mode.py:83-86`), but
the exact-coverage test in `tests/test_plan_mode.py` forces an explicit entry the
moment the tool registers in `TOOL_NAMES` — add it to `PLAN_BLOCKED_TOOLS`
(`gemia/plan_mode.py:51`) in the same commit as the schema.

### 7.2 Inheritance for mid-turn toggles

`set_plan_mode` can flip mid-turn from the HTTP thread; the parent deliberately reads
the flag once per tool call (`gemia/agent_loop_v3.py:360-374`, gate at `:1206`).
Children MUST implement the same read-per-dispatch against the PARENT's live flag:
`if self.parent.plan_mode and not is_plan_safe(name): -> blocked tool_result
(E_PLAN_MODE)`. This is defense in depth — plan mode ON at spawn time already blocks
the spawn itself, but a toggle *during* a running batch must clamp children within one
dispatch, not at their next model call. Child plan-blocks count toward the child's own
failure-nudge state, never toward the parent's `plan_gates_this_turn`
(`gemia/agent_loop_v3.py:1008`, hard stop at `:1226` — a child cannot end the parent
turn through the plan gate).

中文小结：`spawn_subtasks` 进 `PLAN_BLOCKED_TOOLS`（理由：预留预算、启动含变更工具的
子代理、注册资产，三条各自够格）；exact-coverage 测试会强制显式分类。子代理每次派发
实时读父的 plan_mode 标志（纵深防御，覆盖批次运行中途切换），子代理的 plan 拦截不计入
父回合的 plan_gate 硬停计数。

---

## 8. Safety rails

| Rail | Value | Grounding |
|------|-------|-----------|
| Max children per call | **4** — schema `maxItems` and host validation agree | Bounded parallel batch; avoids unbounded provider fan-out |
| Max depth | **1** — direct children cannot call `spawn_subtasks` | Keeps root ownership clear and bounds the task tree |
| Child-to-root guidance | Child-only `ask_root_agent`; root returns text-only guidance | Internal consultation; never asks the user |
| No user interaction | `elicit` remains unavailable to children; `ask_bridge` is not copied into child ctx | §3.1 |
| Child step cap | **None by default**; an explicit caller `max_steps` is honored | The caller controls task shape; no host hard maximum is imposed |
| Child doom loop | Same `_DOOM_LOOP_THRESHOLD = 3` byte-identical guard (`agent_loop_v3.py:136`, `_is_doom_loop` `:962`), per-child state; trips → child ends `status:"error"`, parent turn continues | Inherit semantics, isolate blast radius |
| Child failure nudges | Same `(tool, code)` streak thresholds 5/8 (`agent_loop_v3.py:116,124`), per-child dict | Consistency with parent behavior |
| Batch deadline | **None by default**; an explicit caller deadline is honored; stragglers cancelled → `status:"timeout"` | The caller controls whether a batch should be bounded |
| Budget | Shared parent `BudgetGuard`; paid-media production policy remains authoritative | Prevent duplicate/fresh budget pools while removing artificial child slices |
| Cancellation | The spawn dispatcher owns its child tasks in `try/finally: cancel + gather(return_exceptions=True)`. `asyncio.create_task` children are NOT auto-cancelled when the enclosing coroutine is cancelled — the finally block is mandatory; explicit deadlines cancel stragglers and parent/session cancellation propagates | Covers: parent stream error (`agent_loop_v3.py:1076-1084` returns → task cancellation unwinds through the dispatcher), user closes session (`SessionRunner._cancel_pending`, `session_manager.py:226-232`), deadline expiry |
| SSE replay pressure | Child `tool_exec_progress` coalesced to ≥1 s per child (wrapper around the progress cb) | `REPLAY_BUFFER_SIZE = 200` (`transport/sse.py:38`); 4 verbose children could evict a disconnected client's replay window (`replay_gap`, `sse.py:144-163`) |
| Result flood | Per-child `summary` ≤1200 chars; whole tool_result ≤16 KB | §4.3; rolling window `_ROLLING_USER_TURNS = 8` (`agent_loop_v3.py:82`) |
| Registry integrity | Children share the parent `AssetRegistry`; single-loop confinement makes `allocate_id`/`register_output` race-free; duplicate ids still raise (`_context.py:109-110`) | §3.2 |
| Project writes | Full-profile children may use project-scoped file and edit tools; the existing journal and event loop coordinate writes | Keeps read/write/edit productive without allowing credential/root paths |

One deliberate non-rail: children do NOT get the parent's pre-delivery gate / visual
self-check (`agent_loop_v3.py:1100-1151`). That gate protects *user-facing delivery*;
the parent reviews child output before delivering, and the RC4 gate fires on the
parent as usual after the spawn result returns.

中文小结：护栏全表——每批最多 4 个直接子代理、深度 1、子代理可用 `ask_root_agent`
但不能 `elicit` 或继续 spawn；子代理步数和批次死线仍可由调用方显式指定，doom-loop 与
失败 nudge 按子隔离继承。取消必须在 spawn 派发器的 finally 里显式 cancel+结算，子进度
事件 ≥1 秒合并保护回放环，结果 16KB 传输封顶；子代理仍可使用允许的项目读写和创作工具。

---

## 9. Phased delivery

### Current implementation — full-profile bounded fan-out

Scope: `spawn_subtasks` with full, annotate, and probe profiles; up to four direct children,
child-to-root consultation, optional caller step/deadline values, and project-scoped
read/write/edit.

Implementation evidence:
1. `gemia/subtasks.py` enforces `MAX_CHILDREN = 4`, rejects child fan-out, exposes
   child-only `ask_root_agent`, and retains full-profile project read/write/edit tools.
2. `gemia/tools/_schema.py` documents the four-child `maxItems` contract;
   `tests/test_subtasks.py` covers the over-cap refusal, child refusal, root consultation,
   and actual write dispatch.
3. Focused verification: subtask, file, plan-mode, remote-protection, and capability
   registry tests pass together; parent agent-loop/contract regressions are checked
   separately so unrelated cross-repo drift remains visible.

Primary user story to verify end-to-end: "annotate these 4 clips" → one spawn, four
`annotate` children, `search_media` (per `docs/semantic-search-media-plan.md` §4.4)
finds their content afterwards.

### Phase 2 — per-beat assembly workers (`beat_scout` profile)

Depends on `docs/outline-editing-plan.md` landing (outline IR §2, verbs §3, rough-cut
choreography §6). Children scout candidates per beat and return ranked proposals
(`data: {beat_id, candidates: [{asset_id, in, out, score, why}]}`); the PARENT applies
`timeline_*` verbs serially (D8). The §6.2 checkpoint cadence (N=4) of that plan maps
naturally onto batches of ≤4 scouts.

### Phase 3 — A/B render variants (`render_variant` profile)

Children produce low-res proxy variants via `render_preview` / `lumen_render`
(both register assets; both plan-blocked — normal-mode only). Needs one design
addition before implementation: variant isolation for lumenframe render settings so
two children don't fight over one work area — likely per-child cloned comp or
render-args-only variation. Deliberately unresolved here; decide in a short addendum
when P3 starts.

中文小结：P1 = annotate/probe 扇出，四个工作包各带测试门，一个 codex 会话可交付；
P2 = 逐 beat 侦察工人，依赖 outline 计划落地，子代理只出提案、父串行落时间线；
P3 = A/B 渲染变体，lumenframe 渲染隔离问题留待届时补一页决议。

---

## 10. Acceptance criteria (Phase 1 done means)

1. A 4-clip annotate fan-out completes with wall-clock < 0.5× the serial baseline on
   the same machine, budget totals exact (reservations settled, unspent returned,
   spawn seconds not double-counted).
2. `pytest tests/test_subtasks.py tests/test_budget_guard.py tests/test_plan_mode.py
   tests/test_v3_contract.py` green; `~/Code/lumeri-cli && npm test` green.
3. Killing the batch mid-flight (session close, deadline, parent stream error) leaves
   `BudgetGuard.snapshot()` consistent and emits a terminal `subagent_result` for
   every started child (status timeout/cancelled).
4. In plan mode, `spawn_subtasks` returns the standard `plan_gate` block; toggling
   plan mode ON during a running batch blocks children's mutating dispatches within
   one tool call.
5. Both frontends render child activity grouped/indented under the spawn call; a
   pre-upgrade client shows unknown-kind banners, never silent drops.

---

## 11. Reference index

- `gemia/agent_loop_v3.py:995` `_drive_turn`; `:1154` serial dispatch; `:1206` plan
  gate; `:1261` budget check; `:1337` shared `emit_progress` rebind; `:1388` commit
  site (double-count rule); `:1407` doom-loop check; `:136` threshold; `:116,124`
  nudge thresholds; `:306-309` AskBridge + ctx extra; `:1100-1151` pre-delivery gate;
  `:82` rolling window.
- `gemia/budget_guard.py:125` `BudgetGuard`; `:141` `check`; `:166` `commit`; `:128`
  caps; `:144-147` committed-resources semantics; `:18` `_TOOL_COSTS`.
- `d7f941c:gemia/budget_guard.py:84` `BudgetReservation`; `:131` `reserve`; `:151`
  `commit_reserved`. `d7f941c:gemia/agent_loop_v3.py:84` max-parallel=4; `:412`
  `_child_tool_context`; `:815` `_dispatch_tool_calls_parallel` (bit-rotted, not salvaged).
- `gemia/session_manager.py:67-106` thread+loop per session; `:125-149` single-turn
  guard; `:151-173` `run_project_edit` loop-hop; `:226-232` `_cancel_pending`.
- `gemia/transport/sse.py:5-16` honesty invariants; `:38` replay buffer 200; `:65`
  thread-safe emit; `:144-163` `replay_gap`.
- `gemia/v3_contract.py:27` `EVENT_KINDS`; `:20-22` additive-kind rule; `:85` `E_BUDGET`.
- `gemia/plan_mode.py:28/51/80/83` allow/block sets, turn limit, `is_plan_safe`.
- `gemia/tools/__init__.py:198-200` `DISPATCHER`; `gemia/tools/_schema.py:1718`
  `TOOL_NAMES`; `gemia/tools/_context.py:157-169` `ToolContext`; `:109-110` dup-id raise.
- `gemia/tools/_ask_bridge.py:42-93` emit_and_wait / deliver.
- `tests/test_v3_contract.py:26` `EMIT_FILES`; `:33` literal-kind regex.
- `static/v3/v3.js:419` handlers table; `:621` dispatch; `:75,442` toolCalls Map.
- `~/Code/lumeri-cli/src/App.js:140` handleEvent; `:171` callsById;
  `test/contract.mjs` CLI drift test.
- Intersections: `docs/semantic-search-media-plan.md` §2 annotation contract, §3
  sampling/budget, §4.4 `search_media`, §7.3 async bulk indexing;
  `docs/outline-editing-plan.md` §2 outline IR, §3 verbs, §6 choreography (N=4
  cadence), §8 ADR against host-side assembly.
