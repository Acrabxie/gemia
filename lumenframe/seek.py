"""SEEK — locate a lumenframe document to a time, plus a pixel-free state query.

Two complementary jobs, both built **only** on the existing read paths
(``model`` + ``timebase`` + ``compile``); this module never edits the doc and
never changes compile/render behaviour:

* :func:`frame_at` / :func:`seek` — *render* a single moment. ``seek`` compiles
  the document into a ``LayerStack`` **once** (the same compile + render path
  :mod:`lumenframe.preview` uses) and asks for exactly one frame. The returned
  RGBA array is **byte-identical** to
  ``compile_to_layer_stack(doc, resolver).render_frame(frame_at(doc, seconds))``
  because that is literally the call made under the hood.

* :func:`state_at` — answer "what is the timeline doing at time T?" **without
  rendering any pixels**. It reports which top-level layers are active and, for
  each, the layer-local frame and the *source* frame it samples (honouring a
  ``time_remap`` curve via :func:`lumenframe.model.eval_time_remap`, or a
  constant ``speed`` otherwise), plus opacity and transform. This mirrors the
  compiler's time math (``timebase`` quantisation, ``is_active`` gating, the
  ``time_map_fn`` source mapping) so the report agrees with what would actually
  be rendered, but it touches no compositor.

Robustness: out-of-range seconds clamp into the valid frame range, an empty /
``None`` document yields a single-frame stack and an empty ``layers`` list, and
nested compositions don't crash ``state_at`` (v1 reports top-level layers; a
``composition`` layer is reported as one active layer like any other).

ADD-ONLY: nothing here is imported by ``compile`` / ``model`` / ``ops``; it is a
pure consumer of their public surface.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional

from lumenframe import model, timebase
from lumenframe.compile import (
    Resolver,
    _lane_ordered_children,
    compile_to_layer_stack,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    import numpy as np


__all__ = ["frame_at", "seek", "state_at"]


# ── internal helpers ────────────────────────────────────────────────────


def _canvas_fps(norm_doc: dict[str, Any]) -> float:
    """The single timebase: ``canvas.fps`` of a *normalised* doc."""
    canvas = norm_doc.get("canvas") or {}
    return float(canvas.get("fps") or 0.0) or 0.0


def _total_frames(norm_doc: dict[str, Any], fps: float) -> int:
    """Mirror :func:`compile.compile_to_layer_stack`'s ``total_frames`` exactly.

    ``max(1, round(doc_duration * fps))`` — the same clamp the compiler applies,
    so frame indices computed here line up with the rendered stack.
    """
    duration = model.doc_duration(norm_doc)
    return max(1, int(round(duration * fps)))


def _clamp_frame(idx: int, total_frames: int) -> int:
    """Clamp ``idx`` into ``[0, total_frames - 1]`` (``total_frames`` is >= 1)."""
    if idx < 0:
        return 0
    last = total_frames - 1
    if idx > last:
        return last
    return idx


# ── frame_at ────────────────────────────────────────────────────────────


def frame_at(doc: dict[str, Any] | None, seconds: float) -> int:
    """Frame index for ``seconds`` on ``doc``'s timeline, clamped to range.

    Uses the single canonical timebase (``canvas.fps`` via
    :func:`lumenframe.timebase.to_frame`, i.e. ``int(round(seconds * fps))``)
    and clamps the result into ``[0, total_frames - 1]`` where ``total_frames``
    matches the compiled stack (``max(1, round(doc_duration * fps))``).

    Args:
        doc: A lumenframe document (or ``None`` / partial — it is normalised).
        seconds: A time on the document timeline. Out-of-range values clamp to
            the first / last frame rather than raising.

    Returns:
        An integer frame index always valid for ``render_frame``.
    """
    norm = model.normalize_doc(doc or {})
    fps = _canvas_fps(norm)
    total = _total_frames(norm, fps)
    raw = timebase.to_frame(float(seconds), fps)
    return _clamp_frame(raw, total)


# ── seek ────────────────────────────────────────────────────────────────


def seek(
    doc: dict[str, Any] | None,
    *,
    seconds: float | None = None,
    frame: int | None = None,
    resolver: "Resolver | None" = None,
    strict: bool = False,
) -> "np.ndarray":
    """Render the single frame at ``seconds`` (or an explicit ``frame``).

    The document is compiled into a ``LayerStack`` exactly once, then a single
    ``render_frame`` is issued — the same compile-once-then-render pattern as
    :mod:`lumenframe.preview`. The result is **byte-identical** to
    ``compile_to_layer_stack(doc, resolver=resolver).render_frame(idx)`` for the
    same (clamped) index, because that is exactly the call made here.

    Exactly one of ``seconds`` / ``frame`` must be given. ``seconds`` is mapped
    through :func:`frame_at` (timebase round + clamp); an explicit ``frame`` is
    coerced to int and clamped into ``[0, total_frames - 1]``.

    Args:
        doc: A lumenframe document (or ``None`` / partial).
        seconds: A time to seek to. Mutually exclusive with ``frame``.
        frame: An explicit frame index to seek to. Mutually exclusive with
            ``seconds``.
        resolver: Optional content resolver forwarded to the compile step.
        strict: Forwarded to the compile step; raise on unresolved content.

    Returns:
        A canvas-sized ``(height, width, 4)`` float32 RGBA frame.

    Raises:
        ValueError: if neither or both of ``seconds`` / ``frame`` are given.
    """
    if (seconds is None) == (frame is None):
        raise ValueError("seek() requires exactly one of seconds= or frame=")

    stack = compile_to_layer_stack(doc, resolver=resolver, strict=strict)
    total = int(stack.total_frames)

    if seconds is not None:
        idx = frame_at(doc, seconds)
    else:
        idx = _clamp_frame(int(frame), total)

    return stack.render_frame(idx)


# ── state_at ────────────────────────────────────────────────────────────


def _layer_source_frame(layer: dict[str, Any], local_frame: int, fps: float) -> int:
    """Source-local frame the layer samples at ``local_frame`` (output-local).

    Mirrors the compiler's source mapping:

    * With a ``time_remap`` curve this is exactly what ``compile``'s
      ``time_map_fn`` produces — ``max(0, to_frame(eval_time_remap(curve,
      out_sec) - source_in, fps))`` — so the report matches the rendered
      sample.
    * Otherwise it follows the constant-speed model: ``max(0, to_frame(
      source_in + speed * out_sec, fps))``. With the defaults (``speed == 1``,
      ``source_in == 0``) this reduces to ``local_frame`` — the value the
      backend feeds ``content_fn`` when no remap is wired.

    ``time_remap`` and ``speed`` are mutually exclusive in the model
    (``set_time_remap`` clears ``speed`` to 1.0), so this branch is faithful.
    """
    out_sec = timebase.to_seconds(int(local_frame), fps)
    source_in = model._as_float(layer.get("source_in"))

    remap = layer.get("time_remap")
    if isinstance(remap, dict) and remap.get("keyframes"):
        src_sec = model.eval_time_remap(remap, out_sec)
        return max(0, timebase.to_frame(src_sec - source_in, fps))

    speed = layer.get("speed")
    speed = 1.0 if speed is None else model._as_float(speed)
    return max(0, timebase.to_frame(source_in + speed * out_sec, fps))


def state_at(doc: dict[str, Any] | None, seconds: float) -> dict[str, Any]:
    """Describe the timeline at time ``seconds`` without rendering pixels.

    Reports which **top-level** layers are active and, for each, the data the
    compiler would use to place and sample it. "Active" matches the compiler's
    ``is_active`` gating exactly: a layer is active at frame ``f`` iff
    ``start_frame <= f < end_frame`` where ``start_frame = max(0,
    round(start * fps))`` and ``end_frame = max(start_frame + 1,
    round((start + duration) * fps))`` — i.e. the layer's ``[start,
    start + duration]`` placement covers the seeked time.

    Nested compositions don't crash: a ``composition`` layer is reported like
    any other top-level layer (its own children are not expanded in v1).

    Args:
        doc: A lumenframe document (or ``None`` / partial). Empty docs yield an
            empty ``layers`` list (still with a valid ``frame`` / ``time``).
        seconds: A time on the document timeline; out-of-range values clamp.

    Returns:
        A dict ``{time, frame, active_layer_ids, layers}`` where ``time`` is the
        clamped, frame-snapped seconds actually inspected, ``frame`` is the
        clamped frame index, ``active_layer_ids`` lists active layer ids in
        composite (bottom -> top) order — the SAME lane-aware order ``compile``
        stacks them in (``_lane_ordered_children``: higher ``lane`` = later /
        on top, ties keep tree order; default lane 0 == plain tree order) — and
        ``layers`` is a list of per-active-layer dicts ``{id, local_frame,
        source_frame, opacity, transform}`` in that same order.
    """
    norm = model.normalize_doc(doc or {})
    fps = _canvas_fps(norm)
    total = _total_frames(norm, fps)

    frame = _clamp_frame(timebase.to_frame(float(seconds), fps), total)
    time_snapped = timebase.to_seconds(frame, fps)

    root = norm.get("root") or {}
    # Report layers in the SAME z-order ``compile`` composites them in: lanes are
    # stacked tracks, so children are ordered by ``(lane, tree-index)`` via
    # ``_lane_ordered_children`` (the exact stable sort ``_populate_stack`` runs
    # before assigning z) — a higher lane composites ABOVE (later / on top). With
    # the default lane 0 everywhere this stable sort is the identity permutation,
    # so the reported order stays pure tree order (bottom -> top) as before.
    children = _lane_ordered_children(root.get("children") or [])

    active_layer_ids: list[str] = []
    layers: list[dict[str, Any]] = []

    for layer in children:
        if not isinstance(layer, dict):
            continue
        if not layer.get("visible", True):
            continue

        start = model._as_float(layer.get("start"))
        duration = model._as_float(layer.get("duration"))
        start_frame = max(0, int(round(start * fps)))
        end_frame = max(start_frame + 1, int(round((start + duration) * fps)))

        if not (start_frame <= frame < end_frame):
            continue

        local_frame = frame - start_frame
        source_frame = _layer_source_frame(layer, local_frame, fps)
        transform = {**model.DEFAULT_TRANSFORM, **(layer.get("transform") or {})}

        lid = str(layer.get("id"))
        active_layer_ids.append(lid)
        layers.append(
            {
                "id": lid,
                "local_frame": local_frame,
                "source_frame": source_frame,
                "opacity": float(layer.get("opacity", 1.0)),
                "transform": transform,
            }
        )

    return {
        "time": time_snapped,
        "frame": frame,
        "active_layer_ids": active_layer_ids,
        "layers": layers,
    }
