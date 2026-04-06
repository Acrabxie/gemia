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


# Built-in agents. Add new models here.
BUILTIN_AGENTS: dict[str, SubAgentDef] = {
    "Gemini3.1pro": SubAgentDef(
        name="Gemini3.1pro",
        model="google/gemini-3-flash-preview",
        role="planner",
        description="Gemini 3 Flash via OpenRouter. Fast and cost-efficient planner.",
    ),
    "GPT5.4": SubAgentDef(
        name="GPT5.4",
        model="openai/gpt-4.5",
        role="reviewer",
        description="GPT 5.4 via OpenRouter. Used for plan review and critique.",
        temperature=0.2,
    ),
}

# Default primary planner name
DEFAULT_PLANNER = "Gemini3.1pro"
DEFAULT_REVIEWER = "GPT5.4"


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
        """Return a cached adapter for the named agent."""
        if name not in self._cache:
            defn = BUILTIN_AGENTS.get(name)
            if defn is None:
                raise KeyError(f"Unknown sub-agent: {name!r}. Available: {list(BUILTIN_AGENTS)}")
            self._cache[name] = self._build(defn)
        return self._cache[name]

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
