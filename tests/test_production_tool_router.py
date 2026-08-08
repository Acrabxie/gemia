from gemia.tool_router import ToolRouter


def test_sourcing_stage_exposes_stock_without_flattening_full_catalog() -> None:
    router = ToolRouter(
        "继续完成我们的正式作品",
        state={"production_state": "sourcing"},
    )
    assert "stock_media" in router.active_tool_names
    assert "get_shotlist" in router.active_tool_names
    assert "file_delete" not in router.active_tool_names
    assert not router.is_full_fallback


def test_stage_transition_monotonically_exposes_next_production_tools() -> None:
    router = ToolRouter(
        "继续完成我们的正式作品",
        state={"production_state": "sourcing"},
    )
    router.observe_state({"production_state": "sound_pass"})
    assert "stock_media" in router.active_tool_names
    assert "mix_audio" in router.active_tool_names
    assert "timeline_insert_clip" in router.active_tool_names


def test_production_stage_expands_instead_of_suppressing_requested_tools() -> None:
    router = ToolRouter(
        "在 project://edit/pinball.html 创建单文件 HTML",
        state={"production_state": "created"},
    )
    assert "patch_design_state" in router.active_tool_names
    assert "read_file" in router.active_tool_names
    assert "write_file" in router.active_tool_names
    assert "build" in router.active_tool_names
    assert not router.is_full_fallback


def test_plain_html_creation_routes_to_project_file_authoring() -> None:
    router = ToolRouter(
        "我想玩弹珠机，写个html",
        state={"production_state": "created"},
    )
    assert "design_program" in router.decision.workflows
    assert "write_file" in router.active_tool_names
