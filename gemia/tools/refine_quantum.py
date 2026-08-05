"""refine_quantum -- revise one quantum and rematerialize only its scope."""
from __future__ import annotations

from typing import Any, Mapping

from gemia.quanta.traverse import find_node, is_content_node, leaf_walk, lift_flat_quanta
from gemia.tools._context import ToolContext
from gemia.tools.quanta_frames import rematerialize_quanta_scope_assets


def _project(ctx: ToolContext):
    if ctx.project is None:
        raise ValueError("refine_quantum needs a project-backed session")
    return ctx.project


def _containing_scope_id(quanta: Mapping[str, Any], quantum_id: str) -> str:
    """The content scope whose subtree the edit re-materializes: the quantum
    itself when it is a content scope, its scope when it is a state."""
    node = find_node(quanta, quantum_id)
    if node is None:
        raise ValueError(f"refine_quantum: no quantum with id {quantum_id!r}")
    if is_content_node(node):
        return quantum_id
    for leaf in leaf_walk(quanta, include_hidden=True):
        if leaf.state_id == quantum_id:
            return leaf.scope_id
    raise ValueError(
        f"refine_quantum targets a group ({quantum_id!r}); groups have nothing to "
        "rematerialize — use update_quantum for structural edits"
    )


async def dispatch(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    # Lazy imports avoid coupling the dispatcher registry back into this verb.
    from gemia.tools import assemble_quanta as _assemble
    from gemia.tools import quanta as _quanta

    project = _project(ctx)
    quantum_id = str(args.get("quantum_id") or "").strip()
    if not quantum_id:
        raise ValueError("refine_quantum requires 'quantum_id'")
    fields = args.get("fields")
    if not isinstance(fields, dict) or not fields:
        raise ValueError("refine_quantum requires a non-empty 'fields' object")

    before = lift_flat_quanta(project.load().get("quanta") or None)
    if not leaf_walk(before):
        raise ValueError("quanta is empty — call draft_quanta or set_quanta first")
    scope_id = _containing_scope_id(before, quantum_id)
    old_cache = ctx.extra.get("quanta_frame_cache")
    previous = None
    if (
        isinstance(old_cache, Mapping)
        and old_cache.get("key") == _assemble.quanta_frame_cache_key(before)
        and isinstance(old_cache.get("result"), Mapping)
    ):
        previous = old_cache["result"]

    updated = await _quanta.dispatch_update_quantum(
        {"op": "patch", "quantum_id": quantum_id, "fields": fields}, ctx
    )
    current = project.load().get("quanta")
    if not isinstance(current, Mapping):
        raise ValueError("updated quanta is unavailable")

    # An update changes the quanta digest. Seed the new cache with a selective
    # render when possible so assemble_quanta can rebuild the timeline without
    # rasterizing unchanged scopes again.
    ctx.extra.pop("quanta_frame_cache", None)
    rendered = rematerialize_quanta_scope_assets(
        current,
        ctx,
        scope_id=scope_id,
        previous=previous,
        scale=1,
        fail_on_overflow=bool(args.get("fail_on_overflow", False)),
    )
    ctx.extra["quanta_frame_cache"] = {
        "key": _assemble.quanta_frame_cache_key(lift_flat_quanta(current)),
        "result": rendered,
    }
    assembled = await _assemble.dispatch(
        {"fail_on_overflow": bool(args.get("fail_on_overflow", False))}, ctx
    )
    return {
        **assembled,
        "refined": True,
        "updated_quantum": quantum_id,
        "rematerialized_scope_id": scope_id,
        "update_seq": updated.get("seq"),
        "quanta": updated.get("quanta"),
        "summary": (
            f"refined quantum {quantum_id}, {rendered.get('summary')}, and rebuilt "
            f"{assembled.get('frame_count')} timeline state(s)"
        ),
    }


__all__ = ["dispatch"]
