"""``log_note`` verb for the v3 agent.

Append an explicit one-line note to TODAY'S daily log
(``~/.gemia/memory/daily/YYYY-MM-DD.md``) via
:func:`gemia.memory.append_daily_entry`. This is for short-lived progress /
decisions worth a breadcrumb in the running log — NOT for durable facts that
should survive into future sessions (use ``remember`` for those).

The host already auto-logs a one-line turn summary at the end of each turn;
``log_note`` lets the agent add its own extra breadcrumb when something is
worth recording mid-turn.

Logging is best-effort and must never break a turn: secret-looking or empty
content is skipped (returned as ``logged: False``) rather than raised.
"""
from __future__ import annotations

from typing import Any

from gemia import memory, project_context
from gemia.tools._context import ToolContext


async def dispatch(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Append a one-line note to today's daily log.

    Args:
        text: required. The note to append (collapsed to a single line).

    Returns ``{"logged": bool, ...}``. Never raises for skipped content
    (empty / secret-looking): those come back as ``logged: False`` with a
    ``reason``.
    """
    text = str(args.get("text") or "").strip()
    if not text:
        return {"logged": False, "reason": "empty", "summary": "Nothing to log."}

    scope = str(args.get("scope") or "auto").strip().lower()
    if scope not in {"auto", "global", "project"}:
        return {"logged": False, "reason": "invalid_scope", "summary": "Invalid log scope."}
    extra = getattr(ctx, "extra", {}) if ctx is not None else {}
    store = extra.get("production_store")
    project_id = str(extra.get("project_id") or "")
    has_project = project_context.is_project_workspace(store, project_id)
    use_project = scope == "project" or (
        scope == "auto" and has_project
    )
    if use_project and not has_project:
        return {
            "logged": False,
            "reason": "project_unavailable",
            "summary": "Project log is unavailable outside a Project session.",
        }
    record = (
        project_context.append_log(store, project_id, text)
        if use_project
        else memory.append_daily_entry(text)
    )
    written = bool(record.get("written"))
    out: dict[str, Any] = {
        "logged": written,
        "scope": "project" if use_project else "global",
        "entry": record.get("entry", ""),
        "path": record.get("path", ""),
    }
    if not written:
        reason = record.get("reason", "skipped")
        out["reason"] = reason
        out["summary"] = f"Note not logged ({reason})."
    else:
        out["summary"] = (
            "Logged a note to the Project log."
            if use_project
            else "Logged a note to today's global log."
        )
    return out


__all__ = ["dispatch"]
