from pathlib import Path

import server
from tests_http_harness import create_raw_request, run_server_handler


def video_frontend_source(root: Path) -> str:
    """Read the executable Video scripts in browser load order.

    Feature modules intentionally keep their implementation outside the small
    workspace entrypoint, so frontend behavior contracts must cover the same
    script set the page loads rather than one historical file.
    """
    v3_root = root / "static" / "v3"
    return "\n".join(
        (v3_root / name).read_text(encoding="utf-8")
        for name in ("v3-markdown.js", "v3-settings.js", "v3-auth.js", "v3.js")
    )


def make_request(method, path, headers=None, body=None):
    raw_request = create_raw_request(method, path, headers, body)
    response = run_server_handler(server._Handler, raw_request)
    return response["status"], response["headers"].get("cache-control", ""), response["body"]


def test_server_startup_does_not_eagerly_expand_the_lumerai_video_package() -> None:
    source = Path(server.__file__).read_text(encoding="utf-8")

    # Importing lumerai.sandbox executes lumerai/__init__.py, which expands the
    # complete video surface.  That context variable was unused here and made
    # cold start depend on hundreds of small reads from the external volume.
    assert "from lumerai.sandbox import sandbox_ctx as _sandbox_ctx" not in source


def test_root_is_intentionally_blank() -> None:
    """Root is reserved for the future Lumeri family portal; Video lives at /video."""
    response = run_server_handler(server._Handler, create_raw_request("GET", "/"))

    assert response["status"] == 200
    html = response["body"].decode("utf-8")
    assert "v3.js" not in html
    assert "<body></body>" in html


def test_video_serves_frontend() -> None:
    """The Lumeri Video UI (static/v3/ on disk) is served under /video."""
    video = run_server_handler(server._Handler, create_raw_request("GET", "/video/"))

    assert video["status"] == 200
    html = video["body"].decode("utf-8")
    assert "/video/v3.js" in html
    assert "/video/v3.css" in html
    scripts = (
        "/video/v3-markdown.js",
        "/video/v3-settings.js",
        "/video/v3-auth.js",
        "/video/v3.js",
    )
    assert all(script in html for script in scripts)
    assert [html.index(script) for script in scripts] == sorted(html.index(script) for script in scripts)


def test_quanta_uses_video_workspace_shell_and_keeps_kernel_demo() -> None:
    quanta = run_server_handler(server._Handler, create_raw_request("GET", "/quanta"))
    demo = run_server_handler(server._Handler, create_raw_request("GET", "/quanta/demo"))

    assert quanta["status"] == 200
    workspace_html = quanta["body"].decode("utf-8")
    assert "/video/v3.js" in workspace_html
    assert "/video/v3.css" in workspace_html
    assert demo["status"] == 200
    demo_html = demo["body"].decode("utf-8")
    assert demo_html != workspace_html
    assert "quanta" in demo_html.lower()


def test_model_selection_is_recommendation_not_a_lock() -> None:
    root = Path(server.__file__).resolve().parent
    source = video_frontend_source(root)
    server_source = Path(server.__file__).read_text(encoding="utf-8")

    assert "active.locked" not in source
    assert "强制锁定最强模型" not in source
    assert "strongest_model_lock" not in server_source
    assert '<span class="model-tag">推荐</span>' in source
    assert 'st.modelSource = "manual"' in source
    assert "模型扫描失败：" in source


def test_project_creation_has_button_slash_and_optional_folder_flow() -> None:
    root = Path(server.__file__).resolve().parent
    html = (root / "static/v3/index.html").read_text(encoding="utf-8")
    source = video_frontend_source(root)

    assert 'id="new-project-btn"' in html
    assert html.index('id="new-project-btn"') < html.index('id="new-session-btn"')
    assert html.index('id="chat-rail"') < html.index('id="project-sidebar"')
    assert 'id="history-drawer"' not in html
    assert 'id="project-sidebar-body"' in html
    assert '{ name: "project", desc: "新建 Project" }' in source
    assert 'case "project": openCreateProjectDialog(); break;' in source
    assert "本机目录" in source
    assert "目录是可选的" in source
    assert '...(String(sourceRoot || "").trim() ? { source_root:' in source
    assert "data-project-sessions" not in source
    assert "toggleHistoryDrawer" not in source
    assert "async function renderProjectSidebar()" in source
    assert 'apiFetch("/session-history/list?limit=100")' in source
    assert "data-project-new-session" not in source
    assert "data-project-show-more" in source
    assert "data-unassigned-new-session" not in source
    assert "state.projectId ? { fork_from_project_id: state.projectId } : {}" in source
    assert "function isUserFacingProject(project)" in source
    assert 'name === "DMG Project QA"' in source
    assert "name === id" in source


def test_video_serves_lumeri_working_indicator_assets() -> None:
    for path, expected in (
        ("/video/lumeri-working.svg", "Lumeri 正在工作"),
        ("/video/lumeri-working-static.svg", "Lumeri"),
    ):
        response = run_server_handler(server._Handler, create_raw_request("GET", path))

        assert response["status"] == 200
        assert response["headers"].get("content-type", "").startswith("image/svg+xml")
        svg = response["body"].decode("utf-8")
        assert expected in svg
        assert "width=\"90\"" in svg


def test_video_working_indicator_renders_under_assistant_output() -> None:
    root = Path(server.__file__).resolve().parent
    css = (root / "static/v3/v3.css").read_text(encoding="utf-8")
    source = video_frontend_source(root)

    assert "assistantMarkHtml" in source
    assert "startedAt: Date.now()" in source
    assert "completedAt: null" in source
    assert "formatWorkElapsed(turn, isActiveTurn)" in source
    assert "const shouldShowMark = isActiveTurn || hasAssistant" in source
    assert 'isActiveTurn ? "lumeri-working.svg" : "lumeri-working-static.svg"' in source
    assert "${assistantHtml}" in source
    assert "${assistantMarkHtml}" in source
    assert source.count("${assistantMarkHtml}") == 1
    assert source.index("${assistantHtml}") < source.index("${assistantMarkHtml}")
    assert ".assistant-workmark" in css
    assert ".assistant-workmark.is-static" in css
    assert "width: 24px" in css
    assert "height: 12px" in css
    assert "font-variant-numeric: tabular-nums" in css
    assert "border-radius: var(--shape-sm)" in css


def test_setup_supports_openai_subscription_quota_without_api_key() -> None:
    root = Path(server.__file__).resolve().parent
    source = video_frontend_source(root)
    css = (root / "static/v3/v3.css").read_text(encoding="utf-8")

    assert 'providerId === "openai_subscription"' in source
    assert "登录 Codex" in source
    assert 'apiFetch("/config/codex-login", { method: "POST" })' in source
    assert 'apiFetch("/config/codex-login-status")' in source
    assert 'authUrl.origin !== "https://auth.openai.com"' in source
    assert ".setup-codex-login" in css


def test_setup_surfaces_cloud_account_user_message() -> None:
    root = Path(server.__file__).resolve().parent
    source = video_frontend_source(root)

    assert "d.user_message || d.message || d.error" in source


def test_local_shortcuts_remain_actionable_without_a_runtime_session() -> None:
    root = Path(server.__file__).resolve().parent
    source = video_frontend_source(root)

    composer_start = source.index("  function syncComposerAction()")
    composer_end = source.index("\n  function render()", composer_start)
    composer = source[composer_start:composer_end]
    click_start = source.index('  els.sendBtn.addEventListener("click", () => {')
    click_end = source.index('  els.promptInput.addEventListener("keydown"', click_start)
    click_handler = source[click_start:click_end]
    shell_start = source.index("  function syncShell()")
    shell_end = source.index('  els.promptInput.addEventListener("input", syncShell)', shell_start)
    shell = source[shell_start:shell_end]

    assert "return SLASH_COMMANDS;" in source
    setup_start = source.index("  function openSetupPanel()")
    setup_end = source.index('\n    let overlay = $("#setup-modal")', setup_start)
    assert "byokAllowed()" not in source[setup_start:setup_end]
    assert "const shortcut = hasText ? parseSlashName(text) : null;" in composer
    assert 'const label = shortcut ? "执行快捷指令"' in composer
    assert click_handler.index("const name = msg && parseSlashName(msg);") < click_handler.index("if (!state.sessionId) return;")
    assert "const canSubmit = !!msg && (!!state.sessionId || !!shortcut);" in shell
    assert "const showPrimary = !!state.sessionId || !!shortcut;" in shell
    assert "已保存 Lumeri Credits 供应商偏好" in source
    assert "当前仅保存供应商偏好；Credits 结算功能将在后续开放。" in source
    assert "创作费用从当前 Lumeri 账户的 Credits 中结算。" not in source
    assert "当前账户使用 Lumeri 托管" not in source
    assert "在线创作暂未开放" not in source


def test_video_and_quanta_share_age_aware_switchable_provider_ui() -> None:
    root = Path(server.__file__).resolve().parent
    source = video_frontend_source(root)

    assert '"lumeri", "openai_subscription"' in Path(server.__file__).read_text(encoding="utf-8")
    assert "allowed_providers" in source
    assert "选择 Lumeri Credits" in source
    assert "仅保存供应商偏好；Credits 结算功能后续开放" in source
    assert "使用 ChatGPT 订阅额度" in source
    assert 'selectedProvider === "custom"' in source

    for route in ("/video/", "/quanta"):
        response = run_server_handler(server._Handler, create_raw_request("GET", route))
        assert response["status"] == 200
        assert "/video/v3.js" in response["body"].decode("utf-8")


def test_video_product_requests_are_centralized_in_api_client():
    root = Path(server.__file__).parent
    source = video_frontend_source(root)
    preview = (root / "static" / "v3" / "preview.html").read_text(encoding="utf-8")
    client = (root / "static" / "v3" / "api-client.js").read_text(encoding="utf-8")
    index = (root / "static" / "v3" / "index.html").read_text(encoding="utf-8")
    assert "fetch(" not in source
    assert "fetch(" not in preview
    assert "globalThis.fetch(" in client
    assert "`/api/v1${input}`" in client
    assert index.index("/video/api-client.js") < index.index("/video/v3.js")


def test_setup_model_focus_shows_all_scanned_models_before_typing() -> None:
    root = Path(server.__file__).resolve().parent
    source = video_frontend_source(root)
    focus_start = source.index('inp.addEventListener("focus", () => {')
    focus_end = source.index('document.addEventListener("click"', focus_start)
    focus_handler = source[focus_start:focus_end]

    assert "renderModelList(st.scannedModels, listEl, inp)" in focus_handler
    assert "filterModelList(inp.value, listEl, inp)" not in focus_handler


def test_signed_in_avatar_opens_compact_account_action_menu() -> None:
    root = Path(server.__file__).resolve().parent
    html = (root / "static/v3/index.html").read_text(encoding="utf-8")
    css = (root / "static/v3/v3.css").read_text(encoding="utf-8")
    source = video_frontend_source(root)

    assert 'aria-controls="account-menu"' in html
    assert 'id="account-menu"' in html
    assert html.count("data-account-action=") == 4
    assert all(
        f'data-account-action="{action}"' in html
        for action in ("settings", "setup", "help", "logout")
    )
    assert ".account-menu {" in css
    assert "border-radius: var(--shape-lg);" in css
    rail_head_css = css[css.index(".rail-head {"):css.index(".rail-head-spacer")]
    assert "position: relative;" in rail_head_css
    assert "function openAccountMenu()" in source
    assert "accountMenu.hidden ? openAccountMenu() : closeAccountMenu();" in source
    assert "setupAuth(initialAuthSession);" not in (root / "static/v3/v3-auth.js").read_text(encoding="utf-8")
    assert source.count("setupAuth(initialAuthSession);") == 1
    assert 'if (action === "settings") { openModelPicker(); return; }' in source
    assert 'if (action === "setup") { openSetupPanel(); return; }' in source
    assert 'if (action === "logout")' in source


def test_preview_is_a_single_timeline_player_not_an_asset_wall() -> None:
    root = Path(server.__file__).resolve().parent
    html = (root / "static/v3/index.html").read_text(encoding="utf-8")
    source = video_frontend_source(root)

    render_assets = source[source.index("function renderAssets"):source.index("const MEDIA_LIBRARY_KINDS")]
    assert "时间线播放" in html
    assert 'id="delivery-review-video"' in html
    assert 'id="timeline-preview-empty"' in html
    assert 'id="asset-grid" hidden' in html
    assert "currentTimelinePreview()" in source
    assert "derived_preview" in source
    assert "els.assetGrid.hidden = true" in render_assets
    assert "asset-card" not in render_assets
    assert "<img" not in render_assets
    assert "<audio" not in render_assets


def test_legacy_v3_redirects_to_video() -> None:
    index = run_server_handler(server._Handler, create_raw_request("GET", "/v3/"))
    assert index["status"] == 301
    assert index["headers"].get("location") == "/video"

    asset = run_server_handler(server._Handler, create_raw_request("GET", "/v3/v3.js"))
    assert asset["status"] == 301
    assert asset["headers"].get("location") == "/video/v3.js"


def test_retired_tauri_routes_are_gone() -> None:
    for method, path in (
        ("GET", "/tasks"),
        ("GET", "/task/some-task"),
        ("GET", "/assets/index.js"),
        ("GET", "/next"),
        ("POST", "/run-prompt"),
        ("POST", "/run-skill"),
        ("POST", "/quick-action"),
        ("POST", "/merge-clips"),
        ("POST", "/answer-ask/abc"),
        ("POST", "/revise-task/abc"),
        ("POST", "/task/abc/feedback"),
    ):
        response = run_server_handler(server._Handler, create_raw_request(method, path))
        assert response["status"] == 404, f"{method} {path} should be retired, got {response['status']}"


def test_runtime_api_is_feature_flagged(monkeypatch) -> None:
    monkeypatch.delenv("LUMERAI_VNEXT", raising=False)

    response = run_server_handler(
        server._Handler,
        create_raw_request("POST", "/runtime/dev/workspace", body={"session_id": "proj_rt"}),
    )

    assert response["status"] == 404
    assert response["body_json"]["error"] == "vNext runtime is disabled"


def test_server_defaults_to_lan_bind(monkeypatch) -> None:
    monkeypatch.delenv("GEMIA_HOST", raising=False)
    monkeypatch.delenv("LUMERI_HOST", raising=False)

    assert server._configured_server_host() == "0.0.0.0"
    assert "http://127.0.0.1:7788" in server._server_urls("0.0.0.0", 7788)


def test_favicon_request_is_not_a_browser_console_404() -> None:
    status, cache_control, raw = make_request("GET", "/favicon.ico")

    assert status == 204
    assert cache_control == "no-store"
    assert raw == b""


def test_video_asset_responses_close_connections() -> None:
    response = run_server_handler(server._Handler, create_raw_request("GET", "/video/v3.js"))

    assert response["status"] == 200
    assert response["headers"].get("connection") == "close"


def test_file_responses_support_byte_ranges(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(server, "_BASE_DIR", tmp_path)
    output = tmp_path / "temp" / "range.bin"
    output.parent.mkdir(parents=True)
    output.write_bytes(b"0123456789")

    response = run_server_handler(
        server._Handler,
        create_raw_request("GET", "/file/temp/range.bin", headers={"Range": "bytes=2-5"}),
    )

    assert response["status"] == 206
    assert response["headers"].get("accept-ranges") == "bytes"
    assert response["headers"].get("content-range") == "bytes 2-5/10"
    assert response["headers"].get("content-length") == "4"
    assert response["body"] == b"2345"


def test_file_responses_reject_invalid_byte_ranges(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(server, "_BASE_DIR", tmp_path)
    output = tmp_path / "temp" / "range.bin"
    output.parent.mkdir(parents=True)
    output.write_bytes(b"0123456789")

    response = run_server_handler(
        server._Handler,
        create_raw_request("GET", "/file/temp/range.bin", headers={"Range": "bytes=20-30"}),
    )

    assert response["status"] == 416
    assert response["headers"].get("content-range") == "bytes */10"
    assert response["headers"].get("accept-ranges") == "bytes"
    assert response["body"] == b""


def test_file_route_serves_temp_outputs_without_allowing_escape(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(server, "_BASE_DIR", tmp_path)
    output = tmp_path / "temp" / "veo" / "preview.mp4"
    output.parent.mkdir(parents=True)
    output.write_bytes(b"video")

    response = run_server_handler(server._Handler, create_raw_request("GET", "/file/temp/veo/preview.mp4"))
    assert response["status"] == 200
    assert response["body"] == b"video"

    escaped = run_server_handler(server._Handler, create_raw_request("GET", "/file/temp/../server.py"))
    assert escaped["status"] == 403

    unknown_root = run_server_handler(server._Handler, create_raw_request("GET", "/file/private/secret.mp4"))
    assert unknown_root["status"] == 403


def test_quanta_pager_static_files_support_get_head_and_query_strings() -> None:
    path = "/video/quanta.html?session_id=session_1&frame=0:0:img_001"
    get_response = run_server_handler(server._Handler, create_raw_request("GET", path))
    head_response = run_server_handler(server._Handler, create_raw_request("HEAD", path))

    assert get_response["status"] == 200
    assert get_response["headers"].get("content-type", "").startswith("text/html")
    assert b'src="quanta.js"' in get_response["body"]
    assert head_response["status"] == 200
    assert head_response["body"] == b""
    assert head_response["headers"].get("content-length") == str(len(get_response["body"]))

    for asset_path, content_type in (
        ("/video/quanta.css?cache=1", "text/css"),
        ("/video/quanta.js?cache=1", "text/javascript"),
    ):
        response = run_server_handler(server._Handler, create_raw_request("GET", asset_path))
        assert response["status"] == 200
        assert response["headers"].get("content-type", "").startswith(content_type)
