"""Shotlist / storyboard IR: schema normalization + patch ops.

The shotlist is an outline/storyboard-driven editing plan stored inside
``project_state`` so it rides the same append-only patch log (undo + audit) as
timeline edits. These tests pin three invariants:

* ``normalize_shotlist`` coerces partial, model-authored input into canonical
  shape without raising (garbage dropped, ids backfilled, fields clamped).
* ``set_shotlist`` / ``update_shot`` mutate the IR through the patch pipeline;
  ``update_shot`` is a targeted merge that leaves sibling shots untouched.
* the shotlist survives an unrelated timeline op — the two live side by side in
  one project_state and neither erases the other.
"""
from __future__ import annotations

import pytest

from gemia.project_model import (
    empty_project,
    empty_shotlist,
    iter_shots,
    normalize_project,
    normalize_shotlist,
)
from lumerai.patches import TimelinePatchError, apply_timeline_patches


def _patch(*ops):
    return {"version": 1, "ops": list(ops)}


# ── normalization ──────────────────────────────────────────────────────────
def test_empty_project_has_shotlist():
    p = empty_project()
    assert p["shotlist"] == empty_shotlist()
    assert p["shotlist"]["scenes"] == []


def test_normalize_shotlist_coerces_partial_input():
    sl = normalize_shotlist(
        {
            "logline": "A promo",
            "style": "cinematic",
            "target_duration_sec": 30,
            "scenes": [
                {
                    "title": "Open",
                    "shots": [
                        {
                            "description": "city dawn",
                            "duration_sec": 4,
                            "source": "search",
                            "search_query": "city sunrise",
                            "on_screen_text": "2026",
                        },
                        {"description": "no id, defaults"},
                        "junk-shot",  # non-dict -> normalized to a stub
                    ],
                },
                "junk-scene",  # non-dict -> normalized to an empty scene
            ],
        }
    )
    assert sl["logline"] == "A promo"
    assert sl["target_duration_sec"] == 30.0
    assert len(sl["scenes"]) == 2
    shots = sl["scenes"][0]["shots"]
    assert len(shots) == 3  # every entry survives with a backfilled id
    assert all(s["id"] for s in shots)
    first = shots[0]
    assert first["source"] == "search" and first["status"] == "draft"
    assert first["duration_sec"] == 4.0 and first["on_screen_text"] == "2026"
    assert first["asset_id"] is None and first["transition_after"] is None


def test_normalize_shotlist_clamps_bad_fields():
    sl = normalize_shotlist(
        {"scenes": [{"shots": [{"source": "wat", "status": "nope", "duration_sec": -5}]}]}
    )
    shot = sl["scenes"][0]["shots"][0]
    assert shot["source"] == "unset"
    assert shot["status"] == "draft"
    assert shot["duration_sec"] == 0.1  # clamped to the floor, never <= 0


def test_normalize_shotlist_non_dict_returns_empty():
    assert normalize_shotlist(None) == empty_shotlist()
    assert normalize_shotlist("nope") == empty_shotlist()


def test_transition_cut_collapses_to_none():
    sl = normalize_shotlist(
        {"scenes": [{"shots": [
            {"transition_after": {"kind": "cut"}},
            {"transition_after": {"kind": "dissolve", "duration_sec": 0.75}},
        ]}]}
    )
    shots = sl["scenes"][0]["shots"]
    assert shots[0]["transition_after"] is None
    assert shots[1]["transition_after"] == {"kind": "dissolve", "duration_sec": 0.75}


def test_shotlist_survives_re_normalization():
    p = empty_project()
    raw = {**p, "shotlist": {"scenes": [{"shots": [{"id": "a", "description": "x"}]}]}}
    once = normalize_project(raw)
    twice = normalize_project(once)
    assert once["shotlist"] == twice["shotlist"]
    assert [s["id"] for _, s in iter_shots(twice["shotlist"])] == ["a"]


def test_range_evidence_roundtrips_with_full_provenance():
    raw = {
        "scenes": [{"shots": [{
            "id": "a",
            "asset_id": "session_1",
            "library_asset_id": "lib_1",
            "source_in": 12.0,
            "source_out": 17.0,
            "evidence": {
                "evidence_id": "lib_1:ann_1",
                "annotation_id": "ann_1",
                "library_asset_id": "lib_1",
                "start_sec": 12.0,
                "end_sec": 20.0,
                "label": "best answer",
                "note": "creator reviewed",
                "tags": ["interview", "approved"],
                "category": "dialogue",
                "confidence": 0.73,
                "source": "user",
                "decision": "prefer",
                "metadata": {"evidence": {"version": 1, "claim_key": "editorial.usability"}},
                "score_components": {"query": 0.9, "duration": 1.0},
                "algorithm_version": "evidence-fit-v1",
            },
            "alternatives": [{
                "evidence_id": "lib_2:ann_2",
                "annotation_id": "ann_2",
                "library_asset_id": "lib_2",
                "label": "backup",
                "reason_not_selected": "lower query relevance",
            }],
        }]}],
    }
    once = normalize_shotlist(raw)
    twice = normalize_shotlist(once)
    shot = twice["scenes"][0]["shots"][0]
    assert once == twice
    assert shot["library_asset_id"] == "lib_1"
    assert (shot["source_in"], shot["source_out"]) == (12.0, 17.0)
    assert shot["evidence"]["note"] == "creator reviewed"
    assert shot["evidence"]["tags"] == ["interview", "approved"]
    assert shot["evidence"]["metadata"]["evidence"]["claim_key"] == "editorial.usability"
    assert shot["evidence"]["confidence"] == 0.73
    assert shot["alternatives"][0]["reason_not_selected"] == "lower query relevance"


def test_invalid_source_range_is_cleared_not_invented():
    shot = normalize_shotlist({"scenes": [{"shots": [{
        "asset_id": "session_1", "source_in": 7, "source_out": 4,
    }]}]})["scenes"][0]["shots"][0]
    assert shot["source_in"] is None and shot["source_out"] is None


# ── patch ops ───────────────────────────────────────────────────────────────
def _seeded():
    return apply_timeline_patches(
        empty_project(),
        [_patch({
            "op": "set_shotlist",
            "shotlist": {
                "logline": "promo",
                "target_duration_sec": 20,
                "scenes": [{"id": "sc1", "title": "Open", "shots": [
                    {"id": "a", "description": "city dawn", "duration_sec": 4,
                     "source": "search", "search_query": "city sunrise"},
                    {"id": "b", "description": "logo", "duration_sec": 3},
                ]}],
            },
        })],
    )


def test_set_shotlist_replaces_ir():
    p = _seeded()
    assert [s["id"] for _, s in iter_shots(p["shotlist"])] == ["a", "b"]
    assert p["shotlist"]["target_duration_sec"] == 20.0


def test_set_shotlist_requires_object():
    with pytest.raises(TimelinePatchError):
        apply_timeline_patches(empty_project(), [_patch({"op": "set_shotlist"})])


def test_set_project_title_is_revisionable_and_validated():
    titled = apply_timeline_patches(
        empty_project(), [_patch({"op": "set_project_title", "title": "回声协议"})]
    )
    assert titled["title"] == "回声协议"

    with pytest.raises(TimelinePatchError, match="E_BAD_ARG"):
        apply_timeline_patches(
            titled, [_patch({"op": "set_project_title", "title": "   "})]
        )


def test_update_shot_merges_single_shot():
    p = apply_timeline_patches(
        _seeded(),
        [_patch({"op": "update_shot", "shot_id": "a",
                 "fields": {"asset_id": "vid_007", "status": "filled"}})],
    )
    by_id = {s["id"]: s for _, s in iter_shots(p["shotlist"])}
    assert by_id["a"]["asset_id"] == "vid_007" and by_id["a"]["status"] == "filled"
    # untouched fields preserved
    assert by_id["a"]["description"] == "city dawn" and by_id["a"]["duration_sec"] == 4.0
    # sibling shot fully untouched
    assert by_id["b"]["asset_id"] is None and by_id["b"]["status"] == "draft"


def test_update_shot_cannot_change_id():
    p = apply_timeline_patches(
        _seeded(),
        [_patch({"op": "update_shot", "shot_id": "a", "fields": {"id": "hacked", "notes": "x"}})],
    )
    ids = [s["id"] for _, s in iter_shots(p["shotlist"])]
    assert ids == ["a", "b"] and "hacked" not in ids


def test_update_shot_missing_id_raises_not_found():
    with pytest.raises(TimelinePatchError) as exc:
        apply_timeline_patches(
            _seeded(),
            [_patch({"op": "update_shot", "shot_id": "zzz", "fields": {"notes": "x"}})],
        )
    assert exc.value.code == "E_NOT_FOUND"


def test_update_shot_requires_fields():
    with pytest.raises(TimelinePatchError):
        apply_timeline_patches(
            _seeded(), [_patch({"op": "update_shot", "shot_id": "a"})]
        )


def test_shotlist_coexists_with_timeline_ops():
    seeded = _seeded()
    p = apply_timeline_patches(seeded, [_patch({"op": "add_marker", "time": 1.5, "label": "beat"})])
    # timeline op landed
    assert p["timeline"]["markers"][0]["label"] == "beat"
    # shotlist untouched by the timeline op
    assert [s["id"] for _, s in iter_shots(p["shotlist"])] == ["a", "b"]
