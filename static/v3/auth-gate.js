/* Shared Lumeri workspace boot gate.
 *
 * This file intentionally has no workspace dependencies.  It is the only
 * application logic allowed to run before the cloud account confirms that the
 * current account has completed onboarding.
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.LumeriAuthGate = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  const SESSION_PATH = "/auth/session";
  const DEVICE_START_PATH = "/auth/device/start";
  const DEVICE_TOKEN_PATH = "/auth/device/token";
  const DEFAULT_ACCOUNT_ORIGIN = "https://accounts.lumeri.io";

  function surfaceProductName(pathname) {
    return String(pathname || "").startsWith("/quanta") ? "Lumeri Quanta" : "Lumeri Video";
  }

  function workspaceAllowed(session) {
    if (!session || typeof session !== "object") return false;
    if (session.cloud_login_enabled !== true) return true;
    return !!session.account && session.account.onboarding_completed === true;
  }

  function responseError(result, fallback) {
    const data = result && result.data && typeof result.data === "object" ? result.data : {};
    return new Error(data.user_message || data.service_error || data.message || data.error || fallback);
  }

  async function waitForWorkspaceAccess(options) {
    const request = options && options.request;
    const view = options && options.view;
    const sleep = (options && options.sleep) || ((ms) => new Promise((resolve) => setTimeout(resolve, ms)));
    const openExternal = (options && options.openExternal) || (() => {});
    const accountOrigin = (options && options.accountOrigin) || DEFAULT_ACCOUNT_ORIGIN;
    if (typeof request !== "function" || !view) throw new TypeError("auth gate dependencies are missing");

    let session = null;
    for (;;) {
      if (!session) {
        view.showChecking();
        try {
          const result = await request("GET", SESSION_PATH);
          if (!result || result.ok !== true) throw responseError(result, "无法检查 Lumeri 账户");
          session = result.data || {};
          if (session.account_service_available === false) {
            throw new Error(session.service_error || "暂时无法连接 Lumeri 登录服务");
          }
        } catch (error) {
          view.showError(error && error.message ? error.message : "暂时无法连接 Lumeri 登录服务");
          await view.waitForAction("重试");
          session = null;
          continue;
        }
      }

      if (workspaceAllowed(session)) return session;

      if (session.account) {
        view.showOnboarding();
        await view.waitForAction("完成账户设置");
        openExternal(accountOrigin);
        view.showOnboarding();
        for (;;) {
          await sleep(2000);
          try {
            const result = await request("GET", SESSION_PATH);
            if (!result || result.ok !== true) throw responseError(result, "无法检查账户设置");
            session = result.data || {};
            if (session.account_service_available === false) {
              throw new Error(session.service_error || "暂时无法连接 Lumeri 登录服务");
            }
          } catch (error) {
            view.showError(error && error.message ? error.message : "暂时无法连接 Lumeri 登录服务");
            await view.waitForAction("重试");
            session = null;
            break;
          }
          if (workspaceAllowed(session) || !session.account) break;
        }
        continue;
      }

      view.showSignedOut();
      await view.waitForAction("继续登录");
      try {
        const started = await request("POST", DEVICE_START_PATH, {});
        if (!started || started.ok !== true) throw responseError(started, "无法开始登录");
        const device = started.data || {};
        if (!device.attempt_id || !device.verification_uri_complete) {
          throw new Error("登录服务没有返回授权页面");
        }
        view.showDevice(device);
        openExternal(device.verification_uri_complete);
        const intervalMs = Math.max(2000, Math.min(10000, Number(device.interval || 3) * 1000));
        for (;;) {
          await sleep(intervalMs);
          const token = await request("POST", DEVICE_TOKEN_PATH, { attempt_id: device.attempt_id });
          if (token && token.status === 202 && token.data && token.data.pending) continue;
          if (!token || token.ok !== true) throw responseError(token, "登录未完成");
          session = token.data || {};
          break;
        }
      } catch (error) {
        view.showError(error && error.message ? error.message : "登录未完成");
        await view.waitForAction("重试");
        session = null;
      }
    }
  }

  function createDomView(document) {
    const gate = document.getElementById("auth-gate");
    const title = document.getElementById("auth-gate-title");
    const message = document.getElementById("auth-gate-message");
    const action = document.getElementById("auth-gate-action");
    const status = document.getElementById("auth-gate-status");
    const code = document.getElementById("auth-gate-code");
    const link = document.getElementById("auth-gate-link");
    const error = document.getElementById("auth-gate-error");
    const productName = surfaceProductName(document.location && document.location.pathname);
    if (productName === "Lumeri Quanta" && document.documentElement && document.documentElement.classList) {
      document.documentElement.classList.add("quanta-surface");
      document.title = productName;
    }
    const brand = document.querySelector && document.querySelector(".app-auth-gate-brand");
    if (brand) brand.textContent = productName;
    if (productName === "Lumeri Quanta" && document.querySelector) {
      const mark = document.querySelector(".app-auth-gate-mark");
      const favicon = document.querySelector('link[rel~="icon"]');
      if (mark) {
        mark.src = "/video/quanta-mark.svg";
        mark.alt = "Lumeri Quanta";
      }
      if (favicon) favicon.href = "/video/quanta-favicon.svg";
    }
    if (!gate || !title || !message || !action || !status || !code || !link || !error) {
      throw new Error("Lumeri 登录界面不完整");
    }

    let actionResolve = null;
    const reset = () => {
      action.hidden = true;
      action.disabled = false;
      status.hidden = true;
      error.hidden = true;
      error.textContent = "";
      action.onclick = null;
      actionResolve = null;
    };

    return {
      showChecking() {
        reset();
        title.textContent = "";
        message.textContent = "";
        gate.dataset.state = "checking";
      },
      showSignedOut() {
        reset();
        title.textContent = `登录 ${productName}`;
        message.textContent = "使用 Google 或邮箱验证码继续。";
        gate.dataset.state = "signed-out";
      },
      showOnboarding() {
        reset();
        title.textContent = "完成账户设置";
        message.textContent = `完成设置后即可进入 ${productName}。`;
        gate.dataset.state = "onboarding";
      },
      showDevice(device) {
        reset();
        title.textContent = "在浏览器中继续";
        message.textContent = "完成登录后，此页面会自动进入工作区。";
        code.textContent = String(device.user_code || "");
        link.href = String(device.verification_uri_complete || DEFAULT_ACCOUNT_ORIGIN);
        status.hidden = false;
        gate.dataset.state = "waiting";
      },
      showError(text) {
        reset();
        title.textContent = "暂时无法登录";
        message.textContent = "请检查网络后重试。";
        error.textContent = String(text || "Lumeri 登录服务暂不可用");
        error.hidden = false;
        gate.dataset.state = "error";
      },
      waitForAction(label) {
        if (actionResolve) actionResolve();
        action.hidden = false;
        action.textContent = String(label || "继续");
        return new Promise((resolve) => {
          actionResolve = resolve;
          action.onclick = () => {
            action.disabled = true;
            action.onclick = null;
            actionResolve = null;
            resolve();
          };
        });
      },
    };
  }

  function revealWorkspace(document) {
    const gate = document.getElementById("auth-gate");
    for (const id of ["app-header", "app-main"]) {
      const element = document.getElementById(id);
      if (!element) continue;
      element.hidden = false;
      element.removeAttribute("inert");
      element.removeAttribute("aria-hidden");
    }
    if (gate) gate.hidden = true;
    document.documentElement.classList.add("workspace-ready");
  }

  return {
    SESSION_PATH,
    DEVICE_START_PATH,
    DEVICE_TOKEN_PATH,
    workspaceAllowed,
    surfaceProductName,
    waitForWorkspaceAccess,
    createDomView,
    revealWorkspace,
  };
});
