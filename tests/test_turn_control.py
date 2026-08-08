from __future__ import annotations

import pytest

from gemia.turn_control import (
    E_CLARIFICATION_POLICY,
    ClarificationDecisionKind,
    ClarificationGuard,
    ClarificationReason,
    TurnIntent,
    classify_turn_intent,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("你好！", TurnIntent.CONVERSATION),
        ("谢谢你。", TurnIntent.CONVERSATION),
        ("Never mind.", TurnIntent.CONVERSATION),
        ("Stop immediately.", TurnIntent.CONVERSATION),
        ("你是谁？", TurnIntent.INFORMATION),
        ("你用的什么模型？", TurnIntent.INFORMATION),
        ("Which model are you using?", TurnIntent.INFORMATION),
        ("先给我一个计划，不要执行", TurnIntent.PLAN),
        ("Outline the steps, do not execute.", TurnIntent.PLAN),
    ],
)
def test_turn_intent_explicit_classes(text: str, expected: TurnIntent) -> None:
    assert classify_turn_intent(text) is expected
    assert TurnIntent.classify(text) is expected


@pytest.mark.parametrize(
    "text",
    [
        "你好，帮我剪辑这个视频",
        "谢谢，继续渲染",
        "你是谁？然后帮我导出项目",
        "执行这个计划",
        "按上面的方案开始做",
        "Please fix the broken export",
        "给视频加字幕并调色",
    ],
)
def test_action_words_override_conversational_or_plan_words(text: str) -> None:
    assert classify_turn_intent(text) is TurnIntent.ACTIONABLE


@pytest.mark.parametrize("text", ["", "随便看看", "这段片子怎么样", "maybe later"])
def test_empty_and_unknown_intent_defaults_to_actionable(text: str) -> None:
    assert classify_turn_intent(text) is TurnIntent.ACTIONABLE


def test_safe_conversation_requires_full_match() -> None:
    assert classify_turn_intent("前面说了你好但还没完成") is TurnIntent.ACTIONABLE
    assert classify_turn_intent("我只是想说谢谢然后继续") is TurnIntent.ACTIONABLE
    assert classify_turn_intent("停止渲染并重新导出") is TurnIntent.ACTIONABLE


@pytest.mark.parametrize(
    "text",
    [
        # The reported regression: a capability question was forced through the
        # action tool surface and hard-errored as "no objective progress".
        "你有vector motion啦？",
        "你有 vector motion 吗？",
        "你有没有转场功能？",
        "有没有矢量动效？",
        "支不支持竖屏？",
        "你能不能做矢量动效？",
        "你会做粒子效果吗？",
        "什么是矢量动效？",
        "vector motion 是什么？",
        "这个功能是啥？",
        "Do you have vector motion?",
        "Does it support transitions?",
        "Is there a way to loop it?",
        "What is a motion graphic?",
    ],
)
def test_capability_and_existence_questions_are_information(text: str) -> None:
    # These end honestly with prose, not a goal mutation. INFORMATION keeps
    # their tool surface empty; completion itself is owned by whether the model
    # actually committed to execution with a tool call.
    assert classify_turn_intent(text) is TurnIntent.INFORMATION


@pytest.mark.parametrize(
    "text",
    [
        "哦我说的是通过Google官方的api接入photo",
        "我想了解通过Google官方API接入Google Photos，该怎么做？",
        "我不是让你画图，我是在问 Google Photos API 怎么接",
        "解释一下 Photos Picker API",
        "Google Photos 接入 Lumeri 是什么架构？",
        "如何通过 OAuth 接入？",
        "I mean the official Google API integration, not an image",
        "I want to understand how to integrate Google Photos",
        "Explain the Google Photos Picker API",
        "How should Google Photos OAuth work?",
        "你能解释怎么接入吗？",
    ],
)
def test_explanations_how_to_and_corrections_are_information(text: str) -> None:
    assert classify_turn_intent(text) is TurnIntent.INFORMATION


@pytest.mark.parametrize(
    "text",
    [
        "请直接把Google Photos接进Lumeri",
        "现在直接实现 Google Photos Picker API 接入",
        "帮我把 OAuth 和 Picker 接好并测试",
        "按上面的方案开始接入",
        "先解释一下，然后直接接入并测试",
        "我的意思是让你直接把它接入并测试",
        "Please integrate Google Photos into Lumeri now",
        "Implement and test the OAuth + Picker flow",
        "Go ahead with the integration",
        "Please implement it now, then explain the architecture",
        "你能帮我接入吗？",
    ],
)
def test_explicit_integration_execution_stays_actionable(text: str) -> None:
    assert classify_turn_intent(text) is TurnIntent.ACTIONABLE


@pytest.mark.parametrize(
    "text",
    [
        # An action verb anywhere still wins over question phrasing.
        "你能帮我生成一个视频吗",  # 生成 / 帮我
        "可以帮我导出4k吗",  # 导出
        "支不支持自动字幕",  # 字幕
        "帮我看看有没有问题",  # 帮我
        # "所有" / bare status questions must not read as a possession query.
        "把所有片段都删了吗",
        # Bare opinion questions stay on the conservative actionable default.
        "这段片子怎么样",
    ],
)
def test_question_phrasing_does_not_defeat_action_or_default(text: str) -> None:
    assert classify_turn_intent(text) is TurnIntent.ACTIONABLE


@pytest.mark.parametrize(
    "reason",
    [
        ClarificationReason.MISSING_SOURCE,
        ClarificationReason.IRREVERSIBLE_ACTION,
        ClarificationReason.EXTERNAL_PAID_UNREQUESTED,
        ClarificationReason.SENSITIVE_IDENTITY_PRIVACY_COPYRIGHT,
        ClarificationReason.MULTI_TARGET_AMBIGUITY,
        ClarificationReason.USER_REQUESTED_CHOICE,
    ],
)
def test_first_blocking_reason_may_ask(reason: ClarificationReason) -> None:
    guard = ClarificationGuard()

    result = guard.decide(
        reason,
        question="Which source should I use?",
        defaults={"fallback": {"mode": "safe"}},
    )

    assert result.decision is ClarificationDecisionKind.ASK
    assert result.should_ask is True
    assert result.reason is reason
    assert result.defaults == {"fallback": {"mode": "safe"}}
    assert result.question == "Which source should I use?"
    assert result.error_code is None
    assert guard.asks_used == 1
    assert guard.can_ask is True


def test_later_genuinely_blocking_decision_is_still_allowed() -> None:
    guard = ClarificationGuard()
    guard.decide(ClarificationReason.MISSING_SOURCE, question="source?")

    result = guard.decide(
        ClarificationReason.USER_REQUESTED_CHOICE,
        question="A or B?",
        defaults={"choice": "A"},
    )

    assert result.decision is ClarificationDecisionKind.ASK
    assert result.error_code is None
    assert result.reason is ClarificationReason.USER_REQUESTED_CHOICE
    assert result.question == "A or B?"
    assert result.defaults == {"choice": "A"}
    assert guard.asks_used == 2


def test_creative_preference_uses_explicit_defaults_without_asking() -> None:
    guard = ClarificationGuard()
    supplied = {"style": "cinematic", "grade": {"warmth": 0.2}}

    result = guard.decide(
        ClarificationReason.CREATIVE_PREFERENCE,
        question="Which visual style?",
        defaults=supplied,
    )

    assert result.decision is ClarificationDecisionKind.DEFAULT
    assert result.uses_defaults is True
    assert result.should_ask is False
    assert result.defaults == supplied
    assert guard.asks_used == 0
    assert guard.can_ask is True

    # The decision owns a structured copy, not the caller's mutable mapping.
    supplied["grade"]["warmth"] = 1.0
    assert result.defaults["grade"]["warmth"] == 0.2


def test_creative_preference_without_defaults_is_policy_denied() -> None:
    guard = ClarificationGuard()

    result = guard.request(
        "creative_preference",
        question="Warm or cool?",
    )

    assert result.decision is ClarificationDecisionKind.DENY
    assert result.error_code == E_CLARIFICATION_POLICY
    assert result.defaults == {}
    assert guard.asks_used == 0


def test_default_resolution_does_not_consume_a_blocking_ask() -> None:
    guard = ClarificationGuard()
    guard.evaluate(ClarificationReason.MISSING_SOURCE, question="source?")

    creative = guard.evaluate(
        ClarificationReason.CREATIVE_PREFERENCE,
        defaults={"style": "clean"},
    )
    later = guard.evaluate(ClarificationReason.IRREVERSIBLE_ACTION)

    assert creative.decision is ClarificationDecisionKind.DEFAULT
    assert later.decision is ClarificationDecisionKind.ASK
    assert guard.asks_used == 2


def test_reset_starts_a_new_turn() -> None:
    guard = ClarificationGuard()
    guard.decide(ClarificationReason.MISSING_SOURCE)
    guard.reset()

    result = guard.decide(ClarificationReason.IRREVERSIBLE_ACTION)

    assert result.decision is ClarificationDecisionKind.ASK
    assert guard.asks_used == 1


def test_decision_serializes_enums_and_preserves_nested_defaults() -> None:
    guard = ClarificationGuard()
    result = guard.decide(
        ClarificationReason.MISSING_SOURCE,
        question=" source? ",
        defaults={"source": {"kind": "selected_media"}},
    )

    payload = result.to_dict()
    assert payload == {
        "decision": "ask",
        "reason": "missing_source",
        "defaults": {"source": {"kind": "selected_media"}},
        "question": "source?",
        "error_code": None,
        "message": "clarification allowed",
    }

    payload["defaults"]["source"]["kind"] = "mutated"
    assert result.defaults["source"]["kind"] == "selected_media"


def test_unknown_clarification_reason_is_rejected() -> None:
    with pytest.raises(ValueError):
        ClarificationGuard().decide("ordinary_preference")
