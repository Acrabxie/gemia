"""Semantic parameter system — where creative language becomes numbers.

Agents (and humans) speak in seven semantic axes, each 0..1:

* ``energy``      — how much kinetic force the scene carries
* ``smoothness``  — how rounded the motion curves feel
* ``playfulness`` — bounce, overshoot, irregular rhythm
* ``elegance``    — restraint: fewer moves, longer holds, precision
* ``complexity``  — how many elements / sub-movements participate
* ``density``     — how visually packed the canvas is (particles, decoration)
* ``organicness`` — geometric ↔ organic form and timing

Resolution order (later wins)::

    style baseline  →  feeling adjectives (±nudges)  →  explicit overrides

The result is a :class:`ResolvedParams` exposing DERIVED low-level values —
the only place semantics map to numbers, unit-tested as a table. Behaviours
and choreography read the derived values and never invent their own mapping.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

SEMANTIC_AXES: tuple[str, ...] = (
    "energy", "smoothness", "playfulness", "elegance",
    "complexity", "density", "organicness",
)

#: Neutral baseline when no style is chosen.
NEUTRAL: dict[str, float] = {axis: 0.5 for axis in SEMANTIC_AXES}

#: Feeling adjectives → axis nudges. Bilingual on purpose: the tool surface
#: is used from Chinese and English briefs alike. Extend freely — unknown
#: feelings are reported, not fatal.
FEELINGS: dict[str, dict[str, float]] = {
    # energy / drive
    "energetic": {"energy": +0.2, "playfulness": +0.1},
    "dynamic": {"energy": +0.15},
    "calm": {"energy": -0.2, "smoothness": +0.15},
    "gentle": {"energy": -0.15, "smoothness": +0.2},
    "bold": {"energy": +0.15, "complexity": -0.05},
    "快": {"energy": +0.2},
    "活力": {"energy": +0.2, "playfulness": +0.1},
    "平静": {"energy": -0.2, "smoothness": +0.15},
    # character
    "playful": {"playfulness": +0.2, "organicness": +0.05},
    "fun": {"playfulness": +0.2},
    "serious": {"playfulness": -0.2, "elegance": +0.1},
    "俏皮": {"playfulness": +0.2},
    "creative": {"organicness": +0.1, "complexity": +0.1},
    "创意": {"organicness": +0.1, "complexity": +0.1},
    "intelligent": {"smoothness": +0.1, "elegance": +0.1},
    "智能": {"smoothness": +0.1, "elegance": +0.1},
    "futuristic": {"organicness": +0.15, "smoothness": +0.1},
    "未来": {"organicness": +0.15, "smoothness": +0.1},
    "premium": {"elegance": +0.2, "energy": -0.1},
    "高级": {"elegance": +0.2, "energy": -0.1},
    "elegant": {"elegance": +0.2, "smoothness": +0.1},
    "优雅": {"elegance": +0.2, "smoothness": +0.1},
    "minimal": {"complexity": -0.2, "density": -0.15, "elegance": +0.1},
    "极简": {"complexity": -0.2, "density": -0.15, "elegance": +0.1},
    "rich": {"density": +0.15, "complexity": +0.15},
    "丰富": {"density": +0.15, "complexity": +0.15},
    "organic": {"organicness": +0.25},
    "有机": {"organicness": +0.25},
    "geometric": {"organicness": -0.2},
    "几何": {"organicness": -0.2},
    "warm": {"organicness": +0.1, "smoothness": +0.1},
    "cinematic": {"elegance": +0.15, "energy": -0.05},
    "电影感": {"elegance": +0.15, "energy": -0.05},
}

#: Hard cap on particle instances per scene — SVG element count discipline.
PARTICLE_CAP = 420


def clamp01(v: float) -> float:
    return min(max(float(v), 0.0), 1.0)


@dataclass(frozen=True)
class ResolvedParams:
    """Semantic axes + the derived low-level values behaviours consume."""

    axes: dict[str, float] = field(default_factory=lambda: dict(NEUTRAL))
    #: ease token set, chosen by style + smoothness (see styles.py).
    ease_enter: str = "enter"
    ease_exit: str = "exit"
    ease_move: str = "move"
    #: extra style hints behaviours may read (stroke weight bias, glow, …).
    hints: dict[str, Any] = field(default_factory=dict)
    #: feelings that matched nothing (surfaced to the caller, never fatal).
    unknown_feelings: tuple[str, ...] = ()

    # ── derived values (the mapping table) ──────────────────────────────

    @property
    def tempo(self) -> float:
        """Duration multiplier: 1 = neutral, >1 slower. Elegance slows,
        energy quickens — energy dominates 2:1."""
        e, g = self.axes["energy"], self.axes["elegance"]
        return round(clamp01(0.5 - (e - 0.5) * 0.8 + (g - 0.5) * 0.4) + 0.55, 4)

    @property
    def overshoot(self) -> float:
        """Scale/position overshoot amount, 0..0.35. Playfulness drives it,
        elegance suppresses it to zero."""
        p, g = self.axes["playfulness"], self.axes["elegance"]
        return round(max(0.0, p * 0.35 - g * 0.25), 4)

    @property
    def stagger_spread(self) -> float:
        """Fraction of a window spent staggering members, 0.05..0.6."""
        e, c = self.axes["energy"], self.axes["complexity"]
        return round(0.05 + (1.0 - e) * 0.25 + c * 0.3, 4)

    @property
    def particle_count(self) -> int:
        """Instances for particle behaviours (density × complexity, capped)."""
        d, c = self.axes["density"], self.axes["complexity"]
        return min(PARTICLE_CAP, int(24 + d * 260 + c * 120))

    @property
    def wobble(self) -> float:
        """Organic irregularity 0..1 fed to blob/liquid/jitter maths."""
        return round(self.axes["organicness"], 4)

    @property
    def hold_fraction(self) -> float:
        """Fraction of the scene reserved as final negative-space hold."""
        g = self.axes["elegance"]
        return round(0.08 + g * 0.17, 4)

    @property
    def decoration_share(self) -> float:
        """How much of the canvas budget decoration may claim, 0..0.5."""
        d, g = self.axes["density"], self.axes["elegance"]
        return round(max(0.0, d * 0.5 - g * 0.15), 4)

    @property
    def ease_emphasis(self) -> str:
        """Emphasis-move ease: dramatic when playful, soft when elegant."""
        if self.overshoot >= 0.12:
            return "dramatic"
        return "soft" if self.axes["elegance"] >= 0.55 else self.ease_move

    def to_dict(self) -> dict[str, Any]:
        return {
            "axes": dict(self.axes),
            "derived": {
                "tempo": self.tempo,
                "overshoot": self.overshoot,
                "stagger_spread": self.stagger_spread,
                "particle_count": self.particle_count,
                "wobble": self.wobble,
                "hold_fraction": self.hold_fraction,
                "decoration_share": self.decoration_share,
                "ease": {
                    "enter": self.ease_enter,
                    "exit": self.ease_exit,
                    "move": self.ease_move,
                    "emphasis": self.ease_emphasis,
                },
            },
            "hints": dict(self.hints),
            "unknown_feelings": list(self.unknown_feelings),
        }


def resolve(
    *,
    baseline: Mapping[str, float] | None = None,
    feelings: list[str] | None = None,
    overrides: Mapping[str, float] | None = None,
    ease_set: Mapping[str, str] | None = None,
    hints: Mapping[str, Any] | None = None,
) -> ResolvedParams:
    """Resolve semantic axes: baseline → feeling nudges → explicit overrides.

    Unknown feeling words are collected (case/whitespace-normalised lookup)
    rather than raised — a brief with one odd adjective should not fail; the
    caller surfaces ``unknown_feelings`` so the agent can react.
    Unknown *override* axes DO raise: an explicit number aimed at a
    nonexistent axis is a programming error, not taste.
    """
    axes = {**NEUTRAL, **{k: clamp01(v) for k, v in (baseline or {}).items() if k in SEMANTIC_AXES}}
    bad_base = set(baseline or {}) - set(SEMANTIC_AXES)
    if bad_base:
        raise ValueError(f"unknown baseline axes: {sorted(bad_base)}")

    unknown: list[str] = []
    for word in feelings or []:
        key = str(word).strip().lower()
        nudges = FEELINGS.get(key)
        if nudges is None:
            unknown.append(str(word))
            continue
        for axis, delta in nudges.items():
            axes[axis] = clamp01(axes[axis] + delta)

    bad = set(overrides or {}) - set(SEMANTIC_AXES)
    if bad:
        raise ValueError(f"unknown semantic axes: {sorted(bad)} (use {SEMANTIC_AXES})")
    for axis, value in (overrides or {}).items():
        axes[axis] = clamp01(value)

    eases = {"enter": "enter", "exit": "exit", "move": "move", **(ease_set or {})}
    return ResolvedParams(
        axes=axes,
        ease_enter=eases["enter"],
        ease_exit=eases["exit"],
        ease_move=eases["move"],
        hints=dict(hints or {}),
        unknown_feelings=tuple(unknown),
    )
