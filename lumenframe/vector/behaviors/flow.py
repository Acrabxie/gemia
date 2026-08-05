"""Flow family — continuous, living motion (loops and organic movement).

Verbs: ``wave`` (travelling vertical undulation), ``breathe`` (slow scale
pulse), ``liquid`` (blob morph cycle), ``drift`` (slow wandering), ``orbit``
(circular travel around the resting point).

Flow verbs are cycle-shaped: they start AND end at the resting state so a
scene can hold, loop, or hand off to an exit cleanly. Amplitudes follow
``energy``; irregularity follows ``organicness`` (via the seeded rng).
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


def _cycles(window: Window, level: ResolvedParams) -> float:
    """How many full cycles fit the window at this tempo (>= 1)."""
    span = window[1] - window[0]
    cycle = motion.band_duration("cycle", tempo=level.tempo, available=span)
    return max(1.0, round(span / cycle, 2))


@behavior("flow.wave", family="flow",
          summary="travelling undulation; members bob with phase offsets")
def wave(scene: dict, nodes: Sequence[Node], window: Window,
         level: ResolvedParams, rng: random.Random) -> None:
    t0, t1 = window
    height = float(scene.get("height") or 1080)
    amp = height * (0.008 + 0.03 * level.axes["energy"])
    cycles = _cycles(window, level)
    delays = stagger_delays(len(nodes), pattern="sequential")
    for node, phase in zip(nodes, delays):
        if node.get("kind") == "particles":
            for i, inst in enumerate((node.get("particles") or {}).get("instances") or []):
                p = (phase + i / max(1, len(node["particles"]["instances"]))) % 1.0
                _inst_osc(inst, "y", t0, t1, amp=amp * (0.6 + 0.8 * rng.random()),
                          cycles=cycles, phase=p)
            continue
        motion.oscillate(node, "y", t0, t1, center=0.0,
                         amplitude=amp * (1.0 + level.wobble * (rng.random() - 0.5)),
                         cycles=cycles, ease="soft", phase=phase)


def _inst_osc(inst: dict, prop: str, t0: float, t1: float, *, amp: float,
              cycles: float, phase: float) -> None:
    """Instance-level oscillation (instances share the node keyframe shape)."""
    motion.oscillate(inst, prop, t0, t1, center=0.0, amplitude=amp,
                     cycles=cycles, ease="soft", phase=phase)


@behavior("flow.breathe", family="flow",
          summary="slow scale pulse around rest — the quiet-alive look")
def breathe(scene: dict, nodes: Sequence[Node], window: Window,
            level: ResolvedParams, rng: random.Random) -> None:
    t0, t1 = window
    depth = 0.015 + 0.05 * level.axes["energy"]
    cycles = max(1.0, _cycles(window, level) * 0.5)  # breathing is slower
    for i, node in enumerate(nodes):
        if node.get("kind") == "particles":
            continue
        motion.oscillate(node, "scale", t0, t1, center=1.0,
                         amplitude=depth * (1.0 + 0.3 * level.wobble * (rng.random() - 0.5)),
                         cycles=cycles, ease="soft", phase=(i * 0.17) % 1.0)


@behavior("flow.liquid", family="flow",
          summary="organic blob morph cycle (path nodes reshape and return)",
          kinds=("path",))
def liquid(scene: dict, nodes: Sequence[Node], window: Window,
           level: ResolvedParams, rng: random.Random) -> None:
    t0, t1 = window
    for node in nodes:
        if node.get("kind") != "path":
            continue
        base = [tuple(s) for s in node["path"]]
        x0, y0, x1, y1 = geometry.bbox(base)
        center = ((x0 + x1) / 2.0, (y0 + y1) / 2.0)
        radius = max(x1 - x0, y1 - y0) / 2.0 or 1.0
        wob = 0.1 + 0.4 * level.wobble
        lobes = 5 + int(level.axes["complexity"] * 4)
        # Two intermediate liquid states, then home — a full cycle.
        alt1 = geometry.blob(center, radius, wobble=wob, lobes=lobes, rng=rng)
        alt2 = geometry.blob(center, radius, wobble=wob, lobes=lobes, rng=rng)
        n = max(len(list(geometry.iter_cubics(base))),
                len(list(geometry.iter_cubics(alt1))),
                len(list(geometry.iter_cubics(alt2))), 8)
        home = geometry.resample_path(geometry.bake_close(base), n)
        a1 = geometry.resample_path(geometry.bake_close(alt1), n)
        a2 = geometry.resample_path(geometry.bake_close(alt2), n)
        third = (t1 - t0) / 3.0
        vscene.add_track(node, "d", [
            vscene.kf(t0, home, "soft"),
            vscene.kf(t0 + third, a1, "soft"),
            vscene.kf(t0 + 2 * third, a2, "soft"),
            vscene.kf(t1, home),
        ])


@behavior("flow.drift", family="flow",
          summary="slow seeded wander around rest; returns home")
def drift(scene: dict, nodes: Sequence[Node], window: Window,
          level: ResolvedParams, rng: random.Random) -> None:
    t0, t1 = window
    width = float(scene.get("width") or 1920)
    radius = width * (0.004 + 0.02 * level.axes["energy"])
    for node in nodes:
        targets = node.get("particles", {}).get("instances", []) \
            if node.get("kind") == "particles" else [node]
        for i, target in enumerate(targets):
            cycles = 1.0 + rng.random() * (1.0 + level.wobble)
            motion.oscillate(target, "x", t0, t1, center=0.0,
                             amplitude=radius * (0.5 + rng.random()),
                             cycles=cycles, ease="soft", phase=rng.random())
            motion.oscillate(target, "y", t0, t1, center=0.0,
                             amplitude=radius * (0.5 + rng.random()),
                             cycles=cycles * 0.8, ease="soft", phase=rng.random())


@behavior("flow.orbit", family="flow",
          summary="circular travel around the resting point, baked to keyframes")
def orbit(scene: dict, nodes: Sequence[Node], window: Window,
          level: ResolvedParams, rng: random.Random) -> None:
    t0, t1 = window
    width = float(scene.get("width") or 1920)
    radius = width * (0.01 + 0.03 * level.axes["energy"])
    steps = 8  # per revolution — CSS interpolates the arcs smoothly enough
    cycles = _cycles(window, level)
    total = max(4, int(steps * cycles))
    span = t1 - t0
    for k, node in enumerate(nodes):
        if node.get("kind") == "particles":
            continue
        direction = 1.0 if k % 2 == 0 else -1.0
        phase0 = rng.random() * 2.0 * math.pi
        xs, ys = [], []
        for i in range(total + 1):
            ang = phase0 + direction * 2.0 * math.pi * cycles * (i / total)
            # Start/end at the resting point by scaling the radius envelope
            # up from 0 and back down at the tail.
            env = min(1.0, i / (total * 0.15), (total - i) / (total * 0.15)) if total else 0.0
            xs.append(math.cos(ang) * radius * env)
            ys.append(math.sin(ang) * radius * env)
        from lumenframe.vector import scene as vscene
        for prop, vals in (("x", xs), ("y", ys)):
            vscene.add_track(node, prop, [
                vscene.kf(t0 + span * (i / total), round(v, 3), "linear")
                for i, v in enumerate(vals)
            ])
