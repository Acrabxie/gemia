from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from gemia.tool_router import (
    BASELINE_TOOLS,
    MASTER_TOOL_NAMES,
    SYSTEM_TOOLS,
    TOOL_PACKS,
    ToolRouter,
    catalog_coverage,
    classify_request,
    routing_enabled_from_env,
)

_HISTORICAL_PROMPT_FIXTURE = (
    Path(__file__).parent / "fixtures" / "tool_router_historical_prompts.json"
)


def _load_historical_prompt_corpus() -> dict[str, object]:
    return json.loads(_HISTORICAL_PROMPT_FIXTURE.read_text(encoding="utf-8"))


def test_catalog_exactly_covers_current_tool_schemas() -> None:
    assert len(MASTER_TOOL_NAMES) == len(set(MASTER_TOOL_NAMES))
    assert catalog_coverage() == (frozenset(), frozenset())
    # vector_motion must belong to a pack (else it only surfaces on full
    # fallback and the model falls back to hand-pushed keyframes).
    assert "vector_motion" in MASTER_TOOL_NAMES
    assert {
        "publish_cloud_guide", "list_cloud_guides", "load_cloud_guide"
    } <= BASELINE_TOOLS


def test_system_capabilities_are_default_and_not_workflow_routed() -> None:
    assert frozenset({
        "elicit", "spawn_subtasks",
        "recall_skills", "save_skill", "remember", "log_note",
        "publish_cloud_guide", "list_cloud_guides", "load_cloud_guide",
    }) == SYSTEM_TOOLS
    routed_tools = frozenset().union(*TOOL_PACKS.values())
    assert not (SYSTEM_TOOLS & routed_tools)

    for request in (
        "你好",
        "拉起子agent",
        "有没有子 agent 能力",
        "生成一张产品海报",
        "把片段插入时间线",
    ):
        assert set(ToolRouter(request).active_tool_names) >= SYSTEM_TOOLS, request


def test_canvas_capabilities_remain_workflow_filtered() -> None:
    conversation = set(ToolRouter("你好").active_tool_names)
    assert "generate_image" not in conversation
    assert "timeline_insert_clip" not in conversation
    assert "lumen_patch" not in conversation

    image = set(ToolRouter("生成一张产品海报").active_tool_names)
    assert "generate_image" in image
    assert "timeline_insert_clip" not in image


def test_conversation_gets_general_tools_and_mixed_greeting_stays_actionable() -> None:
    conversation = ToolRouter("你好")
    assert conversation.decision.kind == "conversation"
    assert "probe_media" in conversation.active_tool_names
    assert "web_search" in conversation.active_tool_names
    assert "elicit" in conversation.active_tool_names
    assert "recall_skills" in conversation.active_tool_names

    search = ToolRouter("为什么读不到自己的搜索引擎")
    assert "web" in search.decision.workflows
    assert "web_search" in search.active_tool_names

    actionable = ToolRouter("你好，帮我做一个 7 秒的动画视频")
    assert actionable.decision.kind == "actionable"
    assert actionable.active_tool_names
    assert "recall_skills" in actionable.active_tool_names
    assert len(actionable.decision.workflows) <= 2


def test_run_shell_is_a_baseline_tool_for_every_local_route() -> None:
    assert "run_shell" in BASELINE_TOOLS
    for request in (
        "你好",
        "你就不能自己帮我打开吗",
        "打开这个 HTML 到 Chrome",
        "运行 bash 命令 open -a Google Chrome",
        "生成一张产品海报",
    ):
        assert "run_shell" in ToolRouter(request).active_tool_names, request


def test_kill_switch_restores_the_full_master_surface() -> None:
    assert routing_enabled_from_env({}) is True
    assert routing_enabled_from_env({"LUMERI_V3_TOOL_ROUTING": "off"}) is False
    assert routing_enabled_from_env({"LUMERI_V3_TOOL_ROUTING": "1"}) is True

    router = ToolRouter("你好", enabled=False)
    assert router.active_tool_names == MASTER_TOOL_NAMES
    assert router.is_full_fallback is True


def test_curated_initial_routes_meet_canvas_schema_budget() -> None:
    prompts = [
        "分析这个素材的时长、分辨率和帧率",
        "生成一张 16:9 的产品海报图片",
        "做一个 7 秒动画视频",
        "给这个视频加字幕并调色",
        "给短片加音乐和旁白",
        "列出三个镜头的分镜并组装",
        "把片段插入时间线第二轨",
        "给图层设置位置和透明度",
        "把这一段倒放并做速度坡",
        "给人物加蒙版并做绿幕抠像",
        "在目录里复制并整理这些文件",
        "搜索网上最新的参考资料",
        "给素材添加场景标签",
        "把当前工程导出为 OTIO",
        "记住这个剪辑偏好",
        "让多个代理并行分析素材",
        "做一个 logo 开场动画",
        "生成视频并配旁白",
        "分析图片并把它改成竖版海报",
        "读取这个文件",
    ]
    routed_counts: list[int] = []
    for prompt in prompts:
        router = ToolRouter(prompt)
        assert router.decision.kind == "actionable"
        assert 1 <= len(router.decision.workflows) <= 2
        routed_counts.append(len(set(router.active_tool_names) - BASELINE_TOOLS))

    # System capabilities are deliberate fixed overhead.  This budget guards
    # the canvas/workflow increment so adding an always-present system verb
    # cannot be mistaken for a routing regression.
    ordered = sorted(routed_counts)
    p95 = ordered[math.ceil(0.95 * len(ordered)) - 1]
    assert sum(routed_counts) / len(routed_counts) <= 15
    assert p95 <= 25


def test_repository_history_prompt_pack_recall_and_full_fallback_rate() -> None:
    corpus = _load_historical_prompt_corpus()
    assert corpus["corpus_kind"] == "repository_history_test_prompts"
    assert corpus["is_production_conversation_data"] is False
    samples = corpus["samples"]
    assert isinstance(samples, list) and len(samples) >= 30

    seen: set[tuple[str, str]] = set()
    misses: list[tuple[str, str, tuple[str, ...]]] = []
    full_fallbacks = 0
    for sample in samples:
        assert isinstance(sample, dict)
        assert set(sample) == {
            "prompt",
            "required_pack",
            "source_path",
            "source_revision",
        }
        prompt = sample["prompt"]
        required_pack = sample["required_pack"]
        source_path = sample["source_path"]
        source_revision = sample["source_revision"]
        assert isinstance(prompt, str) and prompt.strip()
        assert isinstance(required_pack, str) and required_pack in TOOL_PACKS
        assert source_revision == "487c298"
        assert isinstance(source_path, str) and source_path.startswith("tests/")
        assert source_path.endswith(".py")
        provenance_key = (source_path, prompt)
        assert provenance_key not in seen
        seen.add(provenance_key)

        router = ToolRouter(prompt)
        if required_pack not in router.decision.workflows:
            misses.append((prompt, required_pack, router.decision.workflows))
            router.note_no_progress()
            if required_pack not in router.active_packs:
                router.note_no_progress()
            full_fallbacks += int(router.is_full_fallback)
        assert required_pack in router.active_packs or router.is_full_fallback

    recall = 1 - (len(misses) / len(samples))
    fallback_rate = full_fallbacks / len(samples)
    assert recall >= 0.95, f"required-pack recall={recall:.1%}; misses={misses}"
    assert fallback_rate < 0.05, f"fallback-to-104={fallback_rate:.1%}"


def test_no_progress_expansion_is_monotonic_then_falls_back_to_all_tools() -> None:
    router = ToolRouter("做一个 logo 开场动画")
    initial = set(router.active_tool_names)

    first = router.note_no_progress()
    after_first = set(router.active_tool_names)
    assert first.stage == "adjacent"
    assert first.added_pack is not None
    assert initial < after_first
    assert len(after_first) < len(MASTER_TOOL_NAMES)

    second = router.note_no_progress()
    assert second.stage == "full"
    assert router.active_tool_names == MASTER_TOOL_NAMES
    assert router.is_full_fallback is True


def test_progress_resets_consecutive_no_progress_counter() -> None:
    router = ToolRouter("给图层设置透明度")
    assert router.note_no_progress().stage == "adjacent"
    assert router.no_progress_count == 1

    router.note_progress()
    assert router.no_progress_count == 0
    assert router.note_no_progress().stage == "adjacent"
    assert router.no_progress_count == 1


def test_exhausted_adjacency_falls_back_full_even_after_progress_resets() -> None:
    router = ToolRouter("继续处理")
    assert router.note_no_progress().stage == "adjacent"
    router.note_progress()
    assert router.note_no_progress().stage == "adjacent"
    router.note_progress()
    expansion = router.note_no_progress()
    assert expansion.stage == "full"
    assert router.is_full_fallback is True
    assert router.active_tool_names == MASTER_TOOL_NAMES


def test_hidden_known_tool_can_expand_pack_in_canonical_order() -> None:
    router = ToolRouter("继续处理")
    before = set(router.active_tool_names)
    assert "project_import_otio" not in before

    activated = router.activate_for_tool("project_import_otio")
    assert activated == "interchange"
    assert before < set(router.active_tool_names)
    assert router.activate_for_tool("not_a_real_tool") is None

    master_positions = {name: index for index, name in enumerate(MASTER_TOOL_NAMES)}
    positions = [master_positions[name] for name in router.active_tool_names]
    assert positions == sorted(positions)


def test_pending_job_state_adds_job_support_without_removing_tools() -> None:
    router = ToolRouter("做一个视频", state={"pending_jobs": {"job-1": "running"}})
    initial = set(router.active_tool_names)
    assert {"check_job", "wait_for_job"} <= initial

    router.observe_state({"pending_jobs": {"job-1": "done"}})
    assert initial <= set(router.active_tool_names)


def test_classification_is_deterministic() -> None:
    request = "把视频倒放并加音乐"
    assert classify_request(request) == classify_request(request)


def test_independent_cover_image_keeps_both_deliverable_workflows() -> None:
    decision = classify_request("做一个视频并生成一张封面图")
    assert decision.workflows == ("video_generation", "image")


def test_source_image_made_into_video_routes_to_video_generation() -> None:
    decision = classify_request("把这张图片做成视频")
    assert decision.workflows[:2] == ("video_generation", "image")


def test_common_adjustment_language_routes_to_editing_not_read_only_inspection() -> None:
    decision = classify_request("把画面调亮一点", state={"has_assets": True})
    assert decision.primary_workflow == "video_edit"


def test_fresh_lumenframe_appearance_language_exposes_the_layer_patch_tools() -> None:
    prompts = (
        "请直接在 LumenFrame 中做一个 3 秒、可编辑的复杂合成，不要只解释，也不要生成外部图片或视频："
        "深蓝到青色的径向渐变背景；中央椭圆作为剪贴底层；在它上方叠一层粉紫线性渐变并剪贴到椭圆，"
        "透明度 64%，使用滤色混合，添加 8px 高斯模糊和有羽化的椭圆蒙版；"
        "最上方放一个覆盖完整 3 秒的调整层并加 2px 高斯模糊。完成后渲染第 0 帧 PNG，并检查确认每项属性都已真正写入。",
        "请直接在 LumenFrame 中做一个 3 秒、可编辑的复杂图层合成：深蓝到青色的径向渐变背景；"
        "中央椭圆作为剪贴底层；在它上方叠一层粉紫线性渐变并剪贴到椭圆，透明度 64%，使用滤色混合，"
        "添加 8px 高斯模糊和有羽化的椭圆蒙版；最上方放一个覆盖完整 3 秒的调整层并加 2px 高斯模糊。"
        "完成后用 LumenFrame 渲染并检查确认每项属性都已真正写入。",
    )

    for prompt in prompts:
        router = ToolRouter(prompt)
        assert "lumen_core" in router.active_packs
        assert "lumen_patch" in router.active_tool_names
        assert "get_lumenframe" in router.active_tool_names

    negated = ToolRouter(prompts[0])
    assert "video_generation" not in negated.active_packs
    assert "image" not in negated.active_packs
    assert "generate_video" not in negated.active_tool_names
    assert "generate_image" not in negated.active_tool_names


@pytest.mark.parametrize(
    "prompt",
    (
        "用 LumenFrame 图层合成，不要生成图片或视频",
        "Use LumenFrame layers without generating images or videos",
        "Use LumenFrame layers; do not generate external images or videos",
    ),
)
def test_negated_media_generation_does_not_displace_lumenframe(prompt: str) -> None:
    router = ToolRouter(prompt)
    assert "lumen_core" in router.active_packs
    assert "video_generation" not in router.active_packs
    assert "image" not in router.active_packs


def test_media_registration_intents_expose_deterministic_import_chain() -> None:
    registration_only = ToolRouter(
        "登记到项目素材库", state={"production_state": "rough_cut"}
    )
    assert "files" in registration_only.decision.workflows
    assert "copy_in" in registration_only.active_tool_names

    for request in (
        "上传这个视频并放到时间轴",
        "把这个素材放到时间线",
        "import this media into the timeline",
    ):
        router = ToolRouter(request, state={"production_state": "rough_cut"})
        assert {"files", "timeline"} <= set(router.decision.workflows), request
        assert "copy_in" in router.active_tool_names, request
        assert "timeline_insert_clip" in router.active_tool_names, request


def test_prompt_requires_registered_asset_before_timeline_insertion() -> None:
    prompt = Path("gemia/prompts/system_v3.md").read_text(encoding="utf-8")

    assert "asset_registered=true" in prompt
    assert "pass that exact returned `asset_id`" in prompt
    assert "`search_frames` result is not registration" in prompt


def test_google_photos_api_topic_does_not_route_to_image_generation() -> None:
    for request in (
        "哦我说的是通过Google官方的api接入photo",
        "我想了解通过Google官方API接入Google Photos，该怎么做？",
        "请直接把 Google Photos Picker API 接进 Lumeri",
        "Explain how the Google Photos OAuth integration works",
    ):
        router = ToolRouter(request)
        assert "image" not in router.decision.workflows
        assert "generate_image" not in router.active_tool_names


def test_explicit_google_photos_diagram_request_still_routes_to_image() -> None:
    for request in (
        "生成一张 Google Photos API 接入架构图",
        "Create a Google Photos integration diagram",
    ):
        router = ToolRouter(request)
        assert "image" in router.decision.workflows
        assert "generate_image" in router.active_tool_names
