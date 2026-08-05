from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_current_workspace_is_served_without_account_gate() -> None:
    html = (ROOT / "static/v3/index.html").read_text(encoding="utf-8")
    script = (ROOT / "static/v3/v3.js").read_text(encoding="utf-8")
    workspace = html + script

    for visible_module in ("预览", "大纲", "后台任务", "时间线", "AI 供应商设置"):
        assert visible_module in workspace
    assert "auth-gate.js" not in html
    assert "登录后可用" not in script
    assert "Codex Subscription" not in script


def test_provider_errors_are_creator_readable_and_redacted() -> None:
    script = (ROOT / "static/v3/v3.js").read_text(encoding="utf-8")

    assert "function creatorErrorMessage" in script
    assert "AI 供应商额度不足" in script
    assert "无法连接 AI 供应商" in script
    assert 'text: `Error: ${errorReason}`' not in script


def test_local_projects_group_sessions_without_accounts(tmp_path, monkeypatch) -> None:
    from gemia import local_projects

    monkeypatch.setattr(local_projects, "PROJECTS_PATH", tmp_path / "projects.json")
    project = local_projects.create_project("Windows 体验")
    local_projects.link_session(project["project_id"], "v3-test")

    assert local_projects.project_for_session("v3-test") == project["project_id"]
    assert local_projects.update_session("v3-test", title="发送成功", pinned=True) == {
        "session_id": "v3-test",
        "title": "发送成功",
        "created_at": local_projects.session_metadata()["v3-test"]["created_at"],
        "pinned": True,
    }
    assert local_projects.remove_session("v3-test") is True
    assert local_projects.project_for_session("v3-test") is None
