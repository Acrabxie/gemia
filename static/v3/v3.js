/* Lumeri v3 frontend — vanilla JS, no build step.
 *
 * Connects to the agent loop via:
 *   POST /sessions                              create session
 *   POST /sessions/{id}/assets                  raw body + X-Filename
 *   POST /sessions/{id}/turn                    {"message": "..."} (202)
 *   GET  /sessions/{id}/stream                  EventSource + last_event_id replay
 *   GET  /sessions/{id}/assets/{aid}            preview URL for the asset
 *   POST /sessions/{id}/resume                  restore durable runner
 *   GET  /projects/{pid}/artifacts/{aid}         persistent preview URL
 *   POST /projects/{pid}/runs/{rid}/review       human acceptance/revision
 *   POST /sessions/{id}/close                   teardown
 *
 * Invariants (mirror the agent loop's promises):
 *   - Every event kind has a handler. Unknown kinds raise a visible
 *     banner — never silent drop.
 *   - Tool execution is presented as a high-level activity state; raw tool
 *     payloads and model work logs never enter the user-facing stream.
 *   - Durable project previews load from /projects/{pid}/artifacts/{aid};
 *     /sessions/{id}/assets/{aid} remains a v1 compatibility alias.
 */

(async function () {
  "use strict";
  const apiFetch = (...args) => window.LumeriApi["fetch"](...args);

  function byokAllowed() {
    return true;
  }

  // The personal iPad package owns its workspace locally. Its model request is
  // one-shot and never creates a Mac session/project/timeline.
  const isLocalWorkspace = globalThis.__lumeriLocalWorkspace === true;

  const $ = (sel) => document.querySelector(sel);
  const isApplePlatform = /Mac|iPhone|iPad|iPod/.test(navigator.platform || navigator.userAgent || "");
  const shortcutPrefix = isApplePlatform ? "⌘" : "Ctrl+";
  const uploadShortcutLabel = $("#upload-shortcut-label");
  if (uploadShortcutLabel) uploadShortcutLabel.textContent = `${shortcutPrefix}U`;
  const pageParams = new URLSearchParams(location.search || "");
  const cliPreviewSessionId = pageParams.get("mode") === "cli-preview"
    ? (pageParams.get("session") || "").trim()
    : "";
  const isCliPreview = !!cliPreviewSessionId;
  document.documentElement.classList.toggle("cli-preview", isCliPreview);
  if (isCliPreview) document.title = "Lumeri Video · CLI Preview";

  // Inline the icon sprite once so every <use href="#i-*"> resolves, including
  // ones rendered before the fetch lands (SVG <use> re-resolves on DOM insert).
  apiFetch("/v3/icons.svg")
    .then((r) => (r.ok ? r.text() : ""))
    .then((t) => {
      if (!t) return;
      const holder = document.createElement("div");
      holder.innerHTML = t;
      const sprite = holder.querySelector("svg");
      if (sprite) document.body.prepend(sprite);
    })
    .catch(() => {});

  const els = {
    sessionLabel: $("#session-id-label"),
    connPill: $("#connection-pill"),
    projectBtn: $("#project-btn"),
    setupBtn: $("#setup-btn"),
    projectNameLabel: $("#project-name-label"),
    newProjectBtn: $("#new-project-btn"),
    newSessionBtn: $("#new-session-btn"),
    projectSidebar: $("#project-sidebar"),
    projectSidebarBody: $("#project-sidebar-body"),
    timeline: $("#timeline"),
    emptyState: $("#empty-state"),
    assetGrid: $("#asset-grid"),
    deliveryReviewMaster: $("#delivery-review-master"),
    deliveryReviewVideo: $("#delivery-review-video"),
    deliveryReviewMeta: $("#delivery-review-meta"),
    deliveryReviewOpen: $("#delivery-review-open"),
    timelinePreviewEmpty: $("#timeline-preview-empty"),
    mediaLibraryGrid: $("#media-library-grid"),
    libraryRefreshBtn: $("#library-refresh-btn"),
    libraryRoughcutBtn: $("#library-roughcut-btn"),
    libraryAnnotateBtn: $("#library-annotate-btn"),
    roughcutJobStatus: $("#roughcut-job-status"),
    uploadInput: $("#upload-input"),
    uploadBtn: $("#upload-btn"),
    promptInput: $("#prompt-input"),
    voiceInputStatus: $("#voice-input-status"),
    sendBtn: $("#send-btn"),
    inputShell: $("#input-shell"),
    sandboxBtn: $("#sandbox-toggle-btn"),
    planBtn: $("#plan-toggle-btn"),
    planBar: $("#plan-bar"),
    askDock: $("#ask-dock"),
    slashMenu: $("#slash-menu"),
    appMain: $("#app-main"),
    railHistory: $("#rail-history"),
    chatScrollBottom: $("#chat-scroll-bottom"),
    productionStrip: $("#production-strip"),
    productionState: $("#production-state-label"),
    productionRevision: $("#production-revision-label"),
    productionBudget: $("#production-budget-label"),
    productionMix: $("#production-mix-label"),
    productionBlockers: $("#production-blockers"),
    productionReview: $("#production-review"),
    reviewStartSec: $("#review-start-sec"),
    reviewEndSec: $("#review-end-sec"),
    reviewNote: $("#review-note"),
    reviewWatchedFullVideo: $("#review-watched-full-video"),
    reviewCreativeChecks: [...document.querySelectorAll("[data-review-dimension]")],
    requestChangesBtn: $("#request-changes-btn"),
    approveProductionBtn: $("#approve-production-btn"),
    productionReviewStatus: $("#production-review-status"),
  };

  /** @typedef {{ asset_id: string, kind: string, summary: string, source: "user"|"tool", final?: boolean }} AssetEntry */

  const state = {
    sessionId: null,
    projectId: null,
    projectName: null,
    projectSourceRoot: null,
    runId: null,
    projectRevision: 0,
    productionRevision: 0,
    productionState: null,
    productionOutcome: null,
    productionBudget: null,
    productionBlockers: [],
    productionDelivery: null,
    productionAcceptance: null,
    productionAssetMix: null,
    chatOnly: false,
    eventSource: null,
    turnInProgress: false,
    turns: [],                  // array of TurnRecord
    currentTurn: null,          // TurnRecord (also last in turns[])
    selectedClipId: null,       // direct-edit: currently selected clip
    ptDrag: null,               // direct-edit: active drag/trim gesture
    /** @type {AssetEntry[]} */
    assets: [],
    /** @type {string[]} */
    errors: [],
    uploadStatus: null,
    lastEventId: null,
    reconnectTimer: null,
    serverInstanceId: null,
    recoveringSession: false,
    projectTimeline: null,      // fetched from /sessions/{id}/timeline
    timelinePollTimer: null,
    mediaLibrary: [],
    sessionNonMediaAssets: [],
    librarySection: "media",
    libraryFocusName: "",
    mediaAnnotations: new Map(), // media-library asset_id -> annotations[]
    roughcutManifests: new Map(), // media-library asset_id -> persisted review manifest
    roughcutJob: null,
    roughcutPollTimer: null,
    mediaLibraryStatus: "idle",
    _followChatBottom: true,    // false while the creator is reading above
    planMode: false,            // mirrors the backend per-session flag
    planReady: false,           // a turn completed while planning → offer approval
    pendingAsk: null,           // {question_id, question} while elicit awaits
    sessionTitle: null,         // auto-generated title
    activeHistoryId: null,      // selected legacy/chat-only history snapshot
    userMessageCount: 0,        // user message counter for auto-title triggers
    stopPending: false,
    // Background shell jobs (run_shell run_in_background=true), keyed by
    // job_id: {job_id, status, summary, exit_code, elapsed_sec, output_tail,
    // _killing}. Fed by background_task_update SSE + GET /sessions/{id} tasks.
    backgroundTasks: new Map(),
  };

  function makeClientTurnId() {
    if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID();
    return `web-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 12)}`;
  }

  function newTurn(userMessage, startedAt = Date.now()) {
    const sentAt = Number(startedAt);
    return {
      userMessage,
      clientTurnId: makeClientTurnId(),
      startedAt: Number.isFinite(sentAt) && sentAt > 0 ? sentAt : Date.now(),
      completedAt: null,
      assistantText: "",
      pendingAssistantText: "", // canonical text buffer; safe rounds may also render live
      streaming: false,
      streamRetryText: "",
      toolCalls: new Map(),     // call_id -> ToolCallState
      orderedCallIds: [],
      guidance: [],             // user steering messages inside this same turn
      banners: [],              // { kind: "budget"|"turn_error"|"unknown", text }
      complete: false,
    };
  }

  // ── render ──────────────────────────────────────────────────────────

  function escapeHTML(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  }

  function creatorErrorMessage(raw) {
    const message = String(raw || "").toLowerCase();
    if (/\b(401|403)\b|unauthori[sz]ed|authentication|invalid api.?key/.test(message)) {
      return "AI 供应商凭据无效，请在设置中检查 API Key。";
    }
    if (/\b402\b|credit|quota|insufficient|weekly limit|billing/.test(message)) {
      return "AI 供应商额度不足，请充值、调整额度或更换模型后重试。";
    }
    if (/\b429\b|rate.?limit|too many requests/.test(message)) {
      return "AI 供应商请求过于频繁，请稍后重试。";
    }
    if (/timeout|timed out|connection|refused|proxy|network|dns/.test(message)) {
      return "无法连接 AI 供应商，请检查网络、代理和供应商设置。";
    }
    return "本次模型请求未完成，请检查 AI 供应商设置后重试。";
  }

  // The activity stream is an orientation aid, not a developer console. The
  // only specific wording comes from Lumeri's own constrained activity label;
  // the display layer rejects anything that could be code or implementation
  // detail before it reaches the DOM.
  const ACTIVITY_TEXT_MAX_CHARS = 72;
  const PROGRESS_REPORT_MAX_CHARS = 240;
  const ACTIVITY_TEXT_UNSAFE_RE = /[`{}[\]<>\\]|[=;]|(?:https?|file):\/\/|(?:^|\s)(?:\/|~\/|[A-Za-z]:[\\/])|\b[\w.-]+\.(?:py|js|jsx|ts|tsx|json|md|yaml|yml|sh|bash|zsh|html|css|sql)\b|\b[a-z][a-z0-9]*_[a-z0-9_]+\b|\b(?:api[_-]?key|token|password|secret|system[_ -]?prompt|reasoning|thought[_ -]?signature|asset[_ -]?id)\b|(?:代码|路径|工具名?|参数|命令|思维链|推理|内部)/i;

  const TOOL_CATEGORY = {
    generate_image: "创建", generate_video: "创建", generate_audio: "创建",
    narrate: "创建", build: "创建",
    lumen_render: "创建", lumen_render_range: "创建", vector_motion: "创建",

    edit_image: "编辑", edit_video: "编辑", edit_audio: "编辑",
    composite: "编辑", color_grade: "编辑", adjust_media: "编辑",
    paint_overlay: "编辑", paint_mask_effect: "编辑", add_overlay: "编辑",
    transform_geometry: "编辑", smart_reframe: "编辑",
    subtitle: "编辑", animate_captions: "编辑", lumen_patch: "编辑",
    grade: "编辑", kinetic_type: "编辑", edit_grammar: "编辑",
    camera: "编辑", compose: "编辑", rhythm_edit: "编辑",

    arrange_timeline: "剪辑", lumen_comp_to_timeline: "剪辑",
    timeline_insert_clip: "剪辑", timeline_delete_clip: "剪辑",
    timeline_move_clip: "剪辑", timeline_trim_clip: "剪辑",
    timeline_split_clip: "剪辑", timeline_set_clip_time: "剪辑",
    timeline_add_transition: "剪辑", timeline_set_clip_effects: "剪辑",
    timeline_add_track: "剪辑", timeline_set_track: "剪辑",
    timeline_undo: "剪辑", inspect_timeline: "剪辑", get_timeline: "剪辑",
    mix_audio: "剪辑", align_audio: "剪辑", detect_beats: "剪辑",

    search_library: "搜索", search_media: "搜索", search_frames: "搜索",
    web_search: "搜索", web_open: "搜索", fetch: "搜索",

    extract_frame: "分析", probe_media: "分析", analyze_media: "分析",
    get_safe_areas: "分析", inspect_lottie: "分析",
    annotate_media: "分析", get_media_annotations: "分析",
    write_media_annotation: "分析",
    get_lumenframe: "分析", lumen_seek: "分析", render_preview: "分析",

    assemble_shotlist: "脚本", draft_shotlist: "脚本", set_shotlist: "脚本",
    update_shot: "脚本", get_shotlist: "脚本", refine_shot: "脚本",

    draft_quanta: "演示", set_quanta: "演示", update_quantum: "演示",
    get_quanta: "演示", assemble_quanta: "演示", refine_quantum: "演示",

    export: "导出", project_export: "导出",
    project_export_otio: "导出", project_import_otio: "导出",

    read_file: "文件", write_file: "文件", copy_in: "文件",
    list_dir: "文件", move_file: "文件", organize_files: "文件",
    run_shell: "文件",

    save_skill: "记忆", recall_skills: "记忆",
    remember: "记忆", log_note: "记忆",

    elicit: "交互",
    spawn_subtasks: "执行", check_job: "执行", wait_for_job: "执行",
    kill_job: "执行",
  };

  const CATEGORY_DEFAULTS = {
    创建: { running: "正在生成素材", done: "素材已生成" },
    编辑: { running: "正在调整素材", done: "素材已调整" },
    剪辑: { running: "正在编排时间线", done: "时间线已更新" },
    搜索: { running: "正在查找资源", done: "查找完成" },
    分析: { running: "正在检视素材", done: "检视完成" },
    脚本: { running: "正在整理拍摄方案", done: "方案已更新" },
    演示: { running: "正在编排演示", done: "演示已更新" },
    导出: { running: "正在导出成片", done: "成片已导出" },
    文件: { running: "正在处理文件", done: "文件已处理" },
    记忆: { running: "正在记录", done: "已记录" },
    交互: { running: "等待你的选择", done: "已确认" },
    执行: { running: "正在执行", done: "执行完成" },
  };

  const CATEGORY_ICON = {
    创建: "i-spark",
    编辑: "i-sliders",
    剪辑: "i-scissors",
    搜索: "i-search",
    分析: "i-eye",
    脚本: "i-clapperboard",
    演示: "i-clapperboard",
    导出: "i-export",
    文件: "i-folder",
    记忆: "i-brain",
    交互: "i-chat-q",
    执行: "i-gear",
  };

  function toolCategory(name) {
    return TOOL_CATEGORY[name] || "执行";
  }

  function safeActivityText(value) {
    const text = String(value || "").trim().replace(/\s+/g, " ");
    if (!text || text.length > ACTIVITY_TEXT_MAX_CHARS || ACTIVITY_TEXT_UNSAFE_RE.test(text)) {
      return "";
    }
    return text;
  }

  function safeProgressReport(value) {
    const text = String(value || "").trim().replace(/\s+/g, " ");
    if (!text || text.length > PROGRESS_REPORT_MAX_CHARS || ACTIVITY_TEXT_UNSAFE_RE.test(text)) {
      return "";
    }
    return text;
  }

  function stripActivityMarkup(value) {
    const withoutBlocks = String(value || "").replace(/<(?:activity|report)\b[^>]*>[\s\S]*?<\/(?:activity|report)\s*>/gi, "");
    return withoutBlocks
      .split(/\r?\n/)
      .filter((line) => !/<\/?(?:activity|report)\b/i.test(line))
      .join("\n")
      .trim();
  }

  function activityLabel(tc) {
    const activityText = safeActivityText(tc.activityText);
    const cat = toolCategory(tc.tool_name);
    const defaults = CATEGORY_DEFAULTS[cat] || CATEGORY_DEFAULTS["执行"];
    if (tc.status === "done" || tc.status === "ok") {
      return activityText || defaults.done;
    }
    if (tc.status === "failed" || tc.status === "error" || tc.status === "timeout") {
      return "未能完成";
    }
    if (tc.status === "gated" || tc.status === "needs_user") {
      return tc.status === "needs_user" ? "等待你的选择" : "等待你的批准";
    }
    if (tc.status === "cancelled") return "已取消";
    return activityText || defaults.running;
  }

  function activityPhase(status) {
    if (status === "done" || status === "ok") return "complete";
    if (status === "failed" || status === "error" || status === "timeout") return "attention";
    if (status === "gated" || status === "needs_user" || status === "cancelled") return "waiting";
    return "active";
  }

  // ── Markdown renderer ───────────────────────────────────────────────

  function renderMarkdown(src) {
    if (!src) return "";
    const text = String(src);

    // Extract fenced code blocks before any other processing
    const codeBlocks = [];
    const withPlaceholders = text.replace(/^```(\w*)\n([\s\S]*?)^```/gm, (_, lang, code) => {
      const idx = codeBlocks.length;
      codeBlocks.push(`<pre class="md-code-block"><code class="lang-${escapeHTML(lang || "text")}">${escapeHTML(code.replace(/\n$/, ""))}</code></pre>`);
      return `\x00CB${idx}\x00`;
    });

    // Split into block-level chunks by double newline
    const blocks = withPlaceholders.split(/\n{2,}/);
    const out = [];
    let orderedListNextStart = null;

    for (let i = 0; i < blocks.length; i++) {
      const block = blocks[i];
      const orderedMarker = block.trim().match(/^(\d+)[.)]\s/);
      if (!orderedMarker) orderedListNextStart = null;

      // Code block placeholder
      if (/^\x00CB\d+\x00$/.test(block.trim())) {
        out.push(codeBlocks[+block.trim().slice(3, -1)]);
        continue;
      }

      // Heading
      const hm = block.match(/^(#{1,6})\s+(.+)$/m);
      if (hm && block.trim().startsWith("#")) {
        const lvl = hm[1].length;
        out.push(`<h${lvl} class="md-h">${mdInline(hm[2])}</h${lvl}>`);
        continue;
      }

      // Horizontal rule
      if (/^(\s*[-*_]){3,}\s*$/.test(block.trim())) {
        out.push(`<hr class="md-hr">`);
        continue;
      }

      // Blockquote
      if (block.trim().startsWith(">")) {
        const inner = block.replace(/^>\s?/gm, "");
        out.push(`<blockquote class="md-blockquote">${renderMarkdown(inner)}</blockquote>`);
        continue;
      }

      // Table
      const tableLines = block.trim().split("\n");
      if (tableLines.length >= 2 && tableLines[0].includes("|") && /^[\s|:-]+$/.test(tableLines[1])) {
        out.push(mdTable(tableLines));
        continue;
      }

      // Unordered list
      if (/^[\t ]*[-*+]\s/.test(block.trim())) {
        out.push(mdList(block, "ul"));
        continue;
      }

      // Ordered list
      if (orderedMarker) {
        const requestedStart = Number(orderedMarker[1]) || 1;
        const start = orderedListNextStart !== null && requestedStart === 1
          ? orderedListNextStart
          : requestedStart;
        out.push(mdList(block, "ol", start));
        const itemCount = block.match(/^[\t ]*\d+[.)]\s+/gm)?.length || 1;
        orderedListNextStart = start + itemCount;
        continue;
      }

      // Paragraph (may contain inline code block placeholders on their own line)
      const lines = block.split("\n");
      const paraLines = [];
      for (const ln of lines) {
        if (/^\x00CB\d+\x00$/.test(ln.trim())) {
          if (paraLines.length) {
            out.push(`<p>${mdInline(paraLines.join("\n"))}</p>`);
            paraLines.length = 0;
          }
          out.push(codeBlocks[+ln.trim().slice(3, -1)]);
        } else {
          paraLines.push(ln);
        }
      }
      if (paraLines.length) {
        out.push(`<p>${mdInline(paraLines.join("\n"))}</p>`);
      }
    }
    return out.join("\n");
  }

  function mdInline(s) {
    let r = escapeHTML(s);
    // Inline code (must come before bold/italic to avoid conflicts)
    r = r.replace(/`([^`\n]+?)`/g, '<code class="md-inline-code">$1</code>');
    // Entity references — before bold/italic so underscore-delimited IDs
    // (v_001, s0_shot0) are not consumed by emphasis rules.
    r = r.replace(/\b(v_\d+|img_\d+|aud_\d+|lot_\d+)\b/g,
      '<span class="md-entity" data-entity-kind="asset" data-entity-id="$1" role="link" tabindex="0">$1</span>');
    r = r.replace(/\b(clip_[a-f0-9]{8,16})\b/g,
      '<span class="md-entity" data-entity-kind="clip" data-entity-id="$1" role="link" tabindex="0">$1</span>');
    r = r.replace(/\b(s\d+_shot\d+)\b/g,
      '<span class="md-entity" data-entity-kind="shot" data-entity-id="$1" role="link" tabindex="0">$1</span>');
    r = r.replace(/\b(scene\d+)\b/g,
      '<span class="md-entity" data-entity-kind="scene" data-entity-id="$1" role="link" tabindex="0">$1</span>');
    // Images
    r = r.replace(/!\[([^\]]*)\]\(([^)]+)\)/g, '<img class="md-img" alt="$1" src="$2">');
    // Links
    r = r.replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a class="md-link" href="$2" target="_blank" rel="noopener">$1</a>');
    // Bold + italic
    r = r.replace(/\*\*\*(.+?)\*\*\*/g, "<strong><em>$1</em></strong>");
    // Bold
    r = r.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
    r = r.replace(/__(.+?)__/g, "<strong>$1</strong>");
    // Italic
    r = r.replace(/\*(.+?)\*/g, "<em>$1</em>");
    r = r.replace(/_(.+?)_/g, "<em>$1</em>");
    // Strikethrough
    r = r.replace(/~~(.+?)~~/g, "<del>$1</del>");
    // Line break (trailing double space or backslash)
    r = r.replace(/  \n/g, "<br>");
    r = r.replace(/\\\n/g, "<br>");
    // Single newlines within a paragraph → <br>
    r = r.replace(/\n/g, "<br>");
    return r;
  }

  function mdList(block, tag, start = 1) {
    const lines = block.split("\n");
    const items = [];
    for (const ln of lines) {
      const m = tag === "ul"
        ? ln.match(/^[\t ]*[-*+]\s+(.*)/)
        : ln.match(/^[\t ]*\d+[.)]\s+(.*)/);
      if (m) items.push(`<li>${mdInline(m[1])}</li>`);
      else if (items.length) {
        items[items.length - 1] = items[items.length - 1].replace("</li>", `<br>${mdInline(ln.trim())}</li>`);
      }
    }
    const startAttr = tag === "ol" && start !== 1 ? ` start="${start}"` : "";
    return `<${tag} class="md-list"${startAttr}>${items.join("")}</${tag}>`;
  }

  function mdTable(lines) {
    const parseRow = (ln) => ln.replace(/^\|/, "").replace(/\|$/, "").split("|").map((c) => c.trim());
    const headers = parseRow(lines[0]);
    const alignRow = parseRow(lines[1]);
    const aligns = alignRow.map((c) => {
      if (c.startsWith(":") && c.endsWith(":")) return "center";
      if (c.endsWith(":")) return "right";
      return "left";
    });
    let html = '<table class="md-table"><thead><tr>';
    for (let i = 0; i < headers.length; i++) {
      html += `<th style="text-align:${aligns[i] || "left"}">${mdInline(headers[i])}</th>`;
    }
    html += "</tr></thead><tbody>";
    for (let r = 2; r < lines.length; r++) {
      if (!lines[r].trim()) continue;
      const cells = parseRow(lines[r]);
      html += "<tr>";
      for (let i = 0; i < headers.length; i++) {
        html += `<td style="text-align:${aligns[i] || "left"}">${mdInline(cells[i] || "")}</td>`;
      }
      html += "</tr>";
    }
    html += "</tbody></table>";
    return html;
  }

  function lastEventStorageKey(sessionId) {
    return `lumeri:v3:last-event:${sessionId}`;
  }

  function loadLastEventId(sessionId) {
    if (!sessionId) return null;
    try {
      return window.localStorage.getItem(lastEventStorageKey(sessionId));
    } catch {
      return null;
    }
  }

  function saveLastEventId(sessionId, eventId) {
    if (!sessionId || !eventId) return;
    state.lastEventId = String(eventId);
    try {
      window.localStorage.setItem(lastEventStorageKey(sessionId), state.lastEventId);
    } catch {}
  }

  function clearReconnectTimer() {
    if (!state.reconnectTimer) return;
    window.clearTimeout(state.reconnectTimer);
    state.reconnectTimer = null;
  }

  function syncComposerAction() {
    const hasText = els.promptInput.value.trim().length > 0;
    els.sendBtn.disabled = !state.sessionId || state.stopPending || state.recoveringSession;
    els.sendBtn.classList.toggle("is-voice", !state.turnInProgress && !voiceInput.listening && !hasText);
    els.sendBtn.classList.toggle("is-stop", state.turnInProgress && !hasText);
    els.sendBtn.classList.toggle("is-listening", !!voiceInput.listening);
    if (state.turnInProgress && !hasText && !voiceInput.listening) {
      els.sendBtn.querySelector("use")?.setAttribute("href", "#i-stop-solid");
      els.sendBtn.setAttribute("aria-label", "停止当前执行");
      els.sendBtn.title = "停止当前执行";
      els.sendBtn.disabled = state.stopPending;
    } else if (voiceInput.listening) {
      els.sendBtn.querySelector("use")?.setAttribute("href", "#i-mic");
      els.sendBtn.setAttribute("aria-label", "停止语音输入");
      els.sendBtn.title = "停止语音输入";
      els.sendBtn.disabled = false;
    } else if (hasText) {
      els.sendBtn.querySelector("use")?.setAttribute("href", "#i-send");
      els.sendBtn.setAttribute("aria-label", state.turnInProgress ? "引导当前执行" : "发送");
      els.sendBtn.title = state.turnInProgress ? "引导当前执行" : "发送";
    } else {
      els.sendBtn.querySelector("use")?.setAttribute("href", "#i-mic");
      els.sendBtn.setAttribute("aria-label", "语音输入");
      els.sendBtn.title = "语音输入";
    }
  }

  function render() {
    els.sessionLabel.textContent = state.sessionTitle || state.sessionId || "—";
    if (els.projectNameLabel) {
      els.projectNameLabel.textContent = state.projectName || "Project";
    }
    syncProjectSidebarSelection();
    const busy = !state.sessionId || state.turnInProgress;
    els.uploadBtn.disabled = busy;
    els.inputShell.classList.toggle("is-steering", state.turnInProgress);
    els.inputShell.classList.toggle("is-working", state.turnInProgress);
    syncComposerAction();
    els.promptInput.placeholder = "描述你想要的视频，或输入 / 唤起命令…";
    document.querySelectorAll(".pt-action-btn, .pt-edit-btn").forEach((b) => { b.disabled = busy; });
    updateEditHint();   // selection-aware split/delete rule wins over the blanket disable above

    const railEmpty = document.getElementById("rail-empty");
    const hasTimelinePreview = !!currentTimelinePreview();
    if (!state.turns.length && !hasTimelinePreview) {
      els.timeline.hidden = true;
      els.emptyState.hidden = false;
      if (railEmpty) railEmpty.hidden = false;
    } else {
      els.emptyState.hidden = true;
      if (railEmpty) railEmpty.hidden = true;
      els.timeline.hidden = false;
      els.timeline.innerHTML = state.turns.map((turn, idx) => renderTurn(turn, idx)).join("");
    }

    // 有素材就自动展开左侧时间轴抽屉（一次性，之后尊重用户手动开合）。
    if (!state._drawerAutoShown && state.assets && state.assets.length > 0) {
      state._drawerAutoShown = true;
      toggleDrawer(true);
    }

    renderDeliveryReviewMaster();
    renderAssets();
    renderMediaLibrary();
    renderProductionUi();
    renderPlanUi();
    autoScrollChat();
  }

  const PRODUCTION_STATE_LABELS = {
    created: "已创建",
    preflight: "生产预检",
    sourcing: "素材准备",
    rough_cut: "粗剪",
    sound_pass: "声音制作",
    visual_pass: "视觉制作",
    rendering: "正在渲染",
    verifying: "正在质检",
    ready_for_review: "等待你的审片",
    revising: "按反馈返修",
    accepted: "已确认可以发布",
    blocked: "生产受阻",
    cancelled: "已取消",
    failed: "生产失败",
  };

  function budgetView(raw) {
    const b = raw && typeof raw === "object" ? raw : {};
    const limit = Number(b.limit_usd ?? b.max_usd ?? b.hard_cap_usd ?? b.budget_max_usd ?? 15);
    const spent = Number(b.spent_usd ?? b.actual_usd ?? b.committed_usd ?? 0);
    const reserved = Number(b.reserved_usd ?? b.pending_usd ?? 0);
    return {
      limit: Number.isFinite(limit) && limit > 0 ? limit : 15,
      spent: Number.isFinite(spent) && spent >= 0 ? spent : 0,
      reserved: Number.isFinite(reserved) && reserved >= 0 ? reserved : 0,
    };
  }

  function applyProductionSnapshot(data) {
    if (!data || typeof data !== "object") return;
    if (data.project_id !== undefined) state.projectId = data.project_id || null;
    if (data.run_id !== undefined) state.runId = data.run_id || null;
    if (data.project_revision !== undefined) {
      const revision = Number(data.project_revision);
      if (Number.isFinite(revision) && revision >= 0) state.projectRevision = Math.floor(revision);
    }
    if (data.production_revision !== undefined) {
      const revision = Number(data.production_revision);
      if (Number.isFinite(revision) && revision >= 0) state.productionRevision = Math.floor(revision);
    }
    if (data.production_state !== undefined) state.productionState = data.production_state || null;
    if (data.outcome !== undefined) state.productionOutcome = data.outcome || null;
    if (data.budget !== undefined) state.productionBudget = data.budget || null;
    if (data.budget_ledger !== undefined) state.productionBudget = data.budget_ledger || null;
    if (Array.isArray(data.blockers)) state.productionBlockers = data.blockers;
    if (data.delivery !== undefined) state.productionDelivery = data.delivery || null;
    if (data.acceptance !== undefined) state.productionAcceptance = data.acceptance || null;
    if (data.asset_mix !== undefined) state.productionAssetMix = data.asset_mix || null;
    if (data.source_mix !== undefined) state.productionAssetMix = data.source_mix || null;
    if (data.chat_only !== undefined) state.chatOnly = !!data.chat_only;
  }

  function renderProductionUi() {
    if (!els.productionStrip) return;
    const visible = !!(state.projectId || state.runId || state.chatOnly);
    els.productionStrip.hidden = !visible;
    if (!visible) return;

    const pState = String(state.productionState || "created");
    els.productionStrip.classList.toggle("is-blocked", ["blocked", "failed"].includes(pState));
    els.productionStrip.classList.toggle("is-accepted", pState === "accepted");

    if (state.chatOnly && !state.projectId) {
      els.productionState.textContent = "仅聊天记录 · 工程未保存";
      els.productionRevision.textContent = "—";
      els.productionBudget.textContent = "无生产账本";
      els.productionMix.hidden = true;
      els.productionBlockers.hidden = false;
      els.productionBlockers.innerHTML = "这是一条旧记录，只能恢复对话，不能伪装成已经恢复的工程。";
      els.productionReview.hidden = true;
      els.productionStrip.classList.remove("is-warning", "is-accepted");
      return;
    }

    els.productionState.textContent = PRODUCTION_STATE_LABELS[pState] || pState;
    els.productionRevision.textContent = `r${state.projectRevision || 0}`;
    const b = budgetView(state.productionBudget);
    els.productionBudget.textContent = b.reserved > 0
      ? `$${b.spent.toFixed(2)} + $${b.reserved.toFixed(2)}预留 / $${b.limit.toFixed(2)}`
      : `$${b.spent.toFixed(2)} / $${b.limit.toFixed(2)}`;
    els.productionStrip.classList.toggle("is-warning", b.spent + b.reserved >= Math.min(12, b.limit));

    const mix = state.productionAssetMix;
    if (mix && typeof mix === "object" && Object.keys(mix).length) {
      const labels = {
        total: "素材",
        video: "视频",
        image: "图片",
        audio: "音频",
        lottie: "动效",
        external: "外部/公开",
        generated: "生成",
        generated_video: "生成视频",
        generated_image: "生成图片",
        generated_audio: "生成音频",
        derived: "本地派生",
        missing: "缺失",
      };
      const order = ["total", "video", "image", "audio", "external", "generated_video", "generated_image", "generated_audio", "derived", "missing"];
      const parts = order
        .filter((kind) => kind === "generated_video" ? mix[kind] !== undefined : Number(mix[kind]) > 0)
        .map((kind) => `${labels[kind]} ${Number(mix[kind])}`);
      if (mix.provenance_complete === true) parts.push("来源完整");
      else if (mix.provenance_complete === false) parts.push("来源待补齐");
      els.productionMix.textContent = parts.join(" · ");
      els.productionMix.hidden = !parts.length;
    } else {
      const counts = new Map();
      for (const asset of state.assets) {
        const kind = asset.source_class || asset.origin || asset.source || "素材";
        counts.set(kind, (counts.get(kind) || 0) + 1);
      }
      const parts = [...counts].map(([kind, count]) => `${kind} ${count}`);
      els.productionMix.textContent = parts.join(" · ");
      els.productionMix.hidden = !parts.length;
    }

    const blockers = (state.productionBlockers || []).slice(0, 3).map((item) => {
      if (typeof item === "string") return item;
      return String(item?.message || item?.summary || item?.code || "未说明的阻塞项");
    });
    els.productionBlockers.hidden = !blockers.length;
    els.productionBlockers.innerHTML = blockers.length
      ? `<strong>阻塞：</strong>${blockers.map(escapeHTML).join("；")}`
      : "";

    els.productionReview.hidden = pState !== "ready_for_review";
    const reviewBusy = els.productionReview.dataset.submitting === "1";
    const deliveryReady = !!currentReviewMaster();
    els.requestChangesBtn.disabled = reviewBusy;
    els.approveProductionBtn.disabled = reviewBusy || !deliveryReady;
    if (pState === "ready_for_review" && !deliveryReady && !reviewBusy) {
      els.productionReviewStatus.dataset.autoStatus = "delivery-missing";
      els.productionReviewStatus.textContent = "正式审片母版不可用，已禁止确认发布。";
    } else if (els.productionReviewStatus.dataset.autoStatus === "delivery-missing") {
      delete els.productionReviewStatus.dataset.autoStatus;
      els.productionReviewStatus.textContent = "";
    }
  }

  // Plan-mode toggle button + hint/approval bar. Signature-guarded like
  // renderAssets: render() runs on every SSE event, and rebuilding the bar's
  // innerHTML would restart its CSS transitions and drop button focus.
  function renderPlanUi() {
    if (!els.planBtn || !els.planBar) return;
    const sig = `${state.planMode}|${state.planReady}|${state.turnInProgress}|${!!state.sessionId}`;
    if (sig === state._planSig) return;
    state._planSig = sig;

    els.planBtn.disabled = !state.sessionId;
    els.planBtn.classList.toggle("on", state.planMode);
    els.planBtn.title = state.planMode
      ? "计划模式已开启：只查看和规划，不做改动（点击关闭）"
      : "计划模式：只查看和规划，批准后才执行改动";

    if (!state.planMode) {
      els.planBar.hidden = true;
      els.planBar.innerHTML = "";
      return;
    }
    els.planBar.hidden = false;
    if (state.planReady && !state.turnInProgress) {
      els.planBar.innerHTML = `
        <span class="plan-bar-text">计划已就绪</span>
        <span class="plan-bar-actions">
          <button type="button" class="plan-approve" data-plan-approve>批准</button>
          <button type="button" class="plan-refine" data-plan-dismiss>继续规划</button>
        </span>
      `;
    } else {
      els.planBar.innerHTML = `
        <span class="plan-bar-text" title="Lumeri 只查看和规划，等你批准后才会改动项目">计划模式已开启</span>
      `;
    }
  }

  function renderTurn(turn, idx) {
    const callsHtml = buildCallGroups(turn).map(renderCallGroup).join("");
    const bannersHtml = turn.banners.map(renderBanner).join("");
    const isActiveTurn = state.turnInProgress && turn === state.currentTurn;
    const guidanceHtml = (turn.guidance || []).map((text) =>
      `<div class="turn-guidance${isActiveTurn ? " is-active" : ""}" role="status" aria-label="Lumeri 进度反馈">
        <span class="turn-guidance-mark" aria-hidden="true"></span>${escapeHTML(text)}
      </div>`
    ).join("");
    const hasAssistant = turn.assistantText || turn.streaming;
    const assistantHtml = hasAssistant
      ? `<div class="assistant-bubble${turn.streaming ? " streaming" : ""}">${renderMarkdown(turn.assistantText)}</div>`
      : "";
    const shouldShowMark = isActiveTurn || hasAssistant;
    const workElapsed = formatWorkElapsed(turn, isActiveTurn);
    const streamRetry = isActiveTurn && turn.streamRetryText
      ? ` · ${escapeHTML(turn.streamRetryText)}`
      : "";
    const assistantMarkHtml = shouldShowMark
      ? `<div class="assistant-workmark${isActiveTurn ? " is-active" : " is-static"}"${isActiveTurn ? ' role="status" aria-live="polite" aria-label="Lumeri 正在生成"' : ' aria-hidden="true"'}>
          <img src="/v3/${isActiveTurn ? "lumeri-working.svg" : "lumeri-working-static.svg"}" alt="" aria-hidden="true" />
          ${isActiveTurn ? "Working" : ""}${streamRetry}${workElapsed ? ` <span>${escapeHTML(workElapsed)}</span>` : ""}
        </div>`
      : "";
    const actionsHtml = (hasAssistant && turn.assistantText && !turn.streaming)
      ? `<div class="assistant-actions">
          <button type="button" class="assistant-action-btn" data-copy-assistant="${idx}" title="复制">
            <svg aria-hidden="true"><use href="#i-copy"/></svg>
          </button>
          <button type="button" class="assistant-action-btn" data-speak-assistant="${idx}" title="朗读">
            <svg aria-hidden="true"><use href="#i-volume"/></svg>
          </button>
        </div>`
      : "";
    // Retract only makes sense for the newest settled turn: the backend
    // anchors on its last real user message, so older bubbles can't match.
    const canRetract = idx === state.turns.length - 1 && !state.turnInProgress;
    const sentTime = formatSentTime(turn);
    const sentTimeTitle = formatSentTime(turn, { full: true });
    const userActionsHtml = `<div class="user-actions">
          ${sentTime ? `<time class="message-sent-time" datetime="${new Date(turn.startedAt).toISOString()}" title="发送于 ${escapeHTML(sentTimeTitle)}">${escapeHTML(sentTime)}</time>` : ""}
          <button type="button" class="assistant-action-btn" data-copy-user="${idx}" title="复制">
            <svg aria-hidden="true"><use href="#i-copy"/></svg>
          </button>
          ${canRetract ? `<button type="button" class="assistant-action-btn" data-retract-user="${idx}" title="撤回">
            <svg aria-hidden="true"><use href="#i-undo"/></svg>
          </button>` : ""}
        </div>`;
    return `
      ${idx ? '<div class="turn-divider" role="separator"></div>' : ""}
      <div class="user-bubble">${renderMarkdown(turn.userMessage)}</div>
      ${userActionsHtml}
      ${guidanceHtml}
      ${callsHtml}
      ${bannersHtml}
      ${assistantHtml}
      ${assistantMarkHtml}
      ${actionsHtml}
    `;
  }

  // Keep each activity at the same calm, high-level granularity. The backend
  // still tracks recoveries; exposing that diagnostic arc is not useful here.
  function callGroupStatus(calls) {
    if (calls.some((tc) => tc.status === "running")) return "running";
    if (calls.some((tc) => tc.status === "pending")) return "pending";
    if (calls.some((tc) => tc.status === "needs_user")) return "needs_user";
    if (calls.some((tc) => tc.status === "gated")) return "gated";
    if (calls.some((tc) => tc.status === "failed" || tc.status === "error" || tc.status === "timeout")) return "failed";
    if (calls.every((tc) => tc.status === "cancelled")) return "cancelled";
    return calls[calls.length - 1]?.status || "pending";
  }

  function buildCallGroups(turn) {
    const groups = [];
    for (const tc of turn.orderedCallIds.map((cid) => turn.toolCalls.get(cid)).filter(Boolean)) {
      const category = toolCategory(tc.tool_name);
      const activityText = safeActivityText(tc.activityText);
      const progressReport = safeProgressReport(tc.progressReport);
      const previous = groups[groups.length - 1];
      // The model-authored activity text names the purpose of a batch. Use it
      // as the archive key so one purpose can contain reading, searching,
      // editing, and execution steps without being split by tool category.
      // Calls without a safe purpose retain the old category fallback.
      const key = activityText ? `purpose:${activityText}` : `category:${category}`;
      if (previous?.key === key) {
        previous.calls.push(tc);
        if (!previous.progressReport && progressReport) previous.progressReport = progressReport;
        if (!previous.activityText && activityText) previous.activityText = activityText;
      } else {
        groups.push({ calls: [tc], category, activityText, progressReport, key });
      }
    }
    return groups;
  }

  function renderCallGroup(group) {
    const last = group.calls[group.calls.length - 1];
    const progressReport = safeProgressReport(group.progressReport);
    const reportHtml = progressReport
      ? `<div class="midturn-report" aria-label="阶段汇报">
          <div>${renderMarkdown(progressReport)}</div>
        </div>`
      : "";
    const groupedCall = {
      ...last,
      activityText: group.activityText || last.activityText,
      status: callGroupStatus(group.calls),
    };
    if (group.calls.length === 1) {
      return reportHtml + renderToolCall(groupedCall);
    }

    const label = activityLabel(groupedCall);
    const phase = activityPhase(groupedCall.status);
    const category = group.category || toolCategory(last.tool_name);
    const iconId = CATEGORY_ICON[category] || "i-gear";
    const open = phase === "active" || phase === "attention" || phase === "waiting";
    const detailHtml = group.calls.map((tc) => renderToolCall({
      ...tc,
      // The leader owns the purpose summary. Children describe only their
      // high-level kind and status, keeping tool names and arguments private.
      activityText: "",
    })).join("");
    return `${reportHtml}
      <details class="activity-archive activity-archive--${phase}"${open ? " open" : ""}>
        <summary class="activity-archive-head" aria-label="${escapeHTML(label)}">
          <svg class="activity-icon" aria-hidden="true"><use href="#${iconId}"/></svg>
          <span class="activity-desc">${escapeHTML(label)}</span>
          <span class="activity-archive-count">${group.calls.length} 项</span>
        </summary>
        <div class="activity-archive-items">${detailHtml}</div>
      </details>`;
  }

  // Quanta pager links are the ONE user entry point surfaced from a tool
  // result. Security boundary: same-origin, exact pathname, no free-form
  // URLs from the model ever reach an href.
  function safeQuantaPagerUrl(value) {
    if (typeof value !== "string" || !value) return null;
    try {
      const parsed = new URL(value, window.location.origin);
      if (parsed.origin !== window.location.origin) return null;
      if (parsed.pathname !== "/v3/quanta.html") return null;
      return `${parsed.pathname}${parsed.search}`;
    } catch (_) {
      return null;
    }
  }

  function renderToolCall(tc) {
    const label = activityLabel(tc);
    const phase = activityPhase(tc.status);
    const category = toolCategory(tc.tool_name);
    const iconId = CATEGORY_ICON[category] || "i-gear";
    const pagerHtml = tc.pagerUrl
      ? ` <a class="activity-link" href="${escapeHTML(tc.pagerUrl)}" target="_blank" rel="noopener">打开演示 ↗</a>`
      : "";
    return `
      <div class="activity-line activity-line--${phase}" aria-label="${escapeHTML(label)}">
        <svg class="activity-icon" aria-hidden="true"><use href="#${iconId}"/></svg>
        <span class="activity-desc">${escapeHTML(label)}${pagerHtml}</span>
      </div>
    `;
  }

  function renderBanner(banner) {
    // This is host copy, not Lumeri-authored conversation. Source alone is
    // enough to distinguish it: calm blue text, with no severity container.
    return `<div class="system-message" data-system-kind="${escapeHTML(banner.kind)}">${escapeHTML(banner.text)}</div>`;
  }

  function renderAssets() {
    // The preview is a timeline monitor, never an asset browser. Source media,
    // extracted frames and intermediate outputs belong in the Library only.
    if (state._assetsSig === "timeline-preview-only") return;
    state._assetsSig = "timeline-preview-only";
    els.assetGrid.innerHTML = "";
    els.assetGrid.hidden = true;
  }

  const MEDIA_LIBRARY_KINDS = new Set(["video", "image", "audio", "lottie"]);
  const NON_MEDIA_ASSET_EXTENSIONS = new Set([
    "svg", "pdf", "md", "txt", "csv", "doc", "docx", "ppt", "pptx",
    "xls", "xlsx", "ttf", "otf", "woff", "woff2", "zip", "3mf", "stl",
    "obj", "glb", "gltf",
  ]);

  function isTesterManagedWorkspace() {
    return document.documentElement.dataset.lumeriTesterManaged === "true";
  }

  function testerSessionLibraryAssets() {
    if (!isTesterManagedWorkspace() || !state.sessionId) return [];
    return state.assets
      .filter((asset) => asset?.asset_id)
      .map((asset) => {
        const assetId = String(asset.asset_id);
        const mediaKind = String(asset.kind || inferKindFromAssetId(assetId));
        const previewSrc = `/sessions/${encodeURIComponent(state.sessionId)}/assets/${encodeURIComponent(assetId)}`;
        return {
          ...asset,
          asset_id: assetId,
          name: String(asset.summary || assetId),
          media_kind: mediaKind,
          preview_src: previewSrc,
          thumbnail_src: mediaKind === "image" ? previewSrc : "",
          tester_session_asset: true,
        };
      });
  }

  function fileExtension(name) {
    const match = String(name || "").toLowerCase().match(/\.([a-z0-9]+)$/);
    return match ? match[1] : "";
  }

  function isMediaLibraryAsset(asset) {
    return MEDIA_LIBRARY_KINDS.has(String(asset?.media_kind || ""));
  }

  function libraryAssetsForSection() {
    const managedAssets = testerSessionLibraryAssets();
    const libraryAssets = isTesterManagedWorkspace() ? managedAssets : state.mediaLibrary;
    if (state.librarySection === "media") {
      return libraryAssets.filter(isMediaLibraryAsset);
    }
    const byName = new Map();
    if (!isTesterManagedWorkspace()) {
      state.sessionNonMediaAssets.forEach((asset) => byName.set(String(asset.name || "").toLowerCase(), asset));
    }
    libraryAssets.filter((asset) => !isMediaLibraryAsset(asset)).forEach((asset) => {
      byName.set(String(asset.name || "").toLowerCase(), asset);
    });
    return [...byName.values()];
  }

  function librarySectionsHtml() {
    const media = state.librarySection === "media";
    return `<div class="library-sections" role="tablist" aria-label="素材类型">
      <button type="button" class="library-section${media ? " active" : ""}" role="tab" aria-selected="${media}" data-library-section="media">媒体素材</button>
      <button type="button" class="library-section${media ? "" : " active"}" role="tab" aria-selected="${!media}" data-library-section="non-media">非媒体素材</button>
    </div>`;
  }

  function syncLibrarySectionUi() {
    document.querySelectorAll("[data-library-section]").forEach((button) => {
      const active = button.dataset.librarySection === state.librarySection;
      button.classList.toggle("active", active);
      button.setAttribute("aria-selected", active ? "true" : "false");
    });
    const media = state.librarySection === "media";
    if (els.libraryRoughcutBtn) els.libraryRoughcutBtn.hidden = !media;
    if (els.libraryAnnotateBtn) els.libraryAnnotateBtn.hidden = !media;
    document
      .querySelectorAll('[data-workspace-module="library"] .workspace-module-meta')
      .forEach((meta) => { meta.textContent = media ? "媒体素材" : "非媒体素材"; });
  }

  function formatLibraryFileSize(value) {
    const bytes = Number(value || 0);
    if (!Number.isFinite(bytes) || bytes <= 0) return "";
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(bytes < 10 * 1024 ? 1 : 0)} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  }

  function renderNonMediaLibraryCards(assets) {
    return assets.map((asset) => {
      const assetId = String(asset.asset_id || asset.id || "");
      const name = String(asset.name || "未命名文件");
      const extension = fileExtension(name);
      const previewSrc = String(asset.preview_src || "");
      const selected = !!state.libraryFocusName && name.toLowerCase() === state.libraryFocusName.toLowerCase();
      const thumb = extension === "svg" && previewSrc
        ? `<img class="library-thumb library-file-thumb" src="${escapeHTML(previewSrc)}" alt="" loading="lazy" />`
        : `<div class="library-thumb blank" aria-hidden="true"><svg viewBox="0 0 24 24"><use href="#i-file"/></svg></div>`;
      const meta = [extension ? extension.toUpperCase() : "文件", formatLibraryFileSize(asset.file_size_bytes)]
        .filter(Boolean).join(" · ");
      return `<div class="library-card library-file-card${selected ? " selected" : ""}"
          data-library-asset="${escapeHTML(assetId)}"
          data-library-name="${escapeHTML(name)}"
          ${selected ? 'aria-current="true"' : ""}
          title="${escapeHTML(name)}">
        ${thumb}
        <div class="library-card-body">
          <div class="library-title">${escapeHTML(name.replace(/\.[a-z0-9]+$/i, ""))}</div>
          <div class="library-meta">${escapeHTML(meta)}</div>
        </div>
      </div>`;
    }).join("");
  }

  function scrollFocusedLibraryAsset() {
    if (!state.libraryFocusName) return;
    window.requestAnimationFrame(() => {
      const card = [...document.querySelectorAll("[data-library-name]")].find(
        (item) => String(item.dataset.libraryName || "").toLowerCase() === state.libraryFocusName.toLowerCase()
      );
      card?.scrollIntoView({ block: "nearest" });
    });
  }

  function openNonMediaLibraryLink(link) {
    const href = String(link?.getAttribute("href") || "");
    if (!href.startsWith("sandbox:")) return false;
    const rawPath = decodeURIComponent(href.slice("sandbox:".length));
    const name = rawPath.split("/").filter(Boolean).pop() || String(link.textContent || "");
    if (!NON_MEDIA_ASSET_EXTENSIONS.has(fileExtension(name))) return false;
    state.librarySection = "non-media";
    state.libraryFocusName = name;
    toggleTray(true)?.then(() => {
      renderMediaLibrary();
      if (stageTabs.includes("library")) refreshPanel("library");
      scrollFocusedLibraryAsset();
    });
    return true;
  }

  function renderMediaLibrary() {
    if (!els.mediaLibraryGrid) return;
    syncLibrarySectionUi();
    if (state.mediaLibraryStatus === "loading") {
      els.mediaLibraryGrid.innerHTML = `<p class="placeholder">加载中…</p>`;
      return;
    }
    if (state.mediaLibraryStatus === "signed-out" && state.librarySection === "media") {
      els.mediaLibraryGrid.innerHTML = `<p class="placeholder">本地素材库暂不可用</p>`;
      return;
    }
    const visibleAssets = libraryAssetsForSection();
    if (!visibleAssets.length) {
      els.mediaLibraryGrid.innerHTML = `<p class="placeholder">${state.librarySection === "media" ? "暂无媒体素材" : "暂无非媒体素材"}</p>`;
      return;
    }
    if (state.librarySection === "non-media") {
      els.mediaLibraryGrid.innerHTML = renderNonMediaLibraryCards(visibleAssets);
      scrollFocusedLibraryAsset();
      return;
    }
    els.mediaLibraryGrid.innerHTML = visibleAssets.map((asset) => {
      const assetId = asset.asset_id || asset.id || "";
      const summary = asset.annotation_summary || {};
      const kind = asset.media_kind || "media";
      // 机器话不示人：hash 文件名/内部 ID 退到 title 悬停，卡面只留人话
      const kindLabel = LIBRARY_KIND_LABEL[kind] || "素材";
      const title = libraryDisplayName(asset, kindLabel);
      const allTags = [...(summary.tags || []), ...(summary.labels || [])];
      const shownTags = allTags.slice(0, 2);
      const moreTags = allTags.length - shownTags.length;
      const markerCount = Number(summary.count || 0);
      const anns = state.mediaAnnotations.get(assetId) || [];
      const roughcut = state.roughcutManifests.get(assetId);
      const annHtml = anns.length
        ? `<div class="annotation-list">${anns.map(renderAnnotation).join("")}</div>`
        : "";
      // 缩略图缺失 → 类型图标占位，不给黑块
      const thumb = asset.thumbnail_src
        ? `<img class="library-thumb" src="${escapeHTML(asset.thumbnail_src)}" alt="" loading="lazy" />`
        : `<div class="library-thumb blank" aria-hidden="true"><svg viewBox="0 0 24 24"><use href="#${LIBRARY_KIND_ICON[kind] || "i-file"}"/></svg></div>`;
      const tagsHtml = (markerCount || shownTags.length)
        ? `<div class="library-tags">
            ${markerCount ? `<span title="标记数"><svg viewBox="0 0 24 24" aria-hidden="true"><use href="#i-marker"/></svg>${markerCount}</span>` : ""}
            ${shownTags.map((tag) => `<span>${escapeHTML(tag)}</span>`).join("")}
            ${moreTags > 0 ? `<span>+${moreTags}</span>` : ""}
          </div>`
        : "";
      return `
        <div class="library-card" data-library-asset="${escapeHTML(assetId)}" title="${escapeHTML(asset.name || assetId)}">
          ${thumb}
          <div class="library-card-body">
            <div class="library-title">${escapeHTML(title)}</div>
            <div class="library-meta">${escapeHTML(kindLabel)}${kind === "image" ? "" : (formatMediaDuration(asset.duration) ? " · " + escapeHTML(formatMediaDuration(asset.duration)) : "")}</div>
            ${tagsHtml}
            ${asset.tester_session_asset ? "" : `<div class="library-card-actions">
              ${kind === "video" || kind === "audio" ? `<button type="button" class="library-small-btn icon-btn" title="粗剪准备" aria-label="粗剪准备" data-library-roughcut="${escapeHTML(assetId)}"><svg viewBox="0 0 24 24" aria-hidden="true"><use href="#i-wand"/></svg></button>` : ""}
              <button type="button" class="library-small-btn icon-btn" title="标注" aria-label="标注" data-library-annotate="${escapeHTML(assetId)}"><svg viewBox="0 0 24 24" aria-hidden="true"><use href="#i-wand"/></svg></button>
              <button type="button" class="library-small-btn icon-btn" title="复核" aria-label="复核" data-library-load="${escapeHTML(assetId)}"><svg viewBox="0 0 24 24" aria-hidden="true"><use href="#i-marker"/></svg></button>
            </div>`}
            ${annHtml}
            ${roughcut ? renderRoughcutReview(asset, roughcut) : ""}
          </div>
        </div>
      `;
    }).join("");
    wireRoughcutControls(els.mediaLibraryGrid);
  }

  const LIBRARY_KIND_LABEL = { video: "视频", image: "图片", audio: "音频" };
  const LIBRARY_KIND_ICON = { video: "i-film", image: "i-image", audio: "i-music" };

  // Human title for a media card: strip the extension; if what's left still
  // reads as a machine id, fall back to "未命名<类型>". A hash must be
  // hex-only AND contain a digit AND be long — so a readable name that merely
  // happens to use a–f letters (e.g. "faceded-beef") is NOT mistaken for one.
  function libraryDisplayName(asset, kindLabel) {
    const base = String(asset.name || "").replace(/\.[a-z0-9]{2,5}$/i, "");
    const compact = base.replace(/[-_]/g, "");
    const looksHashed = compact.length >= 16 && /^[0-9a-f]+$/i.test(compact) && /[0-9]/.test(compact);
    const machine = !base || looksHashed || /^asset[_-]/i.test(base);
    return machine ? `未命名${kindLabel}` : base;
  }

  // Media duration for a card: "14.7 秒" under a minute, "2:05" beyond.
  // Distinct from formatSeconds (used for annotation timecodes) — returns ""
  // for missing/zero so images and durationless assets show no "0.0s".
  function formatMediaDuration(value) {
    const n = Number(value || 0);
    if (!Number.isFinite(n) || n <= 0) return "";
    if (n < 60) return `${n < 10 ? n.toFixed(1) : Math.round(n)} 秒`;
    const m = Math.floor(n / 60);
    const s = Math.round(n % 60);
    return `${m}:${String(s).padStart(2, "0")}`;
  }

  function renderAnnotation(annotation) {
    const range = annotation.scope === "time_range"
      ? `${formatSeconds(annotation.start_sec)}-${formatSeconds(annotation.end_sec)}`
      : annotation.scope;
    const tags = (annotation.tags || []).slice(0, 4).map((tag) => `<span>${escapeHTML(tag)}</span>`).join("");
    return `
      <div class="annotation-item">
        <div><strong>${escapeHTML(annotation.label || "marker")}</strong> <span>${escapeHTML(range)}</span></div>
        ${annotation.note ? `<p>${escapeHTML(annotation.note)}</p>` : ""}
        ${tags ? `<div class="library-tags">${tags}</div>` : ""}
      </div>
    `;
  }

  function formatSeconds(value) {
    const n = Number(value || 0);
    if (!Number.isFinite(n)) return "0.0s";
    return `${n.toFixed(1)}s`;
  }

  function renderRoughcutReview(asset, manifest) {
    const assetId = asset.asset_id || asset.id || "";
    const take = manifest.take || {};
    const takeLabel = take.user_decision === "select" || take.selected
      ? "推荐主条"
      : take.user_decision === "reject" ? "已排除" : `备选 · 第 ${take.rank || 1} 名`;
    const suggestions = (manifest.cleanup_suggestions || []).map((item) => `
      <div class="roughcut-row ${escapeHTML(item.review_status || "pending")}">
        <button type="button" class="roughcut-time" data-roughcut-seek="${Number(item.start_sec || 0)}">${escapeHTML(formatSeconds(item.start_sec))}</button>
        <span class="roughcut-copy">${escapeHTML(item.label || item.kind)}</span>
        <span class="roughcut-review-actions">
          <button type="button" data-roughcut-review="accept" data-roughcut-type="cleanup" data-roughcut-id="${escapeHTML(item.id)}" data-roughcut-asset="${escapeHTML(assetId)}">接受</button>
          <button type="button" data-roughcut-review="reject" data-roughcut-type="cleanup" data-roughcut-id="${escapeHTML(item.id)}" data-roughcut-asset="${escapeHTML(assetId)}">保留</button>
        </span>
      </div>`).join("");
    const segments = (manifest.transcript?.segments || []).map((segment) => `
      <div class="roughcut-transcript-row">
        <button type="button" class="roughcut-time" data-roughcut-seek="${Number(segment.start_sec || 0)}">${escapeHTML(formatSeconds(segment.start_sec))}</button>
        <input type="text" value="${escapeHTML(segment.corrected_text || segment.text || "")}" aria-label="转写文本" data-roughcut-transcript-input="${escapeHTML(segment.id)}" />
        <button type="button" data-roughcut-review="correct" data-roughcut-type="transcript" data-roughcut-id="${escapeHTML(segment.id)}" data-roughcut-asset="${escapeHTML(assetId)}">保存</button>
      </div>`).join("");
    const player = asset.media_kind === "audio"
      ? `<audio class="roughcut-preview" src="${escapeHTML(asset.preview_src || "")}" controls preload="metadata"></audio>`
      : `<video class="roughcut-preview" src="${escapeHTML(asset.preview_src || "")}" controls preload="metadata"></video>`;
    return `
      <section class="roughcut-review" aria-label="粗剪复核">
        <div class="roughcut-summary"><strong>${escapeHTML(takeLabel)}</strong><span>质量 ${Math.round(Number(manifest.score || 0) * 100)}</span></div>
        ${player}
        <div class="roughcut-take-actions">
          <button type="button" data-roughcut-review="select" data-roughcut-type="take" data-roughcut-id="take" data-roughcut-asset="${escapeHTML(assetId)}">选为主条</button>
          <button type="button" data-roughcut-review="alternative" data-roughcut-type="take" data-roughcut-id="take" data-roughcut-asset="${escapeHTML(assetId)}">保留备选</button>
          <button type="button" data-roughcut-review="reject" data-roughcut-type="take" data-roughcut-id="take" data-roughcut-asset="${escapeHTML(assetId)}">排除</button>
        </div>
        ${segments ? `<details class="roughcut-section"><summary>转写 · ${(manifest.transcript?.segments || []).length} 段</summary>${segments}</details>` : ""}
        <details class="roughcut-section" ${suggestions ? "open" : ""}><summary>建议清理 · ${(manifest.cleanup_suggestions || []).length} 处</summary>${suggestions || `<p class="placeholder">未发现需要清理的停顿或口头禅</p>`}</details>
      </section>`;
  }

  function wireRoughcutControls(root) {
    if (!root) return;
    root.querySelectorAll("[data-library-roughcut], [data-panel-lib-roughcut]").forEach((button) => {
      button.onclick = (event) => {
        event.stopPropagation();
        const assetId = button.dataset.libraryRoughcut || button.dataset.panelLibRoughcut;
        startRoughcutPreparation(assetId).catch((err) => {
          state.errors.push(`粗剪准备失败: ${err.message}`);
          render();
        });
      };
    });
    root.querySelectorAll("[data-library-load], [data-panel-lib-load]").forEach((button) => {
      button.onclick = (event) => {
        event.stopPropagation();
        const assetId = button.dataset.libraryLoad || button.dataset.panelLibLoad;
        Promise.all([loadMediaAnnotations(assetId), loadRoughcutManifest(assetId)]).then(() => refreshPanel("library")).catch((err) => {
          state.errors.push(`复核加载失败: ${err.message}`);
          render();
        });
      };
    });
    root.querySelectorAll("[data-roughcut-review]").forEach((button) => {
      button.onclick = (event) => {
        event.stopPropagation();
        reviewRoughcut(button).then(() => refreshPanel("library")).catch((err) => {
          state.errors.push(`复核保存失败: ${err.message}`);
          render();
        });
      };
    });
    root.querySelectorAll("[data-roughcut-seek]").forEach((button) => {
      button.onclick = (event) => {
        event.stopPropagation();
        const player = button.closest(".library-card")?.querySelector(".roughcut-preview");
        if (player) { player.currentTime = Number(button.dataset.roughcutSeek || 0); player.play().catch(() => {}); }
      };
    });
  }

  function formatWorkElapsed(turn, active) {
    if (!turn?.startedAt) return "";
    const end = active ? Date.now() : turn.completedAt;
    if (!end) return "";
    const seconds = Math.max(0, Math.floor((end - turn.startedAt) / 1000));
    if (seconds < 60) return `${seconds}s`;
    const minutes = Math.floor(seconds / 60);
    const rest = seconds % 60;
    return `${minutes}m${String(rest).padStart(2, "0")}s`;
  }

  function formatSentTime(turn, { full = false } = {}) {
    if (!turn?.startedAt) return "";
    const sentAt = new Date(turn.startedAt);
    if (Number.isNaN(sentAt.getTime())) return "";
    const options = full
      ? { year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit" }
      : { hour: "2-digit", minute: "2-digit" };
    return new Intl.DateTimeFormat(undefined, options).format(sentAt);
  }

  // ── event handlers (one per kind, no silent drop) ──────────────────
  function autoScrollChat() {
    const rail = els.railHistory;
    if (!rail) return;
    if (state._followChatBottom) {
      rail.scrollTop = rail.scrollHeight;
    }
    syncChatScrollButton();
  }

  function chatIsNearBottom() {
    const rail = els.railHistory;
    return !rail || rail.scrollHeight - rail.scrollTop - rail.clientHeight < 80;
  }

  function syncChatScrollButton() {
    if (!els.chatScrollBottom) return;
    els.chatScrollBottom.hidden = chatIsNearBottom();
  }

  function scrollChatToBottom({ smooth = true } = {}) {
    const rail = els.railHistory;
    if (!rail) return;
    state._followChatBottom = true;
    rail.scrollTo({
      top: rail.scrollHeight,
      behavior: smooth ? "smooth" : "auto",
    });
    syncChatScrollButton();
  }

  const handlers = {
    turn_start: () => {
      state.turnInProgress = true;
      state.stopPending = false;
      if (state.currentTurn) {
        state.currentTurn.streaming = false;
      }
    },
    turn_guidance_queued: () => {},
    turn_guidance_applied: () => {
      const t = state.currentTurn;
      if (!t) return;
      // Text streamed before the safe steering boundary is a superseded draft.
      t.assistantText = "";
      t.pendingAssistantText = "";
      t.streaming = false;
    },
    turn_cancelled: () => {
      dismissAskDock();
      state.turnInProgress = false;
      state.stopPending = false;
      const t = state.currentTurn;
      if (!t) return;
      t.completedAt = Date.now();
      for (const tc of t.toolCalls.values()) {
        if (tc.status === "pending" || tc.status === "running") tc.status = "cancelled";
      }
      t.pendingAssistantText = "";
      t.streaming = false;
      t.complete = true;
      autoSaveSession();
    },
    model_text_delta: (ev) => {
      const t = state.currentTurn;
      if (!t) return;
      t.streamRetryText = "";
      // The backend marks only tool-free, user-visible rounds as streamable.
      // Unmarked deltas stay buffered because they may still become a tool
      // preamble and must never flash in the assistant reply.
      t.pendingAssistantText += String(ev.delta || "");
      if (ev.display === "stream") {
        t.assistantText = stripActivityMarkup(t.pendingAssistantText);
        t.streaming = true;
      }
    },
    model_stream_reset: (ev) => {
      const t = state.currentTurn;
      if (!t) return;
      t.assistantText = "";
      t.pendingAssistantText = "";
      t.streaming = false;
      t.streamRetryText = `Reconnecting ${Number(ev.retry) || 1}/${Number(ev.max_retries) || 1}`;
      for (const [callId, call] of t.toolCalls) {
        if (call.status === "pending") t.toolCalls.delete(callId);
      }
      t.orderedCallIds = t.orderedCallIds.filter((callId) => t.toolCalls.has(callId));
    },
    model_tool_call_start: (ev) => {
      const t = state.currentTurn;
      if (!t) return;
      // Text streamed before a tool proposal can be internal deliberation.
      // Discard it rather than turning it into a user-facing work log.
      t.assistantText = "";
      t.pendingAssistantText = "";
      t.streaming = false;
      t.streamRetryText = "";
      t.toolCalls.set(ev.call_id, {
        call_id: ev.call_id,
        tool_name: ev.tool_name,
        status: "pending",
        activityText: "",
        progressReport: "",
      });
      t.orderedCallIds.push(ev.call_id);
    },
    // Raw arguments are deliberately never retained by the display layer.
    // The only text allowed through is Lumeri's backend-validated activity label.
    model_tool_call_ready: (ev) => {
      const tc = state.currentTurn?.toolCalls.get(ev.call_id);
      if (tc) {
        tc.activityText = safeActivityText(ev.activity_text);
        tc.progressReport = safeProgressReport(ev.progress_report);
      }
    },
    tool_exec_start: (ev) => {
      if (ev.agent_id) return;
      const tc = state.currentTurn?.toolCalls.get(ev.call_id);
      if (tc) tc.status = "running";
    },
    tool_exec_progress: () => {},
    tool_exec_result: (ev) => {
      if (ev.agent_id) return;
      const t = state.currentTurn;
      const tc = t?.toolCalls.get(ev.call_id);
      if (!tc) return;
      tc.status = "done";
      const pagerUrl = safeQuantaPagerUrl(
        ev.result?.pager_url ?? ev.result?.first_state_pager_url
      );
      if (pagerUrl) tc.pagerUrl = pagerUrl;
      const assetId = ev.result?.asset_id;
      if (assetId) {
        state.assets.push({
          asset_id: assetId,
          kind: ev.result?.kind || inferKindFromAssetId(assetId),
          summary: ev.result?.summary || "",
          source: "tool",
          source_class: ev.result?.source_class || ev.result?.origin || null,
          origin: ev.result?.origin || null,
          provenance: ev.result?.provenance || null,
          final: false,
        });
      }
    },
    tool_exec_error: (ev) => {
      if (ev.agent_id) return;
      const tc = state.currentTurn?.toolCalls.get(ev.call_id);
      if (tc) {
        tc.status = "failed";
      }
    },
    // The parent spawn_subtasks row already says that work is happening in
    // parallel. Child goals, summaries, paths, and internal tool calls stay out
    // of the user-facing activity stream.
    subagent_start: () => {},
    subagent_result: () => {},
    budget_gate: (ev) => {
      const t = state.currentTurn;
      const tc = t?.toolCalls.get(ev.call_id);
      if (tc) tc.status = "gated";
      if (t && !t.banners.some((b) => b.kind === "budget")) {
        t.banners.push({
          kind: "budget",
          text: "当前任务已暂停",
        });
      }
    },
    plan_gate: (ev) => {
      const t = state.currentTurn;
      const tc = t?.toolCalls.get(ev.call_id);
      if (tc) tc.status = "gated";
      if (t && !t.banners.some((b) => b.kind === "plan")) {
        t.banners.push({
          kind: "plan",
          text: "计划模式下等待你的批准",
        });
      }
    },
    plan_mode_changed: (ev) => {
      state.planMode = !!ev.enabled;
      if (!state.planMode) state.planReady = false;
    },
    completion_check: () => {
      // Receive-only compatibility for replaying events from an older backend.
      // Current AgentLoopV3 never emits completion_check.
      const t = state.currentTurn;
      if (!t) return;
      t.assistantText = "";
      t.pendingAssistantText = "";
      t.streaming = false;
    },
    turn_wrapup: (ev) => {
      // This is Lumeri's hand-off, not a generic host banner. For an
      // interrupted turn the backend may already have released the model's
      // own closing report; preserve that richer text. Other stop reasons may
      // leave a partial stream fragment, so use the
      // backend's deterministic wrap-up instead of presenting a broken draft.
      dismissAskDock();
      const t = state.currentTurn;
      state.turnInProgress = false;
      state.stopPending = false;
      if (!t) return;
      t.completedAt = Date.now();
      const modelReport = ev.reason === "incomplete_goal"
        ? stripActivityMarkup(t.pendingAssistantText).trim()
        : "";
      const rawFallbackReport = String(ev.message || "").trim();
      const fallbackReport = /stream errored|http\s*[0-9]{3}|openrouter|credits|quota|api.?key/i.test(rawFallbackReport)
        ? creatorErrorMessage(rawFallbackReport)
        : rawFallbackReport;
      t.assistantText = modelReport || fallbackReport
        || "我先停在这里。当前进度已经保留；你让我继续，我会从这里接着处理。";
      t.pendingAssistantText = "";
      t.streaming = false;
      t.complete = true;
      autoSaveSession();
    },
    ask_question: (ev) => {
      const q = ev.question || {};
      state.pendingAsk = {
        question_id: q.question_id,
        question: q,
      };
      renderAskDock();
    },
    timeline_op: () => {
      // Timeline patch landed: refresh the project timeline panel immediately
      // rather than waiting for the next poll interval.
      fetchProjectTimeline({ force: true });
    },
    production_state_changed: (ev) => {
      applyProductionSnapshot(ev);
      if (ev.state !== undefined && ev.production_state === undefined) {
        state.productionState = ev.state || null;
      }
      if (!["ready_for_review", "accepted"].includes(String(state.productionState || ""))) {
        state.productionDelivery = null;
        unloadDeliveryReviewMaster();
      }
      autoSaveSession();
    },
    project_revision_committed: (ev) => {
      applyProductionSnapshot(ev);
      if (!currentReviewMaster()) {
        state.productionDelivery = null;
        unloadDeliveryReviewMaster();
      }
      // Any commit makes previews/evidence for an older revision stale.  The
      // authoritative timeline is fetched immediately instead of leaving a
      // visually plausible but old canvas on screen.
      fetchProjectTimeline({ force: true });
      autoSaveSession();
    },
    budget_updated: (ev) => {
      applyProductionSnapshot(ev);
      if (ev.budget) state.productionBudget = ev.budget;
      autoSaveSession();
    },
    delivery_ready: (ev) => {
      applyProductionSnapshot(ev);
      state.productionDelivery = ev.delivery || null;
      if (!ev.production_state) state.productionState = "ready_for_review";
      autoSaveSession();
    },
    acceptance_updated: (ev) => {
      applyProductionSnapshot(ev);
      state.productionAcceptance = ev.acceptance || ev;
      if (ev.action === "approve" && !ev.production_state) state.productionState = "accepted";
      if (ev.action === "request_changes" && !ev.production_state) state.productionState = "revising";
      if (ev.action === "request_changes" || !currentReviewMaster()) {
        state.productionDelivery = null;
        unloadDeliveryReviewMaster();
      }
      autoSaveSession();
    },
    background_task_update: (ev) => {
      // Background shell job status change (running → done/failed). Arrives
      // mid-turn AND between turns; authoritative, so it clears any
      // optimistic _killing flag.
      if (!ev.job_id) return;
      const prev = state.backgroundTasks.get(ev.job_id) || {};
      state.backgroundTasks.set(ev.job_id, {
        ...prev,
        job_id: ev.job_id,
        status: ev.status || prev.status || "running",
        summary: ev.summary ?? prev.summary ?? "",
        exit_code: ev.exit_code ?? prev.exit_code ?? null,
        elapsed_sec: ev.elapsed_sec ?? prev.elapsed_sec ?? null,
        output_tail: ev.output_tail ?? prev.output_tail ?? "",
        _killing: false,
      });
      if (ev.status === "running" && !bgActive) { bgActive = true; renderStageTabs(); }
      scheduleTasksPanelRefresh();
    },
    protocol_hello: (ev) => {
      state.protocolVersion = ev.protocol_version;
      const nextInstanceId = String(ev.server_instance_id || "");
      const restarted = !!(
        state.serverInstanceId
        && nextInstanceId
        && state.serverInstanceId !== nextInstanceId
      );
      state.serverInstanceId = nextInstanceId || state.serverInstanceId;
      if (restarted && state.sessionId && !state.recoveringSession) {
        const sessionId = state.sessionId;
        state.recoveringSession = true;
        state.turnInProgress = false;
        state.stopPending = false;
        refreshSessionState().catch((err) => {
          if (state.sessionId === sessionId) {
            state.errors.push(`session recovery failed: ${err.message}`);
          }
        }).finally(() => {
          if (state.sessionId === sessionId) {
            state.recoveringSession = false;
            render();
          }
        });
      }
    },
    replay_gap: (ev) => {
      const text = "连接已恢复，正在同步最新状态";
      const banner = {
        kind: "info",
        text,
      };
      if (state.currentTurn) state.currentTurn.banners.push(banner);
      state.errors.push(text);
      state.turnInProgress = false;
      state.stopPending = false;
      const sessionId = state.sessionId;
      if (state.eventSource) {
        state.eventSource.close();
        state.eventSource = null;
      }
      refreshSessionState().then(() => {
        if (state.sessionId === sessionId) connectSse(sessionId);
      }).catch((err) => {
        state.errors.push(`session refresh failed: ${err.message}`);
        scheduleReconnect(1000);
      }).finally(render);
    },
    turn_complete: (ev) => {
      dismissAskDock();
      const t = state.currentTurn;
      state.turnInProgress = false;
      state.stopPending = false;
      if (!t) return;
      t.completedAt = Date.now();
      t.assistantText = stripActivityMarkup(t.pendingAssistantText);
      t.pendingAssistantText = "";
      t.streaming = false;
      t.complete = true;
      t.outcome = ev.outcome || "progressed";
      state.productionOutcome = t.outcome;
      applyProductionSnapshot(ev);
      // Backend now sends only user-facing deliverables in final_asset_ids
      // (usually export outputs). Mark every listed deliverable as final.
      const finals = ev.deliverable_asset_ids || ev.final_asset_ids || [];
      for (const deliverable of finals) {
        const existing = state.assets.find((a) => a.asset_id === deliverable);
        if (existing) existing.final = true;
      }
      // Refresh timeline after every completed turn — verb results may have
      // updated the project even if no timeline_op event was fired this turn.
      fetchProjectTimeline({ force: true });
      // While planning, a completed turn means the plan text is on screen —
      // surface the approval bar.
      if (state.planMode) state.planReady = true;
      // Auto-save after every completed turn; auto-title at turn 1 and 5.
      autoSaveSession();
      if (state.userMessageCount === 1 || state.userMessageCount === 5) {
        autoGenerateTitle();
      }
    },
    turn_error: (ev) => {
      dismissAskDock();
      state.turnInProgress = false;
      state.stopPending = false;
      const t = state.currentTurn;
      if (t) {
        t.completedAt = Date.now();
        t.streaming = false;
        t.complete = true;
        // An "incomplete_goal" stop is not a failure — the model has already
        // delivered its own words (when the turn did work) and a soft
        // turn_wrapup note follows. Render it gently, never as a red interrupt.
        // Genuine host failures (budget, doom loop, stream error) still show
        // the turn_error banner.
        if (ev.reason !== "incomplete_goal") {
          const errorReason = String(ev.error || ev.message || "未知错误");
          t.banners.push({ kind: "turn_error", text: creatorErrorMessage(errorReason) });
        }
      }
    },
  };

  function dispatch(ev) {
    // Debug hook: raw event log accessible from DevTools console and test harnesses.
    (window.__lumeriEvents = window.__lumeriEvents || []).push(ev);
    const handler = handlers[ev.kind];
    if (!handler) {
      const t = state.currentTurn;
      const banner = { kind: "unknown", text: "收到一个暂未显示的活动状态" };
      if (t) t.banners.push(banner);
      state.errors.push(banner.text);
      console.error("unhandled SSE event kind", ev);
      return;
    }
    handler(ev);
  }

  function inferKindFromAssetId(assetId) {
    if (String(assetId).startsWith("img_")) return "image";
    if (String(assetId).startsWith("aud_")) return "audio";
    return "video";
  }

  function artifactUrl(assetId) {
    if (state.projectId) {
      return `/projects/${encodeURIComponent(state.projectId)}/artifacts/${encodeURIComponent(assetId)}`;
    }
    return `/sessions/${encodeURIComponent(state.sessionId || "")}/assets/${encodeURIComponent(assetId)}`;
  }

  function currentReviewMaster() {
    if (!["ready_for_review", "accepted"].includes(String(state.productionState || ""))) return null;
    const delivery = state.productionDelivery;
    const master = delivery && typeof delivery === "object" ? delivery.review_master : null;
    if (!master || typeof master !== "object") return null;
    if (Number(delivery.project_revision) !== Number(state.projectRevision)) return null;
    if (Number(master.project_revision) !== Number(state.projectRevision)) return null;
    if (!/^[A-Za-z0-9][A-Za-z0-9_-]{0,95}$/.test(String(master.asset_id || ""))) return null;
    if (!/^[0-9a-f]{64}$/.test(String(master.sha256 || ""))) return null;
    return master;
  }

  function currentTimelinePreview() {
    const reviewMaster = currentReviewMaster();
    if (reviewMaster) return reviewMaster;
    const playable = state.assets.filter((asset) => {
      if (String(asset?.kind || "") !== "video") return false;
      if (asset?.final) return true;
      const sourceKind = typeof asset?.source === "object"
        ? String(asset.source?.kind || "")
        : String(asset?.source || "");
      const summary = String(asset?.summary || "");
      return sourceKind === "derived_preview"
        || /timeline preview|timeline inspection composited draft/i.test(summary);
    });
    return playable[playable.length - 1] || null;
  }

  function unloadDeliveryReviewMaster() {
    if (!els.deliveryReviewVideo) return;
    try { els.deliveryReviewVideo.pause(); } catch {}
    els.deliveryReviewVideo.removeAttribute("src");
    delete els.deliveryReviewVideo.dataset.assetId;
    try { els.deliveryReviewVideo.load(); } catch {}
    if (els.deliveryReviewMaster) els.deliveryReviewMaster.hidden = true;
    if (els.timelinePreviewEmpty) els.timelinePreviewEmpty.hidden = false;
  }

  function renderDeliveryReviewMaster() {
    if (!els.deliveryReviewMaster || !els.deliveryReviewVideo) return;
    bindTimelinePreviewSync();
    const preview = currentTimelinePreview();
    if (!preview) {
      if (els.deliveryReviewVideo.dataset.assetId) unloadDeliveryReviewMaster();
      else els.deliveryReviewMaster.hidden = true;
      if (els.timelinePreviewEmpty) els.timelinePreviewEmpty.hidden = false;
      return;
    }
    const assetId = String(preview.asset_id);
    const url = artifactUrl(assetId);
    if (els.deliveryReviewVideo.dataset.assetId !== assetId) {
      try { els.deliveryReviewVideo.pause(); } catch {}
      els.deliveryReviewVideo.src = url;
      els.deliveryReviewVideo.dataset.assetId = assetId;
      els.deliveryReviewVideo.load();
    }
    if (els.timelinePreviewEmpty) els.timelinePreviewEmpty.hidden = true;
    els.deliveryReviewMaster.hidden = false;
    syncTimelinePreviewToPlayhead();
  }

  // ── ask dock (declarative answering) ────────────────────────────────

  function dismissAskDock() {
    state.pendingAsk = null;
    if (els.askDock) {
      els.askDock.hidden = true;
      els.askDock.innerHTML = "";
    }
  }

  function _askControlDom(key, ctrl) {
    const wrap = document.createElement("div");
    wrap.className = "ask-field";
    wrap.dataset.controlKey = key;

    const label = document.createElement("span");
    label.className = "ask-field-label";
    label.textContent = key;
    wrap.appendChild(label);

    const errEl = document.createElement("div");
    errEl.className = "ask-field-error";

    const type = ctrl.type;
    if (type === "select") {
      const group = document.createElement("div");
      group.className = "ask-radio-group";
      for (const opt of (ctrl.options || [])) {
        const lbl = document.createElement("label");
        const radio = document.createElement("input");
        radio.type = "radio";
        radio.name = `ask-${key}`;
        radio.value = opt.value;
        if (ctrl.default != null && opt.value === ctrl.default) radio.checked = true;
        lbl.appendChild(radio);
        lbl.appendChild(document.createTextNode(opt.label));
        group.appendChild(lbl);
      }
      wrap.appendChild(group);
    } else if (type === "multi_select") {
      const group = document.createElement("div");
      group.className = "ask-check-group";
      for (const opt of (ctrl.options || [])) {
        const lbl = document.createElement("label");
        const chk = document.createElement("input");
        chk.type = "checkbox";
        chk.name = `ask-${key}`;
        chk.value = opt.value;
        lbl.appendChild(chk);
        lbl.appendChild(document.createTextNode(opt.label));
        group.appendChild(lbl);
      }
      wrap.appendChild(group);
    } else if (type === "text") {
      const inp = ctrl.multiline ? document.createElement("textarea") : document.createElement("input");
      inp.className = "ask-text-input";
      inp.name = `ask-${key}`;
      if (ctrl.placeholder) inp.placeholder = ctrl.placeholder;
      if (ctrl.max_length) inp.maxLength = ctrl.max_length;
      if (ctrl.multiline) inp.rows = 3;
      wrap.appendChild(inp);
    } else if (type === "slider") {
      const sw = document.createElement("div");
      sw.className = "ask-slider-wrap";
      const range = document.createElement("input");
      range.type = "range";
      range.name = `ask-${key}`;
      range.min = ctrl.min ?? 0;
      range.max = ctrl.max ?? 100;
      range.step = ctrl.step ?? 1;
      range.value = ctrl.default ?? ctrl.min ?? 0;
      const valSpan = document.createElement("span");
      valSpan.className = "ask-slider-val";
      valSpan.textContent = range.value;
      range.addEventListener("input", () => { valSpan.textContent = range.value; });
      sw.appendChild(range);
      sw.appendChild(valSpan);
      wrap.appendChild(sw);
    } else if (type === "panel") {
      const pg = document.createElement("div");
      pg.className = "ask-panel-group";
      if (ctrl.description) {
        const t = document.createElement("div");
        t.className = "ask-panel-group-title";
        t.textContent = ctrl.description;
        pg.appendChild(t);
      }
      for (const [fk, fv] of Object.entries(ctrl.fields || {})) {
        pg.appendChild(_askControlDom(`${key}.${fk}`, fv));
      }
      wrap.appendChild(pg);
    } else {
      const note = document.createElement("div");
      note.style.cssText = "font-size:11px;opacity:.6";
      note.textContent = `(${type || "unknown"} control — 不支持在线编辑)`;
      wrap.appendChild(note);
    }

    wrap.appendChild(errEl);
    return wrap;
  }

  function _collectAskValue(key, ctrl, dock) {
    const type = ctrl.type;
    if (type === "select") {
      const checked = dock.querySelector(`input[name="ask-${CSS.escape(key)}"]:checked`);
      return checked ? checked.value : null;
    }
    if (type === "multi_select") {
      return [...dock.querySelectorAll(`input[name="ask-${CSS.escape(key)}"]:checked`)]
        .map((c) => c.value);
    }
    if (type === "text") {
      const inp = dock.querySelector(`[name="ask-${CSS.escape(key)}"]`);
      return inp ? inp.value : "";
    }
    if (type === "slider") {
      const inp = dock.querySelector(`input[name="ask-${CSS.escape(key)}"]`);
      return inp ? parseFloat(inp.value) : null;
    }
    if (type === "panel") {
      const result = {};
      for (const [fk, fv] of Object.entries(ctrl.fields || {})) {
        result[fk] = _collectAskValue(`${key}.${fk}`, fv, dock);
      }
      return result;
    }
    return null;
  }

  function renderAskDock() {
    const dock = els.askDock;
    if (!dock || !state.pendingAsk) return;
    const q = state.pendingAsk.question;

    dock.innerHTML = "";

    const title = document.createElement("div");
    title.className = "ask-dock-title";
    title.textContent = q.title || "需要你的输入";
    dock.appendChild(title);

    const desc = document.createElement("div");
    desc.className = "ask-dock-desc";
    desc.textContent = q.description || "";
    dock.appendChild(desc);

    for (const [key, ctrl] of Object.entries(q.controls || {})) {
      dock.appendChild(_askControlDom(key, ctrl));
    }

    const actions = document.createElement("div");
    actions.className = "ask-actions";
    const submitBtn = document.createElement("button");
    submitBtn.className = "ask-submit";
    submitBtn.textContent = "提交";
    submitBtn.addEventListener("click", () => submitAskAnswer());
    actions.appendChild(submitBtn);
    dock.appendChild(actions);

    dock.hidden = false;
  }

  async function submitAskAnswer() {
    if (!state.pendingAsk || !state.sessionId) return;
    const q = state.pendingAsk.question;
    const dock = els.askDock;

    const answers = {};
    for (const [key, ctrl] of Object.entries(q.controls || {})) {
      answers[key] = _collectAskValue(key, ctrl, dock);
    }

    const submitBtn = dock.querySelector(".ask-submit");
    if (submitBtn) submitBtn.disabled = true;

    dock.querySelectorAll(".ask-field-error").forEach((e) => { e.textContent = ""; });

    try {
      const res = await apiFetch(`/sessions/${state.sessionId}/ask_response`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          question_id: state.pendingAsk.question_id,
          answers,
        }),
      });
      if (res.ok) {
        dismissAskDock();
        return;
      }
      const body = await res.json().catch(() => ({}));
      if (res.status === 422 && body.field_errors) {
        for (const [field, msg] of Object.entries(body.field_errors)) {
          const el = dock.querySelector(`[data-control-key="${CSS.escape(field)}"] .ask-field-error`);
          if (el) el.textContent = msg;
        }
      } else {
        state.currentTurn?.banners.push({
          kind: "info",
          text: "提交未成功，请稍后重试",
        });
        render();
      }
    } catch (err) {
      state.currentTurn?.banners.push({
        kind: "info",
        text: "提交未成功，请稍后重试",
      });
      render();
    } finally {
      if (submitBtn) submitBtn.disabled = false;
    }
  }

  // ── SSE connection ──────────────────────────────────────────────────

  // Status is a bare dot — the state name lives in title/aria-label only.
  function setConnPill(text, cls) {
    els.connPill.title = text;
    els.connPill.setAttribute("aria-label", text);
    els.connPill.className = `status-pill ${cls}`;
  }

  function scheduleReconnect(delayMs = 1000) {
    clearReconnectTimer();
    state.reconnectTimer = window.setTimeout(() => {
      state.reconnectTimer = null;
      if (state.sessionId) connectSse(state.sessionId);
    }, delayMs);
  }

  function connectSse(sessionId) {
    if (isLocalWorkspace) {
      setConnPill("iPad 本地工作区", "live");
      return;
    }
    if (state.eventSource) {
      state.eventSource.close();
      state.eventSource = null;
    }
    const lastId = state.lastEventId || loadLastEventId(sessionId);
    const qs = lastId ? `?last_event_id=${encodeURIComponent(lastId)}` : "";
    const es = new EventSource(`/sessions/${sessionId}/stream${qs}`);
    es.onopen = () => {
      if (state.sessionId !== sessionId || state.eventSource !== es) return;
      clearReconnectTimer();
      setConnPill("live", "live");
    };
    es.onerror = () => {
      if (state.sessionId !== sessionId || state.eventSource !== es) return;
      setConnPill("reconnecting", "reconnecting");
      scheduleReconnect(1500);
    };
    es.onmessage = (e) => {
      // A closed EventSource may still have one queued callback. Never let an
      // event from the session we just left mutate the newly selected chat.
      if (state.sessionId !== sessionId || state.eventSource !== es) return;
      try {
        if (e.lastEventId) saveLastEventId(sessionId, e.lastEventId);
        const ev = JSON.parse(e.data);
        dispatch(ev);
        render();
      } catch {
        const banner = { kind: "unknown", text: "收到一个无法读取的活动状态" };
        state.currentTurn?.banners.push(banner);
        render();
      }
    };
    state.eventSource = es;
  }

  // ── Project timeline (CapCut-style editor) ──────────────────────────
  // A px/second timeline: adaptive ruler, wheel/key zoom, multi video+audio
  // tracks, zoom-adaptive per-clip filmstrip + waveform (when the clip
  // exposes asset_id),
  // draggable playhead, client-side markers, and drag/move/trim/split/delete
  // wired to the SAME /sessions/{id}/timeline/op endpoint as the model verbs.

  const TL = {
    pps: 64, minPps: 8, maxPps: 480,
    snap: true,
    playhead: 0,
    scrubbing: false,
    scrub: null,
    scrubFrame: 0,
    zoomAnchor: null,
    drag: null,
    markers: [],            // {time,label,color} — client-side only (no backend marker model yet)
    extraTracks: [],        // client-added empty display lanes (no backend add_track op)
    built: false,
    model: null,
    rulerCtx: null,
    frames: new Map(),      // assetId|time → frame dataURL, shared across zoom levels
    frameFail: new Set(),   // assetIds whose extraction failed → solid clips, no retry
    frameRigs: new Map(),   // assetId → {video,dur,aspect,queue,running,tick} persistent extractor
    _rigTick: 0,
    wave: new Map(),        // assetId -> number[] peaks
    waveBusy: new Set(),
    audioCtx: null,
    previewSyncBound: false,
  };
  const TL_RULER_H = 26, TL_TRACK_H = 58, TL_LANE_PAD = 10, TL_MIN_CONTENT = 30;
  const TL_MARKER_COLORS = ["#ff3b4e", "#ffb13b", "#4ea1ff", "#7a5cff", "#2fd178"];
  const TL_CLIP_COLOR = {
    video:   ["#1d4a34", "#37a06a"],
    image:   ["#34295f", "#7a5cff"],
    lottie:  ["#203f46", "#35c3b8"],
    paint:   ["#4c2148", "#ff5ac8"],
    audio:   ["#1f3a5c", "#4ea1ff"],
    text:    ["#4a2c18", "#d98a4e"],
    overlay: ["#34295f", "#7a5cff"],
  };

  const tlPanel   = () => document.getElementById("project-timeline-panel");
  const tlScroll  = () => document.getElementById("ptl-scroll");
  const tlContent = () => document.getElementById("ptl-content");
  const tlRuler   = () => document.getElementById("ptl-ruler");
  const tlHeaders = () => document.getElementById("ptl-headers");
  const timeToX = (t) => t * TL.pps;
  const pxToTime = (px) => px / TL.pps;
  const timelineUiScale = () => {
    const value = Number.parseFloat(getComputedStyle(tlPanel() || document.documentElement)
      .getPropertyValue("--timeline-ui-scale"));
    return Number.isFinite(value) ? value : 1;
  };
  const timelineTrackHeight = () => TL_TRACK_H * timelineUiScale();
  const timelineLanePad = () => TL_LANE_PAD * timelineUiScale();

  function applyTimelineTrackScale() {
    if (!TL.built || state.ptDrag) return;
    const content = tlContent(), headers = tlHeaders();
    if (!content || !headers) return;
    const trackHeight = timelineTrackHeight();
    const lanePad = timelineLanePad();
    const heads = headers.querySelectorAll(".ptl-head");
    const lanes = content.querySelectorAll(".ptl-lane");
    content.style.height = `${lanes.length * trackHeight + lanePad * 2}px`;
    headers.style.paddingTop = `${lanePad}px`;
    headers.style.paddingBottom = `${lanePad}px`;
    heads.forEach((head) => { head.style.height = `${trackHeight}px`; });
    lanes.forEach((lane, index) => {
      lane.style.top = `${lanePad + index * trackHeight}px`;
      lane.style.height = `${trackHeight}px`;
    });
    sizeRuler();
    drawRuler();
  }

  function fmtTC(s) {
    if (!isFinite(s) || s < 0) s = 0;
    const fps = Math.round((TL.model && TL.model.fps) || 30);
    const m = Math.floor(s / 60), sec = Math.floor(s % 60);
    const f = Math.floor((s - Math.floor(s)) * fps);
    return `${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}:${String(f).padStart(2, "0")}`;
  }

  function trackCompatible(mediaKind, trackKind) {
    if (mediaKind === "audio") return trackKind === "audio";
    if (mediaKind === "video") return trackKind === "video";
    if (mediaKind === "image") return trackKind === "video" || trackKind === "overlay";
    if (mediaKind === "text" || mediaKind === "lottie") return trackKind === "overlay";
    return false;
  }

  // Build the model the editor lays out: real tracks if present, else default
  // empty Video+Audio lanes so the timeline shows on load. Client-added lanes
  // (extraTracks) are merged in unless a real track already uses the id.
  function timelineModel(data) {
    const d = data || state.projectTimeline || {};
    let tracks = Array.isArray(d.tracks) ? d.tracks.map((t) => ({ ...t })) : [];
    if (!tracks.length) {
      tracks = [
        { id: "V1", kind: "video", name: "视频", clips: [] },
        { id: "A1", kind: "audio", name: "音频", clips: [] },
      ];
    }
    const rank = (k) => (k === "audio" ? 1 : 0);
    tracks = tracks.map((t, i) => ({ ...t, _i: i })).sort((a, b) => rank(a.kind) - rank(b.kind) || a._i - b._i);
    let lastEnd = 0;
    for (const t of tracks) for (const c of (t.clips || [])) lastEnd = Math.max(lastEnd, (c.start || 0) + (c.duration || 0));
    const contentDur = Math.max(d.duration || 0, lastEnd, TL_MIN_CONTENT);
    return { tracks, duration: d.duration || 0, mediaEnd: lastEnd, contentDur, fps: d.fps || 30, width: d.width || 1920, height: d.height || 1080, patch_seq: d.patch_seq || 0 };
  }

  function buildTimelineShell() {
    if (TL.built) return;
    const panel = tlPanel();
    if (!panel) return;
    panel.hidden = false;
    panel.classList.add("ptl");
    panel.innerHTML = `
      <div class="ptl-toolbar">
        <div class="ptl-tgroup">
          <button class="ptl-btn ptl-ico-btn pt-edit-btn" id="ptl-undo" title="撤销 (${shortcutPrefix}Z)"><svg viewBox="0 0 16 16"><path d="M6.5 4 3.5 7l3 3"/><path d="M3.5 7H10a3.5 3.5 0 0 1 0 7H7.5"/></svg></button>
          <button class="ptl-btn ptl-ico-btn pt-edit-btn" id="ptl-redo" title="重做"><svg viewBox="0 0 16 16"><path d="M9.5 4l3 3-3 3"/><path d="M12.5 7H6a3.5 3.5 0 0 0 0 7h2.5"/></svg></button>
        </div>
        <div class="ptl-sep"></div>
        <div class="ptl-tgroup">
          <button class="ptl-btn ptl-ico-btn pt-edit-btn" id="ptl-split" title="在指针处分割 (S)" aria-label="分割"><svg viewBox="0 0 16 16"><path d="M8 2.5v11"/><rect x="2.6" y="5" width="3.4" height="6" rx="1.1"/><rect x="10" y="5" width="3.4" height="6" rx="1.1"/></svg></button>
          <button class="ptl-btn ptl-ico-btn pt-edit-btn" id="ptl-delete" title="删除所选 (Del)" aria-label="删除"><svg viewBox="0 0 16 16"><path d="M3 4.5h10"/><path d="M6 4.5V3h4v1.5"/><path d="M4.6 4.5 5.1 13.3h5.8l.5-8.8"/><path d="M6.9 6.8v4.3M9.1 6.8v4.3"/></svg></button>
          <button class="ptl-btn ptl-ico-btn pt-edit-btn" id="ptl-marker" title="在指针处加标记 (M)" aria-label="标记"><svg viewBox="0 0 16 16"><path d="M4.5 2v12"/><path d="M4.5 2.8h7.3l-1.8 2.6 1.8 2.6H4.5"/></svg></button>
        </div>
        <div class="ptl-sep"></div>
        <button class="ptl-btn ptl-ico-btn ptl-toggle" id="ptl-snap" title="吸附对齐" aria-label="吸附对齐"><svg viewBox="0 0 16 16"><path d="M4 2.5v5a4 4 0 0 0 8 0v-5"/><path d="M4 2.5h2.4M9.6 2.5H12M4 6h2.4M9.6 6H12"/></svg></button>
        <div class="ptl-spacer"></div>
        <div class="ptl-tc" id="ptl-tc">00:00:00</div>
        <div class="ptl-zoom">
          <button class="ptl-btn ptl-ico-btn" id="ptl-zoom-out" title="缩小 (−)"><svg viewBox="0 0 16 16"><circle cx="6.8" cy="6.8" r="3.8"/><path d="M9.6 9.6 13.5 13.5"/><path d="M5 6.8h3.6"/></svg></button>
          <input type="range" id="ptl-zoom" class="ptl-range" min="${TL.minPps}" max="${TL.maxPps}" value="${TL.pps}" />
          <button class="ptl-btn ptl-ico-btn" id="ptl-zoom-in" title="放大 (＋)"><svg viewBox="0 0 16 16"><circle cx="6.8" cy="6.8" r="3.8"/><path d="M9.6 9.6 13.5 13.5"/><path d="M5 6.8h3.6M6.8 5v3.6"/></svg></button>
        </div>
      </div>
      <div class="ptl-main">
        <div class="ptl-ruler-row">
          <div class="ptl-corner"></div>
          <canvas id="ptl-ruler"></canvas>
        </div>
        <div class="ptl-lanes-row">
          <div class="ptl-headers" id="ptl-headers"></div>
          <div class="ptl-scroll" id="ptl-scroll"><div class="ptl-content" id="ptl-content"></div></div>
        </div>
      </div>
      <div class="pt-quick-actions" id="pt-quick-actions">
        <button class="pt-action-btn" data-cmd="export the project at 1080p quality" title="导出 1080p"><svg viewBox="0 0 24 24" aria-hidden="true"><use href="#i-export"/></svg>1080p</button>
        <button class="pt-action-btn" data-cmd="export the project as draft quality" title="导出草稿"><svg viewBox="0 0 24 24" aria-hidden="true"><use href="#i-export"/></svg>草稿</button>
        <button class="pt-action-btn" data-cmd="add a title overlay at the start of the timeline" title="在片头加标题"><svg viewBox="0 0 24 24" aria-hidden="true"><use href="#i-text"/></svg>标题</button>
        <button class="pt-action-btn" data-cmd="get the current timeline layout" title="获取时间线布局"><svg viewBox="0 0 24 24" aria-hidden="true"><use href="#i-layers"/></svg>布局</button>
      </div>`;

    TL.rulerCtx = tlRuler().getContext("2d");
    TL.built = true;
    loadMarkers();

    document.getElementById("ptl-undo").onclick = () => { if (editingEnabled()) postTimelineOp({ op: "undo", steps: 1 }); };
    document.getElementById("ptl-redo").onclick = () => { /* backend exposes no redo op yet */ };
    document.getElementById("ptl-split").onclick = splitSelected;
    document.getElementById("ptl-delete").onclick = deleteSelected;
    document.getElementById("ptl-marker").onclick = () => addMarker(TL.playhead);
    const snapBtn = document.getElementById("ptl-snap");
    const syncSnap = () => snapBtn.classList.toggle("on", TL.snap);
    snapBtn.onclick = () => { TL.snap = !TL.snap; syncSnap(); };
    syncSnap();
    const zoom = document.getElementById("ptl-zoom");
    zoom.oninput = () => setPps(+zoom.value);
    document.getElementById("ptl-zoom-in").onclick = () => setPps(TL.pps * 1.25);
    document.getElementById("ptl-zoom-out").onclick = () => setPps(TL.pps / 1.25);

    const scroll = tlScroll();
    let hydrateQueued = false;
    scroll.addEventListener("scroll", () => {
      if (TL.zoomAnchor) {
        TL.zoomAnchor.time = pxToTime(scroll.scrollLeft + TL.zoomAnchor.px);
      }
      drawRuler();
      tlHeaders().style.transform = `translateY(${-scroll.scrollTop}px)`;
      // clips scrolled into view lazily extract their frames (rAF-throttled)
      if (!hydrateQueued) {
        hydrateQueued = true;
        requestAnimationFrame(() => { hydrateQueued = false; hydrateMedia(); });
      }
    });
    // Mouse wheel = zoom anchored at the cursor (like 剪映). Shift-wheel or a
    // horizontal-dominant wheel = pan. ⌘/ctrl also zoom (trackpad pinch).
    const wheelZoom = (e, rectEl) => {
      if (e.shiftKey || Math.abs(e.deltaX) > Math.abs(e.deltaY)) {
        e.preventDefault();
        scroll.scrollLeft += (Math.abs(e.deltaX) > Math.abs(e.deltaY) ? e.deltaX : e.deltaY);
        return;
      }
      if (!e.deltaY) return;
      e.preventDefault();
      const px = e.clientX - rectEl.getBoundingClientRect().left;
      setPps(TL.pps * (e.deltaY < 0 ? 1.12 : 0.89), pxToTime(scroll.scrollLeft + px), px);
    };
    scroll.addEventListener("wheel", (e) => wheelZoom(e, scroll), { passive: false });

    const ruler = tlRuler();
    const rememberZoomAnchor = (clientX) => {
      const r = scroll.getBoundingClientRect();
      const px = Math.max(0, Math.min(r.width, clientX - r.left));
      TL.zoomAnchor = { time: pxToTime(scroll.scrollLeft + px), px };
    };
    const seek = (clientX) => {
      rememberZoomAnchor(clientX);
      const r = scroll.getBoundingClientRect();
      const px = Math.max(0, Math.min(r.width, clientX - r.left));
      setPlayhead(pxToTime(scroll.scrollLeft + px));
    };
    const scrubStep = () => {
      const scrub = TL.scrub;
      if (!scrub) { TL.scrubFrame = 0; return; }
      const r = scroll.getBoundingClientRect();
      const edge = Math.min(r.width / 2, 54 * timelineUiScale());
      let targetVelocity = 0;
      if (scrub.clientX < r.left + edge) {
        targetVelocity = -18 * Math.min(1, (r.left + edge - scrub.clientX) / Math.max(1, edge));
      } else if (scrub.clientX > r.right - edge) {
        targetVelocity = 18 * Math.min(1, (scrub.clientX - (r.right - edge)) / Math.max(1, edge));
      }
      scrub.velocity = scrub.velocity * 0.72 + targetVelocity * 0.28;
      if (Math.abs(scrub.velocity) > 0.1) {
        const before = scroll.scrollLeft;
        scroll.scrollLeft += scrub.velocity;
        if (scroll.scrollLeft !== before) seek(scrub.clientX);
      }
      TL.scrubFrame = requestAnimationFrame(scrubStep);
    };
    const startScrub = (e, surface) => {
      if (e.button !== 0 || e.target.closest(".ptl-clip")) return;
      if (TL.scrubFrame) cancelAnimationFrame(TL.scrubFrame);
      TL.scrubbing = true;
      TL.scrub = { pointerId: e.pointerId, surface, clientX: e.clientX, velocity: 0 };
      scroll.classList.add("is-scrubbing");
      try { surface.setPointerCapture(e.pointerId); } catch {}
      seek(e.clientX);
      TL.scrubFrame = requestAnimationFrame(scrubStep);
      e.preventDefault();
    };
    const moveScrub = (e) => {
      rememberZoomAnchor(e.clientX);
      if (!TL.scrub || TL.scrub.pointerId !== e.pointerId) return;
      TL.scrub.clientX = e.clientX;
      seek(e.clientX);
    };
    const finishScrub = (e) => {
      if (!TL.scrub || (e && TL.scrub.pointerId !== e.pointerId)) return;
      TL.scrub = null;
      TL.scrubbing = false;
      scroll.classList.remove("is-scrubbing");
      if (TL.scrubFrame) cancelAnimationFrame(TL.scrubFrame);
      TL.scrubFrame = 0;
    };
    for (const surface of [ruler, scroll]) {
      surface.addEventListener("pointerdown", (e) => startScrub(e, surface));
      surface.addEventListener("pointermove", moveScrub);
      surface.addEventListener("pointerup", finishScrub);
      surface.addEventListener("pointercancel", finishScrub);
    }
    ruler.addEventListener("dblclick", (e) => {
      const r = ruler.getBoundingClientRect();
      addMarker(Math.max(0, pxToTime(scroll.scrollLeft + (e.clientX - r.left))));
    });
    ruler.addEventListener("wheel", (e) => wheelZoom(e, ruler), { passive: false });

    setupClipPointer();
    setupTimelineKeys();
    window.addEventListener("resize", () => { sizeRuler(); drawRuler(); });
    renderProjectTimeline(state.projectTimeline);   // show empty editor immediately
  }

  // ── ruler ───────────────────────────────────────────────────────────
  function sizeRuler() {
    const ruler = tlRuler(); if (!ruler || !TL.rulerCtx) return;
    const w = ruler.clientWidth || 1, dpr = window.devicePixelRatio || 1;
    ruler.width = Math.max(1, Math.floor(w * dpr));
    ruler.height = Math.floor(TL_RULER_H * dpr);
    TL.rulerCtx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }
  function chooseStep() {
    const steps = [0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600];
    for (const s of steps) if (s * TL.pps >= 66) return s;
    return 600;
  }
  function fmtRulerLabel(t, step) {
    const m = Math.floor(t / 60), s = t % 60;
    if (step < 1) return `${m}:${String(Math.floor(s)).padStart(2, "0")}.${Math.round((s - Math.floor(s)) * 10)}`;
    return `${m}:${String(Math.floor(s)).padStart(2, "0")}`;
  }
  function drawRuler() {
    const ruler = tlRuler(), ctx = TL.rulerCtx, scroll = tlScroll();
    if (!ruler || !ctx || !scroll) return;
    const dpr = window.devicePixelRatio || 1;
    if (ruler.width !== Math.floor((ruler.clientWidth || 1) * dpr)) sizeRuler();
    const w = ruler.clientWidth || 1, h = TL_RULER_H, left = scroll.scrollLeft;
    ctx.clearRect(0, 0, w, h);
    ctx.fillStyle = "#0c0f13"; ctx.fillRect(0, 0, w, h);
    const step = chooseStep();
    const minor = step / (step >= 5 ? 5 : 2);
    const tEnd = pxToTime(left + w);
    ctx.font = "10px ui-monospace, Menlo, monospace";
    ctx.strokeStyle = "#222a33"; ctx.beginPath();
    for (let t = Math.floor(left / TL.pps / minor) * minor; t <= tEnd + minor; t += minor) {
      const x = Math.round(timeToX(t) - left) + 0.5;
      if (x < -2 || x > w + 2) continue;
      ctx.moveTo(x, h - 6); ctx.lineTo(x, h);
    }
    ctx.stroke();
    ctx.strokeStyle = "#3c4654"; ctx.fillStyle = "#9aa4b2"; ctx.beginPath();
    for (let t = Math.floor(left / TL.pps / step) * step; t <= tEnd + step; t += step) {
      const x = Math.round(timeToX(t) - left) + 0.5;
      if (x < -40 || x > w + 40) continue;
      ctx.moveTo(x, 5); ctx.lineTo(x, h);
      ctx.fillText(fmtRulerLabel(t, step), x + 3, 13);
    }
    ctx.stroke();
    for (const mk of TL.markers) {
      const x = timeToX(mk.time) - left;
      if (x < -6 || x > w + 6) continue;
      ctx.fillStyle = mk.color || "#ffcf3b";
      ctx.beginPath(); ctx.moveTo(x, 2); ctx.lineTo(x + 5, 8); ctx.lineTo(x, 14); ctx.lineTo(x - 5, 8); ctx.closePath(); ctx.fill();
    }
    const px = timeToX(TL.playhead) - left;
    if (px >= -6 && px <= w + 6) {
      ctx.fillStyle = "#ff3b4e";
      ctx.beginPath(); ctx.moveTo(px - 5, 0); ctx.lineTo(px + 5, 0); ctx.lineTo(px, 7); ctx.closePath(); ctx.fill();
    }
  }

  // ── zoom / playhead / markers ───────────────────────────────────────
  function setPps(next, anchorTime, anchorPx) {
    const scroll = tlScroll(); if (!scroll) return;
    if (state.ptDrag) return;   // don't zoom mid-drag: clip DOM can't reflow under the render guard
    if (anchorTime == null) {
      const anchor = TL.zoomAnchor;
      anchorPx = anchor ? anchor.px : scroll.clientWidth / 2;
      anchorTime = anchor ? anchor.time : pxToTime(scroll.scrollLeft + anchorPx);
    }
    TL.pps = Math.max(TL.minPps, Math.min(TL.maxPps, next));
    const z = document.getElementById("ptl-zoom"); if (z) z.value = String(Math.round(TL.pps));
    renderProjectTimeline(state.projectTimeline);
    scroll.scrollLeft = Math.max(0, timeToX(anchorTime) - anchorPx);
    TL.zoomAnchor = { time: anchorTime, px: anchorPx };
    drawRuler();
  }
  function positionPlayhead() {
    const ph = document.getElementById("ptl-playhead");
    if (ph) ph.style.left = timeToX(TL.playhead) + "px";
    const tc = document.getElementById("ptl-tc");
    if (tc) tc.textContent = fmtTC(TL.playhead);
  }
  function syncTimelinePreviewToPlayhead() {
    const video = els.deliveryReviewVideo;
    if (!video || !video.dataset.assetId) return;
    const duration = Number(video.duration);
    const target = Number.isFinite(duration) && duration > 0
      ? Math.min(TL.playhead, duration)
      : TL.playhead;
    if (Math.abs((Number(video.currentTime) || 0) - target) < 0.025) return;
    try { video.currentTime = target; } catch {}
  }
  function bindTimelinePreviewSync() {
    const video = els.deliveryReviewVideo;
    if (!video || TL.previewSyncBound) return;
    TL.previewSyncBound = true;
    video.addEventListener("loadedmetadata", syncTimelinePreviewToPlayhead);
    const syncPlayheadFromPreview = () => {
      if (!video.dataset.assetId) return;
      setPlayhead(video.currentTime, { syncPreview: false });
    };
    video.addEventListener("timeupdate", syncPlayheadFromPreview);
    video.addEventListener("seeked", syncPlayheadFromPreview);
  }
  function setPlayhead(t, { syncPreview = true } = {}) {
    const mediaEnd = Math.max(0, Number(TL.model && TL.model.mediaEnd) || 0);
    TL.playhead = Math.max(0, Math.min(mediaEnd, Number(t) || 0));
    positionPlayhead();
    drawRuler();
    if (syncPreview) syncTimelinePreviewToPlayhead();
  }
  function markerKey() { return `lumeri:v3:markers:${state.sessionId || "_"}`; }
  function loadMarkers() {
    try { TL.markers = JSON.parse(window.localStorage.getItem(markerKey()) || "[]") || []; } catch { TL.markers = []; }
  }
  function saveMarkers() {
    try { window.localStorage.setItem(markerKey(), JSON.stringify(TL.markers)); } catch {}
    autoSaveSession().catch(() => {});
  }
  function positionMarkers() {
    const layer = document.getElementById("ptl-markers");
    if (!layer) return;
    layer.innerHTML = TL.markers.map((m) =>
      `<div class="ptl-marker" style="left:${timeToX(m.time)}px;border-color:${m.color || "#ffcf3b"}" title="${escapeHTML(m.label || "")}"></div>`
    ).join("");
  }
  function addMarker(time) {
    const m = { time: Math.max(0, +Number(time).toFixed(3)), label: `标记 ${TL.markers.length + 1}`, color: TL_MARKER_COLORS[TL.markers.length % TL_MARKER_COLORS.length] };
    const near = TL.markers.findIndex((x) => Math.abs(x.time - m.time) < 0.15);
    if (near >= 0) TL.markers.splice(near, 1); else TL.markers.push(m);
    TL.markers.sort((a, b) => a.time - b.time);
    saveMarkers(); positionMarkers(); drawRuler();
  }

  // ── clip element + lazy media (filmstrip / waveform) ────────────────
  function buildClipEl(clip, track) {
    const el = document.createElement("div");
    const kind = clip.media_kind || "video";
    const isPaint = String(clip.name || "").startsWith("paint:");
    el.className = `ptl-clip ${kind}` + (isPaint ? " paint" : "") + (clip.id === state.selectedClipId ? " selected" : "");
    el.dataset.clipId = clip.id;
    el.dataset.trackId = clip.track_id || track.id;
    el.dataset.start = clip.start;
    el.dataset.duration = clip.duration;
    el.dataset.sourceIn = clip.source_in ?? 0;
    el.dataset.sourceOut = clip.source_out ?? 0;
    el.dataset.mediaKind = kind;
    el.dataset.assetId = clip.asset_id || "";
    el.style.left = timeToX(clip.start) + "px";
    el.style.width = Math.max(timeToX(clip.duration), 8) + "px";
    const col = isPaint ? TL_CLIP_COLOR.paint : (TL_CLIP_COLOR[kind] || TL_CLIP_COLOR.video);
    el.style.setProperty("--cfill", col[0]);
    const label = kind === "text" ? (clip.text_config?.content?.slice(0, 24) || clip.name) : clip.name;
    // Outgoing transition (payload key "transition" ← lumerai transition_after):
    // a badge on the clip's right edge. Export renders a hard cut until xfade
    // lands, so the title says "preview only" honestly.
    const trans = clip.transition && clip.transition.kind && clip.transition.kind !== "cut" ? clip.transition : null;
    const transHtml = trans
      ? `<span class="ptl-clip-trans" title="${escapeHTML(trans.kind)} ${Number(trans.duration_sec || 0).toFixed(2)}s — 导出暂为硬切">⇄</span>`
      : "";
    el.innerHTML =
      `<div class="ptl-clip-media"></div><div class="ptl-clip-grad"></div>` +
      `<span class="ptl-clip-label">${escapeHTML(label || "clip")}</span>` + transHtml +
      `<div class="ptl-handle l" data-handle="left"></div><div class="ptl-handle r" data-handle="right"></div>`;
    return el;
  }
  function hydrateMedia() {
    const content = tlContent(); if (!content) return;
    const [vLo, vHi] = fsViewport();
    content.querySelectorAll(".ptl-clip").forEach((el) => {
      const assetId = el.dataset.assetId;
      if (!assetId) return;                       // no source → solid color (graceful)
      const x0 = parseFloat(el.style.left) || 0;
      const x1 = x0 + (parseFloat(el.style.width) || 0);
      if (x1 < vLo || x0 > vHi) return;           // off-screen → the scroll handler hydrates later
      if (el.dataset.mediaKind === "audio") ensureWaveform(el, assetId);
      else if (el.dataset.mediaKind === "video") paintFilmstrip(el, assetId);
      else if (el.dataset.mediaKind === "image") paintImageStrip(el, assetId);
    });
  }

  // Filmstrip = frames sampled on a per-zoom source-time grid (剪映-style).
  // The interval ladder is powers of two so a frame cached at one zoom level
  // is reused at every other level (each 0.8s-grid frame also sits on the
  // 0.4s grid); tile count follows TL.pps with no hard cap — zooming in
  // simply reveals more frames. Tiles are pinned to source time, and each
  // asset keeps one persistent <video> so zooming never re-downloads media.
  const FS_IVS = [0.2, 0.4, 0.8, 1.6, 3.2, 6.4, 12.8, 25.6, 51.2];
  const FS_TILE_H = 46, FS_VIEW_MARGIN = 1.0;   // extract only ±1 screen around the viewport
  const fsKey = (assetId, t) => `${assetId}|${t.toFixed(2)}`;

  function fsViewport(marginFactor = FS_VIEW_MARGIN) {
    const scroll = tlScroll();
    if (!scroll) return [-Infinity, Infinity];
    const m = scroll.clientWidth * marginFactor;
    return [scroll.scrollLeft - m, scroll.scrollLeft + scroll.clientWidth + m];
  }

  function fsPlan(el, assetId) {
    const inS = +el.dataset.sourceIn || 0;
    let outS = +el.dataset.sourceOut || 0;
    if (!(outS > inS)) outS = inS + Math.max(+el.dataset.duration || 0, 0.05);
    const rig = TL.frameRigs.get(assetId);
    const aspect = (rig && rig.aspect) || 16 / 9;
    const spt = (FS_TILE_H * aspect) / TL.pps;    // seconds an uncropped tile spans
    const iv = FS_IVS.find((v) => v >= spt) || FS_IVS[FS_IVS.length - 1];
    const tiles = [];
    for (let k = Math.floor(inS / iv); k * iv < outS; k++) {
      const t0 = Math.max(k * iv, inS), t1 = Math.min((k + 1) * iv, outS);
      if (t1 - t0 < 0.01) continue;
      // Sample at the cell's LEFT edge (k*iv, like NLEs), not its centre:
      // edges of the 2× coarser grid are a subset of this grid's edges, so
      // frames cached at one zoom level are reused at every other level.
      tiles.push({ x: (t0 - inS) * TL.pps, w: (t1 - t0) * TL.pps, t: +(k * iv).toFixed(2) });
    }
    return { iv, inS, outS, tiles };
  }

  function paintFilmstrip(el, assetId) {
    const media = el.querySelector(".ptl-clip-media");
    if (!media) return;
    if (TL.frameFail.has(assetId)) { media.innerHTML = ""; media.dataset.sig = "fail"; return; }
    const plan = fsPlan(el, assetId);
    const sig = `${plan.iv}|${TL.pps.toFixed(2)}|${plan.inS}|${plan.outS}`;
    if (media.dataset.sig !== sig) {
      media.dataset.sig = sig;
      media.innerHTML = plan.tiles.map((tile) => {
        const u = TL.frames.get(fsKey(assetId, tile.t));
        return `<div class="fs-tile" data-ft="${tile.t.toFixed(2)}"${u ? ` data-done="1"` : ""}`
          + ` style="left:${tile.x.toFixed(1)}px;width:${(tile.w + 0.5).toFixed(1)}px;${u ? `background-image:url(${u})` : ""}"></div>`;
      }).join("");
    } else {
      // same layout → only fill tiles whose frame landed since the last paint
      media.querySelectorAll(".fs-tile:not([data-done])").forEach((tile) => {
        const u = TL.frames.get(fsKey(assetId, +tile.dataset.ft));
        if (u) { tile.style.backgroundImage = `url(${u})`; tile.dataset.done = "1"; }
      });
    }
    const clipX = parseFloat(el.style.left) || 0;
    const [vLo, vHi] = fsViewport();
    const want = plan.tiles
      .filter((tile) => !TL.frames.has(fsKey(assetId, tile.t)))
      .filter((tile) => clipX + tile.x + tile.w >= vLo && clipX + tile.x <= vHi)
      .map((tile) => tile.t);
    if (want.length) requestFrames(assetId, want);
  }

  function paintImageStrip(el, assetId) {
    // Image clips repeat the still across the clip, filmstrip-style.
    const media = el.querySelector(".ptl-clip-media");
    if (!media || media.dataset.sig === "img") return;
    media.dataset.sig = "img";
    media.innerHTML = "";
    media.style.background = `url("${artifactUrl(assetId)}") left center / auto 100% repeat-x`;
  }

  function requestFrames(assetId, times) {
    let rig = TL.frameRigs.get(assetId);
    if (!rig) { rig = { video: null, dur: 0, aspect: 0, queue: new Set(), running: false, tick: 0 }; TL.frameRigs.set(assetId, rig); }
    rig.tick = ++TL._rigTick;
    times.forEach((t) => rig.queue.add(+(+t).toFixed(2)));
    if (!rig.running) runRig(assetId, rig);
  }

  async function runRig(assetId, rig) {
    if (rig.running) return;
    rig.running = true;
    try {
      if (!rig.video) {
        await fsOpenVideo(assetId, rig);
        fsEvictRigs(assetId);
        repaintAsset(assetId, true);   // real aspect known → tile geometry may change
      }
      const tw = Math.max(8, Math.round(FS_TILE_H * (rig.aspect || 16 / 9)));
      const canvas = document.createElement("canvas");
      canvas.width = tw; canvas.height = FS_TILE_H;
      const ctx = canvas.getContext("2d");
      while (rig.queue.size) {
        const t = Math.min(...rig.queue);
        rig.queue.delete(t);
        const key = fsKey(assetId, t);
        if (TL.frames.has(key)) continue;
        const dur = rig.dur || t + 1;
        await seekVideo(rig.video, Math.min(Math.max(0, t), Math.max(0, dur - 0.02)));
        try { ctx.drawImage(rig.video, 0, 0, tw, FS_TILE_H); TL.frames.set(key, canvas.toDataURL("image/jpeg", 0.55)); }
        catch { TL.frameFail.add(assetId); rig.queue.clear(); break; }   // tainted/undrawable → give up, no retry loop
        repaintAsset(assetId, false);
      }
    } catch {
      TL.frameFail.add(assetId);       // not a decodable video → solid clip, never retry
      rig.queue.clear();
      repaintAsset(assetId, true);
    } finally {
      rig.running = false;
      if (rig.queue.size) runRig(assetId, rig);
    }
  }

  function fsOpenVideo(assetId, rig) {
    return new Promise((resolve, reject) => {
      const video = document.createElement("video");
      video.muted = true; video.preload = "auto"; video.crossOrigin = "anonymous";
      const timer = setTimeout(() => reject(new Error("fs open timeout")), 12000);
      video.addEventListener("error", () => { clearTimeout(timer); reject(new Error("fs load error")); }, { once: true });
      video.addEventListener("loadedmetadata", () => {
        clearTimeout(timer);
        rig.dur = isFinite(video.duration) && video.duration > 0 ? video.duration : 0;
        rig.aspect = (video.videoWidth / Math.max(1, video.videoHeight)) || 16 / 9;
        rig.video = video;
        resolve();
      }, { once: true });
      video.src = artifactUrl(assetId);   // set src last, after listeners
    });
  }

  function fsEvictRigs(keepId, cap = 4) {
    // Keep at most `cap` decoders open; cached frames survive eviction and the
    // rig transparently reopens if a new zoom level needs more frames.
    const open = [...TL.frameRigs.entries()].filter(([id, r]) => r.video && id !== keepId && !r.running);
    open.sort((a, b) => a[1].tick - b[1].tick);
    while (open.length > cap - 1) {
      const [, r] = open.shift();
      try { r.video.removeAttribute("src"); r.video.load(); } catch {}
      r.video = null;
    }
  }

  function repaintAsset(assetId, replan) {
    const content = tlContent(); if (!content) return;
    content.querySelectorAll(`.ptl-clip[data-asset-id="${CSS.escape(assetId)}"]`).forEach((el) => {
      if (el.dataset.mediaKind !== "video") return;
      if (replan) { const m = el.querySelector(".ptl-clip-media"); if (m) delete m.dataset.sig; }
      paintFilmstrip(el, assetId);
    });
  }
  function seekVideo(video, t) {
    return new Promise((res) => {
      let settled = false;
      const on = () => { if (!settled) { settled = true; video.removeEventListener("seeked", on); clearTimeout(guard); res(); } };
      video.addEventListener("seeked", on);
      const guard = setTimeout(on, 1500);
      try { video.currentTime = t; } catch { on(); }
    });
  }
  function ensureWaveform(el, assetId) {
    const inS = +el.dataset.sourceIn, outS = +el.dataset.sourceOut;
    const key = `${assetId}|${inS.toFixed(2)}|${outS.toFixed(2)}`;   // trim-aware: each slice gets its own peaks
    const media = el.querySelector(".ptl-clip-media");
    const draw = (peaks) => { if (peaks && peaks.length && media && document.body.contains(el)) drawWave(media, peaks); };
    const cached = TL.wave.get(key);
    if (cached) { draw(cached); return; }
    if (TL.waveBusy.has(key)) return;
    TL.waveBusy.add(key);
    decodeWave(assetId, inS, outS).then((peaks) => {
      TL.waveBusy.delete(key);
      TL.wave.set(key, peaks || []);                    // cache (incl. empty) → no re-decode storm
      draw(peaks);
    }).catch(() => TL.waveBusy.delete(key));
  }
  async function decodeWave(assetId, inS, outS, samples = 240) {
    const res = await apiFetch(artifactUrl(assetId));
    if (!res.ok) throw new Error("wave fetch " + res.status);
    const buf = await res.arrayBuffer();
    if (!TL.audioCtx) TL.audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    const decoded = await TL.audioCtx.decodeAudioData(buf.slice(0));
    const ch = decoded.getChannelData(0);
    const sr = decoded.sampleRate || 44100;
    const s0 = inS > 0 ? Math.min(ch.length, Math.floor(inS * sr)) : 0;
    const s1 = (outS && outS > inS) ? Math.min(ch.length, Math.floor(outS * sr)) : ch.length;
    const len = Math.max(1, s1 - s0);
    const bucket = Math.max(1, Math.floor(len / samples));
    const peaks = new Array(samples).fill(0);
    let max = 0.0001;
    for (let i = 0; i < samples; i++) {
      let p = 0; const s = s0 + i * bucket, e = Math.min(s + bucket, s1);
      for (let j = s; j < e; j++) { const v = Math.abs(ch[j]); if (v > p) p = v; }
      peaks[i] = p; if (p > max) max = p;
    }
    for (let i = 0; i < samples; i++) peaks[i] /= max;
    return peaks;
  }
  function drawWave(media, peaks) {
    const w = Math.max(2, media.clientWidth), h = Math.max(2, media.clientHeight);
    const dpr = window.devicePixelRatio || 1;
    const canvas = document.createElement("canvas");
    canvas.width = Math.floor(w * dpr); canvas.height = Math.floor(h * dpr);
    const ctx = canvas.getContext("2d"); ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.strokeStyle = "rgba(180,225,255,0.85)"; ctx.lineWidth = 1; ctx.beginPath();
    const mid = h / 2;
    for (let x = 0; x < w; x++) {
      const p = peaks[Math.floor((x / w) * peaks.length)] || 0;
      const amp = p * (h / 2 - 2);
      ctx.moveTo(x + 0.5, mid - amp); ctx.lineTo(x + 0.5, mid + amp);
    }
    ctx.stroke();
    media.innerHTML = ""; media.appendChild(canvas);
  }

  async function fetchProjectTimeline(options = {}) {
    if (!state.sessionId) return;
    if (state.ptDrag) return;   // never re-fetch/reconcile mid-drag (would detach the dragged clip)
    if (TL.timelineFetchInFlight) return;
    TL.timelineFetchInFlight = true;
    const sessionId = state.sessionId;
    const activationSeq = runtimeActivationSeq;
    try {
      const r = await apiFetch(`/sessions/${sessionId}/timeline`);
      if (!r.ok) return;
      const data = await r.json();
      // A timeline request can finish after the creator has already selected
      // another session. Never paint the old production into the new view.
      if (state.sessionId !== sessionId || runtimeActivationSeq !== activationSeq) return;
      state.projectTimeline = data;
      if (!options.force && data.patch_seq === TL._renderedSeq) return;   // unchanged → skip 3s DOM rebuild + media re-hydrate
      renderProjectTimeline(data);
    } catch { /* ignore network errors */ }
    finally { TL.timelineFetchInFlight = false; }
  }

  function startTimelinePoll() {
    stopTimelinePoll();
    TL._renderedSeq = null;   // force the first fetch of a (new) session to render authoritative state
    state.timelinePollTimer = setInterval(fetchProjectTimeline, 5000);
    fetchProjectTimeline();
  }

  function stopTimelinePoll() {
    if (state.timelinePollTimer) {
      clearInterval(state.timelinePollTimer);
      state.timelinePollTimer = null;
    }
  }

  function renderProjectTimeline(data) {
    if (state.ptDrag) return;   // defensive: don't rebuild the DOM under an active drag
    if (!TL.built) { buildTimelineShell(); return; }   // build() calls back into render once
    const model = timelineModel(data);
    TL.model = model;
    TL._renderedSeq = model.patch_seq;   // poll skips re-render until patch_seq changes
    const content = tlContent(), headers = tlHeaders();
    if (!content || !headers) return;

    const contentW = Math.ceil(model.contentDur * TL.pps);
    content.style.width = contentW + "px";
    const trackHeight = timelineTrackHeight();
    const lanePad = timelineLanePad();
    content.style.height = (model.tracks.length * trackHeight + lanePad * 2) + "px";
    headers.style.paddingTop = lanePad + "px";
    headers.style.paddingBottom = lanePad + "px";

    headers.innerHTML = model.tracks.map((t) => {
      const isA = t.kind === "audio";
      return `<div class="ptl-head ${escapeHTML(t.kind)}" style="height:${trackHeight}px" title="${escapeHTML(t.name || t.id)}">`
        + `<span class="ptl-head-kind">${isA ? "♪" : "▦"} ${escapeHTML(t.id)}</span></div>`;
    }).join("");

    content.innerHTML = "";
    model.tracks.forEach((t, i) => {
      const lane = document.createElement("div");
      lane.className = `ptl-lane ${t.kind}`;
      lane.dataset.trackId = t.id;
      lane.dataset.trackKind = t.kind;
      lane.style.top = (lanePad + i * trackHeight) + "px";
      lane.style.height = trackHeight + "px";
      (t.clips || []).forEach((clip) => lane.appendChild(buildClipEl(clip, t)));
      content.appendChild(lane);
    });
    const ph = document.createElement("div"); ph.className = "ptl-playhead"; ph.id = "ptl-playhead"; content.appendChild(ph);
    const mk = document.createElement("div"); mk.id = "ptl-markers"; content.appendChild(mk);

    setPlayhead(TL.playhead);
    positionMarkers();
    sizeRuler();
    drawRuler();
    updateEditHint();
    requestAnimationFrame(hydrateMedia);
  }

  function fmtSec(s) {
    if (!isFinite(s)) return "0:00";
    const m = Math.floor(s / 60);
    const sec = Math.floor(s % 60);
    return `${m}:${String(sec).padStart(2, "0")}`;
  }

  // ── direct edit (DE): user drag/trim via the shared /timeline/op endpoint ──
  // Every gesture compiles to ONE patches.py op applied through the SAME
  // ProjectStore path as the model's verbs — no parallel edit state.

  function editingEnabled() {
    return !!state.sessionId && !state.turnInProgress;
  }

  async function postTimelineOp(opBody) {
    if (!state.sessionId) return null;
    try {
      const r = await apiFetch(`/sessions/${state.sessionId}/timeline/op`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          ...opBody,
          expected_project_revision: state.projectRevision,
        }),
      });
      const data = await r.json().catch(() => ({}));
      if (!r.ok) {
        // Rejected (E_OVERLAP/E_RANGE/…). Surface the typed code, snap back.
        state.errors.push(`edit rejected: ${[data.code, data.error].filter(Boolean).join(" ") || r.status}`);
        await fetchProjectTimeline();
        render();
        return null;
      }
      // Export honesty (docs/timeline-canonical-plan.md §4): the edit applied,
      // but stored fields the exporter won't render — surface the typed
      // W_NOT_EXPORTED warnings in the message strip. Warn, never silent.
      if (Array.isArray(data.warnings) && data.warnings.length) {
        for (const w of data.warnings) state.errors.push(String(w));
        render();
      }
      applyProductionSnapshot(data);
      state.projectTimeline = data;
      renderProjectTimeline(data);     // reconcile from authoritative post-state
      return data;
    } catch (err) {
      state.errors.push(`edit failed: ${err.message}`);
      await fetchProjectTimeline();
      render();
      return null;
    }
  }

  function selectClip(clipId) {
    state.selectedClipId = clipId;
    document.querySelectorAll("#ptl-content .ptl-clip").forEach((el) => {
      el.classList.toggle("selected", el.dataset.clipId === clipId);
    });
    updateEditHint();
  }

  function focusEntity(kind, id) {
    if (kind === "clip") {
      toggleDrawer(true);
      setActiveTab("timeline");
      selectClip(id);
      const el = document.querySelector(`#ptl-content .ptl-clip[data-clip-id="${CSS.escape(id)}"]`);
      if (el) el.scrollIntoView({ behavior: "smooth", block: "nearest", inline: "center" });
    } else if (kind === "asset") {
      const card = document.querySelector(`.asset-card[data-asset-id="${CSS.escape(id)}"]`);
      if (card) {
        card.scrollIntoView({ behavior: "smooth", block: "nearest" });
        card.classList.add("flash");
        setTimeout(() => card.classList.remove("flash"), 1200);
      }
    } else if (kind === "shot" || kind === "scene") {
      if (!stageTabs.includes("outline")) { stageTabs.push("outline"); saveStageTabs(); }
      setActiveTab("outline");
      const sel = kind === "shot"
        ? `.outline-row[data-shot-id="${CSS.escape(id)}"]`
        : `.outline-scene[data-scene-id="${CSS.escape(id)}"]`;
      requestAnimationFrame(() => {
        const el = document.querySelector(sel);
        if (el) {
          el.scrollIntoView({ behavior: "smooth", block: "nearest" });
          el.classList.add("flash");
          setTimeout(() => el.classList.remove("flash"), 1200);
        }
      });
    }
  }

  function selectedClip() {
    const tl = state.projectTimeline;
    if (!tl || !state.selectedClipId) return null;
    for (const tr of tl.tracks || []) {
      for (const c of (tr.clips || [])) {
        if (c.id === state.selectedClipId) return c;
      }
    }
    return null;
  }

  function updateEditHint() {
    const has = !!selectedClip() && editingEnabled();
    const split = document.getElementById("ptl-split");
    const del = document.getElementById("ptl-delete");
    if (split) split.disabled = !has;
    if (del) del.disabled = !has;
  }

  function splitSelected() {
    const c = selectedClip();
    if (!c || !editingEnabled()) return;
    const inside = TL.playhead > c.start + 0.04 && TL.playhead < c.start + c.duration - 0.04;
    const at = inside ? TL.playhead : c.start + c.duration / 2;
    postTimelineOp({ op: "split", clip_id: c.id, at_time: +at.toFixed(6) });
  }
  function deleteSelected() {
    const c = selectedClip();
    if (!c || !editingEnabled()) return;
    postTimelineOp({ op: "delete", clip_id: c.id }).then((r) => { if (r) { state.selectedClipId = null; updateEditHint(); } });
  }

  function laneUnder(clientY) {
    const content = tlContent(); if (!content) return null;
    let found = null;
    content.querySelectorAll(".ptl-lane").forEach((lane) => {
      const r = lane.getBoundingClientRect();
      if (clientY >= r.top && clientY <= r.bottom) found = lane;
    });
    return found;
  }

  // Pointer drag/trim on the content layer. Every gesture compiles to ONE
  // patches.py op (move/trim/set_time) through the shared /timeline/op path.
  function setupClipPointer() {
    const content = tlContent();
    if (!content) return;

    content.addEventListener("pointerdown", (ev) => {
      const clipEl = ev.target.closest(".ptl-clip");
      if (!clipEl) return;
      selectClip(clipEl.dataset.clipId);
      if (!editingEnabled()) return;
      const handle = ev.target.closest(".ptl-handle");
      const d = {
        clipId: clipEl.dataset.clipId,
        mode: handle ? handle.dataset.handle : "move",   // left | right | move
        startX: ev.clientX,
        origStart: parseFloat(clipEl.dataset.start) || 0,
        origDur: parseFloat(clipEl.dataset.duration) || 0,
        sourceIn: parseFloat(clipEl.dataset.sourceIn) || 0,
        sourceOut: parseFloat(clipEl.dataset.sourceOut) || 0,
        mediaKind: clipEl.dataset.mediaKind,
        origTrack: clipEl.dataset.trackId,
        el: clipEl,
      };
      TL.drag = d;
      state.ptDrag = d;             // pauses polling/reconcile mid-gesture
      clipEl.classList.add("dragging");
      try { clipEl.setPointerCapture(ev.pointerId); } catch {}
      ev.preventDefault();
    });

    content.addEventListener("pointermove", (ev) => {
      const d = TL.drag;
      if (!d) return;
      const dt = pxToTime(ev.clientX - d.startX);
      if (d.mode === "move") {
        d.pendStart = Math.max(0, snapSeconds(d.origStart + dt, d));
        d.el.style.left = timeToX(d.pendStart) + "px";
        const lane = laneUnder(ev.clientY);
        const tid = lane && lane.dataset.trackId;
        if (lane && tid && !tid.endsWith("*") && trackCompatible(d.mediaKind, lane.dataset.trackKind)) {
          d.pendTrack = tid;                            // only real (persisted) lanes are drop targets
          if (d.el.parentNode !== lane) lane.appendChild(d.el);
        }
      } else if (d.mode === "right") {
        const end = snapSeconds(d.origStart + d.origDur + dt, d);
        d.pendDur = Math.max(0.1, end - d.origStart);
        d.el.style.width = Math.max(timeToX(d.pendDur), 8) + "px";
      } else { // left-trim head
        const ns = snapSeconds(d.origStart + dt, d);
        const lo = Math.max(0, d.origStart - d.sourceIn);   // can't pull the head before the source start
        d.pendStart = Math.max(lo, Math.min(ns, d.origStart + d.origDur - 0.1));
        d.pendDur = d.origDur - (d.pendStart - d.origStart);
        d.el.style.left = timeToX(d.pendStart) + "px";
        d.el.style.width = Math.max(timeToX(d.pendDur), 8) + "px";
      }
    });

    const finish = () => {
      const d = TL.drag;
      if (!d) return;
      TL.drag = null; state.ptDrag = null;
      d.el.classList.remove("dragging");
      if (d.mode === "move" && d.pendStart != null) {
        const op = { op: "move", clip_id: d.clipId, start: +d.pendStart.toFixed(6) };
        if (d.pendTrack) op.track_id = d.pendTrack;
        postTimelineOp(op);
      } else if (d.mode === "right" && d.pendDur != null) {
        if (d.mediaKind === "video" || d.mediaKind === "audio")
          postTimelineOp({ op: "trim", clip_id: d.clipId, source_out: +(d.sourceIn + d.pendDur).toFixed(6) });
        else
          postTimelineOp({ op: "set_time", clip_id: d.clipId, duration: +d.pendDur.toFixed(6) });
      } else if (d.mode === "left" && d.pendStart != null) {
        if (d.mediaKind === "video" || d.mediaKind === "audio") {
          const newIn = Math.max(0, d.sourceIn + (d.pendStart - d.origStart));
          if (d.pendStart < d.origStart) {
            // expanding head leftward: move first (frees the right side), then extend the in-point
            postTimelineOp({ op: "move", clip_id: d.clipId, start: +d.pendStart.toFixed(6) })
              .then((r) => { if (r) postTimelineOp({ op: "trim", clip_id: d.clipId, source_in: +newIn.toFixed(6) }); });
          } else {
            // shrinking head rightward: trim in place first, then slide into the freed space
            postTimelineOp({ op: "trim", clip_id: d.clipId, source_in: +newIn.toFixed(6) })
              .then((r) => { if (r) postTimelineOp({ op: "move", clip_id: d.clipId, start: +d.pendStart.toFixed(6) }); });
          }
        } else {
          postTimelineOp({ op: "set_time", clip_id: d.clipId, start: +d.pendStart.toFixed(6), duration: +d.pendDur.toFixed(6) });
        }
      } else {
        renderProjectTimeline(state.projectTimeline);   // plain click: re-sync layout
      }
    };
    content.addEventListener("pointerup", finish);
    content.addEventListener("pointercancel", finish);
  }

  function setupTimelineKeys() {
    document.addEventListener("keydown", (ev) => {
      const t = ev.target;
      if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.isContentEditable)) return;
      if (ev.key === "Delete" || ev.key === "Backspace") { ev.preventDefault(); deleteSelected(); return; }
      if (ev.key === "s" || ev.key === "S") { splitSelected(); return; }
      if (ev.key === "m" || ev.key === "M") { addMarker(TL.playhead); return; }
      if ((ev.metaKey || ev.ctrlKey) && (ev.key === "z" || ev.key === "Z")) { ev.preventDefault(); if (editingEnabled()) postTimelineOp({ op: "undo", steps: 1 }); return; }
      if (ev.key === "+" || ev.key === "=") { setPps(TL.pps * 1.25); return; }
      if (ev.key === "-" || ev.key === "_") { setPps(TL.pps / 1.25); return; }
    });
  }

  function setupTimelineDirectEdit() {
    buildTimelineShell();   // builds the panel DOM + wires every interaction
  }

  // Snap a timeline-second to nearby clip edges / playhead / markers (≈8px),
  // else to a 0.5s grid; clamp >= 0.
  function snapSeconds(sec, d) {
    sec = Math.max(0, sec);
    if (!TL.snap) return sec;
    const tol = pxToTime(8);
    let best = sec, bestDist = tol;
    const cand = [TL.playhead, ...TL.markers.map((m) => m.time)];
    document.querySelectorAll("#ptl-content .ptl-clip").forEach((el) => {
      if (d && el.dataset.clipId === d.clipId) return;
      const s = parseFloat(el.dataset.start) || 0;
      cand.push(s, s + (parseFloat(el.dataset.duration) || 0));
    });
    for (const c of cand) { const dist = Math.abs(sec - c); if (dist < bestDist) { best = c; bestDist = dist; } }
    if (bestDist === tol) best = Math.round(sec * 2) / 2;
    return Math.max(0, best);
  }

  // ── API calls ───────────────────────────────────────────────────────

  function isUserFacingProject(project) {
    const id = String(project?.project_id || "").trim();
    const name = String(project?.name || "").trim();
    const sessions = Array.isArray(project?.sessions) ? project.sessions : [];
    // Sessions created before the Project feature received an automatic
    // project-* / v3-* production container. They remain recoverable as Chats,
    // but they are not user-created Projects and must not inflate this list.
    if (!id || !name || name === id) return false;
    // Packaging acceptance created two empty local sentinels. Keep their data
    // recoverable on disk, but do not present test residue as user work.
    if (name === "DMG Project QA" && !project.source_root && !sessions.length) return false;
    return true;
  }

  async function fetchProjects() {
    const response = await apiFetch("/projects");
    if (!response.ok) throw new Error(`GET /projects failed: ${response.status}`);
    const payload = await response.json();
    return Array.isArray(payload.projects) ? payload.projects.filter(isUserFacingProject) : [];
  }

  const projectSidebarState = {
    collapsed: new Set(),
    expandedSessions: new Set(),
    refreshToken: 0,
    liveSessions: new Map(),
    projectNames: new Map(),
    knownActivity: new Set(),
  };

  function toggleProjectGroup(button) {
    const projectId = button?.dataset.projectToggle;
    if (!projectId) return;
    if (projectSidebarState.collapsed.has(projectId)) projectSidebarState.collapsed.delete(projectId);
    else projectSidebarState.collapsed.add(projectId);
    const collapsed = projectSidebarState.collapsed.has(projectId);
    button.closest(".project-tree-group")?.classList.toggle("is-collapsed", collapsed);
    button.setAttribute("aria-expanded", String(!collapsed));
  }

  // The sidebar body persists while its rows are re-rendered. Delegate this
  // action once so a touch cannot land on a row whose short-lived listener was
  // replaced by an asynchronous sidebar refresh.
  function bindProjectSidebarInteractions() {
    const body = els.projectSidebarBody;
    if (!body || body.dataset.projectInteractionsBound === "true") return;
    body.dataset.projectInteractionsBound = "true";
    body.addEventListener("click", (event) => {
      const button = event.target.closest("[data-project-toggle]");
      if (button && body.contains(button)) toggleProjectGroup(button);
    });
  }

  function projectNamesFrom(projects) {
    const names = new Map();
    for (const project of projects || []) {
      const name = String(project.name || "").trim();
      if (!name) continue;
      const ids = [project.project_id, ...(project.sessions || []).map((session) => session.project_id)];
      ids.map((id) => String(id || "")).filter(Boolean).forEach((id) => names.set(id, name));
    }
    return names;
  }

  function projectSessionTitle(session, snapshot) {
    if (snapshot?.title && snapshot.title !== "Gemia Session") return snapshot.title;
    const id = String(session?.session_id || "");
    return id ? `会话 ${id.slice(-6)}` : "未命名会话";
  }

  function projectSessionRowHtml({
    sessionId = "",
    snapshotId = "",
    title,
    projectId = "",
    runId = "",
    active = false,
    pinned = false,
    hasHistory = false,
    canReceiveHandoff = false,
  }) {
    const runtimeAttrs = sessionId
      ? ` data-runtime-session-id="${escapeHTML(sessionId)}"`
      : "";
    const navigationAttrs = projectId
      ? ` data-project-session-id="${escapeHTML(sessionId)}" data-project-id="${escapeHTML(projectId)}" data-run-id="${escapeHTML(runId)}"${snapshotId ? ` data-snapshot-id="${escapeHTML(snapshotId)}"` : ""}`
      : ` data-chat-snapshot-id="${escapeHTML(snapshotId)}"`;
    const menu = sessionId ? `
      <button type="button" class="project-tree-session-more" data-session-menu-toggle="${escapeHTML(sessionId)}" aria-label="${escapeHTML(title)}的更多操作" aria-haspopup="menu" aria-expanded="false">
        <svg viewBox="0 0 24 24" aria-hidden="true"><use href="#i-more"/></svg>
      </button>
      <div class="project-tree-session-menu" role="menu" hidden>
        <button type="button" role="menuitem" data-session-action="pin" data-session-id="${escapeHTML(sessionId)}" data-session-pinned="${pinned ? "1" : "0"}">
          <svg viewBox="0 0 24 24" aria-hidden="true"><use href="#i-pin"/></svg><span>${pinned ? "取消固定" : "固定会话"}</span>
        </button>
        ${canReceiveHandoff ? `<button type="button" role="menuitem" data-session-action="handoff" data-session-id="${escapeHTML(sessionId)}">
          <svg viewBox="0 0 24 24" aria-hidden="true"><use href="#i-send"/></svg><span>交接当前成果到此会话</span>
        </button>` : ""}
        <button type="button" role="menuitem" class="is-danger" data-session-action="delete" data-session-id="${escapeHTML(sessionId)}">
          <svg viewBox="0 0 24 24" aria-hidden="true"><use href="#i-trash"/></svg><span>删除会话</span>
        </button>
      </div>` : "";
    return `<div class="project-tree-session-row${pinned ? " is-pinned" : ""}">
      <button type="button" class="project-tree-session${active ? " is-active" : ""}"${navigationAttrs}${runtimeAttrs} data-session-has-history="${hasHistory ? "1" : "0"}" data-session-title="${escapeHTML(title)}"${active ? ' aria-current="true"' : ""} title="${escapeHTML(title)}">
        <span class="project-tree-session-title">${escapeHTML(title)}</span>
        <span class="project-tree-session-dot" aria-hidden="true"></span>
        <svg class="project-tree-session-pin" viewBox="0 0 24 24" aria-label="已固定"><use href="#i-pin"/></svg>
      </button>
      ${menu}
    </div>`;
  }

  function syncProjectSidebarSelection() {
    if (!els.projectSidebarBody) return;
    els.projectSidebarBody.querySelectorAll("[data-project-session-id], [data-chat-snapshot-id]").forEach((row) => {
      const selected = row.dataset.projectSessionId
        ? !!state.sessionId && row.dataset.projectSessionId === state.sessionId
        : !!state.activeHistoryId && row.dataset.chatSnapshotId === state.activeHistoryId;
      row.classList.toggle("is-active", selected);
      if (selected) row.setAttribute("aria-current", "true");
      else row.removeAttribute("aria-current");
    });
  }

  function sessionHasRunningWork(session) {
    return !!(
      session?.turn_in_progress
      || (Array.isArray(session?.pending_jobs) && session.pending_jobs.length)
    );
  }

  function applyProjectSessionIndicators() {
    const body = els.projectSidebarBody;
    if (!body) return;
    body.querySelectorAll("[data-runtime-session-id]").forEach((row) => {
      const sessionId = row.dataset.runtimeSessionId;
      if (!sessionId) return;
      const live = projectSidebarState.liveSessions.get(sessionId);
      const running = sessionHasRunningWork(live);
      if (running || row.dataset.sessionHasHistory === "1") {
        projectSidebarState.knownActivity.add(sessionId);
      }
      const complete = !running && projectSidebarState.knownActivity.has(sessionId);
      row.classList.toggle("is-running", running);
      row.classList.toggle("is-complete", complete);
      const status = running ? "执行中" : complete ? "已完成" : "";
      const title = row.dataset.sessionTitle || row.title || "会话";
      row.title = status ? `${title} · ${status}` : title;
      row.setAttribute("aria-label", status ? `${title}，${status}` : title);
    });
  }

  async function refreshProjectSessionIndicators() {
    if (!els.projectSidebarBody || document.visibilityState === "hidden") return;
    if (projectSidebarState.indicatorRefreshInFlight) return;
    projectSidebarState.indicatorRefreshInFlight = true;
    try {
      const response = await apiFetch("/sessions?compact=1");
      if (!response.ok) return;
      const payload = await response.json();
      const sessions = Array.isArray(payload.sessions) ? payload.sessions : [];
      projectSidebarState.liveSessions = new Map(
        sessions.map((session) => [String(session.session_id || ""), session]),
      );
      applyProjectSessionIndicators();
    } catch {}
    finally { projectSidebarState.indicatorRefreshInFlight = false; }
  }

  async function renderProjectSidebar() {
    const body = els.projectSidebarBody;
    if (!body || isCliPreview) return;
    const refreshToken = ++projectSidebarState.refreshToken;
    let projects = [];
    let snapshots = [];
    try {
      const [projectResult, historyResult, runtimeResult] = await Promise.allSettled([
        fetchProjects(),
        apiFetch("/session-history/list?limit=100").then(async (response) => {
          if (!response.ok) throw new Error(`history failed: ${response.status}`);
          const payload = await response.json();
          return Array.isArray(payload.sessions) ? payload.sessions : [];
        }),
        apiFetch("/sessions?compact=1").then(async (response) => {
          if (!response.ok) throw new Error(`sessions failed: ${response.status}`);
          const payload = await response.json();
          return Array.isArray(payload.sessions) ? payload.sessions : [];
        }),
      ]);
      if (refreshToken !== projectSidebarState.refreshToken) return;
      if (projectResult.status === "fulfilled") projects = projectResult.value;
      if (historyResult.status === "fulfilled") snapshots = historyResult.value;
      if (runtimeResult.status === "fulfilled") {
        projectSidebarState.liveSessions = new Map(
          runtimeResult.value.map((session) => [String(session.session_id || ""), session]),
        );
      }
      if (projectResult.status === "rejected" && historyResult.status === "rejected") {
        throw projectResult.reason;
      }
    } catch {
      body.innerHTML = `<p class="project-sidebar-placeholder">Projects 暂时无法载入</p>`;
      return;
    }
    projectSidebarState.projectNames = projectNamesFrom(projects);

    const snapshotsBySession = new Map();
    snapshots.forEach((snapshot) => {
      const sessionId = String(snapshot.v3_session_id || "");
      if (sessionId && !snapshotsBySession.has(sessionId)) snapshotsBySession.set(sessionId, snapshot);
    });

    const projectHtml = projects.map((project) => {
      const projectId = String(project.project_id || "");
      const collapsed = projectSidebarState.collapsed.has(projectId);
      const sessions = Array.isArray(project.sessions) ? project.sessions : [];
      const showAll = projectSidebarState.expandedSessions.has(projectId);
      const visibleSessions = showAll ? sessions : sessions.slice(0, 5);
      const sessionRows = visibleSessions.map((session) => {
        const sessionId = String(session.session_id || "");
        const snapshot = snapshotsBySession.get(sessionId);
        const active = sessionId === state.sessionId;
        const title = projectSessionTitle(session, snapshot);
        return projectSessionRowHtml({
          sessionId,
          snapshotId: snapshot?.id || "",
          title,
          projectId: session.project_id || projectId,
          runId: session.run_id || "",
          active,
          pinned: !!session.pinned,
          hasHistory: !!snapshot?.message_count,
          canReceiveHandoff: !!state.sessionId && !!state.projectId && sessionId !== state.sessionId,
        });
      }).join("");
      const more = sessions.length > 5 && !showAll
        ? `<button type="button" class="project-tree-show-more" data-project-show-more="${escapeHTML(projectId)}">显示更多</button>`
        : "";
      return `<section class="project-tree-group${collapsed ? " is-collapsed" : ""}" data-project-group="${escapeHTML(projectId)}">
        <button type="button" class="project-tree-head" data-project-toggle="${escapeHTML(projectId)}" aria-expanded="${String(!collapsed)}" title="${escapeHTML(project.source_root || project.name || projectId)}">
          <svg viewBox="0 0 24 24" aria-hidden="true"><use href="#i-folder"/></svg>
          <span class="project-tree-name">${escapeHTML(project.name || projectId)}</span>
          <svg class="project-tree-chevron" viewBox="0 0 24 24" aria-hidden="true"><use href="#i-chevron-d"/></svg>
        </button>
        <div class="project-tree-children">
          ${sessionRows || `<p class="project-tree-empty">还没有会话</p>`}
          ${more}
        </div>
      </section>`;
    }).join("");

    const visibleProjectIds = new Set(projects.map((project) => String(project.project_id || "")));
    // A forked production is rendered under its source Project, while its
    // durable snapshot keeps the fork's own project_id. Membership therefore
    // has to follow the session id actually rendered above; filtering only by
    // the source project id duplicates the same conversation under Chats.
    const projectSessionIds = new Set(
      projects.flatMap((project) => (
        Array.isArray(project.sessions) ? project.sessions : []
      )).map((session) => String(session.session_id || "")).filter(Boolean),
    );
    const unassigned = snapshots.filter((snapshot) => {
      const sessionId = String(snapshot.v3_session_id || "");
      return !visibleProjectIds.has(String(snapshot.project_id || ""))
        && !projectSessionIds.has(sessionId);
    });
    const unassignedRows = unassigned.slice(0, 8).map((snapshot) => {
      const active = snapshot.v3_session_id && snapshot.v3_session_id === state.sessionId;
      const title = snapshot.title && snapshot.title !== "Gemia Session" ? snapshot.title : "未命名会话";
      return projectSessionRowHtml({
        sessionId: snapshot.v3_session_id || "",
        snapshotId: snapshot.id,
        title,
        active,
        pinned: !!snapshot.pinned,
        hasHistory: !!snapshot.message_count,
      });
    }).join("");

    body.innerHTML = `${projectHtml || `<p class="project-tree-empty">还没有 Project</p>`}
      <section class="project-tree-chats">
        <span class="project-tree-section-title">Chats</span>
        ${unassignedRows || `<p class="project-tree-empty">还没有独立会话</p>`}
      </section>`;
    applyProjectSessionIndicators();

    body.querySelectorAll("[data-project-show-more]").forEach((button) => {
      button.addEventListener("click", () => {
        projectSidebarState.expandedSessions.add(button.dataset.projectShowMore);
        renderProjectSidebar();
      });
    });
    body.querySelectorAll("[data-session-menu-toggle]").forEach((button) => {
      button.addEventListener("click", (event) => {
        event.stopPropagation();
        const row = button.closest(".project-tree-session-row");
        const menu = row?.querySelector(".project-tree-session-menu");
        if (!menu) return;
        body.querySelectorAll(".project-tree-session-row.is-menu-open").forEach((openRow) => {
          if (openRow === row) return;
          openRow.classList.remove("is-menu-open");
          openRow.querySelector(".project-tree-session-menu")?.setAttribute("hidden", "");
          openRow.querySelector("[data-session-menu-toggle]")?.setAttribute("aria-expanded", "false");
        });
        const opening = menu.hidden;
        menu.hidden = !opening;
        row.classList.toggle("is-menu-open", opening);
        button.setAttribute("aria-expanded", String(opening));
        if (opening) {
          setTimeout(() => document.addEventListener("click", () => {
            menu.hidden = true;
            row.classList.remove("is-menu-open");
            button.setAttribute("aria-expanded", "false");
          }, { once: true }), 0);
        }
      });
      button.addEventListener("keydown", (event) => {
        if (event.key !== "Escape") return;
        const row = button.closest(".project-tree-session-row");
        const menu = row?.querySelector(".project-tree-session-menu");
        if (menu) menu.hidden = true;
        row?.classList.remove("is-menu-open");
        button.setAttribute("aria-expanded", "false");
        button.focus();
      });
    });
    body.querySelectorAll("[data-session-action]").forEach((button) => {
      button.addEventListener("click", async (event) => {
        event.stopPropagation();
        const sessionId = button.dataset.sessionId;
        if (!sessionId) return;
        const row = button.closest(".project-tree-session-row");
        const sessionButton = row?.querySelector(".project-tree-session");
        const title = sessionButton?.dataset.sessionTitle || "此会话";
        try {
          if (button.dataset.sessionAction === "pin") {
            const pinned = button.dataset.sessionPinned !== "1";
            const response = await apiFetch(`/sessions/${encodeURIComponent(sessionId)}/pin`, {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ pinned }),
            });
            const payload = await response.json().catch(() => ({}));
            if (!response.ok) throw new Error(payload.error || `固定会话失败 (${response.status})`);
            await renderProjectSidebar();
            return;
          }
          if (button.dataset.sessionAction === "handoff") {
            const sourceSessionId = state.sessionId;
            if (!sourceSessionId || sourceSessionId === sessionId) return;
            if (!window.confirm(`将当前会话已完成的素材和成片交接给“${title}”？\n\n时间轴、聊天和运行上下文不会共享。`)) return;
            const response = await apiFetch(`/sessions/${encodeURIComponent(sourceSessionId)}/handoff`, {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ target_session_id: sessionId }),
            });
            const payload = await response.json().catch(() => ({}));
            if (!response.ok) throw new Error(payload.error || `交接失败 (${response.status})`);
            const sent = Array.isArray(payload.transferred) ? payload.transferred.length : 0;
            const already = Array.isArray(payload.already_available) ? payload.already_available.length : 0;
            state.errors.push(`已交接 ${sent} 个成果到“${title}”${already ? `；${already} 个已有` : ""}。`);
            render();
            return;
          }
          if (button.dataset.sessionAction === "delete") {
            if (!window.confirm(`删除“${title}”？`)) return;
            const wasCurrent = sessionId === state.sessionId;
            const projectId = sessionButton?.dataset.projectId || "";
            const response = await apiFetch(`/sessions/${encodeURIComponent(sessionId)}`, {
              method: "DELETE",
            });
            const payload = await response.json().catch(() => ({}));
            if (!response.ok) throw new Error(payload.error || `删除会话失败 (${response.status})`);
            if (wasCurrent) {
              await createSession(projectId ? { fork_from_project_id: projectId } : {});
            }
            await renderProjectSidebar();
          }
        } catch (error) {
          state.errors.push(error.message);
          render();
        }
      });
    });
    body.querySelectorAll("[data-project-session-id]").forEach((button) => {
      button.addEventListener("click", async () => {
        if (button.dataset.projectSessionId === state.sessionId) return;
        try {
          if (button.dataset.snapshotId) await loadHistorySession(button.dataset.snapshotId);
          else await resumeSession(button.dataset.projectSessionId, {
            project_id: button.dataset.projectId,
            run_id: button.dataset.runId,
          });
          await renderProjectSidebar();
        } catch (error) {
          state.errors.push(`session restore failed: ${error.message}`);
          render();
        }
      });
    });
    body.querySelectorAll("[data-chat-snapshot-id]").forEach((button) => {
      button.addEventListener("click", async () => {
        if (await loadHistorySession(button.dataset.chatSnapshotId)) await renderProjectSidebar();
      });
    });
  }

  async function syncCurrentProject() {
    if (!state.projectId) return;
    try {
      const projects = await fetchProjects();
      const project = projects.find((item) => (
        item.project_id === state.projectId
        || (item.sessions || []).some((session) => session.project_id === state.projectId)
      ));
      state.projectName = project?.name || state.projectId;
      state.projectSourceRoot = project?.source_root || null;
    } catch {
      state.projectName = state.projectId;
      state.projectSourceRoot = null;
    }
  }

  function ensureProjectModal() {
    let modal = document.getElementById("project-modal");
    if (modal) return modal;
    modal = document.createElement("div");
    modal.id = "project-modal";
    modal.className = "project-modal";
    modal.hidden = true;
    modal.innerHTML = `
      <section class="project-dialog" role="dialog" aria-modal="true" aria-labelledby="project-dialog-title">
        <div class="project-dialog-head">
          <h2 id="project-dialog-title">Projects</h2>
          <button type="button" class="icon-btn" data-project-close aria-label="关闭"><svg viewBox="0 0 24 24"><use href="#i-close"/></svg></button>
        </div>
        <div class="project-list" data-project-list><p class="project-empty">加载中…</p></div>
        <div class="project-dialog-actions">
          <button type="button" data-project-undo>撤销文件操作</button>
          <button type="button" data-project-redo>重做</button>
          <span style="flex:1"></span>
          <button type="button" data-project-open>新建 Project</button>
        </div>
      </section>`;
    document.body.appendChild(modal);
    modal.addEventListener("click", (event) => {
      if (event.target === modal || event.target.closest("[data-project-close]")) modal.hidden = true;
    });
    return modal;
  }

  async function projectHistoryAction(action) {
    if (!state.projectId) return;
    const response = await apiFetch(`/projects/${encodeURIComponent(state.projectId)}/${action}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });
    if (!response.ok) {
      const payload = await response.json().catch(() => ({}));
      throw new Error(payload.error || `${action} failed: ${response.status}`);
    }
    filesState = null;
    refreshPanel("files");
    await openProjectModal();
  }

  async function chooseProjectFolder() {
    if (typeof window.lumeriDesktop?.pickProjectFolder === "function") {
      const picked = await window.lumeriDesktop.pickProjectFolder();
      return typeof picked === "string" ? picked : (picked?.path || "");
    }
    try {
      const response = await apiFetch("/projects/pick-folder", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}",
      });
      if (response.ok) return (await response.json()).path || "";
    } catch {}
    return window.prompt("输入要作为 Lumeri Project 打开的本机文件夹绝对路径：", "") || "";
  }

  async function createProject({ name, sourceRoot = "" }) {
    const response = await apiFetch("/projects", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name: String(name || "").trim() || "未命名 Project",
        ...(String(sourceRoot || "").trim() ? { source_root: String(sourceRoot).trim() } : {}),
      }),
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.error || `create project failed: ${response.status}`);
    ensureProjectModal().hidden = true;
    const createModal = document.getElementById("project-create-modal");
    if (createModal) createModal.hidden = true;
    await createSession({ project_id: payload.project_id });
  }

  function ensureCreateProjectModal() {
    let modal = document.getElementById("project-create-modal");
    if (modal) return modal;
    modal = document.createElement("div");
    modal.id = "project-create-modal";
    modal.className = "project-modal";
    modal.hidden = true;
    modal.innerHTML = `
      <section class="project-dialog project-create-dialog" role="dialog" aria-modal="true" aria-labelledby="project-create-title">
        <div class="project-dialog-head">
          <h2 id="project-create-title">新建 Project</h2>
          <button type="button" class="icon-btn" data-project-create-close aria-label="关闭"><svg viewBox="0 0 24 24"><use href="#i-close"/></svg></button>
        </div>
        <div class="project-create-body">
          <label class="project-create-field">
            <span>Project 名称</span>
            <input type="text" data-project-create-name maxlength="80" placeholder="未命名 Project" autocomplete="off">
          </label>
          <div class="project-folder-choice">
            <div>
              <strong>本机目录</strong>
              <p data-project-create-path>未选择。产物将保存在 Lumeri 的 Project 目录中。</p>
            </div>
            <button type="button" data-project-create-pick>选择目录…</button>
          </div>
          <button type="button" class="project-folder-clear" data-project-create-clear hidden>不使用本机目录</button>
          <p class="project-create-note">目录是可选的。无论是否绑定目录，Project 内的会话都会共享剪辑产物、记忆和日志。</p>
        </div>
        <div class="project-dialog-actions">
          <button type="button" data-project-create-cancel>取消</button>
          <button type="button" class="project-create-submit" data-project-create-submit>创建 Project</button>
        </div>
      </section>`;
    document.body.appendChild(modal);
    const close = () => { modal.hidden = true; };
    modal.addEventListener("click", (event) => { if (event.target === modal) close(); });
    modal.querySelector("[data-project-create-close]").onclick = close;
    modal.querySelector("[data-project-create-cancel]").onclick = close;
    modal.querySelector("[data-project-create-pick]").onclick = async () => {
      const picked = (await chooseProjectFolder()).trim();
      if (!picked) return;
      modal.dataset.sourceRoot = picked;
      modal.querySelector("[data-project-create-path]").textContent = picked;
      modal.querySelector("[data-project-create-clear]").hidden = false;
    };
    modal.querySelector("[data-project-create-clear]").onclick = () => {
      modal.dataset.sourceRoot = "";
      modal.querySelector("[data-project-create-path]").textContent = "未选择。产物将保存在 Lumeri 的 Project 目录中。";
      modal.querySelector("[data-project-create-clear]").hidden = true;
    };
    modal.querySelector("[data-project-create-submit]").onclick = async () => {
      const submit = modal.querySelector("[data-project-create-submit]");
      if (submit.disabled) return;
      submit.disabled = true;
      try {
        await createProject({
          name: modal.querySelector("[data-project-create-name]").value,
          sourceRoot: modal.dataset.sourceRoot || "",
        });
      } catch (error) {
        state.errors.push(error.message);
        render();
      } finally {
        submit.disabled = false;
      }
    };
    return modal;
  }

  function openCreateProjectDialog() {
    const modal = ensureCreateProjectModal();
    modal.dataset.sourceRoot = "";
    modal.querySelector("[data-project-create-name]").value = "";
    modal.querySelector("[data-project-create-path]").textContent = "未选择。产物将保存在 Lumeri 的 Project 目录中。";
    modal.querySelector("[data-project-create-clear]").hidden = true;
    modal.hidden = false;
    modal.querySelector("[data-project-create-name]").focus();
  }

  async function openProjectModal() {
    const modal = ensureProjectModal();
    const list = modal.querySelector("[data-project-list]");
    modal.hidden = false;
    list.innerHTML = `<p class="project-empty">加载中…</p>`;
    const projects = await fetchProjects();
    list.innerHTML = projects.length ? projects.map((project) => {
      const sessions = Array.isArray(project.sessions) ? project.sessions.length : (project.session_ids || []).length;
      const active = project.project_id === state.projectId;
      return `<button type="button" class="project-row${active ? " is-active" : ""}" data-project-id="${escapeHTML(project.project_id)}">
        <span class="project-row-name">${escapeHTML(project.name || project.project_id)}</span>
        <span class="project-row-path">${escapeHTML(project.source_root || "仅使用 Lumeri Project 目录")}</span>
        <span class="project-row-count">${sessions} 个会话</span>
        <span class="project-row-context">记忆 ${Number(project.context?.memory_entries || 0)} · 日志 ${Number(project.context?.log_entries || 0)}</span>
      </button>`;
    }).join("") : `<p class="project-empty">还没有 Project。打开一个本机文件夹开始。</p>`;
    list.querySelectorAll("[data-project-id]").forEach((row) => {
      row.addEventListener("click", async () => {
        modal.hidden = true;
        await createSession({ project_id: row.dataset.projectId });
      });
    });
    modal.querySelector("[data-project-open]").onclick = () => {
      modal.hidden = true;
      openCreateProjectDialog();
    };
    const currentProject = projects.find((project) => project.project_id === state.projectId);
    modal.querySelector("[data-project-undo]").disabled = !currentProject?.file_history?.can_undo;
    modal.querySelector("[data-project-redo]").disabled = !currentProject?.file_history?.can_redo;
    modal.querySelector("[data-project-undo]").onclick = () => {
      projectHistoryAction("undo").catch((error) => { state.errors.push(error.message); render(); });
    };
    modal.querySelector("[data-project-redo]").onclick = () => {
      projectHistoryAction("redo").catch((error) => { state.errors.push(error.message); render(); });
    };
  }

  function resetRuntimeView() {
    // Reset per-session timeline state so no project pixels or media handles
    // leak across a real history switch.
    unloadDeliveryReviewMaster();
    if (state.roughcutPollTimer) {
      clearTimeout(state.roughcutPollTimer);
      state.roughcutPollTimer = null;
    }
    TL.extraTracks = [];
    TL.playhead = 0;
    TL.model = null;
    TL.markers = [];
    TL.frames.clear();
    TL.frameFail.clear();
    TL.frameRigs.forEach((rig) => {
      try {
        if (rig.video) { rig.video.removeAttribute("src"); rig.video.load(); }
      } catch {}
    });
    TL.frameRigs.clear();
    TL.wave.clear();
    TL.waveBusy.clear();
    TL._renderedSeq = null;
    // Clear rendered timeline pixels immediately. The next session's
    // authoritative timeline arrives asynchronously.
    if (tlHeaders()) tlHeaders().innerHTML = "";
    if (tlContent()) tlContent().innerHTML = "";
    state.selectedClipId = null;
    state.turns = [];
    state.currentTurn = null;
    state.assets = [];
    state.errors = [];
    state.turnInProgress = false;
    state._followChatBottom = true;
    state.stopPending = false;
    state.lastEventId = null;
    state.projectTimeline = null;
    state.mediaLibrary = [];
    state.sessionNonMediaAssets = [];
    state.mediaLibraryStatus = "idle";
    state.mediaAnnotations = new Map();
    state.roughcutManifests = new Map();
    state.roughcutJob = null;
    state.libraryFocusName = "";
    state.planMode = false;
    state.planReady = false;
    state.sessionTitle = null;
    state.activeHistoryId = null;
    state.userMessageCount = 0;
    state.projectId = null;
    state.projectName = null;
    state.projectSourceRoot = null;
    state.runId = null;
    state.projectRevision = 0;
    state.productionRevision = 0;
    state.productionState = null;
    state.productionOutcome = null;
    state.productionBudget = null;
    state.productionBlockers = [];
    state.productionDelivery = null;
    state.productionAcceptance = null;
    state.productionAssetMix = null;
    state.chatOnly = false;
    state.backgroundTasks = new Map();
    // The Library is also rendered outside the main chat render path. Blank
    // both copies at the session boundary so the old session cannot remain
    // visible while the new session snapshot/library request is in flight.
    renderMediaLibrary();
    const libraryPanelBody = panelBodyFor("library");
    if (libraryPanelBody) {
      libraryPanelBody.innerHTML = `${librarySectionsHtml()}<p class="placeholder">加载中…</p>`;
    }
  }

  let runtimeActivationSeq = 0;
  const sessionViewCache = new Map();

  function cacheCurrentSessionView() {
    if (!state.sessionId) return;
    sessionViewCache.set(state.sessionId, {
      turns: state.turns,
      currentTurn: state.currentTurn,
      sessionTitle: state.sessionTitle,
      userMessageCount: state.userMessageCount,
      lastEventId: state.lastEventId,
      pendingAsk: state.pendingAsk,
      backgroundTasks: state.backgroundTasks,
    });
  }

  function restoreCachedSessionView(sessionId) {
    const cached = sessionViewCache.get(sessionId);
    if (!cached) return false;
    state.turns = cached.turns;
    state.currentTurn = cached.currentTurn;
    state.sessionTitle = cached.sessionTitle;
    state.userMessageCount = cached.userMessageCount;
    state.lastEventId = cached.lastEventId;
    state.pendingAsk = cached.pendingAsk;
    state.backgroundTasks = cached.backgroundTasks;
    return true;
  }

  async function detachRuntime() {
    cacheCurrentSessionView();
    clearReconnectTimer();
    stopTimelinePoll();
    if (state.eventSource) {
      state.eventSource.close();
      state.eventSource = null;
    }
  }

  async function activateSessionPayload(data, activationSeq, hydrateHistory = null) {
    if (!data?.session_id) throw new Error("session response did not include session_id");
    if (activationSeq !== runtimeActivationSeq) return null;
    resetRuntimeView();
    state.sessionId = data.session_id;
    // Restore the saved chat before reconnecting SSE. Otherwise replayed
    // background events can arrive first and then be erased by a stale
    // history snapshot, making a still-running session look reset.
    if (hydrateHistory) hydrateHistory();
    applyProductionSnapshot(data);
    await syncCurrentProject();
    if (activationSeq !== runtimeActivationSeq || state.sessionId !== data.session_id) return null;
    loadMarkers();
    // POST /resume and POST /sessions already return the authoritative full
    // session snapshot.  Reuse it instead of immediately issuing a duplicate
    // GET /sessions/{id}, which repeated all backend snapshot work.
    await refreshSessionState(data);
    if (activationSeq !== runtimeActivationSeq || state.sessionId !== data.session_id) return null;
    connectSse(state.sessionId);
    startTimelinePoll();
    fetchMediaLibrary().catch(() => {});
    render();
    renderProjectSidebar();
    return data;
  }

  async function createSession(options = {}) {
    const activationSeq = ++runtimeActivationSeq;
    if (state.sessionId) await autoSaveSession();
    // Switching the visible chat only detaches this UI. The old SessionRunner
    // must keep working independently in the server.
    await detachRuntime();
    setConnPill("opening…", "");
    const request = { method: "POST" };
    if (options.project_id || options.run_id || options.fork_from_project_id) {
      request.headers = { "Content-Type": "application/json" };
      request.body = JSON.stringify({
        ...(options.project_id ? { project_id: options.project_id } : {}),
        ...(options.run_id ? { run_id: options.run_id } : {}),
        ...(options.fork_from_project_id ? { fork_from_project_id: options.fork_from_project_id } : {}),
      });
    }
    const r = await apiFetch("/sessions", request);
    if (!r.ok) throw new Error(`POST /sessions failed: ${r.status}`);
    const data = await r.json();
    const activated = await activateSessionPayload(data, activationSeq);
    if (!activated) return null;
    // Persist the durable project/run reference even before the first chat
    // message or upload. A browser restart must not orphan a real empty
    // project merely because the creator had not typed yet.
    await autoSaveSession();
    return activated;
  }

  async function resumeSession(sessionId, expected = {}) {
    const activationSeq = ++runtimeActivationSeq;
    const expectedProjectId = expected.project_id || null;
    const expectedRunId = expected.run_id || null;
    if (!!expectedProjectId !== !!expectedRunId) {
      throw new Error("incomplete durable production reference");
    }
    if (!sessionId) throw new Error("no durable session reference");
    if (state.sessionId) await autoSaveSession();
    await detachRuntime();
    setConnPill("opening…", "");
    const r = await apiFetch(`/sessions/${encodeURIComponent(sessionId)}/resume`, { method: "POST" });
    if (!r.ok) throw new Error(`resume session failed: ${r.status}`);
    const data = await r.json();
    if (expectedProjectId && (
      data.project_id !== expectedProjectId || data.run_id !== expectedRunId
    )) {
      throw new Error("resumed production identity did not match history");
    }
    return activateSessionPayload(data, activationSeq, expected.hydrateHistory || null);
  }

  // CLI preview is the canonical Video workspace attached to the session the
  // terminal already owns. It must never create, replace, or close that
  // session; only the chat surfaces are removed by the .cli-preview CSS mode.
  async function attachSession(sessionId) {
    const activationSeq = ++runtimeActivationSeq;
    await detachRuntime();
    setConnPill("opening…", "");
    resetRuntimeView();
    state.sessionId = sessionId;
    state.lastEventId = null;
    loadMarkers();
    TL._renderedSeq = null;
    await refreshSessionState();
    if (activationSeq !== runtimeActivationSeq || state.sessionId !== sessionId) return null;
    connectSse(sessionId);
    startTimelinePoll();
    fetchMediaLibrary().catch(() => {});
    const emptyHint = document.querySelector("#empty-state .empty-sub");
    if (emptyHint) emptyHint.textContent = "在终端描述你想要的视频";
    render();
  }

  // ── session persistence (auto-save + auto-title) ────────────────────

  function _collectSessionMessages() {
    const msgs = [];
    for (const turn of state.turns) {
      if (turn.userMessage) msgs.push({ role: "user", content: turn.userMessage, timestamp: turn.startedAt || Date.now() });
      for (const guidance of (turn.guidance || [])) {
        msgs.push({ role: "status", content: guidance, statusType: "guidance", timestamp: Date.now() });
      }
      if (turn.assistantText) msgs.push({ role: "status", content: turn.assistantText, statusType: "succeeded", timestamp: turn.completedAt || Date.now() });
    }
    return msgs;
  }

  async function autoSaveSession({ requireAcknowledgement = false } = {}) {
    if (!state.sessionId) return true;
    const messages = _collectSessionMessages();
    if (!messages.length && !state.projectId) return true;
    const payload = {
      session_id: state.sessionId,
      v3_session_id: state.sessionId,
      project_id: state.projectId,
      run_id: state.runId,
      project_revision: state.projectRevision,
      production_state: state.productionState,
      chat_only: !state.projectId,
      title: state.sessionTitle || undefined,
      messages,
      timeline_markers: TL.markers,
      project_state: null,
      project: null,
    };
    try {
      const response = await apiFetch("/session-history", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (!response.ok) {
        throw new Error(`POST /session-history failed: ${response.status}`);
      }
      return true;
    } catch (error) {
      if (requireAcknowledgement) throw error;
      return false;
    }
  }

  window.__lumeriTesterPrepareLogout = async () => {
    return autoSaveSession({ requireAcknowledgement: true });
  };

  async function retractTurn(turnIdx) {
    // Only the newest settled turn is retractable; expected_message lets the
    // backend refuse if its history and this UI have drifted apart.
    if (turnIdx !== state.turns.length - 1 || state.turnInProgress) return;
    const turn = state.turns[turnIdx];
    if (!turn || !state.sessionId) return;
    let failText = "撤回失败，请稍后重试。";
    try {
      const r = await apiFetch(`/sessions/${encodeURIComponent(state.sessionId)}/retract`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ expected_message: turn.userMessage }),
      });
      if (r.ok) {
        state.turns.pop();
        state.currentTurn = state.turns[state.turns.length - 1] || null;
        state.userMessageCount = Math.max(0, state.userMessageCount - 1);
        // Hand the text back for re-editing, but never clobber a draft.
        if (!els.promptInput.value.trim()) els.promptInput.value = turn.userMessage;
        render();
        autoSaveSession();
        els.promptInput.focus();
        return;
      }
      const err = await r.json().catch(() => null);
      if (err?.error) failText = err.error;
    } catch {}
    turn.banners.push({ kind: "turn_error", text: failText });
    render();
  }

  async function autoGenerateTitle() {
    if (!state.sessionId) return;
    const messages = _collectSessionMessages();
    if (!messages.length) return;
    try {
      const r = await apiFetch(`/sessions/${state.sessionId}/auto_title`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ messages }),
      });
      if (!r.ok) return;
      const data = await r.json();
      if (data.title) {
        state.sessionTitle = data.title;
        els.sessionLabel.textContent = data.title;
        autoSaveSession();
        renderProjectSidebar();
      }
    } catch {}
  }

  async function refreshSessionState(snapshot = null) {
    if (!state.sessionId) return;
    const sessionId = state.sessionId;
    const activationSeq = runtimeActivationSeq;
    let data = snapshot;
    if (!data) {
      const r = await apiFetch(`/sessions/${sessionId}`);
      if (!r.ok) throw new Error(`GET /sessions/${sessionId} failed: ${r.status}`);
      data = await r.json();
    }
    if (state.sessionId !== sessionId || runtimeActivationSeq !== activationSeq) return;
    applyProductionSnapshot(data);
    const finalIds = new Set(state.assets.filter((a) => a.final).map((a) => a.asset_id));
    state.assets = (data.assets || []).map((a) => ({
      asset_id: a.asset_id,
      kind: a.kind || inferKindFromAssetId(a.asset_id),
      summary: a.summary || "",
      source: a.source || "tool",
      source_class: a.source_class || a.origin || null,
      origin: a.origin || null,
      provenance: a.provenance || null,
      final: finalIds.has(a.asset_id),
    }));
    if (data.latest_event_id !== null && data.latest_event_id !== undefined) {
      saveLastEventId(state.sessionId, data.latest_event_id);
    }
    if (typeof data.plan_mode === "boolean") {
      state.planMode = data.plan_mode;
      if (!state.planMode) state.planReady = false;
    }
    if (typeof data.turn_in_progress === "boolean") {
      state.turnInProgress = data.turn_in_progress;
      if (!state.turnInProgress) state.stopPending = false;
    }
    if (Array.isArray(data.tasks)) {
      // Server snapshot is authoritative after SSE ring-buffer gaps, but the
      // REST list omits exit_code/output_tail — preserve what SSE taught us.
      const next = new Map();
      for (const t of data.tasks) {
        if (!t.job_id) continue;
        const prev = state.backgroundTasks.get(t.job_id) || {};
        next.set(t.job_id, {
          ...prev,
          job_id: t.job_id,
          status: t.status || prev.status || "running",
          summary: t.summary ?? prev.summary ?? "",
          elapsed_sec: t.elapsed_sec ?? prev.elapsed_sec ?? null,
          error: t.error ?? prev.error ?? null,
        });
      }
      state.backgroundTasks = next;
      scheduleTasksPanelRefresh();
    }
  }

  async function fetchSessionNonMediaAssets(
    sessionId = state.sessionId,
    activationSeq = runtimeActivationSeq,
  ) {
    if (!sessionId) {
      if (runtimeActivationSeq !== activationSeq) return;
      state.sessionNonMediaAssets = [];
      return;
    }
    const qs = `root=session&path=&session=${encodeURIComponent(sessionId)}`;
    const response = await apiFetch(`/files/list?${qs}`);
    if (state.sessionId !== sessionId || runtimeActivationSeq !== activationSeq) return;
    if (!response.ok) {
      state.sessionNonMediaAssets = [];
      return;
    }
    const data = await response.json();
    if (state.sessionId !== sessionId || runtimeActivationSeq !== activationSeq) return;
    state.sessionNonMediaAssets = (Array.isArray(data.entries) ? data.entries : [])
      .filter((entry) => !entry.is_dir && NON_MEDIA_ASSET_EXTENSIONS.has(fileExtension(entry.name)))
      .map((entry) => {
        const name = String(entry.name || "");
        const fileQs = `root=session&path=${encodeURIComponent(name)}&session=${encodeURIComponent(state.sessionId)}`;
        return {
          asset_id: `workspace:${name}`,
          name,
          media_kind: "file",
          library_category: "non-media",
          file_size_bytes: Number(entry.size || 0),
          preview_src: `/files/get?${fileQs}`,
          workspace_path: name,
        };
      });
  }

  async function fetchMediaLibrary() {
    if (isTesterManagedWorkspace()) {
      state.mediaLibrary = [];
      state.sessionNonMediaAssets = [];
      state.mediaLibraryStatus = "ready";
      render();
      return;
    }
    return fetchLegacyMediaLibrary();
  }

  async function fetchLegacyMediaLibrary() {
    const sessionId = state.sessionId;
    const activationSeq = runtimeActivationSeq;
    state.mediaLibraryStatus = "loading";
    render();
    await fetchSessionNonMediaAssets(sessionId, activationSeq).catch(() => {
      if (state.sessionId === sessionId && runtimeActivationSeq === activationSeq) {
        state.sessionNonMediaAssets = [];
      }
    });
    if (state.sessionId !== sessionId || runtimeActivationSeq !== activationSeq) return;
    try {
      const r = await apiFetch("/media-library/list?limit=100");
      if (state.sessionId !== sessionId || runtimeActivationSeq !== activationSeq) return;
      if (r.status === 401) {
        state.mediaLibrary = [];
        state.mediaLibraryStatus = "signed-out";
        render();
        if (stageTabs.includes("library")) refreshPanel("library");
        return;
      }
      if (!r.ok) throw new Error(`GET /media-library/list failed: ${r.status}`);
      const data = await r.json();
      if (state.sessionId !== sessionId || runtimeActivationSeq !== activationSeq) return;
      state.mediaLibrary = Array.isArray(data.assets) ? data.assets : [];
      state.mediaLibraryStatus = "ready";
      if (stageTabs.includes("library")) refreshPanel("library");
    } catch (err) {
      if (state.sessionId !== sessionId || runtimeActivationSeq !== activationSeq) return;
      state.mediaLibrary = [];
      state.mediaLibraryStatus = "error";
      state.errors.push(`media library failed: ${err.message}`);
    }
    render();
  }

  async function annotateLibraryAsset(assetId) {
    const body = assetId
      ? { asset_ids: [assetId], mode: "quick", language: promptLanguage() }
      : { all: true, kind: "video", mode: "quick", max_assets: 20, language: promptLanguage() };
    const r = await apiFetch("/media-library/annotate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!r.ok) throw new Error(`annotate failed: ${r.status}`);
    await r.json();
    await fetchMediaLibrary();
    if (assetId) await loadMediaAnnotations(assetId);
  }

  async function startRoughcutPreparation(assetId = "") {
    const assetIds = assetId
      ? [assetId]
      : state.mediaLibrary.filter((asset) => ["video", "audio"].includes(asset.media_kind)).map((asset) => asset.asset_id);
    if (!assetIds.length) throw new Error("没有可准备的视频或音频素材");
    const r = await apiFetch("/media-library/prepare", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ asset_ids: assetIds, language: promptLanguage(), create_proxies: true, resume: true, background: true }),
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.error || `prepare failed: ${r.status}`);
    state.roughcutJob = data;
    renderRoughcutJobStatus();
    pollRoughcutJob(data.job_id);
  }

  async function pollRoughcutJob(jobId) {
    if (state.roughcutPollTimer) clearTimeout(state.roughcutPollTimer);
    try {
      const r = await apiFetch(`/media-library/prepare/${encodeURIComponent(jobId)}`);
      const data = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(data.error || `job failed: ${r.status}`);
      state.roughcutJob = data;
      renderRoughcutJobStatus();
      if (["ready", "partial"].includes(data.status)) {
        const ids = (data.result?.results || []).filter((item) => item.status === "ready").map((item) => item.asset_id);
        await Promise.all(ids.map((id) => loadRoughcutManifest(id)));
        await fetchMediaLibrary();
        await refreshPanel("library");
        return;
      }
      if (["error", "interrupted"].includes(data.status)) return;
      state.roughcutPollTimer = setTimeout(() => pollRoughcutJob(jobId), 900);
    } catch (err) {
      state.errors.push(`粗剪准备失败: ${err.message}`);
      render();
    }
  }

  function renderRoughcutJobStatus() {
    if (!els.roughcutJobStatus) return;
    const job = state.roughcutJob;
    els.roughcutJobStatus.hidden = !job;
    if (!job) return;
    const progress = Math.max(0, Math.min(Number(job.progress || 0), 100));
    const terminal = ["ready", "partial", "error", "interrupted"].includes(job.status);
    els.roughcutJobStatus.innerHTML = `<div><span>${escapeHTML(terminal ? (job.message || job.status) : "正在准备素材")}</span><strong>${Math.round(progress)}%</strong></div><i style="width:${progress}%"></i>`;
  }

  async function loadRoughcutManifest(assetId) {
    const r = await apiFetch(`/media-library/${encodeURIComponent(assetId)}/roughcut`);
    if (r.status === 404) return null;
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.error || `roughcut failed: ${r.status}`);
    state.roughcutManifests.set(assetId, data.manifest);
    render();
    return data.manifest;
  }

  async function reviewRoughcut(button) {
    const assetId = button.dataset.roughcutAsset;
    const targetType = button.dataset.roughcutType;
    const targetId = button.dataset.roughcutId;
    const action = button.dataset.roughcutReview;
    const card = button.closest(".library-card");
    const input = targetType === "transcript" ? card?.querySelector(`[data-roughcut-transcript-input="${CSS.escape(targetId)}"]`) : null;
    const r = await apiFetch(`/media-library/${encodeURIComponent(assetId)}/roughcut/review`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ target_type: targetType, target_id: targetId, action, text: input?.value || "" }),
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.error || `review failed: ${r.status}`);
    state.roughcutManifests.set(assetId, data.manifest);
    render();
  }

  async function loadMediaAnnotations(assetId) {
    const r = await apiFetch(`/media-library/${encodeURIComponent(assetId)}/annotations`);
    if (!r.ok) throw new Error(`annotations failed: ${r.status}`);
    const data = await r.json();
    state.mediaAnnotations.set(assetId, Array.isArray(data.annotations) ? data.annotations : []);
    render();
  }

  function promptLanguage() {
    return /[\u4e00-\u9fff]/.test(els.promptInput?.value || "") ? "zh" : "en";
  }

  async function uploadFile(file, retryExpiredSession = true) {
    if (!state.sessionId) throw new Error("no session");
    setUploadStatus(`uploading ${file.name}…`);
    const r = await apiFetch(`/sessions/${state.sessionId}/assets`, {
      method: "POST",
      headers: {
        "X-Filename": encodeURIComponent(file.name),
        "Content-Type": file.type || "application/octet-stream",
      },
      body: file,
    });
    // A restart can leave an already-open tab holding a stale runtime session
    // id. Resume the same durable project/run before retrying; never replace
    // real work with a fresh empty project just to make the upload succeed.
    if (r.status === 404 && retryExpiredSession) {
      setUploadStatus("会话已更新，正在恢复后重新上传…");
      const staleSessionId = state.sessionId;
      const expected = { project_id: state.projectId, run_id: state.runId };
      await resumeSession(staleSessionId, expected);
      return uploadFile(file, false);
    }
    if (!r.ok) {
      setUploadStatus(`upload failed (${r.status})`);
      throw new Error(`upload failed: ${r.status}`);
    }
    const data = await r.json();
    state.assets.push({
      asset_id: data.asset_id,
      kind: file.type?.startsWith("image/")
        ? "image"
        : file.type?.startsWith("audio/")
          ? "audio"
          : "video",
      summary: `uploaded ${data.filename} (${(data.size_bytes / 1024).toFixed(1)} KB)`,
      source: "user",
      source_class: "external",
      final: false,
    });
    setUploadStatus(`uploaded as ${data.asset_id}`);
    fetchMediaLibrary().catch(() => {});
    render();
    return data.asset_id;
  }

  function setUploadStatus(text) {
    state.uploadStatus = text;
    let label = document.querySelector(".upload-status");
    if (!label) {
      label = document.createElement("span");
      label.className = "upload-status";
      // Upload button now lives in the closed "+" menu — anchor the status as a
      // toast on the input shell instead so it stays visible.
      (document.getElementById("input-shell") || document.body).appendChild(label);
    }
    label.textContent = text || "";
  }

  async function submitTurn(message) {
    if (!state.sessionId) throw new Error("no session");
    if (state.recoveringSession) throw new Error("session is recovering");
    const sessionId = state.sessionId;
    const activationSeq = runtimeActivationSeq;
    state.userMessageCount++;
    const turn = newTurn(message);
    state.turns.push(turn);
    state.currentTurn = turn;
    state.turnInProgress = true;
    state.planReady = false;   // a new turn supersedes any pending approval offer
    render();
    // Persist the user-visible checkpoint before the network request. If the
    // daemon restarts mid-turn, history and Agent runtime recover the same
    // newest user message instead of disagreeing about what can be retracted.
    await autoSaveSession();
    if (isLocalWorkspace) {
      try {
        const history = _collectSessionMessages().filter((item) => item.role === "user" || item.statusType === "succeeded");
        const messages = history.map((item) => ({
          role: item.role === "user" ? "user" : "assistant",
          content: item.content,
        }));
        const response = await apiFetch("/local-chat", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ messages }),
        });
        const payload = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(payload.error || `模型请求失败 (${response.status})`);
        turn.assistantText = String(payload.text || "");
        turn.completedAt = Date.now();
        turn.complete = true;
        state.turnInProgress = false;
        await autoSaveSession();
        render();
        return true;
      } catch (error) {
        turn.banners.push({ kind: "turn_error", text: error.message || "模型请求未完成" });
        turn.completedAt = Date.now();
        turn.complete = true;
        state.turnInProgress = false;
        render();
        return false;
      }
    }
    let r;
    let data = {};
    // A project revision conflict means this session's visible snapshot was
    // overtaken before the turn started. No work was claimed server-side, so
    // it is safe to sync once and retry the SAME durable user turn/id instead
    // of completing it as an error and making the creator send it again.
    for (let attempt = 0; attempt < 2; attempt++) {
      r = await apiFetch(`/sessions/${sessionId}/turn`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          message,
          client_turn_id: turn.clientTurnId,
          expected_project_revision: state.projectRevision,
        }),
      });
      data = await r.json().catch(() => ({}));
      if (r.ok || data.code !== "E_REVISION_CONFLICT" || attempt > 0) break;
      if (state.sessionId !== sessionId || runtimeActivationSeq !== activationSeq) return false;
      try {
        if (data.project_revision !== undefined) {
          applyProductionSnapshot(data);
        } else {
          // Rolling-upgrade fallback for an older daemon that does not yet
          // return the authoritative admission revision in its 409 response.
          await refreshSessionState();
        }
        if (state.sessionId !== sessionId || runtimeActivationSeq !== activationSeq) return false;
        state.turnInProgress = true;
        await autoSaveSession();
      } catch {
        break;
      }
    }
    // A turn acknowledgement can race a chat switch. It belongs to the
    // session that submitted it and must never restore working state, project
    // revision, or stop controls onto whichever chat is visible now.
    if (state.sessionId !== sessionId || runtimeActivationSeq !== activationSeq) return r.ok;
    if (!r.ok) {
      const revisionConflict = data.code === "E_REVISION_CONFLICT";
      turn.banners.push({
        kind: revisionConflict ? "info" : "turn_error",
        text: revisionConflict
          ? "工程在自动接续期间再次发生变化，这次请求尚未开始。"
          : (data.error || "任务未能开始，请稍后重试"),
      });
      state.turnInProgress = false;
      turn.completedAt = Date.now();
      turn.complete = true;
      if (revisionConflict) await refreshSessionState().catch(() => {});
      render();
      return false;
    }
    applyProductionSnapshot(data);
    if (data.duplicate && !data.scheduled) {
      state.turnInProgress = false;
      turn.completedAt = Date.now();
      turn.complete = true;
      turn.banners.push({ kind: "info", text: "这条请求已经处理过，没有重复执行或重复计费。" });
      await refreshSessionState().catch(() => {});
      render();
    }
    return true;
  }

  async function steerTurn(message) {
    if (!state.sessionId || !state.turnInProgress) throw new Error("no active turn");
    const sessionId = state.sessionId;
    const activationSeq = runtimeActivationSeq;
    const turn = state.currentTurn;
    const r = await apiFetch(`/sessions/${sessionId}/steer`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message }),
    });
    if (!r.ok) {
      const data = await r.json().catch(() => ({}));
      throw new Error(data.error || `引导未送达 (${r.status})`);
    }
    if (
      state.sessionId !== sessionId
      || runtimeActivationSeq !== activationSeq
      || state.currentTurn !== turn
    ) return;
    if (turn) turn.guidance.push(message);
    render();
  }

  async function stopCurrentTurn() {
    if (!state.sessionId || !state.turnInProgress || state.stopPending) return;
    const sessionId = state.sessionId;
    const activationSeq = runtimeActivationSeq;
    const turn = state.currentTurn;
    state.stopPending = true;
    render();
    try {
      const r = await apiFetch(`/sessions/${sessionId}/stop`, { method: "POST" });
      if (!r.ok) {
        const data = await r.json().catch(() => ({}));
        throw new Error(data.error || `停止未生效 (${r.status})`);
      }
    } catch (err) {
      if (
        state.sessionId !== sessionId
        || runtimeActivationSeq !== activationSeq
        || state.currentTurn !== turn
      ) return;
      state.stopPending = false;
      turn?.banners.push({ kind: "info", text: "停止请求未成功，请再试一次" });
      state.errors.push(`stop turn failed: ${err.message}`);
      render();
    }
  }

  async function submitProductionReview(action) {
    if (!state.projectId || !state.runId || !els.productionReview) return;
    const note = els.reviewNote.value.trim();
    const startRaw = els.reviewStartSec.value.trim();
    const endRaw = els.reviewEndSec.value.trim();
    if (action === "request_changes" && !note) {
      els.productionReviewStatus.textContent = "请写下要修改的内容。";
      els.reviewNote.focus();
      return;
    }
    if (!!startRaw !== !!endRaw) {
      els.productionReviewStatus.textContent = "时间范围需要同时填写开始和结束。";
      return;
    }
    const creativeChecks = Object.fromEntries(
      els.reviewCreativeChecks.map((input) => [input.dataset.reviewDimension, input.checked]),
    );
    if (action === "approve" && !currentReviewMaster()) {
      els.productionReviewStatus.textContent = "正式审片母版不可用，不能确认发布。";
      return;
    }
    if (
      action === "approve"
      && (
        !els.reviewWatchedFullVideo.checked
        || Object.values(creativeChecks).some((value) => value !== true)
      )
    ) {
      els.productionReviewStatus.textContent = "请先完整观看，并逐项确认叙事、节奏、视觉、声音和可发布性。";
      return;
    }
    const payload = {
      action,
      note,
      expected_project_revision: state.projectRevision,
    };
    if (action === "approve") {
      payload.watched_full_video = true;
      payload.creative_checks = creativeChecks;
    }
    if (startRaw && endRaw) {
      payload.start_sec = Number(startRaw);
      payload.end_sec = Number(endRaw);
    }
    els.productionReview.dataset.submitting = "1";
    els.productionReviewStatus.textContent = action === "approve" ? "正在确认…" : "正在提交返修意见…";
    renderProductionUi();
    try {
      const r = await apiFetch(
        `/projects/${encodeURIComponent(state.projectId)}/runs/${encodeURIComponent(state.runId)}/review`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        },
      );
      const data = await r.json().catch(() => ({}));
      if (!r.ok) {
        if (data.code === "E_REVISION_CONFLICT") await refreshSessionState().catch(() => {});
        throw new Error(data.error || `review failed: ${r.status}`);
      }
      applyProductionSnapshot(data);
      if (!data.production_state) {
        state.productionState = action === "approve" ? "accepted" : "revising";
      }
      els.productionReviewStatus.textContent = action === "approve"
        ? "已记录：可以发布。"
        : "返修意见已进入同一个工程。";
      if (action === "request_changes") {
        els.reviewNote.value = "";
        els.reviewStartSec.value = "";
        els.reviewEndSec.value = "";
      }
      await autoSaveSession();
    } catch (err) {
      els.productionReviewStatus.textContent = err.message || "审片意见未能提交。";
    } finally {
      delete els.productionReview.dataset.submitting;
      render();
    }
  }

  // ── wiring ──────────────────────────────────────────────────────────

  els.newSessionBtn.addEventListener("click", () => {
    createSession(state.projectId ? { fork_from_project_id: state.projectId } : {}).catch((err) => {
      state.errors.push(`create session failed: ${err.message}`);
      setConnPill("failed", "failed");
      render();
    });
  });
  els.newProjectBtn?.addEventListener("click", openCreateProjectDialog);
  els.projectBtn?.addEventListener("click", () => {
    const open = els.projectBtn.getAttribute("aria-expanded") !== "true";
    els.projectBtn.setAttribute("aria-expanded", String(open));
    els.projectBtn.setAttribute("aria-label", open ? "收起 Projects 与会话" : "打开 Projects 与会话");
    els.projectBtn.title = open ? "收起 Projects 与会话" : "打开 Projects 与会话";
    els.appMain?.classList.toggle("project-sidebar-collapsed", !open);
    if (els.projectSidebar) {
      els.projectSidebar.hidden = !open;
      els.projectSidebar.setAttribute("aria-hidden", String(!open));
    }
  });

  els.requestChangesBtn?.addEventListener("click", () => submitProductionReview("request_changes"));
  els.approveProductionBtn?.addEventListener("click", () => submitProductionReview("approve"));

  els.uploadBtn.addEventListener("click", () => els.uploadInput.click());
  document.addEventListener("keydown", (e) => {
    if ((e.metaKey || e.ctrlKey) && (e.key === "u" || e.key === "U")) {
      e.preventDefault();
      if (!els.uploadBtn.disabled) els.uploadBtn.click();
    }
  });
  els.uploadInput.addEventListener("change", () => {
    const files = Array.from(els.uploadInput.files || []);
    if (!files.length) return;
    files.reduce((chain, file, index) => chain.then(async () => {
      setUploadStatus(`正在上传 ${index + 1}/${files.length} · ${file.name}`);
      await uploadFile(file);
    }), Promise.resolve()).catch((err) => {
      state.errors.push(`upload failed: ${err.message}`);
      render();
    }).finally(() => { els.uploadInput.value = ""; });
  });

  els.libraryRefreshBtn?.addEventListener("click", () => {
    fetchMediaLibrary().catch((err) => {
      state.errors.push(`media library failed: ${err.message}`);
      render();
    });
  });

  els.libraryAnnotateBtn?.addEventListener("click", () => {
    annotateLibraryAsset("").catch((err) => {
      state.errors.push(`annotate media failed: ${err.message}`);
      render();
    });
  });

  els.libraryRoughcutBtn?.addEventListener("click", () => {
    startRoughcutPreparation().catch((err) => {
      state.errors.push(`粗剪准备失败: ${err.message}`);
      render();
    });
  });

  let _speakingUtterance = null;

  document.addEventListener("click", (e) => {
    const librarySection = e.target.closest("[data-library-section]");
    if (librarySection) {
      state.librarySection = librarySection.dataset.librarySection === "non-media" ? "non-media" : "media";
      state.libraryFocusName = "";
      renderMediaLibrary();
      if (stageTabs.includes("library")) refreshPanel("library");
      return;
    }

    const markdownLink = e.target.closest("a.md-link");
    if (markdownLink && openNonMediaLibraryLink(markdownLink)) {
      e.preventDefault();
      return;
    }

    // ── Entity reference click → navigate ──
    const entity = e.target.closest(".md-entity[data-entity-kind]");
    if (entity) {
      focusEntity(entity.dataset.entityKind, entity.dataset.entityId);
      return;
    }

    // ── Copy assistant text ──
    const copyBtn = e.target.closest("[data-copy-assistant]");
    if (copyBtn) {
      const turnIdx = Number(copyBtn.dataset.copyAssistant);
      const turn = state.turns[turnIdx];
      if (turn?.assistantText) {
        navigator.clipboard.writeText(turn.assistantText).then(() => {
          const svg = copyBtn.querySelector("svg use");
          if (svg) { svg.setAttribute("href", "#i-check"); setTimeout(() => svg.setAttribute("href", "#i-copy"), 1200); }
        });
      }
      return;
    }

    // ── Copy user message ──
    const copyUserBtn = e.target.closest("[data-copy-user]");
    if (copyUserBtn) {
      const turnIdx = Number(copyUserBtn.dataset.copyUser);
      const turn = state.turns[turnIdx];
      if (turn?.userMessage) {
        navigator.clipboard.writeText(turn.userMessage).then(() => {
          const svg = copyUserBtn.querySelector("svg use");
          if (svg) { svg.setAttribute("href", "#i-check"); setTimeout(() => svg.setAttribute("href", "#i-copy"), 1200); }
        });
      }
      return;
    }

    // ── Retract last user turn ──
    const retractBtn = e.target.closest("[data-retract-user]");
    if (retractBtn) {
      const turnIdx = Number(retractBtn.dataset.retractUser);
      retractTurn(turnIdx);
      return;
    }

    // ── Speak assistant text ──
    const speakBtn = e.target.closest("[data-speak-assistant]");
    if (speakBtn) {
      if (_speakingUtterance && speechSynthesis.speaking) {
        speechSynthesis.cancel();
        _speakingUtterance = null;
        const svg = speakBtn.querySelector("svg use");
        if (svg) svg.setAttribute("href", "#i-volume");
        return;
      }
      const turnIdx = Number(speakBtn.dataset.speakAssistant);
      const turn = state.turns[turnIdx];
      if (turn?.assistantText && window.speechSynthesis) {
        const u = new SpeechSynthesisUtterance(turn.assistantText);
        u.lang = "zh-CN";
        const svg = speakBtn.querySelector("svg use");
        if (svg) svg.setAttribute("href", "#i-stop");
        u.onend = () => { _speakingUtterance = null; if (svg) svg.setAttribute("href", "#i-volume"); };
        u.onerror = u.onend;
        _speakingUtterance = u;
        speechSynthesis.speak(u);
      }
      return;
    }

    const roughcutBtn = e.target.closest("[data-library-roughcut]");
    if (roughcutBtn) {
      startRoughcutPreparation(roughcutBtn.dataset.libraryRoughcut).catch((err) => {
        state.errors.push(`粗剪准备失败: ${err.message}`);
        render();
      });
      return;
    }
    const annotateBtn = e.target.closest("[data-library-annotate]");
    if (annotateBtn) {
      const assetId = annotateBtn.dataset.libraryAnnotate;
      annotateLibraryAsset(assetId).catch((err) => {
        state.errors.push(`annotate ${assetId} failed: ${err.message}`);
        render();
      });
      return;
    }
    const loadBtn = e.target.closest("[data-library-load]");
    if (loadBtn) {
      const assetId = loadBtn.dataset.libraryLoad;
      Promise.all([loadMediaAnnotations(assetId), loadRoughcutManifest(assetId)]).catch((err) => {
        state.errors.push(`load annotations failed: ${err.message}`);
        render();
      });
      return;
    }
    const reviewBtn = e.target.closest("[data-roughcut-review]");
    if (reviewBtn) {
      reviewRoughcut(reviewBtn).catch((err) => {
        state.errors.push(`复核保存失败: ${err.message}`);
        render();
      });
      return;
    }
    const seekBtn = e.target.closest("[data-roughcut-seek]");
    if (seekBtn) {
      const player = seekBtn.closest(".library-card")?.querySelector(".roughcut-preview");
      if (player) {
        player.currentTime = Number(seekBtn.dataset.roughcutSeek || 0);
        player.play().catch(() => {});
      }
    }
  });

  // ── /model picker ───────────────────────────────────────────────────
  // Lists the backend's priority-ordered model catalog + thinking-effort
  // tiers, marks the active pick, and lets the user switch. The selection is
  // global + persisted (config.json:lumeri_v3_model / lumeri_v3_effort) — the
  // same store the CLI /model uses — so it sticks across sessions/restarts.
  async function fetchModelInfo() {
    const r = await apiFetch("/model");
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    return r.json();
  }

  async function postModelSelection(body) {
    const r = await apiFetch("/model", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.error || `HTTP ${r.status}`);
    return data;
  }

  function openModelPicker() {
    let overlay = $("#model-modal");
    if (!overlay) {
      overlay = document.createElement("div");
      overlay.id = "model-modal";
      overlay.className = "dialog-modal";
      overlay.hidden = true;
      overlay.innerHTML = `
        <div class="model-backdrop" data-model-close></div>
        <div class="dialog-shell model-dialog" role="dialog" aria-modal="true" aria-labelledby="model-title">
          <button type="button" class="dialog-close" data-model-close aria-label="关闭">×</button>
          <h2 id="model-title">模型与思考强度</h2>
          <div class="model-list" id="model-list"></div>
          <div class="model-add-wrap" id="model-add-wrap">
            <button type="button" class="model-add-btn" id="model-add-btn">+ 添加模型</button>
            <div class="model-add-dropdown" id="model-add-dropdown" hidden>
              <div class="model-add-search-wrap">
                <input type="text" class="model-add-search" id="model-add-search" placeholder="搜索或输入模型 ID…">
                <span class="model-add-spinner" id="model-add-spinner" hidden></span>
              </div>
              <div class="model-add-list" id="model-add-list"></div>
              <button type="button" class="model-add-custom" id="model-add-custom" hidden>添加自定义模型</button>
            </div>
          </div>
          <div class="model-effort-label">思考强度</div>
          <div class="model-efforts" id="model-efforts"></div>
          <div class="model-save-wrap" id="model-save-wrap" hidden>
            <button type="button" class="model-save-btn" id="model-save-btn">保存</button>
          </div>
          <p class="dialog-error" id="model-error" hidden></p>
        </div>`;
      document.body.appendChild(overlay);
      overlay.querySelectorAll("[data-model-close]").forEach((el) =>
        el.addEventListener("click", () => { overlay.hidden = true; }));
      document.addEventListener("keydown", (e) => {
        if (e.key === "Escape" && !overlay.hidden) overlay.hidden = true;
      });
    }
    const errEl = $("#model-error", overlay);
    const setErr = (msg) => {
      if (!errEl) return;
      if (msg) { errEl.textContent = msg; errEl.hidden = false; }
      else { errEl.hidden = true; }
    };

    let scannedModels = [];
    let lastInfo = null;
    let pendingModel = null;
    let pendingEffort = null;

    function renderInfo(info) {
      lastInfo = info;
      const active = info.active || {};
      if (pendingModel === null) pendingModel = active.model;
      if (pendingEffort === null) pendingEffort = active.effort;
      const selModel = pendingModel || active.model;
      const selEffort = pendingEffort || active.effort;
      const list = $("#model-list", overlay);
      const canDelete = (info.priority || []).length > 1;
      list.innerHTML = (info.priority || []).map((it, i) => {
        const on = it.id === selModel;
        return `
          <div class="model-row${on ? " active" : ""}">
            <button type="button" class="model-row-select" data-model-id="${escapeHTML(it.id)}">
              <span class="model-dot">${on ? "●" : "○"}</span>
              <span class="model-name">${escapeHTML(it.label)}${i === 0 ? ' <span class="model-tag">推荐</span>' : ""}</span>
              <span class="model-id">${escapeHTML(it.id)}</span>
            </button>${canDelete ? `<button type="button" class="model-del" data-del-id="${escapeHTML(it.id)}" aria-label="删除 ${escapeHTML(it.label)}">×</button>` : ""}
          </div>`;
      }).join("");
      const efforts = $("#model-efforts", overlay);
      efforts.innerHTML = (info.efforts || []).map((e) => {
        const on = e === selEffort;
        return `<button type="button" class="model-chip${on ? " active" : ""}" data-effort="${escapeHTML(e)}">${escapeHTML(e)}</button>`;
      }).join("");

      list.querySelectorAll("[data-model-id]").forEach((btn) =>
        btn.addEventListener("click", () => { pendingModel = btn.dataset.modelId; renderInfo(info); }));
      list.querySelectorAll("[data-del-id]").forEach((btn) =>
        btn.addEventListener("click", (e) => { e.stopPropagation(); removeModel(btn.dataset.delId); }));
      efforts.querySelectorAll("[data-effort]").forEach((btn) =>
        btn.addEventListener("click", () => { pendingEffort = btn.dataset.effort; renderInfo(info); }));

      const changed = selModel !== active.model || selEffort !== active.effort;
      const saveWrap = $("#model-save-wrap", overlay);
      if (saveWrap) saveWrap.hidden = !changed;
      const addWrap = $("#model-add-wrap", overlay);
      if (addWrap) addWrap.hidden = false;
    }

    async function apply() {
      setErr("");
      try {
        const body = {};
        if (pendingModel) body.model = pendingModel;
        if (pendingEffort) body.effort = pendingEffort;
        const data = await postModelSelection(body);
        pendingModel = data.active?.model || pendingModel;
        pendingEffort = data.active?.effort || pendingEffort;
        renderInfo(data);
      } catch (e) {
        setErr(`保存失败：${e.message}`);
      }
    }

    $("#model-save-btn", overlay).addEventListener("click", apply);

    async function removeModel(id) {
      setErr("");
      try {
        const r = await apiFetch("/model/remove", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ id }),
        });
        const data = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(data.error || `HTTP ${r.status}`);
        renderInfo(data);
      } catch (e) {
        setErr(`删除失败：${e.message}`);
      }
    }

    async function addModel(id, label) {
      setErr("");
      try {
        const r = await apiFetch("/model/add", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ id, label: label || id }),
        });
        const data = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(data.error || `HTTP ${r.status}`);
        renderInfo(data);
        closeAddDropdown();
      } catch (e) {
        setErr(`添加失败：${e.message}`);
      }
    }

    // ── add-model dropdown ──
    const addBtn = $("#model-add-btn", overlay);
    const dropdown = $("#model-add-dropdown", overlay);
    const searchInp = $("#model-add-search", overlay);
    const addList = $("#model-add-list", overlay);
    const customBtn = $("#model-add-custom", overlay);
    const spinner = $("#model-add-spinner", overlay);

    function closeAddDropdown() {
      dropdown.hidden = true;
      searchInp.value = "";
      customBtn.hidden = true;
    }

    addBtn.addEventListener("click", () => {
      if (!dropdown.hidden) { closeAddDropdown(); return; }
      dropdown.hidden = false;
      searchInp.value = "";
      searchInp.focus();
      renderAddList("");
      if (!scannedModels.length) scanModels();
    });

    document.addEventListener("click", (e) => {
      const wrap = $("#model-add-wrap", overlay);
      if (wrap && !wrap.contains(e.target)) closeAddDropdown();
    });

    searchInp.addEventListener("input", () => {
      renderAddList(searchInp.value);
    });

    customBtn.addEventListener("click", () => {
      const v = searchInp.value.trim();
      if (v) addModel(v, v);
    });

    function renderAddList(query) {
      const q = query.toLowerCase().trim();
      const currentIds = new Set(
        [...overlay.querySelectorAll("[data-model-id]")].map((el) => el.dataset.modelId)
      );
      let filtered = scannedModels.filter((m) => !currentIds.has(m.id));
      if (q) filtered = filtered.filter((m) =>
        m.id.toLowerCase().includes(q) || (m.name || "").toLowerCase().includes(q)
      );
      addList.innerHTML = filtered.slice(0, 20).map((m) => {
        const name = m.name ? ` <span class="model-add-name">${escapeHTML(m.name)}</span>` : "";
        return `<button type="button" class="model-add-opt" data-add-id="${escapeHTML(m.id)}">${escapeHTML(m.id)}${name}</button>`;
      }).join("") || (q ? '<div class="model-add-empty">无匹配结果</div>' : '<div class="model-add-empty">扫描中…</div>');
      addList.querySelectorAll("[data-add-id]").forEach((btn) =>
        btn.addEventListener("click", () => addModel(btn.dataset.addId, btn.dataset.addId)));
      customBtn.hidden = !q || filtered.some((m) => m.id === q);
    }

    let scanAbortCtrl = null;
    async function scanModels() {
      if (scanAbortCtrl) scanAbortCtrl.abort();
      const ctrl = scanAbortCtrl = new AbortController();
      spinner.hidden = false;
      try {
        const r = await apiFetch("/config/list-models", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({}),
          signal: ctrl.signal,
        });
        const d = await r.json().catch(() => ({}));
        if (ctrl.signal.aborted) return;
        if (d.models && d.models.length) {
          scannedModels = d.models;
          renderAddList(searchInp.value);
        } else if (!d.ok) {
          setErr(`模型扫描失败：${d.error || `HTTP ${r.status}`}`);
        }
      } catch (e) {
        if (e.name === "AbortError") return;
        setErr(`模型扫描失败：${e.message}`);
      }
      spinner.hidden = true;
    }

    setErr("");
    $("#model-list", overlay).innerHTML = '<div class="model-loading">加载中…</div>';
    $("#model-efforts", overlay).innerHTML = "";
    overlay.hidden = false;
    fetchModelInfo().then(renderInfo).catch((e) => setErr(`加载失败：${e.message}`));
  }

  // ── AI 供应商 Setup 面板 ─────────────────────────────────────────────
  // 列常见 provider（Vertex/Gemini/OpenAI API/OpenAI 订阅/Claude/OpenRouter）+ 自定义(OpenAI 兼容)，
  // 密钥经 POST /config 白名单存盘、即时生效；测试连接走 POST /config/test-brain。
  function openSetupPanel() {
    if (!byokAllowed()) return;
    let overlay = $("#setup-modal");
    if (!overlay) {
      overlay = document.createElement("div");
      overlay.id = "setup-modal";
      overlay.className = "dialog-modal";
      overlay.hidden = true;
      overlay.innerHTML = `
        <div class="model-backdrop" data-setup-close></div>
        <div class="dialog-shell setup-dialog" role="dialog" aria-modal="true" aria-labelledby="setup-title">
          <button type="button" class="dialog-close" data-setup-close aria-label="关闭">×</button>
          <h2 id="setup-title">AI 供应商配置</h2>
          <div class="setup-providers" id="setup-providers"></div>
          <div class="setup-fields" id="setup-fields"></div>
          <div class="setup-actions">
            <button type="button" class="setup-test" id="setup-test">测试连接</button>
            <button type="button" class="setup-save" id="setup-save">保存并启用</button>
          </div>
          <p class="setup-result" id="setup-result" hidden></p>
          <div class="setup-divider"></div>
          <h3 class="setup-section-title">搜索引擎</h3>
          <div class="search-provider-chips" id="search-provider-chips"></div>
          <div class="search-fields" id="search-fields"></div>
          <div class="setup-actions">
            <button type="button" class="setup-save search-save" id="search-save">保存搜索配置</button>
          </div>
          <p class="setup-result" id="search-result" hidden></p>
          <p class="dialog-error" id="setup-error" hidden></p>
        </div>`;
      document.body.appendChild(overlay);
      overlay.querySelectorAll("[data-setup-close]").forEach((el) =>
        el.addEventListener("click", () => { overlay.hidden = true; }));
      document.addEventListener("keydown", (e) => {
        if (e.key === "Escape" && !overlay.hidden) overlay.hidden = true;
      });
    }
    const st = {
      info: null,
      sel: "",
      vals: {},
      curProvider: "",
      scannedModels: [],
      providerOrder: [],
      modelSource: "recommended",
    };
    const errEl = $("#setup-error", overlay);
    const resEl = $("#setup-result", overlay);
    const setErr = (m) => { if (m) { errEl.textContent = m; errEl.hidden = false; } else errEl.hidden = true; };
    const setRes = (m, ok) => {
      if (!m) { resEl.hidden = true; return; }
      resEl.textContent = m; resEl.hidden = false;
      resEl.className = "setup-result " + (ok ? "ok" : "bad");
    };

    const FIELD_META = {
      profile_name:    { label: "配置名称", ph: "例如 Sisyphus / 公司网关" },
      vertex_project:  { label: "GCP 项目 ID", ph: "my-project-123" },
      vertex_location: { label: "区域", ph: "global / us-east5 / us-central1" },
      base_url:        { label: "Base URL", ph: "https://…/v1/chat/completions" },
      key:             { label: "API Key", ph: "sk-…（留空=沿用已存）" },
    };

    function providerCard(p, active) {
      const label = p.id === "custom" ? "自定义" : p.label;
      return `<div class="setup-pcard${active ? " active" : ""}" data-pid="${escapeHTML(p.id)}" draggable="true">
        <span class="setup-drag" title="拖动排序">☰</span>
        <div class="setup-ptext">
          <span class="setup-pname">${escapeHTML(label)}</span>
        </div>
      </div>`;
    }

    function keyStateLabel(p, info) {
      if (!p.key_field) return "";
      const savedProfile = info.profiles && info.profiles[p.id];
      if (savedProfile) {
        return savedProfile.has_key ? ' <span class="setup-haskey">已配置</span>' : "";
      }
      const map = { openrouter_api_key: "openrouter", gemini_api_key: "gemini", anthropic_api_key: "anthropic", openai_api_key: "openai" };
      const has = info.has_key && info.has_key[map[p.key_field]];
      return has ? ' <span class="setup-haskey">已配置</span>' : "";
    }

    function getProviders() {
      if (!st.info) return [];
      return st.providerOrder
        .map((id) => (st.info.providers || []).find((x) => x.id === id))
        .filter(Boolean);
    }

    // ── drag-to-reorder ──
    let dragSrc = null;
    function initDrag(container) {
      container.addEventListener("dragstart", (e) => {
        const card = e.target.closest("[data-pid]");
        if (!card) return;
        dragSrc = card;
        card.classList.add("dragging");
        e.dataTransfer.effectAllowed = "move";
        e.dataTransfer.setData("text/plain", card.dataset.pid);
      });
      container.addEventListener("dragend", (e) => {
        const card = e.target.closest("[data-pid]");
        if (card) card.classList.remove("dragging");
        container.querySelectorAll("[data-pid]").forEach((c) => c.classList.remove("drag-over"));
        dragSrc = null;
      });
      container.addEventListener("dragover", (e) => {
        e.preventDefault();
        e.dataTransfer.dropEffect = "move";
        const card = e.target.closest("[data-pid]");
        container.querySelectorAll("[data-pid]").forEach((c) => c.classList.remove("drag-over"));
        if (card && card !== dragSrc) card.classList.add("drag-over");
      });
      container.addEventListener("drop", (e) => {
        e.preventDefault();
        const target = e.target.closest("[data-pid]");
        if (!target || !dragSrc || target === dragSrc) return;
        const fromId = dragSrc.dataset.pid;
        const toId = target.dataset.pid;
        const arr = st.providerOrder;
        const fi = arr.indexOf(fromId), ti = arr.indexOf(toId);
        if (fi < 0 || ti < 0) return;
        arr.splice(fi, 1);
        arr.splice(ti, 0, fromId);
        renderProviders();
      });
    }

    function renderProviders() {
      const container = $("#setup-providers", overlay);
      const cur = st.sel;
      container.innerHTML = getProviders().map((p) => providerCard(p, p.id === cur)).join("");
      container.querySelectorAll("[data-pid]").forEach((c) => {
        c.addEventListener("click", (e) => {
          if (e.target.closest(".setup-drag")) return;
          selectProvider(c.dataset.pid);
        });
      });
    }

    // ── model combo-box (auto-scan + free input) ──
    let scanAbort = null;
    function renderModelField(box, p, curVal) {
      const wrap = document.createElement("label");
      wrap.className = "setup-f";
      wrap.innerHTML = `<span>模型 ID</span><div class="setup-model-wrap">
        <input type="text" data-f="model" value="${escapeHTML(curVal)}" placeholder="输入模型 ID 或从列表选择">
        <span class="setup-model-spinner" hidden></span>
        <div class="setup-model-list" hidden></div>
      </div>`;
      box.appendChild(wrap);

      const inp = wrap.querySelector('input[data-f="model"]');
      const spinner = wrap.querySelector(".setup-model-spinner");
      const listEl = wrap.querySelector(".setup-model-list");

      inp.addEventListener("input", () => {
        st.vals.model = inp.value;
        st.modelSource = "manual";
        filterModelList(inp.value, listEl, inp);
      });
      inp.addEventListener("focus", () => {
        if (st.scannedModels.length) { renderModelList(st.scannedModels, listEl, inp); listEl.hidden = false; }
      });

      document.addEventListener("click", (e) => {
        if (!wrap.contains(e.target)) listEl.hidden = true;
      });

      // presets as immediate fallback
      if (p.model_presets && p.model_presets.length) {
        st.scannedModels = p.model_presets.map((m) => ({ id: m }));
        renderModelList(st.scannedModels, listEl, inp);
      }

      // auto-scan on render
      autoScanModels(inp, listEl, spinner, p);
    }

    async function autoScanModels(inp, listEl, spinner, p) {
      if (scanAbort) scanAbort.abort();
      const ctrl = scanAbort = new AbortController();
      spinner.hidden = false;
      try {
        const body = buildBody();
        const r = await apiFetch("/config/list-models", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body), signal: ctrl.signal,
        });
        const d = await r.json().catch(() => ({}));
        if (ctrl.signal.aborted) return;
        if (!r.ok || !d.ok) throw new Error(d.error || `HTTP ${r.status}`);
        if (d.models && d.models.length) {
          st.scannedModels = d.models;
          if (st.modelSource === "recommended" && d.recommended_model) {
            inp.value = d.recommended_model;
            st.vals.model = d.recommended_model;
          }
          renderModelList(d.models, listEl, inp);
        }
      } catch (e) {
        if (e.name === "AbortError") return;
        setErr(`模型扫描失败：${e.message}`);
      } finally {
        if (scanAbort === ctrl) spinner.hidden = true;
      }
    }

    function renderModelList(models, listEl, inp) {
      listEl.innerHTML = models.map((m) => {
        const name = m.name ? `<span class="setup-model-name">${escapeHTML(m.name)}</span>` : "";
        return `<div class="setup-model-opt" data-mid="${escapeHTML(m.id)}">${escapeHTML(m.id)}${name}</div>`;
      }).join("");
      listEl.querySelectorAll("[data-mid]").forEach((opt) => {
        opt.addEventListener("click", () => {
          inp.value = opt.dataset.mid;
          st.vals.model = opt.dataset.mid;
          st.modelSource = "manual";
          listEl.hidden = true;
        });
      });
    }

    function filterModelList(query, listEl, inp) {
      if (!st.scannedModels.length) return;
      const q = query.toLowerCase();
      const filtered = q ? st.scannedModels.filter((m) =>
        m.id.toLowerCase().includes(q) || (m.name && m.name.toLowerCase().includes(q))
      ) : st.scannedModels;
      renderModelList(filtered, listEl, inp);
      listEl.hidden = filtered.length === 0;
    }

    function renderFields() {
      const p = getProviders().find((x) => x.id === st.sel);
      const providerId = p && (p.provider || p.id);
      const box = $("#setup-fields", overlay);
      if (!p) { box.innerHTML = ""; return; }
      box.innerHTML = "";
      st.scannedModels = [];

      if (providerId === "custom") {
        const meta = FIELD_META.profile_name;
        const label = document.createElement("label");
        label.className = "setup-f";
        label.innerHTML = `<span>${escapeHTML(meta.label)}</span>
          <input type="text" data-f="profile_name" value="${escapeHTML(st.vals.profile_name || "")}" placeholder="${escapeHTML(meta.ph)}">`;
        box.appendChild(label);
      }

      for (const f of p.fields) {
        if (f === "model") {
          let val = st.vals.model ?? "";
          if (!val && st.sel === st.curProvider) val = st.info.model || "";
          if (!val) val = p.recommended_model || "";
          if (val) st.vals.model = val;
          renderModelField(box, p, val);
          continue;
        }
        const meta = FIELD_META[f] || { label: f, ph: "" };
        let val = st.vals[f] ?? "";
        if (!val) {
          if (f === "vertex_project") val = st.info.vertex_project || "";
          else if (f === "vertex_location") val = st.info.vertex_location || "";
          else if (f === "base_url" && st.sel === st.curProvider) val = st.info.base_url || "";
        }
        const label = document.createElement("label");
        label.className = "setup-f";
        label.innerHTML = `<span>${escapeHTML(meta.label)}</span>
          <input type="text" data-f="${f}" value="${escapeHTML(val)}" placeholder="${escapeHTML(meta.ph)}">`;
        box.appendChild(label);
      }
      if (p.key_field) {
        const label = document.createElement("label");
        label.className = "setup-f";
        label.innerHTML = `<span>${escapeHTML(FIELD_META.key.label)}${keyStateLabel(p, st.info)}</span>
          <input type="password" data-f="key" value="" placeholder="${escapeHTML(FIELD_META.key.ph)}">`;
        box.appendChild(label);
      }

      const effs = st.info.efforts || [];
      const curEff = st.vals.effort || st.info.effort || "medium";
      const effDiv = document.createElement("div");
      effDiv.innerHTML = `<div class="setup-effort-label">思考强度</div><div class="setup-efforts">${effs.map((e) =>
        `<button type="button" class="setup-echip${e === curEff ? " active" : ""}" data-eff="${escapeHTML(e)}">${escapeHTML(e)}</button>`).join("")}</div>`;
      box.appendChild(effDiv);


      box.querySelectorAll("input[data-f]").forEach((inp) => {
        if (inp.dataset.f !== "model") inp.addEventListener("input", () => { st.vals[inp.dataset.f] = inp.value; });
      });
      box.querySelectorAll("[data-eff]").forEach((b) =>
        b.addEventListener("click", () => {
          st.vals.effort = b.dataset.eff;
          box.querySelectorAll("[data-eff]").forEach((x) => x.classList.toggle("active", x === b));
        }));
    }


    function selectProvider(pid) {
      st.sel = pid;
      const p = getProviders().find((item) => item.id === pid);
      const saved = (st.info.profiles && st.info.profiles[pid]) || {};
      const savedModel = saved.model || "";
      st.vals = {
        effort: saved.effort || "medium",
        model: savedModel || (p && p.recommended_model) || "",
        profile_name: saved.name || (pid === "custom:new" ? "" : p && p.label) || "",
        base_url: saved.base_url || "",
        vertex_project: saved.vertex_project || "",
        vertex_location: saved.vertex_location || saved.location || "",
      };
      st.modelSource = savedModel ? "saved" : "recommended";
      st.scannedModels = [];
      renderProviders();
      setRes(""); setErr("");
      renderFields();
    }

    function buildBody() {
      const p = getProviders().find((x) => x.id === st.sel);
      const providerId = p && (p.provider || p.id);
      const body = { provider: providerId, profile_id: st.sel };
      if (providerId === "custom") body.profile_name = st.vals.profile_name || "自定义";
      if (st.vals.model) body.model = st.vals.model;
      if (st.vals.effort) body.effort = st.vals.effort;
      if (p && p.fields.includes("base_url")) body.base_url = st.vals.base_url || "";
      if (st.vals.vertex_project) body.vertex_project = st.vals.vertex_project;
      if (st.vals.vertex_location) body.location = st.vals.vertex_location, body.vertex_location = st.vals.vertex_location;
      if (p && p.key_field && st.vals.key) body[p.key_field] = st.vals.key;
      return body;
    }

    async function doSave() {
      setErr(""); setRes("保存中…", true);
      try {
        const r = await apiFetch("/config", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(buildBody()) });
        const d = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(d.error || `HTTP ${r.status}`);
        setRes("已保存并启用 ✓（新会话即生效）", true);
      } catch (e) { setRes(""); setErr(`保存失败：${e.message}`); }
    }

    async function doTest() {
      setErr(""); setRes("测试中…（可能需数秒）", true);
      try {
        const r = await apiFetch("/config/test-brain", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(buildBody()) });
        const d = await r.json().catch(() => ({}));
        if (d.ok) setRes(`连接成功 ✓ ${d.provider}/${d.model} — 回样「${d.sample || ""}」`, true);
        else setRes(`连接失败：${d.error || "未知错误"}（${d.provider || ""}/${d.model || ""}）`, false);
      } catch (e) { setRes(""); setErr(`测试失败：${e.message}`); }
    }

    $("#setup-save", overlay).onclick = doSave;
    $("#setup-test", overlay).onclick = doTest;

    // ── 搜索引擎配置 ──
    const SEARCH_PROVIDERS = [
      { id: "auto",       label: "自动",       hint: "按优先级自动检测已配置的引擎", fields: [] },
      { id: "tavily",     label: "Tavily",     hint: "AI 优化搜索", fields: [{ key: "tavily_api_key", label: "API Key", ph: "tvly-…" }] },
      { id: "brave",      label: "Brave",      hint: "隐私优先搜索", fields: [{ key: "brave_api_key", label: "API Key", ph: "BSA…" }] },
      { id: "serper",     label: "Serper",      hint: "Google 搜索 API", fields: [{ key: "serper_api_key", label: "API Key", ph: "" }] },
      { id: "exa",        label: "Exa",         hint: "语义搜索", fields: [{ key: "exa_api_key", label: "API Key", ph: "" }] },
      { id: "bing",       label: "Bing",        hint: "微软 Bing 搜索", fields: [{ key: "bing_api_key", label: "API Key", ph: "" }] },
      { id: "google_cse", label: "Google CSE",  hint: "自定义搜索引擎", fields: [{ key: "google_cse_key", label: "API Key", ph: "" }, { key: "google_cse_id", label: "搜索引擎 ID (CX)", ph: "" }] },
      { id: "searxng",    label: "SearXNG",     hint: "自托管、免费", fields: [{ key: "searxng_url", label: "实例 URL", ph: "https://searx.example.com" }, { key: "searxng_api_key", label: "Bearer Token（可选）", ph: "" }] },
      { id: "duckduckgo",  label: "DuckDuckGo", hint: "免费、无需密钥", fields: [] },
    ];
    let searchSel = "auto";
    const searchVals = {};
    const searchResEl = $("#search-result", overlay);
    const setSearchRes = (m, ok) => {
      if (!m) { searchResEl.hidden = true; return; }
      searchResEl.textContent = m; searchResEl.hidden = false;
      searchResEl.className = "setup-result " + (ok ? "ok" : "bad");
    };

    function renderSearchChips(searchInfo) {
      const chips = $("#search-provider-chips", overlay);
      chips.innerHTML = SEARCH_PROVIDERS.map((sp) => {
        const on = sp.id === searchSel;
        const hasKey = searchInfo && searchInfo.has_key && searchInfo.has_key[sp.id];
        const dot = hasKey ? '<span class="search-key-dot"></span>' : "";
        return `<button type="button" class="search-chip${on ? " active" : ""}" data-sp="${escapeHTML(sp.id)}">${dot}${escapeHTML(sp.label)}</button>`;
      }).join("");
      chips.querySelectorAll("[data-sp]").forEach((btn) =>
        btn.addEventListener("click", () => { searchSel = btn.dataset.sp; renderSearchChips(searchInfo); renderSearchFields(); }));
    }

    function renderSearchFields() {
      const box = $("#search-fields", overlay);
      const sp = SEARCH_PROVIDERS.find((p) => p.id === searchSel);
      if (!sp || !sp.fields.length) {
        box.innerHTML = sp && sp.id === "auto"
          ? '<p class="search-hint">自动模式按优先级探测：Tavily → Serper → Brave → Exa → Google CSE → Bing → SearXNG → DuckDuckGo</p>'
          : '<p class="search-hint">无需配置</p>';
        return;
      }
      box.innerHTML = sp.fields.map((f) => {
        const val = searchVals[f.key] || "";
        const isSecret = f.key.includes("api_key") || f.key.includes("key");
        return `<label class="setup-f"><span>${escapeHTML(f.label)}</span>
          <input type="${isSecret ? "password" : "text"}" data-sf="${escapeHTML(f.key)}" value="${escapeHTML(val)}" placeholder="${escapeHTML(f.ph || "留空=沿用已存")}"></label>`;
      }).join("");
      box.querySelectorAll("input[data-sf]").forEach((inp) =>
        inp.addEventListener("input", () => { searchVals[inp.dataset.sf] = inp.value; }));
    }

    async function doSearchSave() {
      setSearchRes("保存中…", true);
      try {
        const body = { search_provider: searchSel };
        for (const [k, v] of Object.entries(searchVals)) { if (v) body[k] = v; }
        const r = await apiFetch("/config", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
        const d = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(d.error || `HTTP ${r.status}`);
        setSearchRes("已保存 ✓", true);
      } catch (e) { setSearchRes(`保存失败：${e.message}`, false); }
    }

    $("#search-save", overlay).onclick = doSearchSave;

    setErr(""); setRes("");
    const provBox = $("#setup-providers", overlay);
    provBox.innerHTML = '<div class="model-loading">加载中…</div>';
    $("#setup-fields", overlay).innerHTML = "";
    initDrag(provBox);
    overlay.hidden = false;
    apiFetch("/config").then((r) => r.json()).then((cfg) => {
      const info = cfg.brain;
      if (!info) { setErr("该供应商配置暂不可用"); return; }
      st.info = info;
      st.vals = { effort: info.effort || "medium" };
      const templates = info.providers || [];
      const customTemplate = templates.find((p) => p.id === "custom");
      const fixed = templates.filter((p) => p.id !== "custom");
      const customProfiles = Object.values(info.profiles || {})
        .filter((profile) => profile.provider === "custom")
        .map((profile) => ({
          ...customTemplate,
          id: profile.id,
          provider: "custom",
          label: profile.name || "自定义",
        }));
      const newCustom = {
        ...customTemplate,
        id: "custom:new",
        provider: "custom",
        label: "＋ 添加自定义服务",
      };
      st.info.providers = [...fixed, ...customProfiles, newCustom];
      st.providerOrder = st.info.providers.map((p) => p.id);
      const preferred = info.active_profile || info.provider || "openrouter";
      const availableProviders = getProviders();
      const cur = availableProviders.some((provider) => provider.id === preferred)
        ? preferred
        : availableProviders[0]?.id;
      if (!cur) {
        setErr("账户供应商设置不可用");
        return;
      }
      st.curProvider = cur;
      const idx = st.providerOrder.indexOf(cur);
      if (idx > 0) { st.providerOrder.splice(idx, 1); st.providerOrder.unshift(cur); }
      renderProviders();
      selectProvider(cur);
      // 搜索引擎初始化
      const searchInfo = cfg.search || {};
      searchSel = searchInfo.provider || "auto";
      renderSearchChips(searchInfo);
      renderSearchFields();
    }).catch((e) => setErr(`加载失败：${e.message}`));
  }

  els.setupBtn?.addEventListener("click", openSetupPanel);

  // ── slash-command palette ───────────────────────────────────────────
  // A "/" at the start of an empty-ish line opens a floating command menu
  // above the composer. Mirrors the CLI slash set (src/slash.js), mapping
  // each command to an existing web action so the two clients stay in sync.
  // 描述 ≤10 字（文字精简）；细节进 /help，不进菜单行
  const SLASH_COMMANDS = [
    { name: "help",    desc: "所有命令" },
    { name: "new",     desc: "开启新会话" },
    { name: "project", desc: "新建 Project" },
    { name: "clear",   desc: "清空当前对话" },
    { name: "upload",  desc: "上传素材" },
    { name: "plan",    desc: "只规划，批准后执行" },
    { name: "model",   desc: "切换模型与强度" },
    { name: "setup",   desc: "配置 AI 供应商" },
    { name: "sandbox", desc: "沙盒开关" },
    { name: "library", desc: "刷新媒体库标注" },
  ];
  const slash = { open: false, items: [], sel: 0 };

  function availableSlashCommands() {
    return SLASH_COMMANDS.filter((command) => command.name !== "setup" || byokAllowed());
  }

  function knownSlash(name) { return availableSlashCommands().some((c) => c.name === name); }

  // Command name iff the line is `/name` (any trailing arg ignored) and known.
  function parseSlashName(line) {
    if (!line.startsWith("/")) return null;
    const sp = line.indexOf(" ");
    const name = (sp === -1 ? line.slice(1) : line.slice(1, sp)).toLowerCase();
    return knownSlash(name) ? name : null;
  }

  // Autocomplete state: active only while the line is a single `/token` (no space).
  function slashMatch(line) {
    if (!line.startsWith("/") || line.includes(" ")) return null;
    const frag = line.slice(1).toLowerCase();
    const matches = availableSlashCommands().filter((c) => c.name.startsWith(frag));
    return matches.length ? matches : null;
  }

  function slashRender() {
    const m = els.slashMenu;
    if (!m) return;
    if (!slash.open) { m.hidden = true; m.innerHTML = ""; return; }
    const rows = slash.items.map((c, i) => `
      <div class="slash-item${i === slash.sel ? " active" : ""}" data-slash="${c.name}">
        <span class="slash-name">/${c.name}</span>
        <span class="slash-desc">${escapeHTML(c.desc)}</span>
      </div>`).join("");
    m.innerHTML = rows;
    m.hidden = false;
    m.querySelector(".slash-item.active")?.scrollIntoView({ block: "nearest" });
  }

  function slashSync() {
    const matches = slashMatch(els.promptInput.value);
    if (!matches) { slash.open = false; slashRender(); return; }
    slash.open = true;
    slash.items = matches;
    slash.sel = 0;
    slashRender();
  }

  function slashClose() { slash.open = false; slashRender(); }

  function execSlash(name) {
    // /help lists everything by re-opening the menu on a bare slash.
    if (name === "help") { els.promptInput.value = "/"; slashSync(); els.promptInput.focus(); return; }
    switch (name) {
      case "new":     els.newSessionBtn.click(); break;
      case "project": openCreateProjectDialog(); break;
      case "clear":   state.turns = []; state.currentTurn = null; render(); break;
      case "upload":  els.uploadBtn.click(); break;
      case "plan":    els.planBtn?.click(); break;
      case "model":   openModelPicker(); break;
      case "setup":   openSetupPanel(); break;
      case "sandbox": els.sandboxBtn?.click(); break;
      case "library": els.libraryRefreshBtn?.click(); break;
    }
    els.promptInput.value = "";
    slashClose();
    syncShell();
  }

  // Menu navigation. Returns true when it consumed the key.
  function slashKeydown(e) {
    if (!slash.open || !slash.items.length) return false;
    const n = slash.items.length;
    if (e.key === "ArrowDown") { e.preventDefault(); slash.sel = (slash.sel + 1) % n; slashRender(); return true; }
    if (e.key === "ArrowUp")   { e.preventDefault(); slash.sel = (slash.sel - 1 + n) % n; slashRender(); return true; }
    if (e.key === "Escape")    { e.preventDefault(); slashClose(); return true; }
    if (e.key === "Tab")       { e.preventDefault(); els.promptInput.value = "/" + slash.items[slash.sel].name; slashClose(); return true; }
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing && e.keyCode !== 229) {
      e.preventDefault();
      execSlash(slash.items[slash.sel].name);
      return true;
    }
    return false;
  }

  els.promptInput.addEventListener("input", slashSync);
  // Clicking a menu row runs it; clicking elsewhere dismisses the menu.
  els.slashMenu?.addEventListener("mousedown", (e) => {
    const row = e.target.closest(".slash-item[data-slash]");
    if (!row) return;
    e.preventDefault();               // keep focus in the textarea
    execSlash(row.dataset.slash);
  });
  document.addEventListener("click", (e) => {
    if (!slash.open) return;
    if (e.target.closest(".composer")) return;
    slashClose();
  });

  els.sendBtn.addEventListener("click", () => {
    if (!state.sessionId) return;
    const msg = els.promptInput.value.trim();
    if (state.turnInProgress && !msg) { stopCurrentTurn(); return; }
    if (!SpeechRecognition && !els.promptInput.value.trim()) return;
    if (voiceInput.listening) {
      stopVoiceInput();
      return;
    }
    if (!msg) {
      startVoiceInput();
      return;
    }
    const name = parseSlashName(msg);
    if (name) { execSlash(name); return; }
    (state.turnInProgress ? steerTurn(msg) : submitTurn(msg))
      .then(() => { els.promptInput.value = ""; slashClose(); syncShell(); })
                   .catch((err) => {
                     state.errors.push(`submit turn failed: ${err.message}`);
                     state.currentTurn?.banners.push({ kind: "info", text: "任务未能开始，请稍后重试" });
                     render();
                   });
  });
  els.promptInput.addEventListener("keydown", (e) => {
    // Slash menu gets first crack at arrows/enter/tab/esc.
    if (slashKeydown(e)) return;
    if (voiceInput.listening && e.key === "Enter") {
      e.preventDefault();
      stopVoiceInput();
      return;
    }
    // Enter sends; Shift+Enter = newline. Never send mid-IME-composition (中文输入法候选).
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing && e.keyCode !== 229) {
      e.preventDefault();
      // A bare `/command` runs directly — works even when send is disabled (no session).
      const raw = els.promptInput.value.trim();
      const name = raw && parseSlashName(raw);
      if (name) { execSlash(name); return; }
      els.sendBtn.click();
    }
  });

  // ── input shell: "+" popover · auto-grow · send-appears-on-text ──────
  const shell = $("#input-shell");
  const plusBtn = $("#plus-btn");
  const plusMenu = $("#plus-menu");
  const previewStage = $("#preview-stage");
  const assetsTray = $("#assets-tray");

  // Grow the pill past one line; reveal the ice send-disc once there is text.
  // The grow decision is measured at the NON-grown (buttons-inline) width so it
  // can't feed back on itself: measuring while grown widens the field, un-wraps
  // the text, and would flip the decision back — the boundary jitter bug. We
  // drop .is-grown, read scrollHeight synchronously (no paint between), then
  // restore the real state and size the field to the actual layout.
  function syncShell() {
    const ta = els.promptInput;
    shell.classList.remove("is-grown");
    ta.style.height = "auto";
    const grown = ta.scrollHeight > 48 || ta.value.includes("\n");  // measured at pill width
    shell.classList.toggle("is-grown", grown);
    ta.style.height = "auto";
    ta.style.height = Math.min(ta.scrollHeight, 200) + "px";
    const msg = els.promptInput.value.trim();
    const canSubmit = !!msg && !!state.sessionId;
    const showPrimary = !!state.sessionId;
    shell.classList.toggle("has-text", canSubmit);
    shell.classList.toggle("show-primary", showPrimary);
    syncComposerAction();
  }
  els.promptInput.addEventListener("input", syncShell);

  // ── voice input: browser speech recognition → editable composer text ──
  // Recognition never submits a turn. The user can review/correct the text,
  // then explicitly send it. Chrome exposes the API with a webkit prefix.
  const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  const voiceInput = {
    recognition: null,
    listening: false,
    requesting: false,
    baseText: "",
    hadResult: false,
    stopMessage: "",
    errorMessage: "",
    statusTimer: null,
  };

  function setVoiceStatus(message, persistent = false) {
    clearTimeout(voiceInput.statusTimer);
    els.voiceInputStatus.textContent = message || "";
    els.voiceInputStatus.hidden = !message;
    if (message && !persistent) {
      voiceInput.statusTimer = setTimeout(() => { els.voiceInputStatus.hidden = true; }, 4200);
    }
  }

  function joinVoiceText(base, spoken) {
    const left = String(base || "").trimEnd();
    const right = String(spoken || "").trim();
    if (!left) return right;
    if (!right) return left;
    return `${left} ${right}`;
  }

  function renderVoiceState() {
    shell.classList.toggle("is-listening", voiceInput.listening);
    els.sendBtn.classList.toggle("is-listening", voiceInput.listening);
    els.sendBtn.setAttribute("aria-pressed", String(voiceInput.listening));
    syncShell();
    render();
  }

  function stopVoiceInput(message = "语音已转成文字，请确认后发送") {
    if (!voiceInput.listening) return;
    voiceInput.stopMessage = message;
    try { voiceInput.recognition?.stop(); } catch {}
  }

  async function requestMicrophonePermission() {
    if (!navigator.mediaDevices?.getUserMedia) {
      setVoiceStatus("麦克风权限只能在 HTTPS 或 localhost 页面申请");
      return false;
    }
    voiceInput.requesting = true;
    els.sendBtn.setAttribute("aria-busy", "true");
    setVoiceStatus("正在申请麦克风权限…", true);
    let stream = null;
    try {
      // Calling getUserMedia directly from the click handler makes Chrome show
      // its permission prompt before speech recognition starts.
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      return true;
    } catch (error) {
      const messages = {
        NotAllowedError: "麦克风权限被拒绝，请在浏览器地址栏中允许后重试",
        SecurityError: "当前页面无法申请麦克风权限，请使用 HTTPS 或 localhost",
        NotFoundError: "没有检测到可用的麦克风",
        NotReadableError: "麦克风正被其他应用占用，请关闭后重试",
        AbortError: "麦克风启动失败，请重试",
      };
      setVoiceStatus(messages[error?.name] || "无法取得麦克风权限，请检查浏览器设置");
      return false;
    } finally {
      // Permission is retained by the browser; release the probe stream so the
      // speech recognizer can own the microphone without two active captures.
      stream?.getTracks().forEach((track) => track.stop());
      voiceInput.requesting = false;
      els.sendBtn.removeAttribute("aria-busy");
    }
  }

  async function startVoiceInput() {
    if (!SpeechRecognition) {
      setVoiceStatus("此浏览器不支持语音输入，请使用最新版 Chrome");
      return;
    }
    if (voiceInput.listening) { stopVoiceInput(); return; }
    if (voiceInput.requesting) return;
    if (!await requestMicrophonePermission()) return;

    const recognition = new SpeechRecognition();
    recognition.lang = document.documentElement.lang || navigator.language || "zh-CN";
    recognition.continuous = true;
    recognition.interimResults = true;
    recognition.maxAlternatives = 1;
    voiceInput.recognition = recognition;
    voiceInput.baseText = els.promptInput.value;
    voiceInput.hadResult = false;
    voiceInput.stopMessage = "";
    voiceInput.errorMessage = "";

    recognition.onstart = () => {
      voiceInput.listening = true;
      renderVoiceState();
      setVoiceStatus("正在听… 再点一次麦克风即可停止", true);
    };
    recognition.onresult = (event) => {
      let spoken = "";
      for (let i = 0; i < event.results.length; i += 1) {
        spoken += event.results[i][0]?.transcript || "";
      }
      voiceInput.hadResult = voiceInput.hadResult || Boolean(spoken.trim());
      els.promptInput.value = joinVoiceText(voiceInput.baseText, spoken);
      els.promptInput.dispatchEvent(new Event("input", { bubbles: true }));
    };
    recognition.onerror = (event) => {
      const messages = {
        "not-allowed": "麦克风权限被拒绝，请在浏览器设置中允许后重试",
        "service-not-allowed": "浏览器已禁止语音识别服务",
        "audio-capture": "没有检测到可用的麦克风",
        "no-speech": "没有听到语音，请再试一次",
        "network": "语音识别网络不可用，请检查连接后重试",
      };
      if (event.error !== "aborted" || !voiceInput.stopMessage) {
        voiceInput.errorMessage = messages[event.error] || "语音识别暂时不可用，请重试";
      }
    };
    recognition.onend = () => {
      voiceInput.listening = false;
      voiceInput.recognition = null;
      renderVoiceState();
      if (voiceInput.errorMessage) setVoiceStatus(voiceInput.errorMessage);
      else if (voiceInput.stopMessage) setVoiceStatus(voiceInput.stopMessage);
      else if (voiceInput.hadResult) setVoiceStatus("语音已转成文字，请确认后发送");
      else setVoiceStatus("语音输入已结束");
      els.promptInput.focus();
    };

    try { recognition.start(); }
    catch { setVoiceStatus("语音输入正在启动，请稍后再试"); }
  }

  if (!SpeechRecognition) {
    els.sendBtn.classList.add("is-unavailable");
    els.sendBtn.setAttribute("aria-disabled", "true");
  }
  // Starter suggestion chips (rail empty state): click fills the composer.
  document.getElementById("rail-empty")?.addEventListener("click", (e) => {
    const chip = e.target.closest(".suggest-chip");
    if (!chip) return;
    els.promptInput.value = chip.dataset.suggest || chip.textContent.trim();
    syncShell();
    els.promptInput.focus();
  });
  els.railHistory?.addEventListener("scroll", () => {
    state._followChatBottom = chatIsNearBottom();
    syncChatScrollButton();
  }, { passive: true });
  els.chatScrollBottom?.addEventListener("click", () => {
    scrollChatToBottom();
  });

  // "+" is the single entry point. Popover opens upward from the shell.
  function openPlus()  { plusMenu.hidden = false; plusBtn.setAttribute("aria-expanded", "true"); }
  function closePlus() { plusMenu.hidden = true;  plusBtn.setAttribute("aria-expanded", "false"); }
  plusBtn.addEventListener("click", (e) => { e.stopPropagation(); plusMenu.hidden ? openPlus() : closePlus(); });
  plusMenu.addEventListener("click", (e) => {
    const item = e.target.closest(".plus-item");
    if (!item) return;
    // plan / sandbox rows: forward to the MOVED real button (keeps its listener);
    // the switch is pointer-events:none so a real click always targets the row.
    // Guard: the programmatic .click() re-enters here with target===inner → skip.
    const inner = item.querySelector("#plan-toggle-btn, #sandbox-toggle-btn");
    if (inner) { if (!inner.contains(e.target)) inner.click(); return; }   // stay open — flip is visible
    const kind = item.dataset.plus;
    if (kind === "slash")    { closePlus(); els.promptInput.value = "/"; slashSync(); els.promptInput.focus(); return; }
    if (kind === "assets")   { closePlus(); toggleTray(true); return; }
    closePlus();   // upload row already fired its own listener (→ file picker)
  });
  document.addEventListener("click", (e) => {
    if (plusMenu.hidden) return;
    if (e.target.closest("#plus-menu") || e.target.closest("#plus-btn")) return;
    closePlus();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !plusMenu.hidden) { closePlus(); plusBtn.focus(); }
  });
  // Switch rows are divs (a real <button> sits inside — nesting buttons is invalid
  // HTML), so give them the keyboard side of the switch contract: Space/Enter flips.
  plusMenu.addEventListener("keydown", (e) => {
    const row = e.target.closest('.plus-item[role="menuitemcheckbox"]');
    if (!row) return;
    if (e.key === " " || e.key === "Enter") { e.preventDefault(); row.click(); }
  });
  // Keep row aria-checked in sync with the moved real buttons' state classes
  // (renderPlanUi toggles .on, renderSandbox toggles .off) without touching render().
  const syncAria = () => {
    plusMenu.querySelector('[data-plus="plan"]')
      ?.setAttribute("aria-checked", els.planBtn?.classList.contains("on") ? "true" : "false");
    plusMenu.querySelector('[data-plus="sandbox"]')
      ?.setAttribute("aria-checked", els.sandboxBtn?.classList.contains("off") ? "false" : "true");
  };
  if (els.planBtn && els.sandboxBtn) {
    const mo = new MutationObserver(syncAria);
    mo.observe(els.planBtn, { attributes: true, attributeFilter: ["class"] });
    mo.observe(els.sandboxBtn, { attributes: true, attributeFilter: ["class"] });
    syncAria();
  }

  // Left-stage timeline drawer (also mirrored on the "+" timeline switch).
  function toggleDrawer(force) {
    const open = force === undefined ? !previewStage.classList.contains("drawer-open") : force;
    if (open && !stageTabs.includes("timeline")) {
      stageTabs.push("timeline");
      saveStageTabs();
    }
    previewStage.classList.toggle("drawer-open", open);
    renderStageTabs();
  }
  // Summoned media-library tray.
  function toggleTray(open) {
    assetsTray.hidden = !open;
    return open ? fetchMediaLibrary().catch(() => {}) : Promise.resolve();
  }
  $("#assets-tray-close")?.addEventListener("click", () => toggleTray(false));
  assetsTray?.addEventListener("click", (e) => { if (e.target === assetsTray) toggleTray(false); });

  // ── workspace modules: the strip controls visibility, not exclusive pages ──
  const stagePanel = $("#stage-panel");
  const workspaceBoard = $("#workspace-board");
  const timelineDrawer = $("#timeline-drawer");
  const WorkspaceLayout = window.LumeriWorkspaceLayout;

  const STAGE_VIEWS = {
    timeline: { label: "时间线", ico: '<path d="M5 10v4M9 7v10M13 9v6M17 6v12M21 10v4"/>' },
    outline:  { label: "大纲", ico: '<rect x="3.5" y="5.5" width="17" height="13" rx="2.5"/><path d="M7 10h6M7 13.5h9.5"/>' },
    tasks:    { label: "后台任务", ico: '<circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/>' },
    files:    { label: "文件", ico: '<path d="M3.5 6.5c0-1.1.9-2 2-2h3.6c.5 0 .9.2 1.2.6l1.4 1.9H18.5c1.1 0 2 .9 2 2v8.5c0 1.1-.9 2-2 2h-13c-1.1 0-2-.9-2-2z"/>' },
    library:  { label: "素材库", ico: '<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>' },
  };
  const PREVIEW_ICO = '<rect x="3.5" y="5" width="17" height="12" rx="2.5"/><path d="M10.4 8.6l3.8 2.4-3.8 2.4z"/><path d="M8.5 20h7"/>';
  const stageTabsBox = $("#stage-tabs");
  const stageTabList = $("#stage-tab-list");
  const stageAddBtn = $("#stage-add-btn");
  const stageAddMenu = $("#stage-add-menu");
  const stageOverflowBtn = $("#stage-overflow-btn");
  const stageOverflowMenu = $("#stage-overflow-menu");
  let stageTabs = [];
  let activeTab = "preview";
  let bgActive = false;   // any session running / has pending jobs → tasks tab badge
  let didMigrateModules = false;
  const DEFAULT_MODULES = ["timeline", "outline", "tasks"];
  const PANEL_MODULES = new Set(["outline", "tasks", "files", "library"]);
  const ALL_WORKSPACE_MODULES = ["preview", "outline", "tasks", "timeline", "files", "library"];
  const WORKSPACE_ORDER_KEY = "lumeri:v3:workspace-order";
  const WORKSPACE_SIZES_KEY = "lumeri:v3:workspace-sizes";
  let workspaceOrder = [...ALL_WORKSPACE_MODULES];
  let workspaceSizes = {};
  try {
    stageTabs = JSON.parse(window.localStorage.getItem("lumeri:v3:stage-tabs") || "[]")
      .filter((k) => STAGE_VIEWS[k]);
  } catch {}
  try {
    // One-time migration from exclusive tabs to the simultaneous modular desk.
    if (window.localStorage.getItem("lumeri:v3:module-layout") !== "1") {
      stageTabs = [...new Set([...DEFAULT_MODULES, ...stageTabs])];
      window.localStorage.setItem("lumeri:v3:module-layout", "1");
      didMigrateModules = true;
    }
  } catch {
    if (!stageTabs.length) stageTabs = [...DEFAULT_MODULES];
  }
  try {
    const saved = JSON.parse(window.localStorage.getItem(WORKSPACE_ORDER_KEY) || "[]");
    const valid = Array.isArray(saved) ? saved.filter((id, i) => ALL_WORKSPACE_MODULES.includes(id) && saved.indexOf(id) === i) : [];
    workspaceOrder = [...valid, ...ALL_WORKSPACE_MODULES.filter((id) => !valid.includes(id))];
  } catch {}
  try {
    const saved = JSON.parse(window.localStorage.getItem(WORKSPACE_SIZES_KEY) || "{}");
    if (saved && typeof saved === "object") workspaceSizes = saved;
  } catch {}

  function saveStageTabs() {
    try { window.localStorage.setItem("lumeri:v3:stage-tabs", JSON.stringify(stageTabs)); } catch {}
  }
  function saveWorkspaceLayout() {
    try {
      window.localStorage.setItem(WORKSPACE_ORDER_KEY, JSON.stringify(workspaceOrder));
      window.localStorage.setItem(WORKSPACE_SIZES_KEY, JSON.stringify(workspaceSizes));
    } catch {}
  }
  function orderedStageTabs() {
    return workspaceOrder.filter((id) => stageTabs.includes(id))
      .concat(stageTabs.filter((id) => !workspaceOrder.includes(id)));
  }
  function visibleWorkspaceIds() {
    const visible = new Set(["preview"]);
    for (const id of orderedStageTabs()) {
      if (PANEL_MODULES.has(id) || (id === "timeline" && previewStage.classList.contains("drawer-open"))) visible.add(id);
    }
    return workspaceOrder.filter((id) => visible.has(id))
      .concat([...visible].filter((id) => !workspaceOrder.includes(id)));
  }
  function applyWorkspaceLayout() {
    if (!workspaceBoard || !WorkspaceLayout) return;
    const ids = visibleWorkspaceIds();
    const inset = 8;
    const bounds = {
      width: Math.max(1, workspaceBoard.clientWidth - inset * 2),
      height: Math.max(1, workspaceBoard.clientHeight - inset * 2),
      gap: 8,
    };
    const packed = WorkspaceLayout.flowModules(
      ids.map((id) => ({ id, ...WorkspaceLayout.clampSize(id, workspaceSizes[id]) })),
      bounds,
    );
    workspaceBoard.querySelectorAll("[data-workspace-module]").forEach((module) => {
      const place = packed.placements[module.dataset.workspaceModule];
      module.hidden = !place;
      if (!place) return;
      module.style.transform = `translate3d(${place.x + inset}px, ${place.y + inset}px, 0)`;
      module.style.width = `${place.width}px`;
      module.style.height = `${place.height}px`;
      if (module.dataset.workspaceModule === "timeline" && WorkspaceLayout.timelineScale) {
        const scale = WorkspaceLayout.timelineScale(place.width, place.height);
        if (module.style.getPropertyValue("--timeline-ui-scale") !== String(scale)) {
          module.style.setProperty("--timeline-ui-scale", String(scale));
          window.requestAnimationFrame(applyTimelineTrackScale);
        }
      }
    });
  }
  function hideWorkspaceModule(id) {
    stageTabs = stageTabs.filter((key) => key !== id);
    if (id === "timeline") previewStage.classList.remove("drawer-open");
    saveStageTabs();
    if (activeTab === id) activeTab = "preview";
    renderStageTabs();
  }
  if (didMigrateModules) saveStageTabs();

  function setActiveTab(k) {
    activeTab = k;
    if (k === "timeline") toggleDrawer(true);
    if (PANEL_MODULES.has(k)) refreshPanel(k);
    renderStageTabs();
  }
  function panelBodyFor(view) {
    return stagePanel?.querySelector(`[data-panel-body="${view}"]`) || null;
  }
  function refreshPanel(view = activeTab) {
    const body = panelBodyFor(view);
    if (!body) return;
    if (view === "outline") renderOutlinePanel(body);
    else if (view === "tasks") renderTasksPanel(body);
    else if (view === "files") renderFilesPanel(body);
    else if (view === "library") renderLibraryPanel(body);
  }
  let tasksPanelRefreshTimer = null;
  function scheduleTasksPanelRefresh() {
    if (tasksPanelRefreshTimer) return;
    tasksPanelRefreshTimer = setTimeout(() => {
      tasksPanelRefreshTimer = null;
      refreshPanel("tasks");
    }, 150);
  }
  function refreshVisibleModules() {
    stageTabs.filter((k) => PANEL_MODULES.has(k)).forEach(refreshPanel);
  }
  function syncWorkspaceModules() {
    if (!stagePanel) return;
    const visible = stageTabs.filter((k) => PANEL_MODULES.has(k));
    stagePanel.hidden = visible.length === 0;
    const signature = `${visible.join("|")}|tasks:${bgActive}`;
    if (stagePanel.dataset.signature !== signature) {
      stagePanel.dataset.signature = signature;
      stagePanel.innerHTML = visible.map((k) => `
        <section class="workspace-module workspace-side-module" data-workspace-module="${k}" aria-labelledby="workspace-${k}-title">
          <div class="workspace-module-head" data-module-drag="${k}" draggable="true">
            <svg class="module-drag-grip" viewBox="0 0 24 24" aria-hidden="true"><use href="#i-grip"/></svg>
            <span class="workspace-module-title" id="workspace-${k}-title">
              <svg viewBox="0 0 24 24" aria-hidden="true">${STAGE_VIEWS[k].ico}</svg><span class="label">${STAGE_VIEWS[k].label}</span>
            </span>
            ${k === "tasks" && bgActive ? `<span class="tab-badge" title="有后台任务在运行"></span>` : ""}
          <span class="workspace-module-meta">${k === "outline" ? "镜头结构" : k === "tasks" ? "运行状态" : k === "library" ? (state.librarySection === "media" ? "媒体素材" : "非媒体素材") : "只读浏览"}</span>
            <button type="button" class="workspace-module-refresh" data-module-refresh="${k}" title="刷新${STAGE_VIEWS[k].label}" aria-label="刷新${STAGE_VIEWS[k].label}"><svg viewBox="0 0 24 24" aria-hidden="true"><use href="#i-refresh"/></svg></button>
            <button type="button" class="workspace-module-close" data-module-close="${k}" title="隐藏${STAGE_VIEWS[k].label}" aria-label="隐藏${STAGE_VIEWS[k].label}"><svg viewBox="0 0 24 24" aria-hidden="true"><use href="#i-close"/></svg></button>
          </div>
          <div class="panel-tray-body" data-panel-body="${k}"><p class="placeholder">加载中…</p></div>
          <div class="module-resize-edge module-resize-edge-x" data-module-resize="${k}" data-resize-axis="x" role="separator" tabindex="0" aria-label="调整${STAGE_VIEWS[k].label}宽度"></div>
          <div class="module-resize-edge module-resize-edge-y" data-module-resize="${k}" data-resize-axis="y" role="separator" tabindex="0" aria-label="调整${STAGE_VIEWS[k].label}高度"></div>
          <div class="module-resize-edge module-resize-corner" data-module-resize="${k}" data-resize-axis="both" role="separator" tabindex="0" aria-label="同时调整${STAGE_VIEWS[k].label}宽度和高度"></div>
        </section>`).join("");
      window.queueMicrotask(refreshVisibleModules);
    }
    stagePanel.querySelectorAll("[data-workspace-module]").forEach((module) => {
      module.classList.toggle("is-focused", module.dataset.workspaceModule === activeTab);
    });
    applyWorkspaceLayout();
  }

  function renderStageTabs() {
    if (!stageTabList) return;
    const tabHtml = (k, label, ico, closable) => `
      <button type="button" class="stage-tab is-visible${activeTab === k ? " active" : ""}" data-stage-tab="${k}" role="tab" aria-selected="${activeTab === k}"${closable ? ` draggable="true" data-tab-drag="${k}"` : ""}>
        <svg viewBox="0 0 24 24" aria-hidden="true">${ico}</svg><span>${label}</span>
        ${k === "tasks" && bgActive ? `<span class="tab-badge" title="有后台任务在运行" aria-label="有后台任务在运行"></span>` : ""}
        ${closable ? `<span class="stage-tab-x" data-stage-remove="${k}" role="button" title="移除" aria-label="移除${label}">
          <svg viewBox="0 0 24 24" aria-hidden="true"><use href="#i-close"/></svg>
        </span>` : ""}
      </button>`;
    stageTabList.innerHTML =
      tabHtml("preview", "预览", PREVIEW_ICO, false)
      + orderedStageTabs().map((k) => tabHtml(k, STAGE_VIEWS[k].label, STAGE_VIEWS[k].ico, true)).join("");
    syncWorkspaceModules();
  }

  function renderStageAddMenu() {
    const avail = Object.keys(STAGE_VIEWS).filter((k) => !stageTabs.includes(k));
    stageAddMenu.innerHTML = avail.length
      ? avail.map((k) => `
          <button type="button" class="plus-item" role="menuitem" data-stage-add="${k}">
            <svg class="plus-ico" viewBox="0 0 24 24" aria-hidden="true">${STAGE_VIEWS[k].ico}</svg>
            <span class="plus-label">${STAGE_VIEWS[k].label}</span>
          </button>`).join("")
      : `<div class="stage-add-empty">已全部添加</div>`;
  }
  function openStageAdd() { renderStageAddMenu(); stageAddMenu.hidden = false; stageAddBtn.setAttribute("aria-expanded", "true"); renderStageTabs(); }
  function closeStageAdd() { stageAddMenu.hidden = true; stageAddBtn.setAttribute("aria-expanded", "false"); renderStageTabs(); }
  stageAddBtn?.addEventListener("click", (e) => {
    e.stopPropagation();
    stageAddMenu.hidden ? openStageAdd() : closeStageAdd();
  });
  document.addEventListener("click", (e) => {
    if (stageAddMenu && !stageAddMenu.hidden
        && !e.target.closest("#stage-add-menu") && !e.target.closest("#stage-add-btn")) closeStageAdd();
    if (stageOverflowMenu && !stageOverflowMenu.hidden
        && !e.target.closest("#stage-overflow-menu") && !e.target.closest("#stage-overflow-btn")) closeStageOverflow();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      if (stageAddMenu && !stageAddMenu.hidden) closeStageAdd();
      if (stageOverflowMenu && !stageOverflowMenu.hidden) closeStageOverflow();
    }
  });

  // ⌘/Ctrl + 1–9 jumps to the Nth stage tab (preview = 1). In a browser the OS
  // may intercept ⌘1–8 for its own tabs; inside the desktop shell it lands.
  document.addEventListener("keydown", (e) => {
    if (!(e.metaKey || e.ctrlKey) || e.altKey || e.shiftKey) return;
    if (!/^[1-9]$/.test(e.key)) return;
    const order = ["preview", ...stageTabs];
    const idx = Number(e.key) - 1;
    if (idx >= order.length) return;
    e.preventDefault();
    setActiveTab(order[idx]);
  });

  // Overflow "⋮": secondary actions (refresh the active view) tucked off the bar.
  function renderStageOverflow() {
    const canRefresh = PANEL_MODULES.has(activeTab);
    stageOverflowMenu.innerHTML = `
      <button type="button" class="plus-item" role="menuitem" data-overflow="refresh"${canRefresh ? "" : " disabled"}>
        <svg class="plus-ico" viewBox="0 0 24 24" aria-hidden="true"><use href="#i-refresh"/></svg>
        <span class="plus-label">刷新当前视图</span>
      </button>`;
  }
  function openStageOverflow() { renderStageOverflow(); stageOverflowMenu.hidden = false; stageOverflowBtn.setAttribute("aria-expanded", "true"); }
  function closeStageOverflow() { stageOverflowMenu.hidden = true; stageOverflowBtn.setAttribute("aria-expanded", "false"); }
  stageOverflowBtn?.addEventListener("click", (e) => {
    e.stopPropagation();
    stageOverflowMenu.hidden ? openStageOverflow() : closeStageOverflow();
  });

  stageTabsBox?.addEventListener("click", (e) => {
    const ov = e.target.closest("[data-overflow]");
    if (ov) {
      if (ov.dataset.overflow === "refresh") refreshPanel();
      closeStageOverflow();
      return;
    }
    const add = e.target.closest("[data-stage-add]");
    if (add) {
      const k = add.dataset.stageAdd;
      if (!stageTabs.includes(k)) { stageTabs.push(k); saveStageTabs(); }
      closeStageAdd();
      setActiveTab(k);
      return;
    }
    const rm = e.target.closest("[data-stage-remove]");
    if (rm) {
      e.stopPropagation();
      hideWorkspaceModule(rm.dataset.stageRemove);
      return;
    }
    const tab = e.target.closest("[data-stage-tab]");
    if (!tab) return;
    const k = tab.dataset.stageTab;
    // Re-clicking the active timeline tab toggles its drawer; other tabs are idempotent.
    if (k === "timeline" && activeTab === "timeline") { toggleDrawer(); return; }
    setActiveTab(k);
  });
  if (stageTabs.includes("timeline")) previewStage.classList.add("drawer-open");
  renderStageTabs();
  if ("ResizeObserver" in window && workspaceBoard) {
    new ResizeObserver(() => applyWorkspaceLayout()).observe(workspaceBoard);
  } else {
    window.addEventListener("resize", applyWorkspaceLayout);
  }

  // A horizontal drop means "put us side by side": cap both width weights under
  // the full-width (own-row) regime and scale the pair into one row's budget,
  // so e.g. timeline can sit next to preview instead of always owning a row.
  function ensureSideBySide(aId, bId) {
    if (!WorkspaceLayout) return;
    const rowLimit = (WorkspaceLayout.ROW_FILL_LIMIT ?? 136) - 0.5;
    const sideCap = (WorkspaceLayout.FULL_WIDTH_THRESHOLD ?? 78) - 1;
    const pair = [aId, bId].map((id) => ({ id, size: WorkspaceLayout.clampSize(id, workspaceSizes[id]) }));
    let widths = pair.map((item) => Math.min(item.size.width, sideCap));
    const sum = widths[0] + widths[1];
    if (sum > rowLimit) widths = widths.map((value) => value * rowLimit / sum);
    pair.forEach((item, index) => {
      workspaceSizes[item.id] = WorkspaceLayout.clampSize(item.id, { ...item.size, width: widths[index] });
    });
  }

  // A vertical drop means "put us one above the other". Persist both modules
  // in the full-row regime so the flow layout cannot immediately fold them
  // back into the same row. A later horizontal drop reverses this via
  // ensureSideBySide().
  function ensureStacked(aId, bId) {
    if (!WorkspaceLayout?.fullRowSize) return;
    [aId, bId].forEach((id) => {
      workspaceSizes[id] = WorkspaceLayout.fullRowSize(id, workspaceSizes[id]);
    });
  }

  // Dragging changes only module order; justified flow then re-tiles the desk
  // edge-to-edge without disturbing module contents.
  let draggedModule = null;
  let dropTarget = null;
  let dropAfter = false;
  let dropHorizontal = false;
  const clearDropState = () => {
    workspaceBoard?.querySelectorAll(".is-drop-before, .is-drop-after").forEach((el) =>
      el.classList.remove("is-drop-before", "is-drop-after"));
    workspaceBoard?.querySelectorAll("[data-drop-axis]").forEach((el) =>
      el.removeAttribute("data-drop-axis"));
    dropTarget = null;
  };
  workspaceBoard?.addEventListener("dragstart", (e) => {
    const handle = e.target.closest("[data-module-drag]");
    if (!handle) { e.preventDefault(); return; }
    draggedModule = handle.dataset.moduleDrag;
    handle.closest("[data-workspace-module]")?.classList.add("is-dragging");
    e.dataTransfer.effectAllowed = "move";
    e.dataTransfer.setData("text/plain", draggedModule);
  });
  workspaceBoard?.addEventListener("dragover", (e) => {
    if (!draggedModule) return;
    const target = e.target.closest("[data-workspace-module]");
    if (!target || target.dataset.workspaceModule === draggedModule) return;
    e.preventDefault();
    clearDropState();
    dropTarget = target.dataset.workspaceModule;
    const rect = target.getBoundingClientRect();
    const dx = (e.clientX - (rect.left + rect.width / 2)) / Math.max(1, rect.width);
    const dy = (e.clientY - (rect.top + rect.height / 2)) / Math.max(1, rect.height);
    dropHorizontal = Math.abs(dx) > Math.abs(dy);
    dropAfter = dropHorizontal ? dx > 0 : dy > 0;
    target.dataset.dropAxis = dropHorizontal ? "horizontal" : "vertical";
    target.classList.add(dropAfter ? "is-drop-after" : "is-drop-before");
  });
  workspaceBoard?.addEventListener("drop", (e) => {
    e.preventDefault();
    if (!draggedModule || !dropTarget) return;
    workspaceOrder = workspaceOrder.filter((id) => id !== draggedModule);
    const targetIndex = workspaceOrder.indexOf(dropTarget);
    workspaceOrder.splice(Math.max(0, targetIndex + (dropAfter ? 1 : 0)), 0, draggedModule);
    if (dropHorizontal) ensureSideBySide(draggedModule, dropTarget);
    else ensureStacked(draggedModule, dropTarget);
    stageTabs = orderedStageTabs();
    saveStageTabs();
    saveWorkspaceLayout();
    clearDropState();
    workspaceBoard.querySelectorAll(".is-dragging").forEach((el) => el.classList.remove("is-dragging"));
    draggedModule = null;
    renderStageTabs();
  });
  workspaceBoard?.addEventListener("dragend", () => {
    clearDropState();
    workspaceBoard.querySelectorAll(".is-dragging").forEach((el) => el.classList.remove("is-dragging"));
    draggedModule = null;
  });

  // The tab strip mirrors module drag: dragging a tab reorders workspaceOrder,
  // and the justified flow re-tiles the desk. Preview stays pinned first.
  let draggedTab = null;
  const clearTabDropState = () => {
    stageTabList?.querySelectorAll(".is-drop-before, .is-drop-after").forEach((el) =>
      el.classList.remove("is-drop-before", "is-drop-after"));
  };
  stageTabList?.addEventListener("dragstart", (e) => {
    const tab = e.target.closest("[data-tab-drag]");
    if (!tab) { e.preventDefault(); return; }
    draggedTab = tab.dataset.tabDrag;
    tab.classList.add("is-dragging");
    e.dataTransfer.effectAllowed = "move";
    e.dataTransfer.setData("text/plain", draggedTab);
  });
  stageTabList?.addEventListener("dragover", (e) => {
    if (!draggedTab) return;
    const target = e.target.closest("[data-tab-drag]");
    if (!target || target.dataset.tabDrag === draggedTab) return;
    e.preventDefault();
    clearTabDropState();
    const rect = target.getBoundingClientRect();
    target.classList.add(e.clientX > rect.left + rect.width / 2 ? "is-drop-after" : "is-drop-before");
  });
  stageTabList?.addEventListener("drop", (e) => {
    const target = e.target.closest("[data-tab-drag]");
    if (!draggedTab || !target || target.dataset.tabDrag === draggedTab) return;
    e.preventDefault();
    const rect = target.getBoundingClientRect();
    const after = e.clientX > rect.left + rect.width / 2;
    workspaceOrder = workspaceOrder.filter((id) => id !== draggedTab);
    const targetIndex = workspaceOrder.indexOf(target.dataset.tabDrag);
    workspaceOrder.splice(Math.max(0, targetIndex + (after ? 1 : 0)), 0, draggedTab);
    stageTabs = orderedStageTabs();
    saveStageTabs();
    saveWorkspaceLayout();
    draggedTab = null;
    renderStageTabs();
  });
  stageTabList?.addEventListener("dragend", () => {
    clearTabDropState();
    stageTabList.querySelectorAll(".is-dragging").forEach((el) => el.classList.remove("is-dragging"));
    draggedTab = null;
  });

  // Edges resize in continuous percentages. The justified flow immediately
  // gives the released space to neighbours, keeping the desk fully tiled.
  let resizeState = null;
  workspaceBoard?.addEventListener("pointerdown", (e) => {
    const edge = e.target.closest("[data-module-resize]");
    if (!edge || !WorkspaceLayout) return;
    e.preventDefault();
    e.stopPropagation();
    edge.focus({ preventScroll: true });
    const id = edge.dataset.moduleResize;
    const size = WorkspaceLayout.clampSize(id, workspaceSizes[id]);
    resizeState = {
      id, axis: edge.dataset.resizeAxis, startX: e.clientX, startY: e.clientY, size,
      boardWidth: Math.max(1, workspaceBoard.clientWidth - 16),
      boardHeight: Math.max(1, workspaceBoard.clientHeight - 16),
    };
    workspaceBoard.classList.add("is-resizing");
    edge.closest("[data-workspace-module]")?.classList.add("is-resizing");
    edge.setPointerCapture?.(e.pointerId);
  });
  document.addEventListener("pointermove", (e) => {
    if (!resizeState || !WorkspaceLayout) return;
    const next = { ...resizeState.size };
    if (resizeState.axis === "x" || resizeState.axis === "both") {
      next.width += (e.clientX - resizeState.startX) / resizeState.boardWidth * 100;
    }
    if (resizeState.axis === "y" || resizeState.axis === "both") {
      next.height += (e.clientY - resizeState.startY) / resizeState.boardHeight * 100;
    }
    workspaceSizes[resizeState.id] = WorkspaceLayout.clampSize(resizeState.id, next);
    applyWorkspaceLayout();
  });
  document.addEventListener("pointerup", () => {
    if (!resizeState) return;
    workspaceBoard?.querySelector(`[data-workspace-module="${resizeState.id}"]`)?.classList.remove("is-resizing");
    workspaceBoard?.classList.remove("is-resizing");
    resizeState = null;
    saveWorkspaceLayout();
  });
  workspaceBoard?.addEventListener("keydown", (e) => {
    const edge = e.target.closest("[data-module-resize]");
    if (!edge || !WorkspaceLayout || !["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"].includes(e.key)) return;
    const axis = edge.dataset.resizeAxis;
    const horizontalKey = ["ArrowLeft", "ArrowRight"].includes(e.key);
    if ((horizontalKey && axis === "y") || (!horizontalKey && axis === "x")) return;
    e.preventDefault();
    const id = edge.dataset.moduleResize;
    const size = WorkspaceLayout.clampSize(id, workspaceSizes[id]);
    const step = e.shiftKey ? 5 : 2;
    if (horizontalKey) size.width += e.key === "ArrowRight" ? step : -step;
    else size.height += e.key === "ArrowDown" ? step : -step;
    workspaceSizes[id] = WorkspaceLayout.clampSize(id, size);
    saveWorkspaceLayout();
    applyWorkspaceLayout();
  });
  workspaceBoard?.addEventListener("click", (e) => {
    const close = e.target.closest("[data-module-close]");
    if (close) { e.stopPropagation(); hideWorkspaceModule(close.dataset.moduleClose); return; }
    const refresh = e.target.closest("[data-module-refresh]");
    if (refresh) { e.stopPropagation(); refreshPanel(refresh.dataset.moduleRefresh); return; }
    const module = e.target.closest("[data-workspace-module]");
    if (module) setActiveTab(module.dataset.workspaceModule);
  });

  // Live badge on the 后台任务 tab: a slow, visibility-gated /sessions poll
  // flips bgActive when any session is mid-turn or has pending generation jobs.
  async function pollBgTasks() {
    if (document.visibilityState === "hidden") return;
    let active = false;
    try {
      const r = await apiFetch("/sessions?compact=1");
      if (r.ok) {
        const sessions = (await r.json()).sessions || [];
        active = sessions.some((s) => s.turn_in_progress || (s.pending_jobs || []).length > 0);
      }
    } catch {}
    active = active || [...state.backgroundTasks.values()].some((t) => t.status === "running");
    if (active !== bgActive) { bgActive = active; renderStageTabs(); }
  }
  pollBgTasks();
  window.setInterval(pollBgTasks, 12000);

  const fmtBytes = (n) => {
    if (!Number.isFinite(n)) return "";
    if (n >= 1 << 30) return (n / (1 << 30)).toFixed(1) + " GB";
    if (n >= 1 << 20) return (n / (1 << 20)).toFixed(1) + " MB";
    if (n >= 1024) return Math.round(n / 1024) + " KB";
    return n + " B";
  };
  const fmtAgo = (epoch) => {
    if (!epoch) return "";
    const s = Math.max(0, (Date.now() / 1000) - epoch);
    if (s < 60) return "刚刚";
    if (s < 3600) return Math.floor(s / 60) + " 分钟前";
    if (s < 86400) return Math.floor(s / 3600) + " 小时前";
    return Math.floor(s / 86400) + " 天前";
  };

  // ── outline panel: the shotlist riding /timeline ──────────────────────
  const SHOT_STATUS = { draft: ["草稿", ""], filled: ["已配素材", "filled"], placed: ["已上时间线", "placed"] };
  async function renderOutlinePanel(body) {
    if (!body) return;
    if (!state.sessionId) { body.innerHTML = `<p class="placeholder">暂无会话</p>`; return; }
    let sl = null;
    try {
      const r = await apiFetch(`/sessions/${state.sessionId}/timeline`);
      if (r.ok) sl = (await r.json()).shotlist;
    } catch {}
    if (!body.isConnected || !stageTabs.includes("outline")) return;
    const scenes = (sl && Array.isArray(sl.scenes)) ? sl.scenes : [];
    const shotCount = scenes.reduce((n, sc) => n + ((sc.shots || []).length), 0);
    if (!shotCount) { body.innerHTML = `<p class="placeholder">暂无大纲 — 让 Lumeri 起草分镜后在这里查看</p>`; return; }
    let html = "";
    if (sl.logline) html += `<p class="outline-logline">${escapeHTML(sl.logline)}</p>`;
    let no = 0;
    for (const sc of scenes) {
      if (sc.title) html += `<div class="outline-scene" data-scene-id="${escapeHTML(sc.id)}">${escapeHTML(sc.title)}</div>`;
      for (const shot of (sc.shots || [])) {
        no += 1;
        const st = SHOT_STATUS[shot.status] || SHOT_STATUS.draft;
        const meta = [
          `${Number(shot.duration_sec || 0).toFixed(1)}s`,
          shot.narration ? `旁白：${shot.narration}` : "",
          shot.on_screen_text ? `字幕：${shot.on_screen_text}` : "",
          shot.mood || "",
        ].filter(Boolean).join(" · ");
        html += `
          <div class="outline-row" data-shot-id="${escapeHTML(shot.id)}">
            <span class="outline-no ${st[1]}" title="${st[0]}">${no}</span>
            <span class="outline-main">
              <span class="outline-beat">${escapeHTML(shot.description || "(未命名镜头)")}</span>
              ${meta ? `<span class="outline-meta">${escapeHTML(meta)}</span>` : ""}
            </span>
          </div>`;
      }
    }
    body.innerHTML = html;
  }

  // ── background tasks panel: group active work by Project ──────────────
  function creatorTaskLabel(summary, kind = "") {
    const text = String(summary || "").trim();
    const haystack = `${kind} ${text}`.toLowerCase();
    const labels = [
      [/(render|export|encode|transcod|ffmpeg|渲染|导出|转码)/, "正在渲染画面"],
      [/(generate[_ -]?video|video generation|生成视频)/, "正在生成视频"],
      [/(generate[_ -]?image|image generation|生成图像)/, "正在生成图像"],
      [/(generate[_ -]?audio|voice|speech|生成音频|配音)/, "正在处理声音"],
      [/(download|fetch|stock media|下载|获取素材|搜索素材)/, "正在准备素材"],
      [/(test|check|verify|inspect|probe|测试|检查|验证|审查)/, "正在检查结果"],
      [/(build|compile|install|构建|编译|安装)/, "正在准备项目"],
      [/(analyse|analyze|detect|track|分析|识别|跟踪)/, "正在分析画面"],
    ];
    const internal = /(^|\s)(local:|python\d*|node|bash|zsh|sh|uv|npm|pnpm|ffmpeg|git)(\s|$|:)|[\\/]|--?[a-z\d_-]+|[;&|`$<>]|\b(?:job|shell|build)_[a-z\d_-]+\b/i;
    const hasChinese = /[\u3400-\u9fff]/.test(text);
    if (text && hasChinese && !internal.test(text)) return text.slice(0, 32);
    const match = labels.find(([pattern]) => pattern.test(haystack));
    if (match) return match[1];
    if (/subagent|agent/.test(String(kind).toLowerCase())) return "正在协助处理";
    return "正在处理创作任务";
  }

  function creatorTaskStatus(status) {
    return ({
      submitted: "准备中",
      pending: "等待中",
      running: "进行中",
      killing: "停止中…",
      done: "已完成",
      failed: "失败",
    })[String(status || "").toLowerCase()] || "进行中";
  }

  async function renderTasksPanel(body) {
    if (!body) return;
    let sessions = null;
    try {
      const r = await apiFetch("/sessions?compact=1");
      if (r.ok) sessions = (await r.json()).sessions;
    } catch {}
    if (!body.isConnected || !stageTabs.includes("tasks")) return;
    if (!Array.isArray(sessions)) { body.innerHTML = `<p class="placeholder">读取失败</p>`; return; }
    const activeSessions = sessions.filter((s) => (
      s.turn_in_progress
      || (s.pending_jobs || []).length > 0
      || (s.active_subagents || []).length > 0
    ));
    if (!activeSessions.length) { body.innerHTML = `<p class="placeholder">暂无进行中的后台任务</p>`; return; }
    const missingProjectName = activeSessions.some((session) => (
      session.project_id && !projectSidebarState.projectNames.has(String(session.project_id))
    ));
    if (missingProjectName) {
      try {
        const knownProjects = await fetchProjects();
        projectSidebarState.projectNames = projectNamesFrom(knownProjects);
      } catch {}
    }

    const projects = new Map();
    activeSessions.forEach((session) => {
      const projectId = String(session.project_id || "");
      const key = projectId || `session:${session.session_id}`;
      const group = projects.get(key) || { projectId, sessions: [] };
      group.sessions.push(session);
      projects.set(key, group);
    });
    body.innerHTML = [...projects.values()].map((group) => {
      const currentProject = !!(
        (group.projectId && group.projectId === state.projectId)
        || group.sessions.some((session) => session.session_id === state.sessionId)
      );
      const knownProjectName = projectSidebarState.projectNames.get(group.projectId) || "";
      const currentProjectName = state.projectName && state.projectName !== group.projectId
        ? state.projectName
        : "";
      const projectLabel = currentProject
        ? (knownProjectName || currentProjectName || "当前 Project")
        : (knownProjectName || (group.projectId ? "其他 Project" : "独立会话"));
      const rows = group.sessions.map((s) => {
        const mine = s.session_id === state.sessionId;
        const sessionRow = s.turn_in_progress ? `
          <div class="task-row task-session">
            <span class="task-dot running"></span>
            <span class="task-main">
              <span class="task-name">会话${mine ? " · 当前" : ""}</span>
              <span class="task-sub">执行中${s.plan_mode ? " · 计划模式" : ""} · ${fmtAgo(s.last_used_at)}</span>
            </span>
          </div>` : "";
        const jobs = (s.pending_jobs || []).map((j) => `
          <div class="task-row task-job">
            <span class="task-dot ${j.last_polled_status === "failed" ? "failed" : "running"}"></span>
            <span class="task-main">
              <span class="task-name">${escapeHTML(creatorTaskLabel(j.summary, j.kind))}</span>
              <span class="task-sub">${escapeHTML(creatorTaskStatus(j.last_polled_status))}</span>
            </span>
          </div>`).join("");
        const subagents = (s.active_subagents || []).map((agent) => `
          <div class="task-row task-subagent">
            <span class="task-dot running"></span>
            <span class="task-main">
              <span class="task-name"><span class="task-kind">子代理</span>${escapeHTML(creatorTaskLabel(agent.goal, "subagent"))}</span>
              <span class="task-sub">正在协助处理</span>
            </span>
          </div>`).join("");
        const shellJobs = mine ? renderShellJobRows() : "";
        return sessionRow + jobs + subagents + shellJobs;
      }).join("");
      return `
        <section class="task-project" data-project-id="${escapeHTML(group.projectId)}">
          <div class="task-project-head">
            <span class="task-project-label">${escapeHTML(projectLabel)}</span>
            <span class="task-project-count">${group.sessions.length} 个会话</span>
          </div>
          <div class="task-project-rows">${rows}</div>
        </section>`;
    }).join("");
  }

  // Current session's background shell jobs (run_shell run_in_background),
  // rendered inside the tasks module with a per-running-row kill button.
  function renderShellJobRows() {
    if (!state.backgroundTasks.size) return "";
    const statusText = (t) => {
      if (t._killing) return "停止中…";
      if (t.status === "running") return "进行中";
      if (t.status === "done") return t.exit_code === 0 || t.exit_code == null ? "完成" : `完成 (退出码 ${t.exit_code})`;
      if (t.status === "failed") return t.error === "killed by kill_job" ? "已停止" : "失败";
      return t.status || "";
    };
    return [...state.backgroundTasks.values()].filter((t) => t.status === "running" || t._killing).map((t) => `
      <div class="task-row task-job" data-job-id="${escapeHTML(t.job_id)}">
        <span class="task-dot ${t.status === "failed" ? "failed" : t.status === "done" ? "done" : "running"}"></span>
        <span class="task-main">
          <span class="task-name">${escapeHTML(creatorTaskLabel(t.summary, "shell"))}</span>
          <span class="task-sub">${escapeHTML(statusText(t))}${t.elapsed_sec != null ? ` · ${Math.round(t.elapsed_sec)}s` : ""}</span>
        </span>
        ${t.status === "running" && !t._killing
          ? `<button type="button" class="task-kill" data-task-kill="${escapeHTML(t.job_id)}">停止</button>`
          : ""}
      </div>`).join("");
  }

  async function killBackgroundTask(jobId) {
    const t = state.backgroundTasks.get(jobId);
    if (!t || !state.sessionId) return;
    t._killing = true;
    scheduleTasksPanelRefresh();
    try {
      const r = await apiFetch(
        `/sessions/${state.sessionId}/tasks/${encodeURIComponent(jobId)}/kill`,
        { method: "POST" },
      );
      if (!r.ok) throw new Error(`kill failed: ${r.status}`);
    } catch (err) {
      t._killing = false;
      state.errors.push(`停止后台任务失败: ${err.message}`);
      scheduleTasksPanelRefresh();
      render();
    }
  }

  document.addEventListener("click", (event) => {
    const btn = event.target.closest?.("[data-task-kill]");
    if (btn) killBackgroundTask(btn.getAttribute("data-task-kill"));
  });

  function hydrateHistoryMessages(session) {
    state.turns = [];
    state.currentTurn = null;
    state.sessionTitle = session.title || null;
    state.userMessageCount = 0;
    els.sessionLabel.textContent = state.sessionTitle || state.sessionId || "—";
    const msgs = session.messages || [];
    let currentTurn = null;
    for (const msg of msgs) {
      if (msg.role === "user") {
        currentTurn = newTurn(msg.content || "", msg.timestamp);
        state.turns.push(currentTurn);
        state.userMessageCount++;
      } else if (msg.role === "status" && msg.statusType === "guidance" && currentTurn) {
        currentTurn.guidance.push(msg.content || "");
      } else if (msg.role === "status" && currentTurn) {
        currentTurn.assistantText = msg.content || "";
        currentTurn.completedAt = Number(msg.timestamp) || Date.now();
        currentTurn.complete = true;
      }
    }
    state.currentTurn = state.turns[state.turns.length - 1] || null;
    if (state.sessionId && Array.isArray(session.timeline_markers)) {
      const markers = session.timeline_markers.filter((marker) => (
        marker && Number.isFinite(Number(marker.time)) && Number(marker.time) >= 0
      )).slice(0, 500);
      try { window.localStorage.setItem(markerKey(), JSON.stringify(markers)); } catch {}
      TL.markers = markers;
    }
  }

  async function restoreHistoryRecord(session) {
    if (!session.project_id) {
      const activationSeq = ++runtimeActivationSeq;
      if (state.sessionId) await autoSaveSession();
      await detachRuntime();
      if (activationSeq !== runtimeActivationSeq) return false;
      resetRuntimeView();
      state.sessionId = null;
      state.chatOnly = true;
      state.activeHistoryId = session.id || null;
      hydrateHistoryMessages(session);
      render();
      return true;
    }

    if (!session.v3_session_id || !session.run_id) {
      throw new Error("历史工程缺少 session/run 关联，已拒绝创建替代工程。");
    }
    const resumed = await resumeSession(session.v3_session_id, {
      project_id: session.project_id,
      run_id: session.run_id,
      hydrateHistory: () => {
        if (!restoreCachedSessionView(session.v3_session_id)) {
          hydrateHistoryMessages(session);
        }
      },
    });
    if (!resumed) return false;
    await autoSaveSession();
    render();
    return true;
  }

  async function loadHistorySession(snapshotId) {
    try {
      const r = await apiFetch(`/session-history/${encodeURIComponent(snapshotId)}`);
      if (!r.ok) return false;
      const session = await r.json();
      return restoreHistoryRecord(session);
    } catch (err) {
      state.errors.push(`history restore failed: ${err.message}`);
      if (state.currentTurn) {
        state.currentTurn.banners.push({ kind: "turn_error", text: "历史工程未能恢复；没有切换到一个假的空工程。" });
      }
      render();
      return false;
    }
  }

  // ── files panel: whitelisted read-only browser (/files/*) ────────────
  let filesState = null;   // null = root picker; else {root, session, path}
  const FILE_ICON = (name) => {
    const ext = (name.split(".").pop() || "").toLowerCase();
    if (["mp4", "mov", "webm", "mkv", "avi"].includes(ext)) return "i-film";
    if (["png", "jpg", "jpeg", "gif", "webp", "svg", "bmp"].includes(ext)) return "i-image";
    if (["mp3", "wav", "m4a", "aac", "flac", "ogg"].includes(ext)) return "i-music";
    return "i-file";
  };
  async function renderFilesPanel(body) {
    if (!body) return;
    if (isTesterManagedWorkspace()) {
      body.innerHTML = `<p class="placeholder">测试版仅显示当前会话已登记的产物</p>`;
      return;
    }
    if (!filesState) {
      let roots = [];
      try {
        const r = await apiFetch("/files/roots");
        if (r.ok) roots = (await r.json()).roots || [];
      } catch {}
      if (!body.isConnected || !stageTabs.includes("files")) return;
      let html = "";
      if (state.sessionId) {
        if (state.projectId && state.projectSourceRoot) {
          html += `<button type="button" class="file-row" data-file-root="project_source">
            <svg viewBox="0 0 24 24" aria-hidden="true"><use href="#i-folder"/></svg>
            <span class="file-name">Project 源文件夹</span></button>`;
        }
        if (state.projectId) {
          html += `<button type="button" class="file-row" data-file-root="project_edit">
            <svg viewBox="0 0 24 24" aria-hidden="true"><use href="#i-folder"/></svg>
            <span class="file-name">Lumeri 剪辑目录</span></button>`;
        }
        html += `<button type="button" class="file-row" data-file-root="session">
          <svg viewBox="0 0 24 24" aria-hidden="true"><use href="#i-folder"/></svg>
          <span class="file-name">当前会话工作区</span></button>`;
      }
      html += roots.map((rt) => `
        <button type="button" class="file-row" data-file-root="${escapeHTML(rt.key)}">
          <svg viewBox="0 0 24 24" aria-hidden="true"><use href="#i-folder"/></svg>
          <span class="file-name">${escapeHTML(rt.label)}</span></button>`).join("");
      body.innerHTML = html || `<p class="placeholder">暂无可浏览目录</p>`;
      return;
    }
    const { root, session, path } = filesState;
    const qs = `root=${encodeURIComponent(root)}&path=${encodeURIComponent(path)}${session ? `&session=${encodeURIComponent(session)}` : ""}`;
    let data = null;
    try {
      const r = await apiFetch(`/files/list?${qs}`);
      if (r.ok) data = await r.json();
    } catch {}
    if (!body.isConnected || !stageTabs.includes("files")) return;
    if (!data) { body.innerHTML = `<p class="placeholder">读取失败</p>`; return; }
    const segs = path ? path.split("/") : [];
    const crumbs = [`<button type="button" data-file-crumb="">${escapeHTML(root === "session" ? "工作区" : root)}</button>`]
      .concat(segs.map((seg, i) =>
        `<span>/</span><button type="button" data-file-crumb="${escapeHTML(segs.slice(0, i + 1).join("/"))}">${escapeHTML(seg)}</button>`))
      .join("");
    const rows = (data.entries || []).map((en) => {
      const child = path ? `${path}/${en.name}` : en.name;
      return en.is_dir
        ? `<button type="button" class="file-row" data-file-dir="${escapeHTML(child)}">
            <svg viewBox="0 0 24 24" aria-hidden="true"><use href="#i-folder"/></svg>
            <span class="file-name">${escapeHTML(en.name)}</span></button>`
        : `<button type="button" class="file-row" data-file-open="${escapeHTML(child)}">
            <svg viewBox="0 0 24 24" aria-hidden="true"><use href="#${FILE_ICON(en.name)}"/></svg>
            <span class="file-name">${escapeHTML(en.name)}</span>
            <span class="file-size">${fmtBytes(en.size)}</span></button>`;
    }).join("");
    body.innerHTML = `
      <div class="files-crumbs"><button type="button" data-file-crumb="__roots__" title="所有目录"><svg viewBox="0 0 24 24" aria-hidden="true" style="width:12px;height:12px"><use href="#i-chevron-l"/></svg></button>${crumbs}</div>
      ${rows || `<p class="placeholder">空目录</p>`}
      ${data.truncated ? `<p class="placeholder">（仅显示前 500 项）</p>` : ""}`;
  }
  async function renderLibraryPanel(body) {
    if (!body) return;
    const sessionId = state.sessionId;
    const activationSeq = runtimeActivationSeq;
    const isCurrentLibraryPanel = () => (
      body.isConnected
      && stageTabs.includes("library")
      && state.sessionId === sessionId
      && runtimeActivationSeq === activationSeq
    );
    if (state.mediaLibraryStatus === "idle" || state.mediaLibraryStatus === "error") {
      body.innerHTML = `${librarySectionsHtml()}<p class="placeholder">加载中…</p>`;
      await fetchMediaLibrary();
      if (!isCurrentLibraryPanel()) return;
    }
    if (state.mediaLibraryStatus === "loading") {
      body.innerHTML = `${librarySectionsHtml()}<p class="placeholder">加载中…</p>`;
      return;
    }
    if (state.mediaLibraryStatus === "signed-out" && state.librarySection === "media") {
      body.innerHTML = `${librarySectionsHtml()}<p class="placeholder">本地素材库暂不可用</p>`;
      return;
    }
    const visibleAssets = libraryAssetsForSection();
    if (!visibleAssets.length) {
      body.innerHTML = `${librarySectionsHtml()}<p class="placeholder">${state.librarySection === "media" ? "暂无媒体素材" : "暂无非媒体素材"}</p>`;
      return;
    }
    const roughcutIds = visibleAssets
      .filter((asset) => {
        if (state.librarySection !== "media") return false;
        const assetId = asset.asset_id || asset.id || "";
        const summary = asset.annotation_summary || {};
        const tags = [...(summary.tags || []), ...(summary.labels || [])];
        return assetId && tags.includes("roughcut") && !state.roughcutManifests.has(assetId);
      })
      .map((asset) => asset.asset_id || asset.id);
    if (roughcutIds.length) {
      await Promise.all(roughcutIds.map((assetId) => loadRoughcutManifest(assetId).catch(() => null)));
      if (!isCurrentLibraryPanel()) return;
    }
    if (state.librarySection === "non-media") {
      body.innerHTML = `${librarySectionsHtml()}${renderNonMediaLibraryCards(visibleAssets)}`;
      syncLibrarySectionUi();
      scrollFocusedLibraryAsset();
      return;
    }
    const cards = visibleAssets.map((asset) => {
      const assetId = asset.asset_id || asset.id || "";
      const kind = asset.media_kind || "media";
      const kindLabel = LIBRARY_KIND_LABEL[kind] || "素材";
      const title = libraryDisplayName(asset, kindLabel);
      const summary = asset.annotation_summary || {};
      const allTags = [...(summary.tags || []), ...(summary.labels || [])];
      const shownTags = allTags.slice(0, 2);
      const moreTags = allTags.length - shownTags.length;
      const thumb = asset.thumbnail_src
        ? `<img class="library-thumb" src="${escapeHTML(asset.thumbnail_src)}" alt="" loading="lazy" />`
        : `<div class="library-thumb blank" aria-hidden="true"><svg viewBox="0 0 24 24"><use href="#${LIBRARY_KIND_ICON[kind] || "i-file"}"/></svg></div>`;
      const tagsHtml = shownTags.length
        ? `<div class="library-tags">${shownTags.map((t) => `<span>${escapeHTML(t)}</span>`).join("")}${moreTags > 0 ? `<span>+${moreTags}</span>` : ""}</div>`
        : "";
      const dur = kind !== "image" && formatMediaDuration(asset.duration);
      const roughcut = state.roughcutManifests.get(assetId);
      return `
        <div class="library-card" data-library-asset="${escapeHTML(assetId)}" title="${escapeHTML(asset.name || assetId)}">
          ${thumb}
          <div class="library-card-body">
            <div class="library-title">${escapeHTML(title)}</div>
            <div class="library-meta">${escapeHTML(kindLabel)}${dur ? " · " + escapeHTML(dur) : ""}</div>
            ${tagsHtml}
            ${asset.tester_session_asset ? "" : `<div class="library-card-actions">
              ${kind === "video" || kind === "audio" ? `<button type="button" class="library-small-btn icon-btn" title="粗剪准备" aria-label="粗剪准备" data-panel-lib-roughcut="${escapeHTML(assetId)}"><svg viewBox="0 0 24 24" aria-hidden="true"><use href="#i-wand"/></svg></button>` : ""}
              <button type="button" class="library-small-btn icon-btn" title="标注" aria-label="标注" data-panel-lib-annotate="${escapeHTML(assetId)}"><svg viewBox="0 0 24 24" aria-hidden="true"><use href="#i-wand"/></svg></button>
              <button type="button" class="library-small-btn icon-btn" title="标记" aria-label="标记" data-panel-lib-load="${escapeHTML(assetId)}"><svg viewBox="0 0 24 24" aria-hidden="true"><use href="#i-marker"/></svg></button>
            </div>`}
            ${roughcut ? renderRoughcutReview(asset, roughcut) : ""}
          </div>
        </div>`;
    }).join("");
    body.innerHTML = `${librarySectionsHtml()}${cards}`;
    syncLibrarySectionUi();
    wireRoughcutControls(body);
  }

  stagePanel?.addEventListener("click", (e) => {
    const libRoughcut = e.target.closest("[data-panel-lib-roughcut]");
    if (libRoughcut) { startRoughcutPreparation(libRoughcut.dataset.panelLibRoughcut).catch(() => {}); return; }
    const libAnnotate = e.target.closest("[data-panel-lib-annotate]");
    if (libAnnotate) { annotateLibraryAsset(libAnnotate.dataset.panelLibAnnotate).catch(() => {}); return; }
    const libLoad = e.target.closest("[data-panel-lib-load]");
    if (libLoad) { Promise.all([loadMediaAnnotations(libLoad.dataset.panelLibLoad), loadRoughcutManifest(libLoad.dataset.panelLibLoad)]).then(() => refreshPanel("library")).catch(() => {}); return; }
    const roughcutReview = e.target.closest("[data-roughcut-review]");
    if (roughcutReview) { reviewRoughcut(roughcutReview).then(() => refreshPanel("library")).catch(() => {}); return; }
    const roughcutSeek = e.target.closest("[data-roughcut-seek]");
    if (roughcutSeek) {
      const player = roughcutSeek.closest(".library-card")?.querySelector(".roughcut-preview");
      if (player) { player.currentTime = Number(roughcutSeek.dataset.roughcutSeek || 0); player.play().catch(() => {}); }
      return;
    }
    const rootBtn = e.target.closest("[data-file-root]");
    if (rootBtn) {
      const key = rootBtn.dataset.fileRoot;
      const sessionBound = ["session", "project_source", "project_edit"].includes(key);
      filesState = { root: key, session: sessionBound ? state.sessionId : "", path: "" };
      refreshPanel("files");
      return;
    }
    const crumb = e.target.closest("[data-file-crumb]");
    if (crumb) {
      if (crumb.dataset.fileCrumb === "__roots__") filesState = null;
      else filesState = { ...filesState, path: crumb.dataset.fileCrumb };
      refreshPanel("files");
      return;
    }
    const dir = e.target.closest("[data-file-dir]");
    if (dir) { filesState = { ...filesState, path: dir.dataset.fileDir }; refreshPanel("files"); return; }
    const file = e.target.closest("[data-file-open]");
    if (file) {
      const { root, session } = filesState;
      const qs = `root=${encodeURIComponent(root)}&path=${encodeURIComponent(file.dataset.fileOpen)}${session ? `&session=${encodeURIComponent(session)}` : ""}`;
      window.open(`/files/get?${qs}`, "_blank", "noopener");
    }
  });

  // Visible live modules refresh together; files/history remain stable while
  // the user is reading or navigating them.
  window.setInterval(() => {
    if (document.visibilityState === "hidden") return;
    if (stageTabs.includes("outline")) refreshPanel("outline");
    if (stageTabs.includes("tasks")) refreshPanel("tasks");
  }, 5000);

  window.setInterval(() => {
    if (document.visibilityState === "hidden" || !state.turnInProgress) return;
    render();
  }, 1000);

  // First-run discovery pulse on "+" (controls are hidden behind it now).
  try {
    if (!window.localStorage.getItem("lumeri:v3:plus-seen")) {
      plusBtn.classList.add("pulse");
      window.localStorage.setItem("lumeri:v3:plus-seen", "1");
    }
  } catch {}

  // ── timeline quick-action buttons ──────────────────────────────────
  // Any .pt-action-btn with a data-cmd attribute pre-fills the prompt and sends.
  document.addEventListener("click", (e) => {
    const btn = e.target.closest(".pt-action-btn[data-cmd]");
    if (!btn || !state.sessionId || state.turnInProgress) return;
    const cmd = btn.dataset.cmd;
    if (!cmd) return;
    els.promptInput.value = cmd;
    els.sendBtn.click();
  });

  // ── plan mode ───────────────────────────────────────────────────────
  // The backend answers with the authoritative state AND broadcasts a
  // plan_mode_changed SSE event, so other connected clients (e.g. the CLI on
  // the same session) stay in sync.
  async function setPlanMode(enabled) {
    if (!state.sessionId) return;
    const r = await apiFetch(`/sessions/${state.sessionId}/plan_mode`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled }),
    });
    if (!r.ok) throw new Error(`plan_mode toggle failed: ${r.status}`);
    const data = await r.json();
    state.planMode = !!data.plan_mode;
    if (!state.planMode) state.planReady = false;
    render();
  }

  els.planBtn?.addEventListener("click", () => {
    setPlanMode(!state.planMode).catch((err) => {
      state.errors.push(err.message);
      render();
    });
  });

  const PLAN_APPROVE_MESSAGE = "计划已批准，请立即按计划执行。(Plan approved — execute it now.)";

  document.addEventListener("click", (e) => {
    if (e.target.closest("[data-plan-approve]")) {
      if (state.turnInProgress) return;
      setPlanMode(false)
        .then(() => submitTurn(PLAN_APPROVE_MESSAGE))
        .catch((err) => {
          state.errors.push(`approve plan failed: ${err.message}`);
          render();
        });
      return;
    }
    if (e.target.closest("[data-plan-dismiss]")) {
      state.planReady = false;
      render();
    }
  });

  // ── sandbox toggle ──────────────────────────────────────────────────
  function renderSandbox(disabled) {
    els.sandboxBtn.classList.toggle("off", disabled);
    els.sandboxBtn.textContent = disabled ? "沙盒关闭" : "沙盒";
    els.sandboxBtn.title = disabled ? "沙盒已关闭，代码可访问完整系统（点击重新开启）" : "沙盒已开启（点击关闭）";
  }
  async function syncSandbox() {
    try {
      const r = await apiFetch("/settings/sandbox");
      if (r.ok) renderSandbox(!!(await r.json()).sandbox_disabled);
    } catch {}
  }
  els.sandboxBtn.addEventListener("click", async () => {
    const next = !els.sandboxBtn.classList.contains("off");
    try {
      const r = await apiFetch("/settings/sandbox", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ disabled: next }),
      });
      if (r.ok) renderSandbox(!!(await r.json()).sandbox_disabled);
    } catch (err) {
      state.errors.push(`sandbox toggle failed: ${err.message}`);
      render();
    }
  });
  syncSandbox();
  setupTimelineDirectEdit();



  async function restoreCurrentSessionOrCreate() {
    let saved = null;
    try {
      const r = await apiFetch("/session-history");
      if (r.ok) {
        saved = await r.json();
      }
    } catch {}
    // Failure to read any history may start a new project.  Once a persisted
    // production/chat record was read, however, its restore error must escape
    // to the boot error UI; silently replacing it would orphan real work.
    if (saved?.project_id || (saved?.messages || []).length) {
      return restoreHistoryRecord(saved);
    }
    return createSession();
  }

  // Normal Web boot restores the durable current project. CLI preview only
  // observes the explicit live session supplied by the terminal.
  const boot = isCliPreview ? attachSession(cliPreviewSessionId) : restoreCurrentSessionOrCreate();
  if (!isCliPreview) {
    bindProjectSidebarInteractions();
    renderProjectSidebar();
  }
  if (!isCliPreview) window.setInterval(refreshProjectSessionIndicators, 8000);
  boot.catch((err) => {
    state.errors.push(`initial session failed: ${err.message}`);
    setConnPill("failed", "failed");
    render();
  });

  // Detach the browser only. Session runners are server-owned and may be
  // executing in parallel with other chats or while this page reloads.
  window.addEventListener("beforeunload", () => {
    stopTimelinePoll();
    if (voiceInput.listening) {
      try { voiceInput.recognition?.abort(); } catch {}
    }
  });
})();
