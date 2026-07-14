"""Phase 1 gate for Lumeri AS an MCP server (docs/mcp-interface-plan.md §6).

In-process MCP client harness — no subprocess, no network, NEVER touches the
live 7788 sidecar. Each test spins its OWN ``SessionManager`` rooted under a
tmp dir and drives the REAL ``mcp.server.Server`` object through the SDK's
in-memory paired streams
(``mcp.shared.memory.create_connected_server_and_client_session``).

The whole file is guarded by ``pytest.importorskip("mcp")`` so the suite is
green whether or not the optional ``mcp`` SDK is installed (the SDK is an
opt-in extra: ``pip install lumeri[mcp]``).

Required cases (spec §6, Phase 1 gate):
1. initialize handshake; negotiated protocolVersion ∈ {2025-06-18, 2025-03-26}.
2. tools/list == the Phase 1 frozen set EXACTLY (drift test).
3. schema parity: every 1:1 tool's inputSchema == the §2.3 transform.
4. functional path: create_session → insert a clip → get_timeline shows it.
5. plan gate: plan_mode ON → timeline mutation isError E_PLAN_MODE; read ok.
6. budget gate: tiny cap → verb → E_BUDGET payload with reason + alternatives.
7. asset resource returns a path descriptor (not base64).
8. exclusion lock: run_shell/generate_video/remember absent; run_verb refuses.
9. turn-collision: turn_in_progress forced → mutating verb E_BUSY; read ok.
10. SSE mirror: transcript/ring carries tool_exec_* with origin:"mcp".
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

pytest.importorskip("mcp")  # opt-in SDK; skip cleanly when absent.

from mcp.shared.memory import (  # noqa: E402
    create_connected_server_and_client_session,
)

from gemia.mcp.server import build_server  # noqa: E402
from gemia.mcp.toolset import PHASE1_TOOLSET, mcp_input_schema  # noqa: E402
from gemia.session_manager import SessionManager  # noqa: E402


# ── harness ──────────────────────────────────────────────────────────────────


def _make_manager(tmp_path: Path, **kwargs) -> SessionManager:
    """A fresh, isolated SessionManager rooted under an APFS tmp dir. NEVER the
    live 7788 singleton (get_manager is not called)."""
    return SessionManager(
        output_root=tmp_path / "root",
        sweep_interval_sec=0,  # no background sweeper in tests
        idle_timeout_sec=0,
        **kwargs,
    )


async def _drive(mgr: SessionManager, coro_fn):
    """Run ``coro_fn(client)`` against an in-memory-connected client/server."""
    server = build_server(mgr)
    async with create_connected_server_and_client_session(
        server, raise_exceptions=False
    ) as client:
        return await coro_fn(client)


def _text_payload(result) -> dict:
    """Extract the structured dict from a CallToolResult (structuredContent, or
    the JSON text block)."""
    if result.structuredContent is not None:
        return result.structuredContent
    block = result.content[0]
    return json.loads(block.text)


def _create_session(client):
    return client.call_tool("create_session", {})


async def _new_session_id(client) -> str:
    res = await client.call_tool("create_session", {})
    return _text_payload(res)["session_id"]


# ── case 1: initialize handshake + protocol version ──────────────────────────


# The spec (D8) pins the design to protocol revision 2025-06-18, accepts down
# to 2025-03-26, and delegates version negotiation to the SDK. The SDK echoes
# the client's requested version when supported, else falls back to LATEST. We
# therefore assert two things: (a) the server speaks the pinned 2025-06-18
# revision (it is in the SDK's supported set), and (b) a live handshake
# negotiates a supported version no older than the documented 2025-03-26 floor.
_PINNED_PROTOCOL = "2025-06-18"
_MIN_PROTOCOL = "2025-03-26"


def test_initialize_handshake_negotiates_supported_protocol(tmp_path):
    from mcp.shared.version import SUPPORTED_PROTOCOL_VERSIONS

    # (a) the pinned revision is one the SDK/server can speak.
    assert _PINNED_PROTOCOL in SUPPORTED_PROTOCOL_VERSIONS

    mgr = _make_manager(tmp_path)
    try:

        async def scenario(client):
            # create_connected_server_and_client_session already called
            # initialize(); re-run one to read the negotiated protocol.
            server = build_server(mgr)
            async with create_connected_server_and_client_session(server) as c:
                init = await c.initialize()
                return init.protocolVersion

        proto = asyncio.run(scenario(None))
        # (b) negotiated version is SDK-supported and not older than the floor.
        assert proto in SUPPORTED_PROTOCOL_VERSIONS
        assert proto >= _MIN_PROTOCOL  # ISO date strings sort chronologically
    finally:
        mgr.close_all()


# ── case 2: tools/list == the frozen Phase 1 set, EXACTLY ────────────────────


def test_tools_list_equals_frozen_phase1_set(tmp_path):
    mgr = _make_manager(tmp_path)
    try:

        async def scenario(client):
            res = await client.list_tools()
            return {t.name for t in res.tools}

        names = asyncio.run(_drive(mgr, scenario))
        assert names == set(PHASE1_TOOLSET), (
            "MCP surface drifted from the frozen Phase 1 toolset: "
            f"extra={names - set(PHASE1_TOOLSET)} missing={set(PHASE1_TOOLSET) - names}"
        )
    finally:
        mgr.close_all()


# ── case 3: schema parity for every 1:1 verb ─────────────────────────────────


def test_verb_input_schemas_match_mechanical_transform(tmp_path):
    mgr = _make_manager(tmp_path)
    native = {"create_session", "list_sessions", "get_session", "close_session", "import_media"}
    try:

        async def scenario(client):
            res = await client.list_tools()
            return {t.name: t.inputSchema for t in res.tools}

        schemas = asyncio.run(_drive(mgr, scenario))
        for name, schema in schemas.items():
            if name in native:
                continue
            assert schema == mcp_input_schema(name), f"schema drift for {name}"
    finally:
        mgr.close_all()


# ── case 4: functional path create → insert → get_timeline ───────────────────


def test_functional_create_insert_get_timeline(tmp_path):
    mgr = _make_manager(tmp_path)
    try:

        async def scenario(client):
            sid = await _new_session_id(client)
            # Insert a TEXT clip (no ffmpeg / no real asset needed) so the
            # functional path is deterministic and offline.
            ins = await client.call_tool(
                "timeline_insert_clip",
                {"session_id": sid, "text": {"content": "Hello Lumeri"}},
            )
            ins_payload = _text_payload(ins)
            assert ins.isError is False, ins_payload
            clip_id = ins_payload["clip_id"]
            tl = await client.call_tool("get_timeline", {"session_id": sid})
            tl_payload = _text_payload(tl)
            return clip_id, json.dumps(tl_payload)

        clip_id, tl_json = asyncio.run(_drive(mgr, scenario))
        assert clip_id and clip_id in tl_json
    finally:
        mgr.close_all()


# ── case 5: plan gate blocks a mutation, read still works ─────────────────────


def test_plan_mode_gates_mcp_mutation(tmp_path):
    mgr = _make_manager(tmp_path)
    try:

        async def scenario(client):
            sid = await _new_session_id(client)
            mgr.get(sid).set_plan_mode(True)
            blocked = await client.call_tool(
                "timeline_insert_clip",
                {"session_id": sid, "text": {"content": "nope"}},
            )
            read_ok = await client.call_tool("get_timeline", {"session_id": sid})
            return blocked, read_ok

        blocked, read_ok = asyncio.run(_drive(mgr, scenario))
        assert blocked.isError is True
        payload = _text_payload(blocked)
        assert payload["error_code"] == "E_PLAN_MODE"
        assert payload.get("blocked_by_plan_mode") is True
        assert read_ok.isError is False  # reads still allowed while planning
    finally:
        mgr.close_all()


# ── case 6: budget gate → E_BUDGET with reason + alternatives ────────────────


def test_budget_gate_blocks_over_cap_verb(tmp_path):
    mgr = _make_manager(tmp_path)
    try:

        async def scenario(client):
            sid = await _new_session_id(client)
            # Shrink the SAME BudgetGuard the loop uses to a tiny time cap so any
            # verb trips the time gate. analyze_media has a nonzero eta (4s).
            guard = mgr.get(sid).agent.budget
            guard.max_seconds = 0.0
            guard.max_usd = 0.0
            # Register an asset so analyze_media has a target arg shape; the gate
            # fires BEFORE dispatch, so the asset need not exist on disk here —
            # use probe_media which is pure-read but still gated on time.
            res = await client.call_tool(
                "get_timeline", {"session_id": sid, "history": 0}
            )
            return res

        res = asyncio.run(_drive(mgr, scenario))
        assert res.isError is True
        payload = _text_payload(res)
        assert payload["error_code"] == "E_BUDGET"
        assert payload["blocked_by_budget"] is True
        assert payload["approval_cannot_override"] is True
        assert "needs_approval" not in payload
        assert "reason" in payload
        assert "alternatives" in payload
    finally:
        mgr.close_all()


# ── case 7: asset resource returns a PATH descriptor, not base64 ─────────────


def test_asset_resource_returns_path_descriptor(tmp_path):
    mgr = _make_manager(tmp_path)
    # A real (tiny) media file so import_media / the descriptor has a path.
    media = tmp_path / "clip.mp4"
    media.write_bytes(b"\x00\x00\x00\x18ftypmp42fake-bytes-for-descriptor-only")
    try:

        async def scenario(client):
            sid = await _new_session_id(client)
            imp = await client.call_tool(
                "import_media", {"session_id": sid, "path": str(media)}
            )
            aid = _text_payload(imp)["asset_id"]
            from pydantic import AnyUrl

            uri = AnyUrl(f"lumeri://session/{sid}/asset/{aid}")
            read = await client.read_resource(uri)
            return read

        read = asyncio.run(_drive(mgr, scenario))
        block = read.contents[0]
        descriptor = json.loads(block.text)
        assert descriptor["path"] == str(media)  # absolute path, not bytes
        assert descriptor["kind"] == "video"
        assert "size_bytes" in descriptor
        # It is a descriptor, NOT the file bytes (no base64 blob).
        assert not hasattr(block, "blob") or getattr(block, "blob", None) is None
    finally:
        mgr.close_all()


# ── case 8: exclusion lock ───────────────────────────────────────────────────


def test_excluded_verbs_absent_and_run_verb_refuses(tmp_path):
    mgr = _make_manager(tmp_path)
    try:

        async def scenario(client):
            res = await client.list_tools()
            names = {t.name for t in res.tools}
            return names

        names = asyncio.run(_drive(mgr, scenario))
        for excluded in ("run_shell", "generate_video", "remember"):
            assert excluded not in names

        # run_verb itself refuses an excluded verb even when called directly.
        from gemia.session_manager import VerbGateError

        runner = mgr.create_session()
        with pytest.raises(VerbGateError) as ei:
            runner.run_verb("run_shell", {})
        assert ei.value.code == "E_TOOL"
    finally:
        mgr.close_all()


# ── case 9: turn-collision guard ─────────────────────────────────────────────


def test_turn_collision_blocks_mutation_allows_read(tmp_path):
    mgr = _make_manager(tmp_path)
    try:
        runner = mgr.create_session()
        # Force a turn to appear in-flight.
        with runner._state_lock:
            runner._turn_in_progress = True

        from gemia.session_manager import VerbGateError

        # Mutating verb → E_BUSY.
        with pytest.raises(VerbGateError) as ei:
            runner.run_verb(
                "timeline_insert_clip", {"text": {"content": "x"}}
            )
        assert ei.value.code == "E_BUSY"

        # Read verb interleaves fine.
        out = runner.run_verb("get_timeline", {})
        assert isinstance(out, dict)
    finally:
        mgr.close_all()


# ── case 10: SSE mirror carries origin:"mcp" ─────────────────────────────────


def test_sse_mirror_tags_origin_mcp(tmp_path):
    mgr = _make_manager(tmp_path)
    try:
        runner = mgr.create_session()
        events: list[dict] = []
        # Wrap the runner's event sink so we can observe the mirrored SSE events
        # (same sink that feeds the durable transcript + SSE ring).
        orig_emit = runner._emit_event

        def _capture(event):
            events.append(event)
            orig_emit(event)

        runner._emit_event = _capture  # type: ignore[method-assign]

        out = runner.run_verb("timeline_insert_clip", {"text": {"content": "hi"}})
        assert out["applied"] is True

        kinds = [(e["kind"], e.get("origin")) for e in events]
        assert ("tool_exec_start", "mcp") in kinds
        assert ("tool_exec_result", "mcp") in kinds
    finally:
        mgr.close_all()


def test_returned_failure_maps_to_mcp_error(tmp_path, monkeypatch):
    import gemia.tools as tools_mod
    from gemia.session_manager import VerbGateError

    async def returned_failure(args, ctx):
        return {"exit_code": 9, "error": "probe failed"}

    monkeypatch.setitem(tools_mod.DISPATCHER, "get_timeline", returned_failure)
    mgr = _make_manager(tmp_path)
    try:
        runner = mgr.create_session()
        events: list[dict] = []
        original_emit = runner._emit_event

        def capture(event):
            events.append(event)
            original_emit(event)

        runner._emit_event = capture  # type: ignore[method-assign]

        with pytest.raises(VerbGateError) as exc_info:
            runner.run_verb("get_timeline", {})

        assert exc_info.value.code == "E_PROCESS_EXIT"
        assert any(
            event.get("kind") == "tool_exec_error"
            and event.get("origin") == "mcp"
            and event.get("error_code") == "E_PROCESS_EXIT"
            for event in events
        )
        assert not any(event.get("kind") == "tool_exec_result" for event in events)
    finally:
        mgr.close_all()
