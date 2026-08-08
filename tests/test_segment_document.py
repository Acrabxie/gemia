from __future__ import annotations

import copy

import pytest

from gemia.project_model import empty_project, normalize_project
from gemia.project_store import ProjectStore, ProjectHandle
from lumerai.patches import TimelinePatchError, apply_timeline_patches


def project_with_clip():
    project = empty_project(title="segment")
    project["assets"] = [{
        "id": "asset_1", "name": "shot.mp4", "media_kind": "video",
        "mime_type": "video/mp4", "source_path": "/tmp/shot.mp4", "duration": 6,
    }]
    project["timeline"]["clips"] = [{
        "id": "clip_1", "asset_id": "asset_1", "track_id": "V1", "name": "shot.mp4",
        "media_kind": "video", "start": 0.0, "duration": 6.0,
        "source_in": 0.0, "source_out": 6.0, "enabled": True,
    }]
    return normalize_project(project)


def patch(*ops):
    return [{"version": 1, "ops": list(ops)}]


def test_reading_flat_clip_is_zero_write_and_structural():
    project = project_with_clip()
    assert project["segments"] == {}
    read = apply_timeline_patches(project, [])
    assert read["segments"] == {}
    assert read["timeline"]["clips"][0].get("segment_ref") is None


def test_agent_insert_enters_timeline_with_parseable_segment():
    project = empty_project(title="insert")
    project = apply_timeline_patches(project, patch({
        "op": "insert_clip", "track_id": "V1", "at": "append",
        "data": {
            "asset": {"id": "asset_1", "name": "new.mp4", "media_kind": "video", "duration": 2.0},
            "clip": {"id": "created_clip", "asset_id": "asset_1", "media_kind": "video", "duration": 2.0, "source_in": 0.0, "source_out": 2.0},
        },
    }))
    clip = project["timeline"]["clips"][0]
    assert clip["segment_ref"] in project["segments"]
    assert project["segments"][clip["segment_ref"]]["layers"]


def test_first_edit_lazily_creates_segment_and_claims_layer():
    project = project_with_clip()
    updated = apply_timeline_patches(project, patch({
        "op": "segment_edit", "clip_id": "clip_1", "action": "set_layer",
        "entity_ref": "layer_clip_1", "changes": {"visible": False},
        "expected_segment_revision": 0, "actor": "human", "client_op_id": "human-1",
    }))
    clip = updated["timeline"]["clips"][0]
    assert clip["segment_ref"] in updated["segments"]
    segment = updated["segments"][clip["segment_ref"]]
    assert segment["persisted"] is True
    assert segment["revision"] == 1
    assert segment["layers"][0]["visible"] is False
    assert segment["reservations"]["layer_clip_1"]["owner"] == "human"


def test_agent_cannot_overwrite_reserved_object_but_can_edit_other_layer():
    project = project_with_clip()
    project = apply_timeline_patches(project, patch({
        "op": "segment_edit", "clip_id": "clip_1", "action": "set_layer",
        "entity_ref": "layer_clip_1", "changes": {"visible": False},
        "actor": "human", "client_op_id": "human-1",
    }))
    ref = project["timeline"]["clips"][0]["segment_ref"]
    segment = copy.deepcopy(project["segments"][ref])
    segment["layers"].append({"id": "layer_extra", "name": "额外", "visible": True, "revision": 0})
    project["segments"][ref] = segment
    with pytest.raises(TimelinePatchError, match="E_ENTITY_RESERVED"):
        apply_timeline_patches(project, patch({
            "op": "segment_edit", "clip_id": "clip_1", "action": "set_layer",
            "entity_ref": "layer_clip_1", "changes": {"opacity": 0.2},
            "actor": "agent", "client_op_id": "agent-blocked",
            "expected_segment_revision": 1,
        }))
    edited = apply_timeline_patches(project, patch({
        "op": "segment_edit", "clip_id": "clip_1", "action": "set_layer",
        "entity_ref": "layer_extra", "changes": {"opacity": 0.6},
        "actor": "agent", "client_op_id": "agent-free",
        "expected_segment_revision": 1,
    }))
    assert edited["segments"][ref]["layers"][-1]["opacity"] == 0.6
    assert edited["segments"][ref]["handback_required"] is True


def test_save_releases_reservations_and_revision_conflict_is_fail_closed():
    project = apply_timeline_patches(project_with_clip(), patch({
        "op": "segment_edit", "clip_id": "clip_1", "action": "set_layer",
        "entity_ref": "layer_clip_1", "changes": {"visible": False},
        "actor": "human", "client_op_id": "human-1",
    }))
    with pytest.raises(TimelinePatchError, match="E_SEGMENT_REVISION_CONFLICT"):
        apply_timeline_patches(project, patch({
            "op": "segment_edit", "clip_id": "clip_1", "action": "set_layer",
            "entity_ref": "layer_clip_1", "changes": {"visible": True},
            "actor": "human", "client_op_id": "stale", "expected_segment_revision": 0,
        }))
    saved = apply_timeline_patches(project, patch({
        "op": "segment_edit", "clip_id": "clip_1", "action": "save",
        "actor": "human", "client_op_id": "save-1", "expected_segment_revision": 1,
    }))
    ref = saved["timeline"]["clips"][0]["segment_ref"]
    assert saved["segments"][ref]["reservations"] == {}
    assert saved["segments"][ref]["handback_required"] is False


def test_split_and_duplicate_deep_copy_segment_identity():
    project = apply_timeline_patches(project_with_clip(), patch({
        "op": "segment_edit", "clip_id": "clip_1", "action": "set_state",
        "entity_ref": "state_clip_1", "changes": {"dwell_sec": 2.5},
        "actor": "human", "client_op_id": "edit-1",
    }))
    split = apply_timeline_patches(project, patch({
        "op": "split_clip", "clip_id": "clip_1", "at_time": 2.0, "new_clip_id": "clip_2",
    }))
    first, second = split["timeline"]["clips"]
    assert first["segment_ref"] != second["segment_ref"]
    assert split["segments"][first["segment_ref"]]["clip_id"] == "clip_1"
    assert split["segments"][second["segment_ref"]]["clip_id"] == "clip_2"
    duplicated = apply_timeline_patches(split, patch({
        "op": "duplicate_clip", "clip_id": "clip_2", "new_clip_id": "clip_3",
    }))
    refs = [c.get("segment_ref") for c in duplicated["timeline"]["clips"]]
    assert len(set(refs)) == 3


def test_split_flat_legacy_clip_materializes_two_independent_documents():
    split = apply_timeline_patches(project_with_clip(), patch({
        "op": "split_clip", "clip_id": "clip_1", "at_time": 2.0, "new_clip_id": "clip_2",
    }))
    first, second = split["timeline"]["clips"]
    assert first["segment_ref"] != second["segment_ref"]
    assert split["segments"][first["segment_ref"]]["timeline"][0]["duration"] == 2.0
    assert split["segments"][second["segment_ref"]]["timeline"][0]["duration"] == 4.0


def test_state_tree_add_copy_delete_and_reorder():
    project = project_with_clip()
    project = apply_timeline_patches(project, patch({
        "op": "segment_edit", "clip_id": "clip_1", "action": "state_add",
        "actor": "human", "client_op_id": "state-add",
    }))
    ref = project["timeline"]["clips"][0]["segment_ref"]
    state_id = project["segments"][ref]["states"][-1]["id"]
    project = apply_timeline_patches(project, patch({
        "op": "segment_edit", "clip_id": "clip_1", "action": "state_copy",
        "entity_ref": state_id, "new_state_id": "state_copy", "actor": "human", "client_op_id": "state-copy",
        "expected_segment_revision": 1,
    }))
    project = apply_timeline_patches(project, patch({
        "op": "segment_edit", "clip_id": "clip_1", "action": "state_reorder",
        "entity_ref": "state_copy", "index": 0, "actor": "human", "client_op_id": "state-move",
        "expected_segment_revision": 2,
    }))
    project = apply_timeline_patches(project, patch({
        "op": "segment_edit", "clip_id": "clip_1", "action": "state_delete",
        "entity_ref": state_id, "actor": "human", "client_op_id": "state-delete",
        "expected_segment_revision": 3,
    }))
    assert [s["id"] for s in project["segments"][ref]["states"]] == ["state_copy", "state_clip_1"]


def test_internal_material_time_keeps_outer_clip_authoritative():
    project = project_with_clip()
    project = apply_timeline_patches(project, patch({
        "op": "segment_edit", "clip_id": "clip_1", "action": "set_timeline",
        "entity_ref": "material_clip_1", "changes": {"duration": 4.25},
        "actor": "human", "client_op_id": "time-1", "expected_segment_revision": 0,
    }))
    clip = project["timeline"]["clips"][0]
    ref = clip["segment_ref"]
    assert clip["duration"] == 4.25
    assert clip["source_out"] == 4.25
    assert project["segments"][ref]["timeline"][0]["duration"] == 4.25


def test_segment_render_reference_is_ephemeral_and_uses_layer_visibility_and_geometry():
    from gemia.project_export import _apply_segment_render_refs

    project = apply_timeline_patches(project_with_clip(), patch({
        "op": "segment_edit", "clip_id": "clip_1", "action": "set_layer",
        "entity_ref": "layer_clip_1", "changes": {"visible": False, "x": 120, "scale": 0.8},
        "actor": "human", "client_op_id": "render-ref-1", "expected_segment_revision": 0,
    }))
    _apply_segment_render_refs(project)
    clip = project["timeline"]["clips"][0]
    assert clip.get("_segment_render_hidden") is True
    assert clip["effects"]["x"] == 120
    assert clip["effects"]["scale"] == 0.8


def test_project_store_segment_edit_is_idempotent_and_prompt_visible(tmp_path):
    store = ProjectStore(tmp_path)
    seed = project_with_clip()
    store.create("p1", seed=seed)
    handle = ProjectHandle(store, "p1", session_id="s1")
    op = {
        "op": "segment_edit", "clip_id": "clip_1", "action": "set_layer",
        "entity_ref": "layer_clip_1", "changes": {"visible": False},
        "actor": "human", "client_op_id": "same-op", "expected_segment_revision": 0,
    }
    first = handle.apply_ops([op], client_op_id="same-op")
    second = handle.apply_ops([op], client_op_id="same-op")
    assert first["patch_seq_end"] == second["patch_seq_end"]
    assert second["duplicate"] is True
    assert "segment_revision=1" in handle.segment_compact_text()
    assert "reserved=[layer_clip_1:human]" in handle.segment_compact_text()
