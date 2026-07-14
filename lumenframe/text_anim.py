"""Text animation presets — CapCut-style title moves expressed as keyframes.

This module is pure *sugar* over the existing keyframe machinery: each preset
turns into a list of plain ``set_keyframe`` op dicts on the standard
``transform`` / ``opacity`` properties, so it rides the exact same resolver /
render path as a hand-authored animation. There is **no** resolver change and no
new property: a preset only ever writes ``opacity`` and ``transform.*`` tracks.

The single public entry point is :func:`text_anim_ops`, which returns the op
dicts for a preset; :func:`apply_layer` in :mod:`lumenframe.ops` dispatches the
``animate_text`` op through here and feeds the resulting ops back into the normal
``set_keyframe`` handler. Keeping the preset math here (rather than inline in
``ops.py``) lets third parties reuse / introspect the curves directly.

Presets (all transform/opacity only):

* ``fade_in_words`` — opacity 0 -> 1 with a tiny upward settle; the "words"
  framing is conveyed by a soft, slightly staggered ease (a single text layer
  has no per-word sub-layers, so we animate the block's opacity + rise).
* ``pop``           — a scale punch 0 -> ~1.2 -> 1 (overshoot then settle),
  the classic CapCut "pop" title bounce, plus an opacity fade-in.
* ``wave``          — a gentle vertical sine settle: the title drops in with a
  couple of overshooting y keyframes so it appears to "wave" into place.
* ``rise``          — slides up from below while fading in (translate + opacity).

Times are **absolute** (global document time), matching ``animate_layer``.
"""
from __future__ import annotations

from typing import Any

#: The presets this module understands (kept here so callers / catalog can list).
TEXT_ANIM_PRESETS: tuple[str, ...] = ("fade_in_words", "pop", "wave", "rise")

#: Default punch overshoot for ``pop`` (1.2 == 120% before settling to 100%).
POP_OVERSHOOT: float = 1.2

#: Default rise/wave travel in pixels (how far below the title starts).
RISE_TRAVEL: float = 80.0


class TextAnimError(ValueError):
    """Raised for an unknown preset or a non-positive duration."""


def _kf(layer_id: str, prop: str, t: float, value: float, interp: str = "linear") -> dict[str, Any]:
    """Build one ``set_keyframe`` op dict (absolute time, seconds)."""
    return {
        "op": "set_keyframe",
        "layer_id": str(layer_id),
        "property": str(prop),
        "t": round(float(t), 6),
        "value": float(value),
        "interp": str(interp),
    }


def text_anim_ops(
    layer_id: str,
    preset: str,
    *,
    layer_start: float,
    layer_duration: float,
    duration: float = 0.5,
    easing: str = "linear",
) -> list[dict[str, Any]]:
    """Expand a text-animation ``preset`` into ``set_keyframe`` op dicts.

    Args:
        layer_id: target text layer id.
        preset: one of :data:`TEXT_ANIM_PRESETS`.
        layer_start / layer_duration: the layer's timeline placement (seconds),
            used to anchor the animation window and to clamp ``duration``.
        duration: animation length in seconds (clamped to the layer duration).
        easing: keyframe interp for the in-move (default ``linear``; also
            ``ease``/``ease_in``/``ease_out``). The settle/overshoot keyframes
            always land ``linear`` so the overshoot reads cleanly.

    Returns:
        A list of op dicts targeting only ``opacity`` and ``transform.*`` — never
        a custom property — so the standard resolver renders them unchanged.

    Raises:
        TextAnimError: unknown preset or non-positive duration.
    """
    if preset not in TEXT_ANIM_PRESETS:
        raise TextAnimError(
            f"animate_text: unknown preset {preset!r} (use {', '.join(TEXT_ANIM_PRESETS)})"
        )
    dur = float(duration)
    if dur <= 0:
        raise TextAnimError("animate_text: duration must be > 0")
    # Clamp the animation window to the layer's own span.
    if layer_duration > 0:
        dur = min(dur, float(layer_duration))

    t0 = float(layer_start)
    t1 = t0 + dur
    interp = easing if easing in {"linear", "ease", "ease_in", "ease_out"} else "ease_out"

    if preset == "fade_in_words":
        # Block fades in while settling a few pixels upward — a soft "words"
        # reveal. opacity 0 -> 1; transform.y travels from +rise/4 to 0.
        rise = RISE_TRAVEL / 4.0
        return [
            _kf(layer_id, "opacity", t0, 0.0, interp),
            _kf(layer_id, "opacity", t1, 1.0, interp),
            _kf(layer_id, "transform.y", t0, rise, interp),
            _kf(layer_id, "transform.y", t1, 0.0, interp),
        ]

    if preset == "pop":
        # Scale punch: 0 -> overshoot -> 1. The overshoot lands at the midpoint
        # so the title visibly grows past 100% then settles back. opacity rides
        # in over the first half so the punch is visible.
        mid = round((t0 + t1) / 2.0, 6)
        ops: list[dict[str, Any]] = []
        for prop in ("transform.scale_x", "transform.scale_y"):
            ops.append(_kf(layer_id, prop, t0, 0.0, interp))
            ops.append(_kf(layer_id, prop, mid, POP_OVERSHOOT, "linear"))
            ops.append(_kf(layer_id, prop, t1, 1.0, "linear"))
        ops.append(_kf(layer_id, "opacity", t0, 0.0, interp))
        ops.append(_kf(layer_id, "opacity", mid, 1.0, interp))
        return ops

    if preset == "wave":
        # Drop in with a damped vertical overshoot: start below, overshoot above
        # zero, then settle. Three transform.y keyframes + an opacity fade-in.
        third = round(t0 + dur / 3.0, 6)
        two_third = round(t0 + 2.0 * dur / 3.0, 6)
        return [
            _kf(layer_id, "transform.y", t0, RISE_TRAVEL, interp),
            _kf(layer_id, "transform.y", third, -RISE_TRAVEL * 0.25, "linear"),
            _kf(layer_id, "transform.y", two_third, RISE_TRAVEL * 0.1, "linear"),
            _kf(layer_id, "transform.y", t1, 0.0, "linear"),
            _kf(layer_id, "opacity", t0, 0.0, interp),
            _kf(layer_id, "opacity", t1, 1.0, interp),
        ]

    # preset == "rise"
    # Slide up from below while fading in.
    return [
        _kf(layer_id, "transform.y", t0, RISE_TRAVEL, interp),
        _kf(layer_id, "transform.y", t1, 0.0, interp),
        _kf(layer_id, "opacity", t0, 0.0, interp),
        _kf(layer_id, "opacity", t1, 1.0, interp),
    ]
