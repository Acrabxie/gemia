"""Sub-agent registry for Gemia.

Each sub-agent wraps a GeminiAdapter pointed at a specific OpenRouter model.
Agents are identified by short names; the registry handles construction and caching.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .gemini_adapter import GeminiAdapter

# ---------------------------------------------------------------------------
# Agent definitions
# ---------------------------------------------------------------------------

@dataclass
class SubAgentDef:
    name: str                  # short name used in API / config
    model: str                 # OpenRouter model slug
    role: str                  # "planner" | "reviewer" | "general"
    description: str = ""
    temperature: float = 0.0
    log_dir: str = ""

    def __post_init__(self) -> None:
        if not self.log_dir:
            self.log_dir = f"logs/{self.name}"


# Built-in agents. Add new models here. Names match the actual underlying
# model so that planner self-introduction, OpenRouter cache_control fingerprint,
# and the front-end agent picker stay consistent. Old names (`Gemini3.1pro`,
# `GPT5.4`) are still accepted as aliases below for backward compat.
BUILTIN_AGENTS: dict[str, SubAgentDef] = {
    "Gemini31Pro": SubAgentDef(
        name="Gemini31Pro",
        model="google/gemini-3.1-pro-preview",
        role="planner",
        description="Gemini 3.1 Pro via OpenRouter. Default high-quality Lumeri planner.",
    ),
    "GeminiFlash3": SubAgentDef(
        name="GeminiFlash3",
        model="google/gemini-3-flash-preview",
        role="planner",
        description="Gemini 3 Flash via OpenRouter. Optional fast planner, not the default.",
    ),
    "GPT45": SubAgentDef(
        name="GPT45",
        model="openai/gpt-4.5",
        role="reviewer",
        description="GPT-4.5 via OpenRouter. Used for plan review and critique.",
        temperature=0.2,
    ),
}

# Backwards-compatible aliases for old API/UI strings — resolved by SubAgentRegistry.get
AGENT_ALIASES: dict[str, str] = {
    "Gemini3.1pro": "Gemini31Pro",
    "LumeriPlanner": "Gemini31Pro",
    "GPT5.4": "GPT45",
}

# Default primary planner name
DEFAULT_PLANNER = "Gemini31Pro"
DEFAULT_REVIEWER = "GPT45"


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

class SubAgentRegistry:
    """Builds and caches GeminiAdapter instances per agent name."""

    def __init__(self, api_key: str | None = None) -> None:
        self._api_key = api_key
        self._cache: dict[str, "GeminiAdapter"] = {}

    def _build(self, defn: SubAgentDef) -> "GeminiAdapter":
        from .gemini_adapter import GeminiAdapter
        return GeminiAdapter(
            api_key=self._api_key,
            model=defn.model,
            log_dir=defn.log_dir,
        )

    def get(self, name: str) -> "GeminiAdapter":
        """Return a cached adapter for the named agent.

        Resolves legacy aliases (`Gemini3.1pro`, `GPT5.4`) so older clients
        keep working after the rename.
        """
        canonical = AGENT_ALIASES.get(name, name)
        if canonical not in self._cache:
            defn = BUILTIN_AGENTS.get(canonical)
            if defn is None:
                raise KeyError(f"Unknown sub-agent: {name!r}. Available: {list(BUILTIN_AGENTS)}")
            self._cache[canonical] = self._build(defn)
        return self._cache[canonical]

    def planner(self, name: str | None = None) -> "GeminiAdapter":
        return self.get(name or DEFAULT_PLANNER)

    def reviewer(self) -> "GeminiAdapter":
        return self.get(DEFAULT_REVIEWER)

    @staticmethod
    def list_agents() -> list[dict]:
        return [
            {
                "name": d.name,
                "model": d.model,
                "role": d.role,
                "description": d.description,
            }
            for d in BUILTIN_AGENTS.values()
        ]
