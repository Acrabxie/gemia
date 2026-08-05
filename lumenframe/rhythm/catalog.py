"""Agent-facing catalog — the whole rhythm vocabulary in one call.

Mirrors ``lumenframe.vector.catalog``: a machine-readable dict for tools and a
compact prose block for prompts. A test pins these to the real registrations
(anti-drift), so the vocabulary an agent is told about can never diverge from
what the library actually runs.
"""
from __future__ import annotations

from typing import Any

from lumenframe.rhythm.params import RHYTHM_AXES, rhythm_vocab
from lumenframe.rhythm.rhythm import SYNC
from lumenframe.rhythm.styles import BOOK


def rhythm_catalog() -> dict[str, Any]:
    """Everything an agent needs to write a rhythm brief or a feedback phrase."""
    return {
        "axes": list(RHYTHM_AXES),
        "styles": BOOK.catalog(),
        "sync_patterns": SYNC.catalog(),
        "feedback_vocabulary": rhythm_vocab().vocabulary(),
        "brief_shape": {
            "bpm": "number (required, 20..400)",
            "time_signature": "[numerator, denominator] (default [4, 4])",
            "sections": "[{name, bars}] — intro/verse/chorus/drop …",
            "clips": "[{id, duration}] — assigned to segments in order",
            "style": "sync pattern archetype or alias (e.g. edm→build_drop)",
            "feeling": "[adjectives] — driving/tight/sparse/building …",
            "energy": "0..1 shortcut → energy axis",
            "sync": "0..1 shortcut → tightness axis",
            "offset_ms": "shift the whole grid (audio latency)",
            "total_duration": "cap the grid to the audio length (seconds)",
            "params": "{axis: 0..1} explicit overrides (win)",
            "seed": "int — only varies syncopation choices",
        },
    }


def describe_rhythm() -> str:
    """Compact prompt block: styles + sync patterns + brief shape."""
    lines = [
        "rhythm_edit briefs: {bpm (required), time_signature:[4,4], sections:[{name,bars}],",
        " clips:[{id,duration}], style, feeling:[…], energy, sync, offset_ms, total_duration,",
        " params:{energy|tightness|drive|build: 0..1}, seed}",
        BOOK.describe("Sync"),
        SYNC.describe("Patterns:"),
        "Feedback phrases: more/less + " + ", ".join(rhythm_vocab().vocabulary()[:16]) + ", …",
    ]
    return "\n".join(lines)
