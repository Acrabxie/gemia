from __future__ import annotations

from pathlib import Path

import pytest

from gemia import public_identity as accounts


def test_public_build_uses_one_local_workspace(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(accounts, "ACCOUNTS_ROOT", tmp_path / "workspaces")

    account_id = accounts.current_account_id()
    profile = accounts.current_account()
    session = accounts.auth_session_payload()

    assert account_id == accounts.LOCAL_WORKSPACE_ID
    assert profile["provider"] == "local"
    assert profile["email"] == ""
    assert session["auth_available"] is False
    assert session["mode"] == "local"
    assert session["has_google_client_id"] is False
    assert accounts.account_root(account_id).is_relative_to(tmp_path / "workspaces")


@pytest.mark.parametrize(
    "operation",
    [
        lambda: accounts.start_google_oauth(),
        lambda: accounts.finish_google_oauth("state", "code"),
        lambda: accounts.sign_in_with_google("token"),
        lambda: accounts.verify_google_id_token("token"),
        lambda: accounts.save_google_client_id("client-id"),
    ],
)
def test_public_build_rejects_private_auth_operations(operation) -> None:
    with pytest.raises(accounts.AuthError, match="not included in the public build"):
        operation()


def test_workspace_paths_reject_traversal(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(accounts, "ACCOUNTS_ROOT", tmp_path / "workspaces")

    with pytest.raises(accounts.AuthError, match="Invalid workspace id"):
        accounts.account_root("../outside")
