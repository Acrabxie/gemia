"""Reveal family — how elements come into being.

Verbs: ``draw_on`` (strokes draw themselves), ``fade_in`` (soft emergence),
``grow`` (scale pop from nothing), ``unfold`` (rotate-and-settle like petals),
``rise`` (lift from below with a settle).

Every verb staggers its targets with the level's stagger spread and eases
with the level's curve set — the same verb *feels* playful or luxurious purely
through the resolved parameters, which is the whole point of the layer split.
"""
from __future__ import annotations

import random
from typing import Any, Sequence

from lumenframe.vector import motion
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
    band: str = "enter",
    pattern: str = "sequential",
    rng: random.Random | None = None,
) -> list[tuple[Node, float, float]]:
    """Per-node (node, t0, t1) inside the window: stagger + a duration band.

    The stagger claims ``level.stagger_spread`` of the window; each node's
    move then takes the band duration (tempo-scaled), clamped so the last
    starter still finishes inside the window.
    """
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


@behavior("reveal.draw_on", family="reveal",
          summary="strokes draw themselves in, fills fade up after")
def draw_on(scene: dict, nodes: Sequence[Node], window: Window,
            level: ResolvedParams, rng: random.Random) -> None:
    for node, s, e in _slots(nodes, window, level, band="draw"):
        if node.get("kind") == "particles":
            continue  # draw-on has no meaning for instanced dots
        has_stroke = bool((node.get("style") or {}).get("stroke"))
        has_fill = bool((node.get("style") or {}).get("fill"))
        if has_stroke and node.get("kind") == "path":
            # A draw-on is 0→1 progress: an overshoot ease (playful's
            # "dramatic") would drive stroke-dashoffset below zero and flash a
            # gap at the path seam, so force a monotonic ease.
            motion.draw_on(node, s, e, ease=motion.safe_progress_ease(level.ease_move))
            if has_fill:
                # Fill arrives once the line work is mostly there — on its own
                # alpha channel so the drawing stroke stays visible throughout.
                fs = s + (e - s) * 0.6
                vscene.add_track(node, "fill_opacity", [
                    vscene.kf(fs, 0.0, level.ease_enter), vscene.kf(e, 1.0),
                ])
        else:
            # Text/groups/fill-only paths: emergence reads as a fast fade.
            motion.fade(node, s, e, ease=level.ease_enter)


@behavior("reveal.fade_in", family="reveal",
          summary="soft opacity emergence, barely any travel")
def fade_in(scene: dict, nodes: Sequence[Node], window: Window,
            level: ResolvedParams, rng: random.Random) -> None:
    drift = 12.0 * (1.0 - level.axes["elegance"] * 0.5)
    for node, s, e in _slots(nodes, window, level):
        motion.fade(node, s, e, ease=level.ease_enter)
        if drift > 1.0 and node.get("kind") != "particles":
            motion.move_by(node, s, e, dy=drift, ease=level.ease_enter)


@behavior("reveal.grow", family="reveal",
          summary="scale up from nothing; overshoot follows playfulness")
def grow(scene: dict, nodes: Sequence[Node], window: Window,
         level: ResolvedParams, rng: random.Random) -> None:
    pattern = "center_out" if len(nodes) > 2 else "sequential"
    for node, s, e in _slots(nodes, window, level, pattern=pattern, rng=rng):
        if node.get("kind") == "particles":
            continue
        if level.overshoot > 0.02:
            motion.scale_pop(node, s, e, overshoot=level.overshoot, ease=level.ease_enter)
        else:
            motion.scale_between(node, s, e, start=0.0, end=1.0, ease=level.ease_enter)
        motion.fade(node, s, s + (e - s) * 0.5, ease=level.ease_enter)


@behavior("reveal.unfold", family="reveal",
          summary="rotate-and-settle into place, like petals opening")
def unfold(scene: dict, nodes: Sequence[Node], window: Window,
           level: ResolvedParams, rng: random.Random) -> None:
    swing = 24.0 + 48.0 * level.axes["playfulness"]
    for i, (node, s, e) in enumerate(_slots(nodes, window, level)):
        if node.get("kind") == "particles":
            continue
        sign = -1.0 if i % 2 else 1.0
        motion.rotate_between(node, s, e, start=sign * swing, end=0.0,
                              ease=level.ease_emphasis)
        motion.scale_between(node, s, e, start=0.6, end=1.0, ease=level.ease_enter)
        motion.fade(node, s, s + (e - s) * 0.4, ease=level.ease_enter)


@behavior("reveal.rise", family="reveal",
          summary="lift from below and settle; travel scales with energy")
def rise(scene: dict, nodes: Sequence[Node], window: Window,
         level: ResolvedParams, rng: random.Random) -> None:
    height = float(scene.get("height") or 1080)
    travel = height * (0.04 + 0.08 * level.axes["energy"])
    for node, s, e in _slots(nodes, window, level):
        if node.get("kind") == "particles":
            continue
        motion.move_by(node, s, e, dy=travel, ease=level.ease_enter)
        motion.fade(node, s, s + (e - s) * 0.7, ease=level.ease_enter)
        if level.overshoot > 0.08:
            # A small dip past zero and back — the playful settle.
            over = travel * level.overshoot * 0.4
            mid = s + (e - s) * 0.62
            motion.oscillate(node, "y", mid, e, center=0.0, amplitude=-over,
                             cycles=1.0, ease="soft", decay=0.6)
