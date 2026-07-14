"""Resolve 21 blended ingest, search, audio-folder, and graphics review scene."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import cv2

from gemia.audio.fairlight_folder_tracks import render_fairlight_folder_tracks_manifest
from gemia.video.intellisearch import index_real_media, search_media_index
from gemia.video.krokodove_motion_pack import render_krokodove_motion_pack
from gemia.video.photo_page import render_photo_page_batch_raw_grade
from gemia.video.slate_id import render_slate_id_metadata_plan


@dataclass(frozen=True)
class BlendedIngestSearchGraphicsSceneResult:
    output_path: str
    metadata_path: str


def render_blended_ingest_search_graphics_scene(
    video_paths: Iterable[str],
    output_path: str,
    *,
    still_image_paths: Iterable[str] | None = None,
    query: str = "real footage",
    preset: str = "orbit_grid",
    title: str = "Lumeri ingest review",
    max_seconds: float = 1.0,
    temp_dir: str | Path | None = None,
) -> str:
    """Compose ingest stills, slate metadata, IntelliSearch, Fairlight folders, and graphics."""
    videos = [Path(path).expanduser().resolve() for path in video_paths]
    if not videos:
        raise ValueError("video_paths cannot be empty")
    for video in videos:
        if not video.exists():
            raise FileNotFoundError(f"Video file not found: {video}")
        if not video.is_file():
            raise OSError(f"Video path is not a file: {video}")
    output = Path(output_path).expanduser().resolve()
    if max_seconds <= 0:
        raise ValueError("max_seconds must be greater than 0")

    stills = [Path(path).expanduser().resolve() for path in still_image_paths or []]
    for still in stills:
        if not still.exists():
            raise FileNotFoundError(f"Still image file not found: {still}")
        if not still.is_file():
            raise OSError(f"Still image path is not a file: {still}")

    output.parent.mkdir(parents=True, exist_ok=True)
    work_dir = Path(temp_dir).expanduser().resolve() if temp_dir else output.parent / f"{output.stem}_ingest_search_graphics_steps"
    work_dir.mkdir(parents=True, exist_ok=True)
    diagnostics: list[dict[str, Any]] = []

    generated_stills: list[Path] = []
    if not stills:
        generated_stills = _extract_review_stills(videos, work_dir / "stills", diagnostics=diagnostics)
        stills = generated_stills
    if not stills:
        raise RuntimeError("No still images available for blended ingest scene")

    photo_manifest = Path(render_photo_page_batch_raw_grade(
        [str(path) for path in stills],
        str(work_dir / "photo_page_batch"),
        preset="warm",
        exposure_stops=0.12,
        temperature_shift=-4.0,
        contact_sheet_columns=min(3, max(1, len(stills))),
    ))
    photo_payload = _read_json(photo_manifest)

    slate_video = work_dir / "01_slate_metadata.mp4"
    render_slate_id_metadata_plan(
        str(videos[0]),
        str(slate_video),
        frame_step=4,
        max_long_edge=180,
        min_confidence=0.35,
        metadata_hints={"scene": videos[0].stem, "roll": "ingest"},
    )
    slate_metadata = _read_json(slate_video.with_suffix(".slate_id.json"))

    intelli_index = work_dir / "02_intellisearch_index.json"
    index_real_media(
        [str(path) for path in videos],
        str(intelli_index),
        extra_labels=["resolve21 blended ingest search graphics scene", title],
        max_samples=4,
    )
    search_output = work_dir / "03_intellisearch_search.json"
    search_result = search_media_index(str(intelli_index), query, output_path=str(search_output), limit=5)

    fairlight_manifest = Path(render_fairlight_folder_tracks_manifest(
        _audio_assets_for(videos),
        str(work_dir / "04_fairlight_folder_tracks.json"),
        timeline_id="resolve21_blended_ingest_search_graphics_scene",
    ))

    render_krokodove_motion_pack(
        str(videos[0]),
        str(output),
        preset=preset,
        title=title,
        max_seconds=max_seconds,
    )
    krokodove_metadata = output.with_suffix(".krokodove_motion_pack.json")

    metadata_path = output.with_suffix(".blended_ingest_search_graphics.json")
    metadata = {
        "schema_version": 1,
        "effect": "resolve21_blended_ingest_search_graphics_scene",
        "rendered_at": datetime.now(timezone.utc).isoformat(),
        "source_video_paths": [str(path) for path in videos],
        "provided_still_image_paths": [str(path) for path in still_image_paths or []],
        "generated_still_paths": [str(path) for path in generated_stills],
        "still_count": len(stills),
        "output_path": str(output),
        "components": {
            "photo_page_manifest": str(photo_manifest),
            "photo_contact_sheet": photo_payload.get("contact_sheet", {}).get("path"),
            "slate_video": str(slate_video),
            "slate_metadata": str(slate_video.with_suffix(".slate_id.json")),
            "intellisearch_index": str(intelli_index),
            "intellisearch_search": str(search_output),
            "fairlight_folder_tracks": str(fairlight_manifest),
            "graphics_review_video": str(output),
            "graphics_metadata": str(krokodove_metadata),
        },
        "search": {
            "query": query,
            "match_count": int(search_result.match_count),
            "top_matches": search_result.matches[:3],
        },
        "slate_preview_kind": slate_metadata.get("preview_kind"),
        "diagnostics": diagnostics,
        "review_hints": [
            "confirm photo-page contact sheet uses real footage or supplied stills",
            "check slate metadata before approving bin ingest",
            "review IntelliSearch top matches against the requested query",
            "confirm Fairlight folder roles keep dialogue and music assets organized",
            "watch the Fusion/Krokodove graphics review clip twice before marking stable",
        ],
    }
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return str(output)


def _extract_review_stills(videos: list[Path], output_dir: Path, *, diagnostics: list[dict[str, Any]]) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    stills: list[Path] = []
    for video_index, video in enumerate(videos):
        cap = cv2.VideoCapture(str(video))
        try:
            if not cap.isOpened():
                diagnostics.append({"severity": "warning", "code": "video_unreadable_for_stills", "path": str(video)})
                continue
            frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
            positions = _sample_positions(frame_count)
            for sample_index, position in enumerate(positions):
                if position > 0:
                    cap.set(cv2.CAP_PROP_POS_FRAMES, position)
                ok, frame = cap.read()
                if not ok or frame is None:
                    diagnostics.append({"severity": "warning", "code": "frame_extract_failed", "path": str(video), "frame": position})
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
    return [max(0, int(frame_count * 0.25)), max(1, int(frame_count * 0.75))]


def _audio_assets_for(videos: list[Path]) -> list[dict[str, str]]:
    roles = ["dialogue", "music", "ambience", "sfx"]
    assets = []
    for index, video in enumerate(videos):
        role = roles[index % len(roles)]
        assets.append({"path": str(video), "role": role, "label": f"{role} source {index + 1}: {video.stem}"})
    return assets


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _safe_stem(path: Path) -> str:
    return "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in path.stem) or "media"


__all__ = ["BlendedIngestSearchGraphicsSceneResult", "render_blended_ingest_search_graphics_scene"]
