"""Agent-facing catalog — the kinetic vocabulary in one call.

A machine-readable dict for the tool's ``op:"catalog"`` and a compact prose
block for prompts, mirroring :mod:`lumenframe.vector.catalog`. A test pins this
to the live registries so the documented vocabulary can never drift from what
actually runs.
"""
from __future__ import annotations

from typing import Any

from lumenframe.templates import theme

from lumenframe.kinetic.params import AXES, KINETIC_FEELINGS
from lumenframe.kinetic.styles import STYLES
from lumenframe.kinetic.typography import LAYOUTS, REVEALS


def kinetic_catalog() -> dict[str, Any]:
    """Everything an agent needs to write a kinetic brief or feedback phrase."""
    return {
        "layouts": {e["name"]: e["summary"] for e in LAYOUTS.catalog()},
        "reveals": {e["name"]: e["summary"] for e in REVEALS.catalog()},
        "styles": STYLES.catalog()["styles"],
        "style_aliases": STYLES.catalog()["aliases"],
        "default_style": STYLES.catalog()["default"],
        "axes": list(AXES),
        "feelings": sorted(KINETIC_FEELINGS),
        "feedback_vocabulary": _feedback_words(),
        "palettes": theme.palette_names(),
        "brief": {
            "text | lines": "the copy — a string, or a list of lines",
            "layout": LAYOUTS.names(),
            "style": STYLES.names(),
            "reveal": REVEALS.names(),
            "feeling": "feeling words, e.g. bold / airy / 快",
            "emphasis": "words lifted to the accent colour",
            "duration": "seconds (≤ 58)", "canvas": "{width, height}",
            "palette": "theme name or {role: hex}", "seed": "int",
        },
    }


def _feedback_words() -> list[str]:
    from lumenframe.kinetic.params import kinetic_feedback
    return kinetic_feedback().vocabulary()


def describe_kinetic() -> str:
    """Compact prompt block: brief shape + layouts + reveals + styles."""
    lines = [
        "kinetic_type briefs: {text|lines, layout, style, feeling:[…], reveal?, "
        "emphasis?:[…], duration, canvas, palette, seed}",
        "axes: " + ", ".join(AXES) + " (0..1)",
        LAYOUTS.describe("layouts:"),
        REVEALS.describe("reveals:"),
        STYLES.describe("kinetic"),
        "feedback: more/less + " + ", ".join(_feedback_words()[:14]) + ", …",
    ]
    return "\n".join(lines)
