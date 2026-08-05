"""The discrete-video traversal kernel: one ordered state tree, three faces.

Pure functions (JSON in, JSON out; no I/O, no external state — the same
determinism discipline as ``layout.py``). Every consumer of "which states, in
what order" derives from here, so autoplay, mp4 flattening, the future player,
and the ``get_quanta`` text view can never disagree:

- ``lift_flat_quanta``  v1 flat ``{slides, default_path}`` → v2 state tree
  (idempotent; the authoring sugar normalize relies on).
- ``leaf_walk``         DFS leaf order = the default path (``default_path``
  as a stored field no longer exists in v2).
- ``flat_view``         tree → canonical v1 projection consumed by the
  layout/raster materialization chain (groups vanish, hidden subtrees drop).
- ``step`` / ``run``    the interaction interpreter: (tree, cursor, event) →
  next state. Continuous video is the degenerate case — a linear leaf chain,
  all-auto advance, zero holds — and needs no special branch anywhere here.
- ``flatten``           the autoplay/mp4 projection: (state, dwell,
  transition_in) per visible leaf; interaction edges are never followed.

Node vocabulary (kind is derived from structure, never stored):

- group    no ``blocks``, not under a content node; pure structure.
- content  declares ``blocks``; the geometry scope (today's slide).
- state    child of a content node; one discrete render state (today's
  build) carrying ``visible_block_ids`` + ``dwell_sec`` + ``advance``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterator, Mapping

QUANTA_TREE_VERSION = 2
ADVANCE_WAIT = "wait"
ADVANCE_AUTO = "auto"
_ADVANCE_MODES = {ADVANCE_WAIT, ADVANCE_AUTO}

END = "END"


class QuantaTraverseError(ValueError):
    """Raised when a quanta doc is too malformed to traverse deterministically."""


# ── tree shape helpers ──────────────────────────────────────────────────


def is_tree_doc(quanta: Mapping[str, Any] | None) -> bool:
    """True when the doc already carries the v2 ``root`` tree."""
    return isinstance(quanta, Mapping) and isinstance(quanta.get("root"), Mapping)


def is_content_node(node: Mapping[str, Any]) -> bool:
    """A node that declares ``blocks`` is a content scope (today's slide)."""
    return isinstance(node, Mapping) and isinstance(node.get("blocks"), list)


def _children(node: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    raw = node.get("children")
    if not isinstance(raw, list):
        return []
    return [child for child in raw if isinstance(child, Mapping)]


def _is_hidden(node: Mapping[str, Any]) -> bool:
    return bool(node.get("hidden"))


def _node_id(node: Mapping[str, Any]) -> str:
    return str(node.get("id") or "")


# ── lift: v1 flat authoring sugar → v2 tree ─────────────────────────────


def _lift_state_id(slide_id: str, build_id: str) -> str:
    """Document-unique state id. Already-prefixed ids never re-prefix, so
    ``lift(flat_view(lift(x))) == lift(x)`` — round trips cannot drift."""
    if build_id.startswith(f"{slide_id}_"):
        return build_id
    return f"{slide_id}_{build_id}"


def _lift_link(link: Mapping[str, Any]) -> dict[str, Any]:
    target = str(link.get("target") or "")
    if target.startswith("slide:"):
        target = "quantum:" + target[len("slide:"):]
    return {"trigger": str(link.get("trigger") or ""), "target": target}


def lift_flat_quanta(quanta: Mapping[str, Any] | None) -> dict[str, Any]:
    """Deterministically lift a canonical v1 flat quanta into the v2 tree.

    Idempotent: a doc that already has ``root`` is returned unchanged (same
    object semantics as a normalize pass — callers own copying). The v1
    ``default_path`` orders the lifted scopes and is then dropped; ``builds``
    become state children with document-unique prefixed ids; ``slide:`` link
    targets are rewritten to ``quantum:``.
    """
    if not isinstance(quanta, Mapping):
        return {
            "version": QUANTA_TREE_VERSION,
            "theme": {"tokens": {}, "mood": "", "aspect": "16:9"},
            "root": {"id": "root", "children": []},
        }
    if is_tree_doc(quanta):
        doc = dict(quanta)
        doc["version"] = QUANTA_TREE_VERSION
        return doc

    slides = [s for s in quanta.get("slides") or [] if isinstance(s, Mapping)]
    by_id = {_node_id(slide): slide for slide in slides}
    path = [str(item) for item in quanta.get("default_path") or [] if str(item or "")]
    ordered = (
        [by_id[sid] for sid in path if sid in by_id]
        if path and set(path) == set(by_id) and len(path) == len(by_id)
        else slides
    )

    children: list[dict[str, Any]] = []
    for slide in ordered:
        slide_id = _node_id(slide)
        states = []
        for build in slide.get("builds") or []:
            if not isinstance(build, Mapping):
                continue
            advance = str(build.get("advance") or ADVANCE_WAIT)
            states.append({
                "id": _lift_state_id(slide_id, str(build.get("id") or "")),
                "visible_block_ids": list(build.get("visible_block_ids") or []),
                "dwell_sec": build.get("dwell_sec"),
                "advance": advance if advance in _ADVANCE_MODES else ADVANCE_WAIT,
            })
        node: dict[str, Any] = {
            "id": slide_id,
            "layout": str(slide.get("layout") or "content"),
            "title": str(slide.get("title") or ""),
            "blocks": list(slide.get("blocks") or []),
            "notes": str(slide.get("notes") or ""),
            "mood_override": slide.get("mood_override"),
            "transition": dict(slide.get("transition") or {"kind": "cut"}),
            "links": [
                _lift_link(link)
                for link in slide.get("links") or []
                if isinstance(link, Mapping)
            ],
            "hidden": _is_hidden(slide),
            "children": states,
        }
        children.append(node)

    theme_raw = quanta.get("theme") if isinstance(quanta.get("theme"), Mapping) else {}
    return {
        "version": QUANTA_TREE_VERSION,
        "theme": {
            "tokens": dict(theme_raw.get("tokens") or {}),
            "mood": str(theme_raw.get("mood") or ""),
            "aspect": str(theme_raw.get("aspect") or "16:9"),
        },
        "root": {"id": "root", "children": children},
    }


# ── leaf walk ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Leaf:
    state_id: str
    scope_id: str
    scope_index: int
    state_index: int
    dwell_sec: float
    advance: str
    ancestor_ids: tuple[str, ...] = field(default_factory=tuple)


def _root(quanta: Mapping[str, Any]) -> Mapping[str, Any]:
    root = quanta.get("root") if isinstance(quanta, Mapping) else None
    if not isinstance(root, Mapping):
        raise QuantaTraverseError(
            "quanta doc has no state tree root — lift/normalize it first"
        )
    return root


def _iter_scopes(
    node: Mapping[str, Any],
    *,
    include_hidden: bool,
    hidden_ancestor: bool = False,
    ancestors: tuple[str, ...] = (),
) -> Iterator[tuple[Mapping[str, Any], tuple[str, ...], bool]]:
    """Yield ``(content_node, ancestor_ids, in_hidden)`` in DFS pre-order."""
    for child in _children(node):
        child_hidden = hidden_ancestor or _is_hidden(child)
        if child_hidden and not include_hidden:
            continue
        if is_content_node(child):
            yield child, (*ancestors, _node_id(node)), child_hidden
        else:
            yield from _iter_scopes(
                child,
                include_hidden=include_hidden,
                hidden_ancestor=child_hidden,
                ancestors=(*ancestors, _node_id(node)),
            )


def leaf_walk(
    quanta: Mapping[str, Any], *, include_hidden: bool = False
) -> tuple[Leaf, ...]:
    """DFS state-leaf order — the default path of the discrete video.

    ``scope_index``/``state_index`` are one-for-one order-preserving with the
    v1 ``slide_index``/``build_index`` pair, so the pager URL contract is
    untouched. Hidden subtrees are excluded unless ``include_hidden`` (jump
    target resolution and detour advance use the full walk).
    """
    leaves: list[Leaf] = []
    root = _root(quanta)
    for scope_index, (scope, ancestors, scope_hidden) in enumerate(
        _iter_scopes(root, include_hidden=include_hidden)
    ):
        scope_id = _node_id(scope)
        state_index = 0
        for state in _children(scope):
            if is_content_node(state):
                raise QuantaTraverseError(
                    f"content node {scope_id!r} nests another content node "
                    f"{_node_id(state)!r}; content children must be states"
                )
            if _is_hidden(state) and not include_hidden:
                continue
            advance = str(state.get("advance") or ADVANCE_WAIT)
            leaves.append(Leaf(
                state_id=_node_id(state),
                scope_id=scope_id,
                scope_index=scope_index,
                state_index=state_index,
                dwell_sec=float(state.get("dwell_sec") or 0.0),
                advance=advance if advance in _ADVANCE_MODES else ADVANCE_WAIT,
                ancestor_ids=(*ancestors, scope_id),
            ))
            state_index += 1
    return tuple(leaves)


# ── flat view: tree → canonical v1 projection ───────────────────────────


def flat_view(
    quanta: Mapping[str, Any], *, include_hidden: bool = False
) -> dict[str, Any]:
    """Project the tree back to the canonical v1 flat shape.

    This is the single seam that keeps ``layout.py``/``raster.py`` and the
    whole placed-blocks → PNG → timeline chain rewrite-free: they keep
    consuming ``{slides, default_path}`` while the tree stays the source of
    truth. Groups vanish (pure structure), hidden subtrees drop (unless
    ``include_hidden``), scope order is walk order.
    """
    root = _root(quanta)
    slides: list[dict[str, Any]] = []
    for scope, _ancestors, _hidden in _iter_scopes(root, include_hidden=include_hidden):
        builds = []
        for state in _children(scope):
            if _is_hidden(state) and not include_hidden:
                continue
            builds.append({
                "id": _node_id(state),
                "dwell_sec": state.get("dwell_sec"),
                "visible_block_ids": list(state.get("visible_block_ids") or []),
            })
        slides.append({
            "id": _node_id(scope),
            "layout": str(scope.get("layout") or "content"),
            "title": str(scope.get("title") or ""),
            "blocks": list(scope.get("blocks") or []),
            "notes": str(scope.get("notes") or ""),
            "mood_override": scope.get("mood_override"),
            "builds": builds,
            "links": [dict(link) for link in scope.get("links") or [] if isinstance(link, Mapping)],
            "transition": dict(scope.get("transition") or {"kind": "cut"}),
        })
    theme_raw = quanta.get("theme") if isinstance(quanta.get("theme"), Mapping) else {}
    return {
        "version": 1,
        "theme": {
            "tokens": dict(theme_raw.get("tokens") or {}),
            "mood": str(theme_raw.get("mood") or ""),
            "aspect": str(theme_raw.get("aspect") or "16:9"),
        },
        "slides": slides,
        "default_path": [slide["id"] for slide in slides],
    }


# ── node lookup ─────────────────────────────────────────────────────────


def _walk_nodes(
    node: Mapping[str, Any],
) -> Iterator[Mapping[str, Any]]:
    yield node
    for child in _children(node):
        yield from _walk_nodes(child)


def find_node(quanta: Mapping[str, Any], node_id: str) -> Mapping[str, Any] | None:
    """Locate any node (group/content/state) by document-unique id."""
    for node in _walk_nodes(_root(quanta)):
        if _node_id(node) == node_id:
            return node
    return None


def first_leaf_of(quanta: Mapping[str, Any], node_id: str) -> Leaf | None:
    """First state leaf inside a subtree, in full-walk order (hidden included:
    an explicit jump may enter a hidden subtree). A state node id resolves to
    that state itself."""
    target = find_node(quanta, node_id)
    if target is None:
        return None
    subtree_ids = {_node_id(node) for node in _walk_nodes(target)}
    for leaf in leaf_walk(quanta, include_hidden=True):
        if leaf.state_id in subtree_ids or subtree_ids.intersection(leaf.ancestor_ids):
            return leaf
    return None


# ── step: the interaction interpreter ───────────────────────────────────


@dataclass(frozen=True)
class StepResult:
    to: str                      # state id, or END
    effect: dict[str, Any] | None = None
    transition: dict[str, Any] | None = None


def _leaf_index(leaves: tuple[Leaf, ...], state_id: str) -> int | None:
    for index, leaf in enumerate(leaves):
        if leaf.state_id == state_id:
            return index
    return None


def _nearest_hidden_ancestor(
    quanta: Mapping[str, Any], leaf: Leaf
) -> Mapping[str, Any] | None:
    """Outermost hidden ancestor (or the leaf's own hidden flag holder)."""
    root = _root(quanta)
    by_id = {_node_id(node): node for node in _walk_nodes(root)}
    for ancestor_id in leaf.ancestor_ids:
        node = by_id.get(ancestor_id)
        if node is not None and _is_hidden(node):
            return node
    node = by_id.get(leaf.state_id)
    if node is not None and _is_hidden(node):
        return node
    return None


def _transition_into(quanta: Mapping[str, Any], from_leaf: Leaf | None, to_leaf: Leaf) -> dict[str, Any] | None:
    if from_leaf is not None and from_leaf.scope_id == to_leaf.scope_id:
        return None
    scope = find_node(quanta, to_leaf.scope_id)
    if scope is None:
        return None
    transition = scope.get("transition")
    return dict(transition) if isinstance(transition, Mapping) else None


def _advance_target(quanta: Mapping[str, Any], cursor: Leaf) -> Leaf | None:
    """Structural advance with hidden-detour semantics.

    - From a visible leaf: next leaf in the VISIBLE walk.
    - Inside a hidden subtree H (reachable only via explicit jumps): walk H's
      own leaves in order; past its last leaf, resume at the next visible
      leaf after H — the PowerPoint hidden-appendix detour, deterministic.
    """
    hidden_root = _nearest_hidden_ancestor(quanta, cursor)
    full = leaf_walk(quanta, include_hidden=True)
    visible = leaf_walk(quanta)
    cursor_full = _leaf_index(full, cursor.state_id)
    if cursor_full is None:
        raise QuantaTraverseError(f"unknown cursor state: {cursor.state_id!r}")
    if hidden_root is None:
        visible_ids = [leaf.state_id for leaf in visible]
        try:
            index = visible_ids.index(cursor.state_id)
        except ValueError:  # pragma: no cover — visible cursor is always in walk
            raise QuantaTraverseError(f"cursor state not in visible walk: {cursor.state_id!r}")
        return visible[index + 1] if index + 1 < len(visible) else None
    subtree_ids = {_node_id(node) for node in _walk_nodes(hidden_root)}
    for leaf in full[cursor_full + 1:]:
        inside = leaf.state_id in subtree_ids or subtree_ids.intersection(leaf.ancestor_ids)
        if inside:
            return leaf
        if not _nearest_hidden_ancestor(quanta, leaf):
            return leaf
    return None


def _back_target(quanta: Mapping[str, Any], cursor: Leaf) -> Leaf | None:
    hidden_root = _nearest_hidden_ancestor(quanta, cursor)
    full = leaf_walk(quanta, include_hidden=True)
    visible = leaf_walk(quanta)
    if hidden_root is None:
        visible_ids = [leaf.state_id for leaf in visible]
        index = visible_ids.index(cursor.state_id)
        return visible[index - 1] if index > 0 else None
    subtree_ids = {_node_id(node) for node in _walk_nodes(hidden_root)}
    cursor_full = _leaf_index(full, cursor.state_id)
    for leaf in reversed(full[:cursor_full]):
        inside = leaf.state_id in subtree_ids or subtree_ids.intersection(leaf.ancestor_ids)
        if inside:
            return leaf
        if not _nearest_hidden_ancestor(quanta, leaf):
            return leaf
    return None


def _resolve_target(
    quanta: Mapping[str, Any], cursor: Leaf, target: str
) -> tuple[Leaf | None, dict[str, Any] | None, bool]:
    """→ (leaf, effect, is_end). ``next`` is structural; ``quantum:<id>``
    (or a bare id) resolves to the subtree's first leaf; ``url:`` is an
    external effect with an unchanged cursor."""
    if target.startswith("url:"):
        return cursor, {"url": target[len("url:"):]}, False
    if target == "next":
        advanced = _advance_target(quanta, cursor)
        return advanced, None, advanced is None
    ref = target[len("quantum:"):] if target.startswith("quantum:") else target
    if find_node(quanta, ref) is None:
        raise QuantaTraverseError(f"link target references missing quantum: {target!r}")
    leaf = first_leaf_of(quanta, ref)
    if leaf is None:
        raise QuantaTraverseError(f"link target subtree has no states: {target!r}")
    return leaf, None, False


def _links_of(node: Mapping[str, Any] | None) -> list[Mapping[str, Any]]:
    if node is None:
        return []
    return [link for link in node.get("links") or [] if isinstance(link, Mapping)]


def step(
    quanta: Mapping[str, Any],
    cursor_state_id: str | None,
    event: Mapping[str, Any],
) -> StepResult:
    """One interaction step. Events:

    - ``{"kind": "advance"}``  explicit state-mounted advance edge overrides
      its own state; a content-mounted advance edge is the scope's EXIT edge
      (fires only from the scope's final state — the v1 slide-level advance
      semantics, pinned); otherwise structural next-visible-leaf.
    - ``{"kind": "back"}``     previous leaf (never replays entry animations
      by construction: a state IS its finished look).
    - ``{"kind": "hotspot", "block_id": …}``  nearest mount wins: the state
      itself, then its content scope. No match → no-op.
    - ``{"kind": "goto", "target_id": …}``    jump to any quantum (hidden
      subtrees reachable — that is what hidden is for).

    Cycles are safe: one event, one step.
    """
    visible = leaf_walk(quanta)
    if not visible:
        raise QuantaTraverseError("quanta has no visible states")
    if cursor_state_id is None:
        first = visible[0]
        return StepResult(to=first.state_id, transition=_transition_into(quanta, None, first))
    full = leaf_walk(quanta, include_hidden=True)
    cursor_index = _leaf_index(full, cursor_state_id)
    if cursor_index is None:
        raise QuantaTraverseError(f"unknown cursor state: {cursor_state_id!r}")
    cursor = full[cursor_index]
    kind = str(event.get("kind") or "")

    if kind == "advance":
        state_node = find_node(quanta, cursor.state_id)
        scope_node = find_node(quanta, cursor.scope_id)
        override = next(
            (link for link in _links_of(state_node) if str(link.get("trigger")) == "advance"),
            None,
        )
        if override is None:
            scope_states = [leaf for leaf in full if leaf.scope_id == cursor.scope_id]
            at_scope_end = scope_states and scope_states[-1].state_id == cursor.state_id
            if at_scope_end:
                override = next(
                    (link for link in _links_of(scope_node) if str(link.get("trigger")) == "advance"),
                    None,
                )
        target = str(override.get("target")) if override else "next"
        leaf, effect, is_end = _resolve_target(quanta, cursor, target)
        if is_end or leaf is None:
            return StepResult(to=END)
        return StepResult(to=leaf.state_id, effect=effect,
                          transition=_transition_into(quanta, cursor, leaf))

    if kind == "back":
        leaf = _back_target(quanta, cursor)
        if leaf is None:
            return StepResult(to=cursor.state_id)
        return StepResult(to=leaf.state_id, transition=_transition_into(quanta, cursor, leaf))

    if kind == "hotspot":
        block_id = str(event.get("block_id") or "")
        trigger = f"hotspot:{block_id}"
        state_node = find_node(quanta, cursor.state_id)
        scope_node = find_node(quanta, cursor.scope_id)
        match = next(
            (link for links in (_links_of(state_node), _links_of(scope_node))
             for link in links if str(link.get("trigger")) == trigger),
            None,
        )
        if match is None:
            return StepResult(to=cursor.state_id)
        leaf, effect, is_end = _resolve_target(quanta, cursor, str(match.get("target")))
        if is_end or leaf is None:
            return StepResult(to=END)
        transition = None if leaf.state_id == cursor.state_id else _transition_into(quanta, cursor, leaf)
        return StepResult(to=leaf.state_id, effect=effect, transition=transition)

    if kind == "goto":
        target_id = str(event.get("target_id") or "")
        leaf, effect, _ = _resolve_target(quanta, cursor, f"quantum:{target_id}")
        if leaf is None:
            return StepResult(to=cursor.state_id)
        transition = None if leaf.state_id == cursor.state_id else _transition_into(quanta, cursor, leaf)
        return StepResult(to=leaf.state_id, effect=effect, transition=transition)

    raise QuantaTraverseError(f"unknown traversal event kind: {kind!r}")


def run(
    quanta: Mapping[str, Any],
    events: list[Mapping[str, Any]],
    *,
    start: str | None = None,
) -> tuple[str, ...]:
    """Fold ``step`` over an event stream: tree + edges + events → state
    sequence. The first element is the entry state (``start`` itself when
    given, else the first visible leaf)."""
    if start is None:
        cursor = step(quanta, None, {}).to
    else:
        if _leaf_index(leaf_walk(quanta, include_hidden=True), start) is None:
            raise QuantaTraverseError(f"unknown start state: {start!r}")
        cursor = start
    sequence = [cursor]
    for event in events:
        if cursor == END:
            break
        result = step(quanta, cursor, event)
        sequence.append(result.to)
        cursor = result.to
    return tuple(sequence)


# ── flatten: the autoplay / mp4 projection ──────────────────────────────


@dataclass(frozen=True)
class FlatFrame:
    state_id: str
    scope_id: str
    scope_index: int
    state_index: int
    dwell_sec: float
    transition_in: str   # scope transition on scope-first frames, else "cut"


def flatten(quanta: Mapping[str, Any]) -> tuple[FlatFrame, ...]:
    """Default-walk projection for autoplay and mp4 flattening.

    Interaction edges are never followed (flattening cannot wait for a
    click — a media fact, acknowledged explicitly via
    ``flattened_interactions`` rather than silently dropped). Every dwell
    must be positive.
    """
    frames: list[FlatFrame] = []
    for leaf in leaf_walk(quanta):
        if leaf.dwell_sec <= 0:
            raise QuantaTraverseError(
                f"state {leaf.state_id!r} dwell_sec must be > 0 to flatten"
            )
        transition_in = "cut"
        if leaf.state_index == 0:
            scope = find_node(quanta, leaf.scope_id)
            raw = (scope or {}).get("transition")
            if isinstance(raw, Mapping):
                transition_in = str(raw.get("kind") or "cut")
        frames.append(FlatFrame(
            state_id=leaf.state_id,
            scope_id=leaf.scope_id,
            scope_index=leaf.scope_index,
            state_index=leaf.state_index,
            dwell_sec=leaf.dwell_sec,
            transition_in=transition_in,
        ))
    return tuple(frames)


def flattened_interactions(quanta: Mapping[str, Any]) -> tuple[str, ...]:
    """Ids of nodes whose non-implicit interaction edges a flatten discards
    (feeds the assemble ``degradations`` report)."""
    ids: list[str] = []
    for node in _walk_nodes(_root(quanta)):
        for link in _links_of(node):
            trigger = str(link.get("trigger") or "")
            target = str(link.get("target") or "")
            if trigger == "advance" and target == "next":
                continue
            node_id = _node_id(node)
            if node_id and node_id not in ids:
                ids.append(node_id)
    return tuple(ids)


__all__ = [
    "ADVANCE_AUTO",
    "ADVANCE_WAIT",
    "END",
    "FlatFrame",
    "Leaf",
    "QuantaTraverseError",
    "QUANTA_TREE_VERSION",
    "StepResult",
    "find_node",
    "first_leaf_of",
    "flat_view",
    "flatten",
    "flattened_interactions",
    "is_content_node",
    "is_tree_doc",
    "leaf_walk",
    "lift_flat_quanta",
    "run",
    "step",
]
