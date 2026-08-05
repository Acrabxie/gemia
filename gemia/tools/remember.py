"""``remember`` verb for the v3 agent.

Persist a durable fact the user wants kept ACROSS sessions — a stable
preference, a standing constraint, a name/handle, a recurring workflow choice.
The fact is written to the Gemia durable memory store (``MEMORY.md``) via
:func:`gemia.memory.remember_fact`, which validates it against secrets and is
idempotent-ish: re-remembering with the same ``title`` UPDATES the existing
note instead of duplicating it.

This is the opposite of ``log_note`` (which records short-lived progress in the
daily log). Use ``remember`` only for things worth carrying into FUTURE
sessions, not per-turn status.

Dispatchers must NOT swallow errors; the agent loop wraps each call.
"""
from __future__ import annotations

from typing import Any

from gemia import memory, project_context
from gemia.tools._context import ToolContext


async def dispatch(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Persist a durable user fact/preference to the memory store.

    Args:
        content: required. The fact to remember, in plain text.
        title: optional. A short label/key. When given, re-remembering with the
            same title UPDATES the existing note (no duplicates).
        kind: optional. A category hint (e.g. "preference", "constraint",
            "fact", "workflow").

    Returns the stored entry metadata. Raises ``ValueError`` for empty content
    or secret-bearing content (the store refuses to keep credentials).
    """
    content = str(args.get("content") or "").strip()
    if not content:
        raise ValueError("remember requires non-empty 'content'")
    title = args.get("title")
    kind = args.get("kind")
    scope = str(args.get("scope") or "auto").strip().lower()
    if scope not in {"auto", "global", "project"}:
        raise ValueError("remember scope must be auto, global, or project")
    extra = getattr(ctx, "extra", {}) if ctx is not None else {}
    store = extra.get("production_store")
    project_id = str(extra.get("project_id") or "")
    has_project = project_context.is_project_workspace(store, project_id)
    use_project = scope == "project" or (
        scope == "auto" and has_project
    )
    if use_project and not has_project:
        raise ValueError("project memory is unavailable outside a Project session")

    writer = project_context.remember_fact if use_project else None
    if writer is not None:
        record = writer(
            store,
            project_id,
            content,
            title=str(title).strip() if title else None,
            kind=str(kind).strip() if kind else None,
        )
    else:
        record = memory.remember_fact(
            content,
            title=str(title).strip() if title else None,
            kind=str(kind).strip() if kind else None,
        )
    action = record.get("action", "appended")
    return {
        "remembered": True,
        "scope": "project" if use_project else "global",
        "action": action,
        "title": record.get("title", ""),
        "kind": record.get("kind", ""),
        "entry": record.get("entry", content),
        "summary": (
            f"Remembered {'Project' if use_project else 'global'} durable fact ({action})"
            + (f" — {record['title']}" if record.get("title") else "")
            + "."
        ),
    }


__all__ = ["dispatch"]
