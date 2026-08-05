"""Transform family — one thing becomes another.

Verbs: ``morph`` (each path becomes its builder-declared target shape),
``reshape`` (one strong organic excursion and back — punctuation, not a
loop), ``spin_swap`` (spin out, spin back in — reads as a card flip),
``crossfade`` (paired handoff: outgoing members dissolve, the rest arrive).

Unlike flow, transform verbs are one-way statements: they end in a NEW
state (morph, crossfade) or land back home after a single deliberate
excursion (reshape, spin_swap). Morph targets travel with the nodes —
builders stash them in ``meta.morph_to`` / ``meta.morph_fill`` — so the
verbs stay generic over what is becoming what.
"""
from __future__ import annotations

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
    band: str = "morph",
    moves: float = 1.0,
) -> list[tuple[Node, float, float]]:
    """Per-node (node, t0, t1) inside the window: stagger + a duration band.

    ``moves`` is how many sequential sub-moves the verb performs in one slot
    (an out-and-back excursion is two) — the band budget is granted per move
    so the whole gesture still fits after the stagger lead.
    """
    t0, t1 = window
    span = t1 - t0
    delays = stagger_delays(len(nodes), pattern="sequential")
    lead = span * min(level.stagger_spread, 0.75)
    dur = motion.band_duration(band, tempo=level.tempo,
                               available=(span - lead) / moves) * moves
    out = []
    for node, delay in zip(nodes, delays):
        s = t0 + lead * delay
        out.append((node, round(s, 6), round(min(s + dur, t1), 6)))
    return out


@behavior("transform.morph", family="transform",
          summary="each path morphs into its builder-declared target shape",
          kinds=("path",))
def morph(scene: dict, nodes: Sequence[Node], window: Window,
          level: ResolvedParams, rng: random.Random) -> None:
    for node, s, e in _slots(nodes, window, level, band="morph"):
        target = (node.get("meta") or {}).get("morph_to")
        if node.get("kind") != "path" or not target:
            continue  # builders opt nodes in by stashing a target path
        # align_for_morph resamples both ends to ONE shared cubic count, so
        # the "d" track interpolates command-for-command with no popping.
        home, dest = geometry.align_for_morph(
            [tuple(seg) for seg in node["path"]],
            [tuple(seg) for seg in target],
        )
        vscene.add_track(node, "d", [
            vscene.kf(s, home, level.ease_move),
            vscene.kf(e, dest),
        ])
        fill_to = (node.get("meta") or {}).get("morph_fill")
        fill_from = (node.get("style") or {}).get("fill")
        if fill_to and fill_from:
            motion.color_shift(node, s, e, prop="fill",
                               start=fill_from, end=str(fill_to),
                               ease=level.ease_move)


@behavior("transform.reshape", family="transform",
          summary="one strong organic reshape and back — an emphasis beat",
          kinds=("path",))
def reshape(scene: dict, nodes: Sequence[Node], window: Window,
            level: ResolvedParams, rng: random.Random) -> None:
    wob = 0.15 + 0.45 * level.wobble  # deliberately stronger than flow.liquid
    for node, s, e in _slots(nodes, window, level, band="morph", moves=2.0):
        if node.get("kind") != "path":
            continue
        base = [tuple(seg) for seg in node["path"]]
        x0, y0, x1, y1 = geometry.bbox(base)
        center = ((x0 + x1) / 2.0, (y0 + y1) / 2.0)
        radius = max(x1 - x0, y1 - y0) / 2.0 or 1.0
        lobes = 5 + int(level.axes["complexity"] * 4)
        alt = geometry.blob(center, radius, wobble=wob, lobes=lobes, rng=rng)
        # One shared segment count for every state in the track.
        n = max(len(list(geometry.iter_cubics(base))),
                len(list(geometry.iter_cubics(alt))), 8)
        home = geometry.resample_path(geometry.bake_close(base), n)
        peak = geometry.resample_path(geometry.bake_close(alt), n)
        mid = s + (e - s) * 0.5
        vscene.add_track(node, "d", [
            vscene.kf(s, home, level.ease_emphasis),
            vscene.kf(mid, peak, level.ease_emphasis),
            vscene.kf(e, home),
        ])


@behavior("transform.spin_swap", family="transform",
          summary="spin out, spin back in — reads as a card flip swap")
def spin_swap(scene: dict, nodes: Sequence[Node], window: Window,
              level: ResolvedParams, rng: random.Random) -> None:
    for i, (node, s, e) in enumerate(_slots(nodes, window, level,
                                            band="emphasis", moves=2.0)):
        if node.get("kind") == "particles":
            continue
        sign = -1.0 if i % 2 else 1.0
        half = (e - s) * 0.48
        flip = e - half
        # Out: accelerate away and vanish by the midpoint.
        motion.rotate_between(node, s, s + half, start=0.0, end=90.0 * sign,
                              ease=level.ease_exit)
        motion.scale_between(node, s, s + half, start=1.0, end=0.6,
                             ease=level.ease_exit)
        motion.fade(node, s, s + half, start=1.0, end=0.0, ease=level.ease_exit)
        # In: the SAME node returns from the opposite side. The rotation
        # rewinds +90→-90 during the tiny gap while opacity holds at 0,
        # so the discontinuity is never visible — that hidden turn is what
        # sells the flip.
        motion.rotate_between(node, flip, e, start=-90.0 * sign, end=0.0,
                              ease=level.ease_emphasis)
        motion.scale_between(node, flip, e, start=0.6, end=1.0,
                             ease=level.ease_enter)
        motion.fade(node, flip, e, start=0.0, end=1.0, ease=level.ease_enter)


@behavior("transform.crossfade", family="transform",
          summary="paired handoff — outgoing dissolves while the rest arrive")
def crossfade(scene: dict, nodes: Sequence[Node], window: Window,
              level: ResolvedParams, rng: random.Random) -> None:
    t0, t1 = window
    span = t1 - t0
    outgoing: list[Node] = []
    incoming: list[Node] = []
    for node in nodes:
        if node.get("kind") == "particles":
            continue
        role = (node.get("meta") or {}).get("role")
        (outgoing if role == "outgoing" else incoming).append(node)
    settle = 0.04 + 0.06 * level.axes["energy"]
    # Out claims the first 60%, in the last 60% — the middle fifth overlaps
    # so the canvas is never empty during the handoff.
    for node, s, e in _slots(outgoing, (t0, t0 + span * 0.6), level, band="exit"):
        motion.fade(node, s, e, start=1.0, end=0.0, ease=level.ease_exit)
        motion.scale_between(node, s, e, start=1.0, end=1.0 - settle,
                             ease=level.ease_exit)
    for node, s, e in _slots(incoming, (t1 - span * 0.6, t1), level, band="enter"):
        # Anchor invisibility at the window start so arrivals cannot flash
        # before their turn.
        motion.fade(node, t0, s, start=0.0, end=0.0, ease="hold")
        motion.fade(node, s, e, start=0.0, end=1.0, ease=level.ease_enter)
        motion.scale_between(node, s, e, start=1.0 - settle, end=1.0,
                             ease=level.ease_enter)
