"""Human feedback loop — creative phrases become semantic deltas.

The adjustment contract: feedback **edits the brief**, the scene re-derives
with the same seed. We never patch SVG text — a "more playful" scene is a
*re-choreographed* scene, not a tweaked file.

Phrases are ``more/less X`` (or 更/再/少一点 X); each adjective maps to axis
deltas. Unknown phrases are returned, not raised — the agent decides whether
to ask or ignore.
"""
from __future__ import annotations

import re
from typing import Any

from lumenframe.vector.params import SEMANTIC_AXES, clamp01

#: adjective → axis deltas applied for "more <adjective>" (inverted for less).
ADJUSTMENTS: dict[str, dict[str, float]] = {
    "playful": {"playfulness": +0.2, "energy": +0.1},
    "俏皮": {"playfulness": +0.2, "energy": +0.1},
    "fun": {"playfulness": +0.2},
    "chaotic": {"complexity": +0.2, "smoothness": -0.15},
    "乱": {"complexity": +0.2, "smoothness": -0.15},
    "busy": {"density": +0.2, "complexity": +0.1},
    "premium": {"elegance": +0.25, "energy": -0.1},
    "高级": {"elegance": +0.25, "energy": -0.1},
    "elegant": {"elegance": +0.25},
    "优雅": {"elegance": +0.25},
    "organic": {"organicness": +0.25},
    "有机": {"organicness": +0.25},
    "futuristic": {"organicness": +0.15, "smoothness": +0.1},
    "未来感": {"organicness": +0.15, "smoothness": +0.1},
    "energetic": {"energy": +0.2},
    "活力": {"energy": +0.2},
    "fast": {"energy": +0.2},
    "快": {"energy": +0.2},
    "slow": {"energy": -0.2},
    "慢": {"energy": -0.2},
    "calm": {"energy": -0.2, "smoothness": +0.15},
    "平静": {"energy": -0.2, "smoothness": +0.15},
    "smooth": {"smoothness": +0.2},
    "顺滑": {"smoothness": +0.2},
    "minimal": {"complexity": -0.2, "density": -0.2},
    "极简": {"complexity": -0.2, "density": -0.2},
    "simple": {"complexity": -0.2},
    "简单": {"complexity": -0.2},
    "rich": {"density": +0.2, "complexity": +0.15},
    "丰富": {"density": +0.2, "complexity": +0.15},
    "dense": {"density": +0.2},
    "密": {"density": +0.2},
    "geometric": {"organicness": -0.2},
    "几何": {"organicness": -0.2},
    "bouncy": {"playfulness": +0.25},
    "弹": {"playfulness": +0.25},
    "subtle": {"energy": -0.15, "density": -0.1, "elegance": +0.1},
    "克制": {"energy": -0.15, "density": -0.1, "elegance": +0.1},
    "dramatic": {"energy": +0.15, "playfulness": +0.15},
    "戏剧": {"energy": +0.15, "playfulness": +0.15},
}

_MORE_RE = re.compile(
    r"^\s*(?P<dir>more|less|slightly more|slightly less|much more|much less|更|再|多一点|多点|少一点|少点)\s*(?P<word>.+?)\s*$",
    re.IGNORECASE,
)

_DIR_SIGNS = {
    "more": 1.0, "less": -1.0,
    "slightly more": 0.5, "slightly less": -0.5,
    "much more": 1.6, "much less": -1.6,
    "更": 1.0, "再": 1.0, "多一点": 0.5, "多点": 0.5, "少一点": -0.5, "少点": -1.0 * 0.5,
}


def parse_feedback(phrases: list[str]) -> tuple[dict[str, float], list[str]]:
    """Feedback phrases → accumulated axis deltas + unrecognised phrases.

    Accepts "more playful", "much less chaotic", bare adjectives ("premium"
    == "more premium"), and the Chinese equivalents (更俏皮 / 少一点乱).
    """
    deltas: dict[str, float] = {}
    unknown: list[str] = []
    for phrase in phrases or []:
        raw = str(phrase).strip().lower()
        if not raw:
            continue
        sign, word = 1.0, raw
        m = _MORE_RE.match(raw)
        if m:
            sign = _DIR_SIGNS.get(m.group("dir").lower(), 1.0)
            word = m.group("word").strip()
        table = ADJUSTMENTS.get(word)
        if table is None:
            # Try stripping a trailing 的/感 (更高级的 / 更未来感).
            table = ADJUSTMENTS.get(word.rstrip("的感 "))
        if table is None:
            unknown.append(str(phrase))
            continue
        for axis, delta in table.items():
            deltas[axis] = deltas.get(axis, 0.0) + delta * sign
    return deltas, unknown


def apply_feedback(brief: dict[str, Any], phrases: list[str]) -> tuple[dict[str, Any], list[str]]:
    """A new brief with feedback folded into ``params`` overrides.

    The returned brief carries absolute axis values in ``params`` (current
    resolved axes + deltas, clamped) so repeated adjustments accumulate
    predictably. The original brief is not mutated.
    """
    from lumenframe.vector.styles import resolve_params

    deltas, unknown = parse_feedback(phrases)
    new_brief = {**brief, "params": dict(brief.get("params") or {})}
    if deltas:
        current = resolve_params(
            style=brief.get("style"),
            feelings=list(brief.get("feeling") or []),
            overrides=dict(brief.get("params") or {}),
        )
        for axis in SEMANTIC_AXES:
            if axis in deltas:
                new_brief["params"][axis] = round(
                    clamp01(current.axes[axis] + deltas[axis]), 4
                )
    return new_brief, unknown


def feedback_vocabulary() -> list[str]:
    """The recognised adjectives (agent-facing catalog)."""
    return sorted(ADJUSTMENTS)
