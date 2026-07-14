"""Local semantic media indexing and search for real-video review assets."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from gemia.video.intellisearch_features import (
    _dialog_labels_for,
    _label_candidates_from_text,
    _phrase_labels,
    _probe_video,
    _terms_from_text,
    _time_ranges,
    _visual_labels,
)

_REAL_BACKENDS = {"local_real_video", "pexels", "pixabay", "phone_real_video"}


@dataclass(frozen=True)
class IntelliSearchIndexResult:
    index_path: str
    clip_count: int
    label_count: int


@dataclass(frozen=True)
class IntelliSearchQueryResult:
    query: str
    index_path: str
    output_path: str | None
    match_count: int
    matches: list[dict[str, Any]]


def index_real_media(
    media_paths: list[str] | tuple[str, ...],
    output_path: str,
    *,
    review_report_paths: list[str] | tuple[str, ...] | None = None,
    stock_catalog_path: str | None = None,
    extra_labels: list[str] | tuple[str, ...] | None = None,
    max_samples: int = 6,
) -> IntelliSearchIndexResult:
    """Index real media with searchable labels from files, stock catalog, and review reports."""
    catalog = _read_json_list(stock_catalog_path)
    reviews = _read_review_reports(review_report_paths or [])
    clips = []
    label_total = 0
    for raw_path in media_paths:
        path = Path(raw_path).expanduser().resolve()
        stock_evidence = _match_stock_catalog(path, catalog)
        review_evidence = _match_review_report(path, reviews)
        clip = _build_clip_record(
            path,
            stock_evidence=stock_evidence,
            review_evidence=review_evidence,
            extra_labels=list(extra_labels or []),
            max_samples=max(int(max_samples), 1),
        )
        label_total += len(clip["semantic_labels"])
        clips.append(clip)

    payload = {
        "schema_version": 1,
        "index_kind": "resolve21_ai_intellisearch",
        "indexed_at": datetime.now(timezone.utc).isoformat(),
        "source_count": len(clips),
        "stock_catalog_path": _resolve_optional(stock_catalog_path),
        "review_report_paths": [_resolve_optional(path) for path in review_report_paths or []],
        "clips": clips,
    }
    resolved_output = Path(output_path).expanduser().resolve()
    resolved_output.parent.mkdir(parents=True, exist_ok=True)
    resolved_output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return IntelliSearchIndexResult(
        index_path=str(resolved_output),
        clip_count=len(clips),
        label_count=label_total,
    )


def search_media_index(
    index_path: str,
    query: str,
    *,
    output_path: str | None = None,
    limit: int = 5,
) -> IntelliSearchQueryResult:
    """Search a Gemia IntelliSearch media index and return ranked clip/time-range matches."""
    index = _read_json_file(index_path)
    query_terms = _terms_from_text(query)
    query_text = " ".join(query_terms)
    matches: list[dict[str, Any]] = []
    for clip in index.get("clips", []) if isinstance(index, dict) else []:
        if not isinstance(clip, dict):
            continue
        scored = _score_clip(clip, query_terms=query_terms, query_text=query_text)
        if scored["score"] <= 0:
            continue
        matches.append(scored)
    matches.sort(key=lambda item: (-float(item["score"]), item["path"]))
    matches = matches[: max(int(limit), 1)]

    resolved_output = _resolve_optional(output_path)
    payload = {
        "schema_version": 1,
        "query_kind": "resolve21_ai_intellisearch",
        "queried_at": datetime.now(timezone.utc).isoformat(),
        "index_path": str(Path(index_path).expanduser().resolve()),
        "query": query,
        "query_terms": query_terms,
        "match_count": len(matches),
        "matches": matches,
    }
    if resolved_output:
        path = Path(resolved_output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    return IntelliSearchQueryResult(
        query=query,
        index_path=str(Path(index_path).expanduser().resolve()),
        output_path=resolved_output,
        match_count=len(matches),
        matches=matches,
    )


def _build_clip_record(
    path: Path,
    *,
    stock_evidence: dict[str, Any],
    review_evidence: dict[str, Any],
    extra_labels: list[str],
    max_samples: int,
) -> dict[str, Any]:
    probe = _probe_video(path, max_samples=max_samples)
    label_sources: dict[str, list[str]] = {
        "filename": _label_candidates_from_text(path.stem),
        "visual": _visual_labels(probe),
        "dialog": _dialog_labels_for(path),
        "extra": _label_candidates_from_text(" ".join(extra_labels)),
    }
    if stock_evidence:
        label_sources["stock_catalog"] = _stock_labels(stock_evidence)
    if review_evidence:
        label_sources["review_report"] = _review_labels(review_evidence)

    labels = sorted({label for values in label_sources.values() for label in values if label})
    phrases = sorted({phrase for values in label_sources.values() for phrase in _phrase_labels(values)})
    searchable_text = " ".join(
        [
            path.name,
            str(stock_evidence.get("prompt", "")),
            str(stock_evidence.get("id", "")),
            " ".join(labels),
            " ".join(phrases),
        ]
    )
    return {
        "path": str(path),
        "exists": path.exists(),
        "readable": bool(probe.get("readable")),
        "semantic_labels": labels,
        "semantic_phrases": phrases,
        "label_sources": label_sources,
        "search_terms": sorted(set(_terms_from_text(searchable_text))),
        "time_ranges": _time_ranges(probe),
        "probe": probe,
        "stock_catalog_evidence": _public_stock_evidence(stock_evidence),
        "review_evidence": _public_review_evidence(review_evidence),
    }


def _stock_labels(item: dict[str, Any]) -> list[str]:
    labels = []
    labels.extend(_label_candidates_from_text(str(item.get("id", ""))))
    labels.extend(_label_candidates_from_text(str(item.get("prompt", ""))))
    source = item.get("source")
    if source:
        labels.extend(_label_candidates_from_text(Path(str(source)).expanduser().stem))
    for value in (item.get("kind"), item.get("backend"), item.get("status")):
        if value:
            labels.extend(_label_candidates_from_text(str(value)))
    backend = str(item.get("backend", ""))
    if backend in _REAL_BACKENDS:
        labels.append("real_footage")
    return labels


def _review_labels(report: dict[str, Any]) -> list[str]:
    labels = []
    for finding in report.get("quality_findings", []) or []:
        if isinstance(finding, dict):
            labels.extend(_label_candidates_from_text(str(finding.get("code", ""))))
    render_context = report.get("render_context", {}) if isinstance(report.get("render_context"), dict) else {}
    backend = render_context.get("render_backend", {})
    if isinstance(backend, dict):
        labels.extend(_label_candidates_from_text(str(backend.get("selected", ""))))
    labels.extend(_label_candidates_from_text(str(render_context.get("authoring_mode", ""))))
    real_source = report.get("real_source", {}) if isinstance(report.get("real_source"), dict) else {}
    if real_source.get("confirmed"):
        labels.append("real_source_confirmed")
    return labels


def _score_clip(clip: dict[str, Any], *, query_terms: list[str], query_text: str) -> dict[str, Any]:
    labels = set(str(label) for label in clip.get("semantic_labels", []) or [])
    phrases = set(str(label) for label in clip.get("semantic_phrases", []) or [])
    terms = set(str(term) for term in clip.get("search_terms", []) or [])
    matched_terms = [term for term in query_terms if term in terms or term in labels]
    phrase_key = "_".join(query_terms)
    matched_phrases = []
    if phrase_key in phrases or phrase_key in labels:
        matched_phrases.append(phrase_key)
    if query_text and query_text in " ".join(sorted(terms | labels | phrases)).replace("_", " "):
        matched_phrases.append(query_text.replace(" ", "_"))
    score = len(set(matched_terms)) * 3 + len(set(matched_phrases)) * 5
    return {
        "path": str(clip.get("path", "")),
        "score": score,
        "matched_terms": sorted(set(matched_terms)),
        "matched_phrases": sorted(set(matched_phrases)),
        "semantic_labels": clip.get("semantic_labels", []),
        "time_ranges": clip.get("time_ranges", []),
        "stock_catalog_evidence": clip.get("stock_catalog_evidence", {}),
        "probe": {
            "width": clip.get("probe", {}).get("width"),
            "height": clip.get("probe", {}).get("height"),
            "frame_count": clip.get("probe", {}).get("frame_count"),
            "duration_seconds": clip.get("probe", {}).get("duration_seconds"),
        },
    }


def _match_stock_catalog(path: Path, catalog: list[Any]) -> dict[str, Any]:
    for item in catalog:
        if not isinstance(item, dict):
            continue
        candidates = [item.get("source"), *list(item.get("outputs", []) or [])]
        for candidate in candidates:
            if not candidate:
                continue
            if Path(str(candidate)).expanduser().resolve() == path:
                return dict(item)
    return {}


def _match_review_report(path: Path, reports: list[dict[str, Any]]) -> dict[str, Any]:
    for report in reports:
        for key in ("source", "output"):
            section = report.get(key)
            if not isinstance(section, dict):
                continue
            raw_path = section.get("path")
            if raw_path and Path(str(raw_path)).expanduser().resolve() == path:
                return report
    return {}


def _read_review_reports(paths: list[str] | tuple[str, ...]) -> list[dict[str, Any]]:
    reports = []
    for path in paths:
        payload = _read_json_file(path)
        if payload:
            reports.append(payload)
    return reports


def _read_json_file(path: str | Path | None) -> dict[str, Any]:
    if not path:
        return {}
    try:
        payload = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _read_json_list(path: str | Path | None) -> list[Any]:
    if not path:
        return []
    try:
        payload = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return payload if isinstance(payload, list) else []


def _public_stock_evidence(item: dict[str, Any]) -> dict[str, Any]:
    if not item:
        return {}
    return {
        "id": item.get("id"),
        "kind": item.get("kind"),
        "backend": item.get("backend"),
        "status": item.get("status"),
        "prompt": item.get("prompt"),
        "source": item.get("source"),
    }


def _public_review_evidence(report: dict[str, Any]) -> dict[str, Any]:
    if not report:
        return {}
    return {
        "status": report.get("status"),
        "review_kind": report.get("review_kind"),
        "real_source": report.get("real_source", {}),
    }


def _resolve_optional(path: str | Path | None) -> str | None:
    if path is None:
        return None
    return str(Path(path).expanduser().resolve())


__all__ = [
    "IntelliSearchIndexResult",
    "IntelliSearchQueryResult",
    "index_real_media",
    "search_media_index",
]
