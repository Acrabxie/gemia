/* Lumeri v3 account menu and sign-in flows. */
(() => {
  function createAuth({
    $, apiFetch, byokAllowed, getCurrentAuthSession, setCurrentAuthSession,
    resetSkillCloudState, openModelPicker, openSetupPanel, els, slashSync,
  }) {
  // ── account / login (Google + email one-time-code) ──────────────────
  function setupAuth(initialSession) {
    const modal = $("#auth-modal");
    const accountBtn = $("#account-btn");
    if (!modal || !accountBtn) return;
    const accountMenu = $("#account-menu");
    const menuAvatar = $("#account-menu-avatar");
    const menuName = $("#account-menu-name");
    const menuEmail = $("#account-menu-email");
    const menuSetup = accountMenu?.querySelector('[data-account-action="setup"]');
    const viewSignin = $("#auth-view-signin");
    const viewAccount = $("#auth-view-account");
    const cloudFlow = $("#auth-cloud-flow");
    const cloudStartBtn = $("#auth-cloud-start");
    const cloudStatus = $("#auth-cloud-status");
    const cloudCode = $("#auth-cloud-code");
    const cloudOpen = $("#auth-cloud-open");
    const googleBtn = $("#auth-google-btn");
    const divider = $("#auth-divider");
    const emailForm = $("#auth-email-form");
    const codeForm = $("#auth-code-form");
    const emailInput = $("#auth-email");
    const codeInput = $("#auth-code");
    const sendBtn = $("#auth-send-code");
    const verifyBtn = $("#auth-verify");
    const resendBtn = $("#auth-resend");
    const changeBtn = $("#auth-change-email");
    const codeTarget = $("#auth-code-target");
    const errBox = $("#auth-error");
    const logoutBtn = $("#auth-logout");
    const acctEmail = $("#auth-account-email");
    const avatar = $("#auth-avatar");

    let session = initialSession || null;
    let pendingEmail = "";
    let resendTimer = null;
    let devicePollTimer = null;
    let devicePopup = null;

    const showErr = (msg) => { errBox.textContent = msg || ""; errBox.hidden = !msg; };
    const clearErr = () => showErr("");

    function renderMenuAvatar(acct) {
      if (!menuAvatar) return;
      const fallback = (acct.email || acct.name || "?").trim().charAt(0).toUpperCase();
      menuAvatar.textContent = fallback;
      if (!acct.picture) return;
      menuAvatar.innerHTML = "";
      const img = document.createElement("img");
      img.className = "account-photo";
      img.alt = "";
      img.referrerPolicy = "no-referrer";
      img.src = acct.picture;
      img.onerror = () => { menuAvatar.textContent = fallback; };
      menuAvatar.appendChild(img);
    }

    function renderAccountMenu() {
      const acct = session && session.account;
      if (!acct || !menuName || !menuEmail) return;
      renderMenuAvatar(acct);
      menuName.textContent = acct.name || (acct.email || "Lumeri account").split("@")[0];
      menuEmail.textContent = acct.email || "Signed in";
      if (menuSetup) {
        menuSetup.hidden = !byokAllowed();
      }
    }

    function openAccountMenu() {
      if (!accountMenu || !(session && session.account)) return;
      renderAccountMenu();
      accountMenu.hidden = false;
      accountBtn.setAttribute("aria-expanded", "true");
      requestAnimationFrame(() => accountMenu.querySelector('[role="menuitem"]')?.focus({ preventScroll: true }));
    }

    function closeAccountMenu({ restoreFocus = false } = {}) {
      if (!accountMenu) return;
      accountMenu.hidden = true;
      accountBtn.setAttribute("aria-expanded", "false");
      if (restoreFocus) accountBtn.focus();
    }

    // Signed in = Google photo when present, else round initial badge;
    // signed out = person icon. Email lives in title.
    function applySession(data) {
      const priorAccountId = String(
        getCurrentAuthSession()?.account?.cloud_account_id
        || getCurrentAuthSession()?.account?.account_id
        || getCurrentAuthSession()?.account?.email
        || ""
      );
      session = data || {};
      setCurrentAuthSession(session);
      const acct = session.account;
      const nextAccountId = String(
        acct?.cloud_account_id || acct?.account_id || acct?.email || ""
      );
      if (priorAccountId !== nextAccountId) resetSkillCloudState();
      if (acct && acct.email) {
        if (acct.picture) {
          accountBtn.innerHTML = "";
          const img = document.createElement("img");
          img.className = "account-photo";
          img.alt = "";
          img.referrerPolicy = "no-referrer";
          img.src = acct.picture;
          img.onerror = () => { accountBtn.textContent = acct.email.trim().charAt(0).toUpperCase(); };
          accountBtn.appendChild(img);
        } else {
          accountBtn.textContent = acct.email.trim().charAt(0).toUpperCase();
        }
        accountBtn.title = acct.email;
        accountBtn.setAttribute("aria-label", `账户：${acct.email}`);
        accountBtn.classList.add("signed-in");
      } else {
        accountBtn.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><use href="#i-user"/></svg>';
        accountBtn.title = "登录 / 账户";
        accountBtn.setAttribute("aria-label", "登录 / 账户");
        accountBtn.classList.remove("signed-in");
        closeAccountMenu();
      }
    }

    async function refreshSession() {
      try {
        const r = await apiFetch("/auth/session");
        const data = await r.json();
        applySession(data);
      } catch {}
      return session;
    }

    async function postAuth(url, body) {
      const r = await apiFetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body || {}),
      });
      let data = {};
      try { data = await r.json(); } catch {}
      if (!r.ok) throw new Error(data.user_message || data.error || `请求失败 (${r.status})`);
      return data;
    }

    function stopResend() { if (resendTimer) { clearInterval(resendTimer); resendTimer = null; } }
    function stopDevicePoll() { if (devicePollTimer) { clearTimeout(devicePollTimer); devicePollTimer = null; } }
    function startResend(secs) {
      stopResend();
      let left = secs;
      const tick = () => {
        resendBtn.disabled = left > 0;
        resendBtn.textContent = left > 0 ? `重新发送（${left}s）` : "重新发送";
        if (left <= 0) { stopResend(); return; }
        left -= 1;
      };
      tick();
      resendTimer = setInterval(tick, 1000);
    }

    function showEmailStep() { emailForm.hidden = false; codeForm.hidden = true; stopResend(); }
    function showCodeStep(email) {
      pendingEmail = email;
      codeTarget.textContent = email;
      emailForm.hidden = true;
      codeForm.hidden = false;
      codeInput.value = "";
      startResend(60);
      codeInput.focus();
    }

    function renderModal() {
      clearErr();
      const acct = session && session.account;
      viewAccount.hidden = !acct;
      viewSignin.hidden = !!acct;
      const cloudLogin = !!(session && session.cloud_login_enabled);
      modal.classList.toggle("auth-required", cloudLogin && !acct);
      if (acct) {
        stopDevicePoll();
        acctEmail.textContent = acct.email || acct.name || "已登录";
        if (acct.picture) {
          avatar.classList.add("has-photo");
          avatar.innerHTML = "";
          const img = document.createElement("img");
          img.className = "account-photo";
          img.alt = "";
          img.referrerPolicy = "no-referrer";
          img.src = acct.picture;
          img.onerror = () => {
            avatar.classList.remove("has-photo");
            avatar.textContent = (acct.email || acct.name || "?").trim().charAt(0).toUpperCase();
          };
          avatar.appendChild(img);
        } else {
          avatar.classList.remove("has-photo");
          avatar.textContent = (acct.email || acct.name || "?").trim().charAt(0).toUpperCase();
        }
        return;
      }
      cloudFlow.hidden = !cloudLogin;
      if (cloudLogin) {
        googleBtn.hidden = true;
        divider.hidden = true;
        emailForm.hidden = true;
        codeForm.hidden = true;
        if (session.account_service_available === false) {
          showErr(session.service_error || "暂时无法连接 Lumeri 登录服务");
        }
        return;
      }
      const hasGoogle = !!(session && session.has_google_client_id);
      googleBtn.hidden = !hasGoogle;
      divider.hidden = !hasGoogle;
      showEmailStep();
    }

    function openModal() {
      renderModal();
      modal.hidden = false;
      if (!(session && session.account)) {
        if (session && session.cloud_login_enabled) cloudStartBtn.focus();
        else emailInput.focus();
      }
    }
    function closeModal() {
      if (session && session.cloud_login_enabled && !session.account) return;
      modal.hidden = true; stopResend(); stopDevicePoll(); clearErr();
    }

    async function requestCode(email) {
      clearErr();
      sendBtn.disabled = true; sendBtn.textContent = "发送中…";
      try {
        await postAuth("/auth/email/start", { email });
        showCodeStep(email);
      } catch (e) {
        showErr(e.message);
      } finally {
        sendBtn.disabled = false; sendBtn.textContent = "发送验证码";
      }
    }

    accountBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      if (session && session.account) {
        modal.hidden = true;
        accountMenu.hidden ? openAccountMenu() : closeAccountMenu();
        return;
      }
      closeAccountMenu();
      modal.hidden ? openModal() : closeModal();
    });
    modal.querySelectorAll("[data-auth-close]").forEach((el) => el.addEventListener("click", closeModal));
    document.addEventListener("click", (e) => {
      if (!accountMenu || accountMenu.hidden) return;
      if (accountMenu.contains(e.target) || accountBtn.contains(e.target)) return;
      closeAccountMenu();
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && accountMenu && !accountMenu.hidden) {
        closeAccountMenu({ restoreFocus: true });
        return;
      }
      if (e.key === "Escape" && !modal.hidden) closeModal();
    });

    accountMenu?.addEventListener("keydown", (e) => {
      const items = [...accountMenu.querySelectorAll('[role="menuitem"]')];
      const current = items.indexOf(document.activeElement);
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        const step = e.key === "ArrowDown" ? 1 : -1;
        items[(current + step + items.length) % items.length]?.focus();
      } else if (e.key === "Home" || e.key === "End") {
        e.preventDefault();
        items[e.key === "Home" ? 0 : items.length - 1]?.focus();
      }
    });

    accountMenu?.addEventListener("click", async (e) => {
      const item = e.target.closest("[data-account-action]");
      if (!item) return;
      const action = item.dataset.accountAction;
      closeAccountMenu();
      if (action === "settings") { openModelPicker(); return; }
      if (action === "setup") { openSetupPanel(); return; }
      if (action === "help") {
        els.promptInput.value = "/";
        slashSync();
        els.promptInput.focus();
        return;
      }
      if (action === "logout") {
        try {
          applySession(await postAuth("/auth/logout", {}));
          if (session && session.cloud_login_enabled && !session.account) {
            window.location.reload();
            return;
          }
        } catch {}
      }
    });

    async function pollDeviceLogin(attemptId, intervalSeconds) {
      stopDevicePoll();
      try {
        const r = await apiFetch("/auth/device/token", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ attempt_id: attemptId }),
        });
        let data = {};
        try { data = await r.json(); } catch {}
        if (r.status === 202 && data.pending) {
          devicePollTimer = setTimeout(() => pollDeviceLogin(attemptId, intervalSeconds), intervalSeconds * 1000);
          return;
        }
        if (!r.ok) throw new Error(data.user_message || data.error || `登录失败 (${r.status})`);
        applySession(data);
        renderModal();
        try { devicePopup && devicePopup.close(); } catch {}
        setTimeout(closeModal, 450);
      } catch (error) {
        cloudStartBtn.disabled = false;
        cloudStartBtn.textContent = "重新登录";
        showErr(error.message || "登录未完成");
      }
    }

    cloudStartBtn?.addEventListener("click", async () => {
      clearErr();
      stopDevicePoll();
      cloudStartBtn.disabled = true;
      cloudStartBtn.textContent = "正在打开…";
      try {
        const data = await postAuth("/auth/device/start", {});
        if (!data.attempt_id || !data.verification_uri_complete) throw new Error("登录服务没有返回授权页面");
        cloudCode.textContent = data.user_code || "";
        cloudOpen.href = data.verification_uri_complete;
        cloudStatus.hidden = false;
        devicePopup = window.open(data.verification_uri_complete, "lumeri-account-login", "width=520,height=720");
        cloudStartBtn.textContent = "等待确认…";
        const interval = Math.max(2, Number(data.interval) || 3);
        devicePollTimer = setTimeout(() => pollDeviceLogin(data.attempt_id, interval), interval * 1000);
      } catch (error) {
        cloudStartBtn.disabled = false;
        cloudStartBtn.textContent = "继续登录";
        showErr(error.message || "无法开始登录");
      }
    });

    googleBtn.addEventListener("click", async () => {
      clearErr();
      try {
        const data = await postAuth("/auth/google/start", {});
        if (!data.authorization_url) throw new Error("Google 登录未配置");
        const win = window.open(data.authorization_url, "lumeri-google-login", "width=480,height=640");
        const onMsg = async (ev) => {
          if (ev.origin !== location.origin) return;
          if (!ev.data || ev.data.type !== "lumeri-auth-complete") return;
          window.removeEventListener("message", onMsg);
          try { win && win.close(); } catch {}
          await refreshSession();
          if (session && session.account) { renderModal(); setTimeout(closeModal, 600); }
          else showErr("Google 登录未完成");
        };
        window.addEventListener("message", onMsg);
      } catch (e) { showErr(e.message); }
    });

    emailForm.addEventListener("submit", (e) => {
      e.preventDefault();
      const v = emailInput.value.trim();
      if (v) requestCode(v);
    });
    resendBtn.addEventListener("click", () => { if (pendingEmail) requestCode(pendingEmail); });
    changeBtn.addEventListener("click", () => { showEmailStep(); clearErr(); emailInput.focus(); });

    codeForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      clearErr();
      const code = codeInput.value.replace(/\D/g, "");
      if (code.length !== 6) { showErr("请输入 6 位数字验证码"); return; }
      verifyBtn.disabled = true; verifyBtn.textContent = "登录中…";
      try {
        const data = await postAuth("/auth/email/verify", { email: pendingEmail, code });
        applySession(data);
        renderModal();
        setTimeout(closeModal, 500);
      } catch (e2) {
        showErr(e2.message);
      } finally {
        verifyBtn.disabled = false; verifyBtn.textContent = "登录";
      }
    });

    logoutBtn.addEventListener("click", async () => {
      try { applySession(await postAuth("/auth/logout", {})); } catch {}
      if (session && session.cloud_login_enabled && !session.account) {
        window.location.reload();
        return;
      }
      renderModal();
      if (session && session.cloud_login_enabled && !session.account) openModal();
    });

    const initialRefresh = session ? Promise.resolve(session) : refreshSession();
    initialRefresh.then(() => {
      const params = new URLSearchParams(location.search || "");
      if ((session && session.cloud_login_enabled && !session.account) || params.get("login") === "1" || params.get("auth") === "1") openModal();
    });
  }
    return { setupAuth };
  }

  window.LumeriV3Auth = Object.freeze({ createAuth });
})();
