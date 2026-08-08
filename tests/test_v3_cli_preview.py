from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_cli_preview_reuses_video_workspace_and_attaches_existing_session() -> None:
    source = (ROOT / "static/v3/v3.js").read_text(encoding="utf-8")

    assert 'pageParams.get("mode") === "cli-preview"' in source
    assert "async function attachSession(sessionId)" in source
    assert "await refreshSessionState();" in source
    assert (
        "isCliPreview ? attachSession(cliPreviewSessionId) : "
        "restoreCurrentSessionOrCreate()"
    ) in source
    assert "if (!isCliPreview) renderProjectSidebar();" in source


def test_cli_preview_removes_chat_surfaces_and_expands_video_workspace() -> None:
    css = (ROOT / "static/v3/v3.css").read_text(encoding="utf-8")

    assert "html.cli-preview .app-main" in css
    assert "grid-template-columns: minmax(0, 1fr) 0 0" in css
    assert "html.cli-preview .project-sidebar" in css
    assert "html.cli-preview .chat-rail" in css
    assert ".history-drawer" not in css
    assert "html.cli-preview .auth-modal" in css
    assert "html.cli-preview #input-shell" in css
    assert "html.cli-preview #rail-history" in css
    assert "html.cli-preview #account-btn" in css
