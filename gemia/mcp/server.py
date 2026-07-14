"""Lumeri AS an MCP server — Direction A, Phase 1 (docs/mcp-interface-plan.md).

``build_server()`` returns a low-level ``mcp.server.Server`` exposing the
curated Phase 1 toolset (18 tools: 13 read/timeline 1:1 verbs + 5 MCP-native
lifecycle/import tools). Every 1:1 verb call is routed through
``SessionRunner.run_verb`` — the single choke point that re-applies the plan
gate then the budget gate in agent-loop order against the SAME ``BudgetGuard``
instance, and mirrors ``tool_exec_*`` SSE events with an additive
``origin: "mcp"`` field (D7).

Tool names are byte-identical to internal verb names (D3): the gates look tools
up by exact name, so there is no mapping table to drift. MCP clients already
namespace (Claude Code renders ``mcp__lumeri__probe_media``).

Assets cross the boundary as ``lumeri://session/{id}/asset/{aid}`` resources
returning a PATH descriptor, never base64 (D6): MCP binary contents are
base64-in-JSON and a single 500 MiB video read would materialize ~670 MB of
JSON in both processes and get injected into model context — useless for video.
Lumeri is local-first (D2), so an absolute path is a strictly better handle:
the client's own tools (ffprobe, Read, upload) operate on it directly.

## The ``mcp`` SDK is a LAZY, OPTIONAL dependency
The third-party ``mcp`` package is declared under ``[project.optional-
dependencies] mcp`` (``pip install lumeri[mcp]``). It is imported INSIDE
``build_server`` / ``serve_stdio`` — never at module top — so importing this
module (and running its toolset drift tests) does not require the SDK. When the
SDK is missing, ``build_server`` raises a friendly ``ImportError`` telling the
user how to install it.

## Process model (stdio, Phase 1)
``python -m gemia mcp-serve`` over stdio runs in its OWN process with its own
``SessionManager``. Its sessions persist to disk through the normal sessions
roots but are NOT live-visible in a separately running 7788 web UI. Clients
that want live web visibility should use the HTTP transport (Phase 2). Do not
attempt to attach the stdio process to 7788 in Phase 1.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

from gemia.mcp.toolset import (
    MCP_DESTRUCTIVE,
    MCP_NATIVE_PLAN_SAFE,
    MCP_NATIVE_TOOLS,
    MCP_READ_ONLY,
    PHASE1_TOOLSET,
    internal_verb_description,
    mcp_input_schema,
)
from gemia.session_manager import (
    SessionLimitError,
    SessionManager,
    VerbGateError,
    get_manager,
)
from gemia.v3_contract import PROTOCOL_VERSION

_INSTALL_HINT = (
    "The Lumeri MCP server needs the optional 'mcp' SDK. Install it with:\n"
    "    pip install 'lumeri[mcp]'\n"
    "(or: pip install 'mcp>=1.12,<2')"
)

# Server identity advertised in the MCP initialize handshake.
SERVER_NAME = "lumeri"
SERVER_VERSION = "0.1.0"


def _require_mcp():  # -> module `mcp`
    """Import the optional ``mcp`` SDK lazily with a friendly error."""
    try:
        import mcp  # noqa: F401
        import mcp.types as types  # noqa: F401
        from mcp.server import Server  # noqa: F401
    except ImportError as exc:  # pragma: no cover - exercised only sans SDK
        raise ImportError(_INSTALL_HINT) from exc
    return mcp


# ── native-tool schemas (MCP-only lifecycle/import, no internal verb) ────────
def _native_tool_schemas(types) -> list[Any]:
    T = types.Tool
    return [
        T(
            name="create_session",
            description=(
                "Create a new Lumeri editing session and return its session_id. "
                "Pass that session_id to every other tool."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "account_id": {
                        "type": "string",
                        "description": "Optional account id for provenance.",
                    }
                },
                "required": [],
            },
            annotations=types.ToolAnnotations(readOnlyHint=False, openWorldHint=False),
        ),
        T(
            name="list_sessions",
            description="List the ids of all live Lumeri sessions in this server process.",
            inputSchema={"type": "object", "properties": {}, "required": []},
            annotations=types.ToolAnnotations(readOnlyHint=True, openWorldHint=False),
        ),
        T(
            name="get_session",
            description=(
                "Return a session's assets, plan_mode flag, budget snapshot, "
                "output_dir and protocol_version."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {"type": "string", "description": "Session id from create_session."}
                },
                "required": ["session_id"],
            },
            annotations=types.ToolAnnotations(readOnlyHint=True, openWorldHint=False),
        ),
        T(
            name="close_session",
            description="Close a Lumeri session (polite, not required — the idle sweeper also reaps sessions).",
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {"type": "string", "description": "Session id from create_session."}
                },
                "required": ["session_id"],
            },
            annotations=types.ToolAnnotations(
                readOnlyHint=False, destructiveHint=True, openWorldHint=False
            ),
        ),
        T(
            name="import_media",
            description=(
                "Register an absolute local media file path as a session asset "
                "and return its asset_id. Blocked while the session is in plan mode."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {"type": "string", "description": "Session id from create_session."},
                    "path": {
                        "type": "string",
                        "description": "Absolute path to a local media file on this machine.",
                    },
                    "summary": {
                        "type": "string",
                        "description": "Optional human summary of the asset.",
                    },
                },
                "required": ["session_id", "path"],
            },
            annotations=types.ToolAnnotations(readOnlyHint=False, openWorldHint=False),
        ),
    ]


def _verb_tool_schemas(types) -> list[Any]:
    """The 1:1 Phase 1 verbs, mechanically transformed from ``TOOL_SCHEMAS``."""
    tools = []
    for name in sorted(PHASE1_TOOLSET - MCP_NATIVE_TOOLS):
        tools.append(
            types.Tool(
                name=name,
                description=internal_verb_description(name),
                inputSchema=mcp_input_schema(name),
                annotations=types.ToolAnnotations(
                    readOnlyHint=name in MCP_READ_ONLY,
                    destructiveHint=name in MCP_DESTRUCTIVE,
                    openWorldHint=False,
                ),
            )
        )
    return tools


def build_server(manager: SessionManager | None = None):
    """Build the low-level MCP ``Server`` for Lumeri (Phase 1).

    ``manager`` defaults to the process-local ``get_manager()`` singleton. Tests
    pass their own ``SessionManager`` (rooted under a tmp dir) so the harness
    NEVER touches the live 7788 sidecar.
    """
    mcp = _require_mcp()
    import mcp.types as types
    from mcp.server import Server

    mgr = manager if manager is not None else get_manager()
    server: Server = Server(SERVER_NAME, version=SERVER_VERSION)

    # ── tools/list ──────────────────────────────────────────────────────────
    @server.list_tools()
    async def _list_tools() -> list[Any]:
        return _native_tool_schemas(types) + _verb_tool_schemas(types)

    # ── tools/call ──────────────────────────────────────────────────────────
    @server.call_tool()
    async def _call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        # Returning a dict → SDK puts it in structuredContent + a JSON text
        # block. Raising → SDK builds an isError result carrying the message.
        args = dict(arguments or {})
        if name in MCP_NATIVE_TOOLS:
            return await _handle_native(mgr, name, args)
        # 1:1 verb → the single choke point.
        session_id = args.pop("session_id", None)
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("missing required parameter: session_id")
        runner = mgr.get(session_id)
        if runner is None:
            raise ValueError(f"unknown session: {session_id}")

        emit_progress = _make_progress_forwarder(server, types)
        try:
            return await asyncio.to_thread(
                runner.run_verb, name, args, emit_progress=emit_progress
            )
        except VerbGateError as exc:
            # Surface the structured gate payload as a tool error (isError:true).
            raise _ToolError(json.dumps(exc.payload, ensure_ascii=False, default=str))
        except Exception as exc:
            from gemia.errors import GemiaError

            if isinstance(exc, GemiaError):
                raise _ToolError(
                    json.dumps(exc.to_payload(), ensure_ascii=False, default=str)
                )
            raise

    # ── resources (D6): timeline inline + asset descriptor (path, not bytes) ─
    @server.list_resources()
    async def _list_resources() -> list[Any]:
        out: list[Any] = []
        for sid in mgr.list_sessions():
            runner = mgr.get(sid)
            if runner is None:
                continue
            out.append(
                types.Resource(
                    uri=f"lumeri://session/{sid}/timeline",
                    name=f"timeline:{sid}",
                    description="The session timeline as JSON (same shape as GET /sessions/{id}/timeline).",
                    mimeType="application/json",
                )
            )
            for rec in runner.list_assets():
                aid = rec["asset_id"]
                out.append(
                    types.Resource(
                        uri=f"lumeri://session/{sid}/asset/{aid}",
                        name=f"asset:{sid}/{aid}",
                        description=f"Descriptor (path, not bytes) for asset {aid} [{rec['kind']}].",
                        mimeType="application/json",
                    )
                )
        return out

    @server.list_resource_templates()
    async def _list_resource_templates() -> list[Any]:
        return [
            types.ResourceTemplate(
                uriTemplate="lumeri://session/{session_id}/timeline",
                name="session-timeline",
                description="Timeline JSON for a session.",
                mimeType="application/json",
            ),
            types.ResourceTemplate(
                uriTemplate="lumeri://session/{session_id}/asset/{asset_id}",
                name="session-asset-descriptor",
                description="Asset descriptor (absolute path, size, kind, lineage) — never the bytes.",
                mimeType="application/json",
            ),
        ]

    @server.read_resource()
    async def _read_resource(uri) -> str:
        return _read_lumeri_resource(mgr, str(uri))

    return server


class _ToolErrorSentinel(Exception):
    """Internal: raised to make the SDK return an ``isError`` tool result whose
    text is the structured gate/error payload JSON."""


_ToolError = _ToolErrorSentinel


async def _handle_native(
    mgr: SessionManager, name: str, args: dict[str, Any]
) -> dict[str, Any]:
    """The 5 MCP-native lifecycle/import tools.

    These touch the ``SessionManager`` directly (not the per-session asyncio
    loop), so they run inline on the MCP loop. ``create_session`` spins up a
    session thread synchronously inside ``SessionManager.create_session``; that
    is a short, thread-bounded operation, kept off the MCP loop via
    ``asyncio.to_thread`` so a slow agent init can't stall the MCP event loop.
    """
    if name == "create_session":
        try:
            runner = await asyncio.to_thread(
                mgr.create_session, account_id=args.get("account_id")
            )
        except SessionLimitError as exc:
            raise _ToolError(
                json.dumps(
                    {
                        "error": str(exc),
                        "error_code": "E_BUSY",
                        "hint": "raise LUMERI_V3_MAX_SESSIONS or close idle sessions",
                    }
                )
            )
        return {
            "session_id": runner.session_id,
            "output_dir": str(runner.output_dir),
            "protocol_version": PROTOCOL_VERSION,
            "budget": runner.agent.budget.snapshot(),
        }
    if name == "list_sessions":
        return {"sessions": mgr.list_sessions()}
    # remaining native tools need a session_id.
    session_id = args.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("missing required parameter: session_id")
    if name == "close_session":
        await asyncio.to_thread(mgr.close_session, session_id)
        return {"closed": session_id}
    runner = mgr.get(session_id)
    if runner is None:
        raise ValueError(f"unknown session: {session_id}")
    if name == "get_session":
        return {
            "session_id": session_id,
            "output_dir": str(runner.output_dir),
            "plan_mode": runner.plan_mode,
            "protocol_version": PROTOCOL_VERSION,
            "budget": runner.agent.budget.snapshot(),
            "assets": runner.list_assets(),
        }
    if name == "import_media":
        # plan-mode gate for a native tool (import_media registers an asset).
        if runner.plan_mode and name not in MCP_NATIVE_PLAN_SAFE:
            raise _ToolError(
                json.dumps(
                    {
                        "blocked_by_plan_mode": True,
                        "error_code": "E_PLAN_MODE",
                        "message": (
                            "Plan mode is ON: import_media is blocked because it "
                            "registers a session asset. Turn plan mode off to import."
                        ),
                        "tool_name": "import_media",
                    }
                )
            )
        path = args.get("path")
        if not isinstance(path, str) or not path:
            raise ValueError("import_media requires an absolute 'path' string")
        from pathlib import Path

        asset_id = await asyncio.to_thread(
            runner.add_external_asset, Path(path), summary=str(args.get("summary") or "")
        )
        rec = next((r for r in runner.list_assets() if r["asset_id"] == asset_id), {})
        return {"asset_id": asset_id, "kind": rec.get("kind"), "path": path}
    raise ValueError(f"unknown native tool: {name}")


def _make_progress_forwarder(server, types):
    """Build an ``emit_progress`` callback that forwards ``ProgressUpdate``s to
    MCP ``notifications/progress`` — only when the client sent a progressToken
    (D5). Best-effort: delivery failures are swallowed (progress is best-effort,
    exactly like the SSE path).

    The dispatcher runs on the session loop; the MCP server runs on its own
    anyio loop. The callback marshals via ``run_coroutine_threadsafe`` back onto
    the MCP loop (§2.4). If there is no progressToken, the callback is a no-op.
    """
    try:
        ctx = server.request_context
        token = ctx.meta.progressToken if ctx.meta is not None else None
        session = ctx.session
    except LookupError:
        token = None
        session = None
    if token is None or session is None:
        return None

    mcp_loop = asyncio.get_running_loop()

    def _forward(update: Any) -> None:
        percent = getattr(update, "percent", None)
        message = getattr(update, "message", None)
        eta = getattr(update, "eta_sec", None)
        if eta is not None:
            suffix = f" (~{eta:.0f}s left)"
            message = (message or "") + suffix
        if percent is not None:
            progress = float(percent)
            total: float | None = 100.0
        else:
            # message-only notification without total (D5).
            progress = 0.0
            total = None
        try:
            asyncio.run_coroutine_threadsafe(
                session.send_progress_notification(
                    token, progress, total=total, message=message or None
                ),
                mcp_loop,
            )
        except Exception:
            pass

    return _forward


def _read_lumeri_resource(mgr: SessionManager, uri: str) -> str:
    """Resolve a ``lumeri://session/...`` URI to a JSON string (D6).

    - ``.../timeline`` → the timeline payload dict, inline JSON.
    - ``.../asset/{aid}`` → a descriptor with the absolute path, never base64.
    """
    prefix = "lumeri://session/"
    if not uri.startswith(prefix):
        raise ValueError(f"unknown resource uri: {uri}")
    rest = uri[len(prefix) :]
    parts = rest.split("/")
    session_id = parts[0] if parts else ""
    runner = mgr.get(session_id)
    if runner is None:
        raise ValueError(f"unknown session: {session_id}")

    if len(parts) == 2 and parts[1] == "timeline":
        from gemia import v3_routes

        project = runner.agent.project
        state = project.load()
        meta = project.store.load_meta(project.project_id)
        payload = v3_routes._timeline_payload_dict(
            session_id, project.project_id, state, meta
        )
        return json.dumps(payload, ensure_ascii=False, default=str)

    if len(parts) == 3 and parts[1] == "asset":
        asset_id = parts[2]
        if not runner.agent.registry.contains(asset_id):
            raise ValueError(f"unknown asset: {asset_id}")
        rec = runner.agent.registry.get(asset_id)
        path = rec.path
        size_bytes = None
        try:
            if path.exists():
                size_bytes = path.stat().st_size
        except OSError:
            size_bytes = None
        descriptor = {
            "asset_id": rec.asset_id,
            "kind": rec.kind,
            "path": str(path),  # absolute path, NOT base64 (D6)
            "size_bytes": size_bytes,
            "summary": rec.summary,
            "created_at": rec.created_at,
            "lineage": list(rec.lineage),
            "mime": _guess_mime(str(path)),
        }
        return json.dumps(descriptor, ensure_ascii=False, default=str)

    raise ValueError(f"unknown resource uri: {uri}")


def _guess_mime(path: str) -> str:
    import mimetypes

    mime, _ = mimetypes.guess_type(path)
    return mime or "application/octet-stream"


async def serve_stdio(manager: SessionManager | None = None) -> None:
    """Run the Lumeri MCP server over stdio (the Phase 1 deliverable).

    ``claude mcp add lumeri -- python -m gemia mcp-serve`` and Codex's
    ``mcp_servers`` config consume this.
    """
    _require_mcp()
    from mcp.server.stdio import stdio_server

    server = build_server(manager)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


def run_stdio(manager: SessionManager | None = None) -> None:
    """Blocking stdio entrypoint for the ``mcp-serve`` subcommand."""
    try:
        _require_mcp()
    except ImportError as exc:
        import sys

        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from None
    import anyio

    anyio.run(serve_stdio, manager)


__all__ = ["build_server", "serve_stdio", "run_stdio", "SERVER_NAME", "SERVER_VERSION"]
