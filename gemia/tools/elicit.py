"""Elicitation verb: agent requests structured user input via the ask mechanism.

Flow (human-in-the-loop):
  1. The model calls ``elicit`` with a control schema.
  2. This dispatcher (running on the session's asyncio loop) builds + validates the
     control schema, then asks the :class:`~gemia.tools._ask_bridge.AskBridge` to
     emit an ``ask_question`` SSE event and ``await`` the user's answer.
  3. The frontend renders the controls; the user submits, and an HTTP route
     delivers the answer back onto the session loop, resolving the await.
  4. The answer is validated against the schema; the validated values are returned
     as this tool's result, so the model continues the turn with the answer in hand.

If no answer arrives within the timeout (e.g. no frontend attached), the dispatcher
falls back to per-control defaults so the loop never hangs forever.

Errors follow the stable code + message pattern (``E_ELICIT_*`` / ``E_ASK_*``).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from gemia.tools._context import ToolContext
from gemia.turn_control import ClarificationDecisionKind, ClarificationGuard
from gemia.tools.ask import (
    AskQuestion,
    AskAnswer,
    AskControlType,
    AskError,
    SelectControl,
    MultiSelectControl,
    TextControl,
    SliderControl,
    PanelControl,
    CustomPanelControl,
    validate_ask_answer,
)


async def dispatch(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Emit an ask question, await the user's answer, return the validated answer.

    Args:
        title: human-readable title
        description: optional longer description
        controls: ``{control_key: control_spec}`` (see the tool schema)
        timeout: optional seconds to wait before falling back to defaults
    """
    controls_spec = args.get("controls") or {}
    if not controls_spec:
        return {"error": "no controls specified", "error_code": "E_ELICIT_NO_CONTROLS"}

    try:
        controls = _build_controls(controls_spec)
    except AskError as exc:
        return exc.to_payload()
    except Exception as exc:  # malformed spec → let the model fix its arguments
        return {
            "error": f"invalid control specification: {exc}",
            "error_code": "E_ELICIT_INVALID_SPEC",
        }

    extra = getattr(ctx, "extra", None) or {}
    guard = extra.get("clarification_guard")
    if isinstance(guard, ClarificationGuard):
        reason = args.get("reason")
        if not reason:
            return {
                "error": "elicit requires a host-approved clarification reason",
                "error_code": "E_CLARIFICATION_POLICY",
            }
        try:
            policy = guard.decide(
                str(reason),
                question=str(args.get("title") or "Question"),
                defaults=_explicit_defaults(controls_spec),
            )
        except ValueError:
            return {
                "error": f"unsupported clarification reason: {reason}",
                "error_code": "E_CLARIFICATION_POLICY",
            }
        if policy.decision is ClarificationDecisionKind.DEFAULT:
            return {
                "status": "default_applied",
                "reason": policy.reason.value,
                "answers": policy.defaults,
                "fallback_used": True,
                "host_policy_default": True,
            }
        if policy.decision is ClarificationDecisionKind.DENY:
            return {
                "error": policy.message,
                "error_code": policy.error_code or "E_CLARIFICATION_POLICY",
                "reason": policy.reason.value,
            }

    question = AskQuestion(
        question_id=f"ask_{uuid.uuid4().hex[:12]}",
        title=args.get("title", "Question"),
        description=args.get("description", ""),
        controls=controls,
        metadata={
            "emitted_at": datetime.now(timezone.utc).isoformat(),
            "session_id": getattr(ctx, "session_id", None),
        },
    )

    bridge = extra.get("ask_bridge")
    if bridge is None:
        # No HITL bridge wired into this context (legacy / test path): emit nothing
        # and hand the question back so a caller can route it however it likes.
        return {
            "status": "question_emitted",
            "question_id": question.question_id,
            "question": question.to_dict(),
            "note": "no ask bridge in tool context; cannot await an answer here",
        }

    timeout = args.get("timeout")
    raw = await bridge.emit_and_wait(
        question.to_dict(),
        timeout=float(timeout) if timeout is not None else None,
    )

    fallback_used = raw is None
    if fallback_used:
        raw = _default_answers(controls)

    answer = AskAnswer(
        question_id=question.question_id,
        answers=raw,
        timestamp=datetime.now(timezone.utc).isoformat(),
    )
    validated, error = validate_ask_answer(question, answer)
    if error:
        return {
            "error": f"validation failed: {error}",
            "error_code": "E_ELICIT_INVALID_ANSWER",
            "question_id": question.question_id,
            "fallback_used": fallback_used,
        }

    return {
        "status": "answer_received",
        "question_id": question.question_id,
        "answers": validated,
        "fallback_used": fallback_used,
    }


# ── control construction ───────────────────────────────────────────────────


def _build_one(key_or_index: Any, spec: dict[str, Any]) -> Any:
    """Build a single control object from its spec (shared by top-level + panel)."""
    ctrl_type = spec.get("type")

    if ctrl_type == AskControlType.SELECT:
        return SelectControl(options=spec.get("options", []), default=spec.get("default"))
    if ctrl_type == AskControlType.MULTI_SELECT:
        return MultiSelectControl(
            options=spec.get("options", []),
            min=spec.get("min", 0),
            max=spec.get("max"),
        )
    if ctrl_type == AskControlType.TEXT:
        return TextControl(
            placeholder=spec.get("placeholder", ""),
            multiline=spec.get("multiline", False),
            pattern=spec.get("pattern"),
            min_length=spec.get("min_length", 0),
            max_length=spec.get("max_length"),
        )
    if ctrl_type == AskControlType.SLIDER:
        return SliderControl(
            min=spec.get("min", 0),
            max=spec.get("max", 100),
            step=spec.get("step", 1),
            default=spec.get("default"),
        )
    if ctrl_type == AskControlType.PANEL:
        fields = {
            fkey: _build_one(fkey, fspec)
            for fkey, fspec in (spec.get("fields") or {}).items()
        }
        return PanelControl(fields=fields, description=spec.get("description", ""))
    if ctrl_type == AskControlType.CUSTOM_PANEL:
        return CustomPanelControl(schema=spec.get("schema", {}))

    raise ValueError(f"unsupported control type: {ctrl_type!r} (control {key_or_index!r})")


def _build_controls(spec: dict[str, Any]) -> dict[str, Any]:
    """Parse a ``{key: control_spec}`` mapping into control objects."""
    return {key: _build_one(key, ctrl_spec) for key, ctrl_spec in spec.items()}


_NO_DEFAULT = object()


def _explicit_default_for_spec(spec: dict[str, Any]) -> Any:
    """Return only caller-declared safe defaults; never invent creative taste."""
    ctrl_type = spec.get("type")
    if ctrl_type in {AskControlType.SELECT, AskControlType.SLIDER}:
        return spec["default"] if "default" in spec else _NO_DEFAULT
    if ctrl_type == AskControlType.MULTI_SELECT:
        if "default" in spec:
            return list(spec.get("default") or [])
        return [] if int(spec.get("min", 0) or 0) == 0 else _NO_DEFAULT
    if ctrl_type == AskControlType.PANEL:
        result: dict[str, Any] = {}
        for key, child in (spec.get("fields") or {}).items():
            value = _explicit_default_for_spec(child)
            if value is _NO_DEFAULT:
                return _NO_DEFAULT
            result[key] = value
        return result
    # Text and custom panels are intentionally not fabricated.
    return _NO_DEFAULT


def _explicit_defaults(spec: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, control in spec.items():
        value = _explicit_default_for_spec(control)
        if value is _NO_DEFAULT:
            return {}
        result[key] = value
    return result


# ── default-answer synthesis (no-frontend / timeout fallback) ───────────────


def _default_answers(controls: dict[str, Any]) -> dict[str, Any]:
    """Best-effort valid answer for each control, used when no user answer arrives."""
    out: dict[str, Any] = {}
    for key, ctrl in controls.items():
        out[key] = _default_for(ctrl)
    return out


def _default_for(ctrl: Any) -> Any:
    if isinstance(ctrl, SelectControl):
        if ctrl.default is not None:
            return ctrl.default
        return ctrl.options[0]["value"] if ctrl.options else None
    if isinstance(ctrl, MultiSelectControl):
        need = max(0, int(ctrl.min or 0))
        return [opt["value"] for opt in ctrl.options[:need]]
    if isinstance(ctrl, TextControl):
        return ""
    if isinstance(ctrl, SliderControl):
        return ctrl.default if ctrl.default is not None else ctrl.min
    if isinstance(ctrl, PanelControl):
        return {fk: _default_for(fc) for fk, fc in ctrl.fields.items()}
    return {}  # custom_panel: schema-driven, cannot infer a default


__all__ = ["dispatch"]
