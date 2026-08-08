"""M6 regression: multi-track audio into the execution surface.

Covers the audio path end-to-end the way the agent loop drives it:
  * patch-level media/track matching (E_TRACK_KIND both directions);
  * the verb surface (add audio track, insert audio clip, audio attributes
    via timeline_set_clip_effects);
  * the renderer's third pass — a project with audio exports an MP4 carrying
    a real AAC stream, two audio sources mix, embedded video audio is kept by
    default and dropped when the clip is muted;
  * the backward-compat invariant — a project with no audio exports a silent
    video with no audio stream, exactly as before.

All tests use real ffmpeg (testsrc2 / lavfi sine), a real ProjectStore on
disk, and the DISPATCHER verb layer. Audio-clip OTIO round-trip lives in
test_timeline_m5_otio.py (M6-D).
"""
from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import Any

import pytest

from gemia.project_model import empty_project, normalize_project
from gemia.project_store import ProjectHandle
from gemia.tools import DISPATCHER
from gemia.tools._context import AssetRegistry, ToolContext
from gemia.tools._ffmpeg import audio_stream, ffprobe_metadata
from lumerai.patches import TimelinePatchError, apply_timeline_patches


# ── fixtures ─────────────────────────────────────────────────────────────────


def _make_ctx(tmp_path: Path, session: str) -> ToolContext:
    registry = AssetRegistry()
    handle = ProjectHandle.open(tmp_path / "project", f"m6{session}", session_id=session)
    return ToolContext(
        session_id=session,
        output_dir=tmp_path,
        registry=registry,
        emit_progress=lambda _u: None,
        project=handle,
    )


def _call(verb: str, args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    return asyncio.run(DISPATCHER[verb](args, ctx))


def _clips(ctx: ToolContext) -> list[dict[str, Any]]:
    return ctx.project.load()["timeline"]["clips"]


def _silent_video(tmp_path: Path, name: str = "v.mp4", duration: float = 3.0) -> Path:
    out = tmp_path / name
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi",
         "-i", f"testsrc2=duration={duration}:size=128x128:rate=15",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(out)],
        capture_output=True, check=True,
    )
    return out


def _audio_video(tmp_path: Path, name: str = "talk.mp4", duration: float = 3.0, freq: int = 440) -> Path:
    """A video clip that carries an embedded audio stream (talking head)."""
    out = tmp_path / name
    subprocess.run(
        ["ffmpeg", "-y",
         "-f", "lavfi", "-i", f"testsrc2=duration={duration}:size=128x128:rate=15",
         "-f", "lavfi", "-i", f"sine=frequency={freq}:duration={duration}",
         "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(out)],
        capture_output=True, check=True,
    )
    return out


def _wav(tmp_path: Path, name: str, duration: float = 3.0, freq: int = 440) -> Path:
    out = tmp_path / name
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi",
         "-i", f"sine=frequency={freq}:duration={duration}", str(out)],
        capture_output=True, check=True,
    )
    return out


def _register(ctx: ToolContext, path: Path) -> str:
    return ctx.registry.add_external(path, summary="m6 test asset").asset_id


def _audio_stream_of(export_path: str) -> dict[str, Any] | None:
    return audio_stream(ffprobe_metadata(Path(export_path)))


# ── patch-level media/track matching ───────────────────────────────────────────


def test_audio_clip_on_video_track_rejected() -> None:
    project = normalize_project(empty_project(title="m6"))  # default [V1, A1]
    op = {
        "op": "insert_clip",
        "track_id": "V1",
        "data": {"clip": {
            "id": "ac", "asset_id": "aud", "media_kind": "audio",
            "duration": 2.0, "source_in": 0.0, "source_out": 2.0,
        }},
    }
    with pytest.raises(TimelinePatchError) as exc:
        apply_timeline_patches(project, [{"version": 1, "ops": [op]}])
    assert exc.value.code == "E_TRACK_KIND"


def test_video_clip_on_audio_track_rejected() -> None:
    project = normalize_project(empty_project(title="m6"))
    op = {
        "op": "insert_clip",
        "track_id": "A1",
        "data": {"clip": {
            "id": "vc", "asset_id": "vid", "media_kind": "video",
            "duration": 2.0, "source_in": 0.0, "source_out": 2.0,
        }},
    }
    with pytest.raises(TimelinePatchError) as exc:
        apply_timeline_patches(project, [{"version": 1, "ops": [op]}])
    assert exc.value.code == "E_TRACK_KIND"


def test_audio_clip_on_audio_track_accepted() -> None:
    project = normalize_project(empty_project(title="m6"))
    op = {
        "op": "insert_clip",
        "track_id": "A1",
        "data": {"clip": {
            "id": "ac", "asset_id": "aud", "media_kind": "audio",
            "duration": 2.0, "source_in": 0.0, "source_out": 2.0,
        }},
    }
    updated = apply_timeline_patches(project, [{"version": 1, "ops": [op]}])
    clip = updated["timeline"]["clips"][0]
    assert clip["track_id"] == "A1"
    assert clip["media_kind"] == "audio"
    assert clip["duration"] == pytest.approx(2.0)


# ── verb surface ────────────────────────────────────────────────────────────


def test_add_audio_track_and_insert_audio_clip(tmp_path: Path) -> None:
    ctx = _make_ctx(tmp_path, "addaud")
    out = _call("timeline_add_track", {"kind": "audio"}, ctx)  # A2 (A1 is default)
    track_ids = [t["id"] for t in ctx.project.load()["timeline"]["tracks"]]
    assert track_ids == ["V1", "A1", "A2"]

    wav = _wav(tmp_path, "tone.wav", duration=2.0)
    r = _call("timeline_insert_clip", {"asset_id": _register(ctx, wav), "track_id": "A2"}, ctx)
    assert r["track_id"] == "A2"
    clip = _clips(ctx)[0]
    assert clip["media_kind"] == "audio"
    assert clip["track_id"] == "A2"
    assert clip["duration"] == pytest.approx(2.0, abs=0.2)


def test_insert_audio_auto_resolves_default_audio_track(tmp_path: Path) -> None:
    ctx = _make_ctx(tmp_path, "autoaud")
    wav = _wav(tmp_path, "tone.wav", duration=2.0)
    r = _call("timeline_insert_clip", {"asset_id": _register(ctx, wav)}, ctx)
    assert r["track_id"] == "A1"  # default audio track, no add_track needed


def test_audio_attributes_set_via_verb(tmp_path: Path) -> None:
    ctx = _make_ctx(tmp_path, "fx")
    wav = _wav(tmp_path, "tone.wav", duration=2.0)
    r = _call("timeline_insert_clip", {"asset_id": _register(ctx, wav)}, ctx)
    cid = r["clip_id"]
    _call(
        "timeline_set_clip_effects",
        {"clip_id": cid, "effects": {"gain_db": -3.5, "fade_in": 0.4, "fade_out": 0.6, "muted": True}},
        ctx,
    )
    fx = next(c for c in _clips(ctx) if c["id"] == cid)["effects"]
    assert fx["gain_db"] == pytest.approx(-3.5)
    assert fx["fade_in"] == pytest.approx(0.4)
    assert fx["fade_out"] == pytest.approx(0.6)
    assert fx["muted"] is True


def test_negative_fade_is_bad_arg(tmp_path: Path) -> None:
    ctx = _make_ctx(tmp_path, "badfade")
    wav = _wav(tmp_path, "tone.wav", duration=2.0)
    r = _call("timeline_insert_clip", {"asset_id": _register(ctx, wav)}, ctx)
    with pytest.raises(TimelinePatchError) as exc:
        _call("timeline_set_clip_effects", {"clip_id": r["clip_id"], "effects": {"fade_in": -1.0}}, ctx)
    assert exc.value.code == "E_BAD_ARG"


def test_trim_audio_clip(tmp_path: Path) -> None:
    ctx = _make_ctx(tmp_path, "trim")
    wav = _wav(tmp_path, "tone.wav", duration=5.0)
    r = _call("timeline_insert_clip", {"asset_id": _register(ctx, wav)}, ctx)
    cid = r["clip_id"]
    _call("timeline_trim_clip", {"clip_id": cid, "source_in": 1.0, "source_out": 3.0}, ctx)
    clip = next(c for c in _clips(ctx) if c["id"] == cid)
    assert clip["source_in"] == pytest.approx(1.0)
    assert clip["source_out"] == pytest.approx(3.0)
    assert clip["duration"] == pytest.approx(2.0)


# ── renderer audio pass ───────────────────────────────────────────────────────


def test_export_single_audio_track_has_audio_stream(tmp_path: Path) -> None:
    ctx = _make_ctx(tmp_path, "exp1")
    _call("timeline_insert_clip", {"asset_id": _register(ctx, _silent_video(tmp_path, "v.mp4", 3.0))}, ctx)
    _call("timeline_insert_clip", {"asset_id": _register(ctx, _wav(tmp_path, "m.wav", 3.0))}, ctx)  # A1

    out = _call("project_export", {"quality": "draft", "label": "exp1"}, ctx)

    assert out["has_audio"] is True
    assert out["audio_clips"] == 1
    assert _audio_stream_of(out["export_path"]) is not None
    timeline_dur = ctx.project.load()["timeline"]["duration"]
    # Rendered AAC/MP4 duration is not millisecond-exact (codec priming +
    # container rounding); assert it tracks the timeline within a small margin.
    assert abs(out["duration"] - timeline_dur) < 0.3


def test_export_mixes_music_and_voiceover(tmp_path: Path) -> None:
    ctx = _make_ctx(tmp_path, "mix")
    _call("timeline_insert_clip", {"asset_id": _register(ctx, _silent_video(tmp_path, "v.mp4", 4.0))}, ctx)
    # Music bed on A1 (default), voiceover on A2 — separate tracks so they overlap.
    _call("timeline_insert_clip", {"asset_id": _register(ctx, _wav(tmp_path, "music.wav", 4.0, freq=220))}, ctx)
    _call("timeline_add_track", {"kind": "audio"}, ctx)
    r = _call(
        "timeline_insert_clip",
        {"asset_id": _register(ctx, _wav(tmp_path, "vo.wav", 2.0, freq=880)), "track_id": "A2", "at_time": 1.0},
        ctx,
    )
    assert r["track_id"] == "A2"

    out = _call("project_export", {"quality": "draft", "label": "mix"}, ctx)

    assert out["has_audio"] is True
    assert out["audio_clips"] == 2
    assert _audio_stream_of(out["export_path"]) is not None


def test_export_audio_master_hits_delivery_gate_after_aac(tmp_path: Path) -> None:
    from gemia.project_export import _measure_loudness

    ctx = _make_ctx(tmp_path, "master")
    _call(
        "timeline_insert_clip",
        {"asset_id": _register(ctx, _silent_video(tmp_path, "v.mp4", 6.0))},
        ctx,
    )
    _call(
        "timeline_insert_clip",
        {"asset_id": _register(ctx, _wav(tmp_path, "m.wav", 6.0, freq=440))},
        ctx,
    )

    out = _call("project_export", {"quality": "draft", "label": "master"}, ctx)
    measured = _measure_loudness(Path(out["export_path"]), timeout_sec=30)

    assert measured["status"] == "passed"
    assert -17.0 <= measured["integrated_loudness_lufs"] <= -15.0
    assert measured["true_peak_dbtp"] <= -1.0


def test_embedded_video_audio_preserved_by_default(tmp_path: Path) -> None:
    ctx = _make_ctx(tmp_path, "embed")
    _call("timeline_insert_clip", {"asset_id": _register(ctx, _audio_video(tmp_path, "talk.mp4", 3.0))}, ctx)

    out = _call("project_export", {"quality": "draft", "label": "embed"}, ctx)

    # The talking-head clip keeps its voice even though it sits on a video track.
    assert out["has_audio"] is True
    assert out["audio_clips"] == 0  # not an audio-track clip; embedded source
    assert _audio_stream_of(out["export_path"]) is not None


def test_embedded_video_audio_dropped_when_muted(tmp_path: Path) -> None:
    ctx = _make_ctx(tmp_path, "muteembed")
    r = _call("timeline_insert_clip", {"asset_id": _register(ctx, _audio_video(tmp_path, "talk.mp4", 3.0))}, ctx)
    _call("timeline_set_clip_effects", {"clip_id": r["clip_id"], "effects": {"muted": True}}, ctx)

    out = _call("project_export", {"quality": "draft", "label": "muteembed"}, ctx)

    # Muting the only audio source returns the export to the silent path.
    assert out["has_audio"] is False
    assert _audio_stream_of(out["export_path"]) is None


# ── backward compatibility ──────────────────────────────────────────────────


def test_no_audio_project_exports_silent_video(tmp_path: Path) -> None:
    ctx = _make_ctx(tmp_path, "silent")
    _call("timeline_insert_clip", {"asset_id": _register(ctx, _silent_video(tmp_path, "v.mp4", 2.0))}, ctx)

    out = _call("project_export", {"quality": "draft", "label": "silent"}, ctx)

    assert out["has_audio"] is False
    assert out["audio_clips"] == 0
    assert _audio_stream_of(out["export_path"]) is None
    assert out["duration"] > 0.0
