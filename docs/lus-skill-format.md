# .lus — Lumeri Skill File Format Specification (v1)

Status: 定稿（locked design, pending implementation — see §11 work packages)
Date: 2026-07-06
Scope: `/Volumes/Extreme SSD/lumeri` (backend + web v3; directory renamed from `gemia` 2026-07-17) and `~/Code/lumeri-cli` (TUI client)
Audience: a parser/validator author. This document is the complete reference; no other
document is required to implement `.lus` reading, writing, validation, or migration.

Point Library bundles are specified separately in
[`point-library-lus-format.md`](point-library-lus-format.md). This v1 document
continues to govern legacy text Skills; the v2 bundle is additive and does not
change the legacy parser contract.

> Normative keywords: MUST / MUST NOT / SHOULD / MAY as in RFC 2119.
> All byte offsets and lengths in this spec are **UTF-8 bytes** unless stated otherwise.

---

## 0. Ground truth — the skill infrastructure that exists today

Everything in this section was read from committed code on 2026-07-06. The format design
in §2–§7 is anchored to these exact seams.

There are **four** skill-shaped subsystems in the repo. Only one of them is the target
of `.lus`; the other three are explicitly out of scope (§8.4 says why).

### 0.1 DistilledSkillStore — the v3 agent's durable skill store (MIGRATION TARGET)

- Store class: `gemia/skill_store.py:79` (`DistilledSkillStore`).
- Root resolution: `gemia/skill_store.py:36-49` (`distilled_skills_dir()`) —
  `GEMIA_SKILL_STORE_DIR` env override (used by tests), else `~/.gemia/skills`.
- Serialization today: **one JSON file per skill**, `<slug>.json`, written at
  `gemia/skill_store.py:120,140`. Slug keeps CJK characters
  (`_slugify`, `gemia/skill_store.py:515-520`).
- Record shape (`gemia/skill_store.py:130-139`):
  `{name, source:"distilled", when_to_use, steps: [str], notes, tags: [str], created_at, updated_at}`.
- Idempotent re-save: same name overwrites, `created_at` preserved
  (`gemia/skill_store.py:122-127`).
- Tool entry points: `save_skill` → `gemia/tools/save_skill.py:43`
  (`dispatch_save_skill`, distillation path via `store.distill(...)`), `recall_skills` →
  `gemia/tools/save_skill.py:99`. Wired in the dispatch table at
  `gemia/tools/__init__.py:149-150`; model-facing schemas at
  `gemia/tools/_schema.py:925-947` (save) and `:966-988` (recall).
- Recall: `gemia/skill_store.py:232-273` (`recall_skills`) — loads **all** distilled
  JSONs plus library skills, ranks by substring/token overlap
  (`_relevance`, `gemia/skill_store.py:209-229`; weights: name 3.0, when_to_use 2.0,
  tags 2.0, triggers 2.0, steps 1.0, notes 1.0), projects to a compact view
  (`_recall_view`, `gemia/skill_store.py:276-285`).
- Plan mode: `recall_skills` is ALLOWED (`gemia/plan_mode.py:42`), `save_skill` is
  BLOCKED (`gemia/plan_mode.py:68`). Budget: `save_skill` costs $0.00 / 0.5 s eta
  (`gemia/budget_guard.py:62`).
- **Prompt injection: none.** `gemia/agent_loop_v3.py` contains zero references to
  skills (verified by grep); `gemia/prompts/system_v3.md` contains zero references.
  Skills reach the model ONLY as `recall_skills` tool results. The `{{memory}}` slot
  (`gemia/agent_loop_v3.py:452-463`, filled at `:523`) carries `MEMORY.md` content via
  `gemia.memory.format_memory_for_prompt` — it does not carry skills.
- Tests: `tests/test_skill_distill.py` (save/update/recall e2e, dispatcher wiring,
  AppleDouble `._*` sidecar tolerance — the external SSD creates `._name.json` resource
  forks, see `tests/test_skill_distill.py:31-38`; the store ignores dotfiles at
  `gemia/skill_store.py:151-152`).
- Live contents observed: `~/.gemia/skills/` currently holds 2 distilled skills
  (`audio_ducking_setup.json`, `batch_rough_cut.json`), both Chinese-named content with
  the exact record shape above.

### 0.2 Build-artifact skills (out of scope for .lus)

`save_skill` with a `source` arg archives a built Python file:
`gemia/tools/build.py:406-495`. Writes `<skills_root>/<slug>.py` + `<slug>.json`
metadata (`{name, slug, description, created_at, origin_session, source}`), where
`skills_root` is `<workdirs-parent>/../skills` in production or `<output_dir>/skills`
in tests (`gemia/tools/build.py:461-466`). These are **code artifacts**, not playbooks.
The repo-root `skills/` directory holds 6 such/legacy JSONs.

### 0.3 Legacy v2 plan-template skills (out of scope for .lus)

`SkillStore` (`gemia/skill_store.py:288+`) stores executable **plan templates** in
`skills_v2/*.json` (`{name, description, version:"2.0", origin_task_id, models_used,
parameters, plan{steps}}`), driven by the legacy CLI
(`python -m gemia save-skill`, `gemia/__main__.py:763`) and executed by `PlanEngine`
(`_cmd_run_skill_v2`, `gemia/__main__.py:791+`). ~30 files exist under `skills_v2/`.

### 0.4 Static library skills (format precedent, not migrated)

`gemia/ai/skills/*/SKILL.md` — **YAML frontmatter + markdown body** (e.g.
`gemia/ai/skills/color-grade/SKILL.md`: `id`, `description`, `triggers.primary/
secondary`, `primitives`, `est_tokens`). Loaded by
`gemia/ai/skill_router.py:253` (`load_skill_metadata`), keyword-routed, injected into
the v2 **planner** prompt by `gemia/ai/skill_context.py:31`
(`build_skill_plan_prompt_bundle`). Packaged as data:
`pyproject.toml:29` (`"gemia.ai" = ["skills/*/SKILL.md", "skills/_combos/*.yaml"]`).
Surfaced into v3 recall as read-only "library" candidates via
`gemia/skill_store.py:178-206` (`_library_skills`).

### 0.5 Dependencies and shared guards relevant to the format

- **PyYAML IS a hard dependency**: `pyproject.toml:18` (`"PyYAML>=6"`), present in
  `uv.lock` (line 960). Additionally `gemia/ai/skill_yaml.py:1-22` provides a
  `safe_load` shim with a hand-rolled fallback parser for lean environments where
  PyYAML import fails. (Both spec examples in §9 were verified to parse under the
  fallback shim with PyYAML absent.)
- **Secret guard precedent**: `gemia/memory.py:149-175` — `_has_secret_key` +
  `_text_looks_secret` reject secret-like keys (`api_key`, `token`, `password`, …,
  list at `gemia/memory.py:13-28`) and secret-looking text (`sk-…`, `password = …`,
  bearer tokens). The `.lus` validator (§6) reuses these patterns.
- **Known tool names**: `TOOL_NAMES` at `gemia/tools/_schema.py:1718` — 98 unique
  tool names at the time of writing. This is the reference set for `tools_used`
  validation.
- **CLI/web exposure today: none.** Zero grep hits for "skill" in
  `~/Code/lumeri-cli/src`, `~/Code/lumeri-cli/test`, and `static/v3/v3.js`. Skills are
  invisible to both frontends except as ordinary tool-call events in the transcript.

**中文小结（现状）**：今天的技能体系有四层——v3 智能体的沉淀库（`~/.gemia/skills/*.json`，
`.lus` 的迁移对象）、build 产物技能（`.py`+`.json`）、v2 计划模板（`skills_v2/`）、内置
SKILL.md 技能库（YAML frontmatter + markdown，格式先例）。PyYAML 已是硬依赖且有降级
shim；技能今天**不注入提示词**，只通过 `recall_skills` 工具结果进入模型；CLI 和 web 均
未暴露技能界面。

---

## 1. Design decisions (locked, with alternatives)

| # | Decision | Alternatives considered | Why this one |
|---|----------|------------------------|--------------|
| D1 | Metadata block = **YAML frontmatter** (strict subset, §3), fenced by `---`/`---` immediately after the magic line. JSON metadata is automatically accepted (JSON ⊂ YAML). | Strict JSON block fenced by `---lus-meta---`/`---end-meta---` (the mandated fallback if PyYAML were absent). | PyYAML>=6 is already pinned (`pyproject.toml:18`, `uv.lock:960`), so the no-new-dep condition for choosing JSON does not apply. YAML frontmatter matches the in-repo precedent (`gemia/ai/skills/*/SKILL.md`), is far more human-diffable for CJK text (no `\uXXXX` escapes, no escaped quotes), and the existing `gemia/ai/skill_yaml.py` shim keeps lean test envs working — both §9 examples parse under the shim without PyYAML. |
| D2 | `name` is an **ASCII kebab-case machine key**; the human (often CJK) display name lives in `title`. | Keep CJK in `name`/filename (current `_slugify` keeps CJK, `gemia/skill_store.py:515-520`). | Kebab-case is user-mandated. It also yields portable filenames (the SSD AppleDouble `._*` workarounds in `tests/test_skill_distill.py:31-38` show how fragile exotic filenames already are), stable URLs/CLI args, and unambiguous uniqueness. Recall quality is preserved because ranking searches `title` + `description` + `triggers` (§7.2), where CJK lives. |
| D3 | Stale/missing `checksum` on **read** is a warning, not an error (strict mode escalates to `E_LUS_CHECKSUM`). Writers MUST always emit a fresh checksum. | Hard-fail on any mismatch. | Hand-editing in git is a stated goal ("human-diffable, git-friendly"); hard-failing every hand edit would punish the primary workflow. Integrity still holds where it matters: `save_skill` and the migrator always rewrite it, and strict mode exists for pipelines that need tamper-evidence. |
| D4 | Unknown names in `tools_used` are a **warning** (`W_LUS_UNKNOWN_TOOL`), never an error. | Reject unknown tools. | Mandated forward-compat: a skill saved on a newer Lumeri with new tools must still load on an older one, and library/tool renames must not brick a user's store. |
| D5 | Canonical **writer** is a hand-rolled ~60-line emitter (fixed field order, block style, 2-space indent, LF); parsers use `yaml.safe_load` semantics. | Emit via `yaml.safe_dump`. | `safe_dump` output is nondeterministic across versions/options and the shim's fallback `safe_dump` degrades to JSON (`gemia/ai/skill_yaml.py:18-22`), which would make on-disk bytes depend on the environment. A tiny fixed emitter gives byte-stable diffs everywhere. |
| D6 | Metadata block MUST end within the first **8 KiB** of the file. | No bound. | Enables the cheap metadata-only recall scan (§7.2) with a single bounded read; 8 KiB is ~10× larger than both realistic examples in §9. |
| D7 | Body checksum mismatch aside, `validate_lus` returns warnings as a third tuple element: `(meta, body, warnings)`. | Mandated signature `(meta, body)` exactly. | D3/D4 require a warn-not-fail channel; attaching warnings to the return keeps the function pure (no logging side effects) and testable. This is the only deliberate deviation from the mandated constraint list, and it is additive. |
| D8 | Migration covers **only** the DistilledSkillStore JSONs (§0.1). | Also migrate `skills_v2/`, build-artifact skills, SKILL.md library. | `skills_v2` templates are executable inputs to `PlanEngine` — converting them breaks `_cmd_run_skill_v2` (`gemia/__main__.py:791+`). Build artifacts are code, not playbooks. The SKILL.md library is packaged read-only data with its own router; `.lus` deliberately shares its frontmatter+markdown shape so a future convergence is cheap, but converging now would couple this change to the planner. |
| D9 | Transition dual-read: the store lists `*.lus` first, then legacy `*.json` for names not yet migrated; on collision `.lus` wins. Writes always produce `.lus`. | Hard cutover (read .lus only after migration). | Concurrent agents and older builds may still write JSON for a while; dual-read makes the rollout order-independent and the migrator merely an accelerator. |
| D10 | CR (`\r`) anywhere in the file is rejected (`E_LUS_ENCODING`). | Normalize CRLF→LF on read. | The checksum is defined over raw body bytes (§5); normalization would make the same logical file hash differently depending on the reader. Lumeri is local-first macOS; LF-only is a cheap invariant. |

**中文小结（关键决策）**：元数据选 YAML frontmatter（PyYAML 已是依赖 + SKILL.md 先例 +
中文可读的 diff），`name` 用 ASCII kebab-case、中文放 `title`；写入端用手写的确定性
emitter 保证字节稳定；checksum 手改后默认只警告；未知工具名只警告（向前兼容）；迁移只
针对沉淀库 JSON；过渡期 `.lus` 与 `.json` 双读、`.lus` 优先；文件一律 LF。

---

## 2. File-level rules

A `.lus` file ("Lumeri Skill") is:

1. **UTF-8 text.** No BOM. No `\r` bytes anywhere (LF line endings only) — D10.
2. **≤ 65,536 bytes** (64 KiB) total file size, checked before any parsing.
3. **One skill per file.** The filename MUST be `<name>.lus` where `<name>` is the
   metadata `name` field verbatim (validators warn on mismatch when a path is
   supplied; the store enforces it on write).
4. The file MUST end with exactly one trailing `\n` (writers guarantee this; readers
   MUST NOT reject a missing final newline — it only perturbs the checksum, which is
   handled by D3).
5. Files whose basename starts with `.` (dotfiles, AppleDouble `._*` sidecars) are
   never treated as skills — same rule the store already applies
   (`gemia/skill_store.py:151-152`).

### 2.1 Overall structure (grammar)

```
lus-file   = magic-line meta-block body
magic-line = "#!lus/" major LF          ; line 1, byte 0
major      = 1*DIGIT                    ; no leading zeros, no minor component
meta-block = "---" LF meta-yaml "---" LF
meta-yaml  = *( line LF )               ; strict YAML subset, §3
body       = *( line LF )               ; markdown, §4 — everything after the
                                        ; close-fence line's LF, through EOF
```

- Line 1 MUST be exactly `#!lus/<major>` — regex `^#!lus/[1-9][0-9]*$`. The `#` makes
  the magic line a comment for naive YAML tooling, so `head -n -0` style processing of
  the frontmatter never conflicts.
- Line 2 MUST be exactly `---`.
- The meta block is terminated by the **first** subsequent line that is exactly `---`.
  That close-fence line MUST occur within the first 8,192 bytes of the file (D6).
- The **body** is every byte after the close fence's `\n`, through EOF. A body MUST be
  non-empty after stripping whitespace. Writers emit one blank line between the fence
  and the first heading (see §9), but that blank line belongs to the body and is
  covered by the checksum.

### 2.2 Version negotiation

`SUPPORTED_LUS_MAJORS = frozenset({1})` in the implementation. Parsers MUST reject a
well-formed magic line whose major is not in this set with the **typed error
`E_LUS_VERSION`** (not a generic parse error) so callers can distinguish "not a .lus
file" (`E_LUS_MAGIC`) from "a .lus file from the future" and message the user to
upgrade. Minor/patch format revisions are additive-only and do NOT change the magic
line; anything breaking bumps the major.

**中文小结（文件级规则）**：UTF-8、无 BOM、只允许 LF、整文件 ≤64KB；第 1 行固定
`#!lus/1`；第 2 行起是 `---` 包裹的 YAML 元数据（必须在前 8KB 内闭合）；其后到文件末尾
全部是正文。未知主版本必须抛类型化错误 `E_LUS_VERSION`。

---

## 3. Metadata block

### 3.1 Parsing rules (strict YAML subset)

The text between the two `---` fences MUST parse, via `yaml.safe_load` semantics
(implementation: `gemia.ai.skill_yaml.safe_load`, which falls back to the built-in
mini-parser when PyYAML is absent — `gemia/ai/skill_yaml.py:12-16`), to a **mapping**.

Forbidden YAML features (validator rejects with `E_LUS_META_PARSE` even if PyYAML
would accept them): anchors/aliases (`&`, `*`), tags (`!`), multi-document markers
(`---` can therefore never appear inside the block; the first one always closes it),
and non-string mapping keys. Flow style (`[a, b]`, `{k: v}`) is accepted on read;
the canonical writer emits block style only (D5).

Because JSON is a YAML subset, a metadata block written as a single JSON object is
valid and parses identically — this satisfies "YAML-or-JSON" without a second code
path.

### 3.2 Field reference

Canonical writer order is exactly the table order below. Unknown extra fields are
preserved on round-trip and produce `W_LUS_UNKNOWN_FIELD` (forward compat).

| Field | Type | Req | Validation rule | Max |
|-------|------|-----|-----------------|-----|
| `name` | str | REQUIRED | `^[a-z0-9]+(-[a-z0-9]+)*$` (ASCII kebab-case). Unique key of the skill; also the filename stem. | 64 chars |
| `version` | str | REQUIRED | Strict semver core `^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$` — no pre-release/build suffixes in v1. | 32 chars |
| `lus_version` | int | REQUIRED | MUST equal the magic-line major, else `E_LUS_META_FIELD`. Redundant on purpose: survives copy-paste of the meta block into other documents and lets tools read the version without re-tokenizing line 1. | — |
| `title` | str | REQUIRED | Single line (no `\n`), non-empty after strip. Any Unicode — this is where CJK display names live (D2). | 80 chars |
| `description` | str | REQUIRED | Single line, non-empty. This is the primary recall-matching text (§7.2) — write it as "when X, do Y" matching prose, not marketing copy. | 500 chars |
| `triggers` | list[str] | REQUIRED | 1–16 items; each single-line, non-empty, ≤64 chars; case-insensitively unique. Phrases/patterns a user request would contain (both languages encouraged, cf. the router synonym table `gemia/ai/skill_router.py:63+`). | 16 items |
| `domain` | str | REQUIRED | Enum: `video` \| `deck` \| `cad` \| `general`. | — |
| `tools_used` | list[str] | optional (default `[]`) | ≤32 items, each `^[a-z][a-z0-9_]*$` and ≤64 chars. At save time, items not in `gemia.tools._schema.TOOL_NAMES` (`_schema.py:1718`) produce `W_LUS_UNKNOWN_TOOL` — never an error (D4). | 32 items |
| `parameters` | mapping | optional (default `{"type": "object", "properties": {}}`) | JSON-Schema **subset**: root MUST be `type: object`. Allowed keys anywhere: `type`, `properties`, `items`, `required`, `description`, `enum`, `default`, `minimum`, `maximum`, `minItems`, `maxItems`. Allowed `type` values: `object`, `string`, `number`, `integer`, `boolean`, `array`. Max nesting depth 4; ≤16 properties at root; `required` ⊆ keys of `properties`. Any other key or type → `E_LUS_META_FIELD`. | — |
| `author` | str | optional (default `"lumeri-agent"`) | Single line. No `@`+domain enforcement (not an email — do not put emails in skills). | 64 chars |
| `created_at` | str | REQUIRED | ISO 8601 with explicit UTC offset, e.g. `2026-07-06T08:00:00+00:00`. MUST satisfy `datetime.fromisoformat` and carry tzinfo. | 40 chars |
| `updated_at` | str | REQUIRED | Same rule; MUST be ≥ `created_at`, else `E_LUS_META_FIELD`. | 40 chars |
| `language` | str | REQUIRED | Enum: `zh` \| `en` \| `mixed`. Body language, used by recall display and future prompt-language routing. | — |
| `safety` | mapping | REQUIRED | Exactly the keys `requires_paid_generation: bool` and `mutates_project: bool`. Missing key or non-bool → `E_LUS_META_FIELD`; extra keys → `W_LUS_UNKNOWN_FIELD`. Semantics: `requires_paid_generation` = following the playbook is expected to call paid generation verbs (`generate_image`/`generate_video`/`generate_audio`/`narrate` per `gemia/budget_guard.py` pricing) — surfaced to the user before running the skill under a budget gate. `mutates_project` = the playbook calls tools in `PLAN_BLOCKED_TOOLS` (`gemia/plan_mode.py:51-77`) — i.e. this skill cannot be *executed* (only read) while plan mode is active, and future multi-agent dispatch (§10.2) uses it to gate delegation. This field is advisory metadata; it does not modify the `plan_mode.py` frozensets (their exact-coverage test stays the single source of truth). | — |
| `checksum` | str | REQUIRED on write, optional on read (D3) | `^sha256:[0-9a-f]{64}$`, computed per §5. On read: absent → `W_LUS_CHECKSUM_MISSING`; mismatch → `W_LUS_CHECKSUM_STALE` (or `E_LUS_CHECKSUM` in strict mode). | 71 chars |

**中文小结（元数据）**：`---` 包裹的严格 YAML 子集（禁 anchor/tag/多文档；JSON 天然兼
容）。16 个字段按表格顺序：`name` 是 kebab-case 主键兼文件名，`title`/`description`/
`triggers` 承载召回匹配文本（中文放这里），`domain`/`language` 是枚举，`tools_used` 未知
名只警告，`parameters` 是 JSON-Schema 子集，`safety` 两个布尔位对接付费门与 plan mode，
`checksum` 写时必填、读时缺失或过期只警告。

---

## 4. Body — the markdown playbook

The body is the instruction text the model follows after selecting the skill. It is
plain markdown with the following structural contract.

### 4.1 Required and optional sections

Level-2 headings (`## `), validated by exact line match (case-sensitive, no trailing
spaces):

| Heading | Req | Content rule |
|---------|-----|--------------|
| `## When to use` | REQUIRED, first section | Non-empty prose. The situations that should trigger this skill AND the situations that should not ("Not for …" lines encouraged — cf. the library's 何时不用 convention in `gemia/ai/skills/color-grade/SKILL.md`). |
| `## Steps` | REQUIRED, second section | MUST contain at least one numbered list item (a line matching `^[0-9]+\. `). Steps reference tools by **exact** dispatch-table name in backticks with their key arguments, e.g. `` `timeline_delete_clip` (`ripple: true`) `` — this is what makes a playbook executable rather than aspirational, and it feeds `tools_used` auto-extraction (§7.1). |
| `## Pitfalls` | optional | Failure modes, ordering constraints, cost traps. |
| `## Examples` | optional | Prose plus optional fenced code blocks (` ``` ` fences MUST be balanced). Typical content: one literal tool call as JSON. |

Rules:
- Required sections MUST appear in the order above; `## Pitfalls` and `## Examples`,
  when present, follow `## Steps` in that order.
- Duplicate occurrences of any of the four known headings → `E_LUS_BODY_SECTION`.
- Additional unknown `## ` headings are allowed **after** `## Steps` (forward compat)
  and are ignored by v1 tooling.
- No content may appear before `## When to use` except blank lines.

### 4.2 Content prohibitions (validator-enforced on save AND load)

1. **No secrets** — `E_LUS_SECRET`. The validator applies the same battery as the
   memory guard (`gemia/memory.py:149-175`): key-material patterns
   (`sk-[A-Za-z0-9]{16,}`, `AKIA[0-9A-Z]{16}`, `(?i)bearer\s+[a-z0-9._-]{16,}`,
   `-----BEGIN [A-Z ]*PRIVATE KEY-----`) and assignment patterns
   (`(?i)(api[_-]?key|token|secret|passwd|password)\s*[:=]\s*\S{8,}`), over the whole
   file (meta + body).
2. **No absolute user paths** — `E_LUS_ABS_PATH`. Regexes (multiline):
   `` (^|[\s"'`(=])/(Users|home|Volumes)/ ``, `[A-Za-z]:\\Users\\`, and
   `` (^|[\s"'`(=])~/ ``. Skills must reference assets by `asset_id` / library search and
   files by workspace-relative paths only — a playbook containing one user's disk
   layout is broken on every other machine and leaks private structure.
3. **Size** — total file ≤ 64 KiB (`E_LUS_TOO_LARGE`, §2). There is no separate body
   limit; the file bound is the bound.
4. **No library-closed craft recipes** — `E_LUS_CRAFT_NUMBERS` (added by the §12
   addendum, 2026-07-17; charter §14 S1). Scope differs from items 1-2: the scan
   covers the **body only, outside fenced code blocks** (metadata carries recall
   signals, not steps; a Pitfalls section may quote an anti-example in a fence).
   Save: typed rejection, nothing written. Load: the file is **quarantined from
   recall with a WARNING log**, never silently dropped (see §12.2).

**中文小结（正文）**：正文是模型执行的 markdown 手册。必需章节 `## When to use`、
`## Steps`（至少一条编号步骤，步骤用反引号写精确工具名+关键参数），可选 `## Pitfalls`、
`## Examples`（代码围栏必须配对）。全文件禁密钥、禁绝对用户路径（`/Users/`、`~/` 等），
整文件上限 64KB，违者保存即拒绝。

---

## 5. Checksum

```
body_bytes = all bytes of the file AFTER the '\n' that terminates the meta close-fence
             line, through EOF (includes the conventional leading blank line and the
             trailing '\n')
checksum   = "sha256:" + lowercase_hex(SHA-256(body_bytes))
```

- Covers the **body only**. Rationale: the meta block contains the checksum itself
  (self-reference would require canonicalization gymnastics), and meta edits are
  already diff-reviewable field-by-field; the body is the long free-text part where
  silent corruption or accidental truncation is the real risk.
- Writers (save, migrate, re-save) MUST recompute it on every write.
- Readers: see D3 — warning by default, error in strict mode.
- Both §9 examples carry real, verifiable checksums over their exact bytes.

**中文小结（校验和）**：`checksum` = 元数据闭合 `---` 行之后所有字节的 SHA-256（含结尾
换行），只盖正文不盖元数据；写入端每次重算，读取端默认只警告（严格模式才报错）。

---

## 6. Validation contract — `validate_lus`

Location: new module `gemia/lus.py` (no imports from `gemia.tools` — the store and
tools import *it*, keeping the parser dependency-light and testable standalone).

```python
@dataclass(frozen=True)
class LusMeta:
    name: str
    version: str
    lus_version: int
    title: str
    description: str
    triggers: tuple[str, ...]
    domain: str                      # "video" | "deck" | "cad" | "general"
    tools_used: tuple[str, ...]
    parameters: dict                 # JSON-Schema subset, §3.2
    author: str
    created_at: str                  # ISO 8601, tz-aware
    updated_at: str
    language: str                    # "zh" | "en" | "mixed"
    safety_requires_paid_generation: bool
    safety_mutates_project: bool
    checksum: str | None
    extra: dict                      # unknown fields, preserved for round-trip

@dataclass(frozen=True)
class LusWarning:
    code: str                        # W_LUS_*
    message: str
    field: str | None = None

class LusValidationError(ValueError):
    code: str                        # E_LUS_*
    message: str
    field: str | None                # set for E_LUS_META_FIELD
    line: int | None                 # 1-based line number when locatable

def validate_lus(
    text: str,
    *,
    known_tools: frozenset[str] | None = None,   # pass TOOL_NAMES at save time;
                                                 # None skips W_LUS_UNKNOWN_TOOL
    strict: bool = False,                        # escalates checksum mismatch
) -> tuple[LusMeta, str, list[LusWarning]]:      # (meta, body, warnings) — D7
```

`validate_lus` raises `LusValidationError` on the FIRST failing check, in the exact
order of the table below (so error precedence is deterministic and testable). The
companion functions are `parse_lus(text) -> (LusMeta, str)` (validate, discard
warnings) and `serialize_lus(meta, body) -> str` (canonical emitter, D5; always
recomputes `checksum` and enforces §2 rules on its own output).

### 6.1 Complete typed error list (check order = table order)

| Code | Exact trigger condition |
|------|------------------------|
| `E_LUS_ENCODING` | Input bytes are not valid UTF-8; OR text begins with U+FEFF (BOM); OR any `\r` byte present (D10). (When the API receives `str` not `bytes`, the UTF-8 sub-check is the caller's; BOM/CR checks still apply.) |
| `E_LUS_TOO_LARGE` | `len(text.encode("utf-8")) > 65536`. Checked before any parsing. |
| `E_LUS_MAGIC` | Line 1 does not match `^#!lus/[1-9][0-9]*$` exactly (missing, malformed, leading whitespace, minor component, etc.). |
| `E_LUS_VERSION` | Magic line well-formed but `<major>` ∉ `SUPPORTED_LUS_MAJORS` (= `{1}`). Message includes both the file's major and the supported set. |
| `E_LUS_META_OPEN` | Line 2 is not exactly `---`. |
| `E_LUS_META_TOO_LARGE` | No close-fence line (`---`) found within the first 8,192 bytes (D6) although the file continues past them. |
| `E_LUS_META_UNTERMINATED` | EOF reached without a close-fence line. |
| `E_LUS_META_PARSE` | The block between fences fails `safe_load`; OR parses to a non-mapping; OR uses a forbidden feature (anchor/alias/tag detected by pre-scan regex `(?m)^\s*[^#\n]*[&*!]` on tokens outside quoted scalars — implementation may use PyYAML events instead); OR any mapping key is not a string. |
| `E_LUS_META_FIELD` | Any per-field failure from the §3.2 table: required field missing; wrong type; regex/enum/length violation; `lus_version` ≠ magic major; invalid semver; invalid/naive ISO 8601; `updated_at` < `created_at`; `parameters` outside the schema subset (bad key, bad `type`, depth > 4, `required` ⊄ `properties`); `safety` missing a required key or a value not a bool; `checksum` present but not matching `^sha256:[0-9a-f]{64}$` (malformed ≠ stale — malformed is a hard field error even in non-strict mode); `triggers` empty/oversized/duplicate. `error.field` names the offending field (dotted for nested, e.g. `safety.mutates_project`, `parameters.properties.foo.type`). |
| `E_LUS_BODY_EMPTY` | Body (bytes after close fence) is empty or whitespace-only. |
| `E_LUS_BODY_SECTION` | Missing `## When to use` or `## Steps` (exact line match); required/optional known sections out of the §4.1 order; duplicate known heading; non-blank content before `## When to use`; `## Steps` contains no line matching `^[0-9]+\. `. |
| `E_LUS_BODY_FENCE` | Unbalanced code fences: the count of lines matching `` ^``` `` is odd. |
| `E_LUS_SECRET` | Any §4.2 secret pattern matches anywhere in the file (meta or body). Message includes the pattern class, never the matched text (do not echo secrets). |
| `E_LUS_ABS_PATH` | Any §4.2 absolute-user-path pattern matches anywhere in the file. |
| `E_LUS_CRAFT_NUMBERS` | Any `CRAFT_CLOSED_DOMAINS` pattern matches anywhere in the file — a raw-number craft recipe for a domain closed by an installed point-library (charter §14 S1). Message names the domain and the closing library verb. *(16th code, added by the §12 addendum, 2026-07-17.)* |
| `E_LUS_CHECKSUM` | Strict mode only (`strict=True`): `checksum` present, well-formed, and ≠ SHA-256 of the actual body bytes. |

### 6.2 Warnings (never abort)

| Code | Trigger |
|------|---------|
| `W_LUS_UNKNOWN_TOOL` | `known_tools` provided and some `tools_used` entry ∉ it. One warning per unknown name, `field="tools_used"`. |
| `W_LUS_UNKNOWN_FIELD` | Metadata contains a key outside the §3.2 table (preserved in `meta.extra`), or `safety` has extra keys. |
| `W_LUS_CHECKSUM_MISSING` | `checksum` absent (non-strict read). |
| `W_LUS_CHECKSUM_STALE` | `checksum` present and well-formed but ≠ body hash (non-strict read). Next save auto-heals. |
| `W_LUS_NAME_MISMATCH` | Only when the caller passes the source path via the optional `filename=` kwarg: basename stem ≠ `meta.name`. |

**中文小结（校验契约）**：`gemia/lus.py` 里 `validate_lus(text, known_tools, strict) ->
(meta, body, warnings)`，按固定顺序做检查、第一个失败即抛带 `code`/`field`/`line` 的
`LusValidationError`；16 个错误码覆盖编码、大小、magic、版本、元数据围栏/解析/字段、
正文章节/围栏、密钥、绝对路径、手艺数字（`E_LUS_CRAFT_NUMBERS`，§12 addendum）、
严格模式校验和；5 个警告码覆盖未知工具、未知字段、校验和缺失/过期、文件名不一致。

---

## 7. Lifecycle integration

### 7.1 save_skill → .lus

`dispatch_save_skill` (`gemia/tools/save_skill.py:43`) keeps its model-facing argument
schema unchanged (`gemia/tools/_schema.py:925-947`) — no protocol or tool-schema churn.
The distillation path maps args to a `.lus` file:

| save_skill arg | .lus destination |
|----------------|------------------|
| `name` | `title` verbatim. `name` (machine key) is derived: NFKD → lowercase → non-`[a-z0-9]` runs → `-` → collapse/trim; if the result is empty (pure-CJK input), use `skill-<sha256(title_utf8)[:8]>`. Deterministic, so re-saving the same title hits the same file (idempotency preserved). |
| `when_to_use` / `trigger` | `description` (first 500 chars, single-lined) AND the `## When to use` section body. |
| `steps` / `ops` / `recipe` | `## Steps` as a numbered list (existing coercion `_coerce_steps`, `gemia/skill_store.py:57-76`, is reused; already-numbered strings are not double-numbered). |
| `notes` | `## Pitfalls` (omitted when empty). |
| `tags` | `triggers` (deduplicated, capped at 16). |
| — (derived) | `tools_used`: every token in the steps text matching `^[a-z][a-z0-9_]*$` that is ∈ `TOOL_NAMES` (auto-extraction; unknowns simply aren't extracted). `language`: `zh` if the body contains CJK codepoints and no majority-ASCII sentences, `en` if no CJK, else `mixed`. `domain`: `video` default (v3 agent context) until the tool schema grows an optional `domain` arg (WP2). `safety.requires_paid_generation`: true iff `tools_used` ∩ {`generate_image`, `generate_video`, `generate_audio`, `narrate`}. `safety.mutates_project`: true iff `tools_used` ∩ `PLAN_BLOCKED_TOOLS`. `author`: `"lumeri-agent"`. Timestamps + `checksum`: computed. |

Write algorithm (all inside `DistilledSkillStore`, root unchanged =
`distilled_skills_dir()`, `gemia/skill_store.py:36-49`):

1. Build meta+body; run `validate_lus(serialize_lus(meta, body), known_tools=TOOL_NAMES)`
   — save REJECTS on any `E_*` (secrets/paths/size cannot enter the store), and
   returns warnings in the tool result's `summary`.
2. If `<name>.lus` exists: preserve its `created_at` (current behavior,
   `gemia/skill_store.py:122-127`); copy the old file to `<name>.lus.bak`
   (**exactly one** .bak generation is kept — overwritten each re-save); set `version`
   = caller-supplied value if valid semver, else old version with **patch+1** when
   meta-or-body (excluding `updated_at`/`checksum`) changed, else unchanged.
3. Atomic write: temp file in the same directory + `os.replace` (the store is shared
   by concurrent sessions).
4. Plan-mode/budget classification is untouched: `save_skill` stays in
   `PLAN_BLOCKED_TOOLS` (`gemia/plan_mode.py:68`) and keeps its `budget_guard.py:62`
   entry.

The backward-compat `source` branch (build artifacts, `gemia/tools/save_skill.py:64-67`)
is NOT converted to `.lus` (D8) and keeps delegating to `gemia/tools/build.py:406`.

### 7.2 recall_skills — metadata-only ranking, body on selection

Today recall loads every full JSON (`gemia/skill_store.py:247`). With `.lus`:

1. **Scan (cheap):** for each `<root>/*.lus`, read at most the first 8,192 bytes,
   split off the meta block (guaranteed to fit, D6), parse metadata only. Malformed
   files are skipped (recall never throws because one file is corrupt — same posture
   as today's `except Exception: continue`, `gemia/skill_store.py:154-156`).
2. **Rank on metadata:** `_relevance` weights remap to `name`+`title` 3.0,
   `description` 2.0, `triggers` 2.0. Behavioral delta vs today: `steps`/`notes` text
   (weights 1.0) no longer participates in ranking. Accepted trade-off: `description`
   and `triggers` are now *designed* to carry the matching signal, and loading every
   body to score at weight 1.0 would defeat the cheap scan. Alternative (score bodies
   too) is a one-line revert if recall quality regresses — the scan keeps the full
   text one `read_text` away.
3. **Load bodies for winners only:** the top-`limit` (≤25, `save_skill.py:119`)
   selections get a full `validate_lus` load; the tool result keeps the existing
   `_recall_view` shape (`gemia/skill_store.py:276-285`) so the model-facing contract
   does not change: `when_to_use` ← `description`, `steps` ← the numbered items
   parsed from `## Steps`, `notes` ← `## Pitfalls` text, `tags` ← `triggers`.
   Checksum-stale warnings are appended to `notes` as a single bracketed line.
4. Legacy `*.json` files still present (unmigrated) are scanned exactly as today and
   merged; on `name` collision the `.lus` wins (D9). Library skills
   (`_library_skills`, `gemia/skill_store.py:178-206`) participate unchanged.

### 7.3 Prompt injection — what exists and what changes

**Honest baseline:** nothing is injected today. Skills reach the model only as
`recall_skills` results; the `{{memory}}` slot carries MEMORY.md
(`gemia/agent_loop_v3.py:452-463,523`), and `system_v3.md` never mentions skills
(§0.1). The tool description ("Call this FIRST", `_schema.py:966-968`) is the only
standing nudge.

**v1 keeps tool-results-only surfacing.** Rationale: injecting N skill digests into
every turn spends tokens on all sessions to help some; recall is already a $0 tool
call the model is instructed to make. One additive change is specced as optional
WP5: a single digest line appended by `_memory_for_prompt`
(`gemia/agent_loop_v3.py:452`) when the store is non-empty —
`Saved skills available: <n> (top by recency: a, b, c…) — call recall_skills before multi-step work.`
— capped at 200 chars, metadata-scan only, never bodies. No new `{{slot}}` is added,
so `system_v3.md` and both frontends are untouched (no protocol change, no parity
obligation).

**中文小结（生命周期）**：`save_skill` 参数原样映射到 `.lus`（中文名进 `title`，机器键
派生 kebab-case，工具名从步骤文本自动提取，付费/写入两个安全位自动推导）；保存前必须过
`validate_lus`，密钥/绝对路径/超限直接拒存；重存保留 `created_at`、补丁号自增、留一份
`.bak`、原子写。召回改为"扫元数据排序、只给入选者加载正文"，对模型的返回结构不变；
`.json` 未迁移的照旧参与、同名时 `.lus` 优先。提示词今天不注入技能，v1 维持现状，仅留
一个可选的一行摘要（WP5）。

---

## 8. Migration

### 8.1 Source format

Exactly the DistilledSkillStore JSONs (§0.1) under `distilled_skills_dir()`:
`{name, source, when_to_use, steps: [str], notes, tags, created_at, updated_at}`.
Observed live: 2 files (`audio_ducking_setup.json`, `batch_rough_cut.json`).

### 8.2 Mapping (same table as §7.1, plus)

- JSON `name` (may be CJK) → `title`; machine `name` derived per §7.1. Collisions
  after derivation (two titles hashing/slugging identically) abort THAT file with a
  reported conflict; the run continues.
- JSON `created_at`/`updated_at` carried over verbatim when parseable, else both set
  to migration time (and noted in the report).
- `version` starts at `1.0.0`. `domain`=`video`, `language` auto-detected,
  `safety.*`/`tools_used` derived per §7.1.
- Bodies that fail §4.2 prohibitions (a legacy skill containing an absolute path,
  secret-looking text, or — since the §12 addendum — a library-closed craft recipe,
  `E_LUS_CRAFT_NUMBERS`) are NOT silently rewritten: the file is skipped, reported, and
  left un-renamed for manual review. Migration must not launder policy violations.
  A craft-violating legacy JSON that stays unmigrated does not keep flowing to the
  model either: the dual-read path runs the same craft scan and quarantines it from
  recall with a WARNING log (charter §14 S1 applies to both formats).

### 8.3 Migrator behavior

`scripts/migrate_skills_to_lus.py` (also exposed as `python -m gemia migrate-skills`):

1. For each non-dotfile `*.json` in the store root: parse → build `.lus` text →
   `validate_lus` → atomic-write `<name>.lus` → rename original to
   `<original-basename>.json.bak` (constraint: originals become `.bak`, not deleted).
2. **Idempotent:** a JSON is skipped when (a) its basename already ends `.json.bak`
   — impossible by glob, listed for clarity — or (b) the derived `<name>.lus` already
   exists, or (c) a `<original>.json.bak` sibling exists. Running the migrator twice
   produces a byte-identical tree (test gate, §11 WP3).
3. `--dry-run` prints the plan without writing. Default run prints a per-file report:
   `migrated | skipped(already) | skipped(violation: E_…) | conflict(name)`.
4. Store dual-read (D9) means migration is non-blocking: an unmigrated store keeps
   working; the migrator is idempotent cleanup, not a flag-day.

### 8.4 Explicitly NOT migrated (D8 recap)

`skills_v2/*.json` (executable PlanEngine templates — `gemia/__main__.py:791+` would
break), build-artifact `.py`+`.json` pairs (`gemia/tools/build.py:406` — code, not
playbooks), and `gemia/ai/skills/*/SKILL.md` (packaged planner library with its own
router/combos; `.lus` copies its shape so future convergence is a rename, not a
rewrite).

**中文小结（迁移）**：一次性脚本只迁沉淀库 JSON（当前实际 2 个文件）：字段按表映射、
中文名进 `title`、时间戳保留、版本从 1.0.0 起；原文件改名 `.bak` 保留；重复运行零改动
（幂等测试把关）；含密钥/绝对路径的旧技能不迁、只报告，留人工处理。`skills_v2`、build
产物、内置 SKILL.md 库明确不迁。

---

## 9. Reference examples (byte-exact)

Both files below are the **exact bytes on disk**, including the final trailing
newline. Their `checksum` fields are real SHA-256 digests of their body bytes as
defined in §5 (verified mechanically, and verified to parse under
`gemia/ai/skill_yaml.py`'s fallback parser with PyYAML absent). They double as
mandatory parser test fixtures (WP1).

### 9.1 `beat-cut-rough-cut.lus` — zh, video (卡点粗剪)

````
#!lus/1
---
name: beat-cut-rough-cut
version: 1.0.0
lus_version: 1
title: 卡点粗剪
description: 根据背景音乐的节拍点，把多段视频素材快速拼成一条踩点粗剪时间线，段落切换对齐鼓点。
triggers:
  - 卡点
  - 卡点剪辑
  - 踩点
  - 音乐卡点
  - beat cut
  - beat sync edit
domain: video
tools_used:
  - analyze_media
  - search_library
  - timeline_add_track
  - timeline_insert_clip
  - timeline_split_clip
  - timeline_delete_clip
  - get_timeline
  - project_export
parameters:
  type: object
  properties:
    music_asset:
      type: string
      description: 用作节拍参考的音乐素材 asset_id。
    clip_count:
      type: integer
      minimum: 2
      description: 参与卡点的素材段数，缺省用全部候选素材。
  required:
    - music_asset
author: lumeri-agent
created_at: 2026-07-06T08:00:00+00:00
updated_at: 2026-07-06T08:00:00+00:00
language: zh
safety:
  requires_paid_generation: false
  mutates_project: true
checksum: sha256:db28da0b4bf97f8760a734f19ed948904f8570ffe1bd30dad8dc0435bedf3928
---

## When to use
用户提供（或库里已有）一段背景音乐和多段视频素材，要求"卡点""踩点"或 beat cut 粗剪：
画面切换点要落在音乐鼓点上。适用于粗剪初版，不负责精修转场和调色。

## Steps
1. 用 `search_library` 确认候选视频素材与音乐素材的 asset_id；音乐即参数 `music_asset`。
2. 对 `music_asset` 调 `analyze_media`（`analysis: "beats"`），拿到节拍时间戳列表 beats[]。
3. 用 `timeline_add_track` 确保 V1 视频轨与 A1 音频轨就绪，把音乐插入 A1（`timeline_insert_clip`，`start_sec: 0`）。
4. 按素材顺序循环 `timeline_insert_clip` 到 V1：第 i 段的入点对齐 beats[i]，
   目标时长 = beats[i+1] - beats[i]；素材偏长时先插入再裁。
5. 对超长片段用 `timeline_split_clip` 在下一个节拍处切开，`timeline_delete_clip`
   （`ripple: true`）删掉多余尾段，保证每段结束正好落在节拍上。
6. 用 `get_timeline` 自检：每个 V1 片段的 start_sec 与某个 beat 的偏差应 < 0.05s。
7. `project_export` 导出粗剪样片给用户预览，再按反馈微调。

## Pitfalls
- beats[] 首拍常不在 0s：第一段画面从 beats[0] 开始，0 到 beats[0] 之间留黑或垫全景。
- 素材比节拍间隔短时不要拉伸变速，跳过该拍或换素材，变速交给后续精修。
- `ripple: true` 会移动后续所有片段，先删后插会破坏已对齐的节拍，务必按时间顺序操作。

## Examples
```json
{"tool": "timeline_insert_clip", "args": {"asset_id": "vid_012", "track": "V1", "start_sec": 1.92, "duration_sec": 0.96}}
```
````

(File is 2,716 bytes; body SHA-256 as shown in its `checksum` field.)

### 9.2 `pitch-deck-title-cards.lus` — en, deck

```
#!lus/1
---
name: pitch-deck-title-cards
version: 1.0.0
lus_version: 1
title: Pitch-deck style title cards
description: Turn a list of section headings into clean slide-like title cards and place them as chapter breaks on the timeline.
triggers:
  - title card
  - title cards
  - chapter card
  - section heading
  - deck style
  - slide intro
domain: deck
tools_used:
  - generate_image
  - add_overlay
  - timeline_insert_clip
  - get_timeline
  - get_safe_areas
  - project_export
parameters:
  type: object
  properties:
    headings:
      type: array
      items:
        type: string
      description: Section headings, one card per entry, in display order.
    card_seconds:
      type: number
      minimum: 1
      description: How long each card holds on screen. Default 3.
  required:
    - headings
author: lumeri-agent
created_at: 2026-07-06T08:00:00+00:00
updated_at: 2026-07-06T08:00:00+00:00
language: en
safety:
  requires_paid_generation: true
  mutates_project: true
checksum: sha256:cefb5e697fda3533a12fa7336f13dfed83fb41b3adf770431512cfc68928ffee
---

## When to use
The user wants slide-deck style chapter/section cards inside a video: a clean
background, one large heading per card, inserted between existing segments.
Not for lower-thirds or subtitles (use `subtitle` / `add_overlay` directly).

## Steps
1. Confirm the `headings` list with the user if it was not given explicitly.
2. Call `get_safe_areas` once to get the title-safe rectangle for the project resolution.
3. For each heading, call `generate_image` with a minimal flat-background prompt
   (state brand color if the user gave one; otherwise neutral dark background).
   This is a PAID generation - respect budget gates and reuse one background for
   all cards when possible.
4. Call `add_overlay` to set the heading text on the card image, centered inside
   the title-safe area, one text layer per card.
5. Call `get_timeline` to find segment boundaries, then `timeline_insert_clip`
   each card at its section start with `duration_sec: card_seconds`.
6. `project_export` a preview and ask the user to confirm typography before
   any further styling passes.

## Pitfalls
- Do not regenerate the background per card; one `generate_image` call plus
  per-card `add_overlay` text is cheaper and visually consistent.
- Long headings overflow the safe area: wrap to two lines rather than shrinking
  the font below readable size.
- Inserting cards shifts later clips; insert from the last section backwards so
  earlier boundaries stay valid while you work.
```

(File is 2,549 bytes; body SHA-256 as shown in its `checksum` field. Note the inner
` ```json ` fence in example 9.1 is part of that file's body; the outer fence above is
this document's quoting.)

**中文小结（示例）**：两个逐字节完整示例——中文视频技能"卡点粗剪"（免费工具链，
`mutates_project: true`）和英文 deck 技能"标题卡"（含付费生成，
`requires_paid_generation: true`），`checksum` 均为对正文真实计算的 SHA-256，可直接作为
解析器测试夹具。

---

## 10. Parity and future interactions

### 10.1 CLI and web exposure

- **CLI (`~/Code/lumeri-cli`)**: a local `/skills` slash command (no server round-trip
  needed for v1 — the CLI runs on the same machine and can read
  `~/.gemia/skills/*.lus` metadata directly, mirroring how it stays offline-friendly
  in mock mode). Rendering: one row per skill —
  `name  title  domain  vX.Y.Z  updated YYYY-MM-DD  [paid] [mutates]` — sorted by
  `updated_at` desc; `/skills <name>` prints the full body. No SSE/protocol change ⇒
  no `v3_contract.py` bump and no `contract.json` re-export; the hard parity rule
  (protocol changes land web+CLI+tests same commit) is **not triggered** because the
  protocol is untouched. The CLI feature ships with its own `npm test` snapshot per
  the lumeri→CLI feature-parity memory rule.
- **Web (`static/v3/v3.js`)**: v1 adds no panel; skills remain visible as tool-call
  events in the transcript. When a skills panel lands later it should consume a plain
  REST endpoint (`GET /skills` returning the §6 `LusMeta` list as JSON) rather than a
  new SSE kind — read-only listings don't belong in the event stream.

### 10.2 Multi-agent (planned)

`.lus` bodies are exactly the shape a subtask playbook needs: when the orchestrator
delegates "cut this to the beat" to a sub-agent, it recalls the skill, passes the
**body** as the sub-agent's task brief, `parameters` as the typed argument contract
for the delegation call, and `tools_used` as a pre-filter for the sub-agent's tool
allowlist (a sub-agent running `beat-cut-rough-cut` needs the 8 listed tools, not all
98). `safety.mutates_project` gates delegation while plan mode is active — an
orchestrator in plan mode may *read* any skill but may only dispatch sub-agents for
skills with `mutates_project: false`, keeping the fail-closed spirit of
`gemia/plan_mode.py` without touching its frozensets. Nothing in the format needs to
change for this; multi-agent consumes `.lus` as-is, which is why the metadata carries
machine-checkable fields (`parameters`, `tools_used`, `safety`) rather than prose.

### 10.3 MCP (planned) — skills are NOT exposed as MCP tools in v1

Justification: an MCP tool is a callable function with a stable schema and
deterministic-ish execution; a `.lus` skill is a *prompt-shaped playbook* whose
"execution" is an agent following instructions — exposing each skill as a tool would
advertise N dynamically-changing tools (polluting every connected client's tool budget
and cache), promise a `parameters` contract that is advisory rather than enforced, and
hand foreign MCP clients a prompt-injection vector (a saved skill's body would execute
inside *their* agent loop with *their* credentials). If MCP integration wants skills,
the correct v1 surface is one static tool — `lumeri_recall_skills(query) -> metadata
list` — which is read-only, fixed-schema, and keeps the decision to follow a playbook
inside the calling agent. Revisit only if a concrete client needs direct invocation,
and then behind an explicit per-skill `expose_mcp: true` opt-in (a v2 metadata field,
additive per §2.2).

**中文小结（对等与未来）**：CLI 加本地 `/skills` 列表/详情命令（不动 SSE 协议，故不触发
双端 parity 硬规则，但 CLI 功能对等照常带测试落地）；web 暂不加面板，将来走 REST 而非新
事件。多智能体把 `.lus` 正文当子任务作业书、`parameters` 当委派参数契约、`tools_used`
当子代理工具白名单、`mutates_project` 在 plan mode 下限制委派。v1 不把技能暴露成 MCP
工具：技能是提示词剧本不是函数，动态 N 工具污染客户端、参数契约不可执行、还有跨客户端
提示注入风险；要暴露也只暴露一个只读的 `lumeri_recall_skills`。

---

## 11. Implementation plan — work packages and test gates

Ordering is strict; each WP lands with its gate green before the next starts. No step
changes the SSE protocol, so `v3_contract.py` / `contract.json` stay untouched
throughout (drift tests `tests/test_v3_contract.py` + CLI `test/contract.mjs` must
stay green as a side condition of every WP).

| WP | Content | Size | Test gate (must be red-then-green) |
|----|---------|------|-------------------------------------|
| **WP1** | `gemia/lus.py`: `validate_lus` / `parse_lus` / `serialize_lus` / `LusMeta` / `LusValidationError` / `LusWarning`, per §2–§6. No call sites yet. | ~0.5 day | New `tests/test_lus_format.py`: (a) **round-trip** — `serialize_lus(*parse_lus(t)) == t` for both §9 fixtures embedded verbatim, and `parse(serialize(meta, body))` re-yields equal meta/body for a generated matrix (CJK, flow-style YAML input, JSON-object metadata input); (b) **red cases per error code** — one minimal fixture per `E_*` in §6.1 asserting the exact `code` (and `field` for three representative `E_LUS_META_FIELD` shapes), plus check-order tests (e.g. oversized file with bad magic → `E_LUS_TOO_LARGE`, not `E_LUS_MAGIC`); (c) **warning cases** — all five `W_*`; (d) checksum: both fixtures verify; a mutated body yields `W_LUS_CHECKSUM_STALE` non-strict and `E_LUS_CHECKSUM` strict; (e) both fixtures parse with `skill_yaml`'s fallback (PyYAML import monkeypatched away). |
| **WP2** | Store + tool integration per §7: `DistilledSkillStore` writes `.lus` (atomic, `.bak`, version bump, `created_at` preservation), dual-read `.lus`+`.json`, recall meta-scan + body-on-selection, `save_skill` arg mapping incl. derived fields, save-time validation rejection surfaced as tool error. | ~1 day | `tests/test_skill_distill.py` extended (keeps `GEMIA_SKILL_STORE_DIR` redirection): save → `<name>.lus` exists and `validate_lus` passes; re-save same title → same file, patch bumped, one `.bak`, `created_at` preserved; recall e2e finds it by zh query; legacy `.json` planted beside → still recalled; same-name `.json`+`.lus` → `.lus` wins; save with `sk-`-style secret in steps → tool call fails with `E_LUS_SECRET`, nothing written; dispatcher/schema wiring assertions stay green unchanged. |
| **WP3** | Migrator (`scripts/migrate_skills_to_lus.py` + `python -m gemia migrate-skills`) per §8. | ~0.5 day | New `tests/test_lus_migration.py`: fixture dir with 3 legacy JSONs (one CJK name, one with unparseable timestamp, one containing an absolute path) → run: 2 migrated with correct field mapping + `.json.bak` present, 1 skipped-with-violation and left untouched; **idempotency** — second run over the result changes zero bytes (tree hash equal); dual-read confirms recall parity before/after migration. |
| **WP4** | CLI `/skills` + `/skills <name>` per §10.1 (plus `python -m gemia skills list` for symmetric local access). | ~0.5–1 day | lumeri-cli `npm test`: snapshot test of the listing render over two fixture `.lus` files (copies of §9); unknown-name error path; `test/contract.mjs` unchanged-and-green (proves no protocol drift). |
| **WP5** (optional) | Prompt digest line per §7.3; docs cross-links. | ~0.25 day | Backend test: prompt build with non-empty store contains exactly one digest line ≤200 chars; with empty store contains none; `system_v3.md` byte-identical (slot-free injection). |

Total: ~2.5–3 days. Rollback story: WP2's dual-read means reverting any later WP never
strands data; `.bak` files cover both re-save and migration reversals.

**中文小结（实施计划）**：五个工作包严格顺序：WP1 纯解析/校验模块 + 全错误码红绿测试与
逐字节夹具往返；WP2 存储与工具接入（原子写、`.bak`、版本自增、双读、召回改造）+ e2e；
WP3 迁移器 + 幂等性测试；WP4 CLI `/skills`（无协议变更，contract 测试保持绿即是证明）；
WP5 可选的一行提示词摘要。全程不碰 SSE 契约。

---

## 12. Charter arbitration addendum — skill/library boundary (2026-07-17)

Status: RATIFIED alongside point-library charter v1.3 §14 (user-approved
2026-07-17). This addendum extends the locked design of §2–§7 without
changing any prior decision; it adds one validation check and records the
arbitration between this spec and the charter.

### 12.1 Where skills sit (arbitration)

The point-library charter (`gemia/docs/point-library-charter.md`, RATIFIED)
is the authority on **which layer knowledge may live in**; this spec is the
authority on **how a .lus file is written**. Where the two documents'
worldviews conflict, **charter §14 wins**. The division it fixes:

- **Craft (taste) belongs to point libraries** — colour recipes, curve
  control points, easing/choreography numbers. A skill MUST NOT carry a
  domain's craft as raw numbers once an installed library closes that domain
  (charter §14 rule S1; anti-pattern AP34).
- **Process belongs to skills** — cross-domain workflows, external-service
  usage, per-machine preference, pitfalls. These are the legal long-term
  residents of `~/.gemia/skills/` (charter §14 rule S2) and the reason this
  format exists.
- When a skill references a library-closed domain it speaks the library's
  creative language (verb + look/archetype words + 0..1 semantic axes),
  never numeric recipes (charter §14 rule S3).

### 12.2 E_LUS_CRAFT_NUMBERS — the 16th error code

`validate_lus` gains check **14b** (after `E_LUS_ABS_PATH`, before checksum):
the **body, outside fenced code blocks,** is scanned line-by-line
(`gemia/lus.py::find_craft_leak`) against `CRAFT_CLOSED_DOMAINS` — a registry
of `(domain, closing tool verb, recipe patterns)` for domains closed by an
**installed** point-library (currently `grading`/`grade` and
`motion`/`vector_motion`; the bounded pending list
`CRAFT_GUARD_PENDING_VERBS` names installed creative verbs not yet covered,
and `tests/test_charter_integrity.py` pins closed ∪ pending == the installed
reference set). Metadata is exempt (it carries recall signals, not steps);
fences are exempt (quoting an anti-example in Pitfalls is legal). A match
raises typed `E_LUS_CRAFT_NUMBERS` — save is rejected, nothing is written.

**Load-side semantics (retroactivity):** a pre-existing file that trips the
check is **quarantined from recall with a WARNING log** (file stays on disk,
excluded from `list_distilled`/recall until fixed) — never silently dropped.
The legacy-JSON dual-read path runs the same scan.

The guard matches recipe **shape**, not keywords alone (adversarially
reviewed 2026-07-17): bracketed lists (`shadows=[0.02, 0, -0.02]`,
`tone_curve: [...]`, also via backtick/bold/`to [` wrappers), per-channel
Chinese offsets (`阴影 RGB 各 +0.02`), `cubic-bezier(0.4, …)`, unit-less
control-point decimals (`stagger=0.08`), and same-line `lift`+`gamma`+`gain`
triple assignments. It MUST NOT match the libraries' own semantic axes
(`warmth: 0.7`, `lift: 0.3`, `stagger_spread: 0.6`), archetype names,
placement/timing quantities (`x=120`, "trim to 3s", "fade easing: 0.3s",
"duck by -12dB"), or everyday editing prose ("highlights: 3 best moments",
timestamps `0:32`, "高光时刻", "drop shadows: 0.15 opacity"). Free-form prose
paraphrases of a recipe are deliberately out of regex scope (charter §14.3) —
the recall-side filter and review are the backstop. Test gate:
`tests/test_lus_format.py` (reject/reject/pass + fence + check-order) +
`tests/test_skill_distill.py` (save rejection + two quarantine tests).

**中文小结（仲裁）**：宪章 §14 管"什么知识住哪层"，本规范管"文件怎么写"；冲突以宪章
§14 为准。品味归库、流程归技能；技能提到已关闭域只说创作语言。新增第 16 个错误码
`E_LUS_CRAFT_NUMBERS`（检查 14b）：已安装库关闭的域，其手艺**配方形状**（中英文括号
配方、逐通道偏移、无单位控制点小数、LGG 三连）在保存时 typed 拒存、零写盘；只扫正文
且豁免围栏与元数据；读侧命中=隔离+WARNING 日志，绝不静默；守卫宁窄勿宽，绝不误杀
语义轴、放置量与日常剪辑词汇（"highlights: 3"、"高光时刻"）。

---

## Appendix A — quick reference card

```
file      = "#!lus/1" LF "---" LF yaml-meta "---" LF body
limits    = file ≤ 64 KiB · meta closes ≤ 8 KiB · UTF-8 · LF only · no BOM
meta      = name version lus_version title description triggers domain
            [tools_used] [parameters] [author] created_at updated_at language
            safety{requires_paid_generation, mutates_project} checksum
body      = "## When to use" → "## Steps" (numbered, exact tool names)
            → ["## Pitfalls"] → ["## Examples"] (balanced fences)
checksum  = "sha256:" + hex(SHA256(bytes after close-fence LF … EOF))
errors    = E_LUS_ENCODING E_LUS_TOO_LARGE E_LUS_MAGIC E_LUS_VERSION
            E_LUS_META_OPEN E_LUS_META_TOO_LARGE E_LUS_META_UNTERMINATED
            E_LUS_META_PARSE E_LUS_META_FIELD E_LUS_BODY_EMPTY
            E_LUS_BODY_SECTION E_LUS_BODY_FENCE E_LUS_SECRET E_LUS_ABS_PATH
            E_LUS_CRAFT_NUMBERS E_LUS_CHECKSUM(strict)
warnings  = W_LUS_UNKNOWN_TOOL W_LUS_UNKNOWN_FIELD W_LUS_CHECKSUM_MISSING
            W_LUS_CHECKSUM_STALE W_LUS_NAME_MISMATCH
store     = ~/.gemia/skills/<name>.lus (GEMIA_SKILL_STORE_DIR override)
            re-save → patch bump + one .lus.bak · migrate → .json → .lus + .json.bak
```
