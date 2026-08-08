/* Lumeri v3 model selection and provider setup. */
(() => {
  function createSettings({
    $, apiFetch, escapeHTML, normalizedByokProvider, getCurrentAuthSession,
  }) {
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
      overlay.className = "auth-modal";
      overlay.hidden = true;
      overlay.innerHTML = `
        <div class="model-backdrop" data-model-close></div>
        <div class="auth-dialog model-dialog" role="dialog" aria-modal="true" aria-labelledby="model-title">
          <button type="button" class="auth-x" data-model-close aria-label="关闭">×</button>
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
          <p class="auth-error" id="setup-error" hidden></p>
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
      const current = p.id === st.curProvider;
      const hint = p.hint ? `<span class="setup-phint">${escapeHTML(p.hint)}</span>` : "";
      const badge = current ? '<span class="setup-current">当前</span>' : "";
      return `<div class="setup-pcard${active ? " active" : ""}" data-pid="${escapeHTML(p.id)}" draggable="true">
        <span class="setup-drag" title="拖动排序">☰</span>
        <div class="setup-ptext">
          <span class="setup-pname">${escapeHTML(label)}</span>
          ${hint}
        </div>
        ${badge}
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
      const allowed = new Set(st.info.allowed_providers || []);
      return st.providerOrder
        .map((id) => (st.info.providers || []).find((x) => x.id === id))
        .filter((provider) => provider && (
          !allowed.size
          || allowed.has(normalizedByokProvider(provider.provider || provider.id))
        ))
        // The running account service can briefly lag static assets after a
        // local source update. Credits is selection-only until billing ships,
        // so never render a stale settlement claim from its config payload.
        .map((provider) => normalizedByokProvider(provider.provider || provider.id) === "lumeri"
          ? { ...provider, hint: "仅保存供应商偏好；Credits 结算功能后续开放" }
          : provider);
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
      if (st.info?.cloud_account_mode && st.curProvider && p.id !== st.curProvider) {
        spinner.hidden = true;
        return;
      }
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
      const testButton = $("#setup-test", overlay);
      const saveButton = $("#setup-save", overlay);
      testButton.hidden = providerId === "lumeri";
      saveButton.textContent = providerId === "lumeri" ? "选择 Lumeri Credits" : "保存并启用";

      if (providerId === "lumeri") {
        box.innerHTML = `<div class="setup-provider-note">
          <strong>Lumeri Credits</strong>
          <span>当前仅保存供应商偏好；Credits 结算功能将在后续开放。</span>
        </div>`;
        return;
      }

      if (p.hint) {
        const note = document.createElement("p");
        note.className = "setup-provider-copy";
        note.textContent = p.hint;
        box.appendChild(note);
      }

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

      if (providerId === "openai_subscription") {
        const login = document.createElement("div");
        login.className = "setup-codex-login";
        login.innerHTML = `<button type="button" class="setup-test setup-codex-login-btn">登录 Codex</button>
          <span class="setup-codex-login-status" aria-live="polite">使用 ChatGPT 订阅额度</span>`;
        box.appendChild(login);
        const button = login.querySelector(".setup-codex-login-btn");
        const status = login.querySelector(".setup-codex-login-status");
        button.addEventListener("click", () => doCodexLogin(button, status));
      }

      box.querySelectorAll("input[data-f]").forEach((inp) => {
        if (inp.dataset.f !== "model") inp.addEventListener("input", () => { st.vals[inp.dataset.f] = inp.value; });
      });
      box.querySelectorAll("[data-eff]").forEach((b) =>
        b.addEventListener("click", () => {
          st.vals.effort = b.dataset.eff;
          box.querySelectorAll("[data-eff]").forEach((x) => x.classList.toggle("active", x === b));
        }));
    }

    async function doCodexLogin(button, statusEl) {
      setErr(""); setRes("");
      button.disabled = true;
      button.textContent = "正在登录…";
      statusEl.textContent = "正在创建安全登录链接…";
      const popup = window.open("about:blank", "lumeri-codex-login", "popup,width=720,height=820");
      if (popup) {
        popup.opener = null;
        popup.document.title = "Codex 登录";
        popup.document.body.textContent = "正在打开 ChatGPT 登录…";
      }
      try {
        const response = await apiFetch("/config/codex-login", { method: "POST" });
        const started = await response.json().catch(() => ({}));
        if (!response.ok || !started.authorization_url) {
          throw new Error(started.error || `HTTP ${response.status}`);
        }
        const authUrl = new URL(started.authorization_url);
        if (authUrl.origin !== "https://auth.openai.com") throw new Error("登录地址不是 OpenAI 官方地址");
        if (popup) {
          popup.location.replace(authUrl.href);
          statusEl.textContent = "请在新窗口完成 ChatGPT 登录…";
        } else {
          statusEl.textContent = "登录窗口被拦截：";
          const link = document.createElement("a");
          link.href = authUrl.href;
          link.target = "_blank";
          link.rel = "noopener noreferrer";
          link.textContent = "继续登录";
          statusEl.appendChild(link);
        }

        for (let i = 0; i < 300; i++) {
          await new Promise((resolve) => setTimeout(resolve, 1000));
          const poll = await apiFetch("/config/codex-login-status");
          const auth = await poll.json().catch(() => ({}));
          if (!poll.ok) throw new Error(auth.error || `HTTP ${poll.status}`);
          if (auth.state === "success") {
            statusEl.textContent = `登录成功${auth.email ? `：${auth.email}` : ""}`;
            button.textContent = "重新登录 Codex";
            button.disabled = false;
            const modelInput = $("input[data-f='model']", $("#setup-fields", overlay));
            const modelWrap = modelInput?.closest(".setup-model-wrap");
            if (modelInput && modelWrap) {
              autoScanModels(
                modelInput,
                $(".setup-model-list", modelWrap),
                $(".setup-model-spinner", modelWrap),
                getProviders().find((p) => p.id === st.sel),
              );
            }
            return;
          }
          if (auth.state === "error") throw new Error(auth.error || "Codex 登录失败");
        }
        throw new Error("登录等待超时，请重试");
      } catch (e) {
        if (popup && popup.location.href === "about:blank") popup.close();
        statusEl.textContent = `登录失败：${e.message}`;
        button.textContent = "重新登录 Codex";
        button.disabled = false;
      }
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
        if (!r.ok) throw new Error(d.user_message || d.message || d.error || `HTTP ${r.status}`);
        st.curProvider = st.sel;
        const currentAuthSession = getCurrentAuthSession();
        if (currentAuthSession?.account) {
          currentAuthSession.account.model_provider = d.selected_provider || st.sel;
          currentAuthSession.account.provider_mode = d.provider_mode || currentAuthSession.account.provider_mode;
        }
        renderProviders();
        setRes(
          st.sel === "lumeri"
            ? "已保存 Lumeri Credits 供应商偏好 ✓（Credits 结算功能后续开放）"
            : "已保存并启用 ✓（新会话生效）",
          true,
        );
      } catch (e) { setRes(""); setErr(`保存失败：${e.message}`); }
    }

    async function doTest() {
      setErr(""); setRes("测试中…（可能需数秒）", true);
      try {
        const r = await apiFetch("/config/test-brain", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(buildBody()) });
        const d = await r.json().catch(() => ({}));
        if (d.ok) setRes(`连接成功 ✓ ${d.provider}/${d.model} — 回样「${d.sample || ""}」`, true);
        else setRes(`连接失败：${d.user_message || d.message || d.error || "未知错误"}（${d.provider || ""}/${d.model || ""}）`, false);
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
    let allowedSearchProviders = null;
    const searchResEl = $("#search-result", overlay);
    const setSearchRes = (m, ok) => {
      if (!m) { searchResEl.hidden = true; return; }
      searchResEl.textContent = m; searchResEl.hidden = false;
      searchResEl.className = "setup-result " + (ok ? "ok" : "bad");
    };

    function renderSearchChips(searchInfo) {
      const chips = $("#search-provider-chips", overlay);
      const allowed = Array.isArray(searchInfo?.allowed_providers)
        ? new Set(searchInfo.allowed_providers)
        : null;
      allowedSearchProviders = allowed;
      if (allowed && !allowed.has(searchSel)) searchSel = allowed.has("auto") ? "auto" : "duckduckgo";
      const providers = allowed ? SEARCH_PROVIDERS.filter((sp) => allowed.has(sp.id)) : SEARCH_PROVIDERS;
      chips.innerHTML = providers.map((sp) => {
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
        const autoOrder = SEARCH_PROVIDERS
          .filter((provider) => provider.id !== "auto" && (!allowedSearchProviders || allowedSearchProviders.has(provider.id)))
          .map((provider) => provider.label)
          .join(" → ");
        box.innerHTML = sp && sp.id === "auto"
          ? `<p class="search-hint">自动模式按优先级探测：${escapeHTML(autoOrder)}</p>`
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
        if (!r.ok) throw new Error(d.user_message || d.message || d.error || `HTTP ${r.status}`);
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
      if (!info) {
        setErr("需先登录才能配置供应商");
        return;
      }
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
      const selectedProvider = cfg.selected_provider || "";
      const preferred = selectedProvider === "custom"
        ? info.active_profile
        : selectedProvider || info.active_profile || info.provider || "openrouter";
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


    return { openModelPicker, openSetupPanel };
  }

  window.LumeriV3Settings = Object.freeze({ createSettings });
})();
