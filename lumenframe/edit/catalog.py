"""Agent-facing catalog — the whole cut-grammar vocabulary in one call.

A machine-readable dict for tools (:func:`edit_catalog`) and a compact prose
block for prompts (:func:`describe_edit`). The catalog is derived from the *real*
registrations (styles, transitions, axes, feedback words), so it can never drift
from what the library actually does — a test pins it.
"""
from __future__ import annotations

from typing import Any

from lumenframe.edit.grammar import TRANSITIONS, seasoning_names
from lumenframe.edit.params import AXES, edit_feedback
from lumenframe.edit.styles import STYLES


def edit_catalog() -> dict[str, Any]:
    """Everything an agent needs to write an edit brief or a feedback phrase."""
    book = STYLES.catalog()
    return {
        "output": "cut_plan",
        "rides": "timeline",
        "axes": list(AXES),
        "styles": book["styles"],
        "style_aliases": book["aliases"],
        "default_style": book["default"],
        "transitions": [dict(e) for e in TRANSITIONS.catalog()],
        "seasoning_transitions": seasoning_names(),
        "brief_shape": {
            "clips": "[{id, duration (sec), has_action?, tags?, scene?}] — ≥2",
            "style": "one of " + ", ".join(STYLES.names()),
            "feeling": "adjectives, e.g. seamless / punchy / dreamy / 快",
            "params": "explicit axis overrides (0..1), win over style + feeling",
            "seed": "int — same brief + seed → byte-identical plan",
        },
        "feedback_vocabulary": edit_feedback().vocabulary(),
    }


def describe_edit() -> str:
    """Compact prompt block: brief shape + styles + transitions + feedback."""
    vocab = edit_feedback().vocabulary()
    lines = [
        "edit_grammar briefs: {clips:[{id, duration, has_action?, tags?, scene?}] (≥2),",
        " style, feeling:[…], params:{pace|invisibility|drama|variety: 0..1}, seed}",
        " → a cut plan: one {from_clip, to_clip, transition, duration_ms, j_cut_ms/",
        "   l_cut_ms, trim_in_adjust/trim_out_adjust, reason} per join.",
        STYLES.describe("cut"),
        TRANSITIONS.describe("transitions (straight cuts are the default; the rest are capped seasoning):"),
        "Feedback phrases: more/less + " + ", ".join(vocab[:16]) + ", …",
    ]
    return "\n".join(lines)
