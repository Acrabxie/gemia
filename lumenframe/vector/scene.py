"""The VectorScene IR — a renderer-agnostic scene graph for vector motion.

This is the contract every layer of the engine speaks:

* **behaviours** write animation *tracks* onto nodes,
* **the SVG compiler** reads nodes + tracks and emits an animated document,
* **future renderers** (native lumenframe layers, Lottie, WebGL) read the
  same structure.

Like :mod:`lumenframe.model`, everything is a plain JSON-serialisable dict —
no classes to unpickle, byte-stable round-trips, easy to diff, easy for an
agent to inspect. Factories below build well-formed nodes; consumers must
tolerate extra keys (they are preserved, never dropped).

Scene shape
-----------
::

    {
      "kind": "vector_scene", "version": 1,
      "width": 1920, "height": 1080, "duration": 5.0,
      "background": "#0A0E14" | None,      # None == transparent
      "seed": 7,
      "nodes": [node, ...],                 # paint order: first = back
      "meta": {...}                         # plan / provenance, free-form
    }

Node shape (kinds: ``path`` | ``text`` | ``group`` | ``particles``)
------------------------------------------------------------------
::

    {
      "id": "n1", "kind": "path", "name": "Mark",
      "path": [("M",x,y), ...],             # kind=path: geometry.Path
      "text": {...},                        # kind=text: content + font
      "children": [...],                    # kind=group
      "particles": {...},                   # kind=particles: instances
      "style": {"fill": "#5FC6DE", "stroke": None, "stroke_width": 0,
                 "opacity": 1.0, "line_cap": "round", "line_join": "round"},
      "transform": {"x": 0, "y": 0, "scale": 1.0, "rotation": 0.0},
      "tracks": {prop: [keyframe, ...]},    # written by behaviours
      "meta": {...}
    }

Coordinates: the scene uses **canvas-centred** coordinates, matching
lumenframe's transform convention — ``(0, 0)`` is dead centre, ``x`` grows
right, ``y`` grows down. The SVG compiler translates to top-left space.

Tracks
------
``tracks`` maps an animatable property to a keyframe list. Property
vocabulary (:data:`TRACK_PROPS`):

* ``opacity``      — 0..1
* ``x`` / ``y``    — px offset added to ``transform.x/y``
* ``scale``        — multiplies ``transform.scale``
* ``rotation``     — degrees added to ``transform.rotation``
* ``draw``         — stroke draw-on progress 0..1 (0 = invisible, 1 = full)
* ``d``            — path morph; value is a geometry ``Path``
* ``fill`` / ``stroke`` — colour ("#rrggbb")
* ``fill_opacity`` / ``stroke_opacity`` — paint alpha, independent of opacity

A keyframe is ``{"t": seconds, "value": Any, "ease": ease_token}`` where
``ease`` names how the segment *arriving* at the NEXT keyframe curves
(CSS-style: the ease sits on the departing keyframe). Ease tokens live in
:mod:`lumenframe.vector.motion`.
"""
from __future__ import annotations

import copy
import json
import math
import threading
from typing import Any, Iterator

from lumenframe.vector import geometry

#: Node kinds understood by the compilers.
NODE_KINDS: set[str] = {"path", "text", "group", "particles"}

#: Animatable track properties. ``fill_opacity``/``stroke_opacity`` animate
#: paint alpha independently of node ``opacity`` (draw-on strokes stay visible
#: while a fill arrives later).
TRACK_PROPS: set[str] = {
    "opacity", "x", "y", "scale", "rotation", "draw", "d", "fill", "stroke",
    "fill_opacity", "stroke_opacity",
}

#: Time rounding, mirroring lumenframe.model.TIME_NDIGITS.
TIME_NDIGITS = 6

DEFAULT_STYLE: dict[str, Any] = {
    "fill": "#FFFFFF",
    "stroke": None,
    "stroke_width": 0.0,
    "opacity": 1.0,
    "line_cap": "round",
    "line_join": "round",
}

DEFAULT_NODE_TRANSFORM: dict[str, float] = {
    "x": 0.0,
    "y": 0.0,
    "scale": 1.0,
    "rotation": 0.0,
}

# Per-THREAD id counter. gemia runs one event-loop thread per session
# (session_manager: ``lumeri-v3-<session_id>``), so two sessions building a
# scene concurrently must not share this counter — a global would interleave
# reset/increment across threads and emit duplicate ids / nondeterministic SVG
# for the same brief. threading.local gives each builder thread its own count,
# so ``build_scene`` stays deterministic per seed regardless of concurrency.
_ids = threading.local()


def _next_id(prefix: str) -> str:
    n = getattr(_ids, "counter", 0) + 1
    _ids.counter = n
    return f"{prefix}_{n}"


def reset_ids() -> None:
    """Reset this thread's node id counter (test isolation / reproducible builds)."""
    _ids.counter = 0


# ── factories ────────────────────────────────────────────────────────────


def new_scene(
    *,
    width: int = 1920,
    height: int = 1080,
    duration: float = 5.0,
    background: str | None = "#0A0E14",
    seed: int = 7,
) -> dict[str, Any]:
    return {
        "kind": "vector_scene",
        "version": 1,
        "width": int(width),
        "height": int(height),
        "duration": round(float(duration), TIME_NDIGITS),
        "background": background,
        "seed": int(seed),
        "nodes": [],
        "meta": {},
    }


def _base_node(kind: str, *, id: str | None = None, name: str = "", **extra: Any) -> dict[str, Any]:
    if kind not in NODE_KINDS:
        raise ValueError(f"unknown node kind {kind!r} (use {sorted(NODE_KINDS)})")
    node: dict[str, Any] = {
        "id": id or _next_id(kind),
        "kind": kind,
        "name": name or kind,
        "style": dict(DEFAULT_STYLE),
        "transform": dict(DEFAULT_NODE_TRANSFORM),
        "tracks": {},
        "meta": {},
    }
    for key, value in extra.items():
        if key == "style" and isinstance(value, dict):
            node["style"] = {**DEFAULT_STYLE, **value}
        elif key == "transform" and isinstance(value, dict):
            node["transform"] = {**DEFAULT_NODE_TRANSFORM, **value}
        else:
            node[key] = value
    return node


def path_node(
    path: geometry.Path,
    *,
    id: str | None = None,
    name: str = "",
    style: dict[str, Any] | None = None,
    transform: dict[str, float] | None = None,
) -> dict[str, Any]:
    if not path:
        raise ValueError("path_node requires a non-empty path")
    return _base_node(
        "path", id=id, name=name or "Path",
        path=[tuple(seg) for seg in path],
        style=style or {}, transform=transform or {},
    )


def text_node(
    text: str,
    *,
    id: str | None = None,
    name: str = "",
    font_size: float = 96.0,
    font_family: str = "system-ui, -apple-system, 'SF Pro Display', 'Segoe UI', sans-serif",
    font_weight: str | int = 700,
    letter_spacing: float = 0.0,
    align: str = "center",
    style: dict[str, Any] | None = None,
    transform: dict[str, float] | None = None,
) -> dict[str, Any]:
    if not str(text):
        raise ValueError("text_node requires non-empty text")
    return _base_node(
        "text", id=id, name=name or "Text",
        text={
            "content": str(text),
            "font_size": float(font_size),
            "font_family": str(font_family),
            "font_weight": font_weight,
            "letter_spacing": float(letter_spacing),
            "align": align,
        },
        style=style or {}, transform=transform or {},
    )


def group_node(
    children: list[dict[str, Any]] | None = None,
    *,
    id: str | None = None,
    name: str = "",
    style: dict[str, Any] | None = None,
    transform: dict[str, float] | None = None,
) -> dict[str, Any]:
    return _base_node(
        "group", id=id, name=name or "Group",
        children=list(children or []),
        style=style or {}, transform=transform or {},
    )


def particles_node(
    instances: list[dict[str, Any]],
    *,
    id: str | None = None,
    name: str = "",
    style: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """A field of small instanced glyphs animated as one logical element.

    Each instance: ``{"x": px, "y": px, "r": px, "shape": "dot"|"spark",
    "delay": 0..1, "meta": {...}}`` — positions canvas-centred. Behaviours
    write *per-instance* tracks into ``instance["tracks"]`` using the same
    keyframe shape as node tracks; the compiler emits one tiny element per
    instance. ``delay`` is the instance's normalised stagger position, set
    by choreography.
    """
    return _base_node(
        "particles", id=id, name=name or "Particles",
        particles={"instances": [dict(i) for i in instances]},
        style=style or {},
    )


# ── keyframes / tracks ───────────────────────────────────────────────────


def kf(t: float, value: Any, ease: str = "linear") -> dict[str, Any]:
    """One keyframe. ``ease`` shapes the segment leaving this keyframe."""
    return {"t": round(float(t), TIME_NDIGITS), "value": value, "ease": str(ease)}


def add_track(node: dict[str, Any], prop: str, keyframes: list[dict[str, Any]]) -> None:
    """Set/extend a node's track for ``prop``, keeping keyframes t-sorted.

    Keyframes landing on an existing t (within 1e-6) replace it — so a
    behaviour can safely re-anchor the resting value.
    """
    if prop not in TRACK_PROPS:
        raise ValueError(f"unknown track prop {prop!r} (use {sorted(TRACK_PROPS)})")
    track = list(node.setdefault("tracks", {}).get(prop, []))
    for point in keyframes:
        t = round(float(point["t"]), TIME_NDIGITS)
        track = [p for p in track if abs(float(p["t"]) - t) > 1e-6]
        track.append({"t": t, "value": point["value"], "ease": str(point.get("ease", "linear"))})
    track.sort(key=lambda p: float(p["t"]))
    node["tracks"][prop] = track


def track_span(node: dict[str, Any]) -> tuple[float, float] | None:
    """(first, last) keyframe time across all of a node's tracks, or None."""
    times: list[float] = []
    for points in (node.get("tracks") or {}).values():
        times.extend(float(p["t"]) for p in points)
    inst = (node.get("particles") or {}).get("instances") if node.get("kind") == "particles" else None
    for i in inst or []:
        for points in (i.get("tracks") or {}).values():
            times.extend(float(p["t"]) for p in points)
    if not times:
        return None
    return (min(times), max(times))


# ── traversal / lookup ───────────────────────────────────────────────────


def walk(scene_or_node: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """Every node in paint order (groups before their children)."""
    nodes = scene_or_node.get("nodes")
    if nodes is None:
        yield scene_or_node
        nodes = scene_or_node.get("children") or []
    for node in nodes:
        if isinstance(node, dict):
            yield node
            for sub in node.get("children") or []:
                if isinstance(sub, dict):
                    yield from walk({"nodes": [sub]})


def find_node(scene: dict[str, Any], node_id: str) -> dict[str, Any] | None:
    for node in walk(scene):
        if str(node.get("id")) == str(node_id):
            return node
    return None


def add_node(scene: dict[str, Any], node: dict[str, Any]) -> dict[str, Any]:
    scene.setdefault("nodes", []).append(node)
    return node


def clone_scene(scene: dict[str, Any]) -> dict[str, Any]:
    return copy.deepcopy(scene)


# ── validation ───────────────────────────────────────────────────────────


class SceneError(ValueError):
    """Raised when a scene/node is structurally unusable."""


def validate_scene(scene: dict[str, Any]) -> None:
    """Cheap structural validation: raises :class:`SceneError` on problems."""
    if not isinstance(scene, dict) or scene.get("kind") != "vector_scene":
        raise SceneError("not a vector_scene dict")
    if float(scene.get("duration") or 0) <= 0:
        raise SceneError("scene duration must be > 0")
    if int(scene.get("width") or 0) <= 0 or int(scene.get("height") or 0) <= 0:
        raise SceneError("scene width/height must be > 0")
    seen: set[str] = set()
    duration = float(scene["duration"])
    for node in walk(scene):
        nid = str(node.get("id") or "")
        if not nid:
            raise SceneError("node without id")
        if nid in seen:
            raise SceneError(f"duplicate node id {nid!r}")
        seen.add(nid)
        kind = node.get("kind")
        if kind not in NODE_KINDS:
            raise SceneError(f"node {nid}: unknown kind {kind!r}")
        if kind == "path" and not node.get("path"):
            raise SceneError(f"node {nid}: path node without geometry")
        if kind == "text" and not (node.get("text") or {}).get("content"):
            raise SceneError(f"node {nid}: text node without content")
        for prop, points in (node.get("tracks") or {}).items():
            if prop not in TRACK_PROPS:
                raise SceneError(f"node {nid}: unknown track prop {prop!r}")
            if not points:
                raise SceneError(f"node {nid}: empty track {prop!r}")
            last = -math.inf
            for p in points:
                t = float(p["t"])
                if t < -1e-9 or t > duration + 1e-9:
                    raise SceneError(
                        f"node {nid}: track {prop!r} keyframe t={t} outside 0..{duration}"
                    )
                if t < last - 1e-9:
                    raise SceneError(f"node {nid}: track {prop!r} not t-sorted")
                last = t


def scene_signature(scene: dict[str, Any]) -> str:
    """Stable JSON of the scene (sorted keys) — for caching / dedup / tests."""
    return json.dumps(scene, sort_keys=True, ensure_ascii=False, default=list)
