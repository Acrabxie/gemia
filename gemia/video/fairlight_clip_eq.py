"""Resolve 21 Fairlight six-band clip EQ manifests."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any

from gemia.video.timeline_assets import extract_waveform_peaks, probe_media


DEFAULT_EQ_BANDS: list[dict[str, Any]] = [
    {"id": "low_cut", "frequency_hz": 80, "gain_db": -3.0, "q": 0.7, "kind": "high_pass"},
    {"id": "low_shelf", "frequency_hz": 160, "gain_db": 1.5, "q": 0.8, "kind": "shelf"},
    {"id": "low_mid", "frequency_hz": 450, "gain_db": -1.0, "q": 1.1, "kind": "bell"},
    {"id": "mid_presence", "frequency_hz": 1600, "gain_db": 1.0, "q": 1.0, "kind": "bell"},
    {"id": "air_shelf", "frequency_hz": 7200, "gain_db": 1.8, "q": 0.9, "kind": "shelf"},
    {"id": "de_harsh", "frequency_hz": 12000, "gain_db": -0.8, "q": 1.2, "kind": "bell"},
]


def render_fairlight_6band_clip_eq(
    input_paths: list[str],
    output_dir: str,
    *,
    preset_id: str = "resolve21_fairlight_6band_clip_eq",
    eq_bands: list[dict[str, Any]] | None = None,
    analysis_samples: int = 32,
) -> str:
    """Create a Resolve/Fairlight-style six-band clip EQ manifest from real media."""
    if not input_paths:
        raise ValueError("input_paths must contain at least one media path")
    output_root = Path(output_dir).expanduser().resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    bands = [_normalize_band(raw, index) for index, raw in enumerate(eq_bands or DEFAULT_EQ_BANDS)]
    if len(bands) != 6:
        raise ValueError("Fairlight clip EQ requires exactly six bands")
    samples = max(16, min(int(analysis_samples), 96))

    clips = []
    for index, raw_path in enumerate(input_paths):
        source = Path(raw_path).expanduser().resolve()
        if not source.exists():
            raise FileNotFoundError(f"Input media not found: {source}")
        if not source.is_file():
            raise OSError(f"Input media is not a file: {source}")
        probe = probe_media(str(source))
        peaks = extract_waveform_peaks(str(source), samples=samples)
        clip_id = f"eq_clip_{index:02d}_{_safe_id(source.stem)}"
        loudness_hint = _loudness_hint(peaks, probe)
        clips.append(
            {
                "clip_id": clip_id,
                "source_path": str(source),
                "asset_ref": _asset_ref(source, probe),
                "source_probe": probe,
                "analysis": loudness_hint,
                "eq_bands": [_band_for_clip(band, loudness_hint) for band in bands],
            }
        )

    manifest = {
        "schema_version": 1,
        "effect": "resolve21_fairlight_6band_clip_eq",
        "rendered_at": datetime.now(timezone.utc).isoformat(),
        "preset": {
            "preset_id": _safe_id(preset_id),
            "band_count": 6,
            "clip_count": len(clips),
            "analysis_samples": samples,
        },
        "clips": clips,
        "fairlight_tools": [
            {"tool_id": "clip_eq", "tool": "FairlightClipEQ", "bands": [band["band_id"] for band in bands]},
            {"tool_id": "eq_audit", "tool": "FairlightEQCurveAudit", "target": "clip"},
        ],
        "diagnostics": [
            f"{len(clips)} real media clips analyzed for six-band clip EQ",
            "six deterministic EQ bands emitted per clip",
        ],
        "review_hints": [
            "compare low_cut and de_harsh bands against voice/music content before final mix",
            "keep clip asset_ref stable when transferring EQ settings to Resolve",
            "use analysis.peak_mean and peak_max to spot unusually quiet or clipped media",
        ],
    }
    manifest_path = output_root / "fairlight_6band_clip_eq_manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return str(manifest_path)


def _normalize_band(raw: dict[str, Any], index: int) -> dict[str, Any]:
    kind = str(raw.get("kind") or "bell").lower()
    if kind not in {"bell", "shelf", "high_pass", "low_pass"}:
        kind = "bell"
    frequency = max(20.0, min(float(raw.get("frequency_hz", 1000.0)), 20000.0))
    gain = max(-24.0, min(float(raw.get("gain_db", 0.0)), 24.0))
    q_value = max(0.1, min(float(raw.get("q", 1.0)), 12.0))
    return {
        "band_id": _safe_id(str(raw.get("id") or f"band_{index + 1}")),
        "frequency_hz": round(frequency, 2),
        "gain_db": round(gain, 2),
        "q": round(q_value, 3),
        "kind": kind,
    }


def _loudness_hint(peaks: list[float], probe: dict[str, Any]) -> dict[str, Any]:
    peak_max = max(peaks) if peaks else 0.0
    peak_mean = mean(peaks) if peaks else 0.0
    return {
        "has_audio": bool(probe.get("has_audio")),
        "audio_codec": str(probe.get("audio_codec") or ""),
        "peak_max": round(peak_max, 4),
        "peak_mean": round(peak_mean, 4),
        "recommended_trim_db": round(-3.0 if peak_max > 0.92 else 1.5 if peak_mean < 0.08 else 0.0, 2),
    }


def _band_for_clip(band: dict[str, Any], analysis: dict[str, Any]) -> dict[str, Any]:
    trim = float(analysis["recommended_trim_db"])
    gain = float(band["gain_db"])
    adjusted = gain + (trim * 0.25 if band["kind"] in {"shelf", "bell"} else 0.0)
    result = dict(band)
    result["clip_gain_db"] = round(max(-24.0, min(adjusted, 24.0)), 2)
    result["automation"] = [{"time_seconds": 0.0, "gain_db": result["clip_gain_db"]}]
    return result


def _asset_ref(path: Path, probe: dict[str, Any]) -> str:
    return f"{_safe_id(path.stem)}:{path.stat().st_size}:{round(float(probe.get('duration') or 0.0), 3)}:{int(probe.get('width') or 0)}x{int(probe.get('height') or 0)}"


def _safe_id(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", value.strip()).strip("_").lower() or "item"


__all__ = ["DEFAULT_EQ_BANDS", "render_fairlight_6band_clip_eq"]
