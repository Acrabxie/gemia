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

from gemia import memory
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

    record = memory.remember_fact(
        content,
        title=str(title).strip() if title else None,
        kind=str(kind).strip() if kind else None,
    )
    action = record.get("action", "appended")
    return {
        "remembered": True,
        "action": action,
        "title": record.get("title", ""),
        "kind": record.get("kind", ""),
        "entry": record.get("entry", content),
        "summary": (
            f"Remembered durable fact ({action})"
            + (f" — {record['title']}" if record.get("title") else "")
            + "."
        ),
    }


__all__ = ["dispatch"]
