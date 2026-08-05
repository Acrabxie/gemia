"""Agent-tool helpers — the single-tool, op-discriminated surface shape.

Each point library exposes ONE consolidated tool (the ``update_quantum`` /
``vector_motion`` pattern) with an ``op`` discriminator. The pure creative engine
lives in the library's ``api``; the tool is a thin, side-effect-light wrapper
that validates the op, routes it, and returns a plain dict. These helpers keep
that wrapper uniform across all six libraries and easy to unit-test without a
live session (the gemia adapter that writes to a doc/timeline is a separate, thin
layer, exactly as ``gemia/tools/vector_motion.py`` wraps ``vector.api``).
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable

OPS = ("create", "adjust", "catalog")


def err(code: str, message: str, **extra: Any) -> dict[str, Any]:
    """A uniform error reply (mirrors vector_motion's ``_err``)."""
    return {"applied": False, "error_code": code, "error_message": message, **extra}


def ok(**payload: Any) -> dict[str, Any]:
    return {"applied": True, **payload}


def guard_op(op: Any, allowed: tuple[str, ...] = OPS) -> str | None:
    """Return an error dict-less op string, or None-signalling via exception.

    Returns the normalised op if valid; the caller compares against ``allowed``.
    (Kept simple so libraries can special-case their own ops.)
    """
    return str(op or "create")


def dispatch(
    args: dict[str, Any],
    *,
    tool: str,
    catalog_fn: Callable[[], dict[str, Any]],
    create: Callable[[dict[str, Any]], dict[str, Any]],
    adjust: Callable[[dict[str, Any]], dict[str, Any]],
    allowed: tuple[str, ...] = OPS,
) -> dict[str, Any]:
    """Route a synchronous, pure tool call by ``op``.

    ``create``/``adjust`` are the library's pure handlers (brief in, reply dict
    out). ``catalog`` is answered directly. Unknown ops yield a uniform E_ARG.
    """
    op = str(args.get("op") or "create")
    if op not in allowed:
        return err("E_ARG", f"{tool}: unknown op {op!r} (use {', '.join(allowed)})")
    if op == "catalog":
        return ok(catalog=catalog_fn())
    if op == "create":
        return create(args)
    if op == "adjust":
        return adjust(args)
    return err("E_ARG", f"{tool}: op {op!r} has no handler")
