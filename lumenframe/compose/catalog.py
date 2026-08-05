"""Agent-facing catalog — the whole framing vocabulary in one call.

A machine-readable dict (for the ``catalog`` op) and a compact prose block (for
prompts). A test pins these to the *real* registrations — grids, framings, and
axes — so the documentation an agent reads can never drift from what runs.
"""
from __future__ import annotations

from typing import Any

from lumenframe.compose.framing import GRIDS
from lumenframe.compose.params import COMPOSE_AXES, compose_feedback
from lumenframe.compose.styles import FRAMINGS


def compose_catalog() -> dict[str, Any]:
    """Everything an agent needs to write a framing brief or a feedback phrase."""
    book = FRAMINGS.catalog()
    return {
        "framings": book["styles"],
        "framing_aliases": book["aliases"],
        "default_framing": book["default"],
        "grids": [dict(e) for e in GRIDS.catalog()],
        "axes": list(COMPOSE_AXES),
        "axis_meaning": {
            "tension": "how far off-centre / diagonal the subject sits (0 calm, 1 edge)",
            "balance": "how much counter-mass on the opposite side (→ symmetry)",
            "negative_space": "empty breathing room around the subject",
            "tightness": "crop closeness — how large the subject reads in frame",
        },
        "facings": ["left", "right", "up", "down"],
        "feedback_vocabulary": compose_feedback().vocabulary(),
        "brief": {
            "subjects": "[{bbox:[x,y,w,h] 0..1, weight?, facing?}] (>=1, required)",
            "canvas": "{width,height} or aspect '16:9'",
            "framing": "one of the framings above (or an alias)",
            "feeling": "adjective list, e.g. ['airy','tense']",
            "horizon": "optional source-y (0..1) snapped to a third",
            "params": "explicit axis overrides {tension|balance|negative_space|tightness}",
            "seed": "int; only decides genuine ties",
        },
    }


def describe_compose() -> str:
    """Compact prompt block: framings + axes + the brief shape."""
    lines = [
        "compose_frame briefs: {subjects:[{bbox:[x,y,w,h] 0..1, weight?, "
        "facing?:left|right|up|down}], canvas:{width,height}|aspect, framing, "
        "feeling:[…], horizon?, params:{tension|balance|negative_space|tightness:0..1}, seed}",
        FRAMINGS.describe("Framing"),
        "Grids: " + "; ".join(f"{e['name']} ({e['summary']})" for e in GRIDS.catalog()),
        "Taste floor: subject eye-line lands on a thirds/golden anchor (centre only "
        "for 'centered'); headroom scales with tightness but never crops the head; "
        "facing leaves lead room ahead; horizon snaps to a third; secondary mass "
        "balances to the opposite third; crop stays in-source at target aspect.",
        "Feedback: more/less + " + ", ".join(compose_feedback().vocabulary()[:16]) + ", …",
    ]
    return "\n".join(lines)
