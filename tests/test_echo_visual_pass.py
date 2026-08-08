from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from gemia.echo_local_media import sha256_file
from gemia.project_model import empty_project, normalize_project
from lumerai.patches import apply_timeline_patches
from scripts import build_echo_protocol_v1 as board_builder
from scripts import echo_visual_pass as visual


def _sources() -> dict[str, str]:
    return {slot: f"stock-{slot}" for slot in board_builder.SOURCE_SLOTS}


@dataclass
class _Record:
    asset_id: str
    kind: str
    path: Path
    summary: str
    lineage: tuple[str, ...]
    sha256: str
    source: dict[str, Any]
    license: dict[str, Any]


class _Registry:
    def __init__(self) -> None:
        self.records: dict[str, _Record] = {}
        self.video_counter = 10
        self.allocate_calls = 0
        self.register_calls = 0

    def get(self, asset_id: str) -> _Record:
        try:
            return self.records[asset_id]
        except KeyError:
            raise KeyError(asset_id) from None

    def list_records(self) -> list[_Record]:
        return list(self.records.values())

    def allocate_id(self, kind: str) -> str:
        assert kind == "video"
        self.allocate_calls += 1
        self.video_counter += 1
        return f"v_{self.video_counter:03d}"

    def register_output(self, asset_id: str, **kwargs: Any) -> _Record:
        assert asset_id not in self.records
        self.register_calls += 1
        path = Path(kwargs["path"]).resolve()
        record = _Record(
            asset_id=asset_id,
            kind=str(kwargs["kind"]),
            path=path,
            summary=str(kwargs["summary"]),
            lineage=tuple(kwargs.get("lineage") or ()),
            sha256=sha256_file(path),
            source=dict(kwargs.get("source") or {}),
            license=dict(kwargs.get("license") or {}),
        )
        self.records[asset_id] = record
        return record


def _asset(asset_id: str, kind: str, path: Path, duration: float) -> dict[str, Any]:
    return {
        "id": asset_id,
        "asset_id": asset_id,
        "name": path.name,
        "media_kind": kind,
        "source_path": str(path),
        "duration": duration,
    }


def _audio_clip(
    asset_id: str, track_id: str, start: float, duration: float, *, suffix: str = ""
) -> dict[str, Any]:
    return {
        "id": f"clip-{asset_id}{suffix}",
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


def _seed_project(tmp_path: Path, registry: _Registry) -> tuple[dict[str, Any], tuple]:
    board = board_builder.build_board(_sources())
    state = empty_project(title="回声协议")
    state["shotlist"] = board_builder.build_shotlist(board)
    state["timeline"]["tracks"].extend(
        [
            {"id": "A2", "kind": "audio", "name": "Narration", "index": 2},
            {"id": "A3", "kind": "audio", "name": "SFX", "index": 3},
        ]
    )

    image_ids = sorted({unit.asset_id for unit in board if not unit.is_public_motion})
    for asset_id in image_ids:
        path = tmp_path / "images" / f"{asset_id}.jpg"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"image-bytes-{asset_id}".encode())
        registry.records[asset_id] = _Record(
            asset_id=asset_id,
            kind="image",
            path=path,
            summary="migrated image",
            lineage=(),
            sha256=sha256_file(path),
            source={"kind": "generated_image", "provider": "vertex", "receipt_id": "base"},
            license={"basis": "provider_generated_asset"},
        )
        state["assets"].append(_asset(asset_id, "image", path, 3.0))

    for slot, asset_id in _sources().items():
        path = tmp_path / "stock" / f"{slot}.mp4"
        registry.records[asset_id] = _Record(
            asset_id=asset_id,
            kind="video",
            path=path,
            summary="reviewed stock",
            lineage=(),
            sha256=f"stock-hash-{slot}",
            source={
                "kind": "public_stock",
                "provider": "pexels",
                "production_slot": slot,
                "production_source_status": "approved",
                "real_motion_verified": True,
            },
            license={"name": "Pexels license"},
        )
        state["assets"].append(_asset(asset_id, "video", path, 8.0))

    digest = board_builder.board_digest(board)
    for unit in board:
        clip = {
            "id": unit.clip_id,
            "asset_id": unit.asset_id,
            "track_id": "V1",
            "name": f"{unit.asset_id}.{'mp4' if unit.is_public_motion else 'jpg'}",
            "media_kind": "video" if unit.is_public_motion else "image",
            "start": unit.start_sec,
            "duration": unit.duration_sec,
            "source_in": unit.source_in if unit.is_public_motion else 0.0,
            "source_out": unit.source_out if unit.is_public_motion else unit.duration_sec,
            "enabled": True,
            "provenance": {
                "source": "echo_protocol_v1_board",
                "run_id": board_builder.RUN_ID,
                "trace_id": "trace-rough-cut",
                "unit_id": unit.shot_id,
                "board_digest": digest,
            },
        }
        if unit.is_public_motion:
            clip["effects"] = {"muted": True}
        state["timeline"]["clips"].append(clip)

    audio_dir = tmp_path / "audio"
    for asset_id in (
        board_builder.EXPECTED_MUSIC_ASSET,
        *board_builder.EXPECTED_NARRATION_ASSETS,
    ):
        role = "music" if asset_id == board_builder.EXPECTED_MUSIC_ASSET else "narration"
        path = audio_dir / f"{asset_id}.wav"
        registry.records[asset_id] = _Record(
            asset_id=asset_id,
            kind="audio",
            path=path,
            summary=role,
            lineage=(),
            sha256=f"hash-{asset_id}",
            source={"kind": "owned_audio", "provider": "local", "role": role},
            license={"basis": "owned"},
        )
        state["assets"].append(_asset(asset_id, "audio", path, 120.0 if role == "music" else 5.0))
    state["timeline"]["clips"].append(
        _audio_clip(board_builder.EXPECTED_MUSIC_ASSET, "A1", 0.0, 120.0)
    )
    state["timeline"]["clips"].extend(
        _audio_clip(asset_id, "A2", index * 8.0, 5.0)
        for index, asset_id in enumerate(board_builder.EXPECTED_NARRATION_ASSETS)
    )

    sfx_id = "aud_101"
    sfx_path = audio_dir / "impact.wav"
    registry.records[sfx_id] = _Record(
        asset_id=sfx_id,
        kind="audio",
        path=sfx_path,
        summary="impact",
        lineage=(),
        sha256="hash-sfx",
        source={"kind": "owned_audio", "provider": "local", "role": "sfx"},
        license={"basis": "owned"},
    )
    state["assets"].append(_asset(sfx_id, "audio", sfx_path, 0.8))
    state["timeline"]["clips"].append(_audio_clip(sfx_id, "A3", 20.0, 0.8))
    return normalize_project(state), board


class _Store:
    def __init__(self) -> None:
        self.patch_seq = 9

    def load_meta(self, project_id: str) -> dict[str, Any]:
        assert project_id == "project-echo"
        return {"patch_seq": self.patch_seq}


class _Project:
    def __init__(self, state: dict[str, Any]) -> None:
        self.project_id = "project-echo"
        self.state = state
        self.store = _Store()
        self.applied_ops: list[list[dict[str, Any]]] = []

    def load(self) -> dict[str, Any]:
        return deepcopy(self.state)

    def apply_ops(self, ops: list[dict[str, Any]], *, label: str) -> dict[str, Any]:
        self.state = apply_timeline_patches(self.state, [{"version": 1, "ops": deepcopy(ops)}])
        self.store.patch_seq += 1
        self.applied_ops.append(deepcopy(ops))
        return {"project_state": self.state, "label": label}


class _Manager:
    def __init__(self) -> None:
        self.state = "visual_pass"
        self.production_revision = 30
        self.budget = {
            "limit_usd": 15.0,
            "spent_usd": 1.525,
            "reserved_usd": 0.0,
            "remaining_usd": 13.475,
            "duplicate_billing_count": 0,
            "veo_reserved_calls": 0,
            "veo_reserved_duration_sec": 0.0,
        }
        self.evidence: dict[str, dict[str, Any]] = {}
        self.transitions: list[dict[str, Any]] = []
        self.fail_next_evidence = False

    def get_run(self, project_id: str, run_id: str) -> dict[str, Any]:
        assert project_id == "project-echo" and run_id == visual.RUN_ID
        return {
            "state": self.state,
            "production_state": self.state,
            "revision": self.production_revision,
            "production_revision": self.production_revision,
            "budget": deepcopy(self.budget),
        }

    def record_evidence(self, project_id: str, run_id: str, **kwargs: Any) -> dict[str, Any]:
        if self.fail_next_evidence:
            self.fail_next_evidence = False
            raise RuntimeError("simulated crash after project patch")
        evidence_id = str(kwargs["evidence_id"])
        if evidence_id in self.evidence:
            assert self.evidence[evidence_id] == kwargs
            return {"evidence_id": evidence_id}
        self.evidence[evidence_id] = deepcopy(kwargs)
        self.production_revision += 1
        return {"evidence_id": evidence_id}

    def transition_run(
        self, project_id: str, run_id: str, state: str, **kwargs: Any
    ) -> dict[str, Any]:
        assert kwargs["expected_revision"] == self.production_revision
        self.transitions.append({"state": state, **deepcopy(kwargs)})
        self.state = state
        self.production_revision += 1
        return self.get_run(project_id, run_id)


class _Runner:
    def __init__(
        self, tmp_path: Path, manager: _Manager, registry: _Registry, state: dict[str, Any]
    ) -> None:
        self.project_id = "project-echo"
        self.run_id = visual.RUN_ID
        self.project_revision = 12
        self.output_dir = tmp_path / "workdir"
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.project = _Project(state)
        self.agent = SimpleNamespace(project=self.project, registry=registry)
        self.manager = manager
        self.edit_calls: list[dict[str, Any]] = []
        self.fail_next_edit = False

    def run_project_edit(self, fn, **kwargs: Any) -> Any:
        assert kwargs["expected_project_revision"] == self.project_revision
        self.edit_calls.append(deepcopy(kwargs))
        if self.fail_next_edit:
            self.fail_next_edit = False
            raise RuntimeError("simulated crash before atomic patch")
        result = fn()
        self.project_revision += 1
        return result


class _FakeLocalRenderer:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.probes: dict[str, dict[str, Any]] = {}

    def render(
        self,
        input_path: Path,
        output_path: Path,
        duration: float,
        style: str,
        unit_index: int,
        *,
        source_asset_id: str | None = None,
    ) -> dict[str, Any]:
        output_path = Path(output_path).resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        payload = f"fake-h264|{unit_index}|{style}|{source_asset_id}|{duration}".encode()
        output_path.write_bytes(payload)
        output_hash = sha256_file(output_path)
        fingerprint = visual._stable_digest(
            {
                "unit_index": unit_index,
                "style": style,
                "source_asset_id": source_asset_id,
                "duration": duration,
            }
        )
        lineage = [] if style == "title" else [str(source_asset_id)]
        source = {
            "kind": "owned_video" if style == "title" else "local_mg",
            "provider": "lumeri_local_ffmpeg",
            "generator": "echo_local_media",
            "cost_usd": 0.0,
        }
        license_data = {
            "basis": "project_created_programmatic_video"
            if style == "title"
            else "derived_from_project_asset"
        }
        registration = {
            "kind": "video",
            "path": str(output_path),
            "summary": f"Echo Protocol local {style} motion unit {unit_index}",
            "lineage": lineage,
            "source": source,
            "license": license_data,
        }
        sidecar = {
            "fingerprint": fingerprint,
            "output_sha256": output_hash,
            "registration": registration,
        }
        output_path.with_suffix(output_path.suffix + ".lumeri.json").write_text(
            json.dumps(sidecar), encoding="utf-8"
        )
        probe = {
            "format": {"duration": str(duration)},
            "streams": [
                {
                    "codec_type": "video",
                    "codec_name": "h264",
                    "width": 1920,
                    "height": 1080,
                    "pix_fmt": "yuv420p",
                    "avg_frame_rate": "30/1",
                }
            ],
        }
        self.probes[str(output_path)] = probe
        self.calls.append(
            {
                "unit_index": unit_index,
                "style": style,
                "source_asset_id": source_asset_id,
                "path": output_path,
            }
        )
        return {
            "path": str(output_path),
            "sha256": output_hash,
            "fingerprint": fingerprint,
            "registration_ready": True,
            "lineage": lineage,
            "source": source,
            "license": license_data,
            "registration": registration,
        }

    def probe(self, path: str | Path) -> dict[str, Any]:
        return deepcopy(self.probes[str(Path(path).resolve())])


def _fixture(tmp_path: Path) -> tuple[_Manager, _Runner, _Registry, _FakeLocalRenderer, tuple]:
    registry = _Registry()
    state, board = _seed_project(tmp_path, registry)
    manager = _Manager()
    runner = _Runner(tmp_path, manager, registry, state)
    renderer = _FakeLocalRenderer()
    return manager, runner, registry, renderer, board


def test_approved_style_map_is_exact_and_covers_all_22_local_units() -> None:
    board = board_builder.build_board(_sources())
    actual = {
        unit.index: visual.style_for_unit(unit.index) for unit in board if not unit.is_public_motion
    }
    expected = {unit.index: "ken_burns" for unit in board if not unit.is_public_motion}
    for index in (1, 9):
        expected[index] = "hud"
    for index in (10, 11, 28, 30, 35, 37):
        expected[index] = "hero"
    expected[27] = "memory_fold"
    expected[39] = "white_collapse"
    expected[40] = "white_collapse"
    expected[41] = "iris"
    expected[42] = "title"
    assert actual == expected
    assert len(actual) == 22


def test_visual_pass_is_one_atomic_budget_neutral_patch_with_true_lineage(
    tmp_path: Path,
) -> None:
    manager, runner, registry, renderer, board = _fixture(tmp_path)
    before = runner.project.load()
    audio_before = visual._audio_snapshot(before)
    stock_before = visual._stock_snapshot(before, board)

    result = visual.execute_visual_pass(
        manager,
        runner,
        _sources(),
        render_fn=renderer.render,
        probe_fn=renderer.probe,
    )

    assert result["production_state"] == "rendering"
    assert result["video_clip_count"] == 42
    assert result["local_baked_clip_count"] == 22
    assert result["public_motion_sec"] == 60
    assert result["patch_applied"] is True
    assert len(runner.edit_calls) == 1
    assert runner.project.store.patch_seq == 10
    assert runner.project_revision == 13
    assert len(runner.project.applied_ops) == 1
    ops = runner.project.applied_ops[0]
    assert ops[0]["op"] == "set_shotlist"
    assert sum(op["op"] == "upsert_asset" for op in ops) == 22
    assert sum(op["op"] == "delete_clip" for op in ops) == 22
    assert sum(op["op"] == "insert_clip" for op in ops) == 22

    after = runner.project.load()
    assert visual._audio_snapshot(after) == audio_before
    assert visual._stock_snapshot(after, board) == stock_before
    v1 = [clip for clip in after["timeline"]["clips"] if clip["track_id"] == "V1"]
    assert len(v1) == 42
    assert all(clip["media_kind"] == "video" for clip in v1)
    assert all(
        clip["effects"]["muted"] is True
        for clip in v1
        if clip["id"] in {unit.clip_id for unit in board if unit.is_public_motion}
    )

    new_records = [
        record
        for record in registry.list_records()
        if record.source.get("production_run_id") == visual.RUN_ID
    ]
    assert len(new_records) == 22
    title = next(
        record for record in new_records if record.source["production_unit_id"] == "echo_v1_42"
    )
    assert title.source["kind"] == "owned_video"
    assert title.lineage == ()
    derived = [record for record in new_records if record is not title]
    assert len(derived) == 21
    assert all(record.source["kind"] == "local_mg" for record in derived)
    assert all(len(record.lineage) == 1 for record in derived)
    assert all(record.license["basis"] == "derived_from_project_asset" for record in derived)
    assert manager.budget["spent_usd"] == 1.525
    assert manager.budget["veo_reserved_calls"] == 0
    evidence = next(iter(manager.evidence.values()))
    assert evidence["payload"]["checks"]["single_atomic_patch"] is True
    assert evidence["payload"]["checks"]["audio_clips_unchanged"] is True
    assert manager.transitions[0]["state"] == "rendering"


def test_rendering_replay_reconciles_without_render_allocation_or_patch(
    tmp_path: Path,
) -> None:
    manager, runner, registry, renderer, _board = _fixture(tmp_path)
    visual.execute_visual_pass(
        manager, runner, _sources(), render_fn=renderer.render, probe_fn=renderer.probe
    )
    counts = (
        len(renderer.calls),
        registry.allocate_calls,
        registry.register_calls,
        len(runner.edit_calls),
        len(manager.evidence),
        len(manager.transitions),
    )

    replay = visual.execute_visual_pass(
        manager, runner, _sources(), render_fn=renderer.render, probe_fn=renderer.probe
    )

    assert replay["replayed"] is True
    assert replay["production_state"] == "rendering"
    assert (
        len(renderer.calls),
        registry.allocate_calls,
        registry.register_calls,
        len(runner.edit_calls),
        len(manager.evidence),
        len(manager.transitions),
    ) == counts


def test_registered_outputs_resume_after_crash_without_duplicate_assets(
    tmp_path: Path,
) -> None:
    manager, runner, registry, renderer, _board = _fixture(tmp_path)
    runner.fail_next_edit = True

    with pytest.raises(RuntimeError, match="simulated crash"):
        visual.execute_visual_pass(
            manager, runner, _sources(), render_fn=renderer.render, probe_fn=renderer.probe
        )
    assert registry.allocate_calls == 22
    assert registry.register_calls == 22
    assert runner.project.store.patch_seq == 9
    assert manager.state == "visual_pass"

    result = visual.execute_visual_pass(
        manager, runner, _sources(), render_fn=renderer.render, probe_fn=renderer.probe
    )
    assert result["production_state"] == "rendering"
    assert registry.allocate_calls == 22
    assert registry.register_calls == 22
    assert len(registry.records) == 14 + 10 + 15 + 22


def test_committed_patch_recovers_trace_then_only_records_evidence_and_transitions(
    tmp_path: Path,
) -> None:
    manager, runner, registry, renderer, _board = _fixture(tmp_path)
    manager.fail_next_evidence = True

    with pytest.raises(RuntimeError, match="after project patch"):
        visual.execute_visual_pass(
            manager, runner, _sources(), render_fn=renderer.render, probe_fn=renderer.probe
        )
    assert manager.state == "visual_pass"
    assert (
        visual.classify_visual_timeline(
            runner.project.load(), board_builder.build_board(_sources())
        )
        == "final"
    )
    counts = (
        len(renderer.calls),
        registry.allocate_calls,
        registry.register_calls,
        len(runner.edit_calls),
        runner.project.store.patch_seq,
        runner.project_revision,
    )

    result = visual.execute_visual_pass(
        manager, runner, _sources(), render_fn=renderer.render, probe_fn=renderer.probe
    )

    assert result["production_state"] == "rendering"
    assert result["patch_applied"] is False
    assert (
        len(renderer.calls),
        registry.allocate_calls,
        registry.register_calls,
        len(runner.edit_calls),
        runner.project.store.patch_seq,
        runner.project_revision,
    ) == counts
    assert len(manager.evidence) == 1
    assert manager.transitions[-1]["state"] == "rendering"


def test_budget_change_during_local_bake_fails_before_timeline_commit(
    tmp_path: Path,
) -> None:
    manager, runner, registry, renderer, _board = _fixture(tmp_path)

    def render_and_mutate_budget(*args: Any, **kwargs: Any) -> dict[str, Any]:
        result = renderer.render(*args, **kwargs)
        if len(renderer.calls) == 22:
            manager.budget["reserved_usd"] = 0.25
            manager.budget["remaining_usd"] = 13.225
        return result

    with pytest.raises(visual.EchoVisualPassError, match="budget changed during local baking"):
        visual.execute_visual_pass(
            manager,
            runner,
            _sources(),
            render_fn=render_and_mutate_budget,
            probe_fn=renderer.probe,
        )

    assert registry.register_calls == 22
    assert runner.edit_calls == []
    assert runner.project.store.patch_seq == 9
    assert runner.project_revision == 12
    assert manager.state == "visual_pass"
    assert manager.evidence == {}


def test_partial_timeline_fails_before_render_registration_or_edit(tmp_path: Path) -> None:
    manager, runner, registry, renderer, _board = _fixture(tmp_path)
    target = next(
        clip
        for clip in runner.project.state["timeline"]["clips"]
        if clip.get("id") == "echo_v1_u01"
    )
    target["media_kind"] = "video"
    target["asset_id"] = "v_interrupted"

    with pytest.raises(visual.EchoVisualPassError, match="partial visual-pass timeline"):
        visual.execute_visual_pass(
            manager, runner, _sources(), render_fn=renderer.render, probe_fn=renderer.probe
        )
    assert renderer.calls == []
    assert registry.allocate_calls == 0
    assert registry.register_calls == 0
    assert runner.edit_calls == []
    assert manager.evidence == {}


def test_registry_path_or_unit_conflict_fails_closed_without_guessing_id(
    tmp_path: Path,
) -> None:
    registry = _Registry()
    output = tmp_path / "work" / "unit.mp4"
    output.parent.mkdir(parents=True)
    output.write_bytes(b"current-output")
    baked = {
        "unit_index": 1,
        "unit_id": "echo_v1_01",
        "clip_id": "echo_v1_u01",
        "source_asset_id": "img_001",
        "style": "hud",
        "path": output,
        "sha256": sha256_file(output),
        "fingerprint": "fingerprint-new",
        "lineage": ["img_001"],
        "source": {"kind": "local_mg", "provider": "local"},
        "license": {"basis": "derived_from_project_asset"},
        "summary": "unit one",
    }
    registry.records["v_existing"] = _Record(
        asset_id="v_existing",
        kind="video",
        path=output,
        summary="old conflicting unit",
        lineage=("img_999",),
        sha256="old-hash",
        source={"kind": "local_mg", "provider": "local"},
        license={"basis": "derived_from_project_asset"},
    )

    with pytest.raises(visual.EchoVisualPassError, match="conflicts"):
        visual.reconcile_registry_output(registry, baked)
    assert registry.allocate_calls == 0
    assert registry.register_calls == 0


def test_direct_invocation_is_pinned_to_its_repository() -> None:
    assert Path(visual.__file__).resolve().parents[1] == visual.REPO_ROOT
    sys_path_first = visual.sys.path[0]
    assert sys_path_first
    assert Path(sys_path_first).resolve() == visual.REPO_ROOT
