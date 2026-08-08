from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from typing import Any

import pytest

from gemia.project_model import empty_project, normalize_project, normalize_shotlist
from lumerai.patches import apply_timeline_patches
from scripts import build_echo_protocol_v1 as builder


def _sources() -> dict[str, str]:
    return {slot: f"asset-{slot}" for slot in builder.SOURCE_SLOTS}


def test_locked_board_is_42_units_120_seconds_with_60_seconds_real_motion() -> None:
    board = builder.build_board(_sources())

    assert len(board) == 42
    assert board[0].start_sec == 0
    assert board[-1].end_sec == 120
    assert max(unit.duration_sec for unit in board) == 3

    stock = [unit for unit in board if unit.is_public_motion]
    local = [unit for unit in board if not unit.is_public_motion]
    assert len(stock) == 20
    assert sum(unit.duration_sec for unit in stock) == 60
    assert len(local) == 22
    assert sum(unit.duration_sec for unit in local) == 60
    for slot in builder.SOURCE_SLOTS:
        uses = [unit for unit in stock if unit.reference == slot]
        assert [unit.take for unit in uses] == ["a", "b"]
        assert [unit.source_in for unit in uses] == [0, 3]
        assert len({unit.asset_id for unit in uses}) == 1


def test_locked_board_matches_the_approved_order_and_boundaries() -> None:
    board = builder.build_board(_sources())
    expected = [
        (0, 2, "mg", "img_001"),
        (2, 5, "image", "img_001"),
        (5, 8, "stock", "s01"),
        (8, 11, "image", "img_002"),
        (11, 14, "stock", "s01"),
        (14, 17, "stock", "s02"),
        (17, 20, "image", "img_003"),
        (20, 23, "stock", "s02"),
        (23, 25, "mg", "img_003"),
        (25, 28, "image", "img_004"),
        (28, 31, "mg", "img_004"),
        (31, 34, "stock", "s03"),
        (34, 37, "image", "img_005"),
        (37, 40, "stock", "s03"),
        (40, 43, "stock", "s04"),
        (43, 46, "stock", "s04"),
        (46, 49, "image", "img_006"),
        (49, 52, "stock", "s05"),
        (52, 55, "stock", "s05"),
        (55, 58, "image", "img_007"),
        (58, 61, "stock", "s06"),
        (61, 64, "stock", "s06"),
        (64, 67, "image", "img_008"),
        (67, 70, "stock", "s07"),
        (70, 73, "stock", "s07"),
        (73, 76, "image", "img_009"),
        (76, 78, "mg", "img_009"),
        (78, 81, "image", "img_010"),
        (81, 84, "stock", "s08"),
        (84, 86, "mg", "img_010"),
        (86, 89, "stock", "s08"),
        (89, 92, "image", "img_011"),
        (92, 95, "stock", "s09"),
        (95, 98, "stock", "s09"),
        (98, 101, "image", "img_013"),
        (101, 104, "stock", "s10"),
        (104, 106, "mg", "img_013"),
        (106, 109, "stock", "s10"),
        (109, 112, "mg", "img_014"),
        (112, 115, "image", "img_014"),
        (115, 118, "mg", "img_014"),
        (118, 120, "mg", "img_015"),
    ]
    assert [
        (unit.start_sec, unit.end_sec, unit.kind, unit.reference) for unit in board
    ] == expected


def test_shotlist_is_deterministic_and_round_trips_as_persistent_ir() -> None:
    board = builder.build_board(_sources())
    first = builder.build_shotlist(board)
    second = builder.build_shotlist(board)

    assert first == second
    persisted = normalize_shotlist(first)
    shots = persisted["scenes"][0]["shots"]
    assert persisted["target_duration_sec"] == 120
    assert len(shots) == 42
    assert sum(float(shot["duration_sec"]) for shot in shots) == 120
    assert all(shot["asset_id"] and shot["status"] == "placed" for shot in shots)
    assert [shot["clip_id"] for shot in shots] == [unit.clip_id for unit in board]
    assert all(builder.BOARD_VERSION in str(shot["notes"]) for shot in shots)
    assert "no AI-video generation" in persisted["style"]


class _Record:
    def __init__(self, asset_id: str, kind: str, slot: str = "") -> None:
        self.asset_id = asset_id
        self.kind = kind
        self.source = {
            "production_slot": slot,
            "production_source_status": "approved" if slot else "",
        }


class _Registry:
    def __init__(self) -> None:
        self.records = {
            asset_id: _Record(asset_id, "video", slot)
            for slot, asset_id in _sources().items()
        }
        for asset_id in (
            "img_001", "img_002", "img_003", "img_004", "img_005",
            "img_006", "img_007", "img_008", "img_009", "img_010",
            "img_011", "img_013", "img_014", "img_015",
        ):
            self.records[asset_id] = _Record(asset_id, "image")

    def get(self, asset_id: str) -> _Record:
        return self.records[asset_id]


def _asset(asset_id: str, kind: str, duration: float) -> dict[str, Any]:
    suffix = "wav" if kind == "audio" else "jpg"
    return {
        "id": asset_id,
        "asset_id": asset_id,
        "name": f"{asset_id}.{suffix}",
        "media_kind": kind,
        "source_path": f"/fixtures/{asset_id}.{suffix}",
        "duration": duration,
    }


def _audio_clip(asset_id: str, track_id: str, start: float, duration: float) -> dict[str, Any]:
    return {
        "id": f"clip-{asset_id}",
        "asset_id": asset_id,
        "track_id": track_id,
        "name": f"{asset_id}.wav",
        "media_kind": "audio",
        "start": start,
        "duration": duration,
        "source_in": 0.0,
        "source_out": duration,
        "enabled": True,
    }


def _seed_project() -> dict[str, Any]:
    project = empty_project(title="Legacy Echo")
    project["timeline"]["tracks"].extend(
        [
            {"id": "OV1", "kind": "overlay", "name": "Overlay", "index": 2},
            {"id": "A2", "kind": "audio", "name": "Narration", "index": 3},
        ]
    )
    image_ids = (
        "img_001", "img_002", "img_003", "img_004", "img_005",
        "img_006", "img_007", "img_008", "img_009", "img_010",
        "img_011", "img_013", "img_014", "img_015",
    )
    project["assets"] = [_asset(asset_id, "image", 3.0) for asset_id in image_ids]
    project["assets"].append(_asset(builder.EXPECTED_MUSIC_ASSET, "audio", 120.0))
    project["assets"].extend(
        _asset(asset_id, "audio", 5.0) for asset_id in builder.EXPECTED_NARRATION_ASSETS
    )
    project["timeline"]["clips"] = [
        {
            "id": "legacy-visual",
            "asset_id": "img_001",
            "track_id": "V1",
            "name": "img_001.jpg",
            "media_kind": "image",
            "start": 0.0,
            "duration": 3.0,
            "source_in": 0.0,
            "source_out": 3.0,
            "enabled": True,
        },
        {
            "id": "legacy-title",
            "asset_id": "",
            "track_id": "OV1",
            "name": "old title",
            "media_kind": "text",
            "start": 0.0,
            "duration": 2.0,
            "source_in": 0.0,
            "source_out": 2.0,
            "enabled": True,
            "text_config": {"content": "old", "color": "#ffffff"},
        },
        _audio_clip(builder.EXPECTED_MUSIC_ASSET, "A1", 0.0, 120.0),
    ]
    project["timeline"]["clips"].extend(
        _audio_clip(asset_id, "A2", index * 8.0, 5.0)
        for index, asset_id in enumerate(builder.EXPECTED_NARRATION_ASSETS)
    )
    return normalize_project(project)


class _Store:
    def __init__(self) -> None:
        self.patch_seq = 4

    def load_meta(self, project_id: str) -> dict[str, Any]:
        assert project_id == "project-echo"
        return {"patch_seq": self.patch_seq}


class _Project:
    def __init__(self) -> None:
        self.project_id = "project-echo"
        self.store = _Store()
        self.state = _seed_project()
        self.labels: list[str] = []
        self.applied_ops: list[list[dict[str, Any]]] = []

    def load(self) -> dict[str, Any]:
        return deepcopy(self.state)

    def apply_ops(self, ops: list[dict[str, Any]], *, label: str):
        self.state = apply_timeline_patches(
            self.state, [{"version": 1, "ops": deepcopy(ops)}]
        )
        self.store.patch_seq += 1
        self.labels.append(label)
        self.applied_ops.append(deepcopy(ops))
        return {"project_state": self.state, "patch_seq_end": self.store.patch_seq}


class _Runner:
    def __init__(self, manager: "_Manager") -> None:
        self.project_id = "project-echo"
        self.run_id = builder.RUN_ID
        self.project_revision = 7
        self.project = _Project()
        self.agent = SimpleNamespace(registry=_Registry(), project=self.project)
        self.manager = manager
        self.edit_calls: list[dict[str, Any]] = []

    def run_project_edit(self, fn, **kwargs):
        assert kwargs["expected_project_revision"] == self.project_revision
        self.edit_calls.append(deepcopy(kwargs))
        result = fn()
        self.project_revision += 1
        if self.manager.mutate_budget_on_edit:
            self.manager.budget["spent_usd"] += 1.0
        return result


class _Manager:
    def __init__(self, *, mutate_budget_on_edit: bool = False) -> None:
        self.state = "rough_cut"
        self.production_revision = 12
        self.mutate_budget_on_edit = mutate_budget_on_edit
        self.budget = {
            "limit_usd": 15.0,
            "spent_usd": 1.525,
            "reserved_usd": 0.0,
            "remaining_usd": 13.475,
            "duplicate_billing_count": 0,
            "veo_reserved_calls": 0,
            "veo_reserved_duration_sec": 0.0,
        }
        self.evidence: list[dict[str, Any]] = []
        self.transitions: list[dict[str, Any]] = []

    def get_run(self, project_id, run_id):
        assert project_id == "project-echo"
        assert run_id == builder.RUN_ID
        return {
            "state": self.state,
            "production_state": self.state,
            "revision": self.production_revision,
            "production_revision": self.production_revision,
            "budget": deepcopy(self.budget),
        }

    def record_evidence(self, project_id, run_id, **kwargs):
        assert project_id == "project-echo" and run_id == builder.RUN_ID
        self.evidence.append(deepcopy(kwargs))
        self.production_revision += 1
        return {"evidence_id": kwargs["evidence_id"]}

    def transition_run(self, project_id, run_id, state, **kwargs):
        assert project_id == "project-echo" and run_id == builder.RUN_ID
        assert kwargs["expected_revision"] == self.production_revision
        self.transitions.append({"state": state, **deepcopy(kwargs)})
        self.state = state
        self.production_revision += 1
        return self.get_run(project_id, run_id)


def _passed_manifest() -> dict[str, Any]:
    return {
        "status": "passed",
        "slots": {
            slot: {
                "asset_id": asset_id,
                "machine_gate": "passed",
                "probe": {"duration_sec": 6.0},
                "review": {"decision": "approve"},
            }
            for slot, asset_id in _sources().items()
        },
    }


def test_review_manifest_requires_ten_approved_distinct_six_second_videos() -> None:
    registry = _Registry()
    assert builder.reviewed_sources_from_manifest(_passed_manifest(), registry) == _sources()

    pending = _passed_manifest()
    pending["slots"]["s04"]["review"]["decision"] = ""
    with pytest.raises(builder.EchoBoardError, match="explicit visual approval"):
        builder.reviewed_sources_from_manifest(pending, registry)

    short = _passed_manifest()
    short["slots"]["s07"]["probe"]["duration_sec"] = 5.9
    with pytest.raises(builder.EchoBoardError, match="two 3-second windows"):
        builder.reviewed_sources_from_manifest(short, registry)


def _fake_facts(board) -> dict[str, dict[str, Any]]:
    facts: dict[str, dict[str, Any]] = {}
    for unit in board:
        if unit.asset_id in facts:
            continue
        kind = "video" if unit.is_public_motion else "image"
        suffix = "mp4" if kind == "video" else "jpg"
        duration = 8.0 if kind == "video" else 0.0
        facts[unit.asset_id] = {
            "asset_id": unit.asset_id,
            "kind": kind,
            "path": f"/fixtures/{unit.asset_id}.{suffix}",
            "sha256": f"hash-{unit.asset_id}",
            "width": 1920,
            "height": 1080,
            "duration_sec": duration,
            "asset": {
                "id": unit.asset_id,
                "asset_id": unit.asset_id,
                "name": f"{unit.asset_id}.{suffix}",
                "media_kind": kind,
                "source_path": f"/fixtures/{unit.asset_id}.{suffix}",
                "duration": duration if kind == "video" else 3.0,
            },
        }
    return facts


def test_atomic_rough_cut_is_idempotent_budget_neutral_and_never_calls_veo(
    monkeypatch,
) -> None:
    manager = _Manager()
    runner = _Runner(manager)
    monkeypatch.setattr(
        builder,
        "inspect_board_assets",
        lambda _runner, board: _fake_facts(board),
    )
    audio_before = builder._protected_audio_snapshot(runner.project.load())
    result = builder.execute_rough_cut(
        manager,
        runner,
        _sources(),
        source_review={"status": "passed", "sha256": "review-hash"},
    )

    assert result["production_state"] == "sound_pass"
    assert result["shot_count"] == 42
    assert result["duration_sec"] == 120
    assert result["public_motion_sec"] == 60
    assert result["patch_applied"] is True
    assert len(runner.edit_calls) == 1
    assert runner.project.store.patch_seq == 5
    assert runner.project_revision == 8
    assert len(runner.project.applied_ops) == 1
    ops = runner.project.applied_ops[0]
    assert [op["op"] for op in ops[:2]] == ["set_project_title", "set_shotlist"]
    assert ops[0]["title"] == "回声协议"
    assert sum(op["op"] == "upsert_asset" for op in ops) == 10
    assert sum(op["op"] == "insert_clip" for op in ops) == 42
    assert all("assemble_shotlist" not in label for label in runner.project.labels)
    assert runner.project.labels[0].startswith("trace-echo-rough-cut-")
    clips = runner.project.load()["timeline"]["clips"]
    v1 = [clip for clip in clips if clip["track_id"] == "V1"]
    assert [clip["id"] for clip in v1] == [f"echo_v1_u{i:02d}" for i in range(1, 43)]
    assert sum(clip["media_kind"] == "video" for clip in v1) == 20
    assert all(
        clip["effects"]["muted"] is True
        for clip in v1
        if clip["media_kind"] == "video"
    )
    assert builder._protected_audio_snapshot(runner.project.load()) == audio_before
    assert manager.budget["spent_usd"] == 1.525
    assert manager.budget["veo_reserved_calls"] == 0
    assert manager.evidence[0]["payload"]["checks"]["budget_unchanged"] is True
    assert manager.transitions[0]["state"] == "sound_pass"

    edit_count = len(runner.edit_calls)
    replay = builder.execute_rough_cut(manager, runner, _sources())
    assert replay["replayed"] is True
    assert len(runner.edit_calls) == edit_count
    assert runner.project.store.patch_seq == 5
    assert len(manager.transitions) == 1


def test_budget_change_fails_closed_before_evidence_or_state_transition(monkeypatch) -> None:
    manager = _Manager(mutate_budget_on_edit=True)
    runner = _Runner(manager)
    monkeypatch.setattr(
        builder,
        "inspect_board_assets",
        lambda _runner, board: _fake_facts(board),
    )

    with pytest.raises(builder.EchoBoardError, match="changed the media budget"):
        builder.execute_rough_cut(manager, runner, _sources())

    assert manager.state == "rough_cut"
    assert manager.evidence == []
    assert manager.transitions == []


def test_partial_stable_board_fails_closed_without_writing(monkeypatch) -> None:
    manager = _Manager()
    runner = _Runner(manager)
    partial = {
        "id": "echo_v1_u01",
        "asset_id": "img_001",
        "track_id": "V1",
        "name": "partial.jpg",
        "media_kind": "image",
        "start": 3.0,
        "duration": 2.0,
        "source_in": 0.0,
        "source_out": 2.0,
        "enabled": True,
    }
    runner.project.state["timeline"]["clips"].append(partial)
    monkeypatch.setattr(
        builder,
        "inspect_board_assets",
        lambda _runner, board: _fake_facts(board),
    )

    with pytest.raises(builder.EchoBoardError, match="partial deterministic rough cut"):
        builder.execute_rough_cut(manager, runner, _sources())

    assert runner.edit_calls == []
    assert runner.project.store.patch_seq == 4
    assert manager.evidence == []
