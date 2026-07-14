"""Deterministic, turn-scoped tool routing for Lumeri v3.

The production loop currently owns one flat ``TOOL_SCHEMAS`` list.  This
module is deliberately integration-only: it does not mutate the dispatcher or
the loop, but provides a small state machine that can be wired into the loop to
select a learnable schema subset.

Design invariants:

* conversational turns expose no tools;
* actionable turns start with at most two workflow packs;
* the active set only grows within a turn;
* one consecutive no-progress stop widens to an adjacent pack, the second
  falls back to the complete schema surface;
* disabling ``LUMERI_V3_TOOL_ROUTING`` restores the current full-schema path;
* schema order always follows ``TOOL_SCHEMAS`` so provider payloads remain
  deterministic.

The catalog is explicit on purpose.  Its exact-coverage test is the drift
alarm when a new verb is added to ``gemia.tools._schema``.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any, Iterable, Literal, Mapping

from gemia.tools._schema import TOOL_SCHEMAS


MASTER_TOOL_NAMES: tuple[str, ...] = tuple(
    str(schema["function"]["name"]) for schema in TOOL_SCHEMAS
)
MASTER_TOOL_SET = frozenset(MASTER_TOOL_NAMES)
_SCHEMA_BY_NAME: dict[str, dict[str, Any]] = {
    str(schema["function"]["name"]): schema for schema in TOOL_SCHEMAS
}


CONTROL_TOOLS = frozenset({"elicit"})


# These are workflow-oriented packs, not security boundaries.  Dispatch must
# still fail closed against ToolRouter.active_tool_names when the module is
# integrated into AgentLoopV3.
TOOL_PACKS: dict[str, frozenset[str]] = {
    "general": frozenset({
        "probe_media", "analyze_media", "extract_frame", "search_library",
        "copy_in", "file_list",
    }),
    "media_inspect": frozenset({
        "probe_media", "analyze_media", "extract_frame", "get_safe_areas",
        "inspect_lottie", "search_library", "search_media", "search_frames",
        "get_media_annotations",
    }),
    "image": frozenset({
        "generate_image", "edit_image", "composite", "color_grade",
        "adjust_media", "paint_overlay", "paint_mask_effect", "add_overlay",
        "transform_geometry", "smart_reframe", "get_safe_areas",
        "probe_media", "analyze_media", "extract_frame",
    }),
    "video_generation": frozenset({
        "generate_video", "generate_image", "check_job", "wait_for_job",
        "probe_media", "analyze_media", "extract_frame", "edit_video",
        "export", "copy_in",
    }),
    "video_edit": frozenset({
        "edit_video", "composite", "color_grade", "adjust_media",
        "add_overlay", "arrange_timeline", "subtitle", "animate_captions",
        "transform_geometry", "smart_reframe", "export", "probe_media",
        "analyze_media", "extract_frame", "timeline_insert_clip",
    }),
    "audio": frozenset({
        "generate_audio", "narrate", "mix_audio", "edit_audio",
        "align_audio", "detect_beats", "check_job", "wait_for_job",
        "probe_media", "analyze_media",
    }),
    "storyboard": frozenset({
        "draft_shotlist", "set_shotlist", "update_shot", "get_shotlist",
        "refine_shot", "assemble_shotlist", "search_library", "search_media",
        "search_frames", "generate_image", "generate_video", "check_job",
        "wait_for_job", "inspect_timeline", "project_export",
    }),
    "timeline": frozenset({
        "get_timeline", "timeline_insert_clip", "timeline_delete_clip",
        "timeline_move_clip", "timeline_trim_clip", "timeline_split_clip",
        "timeline_set_clip_time", "timeline_add_transition",
        "timeline_set_clip_effects", "timeline_add_track", "timeline_set_track",
        "timeline_undo", "inspect_timeline", "render_preview", "project_export",
    }),
    "lumen_core": frozenset({
        "get_lumenframe", "lumen_patch", "lumen_add_layer",
        "lumen_set_transform", "lumen_set_opacity", "lumen_delete_layer",
        "lumen_move_layer", "lumen_set_visibility", "lumen_select",
        "lumen_render", "lumen_seek", "lumen_render_range",
    }),
    "lumen_time": frozenset({
        "get_lumenframe", "lumen_set_range", "lumen_set_lane",
        "lumen_retime_segment", "lumen_reverse", "lumen_time_remap",
        "lumen_speed_ramp", "lumen_ripple_delete",
        "lumen_merge_compositions", "lumen_set_work_area", "lumen_render",
        "lumen_render_range",
    }),
    "lumen_mask": frozenset({
        "get_lumenframe", "lumen_set_mask", "lumen_key", "lumen_render",
        "lumen_seek", "lumen_render_range",
    }),
    "motion_graphics": frozenset({
        "build", "run_shell", "copy_in", "file_list", "file_read",
        "file_write", "check_job", "wait_for_job", "probe_media",
        "analyze_media", "extract_frame", "search_library", "search_media",
        "search_frames", "get_lumenframe", "lumen_patch",
        "lumen_add_layer", "lumen_set_transform", "lumen_render", "lumen_seek",
    }),
    "files": frozenset({
        "file_list", "file_read", "file_write", "file_copy", "file_move",
        "file_delete", "read_file", "write_file", "copy_in", "list_dir",
        "move_file", "organize_files",
    }),
    "web": frozenset({"web_search", "web_open", "fetch"}),
    "annotations": frozenset({
        "annotate_media", "get_media_annotations", "write_media_annotation",
        "search_media", "search_library", "probe_media", "analyze_media",
    }),
    "memory_skills": frozenset({
        "save_skill", "recall_skills", "remember", "log_note",
    }),
    "jobs": frozenset({"check_job", "wait_for_job"}),
    "interchange": frozenset({
        "get_timeline", "project_export", "project_export_otio",
        "project_import_otio",
    }),
    "multiagent": frozenset({
        "spawn_subtasks", "probe_media", "analyze_media", "search_library",
        "get_timeline",
    }),
}


WORKFLOW_ORDER: tuple[str, ...] = (
    "storyboard",
    "motion_graphics",
    "video_generation",
    "video_edit",
    "timeline",
    "lumen_core",
    "lumen_time",
    "lumen_mask",
    "image",
    "audio",
    "media_inspect",
    "annotations",
    "files",
    "web",
    "interchange",
    "memory_skills",
    "multiagent",
    "general",
)


WORKFLOW_KEYWORDS: dict[str, tuple[str, ...]] = {
    "storyboard": (
        "分镜", "镜头", "脚本", "大纲", "多镜头", "宣传片", "storyboard",
        "shotlist", "script", "rough cut", "成片",
    ),
    "motion_graphics": (
        "logo", "标志", "片头", "开场动画", "矢量", "动效", "动画", "blender",
        "三维场景", "3d场景",
        "轨迹", "弧线", "关键帧", "motion graphics", "keyframe", "mg动画",
    ),
    "video_generation": (
        "生成视频", "做一个视频", "做个视频", "做一段视频", "图生视频",
        "生成一段视频", "生成一个视频", "生成个视频", "动画视频",
        "做成视频", "转成视频", "转换成视频", "转换为视频",
        "输出视频", "短视频",
        "文生视频", "generate video", "text to video", "image to video", "veo",
    ),
    "video_edit": (
        "剪视频", "视频编辑", "裁视频", "调色", "字幕", "转场", "特效", "重构画幅",
        "导出", "转码", "转换格式", "导出为",
        "调亮", "调暗", "亮度", "曝光", "对比度", "饱和度",
        "edit video", "trim video", "color grade", "subtitle", "captions", "reframe",
        "black and white", "monochrome", "add overlay", "brightness",
        "exposure", "contrast", "saturation",
    ),
    "timeline": (
        "时间线", "时间轴", "片段", "轨道", "视频轨", "音频轨", "插入", "拆分", "剪辑",
        "timeline", "clip", "track", "split", "trim", "transition",
        "title overlay", "insert a title", "add a title", "cut off",
    ),
    "lumen_core": (
        "图层", "合成树", "lumenframe", "layer", "composition",
    ),
    "lumen_time": (
        "变速", "倒放", "重映射", "工作区", "速度坡", "retime", "reverse",
        "time remap", "speed ramp", "work area",
    ),
    "lumen_mask": (
        "蒙版", "抠像", "绿幕", "亮度键", "mask", "chroma", "luma key",
    ),
    "image": (
        "图片", "图像", "照片", "海报", "封面图", "封面图片", "抠图", "修图",
        "做张图", "做一张图",
        "生成图片", "生成图像", "image", "photo",
        "poster", "cover image", "cover art", "gif", "png", "jpeg", "jpg",
    ),
    "audio": (
        "音频", "声音", "音乐", "配乐", "旁白", "口播", "节拍", "audio",
        "music", "voiceover", "narration", "beat", "sound",
    ),
    "media_inspect": (
        "分析素材", "检查素材", "看一下", "时长", "分辨率", "帧率", "编码",
        "元数据", "analyze", "inspect", "duration", "resolution", "fps", "codec",
    ),
    "annotations": (
        "标注", "标签", "素材库", "找素材", "搜素材", "annotation",
        "media library", "search media",
    ),
    "files": (
        "文件", "目录", "文件夹", "路径", "复制", "移动", "整理", "file",
        "folder", "directory", "path", "copy", "move", "organize",
    ),
    "web": (
        "网页", "网站", "互联网", "在线搜索", "搜一下网上", "url", "website",
        "web search", "internet", "latest", "最新资料",
    ),
    "interchange": ("otio", "opentimelineio", "final cut", "工程交换"),
    "memory_skills": ("记住", "长期记忆", "保存技能", "remember", "save skill"),
    "multiagent": ("并行分析", "多个代理", "多代理", "parallel agents", "subtasks"),
    "general": (),
}


ADJACENT_PACKS: dict[str, tuple[str, ...]] = {
    "general": ("media_inspect", "files"),
    "media_inspect": ("files", "video_edit"),
    "motion_graphics": ("lumen_time", "lumen_mask", "video_edit"),
    "video_generation": ("video_edit", "timeline"),
    "video_edit": ("timeline", "audio"),
    "storyboard": ("timeline", "audio"),
    "timeline": ("video_edit", "lumen_core"),
    "lumen_core": ("lumen_time", "lumen_mask"),
    "lumen_time": ("lumen_mask", "timeline"),
    "lumen_mask": ("lumen_core", "image"),
    "image": ("video_edit", "lumen_mask"),
    "audio": ("video_edit", "timeline"),
    "files": ("motion_graphics", "media_inspect"),
    "web": ("files", "media_inspect"),
    "annotations": ("media_inspect", "storyboard"),
    "interchange": ("timeline",),
    "memory_skills": ("general",),
    "multiagent": ("media_inspect",),
}


IntentKind = Literal["conversation", "actionable"]
ExpansionStage = Literal["none", "adjacent", "full"]


@dataclass(frozen=True)
class RouteDecision:
    kind: IntentKind
    workflows: tuple[str, ...]
    scores: tuple[tuple[str, int], ...]
    source: str

    @property
    def primary_workflow(self) -> str:
        return self.workflows[0] if self.workflows else "conversation"


@dataclass(frozen=True)
class RouteExpansion:
    stage: ExpansionStage
    added_pack: str | None
    active_count: int
    no_progress_count: int


def routing_enabled_from_env(env: Mapping[str, str] | None = None) -> bool:
    """Resolve the routing kill switch.  Default is enabled."""
    values = os.environ if env is None else env
    raw = str(values.get("LUMERI_V3_TOOL_ROUTING", "1")).strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _normalized_text(request: str) -> str:
    return " ".join(str(request or "").strip().lower().split())


def _keyword_present(text: str, keyword: str) -> bool:
    """Use token boundaries for ASCII keywords so ``move`` != ``remove``."""
    if re.fullmatch(r"[a-z0-9][a-z0-9 _-]*", keyword):
        return bool(re.search(
            rf"(?<![a-z0-9_]){re.escape(keyword)}(?![a-z0-9_])",
            text,
            re.I,
        ))
    return keyword in text


_CONVERSATION_ONLY = {
    "你好", "您好", "嗨", "哈喽", "在吗", "你是谁", "你能做什么",
    "谢谢", "谢谢你", "谢了", "hi", "hello", "hey", "who are you",
    "what can you do", "thanks", "thank you",
}


def is_conversation_only(request: str) -> bool:
    """Narrow conversational carve-out; mixed greeting+task stays actionable."""
    text = _normalized_text(request)
    text = re.sub(r"[\s!！?？。,.，~～]+$", "", text)
    return text in _CONVERSATION_ONLY


def classify_request(
    request: str,
    *,
    state: Mapping[str, Any] | None = None,
    max_workflows: int = 2,
) -> RouteDecision:
    """Classify a request without an LLM call.

    Keyword scores select at most two workflow packs.  Exact internal tool-name
    mentions get a strong boost.  Live state is only a tie-break/fallback: a
    generic "continue" with a timeline/lumen document should return to that
    document, while pending jobs are handled separately as a support pack.
    """
    if is_conversation_only(request):
        return RouteDecision("conversation", (), (), "conversation")

    text = _normalized_text(request)
    scores: dict[str, int] = {name: 0 for name in WORKFLOW_ORDER}
    for workflow, keywords in WORKFLOW_KEYWORDS.items():
        scores[workflow] += sum(
            1 for keyword in keywords if _keyword_present(text, keyword)
        )

    # Head-noun generation phrasing may contain modifiers between the verb and
    # "video" ("生成一个有音乐的视频", "make a 7-second video").
    if re.search(
        r"(?:生成|制作|创建|做|输出|make|create|generate|produce|output)"
        r".{0,48}(?:视频|短片|成片|videos?|clips?)",
        text,
        re.I,
    ):
        scores["video_generation"] += 4
    if re.search(
        r"(?:生成|制作|创建|做|输出|make|create|generate|produce|output)"
        r".{0,24}(?:图片|图像|海报|照片|封面图|images?|photos?|posters?|cover\s+(?:image|art))",
        text,
        re.I,
    ):
        scores["image"] += 2
    if re.search(
        r"(?:生成|制作|创建|做|输出|make|create|generate|produce|output)"
        r".{0,24}(?:音频|音乐|配乐|旁白|口播|audio|music|voiceover|narration)",
        text,
        re.I,
    ):
        scores["audio"] += 2
    if re.search(
        r"(?:(?:给|为).{0,10}视频.{0,12}(?:加|添加|加入|配上|混入).{0,8}"
        r"(?:音乐|配乐|旁白|口播|音频)|"
        r"(?:把|将).{0,12}(?:音乐|配乐|旁白|口播|音频).{0,10}"
        r"(?:加到|混到|加入|放进).{0,6}视频|"
        r"(?:add|mix|attach).{0,16}(?:music|audio|voiceover|narration)"
        r".{0,12}(?:to|into).{0,6}(?:the\s+)?video|"
        r"(?:把|将).{0,12}(?:图片|图像|照片).{0,12}"
        r"(?:叠加|覆盖|加到|放到).{0,6}视频(?:上|里)?|"
        r"(?:add|place|overlay).{0,16}(?:image|photo).{0,16}"
        r"(?:overlay\s+)?(?:to|onto|on).{0,6}(?:the\s+)?video|"
        r"(?:add|place|overlay).{0,20}(?:logo|graphic).{0,16}"
        r"(?:to|onto|on).{0,6}(?:the\s+)?video|"
        r"(?:给|为).{0,10}视频.{0,12}(?:加|添加|加入|叠加|覆盖).{0,8}"
        r"(?:logo|标志|图标|图片|图像))",
        text,
        re.I,
    ):
        scores["video_edit"] += 4
    if re.search(
        r"(?:(?:add|insert).{0,20}(?:captions?|subtitles?|title).{0,16}"
        r"(?:to|into|on).{0,8}(?:the\s+)?video|"
        r"(?:裁|剪掉|裁掉|删除).{0,16}(?:视频|片段|开头|前\s*\d+\s*秒)|"
        r"(?:把|将).{0,10}视频.{0,12}(?:裁|剪|截).{0,12}(?:秒|时长)|"
        r"(?:remove|cut\s+off|trim).{0,24}(?:seconds?|secs?|video|clip)|"
        r"(?:insert|add).{0,20}(?:title|text).{0,20}(?:seconds?|secs?)|"
        r"(?:插入|添加).{0,16}(?:标题|文字).{0,16}秒)",
        text,
        re.I,
    ):
        scores["video_edit"] += 4
        scores["timeline"] += 3

    # If the user names an exact verb, expose a workflow that owns it instead
    # of expecting the model to rediscover the verb through a full schema dump.
    for tool_name in MASTER_TOOL_NAMES:
        if tool_name in text:
            for workflow in _tool_to_packs(tool_name):
                if workflow in scores:
                    scores[workflow] += 8
                    break

    live = dict(state or {})
    if live.get("has_timeline"):
        scores["timeline"] += 1
    if live.get("has_lumenframe"):
        scores["lumen_core"] += 1
    if live.get("has_assets") and not any(scores.values()):
        scores["media_inspect"] += 1

    ranked = sorted(
        ((name, score) for name, score in scores.items() if score > 0),
        key=lambda item: (-item[1], WORKFLOW_ORDER.index(item[0])),
    )
    if not ranked:
        workflows = ("general",)
        source = "fallback"
    else:
        limit = max(1, min(int(max_workflows), 2))
        workflows = tuple(name for name, _score in ranked[:limit])
        source = "keyword"
    return RouteDecision("actionable", workflows, tuple(ranked), source)


def schemas_for_tool_names(names: Iterable[str]) -> list[dict[str, Any]]:
    """Return schemas in canonical master order, ignoring unknown names."""
    wanted = set(names)
    return [_SCHEMA_BY_NAME[name] for name in MASTER_TOOL_NAMES if name in wanted]


def catalog_coverage() -> tuple[frozenset[str], frozenset[str]]:
    """Return ``(missing, unknown)`` catalog members for exact drift tests."""
    packed = frozenset().union(*TOOL_PACKS.values()) | CONTROL_TOOLS
    return MASTER_TOOL_SET - packed, packed - MASTER_TOOL_SET


def _tool_to_packs(tool_name: str) -> tuple[str, ...]:
    return tuple(name for name in WORKFLOW_ORDER if tool_name in TOOL_PACKS[name])


def _has_pending_jobs(state: Mapping[str, Any] | None) -> bool:
    if not state:
        return False
    pending = state.get("pending_jobs")
    if isinstance(pending, Mapping):
        return any(str(value).lower() not in {"done", "success", "succeeded"} for value in pending.values())
    if isinstance(pending, (list, tuple, set, frozenset)):
        return bool(pending)
    return bool(pending)


class ToolRouter:
    """Turn-scoped monotonic active-tool state.

    ``note_no_progress`` is intentionally tiny and deterministic.  The caller
    decides what counts as progress (normally a successful ToolOutcome state
    change, terminal job transition, or fresh verification) and calls
    ``note_progress`` to reset the consecutive counter.
    """

    def __init__(
        self,
        request: str,
        *,
        state: Mapping[str, Any] | None = None,
        enabled: bool | None = None,
        max_workflows: int = 2,
    ) -> None:
        self.request = str(request or "")
        self.enabled = routing_enabled_from_env() if enabled is None else bool(enabled)
        self.decision = classify_request(
            self.request, state=state, max_workflows=max_workflows
        )
        self._active_names: set[str] = set()
        self._active_packs: list[str] = []
        self._no_progress_count = 0
        self._full_fallback = False

        if not self.enabled:
            self._activate_all()
            return
        if self.decision.kind == "conversation":
            return

        self._active_names.update(CONTROL_TOOLS)
        for workflow in self.decision.workflows:
            self.activate_pack(workflow)
        if _has_pending_jobs(state):
            self.activate_pack("jobs")

    @property
    def active_tool_names(self) -> tuple[str, ...]:
        return tuple(name for name in MASTER_TOOL_NAMES if name in self._active_names)

    @property
    def active_schemas(self) -> list[dict[str, Any]]:
        return schemas_for_tool_names(self._active_names)

    @property
    def active_packs(self) -> tuple[str, ...]:
        return tuple(self._active_packs)

    @property
    def no_progress_count(self) -> int:
        return self._no_progress_count

    @property
    def is_full_fallback(self) -> bool:
        return self._full_fallback

    def activate_pack(self, pack_name: str) -> bool:
        """Monotonically add one pack.  Returns True only when it grew state."""
        if pack_name not in TOOL_PACKS:
            raise KeyError(f"unknown tool pack: {pack_name}")
        before = len(self._active_names)
        self._active_names.update(TOOL_PACKS[pack_name])
        if pack_name not in self._active_packs:
            self._active_packs.append(pack_name)
        return len(self._active_names) > before

    def activate_for_tool(self, tool_name: str) -> str | None:
        """Activate a pack owning a known hidden tool, without executing it."""
        if tool_name not in MASTER_TOOL_SET:
            return None
        if tool_name in self._active_names:
            return next((p for p in self._active_packs if tool_name in TOOL_PACKS[p]), None)
        packs = _tool_to_packs(tool_name)
        if not packs:
            return None
        self.activate_pack(packs[0])
        return packs[0]

    def observe_state(self, state: Mapping[str, Any] | None) -> None:
        """Expand support tools implied by live state; never remove tools."""
        if self.enabled and self.decision.kind == "actionable" and _has_pending_jobs(state):
            self.activate_pack("jobs")

    def note_progress(self) -> None:
        self._no_progress_count = 0

    def note_no_progress(self) -> RouteExpansion:
        if not self.enabled or self.decision.kind == "conversation" or self._full_fallback:
            return RouteExpansion("none", None, len(self._active_names), self._no_progress_count)

        self._no_progress_count += 1
        if self._no_progress_count == 1:
            candidates: list[str] = []
            for workflow in self.decision.workflows:
                candidates.extend(ADJACENT_PACKS.get(workflow, ()))
            candidates.extend(("general", "media_inspect", "files"))
            for pack_name in candidates:
                if pack_name not in self._active_packs:
                    self.activate_pack(pack_name)
                    return RouteExpansion(
                        "adjacent", pack_name, len(self._active_names), self._no_progress_count
                    )

            # Progress may have reset the consecutive counter after earlier
            # adjacent packs were activated. If no adjacent capability remains,
            # this signal is the exhausted-adjacency case and must restore the
            # full surface now rather than returning a dead-end ``none`` stage.
            self._activate_all()
            return RouteExpansion(
                "full", None, len(self._active_names), self._no_progress_count
            )

        # Second consecutive no-progress signal (or an exhausted adjacency
        # list) restores the exact full surface for capability recall.
        if self._no_progress_count >= 2:
            self._activate_all()
            return RouteExpansion("full", None, len(self._active_names), self._no_progress_count)

        return RouteExpansion("none", None, len(self._active_names), self._no_progress_count)

    def _activate_all(self) -> None:
        self._active_names = set(MASTER_TOOL_NAMES)
        self._full_fallback = True


__all__ = [
    "ADJACENT_PACKS",
    "CONTROL_TOOLS",
    "MASTER_TOOL_NAMES",
    "MASTER_TOOL_SET",
    "RouteDecision",
    "RouteExpansion",
    "TOOL_PACKS",
    "ToolRouter",
    "WORKFLOW_KEYWORDS",
    "WORKFLOW_ORDER",
    "catalog_coverage",
    "classify_request",
    "is_conversation_only",
    "routing_enabled_from_env",
    "schemas_for_tool_names",
]
