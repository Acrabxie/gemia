from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class DialogueMatcherResult:
    output_path: str
    metadata_path: str


def render_dialogue_matcher_plan(
    reference_path: str,
    target_path: str,
    output_path: str,
    *,
    sample_seconds: float = 10.0,
    target_track_id: str = "A1",
) -> str:
    """Analyze two dialogue clips and write Resolve-style match metadata."""
    reference = Path(reference_path).expanduser().resolve()
    target = Path(target_path).expanduser().resolve()
    output = Path(output_path).expanduser().resolve()
    if not reference.exists():
        raise FileNotFoundError(f"Reference file not found: {reference}")
    if not target.exists():
        raise FileNotFoundError(f"Target file not found: {target}")

    output.parent.mkdir(parents=True, exist_ok=True)
    metadata_path = output.with_suffix(".dialogue_matcher.json")
    remux_mode = _copy_target_media(target, output)
    reference_has_audio = _has_audio_stream(reference)
    target_has_audio = _has_audio_stream(target)
    diagnostics: list[str] = []
    reference_metrics = _analyze_audio(reference, sample_seconds) if reference_has_audio else {}
    target_metrics = _analyze_audio(target, sample_seconds) if target_has_audio else {}
    if not reference_has_audio:
        diagnostics.append(f"Reference file '{reference}' has no audio stream.")
    if not target_has_audio:
        diagnostics.append(f"Target file '{target}' has no audio stream.")

    level_delta_db = _delta(reference_metrics, target_metrics, "integrated_loudness_lufs", "rms_level_db")
    tone_brightness_delta = _delta(reference_metrics, target_metrics, "zero_crossings_rate")
    noise_floor_delta_db = _delta(reference_metrics, target_metrics, "noise_floor_db", "mean_volume_db")
    room_reverb_delta = _delta(reference_metrics, target_metrics, "flat_factor")
    match_actions: list[dict[str, Any]] = []
    if level_delta_db is not None:
        match_actions.append({"kind": "gain", "value_db": level_delta_db, "reason": "match reference dialogue loudness"})
    if tone_brightness_delta is not None:
        match_actions.append({"kind": "tone_proxy", "zero_crossing_rate_delta": tone_brightness_delta})
    if noise_floor_delta_db is not None:
        match_actions.append({"kind": "noise_floor_proxy", "value_db": noise_floor_delta_db})
    if room_reverb_delta is not None:
        match_actions.append({"kind": "room_proxy", "flat_factor_delta": room_reverb_delta})
    if not match_actions:
        match_actions.append({"kind": "diagnostic_only", "reason": "insufficient audio metrics for automatic match advice"})

    metadata = {
        "schema_version": 1,
        "effect": "resolve21_ai_dialogue_matcher",
        "rendered_at": datetime.now(timezone.utc).isoformat(),
        "reference_path": str(reference),
        "target_path": str(target),
        "output_path": str(output),
        "metadata_path": str(metadata_path),
        "sample_seconds": sample_seconds,
        "target_track_id": target_track_id,
        "remux_mode": remux_mode,
        "analysis_results": {
            "reference": {"has_audio": reference_has_audio, "audio_metrics": reference_metrics},
            "target": {"has_audio": target_has_audio, "audio_metrics": target_metrics},
        },
        "level_delta_db": level_delta_db,
        "tone_brightness_delta": tone_brightness_delta,
        "noise_floor_delta_db": noise_floor_delta_db,
        "room_reverb_delta": room_reverb_delta,
        "match_actions": match_actions,
        "diagnostics": diagnostics,
        "review_hints": [
            "confirm suggested gain delta is plausible",
            "use tone/room proxies as deterministic dry-run guidance",
            "replace analysis-only sidecar with rendered EQ/reverb chain when audio DSP lane is available",
        ],
    }
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return str(output)


def _run(cmd: list[str], message: str) -> str:
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    combined = "\n".join(part for part in (proc.stdout, proc.stderr) if part)
    if proc.returncode != 0:
        raise RuntimeError(f"{message}: {combined[-800:]}")
    return combined


def _copy_target_media(target: Path, output: Path) -> str:
    try:
        _run(["ffmpeg", "-y", "-i", str(target), "-map", "0", "-c", "copy", str(output)], "ffmpeg remux failed")
        return "ffmpeg_stream_copy"
    except RuntimeError:
        shutil.copyfile(target, output)
        return "byte_copy_fallback"


def _has_audio_stream(path: Path) -> bool:
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index", "-of", "csv=p=0", str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode == 0 and bool(proc.stdout.strip())


def _analyze_audio(path: Path, sample_seconds: float) -> dict[str, float]:
    output = _run(
        [
            "ffmpeg", "-hide_banner", "-nostats", "-t", str(max(float(sample_seconds), 0.25)),
            "-i", str(path), "-af", "aformat=channel_layouts=mono,volumedetect,astats=metadata=1:reset=0,ebur128=peak=true",
            "-f", "null", "-",
        ],
        f"ffmpeg dialogue analysis failed for {path}",
    )
    metrics: dict[str, float] = {}
    patterns = {
        "mean_volume_db": r"mean_volume:\s*([-+0-9.]+)\s*dB",
        "max_volume_db": r"max_volume:\s*([-+0-9.]+)\s*dB",
        "rms_level_db": r"RMS level dB:\s*([-+0-9.]+)",
        "peak_level_db": r"Peak level dB:\s*([-+0-9.]+)",
        "noise_floor_db": r"Noise floor dB:\s*([-+0-9.]+)",
        "zero_crossings_rate": r"Zero crossings rate:\s*([-+0-9.]+)",
        "flat_factor": r"Flat factor:\s*([-+0-9.]+)",
    }
    for key, pattern in patterns.items():
        match = re.search(pattern, output)
        if match:
            metrics[key] = round(float(match.group(1)), 5)
    integrated = re.findall(r"I:\s*([-+0-9.]+)\s*LUFS", output)
    lra = re.findall(r"LRA:\s*([-+0-9.]+)\s*LU", output)
    if integrated:
        metrics["integrated_loudness_lufs"] = float(integrated[-1])
    if lra:
        metrics["loudness_range_lu"] = float(lra[-1])
    return metrics


def _delta(reference: dict[str, float], target: dict[str, float], *keys: str) -> float | None:
    for key in keys:
        if key in reference and key in target:
            return round(reference[key] - target[key], 5)
    return None


__all__ = ["DialogueMatcherResult", "render_dialogue_matcher_plan"]
