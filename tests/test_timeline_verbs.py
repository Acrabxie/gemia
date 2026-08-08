"""M3 regression: the timeline_* verb layer end-to-end through ToolContext.

These exercise the dispatchers in ``gemia.tools.timeline`` against a real
``ProjectHandle`` (real ProjectStore on disk), one verb call per patch, the
way the agent loop drives them. Op semantics themselves are covered in
``test_timeline_patches.py``; here we assert the verb adapters build the right
ops, return the post-state summary, and respect the ripple-off default.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from gemia.project_store import ProjectHandle
from gemia.tools import DISPATCHER
from gemia.tools._context import AssetRegistry, ToolContext


def _ctx(tmp_path: Path) -> ToolContext:
    registry = AssetRegistry()
    handle = ProjectHandle.open(tmp_path / "project", "v3-verbtest01", session_id="v3-verbtest01")
    return ToolContext(
        session_id="v3-verbtest01",
        output_dir=tmp_path,
        registry=registry,
        emit_progress=lambda _u: None,
        project=handle,
    )


def _call(verb: str, args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    return asyncio.run(DISPATCHER[verb](args, ctx))


def _register_video(ctx: ToolContext, path: str) -> str:
    rec = ctx.registry.add_external(Path(path), summary="probe clip")
    return rec.asset_id


def _clips(ctx: ToolContext) -> list[dict[str, Any]]:
    return ctx.project.load()["timeline"]["clips"]


# ── insert / get ────────────────────────────────────────────────────────


def test_insert_video_appends_and_returns_summary(sample_video_path: str, tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    aid = _register_video(ctx, sample_video_path)

    out = _call("timeline_insert_clip", {"asset_id": aid}, ctx)

    assert out["applied"] is True
    assert out["clip_id"].startswith("clip_")
    assert out["track_id"] == "V1"
    assert out["seq"] == 1
    clips = _clips(ctx)
    assert len(clips) == 1
    assert clips[0]["start"] == 0.0
    assert clips[0]["duration"] == pytest.approx(2.0, abs=0.2)

    timeline = _call("get_timeline", {}, ctx)
    assert timeline["timeline"]["clip_count"] == 1


def test_insert_text_defaults_to_overlay_track(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)

    out = _call(
        "timeline_insert_clip",
        {"text": {"content": "Hello", "color": "#ff0000"}, "duration": 2.5},
        ctx,
    )

    assert out["track_id"] == "OV1"
    clips = _clips(ctx)
    assert len(clips) == 1
    clip = clips[0]
    assert clip["media_kind"] == "text"
    assert clip["text_config"]["content"] == "Hello"
    assert clip["duration"] == 2.5
    tracks = {t["id"]: t for t in ctx.project.load()["timeline"]["tracks"]}
    assert "OV1" in tracks and tracks["OV1"]["kind"] == "overlay"


def test_insert_audio_asset_lands_on_audio_track(tmp_path: Path) -> None:
    # M6: audio assets are now first-class — they resolve to the default A1
    # audio track (auto-created when absent) instead of being rejected.
    import subprocess

    ctx = _ctx(tmp_path)
    wav = tmp_path / "tone.wav"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=2", str(wav)],
        capture_output=True,
        check=True,
    )
    aid = ctx.registry.add_external(wav, summary="audio").asset_id

    out = _call("timeline_insert_clip", {"asset_id": aid}, ctx)

    assert out["applied"] is True
    assert out["track_id"] == "A1"
    clips = _clips(ctx)
    assert len(clips) == 1
    assert clips[0]["media_kind"] == "audio"
    assert clips[0]["track_id"] == "A1"
    assert clips[0]["duration"] == pytest.approx(2.0, abs=0.2)


# ── split / trim / move / delete ─────────────────────────────────────────


def test_split_keeps_asset_identity_and_returns_new_id(sample_video_path: str, tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    aid = _register_video(ctx, sample_video_path)
    first = _call("timeline_insert_clip", {"asset_id": aid}, ctx)["clip_id"]

    out = _call("timeline_split_clip", {"clip_id": first, "at_time": 1.0}, ctx)

    new_id = out["new_clip_id"]
    assert new_id != first
    clips = {c["id"]: c for c in _clips(ctx)}
    assert len(clips) == 2
    # Both halves keep the same asset; identity is clip_id + source range.
    assert clips[first]["asset_id"] == clips[new_id]["asset_id"] == aid
    assert clips[first]["source_out"] == pytest.approx(clips[new_id]["source_in"], abs=1e-3)


def test_trim_default_no_ripple_does_not_move_neighbour(sample_video_path: str, tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    aid = _register_video(ctx, sample_video_path)
    a = _call("timeline_insert_clip", {"asset_id": aid}, ctx)["clip_id"]
    b = _call("timeline_insert_clip", {"asset_id": aid}, ctx)["clip_id"]
    before = {c["id"]: c["start"] for c in _clips(ctx)}

    # Shrink A from the front; ripple off => B must not move.
    _call("timeline_trim_clip", {"clip_id": a, "source_in": 0.5}, ctx)

    after = {c["id"]: c["start"] for c in _clips(ctx)}
    assert after[b] == before[b]


def test_delete_ripple_closes_gap(sample_video_path: str, tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    aid = _register_video(ctx, sample_video_path)
    a = _call("timeline_insert_clip", {"asset_id": aid}, ctx)["clip_id"]
    b = _call("timeline_insert_clip", {"asset_id": aid}, ctx)["clip_id"]

    _call("timeline_delete_clip", {"clip_id": a, "ripple": True}, ctx)

    clips = _clips(ctx)
    assert len(clips) == 1
    assert clips[0]["id"] == b
    assert clips[0]["start"] == 0.0  # shifted left into the freed slot


# ── undo ──────────────────────────────────────────────────────────────────


def test_undo_reverts_last_verb(sample_video_path: str, tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    aid = _register_video(ctx, sample_video_path)
    _call("timeline_insert_clip", {"asset_id": aid}, ctx)
    assert len(_clips(ctx)) == 1

    out = _call("timeline_undo", {"steps": 1}, ctx)

    assert out["to_seq"] == 0
    assert _clips(ctx) == []


# ── render preview ──────────────────────────────────────────────────────


def test_render_preview_registers_video_asset(sample_video_path: str, tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    aid = _register_video(ctx, sample_video_path)
    _call("timeline_insert_clip", {"asset_id": aid}, ctx)

    out = _call("render_preview", {"label": "m3-smoke"}, ctx)

    assert out["asset_id"] is not None
    assert ctx.registry.contains(out["asset_id"])
    assert out["duration"] == pytest.approx(2.0, abs=0.3)


def test_inspect_timeline_registers_composited_frame(sample_video_path: str, tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    aid = _register_video(ctx, sample_video_path)
    _call("timeline_insert_clip", {"asset_id": aid}, ctx)

    out = _call("inspect_timeline", {"time_sec": 0.5, "label": "inspect-smoke"}, ctx)

    assert ctx.registry.contains(out["preview_asset_id"])
    assert len(out["frame_asset_ids"]) == 1
    frame_id = out["frame_asset_ids"][0]
    assert ctx.registry.contains(frame_id)
    assert ctx.registry.get(frame_id).path.exists()
    assert out["sample_times"][0] == pytest.approx(0.5, abs=0.05)
    assert out["thumbnail_for_next_message"] is True
    assert Path(out["thumbnail_path"]).exists()


def test_inspect_timeline_accepts_explicit_sample_times(
    sample_video_path: str, tmp_path: Path
) -> None:
    ctx = _ctx(tmp_path)
    aid = _register_video(ctx, sample_video_path)
    _call("timeline_insert_clip", {"asset_id": aid}, ctx)

    out = _call(
        "inspect_timeline",
        {"times_sec": [0.25, 1.25], "label": "inspect-explicit"},
        ctx,
    )

    assert out["sample_times"] == pytest.approx([0.25, 1.25], abs=0.05)
    assert len(out["frame_asset_ids"]) == 2


def test_inspect_timeline_normalizes_legacy_all_fields_shape(
    sample_video_path: str, tmp_path: Path
) -> None:
    ctx = _ctx(tmp_path)
    aid = _register_video(ctx, sample_video_path)
    _call("timeline_insert_clip", {"asset_id": aid}, ctx)

    out = _call(
        "inspect_timeline",
        {
            "frame": 0,
            "time_sec": 0,
            "time": 0,
            "start_frame": 0,
            "end_frame": 60,
            "start_sec": 0,
            "end_sec": 2,
            "start": 0,
            "end": 2,
            "max_frames": 2,
            "label": "inspect-legacy",
        },
        ctx,
    )

    assert out["sample_times"] == pytest.approx([0.5, 1.5], abs=0.08)
    assert len(out["frame_asset_ids"]) == 2


# ── M7: track-level ducking verb ─────────────────────────────────────────


def test_set_track_duck_under_via_verb(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    _call("timeline_add_track", {"kind": "audio"}, ctx)  # A2 (A1 is default)

    out = _call("timeline_set_track", {"track_id": "A1", "duck_under": "A2"}, ctx)
    assert out["applied"] is True
    tracks = {t["id"]: t for t in ctx.project.load()["timeline"]["tracks"]}
    assert tracks["A1"]["duck_under"] == "A2"

    # Clearing it sets the field back to None.
    _call("timeline_set_track", {"track_id": "A1", "duck_under": None}, ctx)
    tracks = {t["id"]: t for t in ctx.project.load()["timeline"]["tracks"]}
    assert tracks["A1"]["duck_under"] is None


def test_add_transition_result_carries_export_honesty_note(
    sample_video_path: str, tmp_path: Path
) -> None:
    """Export honesty (docs/timeline-canonical-plan.md §4): kinds the export
    cannot render yet (wipe) carry export_note + warnings; kinds that DO
    render since Phase 1 (fade/dissolve, §5.2) carry neither."""
    ctx = _ctx(tmp_path)
    aid = _register_video(ctx, sample_video_path)
    cid = _call("timeline_insert_clip", {"asset_id": aid}, ctx)["clip_id"]
    # add_transition validates that a following clip exists on the track.
    _call("timeline_insert_clip", {"asset_id": aid}, ctx)

    out = _call(
        "timeline_add_transition",
        {"clip_id": cid, "kind": "wipe", "duration_sec": 0.5},
        ctx,
    )
    assert out["applied"] is True
    assert "hard cut" in out["export_note"]
    assert any(w.startswith("W_NOT_EXPORTED:transition_after:") for w in out["warnings"])

    # dissolve renders on export since Phase 1 — no caveat, no warnings.
    out_dissolve = _call(
        "timeline_add_transition",
        {"clip_id": cid, "kind": "dissolve", "duration_sec": 0.5},
        ctx,
    )
    assert out_dissolve["applied"] is True
    assert "export_note" not in out_dissolve
    assert "warnings" not in out_dissolve

    # A plain cut clears the transition and needs no caveat.
    out_cut = _call("timeline_add_transition", {"clip_id": cid, "kind": "cut"}, ctx)
    assert "export_note" not in out_cut
    assert "warnings" not in out_cut
