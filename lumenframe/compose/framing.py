"""The taste floor — grids, anchors, and the reframe geometry itself.

This is where composition becomes *hard to get wrong*. The agent says
"golden, facing right, airy"; this module decides the exact crop window so that:

* the **primary subject's eye-line** (upper third of its bbox) lands on a
  thirds / golden **anchor** — never dead centre unless the framing is
  ``centered``;
* **headroom** shrinks with ``tightness`` but the head is *never* cropped;
* **lead room** — if the subject faces a direction, more empty space is left in
  front of it (a right-facing subject is placed on the *left* anchor);
* a **horizon**, when the brief implies one, snaps onto a third (never the
  middle) as long as that keeps the subject in frame — subject containment is
  the hard floor, so the horizon drifts off its third rather than crop the head,
  and ``horizon_line`` then reports the fraction it truly lands on;
* **secondary mass** is balanced toward the opposite third;
* the crop stays inside the source and holds the delivered aspect ratio, with
  **safe margins**.

All of this is deterministic: geometry decides, and the seeded rng only breaks a
genuine tie (two equally-weighted subjects, or no reason to prefer left over
right). The grid lattice is a small catalogued vocabulary (:class:`Registry`) so
the overlay and the anchor maths can never silently diverge.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from lumenframe.craft import Registry, clamp01

#: The composition grid vocabulary. Each verb returns a :class:`GridSpec` — the
#: guide lines *and* the anchor lattice — so render + placement share one truth.
GRIDS = Registry("compose grids")

#: 1/phi ≈ 0.618; the golden section lines sit at 1-1/phi and 1/phi.
_PHI_INV = 2.0 / (1.0 + math.sqrt(5.0))  # 0.6180339887…
_G_LO = round(1.0 - _PHI_INV, 6)          # 0.381966…
_G_HI = round(_PHI_INV, 6)                # 0.618034…


@dataclass(frozen=True)
class GridSpec:
    """A composition lattice in crop-relative coordinates (0..1, y grows down).

    ``v`` / ``h`` are the vertical / horizontal guide-line positions; ``low`` /
    ``high`` name the two salient lines an anchor is chosen from (they coincide
    for ``center``). ``spiral`` is an optional golden-spiral flag for the
    overlay.
    """

    kind: str
    v: tuple[float, ...]
    h: tuple[float, ...]
    spiral: bool = False

    @property
    def x_lines(self) -> tuple[float, float]:
        """The (low, high) vertical lines to place a subject horizontally on."""
        if len(self.v) == 1:
            return self.v[0], self.v[0]
        return self.v[0], self.v[-1]

    @property
    def y_lines(self) -> tuple[float, float]:
        if len(self.h) == 1:
            return self.h[0], self.h[0]
        return self.h[0], self.h[-1]


@GRIDS.verb("thirds", summary="3×3 rule-of-thirds grid; anchors at inner intersections")
def _grid_thirds() -> GridSpec:
    return GridSpec(kind="thirds", v=(1 / 3, 2 / 3), h=(1 / 3, 2 / 3))


@GRIDS.verb("golden", summary="phi grid at 0.382/0.618 with a golden spiral overlay")
def _grid_golden() -> GridSpec:
    return GridSpec(kind="golden", v=(_G_LO, _G_HI), h=(_G_LO, _G_HI), spiral=True)


@GRIDS.verb("center", summary="single central cross; symmetry anchor at the middle")
def _grid_center() -> GridSpec:
    return GridSpec(kind="center", v=(0.5,), h=(0.5,))


def grid_for(name: str) -> GridSpec:
    """Look up a grid by hint name, defaulting to thirds for anything unknown."""
    fn = GRIDS.get(name)
    return fn() if fn is not None else _grid_thirds()


# ── subjects ────────────────────────────────────────────────────────────────

#: Where a subject's visual weight sits vertically inside its bbox: the eye-line
#: is a third of the way down, the classic portrait anchor.
_EYE_LINE = 1.0 / 3.0
#: Fraction of the frame margin kept clear of every subject edge (safe area).
_SAFE = 0.04


@dataclass(frozen=True)
class Subject:
    """A normalised subject: bbox in source 0..1 plus optional weight/facing."""

    x: float
    y: float
    w: float
    h: float
    weight: float
    facing: str | None
    index: int

    @property
    def cx(self) -> float:
        return self.x + self.w / 2.0

    @property
    def area(self) -> float:
        return self.w * self.h

    def salient(self, centered: bool) -> tuple[float, float]:
        """The point the composition anchors on: bbox centre for a centred
        framing, otherwise the eye-line (horizontal centre, upper third)."""
        py = self.y + self.h * (0.5 if centered else _EYE_LINE)
        return self.cx, py


def normalise_subjects(raw: list[dict[str, Any]]) -> list[Subject]:
    """Validate + normalise a brief's ``subjects`` into :class:`Subject`s.

    A subject needs a 4-number ``bbox`` inside ``[0, 1]`` with positive size;
    anything else is a structurally unusable brief (the caller raises). The
    default weight is bbox area — a bigger subject reads as more important — so a
    lone subject and an un-weighted crowd both resolve without ceremony.
    """
    subjects: list[Subject] = []
    for i, s in enumerate(raw):
        if not isinstance(s, dict) or "bbox" not in s:
            raise ValueError(f"subject {i} must be an object with a 'bbox'")
        box = s.get("bbox")
        if not (isinstance(box, (list, tuple)) and len(box) == 4):
            raise ValueError(f"subject {i} bbox must be [x, y, w, h]")
        try:
            x, y, w, h = (float(v) for v in box)
        except (TypeError, ValueError):
            raise ValueError(f"subject {i} bbox must be four numbers")
        if w <= 0 or h <= 0:
            raise ValueError(f"subject {i} bbox width/height must be > 0")
        if x < 0 or y < 0 or x + w > 1.0000001 or y + h > 1.0000001:
            raise ValueError(f"subject {i} bbox must lie within the source 0..1")
        facing = s.get("facing")
        if facing is not None and facing not in ("left", "right", "up", "down"):
            raise ValueError(f"subject {i} facing must be left|right|up|down")
        weight = float(s.get("weight", w * h))
        subjects.append(Subject(x, y, w, h, weight, facing, i))
    return subjects


def choose_primary(subjects: list[Subject], rng) -> Subject:
    """Pick the hero subject deterministically.

    Ranked by weight, then area, then top-most, then left-most — a total order
    that ties only when two subjects are genuinely indistinguishable, in which
    case (and only then) the seeded rng decides, so equal briefs still vary
    across seeds while staying byte-stable per seed.
    """
    def key(s: Subject) -> tuple[float, float, float, float]:
        return (s.weight, s.area, -s.y, -s.x)

    best = max(key(s) for s in subjects)
    finalists = sorted((s for s in subjects if key(s) == best), key=lambda s: s.index)
    if len(finalists) == 1:
        return finalists[0]
    return finalists[rng.randrange(len(finalists))]


# ── the reframe ───────────────────────────────────────────────────────────


@dataclass
class Reframe:
    """The computed crop plus everything the recipe + overlay need to render."""

    crop: tuple[float, float, float, float]      # x, y, w, h in source 0..1
    scale: float
    anchor_ideal: tuple[float, float]            # the grid anchor we aimed for
    subject_anchor: tuple[float, float]          # where the eye-line actually landed
    fill: float
    grid: GridSpec
    primary: Subject
    subjects: list[Subject]
    horizon: float | None
    horizon_line: float | None                   # third the horizon snapped to
    notes: list[str]
    balance_note: str


def _fill_fraction(axes) -> float:
    """Target fraction of the frame the subject spans (before aspect fitting).

    Tightness fills the frame; negative_space empties it; tension crops in a
    touch for immediacy (a small, honest coefficient so tightness stays the lead
    voice). Clamped to a sane band so the subject is never larger than the frame
    and never a lonely speck the crop can't even bound. This single number is
    the whole tight↔wide feel.
    """
    fill = (0.30
            + 0.58 * axes.axis("tightness")
            - 0.30 * axes.axis("negative_space")
            + 0.10 * (axes.axis("tension") - 0.5))
    return clamp01_band(fill, 0.16, 0.9)


def clamp01_band(v: float, lo: float, hi: float) -> float:
    return min(max(v, lo), hi)


def _max_rect(r: float) -> tuple[float, float]:
    """Largest (w, h) of aspect ``r = w/h`` that fits the unit square."""
    if r >= 1.0:
        return 1.0, 1.0 / r
    return r, 1.0


def _size_crop(primary: Subject, axes, r: float, notes: list[str]) -> tuple[float, float, float, bool]:
    """Choose crop (w, h) in source units holding aspect ``r``, containing the
    primary with margin set by ``fill``. Returns (fw, fh, fill, capped)."""
    fill = _fill_fraction(axes)
    # The subject must occupy at most ``fill`` of each crop dimension, so the
    # crop is at least this large in each; aspect then couples them.
    fh_from_h = primary.h / fill
    fh_from_w = (primary.w / fill) / r          # width need, expressed as height
    fh = max(fh_from_h, fh_from_w)
    fw = r * fh
    # Never exceed the source: clamp to the largest same-aspect rect that fits.
    mw, mh = _max_rect(r)
    capped = False
    if fw > mw or fh > mh:
        fw, fh = mw, mh
        capped = True
        notes.append("crop capped to source bounds; subject fills the frame")
    return fw, fh, fill, capped


def _place_axis(target: float, size: float, lo_bound: float, hi_bound: float,
                src_max: float) -> tuple[float, float]:
    """Place a crop edge along one axis.

    ``target`` is the ideal top/left so the anchor lands on the grid;
    ``[lo_bound, hi_bound]`` is the interval that keeps the subject fully inside
    the crop (containment + no head-crop); it is then intersected with the
    source ``[0, src_max]``. Returns (edge, deviation) where deviation is how far
    we had to slide off the ideal to respect the constraints.
    """
    lo = max(0.0, lo_bound)
    hi = min(src_max, hi_bound)
    if lo > hi:                        # subject bigger than crop on this axis
        edge = clamp01_band(target, 0.0, src_max)
    else:
        edge = clamp01_band(target, lo, hi)
    return edge, edge - target


def compute_reframe(subjects: list[Subject], primary: Subject, axes, r: float,
                    grid: GridSpec, horizon: float | None, rng) -> Reframe:
    """The heart of the library: subjects + axes + grid → a tasteful crop."""
    notes: list[str] = []
    centered = grid.kind == "center"
    px, py = primary.salient(centered)

    fw, fh, fill, capped = _size_crop(primary, axes, r, notes)

    # ── horizontal anchor: lead room + secondary balance ──────────────────
    x_lo, x_hi = grid.x_lines
    arx, balance_note = _choose_x_anchor(primary, subjects, grid, x_lo, x_hi, rng)
    # ── vertical anchor: eye-line high, adjusted for up/down facing ────────
    y_lo, y_hi = grid.y_lines
    ary = _choose_y_anchor(primary, grid, y_lo, y_hi)

    # Ideal crop origin so the salient point lands on (arx, ary).
    tx = px - arx * fw
    ty = py - ary * fh

    # Containment interval: subject fully inside AND (vertically) head uncropped.
    cx, dx = _place_axis(tx, fw, primary.x + primary.w - fw + _SAFE * fw,
                         primary.x - _SAFE * fw, 1.0 - fw)
    # cy upper bound is the subject top (never crop the head), lower bound keeps
    # the subject's bottom inside the crop.
    cy_hi = primary.y                                   # head stays visible
    cy_lo = primary.y + primary.h - fh + _SAFE * fh     # feet stay inside
    cy, dy = _place_axis(ty, fh, cy_lo, cy_hi, 1.0 - fh)

    horizon_line: float | None = None
    if horizon is not None:
        # The horizon is the *ideal* vertical target, but subject containment
        # (head never cropped, subject fully inside) is a hard floor. Choose the
        # snapped third, express it as a crop origin, then run it through the
        # SAME containment machinery so the horizon drifts off its third only
        # when it genuinely conflicts with keeping the subject in frame.
        line = _horizon_line(horizon)
        ty_h = horizon - line * fh                      # horizon-preferred origin
        cy, dy = _place_axis(ty_h, fh, cy_lo, cy_hi, 1.0 - fh)
        # Recompute the fraction the horizon ACTUALLY sits at after clamping,
        # mirroring how sax/say report the landed anchor — never a faked third.
        actual = (horizon - cy) / fh if fh else line
        horizon_line = actual
        side = "upper" if line < 0.5 else "lower"
        if abs(actual - line) <= 1e-6:
            notes.append(f"horizon snapped to the {side} third")
        else:
            notes.append(
                f"horizon could not reach the {side} third without cropping the "
                f"subject; it sits at {round(actual, 3)} of the frame")

    if abs(dx) > 1e-4 or abs(dy) > 1e-4:
        notes.append("anchor nudged off the ideal line to keep the subject and "
                     "source margins intact")

    # Actual landed anchor (may differ from ideal after clamping).
    sax = (px - cx) / fw if fw else arx
    say = (py - cy) / fh if fh else ary
    scale = 1.0 / fh if fh else 1.0

    # Reconcile the human balance_note with where the subject ACTUALLY landed.
    # When the crop caps to the full source the subject can't be moved onto a
    # third, so a note still claiming "on the left third" would be false — the
    # docstring's "never dead centre unless centered" guarantee. Only overwrite
    # when the landed horizontal anchor really diverges from the intended one.
    if not centered and capped and abs(sax - arx) > 1e-3:
        balance_note = ("frame too tight to reframe: subject sits at "
                        f"{round(clamp01(sax), 3)} of the crop, intended lead "
                        "room not achievable")

    return Reframe(
        crop=(round(cx, 6), round(cy, 6), round(fw, 6), round(fh, 6)),
        scale=round(scale, 6),
        anchor_ideal=(round(arx, 6), round(ary, 6)),
        subject_anchor=(round(clamp01(sax), 6), round(clamp01(say), 6)),
        fill=round(fill, 6),
        grid=grid,
        primary=primary,
        subjects=subjects,
        horizon=horizon,
        horizon_line=horizon_line,
        notes=notes,
        balance_note=balance_note,
    )


def _choose_x_anchor(primary: Subject, subjects: list[Subject], grid: GridSpec,
                     x_lo: float, x_hi: float, rng) -> tuple[float, str]:
    """Horizontal anchor: facing lead-room wins; else balance the counter-mass.

    Returns the anchor x and a human ``balance_note``. A right-facing subject is
    placed on the *low* (left) line so the space it looks into opens ahead of
    it. With no facing, the primary is placed opposite the secondary mass so the
    other subjects fall toward the far third. Genuine ties break on the seed.
    """
    if grid.kind == "center":
        return 0.5, "symmetry: subject centred on the vertical axis"

    facing = primary.facing
    if facing == "right":
        return x_lo, "lead room ahead: subject on the left third, space to the right"
    if facing == "left":
        return x_hi, "lead room ahead: subject on the right third, space to the left"

    others = [s for s in subjects if s.index != primary.index]
    if others:
        # Weighted centroid of the secondary mass, relative to the primary.
        mass = sum(s.weight for s in others) or 1.0
        centroid = sum(s.cx * s.weight for s in others) / mass
        if centroid > primary.cx + 1e-6:
            return x_lo, "counter-mass balanced: subject left, secondary weight to the right"
        if centroid < primary.cx - 1e-6:
            return x_hi, "counter-mass balanced: subject right, secondary weight to the left"
        # Secondary mass sits atop the primary — no horizontal preference.
        pick = (x_lo, x_hi)[rng.randrange(2)]
        return pick, "secondary mass centred over subject; side chosen by seed"

    # Solo subject, no facing: default to the left third (deterministic).
    return x_lo, "solo subject on the left third; right side left open"


def _choose_y_anchor(primary: Subject, grid: GridSpec, y_lo: float, y_hi: float) -> float:
    """Vertical anchor: eye-line high by default; up/down facing adds room.

    A subject facing *up* is placed on the lower line (more sky above it); a
    subject facing *down* is placed on the upper line (more ground below). Centre
    grids anchor on the middle line.
    """
    if grid.kind == "center":
        return 0.5
    if primary.facing == "up":
        return y_hi
    if primary.facing == "down":
        return y_lo
    return y_lo  # eye-line on the upper third — the classic portrait height


def _horizon_line(horizon: float) -> float:
    """Choose the third of the crop the horizon should snap onto — never the
    middle.

    The midpoint between the two thirds is exactly 0.5, so "nearest third" is
    simply a split at 0.5; a horizon on the midline resolves to the UPPER third
    (a grounded, land-heavy frame) deterministically. Returns the chosen line
    fraction only; :func:`compute_reframe` clamps it into the subject
    containment interval so the head is never cropped, and reports the fraction
    the horizon actually lands on.
    """
    return 1 / 3 if horizon <= 0.5 else 2 / 3
