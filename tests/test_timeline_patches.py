"""Timeline v1 (M1) op vocabulary tests.

Covers docs/timeline-v1/01-op-vocabulary.md §6:
1. happy path + typical error (asserting error codes) per op;
2. ripple matrix for insert@time / delete / trim / set_clip_time;
3. split identity regression (asset_id kept, independent trims);
4. legacy insert_clip/replace_clip compatibility;
5. patch atomicity (caller state never polluted);
6. ProjectStore apply + undo_to_seq round trip.
"""
from __future__ import annotations

import copy

import pytest

from gemia.project_model import IMAGE_DURATION, empty_project, normalize_project
from lumerai.patches import (
    EPSILON,
    TimelinePatchError,
    apply_timeline_patches,
    validate_project,
)


# ── builders ─────────────────────────────────────────────────────────


def _patch(*ops: dict) -> dict:
    return {"version": 1, "ops": list(ops)}


def _video_asset(asset_id: str = "asset_demo", duration: float = 10.0, name: str = "demo.mp4") -> dict:
    return {
        "id": asset_id,
        "asset_id": asset_id,
        "name": name,
        "media_kind": "video",
        "mime_type": "video/mp4",
        "source_path": f"/tmp/{name}",
        "duration": duration,
        "metadata": {"duration": duration},
    }


def _image_asset(asset_id: str = "asset_img", name: str = "still.png") -> dict:
    return {
        "id": asset_id,
        "asset_id": asset_id,
        "name": name,
        "media_kind": "image",
        "mime_type": "image/png",
        "source_path": f"/tmp/{name}",
        "duration": IMAGE_DURATION,
    }


def _video_clip(
    clip_id: str,
    start: float,
    duration: float,
    *,
    track_id: str = "V1",
    asset_id: str = "asset_demo",
    source_in: float = 0.0,
) -> dict:
    return {
        "id": clip_id,
        "asset_id": asset_id,
        "track_id": track_id,
        "name": "demo.mp4",
        "media_kind": "video",
        "start": start,
        "duration": duration,
        "source_in": source_in,
        "source_out": source_in + duration,
        "enabled": True,
    }


def _image_clip(clip_id: str, start: float, duration: float = IMAGE_DURATION, *, track_id: str = "OV1") -> dict:
    return {
        "id": clip_id,
        "asset_id": "asset_img",
        "track_id": track_id,
        "name": "still.png",
        "media_kind": "image",
        "start": start,
        "duration": duration,
        "source_in": 0.0,
        "source_out": duration,
        "enabled": True,
    }


def _project(clips: list[dict] | None = None, *, with_overlay: bool = True) -> dict:
    project = empty_project(title="Timeline Patch Test")
    project["assets"] = [_video_asset(), _image_asset()]
    if with_overlay:
        tracks = project["timeline"]["tracks"]
        tracks.insert(1, {"id": "OV1", "kind": "overlay", "name": "Overlay 1", "index": 1})
    project["timeline"]["clips"] = [dict(clip) for clip in clips or []]
    return normalize_project(project)


def _two_video_clips() -> dict:
    return _project([_video_clip("clip_a", 0.0, 2.0), _video_clip("clip_b", 2.0, 1.5)])


def _apply(project: dict, *ops: dict) -> dict:
    return apply_timeline_patches(project, [_patch(*ops)])


def _expect_error(code: str, project: dict, *ops: dict) -> TimelinePatchError:
    with pytest.raises(TimelinePatchError) as exc:
        _apply(project, *ops)
    assert exc.value.code == code
    assert code in str(exc.value)
    return exc.value


def _clips(project: dict, track_id: str | None = None) -> list[dict]:
    clips = [clip for clip in project["timeline"]["clips"]]
    if track_id is not None:
        clips = [clip for clip in clips if clip["track_id"] == track_id]
    return sorted(clips, key=lambda clip: clip["start"])


def _clip(project: dict, clip_id: str) -> dict:
    for clip in project["timeline"]["clips"]:
        if clip["id"] == clip_id:
            return clip
    raise AssertionError(f"clip not found: {clip_id}")


# ── §6.4 legacy compatibility ───────────────────────────────────────


def test_legacy_insert_clip_appends_raw_clip_without_validation() -> None:
    project = _project([_video_clip("clip_a", 0.0, 2.0)])
    op = {
        "op": "insert_clip",
        "target": "timeline",
        "provenance": {"session_id": "sess_old", "script_hash": "h"},
        "data": {
            "asset": _video_asset("asset_new", 1.0, "new.mp4"),
            # Deliberately overlaps clip_a on V1: legacy form must not validate.
            "clip": _video_clip("clip_new", 0.5, 1.0, asset_id="asset_new"),
        },
    }
    updated = _apply(project, op)
    assert len(updated["timeline"]["clips"]) == 2
    inserted = _clip(updated, "clip_new")
    assert inserted["provenance"]["session_id"] == "sess_old"
    assert any(asset["id"] == "asset_new" for asset in updated["assets"])


def test_legacy_replace_clip_merges_and_keeps_id() -> None:
    project = _project([_video_clip("clip_a", 0.0, 2.0)])
    op = {
        "op": "replace_clip",
        "target": "timeline",
        "clip_id": "clip_a",
        "provenance": {"session_id": "sess_rep", "script_hash": "h"},
        "data": {
            "asset": _video_asset("asset_new", 1.0, "new.mp4"),
            "clip": _video_clip("clip_generated", 0.0, 1.0, asset_id="asset_new"),
        },
    }
    updated = _apply(project, op)
    clip = updated["timeline"]["clips"][0]
    assert clip["id"] == "clip_a"
    assert clip["asset_id"] == "asset_new"
    assert clip["provenance"]["session_id"] == "sess_rep"


def test_legacy_replace_clip_missing_target_keeps_value_error() -> None:
    project = _project([])
    op = {"op": "replace_clip", "clip_id": "nope", "data": {"clip": _video_clip("x", 0.0, 1.0)}}
    with pytest.raises(ValueError, match="replace_clip target not found"):
        _apply(project, op)


def test_mixed_batch_with_new_op_arms_validation() -> None:
    project = _project([_video_clip("clip_a", 0.0, 2.0)])
    legacy_overlap = {
        "op": "insert_clip",
        "data": {"clip": _video_clip("clip_new", 0.5, 1.0)},
    }
    _expect_error("E_OVERLAP", project, legacy_overlap, {"op": "add_marker", "time": 0.0})


# ── §3.1 insert_clip (extended form) ────────────────────────────────


def test_insert_clip_extended_appends_to_track_end() -> None:
    project = _two_video_clips()
    op = {
        "op": "insert_clip",
        "track_id": "V1",
        "provenance": {"session_id": "sess_new"},
        "data": {"clip": _video_clip("clip_c", 0.0, 1.0)},
    }
    updated = _apply(project, op)
    clip = _clip(updated, "clip_c")
    assert clip["start"] == pytest.approx(3.5)
    assert clip["provenance"]["session_id"] == "sess_new"
    assert clip["keyframes"] == []


def test_insert_clip_extended_unknown_track_is_not_found() -> None:
    project = _project([])
    op = {"op": "insert_clip", "track_id": "V9", "data": {"clip": _video_clip("clip_c", 0.0, 1.0)}}
    _expect_error("E_NOT_FOUND", project, op)


def test_insert_clip_extended_video_on_audio_track_rejected() -> None:
    project = _project([])
    op = {"op": "insert_clip", "track_id": "A1", "data": {"clip": _video_clip("clip_c", 0.0, 1.0)}}
    _expect_error("E_TRACK_KIND", project, op)


def test_insert_clip_extended_image_track_kinds() -> None:
    # Images may sit on video or overlay tracks (image-based shotlist main
    # chain); non-visual tracks stay strict.
    project = _project([])
    op = {"op": "insert_clip", "track_id": "V1", "data": {"clip": _image_clip("img_x", 0.0)}}
    result = _apply(project, op)
    assert [c["id"] for c in _clips(result, "V1")] == ["img_x"]
    op_audio = {"op": "insert_clip", "track_id": "A1", "data": {"clip": _image_clip("img_y", 0.0)}}
    _expect_error("E_TRACK_KIND", project, op_audio)


def test_insert_clip_at_time_without_ripple_overlap_fails() -> None:
    project = _two_video_clips()
    op = {
        "op": "insert_clip",
        "track_id": "V1",
        "at": {"time": 1.0},
        "data": {"clip": _video_clip("clip_c", 0.0, 1.0)},
    }
    _expect_error("E_OVERLAP", project, op)


def test_insert_clip_at_time_with_ripple_shifts_right() -> None:
    project = _two_video_clips()
    op = {
        "op": "insert_clip",
        "track_id": "V1",
        "at": {"time": 2.0},
        "ripple": True,
        "data": {"clip": _video_clip("clip_c", 0.0, 1.0)},
    }
    updated = _apply(project, op)
    assert _clip(updated, "clip_c")["start"] == pytest.approx(2.0)
    assert _clip(updated, "clip_a")["start"] == pytest.approx(0.0)
    assert _clip(updated, "clip_b")["start"] == pytest.approx(3.0)


def test_insert_clip_at_index_ripples_by_nature() -> None:
    project = _two_video_clips()
    op = {
        "op": "insert_clip",
        "track_id": "V1",
        "at": {"index": 1},
        "data": {"clip": _video_clip("clip_c", 0.0, 1.0)},
    }
    updated = _apply(project, op)
    assert _clip(updated, "clip_c")["start"] == pytest.approx(2.0)
    assert _clip(updated, "clip_b")["start"] == pytest.approx(3.0)
    order = [clip["id"] for clip in _clips(updated, "V1")]
    assert order == ["clip_a", "clip_c", "clip_b"]


def test_insert_clip_at_index_out_of_range_is_bad_arg() -> None:
    project = _two_video_clips()
    op = {
        "op": "insert_clip",
        "track_id": "V1",
        "at": {"index": 5},
        "data": {"clip": _video_clip("clip_c", 0.0, 1.0)},
    }
    _expect_error("E_BAD_ARG", project, op)


def test_insert_text_clip_defaults_on_overlay_track() -> None:
    project = _project([])
    op = {
        "op": "insert_clip",
        "track_id": "OV1",
        "at": {"time": 0},
        "data": {"clip": {"media_kind": "text", "text_config": {"content": "Hello Lumeri"}}},
    }
    updated = _apply(project, op)
    clip = _clips(updated, "OV1")[0]
    assert clip["media_kind"] == "text"
    assert clip["asset_id"] == ""
    assert clip["duration"] == pytest.approx(IMAGE_DURATION)
    assert clip["source_in"] == 0.0
    assert clip["source_out"] == pytest.approx(IMAGE_DURATION)
    config = clip["text_config"]
    assert config["content"] == "Hello Lumeri"
    assert config["font_size"] == 64.0
    assert config["color"] == "#ffffff"
    assert config["position"] is None
    assert config["align"] == "center"


def test_insert_text_clip_empty_content_is_bad_arg() -> None:
    project = _project([])
    op = {
        "op": "insert_clip",
        "track_id": "OV1",
        "data": {"clip": {"media_kind": "text", "text_config": {"content": ""}}},
    }
    _expect_error("E_BAD_ARG", project, op)


# ── §3.2 delete_clip ────────────────────────────────────────────────


def test_delete_clip_without_ripple_keeps_gap() -> None:
    project = _two_video_clips()
    updated = _apply(project, {"op": "delete_clip", "clip_id": "clip_a"})
    clips = _clips(updated, "V1")
    assert [clip["id"] for clip in clips] == ["clip_b"]
    assert clips[0]["start"] == pytest.approx(2.0)


def test_delete_clip_with_ripple_closes_gap() -> None:
    project = _two_video_clips()
    updated = _apply(project, {"op": "delete_clip", "clip_id": "clip_a", "ripple": True})
    clips = _clips(updated, "V1")
    assert clips[0]["start"] == pytest.approx(0.0)


def test_delete_clip_missing_is_not_found() -> None:
    _expect_error("E_NOT_FOUND", _project([]), {"op": "delete_clip", "clip_id": "ghost"})


# ── §3.3 move_clip ──────────────────────────────────────────────────


def test_move_clip_to_free_position() -> None:
    project = _two_video_clips()
    updated = _apply(project, {"op": "move_clip", "clip_id": "clip_b", "start": 5.0})
    assert _clip(updated, "clip_b")["start"] == pytest.approx(5.0)


def test_move_clip_requires_start_or_track() -> None:
    project = _two_video_clips()
    _expect_error("E_BAD_ARG", project, {"op": "move_clip", "clip_id": "clip_b"})


def test_move_clip_destination_overlap_fails() -> None:
    project = _two_video_clips()
    _expect_error("E_OVERLAP", project, {"op": "move_clip", "clip_id": "clip_b", "start": 1.0})


def test_move_clip_video_to_overlay_track_kind_mismatch() -> None:
    project = _two_video_clips()
    _expect_error(
        "E_TRACK_KIND", project, {"op": "move_clip", "clip_id": "clip_b", "track_id": "OV1"}
    )


def test_move_clip_ripple_closes_original_gap() -> None:
    project = _two_video_clips()
    updated = _apply(
        project, {"op": "move_clip", "clip_id": "clip_a", "start": 6.0, "ripple": True}
    )
    assert _clip(updated, "clip_a")["start"] == pytest.approx(6.0)
    assert _clip(updated, "clip_b")["start"] == pytest.approx(0.0)


# ── §3.4 trim_clip ──────────────────────────────────────────────────


def test_trim_clip_updates_source_range_and_duration() -> None:
    project = _two_video_clips()
    updated = _apply(
        project, {"op": "trim_clip", "clip_id": "clip_a", "source_in": 0.5, "source_out": 1.5}
    )
    clip = _clip(updated, "clip_a")
    assert clip["source_in"] == pytest.approx(0.5)
    assert clip["source_out"] == pytest.approx(1.5)
    assert clip["duration"] == pytest.approx(1.0)


def test_trim_clip_beyond_asset_duration_is_range_error() -> None:
    project = _two_video_clips()
    _expect_error(
        "E_RANGE", project, {"op": "trim_clip", "clip_id": "clip_a", "source_out": 99.0}
    )


def test_trim_clip_inverted_range_is_range_error() -> None:
    project = _two_video_clips()
    _expect_error(
        "E_RANGE",
        project,
        {"op": "trim_clip", "clip_id": "clip_a", "source_in": 1.5, "source_out": 1.0},
    )


def test_trim_clip_on_text_is_bad_arg() -> None:
    project = _project([])
    insert = {
        "op": "insert_clip",
        "track_id": "OV1",
        "data": {"clip": {"id": "txt_1", "media_kind": "text", "text_config": {"content": "t"}}},
    }
    seeded = _apply(project, insert)
    _expect_error(
        "E_BAD_ARG", seeded, {"op": "trim_clip", "clip_id": "txt_1", "source_out": 1.0}
    )


def test_trim_clip_ripple_shifts_following_by_delta() -> None:
    project = _two_video_clips()
    updated = _apply(
        project,
        {"op": "trim_clip", "clip_id": "clip_a", "source_in": 0.0, "source_out": 1.0, "ripple": True},
    )
    assert _clip(updated, "clip_a")["duration"] == pytest.approx(1.0)
    assert _clip(updated, "clip_b")["start"] == pytest.approx(1.0)


def test_trim_clip_lengthen_without_ripple_overlaps() -> None:
    project = _two_video_clips()
    _expect_error(
        "E_OVERLAP",
        project,
        {"op": "trim_clip", "clip_id": "clip_a", "source_in": 0.0, "source_out": 2.5},
    )


def test_trim_clip_lengthen_with_ripple_shifts_right() -> None:
    project = _two_video_clips()
    updated = _apply(
        project,
        {"op": "trim_clip", "clip_id": "clip_a", "source_in": 0.0, "source_out": 2.5, "ripple": True},
    )
    assert _clip(updated, "clip_a")["duration"] == pytest.approx(2.5)
    assert _clip(updated, "clip_b")["start"] == pytest.approx(2.5)


# ── §3.5 split_clip ─────────────────────────────────────────────────


def test_split_clip_creates_consistent_halves() -> None:
    project = _project(
        [
            {
                **_video_clip("clip_a", 0.0, 2.0, source_in=1.0),
                "transition_after": {"kind": "dissolve", "duration_sec": 0.5},
            }
        ]
    )
    updated = _apply(
        project, {"op": "split_clip", "clip_id": "clip_a", "at_time": 0.8, "new_clip_id": "clip_a2"}
    )
    front = _clip(updated, "clip_a")
    back = _clip(updated, "clip_a2")
    assert front["duration"] == pytest.approx(0.8)
    assert front["source_in"] == pytest.approx(1.0)
    assert front["source_out"] == pytest.approx(1.8)
    assert front["transition_after"] is None
    assert back["start"] == pytest.approx(0.8)
    assert back["duration"] == pytest.approx(1.2)
    assert back["source_in"] == pytest.approx(1.8)
    assert back["source_out"] == pytest.approx(3.0)
    assert back["transition_after"] == {"kind": "dissolve", "duration_sec": 0.5}
    assert back["asset_id"] == front["asset_id"]


def test_split_clip_at_boundary_is_bad_arg() -> None:
    project = _project([_video_clip("clip_a", 0.0, 2.0)])
    _expect_error("E_BAD_ARG", project, {"op": "split_clip", "clip_id": "clip_a", "at_time": 0.0})
    _expect_error("E_BAD_ARG", project, {"op": "split_clip", "clip_id": "clip_a", "at_time": 2.0})


def test_split_identity_same_asset_halves_trim_independently() -> None:
    """2026-05-10 identity rule: identity = clip_id + source range, never asset."""
    project = _project([_video_clip("clip_a", 0.0, 4.0)])
    updated = _apply(
        project,
        {"op": "split_clip", "clip_id": "clip_a", "at_time": 2.0, "new_clip_id": "clip_b"},
        {"op": "trim_clip", "clip_id": "clip_b", "source_in": 3.0, "source_out": 3.5},
    )
    front = _clip(updated, "clip_a")
    back = _clip(updated, "clip_b")
    assert front["asset_id"] == back["asset_id"] == "asset_demo"
    assert front["id"] != back["id"]
    # Trimming the back half must not touch the front half's source range.
    assert front["source_in"] == pytest.approx(0.0)
    assert front["source_out"] == pytest.approx(2.0)
    assert back["source_in"] == pytest.approx(3.0)
    assert back["source_out"] == pytest.approx(3.5)
    # And the other way round.
    again = apply_timeline_patches(
        updated, [_patch({"op": "trim_clip", "clip_id": "clip_a", "source_in": 0.5, "source_out": 1.0})]
    )
    assert _clip(again, "clip_b")["source_in"] == pytest.approx(3.0)
    assert _clip(again, "clip_b")["source_out"] == pytest.approx(3.5)


# ── §3.6 set_clip_time ──────────────────────────────────────────────


def test_set_clip_time_moves_start() -> None:
    project = _two_video_clips()
    updated = _apply(project, {"op": "set_clip_time", "clip_id": "clip_b", "start": 7.0})
    assert _clip(updated, "clip_b")["start"] == pytest.approx(7.0)


def test_set_clip_time_requires_start_or_duration() -> None:
    project = _two_video_clips()
    _expect_error("E_BAD_ARG", project, {"op": "set_clip_time", "clip_id": "clip_b"})


def test_set_clip_time_duration_on_video_is_bad_arg() -> None:
    project = _two_video_clips()
    _expect_error(
        "E_BAD_ARG", project, {"op": "set_clip_time", "clip_id": "clip_a", "duration": 1.0}
    )


def test_set_clip_time_image_duration_syncs_source_range() -> None:
    project = _project([_image_clip("img_a", 0.0)])
    updated = _apply(project, {"op": "set_clip_time", "clip_id": "img_a", "duration": 5.0})
    clip = _clip(updated, "img_a")
    assert clip["duration"] == pytest.approx(5.0)
    assert clip["source_in"] == 0.0
    assert clip["source_out"] == pytest.approx(5.0)


def test_set_clip_time_image_lengthen_without_ripple_overlaps() -> None:
    project = _project([_image_clip("img_a", 0.0), _image_clip("img_b", 3.0)])
    _expect_error(
        "E_OVERLAP", project, {"op": "set_clip_time", "clip_id": "img_a", "duration": 4.0}
    )


def test_set_clip_time_image_lengthen_with_ripple_shifts() -> None:
    project = _project([_image_clip("img_a", 0.0), _image_clip("img_b", 3.0)])
    updated = _apply(
        project, {"op": "set_clip_time", "clip_id": "img_a", "duration": 4.0, "ripple": True}
    )
    assert _clip(updated, "img_a")["duration"] == pytest.approx(4.0)
    assert _clip(updated, "img_b")["start"] == pytest.approx(4.0)


# ── §3.8 add_transition ─────────────────────────────────────────────


def test_add_transition_dissolve_between_adjacent_clips() -> None:
    project = _two_video_clips()
    updated = _apply(
        project, {"op": "add_transition", "clip_id": "clip_a", "kind": "dissolve"}
    )
    assert _clip(updated, "clip_a")["transition_after"] == {"kind": "dissolve", "duration_sec": 0.5}


def test_add_transition_cut_clears_transition() -> None:
    project = _project(
        [
            {
                **_video_clip("clip_a", 0.0, 2.0),
                "transition_after": {"kind": "wipe", "duration_sec": 0.5},
            },
            _video_clip("clip_b", 2.0, 1.5),
        ]
    )
    updated = _apply(project, {"op": "add_transition", "clip_id": "clip_a", "kind": "cut"})
    assert _clip(updated, "clip_a")["transition_after"] is None


def test_add_transition_non_adjacent_is_bad_arg() -> None:
    project = _project([_video_clip("clip_a", 0.0, 2.0), _video_clip("clip_b", 3.0, 1.5)])
    _expect_error(
        "E_BAD_ARG", project, {"op": "add_transition", "clip_id": "clip_a", "kind": "fade"}
    )


def test_add_transition_duration_longer_than_neighbor_is_bad_arg() -> None:
    project = _two_video_clips()
    _expect_error(
        "E_BAD_ARG",
        project,
        {"op": "add_transition", "clip_id": "clip_a", "kind": "dissolve", "duration_sec": 1.8},
    )


# ── §3.9 set_clip_effects ───────────────────────────────────────────


def test_set_clip_effects_merges_and_null_deletes() -> None:
    project = _two_video_clips()
    updated = _apply(
        project,
        {
            "op": "set_clip_effects",
            "clip_id": "clip_a",
            "effects": {"opacity": 0.5, "rotation": 90, "speed": None},
        },
    )
    effects = _clip(updated, "clip_a")["effects"]
    assert effects["opacity"] == 0.5
    assert effects["rotation"] == 90
    assert "speed" not in effects
    assert effects["mirrored"] is False  # untouched defaults survive the merge
    assert effects["audioDetached"] is False  # non-whitelist legacy keys survive too


def test_set_clip_effects_rejects_non_whitelisted_key() -> None:
    project = _two_video_clips()
    _expect_error(
        "E_BAD_ARG",
        project,
        {"op": "set_clip_effects", "clip_id": "clip_a", "effects": {"glitter": 1}},
    )


def test_set_clip_effects_rejects_bad_value() -> None:
    project = _two_video_clips()
    _expect_error(
        "E_BAD_ARG",
        project,
        {"op": "set_clip_effects", "clip_id": "clip_a", "effects": {"opacity": 2.0}},
    )


# ── §3.10 add_track / remove_track ──────────────────────────────────


def test_add_track_overlay_auto_id_before_audio() -> None:
    project = normalize_project(empty_project(title="tracks"))
    updated = _apply(project, {"op": "add_track", "kind": "overlay"})
    ids = [track["id"] for track in updated["timeline"]["tracks"]]
    assert ids == ["V1", "OV1", "A1"]
    overlay = updated["timeline"]["tracks"][1]
    assert overlay["kind"] == "overlay"
    assert overlay["name"] == "Overlay 1"
    assert [track["index"] for track in updated["timeline"]["tracks"]] == [0, 1, 2]


def test_add_track_video_lands_after_existing_video() -> None:
    project = normalize_project(empty_project(title="tracks"))
    updated = _apply(project, {"op": "add_track", "kind": "video"})
    ids = [track["id"] for track in updated["timeline"]["tracks"]]
    assert ids == ["V1", "V2", "A1"]


def test_add_track_audio_appends_after_existing_audio() -> None:
    # M6: audio tracks are now executable. The default project is [V1, A1];
    # adding another audio track lands at the end as A2.
    project = normalize_project(empty_project(title="tracks"))
    updated = _apply(project, {"op": "add_track", "kind": "audio"})
    tracks = updated["timeline"]["tracks"]
    assert [track["id"] for track in tracks] == ["V1", "A1", "A2"]
    assert tracks[2]["kind"] == "audio"
    assert tracks[2]["name"] == "Audio 2"
    assert [track["index"] for track in tracks] == [0, 1, 2]


def test_add_track_duplicate_id_is_bad_arg() -> None:
    _expect_error(
        "E_BAD_ARG", _project([]), {"op": "add_track", "kind": "video", "track_id": "V1"}
    )


def test_remove_track_only_when_empty() -> None:
    project = _project([])  # OV1 exists and is empty
    updated = _apply(project, {"op": "remove_track", "track_id": "OV1"})
    assert [track["id"] for track in updated["timeline"]["tracks"]] == ["V1", "A1"]


def test_remove_track_v1_is_protected() -> None:
    _expect_error("E_BAD_ARG", _project([]), {"op": "remove_track", "track_id": "V1"})


def test_remove_track_with_clips_is_bad_arg() -> None:
    project = _project([_image_clip("img_a", 0.0)])
    _expect_error("E_BAD_ARG", project, {"op": "remove_track", "track_id": "OV1"})


def test_remove_track_missing_is_not_found() -> None:
    _expect_error("E_NOT_FOUND", _project([]), {"op": "remove_track", "track_id": "OV9"})


# ── M7 §set_track (track-level ducking relationship) ─────────────────


def _two_audio_tracks() -> dict:
    """Default [V1, A1] plus an appended A2 (M6 add_track audio)."""
    project = normalize_project(empty_project(title="ducking"))
    return _apply(project, {"op": "add_track", "kind": "audio"})  # -> A2


def _track(project: dict, track_id: str) -> dict:
    return next(t for t in project["timeline"]["tracks"] if t["id"] == track_id)


def test_set_track_duck_under_sets_field() -> None:
    updated = _apply(_two_audio_tracks(), {"op": "set_track", "track_id": "A1", "duck_under": "A2"})
    assert _track(updated, "A1")["duck_under"] == "A2"


def test_set_track_clear_duck_under() -> None:
    project = _apply(_two_audio_tracks(), {"op": "set_track", "track_id": "A1", "duck_under": "A2"})
    updated = _apply(project, {"op": "set_track", "track_id": "A1", "duck_under": None})
    assert _track(updated, "A1")["duck_under"] is None


def test_set_track_missing_track_id_is_bad_arg() -> None:
    _expect_error("E_BAD_ARG", _two_audio_tracks(), {"op": "set_track"})


def test_set_track_non_audio_track_is_track_kind() -> None:
    _expect_error("E_TRACK_KIND", _two_audio_tracks(), {"op": "set_track", "track_id": "V1", "duck_under": "A1"})


def test_set_track_self_reference_is_bad_arg() -> None:
    _expect_error("E_BAD_ARG", _two_audio_tracks(), {"op": "set_track", "track_id": "A1", "duck_under": "A1"})


def test_set_track_missing_target_is_not_found() -> None:
    _expect_error("E_NOT_FOUND", _two_audio_tracks(), {"op": "set_track", "track_id": "A1", "duck_under": "A9"})


def test_set_track_non_audio_target_is_track_kind() -> None:
    _expect_error("E_TRACK_KIND", _two_audio_tracks(), {"op": "set_track", "track_id": "A1", "duck_under": "V1"})


def test_set_track_cycle_is_bad_arg() -> None:
    project = _apply(_two_audio_tracks(), {"op": "set_track", "track_id": "A1", "duck_under": "A2"})
    # A2 -> A1 would close the cycle A1 -> A2 -> A1.
    _expect_error("E_BAD_ARG", project, {"op": "set_track", "track_id": "A2", "duck_under": "A1"})


# ── §3.11 set_timeline_format ───────────────────────────────────────


def test_set_timeline_format_updates_timeline_only() -> None:
    project = _project([])
    updated = _apply(
        project, {"op": "set_timeline_format", "fps": 60, "width": 1080, "height": 1920}
    )
    timeline = updated["timeline"]
    assert timeline["fps"] == 60.0
    assert timeline["width"] == 1080
    assert timeline["height"] == 1920
    # Export settings belong to M4 and must stay untouched.
    assert updated["render_settings"]["width"] == 1920
    assert updated["render_settings"]["height"] == 1080
    assert updated["render_settings"]["fps"] == 30.0


def test_set_timeline_format_rejects_out_of_range() -> None:
    _expect_error("E_BAD_ARG", _project([]), {"op": "set_timeline_format", "fps": 0})
    _expect_error("E_BAD_ARG", _project([]), {"op": "set_timeline_format", "width": 8})


# ── §3.12 add_marker ────────────────────────────────────────────────


def test_add_marker_appends_marker() -> None:
    project = _project([])
    updated = _apply(project, {"op": "add_marker", "time": 1.25, "label": "beat"})
    marker = updated["timeline"]["markers"][-1]
    assert marker["time"] == pytest.approx(1.25)
    assert marker["label"] == "beat"
    assert marker["id"].startswith("marker_")


def test_add_marker_negative_time_is_bad_arg() -> None:
    _expect_error("E_BAD_ARG", _project([]), {"op": "add_marker", "time": -1.0})


# ── §3.13 upsert_asset ──────────────────────────────────────────────


def test_upsert_asset_merges_existing_entry() -> None:
    project = _project([])
    updated = _apply(
        project, {"op": "upsert_asset", "asset": {"id": "asset_demo", "duration": 12.0}}
    )
    asset = next(item for item in updated["assets"] if item["id"] == "asset_demo")
    assert asset["duration"] == 12.0
    assert asset["name"] == "demo.mp4"  # merge keeps existing fields


def test_upsert_asset_missing_id_is_bad_arg() -> None:
    _expect_error("E_BAD_ARG", _project([]), {"op": "upsert_asset", "asset": {"name": "x"}})


# ── unknown op / error model ────────────────────────────────────────


def test_unknown_op_is_e_op_unknown() -> None:
    error = _expect_error("E_OP_UNKNOWN", _project([]), {"op": "explode"})
    assert "explode" in str(error)


def test_timeline_patch_error_is_value_error_with_code_and_message() -> None:
    error = TimelinePatchError("E_BAD_ARG", "details here")
    assert isinstance(error, ValueError)
    assert error.code == "E_BAD_ARG"
    assert "E_BAD_ARG" in str(error)
    assert "details here" in str(error)


# ── §5 validate_project ─────────────────────────────────────────────


def test_validate_project_detects_missing_track() -> None:
    project = _project([_video_clip("clip_a", 0.0, 2.0)])
    project["timeline"]["clips"][0]["track_id"] = "V9"
    with pytest.raises(TimelinePatchError) as exc:
        validate_project(project)
    assert exc.value.code == "E_NOT_FOUND"


def test_validate_project_detects_overlap() -> None:
    project = _project([_video_clip("clip_a", 0.0, 2.0), _video_clip("clip_b", 1.0, 2.0)])
    with pytest.raises(TimelinePatchError) as exc:
        validate_project(project)
    assert exc.value.code == "E_OVERLAP"


def test_validate_project_exempts_legacy_forced_image_duration() -> None:
    project = _project([_image_clip("img_a", 0.0, 5.0)])
    clip = project["timeline"]["clips"][0]
    clip["source_in"] = 0.0
    clip["source_out"] = IMAGE_DURATION  # legacy forced range with duration 5
    validate_project(project)  # must not raise


def test_validate_project_detects_source_duration_mismatch() -> None:
    project = _project([_video_clip("clip_a", 0.0, 2.0)])
    project["timeline"]["clips"][0]["source_out"] = 5.0
    with pytest.raises(TimelinePatchError) as exc:
        validate_project(project)
    assert exc.value.code == "E_RANGE"


# ── §2 project model normalization contract ─────────────────────────


def test_normalize_respects_explicit_image_duration() -> None:
    project = _project([_image_clip("img_a", 0.0, 5.0)])
    clip = project["timeline"]["clips"][0]
    assert clip["duration"] == pytest.approx(5.0)
    assert clip["source_out"] == pytest.approx(5.0)


def test_normalize_forces_image_duration_only_when_missing() -> None:
    project = _project([_image_clip("img_a", 0.0, 0.0)])
    clip = project["timeline"]["clips"][0]
    assert clip["duration"] == pytest.approx(IMAGE_DURATION)


def test_normalize_adds_keyframes_and_text_config_fields() -> None:
    project = _project([_video_clip("clip_a", 0.0, 2.0)])
    clip = project["timeline"]["clips"][0]
    assert clip["keyframes"] == []
    assert clip["text_config"] is None


def test_default_tracks_stay_v1_a1() -> None:
    tracks = empty_project()["timeline"]["tracks"]
    assert [track["id"] for track in tracks] == ["V1", "A1"]
    assert [track["kind"] for track in tracks] == ["video", "audio"]


def test_normalize_keeps_overlay_track_kind() -> None:
    project = _project([])
    overlay = next(track for track in project["timeline"]["tracks"] if track["id"] == "OV1")
    assert overlay["kind"] == "overlay"


# ── §6.5 atomicity ──────────────────────────────────────────────────


def test_failing_second_op_leaves_input_state_unpolluted() -> None:
    project = _two_video_clips()
    before = copy.deepcopy(project)
    ops = [
        {"op": "move_clip", "clip_id": "clip_b", "start": 9.0},
        {"op": "trim_clip", "clip_id": "clip_a", "source_out": 99.0},  # E_RANGE
    ]
    with pytest.raises(TimelinePatchError) as exc:
        apply_timeline_patches(project, [_patch(*ops)])
    assert exc.value.code == "E_RANGE"
    assert project == before  # caller can safely retry from the old state


# ── §6.6 ProjectStore round trip ────────────────────────────────────


def test_project_store_new_ops_round_trip_with_undo(tmp_path) -> None:
    from gemia.project_store import ProjectStore

    store = ProjectStore(tmp_path / "projects")
    seed = _project([_video_clip("clip_demo", 0.0, 2.0)])
    store.create("proj_m1", seed=seed)
    snapshot = store.load("proj_m1")

    patch = _patch(
        {"op": "split_clip", "clip_id": "clip_demo", "at_time": 1.0, "new_clip_id": "clip_back"},
        {"op": "trim_clip", "clip_id": "clip_back", "source_in": 1.0, "source_out": 1.5},
        {"op": "add_marker", "time": 0.5, "label": "cut here"},
    )
    result = store.apply_patches("proj_m1", [patch], session_id="sess_m1", script_hash="h1")
    assert result["patch_seq_end"] == 1
    state = result["project_state"]
    assert len(state["timeline"]["clips"]) == 2
    assert state["timeline"]["markers"][-1]["label"] == "cut here"

    undone = store.undo_to_seq("proj_m1", 0)
    assert undone["from_seq"] == 1
    assert undone["to_seq"] == 0
    reloaded = store.load("proj_m1")
    assert reloaded["timeline"]["clips"] == snapshot["timeline"]["clips"]
    assert reloaded["timeline"]["markers"] == snapshot["timeline"]["markers"]
    assert reloaded["timeline"]["duration"] == snapshot["timeline"]["duration"]
