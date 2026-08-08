"""Tests for ask mechanism.

Covers:
  - Each control type with valid/invalid answers
  - Panel with multiple fields
  - Custom panel with schema
  - Full ask/answer validation
"""
import pytest
from datetime import datetime
from gemia.tools.ask import (
    AskControlType,
    AskError,
    AskValidationError,
    AskTypeError,
    AskSchemaError,
    SelectControl,
    MultiSelectControl,
    TextControl,
    SliderControl,
    PanelControl,
    CustomPanelControl,
    AskQuestion,
    AskAnswer,
    validate_ask_answer,
)


# ──────────────────────────────────────────────────────────────────────
# SelectControl Tests
# ──────────────────────────────────────────────────────────────────────


def test_select_valid():
    """SelectControl accepts a valid option value."""
    ctrl = SelectControl(options=[
        {"label": "Option A", "value": "a"},
        {"label": "Option B", "value": "b"},
    ])
    value, error = ctrl.validate("a")
    assert value == "a"
    assert error is None


def test_select_invalid_not_in_options():
    """SelectControl rejects invalid option value."""
    ctrl = SelectControl(options=[
        {"label": "Option A", "value": "a"},
    ])
    value, error = ctrl.validate("z")
    assert value is None
    assert "not a valid option" in error


def test_select_with_default():
    """SelectControl uses default if answer is None."""
    ctrl = SelectControl(
        options=[{"label": "A", "value": "a"}],
        default="a"
    )
    value, error = ctrl.validate(None)
    assert value == "a"
    assert error is None


def test_select_requires_answer():
    """SelectControl requires answer if no default."""
    ctrl = SelectControl(options=[{"label": "A", "value": "a"}])
    value, error = ctrl.validate(None)
    assert value is None
    assert "required" in error


def test_select_wrong_type():
    """SelectControl rejects non-string answers."""
    ctrl = SelectControl(options=[{"label": "A", "value": "a"}])
    value, error = ctrl.validate(123)
    assert value is None
    assert "must be a string" in error


# ──────────────────────────────────────────────────────────────────────
# MultiSelectControl Tests
# ──────────────────────────────────────────────────────────────────────


def test_multi_select_valid():
    """MultiSelectControl accepts list of valid options."""
    ctrl = MultiSelectControl(
        options=[
            {"label": "A", "value": "a"},
            {"label": "B", "value": "b"},
            {"label": "C", "value": "c"},
        ],
        min=1,
        max=3,
    )
    value, error = ctrl.validate(["a", "c"])
    assert value == ["a", "c"]
    assert error is None


def test_multi_select_min_constraint():
    """MultiSelectControl enforces min selections."""
    ctrl = MultiSelectControl(
        options=[
            {"label": "A", "value": "a"},
            {"label": "B", "value": "b"},
        ],
        min=2,
    )
    value, error = ctrl.validate(["a"])
    assert value is None
    assert "at least 2" in error


def test_multi_select_max_constraint():
    """MultiSelectControl enforces max selections."""
    ctrl = MultiSelectControl(
        options=[
            {"label": "A", "value": "a"},
            {"label": "B", "value": "b"},
        ],
        max=1,
    )
    value, error = ctrl.validate(["a", "b"])
    assert value is None
    assert "max 1" in error


def test_multi_select_invalid_option():
    """MultiSelectControl rejects invalid options."""
    ctrl = MultiSelectControl(
        options=[
            {"label": "A", "value": "a"},
            {"label": "B", "value": "b"},
        ],
        max=3,
    )
    value, error = ctrl.validate(["a", "z"])
    assert value is None
    assert "not a valid option" in error


def test_multi_select_wrong_type():
    """MultiSelectControl requires list."""
    ctrl = MultiSelectControl(
        options=[{"label": "A", "value": "a"}],
    )
    value, error = ctrl.validate("a")
    assert value is None
    assert "must be a list" in error


# ──────────────────────────────────────────────────────────────────────
# TextControl Tests
# ──────────────────────────────────────────────────────────────────────


def test_text_valid():
    """TextControl accepts plain text."""
    ctrl = TextControl(min_length=1, max_length=100)
    value, error = ctrl.validate("hello world")
    assert value == "hello world"
    assert error is None


def test_text_multiline():
    """TextControl allows newlines when enabled."""
    ctrl = TextControl(multiline=True)
    value, error = ctrl.validate("line1\nline2")
    assert value == "line1\nline2"
    assert error is None


def test_text_min_length():
    """TextControl enforces minimum length."""
    ctrl = TextControl(min_length=5)
    value, error = ctrl.validate("hi")
    assert value is None
    assert "too short" in error


def test_text_max_length():
    """TextControl enforces maximum length."""
    ctrl = TextControl(max_length=5)
    value, error = ctrl.validate("toolong")
    assert value is None
    assert "too long" in error


def test_text_regex_pattern():
    """TextControl validates regex pattern."""
    ctrl = TextControl(pattern=r"^\d{3}-\d{4}$")

    value, error = ctrl.validate("123-4567")
    assert value == "123-4567"
    assert error is None

    value, error = ctrl.validate("invalid")
    assert value is None
    assert "does not match pattern" in error


def test_text_wrong_type():
    """TextControl requires string."""
    ctrl = TextControl()
    value, error = ctrl.validate(123)
    assert value is None
    assert "must be a string" in error


# ──────────────────────────────────────────────────────────────────────
# SliderControl Tests
# ──────────────────────────────────────────────────────────────────────


def test_slider_valid():
    """SliderControl accepts numeric value in range."""
    ctrl = SliderControl(min=0, max=100, step=10)
    value, error = ctrl.validate(50)
    assert value == 50.0
    assert error is None


def test_slider_boundary_values():
    """SliderControl accepts min and max boundary values."""
    ctrl = SliderControl(min=0, max=100, step=1)

    value, error = ctrl.validate(0)
    assert value == 0.0
    assert error is None

    value, error = ctrl.validate(100)
    assert value == 100.0
    assert error is None


def test_slider_out_of_range():
    """SliderControl rejects values outside range."""
    ctrl = SliderControl(min=0, max=100)

    value, error = ctrl.validate(-1)
    assert value is None
    assert "must be in" in error

    value, error = ctrl.validate(101)
    assert value is None
    assert "must be in" in error


def test_slider_step_alignment():
    """SliderControl enforces step alignment."""
    ctrl = SliderControl(min=0, max=100, step=10)

    # Valid step
    value, error = ctrl.validate(30)
    assert value == 30.0
    assert error is None

    # Invalid step
    value, error = ctrl.validate(33)
    assert value is None
    assert "does not align to step" in error


def test_slider_with_default():
    """SliderControl uses default if answer is None."""
    ctrl = SliderControl(min=0, max=100, default=50)
    value, error = ctrl.validate(None)
    assert value == 50.0
    assert error is None


def test_slider_requires_answer():
    """SliderControl requires answer if no default."""
    ctrl = SliderControl(min=0, max=100)
    value, error = ctrl.validate(None)
    assert value is None
    assert "required" in error


def test_slider_non_numeric():
    """SliderControl rejects non-numeric values."""
    ctrl = SliderControl(min=0, max=100)
    value, error = ctrl.validate("abc")
    assert value is None
    assert "must be numeric" in error


# ──────────────────────────────────────────────────────────────────────
# PanelControl Tests
# ──────────────────────────────────────────────────────────────────────


def test_panel_valid():
    """PanelControl validates multiple fields."""
    ctrl = PanelControl(fields={
        "name": TextControl(min_length=1),
        "age": SliderControl(min=0, max=120),
        "colors": MultiSelectControl(
            options=[{"label": "Red", "value": "red"}],
        ),
    })

    answer = {
        "name": "Alice",
        "age": 25,
        "colors": ["red"],
    }

    value, error = ctrl.validate(answer)
    assert error is None
    assert value == answer


def test_panel_field_validation_error():
    """PanelControl reports field-level errors."""
    ctrl = PanelControl(fields={
        "name": TextControl(min_length=5),
    })

    value, error = ctrl.validate({"name": "Bob"})
    assert value is None
    assert "field 'name'" in error
    assert "too short" in error


def test_panel_wrong_type():
    """PanelControl requires dict."""
    ctrl = PanelControl(fields={
        "name": TextControl(),
    })

    value, error = ctrl.validate(["Alice"])
    assert value is None
    assert "must be a dict" in error


# ──────────────────────────────────────────────────────────────────────
# CustomPanelControl Tests
# ──────────────────────────────────────────────────────────────────────


def test_custom_panel_without_validator():
    """CustomPanelControl fails closed without a registered validator."""
    ctrl = CustomPanelControl(schema={
        "type": "complex_nested",
        "nested": {"key": "value"},
    })

    answer = {"some": "data"}
    value, error = ctrl.validate(answer)
    assert value is None
    assert "no registered validator" in error


def test_custom_panel_with_validator():
    """CustomPanelControl uses custom validator."""
    def my_validator(schema, answer):
        if not isinstance(answer, dict) or "required_key" not in answer:
            return None, "missing required_key"
        return answer, None

    ctrl = CustomPanelControl(
        schema={"required": ["required_key"]},
        validator=my_validator,
    )

    value, error = ctrl.validate({})
    assert value is None
    assert "missing required_key" in error

    value, error = ctrl.validate({"required_key": "value"})
    assert value == {"required_key": "value"}
    assert error is None


def test_custom_panel_validator_exception():
    """CustomPanelControl handles validator exceptions gracefully."""
    def broken_validator(schema, answer):
        raise RuntimeError("boom")

    ctrl = CustomPanelControl(
        schema={},
        validator=broken_validator,
    )

    value, error = ctrl.validate({})
    assert value is None
    assert "custom validation failed" in error


# ──────────────────────────────────────────────────────────────────────
# AskQuestion & AskAnswer Tests
# ──────────────────────────────────────────────────────────────────────


def test_ask_question_serialization():
    """AskQuestion serializes to dict and deserializes correctly."""
    q = AskQuestion(
        question_id="q_001",
        title="Choose a color",
        description="Pick your favorite color",
        controls={
            "choice": SelectControl(
                options=[
                    {"label": "Red", "value": "red"},
                    {"label": "Blue", "value": "blue"},
                ]
            )
        },
    )

    d = q.to_dict()
    q2 = AskQuestion.from_dict(d)

    assert q2.question_id == "q_001"
    assert q2.title == "Choose a color"
    assert "choice" in q2.controls


def test_ask_answer_serialization():
    """AskAnswer serializes to dict and deserializes correctly."""
    a = AskAnswer(
        question_id="q_001",
        answers={"choice": "red"},
        timestamp="2026-06-27T16:00:00Z",
    )

    d = a.to_dict()
    a2 = AskAnswer.from_dict(d)

    assert a2.question_id == "q_001"
    assert a2.answers["choice"] == "red"


def test_ask_panel_deserialization():
    """AskQuestion.from_dict handles panel controls correctly."""
    d = {
        "question_id": "q_form",
        "title": "User Form",
        "controls": {
            "userinfo": {
                "type": "panel",
                "description": "User information",
                "fields": {
                    "email": {
                        "type": "text",
                        "pattern": r"^[^\s@]+@[^\s@]+\.[^\s@]+$",
                        "min_length": 5,
                        "max_length": 255,
                        "placeholder": "user@example.com",
                        "multiline": False,
                    },
                    "age": {
                        "type": "slider",
                        "min": 18,
                        "max": 120,
                        "step": 1,
                        "default": 25,
                    },
                },
            }
        },
    }

    q = AskQuestion.from_dict(d)
    assert "userinfo" in q.controls
    assert isinstance(q.controls["userinfo"], PanelControl)


# ──────────────────────────────────────────────────────────────────────
# Full Validation Tests
# ──────────────────────────────────────────────────────────────────────


def test_validate_ask_answer_success():
    """validate_ask_answer passes all controls."""
    q = AskQuestion(
        question_id="q_001",
        title="Multi-field form",
        controls={
            "name": TextControl(min_length=1),
            "age": SliderControl(min=0, max=150),
        },
    )

    a = AskAnswer(
        question_id="q_001",
        answers={"name": "Alice", "age": 30},
    )

    result, error = validate_ask_answer(q, a)
    assert error is None
    assert result["name"] == "Alice"
    assert result["age"] == 30.0


def test_validate_ask_answer_mismatch_id():
    """validate_ask_answer rejects mismatched question_id."""
    q = AskQuestion(
        question_id="q_001",
        title="Test",
        controls={"dummy": TextControl()},
    )

    a = AskAnswer(
        question_id="q_002",
        answers={},
    )

    result, error = validate_ask_answer(q, a)
    assert result is None
    assert "question_id mismatch" in error


def test_validate_ask_answer_control_error():
    """validate_ask_answer reports control validation errors."""
    q = AskQuestion(
        question_id="q_001",
        title="Test",
        controls={
            "choice": SelectControl(
                options=[{"label": "A", "value": "a"}]
            ),
        },
    )

    a = AskAnswer(
        question_id="q_001",
        answers={"choice": "z"},
    )

    result, error = validate_ask_answer(q, a)
    assert result is None
    assert "control 'choice'" in error


def test_required_answer_does_not_substitute_control_defaults():
    question = AskQuestion(
        question_id="q_required",
        title="Choose",
        controls={
            "choice": SelectControl(
                options=[{"label": "A", "value": "a"}], default="a"
            ),
            "strength": SliderControl(min=0, max=10, default=5),
        },
    )

    result, error = validate_ask_answer(
        question,
        AskAnswer(question_id="q_required", answers={}),
    )
    assert result is None
    assert "answer required" in error


def test_required_panel_fields_do_not_substitute_defaults():
    question = AskQuestion(
        question_id="q_panel",
        title="Choose",
        controls={
            "form": PanelControl(
                fields={
                    "choice": SelectControl(
                        options=[{"label": "A", "value": "a"}], default="a"
                    )
                }
            )
        },
    )

    result, error = validate_ask_answer(
        question,
        AskAnswer(question_id="q_panel", answers={"form": {}}),
    )
    assert result is None
    assert "answer required" in error


def test_ask_control_to_dict_roundtrip():
    """All control types serialize and deserialize correctly."""
    controls = {
        "sel": SelectControl(options=[{"label": "A", "value": "a"}]),
        "multi": MultiSelectControl(
            options=[{"label": "B", "value": "b"}],
            min=0, max=2
        ),
        "txt": TextControl(pattern=r"^\w+$", max_length=50),
        "sld": SliderControl(min=-10, max=10, step=0.5, default=0),
    }

    q = AskQuestion(
        question_id="q_test",
        title="Control roundtrip",
        controls=controls,
    )

    d = q.to_dict()
    q2 = AskQuestion.from_dict(d)

    assert set(q2.controls.keys()) == set(controls.keys())
    for key in controls:
        assert type(q2.controls[key]) == type(controls[key])


# ──────────────────────────────────────────────────────────────────────
# Integration Tests
# ──────────────────────────────────────────────────────────────────────


def test_real_world_survey_ask():
    """Real-world survey ask: multi-control form with validation."""
    question = AskQuestion(
        question_id="survey_001",
        title="Video Editing Preferences",
        description="Help us understand your editing workflow",
        controls={
            "form": PanelControl(
                description="Please fill in all fields",
                fields={
                    "experience": SelectControl(
                        options=[
                            {"label": "Beginner", "value": "beginner"},
                            {"label": "Intermediate", "value": "intermediate"},
                            {"label": "Advanced", "value": "advanced"},
                        ],
                    ),
                    "tools_used": MultiSelectControl(
                        options=[
                            {"label": "DaVinci Resolve", "value": "resolve"},
                            {"label": "Adobe Premiere", "value": "premiere"},
                            {"label": "Final Cut Pro", "value": "fcp"},
                        ],
                        min=1,
                        max=3,
                    ),
                    "project_fps": SliderControl(
                        min=24, max=60, step=1, default=30
                    ),
                },
            )
        },
    )

    answer = AskAnswer(
        question_id="survey_001",
        answers={
            "form": {
                "experience": "advanced",
                "tools_used": ["resolve", "premiere"],
                "project_fps": 30,
            }
        },
    )

    result, error = validate_ask_answer(question, answer)
    assert error is None
    assert result["form"]["experience"] == "advanced"
    assert result["form"]["project_fps"] == 30.0


# ──────────────────────────────────────────────────────────────────────
# Option normalisation (forgiving agent-authored option specs)
# ──────────────────────────────────────────────────────────────────────


def test_select_normalizes_bare_string_options():
    """Bare-string options coerce to {label, value} so validation works."""
    ctrl = SelectControl(options=["mp4", "mov"])
    assert ctrl.options == [
        {"label": "mp4", "value": "mp4"},
        {"label": "mov", "value": "mov"},
    ]
    assert ctrl.validate("mov") == ("mov", None)
    val, err = ctrl.validate("avi")
    assert val is None and "not a valid option" in err


def test_select_normalizes_partial_dicts():
    # label-only → value defaults to label; value-only → label defaults to value
    ctrl = SelectControl(options=[{"label": "Bee", "value": "b"}, {"label": "Cee"}, {"value": "d"}])
    assert ctrl.options == [
        {"label": "Bee", "value": "b"},
        {"label": "Cee", "value": "Cee"},
        {"label": "d", "value": "d"},
    ]


def test_multi_select_normalizes_and_defaults_max():
    ctrl = MultiSelectControl(options=["v", "a", "sub"], min=1)
    assert [o["value"] for o in ctrl.options] == ["v", "a", "sub"]
    assert ctrl.max == 3  # defaults to len(options) after normalisation
    assert ctrl.validate(["v", "a"]) == (["v", "a"], None)


def test_normalize_rejects_bad_option_item():
    with pytest.raises(AskSchemaError):
        SelectControl(options=[123])
    with pytest.raises(AskSchemaError):
        SelectControl(options=[{"hint": "no value or label"}])


def test_normalize_rejects_non_list_options():
    with pytest.raises(AskSchemaError):
        SelectControl(options="mp4")


def test_normalized_options_survive_roundtrip():
    ctrl = SelectControl(options=["mp4", "mov"])
    q = AskQuestion(question_id="q1", title="t", controls={"fmt": ctrl})
    restored = AskQuestion.from_dict(q.to_dict())
    assert restored.controls["fmt"].options == [
        {"label": "mp4", "value": "mp4"},
        {"label": "mov", "value": "mov"},
    ]
    assert restored.controls["fmt"].validate("mp4") == ("mp4", None)
