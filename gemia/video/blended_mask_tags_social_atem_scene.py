"""Resolve 21 blended mask, tags, social, and ATEM review scene."""
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from gemia.video.atem_mini_import import render_atem_mini_project_import_timeline_manifest
from gemia.video.magic_mask_cache import render_magic_mask_render_in_place_cache_manifest
from gemia.video.media_pool_tags import render_media_pool_rating_tagging_columns_manifest
from gemia.video.social_delivery import render_vertical_social_resolution_delivery_manifest
from gemia.video.social_upload import render_social_media_upload_preset_manifest


def render_blended_mask_tags_social_atem_scene(
    video_paths: Iterable[str],
    output_path: str,
    *,
    scene_id: str = "resolve21_mask_tags_social_atem_scene",
    max_seconds: float = 1.0,
    temp_dir: str | Path | None = None,
) -> str:
    """Create a fused real-media review package across Resolve 21 batch 008."""
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
    work_dir = Path(temp_dir).expanduser().resolve() if temp_dir else output.parent / f"{output.stem}_mask_tags_social_atem_steps"
    work_dir.mkdir(parents=True, exist_ok=True)
    inputs = [str(path) for path in videos]

    magic_manifest = Path(render_magic_mask_render_in_place_cache_manifest(
        inputs, str(work_dir / "01_magic_mask_cache"), package_id=f"{scene_id}:magic_mask_cache"
    ))
    tags_manifest = Path(render_media_pool_rating_tagging_columns_manifest(
        inputs, str(work_dir / "02_media_pool_tags"), package_id=f"{scene_id}:media_pool_tags", default_rating=4, scene_label=scene_id
    ))
    social_manifest = Path(render_vertical_social_resolution_delivery_manifest(
        inputs, str(work_dir / "03_social_delivery"), package_id=f"{scene_id}:social_delivery", reframing_mode="smart_reframe"
    ))
    upload_manifest = Path(render_social_media_upload_preset_manifest(
        inputs, str(work_dir / "04_social_upload"), package_id=f"{scene_id}:social_upload", privacy="draft", compression_profile="balanced"
    ))
    atem_manifest = Path(render_atem_mini_project_import_timeline_manifest(
        inputs, str(work_dir / "05_atem_import"), package_id=f"{scene_id}:atem_import", project_name=scene_id
    ))

    _write_review_video(videos, output, max_seconds=max_seconds)
    magic_payload = _read_json(magic_manifest)
    tags_payload = _read_json(tags_manifest)
    social_payload = _read_json(social_manifest)
    upload_payload = _read_json(upload_manifest)
    atem_payload = _read_json(atem_manifest)
    metadata = {
        "schema_version": 1,
        "effect": "resolve21_blended_mask_tags_social_atem_scene",
        "rendered_at": datetime.now(timezone.utc).isoformat(),
        "scene": {
            "scene_id": scene_id,
            "source_video_paths": [str(path) for path in videos],
            "output_path": str(output),
            "clip_count": len(videos),
            "max_seconds": float(max_seconds),
        },
        "components": {
            "magic_mask_manifest": str(magic_manifest),
            "media_pool_tags_manifest": str(tags_manifest),
            "vertical_social_delivery_manifest": str(social_manifest),
            "social_upload_manifest": str(upload_manifest),
            "atem_mini_import_manifest": str(atem_manifest),
        },
        "continuity": {
            "magic_asset_refs": _refs(magic_payload.get("sources", [])),
            "tag_asset_refs": _refs(tags_payload.get("assets", [])),
            "social_asset_refs": _refs(social_payload.get("sources", [])),
            "upload_asset_refs": _refs(upload_payload.get("assets", [])),
            "atem_asset_refs": _refs(atem_payload.get("iso_sources", [])),
            "shared_asset_refs": _shared_refs(magic_payload, tags_payload, social_payload, upload_payload, atem_payload),
            "mask_cache_entry_count": len(magic_payload.get("cache_entries", [])),
            "tagged_asset_count": len(tags_payload.get("assets", [])),
            "social_render_job_count": len(social_payload.get("render_jobs", [])),
            "upload_job_count": len(upload_payload.get("upload_jobs", [])),
            "atem_program_edit_count": len(atem_payload.get("timelines", {}).get("program_timeline", {}).get("edits", [])),
        },
        "review_hints": [
            "open the review mp4 and confirm the same real clips drive mask, tags, social, upload, and ATEM manifests",
            "compare Magic Mask cache refs with media-pool rating/tag refs before social delivery",
            "verify social upload jobs do not serialize credentials and ATEM relink items remain ready",
            "run two real-video reproductions before marking this blended scene stable",
        ],
    }
    output.with_suffix(".blended_mask_tags_social_atem.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
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
    proc = subprocess.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat_file), "-c", "copy", str(output)], capture_output=True, text=True, check=False)
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


def _refs(items: list[dict[str, Any]]) -> list[str]:
    return [str(item["asset_ref"]) for item in items if isinstance(item, dict) and item.get("asset_ref")]


def _shared_refs(*payloads: dict[str, Any]) -> list[str]:
    ref_sets = []
    for payload in payloads:
        refs: set[str] = set()
        for key in ("sources", "assets", "iso_sources"):
            value = payload.get(key)
            if isinstance(value, list):
                refs.update(_refs(value))
        if refs:
            ref_sets.append(refs)
    return sorted(set.intersection(*ref_sets)) if ref_sets else []


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _safe_stem(path: Path) -> str:
    return "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in path.stem) or "media"


__all__ = ["render_blended_mask_tags_social_atem_scene"]
