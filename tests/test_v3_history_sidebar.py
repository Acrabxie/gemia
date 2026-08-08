from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_project_sidebar_is_the_only_session_navigation() -> None:
    html = (ROOT / "static/v3/index.html").read_text(encoding="utf-8")
    css = (ROOT / "static/v3/v3.css").read_text(encoding="utf-8")

    assert html.index('id="preview-stage"') < html.index('id="chat-rail"')
    assert html.index('id="chat-rail"') < html.index('id="project-sidebar"')
    assert 'id="history-drawer"' not in html
    assert 'id="history-toggle-btn"' not in html
    assert "grid-template-columns: minmax(0, 1fr) 400px 240px" in css
    assert ".project-sidebar" in css
    assert "border-left: 1px solid var(--m3-outline-variant)" in css
    assert ".project-tree-session.is-active" in css
    assert ".history-drawer" not in css


def test_project_header_toggles_projects_and_session_history() -> None:
    html = (ROOT / "static/v3/index.html").read_text(encoding="utf-8")
    source = (ROOT / "static/v3/v3.js").read_text(encoding="utf-8")
    css = (ROOT / "static/v3/v3.css").read_text(encoding="utf-8")
    wiring = source.split('els.projectBtn?.addEventListener("click"', 1)[1].split(
        "els.requestChangesBtn", 1
    )[0]

    assert 'aria-controls="project-sidebar"' in html
    assert 'aria-expanded="true"' in html
    assert 'class="app-main" id="app-main"' in html
    assert 'classList.toggle("project-sidebar-collapsed", !open)' in wiring
    assert "els.projectSidebar.hidden = !open" in wiring
    assert 'setAttribute("aria-hidden", String(!open))' in wiring
    assert "openProjectModal" not in wiring
    assert ".app-main.project-sidebar-collapsed" in css


def test_project_sidebar_switches_real_sessions_and_marks_selection() -> None:
    source = (ROOT / "static/v3/v3.js").read_text(encoding="utf-8")
    sidebar = source.split("async function renderProjectSidebar()", 1)[1].split(
        "async function syncCurrentProject", 1
    )[0]

    assert "Promise.allSettled" in sidebar
    assert "snapshotsBySession" in sidebar
    assert "await loadHistorySession(button.dataset.snapshotId)" in sidebar
    assert "else await resumeSession(button.dataset.projectSessionId" in sidebar
    assert "project_id: button.dataset.projectId" in sidebar
    assert "run_id: button.dataset.runId" in sidebar
    assert 'aria-current="true"' in source
    assert "visibleProjectIds" in sidebar


def test_project_sidebar_does_not_duplicate_fork_sessions_in_chats() -> None:
    source = (ROOT / "static/v3/v3.js").read_text(encoding="utf-8")
    sidebar = source.split("async function renderProjectSidebar()", 1)[1].split(
        "async function syncCurrentProject", 1
    )[0]

    assert "projectSessionIds" in sidebar
    assert "project.sessions" in sidebar
    assert "!projectSessionIds.has(sessionId)" in sidebar


def test_sidebar_selection_covers_project_and_chat_entries_with_one_active_marker() -> None:
    source = (ROOT / "static/v3/v3.js").read_text(encoding="utf-8")
    selection = source.split("function syncProjectSidebarSelection()", 1)[1].split(
        "function sessionHasRunningWork", 1
    )[0]
    reset = source.split("function resetRuntimeView()", 1)[1].split(
        "let runtimeActivationSeq", 1
    )[0]
    chat_restore = source.split("async function restoreHistoryRecord", 1)[1].split(
        "async function loadHistorySession", 1
    )[0]

    assert '[data-project-session-id], [data-chat-snapshot-id]' in selection
    assert "state.activeHistoryId" in selection
    assert "activeHistoryId: null" in source
    assert "state.activeHistoryId = null;" in reset
    assert "state.activeHistoryId = session.id || null;" in chat_restore


def test_sidebar_uses_one_context_aware_new_session_action() -> None:
    html = (ROOT / "static/v3/index.html").read_text(encoding="utf-8")
    source = (ROOT / "static/v3/v3.js").read_text(encoding="utf-8")
    sidebar = source.split("async function renderProjectSidebar()", 1)[1].split(
        "async function syncCurrentProject", 1
    )[0]
    wiring = source.split('els.newSessionBtn.addEventListener("click"', 1)[1].split(
        "els.newProjectBtn", 1
    )[0]

    assert html.count('id="new-session-btn"') == 1
    assert "data-project-new-session" not in sidebar
    assert "data-unassigned-new-session" not in sidebar
    assert "新建会话" not in sidebar
    assert "state.projectId ? { fork_from_project_id: state.projectId } : {}" in wiring


def test_session_switch_detaches_ui_without_closing_background_runner() -> None:
    source = (ROOT / "static/v3/v3.js").read_text(encoding="utf-8")
    lifecycle = source.split("async function detachRuntime", 1)[1].split(
        "// ── session persistence", 1
    )[0]
    unload = source.split('window.addEventListener("beforeunload"', 1)[1]

    assert "/close" not in lifecycle
    assert "sendBeacon" not in unload
    assert "await detachRuntime();" in lifecycle
    assert "state.sessionId !== sessionId || state.eventSource !== es" in source
    assert "let runtimeActivationSeq = 0" in source
    assert "activationSeq !== runtimeActivationSeq" in lifecycle


def test_delayed_turn_controls_cannot_mutate_the_newly_selected_session() -> None:
    source = (ROOT / "static/v3/v3.js").read_text(encoding="utf-8")
    submit = source.split("async function submitTurn", 1)[1].split(
        "async function steerTurn", 1
    )[0]
    steer = source.split("async function steerTurn", 1)[1].split(
        "async function stopCurrentTurn", 1
    )[0]
    stop = source.split("async function stopCurrentTurn", 1)[1].split(
        "async function submitProductionReview", 1
    )[0]

    for handler in (submit, steer, stop):
        assert "const sessionId = state.sessionId" in handler
        assert "const activationSeq = runtimeActivationSeq" in handler
        assert "state.sessionId !== sessionId" in handler
        assert "runtimeActivationSeq !== activationSeq" in handler
    assert "apiFetch(`/sessions/${sessionId}/turn`" in submit
    assert "apiFetch(`/sessions/${sessionId}/steer`" in steer
    assert "apiFetch(`/sessions/${sessionId}/stop`" in stop


def test_project_session_dots_show_running_and_completed_states() -> None:
    source = (ROOT / "static/v3/v3.js").read_text(encoding="utf-8")
    css = (ROOT / "static/v3/v3.css").read_text(encoding="utf-8")

    assert "sessionHasRunningWork" in source
    assert "applyProjectSessionIndicators" in source
    assert 'apiFetch("/sessions?compact=1")' in source
    assert 'window.setInterval(refreshProjectSessionIndicators, 2500)' in source
    assert "data-runtime-session-id" in source
    assert ".project-tree-session.is-running .project-tree-session-dot" in css
    assert "animation: project-session-breathe" in css
    assert ".project-tree-session.is-complete .project-tree-session-dot" in css


def test_project_session_hover_menu_can_pin_and_delete() -> None:
    source = (ROOT / "static/v3/v3.js").read_text(encoding="utf-8")
    css = (ROOT / "static/v3/v3.css").read_text(encoding="utf-8")
    icons = (ROOT / "static/v3/icons.svg").read_text(encoding="utf-8")

    assert 'data-session-menu-toggle' in source
    assert 'data-session-action="pin"' in source
    assert 'data-session-action="delete"' in source
    assert 'method: "DELETE"' in source
    assert '/pin`' in source
    assert 'window.confirm(`删除“${title}”？`)' in source
    assert ".project-tree-session-row:hover .project-tree-session-more" in css
    assert ".project-tree-session-menu" in css
    assert 'symbol id="i-pin"' in icons


def test_project_session_menu_can_explicitly_handoff_current_results() -> None:
    source = (ROOT / "static/v3/v3.js").read_text(encoding="utf-8")
    sidebar = source.split("async function renderProjectSidebar()", 1)[1].split(
        "async function syncCurrentProject", 1
    )[0]

    assert 'data-session-action="handoff"' in source
    assert "canReceiveHandoff" in source
    assert "sourceSessionId = state.sessionId" in sidebar
    assert "/handoff`" in sidebar
    assert "时间轴、聊天和运行上下文不会共享" in sidebar


def test_linked_history_uses_resume_and_never_creates_a_replacement_run() -> None:
    source = (ROOT / "static/v3/v3.js").read_text(encoding="utf-8")
    resume = source.split("async function resumeSession", 1)[1].split(
        "async function attachSession", 1
    )[0]
    restore = source.split("async function restoreHistoryRecord", 1)[1].split(
        "async function loadHistorySession", 1
    )[0]

    assert "/sessions/${encodeURIComponent(sessionId)}/resume" in resume
    assert "!!expectedProjectId !== !!expectedRunId" in resume
    assert "data.project_id !== expectedProjectId" in resume
    assert "data.run_id !== expectedRunId" in resume
    assert 'apiFetch("/sessions"' not in resume
    assert "if (!session.v3_session_id || !session.run_id)" in restore
    assert "await resumeSession(session.v3_session_id" in restore
    assert "createSession(" not in restore


def test_history_hydrates_before_background_event_replay_connects() -> None:
    source = (ROOT / "static/v3/v3.js").read_text(encoding="utf-8")
    activate = source.split("async function activateSessionPayload", 1)[1].split(
        "async function createSession", 1
    )[0]
    restore = source.split("async function restoreHistoryRecord", 1)[1].split(
        "async function loadHistorySession", 1
    )[0]

    assert activate.index("if (hydrateHistory) hydrateHistory();") < activate.index(
        "connectSse(state.sessionId);"
    )
    assert "if (!restoreCachedSessionView(session.v3_session_id))" in restore
    assert "hydrateHistoryMessages(session);" in restore
    linked_restore = restore.split(
        "if (!session.v3_session_id || !session.run_id)", 1
    )[1]
    assert linked_restore.count("hydrateHistoryMessages(session)") == 1


def test_session_activation_reuses_resume_snapshot_without_duplicate_get() -> None:
    source = (ROOT / "static/v3/v3.js").read_text(encoding="utf-8")
    activate = source.split("async function activateSessionPayload", 1)[1].split(
        "async function createSession", 1
    )[0]
    refresh = source.split("async function refreshSessionState", 1)[1].split(
        "async function connectSse", 1
    )[0]

    assert "await refreshSessionState(data);" in activate
    assert "async function refreshSessionState(snapshot = null)" in source
    assert "let data = snapshot;" in refresh
    assert "if (!data)" in refresh


def test_server_restart_refreshes_authoritative_session_before_input() -> None:
    source = (ROOT / "static/v3/v3.js").read_text(encoding="utf-8")
    hello = source.split("protocol_hello: (ev) =>", 1)[1].split(
        "replay_gap: (ev) =>", 1
    )[0]
    submit = source.split("async function submitTurn", 1)[1].split(
        "async function steerTurn", 1
    )[0]

    assert "state.serverInstanceId !== nextInstanceId" in hello
    assert "state.recoveringSession = true" in hello
    assert "refreshSessionState()" in hello
    assert "state.recoveringSession = false" in hello
    assert "state.recoveringSession" in source.split(
        "function syncComposerAction()", 1
    )[1].split("function render()", 1)[0]
    assert "if (state.recoveringSession)" in submit
    assert "await autoSaveSession();" in submit


def test_session_switch_preserves_visible_in_progress_view() -> None:
    source = (ROOT / "static/v3/v3.js").read_text(encoding="utf-8")
    lifecycle = source.split("const sessionViewCache = new Map()", 1)[1].split(
        "async function activateSessionPayload", 1
    )[0]

    for field in (
        "turns",
        "currentTurn",
        "sessionTitle",
        "userMessageCount",
        "lastEventId",
        "pendingAsk",
        "backgroundTasks",
    ):
        assert f"{field}: state.{field}" in lifecycle
        assert f"state.{field} = cached.{field}" in lifecycle
    assert lifecycle.index("cacheCurrentSessionView();") < lifecycle.index(
        "state.eventSource.close();"
    )


def test_session_switch_clears_timeline_and_library_before_async_hydration() -> None:
    source = (ROOT / "static/v3/v3.js").read_text(encoding="utf-8")
    reset = source.split("function resetRuntimeView()", 1)[1].split(
        "let runtimeActivationSeq = 0", 1
    )[0]
    timeline_fetch = source.split("async function fetchProjectTimeline", 1)[1].split(
        "function startTimelinePoll", 1
    )[0]
    library_fetch = source.split("async function fetchMediaLibrary", 1)[1].split(
        "async function annotateLibraryAsset", 1
    )[0]

    for statement in (
        "TL.model = null;",
        'if (tlHeaders()) tlHeaders().innerHTML = "";',
        'if (tlContent()) tlContent().innerHTML = "";',
        "state.projectTimeline = null;",
        "state.mediaLibrary = [];",
        "state.sessionNonMediaAssets = [];",
        "state.mediaAnnotations = new Map();",
        "state.roughcutManifests = new Map();",
        "renderMediaLibrary();",
        'const libraryPanelBody = panelBodyFor("library");',
        'libraryPanelBody.innerHTML = `${librarySectionsHtml()}<p class="placeholder">加载中…</p>`;',
    ):
        assert statement in reset

    for request in (timeline_fetch, library_fetch):
        assert "const sessionId = state.sessionId;" in request
        assert "const activationSeq = runtimeActivationSeq;" in request
        assert "state.sessionId !== sessionId" in request
        assert "runtimeActivationSeq !== activationSeq" in request

    library_panel = source.split("async function renderLibraryPanel", 1)[1].split(
        "stagePanel?.addEventListener", 1
    )[0]
    assert "const sessionId = state.sessionId;" in library_panel
    assert "const activationSeq = runtimeActivationSeq;" in library_panel
    assert "state.sessionId === sessionId" in library_panel
    assert "runtimeActivationSeq === activationSeq" in library_panel
    assert library_panel.count("if (!isCurrentLibraryPanel()) return;") >= 2


def test_boot_does_not_replace_a_failed_production_restore_with_a_new_session() -> None:
    source = (ROOT / "static/v3/v3.js").read_text(encoding="utf-8")
    boot_restore = source.split("async function restoreCurrentSessionOrCreate", 1)[
        1
    ].split("// Normal Web boot restores", 1)[0]

    assert "let saved = null" in boot_restore
    assert "return restoreHistoryRecord(saved)" in boot_restore
    assert "return await restoreHistoryRecord(saved)" not in boot_restore
    assert boot_restore.index("} catch {}") < boot_restore.index(
        "return restoreHistoryRecord(saved)"
    )
    assert boot_restore.index("return restoreHistoryRecord(saved)") < boot_restore.index(
        "return createSession()"
    )


def test_tester_logout_requires_an_acknowledged_session_snapshot() -> None:
    source = (ROOT / "static/v3/v3.js").read_text(encoding="utf-8")
    persistence = source.split("async function autoSaveSession", 1)[1].split(
        "async function retractTurn", 1
    )[0]

    assert "requireAcknowledgement = false" in persistence
    assert "const response = await apiFetch" in persistence
    assert "if (!response.ok)" in persistence
    assert "if (requireAcknowledgement) throw error" in persistence
    assert "autoSaveSession({ requireAcknowledgement: true })" in persistence
