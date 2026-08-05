"""Assemble family — parts converge into a whole.

Verbs: ``gather`` (members travel home from outside the composition),
``magnetic`` (a slow drift, then a snap into place), ``converge`` (a
scattered particle cloud collapses onto its target shape), ``form`` (the
hero verb: particles swirl, then decisively lock into the mark).

Assemble verbs animate TOWARD rest: authored transforms stay the
destination and every offset track ends at zero, so the composition is
pixel-identical to its designed state the moment the behaviour finishes.
Travel scales with the canvas and ``energy``; arrival order is spatial —
centre lands first for members, edges first for particle fields — so the
whole reads as one object clicking shut, not a bag of tweens.
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


def _travel(scene: dict, level: ResolvedParams) -> float:
    """Displacement / scatter radius: canvas-proportional, energy-driven."""
    base = min(float(scene.get("width") or 1920), float(scene.get("height") or 1080))
    return base * (0.18 + 0.22 * level.axes["energy"])


def _outward(node: Node, rng: random.Random) -> tuple[float, float]:
    """Unit direction from canvas centre through the node's resting point,
    so displaced members frame the composition. A dead-centre node gets a
    seeded direction instead of a zero vector."""
    t = node.get("transform") or {}
    ux, uy = geometry.vnorm((float(t.get("x") or 0.0), float(t.get("y") or 0.0)))
    if ux == 0.0 and uy == 0.0:
        ang = rng.random() * 2.0 * math.pi
        return (math.cos(ang), math.sin(ang))
    return (ux, uy)


def _home_slots(
    nodes: Sequence[Node], window: Window, level: ResolvedParams,
    rng: random.Random,
) -> list[tuple[Node, float, float]]:
    """Per-node (node, t0, t1): centre-out stagger ranked by each node's
    resting position, plus an enter-band travel duration clamped so the
    last starter still finishes inside the window."""
    t0, t1 = window
    span = t1 - t0
    positions = [
        (float((n.get("transform") or {}).get("x") or 0.0),
         float((n.get("transform") or {}).get("y") or 0.0))
        for n in nodes
    ]
    delays = stagger_delays(len(nodes), pattern="center_out",
                            positions=positions, rng=rng)
    lead = span * min(level.stagger_spread, 0.75)
    dur = motion.band_duration("enter", tempo=level.tempo, available=span - lead)
    out = []
    for node, delay in zip(nodes, delays):
        s = t0 + lead * delay
        out.append((node, round(s, 6), round(min(s + dur, t1), 6)))
    return out


def _instance_delays(insts: Sequence[dict], rng: random.Random) -> list[float]:
    """A stored ``delay`` wins; otherwise edges-in over instance positions —
    the frame arrives first and the centre of the mark lands last."""
    positions = [(float(i.get("x") or 0.0), float(i.get("y") or 0.0)) for i in insts]
    fallback = stagger_delays(len(insts), pattern="edges_in",
                              positions=positions, rng=rng)
    return [float(i["delay"]) if i.get("delay") is not None else d
            for i, d in zip(insts, fallback)]


@behavior("assemble.gather", family="assemble",
          summary="members travel home from outside, centre landing first")
def gather(scene: dict, nodes: Sequence[Node], window: Window,
           level: ResolvedParams, rng: random.Random) -> None:
    travel = _travel(scene, level)
    for node, s, e in _home_slots(nodes, window, level, rng):
        if node.get("kind") == "particles":
            continue
        ux, uy = _outward(node, rng)
        d = travel * (1.0 + 0.3 * level.wobble * (rng.random() - 0.5))
        motion.move_by(node, s, e, dx=ux * d, dy=uy * d, ease=level.ease_enter)
        motion.fade(node, s, s + (e - s) * 0.4, ease=level.ease_enter)


def _snap_home(node: Node, prop: str, offset: float, s: float, e: float,
               level: ResolvedParams) -> None:
    """Two-stage approach on one axis: a slow crawl that covers little
    ground, then a swift snap — landing slightly past home when the level
    allows overshoot, with the last segment settling back."""
    if abs(offset) < 1e-9:
        return
    points = [vscene.kf(s, offset, "soft"),
              vscene.kf(s + (e - s) * 0.6, offset * 0.55, "swift")]
    if level.overshoot > 0.02:
        points.append(vscene.kf(s + (e - s) * 0.85,
                                -offset * level.overshoot * 0.15, "soft"))
    points.append(vscene.kf(e, 0.0))
    vscene.add_track(node, prop, points)


@behavior("assemble.magnetic", family="assemble",
          summary="slow drift toward home, then a magnetic snap into place")
def magnetic(scene: dict, nodes: Sequence[Node], window: Window,
             level: ResolvedParams, rng: random.Random) -> None:
    travel = _travel(scene, level)
    for node, s, e in _home_slots(nodes, window, level, rng):
        if node.get("kind") == "particles":
            continue
        ux, uy = _outward(node, rng)
        d = travel * (1.0 + 0.3 * level.wobble * (rng.random() - 0.5))
        _snap_home(node, "x", ux * d, s, e, level)
        _snap_home(node, "y", uy * d, s, e, level)
        motion.fade(node, s, s + (e - s) * 0.4, ease=level.ease_enter)


@behavior("assemble.converge", family="assemble",
          summary="scattered particle cloud collapses onto its target shape",
          kinds=("particles",))
def converge(scene: dict, nodes: Sequence[Node], window: Window,
             level: ResolvedParams, rng: random.Random) -> None:
    t0, t1 = window
    span = t1 - t0
    radius = _travel(scene, level)
    for node in nodes:
        insts = (node.get("particles") or {}).get("instances") or []
        if not insts:
            continue
        # Instance x/y ARE the targets: a disc of relative offsets means the
        # cloud starts as the mark blurred over the disc, then sharpens home.
        offsets = geometry.scatter(len(insts), rng, radius=radius)
        delays = _instance_delays(insts, rng)
        lead = span * min(level.stagger_spread, 0.75)
        dur = motion.band_duration("enter", tempo=level.tempo, available=span - lead)
        for inst, (ox, oy), delay in zip(insts, offsets, delays):
            s = round(t0 + lead * delay, 6)
            e = round(min(s + dur, t1), 6)
            motion.move_by(inst, s, e, dx=ox, dy=oy, ease=level.ease_enter)
            motion.fade(inst, s, e, ease=level.ease_enter)


@behavior("assemble.form", family="assemble",
          summary="particles swirl, then decisively lock into the mark",
          kinds=("particles",))
def form(scene: dict, nodes: Sequence[Node], window: Window,
         level: ResolvedParams, rng: random.Random) -> None:
    t0, t1 = window
    span = t1 - t0
    boundary = round(t0 + span * 0.55, 6)  # swirl phase → home-in phase
    radius = _travel(scene, level)
    swirl = radius * (0.2 + 0.3 * level.axes["energy"])
    for node in nodes:
        insts = (node.get("particles") or {}).get("instances") or []
        if not insts:
            continue
        offsets = geometry.scatter(len(insts), rng, radius=radius)
        delays = _instance_delays(insts, rng)
        lead = (t1 - boundary) * min(level.stagger_spread, 0.6)
        dur = motion.band_duration("burst", tempo=level.tempo,
                                   available=(t1 - boundary) - lead)
        for inst, (ox, oy), delay in zip(insts, offsets, delays):
            # Partial orbit around the scattered position: x/y in opposite
            # phase with unequal cycle counts, so paths curve, never shuttle
            # along one diagonal. Oscillation ends back at the offset.
            phase = rng.random()
            cycles = 0.75 + 0.5 * rng.random()
            amp = swirl * (0.6 + 0.8 * rng.random())
            motion.oscillate(inst, "x", t0, boundary, center=ox, amplitude=amp,
                             cycles=cycles, ease="soft", phase=phase)
            motion.oscillate(inst, "y", t0, boundary, center=oy, amplitude=amp,
                             cycles=cycles * 1.5, ease="soft",
                             phase=(phase + 0.5) % 1.0)
            s = round(boundary + lead * delay, 6)
            e = round(min(s + dur, t1), 6)
            # Decisive home-in: re-anchor the swirl's exit with a swift ease
            # so each particle leaves its hover in one clean snap.
            vscene.add_track(inst, "x", [vscene.kf(s, ox, "swift"), vscene.kf(e, 0.0)])
            vscene.add_track(inst, "y", [vscene.kf(s, oy, "swift"), vscene.kf(e, 0.0)])
            # Opacity holds at 0.85 through the swirl, pops to 1.0 on arrival
            # — the collective landing reads as the event, not the travel.
            vscene.add_track(inst, "opacity", [
                vscene.kf(t0, 0.0, level.ease_enter),
                vscene.kf(round(t0 + span * 0.12, 6), 0.85),
                vscene.kf(round(e - (e - s) * 0.3, 6), 0.85, "swift"),
                vscene.kf(e, 1.0),
            ])
