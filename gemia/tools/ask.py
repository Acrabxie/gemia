"""Interactive ask mechanism for agent loop.

Allows the agent to request structured user input with rich control types:
    - select (single choice)
    - multi_select (multiple choices)
    - text (free-form text)
    - slider (numeric range)
    - panel (grouped form)
    - custom_panel (extensible schema-driven panel)

Each ask request creates a structured question/answer contract that:
  1. Agent emits an ask question with control schema
  2. User responds with validated answer
  3. Agent receives the answer and continues

All errors follow the stable code + message pattern (e.g. E_ASK_INVALID_CHOICE).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal, Optional, Union

import json


class AskControlType(str, Enum):
    """Control type identifiers."""
    SELECT = "select"
    MULTI_SELECT = "multi_select"
    TEXT = "text"
    SLIDER = "slider"
    PANEL = "panel"
    CUSTOM_PANEL = "custom_panel"


class AskError(Exception):
    """Base class for ask validation errors."""
    code: str = "E_ASK"

    def __init__(self, user_message: str, *, detail: str = "") -> None:
        super().__init__(user_message)
        self.user_message = user_message
        self.detail = detail or user_message

    def to_payload(self) -> dict[str, Any]:
        return {
            "error": self.user_message,
            "error_code": self.code,
            "detail": self.detail if self.detail != self.user_message else None,
        }


class AskValidationError(AskError):
    """Invalid answer for a given control."""
    code = "E_ASK_INVALID_ANSWER"


class AskTypeError(AskError):
    """Control type mismatch or unknown type."""
    code = "E_ASK_INVALID_TYPE"


class AskSchemaError(AskError):
    """Control schema definition is malformed."""
    code = "E_ASK_INVALID_SCHEMA"


def _normalize_options(options: Any) -> list[dict[str, Any]]:
    """Coerce option specs into the canonical ``[{label, value}]`` form.

    The wire/validation contract is a list of ``{"label", "value"}`` dicts, but an
    agent (or an LLM authoring the call) will very naturally pass a bare list of
    strings like ``["mp4", "mov"]`` — or a dict carrying only one of the two keys.
    Without this, validation silently sees an empty value set and rejects every
    answer. Accept the forgiving forms and normalise; raise a clear schema error
    (never a silent empty set) for anything that can't be interpreted.
    """
    if options is None:
        return []
    if not isinstance(options, (list, tuple)):
        raise AskSchemaError(f"options must be a list, got {type(options).__name__}")

    out: list[dict[str, Any]] = []
    for i, opt in enumerate(options):
        if isinstance(opt, str):
            out.append({"label": opt, "value": opt})
        elif isinstance(opt, dict):
            has_v, has_l = "value" in opt, "label" in opt
            if not has_v and not has_l:
                raise AskSchemaError(
                    f"option #{i} must carry a 'value' or 'label', got {opt!r}")
            value = opt.get("value", opt.get("label"))
            label = opt.get("label", value)
            out.append({**opt, "label": label, "value": value})
        else:
            raise AskSchemaError(
                f"option #{i} must be a string or {{label, value}} dict, "
                f"got {type(opt).__name__}")
    return out


# ──────────────────────────────────────────────────────────────────────
# Control Schemas
# ──────────────────────────────────────────────────────────────────────


@dataclass
class SelectControl:
    """Single-choice selection.

    Args:
        options: list of {label, value} dicts
        default: optional default value
    """
    type: Literal[AskControlType.SELECT] = AskControlType.SELECT
    options: list[dict[str, Any]] = field(default_factory=list)
    default: Optional[str] = None

    def __post_init__(self):
        self.options = _normalize_options(self.options)

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "options": self.options,
            "default": self.default,
        }

    def validate(self, answer: Any) -> tuple[Any, None] | tuple[None, str]:
        """Validate and return (value, None) or (None, error_message)."""
        if answer is None and self.default is not None:
            answer = self.default
        if answer is None:
            return None, "answer required for select control"

        if not isinstance(answer, str):
            return None, f"answer must be a string, got {type(answer).__name__}"

        valid_values = {opt.get("value") for opt in self.options if "value" in opt}
        if answer not in valid_values:
            return None, f"'{answer}' is not a valid option. Valid: {list(valid_values)}"

        return answer, None


@dataclass
class MultiSelectControl:
    """Multiple-choice selection.

    Args:
        options: list of {label, value} dicts
        min: minimum number of selections (default 0)
        max: maximum number of selections (default len(options))
    """
    type: Literal[AskControlType.MULTI_SELECT] = AskControlType.MULTI_SELECT
    options: list[dict[str, Any]] = field(default_factory=list)
    min: int = 0
    max: Optional[int] = None

    def __post_init__(self):
        self.options = _normalize_options(self.options)
        if self.max is None:
            self.max = len(self.options)
        if self.min < 0 or self.max < self.min:
            raise ValueError(f"invalid min/max: {self.min}/{self.max}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "options": self.options,
            "min": self.min,
            "max": self.max,
        }

    def validate(self, answer: Any) -> tuple[list[Any], None] | tuple[None, str]:
        """Validate and return (values, None) or (None, error_message)."""
        if not isinstance(answer, list):
            return None, f"answer must be a list, got {type(answer).__name__}"

        if len(answer) < self.min:
            return None, f"need at least {self.min} selections, got {len(answer)}"
        if len(answer) > self.max:
            return None, f"max {self.max} selections allowed, got {len(answer)}"

        valid_values = {opt.get("value") for opt in self.options if "value" in opt}
        for val in answer:
            if val not in valid_values:
                return None, f"'{val}' is not a valid option. Valid: {list(valid_values)}"

        return answer, None


@dataclass
class TextControl:
    """Free-form text input.

    Args:
        placeholder: hint text
        multiline: allow line breaks
        pattern: regex validation (optional)
        min_length: minimum length (default 0)
        max_length: maximum length (optional)
    """
    type: Literal[AskControlType.TEXT] = AskControlType.TEXT
    placeholder: str = ""
    multiline: bool = False
    pattern: Optional[str] = None
    min_length: int = 0
    max_length: Optional[int] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "placeholder": self.placeholder,
            "multiline": self.multiline,
            "pattern": self.pattern,
            "min_length": self.min_length,
            "max_length": self.max_length,
        }

    def validate(self, answer: Any) -> tuple[str, None] | tuple[None, str]:
        """Validate and return (text, None) or (None, error_message)."""
        if not isinstance(answer, str):
            return None, f"answer must be a string, got {type(answer).__name__}"

        if len(answer) < self.min_length:
            return None, f"answer too short (min {self.min_length}), got {len(answer)}"
        if self.max_length is not None and len(answer) > self.max_length:
            return None, f"answer too long (max {self.max_length}), got {len(answer)}"

        if self.pattern:
            import re
            if not re.match(self.pattern, answer):
                return None, f"answer does not match pattern: {self.pattern}"

        return answer, None


@dataclass
class SliderControl:
    """Numeric slider with range.

    Args:
        min: minimum value
        max: maximum value
        step: increment step (default 1)
        default: initial value
    """
    type: Literal[AskControlType.SLIDER] = AskControlType.SLIDER
    min: float = 0.0
    max: float = 100.0
    step: float = 1.0
    default: Optional[float] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "min": self.min,
            "max": self.max,
            "step": self.step,
            "default": self.default,
        }

    def validate(self, answer: Any) -> tuple[float, None] | tuple[None, str]:
        """Validate and return (value, None) or (None, error_message)."""
        if answer is None and self.default is not None:
            answer = self.default
        if answer is None:
            return None, "answer required for slider control"

        try:
            value = float(answer)
        except (ValueError, TypeError):
            return None, f"answer must be numeric, got {type(answer).__name__}"

        if value < self.min or value > self.max:
            return None, f"value must be in [{self.min}, {self.max}], got {value}"

        # Check step alignment
        remainder = (value - self.min) % self.step
        if abs(remainder) > 1e-9 and abs(remainder - self.step) > 1e-9:
            return None, f"value {value} does not align to step {self.step}"

        return value, None


@dataclass
class PanelControl:
    """Grouped form: multiple fields submitted together.

    Args:
        fields: {field_key: control} mapping
        description: optional panel description
    """
    type: Literal[AskControlType.PANEL] = AskControlType.PANEL
    fields: dict[str, Union[SelectControl, MultiSelectControl, TextControl, SliderControl]] = field(default_factory=dict)
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "fields": {k: v.to_dict() for k, v in self.fields.items()},
            "description": self.description,
        }

    def validate(self, answer: Any) -> tuple[dict[str, Any], None] | tuple[None, str]:
        """Validate and return (field_dict, None) or (None, error_message)."""
        if not isinstance(answer, dict):
            return None, f"panel answer must be a dict, got {type(answer).__name__}"

        result = {}
        for key, control in self.fields.items():
            field_answer = answer.get(key)
            value, error = control.validate(field_answer)
            if error:
                return None, f"field '{key}': {error}"
            result[key] = value

        return result, None


@dataclass
class CustomPanelControl:
    """Extensible schema-driven panel.

    The schema is a free dict; validation is delegated to the caller
    or a custom validator function.

    Args:
        schema: arbitrary control schema dict
        validator: optional callable (schema, answer) -> (value, error_message or None)
    """
    type: Literal[AskControlType.CUSTOM_PANEL] = AskControlType.CUSTOM_PANEL
    schema: dict[str, Any] = field(default_factory=dict)
    validator: Optional[callable] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "schema": self.schema,
        }

    def validate(self, answer: Any) -> tuple[Any, None] | tuple[None, str]:
        """Validate using custom validator if provided."""
        if self.validator:
            try:
                value = self.validator(self.schema, answer)
                if isinstance(value, tuple) and len(value) == 2:
                    return value  # (value, error_msg)
                return value, None
            except Exception as e:
                return None, f"custom validation failed: {e}"
        # No validator: pass through
        return answer, None


# ──────────────────────────────────────────────────────────────────────
# Ask Request & Response
# ──────────────────────────────────────────────────────────────────────


@dataclass
class AskQuestion:
    """An agent's request for user input.

    Args:
        question_id: unique identifier for this question
        title: human-readable title
        description: optional longer description
        controls: {control_key: control} mapping (single key for simple ask, multiple for complex)
        metadata: optional extra data for rendering/routing
    """
    question_id: str
    title: str
    description: str = ""
    controls: dict[str, Union[
        SelectControl, MultiSelectControl, TextControl, SliderControl,
        PanelControl, CustomPanelControl
    ]] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "question_id": self.question_id,
            "title": self.title,
            "description": self.description,
            "controls": {k: v.to_dict() for k, v in self.controls.items()},
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AskQuestion:
        """Deserialize from dict (useful for SSE/API roundtrips)."""
        controls = {}
        for key, ctrl_data in data.get("controls", {}).items():
            ctrl_type = ctrl_data.get("type")
            if ctrl_type == AskControlType.SELECT:
                controls[key] = SelectControl(**{k: v for k, v in ctrl_data.items() if k != "type"})
            elif ctrl_type == AskControlType.MULTI_SELECT:
                controls[key] = MultiSelectControl(**{k: v for k, v in ctrl_data.items() if k != "type"})
            elif ctrl_type == AskControlType.TEXT:
                controls[key] = TextControl(**{k: v for k, v in ctrl_data.items() if k != "type"})
            elif ctrl_type == AskControlType.SLIDER:
                controls[key] = SliderControl(**{k: v for k, v in ctrl_data.items() if k != "type"})
            elif ctrl_type == AskControlType.PANEL:
                # Recursively deserialize panel fields
                fields = {}
                for fk, fv in ctrl_data.get("fields", {}).items():
                    ft = fv.get("type")
                    if ft == AskControlType.SELECT:
                        fields[fk] = SelectControl(**{k: w for k, w in fv.items() if k != "type"})
                    elif ft == AskControlType.MULTI_SELECT:
                        fields[fk] = MultiSelectControl(**{k: w for k, w in fv.items() if k != "type"})
                    elif ft == AskControlType.TEXT:
                        fields[fk] = TextControl(**{k: w for k, w in fv.items() if k != "type"})
                    elif ft == AskControlType.SLIDER:
                        fields[fk] = SliderControl(**{k: w for k, w in fv.items() if k != "type"})
                controls[key] = PanelControl(fields={**fields})
            elif ctrl_type == AskControlType.CUSTOM_PANEL:
                controls[key] = CustomPanelControl(schema=ctrl_data.get("schema", {}))

        return cls(
            question_id=data["question_id"],
            title=data["title"],
            description=data.get("description", ""),
            controls=controls,
            metadata=data.get("metadata", {}),
        )


@dataclass
class AskAnswer:
    """User's response to an ask question.

    Args:
        question_id: matches the AskQuestion.question_id
        answers: {control_key: value} mapping
        timestamp: when the answer was submitted
    """
    question_id: str
    answers: dict[str, Any] = field(default_factory=dict)
    timestamp: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "question_id": self.question_id,
            "answers": self.answers,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AskAnswer:
        return cls(
            question_id=data["question_id"],
            answers=data.get("answers", {}),
            timestamp=data.get("timestamp"),
        )


# ──────────────────────────────────────────────────────────────────────
# Validation Helpers
# ──────────────────────────────────────────────────────────────────────


def validate_ask_answer(question: AskQuestion, answer: AskAnswer) -> tuple[dict[str, Any], None] | tuple[None, str]:
    """Validate all controls in the answer against the question schema.

    Returns (validated_dict, None) on success or (None, error_message) on failure.
    First-fail: stops at the first invalid control.
    """
    if question.question_id != answer.question_id:
        return None, f"question_id mismatch: expected {question.question_id}, got {answer.question_id}"

    validated = {}
    for key, control in question.controls.items():
        user_answer = answer.answers.get(key)
        value, error = control.validate(user_answer)
        if error:
            return None, f"control '{key}': {error}"
        validated[key] = value

    return validated, None


def validate_ask_answer_all(
    question: AskQuestion, answer: AskAnswer
) -> dict[str, str]:
    """Validate all controls, collecting every field error.

    Returns an empty dict on success, or ``{control_key: error_message}`` for
    each invalid field. Used by the HTTP layer to return 422 with aggregated
    errors so the user can fix everything in one round-trip.
    """
    errors: dict[str, str] = {}
    for key, control in question.controls.items():
        user_answer = answer.answers.get(key)
        _, error = control.validate(user_answer)
        if error:
            errors[key] = error
    return errors
