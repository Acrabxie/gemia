from __future__ import annotations

import hashlib
import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from gemia.compat import ffmpeg_path
from gemia.tools._context import AssetRecord
from scripts import produce_echo_protocol_v1 as operator


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class _Registry:
    def __init__(self, records: list[AssetRecord]) -> None:
        self.records = {record.asset_id: record for record in records}

    def list_records(self) -> list[AssetRecord]:
        return list(self.records.values())

    def get(self, asset_id: str) -> AssetRecord:
        return self.records[asset_id]

    def update_record(
        self,
        asset_id: str,
        *,
        source_patch=None,
        license_patch=None,
        summary=None,
    ) -> AssetRecord:
        record = self.records[asset_id]
        source = dict(record.source)
        source.update(source_patch or {})
        license_info = dict(record.license)
        license_info.update(license_patch or {})
        updated = replace(
            record,
            source=source,
            license=license_info,
            summary=record.summary if summary is None else str(summary),
        )
        self.records[asset_id] = updated
        return updated


class _Manager:
    def __init__(self) -> None:
        self.state = "sourcing"
        self.transitions: list[str] = []
        self.evidence_ids: list[str] = []
        self.closed = 0

    def get_run(self, project_id: str, run_id: str):
        del project_id, run_id
        return {
            "production_state": self.state,
            "state": self.state,
            "budget": {
                "spent_usd": 1.525,
                "reserved_usd": 0.0,
                "remaining_usd": 13.475,
                "duplicate_billing_count": 0,
            },
        }

    def transition_run(self, project_id: str, run_id: str, state: str, **kwargs):
        del project_id, run_id, kwargs
        self.transitions.append(state)
        self.state = state
        return {"state": state}

    def record_evidence(self, project_id: str, run_id: str, **kwargs):
        del project_id, run_id
        evidence_id = str(kwargs["evidence_id"])
        if evidence_id not in self.evidence_ids:
            self.evidence_ids.append(evidence_id)
        return {"evidence_id": evidence_id}

    def close_all(self) -> None:
        self.closed += 1


def _source_record(tmp_path: Path, index: int) -> AssetRecord:
    slot = f"s{index:02d}"
    path = tmp_path / f"source-{slot}.mp4"
    path.write_bytes(f"source-video-{slot}".encode())
    return AssetRecord(
        asset_id=f"v_{index:03d}",
        kind="video",
        path=path,
        summary=f"stock {slot}",
        created_at="2026-07-20T00:00:00+00:00",
        sha256=_sha(path),
        source={
            "kind": "public_stock",
            "provider": "pexels" if index % 2 else "pixabay",
            "provider_asset_id": f"provider-{index}",
            "url": f"https://example.test/source/{index}",
            "production_slot": slot,
            "production_source_status": "pending_review",
        },
        license={
            "name": "Test Stock License",
            "url": "https://example.test/license",
            "attribution": f"creator-{index}",
        },
    )


def _runner(tmp_path: Path):
    records = [_source_record(tmp_path, index) for index in range(1, 11)]
    registry = _Registry(records)
    return SimpleNamespace(
        project_id="project-echo",
        run_id=operator.RUN_ID,
        project_revision=7,
        output_dir=tmp_path / "workdir",
        agent=SimpleNamespace(registry=registry),
    )


def _fake_audit(calls: list[str]):
    def audit(runner, record, *, slot, query, previous=None, **kwargs):
        del kwargs
        calls.append(slot)
        actual_sha = _sha(Path(record.path))
        sheet_path = (
            operator._source_review_dir(runner)
            / "contact-sheets"
            / f"{slot}-{record.asset_id}-{actual_sha[:16]}.jpg"
        )
        sheet_path.parent.mkdir(parents=True, exist_ok=True)
        sheet_path.write_bytes(f"six-frame-sheet-{slot}".encode())
        fingerprint = operator._stable_digest(
            {"slot": slot, "asset_id": record.asset_id, "sha256": actual_sha}
        )
        prior_review = {}
        if (
            isinstance(previous, dict)
            and previous.get("fingerprint") == fingerprint
            and isinstance(previous.get("review"), dict)
        ):
            prior_review = dict(previous["review"])
        return {
            "slot": slot,
            "query": query,
            "asset_id": record.asset_id,
            "path": str(record.path),
            "sha256": actual_sha,
            "provider": record.source["provider"],
            "provider_asset_id": record.source["provider_asset_id"],
            "source_url": record.source["url"],
            "license": dict(record.license),
            "probe": {
                "duration_sec": 12.0,
                "width": 1920,
                "height": 1080,
                "video_codec": "h264",
                "has_video": True,
                "motion_evidence": {"real_motion_verified": True},
            },
            "contact_sheet": {
                "path": str(sheet_path),
                "sha256": _sha(sheet_path),
                "width": 1440,
                "height": 600,
                "frame_count": 6,
                "sample_times_sec": [0.6, 2.8, 4.9, 7.1, 9.2, 11.4],
            },
            "fingerprint": fingerprint,
            "machine_gate": "passed",
            "review": prior_review,
            "audited_at": "2026-07-20T00:00:00+00:00",
        }

    return audit


def test_candidate_gate_requires_landscape_1080p() -> None:
    base = {
        "provider": "pexels",
        "id": "one",
        "duration": 8,
        "download_url": "https://example.test/video.mp4",
        "license": "Pexels License",
    }
    assert operator._candidate_ok({**base, "width": 1920, "height": 1080}, set())
    assert not operator._candidate_ok({**base, "width": 1280, "height": 720}, set())
    assert not operator._candidate_ok({**base, "width": 1080, "height": 1920}, set())


@pytest.mark.parametrize(
    ("source_patch", "license_patch", "message"),
    [
        ({"url": ""}, {}, "source URL"),
        ({"provider_asset_id": ""}, {}, "provider_asset_id"),
        ({}, {"name": ""}, r"license name\+URL"),
        ({}, {"url": "not-a-url"}, r"license name\+URL"),
    ],
)
def test_machine_gate_rejects_incomplete_provenance(
    tmp_path: Path, monkeypatch, source_patch, license_patch, message
) -> None:
    runner = _runner(tmp_path)
    record = runner.agent.registry.get("v_001")
    record.source.update(source_patch)
    record.license.update(license_patch)
    monkeypatch.setattr(
        operator,
        "_probe_physical_source",
        lambda path: pytest.fail(f"probe should not run for bad provenance: {path}"),
    )
    with pytest.raises(operator.SourceGateError, match=message):
        operator._audit_source_record(
            runner,
            record,
            slot="s01",
            query="rainy city",
        )


def test_reused_asset_recomputes_hash_and_rejects_changed_bytes(
    tmp_path: Path, monkeypatch
) -> None:
    runner = _runner(tmp_path)
    record = runner.agent.registry.get("v_001")
    Path(record.path).write_bytes(b"tampered-after-registration")
    monkeypatch.setattr(
        operator,
        "_probe_physical_source",
        lambda path: pytest.fail(f"probe must not run after hash mismatch: {path}"),
    )
    with pytest.raises(operator.SourceGateError, match="bytes changed"):
        operator._audit_source_record(
            runner,
            record,
            slot="s01",
            query="rainy city",
            previous={"review": {"decision": "approve"}},
        )


def test_machine_gate_rejects_720p_even_when_motion_passes(
    tmp_path: Path, monkeypatch
) -> None:
    runner = _runner(tmp_path)
    record = runner.agent.registry.get("v_001")
    monkeypatch.setattr(
        operator,
        "_probe_physical_source",
        lambda path: {
            "duration_sec": 12.0,
            "width": 1280,
            "height": 720,
            "video_codec": "h264",
            "has_video": True,
            "motion_evidence": {"real_motion_verified": True},
        },
    )
    monkeypatch.setattr(
        operator,
        "_write_contact_sheet",
        lambda *args, **kwargs: pytest.fail("low-resolution media needs no sheet"),
    )
    with pytest.raises(operator.SourceGateError, match="landscape 1080p"):
        operator._audit_source_record(
            runner,
            record,
            slot="s01",
            query="rainy city",
        )


def test_source_reaudits_every_reused_slot_and_stays_in_sourcing(
    tmp_path: Path, monkeypatch
) -> None:
    manager = _Manager()
    runner = _runner(tmp_path)
    audit_calls: list[str] = []
    monkeypatch.setattr(operator, "_open", lambda output_root: (manager, runner))
    monkeypatch.setattr(operator, "_audit_source_record", _fake_audit(audit_calls))
    monkeypatch.setattr(
        operator,
        "_call",
        lambda *args, **kwargs: pytest.fail("reused sources must not touch the network"),
    )

    result = operator.source(tmp_path)

    assert audit_calls == [f"s{index:02d}" for index in range(1, 11)]
    assert result["production_state"] == "sourcing"
    assert result["visual_review_required"] is True
    assert result["visual_review_status"] == "pending_review"
    assert manager.transitions == []
    manifest = operator._load_source_manifest(runner, required=True)
    assert set(manifest["slots"]) == {f"s{index:02d}" for index in range(1, 11)}
    assert all(Path(path).is_file() for path in result["contact_sheets"])


def test_review_requires_all_slots_then_transitions_once_and_replays(
    tmp_path: Path, monkeypatch
) -> None:
    manager = _Manager()
    runner = _runner(tmp_path)
    audit_calls: list[str] = []
    fake_audit = _fake_audit(audit_calls)
    monkeypatch.setattr(operator, "_open", lambda output_root: (manager, runner))
    monkeypatch.setattr(operator, "_audit_source_record", fake_audit)
    monkeypatch.setattr(
        operator,
        "_call",
        lambda *args, **kwargs: pytest.fail("review test must stay offline"),
    )
    operator.source(tmp_path)

    for index in range(1, 10):
        result = operator.review_sources(
            tmp_path,
            slot=f"s{index:02d}",
            decision="approve",
            reviewer="Acrab",
            note=f"slot {index} content, continuity and watermark check passed",
        )
        assert result["production_state"] == "sourcing"
        assert manager.transitions == []

    final = operator.review_sources(
        tmp_path,
        slot="s10",
        decision="approve",
        reviewer="Acrab",
        note="slot 10 content, continuity and watermark check passed",
    )
    assert final["production_state"] == "rough_cut"
    assert final["visual_review_status"] == "passed"
    assert final["approved_count"] == 10
    assert manager.transitions == ["rough_cut"]

    replay = operator.review_sources(
        tmp_path,
        slot="s10",
        decision="approve",
        reviewer="Acrab",
        note="slot 10 content, continuity and watermark check passed",
    )
    assert replay["replayed"] is True
    assert replay["production_state"] == "rough_cut"
    assert manager.transitions == ["rough_cut"]


def test_reject_stays_in_sourcing_and_marks_asset_for_replacement(
    tmp_path: Path, monkeypatch
) -> None:
    manager = _Manager()
    runner = _runner(tmp_path)
    monkeypatch.setattr(operator, "_open", lambda output_root: (manager, runner))
    monkeypatch.setattr(operator, "_audit_source_record", _fake_audit([]))
    monkeypatch.setattr(
        operator,
        "_call",
        lambda *args, **kwargs: pytest.fail("review test must stay offline"),
    )
    operator.source(tmp_path)

    result = operator.review_sources(
        tmp_path,
        slot="s01",
        decision="reject",
        reviewer="Acrab",
        note="visible watermark and the shot does not match the rainy-city brief",
    )
    assert result["production_state"] == "sourcing"
    assert result["visual_review_status"] == "changes_requested"
    assert manager.transitions == []
    assert operator._existing_slot(runner, "s01") is None
    assert (
        runner.agent.registry.get("v_001").source["production_source_status"]
        == "rejected"
    )


def test_contact_sheet_contains_six_persistent_frames(tmp_path: Path) -> None:
    video = tmp_path / "motion-1080p.mp4"
    subprocess.run(
        [
            ffmpeg_path(),
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=s=1920x1080:r=2",
            "-t",
            "6",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            str(video),
        ],
        check=True,
        capture_output=True,
    )
    destination = tmp_path / "persistent" / "contact-sheet.jpg"
    result = operator._write_contact_sheet(
        video,
        destination,
        slot="s01",
        asset_id="v_001",
        duration_sec=6.0,
    )
    assert destination.is_file()
    assert result["frame_count"] == 6
    assert result["sha256"] == _sha(destination)
    with Image.open(destination) as image:
        assert image.size == (1440, 600)
