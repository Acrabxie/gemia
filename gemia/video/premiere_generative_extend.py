"""Premiere Generative Extend edit-handle manifest."""
from __future__ import annotations
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from gemia.video.timeline_assets import cache_key_for_path, probe_media

DEFAULT_EXTENSION_REQUESTS: list[dict[str, Any]] = [
    {
        "id": "tail_reaction_video",
        "side": "tail",
        "media_type": "video",
        "duration_seconds": 1.5,
        "intent": "smooth reaction hold before transition",
    },
    {
        "id": "tail_ambient_audio",
        "side": "tail",
        "media_type": "audio",
        "duration_seconds": 2.0,
        "intent": "continue room tone under the outgoing edit",
    },
]

DEFAULT_PROVIDER_CONSTRAINTS: dict[str, Any] = {
    "provider": "premiere_generative_extend_compatible",
    "requires_cloud_ai": True,
    "non_destructive": True,
    "max_video_extension_seconds": 2.0,
    "max_audio_extension_seconds": 10.0,
    "source_media_policy": "send_only_selected_handle_context_when_provider_is_enabled",
    "unsupported_audio_policy": "fall_back_to_crossfade_or_room_tone_bed",
}

def render_premiere_generative_extend_edit_handle_manifest(
    input_paths: list[str],
    output_dir: str,
    *,
    package_id: str = "premiere_generative_extend_edit_handles",
    extension_requests: list[dict[str, Any]] | dict[str, Any] | None = None,
    provider_constraints: dict[str, Any] | None = None,
    transition_intent: str = "smooth reaction transition",
) -> str:
    """Emit non-destructive AI edit-handle requests linked to source media."""
    if not input_paths:
        raise ValueError("input_paths must contain at least one media path")
    output_root = Path(output_dir).expanduser().resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    constraints = _merge_constraints(provider_constraints)
    requests = _normalize_requests(extension_requests)
    sources = []
    for index, raw_path in enumerate(input_paths):
        source = Path(raw_path).expanduser().resolve()
        if not source.exists():
            raise FileNotFoundError(f"Input media not found: {source}")
        if not source.is_file():
            raise OSError(f"Input media is not a file: {source}")
        probe = probe_media(str(source))
        if int(probe.get("width") or 0) <= 0 or int(probe.get("height") or 0) <= 0:
            raise ValueError(f"Generative Extend handle manifests require visual media: {source}")
        sources.append({
            "clip_id": f"pge_clip_{index:02d}_{_safe_id(source.stem)}",
            "source_path": str(source),
            "asset_ref": _asset_ref(source, probe),
            "cache_key": cache_key_for_path(str(source)),
            "source_probe": probe,
        })
    handle_plans = [
        {
            "clip_id": source["clip_id"],
            "asset_ref": source["asset_ref"],
            "cache_key": source["cache_key"],
            "source_range": _source_range(source),
            "requested_handles": [
                _handle_for_request(request, source, constraints) for request in requests
            ],
        }
        for source in sources
    ]
    manifest = {
        "schema_version": 1,
        "effect": "premiere_generative_extend_edit_handle_manifest",
        "rendered_at": datetime.now(timezone.utc).isoformat(),
        "package": {
            "package_id": _safe_id(package_id),
            "clip_count": len(sources),
            "request_count": len(requests),
            "transition_intent": _clean_text(transition_intent),
        },
        "sources": sources,
        "provider_constraints": constraints,
        "edit_handle_plans": handle_plans,
        "premiere_controls": {
            "tool": "Generative Extend",
            "edit_targets": ["clip_head", "clip_tail", "transition_gap", "ambient_audio_tail"],
            "commit_behavior": "create_generated_handle_media_as_timeline_extension",
            "source_media_modified": False,
        },
        "diagnostics": [
            f"{len(sources)} real clips linked to AI edit-handle requests",
            f"{len(requests)} normalized video/audio handle request templates emitted",
        ],
        "review_hints": [
            "manifest is a provider request plan; it does not synthesize pixels locally",
            "rebuild when source cache_key changes",
            "provider upload and eligibility decisions stay explicit in provider_constraints",
        ],
    }
    manifest_path = output_root / "premiere_generative_extend_edit_handle_manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return str(manifest_path)

def _merge_constraints(raw: dict[str, Any] | None) -> dict[str, Any]:
    constraints = dict(DEFAULT_PROVIDER_CONSTRAINTS)
    if isinstance(raw, dict):
        for key, value in raw.items():
            if key not in {"source_path", "cache_key"}:
                constraints[_safe_id(str(key))] = value
    constraints["max_video_extension_seconds"] = _clamp_float(constraints.get("max_video_extension_seconds"), 0.1, 10.0, 2.0)
    constraints["max_audio_extension_seconds"] = _clamp_float(constraints.get("max_audio_extension_seconds"), 0.1, 60.0, 10.0)
    constraints["requires_cloud_ai"] = bool(constraints.get("requires_cloud_ai"))
    constraints["non_destructive"] = bool(constraints.get("non_destructive", True))
    return constraints

def _normalize_requests(raw: list[dict[str, Any]] | dict[str, Any] | None) -> list[dict[str, Any]]:
    if raw is None:
        items = DEFAULT_EXTENSION_REQUESTS
    elif isinstance(raw, dict):
        items = [raw]
    elif isinstance(raw, list):
        items = [item for item in raw if isinstance(item, dict)]
    else:
        items = []
    requests = [_normalize_request(item, index) for index, item in enumerate(items)]
    return requests or [_normalize_request(DEFAULT_EXTENSION_REQUESTS[0], 0)]

def _normalize_request(raw: dict[str, Any], index: int) -> dict[str, Any]:
    media_type = _safe_choice(str(raw.get("media_type") or raw.get("type") or "video"), {"video", "audio"}, "video")
    side = _safe_choice(str(raw.get("side") or raw.get("edge") or "tail"), {"head", "tail"}, "tail")
    default_duration = 1.5 if media_type == "video" else 2.0
    return {
        "request_id": _safe_id(str(raw.get("id") or raw.get("request_id") or f"{side}_{media_type}_{index}")),
        "side": side,
        "media_type": media_type,
        "duration_seconds": _clamp_float(raw.get("duration_seconds", raw.get("duration")), 0.1, 60.0, default_duration),
        "intent": _clean_text(raw.get("intent") or raw.get("description") or f"extend {media_type} at clip {side}"),
    }

def _handle_for_request(request: dict[str, Any], source: dict[str, Any], constraints: dict[str, Any]) -> dict[str, Any]:
    probe = source["source_probe"]
    duration = float(probe.get("duration") or 0.0)
    fps = max(float(probe.get("fps") or 24.0), 1.0)
    requested = float(request["duration_seconds"])
    if request["media_type"] == "video":
        allowed = min(requested, float(constraints["max_video_extension_seconds"]))
        eligible = int(probe.get("width") or 0) > 0 and int(probe.get("height") or 0) > 0
        generated_units = {"frames": max(1, int(round(allowed * fps))), "fps": round(fps, 3)}
    else:
        allowed = min(requested, float(constraints["max_audio_extension_seconds"]))
        eligible = bool(probe.get("has_audio"))
        generated_units = {"samples_proxy": int(round(allowed * 48000)), "sample_rate": 48000}
    anchor = 0.0 if request["side"] == "head" else duration
    context_start = max(0.0, anchor - 2.0) if request["side"] == "tail" else 0.0
    context_end = min(duration, anchor + 2.0) if request["side"] == "head" else duration
    return {
        **request,
        "requested_duration_seconds": requested,
        "planned_duration_seconds": round(allowed, 3),
        "eligible": eligible,
        "ineligible_reason": None if eligible else "source has no matching media stream",
        "anchor_seconds": round(anchor, 3),
        "provider_context_range": {
            "start_seconds": round(context_start, 3),
            "end_seconds": round(context_end, 3),
            "uploads_full_source": False,
        },
        "generated_units": generated_units,
        "timeline_result": {
            "operation": "prepend_generated_handle" if request["side"] == "head" else "append_generated_handle",
            "source_media_modified": False,
            "requires_user_review": bool(constraints.get("requires_cloud_ai")),
        },
    }

def _source_range(source: dict[str, Any]) -> dict[str, Any]:
    probe = source["source_probe"]
    duration = round(float(probe.get("duration") or 0.0), 3)
    fps = max(float(probe.get("fps") or 24.0), 1.0)
    return {
        "start_seconds": 0.0,
        "end_seconds": duration,
        "estimated_frames": max(1, int(round(duration * fps))) if duration else 1,
        "has_audio": bool(probe.get("has_audio")),
    }

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

def _safe_choice(value: str, choices: set[str], default: str) -> str:
    candidate = _safe_id(value)
    return candidate if candidate in choices else default

def _safe_id(value: str) -> str:
    return re.sub(r"[^a-z0-9_-]+", "_", str(value).strip().lower()).strip("_") or "item"

__all__ = [
    "DEFAULT_EXTENSION_REQUESTS",
    "DEFAULT_PROVIDER_CONSTRAINTS",
    "render_premiere_generative_extend_edit_handle_manifest",
]
