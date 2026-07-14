"""Deterministic turn-intent and clarification controls for the v3 loop.

This module deliberately defaults toward action.  A short message may end with
zero tool use only when it fully matches one of the small, explicit safe
classes below; an action embedded in otherwise conversational text wins.

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
    """Host-level intent classes used before the model/tool loop starts."""

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
    r"design|implement|compile|test|debug|clean|convert|merge|split|trim|crop|"
    r"caption|continue|retry|redo|finish)\b|\b(?:help me|color grade)\b"
)


def _full_match(patterns: tuple[re.Pattern[str], ...], text: str) -> bool:
    return any(pattern.fullmatch(text) is not None for pattern in patterns)


def _has_action(text: str) -> bool:
    return any(token in text for token in _ACTION_CJK) or _ACTION_EN.search(text) is not None


def classify_turn_intent(text: str) -> TurnIntent:
    """Return a conservative intent classification for one user message.

    Planning signals are recognized separately, but phrases that explicitly
    execute a plan remain actionable.  Action verbs are checked before the
    zero-tool full-match allowlist, so e.g. ``"谢谢，继续渲染"`` cannot be
    mistaken for a thank-you-only turn.  Empty and unknown text is actionable.
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
    if _has_action(normalized):
        return TurnIntent.ACTIONABLE
    if _full_match(_CONVERSATION_FULL, normalized):
        return TurnIntent.CONVERSATION
    if _full_match(_INFORMATION_FULL, normalized):
        return TurnIntent.INFORMATION
    return TurnIntent.ACTIONABLE


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


E_CLARIFICATION_LIMIT = "E_CLARIFICATION_LIMIT"
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
    """Allow at most one real clarification ask in a user turn.

    Creative taste is never a reason to ask.  It resolves to explicit caller
    defaults when supplied; without defaults it is denied by policy.  Neither
    path consumes the one ask available for a genuinely blocking reason.
    """

    def __init__(self) -> None:
        self._asks_used = 0

    @property
    def asks_used(self) -> int:
        return self._asks_used

    @property
    def can_ask(self) -> bool:
        return self._asks_used == 0

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

        if self._asks_used >= 1:
            return ClarificationDecision(
                decision=ClarificationDecisionKind.DENY,
                reason=reason_value,
                defaults=preserved_defaults,
                question=normalized_question,
                error_code=E_CLARIFICATION_LIMIT,
                message="clarification limit reached for this turn",
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
    "ClarificationReason",
    "ClarificationDecisionKind",
    "ClarificationDecision",
    "ClarificationGuard",
    "E_CLARIFICATION_LIMIT",
    "E_CLARIFICATION_POLICY",
]
