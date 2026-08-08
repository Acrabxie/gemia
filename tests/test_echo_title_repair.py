from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path
from types import MethodType
from typing import Any

import pytest

from gemia.project_model import normalize_project
from scripts import echo_visual_pass as visual
from scripts import repair_echo_title as repair
from tests import test_echo_visual_pass as visual_test


def _safe_root(tmp_path: Path) -> Path:
    resolved = tmp_path.resolve()
    if repair._is_tmp_path(resolved):
        token = hashlib.sha256(str(resolved).encode()).hexdigest()[:16]
        resolved = Path.cwd() / ".pytest_cache" / "echo_title_repair" / token
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def _add_complete_sfx_mix(
    runner: visual_test._Runner, registry: visual_test._Registry, root: Path
) -> None:
    # The visual-pass fixture already contains one A3 cue.  The production
    # case contains eleven, so add ten stable zero-dollar SFX facts.
    for offset in range(10):
        asset_id = f"aud_{102 + offset:03d}"
        path = root / "audio" / f"{asset_id}.wav"
        registry.records[asset_id] = visual_test._Record(
            asset_id=asset_id,
            kind="audio",
            path=path,
            summary="local sfx",
            lineage=(),
            sha256=f"hash-{asset_id}",
            source={
                "kind": "owned_audio",
                "provider": "local",
                "role": "sfx",
                "cost_usd": 0.0,
            },
            license={"basis": "project_created_programmatic_audio"},
        )
        runner.project.state["assets"].append(
            visual_test._asset(asset_id, "audio", path, 0.4)
        )
        runner.project.state["timeline"]["clips"].append(
            visual_test._audio_clip(
                asset_id,
                "A3",
                30.0 + offset * 4.0,
                0.4,
                suffix=f"-{offset}",
            )
        )
    # Match a real ProjectStore load: all clips are canonical before the
    # visual-pass dry run snapshots them.
    runner.project.state = normalize_project(runner.project.state)


def _attach_evidence_ids(manager: visual_test._Manager) -> None:
    original = manager.get_run

    def get_run(project_id: str, run_id: str) -> dict[str, Any]:
        value = original(project_id, run_id)
        value["evidence_ids"] = sorted(
            {repair.REVIEW_EVIDENCE_ID, *manager.evidence.keys()}
        )
        return value

    manager.get_run = get_run  # type: ignore[method-assign]


def _attach_patch_receipts(runner: visual_test._Runner, root: Path) -> None:
    patch_root = root / "project-patches"
    patch_root.mkdir(parents=True, exist_ok=True)
    runner.project.store.patches_dir = MethodType(  # type: ignore[attr-defined]
        lambda _store, project_id: patch_root,
        runner.project.store,
    )
    original = runner.project.apply_ops

    def apply_ops(ops: list[dict[str, Any]], *, label: str) -> dict[str, Any]:
        result = original(ops, label=label)
        # A real ProjectStore normalizes appended assets on the next load.
        # Model that boundary so recovery never depends on raw in-memory shape.
        runner.project.state = normalize_project(runner.project.state)
        seq = runner.project.store.patch_seq
        (patch_root / f"{seq:04d}.json").write_text(
            json.dumps(
                {
                    "seq": seq,
                    "session_id": "session-test",
                    "script_hash": label,
                    "patch": {"version": 1, "ops": deepcopy(ops)},
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        return result

    runner.project.apply_ops = apply_ops  # type: ignore[method-assign]


def _fixture(
    tmp_path: Path,
) -> tuple[
    visual_test._Manager,
    visual_test._Runner,
    visual_test._Registry,
    visual_test._FakeLocalRenderer,
    Path,
    Path,
]:
    root = _safe_root(tmp_path)
    manager, runner, registry, renderer, _board = visual_test._fixture(root)
    _add_complete_sfx_mix(runner, registry, root)
    visual.execute_visual_pass(
        manager,
        runner,
        visual_test._sources(),
        render_fn=renderer.render,
        probe_fn=renderer.probe,
    )
    # The in-memory fake does not normalize on load; the real ProjectStore
    # does.  Canonicalize the newly upserted visual assets before exercising
    # the repair operator.
    runner.project.state = normalize_project(runner.project.state)
    manager.state = "revising"
    _attach_evidence_ids(manager)
    _attach_patch_receipts(runner, root)
    font = root / "fonts" / "complete-cjk-font.ttc"
    font.parent.mkdir(parents=True, exist_ok=True)
    font.write_bytes(b"deterministic-complete-cjk-font")
    return manager, runner, registry, renderer, root, font


def test_title_repair_is_one_atomic_target_only_budget_neutral_patch(
    tmp_path: Path,
) -> None:
    manager, runner, registry, renderer, _root, font = _fixture(tmp_path)
    before = runner.project.load()
    before_audio = repair._audio_snapshot(before)
    before_clips = repair._non_target_clip_snapshot(before)
    before_shots = repair._non_target_shot_snapshot(before)
    before_assets = repair._project_assets(before)
    old_target = repair._clip_by_id(before, repair.TARGET_CLIP_ID)
    edit_count = len(runner.edit_calls)
    allocation_count = registry.allocate_calls
    render_count = len(renderer.calls)
    patch_seq = runner.project.store.patch_seq
    revision = runner.project_revision

    result = repair.execute_title_repair(
        manager,
        runner,
        visual_test._sources(),
        render_fn=renderer.render,
        probe_fn=renderer.probe,
        font_resolver=lambda: font,
    )

    assert result["production_state"] == "rendering"
    assert result["patch_applied"] is True
    assert result["old_asset_id"] == old_target["asset_id"]
    assert result["new_asset_id"] != result["old_asset_id"]
    assert len(runner.edit_calls) == edit_count + 1
    assert registry.allocate_calls == allocation_count + 1
    assert len(renderer.calls) == render_count + 1
    assert runner.project.store.patch_seq == patch_seq + 1
    assert runner.project_revision == revision + 1
    assert result["budget"]["spent_usd"] == 1.525
    assert result["budget"]["reserved_usd"] == 0.0
    assert result["veo_calls"] == 0

    after = runner.project.load()
    assert repair._audio_snapshot(after) == before_audio
    assert repair._non_target_clip_snapshot(after) == before_clips
    assert repair._non_target_shot_snapshot(after) == before_shots
    after_assets = repair._project_assets(after)
    assert all(
        after_assets[asset_id] == asset for asset_id, asset in before_assets.items()
    )
    assert set(after_assets) == set(before_assets) | {result["new_asset_id"]}
    target = repair._clip_by_id(after, repair.TARGET_CLIP_ID)
    assert target["start"] == 118.0
    assert target["duration"] == 2.0
    assert target["asset_id"] == result["new_asset_id"]
    assert target["provenance"]["source"] == "echo_protocol_title_repair"
    assert target["provenance"]["review_evidence_id"] == repair.REVIEW_EVIDENCE_ID
    assert target["provenance"]["replaces_asset_id"] == result["old_asset_id"]
    assert registry.get(result["old_asset_id"])
    assert registry.get(result["new_asset_id"])

    evidence = manager.evidence[result["evidence_id"]]
    checks = evidence["payload"]["checks"]
    assert checks["single_atomic_patch"] is True
    assert checks["target_interval_only"] is True
    assert checks["audio_clips_unchanged"] is True
    assert checks["ai_video_generation_calls"] == 0
    assert checks["human_text_integrity_review_required"] is True
    patch_file = Path(result["patch_receipt"]["path"])
    patch = json.loads(patch_file.read_text(encoding="utf-8"))["patch"]
    assert [op["op"] for op in patch["ops"]] == [
        "update_shot",
        "upsert_asset",
        "delete_clip",
        "insert_clip",
    ]


def test_patch_to_evidence_crash_replays_without_render_asset_or_second_patch(
    tmp_path: Path,
) -> None:
    manager, runner, registry, renderer, _root, font = _fixture(tmp_path)
    manager.fail_next_evidence = True

    with pytest.raises(RuntimeError, match="after project patch"):
        repair.execute_title_repair(
            manager,
            runner,
            visual_test._sources(),
            render_fn=renderer.render,
            probe_fn=renderer.probe,
            font_resolver=lambda: font,
        )
    assert manager.state == "revising"
    assert (
        repair._validate_locked_state(
            runner,
            runner.project.load(),
            visual_test.board_builder.build_board(visual_test._sources()),
        )[0]
        == "post"
    )
    counts = (
        len(renderer.calls),
        registry.allocate_calls,
        registry.register_calls,
        len(runner.edit_calls),
        runner.project.store.patch_seq,
        runner.project_revision,
    )

    result = repair.execute_title_repair(
        manager,
        runner,
        visual_test._sources(),
        render_fn=renderer.render,
        probe_fn=renderer.probe,
        font_resolver=lambda: font,
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

    replay_counts = counts + (len(manager.evidence), len(manager.transitions))
    replay = repair.execute_title_repair(
        manager,
        runner,
        visual_test._sources(),
        render_fn=renderer.render,
        probe_fn=renderer.probe,
        font_resolver=lambda: font,
    )
    assert replay["replayed"] is True
    assert (
        len(renderer.calls),
        registry.allocate_calls,
        registry.register_calls,
        len(runner.edit_calls),
        runner.project.store.patch_seq,
        runner.project_revision,
        len(manager.evidence),
        len(manager.transitions),
    ) == replay_counts


def test_repair_registry_ambiguity_fails_closed(tmp_path: Path) -> None:
    manager, runner, registry, renderer, _root, font = _fixture(tmp_path)
    result = repair.execute_title_repair(
        manager,
        runner,
        visual_test._sources(),
        render_fn=renderer.render,
        probe_fn=renderer.probe,
        font_resolver=lambda: font,
    )
    item = repair._recover_repair_item(
        runner, runner.project.load(), probe_fn=renderer.probe
    )
    original = registry.get(result["new_asset_id"])
    registry.records["v_duplicate"] = visual_test._Record(
        asset_id="v_duplicate",
        kind=original.kind,
        path=original.path,
        summary=original.summary,
        lineage=original.lineage,
        sha256=original.sha256,
        source=deepcopy(original.source),
        license=deepcopy(original.license),
    )

    with pytest.raises(repair.EchoTitleRepairError, match="multiple registry records"):
        repair.reconcile_repair_asset(registry, item)


def test_veo_or_budget_change_blocks_before_render_or_patch(tmp_path: Path) -> None:
    manager, runner, registry, renderer, _root, font = _fixture(tmp_path)
    manager.budget["veo_reserved_calls"] = 1
    before = (
        len(renderer.calls),
        registry.allocate_calls,
        len(runner.edit_calls),
        runner.project.store.patch_seq,
        runner.project_revision,
    )

    with pytest.raises(repair.EchoTitleRepairError, match="forbids Veo"):
        repair.execute_title_repair(
            manager,
            runner,
            visual_test._sources(),
            render_fn=renderer.render,
            probe_fn=renderer.probe,
            font_resolver=lambda: font,
        )
    assert (
        len(renderer.calls),
        registry.allocate_calls,
        len(runner.edit_calls),
        runner.project.store.patch_seq,
        runner.project_revision,
    ) == before


def test_direct_invocation_is_pinned_to_its_repository() -> None:
    assert Path(repair.__file__).resolve().parents[1] == repair.REPO_ROOT
    assert Path(repair.sys.path[0]).resolve() == repair.REPO_ROOT
