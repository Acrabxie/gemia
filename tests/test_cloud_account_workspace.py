from pathlib import Path

from gemia import accounts


def test_cloud_accounts_share_existing_machine_workspace(monkeypatch, tmp_path: Path):
    root = tmp_path / "accounts"
    monkeypatch.setattr(accounts, "ACCOUNTS_ROOT", root)
    monkeypatch.setattr(accounts, "ACTIVE_ACCOUNT_PATH", root / "active.json")

    legacy_id = "google_existing_workspace"
    (root / legacy_id).mkdir(parents=True)
    accounts._atomic_write_json(root / legacy_id / "profile.json", {"account_id": legacy_id})
    accounts._atomic_write_json(root / "active.json", {"account_id": legacy_id})
    marker = root / legacy_id / "media" / "existing-project.marker"
    marker.parent.mkdir(parents=True)
    marker.write_text("preserve", encoding="utf-8")

    first = accounts.activate_cloud_account(
        {"id": "cloud-one", "email": "one@example.com", "display_name": "One"}
    )
    second = accounts.activate_cloud_account(
        {"id": "cloud-two", "email": "two@example.com", "display_name": "Two"}
    )

    assert first["account_id"] == legacy_id
    assert second["account_id"] == legacy_id
    assert second["cloud_account_id"] == "cloud-two"
    assert accounts.account_root(second["account_id"]) == root / legacy_id
    assert marker.read_text(encoding="utf-8") == "preserve"
