"""Agent-facing catalog — the whole grading vocabulary in one call.

Mirrors :func:`lumenframe.vector.catalog.vector_catalog`: a machine-readable dict
for tools and a compact prose block for prompts. A test pins this to the real
registrations (:meth:`Registry.check_catalog`) so the prompt can never advertise
a look, op, or feedback word the engine does not actually implement.
"""
from __future__ import annotations

from typing import Any

from lumenframe.grade.grade import OP_FAMILIES, PIPELINE, REGISTRY
from lumenframe.grade.params import FEEDBACK, GRADE_AXES, SPACE
from lumenframe.grade.styles import STYLES

#: One-line meaning for each axis (prompt sugar).
_AXIS_HELP: dict[str, str] = {
    "warmth": "cool ↔ warm white balance",
    "contrast": "steepness of the protected tone S-curve",
    "saturation": "colour intensity (hard ceiling enforced)",
    "lift": "lifted / faded blacks (matte)",
    "drama": "mood: vignette weight + downward pivot bias",
    "filmic": "analogue optics: grain + halation",
}


def grade_catalog() -> dict[str, Any]:
    """Everything an agent needs to write a grading brief or a feedback phrase."""
    style_cat = STYLES.catalog()
    return {
        "looks": style_cat["styles"],
        "aliases": style_cat["aliases"],
        "default_look": style_cat["default"],
        "axes": {a: _AXIS_HELP.get(a, "") for a in GRADE_AXES},
        "feelings": sorted(SPACE.feelings),
        "ops": [dict(e) for e in REGISTRY.catalog()],
        "op_families": list(OP_FAMILIES),
        "pipeline": list(PIPELINE),
        "feedback_vocabulary": FEEDBACK.vocabulary(),
        "brief_shape": {
            "look": "one of looks/aliases (default: neutral)",
            "feeling": ["adjective phrases, 中/英"],
            "intensity": "0..1 — scales the whole grade toward neutral",
            "params": {a: "0..1" for a in GRADE_AXES},
            "seed": "int (grain field anchor)",
        },
        "output": "grade recipe (temperature/tint, lift/gamma/gain wheels, "
                  "contrast S-curve, saturation/vibrance, split hues, "
                  "black/white points, vignette, grain, halation) + preview SVG "
                  "+ ffmpeg_filter",
    }


def describe_grade() -> str:
    """Compact prompt block: looks + ops + brief shape."""
    lines = [
        "grade briefs: {look: neutral|teal_orange|film|bleach_bypass|noir|"
        "day_for_night|pastel|cyberpunk|vintage|clean, feeling:[…], "
        "intensity: 0..1, params:{warmth|contrast|saturation|lift|drama|filmic: 0..1}, seed}",
        STYLES.describe("grade looks"),
        REGISTRY.describe("grade pipeline (fixed order, each enforces a craft rule):"),
        "Feedback phrases: more/less + " + ", ".join(FEEDBACK.vocabulary()[:16]) + ", …",
    ]
    return "\n".join(lines)
