"""Semantic axis system — the shared floor where creative language becomes numbers.

Every Lumeri "point library" (grade, kinetic type, edit grammar, camera,
composition, rhythm) speaks the same kind of surface: a small set of **semantic
axes**, each ``0..1``, that an agent or human sets with words instead of raw
numbers. This module is the one place that turns those words into axis values;
each library then derives its own low-level values from the resolved axes (its
mapping table), and *nothing* invents an axis-to-number mapping anywhere else.

This generalises :mod:`lumenframe.vector.params` — vector fixed seven motion
axes; here a library declares whatever axes its domain needs via an
:class:`AxisSpace`. The resolution order is identical and battle-tested::

    style baseline  →  feeling adjectives (±nudges)  →  explicit overrides

Unknown *feelings* are collected and surfaced, never fatal (one odd adjective
must not fail a brief). Unknown *override axes* raise — an explicit number aimed
at a nonexistent axis is a programming error, not taste.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


def clamp01(v: float) -> float:
    """Clamp to the closed unit interval ``[0, 1]``."""
    return min(max(float(v), 0.0), 1.0)


def lerp(a: float, b: float, t: float) -> float:
    """Linear interpolation ``a → b`` by ``t`` (t is *not* clamped here)."""
    return a + (b - a) * t


def remap(value: float, lo: float, hi: float) -> float:
    """Map a ``0..1`` axis value onto ``[lo, hi]``."""
    return lo + (hi - lo) * clamp01(value)


#: Shared bilingual feeling adjectives → axis nudges, keyed by axis NAME so a
#: library only feels a nudge for the axes it actually declares. The tool
#: surface is used from Chinese and English briefs alike, so both are first
#: class. A library extends this with its own domain feelings via
#: :meth:`AxisSpace.with_feelings`; unknown feelings are reported, not fatal.
BASE_FEELINGS: dict[str, dict[str, float]] = {
    # energy / drive
    "energetic": {"energy": +0.2},
    "dynamic": {"energy": +0.15},
    "bold": {"energy": +0.15, "drama": +0.1},
    "calm": {"energy": -0.2, "smoothness": +0.15},
    "gentle": {"energy": -0.15, "smoothness": +0.2},
    "subtle": {"energy": -0.15, "elegance": +0.1},
    "dramatic": {"drama": +0.2, "energy": +0.1},
    "快": {"energy": +0.2},
    "活力": {"energy": +0.2},
    "平静": {"energy": -0.2, "smoothness": +0.15},
    "克制": {"energy": -0.15, "elegance": +0.1},
    "戏剧": {"drama": +0.2, "energy": +0.1},
    # character
    "playful": {"playfulness": +0.2},
    "fun": {"playfulness": +0.2},
    "serious": {"playfulness": -0.2, "elegance": +0.1},
    "俏皮": {"playfulness": +0.2},
    "premium": {"elegance": +0.2, "energy": -0.1},
    "高级": {"elegance": +0.2, "energy": -0.1},
    "elegant": {"elegance": +0.2, "smoothness": +0.1},
    "优雅": {"elegance": +0.2, "smoothness": +0.1},
    "minimal": {"complexity": -0.2, "density": -0.15, "elegance": +0.1},
    "极简": {"complexity": -0.2, "density": -0.15, "elegance": +0.1},
    "rich": {"density": +0.15, "complexity": +0.15},
    "丰富": {"density": +0.15, "complexity": +0.15},
    "smooth": {"smoothness": +0.2},
    "顺滑": {"smoothness": +0.2},
    "cinematic": {"elegance": +0.15, "drama": +0.1, "energy": -0.05},
    "电影感": {"elegance": +0.15, "drama": +0.1, "energy": -0.05},
    "warm": {"warmth": +0.2},
    "暖": {"warmth": +0.2},
    "cool": {"warmth": -0.2},
    "冷": {"warmth": -0.2},
    "moody": {"drama": +0.15, "energy": -0.1},
    "氛围": {"drama": +0.15, "energy": -0.1},
    "clean": {"complexity": -0.15, "elegance": +0.1},
    "干净": {"complexity": -0.15, "elegance": +0.1},
}


@dataclass(frozen=True)
class ResolvedAxes:
    """Resolved semantic axes plus the hints/notes a library reads.

    Access is dict-like (``level["energy"]`` / ``level.axis("energy")``). A
    library derives its own low-level numbers from these values; this object
    carries no domain maths itself — that lives in each library, unit-tested as
    a table, exactly like :class:`lumenframe.vector.params.ResolvedParams`.
    """

    values: dict[str, float] = field(default_factory=dict)
    hints: dict[str, Any] = field(default_factory=dict)
    #: feelings that matched nothing (surfaced to the caller, never fatal).
    unknown_feelings: tuple[str, ...] = ()

    def __getitem__(self, axis: str) -> float:
        return self.values[axis]

    def axis(self, name: str, default: float = 0.5) -> float:
        """Axis value, or ``default`` if this space does not declare it."""
        return self.values.get(name, default)

    def to_dict(self) -> dict[str, Any]:
        return {
            "axes": dict(self.values),
            "hints": dict(self.hints),
            "unknown_feelings": list(self.unknown_feelings),
        }


@dataclass(frozen=True)
class AxisSpace:
    """A domain's declared axes, their neutral defaults, and its feeling table.

    ``defaults`` gives the neutral baseline used when no style is chosen.
    ``feelings`` is the merged adjective table (shared + domain). Build one with
    :func:`axis_space` and extend it with :meth:`with_feelings`.
    """

    axes: tuple[str, ...]
    defaults: dict[str, float] = field(default_factory=dict)
    feelings: dict[str, dict[str, float]] = field(default_factory=dict)

    def neutral(self) -> dict[str, float]:
        return {a: clamp01(self.defaults.get(a, 0.5)) for a in self.axes}

    def with_feelings(self, extra: Mapping[str, dict[str, float]]) -> "AxisSpace":
        """A copy whose feeling table is extended/overridden by ``extra``."""
        merged = {**self.feelings, **{k: dict(v) for k, v in extra.items()}}
        return AxisSpace(axes=self.axes, defaults=dict(self.defaults), feelings=merged)

    def resolve(
        self,
        *,
        baseline: Mapping[str, float] | None = None,
        feelings: list[str] | None = None,
        overrides: Mapping[str, float] | None = None,
        hints: Mapping[str, Any] | None = None,
    ) -> ResolvedAxes:
        """Resolve axes: neutral/style baseline → feeling nudges → overrides.

        * ``baseline`` — a style's baseline (subset of axes); missing axes fall
          back to the space's neutral defaults. Unknown baseline axes raise.
        * ``feelings`` — adjective words; each nudges declared axes. A nudge to
          an axis this space does not declare is silently ignored (a shared
          feeling like "warm" is a no-op for a space with no ``warmth`` axis).
          Words matching no feeling are collected in ``unknown_feelings``.
        * ``overrides`` — explicit absolute axis values (win). Unknown override
          axes raise.
        """
        base = self.neutral()
        for axis, value in (baseline or {}).items():
            if axis not in self.axes:
                raise ValueError(f"unknown baseline axis {axis!r} (space has {self.axes})")
            base[axis] = clamp01(value)

        unknown: list[str] = []
        for word in feelings or []:
            key = str(word).strip().lower()
            nudges = self.feelings.get(key) or self.feelings.get(key.rstrip("的感 "))
            if nudges is None:
                unknown.append(str(word))
                continue
            for axis, delta in nudges.items():
                if axis in base:  # ignore nudges to axes this space lacks
                    base[axis] = clamp01(base[axis] + delta)

        bad = set(overrides or {}) - set(self.axes)
        if bad:
            raise ValueError(f"unknown axes {sorted(bad)} (use {self.axes})")
        for axis, value in (overrides or {}).items():
            base[axis] = clamp01(value)

        return ResolvedAxes(values=base, hints=dict(hints or {}), unknown_feelings=tuple(unknown))


def axis_space(
    axes: tuple[str, ...],
    defaults: Mapping[str, float] | None = None,
    *,
    extra_feelings: Mapping[str, dict[str, float]] | None = None,
) -> AxisSpace:
    """Construct an :class:`AxisSpace` seeded with the shared feeling table.

    Only feelings that touch a declared axis stay useful; the rest are inert but
    harmless. Pass ``extra_feelings`` for domain adjectives (e.g. grade's
    ``teal``, camera's ``handheld``).
    """
    defaults = dict(defaults or {})
    bad = set(defaults) - set(axes)
    if bad:
        raise ValueError(f"defaults name undeclared axes {sorted(bad)}")
    feelings = {**BASE_FEELINGS, **{k: dict(v) for k, v in (extra_feelings or {}).items()}}
    return AxisSpace(axes=tuple(axes), defaults=defaults, feelings=feelings)
