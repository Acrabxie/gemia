"""Executable acceptance gates for a real Lumeri production delivery.

The renderer receipt proves what bytes were rendered.  This module proves that
those bytes and the project behind them satisfy the production contract.  It
does not mark a run ``accepted``: human approval remains an explicit review
action owned by :class:`gemia.production_store.ProductionStore`.

All checks are deterministic and side-effect free so the resulting report can
be stored as revision-bound ``Evidence`` and replayed after a restart.
"""

from __future__ import annotations

import math
import statistics
from pathlib import Path
from typing import Any, Iterable, Mapping

from gemia.project_model import normalize_project
from gemia.reality_contract import (
    MAX_MEDIA_BUDGET_USD,
    normalize_reality_contract,
)
from gemia.render_receipt import CANONICAL_RENDER_SEMANTICS_VERSION

FORMAL_REQUIRED_REVIEW_CHECKS = (
    "black_frames",
    "watermarks",
    "text_integrity",
    "character_continuity",
    "real_motion",
)


def _number(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return float(default)
    return result if math.isfinite(result) else float(default)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _project_assets(project: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    return {
        str(item.get("id") or item.get("asset_id") or ""): item
        for item in (project.get("assets") or [])
        if isinstance(item, Mapping)
    }


def _registry_assets(
    asset_records: Iterable[Mapping[str, Any]] | Mapping[str, Any] | None,
) -> dict[str, Mapping[str, Any]]:
    if isinstance(asset_records, Mapping):
        raw = asset_records.get("records", asset_records)
        if isinstance(raw, Mapping):
            return {
                str(key): value
                for key, value in raw.items()
                if isinstance(value, Mapping)
            }
        asset_records = raw if isinstance(raw, list) else []
    return {
        str(item.get("asset_id") or item.get("id") or ""): item
        for item in (asset_records or [])
        if isinstance(item, Mapping)
    }


def _video_track_ids(project: Mapping[str, Any]) -> set[str]:
    timeline = _mapping(project.get("timeline"))
    return {
        str(track.get("id") or "")
        for track in (timeline.get("tracks") or [])
        if isinstance(track, Mapping) and str(track.get("kind") or "") == "video"
    }


def _visual_clips(project: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    timeline = _mapping(project.get("timeline"))
    video_tracks = _video_track_ids(project)
    clips = [
        clip
        for clip in (timeline.get("clips") or [])
        if isinstance(clip, Mapping)
        and bool(clip.get("enabled", True))
        and str(clip.get("track_id") or "") in video_tracks
        and str(clip.get("media_kind") or "") in {"video", "image"}
    ]
    return sorted(
        clips, key=lambda clip: (_number(clip.get("start")), str(clip.get("id")))
    )


def _interval_union_seconds(intervals: Iterable[tuple[float, float]]) -> float:
    ordered = sorted((max(0.0, a), max(0.0, b)) for a, b in intervals if b > a)
    if not ordered:
        return 0.0
    total = 0.0
    start, end = ordered[0]
    for next_start, next_end in ordered[1:]:
        if next_start <= end:
            end = max(end, next_end)
        else:
            total += end - start
            start, end = next_start, next_end
    return total + end - start


def _source_kind(
    asset_id: str,
    project_assets: Mapping[str, Mapping[str, Any]],
    registry_assets: Mapping[str, Mapping[str, Any]],
) -> str:
    project_asset = _mapping(project_assets.get(asset_id))
    registry_asset = _mapping(registry_assets.get(asset_id))
    project_meta = _mapping(project_asset.get("metadata"))
    source = _mapping(registry_asset.get("source")) or _mapping(
        project_meta.get("source")
    )
    return str(source.get("kind") or source.get("type") or "").strip().lower()


def _motion_verified(
    asset_id: str,
    project_assets: Mapping[str, Mapping[str, Any]],
    registry_assets: Mapping[str, Mapping[str, Any]],
    motion_evidence: Mapping[str, Any] | None = None,
) -> bool:
    project_asset = _mapping(project_assets.get(asset_id))
    registry_asset = _mapping(registry_assets.get(asset_id))
    project_meta = _mapping(project_asset.get("metadata"))
    source = _mapping(registry_asset.get("source")) or _mapping(
        project_meta.get("source")
    )
    kind = str(source.get("kind") or source.get("type") or "").strip().lower()
    evidence = _mapping(_mapping(motion_evidence).get(asset_id))
    verified = bool(source.get("real_motion_verified")) or bool(
        evidence.get("real_motion_verified")
    )
    return verified and kind in {
        "stock",
        "public_stock",
        "generated_video",
        "owned_video",
        "camera_original",
    }


def _license_complete(record: Mapping[str, Any]) -> bool:
    license_data = _mapping(record.get("license"))
    source = _mapping(record.get("source"))
    if str(source.get("kind") or "").lower() in {"derived", "local_mg", "render"}:
        return bool(record.get("lineage"))
    return bool(
        (
            license_data.get("url")
            or license_data.get("name")
            or license_data.get("basis")
        )
        and (source.get("url") or source.get("provider") or source.get("receipt_id"))
    )


def _audio_roles(
    project: Mapping[str, Any], registry_assets: Mapping[str, Mapping[str, Any]]
) -> set[str]:
    timeline = _mapping(project.get("timeline"))
    project_assets = _project_assets(project)
    roles: set[str] = set()
    for clip in timeline.get("clips") or []:
        if not isinstance(clip, Mapping) or not bool(clip.get("enabled", True)):
            continue
        if str(clip.get("media_kind") or "") != "audio":
            continue
        asset_id = str(clip.get("asset_id") or "")
        project_asset = _mapping(project_assets.get(asset_id))
        registry_asset = _mapping(registry_assets.get(asset_id))
        source = _mapping(registry_asset.get("source"))
        metadata = _mapping(project_asset.get("metadata"))
        role = str(source.get("role") or metadata.get("role") or "").strip().lower()
        if role:
            roles.add(role)
    return roles


def _path_is_tmp(value: Any) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    try:
        resolved = str(Path(text).expanduser().resolve(strict=False))
    except (OSError, RuntimeError):
        resolved = text
    return (
        resolved == "/tmp"
        or resolved.startswith("/tmp/")
        or resolved == "/private/tmp"
        or resolved.startswith("/private/tmp/")
    )


def _check(
    checks: list[dict[str, Any]],
    blockers: list[dict[str, Any]],
    code: str,
    ok: bool,
    *,
    actual: Any = None,
    expected: Any = None,
    detail: str = "",
) -> None:
    row = {
        "code": code,
        "ok": bool(ok),
        "actual": actual,
        "expected": expected,
    }
    if detail:
        row["detail"] = detail
    checks.append(row)
    if not ok:
        blockers.append({key: value for key, value in row.items() if key != "ok"})


def evaluate_delivery(
    *,
    project: Mapping[str, Any],
    render_receipt: Mapping[str, Any],
    asset_records: Iterable[Mapping[str, Any]] | Mapping[str, Any] | None,
    budget_snapshot: Mapping[str, Any],
    evidence: Mapping[str, Any] | None = None,
    preview_receipt: Mapping[str, Any] | None = None,
    reality_contract: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Evaluate the bytes against this run's persisted RealityContract.

    The returned report is suitable for ``ProductionStore.add_evidence``.  A
    caller may transition to ``ready_for_review`` only when
    ``ready_for_review`` is true.  Human creative approval is intentionally not
    represented as a machine check here.
    """

    normalized = normalize_project(dict(project))
    receipt = dict(render_receipt or {})
    probe = _mapping(receipt.get("probe"))
    audio_analysis = _mapping(receipt.get("audio_analysis"))
    evidence_data = _mapping(evidence)
    motion_evidence = _mapping(evidence_data.get("asset_motion"))
    registry = _registry_assets(asset_records)
    project_asset_map = _project_assets(normalized)
    visual = _visual_clips(normalized)
    checks: list[dict[str, Any]] = []
    blockers: list[dict[str, Any]] = []
    contract = normalize_reality_contract(
        reality_contract,
        hard_cap_usd=MAX_MEDIA_BUDGET_USD,
    )
    deliverable = _mapping(contract.get("deliverable"))
    audio_contract = _mapping(deliverable.get("audio"))
    acceptance = _mapping(contract.get("acceptance"))

    receipt_semantics = int(_number(receipt.get("render_semantics_version"), -1.0))
    _check(
        checks,
        blockers,
        "current_export_render_semantics",
        receipt_semantics == CANONICAL_RENDER_SEMANTICS_VERSION,
        actual=receipt_semantics,
        expected=CANONICAL_RENDER_SEMANTICS_VERSION,
    )

    duration = _number(probe.get("container_duration"))
    target_duration_raw = deliverable.get("duration_sec")
    target_duration = (
        _number(target_duration_raw) if target_duration_raw is not None else None
    )
    duration_tolerance = max(
        0.0, _number(deliverable.get("duration_tolerance_sec"), 0.5)
    )
    _check(
        checks,
        blockers,
        "contract_duration",
        target_duration is not None
        and target_duration > 0
        and abs(duration - target_duration) <= duration_tolerance,
        actual=duration,
        expected=(
            f"{target_duration} +/- {duration_tolerance}s"
            if target_duration is not None
            else "bound RealityContract duration"
        ),
    )
    _check(
        checks,
        blockers,
        "contract_width",
        int(probe.get("width") or 0) == int(deliverable.get("width") or 0),
        actual=probe.get("width"),
        expected=deliverable.get("width"),
    )
    _check(
        checks,
        blockers,
        "contract_height",
        int(probe.get("height") or 0) == int(deliverable.get("height") or 0),
        actual=probe.get("height"),
        expected=deliverable.get("height"),
    )
    fps = _number(probe.get("fps"))
    expected_fps = _number(deliverable.get("fps"))
    _check(
        checks,
        blockers,
        "contract_fps",
        expected_fps > 0 and abs(fps - expected_fps) <= 0.05,
        actual=fps,
        expected=expected_fps,
    )
    _check(
        checks,
        blockers,
        "contract_video_codec",
        str(probe.get("video_codec") or "")
        == str(deliverable.get("video_codec") or ""),
        actual=probe.get("video_codec"),
        expected=deliverable.get("video_codec"),
    )
    pixel_format = probe.get("video_pixel_format", probe.get("pixel_format"))
    _check(
        checks,
        blockers,
        "contract_pixel_format",
        str(pixel_format or "") == str(deliverable.get("pixel_format") or ""),
        actual=pixel_format,
        expected=deliverable.get("pixel_format"),
    )
    audio_required = bool(audio_contract.get("required", True))
    sample_rate = probe.get("audio_sample_rate", probe.get("sample_rate"))
    channels = probe.get("audio_channels", probe.get("channels"))
    if audio_required:
        _check(
            checks,
            blockers,
            "contract_audio_codec",
            str(probe.get("audio_codec") or "")
            == str(audio_contract.get("codec") or ""),
            actual=probe.get("audio_codec"),
            expected=audio_contract.get("codec"),
        )
        _check(
            checks,
            blockers,
            "contract_audio_sample_rate",
            int(sample_rate or 0) == int(audio_contract.get("sample_rate") or 0),
            actual=sample_rate,
            expected=audio_contract.get("sample_rate"),
        )
        _check(
            checks,
            blockers,
            "contract_audio_channels",
            int(channels or 0) == int(audio_contract.get("channels") or 0),
            actual=channels,
            expected=audio_contract.get("channels"),
        )
    video_duration = _number(probe.get("video_duration"))
    audio_duration = _number(probe.get("audio_duration"))
    if audio_required:
        stream_tolerance = max(0.25, duration_tolerance)
        _check(
            checks,
            blockers,
            "video_duration_matches_delivery",
            video_duration > 0.0 and abs(video_duration - duration) <= stream_tolerance,
            actual=video_duration,
            expected=f"{duration} +/- {stream_tolerance}s",
        )
        _check(
            checks,
            blockers,
            "audio_duration_matches_picture",
            audio_duration > 0.0
            and video_duration > 0.0
            and abs(audio_duration - video_duration) <= stream_tolerance,
            actual=audio_duration,
            expected=f"video stream {video_duration} +/- {stream_tolerance}s",
        )
    _check(
        checks,
        blockers,
        "full_decode",
        not bool(acceptance.get("full_decode_required", True))
        or str(_mapping(receipt.get("decode_check")).get("status")) == "passed",
        actual=_mapping(receipt.get("decode_check")).get("status"),
        expected="passed",
    )
    dropped = (
        receipt.get("dropped_fields")
        if isinstance(receipt.get("dropped_fields"), list)
        else []
    )
    _check(
        checks,
        blockers,
        "no_dropped_fields",
        bool(acceptance.get("dropped_fields_allowed", False)) or not dropped,
        actual=len(dropped),
        expected=0,
    )

    integrated_lufs = _number(
        audio_analysis.get(
            "integrated_loudness_lufs", audio_analysis.get("integrated_lufs")
        ),
        float("nan"),
    )
    true_peak = _number(audio_analysis.get("true_peak_dbtp"), float("nan"))
    if audio_required:
        target_lufs = _number(audio_contract.get("integrated_loudness_lufs"), -16.0)
        lufs_tolerance = max(
            0.0, _number(audio_contract.get("loudness_tolerance_lu"), 1.0)
        )
        peak_limit = _number(audio_contract.get("true_peak_max_dbtp"), -1.0)
        _check(
            checks,
            blockers,
            "integrated_loudness",
            math.isfinite(integrated_lufs)
            and abs(integrated_lufs - target_lufs) <= lufs_tolerance,
            actual=integrated_lufs if math.isfinite(integrated_lufs) else None,
            expected=f"{target_lufs} +/- {lufs_tolerance} LUFS",
        )
        _check(
            checks,
            blockers,
            "true_peak",
            math.isfinite(true_peak) and true_peak <= peak_limit,
            actual=true_peak if math.isfinite(true_peak) else None,
            expected=f"<= {peak_limit} dBTP",
        )

    unit_durations = [_number(clip.get("duration")) for clip in visual]
    unit_count = len(unit_durations)
    median_duration = statistics.median(unit_durations) if unit_durations else 0.0
    edit_units = _mapping(acceptance.get("edit_units"))
    unit_min = edit_units.get("min")
    unit_max = edit_units.get("max")
    if unit_min is not None or unit_max is not None:
        min_ok = unit_min is None or unit_count >= int(unit_min)
        max_ok = unit_max is None or unit_count <= int(unit_max)
        _check(
            checks,
            blockers,
            "edit_unit_count",
            min_ok and max_ok,
            actual=unit_count,
            expected={"min": unit_min, "max": unit_max},
        )
    median_limit = acceptance.get("median_shot_duration_max_sec")
    if median_limit is not None:
        _check(
            checks,
            blockers,
            "median_shot_duration",
            bool(unit_durations) and median_duration <= float(median_limit),
            actual=round(median_duration, 6),
            expected=f"<= {float(median_limit)}s",
        )

    verified_motion_intervals: list[tuple[float, float]] = []
    public_motion_assets: set[str] = set()
    static_violations: list[dict[str, Any]] = []
    for clip in visual:
        start = _number(clip.get("start"))
        clip_duration = _number(clip.get("duration"))
        asset_id = str(clip.get("asset_id") or "")
        if str(clip.get("media_kind") or "") == "video" and _motion_verified(
            asset_id, project_asset_map, registry, motion_evidence
        ):
            verified_motion_intervals.append((start, start + clip_duration))
            if _source_kind(asset_id, project_asset_map, registry) in {
                "stock",
                "public_stock",
            }:
                public_motion_assets.add(asset_id)
        elif str(clip.get("media_kind") or "") == "image":
            intentional = bool(
                _mapping(clip.get("provenance")).get("intentional_pause")
            )
            static_limit = acceptance.get("static_shot_max_sec")
            if (
                static_limit is not None
                and clip_duration > float(static_limit) + 1e-6
                and not intentional
            ):
                static_violations.append(
                    {"clip_id": clip.get("id"), "duration": clip_duration}
                )
    motion_seconds = _interval_union_seconds(verified_motion_intervals)
    motion_min = _number(acceptance.get("verified_motion_min_sec"), 0.0)
    if motion_min > 0:
        _check(
            checks,
            blockers,
            "verified_motion_coverage",
            motion_seconds >= motion_min,
            actual=round(motion_seconds, 6),
            expected=f">= {motion_min}s",
        )
    public_min = int(acceptance.get("licensed_public_motion_assets_min") or 0)
    if public_min > 0:
        _check(
            checks,
            blockers,
            "public_motion_asset_count",
            len(public_motion_assets) >= public_min,
            actual=len(public_motion_assets),
            expected=f">= {public_min}",
        )
    if acceptance.get("static_shot_max_sec") is not None:
        _check(
            checks,
            blockers,
            "static_shot_limit",
            not static_violations,
            actual=static_violations,
            expected=(
                "non-intentional static shot <= "
                f"{float(acceptance['static_shot_max_sec'])}s"
            ),
        )

    referenced_ids = {
        str(clip.get("asset_id") or "")
        for clip in _mapping(normalized.get("timeline")).get("clips") or []
        if isinstance(clip, Mapping)
        and bool(clip.get("enabled", True))
        and clip.get("asset_id")
    }
    missing_provenance: list[str] = []
    tmp_references: list[str] = []
    for asset_id in sorted(referenced_ids):
        record = _mapping(registry.get(asset_id))
        if (
            not record
            or not str(record.get("sha256") or "")
            or not _license_complete(record)
        ):
            missing_provenance.append(asset_id)
        path = record.get("path") or _mapping(project_asset_map.get(asset_id)).get(
            "source_path"
        )
        if _path_is_tmp(path):
            tmp_references.append(asset_id)
    _check(
        checks,
        blockers,
        "asset_provenance_complete",
        not bool(acceptance.get("provenance_complete_required", True))
        or not missing_provenance,
        actual=missing_provenance,
        expected="100%",
    )
    _check(
        checks,
        blockers,
        "no_tmp_references",
        not bool(acceptance.get("forbid_temporary_references", True))
        or not tmp_references,
        actual=tmp_references,
        expected=[],
    )

    roles = _audio_roles(normalized, registry)
    required_roles = set(audio_contract.get("required_roles") or [])
    if audio_required and required_roles:
        _check(
            checks,
            blockers,
            "audio_roles_complete",
            required_roles.issubset(roles),
            actual=sorted(roles),
            expected=sorted(required_roles),
        )

    committed = _number(budget_snapshot.get("committed_usd"))
    _check(
        checks,
        blockers,
        "media_budget",
        committed <= _number(_mapping(contract.get("budget")).get("hard_cap_usd")) + 1e-9,
        actual=committed,
        expected=f"<= ${_number(_mapping(contract.get('budget')).get('hard_cap_usd'))}",
    )
    duplicate_billing = int(budget_snapshot.get("duplicate_billing_count") or 0)
    _check(
        checks,
        blockers,
        "duplicate_billing",
        duplicate_billing == 0,
        actual=duplicate_billing,
        expected=0,
    )

    if preview_receipt is not None:
        preview = _mapping(preview_receipt)
        preview_semantics = int(_number(preview.get("render_semantics_version"), -1.0))
        _check(
            checks,
            blockers,
            "current_preview_render_semantics",
            preview_semantics == CANONICAL_RENDER_SEMANTICS_VERSION,
            actual=preview_semantics,
            expected=CANONICAL_RENDER_SEMANTICS_VERSION,
        )
        parity_ok = (
            str(preview.get("graph_hash") or "")
            and str(preview.get("graph_hash")) == str(receipt.get("graph_hash"))
            and int(preview.get("project_revision") or -1)
            == int(receipt.get("project_revision") or -2)
        )
        _check(
            checks,
            blockers,
            "preview_export_graph_parity",
            bool(parity_ok),
            actual={
                "preview_graph_hash": preview.get("graph_hash"),
                "export_graph_hash": receipt.get("graph_hash"),
                "preview_revision": preview.get("project_revision"),
                "export_revision": receipt.get("project_revision"),
            },
            expected="same graph_hash and revision",
        )
    else:
        _check(
            checks,
            blockers,
            "preview_export_graph_parity",
            False,
            actual=None,
            expected="same graph_hash and revision",
        )

    review_results = _mapping(evidence_data.get("review_checks"))
    review_names = tuple(
        str(name) for name in (acceptance.get("agent_review_checks") or []) if str(name)
    )
    for name in review_names:
        value = review_results.get(name)
        passed = value is True or (
            isinstance(value, Mapping) and str(value.get("status") or "") == "passed"
        )
        _check(
            checks, blockers, f"review_{name}", passed, actual=value, expected="passed"
        )

    return {
        "schema": "lumeri.production-acceptance",
        "version": 1,
        "project_revision": int(receipt.get("project_revision") or 0),
        "render_id": str(receipt.get("render_id") or ""),
        "graph_hash": str(receipt.get("graph_hash") or ""),
        "ready_for_review": not blockers,
        "checks": checks,
        "blockers": blockers,
        "human_review_required": True,
        "human_review_dimensions": [
            str(value)
            for value in (acceptance.get("creative_dimensions") or [])
            if str(value)
        ],
    }


__all__ = ["FORMAL_REQUIRED_REVIEW_CHECKS", "evaluate_delivery"]
