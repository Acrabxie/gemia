from __future__ import annotations

from pathlib import Path

import pytest

from gemia import local_workspace


def test_public_build_uses_one_local_workspace(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(local_workspace, "WORKSPACES_ROOT", tmp_path / "workspaces")

    workspace_id = local_workspace.current_workspace_id()

    assert workspace_id == local_workspace.LOCAL_WORKSPACE_ID
    assert local_workspace.workspace_root(workspace_id).is_relative_to(
        tmp_path / "workspaces"
    )
    assert local_workspace.workspace_memory_root(workspace_id).is_relative_to(
        tmp_path / "workspaces"
    )
    assert local_workspace.workspace_session_root(workspace_id).is_relative_to(
        tmp_path / "workspaces"
    )


def test_workspace_paths_reject_traversal(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(local_workspace, "WORKSPACES_ROOT", tmp_path / "workspaces")

    with pytest.raises(local_workspace.WorkspaceError, match="Invalid workspace id"):
        local_workspace.workspace_root("../outside")


def test_public_workspace_has_no_account_operations() -> None:
    forbidden = {
        "auth_session_payload",
        "finish_google_oauth",
        "list_accounts",
        "sign_in_with_google",
        "sign_out",
        "start_google_oauth",
        "switch_account",
    }

    assert forbidden.isdisjoint(dir(local_workspace))


def test_public_server_has_no_account_routes_or_header() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    source = "\n".join(
        (repo_root / rel).read_text(encoding="utf-8")
        for rel in ("server.py", "gemia/v3_routes.py")
    )

    assert "/auth" not in source
    assert "/accounts" not in source
    assert "X-Lumeri-Account" not in source
