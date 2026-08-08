from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_workspace_keeps_preview_and_side_modules_in_one_board() -> None:
    html = (ROOT / "static/v3/index.html").read_text(encoding="utf-8")

    assert 'id="workspace-board"' in html
    assert 'class="workspace-module workspace-preview-module" data-workspace-module="preview"' in html
    assert 'id="stage-panel" aria-label="工作区模块"' in html
    assert html.index('id="empty-state"') < html.index('id="stage-panel"')
    assert html.index('id="stage-panel"') < html.index('id="timeline-drawer"')
    assert html.index('id="timeline-drawer"') < html.index('</div>\n  </section>')


def test_default_modules_are_simultaneous_and_persisted() -> None:
    source = (ROOT / "static/v3/v3.js").read_text(encoding="utf-8")

    assert 'isQuantaSurface ? ["quanta", "tasks", "library"] : ["timeline", "outline", "tasks"]' in source
    assert '`${surfaceStoragePrefix}:module-layout`' in source
    assert 'data-panel-body="${k}"' in source
    assert 'stageTabs.filter((k) => PANEL_MODULES.has(k)).forEach(refreshPanel)' in source
    assert 'previewStage.dataset.tab = panel ? "panel" : "preview"' not in source
    assert 'if (panelView !==' not in source


def test_modular_layout_uses_continuous_auto_filling_flow() -> None:
    css = (ROOT / "static/v3/v3.css").read_text(encoding="utf-8")
    source = (ROOT / "static/v3/v3.js").read_text(encoding="utf-8")

    assert 'position: relative; display: block' in css
    assert 'position: absolute; top: 0; left: 0' in css
    assert 'transition: transform 0.22s' in css
    assert '.module-resize-edge-x' in css
    assert '.module-resize-edge-y' in css
    assert '.module-resize-corner' in css
    assert '.workspace-side-module.is-focused' in css
    assert 'WorkspaceLayout.flowModules' in source
    assert 'new ResizeObserver(() => applyWorkspaceLayout())' in source
    assert 'workspaceBoard.classList.add("is-resizing")' in source
    assert 'workspaceBoard?.classList.remove("is-resizing")' in source
    assert 'next.width += (e.clientX - resizeState.startX)' in source
    assert 'next.height += (e.clientY - resizeState.startY)' in source
    assert 'Math.round((e.clientX - resizeState.startX)' not in source
    assert 'data-module-drag="${k}"' in source
    assert 'data-resize-axis="both"' in source
    assert '`${surfaceStoragePrefix}:workspace-order`' in source
    assert '`${surfaceStoragePrefix}:workspace-sizes`' in source
