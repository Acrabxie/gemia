"Resolve 21 blended immersive delivery, PiP, and script review scene."
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from gemia.video.apple_immersive_foveated import render_apple_immersive_foveated_rendering_manifest
from gemia.video.finaldraft_intelliscript import render_finaldraft_intelliscript_ingest_manifest
from gemia.video.mainconcept_delivery import render_mainconcept_h265_mvhevc_delivery_manifest
from gemia.video.panomap_ilpd import render_panomap_ilpd_stereo_retarget_manifest
from gemia.video.picture_in_picture_resolvefx import render_picture_in_picture_resolvefx_layout


def render_blended_immersive_delivery_pip_script_scene(
    video_paths: Iterable[str],
    output_path: str,
    *,
    scene_id: str = "resolve21_immersive_delivery_pip_script_scene",
    script_text: str | None = None,
    max_seconds: float = 1.0,
    temp_dir: str | Path | None = None,
) -> str:
    """Create a fused real-media review package across Resolve 21 batch 007."""
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
    work_dir = Path(temp_dir).expanduser().resolve() if temp_dir else output.parent / f"{output.stem}_immersive_delivery_pip_script_steps"
    work_dir.mkdir(parents=True, exist_ok=True)
    inputs = [str(path) for path in videos]
    pip_inputs = inputs if len(inputs) >= 2 else [inputs[0], inputs[0]]

    apple_manifest = Path(render_apple_immersive_foveated_rendering_manifest(
        inputs,
        str(work_dir / "01_apple_immersive_foveated"),
        package_id=f"{scene_id}:apple_immersive",
    ))
    delivery_manifest = Path(render_mainconcept_h265_mvhevc_delivery_manifest(
        inputs,
        str(work_dir / "02_mainconcept_delivery"),
        package_id=f"{scene_id}:delivery",
        target_platforms=["apple_immersive", "spatial_review", "archive_master"],
    ))
    panomap_manifest = Path(render_panomap_ilpd_stereo_retarget_manifest(
        inputs,
        str(work_dir / "03_panomap_ilpd"),
        package_id=f"{scene_id}:panomap",
    ))
    pip_manifest = Path(render_picture_in_picture_resolvefx_layout(
        pip_inputs,
        str(work_dir / "04_picture_in_picture"),
        package_id=f"{scene_id}:pip",
    ))
    script_manifest = Path(render_finaldraft_intelliscript_ingest_manifest(
        inputs,
        str(work_dir / "05_finaldraft_intelliscript"),
        package_id=f"{scene_id}:script",
        script_text=script_text,
        reel_name=f"{scene_id}:reel",
    ))

    _write_review_video(videos, output, max_seconds=max_seconds)
    apple_payload = _read_json(apple_manifest)
    delivery_payload = _read_json(delivery_manifest)
    panomap_payload = _read_json(panomap_manifest)
    pip_payload = _read_json(pip_manifest)
    script_payload = _read_json(script_manifest)
    metadata = {
        "schema_version": 1,
        "effect": "resolve21_blended_immersive_delivery_pip_script_scene",
        "rendered_at": datetime.now(timezone.utc).isoformat(),
        "scene": {
            "scene_id": scene_id,
            "source_video_paths": [str(path) for path in videos],
            "output_path": str(output),
            "clip_count": len(videos),
            "max_seconds": float(max_seconds),
            "pip_reused_single_source": len(videos) == 1,
        },
        "components": {
            "apple_immersive_manifest": str(apple_manifest),
            "mainconcept_delivery_manifest": str(delivery_manifest),
            "panomap_ilpd_manifest": str(panomap_manifest),
            "picture_in_picture_manifest": str(pip_manifest),
            "finaldraft_intelliscript_manifest": str(script_manifest),
        },
        "continuity": {
            "apple_asset_refs": _source_asset_refs(apple_payload.get("sources", [])),
            "delivery_asset_refs": _source_asset_refs(delivery_payload.get("sources", [])),
            "panomap_asset_refs": _source_asset_refs(panomap_payload.get("sources", [])),
            "pip_asset_refs": _source_asset_refs(pip_payload.get("sources", [])),
            "script_asset_refs": _source_asset_refs(script_payload.get("sources", [])),
            "shared_asset_refs": _shared_asset_refs(apple_payload, delivery_payload, panomap_payload, script_payload),
            "render_pass_count": len(apple_payload.get("render_passes", [])),
            "deliverable_count": len(delivery_payload.get("deliverables", [])),
            "retarget_count": len(panomap_payload.get("retargets", [])),
            "pip_layout_count": len(pip_payload.get("layouts", [])),
            "script_assignment_count": len(script_payload.get("assignments", [])),
        },
        "review_hints": [
            "open the review mp4 and confirm the same real clips drive immersive, delivery, Panomap, PiP, and script manifests",
            "compare MV-HEVC delivery intent with Apple Immersive foveated render passes before export",
            "confirm Panomap/ILPD retarget values and PiP inset placement do not conflict with scripted beats",
            "run two real-media reproductions before marking this blended scene stable",
        ],
    }
    output.with_suffix(".blended_immersive_delivery_pip_script.json").write_text(
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
    return [str(item["asset_ref"]) for item in items if isinstance(item, dict) and item.get("asset_ref")]


def _shared_asset_refs(*payloads: dict[str, Any]) -> list[str]:
    ref_sets = []
    for payload in payloads:
        value = payload.get("sources")
        if isinstance(value, list):
            refs = set(_source_asset_refs(value))
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


__all__ = ["render_blended_immersive_delivery_pip_script_scene"]
