from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Literal

from .prompt_slimming import build_effective_request
from . import skill_yaml as yaml


SkillRouteSource = Literal["keyword", "llm", "fallback"]

SKILLS_ROOT = Path(__file__).resolve().parent / "skills"
# 无关键词命中时的兜底 skill,按优先级排序;route() 只取前 max_skills 个(默认 3)。
# design-system 领跑,让质量基准在"让它好看点/再处理一下"这类说不清的请求上也随行。
CORE_FALLBACK_SKILLS = ["design-system", "timeline-ops", "color-grade", "transition"]
PROMPT_ONLY_FALLBACK_SKILLS = ["generative", "ad-graphics", "stock-media"]


@dataclass(frozen=True)
class SkillMetadata:
    id: str
    description: str
    primary_triggers: tuple[str, ...]
    secondary_triggers: tuple[str, ...]
    primitives: tuple[str, ...]
    est_tokens: int
    path: Path


@dataclass
class RouteHit:
    skill_id: str
    score: float
    primary_hits: list[str] = field(default_factory=list)
    secondary_hits: list[str] = field(default_factory=list)

    @property
    def has_primary(self) -> bool:
        return bool(self.primary_hits)


@dataclass
class RouteResult:
    skills: list[str]
    source: SkillRouteSource
    confidence: float
    effective_request: str
    raw_request: str
    matched_triggers: dict[str, list[str]] = field(default_factory=dict)
    latency_ms: float = 0.0

    @property
    def is_keyword(self) -> bool:
        return self.source == "keyword"


SkillFallback = Callable[[str, list[SkillMetadata]], list[str]]

_SKILL_CACHE: dict[str, SkillMetadata] | None = None
_TRIGGER_INDEX: dict[str, list[tuple[str, str, float]]] | None = None

_SYNONYMS: dict[str, tuple[str, ...]] = {
    "衔接": ("转场", "过渡"),
    "切换": ("转场",),
    "淡入淡出": ("转场", "溶解", "fade"),
    "冷调": ("冷色", "调色"),
    "暖调": ("暖色", "调色"),
    "标题": ("文字", "字幕", "title"),
    "口播": ("字幕", "caption"),
    "下三分之一": ("lower third", "html"),
    "空间效果": ("blender", "3d", "空间"),
    "达芬奇剪辑软件": ("davinci resolve", "resolve timeline", "fusion page", "color page"),
    "达芬奇软件": ("davinci resolve", "resolve timeline", "fusion page", "color page"),
    "达芬奇": ("davinci resolve", "resolve timeline", "fusion page", "color page"),
    "davinci resolve": ("resolve timeline", "fusion page", "color page"),
    "抠像": ("蒙版", "chroma", "composite"),
    "绿幕": ("chroma", "key"),
    "磨皮": ("皮肤", "瑕疵", "face"),
    "人脸跟踪": ("face tracking", "track face"),
    "跟踪人脸": ("人脸跟踪", "face tracking"),
    "面部跟踪": ("人脸跟踪", "face tracking"),
    "追踪人脸": ("人脸跟踪", "face tracking"),
    "锁定人脸": ("人脸跟踪", "face tracking"),
    "跟踪这张脸": ("人脸跟踪", "face tracking"),
    "主体跟踪": ("人脸跟踪", "tracking face"),
    "变年轻": ("年龄", "age"),
    "变老": ("年龄", "age"),
    "清晰": ("锐化", "ultrasharpen"),
    "糊": ("模糊", "去模糊"),
    "防抖": ("稳定", "stabilize"),
    "片头": ("标题卡", "文字"),
    "一键成片": ("剪辑", "时间线"),
    "素材网站": ("pexels", "pixabay", "stock media"),
    "公开素材": ("stock media", "pexels", "pixabay"),
    "找一段": ("找素材", "搜索素材"),
    "找一个视频": ("找素材", "搜索素材", "公开视频"),
    "找一段视频": ("找素材", "搜索素材", "公开视频"),
    "找段视频": ("找素材", "搜索素材", "公开视频"),
    "补素材": ("找素材", "下载素材"),
    "素材抓取": ("抓取素材", "下载素材"),
    "插入中间": ("拼接", "时间线"),
    "插到中间": ("拼接", "时间线"),
    "放到中间": ("拼接", "时间线"),
    "敲代码": ("微函数", "创作运行时"),
    "写微函数": ("微函数", "创作运行时"),
    "改源码": ("修改底层源码", "创作运行时"),
    "改底层": ("修改底层源码", "创作运行时"),
    "自己插图层": ("自己插入图层", "创作运行时"),
    "自己加图层": ("自主搭图层", "创作运行时"),
    "编辑图层": ("自己编辑图层", "创作运行时"),
    "受限": ("开放创作空间", "创作运行时"),
    "参考资料": ("reference assets", "创作运行时"),
    "参考素材": ("reference assets", "创作运行时"),
    "只写prompt": ("prompt-only", "空画布", "创作运行时"),
    "只写 prompt": ("prompt-only", "空画布", "创作运行时"),
    "prompt-only": ("空画布", "创作运行时"),
    "每个效果小样": ("小样", "preview", "创作运行时"),
    "图层小样": ("小样", "图层", "创作运行时"),
    "实时渲染": ("preview", "代码渲染", "创作运行时"),
    "实时预览": ("preview", "小样", "创作运行时"),
    "自审": ("self review", "review loop", "创作运行时"),
    "自己review": ("self review", "review loop", "创作运行时"),
    "review自己做的": ("review loop", "创作运行时"),
    "局部修改": ("revision", "render pass", "创作运行时"),
    "局部修正": ("revision", "render pass", "创作运行时"),
    "继续推进": ("review loop", "创作运行时"),
    "商业广告": ("广告图文", "广告动效", "promo graphics"),
    "广告文字": ("广告图文", "商业广告"),
    "广告图案": ("广告图文", "商业广告"),
    "广告动效": ("商业广告", "promo graphics"),
    "商业感": ("商业广告", "广告图文"),
    "卖点": ("产品卖点", "product callout"),
    "促销": ("price badge", "cta card"),
    "价格标签": ("price badge", "广告图文"),
    "购买按钮": ("CTA", "cta card"),
    "扫光": ("shimmer sweep", "广告动效"),
    "产品标注": ("product callout", "广告图文"),
    "做一个视频": ("生成视频", "生成一段"),
    "做一段视频": ("生成视频", "生成一段"),
    "做个视频": ("生成视频", "生成一段"),
    "做一条视频": ("生成视频", "生成一段"),
    "广告视频": ("生成视频", "商业广告"),
    "创作视频": ("生成视频", "创作运行时"),
    "视频创作": ("生成视频", "创作运行时"),
    "创作平台": ("生成视频", "创作运行时"),
    "空画布": ("生成视频", "广告图文"),
    "不上传素材": ("生成视频", "空画布"),
    "不用上传素材": ("生成视频", "空画布"),
    "无需上传素材": ("生成视频", "空画布"),
    "从零开始": ("生成视频", "空画布"),
}


def route(
    request: str,
    clarifications: dict[str, Any] | None = None,
    project_state: dict[str, Any] | None = None,
    *,
    max_skills: int = 3,
    llm_fallback: SkillFallback | None = None,
) -> RouteResult:
    """Route a planning request to 1-3 progressive-disclosure planner skills."""
    start = time.perf_counter()
    effective, raw = build_effective_request(request, clarifications)
    text = _routing_text(effective, clarifications, project_state)
    hits = keyword_match(text)
    primary_hits = [hit for hit in hits if hit.has_primary]
    if primary_hits:
        selected = [hit.skill_id for hit in primary_hits[:max(1, max_skills)]]
        latency = (time.perf_counter() - start) * 1000
        confidence = min(1.0, primary_hits[0].score)
        return RouteResult(
            skills=selected,
            source="keyword",
            confidence=confidence,
            effective_request=effective,
            raw_request=raw,
            matched_triggers={hit.skill_id: hit.primary_hits + hit.secondary_hits for hit in primary_hits},
            latency_ms=latency,
        )

    llm_hits: list[str] = []
    if _llm_fallback_enabled(llm_fallback):
        llm_hits = llm_route_fallback(effective, llm_fallback=llm_fallback)
    if llm_hits:
        latency = (time.perf_counter() - start) * 1000
        return RouteResult(
            skills=_known_skill_ids(llm_hits)[:max(1, max_skills)],
            source="llm",
            confidence=0.7,
            effective_request=effective,
            raw_request=raw,
            matched_triggers={},
            latency_ms=latency,
        )

    latency = (time.perf_counter() - start) * 1000
    fallback_skills = PROMPT_ONLY_FALLBACK_SKILLS if _is_prompt_only_project(project_state) else CORE_FALLBACK_SKILLS
    return RouteResult(
        skills=fallback_skills[:max(1, max_skills)],
        source="fallback",
        confidence=0.0,
        effective_request=effective,
        raw_request=raw,
        matched_triggers={},
        latency_ms=latency,
    )


def keyword_match(text: str, *, skills: dict[str, SkillMetadata] | None = None) -> list[RouteHit]:
    skill_map = skills or load_skill_metadata()
    expanded = _expand_text(text)
    hits: dict[str, RouteHit] = {}
    for metadata in skill_map.values():
        hit = RouteHit(skill_id=metadata.id, score=0.0)
        for trigger in _sorted_triggers(metadata.primary_triggers):
            if _contains_trigger(expanded, trigger):
                hit.score += 1.0
                hit.primary_hits.append(trigger)
        for trigger in _sorted_triggers(metadata.secondary_triggers):
            if _contains_trigger(expanded, trigger):
                hit.score += 0.5
                hit.secondary_hits.append(trigger)
        if hit.score > 0:
            hits[metadata.id] = hit
    return sorted(hits.values(), key=lambda item: (-item.score, item.skill_id))


def llm_route_fallback(
    effective_request: str,
    *,
    llm_fallback: SkillFallback | None = None,
    timeout_ms: int = 500,
) -> list[str]:
    """Optional interface for a cheap future fallback router.

    The first shipped version keeps this path disabled unless tests or an
    explicit caller pass a fallback callback and GEMIA_SKILL_LLM_FALLBACK=1.
    """
    if llm_fallback is None:
        return []
    start = time.perf_counter()
    try:
        hits = llm_fallback(effective_request, list(load_skill_metadata().values()))
    except Exception:
        return []
    if (time.perf_counter() - start) * 1000 > timeout_ms:
        return []
    return _known_skill_ids(hits)


def load_skill_metadata(*, refresh: bool = False) -> dict[str, SkillMetadata]:
    global _SKILL_CACHE
    if _SKILL_CACHE is not None and not refresh:
        return dict(_SKILL_CACHE)
    skills: dict[str, SkillMetadata] = {}
    if not SKILLS_ROOT.exists():
        _SKILL_CACHE = {}
        return {}
    for skill_path in sorted(SKILLS_ROOT.glob("*/SKILL.md")):
        if skill_path.parent.name.startswith("_"):
            continue
        metadata = parse_skill_metadata(skill_path)
        skills[metadata.id] = metadata
    _SKILL_CACHE = skills
    return dict(skills)


def parse_skill_metadata(path: Path) -> SkillMetadata:
    text = path.read_text(encoding="utf-8")
    frontmatter = _frontmatter(text)
    triggers = frontmatter.get("triggers") or {}
    primary = tuple(str(item).strip() for item in triggers.get("primary") or [] if str(item).strip())
    secondary = tuple(str(item).strip() for item in triggers.get("secondary") or [] if str(item).strip())
    primitives = tuple(str(item).strip() for item in frontmatter.get("primitives") or [] if str(item).strip())
    return SkillMetadata(
        id=str(frontmatter.get("id") or path.parent.name).strip(),
        description=str(frontmatter.get("description") or "").strip(),
        primary_triggers=primary,
        secondary_triggers=secondary,
        primitives=primitives,
        est_tokens=int(frontmatter.get("est_tokens") or 0),
        path=path,
    )


def skill_descriptions_index() -> str:
    lines: list[str] = []
    for metadata in load_skill_metadata().values():
        triggers = ", ".join(metadata.primary_triggers[:5])
        lines.append(f"- {metadata.id}: {metadata.description} Primary triggers: {triggers}.")
    return "\n".join(lines)


def clear_skill_cache() -> None:
    global _SKILL_CACHE, _TRIGGER_INDEX
    _SKILL_CACHE = None
    _TRIGGER_INDEX = None


def _frontmatter(text: str) -> dict[str, Any]:
    if not text.startswith("---"):
        return {}
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}
    data = yaml.safe_load(parts[1]) or {}
    if not isinstance(data, dict):
        return {}
    return data


def _routing_text(
    effective_request: str,
    clarifications: dict[str, Any] | None,
    project_state: dict[str, Any] | None,
) -> str:
    parts = [effective_request or ""]
    if clarifications:
        parts.extend(str(value) for value in clarifications.values())
    if isinstance(project_state, dict):
        agent_context = project_state.get("agent_context") if isinstance(project_state.get("agent_context"), dict) else {}
        if agent_context.get("prompt_only_creation"):
            parts.extend(["prompt_only_creation", "生成视频", "空画布", "视频创作"])
        if agent_context.get("creative_mode") == "reference_guided" or project_state.get("reference_assets") or project_state.get("referenceAssets"):
            parts.extend(["参考资料", "reference assets", "创作运行时"])
        clips = project_state.get("clips") if isinstance(project_state.get("clips"), list) else []
        kinds = {
            str(clip.get("mediaKind") or clip.get("media_kind") or "").lower()
            for clip in clips
            if isinstance(clip, dict)
        }
        parts.extend(kind for kind in kinds if kind)
    return " ".join(parts)


def _is_prompt_only_project(project_state: dict[str, Any] | None) -> bool:
    if not isinstance(project_state, dict):
        return False
    agent_context = project_state.get("agent_context") if isinstance(project_state.get("agent_context"), dict) else {}
    return bool(agent_context.get("prompt_only_creation") or agent_context.get("mode") == "prompt_only_creation")


def _expand_text(text: str) -> str:
    lowered = (text or "").lower()
    extras: list[str] = []
    for key, synonyms in _SYNONYMS.items():
        if key.lower() in lowered:
            extras.extend(synonyms)
    wants_to_find = any(word in lowered for word in ("找一个", "找一段", "找段", "寻找", "搜索"))
    mentions_media = any(word in lowered for word in ("视频", "素材", "b-roll", "broll", "footage", "photo", "图片"))
    if wants_to_find and mentions_media:
        extras.extend(["找素材", "搜索素材", "公开视频"])
    wants_insert = any(word in lowered for word in ("插入", "插到", "放到", "放进", "加到"))
    mentions_timeline = any(word in lowered for word in ("中间", "之间", "二段", "两段", "片段", "素材", "时间轴"))
    if wants_insert and mentions_timeline:
        extras.extend(["拼接", "时间线"])
    wants_tracking = any(word in lowered for word in ("跟踪", "追踪", "track", "tracking", "锁定"))
    mentions_face_or_subject = any(word in lowered for word in ("人脸", "脸", "面部", "face", "人物", "主体"))
    if wants_tracking and mentions_face_or_subject:
        extras.extend(["人脸跟踪", "face tracking", "track face"])
    return lowered + " " + " ".join(extras).lower()


def _sorted_triggers(triggers: Iterable[str]) -> list[str]:
    return sorted((str(item).strip().lower() for item in triggers if str(item).strip()), key=len, reverse=True)


def _contains_trigger(text: str, trigger: str) -> bool:
    if not trigger:
        return False
    return trigger.lower() in text


def _llm_fallback_enabled(llm_fallback: SkillFallback | None) -> bool:
    if llm_fallback is None:
        return False
    return os.environ.get("GEMIA_SKILL_LLM_FALLBACK", "0") == "1"


def _known_skill_ids(skill_ids: Iterable[str]) -> list[str]:
    known = load_skill_metadata()
    out: list[str] = []
    for skill_id in skill_ids:
        value = str(skill_id).strip()
        if value in known and value not in out:
            out.append(value)
    return out
