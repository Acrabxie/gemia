"""Pure, explainable ranking of time-ranged media evidence.

The persistence and retrieval layers deliberately live elsewhere.  This module
accepts the JSON-shaped result returned by ``search_media_annotations`` and
turns its time-range rows into deterministic rough-cut candidates.  It never
opens the media library and never registers a session asset.

User corrections are explicit.  A row merely having ``source='user'`` does
not make it a preference.  Hard authority comes from
``metadata.evidence.decision`` on a user-authored time-range row:

* ``reject`` vetoes candidates whose midpoint falls inside that correction;
* ``prefer`` is a ranking tier above ordinary observations;
* ``observe`` remains ordinary evidence and keeps its real confidence.
"""
from __future__ import annotations

import json
import math
from copy import deepcopy
from typing import Any, Mapping


ALGORITHM_VERSION = "evidence-fit-v1"
DEFAULT_CLAIM_KEY = "editorial.usability"

# This is deliberately a small, explainable quality component rather than an
# authority override.  Explicit user ``prefer`` is handled as a separate tier.
_SOURCE_QUALITY = {
    "user": 0.40,
    "gemini_vision": 0.32,
    "gemini": 0.28,
    "import": 0.20,
    "system": 0.16,
    "heuristic": 0.08,
}


def rank_evidence_candidates(
    search_result: Mapping[str, Any] | None,
    *,
    query: str,
    desired_duration_sec: float,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Return viable time-range candidates in deterministic rank order.

    ``search_result`` is the plain dictionary returned by
    :func:`gemia.media_search.search_media_annotations`.  Each asset may also
    carry ``annotations`` containing *all* of its persisted rows.  Those rows
    are consulted for user corrections even when the correction did not match
    the current lexical query.

    Asset-scope annotations are never converted into made-up source windows.
    A candidate must have a valid time range and must cover the requested shot
    duration in full.
    """
    evaluated = evaluate_evidence_candidates(
        search_result,
        query=query,
        desired_duration_sec=desired_duration_sec,
    )
    try:
        bounded_limit = max(1, int(limit))
    except (TypeError, ValueError):
        bounded_limit = 20
    return evaluated["candidates"][:bounded_limit]


def evaluate_evidence_candidates(
    search_result: Mapping[str, Any] | None,
    *,
    query: str,
    desired_duration_sec: float,
) -> dict[str, Any]:
    """Rank candidates and retain exclusion diagnostics for fit reports."""
    desired = _positive_float(desired_duration_sec, fallback=3.0)
    raw_results = (
        search_result.get("results")
        if isinstance(search_result, Mapping)
        else None
    )
    assets = raw_results if isinstance(raw_results, list) else []
    candidates: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []

    for raw_asset in assets:
        if not isinstance(raw_asset, Mapping):
            continue
        asset = dict(raw_asset)
        library_asset_id = str(asset.get("library_asset_id") or "").strip()
        if not library_asset_id:
            continue
        asset_duration = _optional_nonnegative_float(asset.get("duration"))
        controls = _user_controls(asset)
        raw_ranges = asset.get("time_ranges")
        if not isinstance(raw_ranges, list):
            continue

        for raw_row in raw_ranges:
            if not isinstance(raw_row, Mapping):
                continue
            row = dict(raw_row)
            source_range = _time_range(row, asset_duration=asset_duration)
            annotation_id = str(row.get("annotation_id") or "").strip()
            if source_range is None or not annotation_id:
                excluded.append(
                    _excluded_row(
                        library_asset_id,
                        annotation_id,
                        "not a traceable time-range annotation",
                        row,
                    )
                )
                continue
            start_sec, end_sec = source_range
            available = end_sec - start_sec
            if available + 1e-9 < desired:
                excluded.append(
                    _excluded_row(
                        library_asset_id,
                        annotation_id,
                        f"range {available:.3f}s is shorter than shot {desired:.3f}s",
                        row,
                        start_sec=start_sec,
                        end_sec=end_sec,
                    )
                )
                continue

            midpoint = start_sec + available / 2.0
            decision_control = _effective_control(controls, midpoint)
            decision = (
                str(decision_control.get("decision") or "observe")
                if decision_control
                else "observe"
            )
            if decision == "reject":
                control_id = str(decision_control.get("annotation_id") or "")
                excluded.append(
                    _excluded_row(
                        library_asset_id,
                        annotation_id,
                        f"rejected by user correction {control_id or '(unidentified)'}",
                        row,
                        start_sec=start_sec,
                        end_sec=end_sec,
                    )
                )
                continue

            candidates.append(
                _candidate(
                    asset,
                    row,
                    library_asset_id=library_asset_id,
                    annotation_id=annotation_id,
                    query=str(query or "").strip(),
                    desired=desired,
                    start_sec=start_sec,
                    end_sec=end_sec,
                    decision_control=decision_control,
                )
            )

    candidates.sort(key=_candidate_sort_key)
    excluded.sort(
        key=lambda item: (
            str(item.get("library_asset_id") or ""),
            _float_or(item.get("start_sec"), -1.0),
            str(item.get("annotation_id") or ""),
            str(item.get("reason") or ""),
        )
    )
    return {
        "algorithm_version": ALGORITHM_VERSION,
        "query": str(query or "").strip(),
        "desired_duration_sec": round(desired, 6),
        "candidates": candidates,
        "excluded": excluded,
    }


def ranges_conflict(
    left: Mapping[str, Any],
    right: Mapping[str, Any],
    *,
    iou_threshold: float = 0.8,
) -> bool:
    """Whether two evidence ranges are too similar to reuse.

    Only ranges from the same library asset conflict.  Distinct non-overlapping
    moments from one long file remain usable by different outline nodes.
    """
    if str(left.get("library_asset_id") or "") != str(
        right.get("library_asset_id") or ""
    ):
        return False
    left_range = _candidate_range(left)
    right_range = _candidate_range(right)
    if left_range is None or right_range is None:
        return False
    l_start, l_end = left_range
    r_start, r_end = right_range
    intersection = max(0.0, min(l_end, r_end) - max(l_start, r_start))
    if intersection <= 0.0:
        return False
    union = max(l_end, r_end) - min(l_start, r_start)
    if union <= 0.0:
        return True
    return intersection / union + 1e-12 >= float(iou_threshold)


def _candidate(
    asset: Mapping[str, Any],
    row: Mapping[str, Any],
    *,
    library_asset_id: str,
    annotation_id: str,
    query: str,
    desired: float,
    start_sec: float,
    end_sec: float,
    decision_control: Mapping[str, Any] | None,
) -> dict[str, Any]:
    available = end_sec - start_sec
    confidence = _optional_float(row.get("confidence"))
    effective_confidence = (
        min(1.0, max(0.0, confidence)) if confidence is not None else 0.5
    )
    source = str(row.get("source") or "").strip().lower() or "unknown"
    raw_asset_score = _float_or(asset.get("score"), 0.0)
    raw_match_rank = _float_or(row.get("match_rank"), 0.0)
    components = {
        "asset_relevance": round(math.tanh(raw_asset_score) * 2.0, 6),
        "annotation_relevance": round(math.tanh(-raw_match_rank), 6),
        "confidence": round(effective_confidence * 2.0, 6),
        "duration_fit": round((desired / available) * 2.0, 6),
        "source_quality": round(_SOURCE_QUALITY.get(source, 0.0), 6),
        "matched_terms": round(
            min(len(_string_list(asset.get("matched_terms"))), 5) * 0.1, 6
        ),
    }
    score = round(sum(components.values()), 6)
    decision = (
        str(decision_control.get("decision") or "observe")
        if decision_control
        else "observe"
    )
    control_id = (
        str(decision_control.get("annotation_id") or "")
        if decision_control
        else ""
    )
    reasons: list[str] = []
    if decision == "prefer":
        reasons.append(f"explicit user preference {control_id}".strip())
    else:
        reasons.append("ordinary evidence (no explicit user preference)")
    reasons.extend(
        [
            f"confidence {effective_confidence:.2f}"
            + (" (unspecified; neutral default for scoring)" if confidence is None else ""),
            f"range {available:.3f}s fully covers {desired:.3f}s shot",
            f"search relevance component {components['asset_relevance'] + components['annotation_relevance']:.3f}",
            f"source quality {source} {components['source_quality']:.2f}",
        ]
    )
    metadata = _dict_value(row.get("metadata"))
    asset_provenance = {
        key: deepcopy(value)
        for key, value in asset.items()
        if key not in {"time_ranges", "annotations"}
    }
    return {
        # Annotation ids are the durable evidence ids; no transient compound id.
        "evidence_id": annotation_id,
        "library_asset_id": library_asset_id,
        "annotation_id": annotation_id,
        "label": str(row.get("label") or ""),
        "note": str(row.get("note") or ""),
        "tags": _string_list(row.get("tags")),
        "category": str(row.get("category") or ""),
        "source": source,
        # Preserve the observation's actual confidence.  Prefer is a separate tier.
        "confidence": confidence,
        "metadata": metadata,
        "created_at": str(row.get("created_at") or ""),
        "updated_at": str(row.get("updated_at") or ""),
        "start_sec": round(start_sec, 6),
        "end_sec": round(end_sec, 6),
        "duration_sec": round(available, 6),
        "query": query,
        "matched_terms": _string_list(asset.get("matched_terms")),
        "match_rank": round(raw_match_rank, 6),
        "decision": decision,
        "decision_control_annotation_id": control_id or None,
        "authority_tier": 1 if decision == "prefer" else 0,
        "score": score,
        "score_components": components,
        "ranking_reasons": reasons,
        "algorithm_version": ALGORITHM_VERSION,
        # Kept in the pure result for audits/debugging.  The stable fields above
        # are sufficient for a compact project representation.
        "provenance": {
            "asset": asset_provenance,
            "annotation": deepcopy(dict(row)),
            "decision_control": deepcopy(
                decision_control.get("raw_annotation")
                if decision_control
                else None
            ),
        },
    }


def _user_controls(asset: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw_rows: list[Any] = []
    for key in ("annotations", "time_ranges"):
        value = asset.get(key)
        if isinstance(value, list):
            raw_rows.extend(value)

    controls: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    asset_duration = _optional_nonnegative_float(asset.get("duration"))
    for raw_row in raw_rows:
        if not isinstance(raw_row, Mapping):
            continue
        row = dict(raw_row)
        if str(row.get("source") or "").strip().lower() != "user":
            continue
        source_range = _time_range(row, asset_duration=asset_duration)
        if source_range is None:
            continue
        evidence_meta = _dict_value(_dict_value(row.get("metadata")).get("evidence"))
        decision = str(evidence_meta.get("decision") or "observe").strip().lower()
        if decision not in {"prefer", "reject"}:
            continue
        claim_key = str(
            evidence_meta.get("claim_key") or DEFAULT_CLAIM_KEY
        ).strip()
        if claim_key != DEFAULT_CLAIM_KEY:
            continue
        annotation_id = str(row.get("annotation_id") or "").strip()
        start_sec, end_sec = source_range
        control = {
            "annotation_id": annotation_id,
            "decision": decision,
            "claim_key": claim_key,
            "start_sec": start_sec,
            "end_sec": end_sec,
            "updated_at": str(row.get("updated_at") or ""),
            "created_at": str(row.get("created_at") or ""),
            "supersedes": _string_list(evidence_meta.get("supersedes")),
            "raw_annotation": deepcopy(row),
        }
        signature = (
            annotation_id,
            decision,
            round(start_sec, 6),
            round(end_sec, 6),
            control["updated_at"],
        )
        if signature not in seen:
            controls.append(control)
            seen.add(signature)

    superseded = {
        old_id
        for control in controls
        for old_id in control.get("supersedes") or []
        if old_id
    }
    return [
        control
        for control in controls
        if not control.get("annotation_id")
        or control["annotation_id"] not in superseded
    ]


def _effective_control(
    controls: list[dict[str, Any]], midpoint: float
) -> dict[str, Any] | None:
    matching = [
        control
        for control in controls
        if float(control["start_sec"]) - 1e-9
        <= midpoint
        <= float(control["end_sec"]) + 1e-9
    ]
    if not matching:
        return None
    dated = [
        control
        for control in matching
        if control.get("updated_at") or control.get("created_at")
    ]
    if dated:
        # ISO timestamps sort chronologically.  Stable trailing keys make ties
        # deterministic without silently turning confidence into authority.
        return max(
            dated,
            key=lambda control: (
                str(control.get("updated_at") or control.get("created_at") or ""),
                str(control.get("annotation_id") or ""),
            ),
        )
    # With no chronology, fail closed on a contradictory correction set.
    matching.sort(
        key=lambda control: (
            0 if control.get("decision") == "reject" else 1,
            str(control.get("annotation_id") or ""),
        )
    )
    return matching[0]


def _time_range(
    row: Mapping[str, Any], *, asset_duration: float | None
) -> tuple[float, float] | None:
    scope = str(row.get("scope") or "").strip().lower()
    if scope and scope != "time_range":
        return None
    start = _optional_nonnegative_float(row.get("start_sec"))
    end = _optional_nonnegative_float(row.get("end_sec"))
    if start is None or end is None:
        return None
    if asset_duration is not None:
        start = min(start, asset_duration)
        end = min(end, asset_duration)
    if end <= start:
        return None
    return start, end


def _candidate_range(item: Mapping[str, Any]) -> tuple[float, float] | None:
    start = _optional_nonnegative_float(item.get("start_sec"))
    end = _optional_nonnegative_float(item.get("end_sec"))
    if start is None or end is None or end <= start:
        return None
    return start, end


def _candidate_sort_key(item: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        -int(item.get("authority_tier") or 0),
        -_float_or(item.get("score"), 0.0),
        str(item.get("library_asset_id") or ""),
        _float_or(item.get("start_sec"), 0.0),
        _float_or(item.get("end_sec"), 0.0),
        str(item.get("annotation_id") or ""),
    )


def _excluded_row(
    library_asset_id: str,
    annotation_id: str,
    reason: str,
    row: Mapping[str, Any],
    *,
    start_sec: float | None = None,
    end_sec: float | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "library_asset_id": library_asset_id,
        "annotation_id": annotation_id,
        "label": str(row.get("label") or ""),
        "reason": reason,
    }
    if start_sec is not None:
        result["start_sec"] = round(start_sec, 6)
    if end_sec is not None:
        result["end_sec"] = round(end_sec, 6)
    return result


def _dict_value(raw: Any) -> dict[str, Any]:
    if isinstance(raw, Mapping):
        return deepcopy(dict(raw))
    if not raw:
        return {}
    try:
        value = json.loads(str(raw))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return deepcopy(value) if isinstance(value, dict) else {}


def _string_list(raw: Any) -> list[str]:
    if isinstance(raw, str):
        try:
            decoded = json.loads(raw)
        except (ValueError, json.JSONDecodeError):
            return [raw] if raw else []
        raw = decoded
    if not isinstance(raw, (list, tuple)):
        return []
    return [str(value) for value in raw if str(value)]


def _positive_float(raw: Any, *, fallback: float) -> float:
    value = _optional_float(raw)
    return value if value is not None and value > 0.0 else fallback


def _optional_nonnegative_float(raw: Any) -> float | None:
    value = _optional_float(raw)
    return value if value is not None and value >= 0.0 else None


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


__all__ = [
    "ALGORITHM_VERSION",
    "DEFAULT_CLAIM_KEY",
    "evaluate_evidence_candidates",
    "ranges_conflict",
    "rank_evidence_candidates",
]
