"""Deterministic per-turn execution ledger for Lumeri v3.

The ledger is intentionally independent from the agent loop.  It can consume
today's raw tool-result dictionaries and a future structured ``ToolOutcome``
object through the same duck-typed adapter.  Its job is to preserve the facts
that prose history tends to lose: what the user asked for, what mutated, what
was verified afterwards, which jobs are still live, and which objective
acceptance criteria remain unsatisfied.

No method in this module executes a tool.  Completion is a derived decision,
not a model-authored claim.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import PurePath
from typing import Any, Iterable, Mapping, Sequence

from gemia.tool_outcome import ToolOutcome, classify_tool_result
from gemia.tool_router import MASTER_TOOL_SET, classify_request


_MISSING = object()


PENDING_JOB_STATES = frozenset({"pending", "queued", "submitted", "running", "processing"})
FAILED_JOB_STATES = frozenset({"failed", "error", "cancelled", "canceled"})
FINISHED_JOB_STATES = frozenset({"done", "success", "succeeded", "completed", "complete"})


VISUAL_VERIFICATION_TOOLS = frozenset({
    "analyze_media",
    "inspect_lottie",
    "inspect_timeline",
    "lumen_seek",
    "lumen_render_range",
    "render_preview",
    "host_visual_review",
})

OBJECTIVE_VERIFICATION_TOOLS = frozenset({"probe_media", "analyze_media"})

# Only host-derived media/project facts may satisfy deterministic acceptance
# criteria.  Production tools can truthfully report that an operation ran, but
# their self-reported duration/fps/etc. are not a substitute for ffprobe or the
# canonical project state.
OBJECTIVE_CRITERIA_TOOLS = frozenset({
    "probe_media", "host_ffprobe", "inspect_timeline", "host_timeline",
})

MUTATION_TOOLS = frozenset({
    "generate_image", "generate_video", "generate_audio", "narrate",
    "subtitle", "animate_captions", "edit_image", "edit_video",
    "composite", "color_grade", "adjust_media", "paint_overlay",
    "paint_mask_effect", "add_overlay", "arrange_timeline", "mix_audio",
    "edit_audio", "transform_geometry", "smart_reframe", "assemble_shotlist",
    "set_shotlist", "update_shot", "refine_shot", "annotate_media",
    "write_media_annotation", "draft_shotlist", "export", "file_write", "file_copy",
    "file_move", "file_delete", "build", "save_skill", "remember",
    "log_note", "lumen_patch", "lumen_add_layer", "lumen_set_transform",
    "lumen_set_opacity", "lumen_delete_layer", "lumen_move_layer",
    "lumen_set_visibility", "lumen_select", "lumen_set_mask", "lumen_key",
    "lumen_render", "lumen_set_range", "lumen_set_lane",
    "lumen_retime_segment", "lumen_reverse", "lumen_time_remap",
    "lumen_speed_ramp", "lumen_ripple_delete", "lumen_merge_compositions",
    "lumen_set_work_area", "timeline_insert_clip", "timeline_delete_clip",
    "timeline_move_clip", "timeline_trim_clip", "timeline_split_clip",
    "timeline_set_clip_time", "timeline_add_transition",
    "timeline_set_clip_effects", "timeline_add_track", "timeline_set_track",
    "timeline_undo", "project_export", "project_export_otio",
    "project_import_otio", "write_file", "copy_in", "move_file",
    "organize_files", "align_audio", "run_shell",
})


_VISUAL_WORKFLOWS = frozenset({
    "image", "video_generation", "video_edit", "storyboard", "timeline",
    "lumen_core", "lumen_time", "lumen_mask", "motion_graphics",
})

_ASSET_WORKFLOWS = frozenset({
    "image", "video_generation", "video_edit", "audio", "motion_graphics",
})

_READ_ONLY_WORKFLOWS = frozenset({"media_inspect", "web"})

_EXPECTED_FINAL_KINDS: dict[str, frozenset[str]] = {
    "image": frozenset({"image"}),
    "video_generation": frozenset({"video"}),
    "video_edit": frozenset({"video"}),
    "audio": frozenset({"audio"}),
    "motion_graphics": frozenset({"video"}),
}

_TOOL_OUTPUT_KIND: dict[str, str] = {
    "generate_image": "image",
    "edit_image": "image",
    "paint_overlay": "image",
    "generate_video": "video",
    "edit_video": "video",
    "composite": "video",
    "color_grade": "video",
    "adjust_media": "video",
    "paint_mask_effect": "video",
    "add_overlay": "video",
    "arrange_timeline": "video",
    "subtitle": "video",
    "animate_captions": "video",
    "transform_geometry": "video",
    "smart_reframe": "video",
    "generate_audio": "audio",
    "narrate": "audio",
    "mix_audio": "audio",
    "edit_audio": "audio",
    "align_audio": "audio",
}


_REQUESTED_OPERATION_SPECS: tuple[
    tuple[str, str, re.Pattern[str], frozenset[str]], ...
] = (
    (
        "subtitle", "Add the requested subtitles or captions",
        re.compile(r"(?:字幕|caption|subtitles?)", re.I),
        frozenset({"subtitle", "animate_captions"}),
    ),
    (
        "color", "Apply the requested color treatment",
        re.compile(
            r"(?:调色|色彩|亮度|对比度|饱和度|曝光|伽马|黑白|单色|灰度|灰阶|去色|"
            r"brightness|contrast|saturation|exposure|gamma|color\s*grade|"
            r"gr[ae]yscale|monochrome|black\s+and\s+white)",
            re.I,
        ),
        frozenset({"color_grade", "adjust_media"}),
    ),
    (
        "transition", "Add the requested transition",
        re.compile(r"(?:转场|transition|dissolve|溶解|淡入淡出)", re.I),
        frozenset({"timeline_add_transition", "arrange_timeline"}),
    ),
    (
        "split", "Split the requested clip",
        re.compile(r"(?:拆分|切开|split\b)", re.I),
        frozenset({"timeline_split_clip"}),
    ),
    (
        "insert", "Insert the requested clip or overlay",
        re.compile(
            r"(?:插入|加入时间线|叠加|覆盖|insert\b|overlay\b|title\s+overlay|"
            r"标题(?:层|叠加)|(?:添加|加入).{0,10}(?:logo|标志|图标|图片|图像)|"
            r"(?:add|place).{0,20}(?:logo|graphic|image|photo))",
            re.I,
        ),
        frozenset({"timeline_insert_clip", "add_overlay", "composite"}),
    ),
    (
        "trim", "Trim the requested media range",
        re.compile(
            r"(?:裁剪|截取|剪取|保留|剪掉|裁掉|删除|"
            r"trim\b|cut(?:\s+off)?\b|keep\b|remove\b)",
            re.I,
        ),
        frozenset({"timeline_trim_clip", "edit_video"}),
    ),
    (
        "retime", "Apply the requested speed or timing change",
        re.compile(
            r"(?:加速|减速|放慢|变速|倒放|快放|慢放|播放速度|速度(?:调|设)|速度坡|"
            r"\d+(?:\.\d+)?\s*倍速|[一二两三四五六七八九十]\s*倍速|"
            r"retime|reverse|speed\s*ramp|speed\s*up|slow.{0,16}down|faster|slower|"
            r"\d+(?:\.\d+)?\s*x\b)",
            re.I,
        ),
        frozenset({
            "edit_video", "lumen_retime_segment",
            "lumen_reverse", "lumen_time_remap", "lumen_speed_ramp",
        }),
    ),
    (
        "voiceover", "Create or attach the requested voiceover",
        re.compile(r"(?:旁白|口播|voiceover|narration)", re.I),
        frozenset({"narrate", "timeline_insert_clip"}),
    ),
    (
        "music", "Create or attach the requested music",
        re.compile(
            r"(?:(?:添加|加入|加上|加|配上|配|生成|制作|混合).{0,3}"
            r"(?:配乐|背景音乐|音乐)|(?:add|create|generate|mix)\s+"
            r"(?:background\s+)?music|music\s+track)",
            re.I,
        ),
        frozenset({
            "generate_audio", "mix_audio", "align_audio", "timeline_insert_clip",
        }),
    ),
    (
        "mask_key", "Apply the requested mask or key",
        re.compile(r"(?:蒙版|抠像|绿幕|亮度键|mask\b|chroma|luma\s+key)", re.I),
        frozenset({"paint_mask_effect", "lumen_set_mask", "lumen_key"}),
    ),
)


def _requested_operation_steps(request: str) -> dict[str, LedgerStep]:
    steps: dict[str, LedgerStep] = {}
    image_only_crop = bool(
        re.search(r"(?:图片|图像|照片|image|photo)", request, re.I)
        and not re.search(
            r"(?:视频|短片|时间线|片段|秒|video|timeline|clip|seconds?|secs?)",
            request,
            re.I,
        )
    )
    for operation_id, description, pattern, _tools in _REQUESTED_OPERATION_SPECS:
        if not pattern.search(request):
            continue
        if operation_id == "trim" and image_only_crop:
            continue
        steps[f"op:{operation_id}"] = LedgerStep(
            f"op:{operation_id}", description
        )
    return steps


def _operation_tools(operation_id: str) -> frozenset[str]:
    for candidate, _description, _pattern, tools in _REQUESTED_OPERATION_SPECS:
        if candidate == operation_id:
            return tools
    return frozenset()


def _requests_monochrome(request: str) -> bool:
    return bool(re.search(
        r"(?:黑白|单色|灰度|灰阶|去色|grayscale|greyscale|monochrome|black\s+and\s+white)",
        request,
        re.I,
    ))


def _requested_color_look(request: str) -> str | None:
    patterns = (
        ("teal_orange", r"(?:青橙|teal[_ -]?orange)"),
        ("warm", r"(?:暖色|暖调|温暖|warm)"),
        ("cool", r"(?:冷色|冷调|cool)"),
        ("vintage", r"(?:复古|vintage)"),
        ("cinematic", r"(?:电影感|电影色调|cinematic)"),
        ("neutral", r"(?:中性色调|neutral)"),
    )
    for look, pattern in patterns:
        if re.search(pattern, request, re.I):
            return look
    return None


def _requested_color_parameters(request: str) -> dict[str, float]:
    names = {
        "brightness": r"亮度|brightness",
        "contrast": r"对比度|contrast",
        "saturation": r"饱和度|saturation",
        "exposure": r"曝光|exposure",
        "gamma": r"伽马|gamma",
    }
    params: dict[str, float] = {}
    for name, label in names.items():
        match = re.search(
            rf"(?:{label}).{{0,10}}?(?P<value>[+-]?\d+(?:\.\d+)?)\s*(?P<percent>%)?",
            request,
            re.I,
        )
        if not match:
            continue
        value = float(match.group("value"))
        context = request[max(0, match.start() - 24):match.end() + 12]
        increase = bool(re.search(
            r"(?:提高|增加|提升|调高|升高|boost|increase|raise)",
            context,
            re.I,
        ))
        decrease = bool(re.search(
            r"(?:降低|减少|调低|下降|decrease|reduce|lower)",
            context,
            re.I,
        ))
        baseline = 1.0 if name in {"contrast", "saturation", "gamma"} else 0.0
        if match.group("percent"):
            ratio = abs(value) / 100.0
            if increase:
                value = baseline + ratio
            elif decrease:
                value = baseline - ratio
            else:
                value = value / 100.0
        elif value >= 0 and (increase or decrease):
            value = baseline + value if increase else baseline - value
        params[name] = value
    return params


def _requested_transition_kind(request: str) -> str | None:
    patterns = (
        ("dissolve", r"(?:溶解|dissolve|cross[ -]?fade)"),
        ("wipe", r"(?:擦除|划像|wipe)"),
        ("fade", r"(?:淡入淡出|渐隐|fade)"),
        ("cut", r"(?:硬切|直接切|\bcut\b)"),
    )
    for kind, pattern in patterns:
        if re.search(pattern, request, re.I):
            return kind
    return None


def _requested_transition_duration(request: str) -> float | None:
    transition_re = re.compile(
        r"(?:转场|溶解|擦除|划像|淡入淡出|transition|dissolve|wipe|fade)",
        re.I,
    )
    candidates: list[float] = []
    for match in re.finditer(
        r"(\d+(?:\.\d+)?)\s*(?:秒|seconds?|secs?|s\b)",
        request,
        re.I,
    ):
        before = request[max(0, match.start() - 12):match.start()]
        after = request[match.end():match.end() + 12]
        if re.search(r"(?:第|at)\s*$", before, re.I):
            continue
        if re.match(r"\s*(?:处|位置|时刻)", after):
            continue
        surrounding = request[max(0, match.start() - 16):match.end() + 16]
        if transition_re.search(surrounding):
            candidates.append(float(match.group(1)))
    return candidates[-1] if candidates else None


def _transition_satisfied(
    request: str,
    transition: Mapping[str, Any],
) -> bool:
    actual_kind = str(
        transition.get("kind") or transition.get("type") or "cut"
    ).strip().lower()
    expected_kind = _requested_transition_kind(request)
    diagnostics = " ".join(
        str(transition.get(name) or "")
        for name in ("warning", "warnings", "export_note", "note", "message")
    )
    if expected_kind not in {None, "cut"} and re.search(
        r"(?:hard\s+cut|renders?\s+(?:as\s+)?(?:a\s+)?cut|"
        r"not\s+supported|unsupported|降级为?硬切|渲染为?硬切)",
        diagnostics,
        re.I,
    ):
        return False
    if expected_kind is not None:
        if actual_kind != expected_kind:
            return False
    elif actual_kind == "cut":
        return False
    expected_duration = _requested_transition_duration(request)
    if expected_duration is None:
        return True
    actual_duration = _parse_number(_first(
        transition, ("duration_sec", "duration", "seconds")
    ))
    return (
        actual_duration is not None
        and abs(actual_duration - expected_duration) <= 0.01
    )


def _requested_speed_factor(request: str) -> float | None:
    number = r"(?P<factor>\d+(?:\.\d+)?|[一二两三四五六七八九十])"
    patterns = (
        re.compile(
            rf"(?P<direction>加速|提速|减速|放慢|变速|(?:播放)?速度(?:调|设)?(?:到|为)?)"
            rf".{{0,12}}?(?:原来的?)?\s*{number}\s*(?:倍|[xX]\b)",
            re.I,
        ),
        re.compile(
            rf"(?:(?:speed\s*up|speed|faster|slower).{{0,8}}?|\b)"
            rf"{number}\s*[xX]\b",
            re.I,
        ),
        re.compile(rf"{number}\s*倍速", re.I),
    )
    words = {
        "一": 1.0, "二": 2.0, "两": 2.0, "三": 3.0, "四": 4.0,
        "五": 5.0, "六": 6.0, "七": 7.0, "八": 8.0, "九": 9.0,
        "十": 10.0,
    }
    for pattern in patterns:
        match = pattern.search(request)
        if not match:
            continue
        raw = match.group("factor")
        factor = float(raw) if raw.replace(".", "", 1).isdigit() else words.get(raw)
        if factor is None or factor <= 0:
            return None
        direction = str(match.groupdict().get("direction") or "")
        # Chinese "减速两倍" conventionally means half speed. Explicit
        # "减速到 0.5 倍" already yields the requested factor directly.
        if direction == "减速" and "到" not in match.group(0) and factor > 1:
            return 1.0 / factor
        return factor
    percent = re.search(
        r"(?P<direction>加速|提速|减速|放慢|变速|"
        r"speed\s*up|slow(?:er|\s*down)?|increase\s+(?:the\s+)?speed|"
        r"decrease\s+(?:the\s+)?speed|set\s+(?:the\s+)?speed|"
        r"(?:播放)?速度(?:调|设)?)"
        r"(?P<body>.{0,24}?)(?P<percent>\d+(?:\.\d+)?)\s*%",
        request,
        re.I,
    )
    if percent:
        ratio = float(percent.group("percent")) / 100.0
        direction = percent.group("direction").lower()
        body = percent.group("body")
        explicit_target = bool(re.search(
            r"(?:到|为|设为|调到|to|at)", body, re.I
        )) or direction.startswith("set")
        if explicit_target or direction in {"变速", "速度", "播放速度"}:
            value = ratio
        elif re.search(r"(?:减速|放慢|slow|decrease)", direction, re.I):
            value = 1.0 - ratio
        else:
            value = 1.0 + ratio
        return value if value > 0 else None
    return None


def _requested_trim_range(request: str) -> tuple[float, float] | None:
    leading_duration = re.search(
        r"(?:(?:截取|裁剪|剪取|保留)\s*前\s*|"
        r"(?:keep|trim)\s+the\s+first\s+)"
        r"(\d+(?:\.\d+)?)\s*(?:秒|seconds?|secs?|s\b)",
        request,
        re.I,
    )
    if leading_duration:
        duration = float(leading_duration.group(1))
        if duration > 0:
            return 0.0, duration
    start_duration = re.search(
        r"(?:从\s*)?第?\s*(\d+(?:\.\d+)?)\s*秒\s*(?:开始)?\s*"
        r"(?:截取|裁剪|剪取|保留)\s*(\d+(?:\.\d+)?)\s*秒",
        request,
        re.I,
    )
    if start_duration:
        start = float(start_duration.group(1))
        duration = float(start_duration.group(2))
        if duration > 0:
            return start, start + duration
    english_start_duration = re.search(
        r"(?:trim|keep|cut)\s+from\s+"
        r"(\d+(?:\.\d+)?)\s*(?:seconds?|secs?|s)\s+for\s+"
        r"(\d+(?:\.\d+)?)\s*(?:seconds?|secs?|s)\b",
        request,
        re.I,
    )
    if english_start_duration:
        start = float(english_start_duration.group(1))
        duration = float(english_start_duration.group(2))
        if duration > 0:
            return start, start + duration
    timecode_range = re.search(
        r"(?:截取|裁剪|剪取|保留|trim|keep|cut).{0,12}?"
        r"(\d{1,2}):(\d{2}(?:\.\d+)?)\s*(?:到|至|to|[-–—~～])\s*"
        r"(\d{1,2}):(\d{2}(?:\.\d+)?)",
        request,
        re.I,
    )
    if timecode_range:
        start = float(timecode_range.group(1)) * 60 + float(timecode_range.group(2))
        end = float(timecode_range.group(3)) * 60 + float(timecode_range.group(4))
        if end > start:
            return start, end
    patterns = (
        re.compile(
            r"(?:截取|裁剪|剪取|保留|trim\b|cut\b).{0,12}?"
            r"(\d+(?:\.\d+)?)\s*(?:秒)?\s*(?:开始)?\s*[,，]?\s*"
            r"(?:到|至|[-–—~～])\s*第?\s*"
            r"(\d+(?:\.\d+)?)\s*秒",
            re.I,
        ),
        re.compile(
            r"(?:trim|cut|keep).{0,12}?(?:from\s*)?"
            r"(\d+(?:\.\d+)?)\s*(?:seconds?|secs?|s)?\s*(?:to|[-–—~])\s*"
            r"(\d+(?:\.\d+)?)\s*(?:seconds?|secs?|s)\b",
            re.I,
        ),
    )
    for pattern in patterns:
        match = pattern.search(request)
        if not match:
            continue
        start, end = float(match.group(1)), float(match.group(2))
        if end > start:
            return start, end
    return None


def _requested_timeline_time(request: str) -> float | None:
    patterns = (
        re.compile(r"(?:在\s*)?第\s*(\d+(?:\.\d+)?)\s*秒", re.I),
        re.compile(
            r"(?:在|at)\s*(\d+(?:\.\d+)?)\s*(?:秒|seconds?|secs?|s\b)\s*(?:处|位置|时刻)?",
            re.I,
        ),
    )
    for pattern in patterns:
        match = pattern.search(request)
        if match:
            return float(match.group(1))
    return None


def _timeline_time_satisfied(
    request: str,
    call_args: Mapping[str, Any],
    facts: Mapping[str, Any],
) -> bool:
    expected = _requested_timeline_time(request)
    if expected is None:
        return True
    actual = _parse_number(_first(
        call_args,
        ("at_time", "time_sec", "timeline_time", "start"),
    ))
    if actual is None:
        actual = _parse_number(_first(
            facts,
            ("at_time", "time_sec", "timeline_time", "start"),
        ))
    return actual is not None and abs(actual - expected) <= 0.01


def _requested_quoted_literal(
    request: str, keyword_pattern: str
) -> str | None:
    match = re.search(
        rf"(?:{keyword_pattern}).{{0,10}}?[“\"'‘]([^”\"'’]+)[”\"'’]",
        request,
        re.I,
    )
    return match.group(1).strip() if match else None


def _requested_insert_duration(request: str) -> float | None:
    patterns = (
        r"(?:插入|添加|加入).{0,18}?(?:标题|文字).{0,12}?"
        r"(?:持续|显示)\s*(\d+(?:\.\d+)?)\s*秒",
        r"(?:插入|添加|加入).{0,12}?(?:持续|显示)\s*"
        r"(\d+(?:\.\d+)?)\s*秒.{0,8}?(?:标题|文字)",
        r"(?:insert|add).{0,24}?(?:title|text).{0,24}?"
        r"for\s+(\d+(?:\.\d+)?)\s*(?:seconds?|secs?|s\b)",
    )
    for pattern in patterns:
        match = re.search(pattern, request, re.I)
        if match:
            return float(match.group(1))
    return None


def _insert_duration_satisfied(
    request: str,
    call_args: Mapping[str, Any],
    facts: Mapping[str, Any],
) -> bool:
    expected = _requested_insert_duration(request)
    if expected is None:
        return True
    actual = _parse_number(_first(
        call_args, ("duration_sec", "duration", "seconds")
    ))
    if actual is None:
        actual = _parse_number(_first(
            facts, ("duration_sec", "duration", "seconds")
        ))
    return actual is not None and abs(actual - expected) <= 0.01


def _literal_content_satisfied(
    operation_id: str,
    request: str,
    call_args: Mapping[str, Any],
    facts: Mapping[str, Any],
) -> bool:
    if operation_id == "subtitle":
        expected = _requested_quoted_literal(
            request, r"字幕|caption|subtitles?"
        )
        if expected is None:
            return True
        actual = call_args.get("text") or facts.get("text")
        if actual is None:
            cues = call_args.get("cues") or facts.get("cues") or []
            if isinstance(cues, Sequence) and not isinstance(cues, (str, bytes)):
                actual = " ".join(
                    str(item.get("text") or "")
                    for item in cues
                    if isinstance(item, Mapping)
                )
        return " ".join(str(actual or "").split()) == " ".join(expected.split())
    if operation_id == "insert":
        expected = _requested_quoted_literal(request, r"标题|title")
        if expected is None:
            return True
        text_payload = call_args.get("text") or facts.get("text")
        actual = (
            text_payload.get("content")
            if isinstance(text_payload, Mapping)
            else text_payload
        )
        return " ".join(str(actual or "").split()) == " ".join(expected.split())
    return True


def _trim_range_satisfied(
    request: str,
    call_args: Mapping[str, Any],
    facts: Mapping[str, Any],
) -> bool:
    remove_leading = re.search(
        r"(?:(?:剪掉|裁掉|删除)\s*(?:前|开头)\s*|"
        r"(?:remove|cut\s+off)\s+the\s+first\s+)"
        r"(\d+(?:\.\d+)?)\s*(?:秒|seconds?|secs?|s\b)",
        request,
        re.I,
    )
    expected = _requested_trim_range(request)
    if expected is None and remove_leading is None:
        return True
    nested = call_args.get("trim")
    trim_args = nested if isinstance(nested, Mapping) else call_args
    start = _parse_number(_first(
        trim_args,
        ("start_sec", "source_in", "start", "in_sec"),
    ))
    end = _parse_number(_first(
        trim_args,
        ("end_sec", "source_out", "end", "out_sec"),
    ))
    if start is None:
        start = _parse_number(_first(
            facts,
            ("start_sec", "source_in", "start", "in_sec"),
        ))
    if end is None:
        end = _parse_number(_first(
            facts,
            ("end_sec", "source_out", "end", "out_sec"),
        ))
    if remove_leading is not None:
        expected_start = float(remove_leading.group(1))
        return (
            start is not None
            and abs(start - expected_start) <= 0.01
            and end is None
        )
    assert expected is not None
    return (
        start is not None
        and end is not None
        and abs(start - expected[0]) <= 0.01
        and abs(end - expected[1]) <= 0.01
    )


def _retime_factor_satisfied(
    request: str,
    call_args: Mapping[str, Any],
    facts: Mapping[str, Any],
    *,
    field_names: Sequence[str],
) -> bool:
    actual = _parse_number(_first(call_args, field_names))
    if actual is None:
        actual = _parse_number(_first(facts, field_names))
    expected = _requested_speed_factor(request)
    if expected is not None:
        return actual is not None and abs(actual - expected) <= 0.01
    if actual is None:
        return False
    if re.search(r"(?:加速|提速|speed\s*up|faster)", request, re.I):
        return actual > 1.0
    if re.search(r"(?:减速|slow(?:er|\s*down)?)", request, re.I):
        return 0.0 < actual < 1.0
    return actual > 0.0 and abs(actual - 1.0) > 1e-9


def _operation_satisfied(
    operation_id: str,
    tool_name: str,
    call_args: Mapping[str, Any],
    facts: Mapping[str, Any],
    request: str,
) -> bool:
    """Require operation-specific evidence, not merely a broad tool name."""
    if tool_name not in _operation_tools(operation_id):
        return False
    if operation_id == "transition" and tool_name == "arrange_timeline":
        transitions = call_args.get("transitions") or facts.get("transitions") or []
        return any(
            isinstance(item, Mapping)
            and _transition_satisfied(request, item)
            for item in transitions
        )
    if operation_id == "transition" and tool_name == "timeline_add_transition":
        return _transition_satisfied(request, {
            **facts,
            **call_args,
        })
    if operation_id == "subtitle":
        return _literal_content_satisfied(
            operation_id, request, call_args, facts
        )
    if operation_id == "voiceover" and tool_name == "narrate":
        expected = _requested_quoted_literal(
            request, r"旁白|口播|voiceover|narration"
        )
        if expected is None:
            return True
        actual = _first(call_args, ("text", "script", "content"))
        if actual is None:
            actual = _first(facts, ("text", "script", "content"))
        return " ".join(str(actual or "").split()) == " ".join(expected.split())
    if operation_id == "color" and _requests_monochrome(request):
        if tool_name != "adjust_media":
            return False
        saturation = _parse_number(
            call_args.get("saturation", facts.get("saturation"))
        )
        return saturation is not None and saturation <= 0.05
    if operation_id == "color":
        requested_look = _requested_color_look(request)
        if requested_look is not None:
            actual_look = str(
                call_args.get("look") or facts.get("look") or ""
            ).strip().lower()
            return tool_name == "color_grade" and actual_look == requested_look
        requested_params = _requested_color_parameters(request)
        if requested_params:
            if tool_name != "adjust_media":
                return False
            return all(
                (actual := _parse_number(
                    call_args.get(name, facts.get(name))
                )) is not None
                and abs(actual - expected) <= 0.01
                for name, expected in requested_params.items()
            )
    if tool_name == "edit_video":
        operation = str(call_args.get("operation") or facts.get("operation") or "").lower()
        if operation_id == "trim":
            return operation == "trim" and _trim_range_satisfied(
                request, call_args, facts
            )
        if operation_id == "retime":
            if operation == "reverse":
                return _requested_speed_factor(request) is None and bool(
                    re.search(r"(?:倒放|反向|reverse)", request, re.I)
                )
            return operation == "speed" and _retime_factor_satisfied(
                request,
                call_args,
                facts,
                field_names=("speed_factor", "speed"),
            )
    if operation_id == "retime" and tool_name == "lumen_retime_segment":
        return _retime_factor_satisfied(
            request,
            call_args,
            facts,
            field_names=("speed", "speed_factor"),
        )
    if operation_id == "trim" and tool_name == "timeline_trim_clip":
        return _trim_range_satisfied(request, call_args, facts)
    if operation_id == "split" and tool_name == "timeline_split_clip":
        return _timeline_time_satisfied(request, call_args, facts)
    if operation_id == "insert":
        return _timeline_time_satisfied(
            request, call_args, facts
        ) and _literal_content_satisfied(
            operation_id, request, call_args, facts
        ) and _insert_duration_satisfied(request, call_args, facts)
    if operation_id == "retime" and _requested_speed_factor(request) is not None:
        # A named factor is objective. Broad time-remap/ramp tools cannot claim
        # it without returning or receiving a matching scalar speed signal.
        return _retime_factor_satisfied(
            request,
            call_args,
            facts,
            field_names=("speed", "speed_factor"),
        )
    if operation_id == "color" and tool_name == "adjust_media":
        defaults = {
            "brightness": 0.0,
            "contrast": 1.0,
            "saturation": 1.0,
            "exposure": 0.0,
            "gamma": 1.0,
        }
        return any(
            name in call_args
            and _parse_number(call_args.get(name)) is not None
            and abs(float(call_args[name]) - default) > 1e-9
            for name, default in defaults.items()
        )
    return True

_TARGET_FIELDS = (
    "asset_id", "asset_ids", "source_asset_id", "input_asset_id",
    "overlay_asset_id", "layer_id", "clip_id", "track_id", "job_id",
    "task_id", "operation_id", "path", "source_path", "src", "url",
)


@dataclass
class LedgerStep:
    id: str
    description: str
    status: str = "open"
    required: bool = True
    evidence_ids: list[str] = field(default_factory=list)


@dataclass
class AcceptanceCriterion:
    id: str
    kind: str
    expected: Any
    target_kind: str | None = None
    match_mode: str = "all"
    actual: Any = None
    status: str = "open"
    evidence_ids: list[str] = field(default_factory=list)


@dataclass
class OutcomeRecord:
    seq: int
    call_id: str
    tool_name: str
    state: str
    ok: bool
    summary: str
    error_code: str | None
    artifacts: tuple[str, ...]
    target_key: str | None
    facts: dict[str, Any]
    call_args: dict[str, Any]
    mutation: bool
    verification: bool


@dataclass
class FailureRecord:
    call_id: str
    tool_name: str
    error_code: str
    summary: str
    seq: int
    job_id: str | None = None
    target_key: str | None = None
    blocking: bool = True


@dataclass(frozen=True)
class CompletionDecision:
    status: str
    blockers: tuple[str, ...]

    @property
    def complete(self) -> bool:
        return self.status == "complete"


def _read(value: Any, key: str, default: Any = _MISSING) -> Any:
    if isinstance(value, Mapping):
        return value.get(key, default)
    return getattr(value, key, default)


def _as_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if value is None:
        return {}
    try:
        return dict(vars(value))
    except (TypeError, AttributeError):
        return {}


def _coerce_bool(value: Any) -> bool | None:
    if value is _MISSING or value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "ok", "success", "succeeded", "applied"}:
        return True
    if text in {"0", "false", "no", "failed", "error", "cancelled", "canceled"}:
        return False
    return bool(value)


def _status(value: Any) -> str:
    if value is _MISSING or value is None:
        return ""
    return str(value).strip().lower()


def _first(mapping: Mapping[str, Any], names: Iterable[str]) -> Any:
    for name in names:
        value = mapping.get(name, _MISSING)
        if value is not _MISSING and value is not None:
            return value
    return None


def _flatten_facts(outcome: Any) -> dict[str, Any]:
    """Collect explicit facts plus current raw result/payload dictionaries."""
    root = _as_mapping(outcome)
    facts: dict[str, Any] = {}
    for nested_name in ("raw_payload", "payload", "result", "data", "facts", "metadata"):
        nested = _read(outcome, nested_name, _MISSING)
        if nested is _MISSING:
            nested = root.get(nested_name, _MISSING)
        if isinstance(nested, Mapping):
            facts.update(nested)
            metadata = nested.get("metadata")
            if isinstance(metadata, Mapping):
                facts.update(metadata)
    facts.update({
        key: value for key, value in root.items()
        if key not in {"raw_payload", "payload", "result", "data", "facts", "metadata", "artifacts"}
    })
    metadata = root.get("metadata")
    if isinstance(metadata, Mapping):
        facts.update(metadata)
    return facts


def _artifact_identifier(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, Mapping):
        raw = _first(value, ("asset_id", "artifact_id", "id", "path", "uri"))
        return str(raw).strip() if raw is not None and str(raw).strip() else None
    raw = _read(value, "asset_id", _MISSING)
    if raw is _MISSING:
        raw = _read(value, "artifact_id", _MISSING)
    if raw is _MISSING:
        raw = _read(value, "id", _MISSING)
    return str(raw).strip() if raw is not _MISSING and raw is not None else None


def _extract_artifacts(outcome: Any, facts: Mapping[str, Any], *, include_asset_id: bool) -> tuple[str, ...]:
    values: list[Any] = []
    explicit = _read(outcome, "artifacts", _MISSING)
    if explicit is not _MISSING and explicit is not None:
        if isinstance(explicit, Sequence) and not isinstance(explicit, (str, bytes)):
            values.extend(explicit)
        else:
            values.append(explicit)
    for key in ("final_asset_ids", "asset_ids"):
        raw = facts.get(key)
        if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
            values.extend(raw)
    for key in ("output_asset_id", "pending_asset_id", "artifact_id", "output_path"):
        if facts.get(key) is not None:
            values.append(facts[key])
    if include_asset_id and facts.get("asset_id") is not None:
        values.append(facts["asset_id"])
    result: list[str] = []
    for value in values:
        identifier = _artifact_identifier(value)
        if identifier and identifier not in result:
            result.append(identifier)
    return tuple(result)


def _has_error_value(value: Any) -> bool:
    if value is _MISSING or value is None or value is False:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, Mapping):
        return bool(value)
    return True


def _outcome_state(outcome: Any, facts: Mapping[str, Any]) -> str:
    if isinstance(outcome, ToolOutcome):
        return outcome.state
    explicit_state = _status(_read(outcome, "state", _MISSING))
    if explicit_state in {"success", "failure", "pending", "noop", "partial"}:
        return explicit_state
    # Compatibility for typed adapters that expose ok/status/facts but are not
    # the canonical ToolOutcome yet.
    explicit_ok = _coerce_bool(_read(outcome, "ok", _MISSING))
    if explicit_ok is not None and not isinstance(outcome, Mapping):
        status = _status(_read(outcome, "status", ""))
        if status in PENDING_JOB_STATES:
            return "pending"
        if status == "partial":
            return "partial"
        return "success" if explicit_ok else "failure"
    raw = outcome if isinstance(outcome, Mapping) else facts
    return classify_tool_result(raw).state


def _infer_ok(outcome: Any, facts: Mapping[str, Any]) -> bool:
    state = _outcome_state(outcome, facts)
    if state in {"success", "pending", "noop", "partial"}:
        return True
    if state == "failure":
        return False
    for name in ("ok", "success"):
        explicit = _coerce_bool(_read(outcome, name, _MISSING))
        if explicit is not None:
            return explicit
    applied = _coerce_bool(_read(outcome, "applied", _MISSING))
    if applied is False:
        return False
    status = _status(_read(outcome, "status", facts.get("status")))
    if status in FAILED_JOB_STATES:
        return False
    exit_code = _read(outcome, "exit_code", facts.get("exit_code", _MISSING))
    if exit_code is not _MISSING and exit_code is not None:
        try:
            if int(exit_code) != 0:
                return False
        except (TypeError, ValueError):
            pass
    error = _read(outcome, "error", facts.get("error", _MISSING))
    error_code = _read(outcome, "error_code", facts.get("error_code", _MISSING))
    return not (_has_error_value(error) or _has_error_value(error_code))


def _error_code(outcome: Any, facts: Mapping[str, Any], ok: bool) -> str | None:
    if ok:
        return None
    raw = _read(outcome, "error_code", facts.get("error_code", _MISSING))
    if _has_error_value(raw):
        return str(raw)
    error = _read(outcome, "error", facts.get("error", _MISSING))
    if isinstance(error, Mapping):
        raw = _first(error, ("code", "type", "message"))
        if raw:
            return str(raw)
    if _has_error_value(error):
        return "tool_error"
    status = _status(_read(outcome, "status", facts.get("status")))
    return status if status in FAILED_JOB_STATES else "unsuccessful_outcome"


def _summary(outcome: Any, facts: Mapping[str, Any], ok: bool) -> str:
    for name in ("summary", "message"):
        value = _read(outcome, name, _MISSING)
        if value is not _MISSING and value is not None and str(value):
            return str(value)
    error = _read(outcome, "error", facts.get("error", _MISSING))
    if _has_error_value(error):
        return str(error)
    return "tool succeeded" if ok else "tool failed"


def _parse_number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _normalize_format(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip().lower()
    if not text:
        return None
    suffix = PurePath(text).suffix.lower().lstrip(".")
    if suffix and len(suffix) <= 6:
        text = suffix
    text = text.lstrip(".").split(",", 1)[0].split()[0]
    aliases = {"jpeg": "jpg", "quicktime": "mov", "matroska": "mkv"}
    return aliases.get(text, text)


def _kind_for_format(value: Any) -> str | None:
    normalized = _normalize_format(value)
    if normalized in {"mp4", "mov", "mkv", "webm"}:
        return "video"
    if normalized in {"gif", "png", "jpg"}:
        return "image"
    if normalized in {"wav", "mp3", "aac", "flac"}:
        return "audio"
    return None


def tool_target_key(value: Mapping[str, Any] | None) -> str | None:
    """Return a stable identity for the object a tool call operates on.

    The key deliberately ignores creative/options arguments so a corrected
    retry against the same asset resolves the earlier failure, while success on
    asset B cannot erase an unresolved failure on asset A.
    """
    if not isinstance(value, Mapping):
        return None
    parts: list[str] = []
    for name in _TARGET_FIELDS:
        raw = value.get(name)
        if raw is None or raw == "" or raw == []:
            continue
        if isinstance(raw, (Mapping, list, tuple, set, frozenset)):
            rendered = json.dumps(raw, ensure_ascii=False, sort_keys=True, default=str)
        else:
            rendered = str(raw).strip()
        if rendered:
            parts.append(f"{name}={rendered}")
    return "|".join(parts) or None


def _infer_artifact_kind(
    artifact_id: str,
    facts: Mapping[str, Any],
    tool_name: str,
    provided: Mapping[str, str] | None,
) -> str | None:
    if provided and artifact_id in provided:
        return str(provided[artifact_id]).strip().lower() or None
    for name in ("media_kind", "output_kind", "asset_kind", "kind"):
        raw = facts.get(name)
        if str(raw).strip().lower() in {"video", "image", "audio", "lottie", "otio"}:
            return str(raw).strip().lower()
    for name in ("output_path", "path", "filename"):
        value = facts.get(name)
        if value:
            suffix = PurePath(str(value)).suffix.lower()
            if suffix in {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}:
                return "video"
            if suffix in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}:
                return "image"
            if suffix in {".wav", ".mp3", ".aac", ".flac", ".ogg", ".m4a"}:
                return "audio"
    lowered = artifact_id.lower()
    if re.match(r"^v(?:[_-]|\d)", lowered):
        return "video"
    if re.match(r"^(?:img|image)(?:[_-]|\d)", lowered):
        return "image"
    if re.match(r"^(?:aud|audio)(?:[_-]|\d)", lowered):
        return "audio"
    return _TOOL_OUTPUT_KIND.get(tool_name)


def _explicitly_intermediate_output(
    tool_name: str,
    call_args: Mapping[str, Any],
    facts: Mapping[str, Any],
) -> bool:
    role = str(
        call_args.get("output_role")
        or call_args.get("role")
        or facts.get("output_role")
        or facts.get("role")
        or ""
    ).strip().lower()
    if role in {"reference", "intermediate", "support", "source"}:
        return True
    for field in ("final", "is_final", "deliverable"):
        if field in facts and _coerce_bool(facts.get(field)) is False:
            return True
    if tool_name != "generate_image":
        return False
    text = " ".join(str(call_args.get(name) or "") for name in ("prompt", "purpose"))
    return bool(re.search(
        r"(?:internal\s+(?:style\s+)?reference|reference\s+(?:image\s+)?only|"
        r"not\s+(?:a\s+)?cover|仅(?:作|供)参考|内部参考|参考素材(?:而非|，不是)封面)",
        text,
        re.I,
    ))


_COUNT_WORDS = {
    "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}

_COUNT_TOKEN_PATTERN = (
    r"\d+|[一二两三四五六七八九十]|"
    r"one|two|three|four|five|six|seven|eight|nine|ten"
)


_OUTPUT_VERB_RE = re.compile(
    r"(?:生成|制作|创建|产出|输出|导出|给我|做|渲染|"
    r"(?:generate|create|make|produce|export|render)\b)",
    re.I,
)

_OUTPUT_NOUN_TO_KIND = {
    "图片": "image", "图片方案": "image", "图像": "image",
    "图像方案": "image", "海报": "image", "海报方案": "image", "照片": "image",
    "封面图": "image", "封面图片": "image",
    "视频": "video", "短视频": "video", "短片": "video", "成片": "video",
    "音频": "audio", "音乐": "audio", "配乐": "audio",
    "旁白": "audio", "口播": "audio",
    "image": "image", "images": "image", "photo": "image",
    "photos": "image", "poster": "image", "posters": "image",
    "cover image": "image", "cover art": "image",
    "video": "video", "videos": "video", "clip": "video", "clips": "video",
    "audio": "audio", "audio track": "audio", "audio tracks": "audio",
    "music": "audio", "voiceover": "audio", "narration": "audio",
}

_OUTPUT_NOUN_PATTERN = (
    r"图片方案|图像方案|海报方案|封面图片|封面图|图片|图像|海报|照片|"
    r"短视频|视频|短片|成片|音频|音乐|配乐|旁白|口播|"
    r"cover\s+image|cover\s+art|audio\s+tracks?|images?|photos?|posters?|"
    r"videos?|clips?|audio|music|voiceover|narration"
)


def _count_value(raw: str) -> int | None:
    normalized = raw.lower()
    return int(normalized) if normalized.isdigit() else _COUNT_WORDS.get(normalized)


def _output_scope(text: str) -> str:
    match = _OUTPUT_VERB_RE.search(text)
    if not match:
        return ""
    # A new sentence normally starts a new request. Keep the scan bounded so
    # source counts in later explanatory prose cannot become deliverable counts.
    scope = re.split(r"[。！？!?;；\n]", text[match.start():], maxsplit=1)[0][:160]
    return re.split(
        r"[,，]?\s*(?:使用|参考|素材(?:包括|包含|含有)?|基于|"
        r"using\b|from\b|based\s+on\b|with\b)",
        scope,
        maxsplit=1,
        flags=re.I,
    )[0]


def _extract_requested_asset_counts(text: str) -> dict[str, int]:
    """Return explicit output counts by kind, excluding source-media counts."""
    scope = _output_scope(text)
    if not scope:
        return {}
    pattern = re.compile(
        r"(?:^|(?:以及|和|及|、|还有|外加|附带|另配|并配|and|plus)\s*|"
        r"并(?:再)?(?:生成|制作|创建|输出|导出)?\s*)"
        r"(?:(?:生成|制作|创建|产出|输出|导出|给我|做|渲染|"
        r"generate|create|make|produce|export|render)\s*)?"
        r"(?:出|成)?\s*"
        rf"(?<![\d.x×*:：])(?P<count>{_COUNT_TOKEN_PATTERN})(?![\d.])\s*"
        r"(?:张|幅|个|段|条|版|套|种|款)?\s*"
        r"(?:不同(?:(?:风格|版本|样式|款式))?的?|"
        r"各(?:种|类)?|候选(?:版本)?的?|版本(?:的)?|"
        r"different\s+(?:styles?|versions?)\s+of\s+(?:an?\s+)?|"
        r"(?:short|vertical|portrait|landscape)\s+|"
        r"\d{3,4}p\s+)?\s*"
        r"(?:(?:有|带|带有|配有|包含|含有).{0,12}?的\s*)?"
        r"(?:(?:各?\s*\d+(?:\.\d+)?\s*(?:秒|fps|帧|s\b)|"
        r"\d+(?:\.\d+)?\s*[- ]\s*seconds?|"
        r"\d{2,5}\s*[x×*]\s*\d{2,5}|"
        r"\d{1,2}\s*[:：]\s*\d{1,2}|\d{3,4}p|"
        r"竖屏|横屏|竖版|横版)\s*(?:的)?\s*){0,4}"
        rf"(?P<noun>{_OUTPUT_NOUN_PATTERN})",
        re.I,
    )
    counts: dict[str, int] = {}
    for match in pattern.finditer(scope):
        leading = scope[max(0, match.start() - 20):match.start()]
        trailing = scope[match.end():match.end() + 18]
        if re.search(
            r"(?:使用|参考|素材(?:包括|包含|含有)?|基于|用)\s*$",
            leading,
        ):
            continue
        if re.match(
            r"\s*(?:作为|用作|用于)?\s*(?:参考|输入|源)(?:图片|图像|素材|文件)?",
            trailing,
        ):
            continue
        value = _count_value(match.group("count"))
        kind = _OUTPUT_NOUN_TO_KIND.get(match.group("noun").lower())
        if value is None or value <= 0 or kind is None:
            continue
        counts[kind] = max(counts.get(kind, 0), value)
    return counts


def _extract_explicit_deliverable_kinds(text: str) -> frozenset[str]:
    """Find output roles without treating pre-verb source nouns as outputs."""
    scope = _output_scope(text)
    kinds = set(_extract_requested_asset_counts(text))
    if not scope:
        return frozenset(kinds)
    direct = re.compile(
        r"(?:生成|制作|创建|产出|输出|导出|给我|做|渲染|"
        r"generate|create|make|produce|export|render)\s*"
        rf"(?:出|成)?\s*(?:a|an|{_COUNT_TOKEN_PATTERN})?\s*"
        r"(?:张|幅|个|段|条|版|套|种|款)?\s*"
        r"(?:(?:有|带|带有|配有|包含|含有).{0,12}?的\s*)?"
        r"(?:(?:\d+(?:\.\d+)?\s*(?:秒|fps|帧)|"
        r"\d+(?:\.\d+)?\s*[- ]\s*seconds?|"
        r"\d{2,5}\s*[x×*]\s*\d{2,5}|竖屏|横屏)\s*[、，,]?\s*){0,4}"
        rf"(?P<noun>{_OUTPUT_NOUN_PATTERN})",
        re.I,
    )
    continuation = re.compile(
        r"(?:和|及|以及|、|还有|外加|附带|另配|并配|and|plus|"
        r"并(?:再)?(?:生成|制作|创建|输出|导出)?)\s*"
        rf"(?:a|an|{_COUNT_TOKEN_PATTERN})?\s*"
        r"(?:张|幅|个|段|条|版|套|种|款)?\s*"
        r"(?:(?:\d+(?:\.\d+)?\s*(?:秒|fps|帧)|"
        r"\d+(?:\.\d+)?\s*[- ]\s*seconds?|"
        r"\d{2,5}\s*[x×*]\s*\d{2,5}|竖屏|横屏)\s*[、，,]?\s*){0,4}"
        rf"(?P<noun>{_OUTPUT_NOUN_PATTERN})",
        re.I,
    )
    for pattern in (direct, continuation):
        for match in pattern.finditer(scope):
            kind = _OUTPUT_NOUN_TO_KIND.get(match.group("noun").lower())
            if kind:
                kinds.add(kind)
    if re.search(
        r"(?:音乐|音频|声音|电影)(?:(?!和|及|、|另配|附带).){0,10}"
        r"(?:海报|封面图|封面图片|图片|图像|照片)",
        scope,
    ):
        # Theme compounds such as "音乐节海报" and "无声电影海报"
        # describe one visual deliverable, not an audio/video side product.
        kinds.discard("audio")
        kinds.discard("video")
        kinds.add("image")
    return frozenset(kinds)


def _targets_video_output(text: str) -> bool:
    patterns = (
        r"(?:给|为).{0,10}视频.{0,12}(?:加|添加|加入|配上|混入)"
        r".{0,8}(?:音乐|配乐|旁白|口播|音频)",
        r"(?:把|将).{0,12}(?:音乐|配乐|旁白|口播|音频)"
        r".{0,10}(?:加到|混到|加入|放进).{0,6}视频",
        r"(?:add|mix|attach).{0,16}(?:music|audio|voiceover|narration)"
        r".{0,12}(?:to|into).{0,6}(?:the\s+)?video",
        r"(?:把|将).{0,12}(?:图片|图像|照片).{0,12}"
        r"(?:叠加|覆盖|加到|放到).{0,6}视频(?:上|里)?",
        r"(?:add|place|overlay).{0,16}(?:image|photo).{0,16}"
        r"(?:overlay\s+)?(?:to|onto|on).{0,6}(?:the\s+)?video",
        r"(?:add|place|overlay).{0,20}(?:logo|graphic).{0,16}"
        r"(?:to|onto|on).{0,6}(?:the\s+)?video",
        r"(?:给|为).{0,10}视频.{0,12}(?:加|添加|加入|叠加|覆盖)"
        r".{0,20}(?:logo|标志|图标|图片|图像)",
        r"视频.{0,12}(?:插入|添加|加入|叠加|覆盖).{0,20}"
        r"(?:logo|标志|图标|图片|图像|照片)",
    )
    return any(re.search(pattern, text, re.I) for pattern in patterns)


def _has_overlay_source_dimensions(text: str) -> bool:
    return bool(re.search(
        r"(?:(?:添加|插入|叠加|覆盖|add|insert|place|overlay).{0,20})"
        r"\d{2,5}\s*[x×*]\s*\d{2,5}.{0,16}"
        r"(?:logo|标志|图标|图片|图像|照片|image|photo|graphic)",
        text,
        re.I,
    ))


def _deliverable_clauses(text: str) -> tuple[tuple[str, str], ...]:
    """Split a compound output request into ``(kind, clause)`` pairs."""
    scope = _output_scope(text)
    if not scope:
        return ()
    parts = re.split(
        r"\s*(?:以及|和|及|、|还有|外加|附带|另配|并配|and|plus|"
        r"并(?:再)?(?:生成|制作|创建|输出|导出)?)\s*",
        scope,
    )
    clauses: list[tuple[str, str]] = []
    noun_re = re.compile(_OUTPUT_NOUN_PATTERN, re.I)
    for part in parts:
        nouns = list(noun_re.finditer(part))
        if not nouns:
            continue
        kind = _OUTPUT_NOUN_TO_KIND.get(nouns[-1].group(0).lower())
        if kind:
            clauses.append((kind, part))
    return tuple(clauses)


def _extract_requested_asset_count(text: str) -> int | None:
    """Extract explicit output multiplicity, excluding source-clip counts."""
    counts = _extract_requested_asset_counts(text)
    if len(counts) == 1:
        return next(iter(counts.values()))
    return None


def _call_asset_ids(call_args: Mapping[str, Any]) -> set[str]:
    values: list[Any] = []
    for name in (
        "asset_id", "source_asset_id", "input_asset_id", "base_asset_id",
    ):
        if call_args.get(name):
            values.append(call_args[name])
    for name in ("asset_ids",):
        raw_many = call_args.get(name)
        if isinstance(raw_many, Sequence) and not isinstance(raw_many, (str, bytes)):
            values.extend(raw_many)
    return {str(value) for value in values if str(value).strip()}


def _inserted_asset_ids(call_args: Mapping[str, Any]) -> set[str]:
    values = _call_asset_ids(call_args)
    for name in (
        "audio_asset_id", "voiceover_asset_id", "overlay_asset_id", "clip_asset_id",
    ):
        value = call_args.get(name)
        if value is not None and str(value).strip():
            values.add(str(value))
    return values


def _last_match(pattern: str, text: str, flags: int = 0) -> re.Match[str] | None:
    matches = list(re.finditer(pattern, text, flags))
    return matches[-1] if matches else None


def _goal_duration_matches(text: str) -> list[re.Match[str]]:
    matches = list(re.finditer(
        r"(?<![\d.])(\d+(?:\.\d+)?)\s*(?:[- ]\s*)?"
        r"(?:秒|seconds?\b|secs?\b|s\b)",
        text,
    ))
    candidates: list[re.Match[str]] = []
    for match in matches:
        before = text[max(0, match.start() - 28):match.start()]
        after = text[match.end():match.end() + 12]
        surrounding = text[max(0, match.start() - 12):match.end() + 12]
        if re.search(r"(?:第|at)\s*$", before, re.I):
            continue
        if re.search(
            r"(?:剪掉|裁掉|删除|remove|cut\s+off).{0,20}$",
            before,
            re.I,
        ):
            continue
        if re.search(
            r"(?:插入|添加|加入|insert|add).{0,16}"
            r"(?:持续|显示|for)\s*$",
            before,
            re.I,
        ) and re.match(
            r"\s*(?:的\s*)?(?:标题|文字|title|text)",
            after,
            re.I,
        ):
            continue
        if re.match(r"\s*(?:处|位置|时刻|开始|结束|插入|拆分)", after):
            continue
        if re.search(
            r"(?:转场|溶解|擦除|划像|淡入淡出|transition|dissolve|wipe|fade)",
            surrounding,
            re.I,
        ):
            continue
        operation_context = text[max(0, match.start() - 48):match.end() + 48]
        if re.search(
            r"(?:字幕|标题|文字|caption|subtitles?|title|text).{0,20}"
            r"(?:持续|显示|for)\s*$",
            before,
            re.I,
        ) or re.search(
            r"(?:淡入|淡出|fade\s*in|fade\s*out).{0,8}$",
            before,
            re.I,
        ):
            continue
        if re.search(
            r"(?:插入|添加|加入|insert|add).{0,24}"
            r"(?:标题|文字|title|text).{0,24}"
            r"(?:持续|显示|for)\s*\d+(?:\.\d+)?\s*"
            r"(?:秒|seconds?|secs?|s\b)",
            operation_context,
            re.I,
        ):
            continue
        scoped_range = re.search(
            r"(?:从|在|from)?\s*\d+(?:\.\d+)?\s*"
            r"(?:秒|seconds?|secs?|s\b)\s*(?:到|至|to|[-–—~～])\s*"
            r"\d+(?:\.\d+)?\s*(?:秒|seconds?|secs?|s\b)"
            r".{0,24}(?:添加|加入|加|应用|add|apply).{0,12}"
            r"(?:字幕|蒙版|标题|caption|subtitles?|mask|title)",
            operation_context,
            re.I,
        )
        if scoped_range:
            continue
        candidates.append(match)
    return candidates


def _last_goal_duration_match(text: str) -> re.Match[str] | None:
    candidates = _goal_duration_matches(text)
    return candidates[-1] if candidates else None


def _dedupe_values(values: Iterable[Any]) -> list[Any]:
    unique: list[Any] = []
    for value in values:
        if value not in unique:
            unique.append(value)
    return unique


def _repeated_slot_specs(text: str) -> dict[str, list[Any]]:
    trim_range = _requested_trim_range(text)
    durations = (
        [trim_range[1] - trim_range[0]]
        if trim_range is not None
        else [float(match.group(1)) for match in _goal_duration_matches(text)]
    )
    dimensions = [
        (int(match.group(1)), int(match.group(2)))
        for match in re.finditer(
            r"(?<!\d)(\d{2,5})\s*[x×*]\s*(\d{2,5})(?!\d)", text
        )
    ]
    fps_values = [
        float(match.group(1))
        for match in re.finditer(
            r"(?<![\d.])(\d+(?:\.\d+)?)\s*(?:fps\b|帧(?:每秒)?\b)",
            text,
        )
    ]
    allowed_aspects = {(16, 9), (9, 16), (1, 1), (4, 3), (3, 4), (4, 5), (21, 9)}
    aspects = [
        candidate
        for match in re.finditer(
            r"(?<!\d)(\d{1,2})\s*[:：]\s*(\d{1,2})(?!\d)", text
        )
        if (candidate := (int(match.group(1)), int(match.group(2)))) in allowed_aspects
    ]
    formats = [
        normalized
        for match in re.finditer(
            r"(?<![\w.])(mp4|mov|mkv|webm|gif|png|jpe?g|wav|mp3|aac|flac|otioz?)(?!\w)",
            text,
            re.I,
        )
        if (normalized := _normalize_format(match.group(1))) is not None
    ]
    return {
        "duration": _dedupe_values(durations),
        "dimensions": _dedupe_values(dimensions),
        "fps": _dedupe_values(fps_values),
        "aspect": _dedupe_values(aspects),
        "format": _dedupe_values(formats),
    }


def _same_kind_deliverable_slots(
    text: str,
    *,
    target_kind: str,
    requested_count: int,
) -> list[dict[str, Any]]:
    if requested_count <= 1:
        return []
    parts = re.split(r"\s*(?:[，,]|(?<!\d)[:：](?!\d))\s*", text)
    slots: list[dict[str, Any]] = []
    for part in parts:
        local = extract_acceptance_criteria(
            part, _scope_deliverables=False
        )
        spec: dict[str, Any] = {}
        for base_id in ("duration", "dimensions", "fps", "aspect", "format"):
            criterion = local.get(base_id)
            if criterion is not None:
                spec[criterion.kind] = criterion.expected
        if spec:
            slots.append(spec)
    if len(slots) < 2 or len(slots) > requested_count:
        return []
    if len({repr(sorted(slot.items())) for slot in slots}) <= 1:
        return []
    return slots


def extract_acceptance_criteria(
    request: str, *, _scope_deliverables: bool = True
) -> dict[str, AcceptanceCriterion]:
    """Extract only deterministic, probe-evaluable media requirements."""
    text = str(request or "")
    lowered = text.lower()
    criteria: dict[str, AcceptanceCriterion] = {}

    asset_counts = _extract_requested_asset_counts(lowered)
    explicit_deliverable_kinds = _extract_explicit_deliverable_kinds(lowered)
    if len(asset_counts) == 1 and len(explicit_deliverable_kinds) <= 1:
        asset_count = next(iter(asset_counts.values()))
        if asset_count > 1:
            criteria["asset_count"] = AcceptanceCriterion(
                "asset_count", "asset_count", asset_count
            )
    elif asset_counts:
        for kind, count in sorted(asset_counts.items()):
            if count > 1:
                criterion_id = f"asset_count:{kind}"
                criteria[criterion_id] = AcceptanceCriterion(
                    criterion_id, criterion_id, count
                )

    trim_range = _requested_trim_range(lowered)
    range_duration = (
        trim_range[1] - trim_range[0] if trim_range is not None else None
    )
    duration = None if range_duration is not None else _last_goal_duration_match(lowered)
    if range_duration is not None or duration:
        criteria["duration"] = AcceptanceCriterion(
            "duration",
            "duration_sec",
            range_duration if range_duration is not None else float(duration.group(1)),
        )

    dimensions = _last_match(
        r"(?<!\d)(\d{2,5})\s*[x×*]\s*(\d{2,5})(?!\d)", lowered
    )
    if dimensions:
        criteria["dimensions"] = AcceptanceCriterion(
            "dimensions", "dimensions",
            (int(dimensions.group(1)), int(dimensions.group(2))),
        )

    fps = _last_match(
        r"(?<![\d.])(\d+(?:\.\d+)?)\s*(?:fps\b|帧(?:每秒)?\b)", lowered
    )
    if not fps:
        fps = _last_match(r"(?:帧率)\s*[:：]?\s*(\d+(?:\.\d+)?)", lowered)
    if fps:
        criteria["fps"] = AcceptanceCriterion("fps", "fps", float(fps.group(1)))

    aspect_matches = list(re.finditer(
        r"(?<!\d)(\d{1,2})\s*[:：]\s*(\d{1,2})(?!\d)", lowered
    ))
    allowed_aspects = {(16, 9), (9, 16), (1, 1), (4, 3), (3, 4), (4, 5), (21, 9)}
    aspect_pair: tuple[int, int] | None = None
    for aspect in reversed(aspect_matches):
        candidate = (int(aspect.group(1)), int(aspect.group(2)))
        if candidate in allowed_aspects:
            aspect_pair = candidate
            criteria["aspect"] = AcceptanceCriterion("aspect", "aspect", candidate)
            break
    if aspect_pair is None:
        if re.search(r"(?:竖屏|竖版|纵向|portrait\b|vertical\b)", lowered):
            aspect_pair = (9, 16)
            criteria["aspect"] = AcceptanceCriterion("aspect", "aspect", aspect_pair)
        elif re.search(r"(?:横屏|横版|横向|landscape\b|horizontal\b)", lowered):
            aspect_pair = (16, 9)
            criteria["aspect"] = AcceptanceCriterion("aspect", "aspect", aspect_pair)

    if "dimensions" not in criteria:
        preset = _last_match(
            r"(?<!\w)(4k|2160p|1440p|1080p|720p)(?!\w)", lowered
        )
        if preset:
            landscape = {
                "4k": (3840, 2160), "2160p": (3840, 2160),
                "1440p": (2560, 1440), "1080p": (1920, 1080),
                "720p": (1280, 720),
            }[preset.group(1)]
            if aspect_pair and aspect_pair[0] < aspect_pair[1]:
                landscape = (landscape[1], landscape[0])
            criteria["dimensions"] = AcceptanceCriterion(
                "dimensions", "dimensions", landscape
            )

    format_match = _last_match(
        r"(?<![\w.])(mp4|mov|mkv|webm|gif|png|jpe?g|wav|mp3|aac|flac|otioz?)(?!\w)",
        lowered,
    )
    if format_match:
        criteria["format"] = AcceptanceCriterion(
            "format", "format", _normalize_format(format_match.group(1))
        )

    negative_audio = re.search(
        r"(?:静音|无声|不要(?:声音|音频|音乐|配乐)|无音频|no\s+audio|without\s+audio|mute[ds]?)",
        lowered,
    )
    positive_audio = re.search(
        r"(?:带(?:声音|音频|音乐|配乐)|有(?:声音|音频|音乐|配乐)|配乐|音乐|旁白|口播|"
        r"with\s+audio|music|voiceover|narration)",
        lowered,
    )
    constrains_video_audio = (
        "video" in explicit_deliverable_kinds
        or bool(re.search(r"(?:视频|短片|成片|video\b|clip\b)", lowered))
    )
    if negative_audio and constrains_video_audio:
        criteria["audio"] = AcceptanceCriterion("audio", "has_audio", False)
    elif positive_audio and constrains_video_audio:
        criteria["audio"] = AcceptanceCriterion("audio", "has_audio", True)

    if _scope_deliverables and len(explicit_deliverable_kinds) > 1:
        scoped_bases: set[str] = set()
        scoped: dict[str, AcceptanceCriterion] = {}
        for target_kind, clause in _deliverable_clauses(lowered):
            local = extract_acceptance_criteria(
                clause, _scope_deliverables=False
            )
            for base_id in ("duration", "dimensions", "fps", "aspect", "format"):
                local_criterion = local.get(base_id)
                if local_criterion is None:
                    continue
                criterion_id = f"{base_id}:{target_kind}"
                scoped[criterion_id] = AcceptanceCriterion(
                    criterion_id,
                    local_criterion.kind,
                    local_criterion.expected,
                    target_kind=target_kind,
                )
                scoped_bases.add(base_id)
        for base_id in scoped_bases:
            criteria.pop(base_id, None)
        criteria.update(scoped)

    if _scope_deliverables and len(explicit_deliverable_kinds) == 1:
        target_kind = next(iter(explicit_deliverable_kinds))
        requested_count = asset_counts.get(target_kind, 1)
        if requested_count > 1:
            slots = _same_kind_deliverable_slots(
                lowered,
                target_kind=target_kind,
                requested_count=requested_count,
            )
            if slots:
                for base_id in ("duration", "dimensions", "fps", "aspect", "format"):
                    criteria.pop(base_id, None)
                for index, expected in enumerate(slots, start=1):
                    criterion_id = f"asset_slot:{target_kind}:{index}"
                    criteria[criterion_id] = AcceptanceCriterion(
                        criterion_id,
                        "asset_slot",
                        expected,
                        target_kind=target_kind,
                        match_mode="slot",
                    )
                return criteria
            criterion_kinds = {
                "duration": "duration_sec",
                "dimensions": "dimensions",
                "fps": "fps",
                "aspect": "aspect",
                "format": "format",
            }
            for base_id, values in _repeated_slot_specs(lowered).items():
                if len(values) <= 1:
                    continue
                criteria.pop(base_id, None)
                for index, expected in enumerate(values, start=1):
                    criterion_id = f"{base_id}:{target_kind}:slot{index}"
                    criteria[criterion_id] = AcceptanceCriterion(
                        criterion_id,
                        criterion_kinds[base_id],
                        expected,
                        target_kind=target_kind,
                        match_mode="one",
                    )

    return criteria


def _workflow_steps(workflow: str, request: str) -> dict[str, LedgerStep]:
    if workflow == "conversation":
        return {}
    if workflow in _READ_ONLY_WORKFLOWS:
        return {"inspect": LedgerStep("inspect", "Inspect the requested source")}
    if workflow == "files" and re.search(
        r"(?:读取|查看|列出|检查|read\b|list\b|inspect\b)", request, re.I
    ) and not re.search(
        r"(?:写入|创建|复制|移动|删除|整理|write\b|copy\b|move\b|delete\b|organize\b)",
        request,
        re.I,
    ):
        return {"inspect": LedgerStep("inspect", "Read or inspect the requested files")}
    if workflow == "annotations" and re.search(
        r"(?:搜索|查询|查看|获取|search\b|get\b|list\b)", request, re.I
    ) and not re.search(
        r"(?:添加|写入|标注|更新|add\b|write\b|annotate\b|update\b)", request, re.I
    ):
        return {"inspect": LedgerStep("inspect", "Inspect the requested annotations")}
    if workflow == "memory_skills" and re.search(
        r"(?:回忆|查询|读取|recall\b|read\b)", request, re.I
    ):
        return {"inspect": LedgerStep("inspect", "Recall the requested memory")}
    if workflow in {"timeline", "lumen_core", "lumen_time", "lumen_mask"} and re.search(
        r"(?:读取|查看|检查|获取|列出|read\b|show\b|inspect\b|get\b|list\b)",
        request,
        re.I,
    ) and not re.search(
        r"(?:插入|添加|移动|删除|拆分|裁剪|设置|调整|修改|倒放|变速|渲染|导出|"
        r"insert\b|add\b|move\b|delete\b|split\b|trim\b|set\b|edit\b|"
        r"change\b|retime\b|render\b|export\b)",
        request,
        re.I,
    ):
        return {"inspect": LedgerStep("inspect", "Inspect the current project state")}
    if workflow == "storyboard":
        steps = {
            "plan": LedgerStep("plan", "Create or update the shot plan"),
            "assemble": LedgerStep("assemble", "Assemble the planned sequence"),
            "verify": LedgerStep("verify", "Review the assembled sequence"),
        }
    elif workflow == "timeline":
        steps = {
            "mutate": LedgerStep("mutate", "Apply the requested timeline mutation"),
            "verify": LedgerStep("verify", "Review the resulting timeline"),
        }
    elif workflow in _ASSET_WORKFLOWS:
        steps = {
            "produce": LedgerStep("produce", "Produce the requested final asset"),
            "verify": LedgerStep("verify", "Verify the final asset after its last mutation"),
        }
    elif workflow.startswith("lumen_"):
        steps = {
            "mutate": LedgerStep("mutate", "Apply the requested composition mutation"),
            "verify": LedgerStep("verify", "Review the resulting composition"),
        }
    else:
        steps = {"act": LedgerStep("act", "Perform the requested action")}

    if re.search(r"(?:导出|交付|export|render\s+final|成片文件)", request, re.I):
        steps["deliver"] = LedgerStep("deliver", "Export the requested deliverable")
    steps.update(_requested_operation_steps(request))
    return steps


@dataclass
class TurnLedger:
    current_request: str
    workflow: str | None = None
    session_origin: str | None = None
    workflows: Sequence[str] | None = None
    required_final_kinds: frozenset[str] = field(
        init=False, default_factory=frozenset
    )
    steps: dict[str, LedgerStep] = field(default_factory=dict)
    criteria: dict[str, AcceptanceCriterion] = field(default_factory=dict)
    outcomes: list[OutcomeRecord] = field(default_factory=list)
    sequence: int = 0
    last_mutation_seq: int = 0
    last_verification_seq: int = 0
    final_asset_ids: list[str] = field(default_factory=list)
    final_asset_kinds: dict[str, str] = field(default_factory=dict)
    verified_final_asset_ids: list[str] = field(default_factory=list)
    criterion_asset_results: dict[str, dict[str, Any]] = field(default_factory=dict)
    pending_jobs: dict[str, str] = field(default_factory=dict)
    unresolved_failures: dict[str, FailureRecord] = field(default_factory=dict)
    superseded_failure_ids: list[str] = field(default_factory=list)
    compact_history: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.current_request = str(self.current_request or "")
        if isinstance(self.workflow, Sequence) and not isinstance(self.workflow, str):
            if self.workflows is None:
                self.workflows = tuple(str(item) for item in self.workflow if str(item))
            self.workflow = str(self.workflow[0]) if self.workflow else None
        routed_workflows: tuple[str, ...] = ()
        if not self.workflow:
            decision = classify_request(self.current_request)
            routed_workflows = decision.workflows
            self.workflow = decision.primary_workflow
        self.workflow = str(self.workflow)
        supplied_workflows = (
            self.workflows
            if self.workflows is not None
            else routed_workflows or (self.workflow,)
        )
        normalized_workflows = tuple(
            dict.fromkeys(
                str(item)
                for item in supplied_workflows
                if str(item) and str(item) != "conversation"
            )
        )
        if self.workflow == "conversation":
            self.workflows = ("conversation",)
        elif self.workflow not in normalized_workflows:
            self.workflows = (self.workflow, *normalized_workflows)
        else:
            self.workflows = normalized_workflows
        explicit_deliverable_kinds = _extract_explicit_deliverable_kinds(
            self.current_request
        )
        self.required_final_kinds = (
            explicit_deliverable_kinds
            if explicit_deliverable_kinds
            else frozenset(_EXPECTED_FINAL_KINDS.get(self.workflow, frozenset()))
        )
        if _targets_video_output(self.current_request):
            self.required_final_kinds = frozenset({"video"})
        if not self.steps:
            self.steps = _workflow_steps(self.workflow, self.current_request)
        if not self.criteria:
            self.criteria = extract_acceptance_criteria(self.current_request)
        format_criterion = self.criteria.get("format")
        format_kind = (
            _kind_for_format(format_criterion.expected)
            if format_criterion is not None
            else None
        )
        targets_video = _targets_video_output(self.current_request)
        if targets_video:
            self.required_final_kinds = frozenset({"video"})
            if format_kind not in {None, "video"}:
                self.criteria.pop("format", None)
            if _has_overlay_source_dimensions(self.current_request):
                self.criteria.pop("dimensions", None)
        elif format_kind and len(self.required_final_kinds) <= 1:
            self.required_final_kinds = frozenset({format_kind})

    @property
    def requires_final_asset(self) -> bool:
        return bool(self.required_final_kinds)

    @property
    def requires_visual_verification(self) -> bool:
        return bool(self.required_final_kinds & {"image", "video", "lottie"})

    @property
    def expected_final_kinds(self) -> frozenset[str]:
        return self.required_final_kinds

    @property
    def final_asset_kind_set(self) -> frozenset[str]:
        return frozenset(
            kind
            for asset_id, kind in self.final_asset_kinds.items()
            if asset_id in self.final_asset_ids
        )

    @property
    def visual_final_asset_ids(self) -> tuple[str, ...]:
        return tuple(
            asset_id
            for asset_id in self.final_asset_ids
            if self.final_asset_kinds.get(asset_id) in {"image", "video", "lottie"}
        )

    @property
    def required_final_asset_count(self) -> int:
        criterion = self.criteria.get("asset_count")
        return int(criterion.expected) if criterion is not None else 1

    def required_final_count_for_kind(self, kind: str | None) -> int:
        normalized = str(kind or "").strip().lower()
        criterion = self.criteria.get(f"asset_count:{normalized}")
        if criterion is not None:
            return int(criterion.expected)
        global_criterion = self.criteria.get("asset_count")
        if global_criterion is not None and len(self.expected_final_kinds) == 1:
            return int(global_criterion.expected)
        return 1

    def mark_step(self, step_id: str, status: str = "done", evidence_id: str | None = None) -> None:
        if step_id not in self.steps:
            raise KeyError(f"unknown ledger step: {step_id}")
        step = self.steps[step_id]
        step.status = str(status)
        if evidence_id and evidence_id not in step.evidence_ids:
            step.evidence_ids.append(evidence_id)

    def note_final_asset(self, asset_id: str, *, kind: str | None = None) -> bool:
        value = str(asset_id).strip()
        normalized_kind = str(kind or "").strip().lower() or None
        if self.expected_final_kinds and normalized_kind not in self.expected_final_kinds:
            return False
        if value and value not in self.final_asset_ids:
            self.final_asset_ids.append(value)
        if value and normalized_kind:
            self.final_asset_kinds[value] = normalized_kind
        self._refresh_asset_count_criterion()
        return bool(value)

    def progress_signature(self) -> tuple[Any, ...]:
        """State transitions that count as objective progress for tool routing."""
        return (
            tuple((name, step.status == "done") for name, step in self.steps.items()),
            tuple((name, item.status, repr(item.actual)) for name, item in self.criteria.items()),
            tuple(self.final_asset_ids),
            tuple(self.verified_final_asset_ids),
            tuple(sorted(self.pending_jobs.items())),
            self.last_mutation_seq,
            self.last_verification_seq > self.last_mutation_seq,
        )

    def resolve_failure(self, call_id: str) -> bool:
        return self.unresolved_failures.pop(str(call_id), None) is not None

    def record_outcome(
        self,
        tool_name: str,
        outcome: Any,
        *,
        call_id: str | None = None,
        mutation: bool | None = None,
        verification: bool | None = None,
        target_key: str | None = None,
        artifact_kinds: Mapping[str, str] | None = None,
        call_args: Mapping[str, Any] | None = None,
        blocking_failure: bool = True,
    ) -> OutcomeRecord:
        """Record either a future ToolOutcome object or today's raw mapping."""
        self.sequence += 1
        tool = str(tool_name)
        facts = _flatten_facts(outcome)
        state = _outcome_state(outcome, facts)
        ok = state != "failure"
        status = _status(_read(outcome, "status", facts.get("status")))
        job_id_raw = _first(facts, ("job_id", "task_id", "operation_id"))
        job_id = str(job_id_raw) if job_id_raw is not None else None
        call = str(call_id or _read(outcome, "call_id", "") or f"{tool}:{self.sequence}")
        is_mutation = bool(tool in MUTATION_TOOLS if mutation is None else mutation)
        is_verification = bool(
            tool in VISUAL_VERIFICATION_TOOLS | OBJECTIVE_VERIFICATION_TOOLS
            if verification is None else verification
        )
        artifacts = _extract_artifacts(
            outcome,
            facts,
            include_asset_id=is_mutation or (tool in {"check_job", "wait_for_job"} and status in FINISHED_JOB_STATES),
        )
        resolved_target = target_key or tool_target_key(facts)
        artifact_kind_map = {
            artifact: _infer_artifact_kind(artifact, facts, tool, artifact_kinds)
            for artifact in artifacts
        }
        if is_mutation and self.workflow in (_READ_ONLY_WORKFLOWS | {"general"}):
            inferred_kind = next(
                (kind for kind in artifact_kind_map.values() if kind),
                _TOOL_OUTPUT_KIND.get(tool),
            )
            if tool == "generate_video":
                promoted_workflow = "video_generation"
            elif tool == "generate_image":
                promoted_workflow = "image"
            elif inferred_kind == "video":
                promoted_workflow = "video_edit"
            elif inferred_kind == "image":
                promoted_workflow = "image"
            elif inferred_kind == "audio":
                promoted_workflow = "audio"
            else:
                promoted_workflow = None
            if promoted_workflow is not None:
                self.workflow = promoted_workflow
                self.required_final_kinds = (
                    self.required_final_kinds
                    | _EXPECTED_FINAL_KINDS.get(promoted_workflow, frozenset())
                )
                self.workflows = tuple(dict.fromkeys((
                    promoted_workflow,
                    *(
                        workflow
                        for workflow in self.workflows or ()
                        if workflow not in (_READ_ONLY_WORKFLOWS | {"general"})
                    ),
                )))
                self.steps = _workflow_steps(self.workflow, self.current_request)
        accepted_artifacts = tuple(
            artifact
            for artifact in artifacts
            if (
                not self.expected_final_kinds
                or artifact_kind_map[artifact] in self.expected_final_kinds
            )
            and not _explicitly_intermediate_output(
                tool, call_args or {}, facts
            )
        )
        # Intermediate assets are real mutations but not mutations of the
        # requested deliverable.  A reference image in a video workflow must
        # not replace the final video or invalidate its verification.
        goal_mutation = bool(
            (
                is_mutation
                and (
                    not self.requires_final_asset
                    or not artifacts
                    or bool(accepted_artifacts)
                )
            )
            or (
                tool in {"check_job", "wait_for_job"}
                and status in FINISHED_JOB_STATES
                and bool(accepted_artifacts)
            )
        )
        error_code = _error_code(outcome, facts, ok)
        summary = _summary(outcome, facts, ok)
        record = OutcomeRecord(
            seq=self.sequence,
            call_id=call,
            tool_name=tool,
            state=state,
            ok=ok,
            summary=summary,
            error_code=error_code,
            artifacts=artifacts,
            target_key=resolved_target,
            facts=facts,
            call_args=dict(call_args or {}),
            mutation=goal_mutation,
            verification=is_verification,
        )
        self.outcomes.append(record)

        self._record_job_state(job_id, status, record)
        if not ok:
            failure = FailureRecord(
                call_id=call,
                tool_name=tool,
                error_code=error_code or "unsuccessful_outcome",
                summary=summary,
                seq=self.sequence,
                job_id=job_id,
                target_key=resolved_target,
                blocking=bool(blocking_failure),
            )
            self.unresolved_failures[call] = failure
            self._mark_action_failed(tool, call)
            return record

        # pending/noop/partial are honest non-failures but cannot prove that a
        # requested mutation or verification is complete. Pending lifecycle
        # state is already represented in pending_jobs when a job id exists.
        if state in {"pending", "noop", "partial"}:
            return record

        self._resolve_successful_retry(tool, job_id, resolved_target)

        if goal_mutation:
            for failed_call_id, failure in self.unresolved_failures.items():
                if failure.job_id and failed_call_id not in self.superseded_failure_ids:
                    self.superseded_failure_ids.append(failed_call_id)
            self.last_mutation_seq = self.sequence
            self._invalidate_prior_verification()
            self._mark_action_done(tool, call, record.call_args, facts)
        elif tool in _READ_ACTION_TOOLS:
            if "inspect" in self.steps:
                self.mark_step("inspect", "done", call)
        elif "act" in self.steps and (
            tool == "spawn_subtasks" or tool not in MASTER_TOOL_SET
        ):
            # General extension tools may not appear in the static mutation
            # catalog; a canonical success still proves the action executed.
            self.mark_step("act", "done", call)

        if goal_mutation and accepted_artifacts:
            accepted_kinds = {
                artifact_kind_map[artifact]
                for artifact in accepted_artifacts
                if artifact_kind_map[artifact]
            }
            replaced_ids = _call_asset_ids(record.call_args)
            # Each deliverable kind has its own replacement policy. A single
            # requested cover replaces an earlier cover while two requested
            # posters accumulate; neither operation may erase an audio output.
            for accepted_kind in accepted_kinds:
                if self.required_final_count_for_kind(accepted_kind) <= 1:
                    replaced_ids.update(
                        asset_id
                        for asset_id in self.final_asset_ids
                        if self.final_asset_kinds.get(asset_id) == accepted_kind
                    )
            if replaced_ids:
                self.final_asset_ids = [
                    asset for asset in self.final_asset_ids if asset not in replaced_ids
                ]
                for asset in replaced_ids:
                    self.final_asset_kinds.pop(asset, None)
                    if asset in self.verified_final_asset_ids:
                        self.verified_final_asset_ids.remove(asset)
        registers_final_output = bool(
            goal_mutation
            or (
                tool in {"check_job", "wait_for_job"}
                and status in FINISHED_JOB_STATES
            )
        )
        if registers_final_output:
            for artifact in accepted_artifacts:
                self.note_final_asset(artifact, kind=artifact_kind_map[artifact])
        self._refresh_asset_count_criterion()
        if registers_final_output and accepted_artifacts and "produce" in self.steps:
            self.mark_step("produce", "done", call)
        if tool in {"export", "project_export", "project_export_otio"} and "deliver" in self.steps:
            self.mark_step("deliver", "done", call)

        if tool in OBJECTIVE_CRITERIA_TOOLS and self._facts_target_final_asset(facts):
            self._evaluate_criteria(facts, call)
            self._resolve_verified_alternative_failures()

        if is_verification and self._verification_is_current(facts):
            if self.requires_visual_verification:
                visual = tool in VISUAL_VERIFICATION_TOOLS
            else:
                visual = tool in VISUAL_VERIFICATION_TOOLS | OBJECTIVE_VERIFICATION_TOOLS
            if visual:
                targets = self._verification_target_ids(facts)
                if self.requires_final_asset:
                    for asset_id in targets:
                        if asset_id not in self.verified_final_asset_ids:
                            self.verified_final_asset_ids.append(asset_id)
                    required_verification_ids = (
                        self.visual_final_asset_ids
                        if self.requires_visual_verification
                        else tuple(self.final_asset_ids)
                    )
                    fully_covered = bool(required_verification_ids) and set(
                        required_verification_ids
                    ).issubset(self.verified_final_asset_ids)
                else:
                    fully_covered = True
                if fully_covered:
                    self.last_verification_seq = self.sequence
                    if "verify" in self.steps:
                        self.mark_step("verify", "done", call)
                    self._resolve_verified_alternative_failures()

        return record

    def _record_job_state(self, job_id: str | None, status: str, record: OutcomeRecord) -> None:
        if not job_id:
            return
        if status in PENDING_JOB_STATES:
            self.pending_jobs[job_id] = status
        elif status in FINISHED_JOB_STATES:
            self.pending_jobs.pop(job_id, None)
        elif status in FAILED_JOB_STATES or not record.ok:
            self.pending_jobs.pop(job_id, None)

    def _resolve_successful_retry(
        self, tool_name: str, job_id: str | None, target_key: str | None
    ) -> None:
        resolved: list[str] = []
        for call_id, failure in self.unresolved_failures.items():
            if failure.job_id:
                if failure.job_id == job_id:
                    resolved.append(call_id)
                continue
            if failure.target_key is not None or target_key is not None:
                same_operation_class = (
                    failure.tool_name == tool_name
                    or (
                        failure.tool_name in MUTATION_TOOLS
                        and tool_name in MUTATION_TOOLS
                    )
                    or (
                        failure.tool_name in _READ_ACTION_TOOLS
                        and tool_name in _READ_ACTION_TOOLS
                    )
                )
                if failure.target_key == target_key and same_operation_class:
                    resolved.append(call_id)
                continue
            if failure.tool_name == tool_name:
                resolved.append(call_id)
        for call_id in resolved:
            self.unresolved_failures.pop(call_id, None)
            if call_id in self.superseded_failure_ids:
                self.superseded_failure_ids.remove(call_id)

    def _resolve_verified_alternative_failures(self) -> None:
        if not self.superseded_failure_ids:
            return
        if self.requires_final_asset:
            if not self.final_asset_ids:
                return
            required_verification_ids = (
                self.visual_final_asset_ids
                if self.requires_visual_verification
                else tuple(self.final_asset_ids)
            )
            if not set(required_verification_ids).issubset(
                self.verified_final_asset_ids
            ):
                return
        elif self.requires_visual_verification and (
            self.last_verification_seq <= self.last_mutation_seq
        ):
            return
        if any(criterion.status != "passed" for criterion in self.criteria.values()):
            return
        for call_id in list(self.superseded_failure_ids):
            self.unresolved_failures.pop(call_id, None)
            self.superseded_failure_ids.remove(call_id)

    def _invalidate_prior_verification(self) -> None:
        if "verify" in self.steps:
            self.steps["verify"].status = "open"
            self.steps["verify"].evidence_ids.clear()
        for criterion in self.criteria.values():
            criterion.actual = None
            criterion.status = "open"
            criterion.evidence_ids.clear()
        self.criterion_asset_results.clear()
        self.verified_final_asset_ids.clear()

    def _mark_action_done(
        self,
        tool_name: str,
        call_id: str,
        call_args: Mapping[str, Any],
        facts: Mapping[str, Any],
    ) -> None:
        if tool_name in {"draft_shotlist", "set_shotlist", "update_shot", "refine_shot"} and "plan" in self.steps:
            self.mark_step("plan", "done", call_id)
        if tool_name == "assemble_shotlist" and "assemble" in self.steps:
            self.mark_step("assemble", "done", call_id)
        for step_id, step in self.steps.items():
            if not step_id.startswith("op:"):
                continue
            operation_id = step_id.split(":", 1)[1]
            if operation_id == "voiceover":
                satisfied = self._voiceover_operation_satisfied(
                    tool_name, call_args, facts
                )
            else:
                satisfied = _operation_satisfied(
                    operation_id,
                    tool_name,
                    call_args,
                    facts,
                    self.current_request,
                )
            if satisfied:
                self.mark_step(step_id, "done", call_id)
        for step_id in ("produce", "mutate", "act"):
            if step_id in self.steps:
                self.mark_step(step_id, "done", call_id)
                break

    def _voiceover_operation_satisfied(
        self,
        tool_name: str,
        call_args: Mapping[str, Any],
        facts: Mapping[str, Any],
    ) -> bool:
        if tool_name not in _operation_tools("voiceover"):
            return False
        expected = _requested_quoted_literal(
            self.current_request, r"旁白|口播|voiceover|narration"
        )
        requires_attachment = _targets_video_output(self.current_request)
        if tool_name == "narrate":
            if not _operation_satisfied(
                "voiceover",
                tool_name,
                call_args,
                facts,
                self.current_request,
            ):
                return False
            return not requires_attachment
        if tool_name != "timeline_insert_clip":
            return False
        if expected is None:
            return True
        inserted_ids = _inserted_asset_ids(call_args) | _inserted_asset_ids(facts)
        if not inserted_ids:
            return False
        normalized_expected = " ".join(expected.split())
        for outcome in self.outcomes:
            if outcome.tool_name != "narrate" or outcome.state != "success":
                continue
            actual = _first(outcome.call_args, ("text", "script", "content"))
            if actual is None:
                actual = _first(outcome.facts, ("text", "script", "content"))
            if " ".join(str(actual or "").split()) != normalized_expected:
                continue
            if inserted_ids.intersection(outcome.artifacts):
                return True
        return False

    def _refresh_asset_count_criterion(self) -> None:
        for criterion in self.criteria.values():
            if criterion.kind == "asset_count":
                actual = len(self.final_asset_ids)
            elif criterion.kind.startswith("asset_count:"):
                required_kind = criterion.kind.split(":", 1)[1]
                actual = sum(
                    1
                    for asset_id in self.final_asset_ids
                    if self.final_asset_kinds.get(asset_id) == required_kind
                )
            else:
                continue
            criterion.actual = actual
            criterion.status = (
                "passed" if actual == int(criterion.expected) else "failed"
            )

    def _mark_action_failed(self, tool_name: str, call_id: str) -> None:
        for step_id in ("produce", "mutate", "act", "inspect", "assemble", "plan", "deliver"):
            if step_id in self.steps and self.steps[step_id].status != "done":
                self.mark_step(step_id, "failed", call_id)
                break

    def _facts_target_final_asset(self, facts: Mapping[str, Any]) -> bool:
        subject = _first(facts, ("asset_id", "output_asset_id", "artifact_id"))
        if not self.final_asset_ids:
            return not self.requires_final_asset
        if subject is not None:
            return str(subject) in self.final_asset_ids
        subjects = facts.get("asset_ids")
        if isinstance(subjects, Sequence) and not isinstance(subjects, (str, bytes)):
            return bool(set(map(str, subjects)) & set(self.final_asset_ids))
        return False

    def _verification_target_ids(self, facts: Mapping[str, Any]) -> set[str]:
        if not self.final_asset_ids:
            return set()
        candidates: set[str] = set()
        subject = _first(facts, ("asset_id", "output_asset_id", "artifact_id"))
        if subject is not None:
            candidates.add(str(subject))
        subjects = facts.get("asset_ids")
        if isinstance(subjects, Sequence) and not isinstance(subjects, (str, bytes)):
            candidates.update(map(str, subjects))
        return candidates & set(self.final_asset_ids)

    def _verification_is_current(self, facts: Mapping[str, Any]) -> bool:
        if self.last_mutation_seq and self.sequence <= self.last_mutation_seq:
            return False
        return self._facts_target_final_asset(facts)

    def _criterion_target_final_asset_ids(
        self, criterion: AcceptanceCriterion
    ) -> tuple[str, ...]:
        """Return only deliverables to which an objective criterion applies."""
        if criterion.target_kind:
            return tuple(
                asset_id
                for asset_id in self.final_asset_ids
                if self.final_asset_kinds.get(asset_id) == criterion.target_kind
            )
        allowed_kinds: frozenset[str] | None
        if criterion.kind == "duration_sec":
            allowed_kinds = frozenset({"video", "audio"})
        elif criterion.kind in {"dimensions", "aspect"}:
            allowed_kinds = frozenset({"video", "image"})
        elif criterion.kind == "fps":
            allowed_kinds = frozenset({"video"})
        elif criterion.kind == "has_audio":
            # "带音乐的视频" constrains the video, while a standalone audio
            # deliverable still needs to be proved when no video is requested.
            allowed_kinds = (
                frozenset({"video"})
                if "video" in self.final_asset_kind_set
                else frozenset({"audio"})
            )
        elif criterion.kind == "format":
            expected = str(criterion.expected).lower()
            if expected in {"mp4", "mov", "mkv", "webm"}:
                allowed_kinds = frozenset({"video"})
            elif expected in {"gif", "png", "jpg", "jpeg"}:
                allowed_kinds = frozenset({"image"})
            elif expected in {"wav", "mp3", "aac", "flac"}:
                allowed_kinds = frozenset({"audio"})
            else:
                allowed_kinds = None
        else:
            allowed_kinds = None
        if allowed_kinds is None:
            return tuple(self.final_asset_ids)
        return tuple(
            asset_id
            for asset_id in self.final_asset_ids
            if self.final_asset_kinds.get(asset_id) in allowed_kinds
        )

    def _evaluate_criteria(self, facts: Mapping[str, Any], evidence_id: str) -> None:
        subject = _first(facts, ("asset_id", "output_asset_id", "artifact_id"))
        subject_id = str(subject) if subject is not None else None
        for criterion in self.criteria.values():
            if criterion.kind == "asset_count" or criterion.kind.startswith(
                "asset_count:"
            ):
                continue
            if criterion.kind == "asset_slot":
                criterion_targets = self._criterion_target_final_asset_ids(criterion)
                if subject_id not in criterion_targets:
                    continue
                actual_spec = _actual_for_asset_slot(criterion.expected, facts)
                if not actual_spec:
                    continue
                results = self.criterion_asset_results.setdefault(criterion.id, {})
                merged = dict(results.get(subject_id) or {})
                merged.update(actual_spec)
                results[subject_id] = merged
                criterion.actual = dict(results)
                if _asset_slot_matches(criterion.expected, merged):
                    criterion.status = "passed"
                if evidence_id not in criterion.evidence_ids:
                    criterion.evidence_ids.append(evidence_id)
                continue
            actual = _actual_for_criterion(criterion, facts)
            if actual is _MISSING:
                continue
            criterion_targets = self._criterion_target_final_asset_ids(criterion)
            if self.requires_final_asset and subject_id not in criterion_targets:
                continue
            if criterion.match_mode == "one":
                if criterion.status != "passed" and _criterion_matches(
                    criterion, actual
                ):
                    criterion.actual = {subject_id: actual}
                    criterion.status = "passed"
                    if evidence_id not in criterion.evidence_ids:
                        criterion.evidence_ids.append(evidence_id)
                continue
            if self.requires_final_asset and len(criterion_targets) > 1:
                results = self.criterion_asset_results.setdefault(criterion.id, {})
                results[subject_id] = actual
                criterion.actual = dict(results)
                criterion.status = (
                    "passed"
                    if all(
                        asset_id in results
                        and _criterion_matches(criterion, results[asset_id])
                        for asset_id in criterion_targets
                    )
                    else "failed"
                )
            else:
                criterion.actual = actual
                criterion.status = (
                    "passed" if _criterion_matches(criterion, actual) else "failed"
                )
            if evidence_id not in criterion.evidence_ids:
                criterion.evidence_ids.append(evidence_id)

    def completion_decision(self) -> CompletionDecision:
        if self.workflow == "conversation":
            return CompletionDecision("complete", ())

        blockers: list[str] = []
        for step in self.steps.values():
            if step.required and step.status != "done":
                blockers.append(f"step:{step.id}:{step.status}")
        for criterion in self.criteria.values():
            if criterion.status != "passed":
                blockers.append(f"criterion:{criterion.id}:{criterion.status}")
        for job_id, status in self.pending_jobs.items():
            prefix = "failed_job" if status in FAILED_JOB_STATES else "pending_job"
            blockers.append(f"{prefix}:{job_id}:{status}")
        for call_id, failure in self.unresolved_failures.items():
            if failure.blocking:
                blockers.append(f"failure:{call_id}:{failure.error_code}")
        if self.requires_final_asset and not self.final_asset_ids:
            blockers.append("final_asset:missing")
        if self.requires_final_asset:
            for kind in sorted(self.expected_final_kinds - self.final_asset_kind_set):
                blockers.append(f"final_asset_kind:{kind}:missing")
        if (
            self.requires_visual_verification
            and self.last_mutation_seq
            and (
                self.last_verification_seq <= self.last_mutation_seq
                or (
                    self.requires_final_asset
                    and not set(self.visual_final_asset_ids).issubset(
                        self.verified_final_asset_ids
                    )
                )
            )
        ):
            blockers.append("verification:stale_or_missing")
        return CompletionDecision("complete" if not blockers else "incomplete", tuple(blockers))

    def can_complete(self) -> bool:
        return self.completion_decision().complete

    def to_dict(self) -> dict[str, Any]:
        return {
            "current_request": self.current_request,
            "workflow": self.workflow,
            "workflows": list(self.workflows or ()),
            "required_final_kinds": sorted(self.required_final_kinds),
            "session_origin": self.session_origin,
            "steps": {name: asdict(step) for name, step in self.steps.items()},
            "criteria": {name: asdict(item) for name, item in self.criteria.items()},
            "outcomes": [asdict(item) for item in self.outcomes],
            "sequence": self.sequence,
            "last_mutation_seq": self.last_mutation_seq,
            "last_verification_seq": self.last_verification_seq,
            "final_asset_ids": list(self.final_asset_ids),
            "final_asset_kinds": dict(self.final_asset_kinds),
            "verified_final_asset_ids": list(self.verified_final_asset_ids),
            "criterion_asset_results": {
                criterion_id: dict(results)
                for criterion_id, results in self.criterion_asset_results.items()
            },
            "pending_jobs": dict(self.pending_jobs),
            "unresolved_failures": {
                call_id: asdict(item) for call_id, item in self.unresolved_failures.items()
            },
            "superseded_failure_ids": list(self.superseded_failure_ids),
            "compact_history": list(self.compact_history),
            "completion": asdict(self.completion_decision()),
        }

    def add_compact_history(self, summaries: Iterable[str]) -> None:
        for summary in summaries:
            text = str(summary).strip()
            if text:
                self.compact_history.append(text[:500])
        self.compact_history = self.compact_history[-40:]

    def to_prompt_text(self) -> str:
        decision = self.completion_decision()
        open_steps = [step.id for step in self.steps.values() if step.status != "done"]
        open_criteria = [item.id for item in self.criteria.values() if item.status != "passed"]
        return (
            f"Turn ledger: workflow={self.workflow}; workflows={self.workflows}; "
            f"required_final_kinds={sorted(self.required_final_kinds)}; "
            f"sequence={self.sequence}; "
            f"open_steps={open_steps}; open_criteria={open_criteria}; "
            f"final_assets={self.final_asset_ids}; final_kinds={self.final_asset_kinds}; "
            f"pending_jobs={self.pending_jobs}; "
            f"completion={decision.status}; blockers={list(decision.blockers)}; "
            f"compact_history={self.compact_history[-12:]}"
        )


_READ_ACTION_TOOLS = frozenset({
    "probe_media", "analyze_media", "extract_frame", "get_safe_areas",
    "inspect_lottie", "search_library", "search_media", "search_frames",
    "get_media_annotations", "web_search", "web_open", "fetch", "file_list",
    "file_read", "read_file", "list_dir", "recall_skills", "get_lumenframe",
    "get_timeline", "inspect_timeline", "render_preview", "detect_beats",
})


def _actual_for_asset_slot(
    expected: Mapping[str, Any], facts: Mapping[str, Any]
) -> dict[str, Any]:
    actual: dict[str, Any] = {}
    for kind, expected_value in expected.items():
        probe = AcceptanceCriterion("slot-probe", kind, expected_value)
        value = _actual_for_criterion(probe, facts)
        if value is not _MISSING:
            actual[kind] = value
    return actual


def _asset_slot_matches(
    expected: Mapping[str, Any], actual: Mapping[str, Any]
) -> bool:
    return all(
        kind in actual
        and _criterion_matches(
            AcceptanceCriterion("slot-probe", kind, expected_value),
            actual[kind],
        )
        for kind, expected_value in expected.items()
    )


def _actual_for_criterion(criterion: AcceptanceCriterion, facts: Mapping[str, Any]) -> Any:
    if criterion.kind == "duration_sec":
        seconds = _parse_number(_first(facts, ("duration_sec", "duration", "seconds")))
        if seconds is None:
            milliseconds = _parse_number(facts.get("duration_ms"))
            if milliseconds is not None:
                seconds = milliseconds / 1000.0
        return seconds if seconds is not None else _MISSING
    if criterion.kind == "dimensions":
        width = _parse_number(facts.get("width"))
        height = _parse_number(facts.get("height"))
        if width is None or height is None:
            return _MISSING
        return int(round(width)), int(round(height))
    if criterion.kind == "fps":
        value = _parse_number(_first(facts, ("fps", "frame_rate", "framerate")))
        return value if value is not None else _MISSING
    if criterion.kind == "aspect":
        width = _parse_number(facts.get("width"))
        height = _parse_number(facts.get("height"))
        if width is None or height in {None, 0}:
            explicit = facts.get("aspect_ratio", _MISSING)
            return explicit
        return width / height
    if criterion.kind == "format":
        raw = _first(facts, ("format", "format_name", "container", "extension", "path", "filename"))
        normalized = _normalize_format(raw)
        return normalized if normalized is not None else _MISSING
    if criterion.kind == "has_audio":
        value = _coerce_bool(facts.get("has_audio", _MISSING))
        return value if value is not None else _MISSING
    return _MISSING


def _criterion_matches(criterion: AcceptanceCriterion, actual: Any) -> bool:
    if criterion.kind == "duration_sec":
        expected = float(criterion.expected)
        return abs(float(actual) - expected) <= max(0.1, expected * 0.02)
    if criterion.kind == "dimensions":
        return tuple(actual) == tuple(criterion.expected)
    if criterion.kind == "fps":
        return abs(float(actual) - float(criterion.expected)) <= 0.1
    if criterion.kind == "aspect":
        expected_width, expected_height = criterion.expected
        expected_ratio = expected_width / expected_height
        if isinstance(actual, str) and ":" in actual:
            left, right = actual.replace("：", ":").split(":", 1)
            parsed_left = _parse_number(left)
            parsed_right = _parse_number(right)
            if parsed_left is None or parsed_right in {None, 0}:
                return False
            actual = parsed_left / parsed_right
        return abs(float(actual) - expected_ratio) <= 0.02
    if criterion.kind == "format":
        return _normalize_format(actual) == _normalize_format(criterion.expected)
    if criterion.kind == "has_audio":
        return bool(actual) is bool(criterion.expected)
    return actual == criterion.expected


__all__ = [
    "AcceptanceCriterion",
    "CompletionDecision",
    "FAILED_JOB_STATES",
    "FailureRecord",
    "FINISHED_JOB_STATES",
    "LedgerStep",
    "MUTATION_TOOLS",
    "OBJECTIVE_VERIFICATION_TOOLS",
    "OutcomeRecord",
    "PENDING_JOB_STATES",
    "TurnLedger",
    "VISUAL_VERIFICATION_TOOLS",
    "extract_acceptance_criteria",
    "tool_target_key",
]
