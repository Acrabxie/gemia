"""assemble_shotlist: the full outline→timeline spine, end to end.

Drives the real dispatchers the way the agent loop would: draft a shotlist,
fill shots with real (registered) footage, assemble onto the timeline, and
assert clips land in order with the planned durations, aligned text overlays,
and the shots marked ``placed``. Also covers skip-of-unfilled and rebuild.
"""
from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from typing import Any

from gemia.project_model import iter_shots
from gemia.project_store import ProjectHandle
from gemia.tools import DISPATCHER
from gemia.tools._context import AssetRegistry, ToolContext


def _make_clip(path: Path, duration: float = 2.0) -> Path:
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", f"testsrc2=duration={duration}:size=96x54:rate=15",
         "-pix_fmt", "yuv420p", str(path)],
        check=True, capture_output=True,
    )
    return path


def _ctx(tmp_path: Path) -> ToolContext:
    handle = ProjectHandle.open(tmp_path / "project", "v3-assemble", session_id="v3-assemble")
    return ToolContext(
        session_id="v3-assemble",
        output_dir=tmp_path,
        registry=AssetRegistry(),
        emit_progress=lambda _u: None,
        project=handle,
    )


def _call(verb: str, args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    return asyncio.run(DISPATCHER[verb](args, ctx))


def _clips(ctx: ToolContext) -> list[dict[str, Any]]:
    return ctx.project.load()["timeline"]["clips"]


def _fill_and_seed(ctx: ToolContext, tmp_path: Path) -> tuple[str, str]:
    a = ctx.registry.add_external(_make_clip(tmp_path / "hook.mp4"), summary="hook").asset_id
    b = ctx.registry.add_external(_make_clip(tmp_path / "reveal.mp4"), summary="reveal").asset_id
    _call("set_shotlist", {"shotlist": {
        "logline": "promo",
        "scenes": [{"id": "sc1", "title": "Main", "shots": [
            {"id": "hook", "description": "opening", "duration_sec": 2,
             "on_screen_text": "Hello", "transition_after": {"kind": "dissolve", "duration_sec": 0.5}},
            {"id": "reveal", "description": "the reveal", "duration_sec": 3},
            {"id": "outro", "description": "logo card", "duration_sec": 2},  # left unfilled
        ]}],
    }}, ctx)
    _call("update_shot", {"shot_id": "hook", "fields": {"asset_id": a, "source": "search", "status": "filled"}}, ctx)
    _call("update_shot", {"shot_id": "reveal", "fields": {"asset_id": b, "source": "search", "status": "filled"}}, ctx)
    return a, b


def test_assemble_is_real_not_stub():
    assert "assemble_shotlist" in DISPATCHER
    assert "stub" not in DISPATCHER["assemble_shotlist"].__qualname__.lower()


def test_full_spine_places_filled_shots(tmp_path):
    ctx = _ctx(tmp_path)
    _fill_and_seed(ctx, tmp_path)

    out = _call("assemble_shotlist", {}, ctx)
    assert out["assembled"] == 2, out
    # the unfilled 'outro' shot is reported, not silently dropped
    assert any(s["shot_id"] == "outro" for s in out["skipped"])

    clips = _clips(ctx)
    video = [c for c in clips if c["track_id"] == "V1"]
    assert len(video) == 2
    # placed in scene order, each trimmed to its planned duration
    assert round(video[0]["duration"], 1) == 2.0
    assert round(video[1]["duration"], 1) == 3.0
    # video clips are sequential (second starts at/after first ends)
    assert video[1]["start"] >= video[0]["start"]
    # the on_screen_text became an aligned text overlay clip
    assert any(c.get("media_kind") == "text" and
               (c.get("text_config") or {}).get("content") == "Hello" for c in clips)

    # shots are marked placed with their clip_id
    shotlist = ctx.project.load()["shotlist"]
    by_id = {s["id"]: s for _sc, s in iter_shots(shotlist)}
    assert by_id["hook"]["status"] == "placed" and by_id["hook"]["clip_id"]
    assert by_id["reveal"]["status"] == "placed"
    assert by_id["outro"]["status"] == "draft"  # untouched


def test_rebuild_clears_and_reassembles(tmp_path):
    ctx = _ctx(tmp_path)
    _fill_and_seed(ctx, tmp_path)
    _call("assemble_shotlist", {}, ctx)
    first_video = [c for c in _clips(ctx) if c["track_id"] == "V1"]
    assert len(first_video) == 2

    # re-running without rebuild should place nothing new (both already placed)
    again = _call("assemble_shotlist", {}, ctx)
    assert again["assembled"] == 0
    assert len([c for c in _clips(ctx) if c["track_id"] == "V1"]) == 2

    # rebuild clears then reassembles to the same 2 video clips (no duplication)
    rebuilt = _call("assemble_shotlist", {"rebuild": True}, ctx)
    assert rebuilt["assembled"] == 2
    assert len([c for c in _clips(ctx) if c["track_id"] == "V1"]) == 2


def test_empty_shotlist_is_noop(tmp_path):
    ctx = _ctx(tmp_path)
    out = _call("assemble_shotlist", {}, ctx)
    assert out["assembled"] == 0
    assert "set_shotlist" in out["summary"]


def test_assemble_uses_selected_range_and_preserves_provenance(tmp_path):
    ctx = _ctx(tmp_path)
    asset_id = ctx.registry.add_external(
        _make_clip(tmp_path / "interview.mp4", duration=8.0), summary="interview"
    ).asset_id
    _call("set_shotlist", {"shotlist": {"scenes": [{"shots": [{
        "id": "answer",
        "description": "the concise answer",
        "duration_sec": 2.5,
        "asset_id": asset_id,
        "library_asset_id": "lib_interview",
        "source": "search",
        "status": "filled",
        "source_in": 3.25,
        "source_out": 5.75,
        "evidence": {
            "evidence_id": "lib_interview:ann_answer",
            "annotation_id": "ann_answer",
            "library_asset_id": "lib_interview",
            "label": "concise answer",
            "source": "user",
            "confidence": 0.81,
        },
    }]}]}}, ctx)

    out = _call("assemble_shotlist", {}, ctx)
    assert out["assembled"] == 1
    assert out["placed"][0]["source_in"] == 3.25
    assert out["placed"][0]["source_out"] == 5.75
    clip = next(c for c in _clips(ctx) if c.get("media_kind") == "video")
    assert (clip["source_in"], clip["source_out"], clip["duration"]) == (3.25, 5.75, 2.5)
    assert clip["provenance"]["shot_id"] == "answer"
    assert clip["provenance"]["evidence_id"] == "lib_interview:ann_answer"
    assert clip["provenance"]["annotation_id"] == "ann_answer"


def test_lottie_shot_uses_overlay_track(tmp_path):
    ctx = _ctx(tmp_path)
    lottie_path = tmp_path / "title.json"
    lottie_path.write_text(
        json.dumps({
            "v": "5.7.4", "fr": 30, "ip": 0, "op": 60,
            "w": 64, "h": 64, "layers": [],
        }),
        encoding="utf-8",
    )
    asset_id = ctx.registry.add_external(lottie_path, summary="title motion").asset_id
    _call("set_shotlist", {"shotlist": {"scenes": [{"shots": [{
        "id": "motion", "duration_sec": 2, "asset_id": asset_id,
        "source": "search", "status": "filled",
    }]}]}}, ctx)

    out = _call("assemble_shotlist", {}, ctx)
    assert out["assembled"] == 1
    clip = next(c for c in _clips(ctx) if c.get("media_kind") == "lottie")
    assert clip["track_id"] == "OV1"
    tracks = {track["id"]: track for track in ctx.project.load()["timeline"]["tracks"]}
    assert tracks["OV1"]["kind"] == "overlay"
