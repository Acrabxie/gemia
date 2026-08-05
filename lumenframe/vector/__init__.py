"""lumenframe.vector — Lumeri's vector motion-design engine.

Not a clip-art generator: a **creative primitive**. Vector elements here are
living things — points, paths, bezier curves, shapes, text, particle fields —
each with position, velocity, lifecycle and form; animation is choreographed
*visual behaviour* (reveal / explode / assemble / flow / transform), not raw
keyframe pushing.

Layering (bottom → top):

* :mod:`lumenframe.vector.geometry`     — pure vector math: bezier, paths,
  shape generators, resampling/morph prep, deterministic sampling.
* :mod:`lumenframe.vector.scene`        — the renderer-agnostic **VectorScene
  IR**: a scene graph of vector nodes plus per-node animation tracks.
* :mod:`lumenframe.vector.motion`       — easing / duration / stagger tokens
  and the track-building toolkit behaviours are written in.
* :mod:`lumenframe.vector.behaviors`    — the high-level behaviour vocabulary
  (draw_on, burst, gather, wave, morph, …) grouped in five families:
  Reveal, Explode, Assemble, Flow, Transform.
* :mod:`lumenframe.vector.styles`       — motion-style archetypes (playful /
  minimal / luxury / tech) expressed as token sets, linked to the palette
  system in :mod:`lumenframe.templates.theme`.
* :mod:`lumenframe.vector.params`       — the semantic parameter system
  (energy, smoothness, playfulness, elegance, complexity, density,
  organicness ∈ [0, 1]) and its mapping onto low-level values.
* :mod:`lumenframe.vector.choreography` — composition intelligence: phase
  arcs (anticipate → reveal → hold), stagger patterns, focal ordering.
* :mod:`lumenframe.vector.svg`          — compiles a VectorScene to a
  self-contained animated SVG + CSS document.
* :mod:`lumenframe.vector.render`       — renderer adapters: the SVG document
  as an ``html`` layer (HyperFrames/Chrome, full fidelity) or degraded native
  lumenframe layers (no browser needed).
* :mod:`lumenframe.vector.api`          — the agent-facing brief: describe a
  scene in creative language, get a placed, renderable result; apply human
  feedback ("more playful", "less chaotic") as semantic deltas.

Everything is deterministic: any randomness flows from an explicit ``seed``,
so the same brief always renders the same pixels (and the html layer's
content-hash render cache actually hits).
"""
from __future__ import annotations

from lumenframe.vector.api import (  # noqa: F401
    adjust_scene,
    build_scene,
    scene_to_html_layer,
    scene_to_svg,
)

__all__ = [
    "build_scene",
    "adjust_scene",
    "scene_to_svg",
    "scene_to_html_layer",
]
