(() => {
  "use strict";

  function requestId() {
    if (globalThis.crypto?.randomUUID) return `web-${globalThis.crypto.randomUUID()}`;
    return `web-${Date.now()}-${Math.random().toString(16).slice(2)}`;
  }

  function apiFetch(input, init = {}) {
    const headers = new Headers(init.headers || {});
    if (!headers.has("X-Request-ID")) headers.set("X-Request-ID", requestId());
    return globalThis.fetch(input, { ...init, headers });
  }

  globalThis.LumeriApi = Object.freeze({ fetch: apiFetch });
})();
