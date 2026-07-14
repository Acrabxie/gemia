"""Tests for the elicit tool (agent-facing ask interface).

``elicit.dispatch`` is async and drives a human-in-the-loop round-trip through an
AskBridge placed in ``ctx.extra["ask_bridge"]``: it emits an ``ask_question`` event
and awaits the answer. These tests use a stub bridge (delivered answer / timeout)
and drive the coroutine with ``asyncio.run`` so no async plugin is needed.
"""
import asyncio
from types import SimpleNamespace

import pytest

from gemia.tools.elicit import dispatch, _build_controls
from gemia.tools.ask import AskControlType
from gemia.turn_control import ClarificationGuard


class StubBridge:
    """Stand-in for AskBridge: returns a preset answer (dict) or None (timeout)."""

    def __init__(self, answer=None):
        self.answer = answer
        self.emitted = []

    async def emit_and_wait(self, question, *, timeout=None):
        self.emitted.append(question)
        return self.answer


def _ctx(bridge=None, guard=None):
    extra = {"ask_bridge": bridge} if bridge is not None else {}
    if guard is not None:
        extra["clarification_guard"] = guard
    return SimpleNamespace(session_id="test_session_123", extra=extra)


def run(coro):
    return asyncio.run(coro)


# ── dispatch: emit + answer round-trip ──────────────────────────────────────


def test_elicit_emits_question_and_returns_answer():
    bridge = StubBridge(answer={"choice": "a"})
    args = {
        "title": "Choose an option",
        "description": "Pick one",
        "controls": {"choice": {"type": "select", "options": [
            {"label": "Option A", "value": "a"}, {"label": "Option B", "value": "b"}]}},
    }
    result = run(dispatch(args, _ctx(bridge)))

    assert result["status"] == "answer_received"
    assert result["answers"]["choice"] == "a"
    assert result["fallback_used"] is False
    # the question was emitted with the right title + control
    assert len(bridge.emitted) == 1
    q = bridge.emitted[0]
    assert q["title"] == "Choose an option"
    assert "choice" in q["controls"]


def test_elicit_no_controls():
    result = run(dispatch({"title": "Test", "controls": {}}, _ctx(StubBridge())))
    assert result["error_code"] == "E_ELICIT_NO_CONTROLS"


def test_elicit_invalid_spec():
    args = {"title": "Test", "controls": {"bad": {"type": "unknown_type"}}}
    result = run(dispatch(args, _ctx(StubBridge())))
    assert result["error_code"] == "E_ELICIT_INVALID_SPEC"


def test_elicit_rejects_invalid_answer():
    bridge = StubBridge(answer={"choice": "z"})  # 'z' is not an option
    args = {"title": "Choose", "controls": {
        "choice": {"type": "select", "options": [{"label": "A", "value": "a"}]}}}
    result = run(dispatch(args, _ctx(bridge)))
    assert result["error_code"] == "E_ELICIT_INVALID_ANSWER"


def test_elicit_timeout_falls_back_to_defaults():
    bridge = StubBridge(answer=None)  # no answer delivered → timeout sentinel
    args = {"title": "t", "controls": {
        "fmt": {"type": "select", "options": ["mp4", "mov"], "default": "mp4"},
        "q":   {"type": "slider", "min": 0, "max": 10, "step": 1, "default": 7}}}
    result = run(dispatch(args, _ctx(bridge)))
    assert result["status"] == "answer_received"
    assert result["fallback_used"] is True
    assert result["answers"] == {"fmt": "mp4", "q": 7}


def test_elicit_panel_form_round_trip():
    bridge = StubBridge(answer={"form": {"email": "a@b.co", "age": 25}})
    args = {"title": "User Information", "controls": {"form": {
        "type": "panel", "description": "Fill in your info", "fields": {
            "email": {"type": "text", "pattern": r"^[^\s@]+@[^\s@]+$", "min_length": 5},
            "age":   {"type": "slider", "min": 18, "max": 120, "step": 1, "default": 25}}}}}
    result = run(dispatch(args, _ctx(bridge)))
    assert result["status"] == "answer_received"
    assert result["answers"]["form"]["age"] == 25
    form_ctrl = bridge.emitted[0]["controls"]["form"]
    assert {"email", "age"} <= set(form_ctrl["fields"])


def test_elicit_bare_string_options_work():
    """An agent passing bare-string options (not {label,value}) still works."""
    bridge = StubBridge(answer={"fmt": "mov"})
    args = {"title": "t", "controls": {"fmt": {"type": "select", "options": ["mp4", "mov"]}}}
    result = run(dispatch(args, _ctx(bridge)))
    assert result["status"] == "answer_received" and result["answers"]["fmt"] == "mov"


def test_elicit_without_bridge_returns_question():
    """Legacy/test context with no bridge: emit nothing, hand back the question."""
    args = {"title": "t", "controls": {"c": {"type": "select", "options": ["a"]}}}
    result = run(dispatch(args, _ctx(bridge=None)))
    assert result["status"] == "question_emitted"
    assert "question" in result and "c" in result["question"]["controls"]


def test_creative_preference_uses_explicit_defaults_without_ask_event():
    bridge = StubBridge(answer={"style": "warm"})
    args = {
        "reason": "creative_preference",
        "title": "Choose a style",
        "controls": {
            "style": {
                "type": "select",
                "options": ["clean", "warm"],
                "default": "clean",
            }
        },
    }

    result = run(dispatch(args, _ctx(bridge, ClarificationGuard())))

    assert result["status"] == "default_applied"
    assert result["answers"] == {"style": "clean"}
    assert bridge.emitted == []


def test_clarification_guard_allows_only_one_real_question():
    guard = ClarificationGuard()
    bridge = StubBridge(answer={"choice": "a"})
    args = {
        "reason": "missing_source",
        "title": "Choose source",
        "controls": {"choice": {"type": "select", "options": ["a", "b"]}},
    }

    first = run(dispatch(args, _ctx(bridge, guard)))
    second = run(dispatch(args, _ctx(bridge, guard)))

    assert first["status"] == "answer_received"
    assert second["error_code"] == "E_CLARIFICATION_LIMIT"
    assert len(bridge.emitted) == 1


def test_creative_preference_without_complete_defaults_is_not_fabricated():
    args = {
        "reason": "creative_preference",
        "title": "Name it",
        "controls": {"name": {"type": "text"}},
    }

    result = run(dispatch(args, _ctx(StubBridge(), ClarificationGuard())))

    assert result["error_code"] == "E_CLARIFICATION_POLICY"


def test_elicit_all_control_types():
    bridge = StubBridge(answer={
        "sel": "a", "multi": ["b"], "txt": "hello", "sld": 5,
        "pnl": {"field1": "x"}, "custom": {"anything": 1}})
    args = {"title": "Kitchen sink", "controls": {
        "sel":    {"type": "select", "options": [{"label": "A", "value": "a"}]},
        "multi":  {"type": "multi_select", "options": [{"label": "B", "value": "b"}]},
        "txt":    {"type": "text", "placeholder": "text"},
        "sld":    {"type": "slider", "min": 0, "max": 10},
        "pnl":    {"type": "panel", "fields": {"field1": {"type": "text"}}},
        "custom": {"type": "custom_panel", "schema": {}}}}
    result = run(dispatch(args, _ctx(bridge)))
    assert result["status"] == "answer_received"
    assert set(bridge.emitted[0]["controls"]) == {"sel", "multi", "txt", "sld", "pnl", "custom"}


# ── _build_controls (schema parsing) ────────────────────────────────────────


def test_build_controls_select():
    controls = _build_controls({"my_select": {"type": "select", "options": [
        {"label": "A", "value": "a"}, {"label": "B", "value": "b"}], "default": "a"}})
    assert controls["my_select"].default == "a"


def test_build_controls_multi_select():
    controls = _build_controls({"my_multi": {"type": "multi_select", "options": [
        {"label": "A", "value": "a"}, {"label": "B", "value": "b"}], "min": 1, "max": 2}})
    assert controls["my_multi"].min == 1 and controls["my_multi"].max == 2


def test_build_controls_text():
    controls = _build_controls({"my_text": {"type": "text", "placeholder": "Enter text",
        "min_length": 1, "max_length": 100, "pattern": r"^[a-z]+$"}})
    assert controls["my_text"].placeholder == "Enter text"
    assert controls["my_text"].pattern == r"^[a-z]+$"


def test_build_controls_slider():
    controls = _build_controls({"my_slider": {"type": "slider",
        "min": 0, "max": 100, "step": 5, "default": 50}})
    s = controls["my_slider"]
    assert (s.min, s.max, s.step, s.default) == (0, 100, 5, 50)


def test_build_controls_panel():
    controls = _build_controls({"my_panel": {"type": "panel", "description": "A form",
        "fields": {"name": {"type": "text", "min_length": 1},
                   "age": {"type": "slider", "min": 0, "max": 150}}}})
    assert {"name", "age"} <= set(controls["my_panel"].fields)


def test_build_controls_custom_panel():
    controls = _build_controls({"my_custom": {"type": "custom_panel",
        "schema": {"type": "complex", "nested": {"key": "value"}}}})
    assert controls["my_custom"].schema["type"] == "complex"


def test_build_controls_invalid_type():
    with pytest.raises(ValueError, match="unsupported control type"):
        _build_controls({"invalid": {"type": "unknown_type"}})


def test_build_controls_invalid_panel_field():
    # Panel fields reuse the same builder, so an unknown nested type also raises.
    with pytest.raises(ValueError, match="unsupported control type"):
        _build_controls({"panel": {"type": "panel", "fields": {
            "bad_field": {"type": "unknown_type"}}}})
