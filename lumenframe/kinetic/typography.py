"""The taste floor — modular type scale, hierarchy, safe margins, rhythm.

This is where amateur output is made *structurally hard*. The agent never picks
a point size or a millisecond delay; it picks a layout, a style and a feeling,
and this module derives every number from a small set of professional rules:

1. **Modular type scale.** Sizes come from ``base · ratio^step`` (ratio ≥ 1.2),
   never from arbitrary values. Hierarchy tiers sit on distinct scale steps, so
   title vs subtitle vs attribution differ by at least one full ratio.
2. **Hierarchy contrast on three channels.** Adjacent tiers differ in *size*
   (scale step), *weight* (≥ 100 font-weight apart) and *colour/opacity*
   (≥ 0.12 fill-opacity apart) — the trio that reads as real hierarchy.
3. **TV title-safe margins.** A hard ≥ 9 % inset on every edge; text is scaled
   down before it is ever allowed to cross it.
4. **Optical leading.** Line-height ratio *shrinks* as size grows, so big
   headings don't float apart and small captions don't collide.
5. **Line length.** Copy wraps at ~30–38 characters (tightened by ``density``);
   nothing is allowed to overflow the safe box — a fit-scale shrinks the whole
   block uniformly, preserving the modular ratios.
6. **Reveal rhythm from pace.** Per-unit stagger and duration are a function of
   the ``pace`` axis, not chosen per call, so timing stays in one house rhythm.

Layouts and reveals are :class:`~lumenframe.craft.registry.Registry` vocabularies
so the catalog can never drift from what actually runs. Everything is a pure
function of the resolved brief — no module state, no wall-clock, no unseeded
randomness — so the same brief renders byte-for-byte identically.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from lumenframe.craft import Registry, clamp01, lerp, remap
from lumenframe.craft.params import ResolvedAxes

# ── registries ──────────────────────────────────────────────────────────────

LAYOUTS = Registry("kinetic.layouts")
REVEALS = Registry("kinetic.reveals")

#: Non-linear easing curves keyed by name. Linear is deliberately absent — a
#: title that eases linearly reads as a bug, so the taste floor never offers it.
EASINGS: dict[str, str] = {
    "out_quint": "cubic-bezier(0.22, 1, 0.36, 1)",
    "out_expo": "cubic-bezier(0.16, 1, 0.3, 1)",
    "out_quad": "cubic-bezier(0.25, 0.46, 0.45, 0.94)",
    "back": "cubic-bezier(0.34, 1.56, 0.64, 1)",
}

# ── constants that define the floor ──────────────────────────────────────────

#: Title-safe inset as a fraction of the shorter canvas edge (hard minimum).
SAFE_INSET = 0.09
#: Absolute smallest rendered size (px) — below this, type is illegible.
MIN_SIZE = 14.0
#: Fraction of size used as the baseline offset from a line's top edge.
BASELINE_FRAC = 0.80


def modular_size(base: float, step: int, ratio: float) -> float:
    """A size on the modular scale: ``base · ratio**step`` (rounded to 0.1)."""
    return round(base * (ratio ** step), 1)


def leading_ratio(size: float, canvas_h: float, density: float) -> float:
    """Line-height ratio that *shrinks* as size grows (optical leading).

    Larger type needs proportionally less leading; denser briefs tighten
    everything. Monotonic decreasing in ``size`` and in ``density``, clamped to
    a legible band.
    """
    base = lerp(1.5, 1.14, clamp01(density))          # density tightens leading
    optical = 0.9 * (size / max(canvas_h, 1.0))        # big type ⇒ less leading
    return round(max(1.02, min(1.6, base - optical)), 4)


def advance_ratio(family: str, condensed: bool) -> float:
    """Mean glyph advance as a fraction of the em (for width estimation).

    A serif and a condensed cut run narrower than a default sans; the number is
    a deliberate slight over-estimate so wrapping errs toward *not* overflowing.
    """
    base = 0.52 if family == "serif" else 0.55
    return round(base * (0.86 if condensed else 1.0), 4)


def tracking_em(density: float, elegance: float) -> float:
    """Letter-spacing in em: airy (low density) opens up, dense tightens.

    Elegance lifts the loose end slightly (refined type breathes), but a dense
    brief can still pull it negative for a packed, condensed look.
    """
    loose = 0.06 + 0.04 * clamp01(elegance)
    return round(lerp(loose, -0.015, clamp01(density)), 4)


def max_line_chars(density: float) -> int:
    """Wrap width in characters — the measure. Denser briefs run tighter,
    but never past the ~35-char sweet spot for readable display type."""
    return int(round(lerp(38, 30, clamp01(density))))


def wrap_text(text: str, max_chars: int) -> list[str]:
    """Wrap to lines of at most ``max_chars``. Space-delimited copy wraps on
    words; space-free scripts (CJK) wrap on character count. Never returns an
    empty list for non-empty input."""
    s = " ".join(str(text).split())
    if not s:
        return []
    if " " not in s:  # no word boundaries (e.g. CJK): hard char wrap
        return [s[i:i + max_chars] for i in range(0, len(s), max_chars)] or [s]
    lines: list[str] = []
    cur = ""
    for word in s.split(" "):
        cand = f"{cur} {word}".strip()
        if len(cand) <= max_chars or not cur:
            cur = cand
        else:
            lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines


def font_weight(axis_weight: float, bump: float = 0.0) -> int:
    """Map the weight axis (+ a per-role bump) to a 100-stepped font-weight."""
    w = clamp01(axis_weight + bump)
    return int(round(lerp(300, 900, w) / 100.0)) * 100


def enforce_weight_ladder(weights: list[int]) -> list[int]:
    """Guarantee a strictly-descending weight ladder with ≥ 100 gaps.

    ``weights`` is ordered most-prominent first. Any tier that isn't at least
    100 lighter than the one above it is pushed down (floored at 200), so the
    weight channel of the hierarchy is *always* legible, whatever the axes said.
    """
    out = list(weights)
    for i in range(1, len(out)):
        ceil = out[i - 1] - 100
        if out[i] > ceil:
            out[i] = max(200, ceil)
    return out


# ── resolved typographic context ─────────────────────────────────────────────


@dataclass
class TypeContext:
    """Everything a layout needs, all derived from the resolved axes + canvas.

    A layout reads this and emits runs; it never re-derives a size or a colour
    from raw axes, so the floor's rules live in exactly one place.
    """

    width: int
    height: int
    ratio: float
    base: float                    # scale anchor size (px) for step 0
    family: str
    condensed: bool
    case: str
    density: float
    elegance: float
    palette: dict[str, Any]
    axes: ResolvedAxes

    # derived
    safe_x: int = 0
    safe_y: int = 0
    max_chars: int = 35
    advance: float = 0.55
    tracking: float = 0.0

    def __post_init__(self) -> None:
        inset = SAFE_INSET
        self.safe_x = int(round(min(self.width, self.height) * inset))
        self.safe_y = int(round(min(self.width, self.height) * inset))
        self.max_chars = max_line_chars(self.density)
        self.advance = advance_ratio(self.family, self.condensed)
        self.tracking = tracking_em(self.density, self.elegance)

    # geometry of the safe box
    @property
    def left(self) -> int:
        return self.safe_x

    @property
    def right(self) -> int:
        return self.width - self.safe_x

    @property
    def top(self) -> int:
        return self.safe_y

    @property
    def bottom(self) -> int:
        return self.height - self.safe_y

    @property
    def avail_w(self) -> float:
        return float(self.right - self.left)

    @property
    def avail_h(self) -> float:
        return float(self.bottom - self.top)

    @property
    def center_x(self) -> float:
        return round((self.left + self.right) / 2.0, 2)

    def size(self, step: int) -> float:
        """A raw size on this context's modular scale (``base·ratio**step``).

        The :data:`MIN_SIZE` legibility floor is **not** applied per tier here —
        clamping each tier independently would collapse the modular ratio between
        two tiers once both hit the floor. The floor is instead enforced once, on
        the whole block, in :func:`_fit_scale`, so the ratios between tiers always
        survive (see finding: tiers-differ).
        """
        return modular_size(self.base, step, self.ratio)

    def estimate_width(self, text: str, size: float) -> float:
        """Estimated rendered width of a single line (px)."""
        spacing = max(0.0, self.tracking)  # negative tracking never grows width
        return len(text) * size * self.advance * (1.0 + spacing)

    def cased(self, text: str) -> str:
        return text.upper() if self.case == "upper" else text


def build_context(axes: ResolvedAxes, width: int, height: int,
                  palette: dict[str, Any], hints: dict[str, Any]) -> TypeContext:
    """Assemble the :class:`TypeContext` for a build from resolved axes + style.

    The scale anchor (``base``) is a fraction of the canvas height nudged up by
    ``energy``/``weight`` and down by ``density`` — so a punchy heavy brief
    reads bigger and a dense one packs tighter — but the *steps* are always the
    modular ratio, never touched.
    """
    ratio = max(1.2, float(hints.get("scale_ratio", 1.25)))
    energy = axes.axis("energy")
    weight = axes.axis("weight")
    density = axes.axis("density")
    anchor_frac = 0.052 + 0.012 * energy + 0.010 * weight - 0.008 * density
    base = round(height * anchor_frac, 2)
    return TypeContext(
        width=int(width), height=int(height), ratio=ratio, base=base,
        family=str(hints.get("family", "sans-serif")),
        condensed=bool(hints.get("condensed", False)),
        case=str(hints.get("case", "none")),
        density=density, elegance=axes.axis("elegance"),
        palette=palette, axes=axes,
    )


# ── run helpers (a run is one laid-out line) ──────────────────────────────────


def _run(role: str, text: str, x: float, y: float, size: float, weight: int,
         ctx: TypeContext, color: str, fill_opacity: float,
         align: str) -> dict[str, Any]:
    return {
        "role": role,
        "text": ctx.cased(text),
        "x": round(x, 2),
        "y": round(y, 2),
        "size": round(size, 1),
        "weight": int(weight),
        "family": ctx.family,
        "color": color,
        "fill_opacity": round(clamp01(fill_opacity), 3),
        "align": align,                       # "middle" | "start"
        "tracking": ctx.tracking,
    }


def _line_height(size: float, ctx: TypeContext) -> float:
    return size * leading_ratio(size, ctx.height, ctx.density)


def _stack(lines: list[dict[str, Any]], ctx: TypeContext, anchor: str) -> None:
    """Assign baseline ``y`` to a vertical stack of runs in place.

    ``anchor`` is ``"center"`` (title cards / quotes), ``"bottom"`` (lower
    third) or ``"top"``. Each run's height uses optical leading; a run may carry
    a ``_pad`` to open extra space above it (e.g. before a subtitle group).
    """
    boxes = [(_line_height(r["size"], ctx), r.get("_pad", 0.0)) for r in lines]
    total = sum(h + p for h, p in boxes)
    if anchor == "center":
        cursor = ctx.top + (ctx.avail_h - total) / 2.0
    elif anchor == "bottom":
        cursor = ctx.bottom - total
    else:
        cursor = float(ctx.top)
    for run, (h, p) in zip(lines, boxes):
        cursor += p
        run["y"] = round(cursor + run["size"] * BASELINE_FRAC, 2)
        cursor += h
        # ``_pad`` is intentionally *kept*: a layout runs _stack -> _fit_scale ->
        # _stack, and the authoritative second _stack (plus _fit_scale's vertical
        # total) must still see the inter-tier pad. _stack rebuilds every y from
        # ``cursor`` on each call, so keeping the key never double-applies it.


def _fit_scale(runs: list[dict[str, Any]], ctx: TypeContext,
               scrolls: bool = False) -> float:
    """Uniform scale so the block fits the safe box — preserves modular ratios.

    Returns the factor applied (``1.0`` if it already fit). Horizontal fit keeps
    the widest line inside the safe width; vertical fit (skipped for scrolling
    layouts) keeps the stack inside the safe height. One factor multiplies every
    size, so the hierarchy's ratios are untouched.
    """
    if not runs:
        return 1.0
    # Horizontal fit is *anchor-aware*: a run's budget is the safe distance on
    # the side its anchor grows toward, not the full safe width. A middle-anchored
    # centred run and a start-anchored run at the safe left both yield budget ==
    # avail_w (so those layouts are unchanged), but an off-centre run — a credits
    # label/value anchored at ``center_x ± gap`` — is measured against the real
    # distance to its safe edge, so it is scaled down before it can overflow.
    h_scale = 1.0
    for r in runs:
        w = ctx.estimate_width(r["text"], r["size"])
        x = r["x"]
        align = r["align"]
        if align == "end":
            budget = x - ctx.left
        elif align == "start":
            budget = ctx.right - x
        else:  # middle
            budget = 2.0 * min(x - ctx.left, ctx.right - x)
        budget = max(1.0, budget)
        if w > budget:
            h_scale = min(h_scale, budget / w)
    v_scale = 1.0
    if not scrolls:
        total_h = sum(_line_height(r["size"], ctx) + r.get("_pad", 0.0) for r in runs)
        if total_h > ctx.avail_h:
            v_scale = ctx.avail_h / total_h
    scale = min(1.0, h_scale, v_scale)
    # Legibility floor is applied to the *block*, not per tier: lift the single
    # uniform factor so the smallest run lands on MIN_SIZE (accepting controlled
    # overflow on tiny canvases). A per-run ``max(MIN_SIZE, …)`` would clamp each
    # tier independently and collapse the hierarchy's ratios to 1.0.
    smallest = min(r["size"] for r in runs)
    if smallest > 0.0:
        scale = max(scale, MIN_SIZE / smallest)
    if scale != 1.0:
        for r in runs:
            r["size"] = round(r["size"] * scale, 1)
            if "_pad" in r:
                r["_pad"] = round(r["_pad"] * scale, 2)
    return round(scale, 4)


def _color(ctx: TypeContext, role: str) -> tuple[str, float]:
    """(colour, fill-opacity) for a hierarchy role — the third contrast channel.

    Primary tiers sit at full opacity on ``text``; secondary tiers step down to
    ``subtext`` at reduced opacity; accent hues carry role labels. The gaps are
    wide enough that :func:`~lumenframe.kinetic.typography` hierarchy always
    reads as three distinct levels.
    """
    p = ctx.palette
    table = {
        "title": (p["text"], 1.0),
        "subtitle": (p["subtext"], 0.9),
        "attribution": (p["subtext"], 0.7),
        "name": (p["text"], 1.0),
        "role": (p["accent"], 0.95),
        "quote": (p["text"], 1.0),
        "lyric": (p["text"], 1.0),
        "bullet": (p["text"], 0.92),
        "caption": (p["text"], 0.95),
        "credit_label": (p["subtext"], 0.8),
        "credit_value": (p["text"], 1.0),
        "mark": (p["accent"], 1.0),
    }
    return table.get(role, (p["text"], 1.0))


# ── layouts (Registry vocabulary) ─────────────────────────────────────────────
#
# Each layout is a pure function ``(ctx, data, rng) -> list[run]``. It reads the
# normalised ``data`` for its role, derives every size from ``ctx`` (the modular
# scale), assigns the weight ladder + colour tiers, stacks the block inside the
# safe box and fit-scales it. A layout never invents a size or a colour.


def _tiered(ctx: TypeContext, tiers: list[tuple[str, str, int, float]]
            ) -> list[dict[str, Any]]:
    """Build wrapped, weight-laddered runs for a list of hierarchy tiers.

    ``tiers`` items are ``(role, text, scale_step, weight_bump)`` most-prominent
    first. Text wraps to the measure; each tier's weight is laddered against the
    tier above it, and its colour/opacity comes from the role table — so all
    three hierarchy channels are set here, once.
    """
    weights = enforce_weight_ladder(
        [font_weight(ctx.axes.axis("weight"), bump) for _, _, _, bump in tiers])
    runs: list[dict[str, Any]] = []
    for (role, text, step, _), weight in zip(tiers, weights):
        if not str(text).strip():
            continue
        size = ctx.size(step)
        color, fop = _color(ctx, role)
        first = True
        for line in wrap_text(text, ctx.max_chars):
            r = _run(role, line, ctx.center_x, 0.0, size, weight, ctx,
                     color, fop, "middle")
            if runs and first:
                r["_pad"] = round(size * 0.55, 2)   # open air above a new tier
            first = False
            runs.append(r)
    return runs


@LAYOUTS.verb("title_card", summary="centered hero title with optional subtitle")
def title_card(ctx: TypeContext, data: dict[str, Any], rng: Any) -> list[dict[str, Any]]:
    tiers = [("title", data.get("title", ""), 3, +0.15)]
    if str(data.get("subtitle") or "").strip():
        tiers.append(("subtitle", data["subtitle"], 0, -0.2))
    runs = _tiered(ctx, tiers)
    _stack(runs, ctx, "center")
    _fit_scale(runs, ctx)
    _stack(runs, ctx, "center")
    return runs


@LAYOUTS.verb("lower_third", summary="name + role in the bottom-left title-safe area")
def lower_third(ctx: TypeContext, data: dict[str, Any], rng: Any) -> list[dict[str, Any]]:
    name = data.get("name") or data.get("title") or ""
    role = data.get("role") or data.get("subtitle") or ""
    weights = enforce_weight_ladder([
        font_weight(ctx.axes.axis("weight"), +0.1),
        font_weight(ctx.axes.axis("weight"), -0.25)])
    runs: list[dict[str, Any]] = []
    for (rl, text, step), weight in zip(
            [("name", name, 1), ("role", role, -1)], weights):
        if not str(text).strip():
            continue
        size = ctx.size(step)
        color, fop = _color(ctx, rl)
        line = wrap_text(text, ctx.max_chars)[0] if str(text).strip() else ""
        r = _run(rl, line, ctx.left, 0.0, size, weight, ctx, color, fop, "start")
        if runs:
            r["_pad"] = round(size * 0.35, 2)
        runs.append(r)
    _stack(runs, ctx, "bottom")
    _fit_scale(runs, ctx)
    _stack(runs, ctx, "bottom")
    return runs


@LAYOUTS.verb("quote", summary="large pull-quote with a smaller attribution")
def quote(ctx: TypeContext, data: dict[str, Any], rng: Any) -> list[dict[str, Any]]:
    body = data.get("quote") or data.get("title") or ""
    attribution = data.get("attribution") or data.get("subtitle") or ""
    tiers = [("quote", body, 2, +0.05)]
    if str(attribution).strip():
        tiers.append(("attribution", attribution, -1, -0.25))
    runs = _tiered(ctx, tiers)
    _stack(runs, ctx, "center")
    _fit_scale(runs, ctx)
    _stack(runs, ctx, "center")
    return runs


@LAYOUTS.verb("kinetic_lyric", summary="one phrase, word-by-word, centered big")
def kinetic_lyric(ctx: TypeContext, data: dict[str, Any], rng: Any) -> list[dict[str, Any]]:
    phrase = data.get("phrase") or data.get("title") or ""
    weight = font_weight(ctx.axes.axis("weight"), +0.05)
    size = ctx.size(3)
    color, fop = _color(ctx, "lyric")
    runs = [_run("lyric", line, ctx.center_x, 0.0, size, weight, ctx,
                 color, fop, "middle") for line in wrap_text(phrase, ctx.max_chars)]
    for r in runs[1:]:
        r["_pad"] = round(size * 0.1, 2)
    _stack(runs, ctx, "center")
    _fit_scale(runs, ctx)
    _stack(runs, ctx, "center")
    return runs


@LAYOUTS.verb("list_reveal", summary="stacked bullet lines, left-aligned")
def list_reveal(ctx: TypeContext, data: dict[str, Any], rng: Any) -> list[dict[str, Any]]:
    bullets = [b for b in (data.get("bullets") or data.get("lines") or []) if str(b).strip()]
    weight = font_weight(ctx.axes.axis("weight"), 0.0)
    size = ctx.size(1)
    color, fop = _color(ctx, "bullet")
    runs: list[dict[str, Any]] = []
    for b in bullets:
        line = wrap_text(f"— {b}", ctx.max_chars)[0]
        r = _run("bullet", line, ctx.left, 0.0, size, weight, ctx, color, fop, "start")
        if runs:
            r["_pad"] = round(size * 0.25, 2)
        runs.append(r)
    _stack(runs, ctx, "center")
    _fit_scale(runs, ctx)
    _stack(runs, ctx, "center")
    return runs


@LAYOUTS.verb("caption", summary="subtitle line in the lower title-safe band")
def caption(ctx: TypeContext, data: dict[str, Any], rng: Any) -> list[dict[str, Any]]:
    text = data.get("caption") or data.get("title") or ""
    weight = font_weight(ctx.axes.axis("weight"), -0.05)
    size = ctx.size(0)
    color, fop = _color(ctx, "caption")
    runs = [_run("caption", line, ctx.center_x, 0.0, size, weight, ctx,
                 color, fop, "middle") for line in wrap_text(text, ctx.max_chars)]
    _stack(runs, ctx, "bottom")
    _fit_scale(runs, ctx)
    _stack(runs, ctx, "bottom")
    return runs


@LAYOUTS.verb("credits_roll", summary="scrolling label/value pairs (name — role)")
def credits_roll(ctx: TypeContext, data: dict[str, Any], rng: Any) -> list[dict[str, Any]]:
    pairs = data.get("pairs") or []
    weights = enforce_weight_ladder([
        font_weight(ctx.axes.axis("weight"), +0.05),
        font_weight(ctx.axes.axis("weight"), -0.1)])
    val_w, lab_w = weights
    size = ctx.size(0)
    gap = round(size * 0.4, 2)
    lab_c, lab_o = _color(ctx, "credit_label")
    val_c, val_o = _color(ctx, "credit_value")
    lh = _line_height(size, ctx)
    runs: list[dict[str, Any]] = []
    cursor = float(ctx.top)
    for label, value in pairs:
        y = round(cursor + size * BASELINE_FRAC, 2)
        runs.append(_run("credit_label", str(label), ctx.center_x - gap, y,
                         size, lab_w, ctx, lab_c, lab_o, "end"))
        runs.append(_run("credit_value", str(value), ctx.center_x + gap, y,
                         size, val_w, ctx, val_c, val_o, "start"))
        cursor += lh
    _fit_scale(runs, ctx, scrolls=True)
    return runs


# ── reveals (Registry vocabulary) ─────────────────────────────────────────────
#
# A reveal's *shape* (keyframes) lives in render; here it is declared as data:
# which unit it staggers over ("run" whole line, "word", or "char"), which
# non-linear easing it rides, and the keyframe id render emits. Registering them
# keeps the catalog honest.

def _reveal(unit: str, easing: str, keyframe: str) -> dict[str, str]:
    return {"unit": unit, "easing": easing, "keyframe": keyframe}


@REVEALS.verb("per_word", summary="each word fades up in sequence",
              unit="word", easing="out_quint", keyframe="ktFade")
def per_word() -> dict[str, str]:
    return _reveal("word", "out_quint", "ktFade")


@REVEALS.verb("per_line", summary="each line rises and fades as a block",
              unit="run", easing="out_quint", keyframe="ktRise")
def per_line() -> dict[str, str]:
    return _reveal("run", "out_quint", "ktRise")


@REVEALS.verb("typewriter", summary="characters appear one at a time",
              unit="char", easing="out_quad", keyframe="ktFade")
def typewriter() -> dict[str, str]:
    return _reveal("char", "out_quad", "ktFade")


@REVEALS.verb("mask_wipe", summary="a hard wipe uncovers the line left-to-right",
              unit="run", easing="out_expo", keyframe="ktWipe")
def mask_wipe() -> dict[str, str]:
    return _reveal("run", "out_expo", "ktWipe")


@REVEALS.verb("rise_fade", summary="the line rises a little as it fades in",
              unit="run", easing="out_quint", keyframe="ktRise")
def rise_fade() -> dict[str, str]:
    return _reveal("run", "out_quint", "ktRise")


@REVEALS.verb("scale_pop", summary="the line pops up from small with overshoot",
              unit="run", easing="back", keyframe="ktPop")
def scale_pop() -> dict[str, str]:
    return _reveal("run", "back", "ktPop")


def reveal_meta(name: str) -> dict[str, str]:
    """The declared spec for a reveal name (raises on unknown — see Registry)."""
    return REVEALS.require(name)()


# ── reveal rhythm (derived from pace, never per call) ─────────────────────────


def stagger_seconds(pace: float) -> float:
    """Seconds between consecutive units — fast pace ⇒ tight stagger."""
    return round(remap(clamp01(pace), 0.34, 0.08), 4)


def unit_duration(pace: float) -> float:
    """How long a single unit's reveal runs — fast pace ⇒ snappier."""
    return round(remap(clamp01(pace), 0.9, 0.42), 4)


def _unit_count(run: dict[str, Any], unit: str) -> int:
    text = str(run["text"])
    if unit == "word":
        return max(1, len(text.split()))
    if unit == "char":
        return max(1, len(text.replace(" ", "")))
    return 1


def assign_reveals(runs: list[dict[str, Any]], reveal_name: str, pace: float,
                   duration: float) -> dict[str, Any]:
    """Attach a reveal track to every run; return the rhythm summary.

    Runs enter in order (title before subtitle …). Within a word/char reveal the
    units cascade by ``stagger``; across runs there's a smaller overlapifying
    gap so the block feels choreographed, not queued. If the sequence would run
    past the clip it's compressed uniformly — the rhythm shape is preserved, the
    reveal simply plays faster.
    """
    meta = reveal_meta(reveal_name)
    unit = meta["unit"]
    easing = meta["easing"]
    keyframe = meta["keyframe"]
    stagger = stagger_seconds(pace)
    dur = unit_duration(pace)
    char_stagger = round(stagger * 0.4, 4)

    intro = 0.12
    t = intro
    for run in runs:
        n = _unit_count(run, unit)
        step = char_stagger if unit == "char" else stagger
        run["reveal"] = {
            "kind": reveal_name, "unit": unit, "keyframe": keyframe,
            "easing": easing, "ease_curve": EASINGS[easing],
            "base_delay": round(t, 4), "dur": dur, "unit_stagger": step,
        }
        span = dur + max(0, n - 1) * step
        t += span * 0.6 + stagger  # overlap successive runs for flow

    end = t
    # Compress if we overran the clip (leave a hold tail of ~12%).
    budget = duration * 0.88
    if end > budget and end > 0:
        factor = budget / end
        for run in runs:
            rv = run["reveal"]
            rv["base_delay"] = round(rv["base_delay"] * factor, 4)
            rv["dur"] = round(rv["dur"] * factor, 4)
            rv["unit_stagger"] = round(rv["unit_stagger"] * factor, 4)
        end = budget
    return {"reveal": reveal_name, "unit": unit, "stagger": stagger,
            "unit_duration": dur, "sequence_end": round(end, 4),
            "compressed": end >= budget}
