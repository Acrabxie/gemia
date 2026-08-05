"""Named, parameterized title animation presets.

Each preset is a pure data record: name, keyframe template, and tunable
numeric parameters.  The video render pipeline and the deck rasterizer both
consume presets through :func:`get_preset` — animation decisions stay in one
place; renderers only interpolate.

Keyframes are normalised to ``[0, 1]`` progress.  Each keyframe carries
``(progress, opacity, scale, y_offset_ratio, easing)``.  ``y_offset_ratio``
is a fraction of line height (positive = downward from anchor).  Easing names
follow CSS timing functions; renderers map them to their native curve.

Custom presets can be registered at runtime with :func:`register_preset`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence


@dataclass(frozen=True)
class Keyframe:
    progress: float
    opacity: float = 1.0
    scale: float = 1.0
    y_offset_ratio: float = 0.0
    easing: str = "ease-out"

    def to_dict(self) -> dict[str, Any]:
        return {
            "progress": self.progress,
            "opacity": self.opacity,
            "scale": self.scale,
            "y_offset_ratio": self.y_offset_ratio,
            "easing": self.easing,
        }


@dataclass(frozen=True)
class TitlePreset:
    name: str
    description: str
    enter: tuple[Keyframe, ...]
    hold: tuple[Keyframe, ...] = (Keyframe(progress=0.0), Keyframe(progress=1.0))
    exit: tuple[Keyframe, ...] = ()
    default_duration_ms: int = 600
    params: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "enter": [k.to_dict() for k in self.enter],
            "hold": [k.to_dict() for k in self.hold],
            "exit": [k.to_dict() for k in self.exit],
            "default_duration_ms": self.default_duration_ms,
            "params": dict(self.params),
        }


_REGISTRY: dict[str, TitlePreset] = {}


def register_preset(preset: TitlePreset) -> None:
    _REGISTRY[preset.name] = preset


def get_preset(name: str) -> TitlePreset:
    if name not in _REGISTRY:
        available = sorted(_REGISTRY)
        raise KeyError(
            f"unknown title preset {name!r}; available: {available!r}"
        )
    return _REGISTRY[name]


def list_presets() -> list[str]:
    return sorted(_REGISTRY)


def preset_catalog() -> list[dict[str, Any]]:
    return [_REGISTRY[name].to_dict() for name in sorted(_REGISTRY)]


# ── built-in presets ────────────────────────────────────────────────

register_preset(TitlePreset(
    name="fade_in",
    description="Opacity ramp from transparent to opaque.",
    enter=(
        Keyframe(progress=0.0, opacity=0.0),
        Keyframe(progress=1.0, opacity=1.0, easing="ease-out"),
    ),
    exit=(
        Keyframe(progress=0.0, opacity=1.0),
        Keyframe(progress=1.0, opacity=0.0, easing="ease-in"),
    ),
    default_duration_ms=500,
))

register_preset(TitlePreset(
    name="slide_up",
    description="Title slides up from below baseline into position.",
    enter=(
        Keyframe(progress=0.0, opacity=0.0, y_offset_ratio=0.8),
        Keyframe(progress=1.0, opacity=1.0, y_offset_ratio=0.0, easing="ease-out"),
    ),
    exit=(
        Keyframe(progress=0.0, opacity=1.0, y_offset_ratio=0.0),
        Keyframe(progress=1.0, opacity=0.0, y_offset_ratio=-0.5, easing="ease-in"),
    ),
    default_duration_ms=600,
))

register_preset(TitlePreset(
    name="scale_pop",
    description="Title pops in with a slight overshoot bounce.",
    enter=(
        Keyframe(progress=0.0, opacity=0.0, scale=0.6),
        Keyframe(progress=0.65, opacity=1.0, scale=1.08, easing="ease-out"),
        Keyframe(progress=1.0, opacity=1.0, scale=1.0, easing="ease-in-out"),
    ),
    exit=(
        Keyframe(progress=0.0, opacity=1.0, scale=1.0),
        Keyframe(progress=1.0, opacity=0.0, scale=0.85, easing="ease-in"),
    ),
    default_duration_ms=500,
))

register_preset(TitlePreset(
    name="typewriter",
    description="Characters reveal left-to-right with a cursor feel.",
    enter=(
        Keyframe(progress=0.0, opacity=0.0),
        Keyframe(progress=1.0, opacity=1.0, easing="linear"),
    ),
    default_duration_ms=800,
    params={"per_char": True, "char_stagger_ms": 45},
))

register_preset(TitlePreset(
    name="quiet_hold",
    description="No entrance animation; title is simply present.",
    enter=(
        Keyframe(progress=0.0, opacity=1.0),
        Keyframe(progress=1.0, opacity=1.0),
    ),
    default_duration_ms=0,
))

register_preset(TitlePreset(
    name="accent_wipe",
    description="A coloured accent bar wipes across, revealing the title behind it.",
    enter=(
        Keyframe(progress=0.0, opacity=0.0, scale=1.0),
        Keyframe(progress=0.4, opacity=1.0, scale=1.0, easing="ease-out"),
        Keyframe(progress=1.0, opacity=1.0, scale=1.0),
    ),
    exit=(
        Keyframe(progress=0.0, opacity=1.0),
        Keyframe(progress=1.0, opacity=0.0, easing="ease-in"),
    ),
    default_duration_ms=700,
    params={"wipe_direction": "left_to_right", "accent_color": "brand"},
))


__all__ = [
    "Keyframe",
    "TitlePreset",
    "get_preset",
    "list_presets",
    "preset_catalog",
    "register_preset",
]
