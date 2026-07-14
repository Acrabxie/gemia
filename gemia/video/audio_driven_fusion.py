"""Resolve 21 audio-driven Fusion animation manifests."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any

from gemia.video.timeline_assets import extract_waveform_peaks, probe_media


DEFAULT_PARAMETER_TARGETS: list[dict[str, Any]] = [
    {"id": "glow_gain", "node": "AudioGlow", "minimum": 0.1, "maximum": 0.85, "curve": "linear"},
    {"id": "scale_pulse", "node": "Transform", "minimum": 1.0, "maximum": 1.08, "curve": "ease_out"},
    {"id": "particle_birth_rate", "node": "AudioParticles", "minimum": 8, "maximum": 42, "curve": "stepped"},
]


def render_audio_driven_fusion_animation(
    input_paths: list[str],
    output_dir: str,
    *,
    animation_id: str = "resolve21_audio_driven_fusion_animation",
    parameter_targets: list[dict[str, Any]] | None = None,
    sample_count: int = 24,
    smoothing: float = 0.25,
) -> str:
    """Map real clip audio levels into Fusion-style parameter automation curves."""
    if not input_paths:
        raise ValueError("input_paths must contain at least one media path")
    samples = max(16, min(int(sample_count), 96))
    smooth = min(max(float(smoothing), 0.0), 0.95)
    output_root = Path(output_dir).expanduser().resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    targets = [_normalize_target(raw, index) for index, raw in enumerate(parameter_targets or DEFAULT_PARAMETER_TARGETS)]

    sources = []
    automation_sets = []
    for index, raw_path in enumerate(input_paths):
        source = Path(raw_path).expanduser().resolve()
        if not source.exists():
            raise FileNotFoundError(f"Input media not found: {source}")
        if not source.is_file():
            raise OSError(f"Input media is not a file: {source}")
        probe = probe_media(str(source))
        peaks = extract_waveform_peaks(str(source), samples=samples)
        smoothed = _smooth_peaks(peaks, smooth)
        source_id = f"audio_clip_{index:02d}_{_safe_id(source.stem)}"
        asset_ref = _asset_ref(source, probe)
        sources.append(
            {
                "clip_id": source_id,
                "source_path": str(source),
                "source_probe": probe,
                "asset_ref": asset_ref,
                "audio_summary": _audio_summary(smoothed, probe),
            }
        )
        automation_sets.append(
            {
                "clip_id": source_id,
                "asset_ref": asset_ref,
                "sample_count": len(smoothed),
                "beat_markers": _beat_markers(smoothed, probe),
                "parameter_curves": [_curve_for_target(target, smoothed, probe) for target in targets],
            }
        )

    manifest = {
        "schema_version": 1,
        "effect": "resolve21_audio_driven_fusion_animation",
        "rendered_at": datetime.now(timezone.utc).isoformat(),
        "animation": {
            "animation_id": _safe_id(animation_id),
            "clip_count": len(sources),
            "parameter_count": len(targets),
            "sample_count": samples,
            "smoothing": smooth,
        },
        "sources": sources,
        "fusion_nodes": _fusion_nodes(targets),
        "automation_sets": automation_sets,
        "diagnostics": [
            f"{len(sources)} real clips analyzed for audio-driven Fusion animation",
            f"{len(targets)} parameter curves emitted per clip from waveform peaks",
        ],
        "review_hints": [
            "inspect beat_markers against audible transients before accepting motion timing",
            "confirm parameter min/max ranges are appropriate for the target Fusion macro",
            "use asset_ref values to reproduce the same automation curves from the same source clips",
        ],
    }
    manifest_path = output_root / "audio_driven_fusion_animation_manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return str(manifest_path)


def _normalize_target(raw: dict[str, Any], index: int) -> dict[str, Any]:
    target_id = _safe_id(str(raw.get("id") or f"parameter_{index}"))
    minimum = float(raw.get("minimum", raw.get("min", 0.0)))
    maximum = float(raw.get("maximum", raw.get("max", 1.0)))
    if maximum < minimum:
        minimum, maximum = maximum, minimum
    curve = str(raw.get("curve") or "linear").strip().lower()
    if curve not in {"linear", "ease_out", "stepped"}:
        curve = "linear"
    return {
        "parameter_id": target_id,
        "node": str(raw.get("node") or "CustomTool"),
        "minimum": round(minimum, 4),
        "maximum": round(maximum, 4),
        "curve": curve,
    }


def _smooth_peaks(peaks: list[float], smoothing: float) -> list[float]:
    result = []
    previous = 0.0
    for peak in peaks:
        value = min(max(float(peak), 0.0), 1.0)
        previous = value if not result else previous * smoothing + value * (1.0 - smoothing)
        result.append(round(previous, 4))
    return result


def _audio_summary(peaks: list[float], probe: dict[str, Any]) -> dict[str, Any]:
    peak_max = max(peaks) if peaks else 0.0
    peak_mean = mean(peaks) if peaks else 0.0
    return {
        "has_audio": bool(probe.get("has_audio")),
        "audio_codec": str(probe.get("audio_codec") or ""),
        "peak_max": round(peak_max, 4),
        "peak_mean": round(peak_mean, 4),
        "nonzero_peak_count": sum(1 for peak in peaks if peak > 0.001),
    }


def _beat_markers(peaks: list[float], probe: dict[str, Any]) -> list[dict[str, Any]]:
    duration = float(probe.get("duration") or 0.0)
    if not peaks:
        return []
    markers = []
    threshold = max(mean(peaks) * 1.2, 0.02)
    for index, peak in enumerate(peaks):
        left = peaks[index - 1] if index > 0 else 0.0
        right = peaks[index + 1] if index + 1 < len(peaks) else 0.0
        if peak >= threshold and peak >= left and peak >= right:
            markers.append({"time_seconds": _time_at(index, len(peaks), duration), "strength": round(peak, 4)})
    return markers[:12]


def _curve_for_target(target: dict[str, Any], peaks: list[float], probe: dict[str, Any]) -> dict[str, Any]:
    minimum = float(target["minimum"])
    maximum = float(target["maximum"])
    keyframes = []
    for index, peak in enumerate(peaks):
        shaped = _shape_peak(peak, target["curve"])
        value = minimum + shaped * (maximum - minimum)
        keyframes.append(
            {
                "time_seconds": _time_at(index, len(peaks), float(probe.get("duration") or 0.0)),
                "value": round(value, 4),
                "source_peak": round(float(peak), 4),
            }
        )
    return {
        "parameter_id": target["parameter_id"],
        "node": target["node"],
        "curve": target["curve"],
        "value_range": [minimum, maximum],
        "keyframes": keyframes,
    }


def _shape_peak(peak: float, curve: str) -> float:
    if curve == "ease_out":
        return 1.0 - (1.0 - peak) * (1.0 - peak)
    if curve == "stepped":
        return 0.0 if peak < 0.2 else 0.5 if peak < 0.55 else 1.0
    return peak


def _time_at(index: int, count: int, duration: float) -> float:
    if count <= 1 or duration <= 0:
        return 0.0
    return round(duration * index / (count - 1), 3)


def _fusion_nodes(targets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    nodes = []
    for target in targets:
        nodes.append(
            {
                "node_id": _safe_id(target["node"]),
                "kind": target["node"],
                "driven_parameter": target["parameter_id"],
                "modifier": "FairlightAnimator",
            }
        )
    return nodes


def _asset_ref(path: Path, probe: dict[str, Any]) -> str:
    return f"{_safe_id(path.stem)}:{path.stat().st_size}:{round(float(probe.get('duration') or 0.0), 3)}:{int(probe.get('width') or 0)}x{int(probe.get('height') or 0)}"


def _safe_id(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", value.strip()).strip("_").lower() or "item"


__all__ = ["DEFAULT_PARAMETER_TARGETS", "render_audio_driven_fusion_animation"]
