"""Deterministic turn-intent and clarification controls for the v3 loop.

This module classifies message intent for presentation only. The classifier
never withholds tools and never decides whether a turn is complete: a
model-authored no-tool response always ends naturally.

``ClarificationGuard`` is intended to be instantiated once per user turn (or
reset between turns).  It preserves a structured decision so callers can emit
or persist the policy result without reconstructing it from prose.
"""
from __future__ import annotations

import copy
import re
import unicodedata
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping


class TurnIntent(str, Enum):
    """Presentation intent used to handle streamed prose/activity markup."""

    CONVERSATION = "conversation"
    INFORMATION = "information"
    ACTIONABLE = "actionable"
    PLAN = "plan"

    @classmethod
    def classify(cls, text: str) -> "TurnIntent":
        """Classify ``text`` using the conservative host policy."""

        return classify_turn_intent(text)


def _normalize(text: str) -> str:
    value = unicodedata.normalize("NFKC", str(text or "")).casefold().strip()
    while value and unicodedata.category(value[0]).startswith("P"):
        value = value[1:].lstrip()
    while value and unicodedata.category(value[-1]).startswith("P"):
        value = value[:-1].rstrip()
    return re.sub(r"\s+", " ", value)


_CONVERSATION_FULL = (
    re.compile(r"(?:你|您)好(?:呀|啊|啦|哇)?"),
    re.compile(r"(?:嗨|哈喽|哈啰|哈罗|早安|早上好|下午好|晚上好|晚安)(?:呀|啊|啦)?"),
    re.compile(r"(?:hello|hi|hey)(?: there)?"),
    re.compile(r"(?:谢谢(?:你|您)?|多谢|感谢(?:你|您)?|辛苦了)(?:呀|啊|啦)?"),
    re.compile(r"(?:thank you|thanks|thx)(?: so much)?"),
    re.compile(
        r"(?:停止|停下|停吧|停一下|先停一下|取消|取消吧|算了|不用了|不需要了|"
        r"别做了|不要继续了|先这样)(?:吧|了)?"
    ),
    re.compile(r"(?:stop(?: immediately| now)?|cancel(?: it)?|never mind|nevermind|that's all|that is all)(?: please)?"),
)

_INFORMATION_FULL = (
    re.compile(r"(?:你是谁|您是谁|你叫什么(?:名字)?|您叫什么(?:名字)?)"),
    re.compile(r"(?:你|您)(?:是干什么的|是做什么的|是什么)"),
    re.compile(r"(?:你|您)(?:是|用的|使用的)?(?:哪个|什么)模型"),
    re.compile(r"(?:当前|现在|这)(?:用的|使用的|是)?(?:哪个|什么)模型"),
    re.compile(r"(?:模型是什么|介绍一下你自己|自我介绍一下)"),
    re.compile(r"(?:who are you|what are you|what is your name)"),
    re.compile(r"(?:(?:what|which) model (?:are you|is this|are you using|do you use))"),
)

# Capability / existence / definitional questions. These ask *whether* a
# feature exists or *what* something is; the honest completion is a prose
# answer, not a goal mutation, so they route to INFORMATION (no tools, no
# ledger). Unlike the ``*_FULL`` allowlists these are substring searches — the
# feature name in the middle is open-ended (e.g. "你有 vector motion 吗"). They
# are consulted only AFTER the action-verb gate, so a request that names a
# concrete edit ("加字幕", "导出 4k") still wins as ACTIONABLE. Existence and
# definitional forms are preferred; bare imperative-in-question-form phrasings
# ("可以帮我做个 X 吗") are intentionally left to the actionable default.
_INFORMATION_QUESTION = (
    # Chinese — existence / possession: 有没有 X / 你有 X 吗 / 有 X 吗.
    # ``有`` is anchored to a subject (你/您) or the start so that "所有" /
    # "没有" / "还有" embedded in a request do not read as possession.
    re.compile(r"有没有\s*\S"),
    re.compile(r"(?:你|您)\s*有\s*\S.*?(?:吗|嘛|么|没|没有|啦|了吗)$"),
    re.compile(r"^有\s*\S.*?(?:吗|嘛|么|没|没有|啦|了吗)$"),
    # Chinese — capability inquiry: 能不能 / 会不会 / 可不可以 / 支不支持 / 是不是
    re.compile(r"(?:能不能|能否|会不会|可不可以|可以不可以|支不支持|是不是)"),
    re.compile(r"(?:你|您)(?:能|会|可以|支持|懂|知道)\s*\S.*?(?:吗|嘛|么)$"),
    # Chinese — definitional: 什么是 X / X 是什么 / X 是啥 / X 什么意思
    re.compile(r"^什么是\s*\S"),
    re.compile(
        r"\S\s*(?:是什么|是啥|是干嘛的?|是干什么的?|有什么用|有啥用|什么意思)"
        r"(?:呀|啊|呢|吗)?$"
    ),
    # Chinese — opinion / small-talk questions.  The honest outcome is a chat
    # answer; without these, "今天有什么想法？" defaulted to ACTIONABLE and the
    # the old completion gate pushed the model to invent deliverables (observed
    # 2026-07-17: an unrequested storyboard, an unrequested $0.10 artwork).
    # Only reached when no action verb is present, so "把这个想法做出来"
    # stays actionable.
    re.compile(r"(?:有什么|有啥|有没有什么)\s*(?:想法|打算|建议|安排|灵感|点子)"),
    re.compile(r"(?:你怎么看|你怎么想|怎么看待|你觉得|你认为)"),
    re.compile(r"^(?:随便)?(?:聊聊|聊会儿?天|说说话|唠唠)"),
    # English — existence / support / definitional / capability inquiry
    re.compile(r"\bdo(?:es)? (?:you|it|they) (?:have|support|offer|include|come with)\b"),
    re.compile(r"\bare you able to\b"),
    re.compile(r"\bis there (?:an?|any|a way)\b"),
    re.compile(r"\bwhat(?:'s| is| are)\b"),
    re.compile(r"\bhave you got\b"),
    re.compile(r"\bwhat do you think\b"),
    re.compile(r"\bany (?:thoughts|ideas|suggestions)\b"),
)

# An explicit request to execute an existing plan is actionable, not planning.
_PLAN_ONLY = (
    re.compile(r"(?:不要|无需|不用|暂不|先不|别).{0,6}执行"),
    re.compile(r"(?:只|仅).{0,6}(?:分析|计划|规划)"),
    re.compile(r"\b(?:do not|don't|without) execute\b"),
    re.compile(r"\bplan only\b"),
)

_PLAN_EXECUTION = (
    re.compile(r"(?:执行|实施|落实|照着|按照|按).{0,12}(?:计划|方案|步骤)"),
    re.compile(r"(?:计划|方案|步骤).{0,12}(?:执行|实施|落实|做出来|开始做)"),
    re.compile(r"\b(?:execute|implement|apply|carry out|proceed with|run)\b.{0,24}\bplan\b"),
)

_PLAN_SIGNALS = (
    re.compile(r"(?:计划|规划|方案|步骤|执行前分析|只分析|不要执行|暂不执行|计划模式)"),
    re.compile(r"\b(?:plan|planning|proposal|approach|outline the steps|do not execute|don't execute)\b"),
)

# High-confidence requests whose honest outcome is an explanation, not a
# workspace mutation.  These are checked before the generic action lexicon so
# a topic phrase such as "how to integrate" does not become execution merely
# because it contains ``integrate``.  An explicit execute-now marker still wins
# (see ``_has_explicit_execution`` below).
_INFORMATION_REQUEST = (
    re.compile(
        r"(?:我(?:只是)?(?:想|要)(?:了解|知道|问|咨询|弄清|理解)|"
        r"(?:请)?(?:解释|介绍|说明|讲讲|说说)(?:一下)?|"
        r"(?:是什么(?:架构|原理|流程|方案)?|是啥|什么意思)|"
        r"(?:怎么|如何|为什么|为何|是怎么).{0,80}(?:做|实现|接入|接进|集成|连接|"
        r"使用|工作|配置|授权|运作)|"
        r"(?:我说的是|我的意思是|我是说|纠正一下|准确地说)|"
        r"不是.{0,48}(?:而是|我是在问))"
    ),
    re.compile(
        r"\b(?:i mean|what i mean|i was asking about|to clarify|"
        r"i (?:just )?want to (?:understand|know|learn)|"
        r"explain|describe|tell me|why|"
        r"how (?:do|does|can|should|would|is|are))\b",
        re.I,
    ),
)

_EXPLICIT_EXECUTION_MARKER = (
    re.compile(
        r"(?:帮我|替我|给我|请(?:你)?(?:直接)?|直接|现在(?:就)?|马上|立刻|"
        r"开始|继续|照着|就按|按上面)"
    ),
    re.compile(
        r"\b(?:please|help me|go ahead|do it|start|continue|now|right now|"
        r"directly)\b",
        re.I,
    ),
)

_ACTION_CJK = (
    "帮我",
    "替我",
    "继续",
    "创建",
    "生成",
    "制作",
    "编辑",
    "剪辑",
    "剪片",
    "修改",
    "修复",
    "调整",
    "添加",
    "删除",
    "移除",
    "导入",
    "导出",
    "渲染",
    "分析",
    "搜索",
    "查找",
    "查询",
    "检查",
    "查看",
    "打开",
    "关闭",
    "保存",
    "下载",
    "上传",
    "发送",
    "发布",
    "安装",
    "运行",
    "执行",
    "接入",
    "接进",
    "接好",
    "集成",
    "连接",
    "重构",
    "写入",
    "写个",
    "画一个",
    "设计",
    "实现",
    "构建",
    "编译",
    "测试",
    "调试",
    "清理",
    "转换",
    "合并",
    "拆分",
    "裁剪",
    "配音",
    "字幕",
    "调色",
    "美化",
    "重试",
    "重做",
    "完成",
)

_ACTION_EN = re.compile(
    r"\b(?:create|make|build|edit|fix|change|modify|update|add|remove|delete|"
    r"import|export|render|analy[sz]e|inspect|search|find|open|close|save|"
    r"download|upload|send|publish|install|run|execute|refactor|write|draw|"
    r"design|implement|integrate|connect|wire|compile|test|debug|clean|convert|"
    r"merge|split|trim|crop|"
    r"caption|continue|retry|redo|finish)\b|\b(?:help me|color grade)\b"
)


def _full_match(patterns: tuple[re.Pattern[str], ...], text: str) -> bool:
    return any(pattern.fullmatch(text) is not None for pattern in patterns)


def _has_action(text: str) -> bool:
    return any(token in text for token in _ACTION_CJK) or _ACTION_EN.search(text) is not None


def _has_explicit_execution(text: str) -> bool:
    return _has_action(text) and any(
        pattern.search(text) for pattern in _EXPLICIT_EXECUTION_MARKER
    )


def classify_turn_intent(text: str) -> TurnIntent:
    """Return a presentation-only intent classification for one user message.

    Planning signals are recognized separately, but phrases that explicitly
    execute a plan remain actionable.  Explicit execute-now language wins over
    explanation/correction wording; high-confidence how-to, explanation, and
    correction forms then get a zero-tool INFORMATION turn.  Generic action
    verbs still win over the narrower capability/existence allowlist, so e.g.
    ``"谢谢，继续渲染"`` cannot be mistaken for a thank-you-only turn. Empty and
    unknown text remains actionable.
    """

    normalized = _normalize(text)
    if not normalized:
        return TurnIntent.ACTIONABLE
    if any(pattern.search(normalized) for pattern in _PLAN_ONLY):
        return TurnIntent.PLAN
    if any(pattern.search(normalized) for pattern in _PLAN_EXECUTION):
        return TurnIntent.ACTIONABLE
    if any(pattern.search(normalized) for pattern in _PLAN_SIGNALS):
        return TurnIntent.PLAN
    if _has_explicit_execution(normalized):
        return TurnIntent.ACTIONABLE
    if any(pattern.search(normalized) for pattern in _INFORMATION_REQUEST):
        return TurnIntent.INFORMATION
    if _has_action(normalized):
        return TurnIntent.ACTIONABLE
    if _full_match(_CONVERSATION_FULL, normalized):
        return TurnIntent.CONVERSATION
    if _full_match(_INFORMATION_FULL, normalized):
        return TurnIntent.INFORMATION
    if any(pattern.search(normalized) for pattern in _INFORMATION_QUESTION):
        return TurnIntent.INFORMATION
    return TurnIntent.ACTIONABLE


# ── staged-directive scoping ─────────────────────────────────────────
#
# "我想做一个宣传片，你先把logo找到" commands ONE step for this turn (find the
# logo); the film is stated future intent. The activity record/router scopes
# this turn to the commanded step while the full message still reaches the
# model, so the larger goal remains context rather than a forced obligation.

_SCOPED_MARKER = re.compile(
    r"(?<![优预事领抢率争祖原在提事])"
    r"(?:你|妳|您|请你?|麻烦你?|帮我)?(?:先|首先|第一步[，,：:]?)"
)
# 先-compounds that are vocabulary, not staging: never treat as a directive.
_SCOPED_FALSE_HEADS = re.compile(
    r"^(?:前|后|生|进|例|决|行|天|驱|导|锋|辈|人|知|见|兆|烈|贤)"
)
# "先别动 / 先不要改" inhibits action; there is no commanded step to scope to.
_SCOPED_INHIBITION = re.compile(r"^(?:别|不要|不用|不急|等等|等一下|暂停|停)")
_SCOPED_WISH_CONTEXT = re.compile(
    r"(?:我想|我要|我打算|我准备|我希望|想要|然后|接下来|之后|再\s|回头|"
    r"\b(?:i\s+want|i'd\s+like|i\s+plan|then|after\s+that|later)\b)",
    re.I,
)
_SCOPED_CLAUSE_END = re.compile(
    r"[。！？；.!?;\n]|[，,]\s*(?:然后|接着|之后|再|回头|完了|\bthen\b|\bafter\b)"
)
_SCOPED_EN = re.compile(
    r"(?:^|[.;!?]\s+)(?:please\s+)?(?:first[,，]?\s+|start\s+by\s+|begin\s+by\s+)(.+?)(?=$|[.;!?])",
    re.I,
)


def extract_scoped_directive(text: str) -> str | None:
    """Return the clause the user staged for THIS turn, or None.

    Only fires on high-confidence staging: a 先/首先/first marker whose clause
    is imperative (not an inhibition like 先别动), and either explicit
    wish/future context elsewhere in the message or the clause sits at the end
    of the message.  Anything ambiguous returns None so the full request keeps
    ruling the turn.
    """
    raw = str(text or "").strip()
    if not raw:
        return None

    best: str | None = None
    for match in _SCOPED_MARKER.finditer(raw):
        rest = raw[match.end():]
        if _SCOPED_FALSE_HEADS.match(rest):
            continue
        if _SCOPED_INHIBITION.match(rest.lstrip()):
            continue
        end = _SCOPED_CLAUSE_END.search(rest)
        clause = (rest[: end.start()] if end else rest).strip(" ，,、")
        if len(clause) < 2:
            continue
        clause_is_final = not (rest[end.start():].strip(" 。！？；.!?;\n") if end else "")
        has_wish = bool(_SCOPED_WISH_CONTEXT.search(raw))
        if not (has_wish or clause_is_final):
            continue
        best = clause
    if best:
        return best

    en = None
    for match in _SCOPED_EN.finditer(raw):
        clause = match.group(1).strip(" ,")
        if len(clause) >= 4:
            en = clause
    return en


class ClarificationReason(str, Enum):
    """The complete policy allowlist for asking the user a question."""

    MISSING_SOURCE = "missing_source"
    IRREVERSIBLE_ACTION = "irreversible_action"
    EXTERNAL_PAID_UNREQUESTED = "external_paid_unrequested"
    SENSITIVE_IDENTITY_PRIVACY_COPYRIGHT = "sensitive_identity_privacy_copyright"
    MULTI_TARGET_AMBIGUITY = "multi_target_ambiguity"
    USER_REQUESTED_CHOICE = "user_requested_choice"
    CREATIVE_PREFERENCE = "creative_preference"


class ClarificationDecisionKind(str, Enum):
    ASK = "ask"
    DEFAULT = "default"
    DENY = "deny"


E_CLARIFICATION_POLICY = "E_CLARIFICATION_POLICY"


@dataclass(frozen=True)
class ClarificationDecision:
    """Structured outcome of one clarification-policy evaluation."""

    decision: ClarificationDecisionKind
    reason: ClarificationReason
    defaults: dict[str, Any] = field(default_factory=dict)
    question: str | None = None
    error_code: str | None = None
    message: str = ""

    @property
    def should_ask(self) -> bool:
        return self.decision is ClarificationDecisionKind.ASK

    @property
    def uses_defaults(self) -> bool:
        return self.decision is ClarificationDecisionKind.DEFAULT

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision.value,
            "reason": self.reason.value,
            "defaults": copy.deepcopy(self.defaults),
            "question": self.question,
            "error_code": self.error_code,
            "message": self.message,
        }


class ClarificationGuard:
    """Allow only genuinely blocking clarification asks.

    Creative taste is never a reason to ask. It resolves to explicit caller
    defaults when supplied; without defaults it is denied by policy. Genuine
    blocking decisions are never rejected merely because an earlier decision
    was already requested in the same turn.
    """

    def __init__(self) -> None:
        self._asks_used = 0

    @property
    def asks_used(self) -> int:
        return self._asks_used

    @property
    def can_ask(self) -> bool:
        return True

    def reset(self) -> None:
        """Begin a new turn when the guard instance is reused."""

        self._asks_used = 0

    def decide(
        self,
        reason: ClarificationReason | str,
        *,
        question: str | None = None,
        defaults: Mapping[str, Any] | None = None,
    ) -> ClarificationDecision:
        reason_value = reason if isinstance(reason, ClarificationReason) else ClarificationReason(reason)
        preserved_defaults = copy.deepcopy(dict(defaults or {}))
        normalized_question = str(question).strip() if question is not None else None

        if reason_value is ClarificationReason.CREATIVE_PREFERENCE:
            if preserved_defaults:
                return ClarificationDecision(
                    decision=ClarificationDecisionKind.DEFAULT,
                    reason=reason_value,
                    defaults=preserved_defaults,
                    question=normalized_question,
                    message="creative preference resolved with explicit defaults",
                )
            return ClarificationDecision(
                decision=ClarificationDecisionKind.DENY,
                reason=reason_value,
                defaults=preserved_defaults,
                question=normalized_question,
                error_code=E_CLARIFICATION_POLICY,
                message="creative preference cannot trigger an ask without explicit defaults",
            )

        self._asks_used += 1
        return ClarificationDecision(
            decision=ClarificationDecisionKind.ASK,
            reason=reason_value,
            defaults=preserved_defaults,
            question=normalized_question,
            message="clarification allowed",
        )

    # Small aliases make the policy natural to use from either a router
    # (``evaluate``) or an interaction bridge (``request``).
    evaluate = decide
    request = decide


__all__ = [
    "TurnIntent",
    "classify_turn_intent",
    "extract_scoped_directive",
    "ClarificationReason",
    "ClarificationDecisionKind",
    "ClarificationDecision",
    "ClarificationGuard",
    "E_CLARIFICATION_POLICY",
]
