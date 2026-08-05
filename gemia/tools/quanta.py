"""quanta_* verbs: the model's read/write interface to the quanta state tree.

A quanta is a DISCRETE VIDEO — one ordered state tree with three faces
(docs/quanta-kernel-plan.md): structure (the tree of groups → content scopes
→ render states), viewing (interaction links + the DFS leaf walk as the
default path), and mutation (node-addressed ops through the patch log).

    1. The model turns the user's topic into a plan and calls ``set_quanta``
       (or scaffolds one with ``draft_quanta``) — the v1 flat ``slides`` shape
       stays accepted forever as authoring sugar and lifts into the tree.
    2. It refines with ``update_quantum`` — field patches on any node, plus
       insert/remove/move for structure (one tool, four ops, atomic batches).
    3. Later phases materialize the quanta (``assemble_quanta``) — nothing
       renders here; drafting and revising the plan is free.

Every mutation flows through ``ctx.project.apply_ops`` — versioned,
auditable, and undoable via ``timeline_undo`` exactly like timeline and
shotlist edits. Mutations return a compact text view of the post-state so
the model does not need a follow-up read. Quanta patches surface as
``timeline_op`` events with ``state_scope=quanta``.
"""
from __future__ import annotations

from typing import Any, Mapping

from gemia.quanta.traverse import is_content_node, leaf_walk, lift_flat_quanta
from gemia.tools._context import ToolContext

QUANTA_OP_LABEL = "quanta-op"

_EDIT_OPS = {
    "patch": "patch_quantum",
    "insert": "insert_quantum",
    "remove": "remove_quantum",
    "move": "move_quantum",
}


def _project(ctx: ToolContext):
    if ctx.project is None:
        raise ValueError(
            "quanta verbs need a project-backed session (ctx.project is None)"
        )
    return ctx.project


def _blocks_summary(blocks: list[dict[str, Any]]) -> str:
    counts: dict[str, int] = {}
    for block in blocks:
        if isinstance(block, dict):
            kind = str(block.get("kind") or "?")
            counts[kind] = counts.get(kind, 0) + 1
    return "+".join(
        f"{kind}×{n}" if n > 1 else kind for kind, n in counts.items()
    ) or "empty"


def _leaf_count(blocks: Any) -> int:
    count = 0
    for block in blocks if isinstance(blocks, list) else []:
        if not isinstance(block, dict):
            continue
        if block.get("kind") == "group":
            count += _leaf_count(block.get("children"))
        else:
            count += 1
    return count


def _scope_line(scope: Mapping[str, Any], *, indent: str) -> str:
    states = [s for s in scope.get("children") or [] if isinstance(s, dict)]
    dwell = sum(float(s.get("dwell_sec") or 0) for s in states)
    leaf_count = _leaf_count(scope.get("blocks"))
    visibility = "→".join(
        str(len(state.get("visible_block_ids") or [])) for state in states
    ) or "0"
    bits = [
        f"{indent}[{scope.get('id')}]",
        str(scope.get("layout") or "content"),
        _blocks_summary(scope.get("blocks") or []),
        f"{len(states)} state {visibility}/{leaf_count} visible ~{dwell:.1f}s",
    ]
    line = " ".join(bits)
    if scope.get("hidden"):
        line += "  (hidden)"
    title = str(scope.get("title") or "").strip()
    if title:
        line += f' — "{title}"'
    notes = str(scope.get("notes") or "").strip()
    if notes:
        line += f'  vo:"{notes[:60]}{"…" if len(notes) > 60 else ""}"'
    for link in scope.get("links") or []:
        if isinstance(link, dict) and not (
            link.get("trigger") == "advance" and link.get("target") == "next"
        ):
            line += f"  ⇢{link.get('trigger')}→{link.get('target')}"
    transition = scope.get("transition") or {}
    if isinstance(transition, dict) and transition.get("kind") not in (None, "cut"):
        line += f"  ⇥{transition['kind']}"
    return line


def render_quanta_text(quanta: Mapping[str, Any] | None) -> str:
    """One-screen human/model-readable view of the state tree.

    Accepts the canonical tree or the v1 flat sugar (lifted for display).
    Depth is indentation; groups render as section headers; hidden subtrees
    are marked, not omitted — the author needs to see the whole edit tree.
    """
    doc = lift_flat_quanta(quanta if isinstance(quanta, Mapping) else None)
    root = doc.get("root") or {}
    scope_count = 0
    state_count = 0
    dwell = 0.0
    lines: list[str] = []

    theme = doc.get("theme") or {}
    head_bits = []
    if theme.get("mood"):
        head_bits.append(f"mood: {theme['mood']}")
    if theme.get("aspect"):
        head_bits.append(f"aspect: {theme['aspect']}")
    if head_bits:
        lines.append(" | ".join(head_bits))

    def visit(node: Mapping[str, Any], depth: int) -> None:
        nonlocal scope_count, state_count, dwell
        indent = "  " * depth
        for child in node.get("children") or []:
            if not isinstance(child, dict):
                continue
            if is_content_node(child):
                scope_count += 1
                states = [s for s in child.get("children") or [] if isinstance(s, dict)]
                state_count += len(states)
                dwell += sum(float(s.get("dwell_sec") or 0) for s in states)
                lines.append(_scope_line(child, indent=indent + "  "))
            else:
                header = f"{indent}▸ {child.get('title') or child.get('id')}"
                if child.get("hidden"):
                    header += "  (hidden)"
                lines.append(header)
                visit(child, depth + 1)

    visit(root, 0)
    if scope_count == 0:
        return "(quanta empty — call draft_quanta or set_quanta to plan the state tree)"
    lines.append(f"— {scope_count} scopes, {state_count} states, ~{dwell:.1f}s dwell")
    return "\n".join(lines)


def _counts(quanta: Mapping[str, Any] | None) -> tuple[int, int]:
    doc = lift_flat_quanta(quanta if isinstance(quanta, Mapping) else None)
    leaves = leaf_walk(doc, include_hidden=True)
    return len({leaf.scope_id for leaf in leaves}), len(leaves)


def _summary(ctx: ToolContext, result: dict[str, Any], **extra: Any) -> dict[str, Any]:
    quanta = (ctx.project.load() if ctx.project else {}).get("quanta") or {}
    scope_count, state_count = _counts(quanta)
    out = {
        "applied": True,
        "seq": result.get("patch_seq_end"),
        "scope_count": scope_count,
        "state_count": state_count,
        "quanta": render_quanta_text(quanta),
    }
    out.update(extra)
    return out


# ── write ───────────────────────────────────────────────────────────────
async def dispatch_set(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    quanta = args.get("quanta")
    if not isinstance(quanta, dict):
        raise ValueError(
            "set_quanta requires a 'quanta' object (state tree, or flat slides[] sugar)"
        )
    project = _project(ctx)
    result = project.apply_ops(
        [{"op": "set_quanta", "quanta": quanta}], label=QUANTA_OP_LABEL
    )
    return _summary(ctx, result)


def _edit_op_from(entry: Mapping[str, Any]) -> dict[str, Any]:
    op_kind = str(entry.get("op") or "patch")
    patch_op = _EDIT_OPS.get(op_kind)
    if patch_op is None:
        raise ValueError(
            f"update_quantum op must be one of {sorted(_EDIT_OPS)}, got {op_kind!r}"
        )
    if op_kind == "patch":
        quantum_id = str(entry.get("quantum_id") or "")
        fields = entry.get("fields")
        if not quantum_id:
            raise ValueError("update_quantum patch requires 'quantum_id'")
        if not isinstance(fields, dict) or not fields:
            raise ValueError("update_quantum patch requires a non-empty 'fields' object")
        return {"op": patch_op, "quantum_id": quantum_id, "fields": fields}
    if op_kind == "insert":
        quantum = entry.get("quantum")
        if not isinstance(quantum, dict):
            raise ValueError("update_quantum insert requires a 'quantum' object")
        out: dict[str, Any] = {
            "op": patch_op,
            "parent_id": str(entry.get("parent_id") or "root"),
            "quantum": quantum,
        }
        if entry.get("index") is not None:
            out["index"] = entry.get("index")
        return out
    if op_kind == "remove":
        quantum_id = str(entry.get("quantum_id") or "")
        if not quantum_id:
            raise ValueError("update_quantum remove requires 'quantum_id'")
        return {"op": patch_op, "quantum_id": quantum_id}
    # move
    quantum_id = str(entry.get("quantum_id") or "")
    if not quantum_id:
        raise ValueError("update_quantum move requires 'quantum_id'")
    out = {
        "op": patch_op,
        "quantum_id": quantum_id,
        "parent_id": str(entry.get("parent_id") or "root"),
    }
    if entry.get("index") is not None:
        out["index"] = entry.get("index")
    return out


async def dispatch_update_quantum(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """The edit tree's single entry point: node-addressed field patches plus
    insert/remove/move structure ops, one at a time or as one atomic batch
    (``ops``) — e.g. remove a quantum AND retarget its in-edges together."""
    ops_arg = args.get("ops")
    if isinstance(ops_arg, list) and ops_arg:
        entries = [entry for entry in ops_arg if isinstance(entry, Mapping)]
        if len(entries) != len(ops_arg):
            raise ValueError("update_quantum ops must be an array of objects")
        mapped = [_edit_op_from(entry) for entry in entries]
    else:
        mapped = [_edit_op_from(args)]
    project = _project(ctx)
    result = project.apply_ops(mapped, label=QUANTA_OP_LABEL)
    touched = [
        str(op.get("quantum_id") or op.get("parent_id") or "") for op in mapped
    ]
    return _summary(ctx, result, updated_quanta=[t for t in touched if t])


# ── read ────────────────────────────────────────────────────────────────
async def dispatch_get(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    quanta = _project(ctx).load().get("quanta") or {}
    scope_count, state_count = _counts(quanta)
    return {
        "scope_count": scope_count,
        "state_count": state_count,
        "quanta_text": render_quanta_text(quanta),
        "quanta": quanta,
    }


__all__ = ["dispatch_set", "dispatch_update_quantum", "dispatch_get", "render_quanta_text"]
