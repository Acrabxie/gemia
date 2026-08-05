"""draft_quanta -- scaffold a full quanta IR in one call, two ways.

Theme mode: give it a single sentence ("一句话主题") and it drafts a COMPLETE
quanta — slides with semantic blocks (text/stat/image/shape/group), speaker
notes, build states with dwell times, and a default_path — following a proven
structure (pitch: Hook → Problem → Solution → Highlights → Numbers → CTA;
report / teach are the analysis and lesson arcs), then persists it through
the same ``set_quanta`` patch op the model uses by hand.

from_shotlist mode: converts the CURRENT storyboard into a quanta using the
spec §2.2 migration table (shot → slide, narration → notes, on_screen_text →
title/text block, shot footage → image block, mood mode → theme.mood,
duration → the slide's single build dwell) plus an auto title cover. This is
the first concrete video↔quanta synergy: plan a video, get its quanta for free.

It is a SCAFFOLD, not a finished quanta: wording, stats, imagery, and dwell
times are meant to be revised with ``update_quantum``. Nothing renders here;
drafting is free. Language (zh/en) is auto-detected unless overridden.
"""
from __future__ import annotations

from collections import Counter
from typing import Any

from gemia.errors import RECOVERY_SWITCH_TOOL, ToolError
from gemia.project_model import iter_shots
from gemia.tools._context import ToolContext


def _project(ctx: ToolContext):
    if ctx.project is None:
        raise ValueError("draft_quanta needs a project-backed session (ctx.project is None)")
    return ctx.project


def _has_cjk(text: str) -> bool:
    return any("一" <= c <= "鿿" for c in (text or ""))


def _detect_lang(sample: str, explicit: Any) -> str:
    if isinstance(explicit, str) and explicit.strip().lower() in ("zh", "en"):
        return explicit.strip().lower()
    return "zh" if _has_cjk(sample) else "en"


# ── structure templates (theme mode) ──────────────────────────────────────
def _text(text: str, *, role: str = "body", bullets: list[str] | None = None,
          style_token: str | None = None) -> dict[str, Any]:
    block: dict[str, Any] = {"kind": "text", "role": role, "text": text}
    if bullets:
        block["bullets"] = bullets
    if style_token:
        block["style_token"] = style_token
    return block


def _bullets(items: list[str]) -> dict[str, Any]:
    """A progressively revealable bullet list.

    Bullets are individual text leaves under a semantic group, rather than
    strings trapped inside one text block. Build snapshots can therefore reveal
    them one by one while the group still gives the layout engine one list slot.
    """
    return {
        "kind": "group",
        "role": "bullets",
        "children": [_text(item, role="bullet") for item in items],
    }


def _assign_draft_block_ids(blocks: list[dict[str, Any]], path: tuple[int, ...] = ()) -> None:
    for index, block in enumerate(blocks):
        block_path = (*path, index + 1)
        block["id"] = "blk_" + "_".join(str(part) for part in block_path)
        if block.get("kind") == "group":
            children = block.get("children")
            if isinstance(children, list):
                _assign_draft_block_ids(children, block_path)


def _draft_leaf_ids(blocks: list[dict[str, Any]]) -> list[str]:
    ids: list[str] = []
    for block in blocks:
        if block.get("kind") == "group":
            children = block.get("children")
            if isinstance(children, list):
                ids.extend(_draft_leaf_ids(children))
        else:
            ids.append(str(block["id"]))
    return ids


def _finalize_draft_slides(slides: list[dict[str, Any]]) -> None:
    """Materialize deterministic ids and full cumulative build snapshots."""
    for slide in slides:
        blocks = slide.get("blocks") if isinstance(slide.get("blocks"), list) else []
        _assign_draft_block_ids(blocks)
        leaf_ids = _draft_leaf_ids(blocks)
        builds = slide.get("builds") if isinstance(slide.get("builds"), list) else []
        if not builds:
            builds = [{"id": "b1", "dwell_sec": 3.0}]
            slide["builds"] = builds
        build_count = len(builds)
        leaf_count = len(leaf_ids)
        for index, build in enumerate(builds):
            build["id"] = f"b{index + 1}"
            # Evenly distribute authored leaves across builds. The snapshot is
            # cumulative and the final threshold is always the exact leaf set.
            threshold = (
                (leaf_count * (index + 1) + build_count - 1) // build_count
                if leaf_count
                else 0
            )
            build["visible_block_ids"] = leaf_ids[:threshold]


def _pitch_slides(theme: str, zh: bool) -> tuple[str, list[dict[str, Any]]]:
    def L(z: str, e: str) -> str:
        return z if zh else e
    cta = L("了解更多 →", "Learn more →")
    slides = [
        {"layout": "title", "title": theme,
         "blocks": [
             _text(theme, role="title", style_token="type.display"),
             _text(L("一句话讲清它为什么值得看", "One line on why this matters"), role="subtitle"),
             {"kind": "shape", "shape": "rect", "role": "accent", "fill_token": "color.accent"},
         ],
         "notes": L("开场：一句话抛出主题，停一拍让标题落地。",
                    "Open with the theme in one line; pause so the title lands."),
         "builds": [{"id": "b1", "dwell_sec": 2.5}]},
        {"layout": "content", "title": L("痛点", "The problem"),
         "blocks": [_bullets([
             L("现状的第一个摩擦点", "The first friction in the status quo"),
             L("它带来的实际代价", "What it actually costs"),
             L("为什么现有做法不够", "Why current approaches fall short"),
         ])],
         "notes": L("点明观众正面临的问题，用一个具体场景引起共鸣。",
                    "Name the problem the audience faces; make it concrete."),
         "builds": [{"id": "b1", "dwell_sec": 1.2}, {"id": "b2", "dwell_sec": 1.2},
                    {"id": "b3", "dwell_sec": 1.6}]},
        {"layout": "content", "title": L("方案", "The solution"),
         "blocks": [
             _text(L("用一句话说清方案如何化解上一页的痛点",
                     "One sentence on how this dissolves the problem"), role="body"),
             {"kind": "image", "role": "hero", "source": "search",
              "query": L(f"{theme} 产品界面", f"{theme} product interface")},
         ],
         "notes": L("方案登场——这是转折点，语气从紧到松。",
                    "The solution arrives — this is the turn; ease the tone."),
         "builds": [{"id": "b1", "dwell_sec": 3.5}]},
        {"layout": "content", "title": L("产品亮点", "Highlights"),
         "blocks": [{"kind": "group", "role": "cards", "children": [
             _text(L("亮点一：最强卖点", "Highlight one: the strongest benefit"), role="card"),
             _text(L("亮点二：差异化能力", "Highlight two: what nothing else does"), role="card"),
             _text(L("亮点三：上手零门槛", "Highlight three: zero friction to start"), role="card"),
         ]}],
         "notes": L("三个亮点逐张展开，每张只讲一件事。",
                    "Reveal the three cards one by one; one idea per card."),
         "builds": [{"id": "b1", "dwell_sec": 1.2}, {"id": "b2", "dwell_sec": 1.2},
                    {"id": "b3", "dwell_sec": 1.6}]},
        {"layout": "stat", "title": L("数据", "By the numbers"),
         "blocks": [
             {"kind": "stat", "value": "3×", "label": L("更快的产出", "faster output")},
             {"kind": "stat", "value": "90%", "label": L("流程自动化", "of the flow automated")},
             {"kind": "stat", "value": L("1 句话", "1 line"), "label": L("即可开始", "to get started")},
         ],
         "notes": L("用数字支撑主张；逐个强调，别一次念完。",
                    "Back the claim with numbers; land them one at a time."),
         "builds": [{"id": "b1", "dwell_sec": 1.0}, {"id": "b2", "dwell_sec": 1.0},
                    {"id": "b3", "dwell_sec": 1.5}]},
        {"layout": "full-bleed", "title": cta,
         "blocks": [
             _text(cta, role="cta", style_token="type.display"),
             {"kind": "shape", "shape": "rect", "role": "accent", "fill_token": "color.accent"},
         ],
         "notes": L("收尾：给出明确的下一步行动。", "Close with one clear next step."),
         "builds": [{"id": "b1", "dwell_sec": 2.5}]},
    ]
    return "confident", slides


def _report_slides(theme: str, zh: bool) -> tuple[str, list[dict[str, Any]]]:
    def L(z: str, e: str) -> str:
        return z if zh else e
    slides = [
        {"layout": "title", "title": theme,
         "blocks": [_text(theme, role="title", style_token="type.display"),
                    _text(L("结论先行的一页式摘要", "A conclusions-first summary"), role="subtitle")],
         "notes": L("开场：报告主题与时间范围。", "Open with the topic and the period covered."),
         "builds": [{"id": "b1", "dwell_sec": 2.5}]},
        {"layout": "content", "title": L("摘要", "Executive summary"),
         "blocks": [_bullets([
             L("最重要的结论", "The single most important conclusion"),
             L("支撑它的关键证据", "The key evidence behind it"),
             L("建议采取的行动", "The action it implies"),
         ])],
         "notes": L("三十秒版本：先给结论，再给证据。", "The 30-second version: conclusion first."),
         "builds": [{"id": "b1", "dwell_sec": 1.0}, {"id": "b2", "dwell_sec": 1.0},
                    {"id": "b3", "dwell_sec": 1.5}]},
        {"layout": "content", "title": L("关键发现", "Key findings"),
         "blocks": [{"kind": "image", "role": "evidence", "source": "search",
                     "query": L(f"{theme} 图表", f"{theme} chart")},
                    _bullets([
                        L("发现一", "Finding one"), L("发现二", "Finding two"),
                        L("发现三", "Finding three"),
                    ])],
         "notes": L("每条发现配一句解释，不展开细节。", "One explaining sentence per finding."),
         "builds": [{"id": "b1", "dwell_sec": 1.5}, {"id": "b2", "dwell_sec": 1.5},
                    {"id": "b3", "dwell_sec": 1.5}]},
        {"layout": "stat", "title": L("数据", "The numbers"),
         "blocks": [
             {"kind": "stat", "value": "—", "label": L("核心指标一", "primary metric")},
             {"kind": "stat", "value": "—", "label": L("核心指标二", "secondary metric")},
         ],
         "notes": L("替换为真实指标；只放能记住的数字。", "Swap in real metrics; only memorable numbers."),
         "builds": [{"id": "b1", "dwell_sec": 1.2}, {"id": "b2", "dwell_sec": 1.3}]},
        {"layout": "content", "title": L("风险与建议", "Risks and recommendations"),
         "blocks": [_bullets([
             L("主要风险及其对策", "The main risk and its mitigation"),
             L("下一步建议", "Recommended next step"),
         ])],
         "notes": L("诚实列风险，给出对应动作。", "Be honest about risk; pair each with an action."),
         "builds": [{"id": "b1", "dwell_sec": 1.5}, {"id": "b2", "dwell_sec": 1.5}]},
        {"layout": "full-bleed", "title": L("结论", "Conclusion"),
         "blocks": [_text(L("一句话结论 + 需要谁做什么决定", "The one-line conclusion + the decision needed"),
                          role="cta", style_token="type.display")],
         "notes": L("收在决定上：需要谁、在何时、拍板什么。", "End on the decision: who decides what, when."),
         "builds": [{"id": "b1", "dwell_sec": 2.5}]},
    ]
    return "calm", slides


def _teach_slides(theme: str, zh: bool) -> tuple[str, list[dict[str, Any]]]:
    def L(z: str, e: str) -> str:
        return z if zh else e
    slides = [
        {"layout": "title", "title": theme,
         "blocks": [_text(theme, role="title", style_token="type.display"),
                    _text(L("十分钟讲明白", "Clear in ten minutes"), role="subtitle")],
         "notes": L("开场：这节课解决什么问题。", "Open with the question this lesson answers."),
         "builds": [{"id": "b1", "dwell_sec": 2.5}]},
        {"layout": "content", "title": L("学习目标", "What you'll learn"),
         "blocks": [_bullets([
             L("目标一：理解核心概念", "Understand the core concept"),
             L("目标二：看懂一个真实例子", "Read one real example"),
             L("目标三：能自己动手复现", "Reproduce it yourself"),
         ])],
         "notes": L("先立预期：学完能做到什么。", "Set expectations: what they can do afterwards."),
         "builds": [{"id": "b1", "dwell_sec": 1.0}, {"id": "b2", "dwell_sec": 1.0},
                    {"id": "b3", "dwell_sec": 1.0}]},
        {"layout": "content", "title": L("核心概念", "The core concept"),
         "blocks": [
             _text(L("用一个类比讲清概念本身", "Explain the concept through one analogy"), role="body"),
             {"kind": "image", "role": "diagram", "source": "search",
              "query": L(f"{theme} 示意图", f"{theme} diagram")},
         ],
         "notes": L("一个概念一页；类比先行，术语后置。", "One concept per slide; analogy before jargon."),
         "builds": [{"id": "b1", "dwell_sec": 2.0}, {"id": "b2", "dwell_sec": 2.5}]},
        {"layout": "content", "title": L("例子", "A worked example"),
         "blocks": [{"kind": "group", "role": "cards", "children": [
             _text(L("步骤一", "Step one"), role="card"),
             _text(L("步骤二", "Step two"), role="card"),
             _text(L("步骤三", "Step three"), role="card"),
         ]}],
         "notes": L("边讲边指：每一步对应画面上的一张卡。", "Point as you go: one card per step."),
         "builds": [{"id": "b1", "dwell_sec": 1.5}, {"id": "b2", "dwell_sec": 1.5},
                    {"id": "b3", "dwell_sec": 1.5}]},
        {"layout": "content", "title": L("回顾", "Recap"),
         "blocks": [_bullets([
             L("刚才学了什么", "What we covered"),
             L("最容易踩的坑", "The most common mistake"),
         ])],
         "notes": L("用提问式回顾巩固记忆。", "Recap with questions, not restatement."),
         "builds": [{"id": "b1", "dwell_sec": 1.2}, {"id": "b2", "dwell_sec": 1.3}]},
        {"layout": "full-bleed", "title": L("动手试试", "Try it yourself"),
         "blocks": [_text(L("动手试试 →", "Try it yourself →"), role="cta",
                          style_token="type.display")],
         "notes": L("布置一个五分钟内能完成的小练习。", "Assign one exercise doable in five minutes."),
         "builds": [{"id": "b1", "dwell_sec": 2.5}]},
    ]
    return "friendly", slides


_TEMPLATES = {"pitch": _pitch_slides, "report": _report_slides, "teach": _teach_slides}


def build_quanta(theme: str, *, template: str = "pitch", lang: str = "en") -> dict[str, Any]:
    """Pure builder (no I/O): theme + template -> a normalized-shape quanta dict."""
    theme = (theme or "").strip()
    zh = lang == "zh"
    slides_fn = _TEMPLATES.get(template, _pitch_slides)
    mood, slides = slides_fn(theme, zh)
    for i, slide in enumerate(slides):
        slide["id"] = f"s{i + 1}"
        slide["transition"] = {"kind": "cut"}
        slide["links"] = []
    _finalize_draft_slides(slides)
    return {
        "version": 1,
        "theme": {"tokens": {}, "mood": mood, "aspect": "16:9"},
        "slides": slides,
        "default_path": [slide["id"] for slide in slides],
    }


# ── from_shotlist migration (spec §2.2) ───────────────────────────────────
def quanta_from_shotlist(shotlist: dict[str, Any]) -> dict[str, Any]:
    """Pure mapper (no I/O): the current storyboard -> a quanta dict.

    shot → slide; narration → notes; on_screen_text → title + text block;
    shot footage (asset_id / search_query / description) → image block;
    mood mode → quanta-level theme.mood; duration → the slide's single build
    dwell; plus an auto title cover as slide one.
    """
    shots = [shot for _scene, shot in iter_shots(shotlist)]
    logline = str((shotlist or {}).get("logline") or "").strip()
    sample = logline + "".join(
        str(s.get("narration") or "") + str(s.get("on_screen_text") or "") for s in shots
    )
    zh = _has_cjk(sample)
    title = logline or ("未命名演示" if zh else "Untitled quanta")

    slides: list[dict[str, Any]] = [{
        "id": "s1", "layout": "title", "title": title,
        "blocks": [_text(title, role="title", style_token="type.display")],
        "notes": "开场：一句话点出主题。" if zh else "Open by stating the theme in one line.",
        "builds": [{"id": "b1", "dwell_sec": 2.5}],
        "links": [], "transition": {"kind": "cut"},
    }]
    for i, shot in enumerate(shots):
        on_screen = str(shot.get("on_screen_text") or "").strip()
        blocks: list[dict[str, Any]] = []
        if on_screen:
            blocks.append(_text(on_screen, role="title"))
        image: dict[str, Any] = {"kind": "image", "role": "hero"}
        if shot.get("asset_id"):
            image["asset_id"] = str(shot["asset_id"])
            image["source"] = str(shot.get("source") or "search")
        else:
            image["source"] = str(shot.get("source") or "search")
            query = str(shot.get("search_query") or shot.get("description") or "").strip()
            if query:
                image["query"] = query
        blocks.append(image)
        transition_after = shot.get("transition_after") or {}
        kind = str(transition_after.get("kind") or "cut") if isinstance(transition_after, dict) else "cut"
        slides.append({
            "id": f"s{i + 2}",
            "layout": "content",
            "title": on_screen,
            "blocks": blocks,
            "notes": str(shot.get("narration") or ""),
            "builds": [{"id": "b1",
                        "dwell_sec": max(0.1, float(shot.get("duration_sec") or 3.0))}],
            "links": [],
            # quanta v1 transitions are cut|fade; dissolve/fade map to fade.
            "transition": {"kind": "fade" if kind in ("dissolve", "fade") else "cut"},
        })

    moods = [str(shot.get("mood")) for shot in shots if shot.get("mood")]
    mood = Counter(moods).most_common(1)[0][0] if moods else ""
    _finalize_draft_slides(slides)
    return {
        "version": 1,
        "theme": {"tokens": {}, "mood": mood, "aspect": "16:9"},
        "slides": slides,
        "default_path": [slide["id"] for slide in slides],
    }


async def dispatch(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    from gemia.tools import quanta as _quanta

    project = _project(ctx)
    from_shotlist = bool(args.get("from_shotlist", False))
    replace = bool(args.get("replace", True))

    if from_shotlist:
        shotlist = (project.load() or {}).get("shotlist") or {}
        if not any(True for _ in iter_shots(shotlist)):
            raise ToolError(
                "the shotlist is empty — there is nothing to convert into a quanta",
                code="E_NOT_FOUND",
                recovery=RECOVERY_SWITCH_TOOL,
                hint="draft_shotlist/set_shotlist first, or call draft_quanta with a 'theme' instead",
            )
        drafted = quanta_from_shotlist(shotlist)
        source = "shotlist"
        lang = "zh" if _has_cjk(str(shotlist.get("logline") or "")) else "en"
        template = None
    else:
        theme = str(args.get("theme") or "").strip()
        if not theme:
            raise ValueError(
                "draft_quanta requires a non-empty 'theme' (one line describing the quanta), "
                "or from_shotlist=true to convert the current storyboard"
            )
        template = str(args.get("template") or "pitch").strip().lower()
        if template not in _TEMPLATES:
            raise ValueError(f"unknown template {template!r}; use one of {sorted(_TEMPLATES)}")
        lang = _detect_lang(theme, args.get("language"))
        drafted = build_quanta(theme, template=template, lang=lang)
        source = "theme"

    scope_count = len(drafted["slides"])
    state_count = sum(
        len(slide.get("builds") or []) or 1 for slide in drafted["slides"]
    )
    if not replace:
        return {
            "drafted": True, "persisted": False, "source": source,
            "template": template, "language": lang,
            "scope_count": scope_count, "state_count": state_count,
            "quanta": drafted,
            "quanta_text": _quanta.render_quanta_text(drafted),
            "summary": f"drafted a {scope_count}-scope quanta from {source} — NOT persisted (replace=false)",
        }

    result = project.apply_ops([{"op": "set_quanta", "quanta": drafted}], label="draft_quanta")
    stored = (project.load() or {}).get("quanta") or {}
    stored_scopes, stored_states = _quanta._counts(stored)
    return {
        "drafted": True, "persisted": True, "source": source,
        "template": template, "language": lang,
        "seq": result.get("patch_seq_end"),
        "scope_count": stored_scopes, "state_count": stored_states,
        "quanta_text": _quanta.render_quanta_text(stored),
        "summary": (
            f"drafted a {scope_count}-scope quanta from {source} and set it as the quanta — "
            "now refine wording/blocks/dwell per scope with update_quantum "
            "(get_quanta to re-read ids). A scaffold: revise before presenting."
        ),
    }


__all__ = ["dispatch", "build_quanta", "quanta_from_shotlist"]
