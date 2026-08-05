"""Agent-facing catalog — the whole creative vocabulary in one call.

Mirrors ``lumenframe.catalog`` / ``templates.describe_templates()``: a
machine-readable dict for tools and a compact prose block for prompts.
"""
from __future__ import annotations

from typing import Any

from lumenframe.templates import theme
from lumenframe.vector.behaviors import BEHAVIOR_CATALOG, _load, describe_behaviors
from lumenframe.vector.builders import MARK_PRESETS, SUBJECT_KINDS
from lumenframe.vector.feedback import feedback_vocabulary
from lumenframe.vector.params import FEELINGS, SEMANTIC_AXES
from lumenframe.vector.styles import ALIASES, STYLES, describe_styles


def vector_catalog() -> dict[str, Any]:
    """Everything an agent needs to write a brief or a feedback phrase."""
    _load()
    return {
        "subjects": {
            "kinds": list(SUBJECT_KINDS),
            "mark_presets": list(MARK_PRESETS),
        },
        "intents": ["reveal", "intro", "loop", "transition", "outro"],
        "styles": {name: spec["summary"] for name, spec in sorted(STYLES.items())},
        "style_aliases": dict(sorted(ALIASES.items())),
        "palettes": theme.palette_names(),
        "semantic_axes": list(SEMANTIC_AXES),
        "feelings": sorted(FEELINGS),
        "behaviors": [dict(entry) for entry in BEHAVIOR_CATALOG],
        "behavior_overrides": {
            "note": "pin any phase to any verb via brief.behaviors",
            "phases": ["entrance", "entrance_particles", "emphasis", "exit", "cycle"],
            "example": {"entrance": "assemble.magnetic",
                        "emphasis": "transform.spin_swap",
                        "exit": "explode.energy_release",
                        "cycle": ["flow.orbit"]},
        },
        "feedback_vocabulary": feedback_vocabulary(),
    }


def describe_vector_module() -> str:
    """Compact prompt block: styles + behaviours + brief shape."""
    lines = [
        "vector_motion briefs: {subject:{kind: logo_text|title|mark|abstract, text?, mark?, preset?},",
        " intent: reveal|intro|loop|transition|outro, style, feeling:[…], duration, palette, seed,",
        " params:{energy|smoothness|playfulness|elegance|complexity|density|organicness: 0..1}}",
        describe_styles(),
        describe_behaviors(),
        "Override any phase's behaviour: brief.behaviors = {entrance|emphasis|exit|cycle: verb}.",
        "Feedback phrases: more/less + " + ", ".join(feedback_vocabulary()[:16]) + ", …",
    ]
    return "\n".join(lines)
