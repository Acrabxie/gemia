/* Lumeri v3 frontend — vanilla JS, no build step.
 *
 * Connects to the agent loop via:
 *   POST /sessions                              create session
 *   POST /sessions/{id}/assets                  raw body + X-Filename
 *   POST /sessions/{id}/turn                    {"message": "..."} (202)
 *   GET  /sessions/{id}/stream                  EventSource + last_event_id replay
 *   GET  /sessions/{id}/assets/{aid}            preview URL for the asset
 *   POST /sessions/{id}/close                   teardown
 *
 * Invariants (mirror the agent loop's promises):
 *   - Every event kind has a handler. Unknown kinds raise a visible
 *     banner — never silent drop.
 *   - tool_exec_progress shows real percent when present, indeterminate
 *     spinner when omitted. We never fabricate progress.
 *   - All asset previews load from /sessions/{id}/assets/{aid}. Tool
 *     results expose asset_id/kind/asset_url, never local filesystem paths.
 */

(function () {
  "use strict";

  const $ = (sel) => document.querySelector(sel);

  // Inline the icon sprite once so every <use href="#i-*"> resolves, including
  // ones rendered before the fetch lands (SVG <use> re-resolves on DOM insert).
  fetch("/v3/icons.svg")
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
    newSessionBtn: $("#new-session-btn"),
    timeline: $("#timeline"),
    emptyState: $("#empty-state"),
    assetGrid: $("#asset-grid"),
    mediaLibraryGrid: $("#media-library-grid"),
    libraryRefreshBtn: $("#library-refresh-btn"),
    libraryAnnotateBtn: $("#library-annotate-btn"),
    uploadInput: $("#upload-input"),
    uploadBtn: $("#upload-btn"),
    promptInput: $("#prompt-input"),
    sendBtn: $("#send-btn"),
    sandboxBtn: $("#sandbox-toggle-btn"),
    planBtn: $("#plan-toggle-btn"),
    planBar: $("#plan-bar"),
    askDock: $("#ask-dock"),
    slashMenu: $("#slash-menu"),
  };

  /** @typedef {{ asset_id: string, kind: string, summary: string, source: "user"|"tool", final?: boolean }} AssetEntry */

  const state = {
    sessionId: null,
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
    projectTimeline: null,      // fetched from /sessions/{id}/timeline
    timelinePollTimer: null,
    mediaLibrary: [],
    mediaAnnotations: new Map(), // media-library asset_id -> annotations[]
    mediaLibraryStatus: "idle",
    planMode: false,            // mirrors the backend per-session flag
    planReady: false,           // a turn completed while planning → offer approval
    pendingAsk: null,           // {question_id, question} while elicit awaits
  };

  function newTurn(userMessage) {
    return {
      userMessage,
      assistantText: "",
      streaming: false,
      toolCalls: new Map(),     // call_id -> ToolCallState
      orderedCallIds: [],
      banners: [],              // { kind: "budget"|"turn_error"|"unknown", text, sub? }
      complete: false,
    };
  }

  // ── render ──────────────────────────────────────────────────────────

  function escapeHTML(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
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

    for (let i = 0; i < blocks.length; i++) {
      const block = blocks[i];

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
      if (/^[\t ]*\d+[.)]\s/.test(block.trim())) {
        out.push(mdList(block, "ol"));
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

  function mdList(block, tag) {
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
    return `<${tag} class="md-list">${items.join("")}</${tag}>`;
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

  function render() {
    els.sessionLabel.textContent = state.sessionId || "—";
    const busy = !state.sessionId || state.turnInProgress;
    els.sendBtn.disabled = busy;
    els.uploadBtn.disabled = busy;
    document.querySelectorAll(".pt-action-btn, .pt-edit-btn").forEach((b) => { b.disabled = busy; });
    updateEditHint();   // selection-aware split/delete rule wins over the blanket disable above

    if (!state.turns.length) {
      els.timeline.hidden = true;
      els.emptyState.hidden = false;
    } else {
      els.emptyState.hidden = true;
      els.timeline.hidden = false;
      els.timeline.innerHTML = state.turns.map((turn, idx) => renderTurn(turn, idx)).join("");
    }

    // 有素材就自动展开左侧时间轴抽屉（一次性，之后尊重用户手动开合）。
    if (!state._drawerAutoShown && state.assets && state.assets.length > 0) {
      state._drawerAutoShown = true;
      document.getElementById("preview-stage")?.classList.add("drawer-open");
      renderStageTabs();
    }

    renderAssets();
    renderMediaLibrary();
    renderPlanUi();
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
    const assistantHtml = (turn.assistantText || turn.streaming)
      ? `<div class="assistant-bubble${turn.streaming ? " streaming" : ""}">${renderMarkdown(turn.assistantText)}</div>`
      : "";
    return `
      ${idx ? '<div class="turn-divider" role="separator"></div>' : ""}
      <div class="user-bubble">${renderMarkdown(turn.userMessage)}</div>
      ${callsHtml}
      ${bannersHtml}
      ${assistantHtml}
    `;
  }

  // A failed call whose recovery is one of these can be "fixed" by a later
  // call — so a success that follows it closes a self-correction arc.
  const RECOVERABLE_RECOVERY = new Set(["fix_args", "switch_tool", "transient_retry"]);

  /** Group a turn's tool calls so a run of recoverable failures followed by a
   *  success renders as one "self-corrected" arc, not scattered cards. */
  function buildCallGroups(turn) {
    const groups = [];
    let openFailures = [];
    const flushSingles = () => {
      for (const f of openFailures) groups.push({ type: "single", tc: f });
      openFailures = [];
    };
    for (const cid of turn.orderedCallIds) {
      const tc = turn.toolCalls.get(cid);
      if (!tc) continue;
      if (tc.status === "failed" && RECOVERABLE_RECOVERY.has(tc.recovery)) {
        openFailures.push(tc);
      } else if (tc.status === "done" && openFailures.length) {
        groups.push({ type: "arc", calls: [...openFailures, tc] });
        openFailures = [];
      } else {
        flushSingles();
        groups.push({ type: "single", tc });
      }
    }
    flushSingles();  // trailing unresolved failures render on their own
    return groups;
  }

  function renderCallGroup(group) {
    if (group.type === "single") return renderToolCall(group.tc);
    return `
      <div class="self-correct-arc">
        <div class="self-correct-badge">⟳ self-corrected</div>
        ${group.calls.map(renderToolCall).join("")}
      </div>
    `;
  }

  function renderTypedError(tc) {
    if (!tc.error) return "";
    const codeChip = tc.errorCode
      ? `<span class="err-chip err-code">${escapeHTML(tc.errorCode)}</span>` : "";
    const recoveryChip = tc.recovery
      ? `<span class="err-chip err-recovery">${escapeHTML(tc.recovery)}</span>` : "";
    const optsHtml = (tc.validOptions && tc.validOptions.length)
      ? `<div class="err-options">options: ${tc.validOptions.map((o) => `<code>${escapeHTML(o)}</code>`).join(" ")}</div>`
      : "";
    const hintHtml = tc.hint
      ? `<div class="err-hint">${escapeHTML(tc.hint)}</div>` : "";
    return `
      <div class="tool-error">
        <div class="err-head">${codeChip}${recoveryChip}<span class="err-msg">${escapeHTML(tc.error)}</span></div>
        ${optsHtml}
        ${hintHtml}
      </div>
    `;
  }

  function renderToolCall(tc) {
    const reasoningHtml = tc.reasoning
      ? `<div class="tool-reasoning">${escapeHTML(tc.reasoning)}</div>`
      : "";
    const argsHtml = tc.args
      ? `<div class="tool-args">${escapeHTML(JSON.stringify(tc.args, null, 2))}</div>`
      : "";
    const summaryHtml = tc.summary
      ? `<div class="tool-summary">${escapeHTML(tc.summary)}</div>`
      : "";
    const errorHtml = renderTypedError(tc);
    const progressHtml = renderProgress(tc);
    const previewHtml = tc.previewAssetId && state.sessionId
      ? `<a class="tool-preview-link" href="/sessions/${state.sessionId}/assets/${tc.previewAssetId}" target="_blank" rel="noopener">open ${tc.previewAssetId} ↗</a>`
      : "";
    const childrenHtml = renderSubagents(tc);
    return `
      ${reasoningHtml}
      <div class="tool-card${tc.status === "failed" ? " failed" : ""}">
        <div class="tool-card-head">
          <span class="tool-name">${escapeHTML(tc.tool_name)}</span>
          <span class="tool-status ${tc.status}">${tc.status}</span>
        </div>
        ${argsHtml}
        ${progressHtml}
        ${summaryHtml}
        ${previewHtml}
        ${errorHtml}
        ${childrenHtml}
      </div>
    `;
  }

  // Render a spawn_subtasks call's children indented beneath it. Each child is a
  // group with a status chip + summary; the child's own tool calls render as
  // compact indented lines within the group.
  function renderSubagents(tc) {
    if (!tc.children || !tc.childOrder || !tc.childOrder.length) return "";
    const groups = tc.childOrder.map((agentId) => {
      const child = tc.children.get(agentId);
      if (!child) return "";
      const callsHtml = (child.callOrder || []).map((k) => {
        const c = child.calls.get(k);
        if (!c) return "";
        const detail = c.status === "done"
          ? escapeHTML(c.summary || "done")
          : c.status === "failed"
            ? escapeHTML(c.error || "failed")
            : escapeHTML(c.progress?.message || c.status);
        return `<div class="subagent-call"><span class="subagent-branch">├─</span> <span class="tool-name">${escapeHTML(c.tool_name)}</span> <span class="tool-status ${c.status}">${c.status}</span> <span class="subagent-call-detail">${detail}</span></div>`;
      }).join("");
      const metaBits = [];
      if (typeof child.steps === "number") metaBits.push(`${child.steps} steps`);
      if (typeof child.spentUsd === "number") metaBits.push(`$${child.spentUsd}`);
      if (typeof child.spentSeconds === "number") metaBits.push(`${child.spentSeconds}s`);
      const metaHtml = metaBits.length ? `<span class="subagent-meta">${escapeHTML(metaBits.join(" · "))}</span>` : "";
      const summaryHtml = child.summary ? `<div class="subagent-summary">${escapeHTML(child.summary)}</div>` : "";
      const assetsHtml = (child.assetIds && child.assetIds.length)
        ? `<div class="subagent-assets">${child.assetIds.map((a) => `<code>${escapeHTML(a)}</code>`).join(" ")}</div>`
        : "";
      return `
        <div class="subagent-group">
          <div class="subagent-head">
            <span class="subagent-id">${escapeHTML(child.agent_id)}</span>
            <span class="subagent-profile">${escapeHTML(child.profile)}</span>
            <span class="tool-status ${child.status}">${escapeHTML(child.status)}</span>
            ${metaHtml}
          </div>
          ${child.goal ? `<div class="subagent-goal">${escapeHTML(child.goal)}</div>` : ""}
          ${callsHtml}
          ${summaryHtml}
          ${assetsHtml}
        </div>
      `;
    }).join("");
    return `<div class="subagents">${groups}</div>`;
  }

  function renderProgress(tc) {
    if (tc.status !== "running") return "";
    const hasPercent = typeof tc.progress?.percent === "number";
    if (hasPercent) {
      const pct = Math.max(0, Math.min(100, tc.progress.percent));
      return `
        <div class="progress-block">
          <div class="progress-bar"><div class="progress-bar-fill" style="width:${pct.toFixed(1)}%"></div></div>
          <div class="progress-text">${pct.toFixed(1)}% ${escapeHTML(tc.progress?.message || "")}</div>
        </div>
      `;
    }
    return `
      <div class="progress-block">
        <div class="progress-bar"><div class="progress-bar-fill indeterminate"></div></div>
        <div class="progress-text">working…</div>
      </div>
    `;
  }

  function renderBanner(banner) {
    const cls = banner.kind === "budget" ? "banner-budget"
              : banner.kind === "turn_error" ? "banner-turn-error"
              : banner.kind === "plan" ? "banner-plan"
              : banner.kind === "info" ? "banner-info"
              : "banner-unknown";
    const sub = banner.sub ? `<small>${escapeHTML(banner.sub)}</small>` : "";
    return `<div class="banner ${cls}">${escapeHTML(banner.text)}${sub}</div>`;
  }

  function renderAssets() {
    // Guard: only touch the DOM when the asset set actually changed. render() runs
    // on every SSE event (see es.onmessage) and every 3s timeline poll; without
    // this guard each call reset assetGrid.innerHTML, destroying and recreating
    // every <video>/<audio>/<img> and forcing the browser to re-fetch each
    // /sessions/{id}/assets/{aid}. While a turn streamed that was several
    // re-fetches per second per asset — the "endless /assets/v_002 requests".
    const sig = !state.assets.length
      ? "empty"
      : state.sessionId + "|" + state.assets.map((a) =>
          `${a.asset_id}:${a.kind}:${a.final ? 1 : 0}:${a.source}:${a.summary || ""}`
        ).join(",");
    if (sig === state._assetsSig) return;
    state._assetsSig = sig;

    if (!state.assets.length) {
      els.assetGrid.innerHTML = "";
      return;
    }
    els.assetGrid.innerHTML = state.assets.map((a) => {
      const url = `/sessions/${state.sessionId}/assets/${a.asset_id}`;
      const playerHtml = a.kind === "image"
        ? `<img src="${url}" alt="${a.asset_id}" />`
        : a.kind === "audio"
          ? `<audio src="${url}" controls preload="metadata"></audio>`
          : `<video src="${url}" controls preload="metadata"${a.final ? " autoplay muted" : ""}></video>`;
      return `
        <div class="asset-card${a.final ? " final" : ""}">
          ${playerHtml}
          <div class="asset-meta">
            ${a.final ? `<svg class="asset-final" viewBox="0 0 24 24" role="img" aria-label="最终成片"><use href="#i-check-circle"/></svg>` : ""}
            <span class="asset-id">${a.asset_id}</span> · ${escapeHTML(a.source)} · ${escapeHTML(a.summary || "")}
          </div>
        </div>
      `;
    }).join("");
  }

  function renderMediaLibrary() {
    if (!els.mediaLibraryGrid) return;
    if (state.mediaLibraryStatus === "loading") {
      els.mediaLibraryGrid.innerHTML = `<p class="placeholder">加载中…</p>`;
      return;
    }
    if (state.mediaLibraryStatus === "signed-out") {
      els.mediaLibraryGrid.innerHTML = `<p class="placeholder">媒体库暂不可用</p>`;
      return;
    }
    if (!state.mediaLibrary.length) {
      els.mediaLibraryGrid.innerHTML = `<p class="placeholder">暂无素材</p>`;
      return;
    }
    els.mediaLibraryGrid.innerHTML = state.mediaLibrary.map((asset) => {
      const assetId = asset.asset_id || asset.id || "";
      const summary = asset.annotation_summary || {};
      const tags = (summary.tags || []).slice(0, 5);
      const labels = (summary.labels || []).slice(0, 3);
      const anns = state.mediaAnnotations.get(assetId) || [];
      const annHtml = anns.length
        ? `<div class="annotation-list">${anns.map(renderAnnotation).join("")}</div>`
        : "";
      const thumb = asset.thumbnail_src
        ? `<img class="library-thumb" src="${escapeHTML(asset.thumbnail_src)}" alt="" loading="lazy" />`
        : `<div class="library-thumb blank">${escapeHTML(asset.media_kind || "media")}</div>`;
      return `
        <div class="library-card" data-library-asset="${escapeHTML(assetId)}">
          ${thumb}
          <div class="library-card-body">
            <div class="library-title">${escapeHTML(asset.name || assetId)}</div>
            <div class="library-meta">${escapeHTML(assetId)} · ${escapeHTML(asset.media_kind || "media")} · ${formatSeconds(asset.duration)}</div>
            <div class="library-tags">
              <span title="标记数"><svg viewBox="0 0 24 24" aria-hidden="true"><use href="#i-marker"/></svg>${Number(summary.count || 0)}</span>
              ${tags.map((tag) => `<span>${escapeHTML(tag)}</span>`).join("")}
              ${labels.map((label) => `<span>${escapeHTML(label)}</span>`).join("")}
            </div>
            <div class="library-card-actions">
              <button type="button" class="library-small-btn icon-btn" title="标注" aria-label="标注" data-library-annotate="${escapeHTML(assetId)}"><svg viewBox="0 0 24 24" aria-hidden="true"><use href="#i-wand"/></svg></button>
              <button type="button" class="library-small-btn icon-btn" title="标记" aria-label="标记" data-library-load="${escapeHTML(assetId)}"><svg viewBox="0 0 24 24" aria-hidden="true"><use href="#i-marker"/></svg></button>
            </div>
            ${annHtml}
          </div>
        </div>
      `;
    }).join("");
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

  // Resolve the child tool-call state a tool_exec_* event with an agent_id
  // belongs to. Child tool activity rides the EXISTING tool_exec_* kinds
  // (gemia/subtasks.py) carrying { call_id: <spawn call>, agent_id, tool_call_id }.
  // Returns null (→ caller ignores) if the spawn call or child isn't tracked yet.
  function childCallState(ev) {
    const t = state.currentTurn;
    if (!t) return null;
    const spawn = t.toolCalls.get(ev.call_id);
    if (!spawn || !spawn.children) return null;
    const child = spawn.children.get(ev.agent_id);
    if (!child) return null;
    const key = ev.tool_call_id || ev.call_id;
    let tc = child.calls.get(key);
    if (!tc) {
      tc = {
        tool_call_id: key,
        tool_name: ev.tool_name || "tool",
        status: "running",
        progress: null,
        summary: null,
        previewAssetId: null,
        error: null,
        errorCode: null,
      };
      child.calls.set(key, tc);
      child.callOrder.push(key);
    }
    return tc;
  }

  // ── event handlers (one per kind, no silent drop) ──────────────────

  const handlers = {
    turn_start: () => {
      state.turnInProgress = true;
      if (state.currentTurn) {
        state.currentTurn.streaming = false;
      }
    },
    model_text_delta: (ev) => {
      const t = state.currentTurn;
      if (!t) return;
      t.assistantText += ev.delta;
      t.streaming = true;
    },
    model_tool_call_start: (ev) => {
      const t = state.currentTurn;
      if (!t) return;
      // Text the model streamed right before this call is its lead-in
      // reasoning — for a corrective retry, this is the "diagnosis" line.
      // Move it off the trailing bubble and onto the card so a self-correction
      // reads as one arc (reason → fix) instead of a detached paragraph.
      const reasoning = (t.assistantText || "").trim();
      t.assistantText = "";
      t.streaming = false;
      t.toolCalls.set(ev.call_id, {
        call_id: ev.call_id,
        tool_name: ev.tool_name,
        status: "pending",
        args: null,
        progress: null,
        summary: null,
        error: null,
        errorCode: null,
        recovery: null,
        validOptions: null,
        hint: null,
        reasoning: reasoning || null,
        previewAssetId: null,
        // Multi-agent fan-out: subagent_start populates this Map (agent_id ->
        // child group) under a spawn_subtasks call. Child tool_exec_* events
        // (carrying agent_id) route into the child's own tool list.
        children: new Map(),
        childOrder: [],
      });
      t.orderedCallIds.push(ev.call_id);
    },
    model_tool_call_ready: (ev) => {
      const t = state.currentTurn;
      const tc = t?.toolCalls.get(ev.call_id);
      if (tc) tc.args = ev.args;
    },
    tool_exec_start: (ev) => {
      // agent_id present → child tool activity, route into the child group.
      if (ev.agent_id) { const c = childCallState(ev); if (c) c.status = "running"; return; }
      const tc = state.currentTurn?.toolCalls.get(ev.call_id);
      if (tc) tc.status = "running";
    },
    tool_exec_progress: (ev) => {
      if (ev.agent_id) {
        const c = childCallState(ev);
        if (c) c.progress = {
          percent: typeof ev.percent === "number" ? ev.percent : null,
          message: ev.message || null,
        };
        return;
      }
      const tc = state.currentTurn?.toolCalls.get(ev.call_id);
      if (!tc) return;
      tc.progress = {
        percent: typeof ev.percent === "number" ? ev.percent : null,
        message: ev.message || null,
      };
    },
    tool_exec_result: (ev) => {
      if (ev.agent_id) {
        const c = childCallState(ev);
        if (c) {
          c.status = "done";
          c.summary = ev.result?.summary || null;
          c.previewAssetId = ev.result?.asset_id || null;
        }
        return;
      }
      const t = state.currentTurn;
      const tc = t?.toolCalls.get(ev.call_id);
      if (!tc) return;
      tc.status = "done";
      tc.summary = ev.result?.summary || null;
      tc.previewAssetId = ev.result?.asset_id || null;
      if (tc.previewAssetId) {
        state.assets.push({
          asset_id: tc.previewAssetId,
          kind: ev.result?.kind || inferKindFromAssetId(tc.previewAssetId),
          summary: ev.result?.summary || "",
          source: "tool",
          final: false,
        });
      }
    },
    tool_exec_error: (ev) => {
      if (ev.agent_id) {
        const c = childCallState(ev);
        if (c) { c.status = "failed"; c.error = ev.error || "unknown error"; c.errorCode = ev.error_code || null; }
        return;
      }
      const tc = state.currentTurn?.toolCalls.get(ev.call_id);
      if (tc) {
        tc.status = "failed";
        tc.error = ev.error || "unknown error";
        // Typed-error fields (present when the host raised a GemiaError/ToolError).
        // These are what turn a dead-end into a fixable, visible diagnosis.
        tc.errorCode = ev.error_code || null;
        tc.recovery = ev.recovery || null;
        tc.validOptions = Array.isArray(ev.valid_options) ? ev.valid_options : null;
        tc.hint = ev.hint || null;
      }
    },
    subagent_start: (ev) => {
      // A child of a spawn_subtasks call is starting. Create its group under the
      // spawn call so its tool activity + result render indented beneath it.
      const t = state.currentTurn;
      const spawn = t?.toolCalls.get(ev.call_id);
      if (!spawn || !spawn.children) return;
      if (!spawn.children.has(ev.agent_id)) {
        spawn.children.set(ev.agent_id, {
          agent_id: ev.agent_id,
          goal: ev.goal || "",
          profile: ev.tool_profile || "",
          status: "running",
          summary: null,
          assetIds: [],
          spentUsd: null,
          spentSeconds: null,
          steps: null,
          calls: new Map(),
          callOrder: [],
        });
        spawn.childOrder.push(ev.agent_id);
      }
    },
    subagent_result: (ev) => {
      // A child finished (any status). Close its group with the terminal record.
      const spawn = state.currentTurn?.toolCalls.get(ev.call_id);
      const child = spawn?.children?.get(ev.agent_id);
      if (!child) return;
      child.status = ev.status || "ok";
      child.summary = ev.summary || null;
      child.assetIds = Array.isArray(ev.asset_ids) ? ev.asset_ids : [];
      child.spentUsd = typeof ev.spent_usd === "number" ? ev.spent_usd : null;
      child.spentSeconds = typeof ev.spent_seconds === "number" ? ev.spent_seconds : null;
      child.steps = typeof ev.steps === "number" ? ev.steps : null;
    },
    budget_gate: (ev) => {
      const t = state.currentTurn;
      const tc = t?.toolCalls.get(ev.call_id);
      if (tc) tc.status = "gated";
      const alt = (ev.alternatives || []).join(", ");
      t?.banners.push({
        kind: "budget",
        text: `budget gate on ${ev.tool_name}: ${ev.reason}`,
        sub: alt ? `alternatives: ${alt}` : "",
      });
    },
    plan_gate: (ev) => {
      // A mutating tool was blocked by plan mode. Same card treatment as a
      // budget gate; one compact banner per gated tool.
      const t = state.currentTurn;
      const tc = t?.toolCalls.get(ev.call_id);
      if (tc) tc.status = "gated";
      t?.banners.push({
        kind: "plan",
        text: `计划模式拦截了 ${ev.tool_name}`,
      });
    },
    plan_mode_changed: (ev) => {
      state.planMode = !!ev.enabled;
      if (!state.planMode) state.planReady = false;
    },
    completion_check: () => {
      // Host-side one-shot goal check re-prompts the model for a FINAL reply
      // (goal-check / visual self-check / failure disclosure). The text the
      // model streamed before this gate was only a draft — discard it so the
      // post-gate round becomes the single user-facing message. Left as-is,
      // model_text_delta would APPEND the gate round onto the draft and the
      // user sees the same answer twice (natural reply + report-style restate).
      const t = state.currentTurn;
      if (!t) return;
      t.assistantText = "";
      t.streaming = false;
    },
    turn_wrapup: (ev) => {
      // Informational "stopped because X; here's what was / wasn't done".
      state.currentTurn?.banners.push({
        kind: "info",
        text: ev.message || `turn stopped (${ev.reason || "unknown"})`,
      });
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
    protocol_hello: (ev) => {
      // Per-connection id-less frame at the top of every stream. The web
      // client is served BY the backend, so a mismatch is near-impossible —
      // record it for debugging, no banner.
      state.protocolVersion = ev.protocol_version;
    },
    replay_gap: (ev) => {
      const text = `SSE replay gap: missed ${ev.missed_event_count || "some"} event(s); refreshing session state.`;
      const banner = {
        kind: "unknown",
        text,
        sub: `requested=${ev.requested_last_event_id}, oldest=${ev.oldest_available_event_id}, latest=${ev.latest_event_id}`,
      };
      if (state.currentTurn) state.currentTurn.banners.push(banner);
      state.errors.push(text);
      state.turnInProgress = false;
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
      if (!t) return;
      t.streaming = false;
      t.complete = true;
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
    },
    turn_error: (ev) => {
      dismissAskDock();
      state.turnInProgress = false;
      const t = state.currentTurn;
      if (t) {
        t.streaming = false;
        t.complete = true;
        t.banners.push({ kind: "turn_error", text: `turn error: ${ev.error || "unknown"}` });
      }
    },
  };

  function dispatch(ev) {
    // Debug hook: raw event log accessible from DevTools console and test harnesses.
    (window.__lumeriEvents = window.__lumeriEvents || []).push(ev);
    const handler = handlers[ev.kind];
    if (!handler) {
      const t = state.currentTurn;
      const banner = { kind: "unknown", text: `unknown event kind: ${ev.kind}`, sub: JSON.stringify(ev) };
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
      const res = await fetch(`/sessions/${state.sessionId}/ask_response`, {
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
          text: `提交失败: ${body.error || res.statusText}`,
        });
        render();
      }
    } catch (err) {
      state.currentTurn?.banners.push({
        kind: "info",
        text: `提交失败: ${err.message}`,
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
    if (state.eventSource) {
      state.eventSource.close();
      state.eventSource = null;
    }
    const lastId = state.lastEventId || loadLastEventId(sessionId);
    const qs = lastId ? `?last_event_id=${encodeURIComponent(lastId)}` : "";
    const es = new EventSource(`/sessions/${sessionId}/stream${qs}`);
    es.onopen = () => {
      clearReconnectTimer();
      setConnPill("live", "live");
    };
    es.onerror = () => {
      setConnPill("reconnecting", "reconnecting");
      scheduleReconnect(1500);
    };
    es.onmessage = (e) => {
      try {
        if (e.lastEventId) saveLastEventId(sessionId, e.lastEventId);
        const ev = JSON.parse(e.data);
        dispatch(ev);
        render();
      } catch (parseErr) {
        const banner = { kind: "unknown", text: `SSE parse error: ${parseErr.message}`, sub: e.data?.slice(0, 200) };
        state.currentTurn?.banners.push(banner);
        render();
      }
    };
    state.eventSource = es;
  }

  // ── Project timeline (CapCut-style editor) ──────────────────────────
  // A px/second timeline: adaptive ruler, wheel/key zoom, multi video+audio
  // tracks, per-clip filmstrip + waveform (when the clip exposes asset_id),
  // draggable playhead, client-side markers, and drag/move/trim/split/delete
  // wired to the SAME /sessions/{id}/timeline/op endpoint as the model verbs.

  const TL = {
    pps: 64, minPps: 8, maxPps: 480,
    snap: true,
    playhead: 0,
    scrubbing: false,
    drag: null,
    markers: [],            // {time,label,color} — client-side only (no backend marker model yet)
    extraTracks: [],        // client-added empty display lanes (no backend add_track op)
    built: false,
    model: null,
    rulerCtx: null,
    filmstrip: new Map(),   // key -> string[] (data URLs)
    filmstripBusy: new Set(),
    wave: new Map(),        // assetId -> number[] peaks
    waveBusy: new Set(),
    audioCtx: null,
  };
  const TL_RULER_H = 26, TL_TRACK_H = 58, TL_MIN_CONTENT = 30;
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

  function fmtTC(s) {
    if (!isFinite(s) || s < 0) s = 0;
    const fps = Math.round((TL.model && TL.model.fps) || 30);
    const m = Math.floor(s / 60), sec = Math.floor(s % 60);
    const f = Math.floor((s - Math.floor(s)) * fps);
    return `${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}:${String(f).padStart(2, "0")}`;
  }

  function trackCompatible(mediaKind, trackKind) {
    if (mediaKind === "audio") return trackKind === "audio";
    return trackKind === "video" || trackKind === "overlay" || trackKind === "text";
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
    const rank = (k) => (k === "audio" ? 2 : k === "overlay" || k === "text" ? 0 : 1);
    tracks = tracks.map((t, i) => ({ ...t, _i: i })).sort((a, b) => rank(a.kind) - rank(b.kind) || a._i - b._i);
    let lastEnd = 0;
    for (const t of tracks) for (const c of (t.clips || [])) lastEnd = Math.max(lastEnd, (c.start || 0) + (c.duration || 0));
    const contentDur = Math.max(d.duration || 0, lastEnd, TL_MIN_CONTENT);
    return { tracks, duration: d.duration || 0, contentDur, fps: d.fps || 30, width: d.width || 1920, height: d.height || 1080, patch_seq: d.patch_seq || 0 };
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
          <button class="ptl-btn ptl-ico-btn pt-edit-btn" id="ptl-undo" title="撤销 (⌘Z)"><svg viewBox="0 0 16 16"><path d="M6.5 4 3.5 7l3 3"/><path d="M3.5 7H10a3.5 3.5 0 0 1 0 7H7.5"/></svg></button>
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
    scroll.addEventListener("scroll", () => {
      drawRuler();
      tlHeaders().style.transform = `translateY(${-scroll.scrollTop}px)`;
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
    const seek = (clientX) => {
      const r = ruler.getBoundingClientRect();
      setPlayhead(Math.max(0, pxToTime(scroll.scrollLeft + (clientX - r.left))));
    };
    ruler.addEventListener("pointerdown", (e) => { try { ruler.setPointerCapture(e.pointerId); } catch {} TL.scrubbing = true; seek(e.clientX); });
    ruler.addEventListener("pointermove", (e) => { if (TL.scrubbing) seek(e.clientX); });
    ruler.addEventListener("pointerup", () => { TL.scrubbing = false; });
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
      anchorTime = pxToTime(scroll.scrollLeft + scroll.clientWidth / 2);
      anchorPx = scroll.clientWidth / 2;
    }
    TL.pps = Math.max(TL.minPps, Math.min(TL.maxPps, next));
    const z = document.getElementById("ptl-zoom"); if (z) z.value = String(Math.round(TL.pps));
    renderProjectTimeline(state.projectTimeline);
    scroll.scrollLeft = Math.max(0, timeToX(anchorTime) - anchorPx);
    drawRuler();
  }
  function positionPlayhead() {
    const ph = document.getElementById("ptl-playhead");
    if (ph) ph.style.left = timeToX(TL.playhead) + "px";
    const tc = document.getElementById("ptl-tc");
    if (tc) tc.textContent = fmtTC(TL.playhead);
  }
  function setPlayhead(t) {
    TL.playhead = Math.max(0, t);
    positionPlayhead();
    drawRuler();
  }
  function markerKey() { return `lumeri:v3:markers:${state.sessionId || "_"}`; }
  function loadMarkers() {
    try { TL.markers = JSON.parse(window.localStorage.getItem(markerKey()) || "[]") || []; } catch { TL.markers = []; }
  }
  function saveMarkers() {
    try { window.localStorage.setItem(markerKey(), JSON.stringify(TL.markers)); } catch {}
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
    el.style.setProperty("--cedge", col[1]);
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
    content.querySelectorAll(".ptl-clip").forEach((el) => {
      const assetId = el.dataset.assetId;
      if (!assetId) return;                       // no source → solid color (graceful)
      if (el.dataset.mediaKind === "audio") ensureWaveform(el, assetId);
      else if (el.dataset.mediaKind === "video") ensureFilmstrip(el, assetId);
    });
  }
  function ensureFilmstrip(el, assetId) {
    const w = el.clientWidth || timeToX(+el.dataset.duration);
    const tiles = Math.max(1, Math.min(16, Math.round(w / 88)));
    const inS = +el.dataset.sourceIn, outS = +el.dataset.sourceOut;
    const key = `${assetId}|${inS.toFixed(2)}|${outS.toFixed(2)}|${tiles}`;
    const media = el.querySelector(".ptl-clip-media");
    const paint = (urls) => { if (media) media.innerHTML = urls.map((u) => `<div class="fs-tile" style="background-image:url(${u})"></div>`).join(""); };
    const cached = TL.filmstrip.get(key);
    if (cached) { paint(cached); return; }              // cached (incl. empty) → no re-extract
    if (TL.filmstripBusy.has(key)) return;
    TL.filmstripBusy.add(key);
    extractFilmstrip(assetId, inS, outS, tiles).then((urls) => {
      TL.filmstripBusy.delete(key);
      TL.filmstrip.set(key, urls || []);                // negative-cache failures: stops the retry storm
      if (document.body.contains(el)) paint(urls || []);
    }).catch(() => TL.filmstripBusy.delete(key));
  }
  function extractFilmstrip(assetId, inS, outS, tiles) {
    return new Promise((resolve) => {
      const url = `/sessions/${state.sessionId}/assets/${assetId}`;
      const video = document.createElement("video");
      video.muted = true; video.preload = "auto"; video.crossOrigin = "anonymous";
      const out = [];
      let done = false;
      const teardown = () => { try { video.pause(); video.removeAttribute("src"); video.load(); } catch {} };
      const finishUp = () => { if (!done) { done = true; clearTimeout(timer); teardown(); resolve(out); } };
      const timer = setTimeout(finishUp, 12000);
      video.addEventListener("error", finishUp, { once: true });
      video.addEventListener("loadedmetadata", async () => {
        const dur = isFinite(video.duration) && video.duration > 0 ? video.duration : (outS || 1);
        const a = inS || 0, b = (outS && outS > a) ? outS : dur;
        const span = Math.max(0.05, b - a);
        const th = 46, tw = Math.round(th * ((video.videoWidth / Math.max(1, video.videoHeight)) || 1.7));
        const canvas = document.createElement("canvas");
        canvas.width = tw; canvas.height = th;
        const ctx = canvas.getContext("2d");
        for (let i = 0; i < tiles && !done; i++) {
          const t = Math.min(a + span * ((i + 0.5) / tiles), dur - 0.02);
          await seekVideo(video, Math.max(0, t));
          try { ctx.drawImage(video, 0, 0, tw, th); out.push(canvas.toDataURL("image/jpeg", 0.55)); } catch { break; }
        }
        finishUp();
      }, { once: true });
      video.src = url;   // set src last, after listeners are attached
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
    const res = await fetch(`/sessions/${state.sessionId}/assets/${assetId}`);
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
    try {
      const r = await fetch(`/sessions/${state.sessionId}/timeline`);
      if (!r.ok) return;
      const data = await r.json();
      state.projectTimeline = data;
      if (!options.force && data.patch_seq === TL._renderedSeq) return;   // unchanged → skip 3s DOM rebuild + media re-hydrate
      renderProjectTimeline(data);
    } catch { /* ignore network errors */ }
  }

  function startTimelinePoll() {
    stopTimelinePoll();
    TL._renderedSeq = null;   // force the first fetch of a (new) session to render authoritative state
    state.timelinePollTimer = setInterval(fetchProjectTimeline, 3000);
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
    content.style.height = (model.tracks.length * TL_TRACK_H) + "px";

    headers.innerHTML = model.tracks.map((t) => {
      const isA = t.kind === "audio";
      return `<div class="ptl-head ${escapeHTML(t.kind)}" style="height:${TL_TRACK_H}px" title="${escapeHTML(t.name || t.id)}">`
        + `<span class="ptl-head-kind">${isA ? "♪" : "▦"} ${escapeHTML(t.id)}</span></div>`;
    }).join("");

    content.innerHTML = "";
    model.tracks.forEach((t, i) => {
      const lane = document.createElement("div");
      lane.className = `ptl-lane ${t.kind}`;
      lane.dataset.trackId = t.id;
      lane.dataset.trackKind = t.kind;
      lane.style.top = (i * TL_TRACK_H) + "px";
      lane.style.height = TL_TRACK_H + "px";
      (t.clips || []).forEach((clip) => lane.appendChild(buildClipEl(clip, t)));
      content.appendChild(lane);
    });
    const ph = document.createElement("div"); ph.className = "ptl-playhead"; ph.id = "ptl-playhead"; content.appendChild(ph);
    const mk = document.createElement("div"); mk.id = "ptl-markers"; content.appendChild(mk);

    positionPlayhead();
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
      const r = await fetch(`/sessions/${state.sessionId}/timeline/op`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(opBody),
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
        if (lane && tid && !tid.endsWith("*") && trackCompatible(d.mediaKind, lane.dataset.trackKind) && tid !== d.origTrack) {
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
        if (d.pendTrack && d.pendTrack !== d.origTrack) op.track_id = d.pendTrack;
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

  async function createSession() {
    clearReconnectTimer();
    stopTimelinePoll();
    if (state.eventSource) {
      try { await fetch(`/sessions/${state.sessionId}/close`, { method: "POST" }); } catch {}
      state.eventSource.close();
    }
    setConnPill("opening…", "");
    const r = await fetch("/sessions", { method: "POST" });
    if (!r.ok) throw new Error(`POST /sessions failed: ${r.status}`);
    const data = await r.json();
    state.sessionId = data.session_id;
    // reset per-session timeline view state so nothing leaks across sessions
    loadMarkers();              // markers are per-session (localStorage); reload under the real key
    TL.extraTracks = [];        // client-added display lanes
    TL.playhead = 0;
    state.selectedClipId = null;
    state.turns = [];
    state.currentTurn = null;
    state.assets = [];
    state.errors = [];
    state.turnInProgress = false;
    state.lastEventId = null;
    state.projectTimeline = null;
    state.mediaAnnotations = new Map();
    state.planMode = false;     // fresh sessions start with plan mode off
    state.planReady = false;
    connectSse(state.sessionId);
    startTimelinePoll();
    fetchMediaLibrary().catch(() => {});
    render();
  }

  async function refreshSessionState() {
    if (!state.sessionId) return;
    const r = await fetch(`/sessions/${state.sessionId}`);
    if (!r.ok) throw new Error(`GET /sessions/${state.sessionId} failed: ${r.status}`);
    const data = await r.json();
    const finalIds = new Set(state.assets.filter((a) => a.final).map((a) => a.asset_id));
    state.assets = (data.assets || []).map((a) => ({
      asset_id: a.asset_id,
      kind: a.kind || inferKindFromAssetId(a.asset_id),
      summary: a.summary || "",
      source: "tool",
      final: finalIds.has(a.asset_id),
    }));
    if (data.latest_event_id !== null && data.latest_event_id !== undefined) {
      saveLastEventId(state.sessionId, data.latest_event_id);
    }
    if (typeof data.plan_mode === "boolean") {
      state.planMode = data.plan_mode;
      if (!state.planMode) state.planReady = false;
    }
  }

  async function fetchMediaLibrary() {
    state.mediaLibraryStatus = "loading";
    render();
    try {
      const r = await fetch("/media-library/list?limit=100");
      if (r.status === 401) {
        state.mediaLibrary = [];
        state.mediaLibraryStatus = "signed-out";
        render();
        return;
      }
      if (!r.ok) throw new Error(`GET /media-library/list failed: ${r.status}`);
      const data = await r.json();
      state.mediaLibrary = Array.isArray(data.assets) ? data.assets : [];
      state.mediaLibraryStatus = "ready";
    } catch (err) {
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
    const r = await fetch("/media-library/annotate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!r.ok) throw new Error(`annotate failed: ${r.status}`);
    await r.json();
    await fetchMediaLibrary();
    if (assetId) await loadMediaAnnotations(assetId);
  }

  async function loadMediaAnnotations(assetId) {
    const r = await fetch(`/media-library/${encodeURIComponent(assetId)}/annotations`);
    if (!r.ok) throw new Error(`annotations failed: ${r.status}`);
    const data = await r.json();
    state.mediaAnnotations.set(assetId, Array.isArray(data.annotations) ? data.annotations : []);
    render();
  }

  function promptLanguage() {
    return /[\u4e00-\u9fff]/.test(els.promptInput?.value || "") ? "zh" : "en";
  }

  async function uploadFile(file) {
    if (!state.sessionId) throw new Error("no session");
    setUploadStatus(`uploading ${file.name}…`);
    const r = await fetch(`/sessions/${state.sessionId}/assets`, {
      method: "POST",
      headers: {
        "X-Filename": encodeURIComponent(file.name),
        "Content-Type": file.type || "application/octet-stream",
      },
      body: file,
    });
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
    const turn = newTurn(message);
    state.turns.push(turn);
    state.currentTurn = turn;
    state.turnInProgress = true;
    state.planReady = false;   // a new turn supersedes any pending approval offer
    render();
    const r = await fetch(`/sessions/${state.sessionId}/turn`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message }),
    });
    if (!r.ok && r.status !== 202) {
      const body = await r.text();
      turn.banners.push({ kind: "turn_error", text: `POST /turn failed: ${r.status}`, sub: body.slice(0, 200) });
      state.turnInProgress = false;
      render();
    }
  }

  // ── wiring ──────────────────────────────────────────────────────────

  els.newSessionBtn.addEventListener("click", () => {
    createSession().catch((err) => {
      state.errors.push(`create session failed: ${err.message}`);
      setConnPill("failed", "failed");
      render();
    });
  });

  els.uploadBtn.addEventListener("click", () => els.uploadInput.click());
  els.uploadInput.addEventListener("change", () => {
    const file = els.uploadInput.files?.[0];
    if (!file) return;
    uploadFile(file).catch((err) => {
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

  document.addEventListener("click", (e) => {
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
      loadMediaAnnotations(assetId).catch((err) => {
        state.errors.push(`load annotations failed: ${err.message}`);
        render();
      });
    }
  });

  // ── /model picker ───────────────────────────────────────────────────
  // Lists the backend's priority-ordered model catalog + thinking-effort
  // tiers, marks the active pick, and lets the user switch. The selection is
  // global + persisted (config.json:lumeri_v3_model / lumeri_v3_effort) — the
  // same store the CLI /model uses — so it sticks across sessions/restarts.
  async function fetchModelInfo() {
    const r = await fetch("/model");
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    return r.json();
  }

  async function postModelSelection(body) {
    const r = await fetch("/model", {
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
      overlay.className = "auth-modal";
      overlay.hidden = true;
      overlay.innerHTML = `
        <div class="model-backdrop" data-model-close></div>
        <div class="auth-dialog model-dialog" role="dialog" aria-modal="true" aria-labelledby="model-title">
          <button type="button" class="auth-x" data-model-close aria-label="关闭">×</button>
          <h2 id="model-title">模型与思考强度</h2>
          <div class="model-list" id="model-list"></div>
          <div class="model-effort-label">思考强度</div>
          <div class="model-efforts" id="model-efforts"></div>
          <p class="auth-error" id="model-error" hidden></p>
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

    function renderInfo(info) {
      const active = info.active || {};
      const list = $("#model-list", overlay);
      list.innerHTML = (info.priority || []).map((it, i) => {
        const on = it.id === active.model;
        return `
          <button type="button" class="model-row${on ? " active" : ""}" data-model-id="${escapeHTML(it.id)}">
            <span class="model-dot">${on ? "●" : "○"}</span>
            <span class="model-name">${escapeHTML(it.label)}${i === 0 ? ' <span class="model-tag">默认</span>' : ""}</span>
            <span class="model-id">${escapeHTML(it.id)}</span>
          </button>`;
      }).join("");
      const efforts = $("#model-efforts", overlay);
      efforts.innerHTML = (info.efforts || []).map((e) => {
        const on = e === active.effort;
        return `<button type="button" class="model-chip${on ? " active" : ""}" data-effort="${escapeHTML(e)}">${escapeHTML(e)}</button>`;
      }).join("");

      list.querySelectorAll("[data-model-id]").forEach((btn) =>
        btn.addEventListener("click", () => apply({ model: btn.dataset.modelId })));
      efforts.querySelectorAll("[data-effort]").forEach((btn) =>
        btn.addEventListener("click", () => apply({ effort: btn.dataset.effort })));
    }

    async function apply(body) {
      setErr("");
      try {
        const data = await postModelSelection(body);
        renderInfo(data);
      } catch (e) {
        setErr(`切换失败：${e.message}`);
      }
    }

    setErr("");
    $("#model-list", overlay).innerHTML = '<div class="model-loading">加载中…</div>';
    $("#model-efforts", overlay).innerHTML = "";
    overlay.hidden = false;
    fetchModelInfo().then(renderInfo).catch((e) => setErr(`加载失败：${e.message}`));
  }

  // ── AI 供应商 Setup 面板 ─────────────────────────────────────────────
  // 列常见 provider（Vertex/Gemini/OpenAI/Claude/OpenRouter）+ 自定义(OpenAI 兼容)，
  // 密钥经 POST /config 白名单存盘、即时生效；测试连接走 POST /config/test-brain。
  function openSetupPanel() {
    let overlay = $("#setup-modal");
    if (!overlay) {
      overlay = document.createElement("div");
      overlay.id = "setup-modal";
      overlay.className = "auth-modal";
      overlay.hidden = true;
      overlay.innerHTML = `
        <div class="model-backdrop" data-setup-close></div>
        <div class="auth-dialog setup-dialog" role="dialog" aria-modal="true" aria-labelledby="setup-title">
          <button type="button" class="auth-x" data-setup-close aria-label="关闭">×</button>
          <h2 id="setup-title">AI 供应商配置</h2>
          <p class="setup-sub">拖动排序供应商优先级，选中后配置密钥与模型。</p>
          <div class="setup-providers" id="setup-providers"></div>
          <div class="setup-fields" id="setup-fields"></div>
          <div class="setup-actions">
            <button type="button" class="setup-test" id="setup-test">测试连接</button>
            <button type="button" class="setup-save" id="setup-save">保存并启用</button>
          </div>
          <p class="setup-result" id="setup-result" hidden></p>
          <p class="auth-error" id="setup-error" hidden></p>
        </div>`;
      document.body.appendChild(overlay);
      overlay.querySelectorAll("[data-setup-close]").forEach((el) =>
        el.addEventListener("click", () => { overlay.hidden = true; }));
      document.addEventListener("keydown", (e) => {
        if (e.key === "Escape" && !overlay.hidden) overlay.hidden = true;
      });
    }
    const st = { info: null, sel: "", vals: {}, curProvider: "", scannedModels: [], providerOrder: [] };
    const errEl = $("#setup-error", overlay);
    const resEl = $("#setup-result", overlay);
    const setErr = (m) => { if (m) { errEl.textContent = m; errEl.hidden = false; } else errEl.hidden = true; };
    const setRes = (m, ok) => {
      if (!m) { resEl.hidden = true; return; }
      resEl.textContent = m; resEl.hidden = false;
      resEl.className = "setup-result " + (ok ? "ok" : "bad");
    };

    const FIELD_META = {
      vertex_project:  { label: "GCP 项目 ID", ph: "my-project-123" },
      vertex_location: { label: "区域", ph: "global / us-east5 / us-central1" },
      base_url:        { label: "Base URL", ph: "https://…/v1/chat/completions" },
      key:             { label: "API Key", ph: "sk-…（留空=沿用已存）" },
    };

    function providerCard(p, active) {
      return `<div class="setup-pcard${active ? " active" : ""}" data-pid="${escapeHTML(p.id)}" draggable="true">
        <span class="setup-drag" title="拖动排序">☰</span>
        <div class="setup-ptext">
          <span class="setup-pname">${escapeHTML(p.label)}</span>
          <span class="setup-phint">${escapeHTML(p.hint)}</span>
        </div>
      </div>`;
    }

    function keyStateLabel(p, info) {
      if (!p.key_field) return "";
      const map = { openrouter_api_key: "openrouter", gemini_api_key: "gemini", anthropic_api_key: "anthropic", openai_api_key: "openai" };
      const has = info.has_key && info.has_key[map[p.key_field]];
      return has ? ' <span class="setup-haskey">已配置</span>' : "";
    }

    function getProviders() {
      if (!st.info) return [];
      return st.providerOrder.map((id) => (st.info.providers || []).find((x) => x.id === id)).filter(Boolean);
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
        filterModelList(inp.value, listEl, inp);
      });
      inp.addEventListener("focus", () => {
        if (st.scannedModels.length) { filterModelList(inp.value, listEl, inp); listEl.hidden = false; }
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
        const r = await fetch("/config/list-models", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body), signal: ctrl.signal,
        });
        const d = await r.json().catch(() => ({}));
        if (ctrl.signal.aborted) return;
        if (d.models && d.models.length) {
          st.scannedModels = d.models;
          renderModelList(d.models, listEl, inp);
        }
      } catch (e) {
        if (e.name === "AbortError") return;
      }
      spinner.hidden = true;
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
      const box = $("#setup-fields", overlay);
      if (!p) { box.innerHTML = ""; return; }
      box.innerHTML = "";
      st.scannedModels = [];

      for (const f of p.fields) {
        if (f === "model") {
          let val = st.vals.model ?? "";
          if (!val && st.sel === st.curProvider) val = st.info.model || "";
          renderModelField(box, p, val);
          continue;
        }
        const meta = FIELD_META[f] || { label: f, ph: "" };
        let val = st.vals[f] ?? "";
        if (!val) {
          if (f === "vertex_project") val = st.info.vertex_project || "";
          else if (f === "vertex_location") val = st.info.vertex_location || "";
          else if (f === "base_url") val = st.info.base_url || "";
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
      st.vals = { effort: st.vals.effort };
      st.scannedModels = [];
      renderProviders();
      setRes(""); setErr("");
      renderFields();
    }

    function buildBody() {
      const p = getProviders().find((x) => x.id === st.sel);
      const body = { provider: st.sel };
      if (st.vals.model) body.model = st.vals.model;
      if (st.vals.effort) body.effort = st.vals.effort;
      if (st.vals.base_url) body.base_url = st.vals.base_url;
      if (st.vals.vertex_project) body.vertex_project = st.vals.vertex_project;
      if (st.vals.vertex_location) body.location = st.vals.vertex_location, body.vertex_location = st.vals.vertex_location;
      if (p && p.key_field && st.vals.key) body[p.key_field] = st.vals.key;
      return body;
    }

    async function doSave() {
      setErr(""); setRes("保存中…", true);
      try {
        const r = await fetch("/config", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(buildBody()) });
        const d = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(d.error || `HTTP ${r.status}`);
        setRes("已保存并启用 ✓（新会话即生效）", true);
      } catch (e) { setRes(""); setErr(`保存失败：${e.message}`); }
    }

    async function doTest() {
      setErr(""); setRes("测试中…（可能需数秒）", true);
      try {
        const r = await fetch("/config/test-brain", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(buildBody()) });
        const d = await r.json().catch(() => ({}));
        if (d.ok) setRes(`连接成功 ✓ ${d.provider}/${d.model} — 回样「${d.sample || ""}」`, true);
        else setRes(`连接失败：${d.error || "未知错误"}（${d.provider || ""}/${d.model || ""}）`, false);
      } catch (e) { setRes(""); setErr(`测试失败：${e.message}`); }
    }

    $("#setup-save", overlay).onclick = doSave;
    $("#setup-test", overlay).onclick = doTest;

    setErr(""); setRes("");
    const provBox = $("#setup-providers", overlay);
    provBox.innerHTML = '<div class="model-loading">加载中…</div>';
    $("#setup-fields", overlay).innerHTML = "";
    initDrag(provBox);
    overlay.hidden = false;
    fetch("/config").then((r) => r.json()).then((cfg) => {
      const info = cfg.brain;
      if (!info) { setErr("当前无法加载供应商配置"); return; }
      st.info = info;
      st.vals = { effort: info.effort || "medium" };
      st.providerOrder = (info.providers || []).map((p) => p.id);
      const cur = info.provider === "openai" && info.base_url ? "custom" : (info.provider || "openrouter");
      st.curProvider = cur;
      // move active provider to top
      const idx = st.providerOrder.indexOf(cur);
      if (idx > 0) { st.providerOrder.splice(idx, 1); st.providerOrder.unshift(cur); }
      renderProviders();
      selectProvider(cur);
    }).catch((e) => setErr(`加载失败：${e.message}`));
  }

  // ── slash-command palette ───────────────────────────────────────────
  // A "/" at the start of an empty-ish line opens a floating command menu
  // above the composer. Mirrors the CLI slash set (src/slash.js), mapping
  // each command to an existing web action so the two clients stay in sync.
  const SLASH_COMMANDS = [
    { name: "help",    desc: "显示可用命令" },
    { name: "new",     desc: "开启新会话（清空当前对话）" },
    { name: "clear",   desc: "清空当前对话（保留会话与素材）" },
    { name: "upload",  desc: "上传素材文件" },
    { name: "plan",    desc: "计划模式：只规划不执行，批准后再动手" },
    { name: "model",   desc: "切换模型与思考强度（按后台优先级）" },
    { name: "setup",   desc: "配置 AI 供应商与密钥" },
    { name: "sandbox", desc: "切换沙盒开关（关闭后改动落到真实工程）" },
    { name: "library", desc: "刷新媒体库标注" },
  ];
  const slash = { open: false, items: [], sel: 0 };

  function knownSlash(name) { return SLASH_COMMANDS.some((c) => c.name === name); }

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
    const matches = SLASH_COMMANDS.filter((c) => c.name.startsWith(frag));
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
    const msg = els.promptInput.value.trim();
    if (!msg) return;
    const name = parseSlashName(msg);
    if (name) { execSlash(name); return; }
    submitTurn(msg).then(() => { els.promptInput.value = ""; slashClose(); syncShell(); })
                   .catch((err) => {
                     state.errors.push(`submit turn failed: ${err.message}`);
                     render();
                   });
  });

  els.promptInput.addEventListener("keydown", (e) => {
    // Slash menu gets first crack at arrows/enter/tab/esc.
    if (slashKeydown(e)) return;
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
    shell.classList.toggle("has-text", ta.value.trim().length > 0);
  }
  els.promptInput.addEventListener("input", syncShell);

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
    previewStage.classList.toggle("drawer-open", open);
    renderStageTabs();
  }
  // Summoned media-library tray.
  function toggleTray(open) {
    assetsTray.hidden = !open;
    if (open) fetchMediaLibrary().catch(() => {});
  }
  $("#assets-tray-close")?.addEventListener("click", () => toggleTray(false));
  assetsTray?.addEventListener("click", (e) => { if (e.target === assetsTray) toggleTray(false); });

  // ── stage tabs: browser-style strip; "预览" is the permanent home tab ──
  const stagePanel = $("#stage-panel");
  const panelBody = $("#panel-tray-body");
  const panelRefreshBtn = $("#panel-refresh-btn");
  let panelView = null;          // outline/tasks/files while such a tab is active
  let panelPollTimer = null;

  const STAGE_VIEWS = {
    timeline: { label: "时间线", ico: '<path d="M5 10v4M9 7v10M13 9v6M17 6v12M21 10v4"/>' },
    outline:  { label: "大纲", ico: '<rect x="3.5" y="5.5" width="17" height="13" rx="2.5"/><path d="M7 10h6M7 13.5h9.5"/>' },
    tasks:    { label: "后台任务", ico: '<circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/>' },
    files:    { label: "文件", ico: '<path d="M3.5 6.5c0-1.1.9-2 2-2h3.6c.5 0 .9.2 1.2.6l1.4 1.9H18.5c1.1 0 2 .9 2 2v8.5c0 1.1-.9 2-2 2h-13c-1.1 0-2-.9-2-2z"/>' },
  };
  const PREVIEW_ICO = '<rect x="3.5" y="5" width="17" height="12" rx="2.5"/><path d="M10.4 8.6l3.8 2.4-3.8 2.4z"/><path d="M8.5 20h7"/>';
  const stageTabsBox = $("#stage-tabs");
  const stageTabList = $("#stage-tab-list");
  const stageAddBtn = $("#stage-add-btn");
  const stageAddMenu = $("#stage-add-menu");
  let stageTabs = [];
  let activeTab = "preview";
  try {
    stageTabs = JSON.parse(window.localStorage.getItem("lumeri:v3:stage-tabs") || "[]")
      .filter((k) => STAGE_VIEWS[k]);
  } catch {}

  function saveStageTabs() {
    try { window.localStorage.setItem("lumeri:v3:stage-tabs", JSON.stringify(stageTabs)); } catch {}
  }

  function setActiveTab(k) {
    activeTab = k;
    const panel = (k === "outline" || k === "tasks" || k === "files") ? k : null;
    previewStage.dataset.tab = panel ? "panel" : "preview";
    if (stagePanel) stagePanel.hidden = !panel;
    if (panelRefreshBtn) panelRefreshBtn.hidden = !panel;
    panelView = panel;
    if (panelPollTimer) { clearInterval(panelPollTimer); panelPollTimer = null; }
    if (panel) {
      if (panel === "files") filesState = null;   // reopen at the root picker
      refreshPanel();
      // outline follows the shotlist as the model edits it; tasks follows runs.
      if (panel !== "files") panelPollTimer = window.setInterval(refreshPanel, 5000);
    }
    if (k === "timeline") toggleDrawer(true);
    renderStageTabs();
  }
  function refreshPanel() {
    if (!panelView) return;
    if (panelView === "outline") renderOutlinePanel();
    else if (panelView === "tasks") renderTasksPanel();
    else if (panelView === "files") renderFilesPanel();
  }
  panelRefreshBtn?.addEventListener("click", refreshPanel);

  function renderStageTabs() {
    if (!stageTabList) return;
    const tabHtml = (k, label, ico, closable) => `
      <button type="button" class="stage-tab${activeTab === k ? " active" : ""}" data-stage-tab="${k}" role="tab" aria-selected="${activeTab === k}">
        <svg viewBox="0 0 24 24" aria-hidden="true">${ico}</svg><span>${label}</span>
        ${closable ? `<span class="stage-tab-x" data-stage-remove="${k}" role="button" title="移除" aria-label="移除${label}">
          <svg viewBox="0 0 24 24" aria-hidden="true"><use href="#i-close"/></svg>
        </span>` : ""}
      </button>`;
    stageTabList.innerHTML =
      tabHtml("preview", "预览", PREVIEW_ICO, false)
      + stageTabs.map((k) => tabHtml(k, STAGE_VIEWS[k].label, STAGE_VIEWS[k].ico, true)).join("");
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
    if (!stageAddMenu || stageAddMenu.hidden) return;
    if (e.target.closest("#stage-add-menu") || e.target.closest("#stage-add-btn")) return;
    closeStageAdd();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && stageAddMenu && !stageAddMenu.hidden) closeStageAdd();
  });

  stageTabsBox?.addEventListener("click", (e) => {
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
      const k = rm.dataset.stageRemove;
      stageTabs = stageTabs.filter((x) => x !== k);
      saveStageTabs();
      if (k === "timeline") toggleDrawer(false);
      if (activeTab === k) setActiveTab("preview");
      else renderStageTabs();
      return;
    }
    const tab = e.target.closest("[data-stage-tab]");
    if (!tab) return;
    const k = tab.dataset.stageTab;
    // Re-clicking the active timeline tab toggles its drawer; other tabs are idempotent.
    if (k === "timeline" && activeTab === "timeline") { toggleDrawer(); return; }
    setActiveTab(k);
  });
  renderStageTabs();

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
  async function renderOutlinePanel() {
    if (!state.sessionId) { panelBody.innerHTML = `<p class="placeholder">暂无会话</p>`; return; }
    let sl = null;
    try {
      const r = await fetch(`/sessions/${state.sessionId}/timeline`);
      if (r.ok) sl = (await r.json()).shotlist;
    } catch {}
    if (panelView !== "outline") return;   // panel switched while fetching
    const scenes = (sl && Array.isArray(sl.scenes)) ? sl.scenes : [];
    const shotCount = scenes.reduce((n, sc) => n + ((sc.shots || []).length), 0);
    if (!shotCount) { panelBody.innerHTML = `<p class="placeholder">暂无大纲 — 让 Lumeri 起草分镜后在这里查看</p>`; return; }
    let html = "";
    if (sl.logline) html += `<p class="outline-logline">${escapeHTML(sl.logline)}</p>`;
    let no = 0;
    for (const sc of scenes) {
      if (sc.title) html += `<div class="outline-scene">${escapeHTML(sc.title)}</div>`;
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
          <div class="outline-row">
            <span class="outline-no ${st[1]}" title="${st[0]}">${no}</span>
            <span class="outline-main">
              <span class="outline-beat">${escapeHTML(shot.description || "(未命名镜头)")}</span>
              ${meta ? `<span class="outline-meta">${escapeHTML(meta)}</span>` : ""}
            </span>
          </div>`;
      }
    }
    panelBody.innerHTML = html;
  }

  // ── background tasks panel: GET /sessions (runners + pending jobs) ────
  async function renderTasksPanel() {
    let sessions = null;
    try {
      const r = await fetch("/sessions");
      if (r.ok) sessions = (await r.json()).sessions;
    } catch {}
    if (panelView !== "tasks") return;
    if (!Array.isArray(sessions)) { panelBody.innerHTML = `<p class="placeholder">读取失败</p>`; return; }
    if (!sessions.length) { panelBody.innerHTML = `<p class="placeholder">暂无运行中的会话</p>`; return; }
    panelBody.innerHTML = sessions.map((s) => {
      const mine = s.session_id === state.sessionId;
      const cls = s.turn_in_progress ? "running" : "";
      const stateTxt = s.turn_in_progress ? "执行中" : "空闲";
      const jobs = (s.pending_jobs || []).map((j) => `
        <div class="task-row task-job">
          <span class="task-dot ${j.last_polled_status === "failed" ? "failed" : "running"}"></span>
          <span class="task-main">
            <span class="task-name">${escapeHTML(j.summary || j.kind || j.job_id || "任务")}</span>
            <span class="task-sub">${escapeHTML([j.provider, j.last_polled_status].filter(Boolean).join(" · "))}</span>
          </span>
        </div>`).join("");
      return `
        <div class="task-row" title="${escapeHTML(s.session_id)}">
          <span class="task-dot ${cls}"></span>
          <span class="task-main">
            <span class="task-name">${escapeHTML(s.session_id)}${mine ? " · 当前" : ""}</span>
            <span class="task-sub">${stateTxt}${s.plan_mode ? " · 计划模式" : ""} · ${fmtAgo(s.last_used_at)}</span>
          </span>
        </div>${jobs}`;
    }).join("");
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
  async function renderFilesPanel() {
    if (!filesState) {
      let roots = [];
      try {
        const r = await fetch("/files/roots");
        if (r.ok) roots = (await r.json()).roots || [];
      } catch {}
      if (panelView !== "files") return;
      let html = "";
      if (state.sessionId) {
        html += `<button type="button" class="file-row" data-file-root="session">
          <svg viewBox="0 0 24 24" aria-hidden="true"><use href="#i-folder"/></svg>
          <span class="file-name">当前会话工作区</span></button>`;
      }
      html += roots.map((rt) => `
        <button type="button" class="file-row" data-file-root="${escapeHTML(rt.key)}">
          <svg viewBox="0 0 24 24" aria-hidden="true"><use href="#i-folder"/></svg>
          <span class="file-name">${escapeHTML(rt.label)}</span></button>`).join("");
      panelBody.innerHTML = html || `<p class="placeholder">暂无可浏览目录</p>`;
      return;
    }
    const { root, session, path } = filesState;
    const qs = `root=${encodeURIComponent(root)}&path=${encodeURIComponent(path)}${session ? `&session=${encodeURIComponent(session)}` : ""}`;
    let data = null;
    try {
      const r = await fetch(`/files/list?${qs}`);
      if (r.ok) data = await r.json();
    } catch {}
    if (panelView !== "files") return;
    if (!data) { panelBody.innerHTML = `<p class="placeholder">读取失败</p>`; return; }
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
    panelBody.innerHTML = `
      <div class="files-crumbs"><button type="button" data-file-crumb="__roots__" title="所有目录"><svg viewBox="0 0 24 24" aria-hidden="true" style="width:12px;height:12px"><use href="#i-chevron-l"/></svg></button>${crumbs}</div>
      ${rows || `<p class="placeholder">空目录</p>`}
      ${data.truncated ? `<p class="placeholder">（仅显示前 500 项）</p>` : ""}`;
  }
  panelBody?.addEventListener("click", (e) => {
    const rootBtn = e.target.closest("[data-file-root]");
    if (rootBtn) {
      const key = rootBtn.dataset.fileRoot;
      filesState = { root: key, session: key === "session" ? state.sessionId : "", path: "" };
      renderFilesPanel();
      return;
    }
    const crumb = e.target.closest("[data-file-crumb]");
    if (crumb) {
      if (crumb.dataset.fileCrumb === "__roots__") filesState = null;
      else filesState = { ...filesState, path: crumb.dataset.fileCrumb };
      renderFilesPanel();
      return;
    }
    const dir = e.target.closest("[data-file-dir]");
    if (dir) { filesState = { ...filesState, path: dir.dataset.fileDir }; renderFilesPanel(); return; }
    const file = e.target.closest("[data-file-open]");
    if (file) {
      const { root, session } = filesState;
      const qs = `root=${encodeURIComponent(root)}&path=${encodeURIComponent(file.dataset.fileOpen)}${session ? `&session=${encodeURIComponent(session)}` : ""}`;
      window.open(`/files/get?${qs}`, "_blank", "noopener");
    }
  });

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
    const r = await fetch(`/sessions/${state.sessionId}/plan_mode`, {
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
      const r = await fetch("/settings/sandbox");
      if (r.ok) renderSandbox(!!(await r.json()).sandbox_disabled);
    } catch {}
  }
  els.sandboxBtn.addEventListener("click", async () => {
    const next = !els.sandboxBtn.classList.contains("off");
    try {
      const r = await fetch("/settings/sandbox", {
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

  // boot
  createSession().catch((err) => {
    state.errors.push(`initial session failed: ${err.message}`);
    setConnPill("failed", "failed");
    render();
  });

  // teardown on page hide
  window.addEventListener("beforeunload", () => {
    stopTimelinePoll();
    if (state.sessionId) {
      navigator.sendBeacon?.(`/sessions/${state.sessionId}/close`);
    }
  });
})();
