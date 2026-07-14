"""Resolve 21 blended album, MultiMaster, graph, group, and VR review scene."""
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import cv2

from gemia.video.group_versions import render_group_versions_color_workflow
from gemia.video.immersive_vr import render_immersive_vr_delivery_manifest
from gemia.video.layer_graph_versions import render_layer_list_node_graph_versions
from gemia.video.multimaster import render_multimaster_trim_pass_manager
from gemia.video.photo_album import render_photo_album_lightbox_tether_ingest


def render_blended_album_multimaster_vr_scene(
    video_paths: Iterable[str],
    output_path: str,
    *,
    still_image_paths: Iterable[str] | None = None,
    scene_id: str = "resolve21_album_multimaster_vr_scene",
    album_name: str = "Resolve 21 album review",
    max_seconds: float = 1.0,
    temp_dir: str | Path | None = None,
) -> str:
    """Create a fused real-media review scene across batch 005 primitives."""
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
    work_dir = Path(temp_dir).expanduser().resolve() if temp_dir else output.parent / f"{output.stem}_album_multimaster_vr_steps"
    work_dir.mkdir(parents=True, exist_ok=True)

    stills = [Path(path).expanduser().resolve() for path in still_image_paths or []]
    for still in stills:
        if not still.exists():
            raise FileNotFoundError(f"Still image file not found: {still}")
        if not still.is_file():
            raise OSError(f"Still image path is not a file: {still}")
    generated_stills: list[Path] = []
    if not stills:
        generated_stills = _extract_stills(videos, work_dir / "album_stills")
        stills = generated_stills
    if not stills:
        raise RuntimeError("No still images available for blended album scene")

    album_manifest = Path(render_photo_album_lightbox_tether_ingest(
        [str(path) for path in stills],
        str(work_dir / "01_photo_album_lightbox_tether"),
        album_name=album_name,
        album_tags=["resolve21", "album", "vr", "multimaster"],
        default_rating=4,
        lightbox_columns=min(4, max(1, len(stills))),
        tether_session={"session_id": f"{scene_id}:tether", "camera_model": "offline real-video frame capture"},
    ))
    multimaster_manifest = Path(render_multimaster_trim_pass_manager(
        str(videos[0]),
        str(work_dir / "02_multimaster_trim_pass"),
        timeline_id=f"{scene_id}:timeline",
        review_proxy_long_edge=240,
        trim_passes=[
            {"id": "hdr_album_master", "target": "hdr10_pq", "start_trim_seconds": 0.0, "end_trim_seconds": 0.0, "peak_nits": 1000},
            {"id": "sdr_lightbox_trim", "target": "sdr_rec709", "start_trim_seconds": 0.02, "end_trim_seconds": 0.02, "peak_nits": 100},
        ],
    ))
    layer_manifest = Path(render_layer_list_node_graph_versions(
        [str(path) for path in videos],
        str(work_dir / "03_layer_graph_versions"),
        graph_id=f"{scene_id}:layer_graph",
    ))
    group_manifest = Path(render_group_versions_color_workflow(
        [str(path) for path in videos],
        str(work_dir / "04_group_versions_color"),
        group_id=f"{scene_id}:color_groups",
    ))
    vr_manifest = Path(render_immersive_vr_delivery_manifest(
        [str(path) for path in videos],
        str(work_dir / "05_immersive_vr_delivery"),
        package_id=f"{scene_id}:vr_package",
        target_platforms=["headset_review", "web360", "archive_master"],
    ))

    multimaster_payload = _read_json(multimaster_manifest)
    proxy_paths = [Path(item["review_proxy_path"]) for item in multimaster_payload.get("deliverables", []) if item.get("review_proxy_path")]
    _write_review_video(proxy_paths or [videos[0]], output, max_seconds=max_seconds)

    album_payload = _read_json(album_manifest)
    layer_payload = _read_json(layer_manifest)
    group_payload = _read_json(group_manifest)
    vr_payload = _read_json(vr_manifest)
    metadata_path = output.with_suffix(".blended_album_multimaster_vr.json")
    metadata = {
        "schema_version": 1,
        "effect": "resolve21_blended_album_multimaster_vr_scene",
        "rendered_at": datetime.now(timezone.utc).isoformat(),
        "scene": {
            "scene_id": scene_id,
            "source_video_paths": [str(path) for path in videos],
            "provided_still_image_paths": [str(path) for path in still_image_paths or []],
            "generated_still_paths": [str(path) for path in generated_stills],
            "output_path": str(output),
            "still_count": len(stills),
        },
        "components": {
            "album_manifest": str(album_manifest),
            "album_contact_sheet": album_payload.get("lightbox", {}).get("path"),
            "multimaster_manifest": str(multimaster_manifest),
            "multimaster_review_proxies": [str(path) for path in proxy_paths],
            "layer_graph_manifest": str(layer_manifest),
            "group_versions_manifest": str(group_manifest),
            "immersive_vr_manifest": str(vr_manifest),
        },
        "continuity": {
            "album_asset_count": album_payload.get("album", {}).get("asset_count", 0),
            "trim_pass_count": len(multimaster_payload.get("deliverables", [])),
            "layer_asset_refs": layer_payload.get("graph", {}).get("asset_refs", []),
            "group_count": group_payload.get("workflow", {}).get("group_count", 0),
            "vr_deliverable_count": vr_payload.get("package", {}).get("deliverable_count", 0),
        },
        "review_hints": [
            "open the album contact sheet and confirm all stills came from real source media or provided stills",
            "compare MultiMaster HDR and SDR proxy durations before approval",
            "confirm layer graph, group versions, and VR manifests share stable source asset refs",
            "watch the stitched review video twice before marking the blended scene reproducible",
        ],
    }
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return str(output)


def _extract_stills(videos: list[Path], output_dir: Path) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    stills: list[Path] = []
    for video_index, video in enumerate(videos):
        cap = cv2.VideoCapture(str(video))
        try:
            if not cap.isOpened():
                continue
            frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
            positions = _sample_positions(frame_count)
            for sample_index, position in enumerate(positions):
                if position > 0:
                    cap.set(cv2.CAP_PROP_POS_FRAMES, position)
                ok, frame = cap.read()
                if not ok or frame is None:
                    continue
                still = output_dir / f"{video_index:02d}_{sample_index:02d}_{_safe_stem(video)}.png"
                if not cv2.imwrite(str(still), frame):
                    raise OSError(f"Could not write extracted still: {still}")
                stills.append(still)
        finally:
            cap.release()
    return stills


def _sample_positions(frame_count: int) -> list[int]:
    if frame_count <= 2:
        return [0, 1]
    return [max(0, int(frame_count * 0.2)), max(1, int(frame_count * 0.7))]


def _write_review_video(inputs: list[Path], output: Path, *, max_seconds: float) -> None:
    clips = []
    review_dir = output.parent / f"{output.stem}_review_clips"
    review_dir.mkdir(parents=True, exist_ok=True)
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


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _safe_stem(path: Path) -> str:
    return "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in path.stem) or "media"


__all__ = ["render_blended_album_multimaster_vr_scene"]
