from __future__ import annotations

import pytest
import subprocess
from pathlib import Path

from gemia import accounts, media_library
from gemia.media_library import (
    LEGACY_PROJECT_SCOPE,
    MediaLibraryError,
    copy_asset_to_project,
    get_asset,
    import_media,
    library_path,
    list_assets,
    resolve_asset_file,
    soft_delete_asset,
    upload_response_for_asset,
)
from gemia.project_model import IMAGE_DURATION


def _patch_account_roots(monkeypatch, tmp_path: Path) -> None:
    root = tmp_path / "accounts"
    monkeypatch.setattr(accounts, "ACCOUNTS_ROOT", root)
    monkeypatch.setattr(accounts, "ACTIVE_ACCOUNT_PATH", root / "active.json")
    monkeypatch.setattr(accounts, "CONFIG_PATH", tmp_path / "config.json")


def _make_video(path: Path) -> Path:
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=duration=0.6:size=96x54:rate=12",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-an",
            str(path),
        ],
        capture_output=True,
        check=True,
    )
    return path


def _make_image(path: Path) -> Path:
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=skyblue:s=96x54:d=0.1",
            "-frames:v",
            "1",
            str(path),
        ],
        capture_output=True,
        check=True,
    )
    return path


def _make_audio(path: Path) -> Path:
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=0.6",
            str(path),
        ],
        capture_output=True,
        check=True,
    )
    return path


def test_imports_video_image_and_audio_into_account_sqlite(monkeypatch, tmp_path: Path) -> None:
    _patch_account_roots(monkeypatch, tmp_path)
    account_id = "google_account_one"

    video = import_media(account_id, _make_video(tmp_path / "clip.mp4"))
    image = import_media(account_id, _make_image(tmp_path / "still.png"))
    audio = import_media(account_id, _make_audio(tmp_path / "tone.wav"))

    assert library_path(account_id).exists()
    assert video["media_kind"] == "video"
    assert image["media_kind"] == "image"
    assert image["duration"] == IMAGE_DURATION
    assert audio["media_kind"] == "audio"
    assert audio["waveform_peaks"]
    assert len(list_assets(account_id)) == 3
    assert Path(str(video["source_path"])).is_file()
    assert str(video["preview_src"]).startswith("/media-library/file/")


def test_import_dedupes_by_fingerprint_and_restores_soft_deleted(monkeypatch, tmp_path: Path) -> None:
    _patch_account_roots(monkeypatch, tmp_path)
    account_id = "google_account_one"
    image_path = _make_image(tmp_path / "duplicate.png")

    first = import_media(account_id, image_path)
    second = import_media(account_id, image_path)

    assert first["asset_id"] == second["asset_id"]
    assert len(list_assets(account_id)) == 1

    deleted = soft_delete_asset(account_id, first["asset_id"])
    assert deleted["deleted_at"]
    assert list_assets(account_id) == []
    assert get_asset(account_id, first["asset_id"]) is None

    restored = import_media(account_id, image_path)
    assert restored["asset_id"] == first["asset_id"]
    assert len(list_assets(account_id)) == 1


def test_media_library_isolated_by_account(monkeypatch, tmp_path: Path) -> None:
    _patch_account_roots(monkeypatch, tmp_path)
    source = _make_image(tmp_path / "shared.png")

    one = import_media("google_account_one", source)
    two = import_media("google_account_two", source)

    assert one["asset_id"] == two["asset_id"]
    assert library_path("google_account_one") != library_path("google_account_two")
    assert len(list_assets("google_account_one")) == 1
    assert len(list_assets("google_account_two")) == 1
    assert Path(str(one["source_path"])).parent != Path(str(two["source_path"])).parent


def test_media_library_isolated_by_project_and_copy_is_explicit(monkeypatch, tmp_path: Path) -> None:
    _patch_account_roots(monkeypatch, tmp_path)
    account_id = "google_account_one"
    source = _make_image(tmp_path / "shared.png")

    project_one = import_media(account_id, source, project_id="project-one")

    assert project_one["project_id"] == "project-one"
    assert [item["asset_id"] for item in list_assets(account_id, project_id="project-one")] == [project_one["asset_id"]]
    assert list_assets(account_id, project_id="project-two") == []
    assert get_asset(account_id, project_one["asset_id"], project_id="project-two") is None
    with pytest.raises(MediaLibraryError, match="media asset not found"):
        resolve_asset_file(
            account_id,
            project_one["asset_id"],
            "original",
            project_id="project-two",
        )

    project_two = copy_asset_to_project(
        account_id,
        project_one["asset_id"],
        source_project_id="project-one",
        target_project_id="project-two",
    )
    assert project_two["project_id"] == "project-two"
    assert project_two["asset_id"] != project_one["asset_id"]
    assert project_two["fingerprint"] == project_one["fingerprint"]
    assert [item["asset_id"] for item in list_assets(account_id, project_id="project-two")] == [project_two["asset_id"]]


def test_legacy_account_rows_stay_outside_real_projects(monkeypatch, tmp_path: Path) -> None:
    _patch_account_roots(monkeypatch, tmp_path)
    account_id = "google_account_one"
    legacy = import_media(account_id, _make_image(tmp_path / "legacy.png"))

    assert legacy["project_id"] == LEGACY_PROJECT_SCOPE
    assert len(list_assets(account_id)) == 1
    assert list_assets(account_id, project_id="project-new") == []


def test_upload_response_keeps_legacy_shape_with_asset_and_clip(monkeypatch, tmp_path: Path) -> None:
    _patch_account_roots(monkeypatch, tmp_path)
    asset = import_media("google_account_one", _make_image(tmp_path / "still.png"))

    payload = upload_response_for_asset(asset)

    assert payload["asset"]["asset_id"] == asset["asset_id"]
    assert payload["clip"]["assetId"] == asset["asset_id"]
    assert payload["path"] == asset["source_path"]
    assert payload["duration"] == IMAGE_DURATION
    assert payload["thumbnails"]


def test_asset_id_validation(monkeypatch, tmp_path: Path) -> None:
    _patch_account_roots(monkeypatch, tmp_path)
    account_id = "test_account"
    import_media(account_id, _make_image(tmp_path / "test.png"))

    invalid_ids = [
        "invalid",
        "asset_123",
        "asset_12345678901234567890123G",
        "asset_12345678901234567890123",
    ]

    for invalid_id in invalid_ids:
        with pytest.raises(MediaLibraryError, match="invalid media asset id"):
            get_asset(account_id, invalid_id)
        with pytest.raises(MediaLibraryError, match="invalid media asset id"):
            soft_delete_asset(account_id, invalid_id)


def test_resolve_asset_file_cache_traversal(monkeypatch, tmp_path: Path) -> None:
    _patch_account_roots(monkeypatch, tmp_path)
    account_id = "test_account"
    asset = import_media(account_id, _make_image(tmp_path / "test.png"))
    asset_id = asset["asset_id"]

    # Test valid cache file resolution
    valid_path = resolve_asset_file(account_id, asset_id, "cache", "thumbnail.png")
    assert valid_path.name == "thumbnail.png"
    assert "cache" in str(valid_path)

    # Test cache traversal attempts
    invalid_filenames = [
        "../foo.png",
        "/etc/passwd",
        "../../foo.png",
        "cache/foo.png",
        "cache/../foo.png",
    ]
    for filename in invalid_filenames:
        with pytest.raises(MediaLibraryError, match="invalid media cache path"):
            resolve_asset_file(account_id, asset_id, "cache", filename)


def test_media_library_connections_close_after_context(monkeypatch, tmp_path: Path) -> None:
    _patch_account_roots(monkeypatch, tmp_path)

    with media_library._connect("test_account") as conn:
        conn.execute("SELECT 1").fetchone()

    with pytest.raises(Exception, match="closed"):
        conn.execute("SELECT 1")
