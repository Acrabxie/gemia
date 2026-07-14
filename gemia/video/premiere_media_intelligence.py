"Premiere media-intelligence visual marker search manifest."
from __future__ import annotations
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from gemia.video.timeline_assets import cache_key_for_path, probe_media

DEFAULT_SEARCH_QUERIES = ["wide shot", "sync audio", "review marker"]

def render_premiere_media_intelligence_visual_marker_search_manifest(
    input_paths: list[str],
    output_dir: str,
    *,
    package_id: str = "premiere_media_intelligence_visual_marker_search",
    search_queries: list[str] | str | None = None,
    markers: dict[str, list[dict[str, Any]]] | list[dict[str, Any]] | None = None,
    transcripts: dict[str, str] | list[str] | None = None,
    metadata_facets: dict[str, Any] | None = None,
) -> str:
    """Emit a local Premiere-style search index and result-range manifest."""
    if not input_paths:
        raise ValueError("input_paths must contain at least one media path")
    output_root = Path(output_dir).expanduser().resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    queries = _normalize_queries(search_queries)
    assets = []
    for index, raw_path in enumerate(input_paths):
        source = Path(raw_path).expanduser().resolve()
        if not source.exists():
            raise FileNotFoundError(f"Input media not found: {source}")
        if not source.is_file():
            raise OSError(f"Input media is not a file: {source}")
        probe = probe_media(str(source))
        if int(probe.get("width") or 0) <= 0 or int(probe.get("height") or 0) <= 0:
            raise ValueError(f"Premiere media intelligence requires visual media: {source}")
        asset_id = f"pmi_asset_{index:02d}_{_safe_id(source.stem)}"
        transcript = _transcript_for(transcripts, source, index, probe)
        asset_markers = _markers_for(markers, source, index, probe)
        facets = _facet_index(source, probe, transcript, asset_markers, metadata_facets)
        assets.append({
            "asset_id": asset_id,
            "source_path": str(source),
            "asset_ref": _asset_ref(source, probe),
            "cache_key": cache_key_for_path(str(source)),
            "source_probe": probe,
            "local_index": {
                "visual": facets["visual"],
                "transcript": facets["transcript"],
                "markers": asset_markers,
                "metadata": facets["metadata"],
            },
        })
    results = [_search_asset(asset, query) for asset in assets for query in queries]
    results = [item for item in results if item["score"] > 0]
    manifest = {
        "schema_version": 1,
        "effect": "premiere_media_intelligence_visual_marker_search_manifest",
        "rendered_at": datetime.now(timezone.utc).isoformat(),
        "package": {
            "package_id": _safe_id(package_id),
            "asset_count": len(assets),
            "query_count": len(queries),
            "result_count": len(results),
        },
        "privacy": {
            "mode": "local_media_intelligence",
            "uploads_source_media": False,
            "index_material": ["visual_facets", "transcript_terms", "markers", "metadata"],
        },
        "queries": queries,
        "assets": assets,
        "search_results": sorted(results, key=lambda item: (-item["score"], item["asset_id"], item["start_seconds"])),
        "premiere_controls": {
            "panel": "Search",
            "filters": ["Visual", "Transcript", "Markers", "Metadata"],
            "range_behavior": "return_source_time_ranges_without_modifying_media",
        },
        "review_hints": [
            "search index is deterministic local metadata",
            "rebuild when source cache_key changes",
            "result ranges can become markers, selects, or timeline edit candidates",
        ],
    }
    manifest_path = output_root / "premiere_media_intelligence_visual_marker_search_manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return str(manifest_path)

def _facet_index(path: Path, probe: dict[str, Any], transcript: str, markers: list[dict[str, Any]], extra: dict[str, Any] | None) -> dict[str, Any]:
    width, height = int(probe.get("width") or 0), int(probe.get("height") or 0)
    duration = float(probe.get("duration") or 0.0)
    visual = {
        "orientation": "vertical" if height > width else "horizontal",
        "shot_size": "wide" if width >= 960 else "compact",
        "duration_bucket": "long" if duration >= 10 else "short",
        "has_sync_audio": bool(probe.get("has_audio")),
        "resolution": f"{width}x{height}",
        "codec": str(probe.get("codec") or ""),
    }
    metadata = {
        "filename": path.name,
        "stem": path.stem,
        "extension": path.suffix.lower().lstrip("."),
        "media_kind": probe.get("media_kind"),
        "marker_count": len(markers),
        "facet_source": "ffprobe_local",
    }
    if isinstance(extra, dict):
        metadata.update({str(k): v for k, v in extra.items() if k not in {"source_path", "cache_key"}})
    return {"visual": visual, "transcript": _terms(transcript), "metadata": metadata}

def _search_asset(asset: dict[str, Any], query: str) -> dict[str, Any]:
    terms = set(_terms(query))
    index = asset["local_index"]
    visual_terms = {str(v).lower() for v in index["visual"].values()}
    transcript_terms = set(index["transcript"])
    marker_hits = [m for m in index["markers"] if terms & set(m["terms"])]
    metadata_terms = set(_terms(" ".join(str(v) for v in index["metadata"].values())))
    score = 0.0
    reasons = []
    if terms & visual_terms:
        score += 0.35; reasons.append("visual")
    if terms & transcript_terms:
        score += 0.3; reasons.append("transcript")
    if marker_hits:
        score += 0.3; reasons.append("marker")
    if terms & metadata_terms:
        score += 0.15; reasons.append("metadata")
    if not score and query == "review marker" and index["markers"]:
        score = 0.2; reasons.append("marker")
    duration = float(asset["source_probe"].get("duration") or 0.0)
    marker = marker_hits[0] if marker_hits else None
    start = float(marker["time_seconds"]) if marker else 0.0
    return {
        "query": query,
        "asset_id": asset["asset_id"],
        "asset_ref": asset["asset_ref"],
        "score": round(min(score, 1.0), 3),
        "matched_facets": reasons,
        "start_seconds": round(max(0.0, min(start, max(duration - 0.1, 0.0))), 3),
        "end_seconds": round(max(min(duration, start + max(1.0, min(duration or 1.0, 4.0))), 0.1), 3),
    }

def _markers_for(raw: Any, path: Path, index: int, probe: dict[str, Any]) -> list[dict[str, Any]]:
    items = raw.get(path.name) or raw.get(path.stem) if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        items = [{"name": "review marker", "time_seconds": 0.0, "comment": "auto local review marker"}]
    duration = float(probe.get("duration") or 0.0)
    out = []
    for marker_index, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or item.get("label") or f"marker_{marker_index}")
        comment = str(item.get("comment") or item.get("note") or name)
        out.append({
            "marker_id": f"marker_{index:02d}_{marker_index:02d}_{_safe_id(name)}",
            "name": name,
            "time_seconds": _clamp_float(item.get("time_seconds", item.get("time", 0.0)), 0.0, duration, 0.0),
            "comment": comment,
            "terms": sorted(set(_terms(f"{name} {comment}"))),
        })
    return out or [{"marker_id": f"marker_{index:02d}_auto_review", "name": "review marker", "time_seconds": 0.0, "comment": "auto local review marker", "terms": ["auto", "local", "marker", "review"]}]

def _transcript_for(raw: Any, path: Path, index: int, probe: dict[str, Any]) -> str:
    if isinstance(raw, dict):
        return str(raw.get(path.name) or raw.get(path.stem) or "")
    if isinstance(raw, list) and index < len(raw):
        return str(raw[index])
    audio = "sync audio" if probe.get("has_audio") else "silent"
    return f"{path.stem} local review {audio}"

def _normalize_queries(raw: list[str] | str | None) -> list[str]:
    if isinstance(raw, str):
        raw = [raw]
    queries = [_clean_text(item) for item in (raw or DEFAULT_SEARCH_QUERIES) if _clean_text(item)]
    return queries or DEFAULT_SEARCH_QUERIES

def _terms(text: str) -> list[str]:
    return [_safe_id(part) for part in re.findall(r"[A-Za-z0-9_\u4e00-\u9fff]+", _clean_text(text).lower()) if _safe_id(part)]

def _asset_ref(path: Path, probe: dict[str, Any]) -> str:
    duration = round(float(probe.get("duration") or 0.0), 3)
    return f"{_safe_id(path.stem)}:{path.stat().st_size}:{duration}:{int(probe.get('width') or 0)}x{int(probe.get('height') or 0)}"

def _clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()

def _clamp_float(value: Any, min_val: float, max_val: float, default: float) -> float:
    try:
        number = float(value)
    except Exception:
        number = default
    return round(max(min_val, min(number, max_val)), 3)

def _safe_id(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_\-\u4e00-\u9fff]+", "_", str(value).strip()).strip("_").lower() or "item"

__all__ = ["DEFAULT_SEARCH_QUERIES", "render_premiere_media_intelligence_visual_marker_search_manifest"]
