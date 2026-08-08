from __future__ import annotations

from pathlib import Path

from gemia import accounts
from gemia.media_annotations import create_annotation
from gemia.media_library import (
    MediaLibraryError,
    copy_asset_to_project,
    get_asset,
    import_media,
    list_assets,
    resolve_asset_file,
)
from gemia.media_search import search_media_annotations


def _patch_account_roots(monkeypatch, tmp_path: Path) -> None:
    root = tmp_path / "accounts"
    monkeypatch.setattr(accounts, "ACCOUNTS_ROOT", root)
    monkeypatch.setattr(accounts, "ACTIVE_ACCOUNT_PATH", root / "active.json")
    monkeypatch.setattr(accounts, "CONFIG_PATH", tmp_path / "config.json")


def _fake_png(path: Path) -> Path:
    path.write_bytes(b"not-a-real-png-but-safe-for-storage-tests")
    return path


def test_project_scopes_cover_list_access_search_and_explicit_copy(monkeypatch, tmp_path: Path) -> None:
    _patch_account_roots(monkeypatch, tmp_path)
    account_id = "google_project_isolation"
    source = _fake_png(tmp_path / "shared.png")

    project_one = import_media(account_id, source, project_id="project-one")
    create_annotation(
        account_id,
        project_one["asset_id"],
        {"label": "private-one", "source": "user"},
        project_id="project-one",
    )

    assert [item["asset_id"] for item in list_assets(account_id, project_id="project-one")] == [project_one["asset_id"]]
    assert list_assets(account_id, project_id="project-two") == []
    assert get_asset(account_id, project_one["asset_id"], project_id="project-two") is None
    try:
        resolve_asset_file(
            account_id,
            project_one["asset_id"],
            "original",
            project_id="project-two",
        )
    except MediaLibraryError as exc:
        assert str(exc) == "media asset not found"
    else:
        raise AssertionError("cross-Project file access unexpectedly succeeded")
    assert search_media_annotations(
        account_id,
        "private-one",
        project_id="project-one",
    )["result_count"] == 1
    assert search_media_annotations(
        account_id,
        "private-one",
        project_id="project-two",
    )["result_count"] == 0
    project_two = copy_asset_to_project(
        account_id,
        project_one["asset_id"],
        source_project_id="project-one",
        target_project_id="project-two",
    )
    assert project_two["asset_id"] != project_one["asset_id"]
    assert project_two["project_id"] == "project-two"
    # Copying creates a target record, but annotations remain isolated too.
    assert search_media_annotations(
        account_id,
        "private-one",
        project_id="project-two",
    )["result_count"] == 0
