"""Explode family — element release, energy out.

Verbs: ``burst`` (targets fly outward from the collective centre, tumbling),
``scatter`` (particle fields drift apart on seeded headings), ``dissolve``
(the body fades up-and-out while its dust field flares and dies),
``energy_release`` (a radial pulse that rings the paint down).

Explode verbs end AWAY from rest — opacity lands at zero — so they belong in
exit/transition phases. All travel is proportional to the canvas diagonal;
all chaos (headings, tumbles, jitter) comes only from the seeded rng.
"""
from __future__ import annotations

import math
import random
from typing import Any, Sequence

from lumenframe.vector import geometry, motion
from lumenframe.vector import scene as vscene
from lumenframe.vector.behaviors import behavior
from lumenframe.vector.choreography import stagger_delays
from lumenframe.vector.params import ResolvedParams

Node = dict[str, Any]
Window = tuple[float, float]


def _slots(
    nodes: Sequence[Node],
    window: Window,
    level: ResolvedParams,
    *,
    band: str = "burst",
    pattern: str = "sequential",
    rng: random.Random | None = None,
) -> list[tuple[Node, float, float]]:
    """Per-node (node, t0, t1) inside the window: stagger + a duration band."""
    t0, t1 = window
    span = t1 - t0
    delays = stagger_delays(len(nodes), pattern=pattern, rng=rng)
    lead = span * min(level.stagger_spread, 0.75)
    dur = motion.band_duration(band, tempo=level.tempo, available=span - lead)
    out = []
    for node, delay in zip(nodes, delays):
        s = t0 + lead * delay
        out.append((node, round(s, 6), round(min(s + dur, t1), 6)))
    return out


def _centre(node: Node) -> tuple[float, float]:
    """Resting centre in canvas space: geometry for paths, the instance
    field's mean for particles, the transform origin otherwise."""
    if node.get("kind") == "path":
        return geometry.centroid(node["path"])
    if node.get("kind") == "particles":
        insts = (node.get("particles") or {}).get("instances") or []
        if insts:
            return (sum(float(i.get("x") or 0.0) for i in insts) / len(insts),
                    sum(float(i.get("y") or 0.0) for i in insts) / len(insts))
    t = node.get("transform") or {}
    return (float(t.get("x") or 0.0), float(t.get("y") or 0.0))


def _inst_delays(node: Node, rng: random.Random) -> list[tuple[dict, float]]:
    """(instance, normalised delay 0..1) — a baked ``delay`` wins; otherwise a
    seeded random stagger so a burst reads as chaos, not a sweep."""
    insts = (node.get("particles") or {}).get("instances") or []
    fallback: list[float] | None = None
    out = []
    for i, inst in enumerate(insts):
        d = inst.get("delay")
        if d is None:
            if fallback is None:
                fallback = stagger_delays(len(insts), pattern="random", rng=rng)
            d = fallback[i]
        out.append((inst, min(max(float(d), 0.0), 1.0)))
    return out


def _rng_heading(rng: random.Random) -> tuple[float, float]:
    ang = rng.random() * math.tau
    return (math.cos(ang), math.sin(ang))


@behavior("explode.burst", family="explode",
          summary="fly outward from the collective centre, tumbling and fading",
          kinds=("path", "text", "group", "particles"))
def burst(scene: dict, nodes: Sequence[Node], window: Window,
          level: ResolvedParams, rng: random.Random) -> None:
    diag = math.hypot(float(scene.get("width") or 1920), float(scene.get("height") or 1080))
    centres = [_centre(n) for n in nodes]
    cx = sum(c[0] for c in centres) / len(centres)
    cy = sum(c[1] for c in centres) / len(centres)
    travel = diag * (0.1 + 0.22 * level.axes["energy"])
    spin = 260.0 * level.axes["playfulness"]
    pattern = "center_out" if len(nodes) > 2 else "sequential"
    for (node, s, e), centre in zip(
            _slots(nodes, window, level, band="burst", pattern=pattern, rng=rng), centres):
        if node.get("kind") == "particles":
            _burst_field(node, centre, s, window[1], level, rng, diag)
            continue
        heading = geometry.vnorm((centre[0] - cx, centre[1] - cy))
        if heading == (0.0, 0.0):  # a lone node sits ON the centroid
            heading = _rng_heading(rng)
        motion.move_by(node, s, e, dx=heading[0] * travel, dy=heading[1] * travel,
                       ease=level.ease_exit, arrive=False)
        if spin > 1.0:
            sign = 1.0 if rng.random() < 0.5 else -1.0
            motion.rotate_between(node, s, e, start=0.0, end=sign * spin,
                                  ease=level.ease_exit)
        motion.fade(node, s + (e - s) * 0.55, e, start=1.0, end=0.0, ease=level.ease_exit)


def _burst_field(node: Node, centre: tuple[float, float], s0: float, t1: float,
                 level: ResolvedParams, rng: random.Random, diag: float) -> None:
    """Instances radiate from the node centre; the node slot sets the start,
    the instance stagger then spreads across the rest of the window."""
    span = t1 - s0
    lead = span * min(level.stagger_spread, 0.75)
    dur = motion.band_duration("burst", tempo=level.tempo, available=span - lead)
    for inst, d in _inst_delays(node, rng):
        s = s0 + lead * d
        e = min(s + dur, t1)
        heading = geometry.vnorm((float(inst.get("x") or 0.0) - centre[0],
                                  float(inst.get("y") or 0.0) - centre[1]))
        if heading == (0.0, 0.0):
            heading = _rng_heading(rng)
        dist = diag * (0.06 + 0.18 * level.axes["energy"]) * (0.6 + 0.8 * rng.random())
        motion.move_by(inst, s, e, dx=heading[0] * dist, dy=heading[1] * dist,
                       ease=level.ease_exit, arrive=False)
        motion.fade(inst, s + (e - s) * 0.4, e, start=1.0, end=0.0, ease=level.ease_exit)


@behavior("explode.scatter", family="explode",
          summary="instances drift apart on seeded headings, jittering and fading",
          kinds=("particles",))
def scatter(scene: dict, nodes: Sequence[Node], window: Window,
            level: ResolvedParams, rng: random.Random) -> None:
    t0, t1 = window
    diag = math.hypot(float(scene.get("width") or 1920), float(scene.get("height") or 1080))
    steps = 6  # baked samples per drift — enough for a wandering line
    for node in nodes:
        if node.get("kind") != "particles":
            continue
        span = t1 - t0
        lead = span * min(level.stagger_spread, 0.75)
        # Deliberately the long band: scatter is burst's soft, slow cousin.
        dur = motion.band_duration("cycle", tempo=level.tempo, available=span - lead)
        for inst, d in _inst_delays(node, rng):
            s = t0 + lead * d
            e = min(s + dur, t1)
            hx, hy = _rng_heading(rng)
            dist = diag * (0.04 + 0.1 * level.axes["energy"]) * (0.6 + 0.8 * rng.random())
            jitter = dist * 0.3 * level.wobble
            jcycles = 1.0 + rng.random() * 2.0
            xs, ys = [], []
            for i in range(steps + 1):
                u = i / steps
                p = motion.ease_value("soft", u) * dist
                j = math.sin(u * math.tau * jcycles) * jitter * (1.0 - u)
                xs.append(hx * p - hy * j)  # jitter rides the perpendicular
                ys.append(hy * p + hx * j)
            for prop, vals in (("x", xs), ("y", ys)):
                vscene.add_track(inst, prop, [
                    vscene.kf(s + (e - s) * (i / steps), round(v, 3), "linear")
                    for i, v in enumerate(vals)
                ])
            motion.fade(inst, s, e, start=1.0, end=0.0, ease="soft")


@behavior("explode.dissolve", family="explode",
          summary="body fades up-and-out while its dust field flares and dies",
          kinds=("path", "text", "particles"))
def dissolve(scene: dict, nodes: Sequence[Node], window: Window,
             level: ResolvedParams, rng: random.Random) -> None:
    t0, t1 = window
    diag = math.hypot(float(scene.get("width") or 1920), float(scene.get("height") or 1080))
    drift = diag * (0.006 + 0.015 * level.axes["energy"])
    solids = [n for n in nodes if n.get("kind") != "particles"]
    for node, s, e in _slots(solids, window, level, band="exit"):
        motion.fade(node, s, e, start=1.0, end=0.0, ease=level.ease_exit)
        motion.scale_between(node, s, e, start=1.0, end=1.06, ease=level.ease_exit)
    for node in nodes:
        if node.get("kind") != "particles":
            continue
        lead = (t1 - t0) * min(level.stagger_spread, 0.75)
        for inst, d in _inst_delays(node, rng):
            s = t0 + lead * d
            mid = s + (t1 - s) * 0.3
            # Dust flares in as the body dies, then hangs and fades exactly
            # with the window end.
            motion.fade(inst, s, mid, start=0.0, end=1.0, ease=level.ease_enter)
            motion.fade(inst, mid, t1, start=1.0, end=0.0, ease="soft")
            hx, hy = _rng_heading(rng)
            motion.move_by(inst, s, t1, dx=hx * drift, dy=hy * drift,
                           ease="soft", arrive=False)


@behavior("explode.energy_release", family="explode",
          summary="radial pulse: scale swells while stroke and paint ring down",
          kinds=("path", "group"))
def energy_release(scene: dict, nodes: Sequence[Node], window: Window,
                   level: ResolvedParams, rng: random.Random) -> None:
    peak = 1.1 + 0.3 * level.axes["energy"]
    for node, s, e in _slots(nodes, window, level, band="emphasis"):
        if node.get("kind") not in ("path", "group"):
            continue
        # The pulse IS the overshoot — "dramatic" here is the verb's identity,
        # not a taste decision, so it does not defer to ease_emphasis.
        motion.scale_between(node, s, e, start=1.0, end=peak, ease="dramatic")
        if (node.get("style") or {}).get("stroke"):
            vscene.add_track(node, "stroke_opacity", [
                vscene.kf(s, 1.0, level.ease_exit), vscene.kf(e, 0.0),
            ])
            if node.get("kind") == "path":
                # Fast draw-off so the line visibly retracts, not just dims.
                de = s + motion.band_duration("exit", tempo=level.tempo, available=e - s)
                motion.draw_on(node, s, min(de, e), ease=level.ease_exit, reverse=True)
        motion.fade(node, s, e, start=1.0, end=0.0, ease=level.ease_exit)
