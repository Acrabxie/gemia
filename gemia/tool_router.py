"""Deterministic, turn-scoped tool routing for Lumeri v3.

The production loop currently owns one flat ``TOOL_SCHEMAS`` list.  This
module is deliberately integration-only: it does not mutate the dispatcher or
the loop, but provides a small state machine that can be wired into the loop to
select a learnable schema subset.

Design invariants:

* conversational turns expose the small general pack instead of a hard
  zero-tool gate;
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


# System-level capabilities are part of every trusted-local root-agent route.
# They describe the agent/runtime control plane rather than one canvas
# workflow, so hiding them behind request keywords makes installed capabilities
# look unavailable. Remote and Plan policy gates still apply after routing.
# This set is also the single authority for registry ``surface=internal``.
SYSTEM_TOOLS = frozenset({
    "elicit",
    "spawn_subtasks",
    "recall_skills",
    "save_skill",
    "remember",
    "log_note",
    "publish_cloud_guide",
    "list_cloud_guides",
    "load_cloud_guide",
})

# Public/remote demo sessions must not read or mutate the owner's persistent
# memory, Skills, or Cloud Guide data. Role-specific tools (elicit for root,
# ask_root_agent for children) and bounded fan-out remain governed separately.
PRIVATE_SYSTEM_TOOLS = SYSTEM_TOOLS - {"elicit", "spawn_subtasks"}

# Backward-compatible name for callers that still treat the system plane as
# router controls rather than a first-class capability layer.
CONTROL_TOOLS = SYSTEM_TOOLS

# Local execution is a baseline Agent capability, not a workflow-specific
# creative verb. Keeping it active from the first model call prevents a
# routing miss from turning an executable request into user-facing shell
# instructions. Remote sessions and Plan mode still remove/block run_shell
# in AgentLoopV3 at their dedicated security gates.
TRUSTED_LOCAL_HOST_BASELINE = frozenset({"run_shell"})
BASELINE_TOOLS = SYSTEM_TOOLS | TRUSTED_LOCAL_HOST_BASELINE


# These are workflow-oriented packs, not security boundaries.  Dispatch must
# still fail closed against ToolRouter.active_tool_names when the module is
# integrated into AgentLoopV3.
TOOL_PACKS: dict[str, frozenset[str]] = {
    "production_design": frozenset({
        "get_design_state", "patch_design_state",
    }),
    "design_program": frozenset({
        "read_file", "write_file", "list_dir", "build",
        "check_job", "wait_for_job", "kill_job",
    }),
    "general": frozenset({
        "probe_media", "analyze_media", "extract_frame", "search_library",
        "copy_in", "list_dir", "web_search",
    }),
    "media_inspect": frozenset({
        "probe_media", "analyze_media", "extract_frame", "get_safe_areas",
        "inspect_lottie", "search_library", "search_media", "search_frames",
        "stock_media", "get_media_annotations",
    }),
    "image": frozenset({
        "generate_image", "edit_image", "composite", "color_grade",
        "adjust_media", "paint_overlay", "paint_mask_effect", "add_overlay",
        "transform_geometry", "smart_reframe", "get_safe_areas",
        "probe_media", "analyze_media", "extract_frame", "compose",
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
        "analyze_media", "extract_frame", "timeline_insert_clip", "grade",
        "prepare_roughcut",
    }),
    "audio": frozenset({
        "generate_audio", "narrate", "mix_audio", "edit_audio",
        "align_audio", "detect_beats", "check_job", "wait_for_job",
        "probe_media", "analyze_media",
    }),
    "storyboard": frozenset({
        "draft_shotlist", "set_shotlist", "update_shot", "get_shotlist",
        "refine_shot", "assemble_shotlist", "search_library", "search_media",
        "search_frames", "stock_media", "generate_image", "generate_video", "check_job",
        "wait_for_job", "inspect_timeline", "project_export",
    }),
    "quanta": frozenset({
        "draft_quanta", "set_quanta", "update_quantum", "get_quanta",
        "refine_quantum", "assemble_quanta", "search_library", "search_media",
        "search_frames", "generate_image", "check_job", "wait_for_job",
        "inspect_timeline", "project_export",
    }),
    "timeline": frozenset({
        "get_timeline", "timeline_insert_clip", "timeline_delete_clip",
        "timeline_move_clip", "timeline_trim_clip", "timeline_split_clip",
        "timeline_set_clip_time", "timeline_add_transition",
        "timeline_set_clip_effects", "timeline_add_track", "timeline_set_track",
        "timeline_undo", "inspect_timeline", "render_preview", "project_export",
        "verify_delivery",
        "edit_grammar", "rhythm_edit", "lumen_comp_to_timeline",
    }),
    "segment": frozenset({"get_segment", "segment_edit"}),
    "lumen_core": frozenset({
        "get_lumenframe", "lumen_patch",
        "lumen_render", "lumen_seek", "lumen_render_range",
        "vector_motion", "point_library", "camera", "lumen_comp_to_timeline",
    }),
    "lumen_time": frozenset({
        "get_lumenframe", "lumen_patch",
        "lumen_render", "lumen_render_range",
    }),
    "lumen_mask": frozenset({
        "get_lumenframe", "lumen_patch", "lumen_render",
        "lumen_seek", "lumen_render_range",
    }),
    "motion_graphics": frozenset({
        "build", "run_shell", "copy_in", "list_dir", "read_file",
        "write_file", "check_job", "wait_for_job", "probe_media",
        "analyze_media", "extract_frame", "search_library", "search_media",
        "search_frames", "get_lumenframe", "lumen_patch",
        "lumen_render", "lumen_seek",
        "vector_motion", "point_library", "kinetic_type",
    }),
    "files": frozenset({
        "read_file", "write_file", "copy_in", "list_dir",
        "move_file", "organize_files", "file_delete",
    }),
    "web": frozenset({"web_search", "web_open", "fetch"}),
    "annotations": frozenset({
        "annotate_media", "get_media_annotations", "write_media_annotation",
        "search_media", "search_library", "probe_media", "analyze_media",
        "prepare_roughcut",
    }),
    "memory_skills": frozenset({
        "point_library", "install_point_library", "list_point_libraries",
        "rollback_point_library", "publish_point_library",
    }),
    "jobs": frozenset({"check_job", "wait_for_job", "kill_job"}),
    "interchange": frozenset({
        "get_timeline", "project_export", "project_export_otio",
        "project_import_otio",
    }),
    "multiagent": frozenset({
        "probe_media", "analyze_media", "search_library",
        "get_timeline",
    }),
}


WORKFLOW_ORDER: tuple[str, ...] = (
    "production_design",
    "design_program",
    "storyboard",
    "quanta",
    "motion_graphics",
    "video_generation",
    "video_edit",
    "timeline",
    "segment",
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
    "production_design": (
        "creative ir", "reality contract", "production contract", "设计状态",
        "创作意图", "交付规格", "返修范围",
    ),
    "design_program": (
        "design program", "project://design", "project://edit", "project://source",
        "workspace", "html", "写代码", "代码文件", "设计程序", "设计算法",
        "算法驱动", "本地重算",
    ),
    "storyboard": (
        "分镜", "镜头", "脚本", "大纲", "多镜头", "宣传片", "storyboard",
        "shotlist", "script", "rough cut", "成片",
    ),
    "quanta": (
        "演示", "幻灯片", "讲稿", "离散视频", "汇报", "课件", "quanta",
        "slide", "slides", "deck", "presentation", "pitch deck", "ppt",
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
    "segment": (
        "片段细化", "片段结构", "状态树", "图层树", "展开片段", "segment document",
        "segment workspace", "segment edit", "refine clip",
    ),
    "lumen_core": (
        "图层", "合成树", "lumenframe", "layer", "composition",
        "渐变", "半透明", "不透明度", "混合模式", "滤色", "正片叠底",
        "高斯模糊", "调整层", "剪贴到下层", "剪贴图层",
        "gradient", "opacity", "blend mode", "gaussian blur",
        "adjustment layer", "clip to below", "clipping layer",
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
        "media library", "search media", "粗剪", "转写", "口头禅", "废话",
        "选条", "挑条", "rough cut", "transcribe", "filler", "take selection",
    ),
    "files": (
        "文件", "目录", "文件夹", "路径", "复制", "移动", "整理", "file",
        "folder", "directory", "path", "copy", "move", "organize",
    ),
    "web": (
        "网页", "网站", "互联网", "在线搜索", "搜一下网上", "搜索引擎",
        "联网", "查一下", "url", "website", "web search", "search engine",
        "look up", "internet", "latest", "最新资料",
    ),
    "interchange": ("otio", "opentimelineio", "final cut", "工程交换"),
    "memory_skills": (
        "记住", "长期记忆", "保存技能", "上传技能", "发布技能", "云端技能",
        "creator workflow", "remember", "save skill", "skill cloud",
    ),
    "multiagent": ("并行分析", "多个代理", "多代理", "parallel agents", "subtasks"),
    "general": (),
}


ADJACENT_PACKS: dict[str, tuple[str, ...]] = {
    "production_design": ("design_program", "storyboard", "motion_graphics"),
    "design_program": ("timeline", "motion_graphics", "audio"),
    "general": ("media_inspect", "files"),
    "media_inspect": ("files", "video_edit"),
    "motion_graphics": ("lumen_time", "lumen_mask", "video_edit"),
    "video_generation": ("video_edit", "timeline"),
    "video_edit": ("timeline", "audio"),
    "storyboard": ("timeline", "audio"),
    "quanta": ("storyboard", "timeline"),
    "timeline": ("video_edit", "segment", "lumen_core"),
    "segment": ("timeline", "lumen_core"),
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


# A real production spans turns. Prompt keywords select the initial creative
# surface, while the durable ProductionRun stage supplies the capabilities
# needed to continue unfinished work after a new message or process restart.
# These packs only expand visibility; dispatcher/plan/budget gates remain the
# security and money boundaries.
PRODUCTION_STAGE_PACKS: dict[str, tuple[str, ...]] = {
    "created": ("production_design", "general", "media_inspect"),
    "preflight": ("production_design", "general", "media_inspect"),
    "sourcing": ("production_design", "storyboard", "media_inspect"),
    "rough_cut": ("production_design", "design_program", "storyboard", "timeline"),
    "sound_pass": ("production_design", "design_program", "audio", "timeline"),
    # A visual pass must be able to replace rough-cut placeholders with the
    # locally rendered MG/2.5D assets it creates.  Keeping the timeline pack
    # active here makes that revision auditable instead of forcing operators
    # to mutate the project outside the formal host gate.
    "visual_pass": (
        "production_design",
        "design_program",
        "motion_graphics",
        "video_edit",
        "timeline",
    ),
    "rendering": ("production_design", "timeline", "media_inspect"),
    "verifying": ("production_design", "timeline", "media_inspect"),
    "ready_for_review": ("production_design", "timeline", "media_inspect"),
    "revising": (
        "production_design",
        "design_program",
        "timeline",
        "video_edit",
        "audio",
        "motion_graphics",
        "lumen_core",
        "media_inspect",
    ),
    "blocked": ("production_design", "general", "media_inspect"),
    "accepted": ("production_design", "media_inspect"),
    "failed": ("production_design", "general", "media_inspect"),
    "cancelled": (),
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


_INTEGRATION_TOPIC_RE = re.compile(
    r"(?:\b(?:api|oauth|sdk|picker|integration|integrate|connect|wire)\b|"
    r"photos?\s+(?:library|picker)|官方\s*api|接口|接入|接进|集成|连接)",
    re.I,
)

_MEDIA_ARTIFACT_ACTION_RE = re.compile(
    r"(?:(?:生成|制作|创建|画|设计|输出|修图|编辑).{0,48}"
    r"(?:图片|图像|照片|海报|封面|架构图|流程图)|"
    r"(?:make|create|generate|draw|design|produce|edit).{0,48}"
    r"(?:image|photo|poster|cover|diagram))",
    re.I,
)


def _integration_topic_without_media_artifact(text: str) -> bool:
    """True when image/photo names a service/API topic, not a deliverable."""
    return bool(_INTEGRATION_TOPIC_RE.search(text)) and not bool(
        _MEDIA_ARTIFACT_ACTION_RE.search(text)
    )


# "找到 logo" is a retrieval request, not a commission to animate one.  Topic
# nouns (logo, 宣传片, 动画…) used to route pure find/search turns into
# production packs, whose ledgers then demanded a final asset the user never
# asked for this turn.  When the commanded action is retrieval and no
# production verb appears, route to the inspect/files surface instead.
_RETRIEVAL_ACTION_RE = re.compile(
    r"(?:找到|找出|找一下|找找|寻找|查找|搜索|搜一下|搜搜|检索|"
    r"帮我找|给我找|替我找|把.{0,24}找|看看有没有|有没有|在哪里|在哪儿|在哪|哪里有|"
    r"\b(?:find|locate|search\s+for|look\s+for|where\s+is)\b)",
    re.I,
)

_PRODUCTION_ACTION_RE = re.compile(
    r"(?:生成|制作|创建|设计|渲染|导出|合成|拼接|添加|加上|叠加|插入|"
    r"调色|变速|转场|抠图|修图|字幕|配乐|做成|做一|做个|做张|做段|"
    r"画一|画个|画张|画幅|画出|剪辑|剪掉|剪成|裁剪|裁掉|"
    r"\b(?:generate|make|create|produce|render|export|edit|trim|cut|"
    r"add|overlay|insert|animate|design|draw|compose)\b)",
    re.I,
)

_PRODUCTION_PACKS = (
    "storyboard", "quanta", "motion_graphics", "video_generation",
    "video_edit", "timeline", "image", "audio",
    "lumen_core", "lumen_time", "lumen_mask", "interchange",
)

_MEDIA_REGISTRATION_RE = re.compile(
    r"(?:(?:登记|注册|导入|上传).{0,24}"
    r"(?:素材|媒体|视频|音频|图片|图像|文件|素材库|时间轴|时间线)|"
    r"(?:素材|媒体|视频|音频|图片|图像|文件).{0,24}"
    r"(?:登记|注册|导入|上传|放到|放进|加入|插入).{0,12}"
    r"(?:素材库|时间轴|时间线)?|"
    r"\b(?:register|import|upload|add)\b.{0,32}"
    r"\b(?:asset|media|video|audio|image|file|library|timeline)\b)",
    re.I,
)

_TIMELINE_DESTINATION_RE = re.compile(
    r"(?:时间轴|时间线|timeline|放到轨道|放进轨道|加入轨道|插入轨道)",
    re.I,
)

_NEGATED_VIDEO_GENERATION_RE = re.compile(
    r"(?:(?:不要|不需要|无需|别|禁止)(?:再|只|直接)?"
    r"(?:生成|制作|创建|做|输出)(?:任何|外部|新的|额外的)?"
    r"(?:(?:图片|图像|照片)\s*(?:或|和|、)\s*)?(?:视频|短片|成片)|"
    r"\b(?:do\s+not|don't|without|no\s+need\s+to|avoid)\s+"
    r"(?:generat(?:e|ing)|mak(?:e|ing)|creat(?:e|ing)|produc(?:e|ing)|output)"
    r"(?:\s+(?:any|external|new|extra))?\s+"
    r"(?:(?:images?|photos?)\s+(?:or|and)\s+)?(?:videos?|clips?)\b)",
    re.I,
)

_NEGATED_IMAGE_GENERATION_RE = re.compile(
    r"(?:(?:不要|不需要|无需|别|禁止)(?:再|只|直接)?"
    r"(?:生成|制作|创建|做|输出)(?:任何|外部|新的|额外的)?"
    r"(?:(?:视频|短片)\s*(?:或|和|、)\s*)?(?:图片|图像|照片)|"
    r"\b(?:do\s+not|don't|without|no\s+need\s+to|avoid)\s+"
    r"(?:generat(?:e|ing)|mak(?:e|ing)|creat(?:e|ing)|produc(?:e|ing)|output)"
    r"(?:\s+(?:any|external|new|extra))?\s+"
    r"(?:(?:videos?|clips?)\s+(?:or|and)\s+)?(?:images?|photos?)\b)",
    re.I,
)


def _retrieval_without_production(text: str) -> bool:
    """True when the turn commands finding something, not making something."""
    return bool(_RETRIEVAL_ACTION_RE.search(text)) and not bool(
        _PRODUCTION_ACTION_RE.search(text)
    )


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

    # A product name such as "Google Photos" is not a request to create an
    # image.  API/OAuth/integration discussion used to activate the image pack,
    # which then made the ledger require an image artifact.  Route such work to
    # documentation/files instead; an explicit diagram/image request below
    # retains the image pack.
    integration_topic = bool(_INTEGRATION_TOPIC_RE.search(text))
    media_artifact_action = bool(_MEDIA_ARTIFACT_ACTION_RE.search(text))
    if integration_topic and not media_artifact_action:
        scores["image"] = 0
        scores["web"] += 3
        scores["files"] += 2
    elif integration_topic and media_artifact_action:
        scores["image"] += 4

    # A pure retrieval turn ("找到 logo", "find the intro clip") must not be
    # routed into a production pack just because the target noun matches a
    # production keyword; the ledger would then require producing media the
    # user only asked to locate.
    if _retrieval_without_production(text):
        for name in _PRODUCTION_PACKS:
            scores[name] = 0
        scores["media_inspect"] += 3
        scores["files"] += 2
    if re.search(
        r"(?:搜索引擎|在线搜索|联网|web\s+search|search\s+engine)",
        text,
        re.I,
    ):
        scores["web"] += 4

    # Registration is a file-import operation even when the request also says
    # "素材库", which otherwise looks like annotation/search work. If the
    # requested destination is the timeline, expose both halves of the
    # required copy_in -> timeline_insert_clip chain on the first model call.
    if _MEDIA_REGISTRATION_RE.search(text):
        scores["files"] += 5
        if _TIMELINE_DESTINATION_RE.search(text):
            scores["timeline"] += 5

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

    # A creator may explicitly rule out generated media while asking for an
    # editable LumenFrame composition.  Those negative phrases must not win
    # the two-pack limit merely because they contain the words image/video.
    if _NEGATED_VIDEO_GENERATION_RE.search(text):
        scores["video_generation"] = 0
    if _NEGATED_IMAGE_GENERATION_RE.search(text):
        scores["image"] = 0
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
    packed = frozenset().union(*TOOL_PACKS.values()) | SYSTEM_TOOLS
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


def _production_state(state: Mapping[str, Any] | None) -> str:
    if not isinstance(state, Mapping):
        return ""
    value = state.get("production_state") or state.get("production_phase")
    return str(value or "").strip().lower()


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
        self._active_names.update(BASELINE_TOOLS)
        if self.decision.kind == "conversation":
            self.activate_pack("general")
            return

        for workflow in self.decision.workflows:
            self.activate_pack(workflow)
        for workflow in PRODUCTION_STAGE_PACKS.get(_production_state(state), ()):
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
        if not self.enabled or self.decision.kind != "actionable":
            return
        for workflow in PRODUCTION_STAGE_PACKS.get(_production_state(state), ()):
            self.activate_pack(workflow)
        if _has_pending_jobs(state):
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
    "BASELINE_TOOLS",
    "CONTROL_TOOLS",
    "MASTER_TOOL_NAMES",
    "MASTER_TOOL_SET",
    "PRIVATE_SYSTEM_TOOLS",
    "PRODUCTION_STAGE_PACKS",
    "RouteDecision",
    "RouteExpansion",
    "TOOL_PACKS",
    "SYSTEM_TOOLS",
    "TRUSTED_LOCAL_HOST_BASELINE",
    "ToolRouter",
    "WORKFLOW_KEYWORDS",
    "WORKFLOW_ORDER",
    "catalog_coverage",
    "classify_request",
    "is_conversation_only",
    "routing_enabled_from_env",
    "schemas_for_tool_names",
]
