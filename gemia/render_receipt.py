"""Canonical render identity and acceptance receipt primitives.

The receipt binds an output file to three things that used to drift apart:
the normalized project graph, the exact bytes of every referenced source, and
the render preset.  Preview and final export can therefore share one semantic
renderer while still producing different preset-specific artifacts.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from gemia.project_model import normalize_project

RENDER_RECEIPT_SCHEMA_VERSION = 1

# The graph identity must change when canonical rendering behaviour changes,
# even if the persisted timeline and source bytes do not.  Otherwise a fixed
# renderer can silently reuse the artifact id (and idempotency receipt) of an
# output produced by older semantics.  Keep the concrete mastering parameters
# here as the single source used by both the exporter and the graph receipt.
CANONICAL_RENDER_SEMANTICS_VERSION = 3
AUDIO_MASTER_TARGET_LUFS = -16.0
# AAC can introduce inter-sample overs after normalization.  This is the
# production encode target, not the acceptance threshold: the encoded master
# is still independently required to measure <= -1 dBTP.
AUDIO_MASTER_TRUE_PEAK_DBTP = -3.0
AUDIO_MASTER_LRA_LU = 11.0


def canonical_render_semantics() -> dict[str, Any]:
    return {
        "version": CANONICAL_RENDER_SEMANTICS_VERSION,
        "audio_master": {
            "normalization": "ffmpeg_loudnorm_two_pass",
            "integrated_loudness_lufs": AUDIO_MASTER_TARGET_LUFS,
            "true_peak_dbtp": AUDIO_MASTER_TRUE_PEAK_DBTP,
            "loudness_range_lu": AUDIO_MASTER_LRA_LU,
            "premaster_codec": "pcm_f32le",
            "sample_rate": 48000,
            "channels": 2,
            "delivery_codec": "aac",
            "delivery_bitrate": "192k",
            "sidechain_eof_policy": "pad_detector_with_silence_to_master_duration",
            "delivery_duration_policy": "pad_and_trim_to_canonical_master_duration",
        },
    }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while True:
            block = stream.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def _referenced_asset_ids(project: dict[str, Any]) -> set[str]:
    timeline = (
        project.get("timeline") if isinstance(project.get("timeline"), dict) else {}
    )
    result: set[str] = set()
    for clip in timeline.get("clips") or []:
        if not isinstance(clip, dict) or not bool(clip.get("enabled", True)):
            continue
        asset_id = str(clip.get("asset_id") or "").strip()
        if asset_id:
            result.add(asset_id)
    return result


def build_source_manifest(project: dict[str, Any]) -> list[dict[str, Any]]:
    """Hash every source referenced by an enabled timeline clip."""
    normalized = normalize_project(project)
    referenced = _referenced_asset_ids(normalized)
    assets = {
        str(asset.get("id") or asset.get("asset_id") or ""): asset
        for asset in normalized.get("assets") or []
        if isinstance(asset, dict)
    }
    manifest: list[dict[str, Any]] = []
    for asset_id in sorted(referenced):
        asset = assets.get(asset_id)
        if not isinstance(asset, dict):
            manifest.append(
                {
                    "asset_id": asset_id,
                    "media_kind": "unknown",
                    "source_path": "",
                    "status": "asset_missing",
                    "sha256": None,
                    "size_bytes": None,
                }
            )
            continue
        media_kind = str(asset.get("media_kind") or "unknown")
        source_raw = str(asset.get("source_path") or "").strip()
        if not source_raw:
            manifest.append(
                {
                    "asset_id": asset_id,
                    "media_kind": media_kind,
                    "source_path": "",
                    "status": "source_path_missing",
                    "sha256": None,
                    "size_bytes": None,
                }
            )
            continue
        source = Path(source_raw).expanduser()
        resolved = source.resolve(strict=False)
        if not source.exists() or not source.is_file():
            manifest.append(
                {
                    "asset_id": asset_id,
                    "media_kind": media_kind,
                    "source_path": str(resolved),
                    "status": "source_missing",
                    "sha256": None,
                    "size_bytes": None,
                }
            )
            continue
        try:
            size = source.stat().st_size
            digest = file_sha256(source)
        except OSError as exc:
            manifest.append(
                {
                    "asset_id": asset_id,
                    "media_kind": media_kind,
                    "source_path": str(resolved),
                    "status": "source_unreadable",
                    "error": f"{type(exc).__name__}: {exc}"[:500],
                    "sha256": None,
                    "size_bytes": None,
                }
            )
            continue
        manifest.append(
            {
                "asset_id": asset_id,
                "media_kind": media_kind,
                "source_path": str(resolved),
                "status": "ok",
                "sha256": digest,
                "size_bytes": int(size),
            }
        )
    return manifest


def _normalized_graph_project(project: dict[str, Any]) -> dict[str, Any]:
    """Return the normalized project fields that define rendered output.

    Project normalization refreshes audit timestamps on every call and fills a
    missing asset ``created_at`` with the current time.  Those fields are useful
    for persistence, but they are not render inputs and must not make the same
    project graph acquire a new identity between preview and export.
    """
    normalized = normalize_project(project)
    normalized.pop("created_at", None)
    normalized.pop("updated_at", None)
    for asset in normalized.get("assets") or []:
        if isinstance(asset, dict):
            asset.pop("created_at", None)
    return normalized


def build_graph_identity(project: dict[str, Any]) -> dict[str, Any]:
    normalized = _normalized_graph_project(project)
    source_manifest = build_source_manifest(normalized)
    source_manifest_hash = _sha256_bytes(_canonical_json(source_manifest))
    render_semantics = canonical_render_semantics()
    graph_hash = _sha256_bytes(
        _canonical_json(
            {
                "project": normalized,
                "source_manifest_hash": source_manifest_hash,
                "render_semantics": render_semantics,
            }
        )
    )
    missing_sources = [
        dict(item) for item in source_manifest if str(item.get("status")) != "ok"
    ]
    return {
        "graph_hash": graph_hash,
        "source_manifest_hash": source_manifest_hash,
        "source_manifest": source_manifest,
        "missing_sources": missing_sources,
        "render_semantics": render_semantics,
        "render_semantics_version": CANONICAL_RENDER_SEMANTICS_VERSION,
    }


def _slug(value: str) -> str:
    slug = "".join(ch if ch.isalnum() else "-" for ch in str(value).lower()).strip("-")
    return slug[:40] or "render"


def render_id_for(
    *,
    patch_seq: int,
    graph_hash: str,
    preset: str,
    label: str,
) -> str:
    """Return a stable artifact id that cannot collide across graph/preset changes."""
    return (
        f"{int(patch_seq):04d}-{str(graph_hash)[:12]}-"
        f"{_slug(preset)}-{_slug(label)}"
    )


def _finite_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float(default)
    return number if math.isfinite(number) else float(default)


def _fraction(value: Any) -> float:
    text = str(value or "").strip()
    if not text:
        return 0.0
    if "/" in text:
        left, right = text.split("/", 1)
        denominator = _finite_float(right)
        return _finite_float(left) / denominator if denominator else 0.0
    return _finite_float(text)


def _stream_duration(stream: dict[str, Any] | None) -> float:
    if not isinstance(stream, dict):
        return 0.0
    duration = _finite_float(stream.get("duration"), 0.0)
    if duration > 0.0:
        return duration
    duration_ts = _finite_float(stream.get("duration_ts"), 0.0)
    time_base = _fraction(stream.get("time_base"))
    if duration_ts > 0.0 and time_base > 0.0:
        return duration_ts * time_base
    return 0.0


def summarize_probe(probe: dict[str, Any] | None) -> dict[str, Any]:
    value = probe if isinstance(probe, dict) else {}
    streams = [item for item in value.get("streams") or [] if isinstance(item, dict)]
    video = next((item for item in streams if item.get("codec_type") == "video"), None)
    audio = next((item for item in streams if item.get("codec_type") == "audio"), None)
    fmt = value.get("format") if isinstance(value.get("format"), dict) else {}
    duration = _finite_float(fmt.get("duration"), 0.0)
    video_duration = _stream_duration(video)
    audio_duration = _stream_duration(audio)
    if duration <= 0:
        duration = video_duration
    fps = 0.0
    if video is not None:
        fps = _fraction(video.get("avg_frame_rate") or video.get("r_frame_rate"))
    return {
        "has_video": video is not None,
        "has_audio": audio is not None,
        "duration": round(duration, 6),
        "container_duration": round(duration, 6),
        "fps": round(fps, 6),
        "width": (
            int(_finite_float(video.get("width"), 0.0)) if video is not None else None
        ),
        "height": (
            int(_finite_float(video.get("height"), 0.0)) if video is not None else None
        ),
        "video_codec": (
            str(video.get("codec_name") or "") if video is not None else None
        ),
        "video_pixel_format": (
            str(video.get("pix_fmt") or "") if video is not None else None
        ),
        "video_duration": round(video_duration, 6) if video is not None else None,
        "audio_codec": (
            str(audio.get("codec_name") or "") if audio is not None else None
        ),
        "audio_sample_rate": (
            int(_finite_float(audio.get("sample_rate"), 0.0))
            if audio is not None
            else None
        ),
        "audio_channels": (
            int(_finite_float(audio.get("channels"), 0.0))
            if audio is not None
            else None
        ),
        "audio_channel_layout": (
            str(audio.get("channel_layout") or "") if audio is not None else None
        ),
        "audio_duration": round(audio_duration, 6) if audio is not None else None,
    }


def _acceptance_blockers(
    *,
    identity: dict[str, Any],
    output_path: Path,
    output_sha256: str | None,
    probe_summary: dict[str, Any],
    decode_check: dict[str, Any],
    dropped_fields: list[dict[str, Any]],
    expected: dict[str, Any],
    audio_analysis: dict[str, Any],
) -> list[dict[str, Any]]:
    blockers: list[dict[str, Any]] = []
    for item in identity.get("missing_sources") or []:
        blockers.append(
            {
                "code": "source_unavailable",
                "asset_id": item.get("asset_id"),
                "detail": item.get("status"),
            }
        )
    if not output_path.exists() or not output_path.is_file() or not output_sha256:
        blockers.append({"code": "output_missing", "detail": str(output_path)})
    if dropped_fields:
        blockers.append(
            {
                "code": "dropped_fields",
                "count": len(dropped_fields),
                "detail": "stored timeline semantics were not rendered",
            }
        )
    decode_status = str(decode_check.get("status") or "not_run")
    if decode_status == "failed":
        blockers.append(
            {
                "code": "decode_failed",
                "detail": str(decode_check.get("error") or "full decode failed")[:1000],
            }
        )
    if not bool(probe_summary.get("has_video")):
        blockers.append({"code": "video_stream_missing"})
    if bool(expected.get("require_h264_yuv420p")):
        if str(probe_summary.get("video_codec") or "") != "h264":
            blockers.append(
                {
                    "code": "video_codec_mismatch",
                    "expected": "h264",
                    "actual": probe_summary.get("video_codec"),
                }
            )
        if str(probe_summary.get("video_pixel_format") or "") != "yuv420p":
            blockers.append(
                {
                    "code": "pixel_format_mismatch",
                    "expected": "yuv420p",
                    "actual": probe_summary.get("video_pixel_format"),
                }
            )

    expected_width = int(_finite_float(expected.get("width"), 0.0))
    expected_height = int(_finite_float(expected.get("height"), 0.0))
    if expected_width and expected_height:
        if (
            int(probe_summary.get("width") or 0) != expected_width
            or int(probe_summary.get("height") or 0) != expected_height
        ):
            blockers.append(
                {
                    "code": "resolution_mismatch",
                    "expected": {"width": expected_width, "height": expected_height},
                    "actual": {
                        "width": probe_summary.get("width"),
                        "height": probe_summary.get("height"),
                    },
                }
            )
    expected_fps = _finite_float(expected.get("fps"), 0.0)
    actual_fps = _finite_float(probe_summary.get("fps"), 0.0)
    if expected_fps and (not actual_fps or abs(expected_fps - actual_fps) > 0.05):
        blockers.append(
            {"code": "fps_mismatch", "expected": expected_fps, "actual": actual_fps}
        )
    expected_duration = _finite_float(expected.get("duration"), 0.0)
    actual_duration = _finite_float(probe_summary.get("duration"), 0.0)
    tolerance = max(0.25, 2.0 / expected_fps) if expected_fps else 0.25
    if expected_duration and abs(expected_duration - actual_duration) > tolerance:
        blockers.append(
            {
                "code": "duration_mismatch",
                "expected": expected_duration,
                "actual": actual_duration,
                "tolerance": round(tolerance, 6),
            }
        )
    expected_video_duration = _finite_float(
        expected.get("video_duration"), expected_duration
    )
    actual_video_duration = _finite_float(probe_summary.get("video_duration"), 0.0)
    if expected_video_duration and (
        not actual_video_duration
        or abs(expected_video_duration - actual_video_duration) > tolerance
    ):
        blockers.append(
            {
                "code": "video_duration_mismatch",
                "expected": expected_video_duration,
                "actual": actual_video_duration,
                "tolerance": round(tolerance, 6),
            }
        )
    duration_min = _finite_float(expected.get("duration_min"), 0.0)
    duration_max = _finite_float(expected.get("duration_max"), 0.0)
    if duration_min and actual_duration < duration_min:
        blockers.append(
            {
                "code": "duration_below_minimum",
                "minimum": duration_min,
                "actual": actual_duration,
            }
        )
    if duration_max and actual_duration > duration_max:
        blockers.append(
            {
                "code": "duration_above_maximum",
                "maximum": duration_max,
                "actual": actual_duration,
            }
        )
    if bool(expected.get("has_audio")) and not bool(probe_summary.get("has_audio")):
        blockers.append({"code": "audio_stream_missing"})
    if bool(expected.get("has_audio")) and bool(probe_summary.get("has_audio")):
        expected_rate = int(_finite_float(expected.get("audio_sample_rate"), 0.0))
        expected_channels = int(_finite_float(expected.get("audio_channels"), 0.0))
        if (
            expected_rate
            and int(probe_summary.get("audio_sample_rate") or 0) != expected_rate
        ):
            blockers.append(
                {
                    "code": "audio_sample_rate_mismatch",
                    "expected": expected_rate,
                    "actual": probe_summary.get("audio_sample_rate"),
                }
            )
        if (
            expected_channels
            and int(probe_summary.get("audio_channels") or 0) != expected_channels
        ):
            blockers.append(
                {
                    "code": "audio_channels_mismatch",
                    "expected": expected_channels,
                    "actual": probe_summary.get("audio_channels"),
                }
            )
        expected_audio_duration = _finite_float(expected.get("audio_duration"), 0.0)
        actual_audio_duration = _finite_float(probe_summary.get("audio_duration"), 0.0)
        if expected_audio_duration and (
            not actual_audio_duration
            or abs(expected_audio_duration - actual_audio_duration) > 0.25
        ):
            blockers.append(
                {
                    "code": "audio_duration_mismatch",
                    "expected": expected_audio_duration,
                    "actual": actual_audio_duration,
                    "tolerance": 0.25,
                }
            )
        if actual_video_duration and (
            not actual_audio_duration
            or abs(actual_video_duration - actual_audio_duration) > 0.25
        ):
            blockers.append(
                {
                    "code": "audio_video_duration_mismatch",
                    "expected": actual_video_duration,
                    "actual": actual_audio_duration,
                    "tolerance": 0.25,
                }
            )
        loudness_target = expected.get("integrated_loudness_lufs")
        loudness_tolerance = _finite_float(expected.get("loudness_tolerance_lu"), 1.0)
        measured_loudness = audio_analysis.get("integrated_loudness_lufs")
        if loudness_target is not None:
            if measured_loudness is None:
                blockers.append({"code": "loudness_measurement_missing"})
            elif (
                abs(float(measured_loudness) - float(loudness_target))
                > loudness_tolerance
            ):
                blockers.append(
                    {
                        "code": "integrated_loudness_out_of_range",
                        "target": float(loudness_target),
                        "tolerance": loudness_tolerance,
                        "actual": float(measured_loudness),
                    }
                )
        true_peak_max = expected.get("true_peak_max_dbtp")
        measured_peak = audio_analysis.get("true_peak_dbtp")
        if true_peak_max is not None:
            if measured_peak is None:
                blockers.append({"code": "true_peak_measurement_missing"})
            elif float(measured_peak) > float(true_peak_max) + 1e-9:
                blockers.append(
                    {
                        "code": "true_peak_too_high",
                        "maximum": float(true_peak_max),
                        "actual": float(measured_peak),
                    }
                )
    return blockers


def build_render_receipt(
    *,
    project: dict[str, Any],
    project_id: str,
    patch_seq: int,
    preset: str,
    render_id: str,
    output_path: str | Path,
    probe: dict[str, Any] | None,
    decode_check: dict[str, Any] | None,
    dropped_fields: list[dict[str, Any]] | None = None,
    degradations: list[dict[str, Any]] | None = None,
    expected: dict[str, Any] | None = None,
    audio_analysis: dict[str, Any] | None = None,
    graph_identity: dict[str, Any] | None = None,
    project_revision: int | None = None,
) -> dict[str, Any]:
    identity = graph_identity or build_graph_identity(project)
    output = Path(output_path).expanduser().resolve()
    output_sha256: str | None = None
    if output.exists() and output.is_file():
        try:
            output_sha256 = file_sha256(output)
        except OSError:
            output_sha256 = None
    probe_summary = summarize_probe(probe)
    decode = dict(decode_check or {"status": "not_run"})
    dropped = [dict(item) for item in (dropped_fields or [])]
    degraded = [dict(item) for item in (degradations or [])]
    audio_metrics = dict(audio_analysis or {})
    blockers = _acceptance_blockers(
        identity=identity,
        output_path=output,
        output_sha256=output_sha256,
        probe_summary=probe_summary,
        decode_check=decode,
        dropped_fields=dropped,
        expected=dict(expected or {}),
        audio_analysis=audio_metrics,
    )
    if blockers:
        machine_status = "rejected"
    elif str(decode.get("status") or "not_run") != "passed":
        machine_status = "provisional"
    else:
        machine_status = "passed"
    return {
        "schema_version": RENDER_RECEIPT_SCHEMA_VERSION,
        "project_id": str(project_id),
        "project_revision": int(
            patch_seq if project_revision is None else project_revision
        ),
        "render_id": str(render_id),
        "preset": str(preset),
        "graph_hash": identity["graph_hash"],
        "source_manifest_hash": identity["source_manifest_hash"],
        "source_manifest": identity["source_manifest"],
        "render_semantics": identity.get(
            "render_semantics", canonical_render_semantics()
        ),
        "render_semantics_version": identity.get(
            "render_semantics_version", CANONICAL_RENDER_SEMANTICS_VERSION
        ),
        "output_path": str(output),
        "output_sha256": output_sha256,
        "probe": probe_summary,
        "decode_check": decode,
        "audio_analysis": audio_metrics,
        "dropped_fields": dropped,
        "degradations": degraded,
        "machine_status": machine_status,
        "machine_blockers": blockers,
        "review_status": "pending",
        "accepted": False,
        "created_at": _now(),
    }


def bind_render_receipt_revision(
    receipt: dict[str, Any],
    *,
    project_revision: int,
    receipt_path: str | Path | None = None,
) -> dict[str, Any]:
    """Bind a completed receipt to the durable project revision atomically.

    ``ProjectStore.patch_seq`` identifies timeline patches, while the durable
    project revision also covers referenced asset provenance.  Tool dispatch
    can only know that durable revision after any export-time comp refresh has
    completed, so this small final binding is intentionally separate.
    """

    receipt["project_revision"] = int(project_revision)
    if receipt_path is not None:
        path = Path(receipt_path)
        manifest_path: Path | None = None
        manifest: dict[str, Any] | None = None
        if path.name.endswith(".receipt.json"):
            candidate = path.with_name(
                path.name.removesuffix(".receipt.json") + ".manifest.json"
            )
            if candidate.exists():
                try:
                    value = json.loads(candidate.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
                    raise ValueError(
                        f"cannot bind render manifest revision: {candidate}"
                    ) from exc
                if not isinstance(value, dict):
                    raise ValueError(
                        f"render manifest must contain an object: {candidate}"
                    )
                manifest_path = candidate
                manifest = value
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(receipt, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        tmp.replace(path)
        if manifest_path is not None and manifest is not None:
            manifest["project_revision"] = int(project_revision)
            manifest["render_receipt"] = dict(receipt)
            manifest["machine_status"] = receipt.get("machine_status")
            manifest["machine_blockers"] = list(receipt.get("machine_blockers") or [])
            manifest_tmp = manifest_path.with_suffix(manifest_path.suffix + ".tmp")
            manifest_tmp.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )
            manifest_tmp.replace(manifest_path)
    return receipt


__all__ = [
    "RENDER_RECEIPT_SCHEMA_VERSION",
    "CANONICAL_RENDER_SEMANTICS_VERSION",
    "AUDIO_MASTER_TARGET_LUFS",
    "AUDIO_MASTER_TRUE_PEAK_DBTP",
    "AUDIO_MASTER_LRA_LU",
    "canonical_render_semantics",
    "file_sha256",
    "build_source_manifest",
    "build_graph_identity",
    "render_id_for",
    "summarize_probe",
    "build_render_receipt",
    "bind_render_receipt_revision",
]
