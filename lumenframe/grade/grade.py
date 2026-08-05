"""The grade vocabulary + the taste floor — where six dials become a recipe.

This is the module that makes ugly grades structurally hard. A colour grade is
built by running a fixed pipeline of **operations** (white balance → tone S-curve
→ split toning → saturation → vignette → grain → halation). Each operation is a
registered verb (so the catalog can never drift from what actually runs) and each
one *enforces* a craft rule rather than trusting the caller:

* :func:`op_temperature` — white balance from ``warmth``; tint kept tiny.
* :func:`op_scurve`      — a **protected** S-curve. It steepens the mid-tones but
  its endpoints are pinned (``f(0)=0``, ``f(1)=1``), so the toe and shoulder are
  never crushed to a hard clip *unless the look demands it* (``allow_clip``,
  e.g. noir). Faded blacks lift the shadow floor without touching the highlight.
* :func:`op_split`       — shadow/highlight split toning. For any cinematic look
  the two hues are forced **complementary** (≈180° apart, teal↔orange). For
  non-stylised looks the split strength is capped so a reference skin tone never
  drifts more than :data:`SKIN_TOLERANCE_DEG`.
* :func:`op_saturation`  — saturation with a hard ceiling (:data:`SAT_CEILING`);
  no radioactive colour, ever. Monochrome looks are driven to zero.
* :func:`op_vignette` / :func:`op_grain` / :func:`op_halation` — the optical
  finish, all scaled by ``drama`` / ``filmic`` and by the brief ``intensity``.

Everything is deterministic: only :func:`op_grain` touches the rng, and it draws
a fixed-length field from the brief's seeded generator, so the recipe bytes are
byte-stable per seed. ``intensity`` (``0..1``) scales every magnitude toward the
neutral recipe — it can dial a grade *down*, never past a physical limit.
"""
from __future__ import annotations

import colorsys
import math
from dataclasses import dataclass, field
from random import Random
from typing import Any, Callable

from lumenframe.craft import Registry, clamp01, lerp
from lumenframe.craft.determinism import round_floats

from lumenframe.grade.params import GRADE_AXES

# ── enforced limits (the taste floor, as constants) ─────────────────────────

#: Global saturation may never exceed this (no radioactive colour).
SAT_CEILING: float = 1.75
#: Vibrance (low-sat-weighted boost) ceiling.
VIBRANCE_CEILING: float = 0.6
#: Default safe input clip points — blacks/whites stay inside this band unless a
#: look sets ``allow_clip`` (noir, day-for-night).
BLACK_POINT_SAFE_MAX: float = 0.06
WHITE_POINT_SAFE_MIN: float = 0.94
#: A stylised look may crush this far and no further (still not a full clamp).
BLACK_POINT_HARD_MAX: float = 0.14
WHITE_POINT_HARD_MIN: float = 0.86
#: How many degrees a reference skin tone may drift for a NON-stylised look.
SKIN_TOLERANCE_DEG: float = 10.0
#: Reference mid skin tone (linear-ish sRGB, hue ≈ 21°) used for protection.
SKIN_REF_RGB: tuple[float, float, float] = (0.78, 0.52, 0.38)
#: Grain field length — a fixed count keeps the recipe bytes stable per seed.
GRAIN_FIELD_N: int = 16

#: Magnitude scalars (all further scaled by brief ``intensity``).
_MAX_TEMP = 0.6          # normalised warm/cool white-balance push
_MAX_CONTRAST = 0.8      # S-curve blend weight
_MAX_LIFT = 0.16         # faded-black luma floor
_MAX_SPLIT = 0.14        # split-tone chroma per channel
_MAX_VIGNETTE = 0.5
_MAX_GRAIN = 0.4
_MAX_HALATION = 0.35


# ── colour helpers (one source of truth, shared with render + tests) ────────


def _hue_rgb(hue_deg: float) -> tuple[float, float, float]:
    """A unit-ish signed RGB push for a hue, centred on grey.

    Full-saturation HSV at the hue, re-centred to ``[-0.5, +0.5]`` so it can be
    *added* as a chroma offset (positive on the hue's channels, negative on the
    complement). Deterministic and dependency-free.
    """
    r, g, b = colorsys.hsv_to_rgb((hue_deg % 360.0) / 360.0, 1.0, 1.0)
    return (r - 0.5, g - 0.5, b - 0.5)


def _rgb_hue(rgb: tuple[float, float, float]) -> float:
    """Hue of an RGB triple in degrees (0 for greys)."""
    r, g, b = (clamp01(c) for c in rgb)
    mx, mn = max(r, g, b), min(r, g, b)
    if mx - mn < 1e-9:
        return 0.0
    h, _, _ = colorsys.rgb_to_hsv(r, g, b)
    return h * 360.0


def _hue_delta(a: float, b: float) -> float:
    """Smallest absolute angular distance between two hues, in degrees."""
    d = abs((a - b) % 360.0)
    return min(d, 360.0 - d)


def _circular_mean_deg(a: float, b: float) -> float:
    """Circular mean of two hue angles (degrees), robust across the 0/360 seam."""
    ar, br = math.radians(a % 360.0), math.radians(b % 360.0)
    x = math.cos(ar) + math.cos(br)
    y = math.sin(ar) + math.sin(br)
    if abs(x) < 1e-12 and abs(y) < 1e-12:  # antipodal → mean undefined; anchor on a
        return a % 360.0
    return math.degrees(math.atan2(y, x)) % 360.0


# ── the recipe container ────────────────────────────────────────────────────


def neutral_recipe() -> dict[str, Any]:
    """The identity grade — the target every ``intensity`` scale falls back to."""
    return {
        "temperature": 0.0,
        "tint": 0.0,
        "lift": {"r": 0.0, "g": 0.0, "b": 0.0},
        "gamma": {"r": 1.0, "g": 1.0, "b": 1.0},
        "gain": {"r": 1.0, "g": 1.0, "b": 1.0},
        "contrast": {"amount": 0.0, "pivot": 0.5},
        "saturation": 1.0,
        "vibrance": 0.0,
        "shadow_hue": None,
        "highlight_hue": None,
        "split_strength": 0.0,
        "black_point": 0.0,
        "white_point": 1.0,
        "vignette": 0.0,
        "grain": 0.0,
        "grain_field": [],
        "halation": 0.0,
    }


@dataclass
class GradeContext:
    """Per-build knobs the operations read (nothing global, nothing timed)."""

    intensity: float
    hints: dict[str, Any]
    rng: Random
    axes: dict[str, float] = field(default_factory=dict)

    def axis(self, name: str, default: float = 0.5) -> float:
        return self.axes.get(name, default)


# ── the operation registry (anti-drift vocabulary) ──────────────────────────

OP_FAMILIES = ("balance", "tone", "color", "optics")
REGISTRY = Registry("grade ops", families=OP_FAMILIES)

#: The fixed pipeline order. Order matters for determinism (rng draw order) and
#: for correctness (split reads the tone context; skin protection runs last).
PIPELINE: tuple[str, ...] = (
    "balance.temperature",
    "tone.scurve",
    "color.split",
    "color.saturation",
    "optics.vignette",
    "optics.grain",
    "optics.halation",
)

Op = Callable[[dict[str, Any], GradeContext], None]


@REGISTRY.verb("balance.temperature", family="balance",
               summary="white balance from warmth; tint kept tiny and neutral")
def op_temperature(recipe: dict[str, Any], ctx: GradeContext) -> None:
    """Map ``warmth`` to a temperature push (warm = +, cool = -).

    Tint (green↔magenta) is deliberately a fraction of temperature: a healthy
    grade shifts *temperature*, not tint, so faces never go green. Stylised
    looks may carry a small explicit ``tint_bias`` hint.
    """
    w = ctx.axis("warmth")
    temp = (w - 0.5) * 2.0 * _MAX_TEMP * ctx.intensity
    recipe["temperature"] = temp
    recipe["tint"] = (float(ctx.hints.get("tint_bias", 0.0)) + temp * 0.08) * ctx.intensity


@REGISTRY.verb("tone.scurve", family="tone",
               summary="protected S-curve; faded toe; safe black/white points")
def op_scurve(recipe: dict[str, Any], ctx: GradeContext) -> None:
    """Set the contrast S-curve, its pivot, the faded-black floor and the clips.

    ``contrast`` sets the S blend weight. ``drama`` nudges the pivot *down* so
    the grade seats more of the frame in shadow (a moodier tonality). ``lift``
    raises the shadow floor (faded blacks) — encoded into the ``lift`` wheel so
    the highlight is untouched. Black/white input clips stay inside the safe
    band unless the look sets ``allow_clip``.
    """
    c = ctx.axis("contrast")
    drama = ctx.axis("drama")
    lift_ax = ctx.axis("lift")
    amount = (c - 0.5) * 2.0 * _MAX_CONTRAST * ctx.intensity
    pivot = clamp01(0.5 - (drama - 0.2) * 0.14 * ctx.intensity)
    recipe["contrast"] = {"amount": amount, "pivot": pivot}

    faded = lift_ax * _MAX_LIFT * ctx.intensity
    for ch in "rgb":
        recipe["lift"][ch] = faded  # neutral (equal) shadow floor lift

    allow_clip = bool(ctx.hints.get("allow_clip"))
    black_max = BLACK_POINT_HARD_MAX if allow_clip else BLACK_POINT_SAFE_MAX
    white_min = WHITE_POINT_HARD_MIN if allow_clip else WHITE_POINT_SAFE_MIN
    # crush only when the look allows it and the frame is contrasty + faded-free.
    crush = max(0.0, (c - 0.55)) * (drama) * ctx.intensity
    black = min(black_max, crush * 0.4) if allow_clip else min(black_max, crush * 0.12)
    white = max(white_min, 1.0 - (crush * 0.3 if allow_clip else crush * 0.08))
    # a lifted look must not also clip its blacks — that would be contradictory.
    if faded > 0.02:
        black = min(black, BLACK_POINT_SAFE_MAX * 0.5)
    recipe["black_point"] = clamp01(black)
    recipe["white_point"] = clamp01(white)


@REGISTRY.verb("color.split", family="color",
               summary="complementary shadow/highlight split; skin-safe by default")
def op_split(recipe: dict[str, Any], ctx: GradeContext) -> None:
    """Tint shadows and highlights, enforcing a complementary cinematic split.

    * Monochrome looks get no split (the saturation op will grey them out).
    * A cinematic look with no explicit hues defaults to teal shadows / orange
      highlights; if it declares BOTH hues they are *snapped* exactly
      complementary around the circular midpoint of the declared pair — so both
      declared hues influence the result, yet a typo can never produce a muddy
      same-side split.
    * Strength grows with ``drama``; for non-stylised looks it is capped so a
      reference skin tone cannot drift beyond tolerance (enforced in
      :func:`derive_recipe`).
    """
    if ctx.hints.get("monochrome"):
        recipe["shadow_hue"] = recipe["highlight_hue"] = None
        recipe["split_strength"] = 0.0
        return

    cinematic = bool(ctx.hints.get("cinematic"))
    shadow_hue = ctx.hints.get("shadow_hue")
    highlight_hue = ctx.hints.get("highlight_hue")
    if cinematic:
        if shadow_hue is None or highlight_hue is None:
            shadow_hue, highlight_hue = 200.0, 30.0  # teal / orange fallback
        else:
            # snap the pair EXACTLY complementary, centred on the circular
            # midpoint of the declared hues so BOTH influence the result. The
            # highlight is a shadow-side vote at ``highlight+180``; average it
            # with the shadow, then place the pair 180° apart around that mean.
            centre = _circular_mean_deg(float(shadow_hue), float(highlight_hue) + 180.0)
            shadow_hue = centre
            highlight_hue = (centre + 180.0) % 360.0
    if shadow_hue is None or highlight_hue is None:
        recipe["shadow_hue"] = recipe["highlight_hue"] = None
        recipe["split_strength"] = 0.0
        return

    drama = ctx.axis("drama")
    strength = (0.35 + 0.65 * drama) * _MAX_SPLIT * ctx.intensity
    recipe["shadow_hue"] = round(float(shadow_hue) % 360.0, 3)
    recipe["highlight_hue"] = round(float(highlight_hue) % 360.0, 3)
    recipe["split_strength"] = strength
    _write_split(recipe)


def _write_split(recipe: dict[str, Any]) -> None:
    """(Re)compute the lift/gain chroma from the stored split hues + strength."""
    strength = recipe["split_strength"]
    sh, hh = recipe["shadow_hue"], recipe["highlight_hue"]
    faded = recipe["lift"]["r"] if _is_neutral_lift(recipe) else _lift_luma(recipe)
    sr, sg, sb = _hue_rgb(sh) if sh is not None else (0.0, 0.0, 0.0)
    hr, hg, hb = _hue_rgb(hh) if hh is not None else (0.0, 0.0, 0.0)
    recipe["lift"] = {
        "r": faded + sr * strength, "g": faded + sg * strength, "b": faded + sb * strength,
    }
    recipe["gain"] = {
        "r": 1.0 + hr * strength, "g": 1.0 + hg * strength, "b": 1.0 + hb * strength,
    }


def _is_neutral_lift(recipe: dict[str, Any]) -> bool:
    vals = recipe["lift"].values()
    return max(vals) - min(vals) < 1e-9


def _lift_luma(recipe: dict[str, Any]) -> float:
    return sum(recipe["lift"].values()) / 3.0


@REGISTRY.verb("color.saturation", family="color",
               summary="saturation + vibrance under a hard ceiling; mono → 0")
def op_saturation(recipe: dict[str, Any], ctx: GradeContext) -> None:
    """Global saturation with an enforced ceiling; vibrance protects skin.

    Monochrome looks are driven to zero saturation. Everyone else maps the
    ``saturation`` axis onto ``[0.2, 1.8]`` and then *hard-clamps* under
    :data:`SAT_CEILING`, so no combination of feelings and overrides can produce
    radioactive colour. Vibrance (a low-saturation-weighted boost) rides
    ``saturation`` gently and is likewise capped.
    """
    if ctx.hints.get("monochrome"):
        recipe["saturation"] = 0.0
        recipe["vibrance"] = 0.0
        return
    s = ctx.axis("saturation")
    sat = 1.0 + (s - 0.5) * 2.0 * 0.8 * ctx.intensity
    recipe["saturation"] = round(min(SAT_CEILING, max(0.0, sat)), 6)
    recipe["vibrance"] = round(min(VIBRANCE_CEILING, max(0.0, (s - 0.5) * 0.5 * ctx.intensity)), 6)


@REGISTRY.verb("optics.vignette", family="optics",
               summary="mood vignette weight from drama")
def op_vignette(recipe: dict[str, Any], ctx: GradeContext) -> None:
    """Vignette darkening weight — pure ``drama``, capped."""
    recipe["vignette"] = round(min(_MAX_VIGNETTE, ctx.axis("drama") * _MAX_VIGNETTE * ctx.intensity), 6)


@REGISTRY.verb("optics.grain", family="optics",
               summary="filmic grain amount + a seeded, byte-stable field")
def op_grain(recipe: dict[str, Any], ctx: GradeContext) -> None:
    """Grain amount from ``filmic`` plus a fixed-length seeded field.

    The ONLY rng consumer. The field is a short, rounded list drawn from the
    brief's seeded generator, so the recipe serialises to identical bytes for a
    given seed while still varying seed-to-seed.
    """
    amount = min(_MAX_GRAIN, ctx.axis("filmic") * _MAX_GRAIN * ctx.intensity)
    recipe["grain"] = round(amount, 6)
    recipe["grain_field"] = [round(ctx.rng.uniform(-1.0, 1.0), 4) for _ in range(GRAIN_FIELD_N)]


@REGISTRY.verb("optics.halation", family="optics",
               summary="highlight halation bloom from filmic")
def op_halation(recipe: dict[str, Any], ctx: GradeContext) -> None:
    """Highlight halation (red-orange bloom) from ``filmic``, capped."""
    recipe["halation"] = round(min(_MAX_HALATION, ctx.axis("filmic") * _MAX_HALATION * ctx.intensity), 6)


# ── the tone curve (protected S) — one source of truth ──────────────────────


def _s_curve(x: float, pivot: float, amount: float) -> float:
    """Identity blended toward a normalised logistic (or toward the pivot).

    For ``amount >= 0`` (raising contrast) the endpoints are pinned exactly —
    ``f(0)=0`` and ``f(1)=1`` — so the toe and shoulder are protected: the middle
    steepens while the extremes flatten, and the curve can never clip harder than
    the input.

    For ``amount < 0`` the curve deliberately collapses toward the ``pivot`` (a
    low-contrast / matte fade): blacks lift and whites drop toward the pivot, so
    the endpoints are *intentionally not pinned* here — that toe-lift / shoulder-
    drop is the whole point of the faded look. ``f(0)`` and ``f(1)`` drift off
    0/1 as ``|amount|`` grows, and that is by design.
    """
    if amount >= 0.0:
        k = 2.0 + amount * 9.0
        lo = 1.0 / (1.0 + math.exp(k * pivot))
        hi = 1.0 / (1.0 + math.exp(-k * (1.0 - pivot)))
        raw = 1.0 / (1.0 + math.exp(-k * (x - pivot)))
        norm = (raw - lo) / (hi - lo) if hi - lo > 1e-9 else x
        return clamp01(lerp(x, norm, min(1.0, amount / _MAX_CONTRAST)))
    mag = min(1.0, -amount / _MAX_CONTRAST)
    return clamp01(lerp(x, pivot + (x - pivot) * 0.55, mag))


def tone_luma(x: float, recipe: dict[str, Any]) -> float:
    """The achromatic tone response: input clip → protected S-curve."""
    black, white = recipe["black_point"], recipe["white_point"]
    span = max(1e-6, white - black)
    xin = clamp01((clamp01(x) - black) / span)
    amount = recipe["contrast"]["amount"]
    pivot = recipe["contrast"]["pivot"]
    return _s_curve(xin, pivot, amount)


def tone_channel(x: float, recipe: dict[str, Any], ch: str) -> float:
    """Per-channel tone response: luma curve + split lift/gain + gamma."""
    y = tone_luma(x, recipe)
    lift_c = recipe["lift"][ch]
    gain_c = recipe["gain"][ch]
    gamma_c = recipe["gamma"][ch]
    shadow_w = (1.0 - y) ** 1.6
    high_w = y ** 1.6
    y = y + lift_c * shadow_w + (gain_c - 1.0) * high_w
    y = clamp01(y)
    if abs(gamma_c - 1.0) > 1e-9:
        y = clamp01(y ** (1.0 / gamma_c))
    return y


def tone_curve_samples(recipe: dict[str, Any], n: int = 21) -> list[float]:
    """Sample the achromatic tone curve on ``[0, 1]`` (for render + tests)."""
    return [round(tone_luma(i / (n - 1), recipe), 6) for i in range(n)]


# ── applying a recipe to a single RGB (skin-drift + render sampling) ─────────


def apply_recipe_rgb(recipe: dict[str, Any], rgb: tuple[float, float, float]) -> tuple[float, float, float]:
    """Apply the whole colour path (WB → tone → saturation) to one RGB triple.

    The authoritative reference for what the SVG/ffmpeg filters approximate and
    the basis of skin-tone-drift measurement. Vignette/grain/halation are
    spatial and intentionally excluded here.
    """
    r, g, b = (clamp01(c) for c in rgb)
    t, ti = recipe["temperature"], recipe["tint"]
    # white balance: temperature trades R against B; tint trades G against R+B.
    r = clamp01(r * (1.0 + 0.45 * t) * (1.0 + 0.15 * ti))
    b = clamp01(b * (1.0 - 0.45 * t) * (1.0 + 0.15 * ti))
    g = clamp01(g * (1.0 - 0.22 * ti))
    r = tone_channel(r, recipe, "r")
    g = tone_channel(g, recipe, "g")
    b = tone_channel(b, recipe, "b")
    luma = 0.2126 * r + 0.7152 * g + 0.0722 * b
    sat = recipe["saturation"]
    r = clamp01(luma + (r - luma) * sat)
    g = clamp01(luma + (g - luma) * sat)
    b = clamp01(luma + (b - luma) * sat)
    # Vibrance: a low-saturation-weighted second boost. It scales the chroma of
    # already-muted pixels the most and spares the already-saturated ones (skin
    # is fairly saturated, so it is protected). Being a luma-uniform scale it
    # cannot rotate hue — it only recovers muted colour, never shifts skin.
    vib = recipe.get("vibrance", 0.0)
    if vib > 1e-9:
        luma2 = 0.2126 * r + 0.7152 * g + 0.0722 * b
        mx, mn = max(r, g, b), min(r, g, b)
        cur_sat = 0.0 if mx <= 1e-9 else (mx - mn) / mx
        boost = 1.0 + vib * (1.0 - cur_sat)
        r = clamp01(luma2 + (r - luma2) * boost)
        g = clamp01(luma2 + (g - luma2) * boost)
        b = clamp01(luma2 + (b - luma2) * boost)
    return (r, g, b)


def skin_drift(recipe: dict[str, Any]) -> float:
    """Hue drift (degrees) the grade imposes on the reference skin tone."""
    before = _rgb_hue(SKIN_REF_RGB)
    after = _rgb_hue(apply_recipe_rgb(recipe, SKIN_REF_RGB))
    return _hue_delta(before, after)


#: Skin-protection loop bound + per-step attenuation. 24 steps of 0.82 drives
#: every drift-causing field to ~0.9% of its original deviation, which is enough
#: to pull the worst reachable non-stylised brief well under tolerance.
_PROTECT_ITERS: int = 24
_PROTECT_DECAY: float = 0.82


def _protect_skin(recipe: dict[str, Any]) -> bool:
    """Pull EVERY skin-hue-rotating field toward neutral until drift is in budget.

    Skin hue is rotated by more than just the chroma cast: the per-channel tone
    S-curve (``contrast``) and the global saturation scale both shift the skin
    triple's hue on their own. So each bounded step attenuates, toward neutral,
    *all* of them — the split chroma (deviation of lift/gain from their neutral
    mean), temperature/tint/split_strength, the contrast amount, and saturation.
    Luma is preserved (the faded floor and mean gain are untouched). The loop
    stops the instant drift is within tolerance, so a mild cast is barely
    touched; only a hard cast is flattened. Returns ``True`` if it intervened.

    Post-condition (hard floor): on return the reference skin tone drifts no more
    than :data:`SKIN_TOLERANCE_DEG` — the advertised guarantee, asserted, not
    merely documented.
    """
    intervened = False
    for _ in range(_PROTECT_ITERS):
        if skin_drift(recipe) <= SKIN_TOLERANCE_DEG:
            break
        intervened = True
        lm = _lift_luma(recipe)
        gm = sum(recipe["gain"].values()) / 3.0
        for ch in "rgb":
            recipe["lift"][ch] = lm + (recipe["lift"][ch] - lm) * _PROTECT_DECAY
            recipe["gain"][ch] = gm + (recipe["gain"][ch] - gm) * _PROTECT_DECAY
        recipe["temperature"] *= _PROTECT_DECAY
        recipe["tint"] *= _PROTECT_DECAY
        recipe["split_strength"] *= _PROTECT_DECAY
        recipe["contrast"]["amount"] *= _PROTECT_DECAY
        recipe["saturation"] = 1.0 + (recipe["saturation"] - 1.0) * _PROTECT_DECAY
    # Hard floor: the skin-safe guarantee must actually hold when we claim it.
    assert skin_drift(recipe) <= SKIN_TOLERANCE_DEG + 1e-6, (
        f"skin protection failed to converge: drift {skin_drift(recipe):.3f}° "
        f"> tolerance {SKIN_TOLERANCE_DEG}°"
    )
    return intervened


# ── the public derivation ───────────────────────────────────────────────────


def derive_recipe(
    axes: dict[str, float],
    hints: dict[str, Any],
    *,
    intensity: float,
    rng: Random,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Run the full pipeline → ``(recipe, report)``.

    ``report`` records which operations ran, the split, the skin drift, and
    whether skin protection had to intervene — the auditable trail the plan and
    the tool reply surface. The recipe floats are rounded once at the end so two
    structurally-equal builds serialise to identical bytes.
    """
    ctx = GradeContext(
        intensity=clamp01(intensity),
        hints=dict(hints),
        rng=rng,
        axes={a: float(axes.get(a, 0.5)) for a in GRADE_AXES},
    )
    recipe = neutral_recipe()
    ran: list[str] = []
    for name in PIPELINE:
        REGISTRY.require(name)(recipe, ctx)
        ran.append(name)

    stylised = bool(hints.get("stylised") or hints.get("monochrome"))
    protected = False
    if not stylised:
        protected = _protect_skin(recipe)

    report = {
        "ops": ran,
        "split": {
            "shadow_hue": recipe["shadow_hue"],
            "highlight_hue": recipe["highlight_hue"],
            "strength": round(recipe["split_strength"], 6),
            "complementary": _split_is_complementary(recipe),
        },
        "skin_drift_deg": round(skin_drift(recipe), 4),
        "skin_protected": protected,
        "stylised": stylised,
        "curve": tone_curve_samples(recipe),
        "intensity": ctx.intensity,
    }
    return round_floats(recipe, 6), report


def _split_is_complementary(recipe: dict[str, Any], tol: float = 25.0) -> bool:
    sh, hh = recipe["shadow_hue"], recipe["highlight_hue"]
    if sh is None or hh is None:
        return False
    return abs(_hue_delta(sh, hh) - 180.0) <= tol
