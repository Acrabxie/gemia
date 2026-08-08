from pathlib import Path


def test_background_tasks_are_grouped_by_project_and_terminal_rows_are_hidden() -> None:
    source = Path("static/v3/v3.js").read_text(encoding="utf-8")

    assert "const projects = new Map();" in source
    assert 'const projectId = String(session.project_id || "");' in source
    assert "(s.active_subagents || []).length > 0" in source
    assert 'filter((t) => t.status === "running" || t._killing)' in source


def test_active_subagents_are_distinctly_labeled() -> None:
    source = Path("static/v3/v3.js").read_text(encoding="utf-8")
    css = Path("static/v3/v3.css").read_text(encoding="utf-8")

    assert 'class="task-row task-subagent"' in source
    assert 'class="task-kind">子代理</span>' in source
    assert ".task-subagent" in css


def test_background_task_copy_is_creator_facing() -> None:
    source = Path("static/v3/v3.js").read_text(encoding="utf-8")

    assert 'function creatorTaskLabel(summary, kind = "")' in source
    assert '"正在渲染画面"' in source
    assert 'creatorTaskLabel(j.summary, j.kind)' in source
    assert 'creatorTaskLabel(t.summary, "shell")' in source
    assert "[j.provider, j.last_polled_status]" not in source
    assert 'agent.tool_profile ?' not in source
    assert 'projectSidebarState.projectNames.get(group.projectId)' in source
    assert "function projectNamesFrom(projects)" in source
    assert "(project.sessions || []).map((session) => session.project_id)" in source
    assert "group.sessions.some((session) => session.session_id === state.sessionId)" in source
    assert "state.projectName !== group.projectId" in source
    assert 'title="${escapeHTML(s.session_id)}"' not in source


def test_subagent_presence_is_exposed_with_the_compact_session_snapshot() -> None:
    source = Path("gemia/v3_routes.py").read_text(encoding="utf-8")

    assert '"project_id": getattr(runner, "project_id", "") or "",' in source
    assert '"active_subagents": subagents,' in source
