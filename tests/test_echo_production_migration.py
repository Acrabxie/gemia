from __future__ import annotations

import json
from pathlib import Path

import pytest

from gemia.project_store import ProjectStore
from scripts.migrate_echo_protocol_production import (
    _asset_relative_path,
    _copy_canonical_project,
    _publish_into_existing_root,
    _rewrite_json_tree,
    migrate,
)


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def test_legacy_patches_are_audit_only_and_never_active(tmp_path: Path) -> None:
    source = tmp_path / "source"
    current_state = {"schema": "test", "assets": [{"id": "img_001", "duration": 7.0}]}
    _write_json(source / "state.json", current_state)
    _write_json(source / "seed.json", {"schema": "test", "assets": []})
    _write_json(source / "meta.json", {"patch_seq": 2})
    for seq in (1, 2):
        _write_json(
            source / "patches" / f"{seq:04d}.json",
            {"seq": seq, "patch": {"version": 1, "ops": []}},
        )

    destination = tmp_path / "destination"
    audit = _copy_canonical_project(source, destination)

    assert json.loads((destination / "state.json").read_text()) == current_state
    assert list((destination / "patches").glob("[0-9]*.json")) == []
    assert audit["active"] is False
    assert [item["seq"] for item in audit["patches"]] == [1, 2]
    assert all(len(item["sha256"]) == 64 for item in audit["patches"])


def test_appledouble_json_is_never_treated_as_project_history(tmp_path: Path) -> None:
    store = ProjectStore(tmp_path / "projects")
    store.create("project")
    patches = store.patches_dir("project")
    (patches / "._0001.json").write_bytes(b"\x00\x05AppleDouble")

    assert store.history("project") == []


def test_staging_path_rewrite_skips_appledouble_metadata(tmp_path: Path) -> None:
    root = tmp_path / "stage"
    _write_json(root / "record.json", {"path": f"{root}/workdirs/project/asset.wav"})
    (root / "._record.json").write_bytes(b"not json")

    changed = _rewrite_json_tree(root, {str(root): "/published/root"})

    assert json.loads((root / "record.json").read_text())["path"].startswith("/published/root")
    assert len(changed) == 1
    assert (root / "._record.json").read_bytes() == b"not json"


def test_asset_path_must_stay_inside_the_frozen_workdir(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="outside the frozen workdir"):
        _asset_relative_path(
            "/etc/passwd",
            session_id="session",
            source_workdir=tmp_path,
        )


def test_invalid_baseline_fails_before_destination_is_visible(tmp_path: Path) -> None:
    baseline = tmp_path / "wrong-baseline"
    (baseline / "workdir/project/wrong-baseline").mkdir(parents=True)
    _write_json(baseline / "history.json", {})
    destination = tmp_path / "published"

    with pytest.raises(RuntimeError):
        migrate(baseline=baseline, output_root=destination, run_id="run")

    assert not destination.exists()


def test_publish_merges_only_managed_namespaces_into_existing_v3_root(
    tmp_path: Path,
) -> None:
    output_root = tmp_path / "v3"
    baseline_marker = output_root / "baselines" / "frozen" / "marker.txt"
    baseline_marker.parent.mkdir(parents=True)
    baseline_marker.write_text("untouched", encoding="utf-8")
    telemetry = output_root / "telemetry.sqlite3"
    telemetry.write_bytes(b"existing telemetry")

    stage = output_root / ".stage"
    _write_json(stage / "projects" / "project" / "state.json", {"ok": True})
    _write_json(stage / "sessions" / "session" / "meta.json", {"ok": True})
    (stage / "workdirs" / "session").mkdir(parents=True)
    (stage / "workdirs" / "session" / "asset.bin").write_bytes(b"asset")
    _write_json(stage / "migration-status.json", {"status": "verified_staging"})

    receipt = _publish_into_existing_root(
        storage_root=stage,
        output_root=output_root,
        project_id="project",
        session_id="session",
    )

    assert baseline_marker.read_text(encoding="utf-8") == "untouched"
    assert telemetry.read_bytes() == b"existing telemetry"
    assert (output_root / "projects/project/state.json").is_file()
    assert (output_root / "sessions/session/meta.json").is_file()
    assert (output_root / "workdirs/session/asset.bin").read_bytes() == b"asset"
    assert json.loads(receipt.read_text(encoding="utf-8"))["completed"] == [
        "workdir", "session", "project", "status"
    ]


def test_publish_refuses_all_components_before_first_namespace_move(
    tmp_path: Path,
) -> None:
    output_root = tmp_path / "v3"
    stage = output_root / ".stage"
    _write_json(stage / "projects" / "project" / "state.json", {"ok": True})
    _write_json(stage / "sessions" / "session" / "meta.json", {"ok": True})
    (stage / "workdirs" / "session").mkdir(parents=True)
    _write_json(stage / "migration-status.json", {"status": "verified_staging"})
    (output_root / "sessions" / "session").mkdir(parents=True)

    with pytest.raises(RuntimeError, match="namespace collision"):
        _publish_into_existing_root(
            storage_root=stage,
            output_root=output_root,
            project_id="project",
            session_id="session",
        )

    assert (stage / "projects/project/state.json").is_file()
    assert (stage / "workdirs/session").is_dir()
