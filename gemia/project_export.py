"""Full-quality multi-track export from ProjectStore state.

Reads the current project (video + overlay + audio tracks) and produces a
final H.264/AAC MP4 via a three-pass strategy:
  pass 1 — render base video track (all enabled video clips concatenated,
            full resolution, no overlay)
  pass 2 — apply overlay track clips (image overlays, Lottie motion graphics,
            and text captions) via a complex ffmpeg filtergraph on top of the base video
  pass 3 — build the final audio and mux it onto the composited video:
            every audio source — audio-track clips plus the embedded audio
            of video-track clips (unless the clip is muted) — is trimmed to
            its source range, gained (dB->linear), faded, positioned on the
            timeline via adelay, and mixed with amix (mirrors mix_audio).

Backward compatible: a project with no audio anywhere (no audio clips and
no video clip carrying a real, unmuted audio stream) keeps the silent
``-an`` path and exports exactly as before.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from gemia.compat import ffmpeg_path, ffprobe_path
from gemia.project_model import IMAGE_DURATION, normalize_project
from gemia.render_receipt import (
    AUDIO_MASTER_LRA_LU,
    AUDIO_MASTER_TARGET_LUFS,
    AUDIO_MASTER_TRUE_PEAK_DBTP,
    build_graph_identity,
    build_render_receipt,
    render_id_for,
)

try:
    from gemia.video.fonts import resolve_font_path as _resolve_font_path
except Exception:  # pragma: no cover - fonts module optional
    _resolve_font_path = None
from gemia.project_store import ProjectStore
from gemia.video.lottie_renderer import save_lottie_frame_png, select_lottie_renderer
from lumerai.export_support import clip_dropped_fields


class ProjectExportError(RuntimeError):
    """Raised when the project cannot be exported."""

    def __init__(self, code: str, message: str, *, detail: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.detail = detail


_QUALITY_PROFILES: dict[str, dict[str, str]] = {
    "4k": {"crf": "16", "preset": "slow"},
    "1080p": {"crf": "18", "preset": "slow"},
    "720p": {"crf": "20", "preset": "medium"},
    "480p": {"crf": "23", "preset": "medium"},
    "draft": {"crf": "28", "preset": "veryfast"},
}

_HEX_COLOR_RE = re.compile(r"^#([0-9a-fA-F]{6})$")

# Butt-joint tolerance — mirrors lumerai.patches.EPSILON (kept literal here so
# the export module stays import-light; a drift test pins the two together).
_EPSILON = 1e-3

# _render_video_segment floors every segment to 0.1 s (``-t max(d, 0.1)``), so
# transition surgery must never leave a segment below this floor or the floor
# silently *lengthens* it and violates INVARIANT T1 (plan §5.1).
_SEGMENT_FLOOR = 0.1


# ── public entrypoint ────────────────────────────────────────────────────────


def export_project(
    store: ProjectStore,
    project_id: str,
    *,
    output_root: str | Path,
    quality: str = "1080p",
    label: str = "export",
    timeout_sec: int = 300,
    max_long_edge: int | None = None,
    verify_decode: bool = False,
    reality_contract: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Export the current project state as a full-quality H.264 MP4.

    Returns a manifest dict with ``export_path``, ``duration``,
    ``resolution``, and audit metadata.
    """
    if quality not in _QUALITY_PROFILES:
        raise ProjectExportError(
            "bad_quality",
            f"unknown quality {quality!r}; valid: {', '.join(_QUALITY_PROFILES)}",
        )

    project = normalize_project(store.load(project_id))

    # Pass 0 — comp_ref freshness (docs/timeline-canonical-plan.md §3.3): comp
    # clips are LIVE references, so a stale lumenframe doc hash triggers a
    # re-render + asset re-point (itself an undoable patch) before pass 1 reads
    # any source file. Zero comp clips → zero overhead (no lumenframe import).
    comp_refreshed = _refresh_comp_assets(store, project_id, project)
    if comp_refreshed:
        project = normalize_project(store.load(project_id))

    meta = store.load_meta(project_id)
    patch_seq = int(meta.get("patch_seq") or 0)
    graph_identity = build_graph_identity(project)

    timeline = (
        project.get("timeline") if isinstance(project.get("timeline"), dict) else {}
    )
    fps = _pos(timeline.get("fps"), 30.0)
    width = max(2, int(_pos(timeline.get("width"), 1920.0)))
    height = max(2, int(_pos(timeline.get("height"), 1080.0)))
    # enforce even dimensions for yuv420p
    width = width + (width % 2)
    height = height + (height % 2)
    if max_long_edge is not None:
        width, height = _fit_size(width, height, max_long_edge=max_long_edge)

    assets = _build_asset_map(project)
    video_clips = _enabled_video_clips(project, assets)
    overlay_clips = _enabled_overlay_clips(project, assets)
    audio_clips = _enabled_audio_clips(project, assets)

    if not video_clips:
        raise ProjectExportError(
            "no_video_clips",
            "Project has no enabled video clips to export.",
        )

    output_dir = Path(output_root).expanduser().resolve() / "exports" / project_id
    output_dir.mkdir(parents=True, exist_ok=True)
    render_preset = f"{quality}-{width}x{height}"
    export_id = render_id_for(
        patch_seq=patch_seq,
        graph_hash=str(graph_identity["graph_hash"]),
        preset=render_preset,
        label=label,
    )
    export_path = output_dir / f"{export_id}.mp4"

    work_dir = store.renders_dir(project_id) / f"{export_id}.export-work"
    _reset_dir(work_dir)

    # The audio-inclusive master length (lumerai.patches._recompute_duration
    # already folds audio clips into timeline.duration). The composited video
    # is padded with black to reach it, so a music tail that runs past the last
    # video clip plays over black instead of an undefined over-run. For no-audio
    # / audio-shorter projects this equals the video end, so nothing changes.
    master_duration = _pos(timeline.get("duration"), 0.0)

    # Export honesty (docs/timeline-canonical-plan.md §4.2): record every
    # stored-but-unrendered field of every enabled clip BEFORE rendering, so
    # the manifest is honest even about clips that fail later checks.
    dropped_fields: list[dict[str, str]] = []
    for clip in timeline.get("clips") or []:
        if not isinstance(clip, dict) or not bool(clip.get("enabled", True)):
            continue
        for entry in clip_dropped_fields(clip):
            dropped_fields.append({"clip_id": str(clip.get("id") or ""), **entry})

    try:
        base_video, transition_records = _render_base_video(
            video_clips,
            assets,
            work_dir=work_dir,
            width=width,
            height=height,
            fps=fps,
            quality=quality,
            timeout_sec=timeout_sec,
            min_duration=master_duration,
        )
        # Transitions degraded at render time (plan §5.2) are silent drops too.
        for record in transition_records:
            if record.get("status") == "degraded":
                dropped_fields.append(
                    {
                        "clip_id": str(record.get("clip_id") or ""),
                        "field": "transition_after",
                        "reason": str(record.get("reason") or "not_rendered"),
                    }
                )

        # Pass 3 input: gather every audio source first. When there is none
        # (no audio clips and no unmuted video clip with a real audio stream),
        # we keep the exact silent path below — backward compatible.
        audio_sources = _collect_audio_sources(video_clips, audio_clips, assets)

        # The composited (silent) video either goes straight to export_path
        # (no audio → current behaviour) or to a work intermediate we then mux.
        composited = (work_dir / "composited.mp4") if audio_sources else export_path
        if overlay_clips:
            _apply_overlays(
                base_video,
                overlay_clips,
                assets,
                output=composited,
                width=width,
                height=height,
                fps=fps,
                quality=quality,
                timeout_sec=timeout_sec,
            )
        elif audio_sources:
            # Reuse the silent base directly as the video to mux against.
            composited = base_video
        else:
            # No overlays, no audio — copy the base video to final location.
            _run_ffmpeg(
                [
                    ffmpeg_path(),
                    "-y",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-i",
                    str(base_video),
                    "-c",
                    "copy",
                    "-movflags",
                    "+faststart",
                    str(export_path),
                ],
                output=export_path,
                timeout_sec=timeout_sec,
            )

        if audio_sources:
            duck_map = {
                str(t.get("id")): str(t.get("duck_under"))
                for t in (timeline.get("tracks") or [])
                if isinstance(t, dict) and t.get("duck_under")
            }
            _mux_audio_onto_video(
                composited,
                audio_sources,
                output=export_path,
                work_dir=work_dir,
                timeout_sec=timeout_sec,
                duck_map=duck_map,
                master_duration=master_duration,
            )
    finally:
        _cleanup_dir(work_dir)

    probe = _ffprobe(export_path)
    duration = _probe_duration(probe)
    resolution = _probe_resolution(probe) or {"width": width, "height": height}
    decode_check = (
        _full_decode_check(export_path, timeout_sec=timeout_sec)
        if verify_decode
        else {"status": "not_run"}
    )
    audio_analysis = (
        _measure_loudness(export_path, timeout_sec=timeout_sec)
        if verify_decode
        else {"status": "not_run"}
    )
    degradations = [
        dict(record)
        for record in transition_records
        if str(record.get("status") or "") == "degraded"
    ]
    if verify_decode:
        from gemia.reality_contract import (
            MAX_MEDIA_BUDGET_USD,
            normalize_reality_contract,
            render_expectations,
        )

        contract = normalize_reality_contract(
            reality_contract,
            hard_cap_usd=MAX_MEDIA_BUDGET_USD,
        )
        expected = render_expectations(
            contract,
            timeline_duration=master_duration or duration,
        )
    else:
        expected = {
            "duration": master_duration or duration,
            "fps": fps,
            "width": width,
            "height": height,
            "has_audio": bool(audio_sources),
        }
    receipt = build_render_receipt(
        project=project,
        project_id=project_id,
        patch_seq=patch_seq,
        preset=render_preset,
        render_id=export_id,
        output_path=export_path,
        probe=probe,
        decode_check=decode_check,
        dropped_fields=dropped_fields,
        degradations=degradations,
        expected=expected,
        audio_analysis=audio_analysis,
        graph_identity=graph_identity,
    )
    receipt_path = store.renders_dir(project_id) / f"{export_id}.receipt.json"
    receipt["receipt_path"] = str(receipt_path)
    _write_json(receipt_path, receipt)

    manifest = {
        "export_id": export_id,
        "project_id": project_id,
        "patch_seq": patch_seq,
        "export_path": str(export_path),
        "duration": duration,
        "resolution": resolution,
        "quality": quality,
        "render_preset": render_preset,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "video_clips_rendered": len(video_clips),
        "overlay_clips_rendered": len(overlay_clips),
        "audio_clips_rendered": len(audio_clips),
        "audio_sources_rendered": len(audio_sources),
        "has_audio": bool(audio_sources),
        # Export honesty (plan §4.2 item 3 + §5.2): every stored field export
        # dropped, and the render/degrade outcome of every stored transition.
        "transitions_rendered": sum(
            1 for r in transition_records if r.get("status") == "rendered"
        ),
        "transitions": transition_records,
        "dropped_fields": dropped_fields,
        # Pass 0 honesty: every comp_ref asset that was re-rendered (or whose
        # source document went missing) on this export, with old/new hashes.
        "comp_refreshed": comp_refreshed,
        "graph_hash": graph_identity["graph_hash"],
        "source_manifest_hash": graph_identity["source_manifest_hash"],
        "render_semantics_version": graph_identity["render_semantics_version"],
        "render_receipt": receipt,
        "receipt_path": str(receipt_path),
        "machine_status": receipt["machine_status"],
        "machine_blockers": receipt["machine_blockers"],
    }
    manifest_path = store.renders_dir(project_id) / f"{export_id}.manifest.json"
    _write_json(manifest_path, manifest)
    manifest["manifest_path"] = str(manifest_path)
    return manifest


# ── pass 0: comp_ref freshness ───────────────────────────────────────────────


def _comp_ref_of(asset: dict[str, Any]) -> dict[str, Any] | None:
    metadata = asset.get("metadata") if isinstance(asset.get("metadata"), dict) else {}
    ref = metadata.get("comp_ref")
    return ref if isinstance(ref, dict) else None


def _refresh_comp_assets(
    store: ProjectStore,
    project_id: str,
    project: dict[str, Any],
) -> list[dict[str, Any]]:
    """Re-render stale ``comp_ref`` assets before pass 1 reads their files.

    Freshness = sha256 of the current ``lumenframe.json`` BYTES vs the
    ``comp_ref.doc_hash`` recorded at render time (hash decides; no mtime
    fast-path — one file hash per export is negligible). A stale window is
    re-rendered to a NEW content-addressed cache path and the asset re-pointed
    via ONE ``upsert_asset`` patch (undoable; old cache files stay in place).

    A comp that no longer covers ``comp_ref.t_out`` fails the export with
    ``ProjectExportError("comp_shrunk", …)`` naming the clip — silently
    changing ``clip.duration`` would ripple into the overlap/duration
    invariants and pass-3 audio positions (contract §3.3 shrink guard).
    """
    asset_map = {
        str(a.get("id")): a for a in project.get("assets") or [] if isinstance(a, dict)
    }
    timeline = (
        project.get("timeline") if isinstance(project.get("timeline"), dict) else {}
    )
    comp_clips: dict[str, list[str]] = {}
    for clip in timeline.get("clips") or []:
        if not isinstance(clip, dict) or not bool(clip.get("enabled", True)):
            continue
        asset = asset_map.get(str(clip.get("asset_id") or ""))
        if asset is None or _comp_ref_of(asset) is None:
            continue
        comp_clips.setdefault(str(asset.get("id")), []).append(
            str(clip.get("id") or "")
        )
    if not comp_clips:
        return []

    records: list[dict[str, Any]] = []
    doc_file = store.project_dir(project_id) / "lumenframe.json"
    if not doc_file.exists():
        # The comp document is gone: the cached render still exports, but the
        # live reference has degraded to a snapshot. Record honestly, don't fail.
        for asset_id, clip_ids in comp_clips.items():
            records.append(
                {
                    "asset_id": asset_id,
                    "clip_ids": clip_ids,
                    "status": "skipped",
                    "reason": "doc_missing",
                }
            )
        return records

    current_hash = "sha256:" + hashlib.sha256(doc_file.read_bytes()).hexdigest()
    stale = {
        asset_id: asset_map[asset_id]
        for asset_id in comp_clips
        if str((_comp_ref_of(asset_map[asset_id]) or {}).get("doc_hash"))
        != current_hash
    }
    if not stale:
        return []

    # Lazy imports: only a stale comp asset pays for lumenframe.
    try:
        from lumenframe.model import normalize_doc
        from lumenframe.compile import compile_to_layer_stack
        from lumenframe.render_range import export_range
        from lumenframe import timebase
    except ImportError as e:  # pragma: no cover - lumenframe ships with gemia
        raise ProjectExportError(
            "comp_refresh_failed",
            f"lumenframe modules unavailable for comp refresh: {e}",
        )

    try:
        doc = normalize_doc(json.loads(doc_file.read_text(encoding="utf-8")))
        stack = compile_to_layer_stack(doc, strict=False)
        fps = float(stack.fps)
        total_frames = int(stack.total_frames)
    except Exception as e:
        raise ProjectExportError(
            "comp_refresh_failed",
            f"cannot compile lumenframe doc for comp refresh: {e}",
        )

    renders_dir = store.renders_dir(project_id)
    renders_dir.mkdir(parents=True, exist_ok=True)
    ops: list[dict[str, Any]] = []
    for asset_id, asset in stale.items():
        ref = dict(_comp_ref_of(asset) or {})
        t_in = float(ref.get("t_in") or 0.0)
        t_out = float(ref.get("t_out") or 0.0)
        clip_ids = comp_clips[asset_id]
        if int(timebase.to_frame(t_out, fps)) > total_frames:
            raise ProjectExportError(
                "comp_shrunk",
                f"composition no longer covers [{t_in:g}, {t_out:g}) needed by "
                f"clip(s) {', '.join(clip_ids)}; trim the clip or re-window "
                "with lumen_comp_to_timeline",
                detail=clip_ids[0],
            )
        in_ms = int(round(t_in * 1000))
        out_ms = int(round(t_out * 1000))
        new_path = renders_dir / f"comp_{current_hash[7:19]}_{in_ms}_{out_ms}.mp4"
        if not (new_path.exists() and new_path.stat().st_size > 0):
            try:
                export_range(doc, t_in, t_out, str(new_path))
            except ValueError as e:
                raise ProjectExportError(
                    "comp_shrunk",
                    f"comp window [{t_in:g}, {t_out:g}) of clip(s) "
                    f"{', '.join(clip_ids)} is empty after the composition changed: {e}",
                    detail=clip_ids[0],
                )
            except Exception as e:
                raise ProjectExportError(
                    "comp_refresh_failed",
                    f"re-render of comp asset {asset_id} failed: {e}",
                    detail=asset_id,
                )
        old_hash = str(ref.get("doc_hash") or "")
        ref["doc_hash"] = current_hash
        ref["doc_hash_source"] = "file"
        ref["rendered_at"] = datetime.now(timezone.utc).isoformat()
        metadata = dict(asset.get("metadata") or {})
        metadata["comp_ref"] = ref
        # _upsert_asset merges {**existing, **asset}: only re-point the path and
        # provenance; duration/media_kind/etc. survive from the existing asset.
        ops.append(
            {
                "op": "upsert_asset",
                "asset": {
                    "id": asset_id,
                    "source_path": str(new_path),
                    "metadata": metadata,
                },
            }
        )
        records.append(
            {
                "asset_id": asset_id,
                "clip_ids": clip_ids,
                "old_hash": old_hash,
                "new_hash": current_hash,
                "source_path": str(new_path),
                "status": "refreshed",
            }
        )

    # One patch for the whole refresh = one undoable step; undo re-points
    # source_path at the OLD cache file, which still exists (append-only cache).
    store.apply_patches(
        project_id,
        [{"version": 1, "ops": ops}],
        session_id="export-pass0",
        script_hash="export-pass0",
    )
    return records


# ── pass 1: base video ───────────────────────────────────────────────────────


def _render_base_video(
    video_clips: list[dict[str, Any]],
    assets: dict[str, dict[str, Any]],
    *,
    work_dir: Path,
    width: int,
    height: int,
    fps: float,
    quality: str,
    timeout_sec: int,
    min_duration: float = 0.0,
) -> tuple[Path, list[dict[str, Any]]]:
    """Render the base video track to an intermediate file.

    ``min_duration`` is the audio-inclusive master length: when it exceeds the
    last video clip's end, the segment list gains a trailing black gap so the
    composited video reaches the timeline end (a music tail then plays over
    black instead of running past a frozen frame).

    Returns ``(base_path, transition_records)`` — one record per stored
    fade/dissolve stating whether it rendered or degraded to a hard cut and
    why (plan §5.2; the records feed the manifest's honesty fields).

    INVARIANT T1 (plan §5.1): the segment durations must sum EXACTLY to the
    timeline duration — pass 3 positions audio by ``adelay = clip.start`` on
    the assumption that the base video never shrinks or grows. Transitions are
    therefore rendered as duration-preserving segment surgery
    (``_plan_transitions``); a joint-xfade concat pipeline is forbidden.
    """
    profile = _QUALITY_PROFILES[quality]
    video_end = max(
        _pos(c["clip"].get("start"), 0.0) + _clip_duration(c["clip"])
        for c in video_clips
    )
    timeline_duration = max(video_end, _pos(min_duration, 0.0))
    segments: list[dict[str, Any]] = _timeline_segments(video_clips, timeline_duration)
    segments, transition_records = _plan_transitions(video_clips, segments, fps=fps)
    segment_paths: list[Path] = []

    for index, seg in enumerate(segments, start=1):
        seg_start = _pos(seg.get("start"), 0.0)
        seg_end = _pos(seg.get("end"), seg_start)
        seg_dur = max(seg_end - seg_start, 0.0)
        if seg_dur <= 0.001:
            continue

        window = seg.get("window")
        if isinstance(window, dict):
            # Dissolve window segment (plan §5.2): exactly d_eff seconds of
            # A's tail crossfaded into B's pre-handle. Duration-preserving by
            # construction: A's segment was shortened by the same d_eff.
            win_path = work_dir / (
                f"{index:04d}-xfade-{_slug(str(window.get('a_id') or 'a'))}"
                f"-{_slug(str(window.get('b_id') or 'b'))}.mp4"
            )
            _render_xfade_window(
                window,
                win_path,
                width=width,
                height=height,
                fps=fps,
                profile=profile,
                timeout_sec=timeout_sec,
            )
            segment_paths.append(win_path)
            continue

        item = seg.get("item")
        if item is None:
            gap = work_dir / f"{index:04d}-gap.mp4"
            _render_black_segment(
                gap,
                duration=seg_dur,
                width=width,
                height=height,
                fps=fps,
                timeout_sec=timeout_sec,
            )
            segment_paths.append(gap)
            continue

        clip = item["clip"]
        asset = item["asset"]
        source = Path(str(asset.get("source_path") or "")).expanduser()
        if not source.exists():
            raise ProjectExportError(
                "source_not_found",
                f"Source file missing: {source}",
                detail=str(source),
            )

        clip_start = _pos(clip.get("start"), 0.0)
        source_in = _pos(clip.get("source_in"), 0.0) + max(seg_start - clip_start, 0.0)
        source_out = _pos(clip.get("source_out"), source_in + seg_dur)
        trim_dur = min(seg_dur, max(source_out - source_in, 0.1))

        seg_path = work_dir / f"{index:04d}-{_slug(str(clip.get('id') or 'clip'))}.mp4"
        media_kind = str(asset.get("media_kind") or "video")
        _render_video_segment(
            source,
            seg_path,
            source_in=source_in,
            duration=trim_dur,
            width=width,
            height=height,
            fps=fps,
            profile=profile,
            media_kind=media_kind,
            timeout_sec=timeout_sec,
            vf_extra=str(seg.get("vf_extra") or ""),
        )
        segment_paths.append(seg_path)

    if not segment_paths:
        raise ProjectExportError("no_segments", "No renderable video segments found.")

    base = work_dir / "base.mp4"
    if len(segment_paths) == 1:
        segment_paths[0].rename(base)
    else:
        _concat_segments(segment_paths, base, timeout_sec=timeout_sec)
    return base, transition_records


def _render_video_segment(
    source: Path,
    output: Path,
    *,
    source_in: float,
    duration: float,
    width: int,
    height: int,
    fps: float,
    profile: dict[str, str],
    media_kind: str = "video",
    timeout_sec: int,
    vf_extra: str = "",
) -> None:
    vf = _video_filter(width=width, height=height, fps=fps) + vf_extra

    cmd = [
        ffmpeg_path(),
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
    ]

    if media_kind == "image":
        cmd.extend(
            [
                "-loop",
                "1",
                "-framerate",
                f"{fps:.6f}",
                "-t",
                f"{max(duration, 0.1):.6f}",
                "-i",
                str(source),
            ]
        )
    else:
        cmd.extend(
            [
                "-ss",
                f"{source_in:.6f}",
                "-t",
                f"{max(duration, 0.1):.6f}",
                "-i",
                str(source),
            ]
        )

    cmd.extend(
        [
            "-an",
            "-vf",
            vf,
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-crf",
            profile["crf"],
            "-preset",
            profile["preset"],
            "-movflags",
            "+faststart",
            str(output),
        ]
    )
    _run_ffmpeg(cmd, output=output, timeout_sec=timeout_sec)


def _render_black_segment(
    output: Path,
    *,
    duration: float,
    width: int,
    height: int,
    fps: float,
    timeout_sec: int,
) -> None:
    cmd = [
        ffmpeg_path(),
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        f"color=c=black:s={width}x{height}:r={fps}:d={max(duration, 0.1):.6f}",
        "-an",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        str(output),
    ]
    _run_ffmpeg(cmd, output=output, timeout_sec=timeout_sec)


def _concat_segments(segments: list[Path], output: Path, *, timeout_sec: int) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    list_path = output.with_suffix(".concat.txt")
    list_path.write_text(
        "".join(
            f"file '{str(p).replace(chr(39), chr(39) + chr(92) + chr(39) + chr(39))}'\n"
            for p in segments
        ),
        encoding="utf-8",
    )
    cmd = [
        ffmpeg_path(),
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(list_path),
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        str(output),
    ]
    _run_ffmpeg(cmd, output=output, timeout_sec=timeout_sec)


# ── pass 1b: transition planning (plan §5.2) ─────────────────────────────────


def _plan_transitions(
    video_clips: list[dict[str, Any]],
    segments: list[dict[str, Any]],
    *,
    fps: float,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Duration-preserving segment surgery for stored fade/dissolve transitions.

    Returns ``(segments, records)``. Semantics (docs/timeline-canonical-plan.md
    §5.2, INVARIANT T1 — segment durations must sum unchanged):

    - ``fade`` (fade-through-black): pure per-segment ``fade`` filters appended
      to the flanking segments' ``-vf`` chains. Boundaries, durations, and the
      concat list are untouched — T1 holds by construction.
    - ``dissolve`` (true crossfade): B-pre-handle window. A's flank segment is
      shortened by ``d_eff``, an xfade window segment of exactly ``d_eff``
      seconds is inserted, B's segment is untouched:
      ``(A - d_eff) + d_eff + B`` — T1 holds.
      ``d_eff = min(d, B.source_in, A_flank - 0.1, A.duration - 0.1,
      B.duration - 0.1)`` (the ``- 0.1`` terms keep every remaining segment
      above ``_render_video_segment``'s 0.1 s floor; the flank term covers
      multi-track slicing where A's last segment is shorter than the clip).
      ``d_eff < 2/fps`` degrades to a hard cut, recorded as ``no_handle``.
    - Runtime re-checks (write-time validation can go stale — move/delete do
      not clear ``transition_after`` today): a missing/non-adjacent B, or a
      cut whose flanking segments do not belong to A and B (multi-track
      latest-order-wins slicing can interpose another clip), degrades to a
      hard cut recorded as ``not_adjacent``. Never fails the export.
    - Kinds without a renderer (wipe) are not planned here; export_project
      records them via ``lumerai.export_support.clip_dropped_fields``.
    """
    records: list[dict[str, Any]] = []
    insertions: dict[int, dict[str, Any]] = {}

    for item in video_clips:
        clip = item["clip"]
        transition = clip.get("transition_after")
        if not isinstance(transition, dict):
            continue
        kind = str(transition.get("kind") or "")
        if kind not in ("fade", "dissolve"):
            continue
        clip_id = str(clip.get("id") or "")
        d = _pos(transition.get("duration_sec"), 0.0)
        a_start = _pos(clip.get("start"), 0.0)
        cut = a_start + _clip_duration(clip)
        record: dict[str, Any] = {
            "clip_id": clip_id,
            "kind": kind,
            "duration_sec": round(d, 6),
        }

        # Re-check adjacency: the next clip on the same track must butt-join.
        track_id = str(clip.get("track_id") or "")
        b_item: dict[str, Any] | None = None
        for other in video_clips:
            other_clip = other["clip"]
            if other_clip is clip or str(other_clip.get("track_id") or "") != track_id:
                continue
            other_start = _pos(other_clip.get("start"), 0.0)
            if other_start <= a_start:
                continue
            if b_item is None or other_start < _pos(b_item["clip"].get("start"), 0.0):
                b_item = other
        if (
            b_item is None
            or abs(_pos(b_item["clip"].get("start"), 0.0) - cut) > _EPSILON
        ):
            records.append({**record, "status": "degraded", "reason": "not_adjacent"})
            continue
        b_clip = b_item["clip"]

        # The segments flanking the cut must actually belong to A and B.
        i_a: int | None = None
        for i, seg in enumerate(segments):
            seg_item = seg.get("item")
            if (
                seg_item is not None
                and str(seg_item["clip"].get("id")) == clip_id
                and abs(_pos(seg.get("end"), 0.0) - cut) <= 2 * _EPSILON
            ):
                i_a = i
                break
        i_b = i_a + 1 if i_a is not None else None
        if not (
            i_a is not None
            and i_b is not None
            and i_b < len(segments)
            and segments[i_b].get("item") is not None
            and str(segments[i_b]["item"]["clip"].get("id")) == str(b_clip.get("id"))
            and abs(_pos(segments[i_b].get("start"), 0.0) - cut) <= 2 * _EPSILON
        ):
            records.append({**record, "status": "degraded", "reason": "not_adjacent"})
            continue
        seg_a = segments[i_a]
        seg_b = segments[i_b]
        seg_a_dur = _pos(seg_a.get("end"), 0.0) - _pos(seg_a.get("start"), 0.0)

        if kind == "fade":
            # Zero-hazard: filters only, no boundary changes (T1 untouched).
            half = d / 2.0
            if half > 1e-6:
                seg_a["vf_extra"] = (seg_a.get("vf_extra") or "") + (
                    f",fade=t=out:st={max(seg_a_dur - half, 0.0):.6f}:d={half:.6f}"
                )
                seg_b["vf_extra"] = (seg_b.get("vf_extra") or "") + (
                    f",fade=t=in:st=0:d={half:.6f}"
                )
            records.append({**record, "status": "rendered"})
            continue

        # dissolve — the only sync-safe source for B's pre-cut frames is media
        # before B.source_in (a handle); never shift B or shrink the total.
        b_source_in = _pos(b_clip.get("source_in"), 0.0)
        d_eff = min(
            d,
            b_source_in,
            _clip_duration(clip) - _SEGMENT_FLOOR,
            _clip_duration(b_clip) - _SEGMENT_FLOOR,
            seg_a_dur - _SEGMENT_FLOOR,
        )
        if d_eff < 2.0 / max(fps, 1.0):
            records.append({**record, "status": "degraded", "reason": "no_handle"})
            continue

        cut_grid = _pos(seg_b.get("start"), 0.0)  # exact segment-grid boundary
        seg_a["end"] = round(cut_grid - d_eff, 6)
        insertions[i_a] = {
            "start": seg_a["end"],
            "end": cut_grid,
            "item": None,
            "window": {
                "a_id": clip_id,
                "b_id": str(b_clip.get("id") or ""),
                "a_source": str(item["asset"].get("source_path") or ""),
                "b_source": str(b_item["asset"].get("source_path") or ""),
                "a_in": _pos(clip.get("source_in"), 0.0)
                + max(cut_grid - d_eff - a_start, 0.0),
                "b_in": max(b_source_in - d_eff, 0.0),
                "duration": round(d_eff, 6),
            },
        }
        records.append(
            {
                **record,
                "status": "rendered",
                "effective_duration_sec": round(d_eff, 6),
            }
        )

    if not insertions:
        return segments, records
    rebuilt: list[dict[str, Any]] = []
    for i, seg in enumerate(segments):
        rebuilt.append(seg)
        if i in insertions:
            rebuilt.append(insertions[i])
    return rebuilt, records


def _render_xfade_window(
    window: dict[str, Any],
    output: Path,
    *,
    width: int,
    height: int,
    fps: float,
    profile: dict[str, str],
    timeout_sec: int,
) -> None:
    """Render one dissolve window: exactly ``duration`` seconds, A xfaded to B.

    Both inputs are pre-normalized through the same ``_video_filter`` every
    other segment uses, so ``xfade`` at ``offset=0`` outputs exactly
    ``duration`` seconds in concat-compatible encoding (plan §5.2 step 3).
    """
    a_source = Path(str(window.get("a_source") or "")).expanduser()
    b_source = Path(str(window.get("b_source") or "")).expanduser()
    for source in (a_source, b_source):
        if not source.exists():
            raise ProjectExportError(
                "source_not_found",
                f"Source file missing: {source}",
                detail=str(source),
            )
    d = _pos(window.get("duration"), 0.0)
    vf = _video_filter(width=width, height=height, fps=fps)
    cmd = [
        ffmpeg_path(),
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        f"{_pos(window.get('a_in'), 0.0):.6f}",
        "-t",
        f"{d:.6f}",
        "-i",
        str(a_source),
        "-ss",
        f"{_pos(window.get('b_in'), 0.0):.6f}",
        "-t",
        f"{d:.6f}",
        "-i",
        str(b_source),
        "-filter_complex",
        (
            f"[0:v]{vf}[va];[1:v]{vf}[vb];"
            f"[va][vb]xfade=transition=fade:duration={d:.6f}:offset=0[v]"
        ),
        "-map",
        "[v]",
        "-an",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-crf",
        profile["crf"],
        "-preset",
        profile["preset"],
        "-movflags",
        "+faststart",
        str(output),
    ]
    _run_ffmpeg(cmd, output=output, timeout_sec=timeout_sec)


# ── pass 2: overlay compositing ──────────────────────────────────────────────


def _apply_overlays(
    base: Path,
    overlay_clips: list[dict[str, Any]],
    assets: dict[str, dict[str, Any]],
    *,
    output: Path,
    width: int,
    height: int,
    fps: float,
    quality: str,
    timeout_sec: int,
) -> None:
    """Composite overlay (image, Lottie, and text) clips onto the base video."""
    profile = _QUALITY_PROFILES[quality]
    cmd: list[str] = [ffmpeg_path(), "-y", "-hide_banner", "-loglevel", "error"]
    # Always first input: base video
    cmd += ["-i", str(base)]

    image_inputs: list[tuple[int, dict[str, Any], dict[str, Any]]] = (
        []
    )  # (input_index, clip, asset)
    lottie_inputs: list[tuple[int, dict[str, Any], dict[str, Any]]] = []
    text_clips: list[dict[str, Any]] = []
    temp_dirs: list[tempfile.TemporaryDirectory[str]] = []

    try:
        input_idx = 1
        for clip in overlay_clips:
            media_kind = str(clip.get("media_kind") or "")
            if media_kind == "image":
                asset = assets.get(str(clip.get("asset_id") or ""))
                if asset is None:
                    continue
                source = Path(str(asset.get("source_path") or "")).expanduser()
                if not source.exists():
                    continue
                cmd += ["-i", str(source)]
                image_inputs.append((input_idx, clip, asset))
                input_idx += 1
            elif media_kind == "lottie":
                asset = assets.get(str(clip.get("asset_id") or ""))
                if asset is None:
                    continue
                source = Path(str(asset.get("source_path") or "")).expanduser()
                if not source.exists():
                    continue
                temp = tempfile.TemporaryDirectory(prefix="lumeri-lottie-")
                temp_dirs.append(temp)
                pattern = _render_lottie_sequence_for_clip(
                    source, clip, temp.name, width=width, height=height, fps=fps
                )
                cmd += ["-framerate", f"{fps:.6f}", "-i", pattern]
                lottie_inputs.append((input_idx, clip, asset))
                input_idx += 1
            elif media_kind == "text":
                text_clips.append(clip)

        # Build complex filtergraph
        filter_parts: list[str] = []
        last_label = "[0:v]"

        visual_inputs = [*image_inputs, *lottie_inputs]
        for i, (img_idx, clip, _asset) in enumerate(visual_inputs):
            start = _pos(clip.get("start"), 0.0)
            end = start + _clip_duration(clip)
            effects = (
                clip.get("effects") if isinstance(clip.get("effects"), dict) else {}
            )
            x = int(_pos(effects.get("x"), 0.0))
            y = int(_pos(effects.get("y"), 0.0))
            scale = _pos(effects.get("scale"), 1.0)
            opacity = min(1.0, max(0.0, _pos(effects.get("opacity"), 1.0)))
            enable_expr = f"between(t,{start:.6f},{end:.6f})"

            img_label = f"[img{i}]"
            scale_filter = f"[{img_idx}:v]scale=iw*{scale:.4f}:-1{img_label}"
            filter_parts.append(scale_filter)

            out_label = f"[v{i}]"
            ov = (
                f"{last_label}{img_label}overlay="
                f"x={x}:y={y}:"
                f"enable='{enable_expr}'"
            )
            if opacity < 0.999:
                alpha_label = f"[alpha{i}]"
                filter_parts[-1] = (
                    f"[{img_idx}:v]scale=iw*{scale:.4f}:-1,format=rgba,"
                    f"colorchannelmixer=aa={opacity:.4f}{alpha_label}"
                )
                ov = f"{last_label}{alpha_label}overlay=x={x}:y={y}:format=auto:enable='{enable_expr}'"
            filter_parts.append(f"{ov}{out_label}")
            last_label = out_label

        # Text overlays via drawtext
        for j, clip in enumerate(text_clips):
            start = _pos(clip.get("start"), 0.0)
            end = start + _clip_duration(clip)
            config = (
                clip.get("text_config")
                if isinstance(clip.get("text_config"), dict)
                else {}
            )
            text = (
                str(config.get("content") or "")
                .replace("'", "'\\''")
                .replace(":", "\\:")
            )
            font_size = int(max(_pos(config.get("font_size"), 64.0), 8.0))
            color_hex = str(config.get("color") or "#ffffff")
            if _HEX_COLOR_RE.match(color_hex):
                ffcolor = f"0x{color_hex[1:]}"
            else:
                ffcolor = "white"
            effects = (
                clip.get("effects") if isinstance(clip.get("effects"), dict) else {}
            )
            pos = config.get("position")
            if isinstance(pos, dict):
                x = int(_pos(pos.get("x"), width // 2))
                y = int(_pos(pos.get("y"), height // 2))
            else:
                x = int(_pos(effects.get("x"), width // 2))
                y = int(_pos(effects.get("y"), height // 2))

            enable_expr = f"between(t,{start:.6f},{end:.6f})"
            out_label = f"[vt{j}]"
            font_opt = ""
            if _resolve_font_path is not None:
                try:
                    _ff = _resolve_font_path(config)
                except Exception:
                    _ff = None
                if _ff:
                    _ff = (
                        str(_ff)
                        .replace("\\", "\\\\")
                        .replace(":", "\\:")
                        .replace("'", "\\'")
                    )
                    font_opt = f"fontfile='{_ff}':"
            dt_filter = (
                f"{last_label}drawtext="
                f"{font_opt}"
                f"text='{text}':"
                f"fontsize={font_size}:"
                f"fontcolor={ffcolor}:"
                f"x={x}:y={y}:"
                f"enable='{enable_expr}'"
                f"{out_label}"
            )
            filter_parts.append(dt_filter)
            last_label = out_label

        if filter_parts:
            cmd += ["-filter_complex", ";".join(filter_parts), "-map", last_label]
        else:
            cmd += ["-map", "0:v"]

        cmd += [
            "-an",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-crf",
            profile["crf"],
            "-preset",
            profile["preset"],
            "-movflags",
            "+faststart",
            str(output),
        ]
        _run_ffmpeg(cmd, output=output, timeout_sec=timeout_sec)
    finally:
        for temp in temp_dirs:
            temp.cleanup()


def _render_lottie_sequence_for_clip(
    source: Path,
    clip: dict[str, Any],
    output_dir: str,
    *,
    width: int,
    height: int,
    fps: float,
) -> str:
    renderer = select_lottie_renderer()
    meta = renderer.get_metadata(str(source))
    source_fps = float(meta.get("fps") or 30.0)
    source_frames = max(int(meta.get("frames") or 1), 1)
    duration = _clip_duration(clip)
    source_in = _pos(clip.get("source_in"), 0.0)
    frame_count = max(1, int(math.ceil(duration * max(fps, 1.0))))
    seq_dir = Path(output_dir)
    for index in range(frame_count):
        t = source_in + index / max(fps, 1.0)
        source_frame = max(0, min(int(round(t * source_fps)), source_frames - 1))
        save_lottie_frame_png(
            source,
            seq_dir / f"frame_{index + 1:05d}.png",
            width=width,
            height=height,
            frame_index=source_frame,
        )
    return str(seq_dir / "frame_%05d.png")


# ── pass 3: audio mix + mux ──────────────────────────────────────────────────


def _collect_audio_sources(
    video_clips: list[dict[str, Any]],
    audio_clips: list[dict[str, Any]],
    assets: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build the positioned audio sources for the final mix.

    Two origins, mirroring an NLE: (a) every enabled audio-track clip, and
    (b) the embedded audio of each enabled video-track clip — kept by default
    so a talking-head clip keeps its voice — unless that clip is ``muted`` or
    its source carries no audio stream. The latter is what keeps no-audio
    projects (silent renders / testsrc2) on the silent ``-an`` path.

    Each source is ``{path, source_in, source_out, start, gain_db, fade_in,
    fade_out}`` (seconds as floats). Returns ``[]`` when there is no audio.
    """
    sources: list[dict[str, Any]] = []

    # (a) audio-track clips.
    for clip in audio_clips:
        asset = assets.get(str(clip.get("asset_id") or ""))
        if not isinstance(asset, dict):
            continue
        source = Path(str(asset.get("source_path") or "")).expanduser()
        if not source.exists():
            continue
        src = _audio_source_from_clip(clip, source)
        if src is not None:
            sources.append(src)

    # (b) embedded audio of video-track clips (default-on; muted opts out).
    probe_cache: dict[str, bool] = {}
    for item in video_clips:
        clip = item["clip"]
        effects = clip.get("effects") if isinstance(clip.get("effects"), dict) else {}
        if bool(effects.get("muted", False)):
            continue
        asset = item["asset"]
        source = Path(str(asset.get("source_path") or "")).expanduser()
        if not source.exists():
            continue
        key = str(source)
        if key not in probe_cache:
            probe_cache[key] = _source_has_audio(source)
        if not probe_cache[key]:
            continue
        src = _audio_source_from_clip(clip, source)
        if src is not None:
            sources.append(src)

    return sources


def _audio_source_from_clip(
    clip: dict[str, Any], source: Path
) -> dict[str, Any] | None:
    source_in = _pos(clip.get("source_in"), 0.0)
    source_out = _pos(clip.get("source_out"), source_in + _clip_duration(clip))
    if source_out - source_in <= 0.001:
        return None
    effects = clip.get("effects") if isinstance(clip.get("effects"), dict) else {}
    return {
        "path": source,
        "track_id": str(clip.get("track_id") or ""),
        "source_in": source_in,
        "source_out": source_out,
        "start": _pos(clip.get("start"), 0.0),
        "gain_db": _float(effects.get("gain_db"), 0.0),
        "fade_in": max(_float(effects.get("fade_in"), 0.0), 0.0),
        "fade_out": max(_float(effects.get("fade_out"), 0.0), 0.0),
    }


def _source_has_audio(source: Path) -> bool:
    for stream in _ffprobe(source).get("streams") or []:
        if isinstance(stream, dict) and stream.get("codec_type") == "audio":
            return True
    return False


def _audio_source_chain(i: int, src: dict[str, Any]) -> str:
    """One positioned-source filter chain ending in label ``[a{i}]``.

    atrim -> (volume gain) -> (afade in/out) -> aformat fltp/48k/stereo ->
    (adelay to the clip's timeline start). Mirrors gemia/tools/mix_audio.py.
    """
    in_idx = i
    dur = src["source_out"] - src["source_in"]
    chain = (
        f"[{in_idx}:a]atrim=start={src['source_in']:.6f}:end={src['source_out']:.6f},"
        f"asetpts=PTS-STARTPTS"
    )
    if abs(src["gain_db"]) > 1e-6:
        chain += f",volume={_db_to_linear(src['gain_db']):.6f}"
    fade_in = min(src["fade_in"], dur)
    if fade_in > 1e-3:
        chain += f",afade=t=in:st=0:d={fade_in:.6f}"
    fade_out = min(src["fade_out"], dur)
    if fade_out > 1e-3:
        chain += f",afade=t=out:st={max(dur - fade_out, 0.0):.6f}:d={fade_out:.6f}"
    chain += ",aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo"
    start_ms = int(round(src["start"] * 1000.0))
    if start_ms > 0:
        chain += f",adelay={start_ms}:all=1"
    return f"{chain}[a{i}]"


def _resolve_duck_map(
    duck_map: dict[str, str] | None, audio_sources: list[dict[str, Any]]
) -> dict[str, str]:
    """Keep only bed->trigger pairs where both tracks actually have audio."""
    if not duck_map:
        return {}
    have = {str(s.get("track_id") or "") for s in audio_sources if s.get("track_id")}
    return {
        bed: trigger
        for bed, trigger in duck_map.items()
        if bed in have and trigger in have and bed != trigger
    }


def _label(track_id: str) -> str:
    return "".join(ch if ch.isalnum() else "_" for ch in str(track_id)) or "x"


def _mux_audio_onto_video(
    video: Path,
    audio_sources: list[dict[str, Any]],
    *,
    output: Path,
    work_dir: Path,
    timeout_sec: int,
    duck_map: dict[str, str] | None = None,
    master_duration: float = 0.0,
) -> None:
    """Mix all positioned audio sources and mux onto the (silent) video.

    Without an active ducking relationship the graph is a flat
    ``amix=...:duration=longest`` over every source (identical to M6). With
    ducking, sources are grouped into per-track submixes; a bed track's submix
    is sidechain-compressed by its trigger track's submix
    (``sidechaincompress``, mirroring gemia/tools/mix_audio.py duck mode), then
    all track submixes are amix'd.  The resulting premaster is written as
    lossless float PCM, measured, then normalized with the measured values.
    Video is stream-copied; only the normalized audio is encoded as AAC.
    """
    mix_cmd: list[str] = [ffmpeg_path(), "-y", "-hide_banner", "-loglevel", "error"]
    for src in audio_sources:
        mix_cmd += ["-i", str(src["path"])]

    filter_parts: list[str] = [
        _audio_source_chain(i, src) for i, src in enumerate(audio_sources)
    ]
    active = _resolve_duck_map(duck_map, audio_sources)
    source_end = max(
        (
            float(src.get("start") or 0.0)
            + float(src.get("source_out") or 0.0)
            - float(src.get("source_in") or 0.0)
            for src in audio_sources
        ),
        default=0.0,
    )
    delivery_duration = float(master_duration or 0.0)
    if delivery_duration <= 0.0:
        raise ProjectExportError(
            "audio_master_duration_invalid",
            "Audio sources exist but the canonical master duration is not positive.",
        )
    if source_end > delivery_duration + 0.01:
        raise ProjectExportError(
            "audio_source_exceeds_master_duration",
            "An audio source extends beyond the canonical master duration.",
            detail=(
                f"source_end={source_end:.6f}, "
                f"master_duration={delivery_duration:.6f}"
            ),
        )

    if not active:
        # Flat path — byte-for-behaviour identical to M6 (no ducking configured).
        labels = [f"[a{i}]" for i in range(len(audio_sources))]
        if len(labels) == 1:
            filter_parts.append(f"{labels[0]}anull[premaster]")
        else:
            filter_parts.append(
                "".join(labels)
                + f"amix=inputs={len(labels)}:duration=longest:dropout_transition=0[premaster]"
            )
    else:
        _append_ducked_mix(
            filter_parts,
            audio_sources,
            active,
            master_duration=delivery_duration,
        )

    # A sidechain trigger or the last authored clip may end before picture.
    # Keep the premaster aligned to the canonical master so AAC does not stop
    # early and players do not lose an intentional music fade/title tail.
    filter_parts.append(
        f"[premaster]apad=whole_dur={delivery_duration:.6f},"
        f"atrim=end={delivery_duration:.6f}[delivery_premaster]"
    )

    premaster_path = work_dir / "audio-premaster.wav"
    mix_cmd += [
        "-filter_complex",
        ";".join(filter_parts),
        "-map",
        "[delivery_premaster]",
        "-c:a",
        "pcm_f32le",
        "-ar",
        "48000",
        "-ac",
        "2",
        str(premaster_path),
    ]
    _run_ffmpeg(mix_cmd, output=premaster_path, timeout_sec=timeout_sec)

    stats = _measure_loudnorm_pass(
        premaster_path,
        integrated_lufs=AUDIO_MASTER_TARGET_LUFS,
        true_peak_dbtp=AUDIO_MASTER_TRUE_PEAK_DBTP,
        loudness_range_lu=AUDIO_MASTER_LRA_LU,
        timeout_sec=timeout_sec,
    )

    normalize_filter = _loudnorm_second_pass_filter(stats)
    cmd: list[str] = [
        ffmpeg_path(),
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(video),
        "-i",
        str(premaster_path),
        "-map",
        "0:v",
        "-map",
        "1:a:0",
        "-c:v",
        "copy",
        "-af",
        normalize_filter,
        "-c:a",
        "aac",
        "-ar",
        "48000",
        "-ac",
        "2",
        "-b:a",
        "192k",
        "-movflags",
        "+faststart",
        str(output),
    ]
    _run_ffmpeg(cmd, output=output, timeout_sec=timeout_sec)


def _parse_loudnorm_payload(stderr: str) -> dict[str, float]:
    matches = re.findall(r"\{\s*\"input_i\".*?\}", stderr or "", flags=re.DOTALL)
    if not matches:
        raise ValueError("loudnorm did not emit measurement JSON")
    try:
        payload = json.loads(matches[-1])
        result = {
            "input_i": float(payload["input_i"]),
            "input_tp": float(payload["input_tp"]),
            "input_lra": float(payload["input_lra"]),
            "input_thresh": float(payload["input_thresh"]),
            "target_offset": float(payload["target_offset"]),
        }
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid loudnorm measurement: {exc}") from exc
    if not all(math.isfinite(value) for value in result.values()):
        raise ValueError("loudnorm measurement contains non-finite values")
    return result


def _measure_loudnorm_pass(
    path: Path,
    *,
    integrated_lufs: float,
    true_peak_dbtp: float,
    loudness_range_lu: float,
    timeout_sec: int,
) -> dict[str, float]:
    try:
        proc = subprocess.run(
            [
                ffmpeg_path(),
                "-hide_banner",
                "-nostats",
                "-i",
                str(path),
                "-map",
                "0:a:0",
                "-af",
                (
                    f"loudnorm=I={integrated_lufs:g}:TP={true_peak_dbtp:g}:"
                    f"LRA={loudness_range_lu:g}:print_format=json"
                ),
                "-f",
                "null",
                "-",
            ],
            capture_output=True,
            text=True,
            timeout=max(1, int(timeout_sec)),
        )
    except subprocess.TimeoutExpired as exc:
        raise ProjectExportError(
            "audio_master_measurement_failed",
            "Premaster loudness analysis timed out.",
            detail=f"timeout={exc.timeout}",
        ) from exc
    except OSError as exc:
        raise ProjectExportError(
            "audio_master_measurement_failed",
            "Premaster loudness analysis could not start.",
            detail=f"{type(exc).__name__}: {exc}"[:1000],
        ) from exc
    if proc.returncode != 0:
        raise ProjectExportError(
            "audio_master_measurement_failed",
            "Premaster loudness analysis failed.",
            detail=(proc.stderr or proc.stdout or "").strip()[-2000:],
        )
    try:
        return _parse_loudnorm_payload(proc.stderr or "")
    except ValueError as exc:
        raise ProjectExportError(
            "audio_master_measurement_failed",
            "Premaster loudness analysis returned unusable measurements.",
            detail=str(exc),
        ) from exc


def _loudnorm_second_pass_filter(stats: dict[str, float]) -> str:
    return (
        f"loudnorm=I={AUDIO_MASTER_TARGET_LUFS:g}:"
        f"TP={AUDIO_MASTER_TRUE_PEAK_DBTP:g}:LRA={AUDIO_MASTER_LRA_LU:g}:"
        f"measured_I={stats['input_i']:.6f}:"
        f"measured_TP={stats['input_tp']:.6f}:"
        f"measured_LRA={stats['input_lra']:.6f}:"
        f"measured_thresh={stats['input_thresh']:.6f}:"
        f"offset={stats['target_offset']:.6f}:linear=true:print_format=none,"
        "aresample=48000,aformat=sample_fmts=fltp:sample_rates=48000:"
        "channel_layouts=stereo"
    )


def _append_ducked_mix(
    filter_parts: list[str],
    audio_sources: list[dict[str, Any]],
    active: dict[str, str],
    *,
    master_duration: float,
) -> None:
    """Per-track submix -> sidechain beds under triggers -> [premaster]."""
    # Group source indices by track (insertion order preserved).
    by_track: dict[str, list[int]] = {}
    for i, src in enumerate(audio_sources):
        by_track.setdefault(str(src.get("track_id") or ""), []).append(i)

    # Per-track raw submix -> [raw_<t>].
    for tid, idxs in by_track.items():
        raw = f"[raw_{_label(tid)}]"
        if len(idxs) == 1:
            filter_parts.append(f"[a{idxs[0]}]anull{raw}")
        else:
            filter_parts.append(
                "".join(f"[a{j}]" for j in idxs)
                + f"amix=inputs={len(idxs)}:duration=longest:dropout_transition=0{raw}"
            )

    # Split each trigger submix into its audible mix copy and one independent
    # detector copy per bed. FFmpeg filter outputs are single-consumer, so a
    # shared trigger must never reuse one [sc] label across multiple beds.
    beds_by_trigger: dict[str, list[str]] = {}
    for bed, trigger in active.items():
        beds_by_trigger.setdefault(trigger, []).append(bed)
    sidechain_for: dict[tuple[str, str], str] = {}
    premix: dict[str, str] = {}
    for tid in by_track:
        trigger_beds = beds_by_trigger.get(tid, [])
        if trigger_beds:
            mix_l = f"[mix_{_label(tid)}]"
            raw_sidechains = [
                f"[sc_raw_{_label(tid)}_{index}]" for index in range(len(trigger_beds))
            ]
            filter_parts.append(
                f"[raw_{_label(tid)}]asplit={1 + len(trigger_beds)}"
                + mix_l
                + "".join(raw_sidechains)
            )
            for index, bed in enumerate(trigger_beds):
                sc_l = f"[sc_{_label(tid)}_{index}]"
                # sidechaincompress follows the sidechain EOF on the production
                # FFmpeg build. Extend each detector with silence so a shorter
                # narration track cannot truncate a longer music-bed input.
                filter_parts.append(
                    f"{raw_sidechains[index]}apad=whole_dur={master_duration:.6f},"
                    f"atrim=end={master_duration:.6f}{sc_l}"
                )
                sidechain_for[(bed, tid)] = sc_l
            premix[tid] = mix_l
        else:
            premix[tid] = f"[raw_{_label(tid)}]"

    # Beds: compress the bed's premix against its trigger's sidechain copy.
    final: dict[str, str] = dict(premix)
    for bed, trigger in active.items():
        duck_l = f"[duck_{_label(bed)}]"
        filter_parts.append(
            f"{premix[bed]}{sidechain_for[(bed, trigger)]}"
            "sidechaincompress=threshold=0.02:ratio=12:attack=20:release=400"
            f"{duck_l}"
        )
        final[bed] = duck_l

    final_labels = [final[tid] for tid in by_track]
    if len(final_labels) == 1:
        filter_parts.append(f"{final_labels[0]}anull[premaster]")
    else:
        filter_parts.append(
            "".join(final_labels)
            + f"amix=inputs={len(final_labels)}:duration=longest:dropout_transition=0[premaster]"
        )


# ── clip / asset helpers ─────────────────────────────────────────────────────


def _build_asset_map(project: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(a.get("id") or a.get("asset_id") or ""): a
        for a in project.get("assets") or []
        if isinstance(a, dict)
    }


def _enabled_video_clips(
    project: dict[str, Any], assets: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    timeline = (
        project.get("timeline") if isinstance(project.get("timeline"), dict) else {}
    )

    # Collect video track ids
    video_track_ids = set()
    for track in timeline.get("tracks") or []:
        if isinstance(track, dict) and str(track.get("kind")) == "video":
            video_track_ids.add(str(track.get("id") or ""))

    items: list[dict[str, Any]] = []
    for order, clip in enumerate(timeline.get("clips") or []):
        if not isinstance(clip, dict) or not bool(clip.get("enabled", True)):
            continue
        # Only process clips on video tracks
        clip_track_id = str(clip.get("track_id") or "")
        if clip_track_id not in video_track_ids:
            continue
        # Accept video and image media kinds
        clip_media_kind = str(clip.get("media_kind") or "video")
        if clip_media_kind not in {"video", "image"}:
            continue
        asset = assets.get(str(clip.get("asset_id") or ""))
        if not isinstance(asset, dict):
            continue
        # Asset must be video or image
        asset_media_kind = str(asset.get("media_kind") or "video")
        if asset_media_kind not in {"video", "image"}:
            continue
        items.append({"clip": clip, "asset": asset, "order": order})
    items.sort(
        key=lambda it: (_pos(it["clip"].get("start"), 0.0), int(it.get("order") or 0))
    )
    return items


def _enabled_overlay_clips(
    project: dict[str, Any], assets: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    timeline = (
        project.get("timeline") if isinstance(project.get("timeline"), dict) else {}
    )
    result: list[dict[str, Any]] = []
    for clip in timeline.get("clips") or []:
        if not isinstance(clip, dict) or not bool(clip.get("enabled", True)):
            continue
        media_kind = str(clip.get("media_kind") or "")
        if media_kind not in {"image", "text", "lottie"}:
            continue
        result.append(clip)
    result.sort(key=lambda c: _pos(c.get("start"), 0.0))
    return result


def _enabled_audio_clips(
    project: dict[str, Any], assets: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    """Enabled audio-track clips (media_kind == 'audio') with a resolvable asset."""
    timeline = (
        project.get("timeline") if isinstance(project.get("timeline"), dict) else {}
    )
    result: list[dict[str, Any]] = []
    for clip in timeline.get("clips") or []:
        if not isinstance(clip, dict) or not bool(clip.get("enabled", True)):
            continue
        if str(clip.get("media_kind") or "") != "audio":
            continue
        if not isinstance(assets.get(str(clip.get("asset_id") or "")), dict):
            continue
        result.append(clip)
    result.sort(key=lambda c: _pos(c.get("start"), 0.0))
    return result


def _timeline_segments(
    items: list[dict[str, Any]], timeline_duration: float
) -> list[dict[str, Any]]:
    """Return non-overlapping timeline intervals (latest-order clip wins)."""
    boundaries: set[float] = {0.0}
    max_end = 0.0
    for item in items:
        clip = item["clip"]
        start = _pos(clip.get("start"), 0.0)
        end = start + _clip_duration(clip)
        max_end = max(max_end, end)
        boundaries.add(round(start, 6))
        boundaries.add(round(end, 6))
    total = max(timeline_duration, max_end)
    boundaries.add(round(total, 6))
    ordered = sorted(b for b in boundaries if b >= 0)
    segments: list[dict[str, Any]] = []
    for start, end in zip(ordered, ordered[1:]):
        if end <= start + 0.001:
            continue
        mid = start + (end - start) / 2.0
        active = [
            it
            for it in items
            if _pos(it["clip"].get("start"), 0.0) <= mid
            and mid < _pos(it["clip"].get("start"), 0.0) + _clip_duration(it["clip"])
        ]
        if active:
            chosen = max(active, key=lambda it: int(it.get("order") or 0))
            segments.append({"start": start, "end": end, "item": chosen})
        else:
            segments.append({"start": start, "end": end, "item": None})
    return segments


# ── ffmpeg / probe helpers ────────────────────────────────────────────────────


def _video_filter(*, width: int, height: int, fps: float) -> str:
    return (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,"
        f"setsar=1,fps={fps},format=yuv420p"
    )


def _run_ffmpeg(cmd: list[str], output: Path, *, timeout_sec: int) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_sec)
    if proc.returncode != 0:
        raise ProjectExportError(
            "ffmpeg_failed",
            "Export ffmpeg command failed.",
            detail=(proc.stderr or proc.stdout or "").strip()[-2000:],
        )
    if not output.exists() or output.stat().st_size <= 0:
        raise ProjectExportError(
            "output_missing", f"ffmpeg did not create output: {output}"
        )


def _ffprobe(path: Path) -> dict[str, Any]:
    proc = subprocess.run(
        [
            ffprobe_path(),
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if proc.returncode != 0:
        return {}
    try:
        return dict(json.loads(proc.stdout or "{}"))
    except (json.JSONDecodeError, ValueError):
        return {}


def _full_decode_check(path: Path, *, timeout_sec: int) -> dict[str, Any]:
    """Decode every A/V frame; metadata-only probing is not acceptance."""
    try:
        proc = subprocess.run(
            [
                ffmpeg_path(),
                "-v",
                "error",
                "-i",
                str(path),
                "-map",
                "0:v:0",
                "-map",
                "0:a?",
                "-f",
                "null",
                "-",
            ],
            capture_output=True,
            text=True,
            timeout=max(1, int(timeout_sec)),
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "status": "failed",
            "error": f"full decode timed out after {exc.timeout}s",
        }
    except OSError as exc:
        return {"status": "failed", "error": f"{type(exc).__name__}: {exc}"[:1000]}
    if proc.returncode != 0:
        return {
            "status": "failed",
            "error": (proc.stderr or proc.stdout or "full decode failed").strip()[
                -2000:
            ],
        }
    return {"status": "passed"}


def _measure_loudness(path: Path, *, timeout_sec: int) -> dict[str, Any]:
    """Measure integrated loudness and true peak on the encoded master."""
    try:
        proc = subprocess.run(
            [
                ffmpeg_path(),
                "-hide_banner",
                "-nostats",
                "-i",
                str(path),
                "-map",
                "0:a:0",
                "-af",
                "loudnorm=I=-16:TP=-1:LRA=11:print_format=json",
                "-f",
                "null",
                "-",
            ],
            capture_output=True,
            text=True,
            timeout=max(1, int(timeout_sec)),
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "status": "failed",
            "error": f"loudness analysis timed out after {exc.timeout}s",
        }
    except OSError as exc:
        return {"status": "failed", "error": f"{type(exc).__name__}: {exc}"[:1000]}
    stderr = proc.stderr or ""
    if proc.returncode != 0:
        return {"status": "failed", "error": stderr.strip()[-2000:]}
    matches = re.findall(r"\{\s*\"input_i\".*?\}", stderr, flags=re.DOTALL)
    if not matches:
        return {"status": "failed", "error": "loudnorm did not emit measurement JSON"}
    try:
        payload = json.loads(matches[-1])
        integrated = float(payload["input_i"])
        true_peak = float(payload["input_tp"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return {"status": "failed", "error": f"invalid loudness measurement: {exc}"}
    return {
        "status": "passed",
        "integrated_loudness_lufs": round(integrated, 3),
        "true_peak_dbtp": round(true_peak, 3),
    }


def _probe_duration(probe: dict[str, Any]) -> float:
    fmt = probe.get("format") if isinstance(probe.get("format"), dict) else {}
    return round(_pos(fmt.get("duration"), 0.0), 6)


def _probe_resolution(probe: dict[str, Any]) -> dict[str, int] | None:
    for stream in probe.get("streams") or []:
        if isinstance(stream, dict) and stream.get("codec_type") == "video":
            w = int(_pos(stream.get("width"), 0))
            h = int(_pos(stream.get("height"), 0))
            if w > 0 and h > 0:
                return {"width": w, "height": h}
    return None


# ── misc helpers ─────────────────────────────────────────────────────────────


def _pos(value: Any, default: float) -> float:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return float(default)
    if not math.isfinite(n) or n < 0:
        return float(default)
    return n


def _float(value: Any, default: float) -> float:
    """Like _pos but allows negatives (e.g. gain_db can be < 0)."""
    try:
        n = float(value)
    except (TypeError, ValueError):
        return float(default)
    return n if math.isfinite(n) else float(default)


def _db_to_linear(db: float) -> float:
    return 10.0 ** (db / 20.0)


def _clip_duration(clip: dict[str, Any]) -> float:
    return max(_pos(clip.get("duration"), 0.1), 0.1)


def _slug(value: str) -> str:
    slug = "".join(ch if ch.isalnum() else "-" for ch in str(value).lower()).strip("-")
    return slug[:48] or "export"


def _fit_size(width: int, height: int, *, max_long_edge: int) -> tuple[int, int]:
    edge = max(160, int(max_long_edge))
    longest = max(int(width), int(height))
    if longest <= edge:
        return int(width), int(height)
    scale = edge / float(longest)
    fitted_width = max(2, int(round(width * scale)))
    fitted_height = max(2, int(round(height * scale)))
    fitted_width += fitted_width % 2
    fitted_height += fitted_height % 2
    return fitted_width, fitted_height


def _reset_dir(path: Path) -> None:
    if path.exists():
        _cleanup_dir(path)
    path.mkdir(parents=True, exist_ok=True)


def _cleanup_dir(path: Path) -> None:
    if not path.exists():
        return
    for child in path.iterdir():
        if child.is_dir():
            _cleanup_dir(child)
            try:
                child.rmdir()
            except OSError:
                pass
        else:
            child.unlink(missing_ok=True)
    try:
        path.rmdir()
    except OSError:
        pass


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    tmp.replace(path)
