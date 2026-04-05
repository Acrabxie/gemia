from __future__ import annotations

from typing import Any

from .gemini_adapter import (
    GeminiAdapter,
    build_plan_system_prompt,
    build_plan_or_ask_system_prompt,
    build_primitive_plan_system_prompt,
    build_revise_system_prompt,
)
from .sub_agents import SubAgentRegistry


class AIClient:
    """Minimal AI client for planning and revision only."""

    def __init__(self, adapter: GeminiAdapter | None = None, api_key: str | None = None) -> None:
        self._registry = SubAgentRegistry(api_key=api_key)
        # Legacy: if a specific adapter is passed, use it directly under "default"
        self._default_adapter = adapter

    def _adapter(self, agent: str | None = None) -> GeminiAdapter:
        if agent:
            return self._registry.get(agent)
        if self._default_adapter:
            return self._default_adapter
        return self._registry.planner()

    async def plan_from_prompt(
        self,
        request: str,
        *,
        input_path: str,
        output_path: str,
        context: dict[str, Any] | None = None,
        agent: str | None = None,
    ) -> dict[str, Any]:
        payload = {
            "request": request,
            "input_path": input_path,
            "output_path": output_path,
            "context": context or {},
        }
        plan = await self._adapter(agent).generate_plan_json(
            build_plan_system_prompt(),
            payload,
            tag="plan-from-prompt",
        )
        plan["input_path"] = input_path
        plan["output_path"] = output_path
        return plan

    async def plan_or_ask(
        self,
        request: str,
        *,
        input_path: str,
        output_path: str,
        answers: dict[str, str] | None = None,
        agent: str | None = None,
    ) -> dict[str, Any]:
        """Return either {"ask": true, "questions": [...]} or a full Plan JSON."""
        payload: dict[str, Any] = {
            "request": request,
            "input_path": input_path,
            "output_path": output_path,
        }
        if answers:
            payload["clarifications"] = answers
        result = await self._adapter(agent).generate_plan_json(
            build_plan_or_ask_system_prompt(),
            payload,
            tag="plan-or-ask",
        )
        if not result.get("ask"):
            result["input_path"] = input_path
            result["output_path"] = output_path
        return result

    async def plan_from_primitives(
        self,
        request: str,
        *,
        input_path: str,
        output_path: str,
        answers: dict[str, str] | None = None,
        agent: str | None = None,
    ) -> dict[str, Any]:
        """Plan using the full primitive function catalog (v2).

        Returns either ``{"ask": true, "questions": [...]}`` or a v2 Plan dict.
        """
        payload: dict[str, Any] = {
            "request": request,
            "input_path": input_path,
            "output_path": output_path,
        }
        if answers:
            payload["clarifications"] = answers
        result = await self._adapter(agent).generate_plan_json(
            build_primitive_plan_system_prompt(),
            payload,
            tag="plan-v2-primitives",
        )
        if not result.get("ask"):
            result.setdefault("version", "2.0")
        return result

    async def revise_plan(
        self,
        feedback: str,
        *,
        previous_plan: dict[str, Any],
        context: dict[str, Any] | None = None,
        agent: str | None = None,
    ) -> dict[str, Any]:
        payload = {
            "feedback": feedback,
            "previous_plan": previous_plan,
            "context": context or {},
        }
        plan = await self._adapter(agent).generate_plan_json(
            build_revise_system_prompt(),
            payload,
            tag="revise-plan",
        )
        plan.setdefault("input_path", previous_plan.get("input_path", ""))
        plan.setdefault("output_path", previous_plan.get("output_path", ""))
        return plan

    @staticmethod
    def list_agents() -> list[dict]:
        """Return all registered sub-agents."""
        return SubAgentRegistry.list_agents()
