from __future__ import annotations

from copy import deepcopy

from gemia.production_acceptance import evaluate_delivery
from gemia.reality_contract import normalize_reality_contract
from gemia.render_receipt import CANONICAL_RENDER_SEMANTICS_VERSION


_TEST_CONTRACT = normalize_reality_contract(
    {
        "brief": "A 120-second acceptance fixture",
        "deliverable": {
            "duration_sec": 120,
            "audio": {"required_roles": ["music", "narration", "sfx"]},
        },
        "acceptance": {
            "edit_units": {"min": 36, "max": 48},
            "median_shot_duration_max_sec": 3,
            "verified_motion_min_sec": 60,
            "licensed_public_motion_assets_min": 10,
            "static_shot_max_sec": 3,
        },
    }
)


def _fixture() -> tuple[dict, list[dict], dict, dict, dict]:
    assets: list[dict] = []
    clips: list[dict] = []
    registry: list[dict] = []
    for index in range(40):
        asset_id = f"v_{index:03d}"
        source_kind = "stock" if index < 10 else "generated_video"
        assets.append(
            {
                "id": asset_id,
                "media_kind": "video",
                "source_path": f"/work/assets/{asset_id}.mp4",
                "metadata": {},
            }
        )
        registry.append(
            {
                "asset_id": asset_id,
                "kind": "video",
                "path": f"/work/assets/{asset_id}.mp4",
                "sha256": f"hash-{index}",
                "source": {
                    "kind": source_kind,
                    "provider": "pexels" if source_kind == "stock" else "veo",
                    "url": f"https://example.test/{asset_id}",
                    "real_motion_verified": True,
                },
                "license": {"name": "test production license"},
            }
        )
        clips.append(
            {
                "id": f"clip-{index}",
                "asset_id": asset_id,
                "track_id": "V1",
                "media_kind": "video",
                "start": float(index * 3),
                "duration": 3.0,
                "source_in": 0.0,
                "source_out": 3.0,
                "enabled": True,
            }
        )
    for role, start in (("music", 0.0), ("narration", 2.0), ("sfx", 9.0)):
        asset_id = f"aud-{role}"
        assets.append(
            {
                "id": asset_id,
                "media_kind": "audio",
                "source_path": f"/work/assets/{asset_id}.wav",
                "metadata": {"role": role},
            }
        )
        registry.append(
            {
                "asset_id": asset_id,
                "kind": "audio",
                "path": f"/work/assets/{asset_id}.wav",
                "sha256": f"hash-{role}",
                "source": {"kind": "owned_audio", "provider": "local", "role": role},
                "license": {"basis": "owned"},
            }
        )
        clips.append(
            {
                "id": f"clip-{role}",
                "asset_id": asset_id,
                "track_id": "A1",
                "media_kind": "audio",
                "start": start,
                "duration": 110.0 if role == "music" else 3.0,
                "source_in": 0.0,
                "source_out": 110.0 if role == "music" else 3.0,
                "enabled": True,
            }
        )
    project = {
        "schema": "gemia.project",
        "version": 1,
        "project_id": "project-test",
        "title": "Echo",
        "timeline": {
            "fps": 30.0,
            "width": 1920,
            "height": 1080,
            "duration": 120.0,
            "tracks": [
                {"id": "V1", "kind": "video", "name": "Video", "index": 0},
                {"id": "A1", "kind": "audio", "name": "Audio", "index": 1},
            ],
            "clips": clips,
            "markers": [],
        },
        "assets": assets,
    }
    receipt = {
        "render_id": "render-1",
        "project_revision": 7,
        "graph_hash": "same-graph",
        "render_semantics_version": CANONICAL_RENDER_SEMANTICS_VERSION,
        "probe": {
            "duration": 120.0,
            "container_duration": 120.0,
            "fps": 30.0,
            "width": 1920,
            "height": 1080,
            "video_codec": "h264",
            "video_pixel_format": "yuv420p",
            "video_duration": 120.0,
            "audio_codec": "aac",
            "audio_sample_rate": 48000,
            "audio_channels": 2,
            "audio_duration": 120.0,
        },
        "audio_analysis": {"integrated_loudness_lufs": -16.1, "true_peak_dbtp": -1.2},
        "decode_check": {"status": "passed"},
        "dropped_fields": [],
    }
    preview = {
        "project_revision": 7,
        "graph_hash": "same-graph",
        "render_semantics_version": CANONICAL_RENDER_SEMANTICS_VERSION,
    }
    budget = {"committed_usd": 9.925, "duplicate_billing_count": 0}
    evidence = {
        "review_checks": {
            "black_frames": True,
            "watermarks": True,
            "text_integrity": True,
            "character_continuity": True,
            "real_motion": True,
        }
    }
    return project, registry, receipt, preview, {"budget": budget, "evidence": evidence}


def test_formal_delivery_can_reach_ready_for_review() -> None:
    project, registry, receipt, preview, context = _fixture()
    report = evaluate_delivery(
        project=project,
        render_receipt=receipt,
        asset_records=registry,
        budget_snapshot=context["budget"],
        evidence=context["evidence"],
        preview_receipt=preview,
        reality_contract=_TEST_CONTRACT,
    )
    assert report["ready_for_review"] is True
    assert report["human_review_required"] is True
    assert report["blockers"] == []


def test_static_long_shot_and_insufficient_motion_are_blockers() -> None:
    project, registry, receipt, preview, context = _fixture()
    for clip in project["timeline"]["clips"]:
        if clip.get("track_id") == "V1":
            clip["media_kind"] = "image"
            clip["duration"] = 4.0
            break
    report = evaluate_delivery(
        project=project,
        render_receipt=receipt,
        asset_records=registry,
        budget_snapshot=context["budget"],
        evidence=context["evidence"],
        preview_receipt=preview,
        reality_contract=_TEST_CONTRACT,
    )
    codes = {item["code"] for item in report["blockers"]}
    assert "static_shot_limit" in codes


def test_missing_provenance_and_tmp_reference_are_blockers() -> None:
    project, registry, receipt, preview, context = _fixture()
    registry[0] = {**registry[0], "path": "/tmp/stock.mp4", "license": {}}
    report = evaluate_delivery(
        project=project,
        render_receipt=receipt,
        asset_records=registry,
        budget_snapshot=context["budget"],
        evidence=context["evidence"],
        preview_receipt=preview,
        reality_contract=_TEST_CONTRACT,
    )
    codes = {item["code"] for item in report["blockers"]}
    assert {"asset_provenance_complete", "no_tmp_references"}.issubset(codes)


def test_machine_success_never_means_human_accepted() -> None:
    project, registry, receipt, preview, context = _fixture()
    report = evaluate_delivery(
        project=project,
        render_receipt=receipt,
        asset_records=registry,
        budget_snapshot=context["budget"],
        evidence=context["evidence"],
        preview_receipt=preview,
        reality_contract=_TEST_CONTRACT,
    )
    assert report["ready_for_review"] is True
    assert "accepted" not in report


def test_audio_stream_must_cover_the_full_picture_duration() -> None:
    project, registry, receipt, preview, context = _fixture()
    receipt["probe"]["audio_duration"] = 117.4
    report = evaluate_delivery(
        project=project,
        render_receipt=receipt,
        asset_records=registry,
        budget_snapshot=context["budget"],
        evidence=context["evidence"],
        preview_receipt=preview,
        reality_contract=_TEST_CONTRACT,
    )
    assert "audio_duration_matches_picture" in {
        item["code"] for item in report["blockers"]
    }


def test_preview_export_revision_or_graph_mismatch_blocks_delivery() -> None:
    project, registry, receipt, preview, context = _fixture()
    stale = deepcopy(preview)
    stale["project_revision"] = 6
    report = evaluate_delivery(
        project=project,
        render_receipt=receipt,
        asset_records=registry,
        budget_snapshot=context["budget"],
        evidence=context["evidence"],
        preview_receipt=stale,
        reality_contract=_TEST_CONTRACT,
    )
    assert "preview_export_graph_parity" in {
        item["code"] for item in report["blockers"]
    }


def test_matching_legacy_receipts_cannot_pass_current_delivery_gate() -> None:
    project, registry, receipt, preview, context = _fixture()
    receipt["render_semantics_version"] = 2
    preview["render_semantics_version"] = 2

    report = evaluate_delivery(
        project=project,
        render_receipt=receipt,
        asset_records=registry,
        budget_snapshot=context["budget"],
        evidence=context["evidence"],
        preview_receipt=preview,
        reality_contract=_TEST_CONTRACT,
    )

    assert not report["ready_for_review"]
    assert {
        "current_export_render_semantics",
        "current_preview_render_semantics",
    } <= {item["code"] for item in report["blockers"]}
