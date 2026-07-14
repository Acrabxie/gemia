from __future__ import annotations

import copy
import json
import math
from pathlib import Path
from typing import Any


FONT_KEYWORDS = (
    "字幕",
    "标题",
    "文字",
    "文案",
    "贴字",
    "caption",
    "subtitle",
    "title",
    "text",
    "lower third",
)

_STRIP_KEYS = {
    "waveformPeaks",
    "waveform_peaks",
    "thumbnailStrip",
    "thumbnail_strip",
    "thumbnailSrc",
    "thumbnail_src",
    "previewSrc",
    "preview_src",
}

_CATEGORY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "timeline": (
        "裁",
        "剪",
        "截",
        "trim",
        "cut",
        "加速",
        "慢放",
        "慢动作",
        "speed",
        "slow",
        "倒放",
        "reverse",
        "合并",
        "拼接",
        "merge",
        "concat",
        "转场",
        "transition",
        "旋转",
        "rotate",
        "镜像",
        "flip",
        "时间轴",
        "视频",
        "片段",
        "时间范围",
        "区间",
        "秒",
        "timeline",
        "video",
        "clip",
    ),
    "color": (
        "调色",
        "颜色",
        "色彩",
        "暖色",
        "冷色",
        "曝光",
        "对比",
        "饱和",
        "黑白",
        "复古",
        "赛博",
        "hdr",
        "lut",
        "color",
        "grade",
        "exposure",
        "temperature",
        "vintage",
        "cyberpunk",
    ),
    "blur": ("模糊", "虚化", "景深", "散景", "blur", "bokeh", "defocus", "dreamy"),
    "edge": ("锐化", "清晰", "边缘", "轮廓", "sharpen", "sharp", "edge", "outline", "canny", "detail"),
    "stylize": (
        "风格",
        "油画",
        "水墨",
        "漫画",
        "胶片",
        "颗粒",
        "故障",
        "赛博",
        "style",
        "stylize",
        "cartoon",
        "film",
        "grain",
        "glitch",
        "vhs",
        "halftone",
    ),
    "composite": (
        "叠加",
        "合成",
        "混合",
        "蒙版",
        "绿幕",
        "水印",
        "分屏",
        "overlay",
        "composite",
        "blend",
        "mask",
        "chroma",
        "split",
        "watermark",
    ),
    "io": (
        "导出",
        "格式",
        "缩略图",
        "裁切画面",
        "比例",
        "补边",
        "export",
        "proxy",
        "thumbnail",
        "resize",
        "crop",
        "pad",
        "letterbox",
        "aspect",
    ),
    "generative": (
        "生成",
        "图生视频",
        "延展",
        "续写",
        "b-roll",
        "broll",
        "generate",
        "generative",
        "veo",
        "banana",
        "extend",
    ),
    "analysis": (
        "分析",
        "检测",
        "总结",
        "识别",
        "场景",
        "元数据",
        "analysis",
        "detect",
        "summary",
        "review",
        "metadata",
        "scene",
    ),
    "audio": (
        "音频",
        "声音",
        "音乐",
        "静音",
        "音量",
        "分离音频",
        "audio",
        "sound",
        "music",
        "mute",
        "volume",
        "waveform",
        "normalize",
    ),
    "spatial": (
        "blender",
        "lumerilink",
        "空间",
        "三维",
        "3d",
        "视差",
        "景深网格",
        "parallax",
        "depth",
        "volumetric",
        "hologram",
        "spatial",
    ),
    "text_graphics": (
        "字幕",
        "标题",
        "文字",
        "下三分之一",
        "动效",
        "caption",
        "subtitle",
        "title",
        "text",
        "html",
        "lottie",
        "lower third",
        "motion graphics",
        "mg",
    ),
    "face": (
        "人脸",
        "脸",
        "皮肤",
        "磨皮",
        "年龄",
        "肖像",
        "痘",
        "face",
        "skin",
        "portrait",
        "age",
        "blemish",
        "jaw",
        "eye",
        "mouth",
    ),
    "repair": (
        "去噪",
        "降噪",
        "稳定",
        "修复",
        "防抖",
        "去模糊",
        "denoise",
        "stabilize",
        "repair",
        "restore",
        "deblur",
        "ultrasharpen",
    ),
}


def estimate_tokens(value: Any) -> int:
    """Cheap, deterministic token estimate for planning budgets."""
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return max(1, math.ceil(len(text) / 4))


def token_budget(system_prompt: str, user_payload: dict[str, Any]) -> dict[str, int]:
    system_tokens = estimate_tokens(system_prompt)
    payload_tokens = estimate_tokens(user_payload)
    return {
        "system_tokens_est": system_tokens,
        "payload_tokens_est": payload_tokens,
        "total_tokens_est": system_tokens + payload_tokens,
    }


def build_effective_request(request: str, clarifications: dict[str, Any] | None = None) -> tuple[str, str]:
    raw_request = str(request or "").strip()
    if not clarifications:
        return raw_request, raw_request
    values: list[str] = []
    for key in sorted(clarifications):
        value = clarifications.get(key)
        if isinstance(value, (list, tuple)):
            value_text = ", ".join(str(item) for item in value if str(item).strip())
        else:
            value_text = str(value or "").strip()
        if value_text:
            values.append(value_text)
    if not values:
        return raw_request, raw_request
    return f"{raw_request}\n[clarified] {'; '.join(values)}", raw_request


def infer_prompt_categories(
    effective_request: str,
    *,
    clarifications: dict[str, Any] | None = None,
    input_path: str = "",
    project_state: dict[str, Any] | None = None,
    max_categories: int = 3,
) -> list[str]:
    text_parts = [effective_request or ""]
    if clarifications:
        text_parts.extend(str(value) for value in clarifications.values())
    if input_path:
        text_parts.append(Path(input_path).suffix.lower())
    if isinstance(project_state, dict):
        clips = project_state.get("clips") if isinstance(project_state.get("clips"), list) else []
        if any(str(clip.get("mediaKind") or clip.get("media_kind") or "").lower() == "audio" for clip in clips if isinstance(clip, dict)):
            text_parts.append(" audio")
    text = " ".join(text_parts).lower()
    scores: list[tuple[int, str]] = []
    for category, keywords in _CATEGORY_KEYWORDS.items():
        score = sum(1 for keyword in keywords if keyword.lower() in text)
        if score:
            scores.append((score, category))
    if not scores:
        return ["core"]
    scores.sort(key=lambda item: (-item[0], item[1]))
    categories = [category for _score, category in scores[:max_categories]]
    if "timeline" not in categories and any(word in text for word in ("视频", "片段", "clip", "video")):
        categories.append("timeline")
    return categories[:max_categories]


def needs_font_library(effective_request: str, clarifications: dict[str, Any] | None = None) -> bool:
    text = (effective_request or "").lower()
    if clarifications:
        text += " " + " ".join(str(value).lower() for value in clarifications.values())
    return any(keyword in text for keyword in FONT_KEYWORDS)


def strip_font_library(font_library: dict[str, Any], effective_request: str, clarifications: dict[str, Any] | None = None) -> dict[str, Any]:
    if not isinstance(font_library, dict):
        return {}
    wants_fonts = needs_font_library(effective_request, clarifications)
    cleaned = copy.deepcopy(font_library)
    google_fonts = cleaned.pop("google_fonts", None)
    fonts = cleaned.get("fonts")
    if not isinstance(fonts, list):
        if wants_fonts and isinstance(google_fonts, list):
            cleaned["google_fonts"] = google_fonts[:5]
        return cleaned
    if wants_fonts:
        cleaned["fonts"] = fonts[:12]
        if isinstance(google_fonts, list):
            cleaned["google_fonts"] = google_fonts[:5]
        return cleaned
    default_id = str(cleaned.get("default_font_id") or "")
    default_fonts = [
        item for item in fonts
        if isinstance(item, dict) and str(item.get("font_id") or "") == default_id
    ]
    cleaned["fonts"] = default_fonts[:1] if default_fonts else fonts[:1]
    return cleaned


def strip_for_planning(project_state: dict[str, Any] | None, effective_request: str = "") -> dict[str, Any] | None:
    if not isinstance(project_state, dict):
        return None
    payload = _strip_heavy_fields(copy.deepcopy(project_state))
    refs = _first_list(
        payload.get("timeReferences"),
        payload.get("agent_time_references"),
        (payload.get("agent_context") or {}).get("time_references") if isinstance(payload.get("agent_context"), dict) else None,
    )
    payload.pop("agent_time_references", None)
    if refs:
        payload["timeReferences"] = refs
    if isinstance(payload.get("font_library"), dict):
        payload["font_library"] = strip_font_library(payload["font_library"], effective_request)
    agent_context = payload.get("agent_context")
    if isinstance(agent_context, dict):
        _compact_agent_context(agent_context)
    return payload


def video_context_from_project(project_state: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(project_state, dict):
        return None
    clips = project_state.get("clips") if isinstance(project_state.get("clips"), list) else []
    selected_id = str(project_state.get("selectedClipId") or "")
    candidates = [clip for clip in clips if isinstance(clip, dict)]
    candidates.sort(key=lambda clip: 0 if selected_id and str(clip.get("id") or "") == selected_id else 1)
    for clip in candidates:
        kind = str(clip.get("mediaKind") or clip.get("media_kind") or "").lower()
        if kind and kind != "video":
            continue
        summary = clip.get("summary")
        if not isinstance(summary, dict):
            continue
        context = {
            key: summary.get(key)
            for key in ("duration", "mood", "key_frame", "suggested_use")
            if summary.get(key) not in (None, "")
        }
        if context:
            return context
    return None


def _strip_heavy_fields(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _strip_heavy_fields(item)
            for key, item in value.items()
            if key not in _STRIP_KEYS
        }
    if isinstance(value, list):
        return [_strip_heavy_fields(item) for item in value]
    return value


def _first_list(*values: Any) -> list[Any]:
    for value in values:
        if isinstance(value, list):
            return value
    return []


def _compact_agent_context(agent_context: dict[str, Any]) -> None:
    target_ids: set[str] = set()
    for item in agent_context.get("targets") or []:
        if isinstance(item, dict):
            material_id = _material_id(item)
            if material_id:
                target_ids.add(material_id)
    compacted: list[dict[str, str]] = []
    for item in agent_context.get("materials") or []:
        if not isinstance(item, dict):
            continue
        material_id = _material_id(item)
        if not material_id:
            continue
        role = "context"
        if material_id in target_ids:
            role = "target"
        elif item.get("selected"):
            role = "selected"
        compacted.append({"material_id": material_id, "role": role})
    if "materials" in agent_context:
        agent_context["materials"] = compacted
    for key in ("time_references", "current_target", "targets"):
        agent_context.pop(key, None)


def _material_id(item: dict[str, Any]) -> str:
    for key in ("material_id", "asset_id", "clip_id", "id"):
        value = str(item.get(key) or "").strip()
        if value:
            return value
    name = str(item.get("name") or "").strip()
    return name
