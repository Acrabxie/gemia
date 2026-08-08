from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_skill_space_is_an_addable_workspace_module() -> None:
    html = (ROOT / "static/v3/index.html").read_text(encoding="utf-8")
    source = (ROOT / "static/v3/v3.js").read_text(encoding="utf-8")
    css = (ROOT / "static/v3/v3.css").read_text(encoding="utf-8")

    assert 'label: "Skill Space"' in source
    assert 'skills: VIDEO_STAGE_VIEWS.skills' in source
    assert 'else if (view === "skills") renderSkillSpacePanel(body);' in source
    assert '["preview", "outline", "tasks", "timeline", "properties", "files", "library", "skills"]' in source
    assert 'id="skill-space-selection"' in html
    assert ".skill-space-card.selected" in css
    assert ".input-shell.has-skill-space" in css


def test_skill_space_selection_is_session_scoped_and_exact() -> None:
    source = (ROOT / "static/v3/v3.js").read_text(encoding="utf-8")

    assert "const skillSpaceSelections = new Map();" in source
    assert "skillSpaceSelections.get(state.sessionId)" in source
    assert 'apiFetch("/skill-cloud/artifacts")' in source
    assert "content_sha256=${item.content_sha256}" in source
    assert "Load each exact selection with load_cloud_guide" in source
    assert "skillSpace: normalizeSkillSpaceRefs(turn.skillSpace)" in source
    assert "composerAgentMessage(turn.userMessage, turn.skillSpace, turn.workspaceContext)" in source


def test_skill_space_keeps_creator_message_clean_but_shows_selected_titles() -> None:
    source = (ROOT / "static/v3/v3.js").read_text(encoding="utf-8")

    submit = source[source.index("async function submitTurn"):source.index("async function steerTurn")]
    turn_render = source[source.index("function renderTurn"):source.index("function callGroupStatus")]

    assert "const agentMessage = composerAgentMessage(message, selectedGuides, selectedContext);" in submit
    assert "newTurn(message, Date.now(), selectedGuides, selectedContext)" in submit
    assert "message: agentMessage" in submit
    assert "turn.userMessage" in turn_render
    assert "turn-skill-space" in turn_render
    assert "item.title" in turn_render
