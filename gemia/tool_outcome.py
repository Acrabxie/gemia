"""Canonical classification for values returned by v3 tool dispatchers.

Dispatchers historically signal non-success in several different ways: some
raise, while others return a structured mapping containing an ``error_code``,
non-zero ``exit_code``, failed ``status``, or another explicit failure marker.
This module is the single place that translates those shapes into the small
runtime state vocabulary consumed by the agent loop and its adapters.

Classification is deliberately conservative.  Only the explicit markers in
``_has_failure_marker`` make an ordinary result a failure.  In particular,
``applied=False`` without an error is a valid no-op, and unrelated false-valued
fields such as ``logged=False`` do not imply failure.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from gemia.errors import GemiaError


ToolOutcomeState = Literal["success", "failure", "pending", "noop", "partial"]

_VALID_STATES = frozenset({"success", "failure", "pending", "noop", "partial"})
_FAILURE_STATUSES = frozenset({"failed", "error", "cancelled", "canceled"})
_PENDING_STATUSES = frozenset({
    "pending", "queued", "submitted", "running", "processing",
    "question_emitted",
})
_NOOP_STATUSES = frozenset({"noop", "no_op"})


@dataclass(frozen=True, slots=True)
class ToolOutcome:
    """One classified dispatcher outcome, preserving the original payload.

    ``raw_payload`` is intentionally not rewritten or copied.  Consumers can
    feed the exact dispatcher result back to the model while using ``state`` as
    the canonical control-flow decision.  Exception outcomes use the structured
    payload produced from the exception because there is no dispatcher result.
    """

    state: ToolOutcomeState
    raw_payload: Any
    error_code: Any | None = None
    recovery: Any | None = None

    def __post_init__(self) -> None:
        if self.state not in _VALID_STATES:
            raise ValueError(f"invalid tool outcome state: {self.state!r}")

    @property
    def is_failure(self) -> bool:
        return self.state == "failure"

    def error_payload(self, *, tool_name: str | None = None) -> dict[str, Any]:
        """Return a model/event-safe failure payload without losing raw data."""
        if isinstance(self.raw_payload, Mapping):
            payload = dict(self.raw_payload)
        else:
            payload = {
                "error": "tool returned a non-object result",
                "raw_payload": self.raw_payload,
            }
        payload.setdefault("error_code", self.error_code or "E_TOOL_FAILED")
        payload.setdefault("error", f"{tool_name or 'tool'} execution failed")
        if self.recovery is not None:
            payload.setdefault("recovery", self.recovery)
        return payload


def classify_tool_result(raw_payload: Any) -> ToolOutcome:
    """Classify a normal dispatcher return value.

    A dispatcher contract requires an object result. Non-mapping values retain
    their original value but are classified as an explicit protocol failure.
    """

    if not isinstance(raw_payload, Mapping):
        return ToolOutcome(
            state="failure",
            raw_payload=raw_payload,
            error_code="E_TOOL_PROTOCOL",
        )

    error_code = raw_payload.get("error_code")
    recovery = raw_payload.get("recovery")
    status = _normalise_status(raw_payload.get("status"))

    if _has_failure_marker(raw_payload, status=status):
        if not _is_nonempty(error_code):
            if raw_payload.get("timed_out") is True:
                error_code = "E_TIMEOUT"
            elif _is_nonzero_exit_code(raw_payload.get("exit_code")):
                error_code = "E_PROCESS_EXIT"
            elif status in _FAILURE_STATUSES and raw_payload.get("job_id"):
                error_code = "E_JOB_FAILED"
            else:
                error_code = "E_TOOL_FAILED"
        return ToolOutcome(
            state="failure",
            raw_payload=raw_payload,
            error_code=error_code,
            recovery=recovery,
        )

    # An explicit lifecycle state is stronger than a generic ``applied`` flag.
    # For example, a pending job may truthfully report that it has not applied
    # anything yet without being a completed no-op.
    if status in _PENDING_STATUSES:
        state: ToolOutcomeState = "pending"
    elif status == "partial" or raw_payload.get("partial") is True:
        state = "partial"
    elif status in _NOOP_STATUSES or raw_payload.get("applied") is False:
        state = "noop"
    else:
        state = "success"

    return ToolOutcome(
        state=state,
        raw_payload=raw_payload,
        error_code=error_code,
        recovery=recovery,
    )


def classify_tool_exception(exc: Exception) -> ToolOutcome:
    """Convert a raised dispatcher exception into a structured failure."""

    if isinstance(exc, GemiaError):
        payload = exc.to_payload()
    else:
        payload = {
            "error": f"{type(exc).__name__}: {exc}",
            "error_code": "E_UNCAUGHT",
        }

    return ToolOutcome(
        state="failure",
        raw_payload=payload,
        error_code=payload.get("error_code"),
        recovery=payload.get("recovery"),
    )


def _has_failure_marker(payload: Mapping[str, Any], *, status: str) -> bool:
    """Return True only for the explicitly supported failure signals."""

    return (
        _is_nonempty(payload.get("error_code"))
        or _is_nonzero_exit_code(payload.get("exit_code"))
        or payload.get("timed_out") is True
        or status in _FAILURE_STATUSES
        or _is_nonempty(payload.get("error"))
    )


def _normalise_status(value: Any) -> str:
    return value.strip().lower() if isinstance(value, str) else ""


def _is_nonempty(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return bool(value)


def _is_nonzero_exit_code(value: Any) -> bool:
    if value is None or value == "":
        return False
    try:
        return float(value) != 0.0
    except (TypeError, ValueError):
        # A malformed exit-code value is not itself one of the supported
        # failure markers.  Protocol validation belongs to the caller.
        return False


__all__ = [
    "ToolOutcome",
    "ToolOutcomeState",
    "classify_tool_exception",
    "classify_tool_result",
    "outcome_from_exception",
    "outcome_from_result",
]


# Descriptive aliases used by host adapters; the classify_* names remain for
# callers/tests written during the migration.
outcome_from_result = classify_tool_result
outcome_from_exception = classify_tool_exception
