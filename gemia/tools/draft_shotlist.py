"""draft_shotlist -- turn a one-line theme into a full promo storyboard.

Give it a single sentence ("一句话主题") and it drafts a COMPLETE shotlist —
scenes → shots with durations, on-screen text, voiceover (`narration`), mood
tags, and per-shot `search_query` — following a proven structure
(Hook → Problem → Solution → Highlights → CTA, or a narrative arc), then
persists it through the same ``set_shotlist`` patch op the model uses by hand.
The normal flow takes over from there: fill each shot (search_frames /
search_media / generate_*), ``assemble_shotlist``, then ``refine_shot``.

It is a SCAFFOLD, not a finished plan: wording, footage, timings, and
transitions are meant to be revised. Nothing renders here; drafting is free.
Language (zh/en) is auto-detected from the theme unless overridden.
"""
from __future__ import annotations

from typing import Any

from gemia.project_model import iter_shots
from gemia.tools._context import ToolContext

_DEFAULT_TARGET = 30.0
_MIN_SHOT = 1.5


def _project(ctx: ToolContext):
    if ctx.project is None:
        raise ValueError("draft_shotlist needs a project-backed session (ctx.project is None)")
    return ctx.project


def _detect_lang(theme: str, explicit: Any) -> str:
    if isinstance(explicit, str) and explicit.strip().lower() in ("zh", "en"):
        return explicit.strip().lower()
    return "zh" if any("一" <= c <= "鿿" for c in (theme or "")) else "en"


def _promo_beats(zh: bool) -> list[dict[str, Any]]:
    def L(z: str, e: str) -> str:
        return z if zh else e
    return [
        {"title": L("开场钩子", "Hook"), "weight": 0.15, "mood": "energetic",
         "transition": "dissolve",
         "shots": [{"desc": L("抓人眼球的开场镜头，一眼点出主题", "A striking opening shot that states the theme at a glance"),
                    "screen": "theme",
                    "vo": L("用一句话抛出主题，制造好奇", "Open with the theme in one line; create curiosity"),
                    "q": L("震撼 开场 空镜", "striking cinematic opening")}]},
        {"title": L("痛点", "Problem"), "weight": 0.18, "mood": "tense",
         "transition": "dissolve",
         "shots": [{"desc": L("呈现观众正面临的问题或痛点", "Show the problem the audience is facing"),
                    "vo": L("点明痛点，让观众产生共鸣", "Name the pain point so the viewer relates"),
                    "q": L("困扰 问题 场景", "frustration problem scene")}]},
        {"title": L("解决方案", "Solution"), "weight": 0.25, "mood": "hopeful",
         "transition": "cut",
         "shots": [{"desc": L("引出方案/产品——转折点", "Introduce the solution/product — the turn"),
                    "vo": L("方案登场，给出希望", "The solution arrives; offer hope"),
                    "q": L("方案 揭晓 产品", "solution reveal product")},
                   {"desc": L("展示方案如何运作", "Show the solution working"),
                    "vo": L("简述它如何解决那个问题", "Briefly show how it solves that problem"),
                    "q": L("演示 使用 流畅", "in action demo smooth")}]},
        {"title": L("亮点", "Highlights"), "weight": 0.27, "mood": "confident",
         "transition": "cut",
         "shots": [{"desc": L("核心亮点一：最强卖点", "Highlight one: the strongest benefit"),
                    "vo": L("强调第一个关键亮点", "Emphasize the first key benefit"),
                    "q": L("亮点 特写 质感", "feature highlight closeup")},
                   {"desc": L("核心亮点二：情感或成果", "Highlight two: emotion or result"),
                    "vo": L("再加一个亮点，把情绪推向高潮", "Add a second highlight; build to a peak"),
                    "q": L("成果 满意 结果", "result satisfied outcome")}]},
        {"title": L("行动号召", "Call to action"), "weight": 0.15, "mood": "inviting",
         "transition": "cut",
         "shots": [{"desc": L("品牌/logo 收尾 + 明确的行动号召", "Brand/logo close with a clear call to action"),
                    "screen": "cta",
                    "vo": L("给出明确的下一步", "Give a clear next step"),
                    "q": L("logo 收尾 号召", "logo outro call to action")}]},
    ]


def _story_beats(zh: bool) -> list[dict[str, Any]]:
    def L(z: str, e: str) -> str:
        return z if zh else e
    return [
        {"title": L("铺垫", "Setup"), "weight": 0.18, "mood": "calm", "transition": "dissolve",
         "shots": [{"desc": L("建立人物与日常，交代主题背景", "Establish the character and the everyday; set up the theme"),
                    "screen": "theme",
                    "vo": L("平静地引入主题世界", "Calmly introduce the world of the theme"),
                    "q": L("日常 生活 环境", "everyday life establishing")}]},
        {"title": L("推进", "Rising"), "weight": 0.22, "mood": "curious", "transition": "cut",
         "shots": [{"desc": L("出现张力或改变的契机", "Tension or the chance to change appears"),
                    "vo": L("something shifts — 引出改变的动机", "Something shifts; motivate the change"),
                    "q": L("变化 契机 转折", "change turning moment")}]},
        {"title": L("转折", "Turn"), "weight": 0.25, "mood": "hopeful", "transition": "cut",
         "shots": [{"desc": L("主题/产品带来关键转折", "The theme/product delivers the key turn"),
                    "vo": L("关键转折发生", "The key turn happens"),
                    "q": L("突破 转折 关键", "breakthrough key turn")},
                   {"desc": L("展示转折后的不同", "Show what is different after the turn"),
                    "vo": L("展示改变带来的不同", "Show the difference the change makes"),
                    "q": L("对比 之后 不同", "after contrast different")}]},
        {"title": L("高潮", "Climax"), "weight": 0.20, "mood": "triumphant", "transition": "cut",
         "shots": [{"desc": L("情感高点/最佳成果画面", "The emotional high / best-outcome shot"),
                    "vo": L("推向情感高点", "Drive to the emotional high"),
                    "q": L("高潮 成果 喜悦", "climax triumph joy")}]},
        {"title": L("收束", "Resolution"), "weight": 0.15, "mood": "inviting", "transition": "cut",
         "shots": [{"desc": L("回到主题 + 行动号召/落版", "Return to the theme + call to action / endplate"),
                    "screen": "cta",
                    "vo": L("落回主题，给出邀请", "Land back on the theme; extend an invitation"),
                    "q": L("落版 号召 收尾", "endplate call to action")}]},
    ]


_TEMPLATES = {"promo": _promo_beats, "story": _story_beats}


def _allocate(beats: list[dict[str, Any]], target: float) -> list[float]:
    """Per-shot seconds: split each beat's (weight*target) across its shots,
    floor at _MIN_SHOT, then nudge the longest shot so the total hits target."""
    secs: list[float] = []
    for beat in beats:
        n = max(1, len(beat["shots"]))
        per = target * float(beat["weight"]) / n
        for _ in beat["shots"]:
            secs.append(max(_MIN_SHOT, round(per, 1)))
    delta = round(target - sum(secs), 1)
    if abs(delta) >= 0.1 and secs:
        i = secs.index(max(secs))
        secs[i] = max(_MIN_SHOT, round(secs[i] + delta, 1))
    return secs


def build_shotlist(theme: str, *, template: str = "promo",
                   target_duration_sec: float = _DEFAULT_TARGET,
                   style: str | None = None, lang: str = "en") -> dict[str, Any]:
    """Pure builder (no I/O): theme + template -> a normalized-shape shotlist dict."""
    theme = (theme or "").strip()
    zh = lang == "zh"
    beats_fn = _TEMPLATES.get(template, _promo_beats)
    beats = beats_fn(zh)
    target = float(target_duration_sec) if target_duration_sec else _DEFAULT_TARGET
    secs = _allocate(beats, target)

    cta_text = "了解更多 →" if zh else "Learn more →"
    scenes: list[dict[str, Any]] = []
    si = 0
    k = 0
    for bi, beat in enumerate(beats):
        shots: list[dict[str, Any]] = []
        n = len(beat["shots"])
        for j, tpl in enumerate(beat["shots"]):
            role = tpl.get("screen")
            on_screen = None
            if role == "theme":
                on_screen = theme
            elif role == "cta":
                on_screen = cta_text
            # transition only between shots, not after the very last shot overall
            is_last_overall = (bi == len(beats) - 1) and (j == n - 1)
            trans = None
            kind = beat.get("transition", "cut")
            if not is_last_overall and kind and kind != "cut":
                trans = {"kind": kind, "duration_sec": 0.5}
            query = f"{theme} {tpl['q']}".strip() if theme else tpl["q"]
            shots.append({
                "id": f"s{bi}_shot{j}",
                "description": tpl["desc"],
                "duration_sec": secs[k],
                "on_screen_text": on_screen,
                "narration": tpl.get("vo"),
                "mood": beat.get("mood"),
                "source": "search",
                "search_query": query,
                "transition_after": trans,
                "status": "draft",
            })
            k += 1
        scenes.append({"id": f"scene{bi}", "title": beat["title"], "shots": shots})
        si += 1

    logline = theme or ("Untitled promo" if not zh else "未命名宣传片")
    return {
        "logline": logline,
        "style": (style or "").strip(),
        "target_duration_sec": round(target, 3),
        "scenes": scenes,
    }


async def dispatch(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    from gemia.tools import shotlist as _shotlist

    theme = str(args.get("theme") or "").strip()
    if not theme:
        raise ValueError("draft_shotlist requires a non-empty 'theme' (one line describing the video)")
    template = str(args.get("template") or "promo").strip().lower()
    if template not in _TEMPLATES:
        raise ValueError(f"unknown template {template!r}; use one of {sorted(_TEMPLATES)}")
    target = args.get("target_duration_sec")
    try:
        target = float(target) if target not in (None, "") else _DEFAULT_TARGET
    except (TypeError, ValueError):
        target = _DEFAULT_TARGET
    style = args.get("style")
    lang = _detect_lang(theme, args.get("language"))
    replace = bool(args.get("replace", True))

    shotlist = build_shotlist(
        theme, template=template, target_duration_sec=target,
        style=str(style) if style else None, lang=lang,
    )

    shot_count = sum(1 for _ in iter_shots(shotlist))
    planned = sum(float(s.get("duration_sec") or 0) for _sc, s in iter_shots(shotlist))

    if not replace:
        return {
            "drafted": True, "persisted": False, "template": template, "language": lang,
            "shot_count": shot_count, "planned_sec": round(planned, 1),
            "shotlist": shotlist,
            "shotlist_text": _shotlist.render_shotlist_text(shotlist),
            "summary": f"drafted {shot_count}-shot '{template}' storyboard (~{planned:.0f}s) — NOT persisted (replace=false)",
        }

    project = _project(ctx)
    result = project.apply_ops([{"op": "set_shotlist", "shotlist": shotlist}], label="draft_shotlist")
    stored = (project.load() or {}).get("shotlist") or {}
    return {
        "drafted": True, "persisted": True, "template": template, "language": lang,
        "seq": result.get("patch_seq_end"),
        "shot_count": sum(1 for _ in iter_shots(stored)),
        "planned_sec": round(planned, 1),
        "shotlist_text": _shotlist.render_shotlist_text(stored),
        "summary": (
            f"drafted a {shot_count}-shot '{template}' storyboard (~{planned:.0f}s target {target:.0f}s) "
            "and set it as the shotlist — now fill each shot (search_frames/search_media/generate), "
            "then assemble_shotlist. Refine wording/footage/timings before rendering."
        ),
    }


__all__ = ["dispatch", "build_shotlist"]
