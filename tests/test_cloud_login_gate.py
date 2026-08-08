from __future__ import annotations

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _run_gate_scenario(source: str) -> dict:
    completed = subprocess.run(
        ["node", "-e", source, str(ROOT / "static/v3/auth-gate.js")],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


def test_workspace_is_hidden_in_html_until_the_auth_gate_grants_access():
    html = (ROOT / "static/v3/index.html").read_text(encoding="utf-8")
    source = (ROOT / "static/v3/v3.js").read_text(encoding="utf-8")
    gate_source = (ROOT / "static/v3/auth-gate.js").read_text(encoding="utf-8")
    css = (ROOT / "static/v3/v3.css").read_text(encoding="utf-8")

    assert 'id="auth-gate"' in html
    assert 'data-state="checking"' in html
    assert "正在检查账户" not in html
    assert "正在检查账户" not in gate_source
    assert 'message.textContent = "";' in gate_source
    assert '.app-auth-gate[data-state="checking"] .app-auth-gate-content > :not(.app-auth-gate-mark)' in css
    assert 'id="app-header" hidden inert aria-hidden="true"' in html
    assert 'id="app-main" hidden inert aria-hidden="true"' in html
    assert html.index('/video/auth-gate.js') < html.index('/video/v3.js')
    assert source.index("await authGate.waitForWorkspaceAccess") < source.index('apiFetch("/video/icons.svg")')
    assert source.index("await authGate.waitForWorkspaceAccess") < source.index("restoreCurrentSessionOrCreate()")


def test_auth_gate_names_the_shared_shell_from_its_surface_path():
    result = _run_gate_scenario(
        r"""
const gate = require(process.argv[1]);
console.log(JSON.stringify({
  video: gate.surfaceProductName("/video"),
  quanta: gate.surfaceProductName("/quanta"),
  quantaDemo: gate.surfaceProductName("/quanta/demo"),
}));
"""
    )

    assert result == {
        "video": "Lumeri Video",
        "quanta": "Lumeri Quanta",
        "quantaDemo": "Lumeri Quanta",
    }


def test_signed_out_gate_calls_only_auth_endpoints_before_completed_onboarding():
    result = _run_gate_scenario(
        r"""
const gate = require(process.argv[1]);
const calls = [];
let tokenPolls = 0;
const request = async (method, path, body) => {
  calls.push([method, path, body || null]);
  if (path === gate.SESSION_PATH) {
    return {ok: true, status: 200, data: {cloud_login_enabled: true, account: null}};
  }
  if (path === gate.DEVICE_START_PATH) {
    return {ok: true, status: 200, data: {
      attempt_id: "attempt", user_code: "SAFE-CODE",
      verification_uri_complete: "https://accounts.lumeri.io/", interval: 2,
    }};
  }
  tokenPolls += 1;
  if (tokenPolls === 1) return {ok: false, status: 202, data: {pending: true}};
  return {ok: true, status: 200, data: {
    cloud_login_enabled: true,
    account: {id: "account", onboarding_completed: true, age_band: "18_plus"},
  }};
};
const view = {
  showChecking() {}, showSignedOut() {}, showOnboarding() {}, showDevice() {}, showError() {},
  async waitForAction() {},
};
gate.waitForWorkspaceAccess({request, view, sleep: async () => {}, openExternal: () => {}})
  .then((session) => console.log(JSON.stringify({calls, allowed: gate.workspaceAllowed(session)})));
"""
    )

    assert result["allowed"] is True
    assert [call[1] for call in result["calls"]] == [
        "/auth/session",
        "/auth/device/start",
        "/auth/device/token",
        "/auth/device/token",
    ]
    assert not any(
        path.startswith(("/sessions", "/projects", "/session-history", "/media-library"))
        for _, path, _ in result["calls"]
    )


def test_gate_requires_exact_completed_flag_and_fails_closed_on_auth_error():
    result = _run_gate_scenario(
        r"""
const gate = require(process.argv[1]);
const calls = [];
const denied = [
  gate.workspaceAllowed({cloud_login_enabled: true, account: {}}),
  gate.workspaceAllowed({cloud_login_enabled: true, account: {onboarding_completed: false}}),
  gate.workspaceAllowed({cloud_login_enabled: true, account: {onboarding_completed: 1}}),
];
const request = async (method, path) => {
  calls.push([method, path]);
  return {ok: false, status: 503, data: {error: "account service unavailable"}};
};
const view = {
  showChecking() {}, showSignedOut() {}, showOnboarding() {}, showDevice() {}, showError() {},
  async waitForAction() { throw new Error("test stop"); },
};
gate.waitForWorkspaceAccess({request, view, sleep: async () => {}})
  .then(() => { throw new Error("gate unexpectedly opened"); })
  .catch((error) => console.log(JSON.stringify({calls, denied, stopped: error.message})));
"""
    )

    assert result == {
        "calls": [["GET", "/auth/session"]],
        "denied": [False, False, False],
        "stopped": "test stop",
    }


def test_completed_account_reveals_the_workspace_once():
    result = _run_gate_scenario(
        r"""
const gate = require(process.argv[1]);
let calls = 0;
const request = async () => {
  calls += 1;
  return {ok: true, status: 200, data: {
    cloud_login_enabled: true,
    account: {id: "account", onboarding_completed: true, age_band: "13_17"},
  }};
};
const view = {
  showChecking() {}, showSignedOut() {}, showOnboarding() {}, showDevice() {}, showError() {},
  async waitForAction() { throw new Error("should not wait"); },
};
const elements = {
  "auth-gate": {hidden: false},
  "app-header": {hidden: true, removeAttribute(name) { delete this[name]; }},
  "app-main": {hidden: true, removeAttribute(name) { delete this[name]; }},
};
const document = {
  getElementById(id) { return elements[id] || null; },
  documentElement: {classList: {add(value) { this.value = value; }}},
};
gate.waitForWorkspaceAccess({request, view})
  .then((session) => {
    gate.revealWorkspace(document);
    console.log(JSON.stringify({
      calls,
      allowed: gate.workspaceAllowed(session),
      gateHidden: elements["auth-gate"].hidden,
      headerHidden: elements["app-header"].hidden,
      mainHidden: elements["app-main"].hidden,
    }));
  });
"""
    )

    assert result == {
        "calls": 1,
        "allowed": True,
        "gateHidden": True,
        "headerHidden": False,
        "mainHidden": False,
    }
