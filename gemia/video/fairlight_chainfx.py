"""Resolve 21 Fairlight EQ/level matching plus Chain FX manifests."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any

from gemia.video.timeline_assets import extract_waveform_peaks, probe_media


DEFAULT_CHAIN_FX: list[dict[str, Any]] = [
    {"id": "clip_eq", "tool": "FairlightClipEQ", "order": 1, "enabled": True},
    {"id": "level_match", "tool": "LevelMatcher", "order": 2, "enabled": True},
    {"id": "soft_limiter", "tool": "Limiter", "order": 3, "enabled": True, "ceiling_db": -1.0},
]


def render_fairlight_eq_level_match_chainfx(
    input_paths: list[str],
    output_dir: str,
    *,
    chain_id: str = "resolve21_fairlight_eq_level_match_chainfx",
    target_peak: float | None = None,
    chain_fx: list[dict[str, Any]] | None = None,
    analysis_samples: int = 32,
) -> str:
    """Create a deterministic Fairlight level-match and Chain FX manifest."""
    if not input_paths:
        raise ValueError("input_paths must contain at least one media path")
    output_root = Path(output_dir).expanduser().resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    samples = max(16, min(int(analysis_samples), 96))
    fx_chain = [_normalize_fx(raw, index) for index, raw in enumerate(chain_fx or DEFAULT_CHAIN_FX)]

    analyzed = []
    for index, raw_path in enumerate(input_paths):
        source = Path(raw_path).expanduser().resolve()
        if not source.exists():
            raise FileNotFoundError(f"Input media not found: {source}")
        if not source.is_file():
            raise OSError(f"Input media is not a file: {source}")
        probe = probe_media(str(source))
        peaks = extract_waveform_peaks(str(source), samples=samples)
        analysis = _analysis(peaks, probe)
        analyzed.append(
            {
                "clip_id": f"chainfx_clip_{index:02d}_{_safe_id(source.stem)}",
                "source_path": str(source),
                "asset_ref": _asset_ref(source, probe),
                "source_probe": probe,
                "analysis": analysis,
            }
        )

    reference_peak = _target_peak(target_peak, analyzed)
    clips = [_clip_chain(item, reference_peak, fx_chain) for item in analyzed]
    manifest = {
        "schema_version": 1,
        "effect": "resolve21_fairlight_eq_level_match_chainfx",
        "rendered_at": datetime.now(timezone.utc).isoformat(),
        "chain": {
            "chain_id": _safe_id(chain_id),
            "clip_count": len(clips),
            "analysis_samples": samples,
            "reference_peak": reference_peak,
        },
        "chain_fx": fx_chain,
        "clips": clips,
        "match_report": _match_report(clips),
        "diagnostics": [
            f"{len(clips)} real clips analyzed for Fairlight EQ/level matching",
            f"{len(fx_chain)} Chain FX stages emitted in deterministic order",
        ],
        "review_hints": [
            "audition level_delta_db before applying destructive gain",
            "confirm limiter ceiling after EQ and level matching",
            "preserve asset_ref values to reproduce the same matching report",
        ],
    }
    manifest_path = output_root / "fairlight_eq_level_match_chainfx_manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return str(manifest_path)


def _normalize_fx(raw: dict[str, Any], index: int) -> dict[str, Any]:
    order = int(raw.get("order", index + 1))
    ceiling = raw.get("ceiling_db")
    result = {
        "fx_id": _safe_id(str(raw.get("id") or f"fx_{index + 1}")),
        "tool": str(raw.get("tool") or "FairlightFX"),
        "order": max(order, 1),
        "enabled": bool(raw.get("enabled", True)),
    }
    if ceiling is not None:
        result["ceiling_db"] = round(max(-24.0, min(float(ceiling), 0.0)), 2)
    return result


def _analysis(peaks: list[float], probe: dict[str, Any]) -> dict[str, Any]:
    peak_max = max(peaks) if peaks else 0.0
    peak_mean = mean(peaks) if peaks else 0.0
    return {
        "has_audio": bool(probe.get("has_audio")),
        "audio_codec": str(probe.get("audio_codec") or ""),
        "peak_max": round(peak_max, 4),
        "peak_mean": round(peak_mean, 4),
        "dynamic_hint": round(peak_max - peak_mean, 4),
    }


def _target_peak(target_peak: float | None, clips: list[dict[str, Any]]) -> float:
    if target_peak is not None:
        return round(max(0.05, min(float(target_peak), 0.98)), 4)
    peaks = [float(clip["analysis"]["peak_max"]) for clip in clips if clip["analysis"]["peak_max"] > 0]
    return round(min(mean(peaks), 0.88), 4) if peaks else 0.25


def _clip_chain(item: dict[str, Any], reference_peak: float, chain_fx: list[dict[str, Any]]) -> dict[str, Any]:
    peak = max(float(item["analysis"]["peak_max"]), 0.0001)
    level_delta = 20.0 * __import__("math").log10(reference_peak / peak)
    level_delta = max(-18.0, min(level_delta, 18.0))
    return {
        **item,
        "level_match": {
            "target_peak": reference_peak,
            "level_delta_db": round(level_delta, 2),
            "mode": "clip_gain",
        },
        "eq_match": {
            "low_shelf_db": round(-0.5 if item["analysis"]["dynamic_hint"] > 0.45 else 0.5, 2),
            "presence_db": round(0.7 if item["analysis"]["peak_mean"] < 0.1 else 0.0, 2),
            "air_db": round(0.5, 2),
        },
        "chain_instances": [
            {"fx_id": fx["fx_id"], "tool": fx["tool"], "order": fx["order"], "enabled": fx["enabled"]}
            for fx in sorted(chain_fx, key=lambda fx: fx["order"])
        ],
    }


def _match_report(clips: list[dict[str, Any]]) -> dict[str, Any]:
    deltas = [abs(float(clip["level_match"]["level_delta_db"])) for clip in clips]
    return {
        "max_abs_level_delta_db": round(max(deltas) if deltas else 0.0, 2),
        "matched_clip_count": len(clips),
        "needs_manual_audition": any(delta > 9.0 for delta in deltas),
    }


def _asset_ref(path: Path, probe: dict[str, Any]) -> str:
    return f"{_safe_id(path.stem)}:{path.stat().st_size}:{round(float(probe.get('duration') or 0.0), 3)}:{int(probe.get('width') or 0)}x{int(probe.get('height') or 0)}"


def _safe_id(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", value.strip()).strip("_").lower() or "item"


__all__ = ["DEFAULT_CHAIN_FX", "render_fairlight_eq_level_match_chainfx"]
