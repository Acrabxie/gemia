"""Shared deterministic text primitives for Lumeri product surfaces."""

from gemia.text.layout import (
    TextLayoutError,
    TextLayoutResult,
    TextMetrics,
    autofit_text,
    break_lines,
    measure_text,
    wrap_text,
)
from gemia.text.title_presets import (
    Keyframe,
    TitlePreset,
    get_preset,
    list_presets,
    preset_catalog,
    register_preset,
)

__all__ = [
    "Keyframe",
    "TextLayoutError",
    "TextLayoutResult",
    "TextMetrics",
    "TitlePreset",
    "autofit_text",
    "break_lines",
    "get_preset",
    "list_presets",
    "measure_text",
    "preset_catalog",
    "register_preset",
    "wrap_text",
]
