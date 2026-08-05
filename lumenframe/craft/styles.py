"""Style archetypes — named parameter presets a single word reshapes everything with.

A **style** is one named object that sets the axis *baseline* (and optional
domain hints); choosing it re-tunes the whole result. This generalises
:mod:`lumenframe.vector.styles`: there a style also carried motion easing; here
a style is domain-agnostic (baseline + hints + summary), and each library adds
whatever hint keys it needs (grade: film grain; kinetic: type scale; …).

A :class:`StyleBook` binds a set of styles to an :class:`~lumenframe.craft.params.AxisSpace`
and resolves ``style + feelings + overrides`` into :class:`ResolvedAxes`.
Archetype names are trademark-safe; brand-flavoured aliases agents reach for
("google-like", "apple-like", "kodak-like") resolve to them. An unknown *style*
raises (silently restyling misleads), unlike an unknown feeling.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from lumenframe.craft.params import AxisSpace, ResolvedAxes


class StyleError(ValueError):
    """Raised for an unknown style name."""


@dataclass(frozen=True)
class Style:
    name: str
    summary: str
    baseline: dict[str, float] = field(default_factory=dict)
    hints: dict[str, Any] = field(default_factory=dict)


def _fold(name: str) -> str:
    return str(name).strip().lower().replace("-", "").replace("_", "").replace(" ", "")


@dataclass
class StyleBook:
    """A named collection of styles over one axis space, with alias resolution.

    ``default`` is returned for ``resolve_name(None)`` — the house style. Add
    styles at construction or with :meth:`add`; register aliases with
    :meth:`alias`.
    """

    space: AxisSpace
    default: str
    styles: dict[str, Style] = field(default_factory=dict)
    aliases: dict[str, str] = field(default_factory=dict)

    def add(self, name: str, summary: str, baseline: Mapping[str, float],
            hints: Mapping[str, Any] | None = None) -> "StyleBook":
        bad = set(baseline) - set(self.space.axes)
        if bad:
            raise ValueError(f"style {name!r} names undeclared axes {sorted(bad)}")
        self.styles[name] = Style(name=name, summary=summary,
                                  baseline=dict(baseline), hints=dict(hints or {}))
        return self

    def alias(self, alias: str, target: str) -> "StyleBook":
        if target not in self.styles:
            raise ValueError(f"alias {alias!r} → unknown style {target!r}")
        self.aliases[_fold(alias)] = target
        return self

    def names(self) -> list[str]:
        return sorted(self.styles)

    def resolve_name(self, name: str | None) -> str:
        if name is None:
            return self.default
        key = str(name).strip().lower()
        if key in self.styles:
            return key
        folded = _fold(name)
        if folded in self.styles:
            return folded
        if folded in self.aliases:
            return self.aliases[folded]
        raise StyleError(
            f"unknown style {name!r} (use {self.names()} or aliases {sorted(self.aliases)})"
        )

    def spec(self, name: str | None) -> Style:
        return self.styles[self.resolve_name(name)]

    def resolve_params(
        self,
        *,
        style: str | None = None,
        feelings: list[str] | None = None,
        overrides: Mapping[str, float] | None = None,
        extra_hints: Mapping[str, Any] | None = None,
    ) -> ResolvedAxes:
        """Style baseline → feelings → overrides, carrying the style's hints."""
        spec = self.spec(style)
        hints = {**spec.hints, **(extra_hints or {}), "style": spec.name}
        return self.space.resolve(
            baseline=spec.baseline, feelings=feelings, overrides=overrides, hints=hints,
        )

    def describe(self, label: str) -> str:
        """Compact prompt block: one line per style + the alias table."""
        lines = [f"{label} styles:"]
        for name in self.names():
            lines.append(f"- {name}: {self.styles[name].summary}")
        if self.aliases:
            lines.append("Aliases: " + ", ".join(
                f"{a}→{t}" for a, t in sorted(self.aliases.items())))
        return "\n".join(lines)

    def catalog(self) -> dict[str, Any]:
        return {
            "styles": {n: self.styles[n].summary for n in self.names()},
            "aliases": dict(sorted(self.aliases.items())),
            "default": self.default,
        }
