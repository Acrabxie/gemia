"""Human feedback loop — creative phrases become semantic deltas.

The adjustment contract every point library shares: feedback **edits the brief**
and the result re-derives with the same seed. We never patch the output file — a
"more cinematic" grade is a *re-derived* grade, not a nudged LUT.

Phrases are ``more/less X`` (or 更/再/少一点 X); each adjective maps to axis
deltas. Unknown phrases are returned, not raised — the agent decides whether to
ask or ignore. Generalises :mod:`lumenframe.vector.feedback`: the parser is
shared; a library supplies its own adjective→delta table (extending the base).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Mapping

from lumenframe.craft.params import AxisSpace, clamp01

#: Shared bilingual adjective → axis deltas for "more <adjective>" (inverted for
#: less). Only deltas naming an axis a library declares take effect; a library
#: adds domain adjectives (grade: "teal", camera: "handheld") via ``extend``.
BASE_ADJUSTMENTS: dict[str, dict[str, float]] = {
    "playful": {"playfulness": +0.2}, "俏皮": {"playfulness": +0.2},
    "fun": {"playfulness": +0.2},
    "energetic": {"energy": +0.2}, "活力": {"energy": +0.2},
    "fast": {"energy": +0.2}, "快": {"energy": +0.2},
    "slow": {"energy": -0.2}, "慢": {"energy": -0.2},
    "calm": {"energy": -0.2, "smoothness": +0.15}, "平静": {"energy": -0.2, "smoothness": +0.15},
    "smooth": {"smoothness": +0.2}, "顺滑": {"smoothness": +0.2},
    "premium": {"elegance": +0.25, "energy": -0.1}, "高级": {"elegance": +0.25, "energy": -0.1},
    "elegant": {"elegance": +0.25}, "优雅": {"elegance": +0.25},
    "minimal": {"complexity": -0.2, "density": -0.2}, "极简": {"complexity": -0.2, "density": -0.2},
    "simple": {"complexity": -0.2}, "简单": {"complexity": -0.2},
    "rich": {"density": +0.2, "complexity": +0.15}, "丰富": {"density": +0.2, "complexity": +0.15},
    "dense": {"density": +0.2}, "密": {"density": +0.2},
    "dramatic": {"drama": +0.2, "energy": +0.1}, "戏剧": {"drama": +0.2, "energy": +0.1},
    "subtle": {"energy": -0.15, "elegance": +0.1}, "克制": {"energy": -0.15, "elegance": +0.1},
    "cinematic": {"drama": +0.15, "elegance": +0.1}, "电影感": {"drama": +0.15, "elegance": +0.1},
    "warm": {"warmth": +0.2}, "暖": {"warmth": +0.2},
    "cool": {"warmth": -0.2}, "冷": {"warmth": -0.2},
    "moody": {"drama": +0.15, "energy": -0.1}, "氛围": {"drama": +0.15, "energy": -0.1},
    "clean": {"complexity": -0.15, "elegance": +0.1}, "干净": {"complexity": -0.15, "elegance": +0.1},
    "bold": {"energy": +0.15, "drama": +0.1},
}

# Longer/compound directions MUST precede their prefixes ("much more" before
# "much", "slightly less" before "slightly") so the intended one wins the match.
_MORE_RE = re.compile(
    r"^\s*(?P<dir>slightly more|slightly less|much more|much less|a lot more|a lot less|"
    r"more|less|much|slightly|very|a lot|"
    r"更|再|多一点|多点|少一点|少点|稍微)\s*(?P<word>.+?)\s*$",
    re.IGNORECASE,
)

_DIR_SIGNS = {
    "more": 1.0, "less": -1.0,
    "slightly more": 0.5, "slightly less": -0.5,
    "much more": 1.6, "much less": -1.6,
    "a lot more": 1.6, "a lot less": -1.6,
    # bare intensifiers before a (comparative) adjective: "much warmer", "very bold"
    "much": 1.5, "very": 1.3, "a lot": 1.5, "slightly": 0.5,
    "更": 1.0, "再": 1.0, "多一点": 0.5, "多点": 0.5, "少一点": -0.5, "少点": -0.5, "稍微": 0.5,
}


@dataclass
class FeedbackVocab:
    """A library's adjective→delta table over its axis space."""

    space: AxisSpace
    adjustments: dict[str, dict[str, float]] = field(
        default_factory=lambda: {k: dict(v) for k, v in BASE_ADJUSTMENTS.items()}
    )

    def extend(self, extra: Mapping[str, dict[str, float]]) -> "FeedbackVocab":
        self.adjustments.update({k: dict(v) for k, v in extra.items()})
        return self

    def vocabulary(self) -> list[str]:
        """Recognised adjectives that actually move a declared axis."""
        axes = set(self.space.axes)
        return sorted(w for w, d in self.adjustments.items() if set(d) & axes)

    def _lookup(self, word: str) -> dict[str, float] | None:
        """Find an adjective's delta table, tolerating comparatives.

        Tries the word as-is, then with a trailing 的/感 stripped, then the
        English comparative form ("warmer" → "warm", "cooler" → "cool"). The
        comparative fallback is conservative — only "<base>er"/"<base>r" where
        ``base`` is itself a known adjective — so it never guesses ("premier"
        stays unknown)."""
        table = self.adjustments.get(word) or self.adjustments.get(word.rstrip("的感 "))
        if table is not None:
            return table
        if word.endswith("er"):
            for base in (word[:-2], word[:-1]):  # warmer→warm, finer→fine
                if base in self.adjustments:
                    return self.adjustments[base]
        return None

    def parse(self, phrases: list[str]) -> tuple[dict[str, float], list[str]]:
        """Phrases → accumulated axis deltas + unrecognised phrases.

        Accepts "more playful", "much less chaotic", bare adjectives ("premium"
        == "more premium"), and Chinese equivalents (更暖 / 少一点戏剧). A phrase
        whose adjective is known but touches no declared axis is treated as
        unrecognised for *this* library (so the caller can honestly report it).
        """
        axes = set(self.space.axes)
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
            table = self._lookup(word)
            if table is None or not (set(table) & axes):
                unknown.append(str(phrase))
                continue
            for axis, delta in table.items():
                if axis in axes:
                    deltas[axis] = deltas.get(axis, 0.0) + delta * sign
        return deltas, unknown

    def apply(
        self,
        brief: dict[str, Any],
        phrases: list[str],
        resolve_axes: "callable",
    ) -> tuple[dict[str, Any], list[str]]:
        """Fold feedback into a NEW brief's ``params`` (absolute, clamped).

        ``resolve_axes(brief) -> ResolvedAxes`` lets accumulation start from the
        brief's *current* resolved axes so repeated adjustments compound
        predictably. The original brief is never mutated.
        """
        deltas, unknown = self.parse(phrases)
        new_brief = {**brief, "params": dict(brief.get("params") or {})}
        if deltas:
            current = resolve_axes(brief)
            for axis in self.space.axes:
                if axis in deltas:
                    new_brief["params"][axis] = round(
                        clamp01(current.axis(axis) + deltas[axis]), 4)
        return new_brief, unknown
