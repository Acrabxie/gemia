"""Resolve 21 blended Fusion, audio, USD/Hydra, and Fairlight review scene."""
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from gemia.video.audio_driven_fusion import render_audio_driven_fusion_animation
from gemia.video.fairlight_chainfx import render_fairlight_eq_level_match_chainfx
from gemia.video.fairlight_clip_eq import render_fairlight_6band_clip_eq
from gemia.video.fusion_macro_inspector import render_fusion_macro_editor_inspector_manifest
from gemia.video.fusion_usd_hydra import render_fusion_usd_hydra_toolset_manifest


def render_blended_fusion_audio_usd_fairlight_scene(
    video_paths: Iterable[str],
    output_path: str,
    *,
    scene_id: str = "resolve21_fusion_audio_usd_fairlight_scene",
    max_seconds: float = 1.0,
    temp_dir: str | Path | None = None,
) -> str:
    """Create a fused real-media review package across Resolve 21 batch 006."""
    videos = [Path(path).expanduser().resolve() for path in video_paths]
    if not videos:
        raise ValueError("video_paths cannot be empty")
    for video in videos:
        if not video.exists():
            raise FileNotFoundError(f"Video file not found: {video}")
        if not video.is_file():
            raise OSError(f"Video path is not a file: {video}")
    if max_seconds <= 0:
        raise ValueError("max_seconds must be greater than 0")

    output = Path(output_path).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    work_dir = Path(temp_dir).expanduser().resolve() if temp_dir else output.parent / f"{output.stem}_fusion_audio_usd_fairlight_steps"
    work_dir.mkdir(parents=True, exist_ok=True)
    inputs = [str(path) for path in videos]

    macro_manifest = Path(render_fusion_macro_editor_inspector_manifest(
        inputs,
        str(work_dir / "01_fusion_macro_inspector"),
        macro_id=f"{scene_id}:macro",
        macro_name="Lumeri Resolve 21 fused macro",
    ))
    audio_manifest = Path(render_audio_driven_fusion_animation(
        inputs,
        str(work_dir / "02_audio_driven_fusion"),
        animation_id=f"{scene_id}:audio_motion",
        sample_count=24,
    ))
    usd_manifest = Path(render_fusion_usd_hydra_toolset_manifest(
        inputs,
        str(work_dir / "03_fusion_usd_hydra"),
        stage_id=f"{scene_id}:usd_stage",
        frame_range=(1, 120),
    ))
    eq_manifest = Path(render_fairlight_6band_clip_eq(
        inputs,
        str(work_dir / "04_fairlight_clip_eq"),
        preset_id=f"{scene_id}:clip_eq",
        analysis_samples=32,
    ))
    chainfx_manifest = Path(render_fairlight_eq_level_match_chainfx(
        inputs,
        str(work_dir / "05_fairlight_chainfx"),
        chain_id=f"{scene_id}:chainfx",
        analysis_samples=32,
    ))

    _write_review_video(videos, output, max_seconds=max_seconds)
    macro_payload = _read_json(macro_manifest)
    audio_payload = _read_json(audio_manifest)
    usd_payload = _read_json(usd_manifest)
    eq_payload = _read_json(eq_manifest)
    chainfx_payload = _read_json(chainfx_manifest)
    metadata = {
        "schema_version": 1,
        "effect": "resolve21_blended_fusion_audio_usd_fairlight_scene",
        "rendered_at": datetime.now(timezone.utc).isoformat(),
        "scene": {
            "scene_id": scene_id,
            "source_video_paths": [str(path) for path in videos],
            "output_path": str(output),
            "clip_count": len(videos),
            "max_seconds": float(max_seconds),
        },
        "components": {
            "fusion_macro_manifest": str(macro_manifest),
            "audio_driven_fusion_manifest": str(audio_manifest),
            "fusion_usd_hydra_manifest": str(usd_manifest),
            "fairlight_clip_eq_manifest": str(eq_manifest),
            "fairlight_chainfx_manifest": str(chainfx_manifest),
        },
        "continuity": {
            "macro_asset_refs": _source_asset_refs(macro_payload.get("sources", [])),
            "audio_asset_refs": _source_asset_refs(audio_payload.get("sources", [])),
            "usd_asset_refs": _source_asset_refs(usd_payload.get("stage_references", [])),
            "eq_asset_refs": _source_asset_refs(eq_payload.get("clips", [])),
            "chainfx_asset_refs": _source_asset_refs(chainfx_payload.get("clips", [])),
            "fusion_parameter_sets": len(audio_payload.get("automation_sets", [])),
            "usd_layer_count": len(usd_payload.get("usd_layers", [])),
            "fairlight_eq_clip_count": len(eq_payload.get("clips", [])),
            "chainfx_clip_count": len(chainfx_payload.get("clips", [])),
            "shared_asset_refs": _shared_asset_refs(
                macro_payload,
                audio_payload,
                usd_payload,
                eq_payload,
                chainfx_payload,
            ),
        },
        "review_hints": [
            "open the review mp4 and confirm the same real clips drive Fusion, USD, and Fairlight manifests",
            "compare audio-driven keyframes with Fairlight EQ and level-match diagnostics",
            "confirm USD stage references preserve the same asset_ref values used by macro and audio manifests",
            "watch two real-media reproductions before marking this blended scene stable",
        ],
    }
    output.with_suffix(".blended_fusion_audio_usd_fairlight.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return str(output)


def _write_review_video(inputs: list[Path], output: Path, *, max_seconds: float) -> None:
    review_dir = output.parent / f"{output.stem}_review_clips"
    review_dir.mkdir(parents=True, exist_ok=True)
    clips = []
    for index, source in enumerate(inputs):
        clip = review_dir / f"{index:02d}_{_safe_stem(source)}.mp4"
        _trim_normalized(source, clip, max_seconds=max_seconds)
        clips.append(clip)
    concat_file = review_dir / "concat.txt"
    concat_file.write_text("".join(f"file '{clip.as_posix()}'\n" for clip in clips), encoding="utf-8")
    proc = subprocess.run(
        ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat_file), "-c", "copy", str(output)],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg review concat failed: {proc.stderr[-1000:]}")


def _trim_normalized(source: Path, output: Path, *, max_seconds: float) -> None:
    proc = subprocess.run(
        [
            "ffmpeg", "-y", "-i", str(source), "-t", f"{max_seconds:.3f}",
            "-vf", "scale='if(gte(iw,ih),320,-2)':'if(gte(iw,ih),-2,320)'",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-movflags", "+faststart", str(output),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg review clip failed for {source}: {proc.stderr[-1000:]}")


def _source_asset_refs(items: list[dict[str, Any]]) -> list[str]:
    return [str(item["asset_ref"]) for item in items if item.get("asset_ref")]


def _shared_asset_refs(*payloads: dict[str, Any]) -> list[str]:
    ref_sets = []
    for payload in payloads:
        candidates: list[dict[str, Any]] = []
        for key in ("sources", "stage_references", "clips"):
            value = payload.get(key)
            if isinstance(value, list):
                candidates.extend(item for item in value if isinstance(item, dict))
        refs = set(_source_asset_refs(candidates))
        if refs:
            ref_sets.append(refs)
    if not ref_sets:
        return []
    return sorted(set.intersection(*ref_sets))


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _safe_stem(path: Path) -> str:
    return "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in path.stem) or "media"


__all__ = ["render_blended_fusion_audio_usd_fairlight_scene"]
