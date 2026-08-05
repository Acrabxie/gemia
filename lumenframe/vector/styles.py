"""Visual style archetypes — named motion+visual token sets.

A style is the `grading.presets` idea applied to motion: one named object
that sets the semantic-parameter *baseline*, the easing character, and the
visual token kit (palette link, stroke weights, shape vocabulary, effects
hints). Styles never contain behaviour logic; they only bias the numbers the
rest of the engine reads.

Archetype names are trademark-safe; the brand-flavoured aliases agents
naturally reach for ("google-like", "apple-like") resolve to them.
"""
from __future__ import annotations

from typing import Any

from lumenframe.templates import theme
from lumenframe.vector.params import ResolvedParams, resolve

STYLES: dict[str, dict[str, Any]] = {
    # Bouncy, saturated, elastic — the "google-like" energy.
    "playful": {
        "summary": "elastic, colorful, energetic; overshoot and big staggers",
        "baseline": {
            "energy": 0.75, "smoothness": 0.55, "playfulness": 0.85,
            "elegance": 0.25, "complexity": 0.6, "density": 0.55, "organicness": 0.45,
        },
        "ease": {"enter": "swift", "exit": "exit", "move": "dramatic"},
        "palette": "paper",
        "hints": {
            "stroke_weight": 10.0, "line_cap": "round", "corner_radius": 0.35,
            "multi_hue": True, "particle_shape": "dot", "glow": False,
        },
    },
    # Restraint, precision, premium calm — the "apple-like" register.
    "minimal": {
        "summary": "minimal, elegant, smooth; few elements, generous holds",
        "baseline": {
            "energy": 0.35, "smoothness": 0.85, "playfulness": 0.1,
            "elegance": 0.85, "complexity": 0.25, "density": 0.2, "organicness": 0.3,
        },
        "ease": {"enter": "enter", "exit": "exit", "move": "move"},
        "palette": "noir",
        "hints": {
            "stroke_weight": 3.0, "line_cap": "round", "corner_radius": 0.12,
            "multi_hue": False, "particle_shape": "dot", "glow": False,
        },
    },
    # Slow, cinematic, exact — hairline gold on ink.
    "luxury": {
        "summary": "slow, precise, cinematic; long draw-ons, hairline strokes",
        "baseline": {
            "energy": 0.2, "smoothness": 0.8, "playfulness": 0.05,
            "elegance": 0.95, "complexity": 0.35, "density": 0.25, "organicness": 0.35,
        },
        "ease": {"enter": "soft", "exit": "exit", "move": "soft"},
        "palette": {
            "bg": "#0B0A08", "surface": "#171410", "text": "#F5EFE4",
            "subtext": "#A79C88", "accent": "#C9A96A", "accent_soft": "#E3CD9E",
            "grad": [[0.0, "#080705"], [1.0, "#1A150D"]],
        },
        "hints": {
            "stroke_weight": 1.6, "line_cap": "butt", "corner_radius": 0.0,
            "multi_hue": False, "particle_shape": "spark", "glow": False,
            "serif_text": True,
        },
    },
    # Fluid, organic, futuristic — glowing ribbons in deep space.
    "tech": {
        "summary": "fluid, futuristic, organic; continuous flow and glow",
        "baseline": {
            "energy": 0.6, "smoothness": 0.75, "playfulness": 0.3,
            "elegance": 0.6, "complexity": 0.55, "density": 0.45, "organicness": 0.75,
        },
        "ease": {"enter": "enter", "exit": "exit", "move": "move"},
        "palette": "lumeri",
        "hints": {
            "stroke_weight": 5.0, "line_cap": "round", "corner_radius": 0.5,
            "multi_hue": False, "particle_shape": "dot", "glow": True,
            "gradient_fill": True,
        },
    },
    # House style: tech tuned to the brand's light-ribbon language.
    "lumeri": {
        "summary": "ice-blue light ribbons on deep space — the house style",
        "baseline": {
            "energy": 0.55, "smoothness": 0.8, "playfulness": 0.25,
            "elegance": 0.7, "complexity": 0.5, "density": 0.4, "organicness": 0.7,
        },
        "ease": {"enter": "enter", "exit": "exit", "move": "move"},
        "palette": "lumeri",
        "hints": {
            "stroke_weight": 4.5, "line_cap": "round", "corner_radius": 0.5,
            "multi_hue": False, "particle_shape": "dot", "glow": True,
            "gradient_fill": True,
        },
    },
}

#: Brand-flavoured aliases → archetypes. Matching is lenient (lowercase,
#: hyphens/spaces stripped) so "Google-like" / "google like" both land.
ALIASES: dict[str, str] = {
    "googlelike": "playful",
    "google": "playful",
    "applelike": "minimal",
    "apple": "minimal",
    "premium": "luxury",
    "luxurybrand": "luxury",
    "techai": "tech",
    "aifluid": "tech",
    "house": "lumeri",
    "brand": "lumeri",
}

DEFAULT_STYLE = "lumeri"


class StyleError(ValueError):
    """Raised for an unknown style name."""


def style_names() -> list[str]:
    return sorted(STYLES)


def resolve_style_name(name: str | None) -> str:
    """Archetype for a name/alias; None → house default. Unknown → error
    (unlike palettes, silently restyling a *motion* character misleads)."""
    if name is None:
        return DEFAULT_STYLE
    key = str(name).strip().lower()
    if key in STYLES:
        return key
    folded = key.replace("-", "").replace("_", "").replace(" ", "")
    if folded in STYLES:
        return folded
    if folded in ALIASES:
        return ALIASES[folded]
    raise StyleError(f"unknown style {name!r} (use {style_names()} or aliases {sorted(ALIASES)})")


def style_spec(name: str | None) -> dict[str, Any]:
    return STYLES[resolve_style_name(name)]


def resolve_params(
    *,
    style: str | None = None,
    feelings: list[str] | None = None,
    overrides: dict[str, float] | None = None,
) -> ResolvedParams:
    """Style baseline → feelings → overrides, with the style's ease set/hints."""
    spec = style_spec(style)
    return resolve(
        baseline=spec["baseline"],
        feelings=feelings,
        overrides=overrides,
        ease_set=spec["ease"],
        hints={**spec["hints"], "style": resolve_style_name(style)},
    )


def style_palette(name: str | None, palette: str | dict[str, Any] | None = None) -> dict[str, Any]:
    """Palette roles for a style, optionally overridden by the brief.

    The brief's ``palette`` (a `templates.theme` name or partial role dict)
    wins over the style's own palette link.
    """
    if palette is not None:
        return theme.resolve_palette(palette)
    linked = style_spec(name)["palette"]
    return theme.resolve_palette(linked)


def describe_styles() -> str:
    """Compact agent-prompt block: one line per style + alias table."""
    lines = ["Vector motion styles:"]
    for name in style_names():
        spec = STYLES[name]
        lines.append(f"- {name}: {spec['summary']}")
    alias_pairs = ", ".join(f"{a}→{t}" for a, t in sorted(ALIASES.items()))
    lines.append(f"Aliases: {alias_pairs}")
    return "\n".join(lines)
