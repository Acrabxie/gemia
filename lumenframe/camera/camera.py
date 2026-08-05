"""The taste floor — easing curves, the frame-safe geometry, and the moves.

This is where amateur camera work is made structurally hard. Four rules are
enforced here, not left to the caller:

1. **Ease in and out.** Every main-move segment carries a *named* easing from
   :data:`EASINGS`, and none of them is linear — a camera that ramps linearly
   reads as a robot. The move functions only ever emit eased segments.
2. **Motivated.** A push/settle translates *toward the subject's focal point*
   (:func:`motivated_translate`), by as much as the frame budget allows.
3. **Keep the subject in frame.** No move or handheld wobble may reveal a canvas
   edge. :func:`covers_frame` is the exact test (all four viewport corners must
   fall inside the transformed still), and every scale/translate is sized from
   the per-scale :func:`frame_budget` so the guarantee holds by construction.
4. **Subtlety ceiling + organic handheld.** The push scale delta is capped
   (:class:`MotionProfile`); handheld is a *sum of a few low-frequency sines*
   with seeded phase/amplitude (:func:`handheld_channels`), never white noise,
   and its total amplitude is reserved out of the frame budget so it can never
   push the subject off-screen.

A :class:`MotionProfile` turns the resolved axes into these concrete numbers
once per build; the registered move functions read the profile and lay down a
small set of base keyframes. All randomness flows from a single seeded RNG.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable

from lumenframe.craft import Registry, clamp01, lerp, remap

# ── easing curves ───────────────────────────────────────────────────────────
#
# CSS-order cubic-bezier control points ``(x1, y1, x2, y2)``. Every curve here
# eases in and/or out — NONE is the linear diagonal ``(0,0,1,1)``. The move
# functions pick a name from this table; the frame preview compiles the same
# name to a CSS ``cubic-bezier(...)`` timing function, so the "feel" the plan
# names is exactly the feel the preview plays.

EASINGS: dict[str, tuple[float, float, float, float]] = {
    # slow-in / slow-out S-curve — the default motivated move.
    "ease_in_out": (0.42, 0.0, 0.58, 1.0),
    # a gentler, longer-tailed S — cinematic / epic slowness.
    "ease_in_out_soft": (0.5, 0.0, 0.16, 1.0),
    # decelerate onto rest — the "settle" landing of a push.
    "settle": (0.18, 0.72, 0.3, 1.0),
    # a clean ease-out for reveals pulling back to context.
    "ease_out": (0.16, 0.78, 0.42, 1.0),
    # quick, still eased — energetic punch (no linear ramp).
    "punch": (0.4, 0.0, 0.14, 1.0),
    # a very fast middle for a whip pan, easing hard at both ends.
    "whip": (0.7, 0.0, 0.28, 1.0),
}

#: Curves that count as a legitimate *main move* easing (i.e. not a hold). The
#: taste floor forbids linear here; this set is asserted against in the tests.
MAIN_EASE_NAMES: frozenset[str] = frozenset(EASINGS)


def cubic_bezier(name: str, t: float) -> float:
    """Evaluate a named CSS easing at input time ``t`` ∈ ``[0, 1]``.

    Solves the bezier's parametric ``x(s) = t`` for ``s`` (Newton with a
    bisection fallback), then returns ``y(s)`` — the standard CSS timing-function
    semantics. Deterministic and dependency-free.
    """
    x1, y1, x2, y2 = EASINGS[name]
    t = min(max(t, 0.0), 1.0)

    def _bez(a1: float, a2: float, s: float) -> float:
        # bezier with fixed endpoints 0 and 1 → 3(1-s)^2 s a1 + 3(1-s) s^2 a2 + s^3
        u = 1.0 - s
        return 3 * u * u * s * a1 + 3 * u * s * s * a2 + s * s * s

    # find s with x(s) == t
    s = t
    for _ in range(8):  # Newton
        x = _bez(x1, x2, s) - t
        if abs(x) < 1e-6:
            break
        dx = (3 * (1 - s) ** 2 * x1 + 6 * (1 - s) * s * (x2 - x1)
              + 3 * s * s * (1 - x2))
        if abs(dx) < 1e-9:
            break
        s -= x / dx
    if not (0.0 <= s <= 1.0):  # bisection fallback
        lo, hi = 0.0, 1.0
        for _ in range(40):
            s = 0.5 * (lo + hi)
            if _bez(x1, x2, s) < t:
                lo = s
            else:
                hi = s
    return _bez(y1, y2, s)


# ── frame-safe geometry ─────────────────────────────────────────────────────
#
# Coordinate convention matches the templates: the still fills the canvas, the
# transform origin is dead-centre, ``x`` grows right, ``y`` grows down, and
# translate is in pixels. A subject focal point is ``(x, y)`` in ``0..1``.


def frame_budget(scale: float, dim: float) -> float:
    """Max centre-translate (px) along an axis of length ``dim`` at ``scale``.

    At ``scale`` the still is ``scale``× larger than the frame, so it can slide
    by ``(scale-1)/2 * dim`` before an edge of the still reaches the edge of the
    frame. Below ``scale == 1`` there is no budget (and an edge would show).
    """
    return max(0.0, (scale - 1.0) * 0.5 * dim)


def motivated_translate(fx: float, fy: float, scale: float, w: float, h: float,
                        *, bias: float, reserve_x: float, reserve_y: float) -> tuple[float, float]:
    """Translate that pushes the framing *toward* the focal subject.

    To centre a focal point ``(fx, fy)`` at ``scale`` you would translate by
    ``(0.5-fx)·w·scale`` — but that usually reveals an edge, so we clamp to the
    frame budget *minus a reserve* held back for the handheld wobble. ``bias``
    ∈ ``[0,1]`` scales how far along that motivated vector this keyframe sits (a
    push starts near 0 and lands near 1). The result is always frame-safe.
    """
    want_x = (0.5 - fx) * w * scale
    want_y = (0.5 - fy) * h * scale
    lim_x = max(0.0, frame_budget(scale, w) - reserve_x)
    lim_y = max(0.0, frame_budget(scale, h) - reserve_y)
    tx = max(-lim_x, min(lim_x, want_x * bias))
    ty = max(-lim_y, min(lim_y, want_y * bias))
    return tx, ty


def covers_frame(scale: float, tx: float, ty: float, rot_deg: float,
                 w: float, h: float, *, eps: float = 0.75) -> bool:
    """True iff the transformed still still covers the whole viewport.

    Exact test: map each of the four viewport corners back into the still's
    local space (inverse of ``rotate·scale·translate`` about centre) and require
    it to land inside the still's ``w×h`` rectangle. ``eps`` (≈ sub-pixel)
    absorbs float noise so a corner sitting exactly on the edge counts as inside.
    """
    if scale < 1.0:
        return False
    theta = math.radians(rot_deg)
    cos, sin = math.cos(-theta), math.sin(-theta)
    hw, hh = w * 0.5, h * 0.5
    for cx in (-hw, hw):
        for cy in (-hh, hh):
            px, py = cx - tx, cy - ty          # undo translate
            rx = (px * cos - py * sin) / scale  # undo rotate + scale
            ry = (px * sin + py * cos) / scale
            if abs(rx) > hw + eps or abs(ry) > hh + eps:
                return False
    return True


def fit_to_frame(scale: float, tx: float, ty: float, rot_deg: float,
                 w: float, h: float, *, eps: float = 0.75) -> tuple[float, float]:
    """Shrink a translate toward centre until the still covers the viewport.

    A deterministic belt-and-braces guarantee: if ``(tx, ty)`` already covers
    the frame it is returned unchanged; otherwise the largest ``k`` ∈ ``[0,1]``
    with ``k·(tx,ty)`` frame-safe is found by bisection. Because the scale floor
    reserves room for rotation, ``k = 0`` always covers, so this never fails.

    ``eps`` is forwarded to :func:`covers_frame`. Callers that will *round* the
    fitted output before emitting it (see :func:`~lumenframe.camera.render.compose_track`)
    pass a value strictly below the ``0.75`` taste-floor tolerance so that the
    downstream quantisation cannot tip a corner across the asserted predicate.
    """
    if covers_frame(scale, tx, ty, rot_deg, w, h, eps=eps):
        return tx, ty
    lo, hi = 0.0, 1.0
    for _ in range(24):
        mid = 0.5 * (lo + hi)
        if covers_frame(scale, tx * mid, ty * mid, rot_deg, w, h, eps=eps):
            lo = mid
        else:
            hi = mid
    return tx * lo, ty * lo


# ── the motion profile (axes → concrete numbers) ────────────────────────────


@dataclass(frozen=True)
class MotionProfile:
    """Every low-level number a move needs, derived once from the axes.

    Built by :meth:`from_axes`; the move functions read it and never touch raw
    axis values. This is the single auditable table where "energy 0.8" becomes
    "travel 1.6× faster, scale up to 1.32, wobble ±22px".
    """

    w: float
    h: float
    duration: float
    energy: float
    smoothness: float
    drama: float
    drift: float
    push_delta: float          # subtlety-capped zoom ratio for a push
    scale_floor: float         # min scale (reserves room for drift + rotation)
    pan_scale: float           # working scale for pans/tilts (gives translate budget)
    drift_amp_px: float        # total handheld translate amplitude (px)
    drift_rot_deg: float       # total handheld rotation amplitude (deg)
    speed: float               # multiplies handheld frequency
    main_ease: str             # the eased curve a main move uses
    settle_ease: str           # the landing curve
    hold_bias: float           # how far a start keyframe sits toward the subject

    @classmethod
    def from_axes(cls, energy: float, smoothness: float, drama: float, drift: float,
                  w: float, h: float, duration: float) -> "MotionProfile":
        energy = clamp01(energy)
        smoothness = clamp01(smoothness)
        drama = clamp01(drama)
        drift = clamp01(drift)
        min_dim = min(w, h)

        # SUBTLETY CEILING: a push is a small gesture. Default (mid axes) lands
        # ~1.05–1.20; energy/drama raise it but it is hard-capped at 1.7 so a
        # "push in" can never become a nauseating zoom.
        push_delta = 1.0 + remap(drama, 0.05, 0.20) + 0.12 * energy
        push_delta = min(max(push_delta, 1.03), 1.7)

        # Handheld amplitude: up to ~1.8% of the short edge, and a sub-degree
        # sway. Both scale with drift; the total is what the frame budget must
        # reserve.
        drift_amp_px = drift * 0.018 * min_dim
        drift_rot_deg = drift * 0.6

        # SCALE FLOOR: reserve room so the handheld wobble (translate AND the
        # corner-reveal of the tiny rotation) can never expose an edge. The
        # rotation term uses the frame's aspect so a wide frame reserves more.
        aspect = max(w / h, h / w)
        rot_margin = aspect * math.sin(math.radians(drift_rot_deg))
        trans_margin = (2.0 * drift_amp_px / min_dim)  # (scale-1) needed for the wobble
        scale_floor = 1.0 + 1.3 * trans_margin + 1.05 * rot_margin
        scale_floor = max(1.0, scale_floor)

        # Pans/tilts need translate budget → they work above the floor.
        pan_scale = max(scale_floor, 1.0 + remap(max(energy, drama), 0.05, 0.16))

        speed = remap(energy, 0.6, 1.8)

        if energy >= 0.66:
            main_ease = "punch"
        elif smoothness >= 0.6:
            main_ease = "ease_in_out_soft"
        else:
            main_ease = "ease_in_out"
        settle_ease = "settle" if smoothness >= 0.4 else "ease_out"

        return cls(
            w=float(w), h=float(h), duration=float(duration),
            energy=energy, smoothness=smoothness, drama=drama, drift=drift,
            push_delta=push_delta, scale_floor=scale_floor, pan_scale=pan_scale,
            drift_amp_px=drift_amp_px, drift_rot_deg=drift_rot_deg, speed=speed,
            main_ease=main_ease, settle_ease=settle_ease,
            hold_bias=0.12 + 0.1 * drama,
        )


# ── deterministic handheld noise ────────────────────────────────────────────


def handheld_channels(profile: MotionProfile, rng: Any) -> dict[str, list[dict[str, float]]]:
    """Seeded low-frequency sine stacks for tx / ty / rot.

    Handheld is the *sum of three low-frequency sines* per channel, each with a
    seeded frequency, phase and amplitude — organic, band-limited, and stable
    per seed (never white noise). The per-channel amplitudes are normalised so
    their absolute sum equals the channel budget exactly, which is what lets the
    frame-safety maths treat ``drift_amp_px`` as a hard bound.
    """
    def _stack(total: float, lo_cycles: float, hi_cycles: float) -> list[dict[str, float]]:
        raw = [rng.uniform(0.45, 1.0) for _ in range(3)]
        s = sum(raw) or 1.0
        stack = []
        for weight in raw:
            stack.append({
                "amp": total * (weight / s),
                "freq": rng.uniform(lo_cycles, hi_cycles) * profile.speed,
                "phase": rng.uniform(0.0, 2.0 * math.pi),
            })
        return stack

    if profile.drift_amp_px <= 0.0 and profile.drift_rot_deg <= 0.0:
        return {}
    return {
        # translate sways are slower than the rotational sway (a real operator
        # drifts position gently and tips the frame a touch quicker).
        "tx": _stack(profile.drift_amp_px, 0.4, 1.4),
        "ty": _stack(profile.drift_amp_px, 0.5, 1.6),
        "rot": _stack(profile.drift_rot_deg, 0.6, 1.8),
    }


def sample_channel(stack: list[dict[str, float]], t01: float) -> float:
    """Sum a sine stack at normalised time ``t01`` ∈ ``[0, 1]``."""
    total = 0.0
    for s in stack:
        total += s["amp"] * math.sin(2.0 * math.pi * s["freq"] * t01 + s["phase"])
    return total


# ── the move registry (each move → a base track shape) ──────────────────────

MOVES = Registry("camera.moves")

#: A move function signature. It returns *base* keyframes (before the handheld
#: layer): a list of ``{t, scale, tx, ty, rot, ease}`` with ``t`` ∈ ``[0,1]``.
#: ``ease`` is the curve of the segment ENDING at that keyframe (the first
#: keyframe's ease is ``None``). Translate is already frame-safe with the
#: handheld reserve held back, so ``base + wobble`` can never reveal an edge.
MoveFn = Callable[["MotionProfile", dict[str, float], Any, dict[str, Any]], list[dict[str, Any]]]


def _kf(t: float, scale: float, tx: float, ty: float, rot: float,
        ease: str | None) -> dict[str, Any]:
    return {"t": round(t, 4), "scale": round(scale, 5),
            "tx": round(tx, 3), "ty": round(ty, 3), "rot": round(rot, 4), "ease": ease}


def _reserve(profile: MotionProfile) -> float:
    return profile.drift_amp_px


def _mt(profile: MotionProfile, subj: dict[str, float], scale: float, bias: float) -> tuple[float, float]:
    r = _reserve(profile)
    return motivated_translate(subj["x"], subj["y"], scale, profile.w, profile.h,
                               bias=bias, reserve_x=r, reserve_y=r)


@MOVES.verb("push_in", summary="slow motivated zoom that settles onto the subject")
def push_in(profile: MotionProfile, subj: dict[str, float], rng: Any,
            params: dict[str, Any]) -> list[dict[str, Any]]:
    s0 = profile.scale_floor
    s1 = min(s0 * profile.push_delta, 1.7)
    tx0, ty0 = _mt(profile, subj, s0, profile.hold_bias)
    tx1, ty1 = _mt(profile, subj, s1, 1.0)
    return [
        _kf(0.0, s0, tx0, ty0, 0.0, None),
        _kf(1.0, s1, tx1, ty1, 0.0, profile.settle_ease),
    ]


@MOVES.verb("pull_out", summary="pull back off the subject to reveal the wider frame")
def pull_out(profile: MotionProfile, subj: dict[str, float], rng: Any,
             params: dict[str, Any]) -> list[dict[str, Any]]:
    s0 = min(profile.scale_floor * profile.push_delta, 1.7)
    s1 = profile.scale_floor
    tx0, ty0 = _mt(profile, subj, s0, 1.0)
    tx1, ty1 = _mt(profile, subj, s1, profile.hold_bias)
    return [
        _kf(0.0, s0, tx0, ty0, 0.0, None),
        _kf(1.0, s1, tx1, ty1, 0.0, profile.settle_ease),
    ]


@MOVES.verb("reveal", summary="pull back from a tight hold to reveal the context")
def reveal(profile: MotionProfile, subj: dict[str, float], rng: Any,
           params: dict[str, Any]) -> list[dict[str, Any]]:
    s0 = min(profile.scale_floor * profile.push_delta * 1.12, 1.7)
    s1 = profile.scale_floor
    tx0, ty0 = _mt(profile, subj, s0, 1.0)
    tx1, ty1 = _mt(profile, subj, s1, 0.0)
    return [
        _kf(0.0, s0, tx0, ty0, 0.0, None),
        _kf(1.0, s1, tx1, ty1, 0.0, "ease_out"),
    ]


@MOVES.verb("dolly", summary="a stronger physical push straight in toward the subject")
def dolly(profile: MotionProfile, subj: dict[str, float], rng: Any,
          params: dict[str, Any]) -> list[dict[str, Any]]:
    s0 = profile.scale_floor
    s1 = min(s0 * (1.0 + (profile.push_delta - 1.0) * 1.4), 1.7)
    tx0, ty0 = _mt(profile, subj, s0, profile.hold_bias)
    txm, tym = _mt(profile, subj, lerp(s0, s1, 0.6), 0.7)
    tx1, ty1 = _mt(profile, subj, s1, 1.0)
    return [
        _kf(0.0, s0, tx0, ty0, 0.0, None),
        _kf(0.6, lerp(s0, s1, 0.6), txm, tym, 0.0, profile.main_ease),
        _kf(1.0, s1, tx1, ty1, 0.0, profile.settle_ease),
    ]


def _pan(profile: MotionProfile, subj: dict[str, float], sign: float, ease: str) -> list[dict[str, Any]]:
    """A horizontal sweep at a fixed working scale (``sign`` sets direction)."""
    s = profile.pan_scale
    r = _reserve(profile)
    budget = max(0.0, frame_budget(s, profile.w) - r) * 0.85
    ty = motivated_translate(subj["x"], subj["y"], s, profile.w, profile.h,
                             bias=0.35, reserve_x=r, reserve_y=r)[1]
    return [
        _kf(0.0, s, -sign * budget, ty, 0.0, None),
        _kf(1.0, s, sign * budget, ty, 0.0, ease),
    ]


@MOVES.verb("pan_left", summary="sweep the frame left across the scene")
def pan_left(profile: MotionProfile, subj: dict[str, float], rng: Any,
             params: dict[str, Any]) -> list[dict[str, Any]]:
    return _pan(profile, subj, +1.0, profile.main_ease)


@MOVES.verb("pan_right", summary="sweep the frame right across the scene")
def pan_right(profile: MotionProfile, subj: dict[str, float], rng: Any,
              params: dict[str, Any]) -> list[dict[str, Any]]:
    return _pan(profile, subj, -1.0, profile.main_ease)


def _tilt(profile: MotionProfile, subj: dict[str, float], sign: float, ease: str) -> list[dict[str, Any]]:
    s = profile.pan_scale
    r = _reserve(profile)
    budget = max(0.0, frame_budget(s, profile.h) - r) * 0.85
    tx = motivated_translate(subj["x"], subj["y"], s, profile.w, profile.h,
                             bias=0.35, reserve_x=r, reserve_y=r)[0]
    return [
        _kf(0.0, s, tx, -sign * budget, 0.0, None),
        _kf(1.0, s, tx, sign * budget, 0.0, ease),
    ]


@MOVES.verb("tilt_up", summary="tip the frame up the scene")
def tilt_up(profile: MotionProfile, subj: dict[str, float], rng: Any,
            params: dict[str, Any]) -> list[dict[str, Any]]:
    return _tilt(profile, subj, +1.0, profile.main_ease)


@MOVES.verb("tilt_down", summary="tip the frame down the scene")
def tilt_down(profile: MotionProfile, subj: dict[str, float], rng: Any,
              params: dict[str, Any]) -> list[dict[str, Any]]:
    return _tilt(profile, subj, -1.0, profile.main_ease)


@MOVES.verb("ken_burns", summary="the classic slow diagonal drift with a gentle zoom")
def ken_burns(profile: MotionProfile, subj: dict[str, float], rng: Any,
              params: dict[str, Any]) -> list[dict[str, Any]]:
    # A deliberately gentle, long move: a small zoom married to a slow motivated
    # diagonal, always on the soft S so it never feels mechanical.
    s0 = profile.scale_floor * 1.02
    s1 = min(s0 * (1.0 + (profile.push_delta - 1.0) * 0.9 + 0.06), 1.7)
    tx0, ty0 = _mt(profile, subj, s0, 0.0)
    tx1, ty1 = _mt(profile, subj, s1, 1.0)
    return [
        _kf(0.0, s0, tx0, ty0, 0.0, None),
        _kf(1.0, s1, tx1, ty1, 0.0, "ease_in_out_soft"),
    ]


@MOVES.verb("handheld", summary="a near-locked framing whose life is all operator sway")
def handheld(profile: MotionProfile, subj: dict[str, float], rng: Any,
             params: dict[str, Any]) -> list[dict[str, Any]]:
    # The base is a held frame lightly biased toward the subject; the handheld
    # noise layer supplies the motion, so a flat drift==0 profile would be dead
    # still (which is why the "handheld" style pins drift high).
    s = profile.scale_floor
    tx, ty = _mt(profile, subj, s, profile.hold_bias)
    return [
        _kf(0.0, s, tx, ty, 0.0, None),
        _kf(1.0, s, tx, ty, 0.0, profile.main_ease),
    ]


@MOVES.verb("float", summary="a gentle breathing drift with the faintest zoom")
def float_move(profile: MotionProfile, subj: dict[str, float], rng: Any,
               params: dict[str, Any]) -> list[dict[str, Any]]:
    s0 = profile.scale_floor
    s1 = min(s0 * 1.02, 1.7)
    tx, ty = _mt(profile, subj, s0, profile.hold_bias)
    return [
        _kf(0.0, s0, tx, ty, 0.0, None),
        _kf(0.5, s1, tx, ty, 0.0, "ease_in_out_soft"),
        _kf(1.0, s0, tx, ty, 0.0, "ease_in_out_soft"),
    ]


@MOVES.verb("whip", summary="a fast whip pan that snaps across and settles")
def whip(profile: MotionProfile, subj: dict[str, float], rng: Any,
         params: dict[str, Any]) -> list[dict[str, Any]]:
    # A punchy sweep that reaches the far side early and settles for the rest of
    # the shot — the snap, not a leisurely glide.
    s = profile.pan_scale
    r = _reserve(profile)
    budget = max(0.0, frame_budget(s, profile.w) - r) * 0.9
    ty = motivated_translate(subj["x"], subj["y"], s, profile.w, profile.h,
                             bias=0.3, reserve_x=r, reserve_y=r)[1]
    return [
        _kf(0.0, s, -budget, ty, 0.0, None),
        _kf(0.32, s, budget, ty, 0.0, "whip"),
        _kf(1.0, s, budget, ty, 0.0, "settle"),
    ]


def move_names() -> list[str]:
    return MOVES.names()
