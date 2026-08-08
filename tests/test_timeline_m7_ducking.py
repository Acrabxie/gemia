"""M7 regression: track-level ducking + export-duration master contract.

Builds on M6 audio. Two concerns:
  * M7-A — export length is deterministic = project timeline.duration (the
    audio-inclusive master). A music bed longer than the last video clip plays
    over a black tail to the timeline end rather than over-running.
  * M7-B..D — a track may declare ``duck_under = <trigger>``; the renderer
    sidechain-compresses that bed's submix whenever the trigger is loud
    (mirrors gemia/tools/mix_audio.py duck mode).

Real ffmpeg, real ProjectStore, DISPATCHER verb layer.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import Any

from gemia.project_store import ProjectHandle
from gemia.tools import DISPATCHER
from gemia.tools._context import AssetRegistry, ToolContext
from gemia.tools._ffmpeg import audio_stream, ffprobe_metadata, video_stream

# ── fixtures ─────────────────────────────────────────────────────────────────


def _make_ctx(tmp_path: Path, session: str) -> ToolContext:
    registry = AssetRegistry()
    handle = ProjectHandle.open(
        tmp_path / "project", f"m7{session}", session_id=session
    )
    return ToolContext(
        session_id=session,
        output_dir=tmp_path,
        registry=registry,
        emit_progress=lambda _u: None,
        project=handle,
    )


def _call(verb: str, args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    return asyncio.run(DISPATCHER[verb](args, ctx))


def _silent_video(tmp_path: Path, name: str = "v.mp4", duration: float = 2.0) -> Path:
    out = tmp_path / name
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"testsrc2=duration={duration}:size=128x128:rate=15",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(out),
        ],
        capture_output=True,
        check=True,
    )
    return out


def _wav(tmp_path: Path, name: str, duration: float = 3.0, freq: int = 440) -> Path:
    out = tmp_path / name
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency={freq}:duration={duration}",
            str(out),
        ],
        capture_output=True,
        check=True,
    )
    return out


def _register(ctx: ToolContext, path: Path) -> str:
    return ctx.registry.add_external(path, summary="m7 test asset").asset_id


def _meta(export_path: str) -> dict[str, Any]:
    return ffprobe_metadata(Path(export_path))


# ── M7-A: export-duration master contract ──────────────────────────────────────


def test_export_audio_longer_than_video_pads_to_timeline(tmp_path: Path) -> None:
    """A music bed past the last video clip extends the export to timeline.duration
    (video padded black) with both a video and an audio stream."""
    ctx = _make_ctx(tmp_path, "durpad")
    _call(
        "timeline_insert_clip",
        {"asset_id": _register(ctx, _silent_video(tmp_path, "v.mp4", 2.0))},
        ctx,
    )
    _call(
        "timeline_insert_clip",
        {"asset_id": _register(ctx, _wav(tmp_path, "m.wav", 5.0))},
        ctx,
    )  # A1, 0-5s

    timeline_dur = ctx.project.load()["timeline"]["duration"]
    assert abs(timeline_dur - 5.0) < 1e-3

    out = _call("project_export", {"quality": "draft", "label": "durpad"}, ctx)
    meta = _meta(out["export_path"])
    assert video_stream(meta) is not None
    assert audio_stream(meta) is not None
    # Master length is timeline.duration; rendered MP4 is within codec/container margin.
    assert abs(out["duration"] - timeline_dur) < 0.3


def test_export_audio_shorter_than_video_unchanged(tmp_path: Path) -> None:
    """Audio shorter than the video leaves the video length as master (no padding)."""
    ctx = _make_ctx(tmp_path, "durshort")
    _call(
        "timeline_insert_clip",
        {"asset_id": _register(ctx, _silent_video(tmp_path, "v.mp4", 4.0))},
        ctx,
    )
    _call(
        "timeline_insert_clip",
        {"asset_id": _register(ctx, _wav(tmp_path, "m.wav", 2.0))},
        ctx,
    )  # A1, 0-2s

    timeline_dur = ctx.project.load()["timeline"]["duration"]
    assert abs(timeline_dur - 4.0) < 1e-3  # video end is the master

    out = _call("project_export", {"quality": "draft", "label": "durshort"}, ctx)
    meta = _meta(out["export_path"])
    audio = audio_stream(meta)
    assert audio is not None
    assert abs(out["duration"] - 4.0) < 0.3
    assert abs(float(audio.get("duration") or 0.0) - 4.0) < 0.1


# ── M7-D: ducking export path ────────────────────────────────────────────────


def test_export_with_duck_under_produces_audio_output(tmp_path: Path) -> None:
    """A bed track (A2) set to duck_under the voice track (A1) exports without error
    and the result contains both video and audio streams. Verifies the track_id
    field flows from clips through _collect_audio_sources into _resolve_duck_map
    so the sidechain path activates without crashing."""
    ctx = _make_ctx(tmp_path, "duck1")
    _call(
        "timeline_insert_clip",
        {"asset_id": _register(ctx, _silent_video(tmp_path, "v.mp4", 3.0))},
        ctx,
    )
    # Voice on A1
    _call(
        "timeline_insert_clip",
        {
            "asset_id": _register(ctx, _wav(tmp_path, "voice.wav", 3.0, freq=880)),
            "track_id": "A1",
        },
        ctx,
    )
    # Music bed on a second audio track
    _call("timeline_add_track", {"kind": "audio", "name": "Music"}, ctx)
    project_state = ctx.project.load()
    a2_id = next(
        t["id"]
        for t in project_state["timeline"]["tracks"]
        if t.get("kind") == "audio" and t["id"] != "A1"
    )
    _call(
        "timeline_insert_clip",
        {
            "asset_id": _register(ctx, _wav(tmp_path, "music.wav", 3.0, freq=220)),
            "track_id": a2_id,
        },
        ctx,
    )
    # Set the music bed to duck under the voice track
    _call("timeline_set_track", {"track_id": a2_id, "duck_under": "A1"}, ctx)

    # Verify duck_under is persisted
    state = ctx.project.load()
    a2_track = next(t for t in state["timeline"]["tracks"] if t["id"] == a2_id)
    assert a2_track.get("duck_under") == "A1"

    out = _call("project_export", {"quality": "draft", "label": "duck"}, ctx)
    meta = _meta(out["export_path"])
    assert video_stream(meta) is not None
    assert audio_stream(meta) is not None
    assert out["duration"] > 0.5


def test_shorter_duck_trigger_cannot_truncate_longer_music_bed(
    tmp_path: Path,
) -> None:
    """Regression for Echo V1: A2 ends early while ducked A1 reaches picture end."""

    ctx = _make_ctx(tmp_path, "ducktail")
    _call(
        "timeline_insert_clip",
        {"asset_id": _register(ctx, _silent_video(tmp_path, "v.mp4", 4.0))},
        ctx,
    )
    _call(
        "timeline_insert_clip",
        {"asset_id": _register(ctx, _wav(tmp_path, "music.wav", 4.0, freq=220))},
        ctx,
    )
    _call("timeline_add_track", {"kind": "audio", "name": "Voice"}, ctx)
    voice_track = next(
        track["id"]
        for track in ctx.project.load()["timeline"]["tracks"]
        if track.get("kind") == "audio" and track["id"] != "A1"
    )
    _call(
        "timeline_insert_clip",
        {
            "asset_id": _register(ctx, _wav(tmp_path, "voice.wav", 2.0, freq=880)),
            "track_id": voice_track,
            "at_time": 1.0,
        },
        ctx,
    )
    _call(
        "timeline_set_track",
        {"track_id": "A1", "duck_under": voice_track},
        ctx,
    )

    out = _call("project_export", {"quality": "draft", "label": "ducktail"}, ctx)
    audio = audio_stream(_meta(out["export_path"]))
    assert audio is not None
    assert abs(float(audio.get("duration") or 0.0) - 4.0) < 0.1
    tail_db = _mean_volume_db(out["export_path"], 3.25, 3.8)
    assert tail_db is not None and tail_db > -80.0


def test_one_short_trigger_can_feed_two_full_length_ducked_beds(
    tmp_path: Path,
) -> None:
    ctx = _make_ctx(tmp_path, "ducktwobeds")
    _call(
        "timeline_insert_clip",
        {"asset_id": _register(ctx, _silent_video(tmp_path, "v.mp4", 4.0))},
        ctx,
    )
    _call(
        "timeline_insert_clip",
        {"asset_id": _register(ctx, _wav(tmp_path, "bed1.wav", 4.0, freq=220))},
        ctx,
    )
    _call("timeline_add_track", {"kind": "audio", "name": "Voice"}, ctx)
    _call("timeline_add_track", {"kind": "audio", "name": "Bed 2"}, ctx)
    audio_tracks = [
        track["id"]
        for track in ctx.project.load()["timeline"]["tracks"]
        if track.get("kind") == "audio"
    ]
    voice_track, bed2_track = audio_tracks[1:]
    _call(
        "timeline_insert_clip",
        {
            "asset_id": _register(ctx, _wav(tmp_path, "voice.wav", 2.0, freq=880)),
            "track_id": voice_track,
            "at_time": 1.0,
        },
        ctx,
    )
    _call(
        "timeline_insert_clip",
        {
            "asset_id": _register(ctx, _wav(tmp_path, "bed2.wav", 4.0, freq=330)),
            "track_id": bed2_track,
        },
        ctx,
    )
    _call(
        "timeline_set_track",
        {"track_id": "A1", "duck_under": voice_track},
        ctx,
    )
    _call(
        "timeline_set_track",
        {"track_id": bed2_track, "duck_under": voice_track},
        ctx,
    )

    out = _call("project_export", {"quality": "draft", "label": "twobeds"}, ctx)
    audio = audio_stream(_meta(out["export_path"]))
    assert audio is not None
    assert abs(float(audio.get("duration") or 0.0) - 4.0) < 0.1
    tail_db = _mean_volume_db(out["export_path"], 3.25, 3.8)
    assert tail_db is not None and tail_db > -80.0


def test_export_duck_under_no_active_when_trigger_absent(tmp_path: Path) -> None:
    """A bed track with duck_under set but the trigger track has no audio clips
    still exports successfully — the duck pair is silently ignored by _resolve_duck_map
    because the trigger has no audio sources."""
    ctx = _make_ctx(tmp_path, "duck2")
    _call(
        "timeline_insert_clip",
        {"asset_id": _register(ctx, _silent_video(tmp_path, "v.mp4", 2.0))},
        ctx,
    )
    # Only a music bed on A1, no voice clips on A1
    _call("timeline_add_track", {"kind": "audio", "name": "Bed"}, ctx)
    project_state = ctx.project.load()
    a2_id = next(
        t["id"]
        for t in project_state["timeline"]["tracks"]
        if t.get("kind") == "audio" and t["id"] != "A1"
    )
    _call(
        "timeline_insert_clip",
        {
            "asset_id": _register(ctx, _wav(tmp_path, "music.wav", 2.0, freq=220)),
            "track_id": a2_id,
        },
        ctx,
    )
    # duck_under A1 but A1 has no clips (trigger absent → duck silently skipped)
    _call("timeline_set_track", {"track_id": a2_id, "duck_under": "A1"}, ctx)

    out = _call("project_export", {"quality": "draft", "label": "noduck"}, ctx)
    meta = _meta(out["export_path"])
    assert audio_stream(meta) is not None
    assert out["duration"] > 0.5


def test_export_duck_under_clear_reverts_to_flat_mix(tmp_path: Path) -> None:
    """Clearing duck_under (set to None) falls back to the flat amix path."""
    ctx = _make_ctx(tmp_path, "duck3")
    _call(
        "timeline_insert_clip",
        {"asset_id": _register(ctx, _silent_video(tmp_path, "v.mp4", 2.0))},
        ctx,
    )
    _call(
        "timeline_insert_clip",
        {
            "asset_id": _register(ctx, _wav(tmp_path, "voice.wav", 2.0, freq=880)),
            "track_id": "A1",
        },
        ctx,
    )
    _call("timeline_add_track", {"kind": "audio", "name": "Music"}, ctx)
    project_state = ctx.project.load()
    a2_id = next(
        t["id"]
        for t in project_state["timeline"]["tracks"]
        if t.get("kind") == "audio" and t["id"] != "A1"
    )
    _call(
        "timeline_insert_clip",
        {
            "asset_id": _register(ctx, _wav(tmp_path, "music.wav", 2.0, freq=220)),
            "track_id": a2_id,
        },
        ctx,
    )
    _call("timeline_set_track", {"track_id": a2_id, "duck_under": "A1"}, ctx)
    # Clear it
    _call("timeline_set_track", {"track_id": a2_id, "duck_under": None}, ctx)

    state = ctx.project.load()
    a2_track = next(t for t in state["timeline"]["tracks"] if t["id"] == a2_id)
    assert a2_track.get("duck_under") is None

    out = _call("project_export", {"quality": "draft", "label": "clearduck"}, ctx)
    meta = _meta(out["export_path"])
    assert audio_stream(meta) is not None


def _mean_volume_db(path: str, ss: float, to: float) -> float | None:
    """ffmpeg volumedetect mean_volume (dB) over [ss, to], or None if unparsable."""
    import re

    proc = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-ss",
            str(ss),
            "-to",
            str(to),
            "-i",
            str(path),
            "-af",
            "volumedetect",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
    )
    m = re.search(r"mean_volume:\s*(-?[\d.]+) dB", proc.stderr)
    return float(m.group(1)) if m else None


def test_duck_attenuates_bed_in_trigger_region(tmp_path: Path) -> None:
    """Music ducked under voice is measurably quieter in the voice region than the
    same project without ducking — proves sidechaincompress attenuates the bed,
    not merely that the graph runs."""
    ctx = _make_ctx(tmp_path, "duckatt")
    _call(
        "timeline_insert_clip",
        {"asset_id": _register(ctx, _silent_video(tmp_path, "v.mp4", 4.0))},
        ctx,
    )
    # Music bed on A1, full 0-4s.
    _call(
        "timeline_insert_clip",
        {"asset_id": _register(ctx, _wav(tmp_path, "music.wav", 4.0, freq=220))},
        ctx,
    )
    # Voice on A2, 1-3s.
    _call("timeline_add_track", {"kind": "audio", "name": "Voice"}, ctx)
    a2_id = next(
        t["id"]
        for t in ctx.project.load()["timeline"]["tracks"]
        if t.get("kind") == "audio" and t["id"] != "A1"
    )
    _call(
        "timeline_insert_clip",
        {
            "asset_id": _register(ctx, _wav(tmp_path, "voice.wav", 2.0, freq=880)),
            "track_id": a2_id,
            "at_time": 1.0,
        },
        ctx,
    )

    base = _call("project_export", {"quality": "draft", "label": "base"}, ctx)
    base_bed_db = _mean_volume_db(base["export_path"], 0.0, 1.0)
    base_trigger_db = _mean_volume_db(base["export_path"], 1.0, 3.0)

    # Music (A1) ducks under the voice track.
    _call("timeline_set_track", {"track_id": "A1", "duck_under": a2_id}, ctx)
    ducked = _call("project_export", {"quality": "draft", "label": "ducked"}, ctx)
    duck_bed_db = _mean_volume_db(ducked["export_path"], 0.0, 1.0)
    duck_trigger_db = _mean_volume_db(ducked["export_path"], 1.0, 3.0)

    assert None not in {base_bed_db, base_trigger_db, duck_bed_db, duck_trigger_db}
    # Each export is independently mastered to the same LUFS target, so an
    # absolute dB comparison between files is invalid.  Compare the trigger
    # region to that file's own bed-only region: ducking must reduce the
    # trigger-region rise relative to the un-ducked mix.
    base_rise = float(base_trigger_db) - float(base_bed_db)
    ducked_rise = float(duck_trigger_db) - float(duck_bed_db)
    assert ducked_rise < base_rise - 0.5, (
        "expected sidechain ducking to reduce the trigger-region rise: "
        f"base={base_rise:.2f}dB ducked={ducked_rise:.2f}dB"
    )
