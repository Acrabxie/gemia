from __future__ import annotations

import json
from typing import Any

from .prompt_slimming import estimate_tokens


def audit_provider_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Estimate token split for the final provider request envelope."""
    messages = payload.get("messages") if isinstance(payload, dict) else []
    if not isinstance(messages, list):
        messages = []

    static_text = _message_text(messages[0]) if messages else ""
    dynamic_text = ""
    if len(messages) >= 3:
        dynamic_text = _message_text(messages[1])
    user_payload = _extract_user_payload(messages)
    active_specs = user_payload.get("active_primitive_specs") if isinstance(user_payload, dict) else []
    planning_mode = user_payload.get("planning_mode") if isinstance(user_payload, dict) else {}
    selected_skills = planning_mode.get("selected_skills") if isinstance(planning_mode, dict) else []
    selected_primitives = [
        str(item.get("name"))
        for item in active_specs
        if isinstance(item, dict) and item.get("name")
    ] if isinstance(active_specs, list) else []

    project_context = {}
    if isinstance(user_payload, dict):
        for key in ("project_state", "video_context", "context_trust_boundaries"):
            if key in user_payload:
                project_context[key] = user_payload[key]
    user_request = {}
    if isinstance(user_payload, dict):
        for key in ("request", "raw_request", "clarifications"):
            if key in user_payload:
                user_request[key] = user_payload[key]

    return {
        "static_contract_tokens": estimate_tokens(static_text),
        "active_skill_context_tokens": estimate_tokens(dynamic_text) if dynamic_text else 0,
        "active_specs_tokens": estimate_tokens(active_specs),
        "project_context_tokens": estimate_tokens(project_context),
        "user_request_tokens": estimate_tokens(user_request),
        "total_provider_tokens": estimate_tokens(payload),
        "selected_skills": selected_skills if isinstance(selected_skills, list) else [],
        "selected_primitives": selected_primitives,
    }


def _message_text(message: Any) -> str:
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, dict):
                value = part.get("text")
                if value is not None:
                    parts.append(str(value))
            elif isinstance(part, str):
                parts.append(part)
        return "\n".join(parts)
    return ""


def _extract_user_payload(messages: list[Any]) -> dict[str, Any]:
    for message in reversed(messages):
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        text = _message_text(message)
        if not text:
            continue
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}
    return {}
