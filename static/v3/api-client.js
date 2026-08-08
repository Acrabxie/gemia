(() => {
  "use strict";

  const VERSIONED_ROOTS = new Set([
    "auth",
    "config",
    "files",
    "media-library",
    "model",
    "projects",
    "session-history",
    "sessions",
    "settings",
    "starter-recommendations",
  ]);

  function requestId() {
    if (globalThis.crypto?.randomUUID) return `web-${globalThis.crypto.randomUUID()}`;
    return `web-${Date.now()}-${Math.random().toString(16).slice(2)}`;
  }

  function versionedUrl(input) {
    if (typeof input !== "string" || !input.startsWith("/") || input.startsWith("/api/")) {
      return input;
    }
    const root = input.slice(1).split(/[/?#]/, 1)[0];
    return VERSIONED_ROOTS.has(root) ? `/api/v1${input}` : input;
  }

  async function apiFetch(input, init = {}) {
    const headers = new Headers(init.headers || {});
    if (!headers.has("X-Request-ID")) headers.set("X-Request-ID", requestId());
    return globalThis.fetch(versionedUrl(input), { ...init, headers });
  }

  globalThis.LumeriApi = Object.freeze({
    fetch: apiFetch,
    versionedUrl,
  });
})();
