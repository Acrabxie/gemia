"""refine_shot: edit ONE assembled shot in place without rebuilding the timeline.

Covers all five operations: retime, replace, recaption, remove, and error cases
(not-yet-placed and invalid inputs). Each test genuinely exercises the dispatchers
and asserts timeline/shotlist state changes.
"""
from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import Any

from gemia.project_model import iter_shots
from gemia.project_store import ProjectHandle
from gemia.tools import DISPATCHER
from gemia.tools import refine_shot as _refine_shot
from gemia.tools._context import AssetRegistry, ToolContext


def _make_clip(path: Path, duration: float = 2.0) -> Path:
    """Generate a tiny video file for testing."""
    subprocess.run(
        [
            "ffmpeg", "-y", "-f", "lavfi",
            f"-i", f"testsrc2=duration={duration}:size=96x54:rate=15",
            "-pix_fmt", "yuv420p", str(path),
        ],
        check=True, capture_output=True,
    )
    return path


def _make_image(path: Path) -> Path:
    """Generate a tiny 1x1 PNG image for testing."""
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=black:s=1x1:d=1",
         "-pix_fmt", "rgb24", str(path)],
        check=True, capture_output=True,
    )
    return path


def _ctx(tmp_path: Path) -> ToolContext:
    """Create a project-backed context."""
    handle = ProjectHandle.open(tmp_path / "project", "v3-refine", session_id="v3-refine")
    return ToolContext(
        session_id="v3-refine",
        output_dir=tmp_path,
        registry=AssetRegistry(),
        emit_progress=lambda _u: None,
        project=handle,
    )


def _call(verb: str, args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Call a dispatcher and await its result."""
    if verb == "refine_shot":
        return asyncio.run(_refine_shot.dispatch(args, ctx))
    return asyncio.run(DISPATCHER[verb](args, ctx))


def _clips(ctx: ToolContext) -> list[dict[str, Any]]:
    """Get all clips from the timeline."""
    return ctx.project.load()["timeline"]["clips"]


def _setup_two_shots(ctx: ToolContext, tmp_path: Path) -> tuple[str, str]:
    """Set up a shotlist with two filled shots and assemble them."""
    # Create longer videos (6 seconds) to allow retime testing
    a = ctx.registry.add_external(_make_clip(tmp_path / "hook.mp4", duration=6.0), summary="hook").asset_id
    b = ctx.registry.add_external(_make_clip(tmp_path / "reveal.mp4", duration=6.0), summary="reveal").asset_id

    _call("set_shotlist", {
        "shotlist": {
            "logline": "test",
            "scenes": [{
                "id": "sc1",
                "title": "Main",
                "shots": [
                    {
                        "id": "shot1",
                        "description": "opening",
                        "duration_sec": 2.0,
                        "on_screen_text": "Hello",
                    },
                    {
                        "id": "shot2",
                        "description": "reveal",
                        "duration_sec": 3.0,
                    },
                ],
            }],
        }
    }, ctx)

    _call("update_shot", {"shot_id": "shot1", "fields": {"asset_id": a, "source": "search", "status": "filled"}}, ctx)
    _call("update_shot", {"shot_id": "shot2", "fields": {"asset_id": b, "source": "search", "status": "filled"}}, ctx)

    # Assemble both shots onto the timeline.
    _call("assemble_shotlist", {}, ctx)

    return a, b


def test_refine_shot_is_real_not_stub():
    """Verify the tool is real, not a stub."""
    assert hasattr(_refine_shot, "dispatch")
    assert "stub" not in _refine_shot.dispatch.__qualname__.lower()


def test_retime_changes_shot_duration(tmp_path):
    """Test retime: change shot1's duration, assert timeline clip length changes."""
    ctx = _ctx(tmp_path)
    _setup_two_shots(ctx, tmp_path)

    clips = _clips(ctx)
    video_clips = [c for c in clips if c["track_id"] == "V1"]
    old_shot1_duration = video_clips[0]["duration"]
    assert round(old_shot1_duration, 1) == 2.0

    # Retime shot1 to 4 seconds.
    result = _call("refine_shot", {"shot_id": "shot1", "duration_sec": 4.0}, ctx)

    assert result["operation"] == "retime"
    assert result["clip_id"] is not None

    # Assert the clip duration changed.
    clips = _clips(ctx)
    video_clips = [c for c in clips if c["track_id"] == "V1"]
    new_shot1_duration = video_clips[0]["duration"]
    assert round(new_shot1_duration, 1) == 4.0, f"expected 4.0, got {new_shot1_duration}"

    # shot2 should still be present and unchanged.
    assert len(video_clips) == 2
    assert round(video_clips[1]["duration"], 1) == 3.0

    # shot IR should be updated.
    shotlist = ctx.project.load()["shotlist"]
    by_id = {s["id"]: s for _sc, s in iter_shots(shotlist)}
    assert by_id["shot1"]["duration_sec"] == 4.0


def test_retime_preserves_nonzero_source_in(tmp_path):
    ctx = _ctx(tmp_path)
    asset_id = ctx.registry.add_external(
        _make_clip(tmp_path / "long.mp4", duration=20.0), summary="long"
    ).asset_id
    _call("set_shotlist", {"shotlist": {"scenes": [{"shots": [{
        "id": "shot1",
        "description": "selected answer",
        "duration_sec": 5.0,
        "asset_id": asset_id,
        "library_asset_id": "lib_long",
        "source_in": 12.0,
        "source_out": 17.0,
        "source": "search",
        "status": "filled",
        "evidence": {
            "evidence_id": "lib_long:ann_1",
            "annotation_id": "ann_1",
            "library_asset_id": "lib_long",
        },
    }]}]}}, ctx)
    _call("assemble_shotlist", {}, ctx)

    _call("refine_shot", {"shot_id": "shot1", "duration_sec": 3.0}, ctx)

    clip = next(c for c in _clips(ctx) if c.get("media_kind") == "video")
    assert (clip["source_in"], clip["source_out"], clip["duration"]) == (12.0, 15.0, 3.0)
    shot = ctx.project.load()["shotlist"]["scenes"][0]["shots"][0]
    assert (shot["source_in"], shot["source_out"]) == (12.0, 15.0)


def test_retime_text_overlay(tmp_path):
    """Test that retime also updates the text overlay duration."""
    ctx = _ctx(tmp_path)
    _setup_two_shots(ctx, tmp_path)

    clips = _clips(ctx)
    text_clips = [c for c in clips if c["media_kind"] == "text"]
    assert len(text_clips) == 1, "shot1 should have a text overlay"
    old_text_duration = text_clips[0]["duration"]
    assert round(old_text_duration, 1) == 2.0

    # Retime shot1.
    _call("refine_shot", {"shot_id": "shot1", "duration_sec": 5.0}, ctx)

    # Text overlay should also have new duration.
    clips = _clips(ctx)
    text_clips = [c for c in clips if c["media_kind"] == "text"]
    assert len(text_clips) == 1
    new_text_duration = text_clips[0]["duration"]
    assert round(new_text_duration, 1) == 5.0


def test_replace_asset_preserves_position(tmp_path):
    """Test replace: swap shot1's footage, assert position/duration preserved."""
    ctx = _ctx(tmp_path)
    a, b = _setup_two_shots(ctx, tmp_path)

    # Create a third asset to swap in.
    c = ctx.registry.add_external(_make_clip(tmp_path / "alt.mp4", duration=6.0), summary="alt").asset_id

    clips = _clips(ctx)
    video_clips = [c for c in clips if c["track_id"] == "V1"]
    old_clip1 = video_clips[0]
    old_clip1_id = old_clip1.get("id")
    old_start = old_clip1["start"]
    old_duration = old_clip1["duration"]

    # Replace shot1's asset.
    result = _call("refine_shot", {"shot_id": "shot1", "asset_id": c}, ctx)

    assert result["operation"] == "replace"
    new_clip_id = result.get("clip_id")
    assert new_clip_id is not None
    assert new_clip_id != old_clip1_id  # should be a new clip

    # Find the new clip by its id in the timeline.
    clips = _clips(ctx)
    new_clip = next((clip for clip in clips if clip.get("id") == new_clip_id), None)
    assert new_clip is not None, f"new clip {new_clip_id} not found"

    # The new clip should have the same start and duration as the old one.
    assert new_clip["start"] == old_start, f"expected start={old_start}, got {new_clip['start']}"
    assert round(new_clip["duration"], 6) == round(old_duration, 6), \
        f"expected duration={old_duration}, got {new_clip['duration']}"

    # shot2 should still be present and unaffected.
    video_clips = [c for c in clips if c["track_id"] == "V1"]
    assert len(video_clips) == 2  # still 2 clips
    shot2_clip = next((c for c in video_clips if c.get("id") != new_clip_id), None)
    assert shot2_clip is not None
    assert round(shot2_clip["duration"], 1) == 3.0

    # shot IR should reference the new asset.
    shotlist = ctx.project.load()["shotlist"]
    by_id = {s["id"]: s for _sc, s in iter_shots(shotlist)}
    assert by_id["shot1"]["asset_id"] == c


def test_recaption_updates_text_overlay(tmp_path):
    """Test recaption: change shot1's text from 'Hello' to 'World'."""
    ctx = _ctx(tmp_path)
    _setup_two_shots(ctx, tmp_path)

    clips = _clips(ctx)
    text_clips = [c for c in clips if c["media_kind"] == "text"]
    assert len(text_clips) == 1
    old_content = text_clips[0]["text_config"]["content"]
    assert old_content == "Hello"

    # Change the text.
    result = _call("refine_shot", {"shot_id": "shot1", "on_screen_text": "World"}, ctx)

    assert result["operation"] == "recaption"

    # Text overlay should have new content.
    clips = _clips(ctx)
    text_clips = [c for c in clips if c["media_kind"] == "text"]
    assert len(text_clips) == 1
    new_content = text_clips[0]["text_config"]["content"]
    assert new_content == "World"

    # shot IR should be updated.
    shotlist = ctx.project.load()["shotlist"]
    by_id = {s["id"]: s for _sc, s in iter_shots(shotlist)}
    assert by_id["shot1"]["on_screen_text"] == "World"


def test_recaption_empty_removes_text(tmp_path):
    """Test recaption with empty string removes the text overlay."""
    ctx = _ctx(tmp_path)
    _setup_two_shots(ctx, tmp_path)

    clips = _clips(ctx)
    text_clips = [c for c in clips if c["media_kind"] == "text"]
    assert len(text_clips) == 1, "shot1 should have a text overlay initially"

    # Clear the text.
    result = _call("refine_shot", {"shot_id": "shot1", "on_screen_text": ""}, ctx)

    assert result["operation"] == "recaption"

    # Text overlay should be gone.
    clips = _clips(ctx)
    text_clips = [c for c in clips if c["media_kind"] == "text"]
    assert len(text_clips) == 0, "text overlay should be removed"

    # shot IR should have no text.
    shotlist = ctx.project.load()["shotlist"]
    by_id = {s["id"]: s for _sc, s in iter_shots(shotlist)}
    assert by_id["shot1"]["on_screen_text"] is None


def test_remove_deletes_clip_and_text(tmp_path):
    """Test remove: delete shot2 (which has no text), assert clip gone."""
    ctx = _ctx(tmp_path)
    _setup_two_shots(ctx, tmp_path)

    clips = _clips(ctx)
    video_clips = [c for c in clips if c["track_id"] == "V1"]
    assert len(video_clips) == 2

    # Remove shot2.
    result = _call("refine_shot", {"shot_id": "shot2", "remove": True}, ctx)

    assert result["operation"] == "remove"

    # shot2's clip should be gone.
    clips = _clips(ctx)
    video_clips = [c for c in clips if c["track_id"] == "V1"]
    assert len(video_clips) == 1
    assert video_clips[0] is not None  # shot1 is still there

    # shot IR status should revert to "filled" (has asset_id but not placed).
    shotlist = ctx.project.load()["shotlist"]
    by_id = {s["id"]: s for _sc, s in iter_shots(shotlist)}
    assert by_id["shot2"]["status"] == "filled"
    assert by_id["shot2"]["clip_id"] is None


def test_remove_with_text_overlay(tmp_path):
    """Test remove on shot1 (which has text), assert both clip and text gone."""
    ctx = _ctx(tmp_path)
    _setup_two_shots(ctx, tmp_path)

    clips = _clips(ctx)
    text_clips = [c for c in clips if c["media_kind"] == "text"]
    assert len(text_clips) == 1, "shot1 should have a text overlay"

    # Remove shot1.
    result = _call("refine_shot", {"shot_id": "shot1", "remove": True}, ctx)

    assert result["operation"] == "remove"

    # Both shot1's video clip and text overlay should be gone.
    clips = _clips(ctx)
    video_clips = [c for c in clips if c["track_id"] == "V1"]
    text_clips = [c for c in clips if c["media_kind"] == "text"]
    assert len(video_clips) == 1  # only shot2 remains
    assert len(text_clips) == 0  # text overlay is gone
    assert video_clips[0]["duration"] == 3.0  # shot2 is still there


def test_not_yet_placed_returns_guidance(tmp_path):
    """Test that ops on unplaced shots return guidance, not raise."""
    ctx = _ctx(tmp_path)
    a = ctx.registry.add_external(_make_clip(tmp_path / "test.mp4"), summary="test").asset_id

    # Set up a shot but do NOT assemble it.
    _call("set_shotlist", {
        "shotlist": {
            "logline": "test",
            "scenes": [{
                "id": "sc1",
                "title": "Main",
                "shots": [{
                    "id": "unplaced_shot",
                    "description": "not assembled",
                    "duration_sec": 2.0,
                }],
            }],
        }
    }, ctx)
    _call("update_shot", {
        "shot_id": "unplaced_shot",
        "fields": {"asset_id": a, "source": "search", "status": "filled"},
    }, ctx)

    # Try to retime without assembling.
    result = _call("refine_shot", {"shot_id": "unplaced_shot", "duration_sec": 5.0}, ctx)

    # Should return guidance, not raise.
    assert result["clip_id"] is None
    assert "assemble_shotlist" in result["summary"].lower()
    assert result["operation"] == "retime"


def test_unknown_shot_id_raises(tmp_path):
    """Test that unknown shot_id raises ValueError."""
    ctx = _ctx(tmp_path)
    _setup_two_shots(ctx, tmp_path)

    try:
        _call("refine_shot", {"shot_id": "nonexistent", "duration_sec": 5.0}, ctx)
        assert False, "should have raised ValueError"
    except ValueError as e:
        assert "not found" in str(e).lower()


def test_replace_with_unregistered_asset_raises(tmp_path):
    """Test that replace with an unregistered asset raises ValueError."""
    ctx = _ctx(tmp_path)
    _setup_two_shots(ctx, tmp_path)

    try:
        _call("refine_shot", {"shot_id": "shot1", "asset_id": "v_999"}, ctx)
        assert False, "should have raised ValueError"
    except ValueError as e:
        assert "not in session registry" in str(e).lower()


def test_missing_shot_id_raises(tmp_path):
    """Test that missing shot_id raises ValueError."""
    ctx = _ctx(tmp_path)
    _setup_two_shots(ctx, tmp_path)

    try:
        _call("refine_shot", {"duration_sec": 5.0}, ctx)
        assert False, "should have raised ValueError"
    except ValueError as e:
        assert "shot_id" in str(e).lower()


def test_multiple_operations_raises(tmp_path):
    """Test that specifying multiple ops raises ValueError."""
    ctx = _ctx(tmp_path)
    _setup_two_shots(ctx, tmp_path)

    try:
        _call("refine_shot", {"shot_id": "shot1", "duration_sec": 5.0, "asset_id": "v_001"}, ctx)
        assert False, "should have raised ValueError"
    except ValueError as e:
        assert "exactly one" in str(e).lower()


def test_no_operation_raises(tmp_path):
    """Test that no operation specified raises ValueError."""
    ctx = _ctx(tmp_path)
    _setup_two_shots(ctx, tmp_path)

    try:
        _call("refine_shot", {"shot_id": "shot1"}, ctx)
        assert False, "should have raised ValueError"
    except ValueError as e:
        assert "exactly one" in str(e).lower()


def test_retime_with_negative_duration_raises(tmp_path):
    """Test that negative duration_sec raises ValueError."""
    ctx = _ctx(tmp_path)
    _setup_two_shots(ctx, tmp_path)

    try:
        _call("refine_shot", {"shot_id": "shot1", "duration_sec": -1.0}, ctx)
        assert False, "should have raised ValueError"
    except ValueError as e:
        assert "positive" in str(e).lower()


def test_replace_with_empty_asset_id_raises(tmp_path):
    """Test that empty asset_id raises ValueError."""
    ctx = _ctx(tmp_path)
    _setup_two_shots(ctx, tmp_path)

    try:
        _call("refine_shot", {"shot_id": "shot1", "asset_id": ""}, ctx)
        assert False, "should have raised ValueError"
    except ValueError as e:
        assert "empty" in str(e).lower()
