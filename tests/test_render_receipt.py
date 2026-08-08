from __future__ import annotations

import json
from pathlib import Path

from gemia import project_model, render_receipt
from gemia.project_model import empty_project
from gemia.render_receipt import build_graph_identity, build_render_receipt


def _project(source: Path) -> dict:
    project = empty_project(title="receipt test")
    project["project_id"] = "receipt-test"
    project["assets"] = [
        {
            "id": "asset-1",
            "asset_id": "asset-1",
            "name": source.name,
            "media_kind": "video",
            "source_path": str(source),
            "duration": 120.0,
        }
    ]
    project["timeline"]["duration"] = 120.0
    project["timeline"]["clips"] = [
        {
            "id": "clip-1",
            "asset_id": "asset-1",
            "track_id": "V1",
            "media_kind": "video",
            "start": 0.0,
            "duration": 120.0,
            "source_in": 0.0,
            "source_out": 120.0,
            "enabled": True,
        }
    ]
    return project


def _probe(
    *,
    duration: float = 120.0,
    video_duration: float | None = None,
    audio_duration: float | None = None,
    sample_rate: int = 48000,
    channels: int = 2,
) -> dict:
    video_duration = duration if video_duration is None else video_duration
    audio_duration = duration if audio_duration is None else audio_duration
    return {
        "format": {"duration": str(duration)},
        "streams": [
            {
                "codec_type": "video",
                "codec_name": "h264",
                "pix_fmt": "yuv420p",
                "width": 1920,
                "height": 1080,
                "avg_frame_rate": "30/1",
                "duration": str(video_duration),
            },
            {
                "codec_type": "audio",
                "codec_name": "aac",
                "sample_rate": str(sample_rate),
                "channels": channels,
                "channel_layout": "stereo",
                "duration": str(audio_duration),
            },
        ],
    }


def _formal_expected() -> dict:
    return {
        "duration": 120.0,
        "duration_min": 119.5,
        "duration_max": 120.5,
        "fps": 30.0,
        "width": 1920,
        "height": 1080,
        "has_audio": True,
        "require_h264_yuv420p": True,
        "audio_sample_rate": 48000,
        "audio_channels": 2,
        "video_duration": 120.0,
        "audio_duration": 120.0,
        "integrated_loudness_lufs": -16.0,
        "loudness_tolerance_lu": 1.0,
        "true_peak_max_dbtp": -1.0,
    }


def _receipt(tmp_path: Path, **overrides) -> dict:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source-v1")
    output = tmp_path / "output.mp4"
    output.write_bytes(b"render-v1")
    values = {
        "project": _project(source),
        "project_id": "receipt-test",
        "patch_seq": 7,
        "preset": "1080p-1920x1080",
        "render_id": "render-7",
        "output_path": output,
        "probe": _probe(),
        "decode_check": {"status": "passed"},
        "expected": _formal_expected(),
        "audio_analysis": {
            "status": "passed",
            "integrated_loudness_lufs": -16.2,
            "true_peak_dbtp": -1.1,
        },
    }
    values.update(overrides)
    return build_render_receipt(**values)


def test_graph_hash_binds_exact_source_bytes(tmp_path: Path) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"one")
    project = _project(source)
    first = build_graph_identity(project)
    source.write_bytes(b"two")
    second = build_graph_identity(project)
    assert first["source_manifest_hash"] != second["source_manifest_hash"]
    assert first["graph_hash"] != second["graph_hash"]


def test_graph_hash_binds_canonical_render_semantics(
    monkeypatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"same-source")
    project = _project(source)
    first = build_graph_identity(project)

    monkeypatch.setattr(
        render_receipt,
        "CANONICAL_RENDER_SEMANTICS_VERSION",
        first["render_semantics_version"] + 1,
    )
    second = build_graph_identity(project)

    assert first["source_manifest_hash"] == second["source_manifest_hash"]
    assert first["graph_hash"] != second["graph_hash"]
    assert first["render_semantics_version"] != second["render_semantics_version"]


def test_graph_hash_is_stable_across_normalization_timestamps(
    monkeypatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"same-source")
    project = _project(source)

    monkeypatch.setattr(
        project_model,
        "_utc_now",
        lambda: "2026-07-19T10:00:00+00:00",
    )
    preview_identity = build_graph_identity(project)
    monkeypatch.setattr(
        project_model,
        "_utc_now",
        lambda: "2026-07-19T10:05:00+00:00",
    )
    export_identity = build_graph_identity(project)

    assert (
        preview_identity["source_manifest_hash"]
        == export_identity["source_manifest_hash"]
    )
    assert preview_identity["graph_hash"] == export_identity["graph_hash"]


def test_preview_export_identity_stays_bound_to_project_revision(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"same-source")
    output = tmp_path / "output.mp4"
    output.write_bytes(b"same-output")
    project = _project(source)
    common = {
        "project": project,
        "project_id": "receipt-test",
        "patch_seq": 7,
        "output_path": output,
        "probe": _probe(),
        "decode_check": {"status": "passed"},
    }

    preview = build_render_receipt(
        **common,
        preset="draft-640x360",
        render_id="preview-7",
        project_revision=7,
    )
    export = build_render_receipt(
        **common,
        preset="1080p-1920x1080",
        render_id="export-7",
        project_revision=7,
    )
    stale_revision = build_render_receipt(
        **common,
        preset="draft-640x360",
        render_id="preview-8",
        project_revision=8,
    )

    assert preview["graph_hash"] == export["graph_hash"]
    assert preview["project_revision"] == export["project_revision"] == 7
    assert stale_revision["graph_hash"] == export["graph_hash"]
    assert stale_revision["project_revision"] != export["project_revision"]


def test_machine_pass_never_means_human_accepted(tmp_path: Path) -> None:
    receipt = _receipt(tmp_path)
    assert receipt["machine_status"] == "passed"
    assert receipt["machine_blockers"] == []
    assert receipt["review_status"] == "pending"
    assert receipt["accepted"] is False
    assert "acceptance_status" not in receipt


def test_decode_not_run_is_only_provisional(tmp_path: Path) -> None:
    receipt = _receipt(tmp_path, decode_check={"status": "not_run"})
    assert receipt["machine_status"] == "provisional"
    assert receipt["accepted"] is False


def test_missing_source_and_dropped_fields_reject(tmp_path: Path) -> None:
    missing = tmp_path / "missing.mp4"
    output = tmp_path / "out.mp4"
    output.write_bytes(b"out")
    receipt = build_render_receipt(
        project=_project(missing),
        project_id="receipt-test",
        patch_seq=1,
        preset="draft",
        render_id="r1",
        output_path=output,
        probe=_probe(),
        decode_check={"status": "passed"},
        dropped_fields=[
            {"clip_id": "clip-1", "field": "mask", "reason": "not_rendered"}
        ],
        expected=_formal_expected(),
        audio_analysis={
            "integrated_loudness_lufs": -16.0,
            "true_peak_dbtp": -1.0,
        },
    )
    codes = {item["code"] for item in receipt["machine_blockers"]}
    assert receipt["machine_status"] == "rejected"
    assert {"source_unavailable", "dropped_fields"} <= codes


def test_formal_delivery_media_gates_are_machine_blockers(tmp_path: Path) -> None:
    bad_probe = _probe(duration=119.0, sample_rate=44100, channels=1)
    bad_probe["streams"][0]["codec_name"] = "hevc"
    bad_probe["streams"][0]["pix_fmt"] = "yuv444p"
    receipt = _receipt(
        tmp_path,
        probe=bad_probe,
        audio_analysis={
            "status": "passed",
            "integrated_loudness_lufs": -13.5,
            "true_peak_dbtp": -0.2,
        },
    )
    codes = {item["code"] for item in receipt["machine_blockers"]}
    assert receipt["machine_status"] == "rejected"
    assert {
        "video_codec_mismatch",
        "pixel_format_mismatch",
        "duration_below_minimum",
        "audio_sample_rate_mismatch",
        "audio_channels_mismatch",
        "audio_duration_mismatch",
        "integrated_loudness_out_of_range",
        "true_peak_too_high",
    } <= codes


def test_audio_stream_ending_before_picture_is_a_machine_blocker(
    tmp_path: Path,
) -> None:
    receipt = _receipt(
        tmp_path,
        probe=_probe(duration=120.0, audio_duration=117.4),
    )
    assert receipt["probe"]["audio_duration"] == 117.4
    assert receipt["machine_status"] == "rejected"
    assert {"audio_duration_mismatch", "audio_video_duration_mismatch"} <= {
        item["code"] for item in receipt["machine_blockers"]
    }


def test_stream_duration_falls_back_to_duration_ts_times_time_base() -> None:
    probe = _probe()
    probe["streams"][0].pop("duration")
    probe["streams"][0].update({"duration_ts": "3600", "time_base": "1/30"})
    probe["streams"][1].pop("duration")
    probe["streams"][1].update({"duration_ts": "5760000", "time_base": "1/48000"})

    summary = render_receipt.summarize_probe(probe)

    assert summary["video_duration"] == 120.0
    assert summary["audio_duration"] == 120.0
    assert summary["container_duration"] == 120.0


def test_binding_revision_updates_receipt_and_embedded_manifest(tmp_path: Path) -> None:
    receipt_path = tmp_path / "render.receipt.json"
    manifest_path = tmp_path / "render.manifest.json"
    receipt = {
        "project_revision": 1,
        "machine_status": "passed",
        "machine_blockers": [],
    }
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    manifest_path.write_text(
        json.dumps(
            {
                "project_revision": 1,
                "render_receipt": dict(receipt),
                "machine_status": "passed",
                "machine_blockers": [],
            }
        ),
        encoding="utf-8",
    )

    render_receipt.bind_render_receipt_revision(
        receipt,
        project_revision=7,
        receipt_path=receipt_path,
    )

    stored_receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    stored_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert stored_receipt["project_revision"] == 7
    assert stored_manifest["project_revision"] == 7
    assert stored_manifest["render_receipt"]["project_revision"] == 7


def test_video_stream_ending_before_audio_is_a_machine_blocker(
    tmp_path: Path,
) -> None:
    receipt = _receipt(
        tmp_path,
        probe=_probe(duration=120.0, video_duration=117.4, audio_duration=120.0),
    )

    assert receipt["machine_status"] == "rejected"
    assert {"video_duration_mismatch", "audio_video_duration_mismatch"} <= {
        item["code"] for item in receipt["machine_blockers"]
    }


def test_stream_duration_tolerance_is_inclusive_at_quarter_second(
    tmp_path: Path,
) -> None:
    boundary = _receipt(
        tmp_path,
        probe=_probe(
            duration=120.0,
            video_duration=119.75,
            audio_duration=119.75,
        ),
    )
    outside = _receipt(
        tmp_path,
        probe=_probe(
            duration=120.0,
            video_duration=119.749,
            audio_duration=119.749,
        ),
    )

    assert boundary["machine_status"] == "passed"
    assert outside["machine_status"] == "rejected"


def test_missing_stream_durations_fail_closed(tmp_path: Path) -> None:
    probe = _probe()
    probe["streams"][0].pop("duration")
    probe["streams"][1].pop("duration")

    receipt = _receipt(tmp_path, probe=probe)

    assert receipt["machine_status"] == "rejected"
    assert {"video_duration_mismatch", "audio_duration_mismatch"} <= {
        item["code"] for item in receipt["machine_blockers"]
    }


def test_output_hash_changes_with_render_bytes(tmp_path: Path) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    output = tmp_path / "out.mp4"
    output.write_bytes(b"first")
    common = {
        "project": _project(source),
        "project_id": "receipt-test",
        "patch_seq": 1,
        "preset": "draft",
        "render_id": "r1",
        "output_path": output,
        "probe": _probe(),
        "decode_check": {"status": "not_run"},
    }
    first = build_render_receipt(**common)
    output.write_bytes(b"second")
    second = build_render_receipt(**common)
    assert first["output_sha256"] != second["output_sha256"]
