from __future__ import annotations

import json
import subprocess
from pathlib import Path

from gemia import accounts
from gemia.media_annotations import list_annotations
from gemia.media_library import import_media
from gemia import roughcut as RC


def _patch_account_roots(monkeypatch, tmp_path: Path) -> None:
    root = tmp_path / "accounts"
    monkeypatch.setattr(accounts, "ACCOUNTS_ROOT", root)
    monkeypatch.setattr(accounts, "ACTIVE_ACCOUNT_PATH", root / "active.json")
    monkeypatch.setattr(accounts, "CONFIG_PATH", tmp_path / "config.json")


def _make_audio(path: Path) -> Path:
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=2", str(path),
        ],
        check=True,
    )
    return path


def _whisper_payload() -> dict:
    return {
        "result": {"language": "en"},
        "transcription": [
            {
                "offsets": {"from": 0, "to": 1800},
                "text": "Hello, um, this is the keeper take.",
                "tokens": [
                    {"text": " Hello", "offsets": {"from": 0, "to": 300}, "p": 0.95},
                    {"text": " um", "offsets": {"from": 350, "to": 520}, "p": 0.62},
                    {"text": " keeper", "offsets": {"from": 700, "to": 1100}, "p": 0.91},
                ],
            }
        ],
    }


def test_parse_whisper_and_cleanup_suggestions_are_timecoded() -> None:
    transcript = RC._parse_whisper(_whisper_payload())
    suggestions = RC._cleanup_suggestions(
        transcript,
        [{"start_sec": 1.2, "end_sec": 2.2, "duration_sec": 1.0}],
    )
    assert transcript["text"] == "Hello, um, this is the keeper take."
    assert transcript["segments"][0]["start_sec"] == 0.0
    assert {item["kind"] for item in suggestions} == {"filler", "pause"}
    assert all(item["end_sec"] > item["start_sec"] for item in suggestions)
    assert all(item["review_status"] == "pending" for item in suggestions)


def test_take_score_penalizes_filler_heavy_delivery() -> None:
    asset = {"status": "ready", "has_audio": True}
    transcript = {"confidence": 0.9}
    clean = RC._take_score(asset, transcript, [], [], 5.0)
    filler_heavy = RC._take_score(
        asset,
        transcript,
        [],
        [{"kind": "filler"}, {"kind": "filler"}],
        5.0,
    )
    assert round(clean - filler_heavy, 4) == 0.2


def test_prepare_resume_and_human_review_persist(monkeypatch, tmp_path: Path) -> None:
    _patch_account_roots(monkeypatch, tmp_path)
    account_id = "roughcut-account"
    asset = import_media(account_id, _make_audio(tmp_path / "scene_take1.wav"))
    model = tmp_path / "ggml.bin"
    model.write_bytes(b"model")
    monkeypatch.setattr(RC, "_whisper_model", lambda: model)
    monkeypatch.setattr(RC, "_whisper_binary", lambda: "/mock/whisper-cli")
    monkeypatch.setattr(
        RC,
        "_detect_silences",
        lambda source, duration: [{"start_sec": 1.2, "end_sec": 2.0, "duration_sec": 0.8}],
    )

    calls: list[str] = []

    def fake_run(command: list[str], label: str, *, timeout: int = 1800) -> None:
        calls.append(label)
        if label == "audio extraction":
            Path(command[-1]).write_bytes(b"wav")
        elif label == "speech transcription":
            output_base = Path(command[command.index("-of") + 1])
            output_base.with_suffix(".json").write_text(json.dumps(_whisper_payload()), encoding="utf-8")

    monkeypatch.setattr(RC, "_run", fake_run)
    first = RC.prepare_roughcut(account_id, [asset["asset_id"]], create_proxies=False)
    assert first["status"] == "ready"
    assert first["results"][0]["cleanup_suggestions"] == 2
    assert calls == ["audio extraction", "speech transcription"]

    second = RC.prepare_roughcut(account_id, [asset["asset_id"]], create_proxies=False)
    assert second["results"][0]["reused"] is True
    assert calls == ["audio extraction", "speech transcription"]

    manifest = RC.load_roughcut(account_id, asset["asset_id"])
    filler = next(item for item in manifest["cleanup_suggestions"] if item["kind"] == "filler")
    reviewed = RC.apply_roughcut_review(
        account_id,
        asset["asset_id"],
        {"target_type": "cleanup", "target_id": filler["id"], "action": "accept"},
    )
    updated = next(item for item in reviewed["manifest"]["cleanup_suggestions"] if item["id"] == filler["id"])
    assert updated["review_status"] == "accepted"
    annotations = list_annotations(account_id, asset["asset_id"])
    assert any(item["source"] == "roughcut" and item["category"] == "transcript" for item in annotations)
    assert any(item["source"] == "user" and item["category"] == "roughcut_review" for item in annotations)


def test_orphaned_background_job_becomes_resumable(monkeypatch, tmp_path: Path) -> None:
    _patch_account_roots(monkeypatch, tmp_path)
    account_id = "job-account"
    record = {
        "job_id": "roughcut_0123456789ab",
        "status": "running",
        "args": {"asset_ids": ["asset_x"]},
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
    }
    RC._write_job(account_id, record)
    with RC._ACTIVE_JOBS_LOCK:
        RC._ACTIVE_JOBS.clear()
    recovered = RC.get_prepare_job(account_id, record["job_id"])
    assert recovered["status"] == "interrupted"
    assert recovered["recoverable"] is True
