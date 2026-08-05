"""assemble_quanta -- materialize Quanta build states onto dedicated timeline tracks."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from gemia.quanta import QuantaMaterializeError
from gemia.quanta.traverse import (
    flat_view,
    flattened_interactions,
    leaf_walk,
    lift_flat_quanta,
)
from gemia.tools._context import ToolContext
from gemia.tools._ffmpeg import get_video_encoder_args, run_ffmpeg_with_progress
from gemia.tools.quanta_frames import materialize_quanta_frame_assets


QUANTA_VIDEO_TRACK = "QUANTA_V1"
QUANTA_FRAME_TRACK = "QUANTA_OV1"


def _project(ctx: ToolContext):
    if ctx.project is None:
        raise ValueError("assemble_quanta needs a project-backed session")
    return ctx.project


def _quanta_hash(quanta: Mapping[str, Any]) -> str:
    try:
        payload = json.dumps(
            quanta, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise QuantaMaterializeError(f"quanta is not deterministic JSON: {exc}") from exc
    return hashlib.sha256(payload).hexdigest()[:16]


def quanta_frame_cache_key(quanta: Mapping[str, Any], *, scale: int = 1) -> str:
    return f"{_quanta_hash(quanta)}:{scale}x"


def _valid_cached_frames(ctx: ToolContext, key: str) -> dict[str, Any] | None:
    cache = ctx.extra.get("quanta_frame_cache")
    if not isinstance(cache, dict) or cache.get("key") != key:
        return None
    result = cache.get("result")
    if not isinstance(result, dict):
        return None
    for asset_id in result.get("frame_asset_ids") or []:
        if not ctx.registry.contains(str(asset_id)):
            return None
        if not ctx.registry.get(str(asset_id)).path.is_file():
            return None
    return result


async def _ensure_black_video(
    ctx: ToolContext,
    *,
    duration: float,
    width: int,
    height: int,
    fps: float,
    cache_key: str,
) -> str:
    cache = ctx.extra.get("quanta_black_video_cache")
    if isinstance(cache, dict) and cache.get("key") == cache_key:
        cached_id = str(cache.get("asset_id") or "")
        if cached_id and ctx.registry.contains(cached_id) and ctx.registry.get(cached_id).path.is_file():
            return cached_id
    asset_id = ctx.registry.allocate_id("video")
    path = ctx.child_path(asset_id, ".mp4")
    path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi",
        "-i", f"color=c=#101419:s={width}x{height}:r={fps:g}:d={duration:.6f}",
        "-an",
        *get_video_encoder_args("h264"),
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        str(path),
    ]
    await run_ffmpeg_with_progress(cmd, total_seconds=duration, progress=ctx.emit_progress)
    if not path.is_file() or path.stat().st_size <= 0:
        raise QuantaMaterializeError("ffmpeg did not create the quanta background video")
    ctx.registry.register_output(
        asset_id,
        kind="video",
        path=path,
        summary=f"quanta background {width}x{height} {duration:.2f}s",
    )
    ctx.extra["quanta_black_video_cache"] = {"key": cache_key, "asset_id": asset_id}
    return asset_id


def _asset_payload(ctx: ToolContext, asset_id: str, *, duration: float) -> dict[str, Any]:
    record = ctx.registry.get(asset_id)
    return {
        "id": asset_id,
        "asset_id": asset_id,
        "name": record.path.name,
        "media_kind": record.kind,
        "source_path": str(record.path),
        "duration": duration,
    }


def _insert_op(
    *,
    ctx: ToolContext,
    track_id: str,
    clip_id: str,
    asset: dict[str, Any],
    media_kind: str,
    start: float,
    duration: float,
    provenance: dict[str, Any],
) -> dict[str, Any]:
    clip = {
        "id": clip_id,
        "asset_id": asset["asset_id"],
        "media_kind": media_kind,
        "name": asset["name"],
        "duration": round(duration, 6),
        "source_in": 0.0,
        "source_out": round(duration, 6),
        "track_id": track_id,
    }
    return {
        "op": "insert_clip",
        "data": {"asset": asset, "clip": clip},
        "track_id": track_id,
        "at": {"time": round(start, 6)},
        "ripple": False,
        "provenance": {"verb": "assemble_quanta", "session_id": ctx.session_id, **provenance},
    }


def _timeline_ops(
    *,
    ctx: ToolContext,
    project_state: Mapping[str, Any],
    rendered: Mapping[str, Any],
    black_asset_id: str,
    quanta_hash: str,
    total_duration: float,
) -> tuple[list[dict[str, Any]], list[str]]:
    timeline = project_state.get("timeline") if isinstance(project_state.get("timeline"), Mapping) else {}
    tracks = [item for item in timeline.get("tracks") or [] if isinstance(item, Mapping)]
    track_by_id = {str(item.get("id") or ""): item for item in tracks}
    for track_id, expected_kind in ((QUANTA_VIDEO_TRACK, "video"), (QUANTA_FRAME_TRACK, "overlay")):
        existing = track_by_id.get(track_id)
        if existing is not None and str(existing.get("kind") or "") != expected_kind:
            raise QuantaMaterializeError(
                f"dedicated track {track_id} has kind {existing.get('kind')!r}, expected {expected_kind!r}"
            )

    ops: list[dict[str, Any]] = []
    for clip in timeline.get("clips") or []:
        if isinstance(clip, Mapping) and str(clip.get("track_id") or "") in {
            QUANTA_VIDEO_TRACK, QUANTA_FRAME_TRACK,
        }:
            ops.append({"op": "delete_clip", "clip_id": str(clip.get("id") or "")})
    if QUANTA_VIDEO_TRACK not in track_by_id:
        ops.append({"op": "add_track", "kind": "video", "track_id": QUANTA_VIDEO_TRACK, "name": "Quanta Base"})
    if QUANTA_FRAME_TRACK not in track_by_id:
        ops.append({"op": "add_track", "kind": "overlay", "track_id": QUANTA_FRAME_TRACK, "name": "Quanta Frames"})

    clip_ids: list[str] = []
    background_clip_id = f"quanta_{quanta_hash}_background"
    ops.append(_insert_op(
        ctx=ctx,
        track_id=QUANTA_VIDEO_TRACK,
        clip_id=background_clip_id,
        asset=_asset_payload(ctx, black_asset_id, duration=total_duration),
        media_kind="video",
        start=0.0,
        duration=total_duration,
        provenance={"quanta_hash": quanta_hash, "role": "background"},
    ))
    clip_ids.append(background_clip_id)

    cursor = 0.0
    for index, frame in enumerate(rendered.get("frames") or []):
        if not isinstance(frame, Mapping):
            raise QuantaMaterializeError("rendered frame manifest is malformed")
        asset_id = str(frame.get("asset_id") or "")
        dwell = float(frame.get("dwell_sec") or 0.0)
        if dwell <= 0:
            raise QuantaMaterializeError(f"frame {index} has non-positive dwell")
        clip_id = f"quanta_{quanta_hash}_frame_{index:04d}"
        ops.append(_insert_op(
            ctx=ctx,
            track_id=QUANTA_FRAME_TRACK,
            clip_id=clip_id,
            asset=_asset_payload(ctx, asset_id, duration=dwell),
            media_kind="image",
            start=cursor,
            duration=dwell,
            provenance={
                "quanta_hash": quanta_hash,
                "scope_id": str(frame.get("scope_id") or ""),
                "state_id": str(frame.get("state_id") or ""),
            },
        ))
        clip_ids.append(clip_id)
        cursor += dwell
    return ops, clip_ids


async def dispatch(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    project = _project(ctx)
    state = project.load()
    quanta = lift_flat_quanta(state.get("quanta") if isinstance(state.get("quanta"), Mapping) else None)
    if not leaf_walk(quanta):
        raise QuantaMaterializeError("quanta is empty — call draft_quanta or set_quanta first")
    timeline = state.get("timeline") if isinstance(state.get("timeline"), Mapping) else {}
    width = int(timeline.get("width") or 1920)
    height = int(timeline.get("height") or 1080)
    fps = float(timeline.get("fps") or 30.0)
    if (width, height) != (1920, 1080):
        raise QuantaMaterializeError(
            f"quanta v1 timeline must be 1920x1080, got {width}x{height}"
        )
    strict = bool(args.get("fail_on_overflow", False))
    digest = _quanta_hash(quanta)
    cache_key = quanta_frame_cache_key(quanta)
    rendered = _valid_cached_frames(ctx, cache_key)
    if rendered is None:
        rendered = materialize_quanta_frame_assets(
            quanta, ctx, scale=1, fail_on_overflow=strict
        )
        ctx.extra["quanta_frame_cache"] = {"key": cache_key, "result": rendered}
    elif strict and rendered.get("overflow"):
        raise QuantaMaterializeError("cached quanta frames contain overflow; refine the copy")
    frames = rendered.get("frames") or []
    if not frames:
        raise QuantaMaterializeError("quanta produced no build frames")
    total_duration = sum(float(frame.get("dwell_sec") or 0.0) for frame in frames)
    if total_duration <= 0:
        raise QuantaMaterializeError("quanta total dwell must be positive")
    black_key = f"{width}x{height}:{fps:g}:{total_duration:.6f}"
    black_asset_id = await _ensure_black_video(
        ctx,
        duration=total_duration,
        width=width,
        height=height,
        fps=fps,
        cache_key=black_key,
    )
    ops, clip_ids = _timeline_ops(
        ctx=ctx,
        project_state=state,
        rendered=rendered,
        black_asset_id=black_asset_id,
        quanta_hash=digest,
        total_duration=total_duration,
    )
    result = project.apply_ops(ops, label="assemble_quanta")
    transitions = [
        str(scope.get("id") or "")
        for scope in flat_view(quanta).get("slides") or []
        if str((scope.get("transition") or {}).get("kind") or "cut") == "fade"
    ]
    discarded_edges = flattened_interactions(quanta)
    degradations = []
    if transitions:
        degradations.append({"kind": "fade_to_cut", "scope_ids": transitions})
    if discarded_edges:
        # Flattening cannot wait for a click — a media fact, acknowledged
        # explicitly rather than silently dropped (quanta-kernel-plan §3).
        degradations.append({
            "kind": "interaction_flattened",
            "quantum_ids": list(discarded_edges),
        })
    return {
        **rendered,
        "assembled": True,
        "seq": result.get("patch_seq_end"),
        "quanta_hash": digest,
        "background_asset_id": black_asset_id,
        "clip_ids": clip_ids,
        "total_duration_sec": total_duration,
        "timeline": project.compact_text(),
        "degradations": degradations,
        "summary": (
            f"assembled {rendered.get('scope_count')} scope(s) / "
            f"{rendered.get('frame_count')} state(s) onto the timeline "
            f"({total_duration:.1f}s)"
        ),
    }


__all__ = [
    "QUANTA_FRAME_TRACK",
    "QUANTA_VIDEO_TRACK",
    "quanta_frame_cache_key",
    "dispatch",
]
