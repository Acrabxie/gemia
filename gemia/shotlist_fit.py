"""Globally fit persistent media evidence to rough-cut outline nodes.

This is an internal, synchronous planning primitive.  The caller supplies a
plain candidate provider; database access, session asset registration and
project persistence remain outside this module.
"""
from __future__ import annotations

import inspect
import math
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from gemia.media_evidence import (
    ALGORITHM_VERSION,
    evaluate_evidence_candidates,
    ranges_conflict,
)


CandidateProvider = Callable[[str, dict[str, Any]], Mapping[str, Any] | list[Any] | None]

_EXACT_SEARCH_LIMIT = 250_000
_DEFAULT_BEAM_WIDTH = 512


@dataclass(frozen=True)
class _Node:
    index: int
    shot_id: str
    query: str
    duration_sec: float
    shot: dict[str, Any]
    candidates: tuple[dict[str, Any], ...]
    excluded: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class _State:
    # Pairs use original target-node indexes, independent of solver order.
    selections: tuple[tuple[int, dict[str, Any]], ...]
    coverage: int
    preferred: int
    score: float


def fit_shotlist_to_media(
    shotlist: Mapping[str, Any],
    candidate_provider: CandidateProvider | Mapping[str, Any],
    *,
    overwrite: bool = False,
    beam_width: int = _DEFAULT_BEAM_WIDTH,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Fit range evidence to shots while optimizing the outline as a whole.

    The provider is normally ``(query, shot) -> search result dict``.  For pure
    tests/callers it may instead be a mapping keyed by shot id (then query).

    The returned shotlist is a deep copy.  Selected shots receive a durable
    ``library_asset_id``, exact ``source_in``/``source_out``, full evidence
    provenance, and at most three explainable alternatives.  ``asset_id`` is
    intentionally left unset: the integration layer must register the library
    asset in its session before persisting the result.
    """
    if not isinstance(shotlist, Mapping):
        raise TypeError("shotlist must be a mapping")
    if not callable(candidate_provider) and not isinstance(candidate_provider, Mapping):
        raise TypeError("candidate_provider must be callable or a mapping")
    try:
        beam_width = max(8, int(beam_width))
    except (TypeError, ValueError):
        beam_width = _DEFAULT_BEAM_WIDTH

    fitted = deepcopy(dict(shotlist))
    shot_refs = _shot_refs(fitted)
    fixed: list[dict[str, Any]] = []
    targets: list[tuple[str, dict[str, Any]]] = []
    existing_filled = 0
    for fallback_id, shot in shot_refs:
        has_asset = bool(shot.get("asset_id") or shot.get("library_asset_id"))
        if has_asset and not overwrite:
            existing_filled += 1
            fixed_candidate = _fixed_evidence(shot)
            if fixed_candidate is not None:
                fixed.append(fixed_candidate)
            continue
        targets.append((fallback_id, shot))

    nodes: list[_Node] = []
    evidence_map: list[dict[str, Any]] = []
    for index, (fallback_id, shot) in enumerate(targets):
        shot_id = str(shot.get("id") or fallback_id)
        query = _shot_query(shot)
        duration = _positive_float(shot.get("duration_sec"), fallback=3.0)
        if query:
            provided = _provide(candidate_provider, shot_id, query, shot)
            search_result = _as_search_result(provided)
            evaluated = evaluate_evidence_candidates(
                search_result,
                query=query,
                desired_duration_sec=duration,
            )
            candidates = tuple(_normalize_node_scores(evaluated["candidates"]))
            excluded = tuple(evaluated["excluded"])
        else:
            candidates = ()
            excluded = ()
        node = _Node(
            index=index,
            shot_id=shot_id,
            query=query,
            duration_sec=duration,
            shot=shot,
            candidates=candidates,
            excluded=excluded,
        )
        nodes.append(node)
        evidence_map.append(
            {
                "shot_id": shot_id,
                "query": query,
                "duration_sec": round(duration, 6),
                "candidate_count": len(candidates),
                "excluded": deepcopy(list(excluded)),
            }
        )

    best, solver = _solve(nodes, fixed=fixed, beam_width=beam_width)
    selected = {node_index: candidate for node_index, candidate in best.selections}
    assignments: list[dict[str, Any]] = []
    unfilled: list[dict[str, Any]] = []

    for node in nodes:
        chosen = selected.get(node.index)
        if chosen is None:
            if not node.query:
                reason = "no search query or shot description"
            elif not node.candidates:
                reason = "no viable time-ranged evidence long enough for the shot"
            else:
                reason = "no non-overlapping evidence in the global assignment"
            unfilled.append(
                {"shot_id": node.shot_id, "query": node.query, "reason": reason}
            )
            continue

        start_sec = float(chosen["start_sec"])
        source_out = start_sec + node.duration_sec
        # Candidate construction guarantees coverage; this guards float noise.
        source_out = min(source_out, float(chosen["end_sec"]))
        node.shot["asset_id"] = None
        node.shot["library_asset_id"] = chosen["library_asset_id"]
        node.shot["source"] = "search"
        node.shot["source_in"] = round(start_sec, 6)
        node.shot["source_out"] = round(source_out, 6)
        node.shot["evidence"] = deepcopy(chosen)
        node.shot["alternatives"] = _alternatives(
            node,
            chosen,
            selected=selected,
            fixed=fixed,
            nodes=nodes,
        )
        node.shot["status"] = "filled"
        assignments.append(
            {
                "shot_id": node.shot_id,
                "query": node.query,
                "evidence_id": chosen["evidence_id"],
                "library_asset_id": chosen["library_asset_id"],
                "annotation_id": chosen["annotation_id"],
                "source_in": round(start_sec, 6),
                "source_out": round(source_out, 6),
                "decision": chosen["decision"],
                "confidence": chosen.get("confidence"),
                "score": chosen["score"],
                "ranking_reasons": deepcopy(chosen["ranking_reasons"]),
            }
        )

    report = {
        "algorithm_version": ALGORITHM_VERSION,
        "solver": solver,
        "coverage": {"filled": len(assignments), "total": len(nodes)},
        "existing_filled": existing_filled,
        "assignments": assignments,
        "unfilled": unfilled,
        "evidence_map": evidence_map,
    }
    return fitted, report


def _solve(
    nodes: list[_Node], *, fixed: list[dict[str, Any]], beam_width: int
) -> tuple[_State, str]:
    if not nodes:
        return _State((), 0, 0, 0.0), "exact"
    # Most-constrained first improves both exact pruning and beam diversity,
    # while selection/report order remains the original outline order.
    order = sorted(nodes, key=lambda node: (len(node.candidates), node.index))
    search_space = 1
    for node in order:
        search_space *= len(node.candidates) + 1
        if search_space > _EXACT_SEARCH_LIMIT:
            break
    if search_space <= _EXACT_SEARCH_LIMIT:
        return _solve_exact(order, fixed=fixed, node_count=len(nodes)), "exact"
    return (
        _solve_beam(
            order,
            fixed=fixed,
            node_count=len(nodes),
            beam_width=beam_width,
        ),
        f"beam:{beam_width}",
    )


def _solve_exact(
    order: list[_Node], *, fixed: list[dict[str, Any]], node_count: int
) -> _State:
    best: _State | None = None

    def visit(position: int, state: _State) -> None:
        nonlocal best
        if position >= len(order):
            if best is None or _is_better(state, best, node_count=node_count):
                best = state
            return
        node = order[position]
        for candidate in node.candidates:
            if _compatible(candidate, state.selections, fixed):
                visit(position + 1, _extend(state, node.index, candidate))
        visit(position + 1, state)

    visit(0, _State((), 0, 0, 0.0))
    return best or _State((), 0, 0, 0.0)


def _solve_beam(
    order: list[_Node],
    *,
    fixed: list[dict[str, Any]],
    node_count: int,
    beam_width: int,
) -> _State:
    states = [_State((), 0, 0, 0.0)]
    for node in order:
        expanded: list[_State] = []
        for state in states:
            for candidate in node.candidates:
                if _compatible(candidate, state.selections, fixed):
                    expanded.append(_extend(state, node.index, candidate))
            expanded.append(state)
        expanded.sort(key=lambda state: _state_sort_key(state, node_count=node_count))
        states = expanded[:beam_width]
    return min(states, key=lambda state: _state_sort_key(state, node_count=node_count))


def _extend(state: _State, node_index: int, candidate: dict[str, Any]) -> _State:
    return _State(
        selections=state.selections + ((node_index, candidate),),
        coverage=state.coverage + 1,
        preferred=state.preferred + int(candidate.get("authority_tier") or 0),
        score=state.score + _float_or(candidate.get("score"), 0.0),
    )


def _compatible(
    candidate: Mapping[str, Any],
    selections: tuple[tuple[int, dict[str, Any]], ...],
    fixed: list[dict[str, Any]],
) -> bool:
    for other in fixed:
        if _evidence_conflict(candidate, other):
            return False
    for _node_index, other in selections:
        if _evidence_conflict(candidate, other):
            return False
    return True


def _evidence_conflict(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    if str(left.get("library_asset_id") or "") != str(
        right.get("library_asset_id") or ""
    ):
        return False
    left_id = str(left.get("evidence_id") or left.get("annotation_id") or "")
    right_id = str(right.get("evidence_id") or right.get("annotation_id") or "")
    if left_id and right_id and left_id == right_id:
        return True
    return ranges_conflict(left, right, iou_threshold=0.8)


def _is_better(left: _State, right: _State, *, node_count: int) -> bool:
    return _state_sort_key(left, node_count=node_count) < _state_sort_key(
        right, node_count=node_count
    )


def _state_sort_key(state: _State, *, node_count: int) -> tuple[Any, ...]:
    by_node = {node_index: candidate for node_index, candidate in state.selections}
    tie_key = tuple(
        _candidate_identity(by_node[index]) if index in by_node else ("~",)
        for index in range(node_count)
    )
    return (
        -state.coverage,
        -state.preferred,
        -round(state.score, 9),
        tie_key,
    )


def _candidate_identity(candidate: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        str(candidate.get("library_asset_id") or ""),
        round(_float_or(candidate.get("start_sec"), 0.0), 6),
        round(_float_or(candidate.get("end_sec"), 0.0), 6),
        str(candidate.get("annotation_id") or ""),
    )


def _normalize_node_scores(
    candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Put every query's scores on the same 0..1 scale before global fitting.

    SQLite BM25 magnitudes are query-local.  Summing them across outline nodes
    would let one query's numeric scale dominate another node's choice.
    """
    if not candidates:
        return []
    raw_scores = [_float_or(item.get("score"), 0.0) for item in candidates]
    low, high = min(raw_scores), max(raw_scores)
    normalized: list[dict[str, Any]] = []
    for item, raw_score in zip(candidates, raw_scores):
        candidate = deepcopy(item)
        node_score = 1.0 if high - low <= 1e-12 else (raw_score - low) / (high - low)
        candidate["raw_score"] = round(raw_score, 6)
        candidate["score"] = round(node_score, 6)
        components = (
            deepcopy(candidate.get("score_components"))
            if isinstance(candidate.get("score_components"), dict)
            else {}
        )
        components["node_normalized_score"] = round(node_score, 6)
        candidate["score_components"] = components
        reasons = [str(reason) for reason in candidate.get("ranking_reasons") or []]
        reasons.append(f"query-local normalized score {node_score:.3f}")
        candidate["ranking_reasons"] = reasons
        normalized.append(candidate)
    return normalized


def _alternatives(
    node: _Node,
    chosen: Mapping[str, Any],
    *,
    selected: dict[int, dict[str, Any]],
    fixed: list[dict[str, Any]],
    nodes: list[_Node],
) -> list[dict[str, Any]]:
    alternatives: list[dict[str, Any]] = []
    node_names = {item.index: item.shot_id for item in nodes}
    for candidate in node.candidates:
        if _candidate_identity(candidate) == _candidate_identity(chosen):
            continue
        conflicts: list[str] = []
        for index, other in selected.items():
            if index != node.index and _evidence_conflict(candidate, other):
                conflicts.append(node_names.get(index, str(index)))
        if any(_evidence_conflict(candidate, other) for other in fixed):
            conflicts.append("an existing filled shot")
        if conflicts:
            reason = "overlaps evidence selected for " + ", ".join(sorted(conflicts))
        elif int(candidate.get("authority_tier") or 0) < int(
            chosen.get("authority_tier") or 0
        ):
            reason = "lower user-authority tier"
        else:
            reason = "lower global fit score"
        alternative = deepcopy(candidate)
        alternative["reason_not_selected"] = reason
        alternatives.append(alternative)
        if len(alternatives) == 3:
            break
    return alternatives


def _provide(
    provider: CandidateProvider | Mapping[str, Any],
    shot_id: str,
    query: str,
    shot: dict[str, Any],
) -> Any:
    if callable(provider):
        value = provider(query, deepcopy(shot))
        if inspect.isawaitable(value):
            raise TypeError("candidate_provider must be synchronous")
        return value
    if shot_id in provider:
        return provider[shot_id]
    if query in provider:
        return provider[query]
    return {"results": []}


def _as_search_result(raw: Any) -> dict[str, Any]:
    if raw is None:
        return {"results": []}
    if isinstance(raw, Mapping):
        if isinstance(raw.get("results"), list):
            return deepcopy(dict(raw))
        if raw.get("library_asset_id"):
            return {"results": [deepcopy(dict(raw))]}
        return {"results": []}
    if not isinstance(raw, list):
        raise TypeError("candidate_provider must return a search-result dict or list")
    if all(isinstance(item, Mapping) and item.get("time_ranges") is not None for item in raw):
        return {"results": deepcopy(raw)}

    # Convenience for explicit per-shot raw range rows.
    by_asset: dict[str, dict[str, Any]] = {}
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        library_asset_id = str(item.get("library_asset_id") or "").strip()
        if not library_asset_id:
            continue
        asset = by_asset.setdefault(
            library_asset_id,
            {
                "library_asset_id": library_asset_id,
                "name": str(item.get("asset_name") or "media"),
                "kind": str(item.get("kind") or "video"),
                "duration": item.get("asset_duration"),
                "score": item.get("asset_score", 0.0),
                "matched_terms": deepcopy(item.get("matched_terms") or []),
                "time_ranges": [],
                "annotations": [],
            },
        )
        row = deepcopy(dict(item))
        asset["time_ranges"].append(row)
        asset["annotations"].append(row)
    return {"results": list(by_asset.values())}


def _shot_refs(shotlist: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    refs: list[tuple[str, dict[str, Any]]] = []
    scenes = shotlist.get("scenes")
    if not isinstance(scenes, list):
        return refs
    for scene_index, scene in enumerate(scenes, start=1):
        if not isinstance(scene, dict):
            continue
        shots = scene.get("shots")
        if not isinstance(shots, list):
            continue
        for shot_index, shot in enumerate(shots, start=1):
            if isinstance(shot, dict):
                refs.append((f"s{scene_index}_shot{shot_index}", shot))
    return refs


def _shot_query(shot: Mapping[str, Any]) -> str:
    explicit = str(shot.get("search_query") or "").strip()
    if explicit:
        return explicit
    return str(
        shot.get("description")
        or shot.get("narration")
        or shot.get("mood")
        or ""
    ).strip()


def _fixed_evidence(shot: Mapping[str, Any]) -> dict[str, Any] | None:
    library_asset_id = str(shot.get("library_asset_id") or "").strip()
    evidence = shot.get("evidence")
    if not library_asset_id and isinstance(evidence, Mapping):
        library_asset_id = str(evidence.get("library_asset_id") or "").strip()
    if not library_asset_id:
        return None
    start = None
    end = None
    if isinstance(evidence, Mapping):
        start = _optional_float(evidence.get("start_sec"))
        end = _optional_float(evidence.get("end_sec"))
    if start is None or end is None or end <= start:
        start = _optional_float(shot.get("source_in"))
        end = _optional_float(shot.get("source_out"))
    if start is None or end is None or start < 0.0 or end <= start:
        return None
    return {
        "library_asset_id": library_asset_id,
        "evidence_id": str(
            evidence.get("evidence_id") if isinstance(evidence, Mapping) else ""
        ),
        "start_sec": start,
        "end_sec": end,
    }


def _positive_float(raw: Any, *, fallback: float) -> float:
    value = _optional_float(raw)
    return value if value is not None and value > 0.0 else fallback


def _optional_float(raw: Any) -> float | None:
    if isinstance(raw, bool):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _float_or(raw: Any, fallback: float) -> float:
    value = _optional_float(raw)
    return value if value is not None else fallback


__all__ = ["CandidateProvider", "fit_shotlist_to_media"]
