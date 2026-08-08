from __future__ import annotations

from copy import deepcopy
import hashlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from gemia.project_model import empty_project, normalize_project
from lumerai.patches import apply_timeline_patches
from scripts import build_echo_protocol_v1 as rough_cut_builder
from scripts import echo_sound_pass as sound_pass


def _sources() -> dict[str, str]:
    return {slot: f"stock-{slot}" for slot in rough_cut_builder.SOURCE_SLOTS}


def _asset(asset_id: str, kind: str, duration: float) -> dict[str, Any]:
    suffix = {"audio": "wav", "video": "mp4", "image": "jpg"}[kind]
    return {
        "id": asset_id,
        "asset_id": asset_id,
        "name": f"{asset_id}.{suffix}",
        "media_kind": kind,
        "source_path": f"/fixtures/{asset_id}.{suffix}",
        "duration": duration,
    }


def _audio_clip(
    asset_id: str,
    track_id: str,
    start: float,
    duration: float,
    *,
    clip_id: str | None = None,
) -> dict[str, Any]:
    return {
        "id": clip_id or f"clip-{asset_id}",
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


def _rough_cut_project(board: tuple[rough_cut_builder.BoardUnit, ...]) -> dict[str, Any]:
    project = empty_project(title="回声协议")
    project["timeline"]["tracks"].append(
        {
            "id": "A2",
            "kind": "audio",
            "name": "Narration",
            "index": 2,
            "locked": False,
            "muted": False,
            "duck_under": None,
        }
    )
    image_ids = sorted({unit.asset_id for unit in board if not unit.is_public_motion})
    project["assets"] = [_asset(asset_id, "image", 3.0) for asset_id in image_ids]
    project["assets"].extend(
        _asset(unit.asset_id, "video", 8.0)
        for unit in board
        if unit.is_public_motion and unit.take == "a"
    )
    project["assets"].append(
        _asset(rough_cut_builder.EXPECTED_MUSIC_ASSET, "audio", 120.0)
    )
    project["assets"].extend(
        _asset(asset_id, "audio", 5.0)
        for asset_id in rough_cut_builder.EXPECTED_NARRATION_ASSETS
    )
    project["timeline"]["clips"] = [
        _audio_clip(
            rough_cut_builder.EXPECTED_MUSIC_ASSET,
            "A1",
            0.0,
            120.0,
            clip_id="music-original",
        )
    ]
    project["timeline"]["clips"].extend(
        _audio_clip(
            asset_id,
            "A2",
            index * 8.0,
            5.0,
            clip_id=f"narration-original-{index + 1:02d}",
        )
        for index, asset_id in enumerate(rough_cut_builder.EXPECTED_NARRATION_ASSETS)
    )
    project = normalize_project(project)
    facts: dict[str, dict[str, Any]] = {}
    for unit in board:
        if unit.asset_id in facts:
            continue
        kind = "video" if unit.is_public_motion else "image"
        facts[unit.asset_id] = {
            "path": f"/fixtures/{unit.asset_id}.{'mp4' if kind == 'video' else 'jpg'}",
            "asset": _asset(unit.asset_id, kind, 8.0 if kind == "video" else 3.0),
        }
    ops, _trace, _label = rough_cut_builder.build_rough_cut_ops(project, board, facts)
    return apply_timeline_patches(project, [{"version": 1, "ops": ops}])


class _RegistryRecord:
    def __init__(
        self,
        asset_id: str,
        *,
        kind: str,
        path: Path,
        summary: str,
        lineage: tuple[str, ...],
        source: dict[str, Any],
        license: dict[str, Any],
    ) -> None:
        self.asset_id = asset_id
        self.kind = kind
        self.path = path.resolve()
        self.summary = summary
        self.lineage = lineage
        self.source = deepcopy(source)
        self.license = deepcopy(license)
        self.sha256 = hashlib.sha256(self.path.read_bytes()).hexdigest()


class _Registry:
    def __init__(self) -> None:
        self.records: dict[str, _RegistryRecord] = {}
        self.counter = 0
        self.allocate_calls = 0
        self.register_calls = 0

    def list_records(self):
        return list(self.records.values())

    def allocate_id(self, kind: str) -> str:
        assert kind == "audio"
        self.allocate_calls += 1
        self.counter += 1
        # Deliberately not an aud_NNN shape: the operator must consume the
        # returned id rather than guessing the production counter.
        return f"registry-assigned-sfx-{self.counter}"

    def register_output(self, asset_id: str, **kwargs):
        assert asset_id not in self.records
        self.register_calls += 1
        record = _RegistryRecord(
            asset_id,
            kind=kwargs["kind"],
            path=Path(kwargs["path"]),
            summary=kwargs["summary"],
            lineage=tuple(kwargs["lineage"]),
            source=kwargs["source"],
            license=kwargs["license"],
        )
        self.records[asset_id] = record
        return record


class _Store:
    def __init__(self) -> None:
        self.patch_seq = 1

    def load_meta(self, project_id: str) -> dict[str, Any]:
        assert project_id == "project-echo"
        return {"patch_seq": self.patch_seq}


class _Project:
    def __init__(self, state: dict[str, Any]) -> None:
        self.project_id = "project-echo"
        self.state = deepcopy(state)
        self.store = _Store()
        self.applied_ops: list[list[dict[str, Any]]] = []
        self.labels: list[str] = []

    def load(self) -> dict[str, Any]:
        return deepcopy(self.state)

    def apply_ops(self, ops, *, label: str):
        self.state = apply_timeline_patches(
            self.state,
            [{"version": 1, "ops": deepcopy(ops)}],
        )
        self.store.patch_seq += 1
        self.applied_ops.append(deepcopy(ops))
        self.labels.append(label)
        return {"project_state": deepcopy(self.state), "patch_seq_end": self.store.patch_seq}


class _Manager:
    def __init__(self) -> None:
        self.state = "sound_pass"
        self.production_revision = 20
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
        assert project_id == "project-echo" and run_id == sound_pass.RUN_ID
        return {
            "state": self.state,
            "production_state": self.state,
            "revision": self.production_revision,
            "production_revision": self.production_revision,
            "budget": deepcopy(self.budget),
        }

    def record_evidence(self, project_id, run_id, **kwargs):
        assert project_id == "project-echo" and run_id == sound_pass.RUN_ID
        self.evidence.append(deepcopy(kwargs))
        self.production_revision += 1
        return {"evidence_id": kwargs["evidence_id"]}

    def transition_run(self, project_id, run_id, state, **kwargs):
        assert project_id == "project-echo" and run_id == sound_pass.RUN_ID
        assert kwargs["expected_revision"] == self.production_revision
        self.transitions.append({"state": state, **deepcopy(kwargs)})
        self.state = state
        self.production_revision += 1
        return self.get_run(project_id, run_id)


class _Runner:
    def __init__(self, state: dict[str, Any], manager: _Manager, output_dir: Path) -> None:
        self.project_id = "project-echo"
        self.run_id = sound_pass.RUN_ID
        self.project_revision = 4
        self.output_dir = output_dir.resolve()
        self.project = _Project(state)
        self.registry = _Registry()
        self.agent = SimpleNamespace(project=self.project, registry=self.registry)
        self.manager = manager
        self.edit_calls: list[dict[str, Any]] = []

    def run_project_edit(self, fn, **kwargs):
        assert kwargs["expected_project_revision"] == self.project_revision
        self.edit_calls.append(deepcopy(kwargs))
        result = fn()
        self.project_revision += 1
        return result


def _safe_workdir(tmp_path: Path) -> Path:
    token = hashlib.sha256(str(tmp_path).encode()).hexdigest()[:16]
    root = Path.cwd() / ".pytest_cache" / "echo_sound_pass" / token
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve()


def _fake_probe(path: str | Path) -> dict[str, Any]:
    cue = Path(path).stem
    duration = {
        "impact": 0.8,
        "alarm_glitch": 1.92,
        "riser": 2.4,
        "collapse": 1.6,
    }[cue]
    return {
        "format": {"duration": str(duration)},
        "streams": [
            {
                "codec_type": "audio",
                "codec_name": "pcm_s16le",
                "sample_rate": "48000",
                "channels": 2,
                "duration": str(duration),
            }
        ],
    }


def _fake_synthesize(call_counter: list[Path]):
    def _call(output_dir: str | Path) -> dict[str, dict[str, Any]]:
        directory = Path(output_dir).resolve()
        directory.mkdir(parents=True, exist_ok=True)
        call_counter.append(directory)
        result: dict[str, dict[str, Any]] = {}
        for cue in sound_pass.SFX_CUES:
            path = directory / f"{cue}.wav"
            path.write_bytes(f"deterministic-{cue}".encode())
            result[cue] = {
                "path": str(path),
                "fingerprint": f"fingerprint-{cue}",
                "probe": _fake_probe(path),
            }
        return result

    return _call


def _case(tmp_path: Path) -> tuple[_Manager, _Runner, tuple[rough_cut_builder.BoardUnit, ...]]:
    board = rough_cut_builder.build_board(_sources())
    manager = _Manager()
    runner = _Runner(_rough_cut_project(board), manager, _safe_workdir(tmp_path))
    return manager, runner, board


def test_sound_pass_is_one_atomic_budget_neutral_patch_and_preserves_original_audio(
    tmp_path: Path,
) -> None:
    manager, runner, board = _case(tmp_path)
    original = sound_pass._identity_timecode_snapshot(runner.project.load())
    synth_calls: list[Path] = []

    result = sound_pass.execute_sound_pass(
        manager,
        runner,
        board,
        synthesize_fn=_fake_synthesize(synth_calls),
        probe_fn=_fake_probe,
    )

    assert result["production_state"] == "visual_pass"
    assert result["patch_applied"] is True
    assert result["sfx_asset_count"] == 4
    assert result["sfx_clip_count"] == 11
    assert result["project_revision"] == 5
    assert result["patch_seq"] == 2
    assert len(runner.edit_calls) == 1
    assert len(runner.project.applied_ops) == 1
    assert runner.registry.allocate_calls == 4
    assert runner.registry.register_calls == 4
    assert synth_calls == [
        runner.output_dir
        / "production-assets"
        / sound_pass.RUN_ID
        / "sound-pass-v1"
    ]
    assert set(result["asset_ids"].values()) == {
        "registry-assigned-sfx-1",
        "registry-assigned-sfx-2",
        "registry-assigned-sfx-3",
        "registry-assigned-sfx-4",
    }

    persisted = runner.project.load()
    assert sound_pass._identity_timecode_snapshot(persisted) == original
    tracks = {track["id"]: track for track in persisted["timeline"]["tracks"]}
    assert tracks["A1"]["duck_under"] == "A2"
    assert tracks["A3"]["kind"] == "audio"
    sfx = [clip for clip in persisted["timeline"]["clips"] if clip["track_id"] == "A3"]
    assert [clip["id"] for clip in sfx] == [item.clip_id for item in sound_pass.PLACEMENTS]
    assert all(
        runner.registry.records[clip["asset_id"]].source["role"] == "sfx"
        and runner.registry.records[clip["asset_id"]].source["provider"] == "local"
        for clip in sfx
    )
    assert manager.budget["spent_usd"] == 1.525
    assert manager.budget["reserved_usd"] == 0.0
    assert manager.budget["veo_reserved_calls"] == 0
    assert manager.evidence[0]["payload"]["checks"]["budget_unchanged"] is True
    assert manager.evidence[0]["payload"]["checks"]["source_role_sfx_complete"] is True
    assert manager.transitions[0]["state"] == "visual_pass"


def test_complete_sound_pass_rerun_is_a_true_noop(tmp_path: Path) -> None:
    manager, runner, board = _case(tmp_path)
    synth_calls: list[Path] = []
    synth = _fake_synthesize(synth_calls)
    first = sound_pass.execute_sound_pass(
        manager,
        runner,
        board,
        synthesize_fn=synth,
        probe_fn=_fake_probe,
    )
    state_before = runner.project.load()
    patch_seq_before = runner.project.store.patch_seq
    project_revision_before = runner.project_revision
    edit_count = len(runner.edit_calls)
    registration_count = runner.registry.register_calls
    evidence_count = len(manager.evidence)
    transition_count = len(manager.transitions)

    replay = sound_pass.execute_sound_pass(
        manager,
        runner,
        board,
        synthesize_fn=lambda _path: pytest.fail("replay must not synthesize SFX"),
        probe_fn=_fake_probe,
    )

    assert first["production_state"] == "visual_pass"
    assert replay["replayed"] is True
    assert replay["patch_applied"] is False
    assert runner.project.load() == state_before
    assert runner.project.store.patch_seq == patch_seq_before
    assert runner.project_revision == project_revision_before
    assert len(runner.edit_calls) == edit_count
    assert runner.registry.register_calls == registration_count
    assert len(manager.evidence) == evidence_count
    assert len(manager.transitions) == transition_count
    assert len(synth_calls) == 1


def test_partial_stable_sfx_clips_fail_closed_before_synthesis_or_edit(
    tmp_path: Path,
) -> None:
    manager, runner, board = _case(tmp_path)
    partial = runner.project.load()
    partial["timeline"]["tracks"].append(
        {
            "id": "A3",
            "kind": "audio",
            "name": "Sound Design",
            "index": 3,
            "locked": False,
            "muted": False,
            "duck_under": None,
        }
    )
    partial["assets"].append(_asset("partial-sfx", "audio", 0.8))
    partial["timeline"]["clips"].append(
        _audio_clip(
            "partial-sfx",
            "A3",
            0.0,
            0.8,
            clip_id=sound_pass.PLACEMENTS[0].clip_id,
        )
    )
    runner.project.state = normalize_project(partial)

    with pytest.raises(sound_pass.EchoSoundPassError, match="partial deterministic sound pass"):
        sound_pass.execute_sound_pass(
            manager,
            runner,
            board,
            synthesize_fn=lambda _path: pytest.fail("partial state must fail before synthesis"),
            probe_fn=_fake_probe,
        )

    assert runner.edit_calls == []
    assert runner.registry.allocate_calls == 0
    assert manager.evidence == []
    assert manager.transitions == []


def test_budget_change_during_local_registration_refuses_before_project_patch(
    tmp_path: Path,
) -> None:
    manager, runner, board = _case(tmp_path)
    synth = _fake_synthesize([])

    def _mutating_synth(output_dir: str | Path):
        outputs = synth(output_dir)
        manager.budget["reserved_usd"] = 0.25
        manager.budget["remaining_usd"] = 13.225
        return outputs

    with pytest.raises(sound_pass.EchoSoundPassError, match="changed the media budget"):
        sound_pass.execute_sound_pass(
            manager,
            runner,
            board,
            synthesize_fn=_mutating_synth,
            probe_fn=_fake_probe,
        )

    assert runner.edit_calls == []
    assert runner.project.store.patch_seq == 1
    assert runner.project_revision == 4
    assert manager.evidence == []
    assert manager.transitions == []
