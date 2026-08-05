"""Motion toolkit — ease tokens, duration bands, and track builders.

This is the vocabulary behaviours are written in. Nothing here decides *what*
should move (that's a behaviour) or *when* (that's choreography) — only *how
motion is spelled*: named easing curves, duration bands that scale with tempo,
and small pure helpers that write well-formed keyframes onto scene nodes.

Ease tokens
-----------
Aligned with the Lumeri design-manual motion layer (DESIGN.md `motion.*`):

* ``enter``    — decelerate into place, cubic-bezier(0, 0, 0.2, 1)
* ``exit``     — accelerate away,       cubic-bezier(0.4, 0, 1, 1)
* ``move``     — standard travel,       cubic-bezier(0.4, 0, 0.2, 1)
* ``dramatic`` — overshoot/elastic,     cubic-bezier(0.34, 1.56, 0.64, 1) 慎用
* ``linear`` / ``hold`` — no curve / step
* ``soft``     — gentle symmetric ease, cubic-bezier(0.45, 0, 0.55, 1)
* ``swift``    — energetic snap,        cubic-bezier(0.6, 0, 0.1, 1)
* any literal ``bezier(x1,y1,x2,y2)`` string

Tokens resolve to CSS timing-function strings for the SVG compiler and to
sampled scalar curves (:func:`ease_value`) for renderers with no CSS engine.
"""
from __future__ import annotations

import math
import re
from typing import Any, Callable

from lumenframe.vector import scene as vscene

#: token -> cubic-bezier control points (None == special-cased).
EASE_TOKENS: dict[str, tuple[float, float, float, float] | None] = {
    "linear": None,
    "hold": None,
    "enter": (0.0, 0.0, 0.2, 1.0),
    "exit": (0.4, 0.0, 1.0, 1.0),
    "move": (0.4, 0.0, 0.2, 1.0),
    "dramatic": (0.34, 1.56, 0.64, 1.0),
    "soft": (0.45, 0.0, 0.55, 1.0),
    "swift": (0.6, 0.0, 0.1, 1.0),
}

_BEZIER_RE = re.compile(
    r"^bezier\(\s*(-?[\d.]+)\s*,\s*(-?[\d.]+)\s*,\s*(-?[\d.]+)\s*,\s*(-?[\d.]+)\s*\)$"
)

#: Ease tokens whose y stays within [0, 1] — safe for progress-style
#: properties (stroke draw-on, opacity) where an overshoot below 0 or above 1
#: would clip the value and flash an artefact (e.g. a draw-on gap at the seam).
MONOTONIC_EASES: frozenset[str] = frozenset({
    "linear", "enter", "exit", "move", "soft", "swift", "hold",
})


def safe_progress_ease(token: str) -> str:
    """Map an overshoot ease to the nearest monotonic one for 0..1 progress.

    ``dramatic`` (and any custom bezier whose control y leaves [0, 1]) would
    make a draw-on / opacity value overshoot and clip; this returns ``move``
    in that case so the gesture keeps the style's energy without the artefact.
    """
    token = str(token)
    if token in MONOTONIC_EASES:
        return token
    pts = ease_control_points(token)
    if pts is None:
        return token
    _, y1, _, y2 = pts
    if -1e-6 <= y1 <= 1.0 + 1e-6 and -1e-6 <= y2 <= 1.0 + 1e-6:
        return token
    return "move"


class MotionError(ValueError):
    """Raised for unknown ease tokens or malformed durations."""


def ease_control_points(token: str) -> tuple[float, float, float, float] | None:
    """Control points for an ease token (None for linear/hold)."""
    token = str(token)
    if token in EASE_TOKENS:
        return EASE_TOKENS[token]
    match = _BEZIER_RE.match(token)
    if match:
        x1, y1, x2, y2 = (float(g) for g in match.groups())
        # CSS requires x coordinates within [0,1]; y may overshoot.
        x1, x2 = min(max(x1, 0.0), 1.0), min(max(x2, 0.0), 1.0)
        return (x1, y1, x2, y2)
    raise MotionError(
        f"unknown ease token {token!r} (use {sorted(EASE_TOKENS)} or 'bezier(x1,y1,x2,y2)')"
    )


def ease_to_css(token: str) -> str:
    """CSS ``animation-timing-function`` value for a token."""
    pts = ease_control_points(token)
    if pts is None:
        return "step-end" if token == "hold" else "linear"
    return "cubic-bezier({}, {}, {}, {})".format(*(_trim(v) for v in pts))


def _trim(v: float) -> str:
    s = f"{v:.4f}".rstrip("0").rstrip(".")
    return s or "0"


def ease_value(token: str, t: float) -> float:
    """Evaluate an ease token at normalised time ``t`` ∈ [0,1].

    CSS-style cubic-bezier: solve x(u) = t for u, return y(u). Used by
    non-CSS renderers and by behaviours that bake curves into positions.
    """
    t = min(max(float(t), 0.0), 1.0)
    pts = ease_control_points(token)
    if pts is None:
        if token == "hold":
            return 0.0 if t < 1.0 else 1.0
        return t
    x1, y1, x2, y2 = pts

    def bez(u: float, a: float, b: float) -> float:
        # Bernstein form with P0=0, P3=1.
        return 3 * (1 - u) ** 2 * u * a + 3 * (1 - u) * u ** 2 * b + u ** 3

    # Newton iterations on x(u) = t, falling back to bisection.
    u = t
    for _ in range(8):
        x = bez(u, x1, x2) - t
        if abs(x) < 1e-6:
            break
        dx = (
            3 * (1 - u) ** 2 * x1
            + 6 * (1 - u) * u * (x2 - x1)
            + 3 * u ** 2 * (1 - x2)
        )
        if abs(dx) < 1e-9:
            break
        u -= x / dx
        u = min(max(u, 0.0), 1.0)
    return bez(u, y1, y2)


# ── duration bands ───────────────────────────────────────────────────────
#
# Values follow the design-manual bands (text-enter 0.3/0.4/0.8 etc.) and are
# scaled by a tempo multiplier at request time; behaviours ask for a band and
# a tempo, never a literal duration.

DURATION_BANDS: dict[str, dict[str, float]] = {
    "enter": {"min": 0.3, "rec": 0.45, "max": 0.9},
    "exit": {"min": 0.2, "rec": 0.3, "max": 0.6},
    "emphasis": {"min": 0.25, "rec": 0.4, "max": 0.8},
    "draw": {"min": 0.6, "rec": 1.1, "max": 2.4},
    "morph": {"min": 0.5, "rec": 0.9, "max": 1.8},
    "cycle": {"min": 1.6, "rec": 2.8, "max": 6.0},
    "burst": {"min": 0.35, "rec": 0.6, "max": 1.2},
}


def band_duration(band: str, *, tempo: float = 1.0, available: float | None = None) -> float:
    """Concrete duration for a band at a tempo, clamped to band + window.

    ``tempo`` > 1 means *slower* (durations stretch — a luxury pace),
    ``tempo`` < 1 means snappier. ``available`` caps the result to the
    behaviour's window so motion never spills outside its slot.
    """
    spec = DURATION_BANDS.get(str(band))
    if spec is None:
        raise MotionError(f"unknown duration band {band!r} (use {sorted(DURATION_BANDS)})")
    d = spec["rec"] * max(float(tempo), 0.05)
    d = min(max(d, spec["min"]), spec["max"])
    if available is not None:
        d = min(d, max(float(available), 0.05))
    return round(d, 6)


# ── track builders ───────────────────────────────────────────────────────
#
# Each helper writes keyframes onto a node (or particle instance — the shape
# is identical) via scene.add_track and returns the node for chaining. All
# times are absolute scene seconds; callers pass window-derived times.

Node = dict[str, Any]


def fade(node: Node, t0: float, t1: float, *, start: float = 0.0, end: float = 1.0, ease: str = "enter") -> Node:
    vscene.add_track(node, "opacity", [vscene.kf(t0, start, ease), vscene.kf(t1, end)])
    return node


def move_by(
    node: Node, t0: float, t1: float, *, dx: float = 0.0, dy: float = 0.0,
    ease: str = "enter", arrive: bool = True,
) -> Node:
    """Travel by (dx, dy): ``arrive`` animates offset→0 (settle into place),
    otherwise 0→offset (depart)."""
    x0, x1 = (dx, 0.0) if arrive else (0.0, dx)
    y0, y1 = (dy, 0.0) if arrive else (0.0, dy)
    if abs(dx) > 1e-9:
        vscene.add_track(node, "x", [vscene.kf(t0, x0, ease), vscene.kf(t1, x1)])
    if abs(dy) > 1e-9:
        vscene.add_track(node, "y", [vscene.kf(t0, y0, ease), vscene.kf(t1, y1)])
    return node


def scale_between(
    node: Node, t0: float, t1: float, *, start: float, end: float, ease: str = "enter",
) -> Node:
    vscene.add_track(node, "scale", [vscene.kf(t0, start, ease), vscene.kf(t1, end)])
    return node


def scale_pop(
    node: Node, t0: float, t1: float, *, overshoot: float = 0.15, ease: str = "swift",
) -> Node:
    """0 → (1+overshoot) → 1: the classic pop. Overshoot lands at ~62%."""
    mid = t0 + (t1 - t0) * 0.62
    vscene.add_track(node, "scale", [
        vscene.kf(t0, 0.0, ease),
        vscene.kf(mid, 1.0 + max(0.0, overshoot), "soft"),
        vscene.kf(t1, 1.0),
    ])
    return node


def rotate_between(
    node: Node, t0: float, t1: float, *, start: float, end: float, ease: str = "move",
) -> Node:
    vscene.add_track(node, "rotation", [vscene.kf(t0, start, ease), vscene.kf(t1, end)])
    return node


def draw_on(node: Node, t0: float, t1: float, *, ease: str = "move", reverse: bool = False) -> Node:
    """Stroke draw-on: ``draw`` 0→1 (or 1→0 when ``reverse``)."""
    a, b = (1.0, 0.0) if reverse else (0.0, 1.0)
    vscene.add_track(node, "draw", [vscene.kf(t0, a, ease), vscene.kf(t1, b)])
    return node


def morph_to(node: Node, t0: float, t1: float, target_path: list, *, ease: str = "move") -> Node:
    """Morph the node's path to ``target_path``, resampling both to one shared
    cubic-segment count so the ``d`` track interpolates command-for-command
    (CSS ``d: path()`` requires identical command structure — otherwise the
    morph is a non-interpolable snap)."""
    if node.get("kind") != "path":
        raise MotionError("morph_to targets path nodes only")
    from lumenframe.vector import geometry
    home, dest = geometry.align_for_morph(
        [tuple(s) for s in node["path"]],
        [tuple(s) for s in target_path],
    )
    vscene.add_track(node, "d", [vscene.kf(t0, home, ease), vscene.kf(t1, dest)])
    return node


def color_shift(node: Node, t0: float, t1: float, *, prop: str = "fill", start: str, end: str, ease: str = "soft") -> Node:
    if prop not in ("fill", "stroke"):
        raise MotionError("color_shift prop must be fill|stroke")
    vscene.add_track(node, prop, [vscene.kf(t0, str(start), ease), vscene.kf(t1, str(end))])
    return node


def hold_then(node: Node, prop: str, t: float, value: Any) -> Node:
    """Pin ``prop`` at ``value`` from scene start until ``t`` (a step)."""
    vscene.add_track(node, prop, [vscene.kf(0.0, value, "hold"), vscene.kf(t, value)])
    return node


def oscillate(
    node: Node, prop: str, t0: float, t1: float, *,
    center: float, amplitude: float, cycles: float, ease: str = "soft",
    decay: float = 0.0, phase: float = 0.0, samples_per_cycle: int = 8,
) -> Node:
    """Baked damped sine, sampled with a true continuous phase offset.

    The curve is ``center + amplitude·sin(2π(cycles·u + phase))·decay^(...)``
    over ``u`` ∈ [0,1], sampled at ``samples_per_cycle`` points per cycle so a
    non-zero ``phase`` genuinely shifts the waveform (members with different
    phases undulate out of step — a travelling wave), instead of only flipping
    the first extremum's direction. Starts and ends anchored at ``center`` so
    the gesture is loop-safe. ``decay`` ∈ [0,1] shrinks amplitude per cycle.

    CSS gets explicit keyframes, so every renderer that interpolates scalars
    reproduces it identically. The keyframe eases are ``linear`` between dense
    samples (the sine shape lives in the samples, not the timing function).
    """
    if cycles <= 0:
        raise MotionError("oscillate needs cycles > 0")
    n = max(2, int(round(cycles * max(2, samples_per_cycle))))
    span = t1 - t0
    d = max(0.0, min(decay, 1.0))
    points = [vscene.kf(t0, center, ease)]
    for i in range(1, n):
        u = i / n
        env = (1.0 - d) ** (cycles * u) if d > 0 else 1.0
        # Taper the very start/end to the center so the track is anchored even
        # when phase != 0 would otherwise start mid-swing.
        edge = min(1.0, u / 0.12, (1.0 - u) / 0.12)
        val = center + amplitude * env * edge * math.sin(2.0 * math.pi * (cycles * u + phase))
        points.append(vscene.kf(t0 + span * u, round(val, 4), "linear"))
    points.append(vscene.kf(t1, center))
    vscene.add_track(node, prop, points)
    return node


#: Signature every track builder shares — imported by behaviours for typing.
TrackBuilder = Callable[..., Node]
