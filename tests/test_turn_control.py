from __future__ import annotations

import pytest

from gemia.turn_control import (
    E_CLARIFICATION_LIMIT,
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
    assert guard.can_ask is False


def test_second_real_ask_is_denied_with_structured_limit_error() -> None:
    guard = ClarificationGuard()
    guard.decide(ClarificationReason.MISSING_SOURCE, question="source?")

    result = guard.decide(
        ClarificationReason.USER_REQUESTED_CHOICE,
        question="A or B?",
        defaults={"choice": "A"},
    )

    assert result.decision is ClarificationDecisionKind.DENY
    assert result.error_code == E_CLARIFICATION_LIMIT
    assert result.reason is ClarificationReason.USER_REQUESTED_CHOICE
    assert result.question == "A or B?"
    assert result.defaults == {"choice": "A"}
    assert guard.asks_used == 1


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


def test_default_resolution_does_not_bypass_or_consume_real_ask_limit() -> None:
    guard = ClarificationGuard()
    guard.evaluate(ClarificationReason.MISSING_SOURCE, question="source?")

    creative = guard.evaluate(
        ClarificationReason.CREATIVE_PREFERENCE,
        defaults={"style": "clean"},
    )
    limited = guard.evaluate(ClarificationReason.IRREVERSIBLE_ACTION)

    assert creative.decision is ClarificationDecisionKind.DEFAULT
    assert limited.error_code == E_CLARIFICATION_LIMIT
    assert guard.asks_used == 1


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
