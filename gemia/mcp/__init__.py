"""Lumeri MCP integration package (docs/mcp-interface-plan.md).

Direction A (Lumeri AS an MCP server) lives here. Phase 1 ships the stdio
server + the curated read/timeline toolset; Phase 2 adds resources, progress
and HTTP; Phase 3 adds ``client_hub`` (Lumeri consuming external MCP servers).

Nothing in this package imports the third-party ``mcp`` SDK at import time —
the SDK is an optional dependency (``pip install lumeri[mcp]``) and the import
is deferred into the serve entrypoints so the rest of the package (the frozen
toolset + its drift tests) is usable without it.
"""
from __future__ import annotations

from gemia.mcp.toolset import (
    MCP_NATIVE_PLAN_SAFE,
    MCP_NATIVE_TOOLS,
    MCP_TOOLSET,
    PHASE1_TOOLSET,
    mcp_input_schema,
)

__all__ = [
    "MCP_TOOLSET",
    "MCP_NATIVE_TOOLS",
    "MCP_NATIVE_PLAN_SAFE",
    "PHASE1_TOOLSET",
    "mcp_input_schema",
]
