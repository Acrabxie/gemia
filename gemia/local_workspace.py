"""Single local workspace used by the public Lumeri runtime.

The open-source service has no registration, login, account switching, OAuth,
billing, or subscription state.  This module only provides deterministic local
storage paths to the editing and media-library layers.
"""
from __future__ import annotations

import re
from pathlib import Path

from gemia.errors import GemiaError

LOCAL_WORKSPACE_ID = "local_public"
WORKSPACES_ROOT = Path.home() / ".gemia" / "public-workspaces"


class WorkspaceError(GemiaError, ValueError):
    code = "E_WORKSPACE"


def current_workspace_id() -> str:
    return LOCAL_WORKSPACE_ID


def _validate_workspace_id(workspace_id: str) -> str:
    value = workspace_id.strip() if isinstance(workspace_id, str) else ""
    if not re.fullmatch(r"[a-zA-Z0-9_-]{8,80}", value):
        raise WorkspaceError("Invalid workspace id")
    return value


def workspace_root(workspace_id: str = LOCAL_WORKSPACE_ID) -> Path:
    return WORKSPACES_ROOT / _validate_workspace_id(workspace_id)


def workspace_memory_root(workspace_id: str = LOCAL_WORKSPACE_ID) -> Path:
    return workspace_root(workspace_id) / "memory"


def workspace_session_root(workspace_id: str = LOCAL_WORKSPACE_ID) -> Path:
    return workspace_root(workspace_id) / "sessions"


__all__ = [
    "LOCAL_WORKSPACE_ID",
    "WORKSPACES_ROOT",
    "WorkspaceError",
    "current_workspace_id",
    "workspace_memory_root",
    "workspace_root",
    "workspace_session_root",
]
