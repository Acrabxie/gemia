"""Drift tests for the v3 protocol contract (docs/protocol-parity-plan.md P1).

The contract module is the single source of truth; these tests red when:
- an emit site produces a kind the contract doesn't declare (or stops using a
  literal kind string, which would blind this extraction);
- the web handler table misses a declared kind (silent drop = bug);
- the exported contract.json copies are stale vs the module.

Scope note: extraction is pinned to the four real emit files. A repo-wide
grep would wrongly ingest gemini_client.py's INTERNAL stream vocabulary
(text_delta, tool_call_start, ...), creative-sandbox kinds, and stale .bak
files — that inventory trap is why the file list is explicit.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from gemia import v3_contract

REPO = Path(__file__).resolve().parent.parent

# The ONLY files allowed to emit session SSE events. Adding an emit site
# elsewhere? Add the file here and declare its kinds in the contract first.
EMIT_FILES = [
    "gemia/agent_loop_v3.py",
    "gemia/session_manager.py",
    "gemia/transport/sse.py",
    "gemia/tools/_ask_bridge.py",
    "gemia/v3_routes.py",
    # Multi-agent fan-out emits subagent_start / subagent_result and re-emits the
    # existing tool_exec_* kinds (with agent_id) for child tool activity.
    "gemia/subtasks.py",
]

_KIND_LITERAL = re.compile(r'"kind":\s*"([a-z_]+)"')


def _emitted_kinds() -> set[str]:
    kinds: set[str] = set()
    for rel in EMIT_FILES:
        kinds |= set(_KIND_LITERAL.findall((REPO / rel).read_text(encoding="utf-8")))
    return kinds


def test_every_emitted_kind_is_declared() -> None:
    emitted = _emitted_kinds()
    # Extraction sanity: an empty/tiny set means the regex went blind — fail
    # loudly instead of vacuously passing.
    assert len(emitted) >= 18, f"kind extraction looks broken: {sorted(emitted)}"
    undeclared = emitted - v3_contract.EVENT_KINDS
    assert not undeclared, (
        f"emitted but not in v3_contract.EVENT_KINDS: {sorted(undeclared)} — "
        "declare new kinds in the contract BEFORE emitting them"
    )


def test_every_declared_kind_is_emitted_somewhere() -> None:
    stale = (
        v3_contract.EVENT_KINDS
        - v3_contract.RECEIVE_ONLY_EVENT_KINDS
        - _emitted_kinds()
    )
    assert not stale, f"declared but never emitted (stale contract?): {sorted(stale)}"


def test_receive_only_kinds_are_declared_but_not_emitted() -> None:
    assert v3_contract.RECEIVE_ONLY_EVENT_KINDS <= v3_contract.EVENT_KINDS
    assert not (v3_contract.RECEIVE_ONLY_EVENT_KINDS & _emitted_kinds())


def test_timeline_op_spread_cannot_override_kind() -> None:
    """agent_loop_v3 builds one event as {"kind": "timeline_op", **info}; if
    info ever carried a 'kind' key it would silently rename the event. Pin
    that the on_patch info dict is built without a 'kind' key."""
    source = (REPO / "gemia/project_store.py").read_text(encoding="utf-8")
    on_patch_info = re.search(r"info = \{(.*?)\}", source, re.DOTALL)
    assert on_patch_info is not None, "ProjectHandle.on_patch info dict moved"
    assert '"kind"' not in on_patch_info.group(1)


def test_web_handler_table_covers_all_kinds() -> None:
    source = (REPO / "static/v3/v3.js").read_text(encoding="utf-8")
    m = re.search(r"const handlers = \{(.*?)\n  \};", source, re.DOTALL)
    assert m is not None, "v3.js handlers object moved — update this extraction"
    keys = set(re.findall(r"^\s{4}([a-z_]+)\(?[:(]", m.group(1), re.MULTILINE))
    assert len(keys) >= 18, f"handler-key extraction looks broken: {sorted(keys)}"
    missing = v3_contract.EVENT_KINDS - keys
    assert not missing, (
        f"web v3.js has no handler for: {sorted(missing)} — unknown kinds only "
        "banner-fallback, a declared kind must render properly"
    )


def test_exported_contract_json_is_fresh() -> None:
    web_copy = REPO / "static/v3/contract.json"
    assert web_copy.exists(), "run scripts/export_contract.py"
    assert json.loads(web_copy.read_text(encoding="utf-8")) == v3_contract.as_dict()


def test_cli_vendored_contract_matches_when_present() -> None:
    # pwd, not Path.home(): conftest redirects $HOME to a tmp dir for some
    # tests, which must not turn this cross-repo check into a silent skip.
    import os
    import pwd

    real_home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    cli_repo = Path(
        os.environ.get("LUMERI_CLI_REPO")
        or (real_home / "Code" / "lumeri-cli")
    )
    cli_copy = cli_repo / "src" / "contract.json"
    if not cli_copy.exists():
        import pytest

        pytest.skip("lumeri-cli repo not present on this machine")
    cli_contract = json.loads(cli_copy.read_text(encoding="utf-8"))
    server_contract = v3_contract.as_dict()
    if cli_contract == server_contract:
        return

    # Protocol v2 is a Web-first additive production surface.  The v1 CLI is
    # intentionally still supported by the unchanged /sessions fields and its
    # existing unknown-event notice; it must not be handed a v2 contract until
    # it has real project/review UI.  Permit exactly one additive version lag,
    # while still failing if the CLI claims a newer version or contains values
    # the server no longer supports.
    assert cli_contract.get("protocol_version") == server_contract["protocol_version"] - 1
    assert set(cli_contract.get("event_kinds") or []) < set(server_contract["event_kinds"])
    assert set(cli_contract.get("error_codes") or []) <= set(server_contract["error_codes"])
    assert cli_contract.get("ask_controls") == server_contract["ask_controls"]
    assert cli_contract.get("recovery") == server_contract["recovery"]


def test_recovery_vocab_matches_errors_module() -> None:
    from gemia import errors

    assert v3_contract.RECOVERY == errors._RECOVERY_VALUES


def test_ask_controls_match_ask_module() -> None:
    from gemia.tools.ask import AskControlType

    assert v3_contract.ASK_CONTROLS == {c.value for c in AskControlType}


def test_session_info_reports_protocol_version(tmp_path) -> None:
    """GET /sessions/{id} carries protocol_version so clients can warn
    (non-blocking) on mismatch."""
    from types import SimpleNamespace

    from gemia import v3_routes

    src = (REPO / "gemia/v3_routes.py").read_text(encoding="utf-8")
    assert "protocol_version" in src
    assert v3_routes.PROTOCOL_VERSION == v3_contract.PROTOCOL_VERSION
