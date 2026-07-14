"""Compile a lumenframe document down to the render backend.

``compile_to_layer_stack(doc)`` turns the editable layer tree into a
:class:`gemia.video.layers.LayerStack` — the proven RGBA compositor that can
``render_frame`` / ``render_to_video``. This is what makes a document *real*:
the same doc the agent and UI edit becomes pixels.

Built-in content is produced here:

* ``solid``      — a flat canvas-sized colour fill (from ``props.color``)
* ``composition``— compiled recursively; the nested stack's ``render_frame``
  becomes the parent layer's content (precompose / nesting just works)

Everything else (``video`` / ``image`` / ``text`` / extension types) is produced
by a **resolver** you pass in: ``resolver(layer, ctx) -> content_fn | None``.
This keeps the core dependency-light and fully testable with solids, while real
media plugs in exactly where extensions and the asset pipeline live. A resolver
content_fn must return a **canvas-sized** RGBA frame (so transforms stay
centred); ``ctx`` carries ``width`` / ``height`` / ``fps`` / ``total_frames`` /
``assets``.

Mapped per layer: time (start/duration → frame range), z-order (tree order),
opacity, blend mode, centre-origin transform (translate + uniform scale +
rotation, computed analytically so rotation stays centred), opacity/position
keyframes, the per-layer effect chain, **shape masks** (rectangle / ellipse /
polygon / vector path, rasterised in normalised canvas coordinates with
optional feather + invert), **pixel masks** (inline alpha or image asset),
alpha/luma **track mattes**, and **adjustment layers** (After Effects
``composite-below``: the effect chain runs over the flat composite of every
layer beneath the adjustment in its comp, stacked adjustments compounding).

Known limitations (tracked): non-uniform scale collapses to ``scale_x``; anchor
is treated as centre; transform keyframes don't recouple the rotation bounding
box.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

import numpy as np

from lumenframe import model, timebase

ContentFn = Callable[[int], "np.ndarray"]
Resolver = Callable[[dict[str, Any], "ResolveContext"], Optional[ContentFn]]


class CompileError(RuntimeError):
    """Raised in strict mode when a layer's content cannot be produced."""


@dataclass
class ResolveContext:
    width: int
    height: int
    fps: float
    total_frames: int
    assets: list[dict[str, Any]]

    def asset(self, asset_id: str | None) -> dict[str, Any] | None:
        if not asset_id:
            return None
        for a in self.assets:
            if str(a.get("id")) == str(asset_id):
                return a
        return None


# ── entry point ─────────────────────────────────────────────────────────


def compile_to_layer_stack(
    doc: dict[str, Any] | None,
    *,
    resolver: Resolver | None = None,
    strict: bool = False,
):
    """Compile ``doc`` into a renderable ``LayerStack``.

    Args:
        doc: A lumenframe document (dict with canvas, root composition, assets).
        resolver: A callable(layer, ctx) -> content_fn | None. If None, uses
            the default_resolver which handles image/video/text from assets.
        strict: If True, raises CompileError when content cannot be produced.
            If False, skips layers with unresolved content.

    Returns:
        A LayerStack ready for render_frame() / render_to_video().
    """
    from gemia.video.layers import LayerStack  # local: pulls cv2/numpy/PIL

    # Use default resolver if none provided.
    if resolver is None:
        from lumenframe.resolve import default_resolver
        resolver = default_resolver

    norm = model.normalize_doc(doc or {})
    canvas = norm["canvas"]
    width, height = int(canvas["width"]), int(canvas["height"])
    fps = float(canvas["fps"])
    duration = model.doc_duration(norm)
    total_frames = max(1, int(round(duration * fps)))
    ctx = ResolveContext(width, height, fps, total_frames, norm.get("assets") or [])

    stack = LayerStack(width=width, height=height, fps=fps, total_frames=total_frames)
    _populate_stack(stack, norm["root"], ctx, resolver, strict)
    return stack


def _lane_ordered_children(children: list) -> list:
    """Order a comp's children by ``(lane, original-tree-index)`` as stacked tracks.

    Lanes act as parallel timeline tracks: a HIGHER ``lane`` composites ABOVE a
    lower lane, and WITHIN a lane the existing bottom->top tree order is kept.
    Implemented as a STABLE sort keyed only on ``lane`` (Python's ``sorted`` is
    stable, so equal-lane children keep their relative tree order — i.e. the
    original index is the implicit tie-break). A layer without an explicit/valid
    ``lane`` is treated as lane 0.

    CRITICAL: when every child has ``lane == 0`` (the default) this is the
    identity permutation, so the resulting z-order — and therefore every byte of
    the compiled render — is exactly what it is today (pure tree order).
    """
    def _lane(layer) -> int:
        if isinstance(layer, dict):
            try:
                return int(layer.get("lane") or 0)
            except (TypeError, ValueError):
                return 0
        return 0

    return sorted(children, key=_lane)


def _populate_stack(stack, comp: dict[str, Any], ctx: ResolveContext, resolver, strict) -> None:
    from gemia.video.layers import Layer

    # Lanes are real stacked tracks: order children by (lane, tree-index) before
    # anything reads tree order (z assignment below, adjustment composite-below
    # via children[:z], and track-matte sibling lookups all inherit this order).
    # Stable sort on lane => identity when all lane==0 => byte-identical default.
    children = _lane_ordered_children(comp.get("children") or [])
    # Resolve content_fn for every non-adjustment child first so track mattes can
    # borrow a sibling.
    resolved: dict[str, ContentFn] = {}
    matte_sources: set[str] = set()

    for layer in children:
        if isinstance(layer, dict):
            if str(layer.get("type", "")) != "adjustment":
                fn = _content_fn_for(layer, ctx, resolver, strict)
                if fn is not None:
                    resolved[str(layer.get("id"))] = fn
            mask = layer.get("mask")
            if isinstance(mask, dict) and str(mask.get("kind")) in {"alpha_matte", "luma_matte"}:
                if mask.get("source_layer_id"):
                    matte_sources.add(str(mask["source_layer_id"]))

    for z, layer in enumerate(children):
        if not isinstance(layer, dict):
            continue
        if not layer.get("visible", True):
            continue

        ltype = str(layer.get("type", ""))
        if ltype == "adjustment":
            # After Effects composite-below: an adjustment layer's effect chain runs
            # over the flat composite of EVERY layer beneath it in this comp — not
            # just the nearest one. We render that composite as a sub-stack
            # (recursively, so adjustments stacked above each other compound over the
            # cumulative result), then apply the adjustment's effects on top. The
            # adjustment is a real drawn layer: its own opacity / blend / time range /
            # mask govern how the effected composite lands back over the layers below
            # (full opacity + normal blend = a straight replacement, exactly like AE;
            # a shape mask localises the effect to a region).
            content_fn = _adjustment_content(children[:z], layer, ctx, resolver, strict)
            effects = layer.get("effects") or []
            if effects:
                content_fn = _wrap_with_effects(content_fn, effects, ctx)
        else:
            # A layer used only as a track matte feeds the mask, not the canvas.
            if str(layer.get("id")) in matte_sources:
                continue
            content_fn = resolved.get(str(layer.get("id")))
            if content_fn is None:
                continue  # nothing to draw (skipped / unresolved / null)

        start_frame = int(round(model._as_float(layer.get("start")) * ctx.fps))
        end_frame = int(round((model._as_float(layer.get("start")) + model._as_float(layer.get("duration"))) * ctx.fps))

        # Synthesise in/out transitions (stored by add_transition under
        # props.transitions) as a transient per-frame wrapper — the doc is never
        # mutated. The wrapper runs after the effect chain so a transition
        # modulates the final layer content.
        props = layer.get("props")
        if isinstance(props, dict):
            transitions = props.get("transitions")
            if isinstance(transitions, dict) and transitions:
                span = max(1, max(start_frame + 1, end_frame) - max(0, start_frame))
                content_fn = _wrap_with_transitions(content_fn, transitions, span, ctx.fps)

        transform = {**model.DEFAULT_TRANSFORM, **(layer.get("transform") or {})}
        scale = float(transform.get("scale_x", 1.0))
        rotation = float(transform.get("rotation", 0.0))
        position = _centred_position(ctx.width, ctx.height, scale, rotation,
                                     float(transform["x"]), float(transform["y"]))

        runtime = Layer(
            id=str(layer.get("id")),
            name=str(layer.get("name") or ""),
            start_frame=max(0, start_frame),
            end_frame=max(start_frame + 1, end_frame),
            z_index=z,
            blend_mode=_safe_blend(layer.get("blend_mode")),
            opacity=float(layer.get("opacity", 1.0)),
            scale=scale,
            rotation_deg=rotation,
            content_fn=content_fn,
            position=position,
            keyframes=_keyframe_tracks(layer, ctx.fps),
            # Pass expressions for time-driven property animation
            expressions=layer.get("expressions") or {},
        )
        matte = _matte_fn(layer, resolved, children, ctx)
        if matte is not None:
            runtime.mask_fn = matte
        time_map = _time_remap_fn(layer, ctx.fps)
        if time_map is not None:
            runtime.time_map_fn = time_map
        stack.add_layer(runtime)


def _adjustment_content(below_children, adj_layer, ctx: ResolveContext, resolver, strict) -> ContentFn:
    """Composite-below source for an adjustment layer.

    ``below_children`` are the comp children physically beneath the adjustment, in
    their original (parent-comp) timeline. They are populated into a sub-stack that
    shares the parent's frame timeline (recursively applying adjustment semantics,
    so a lower adjustment is already baked into this composite). The adjustment's
    effect chain is applied by the caller; here we only produce the flat backdrop.

    The Layer machinery hands ``content_fn`` a layer-local frame (absolute minus the
    adjustment's start), so we shift it back to the parent-absolute frame before
    sampling the sub-stack. NOTE: each adjustment re-renders everything below it, so
    deeply stacked adjustments cost O(n²) — acceptable, and inherent to the model.
    """
    from gemia.video.layers import LayerStack

    sub = LayerStack(width=ctx.width, height=ctx.height, fps=ctx.fps,
                     total_frames=ctx.total_frames)
    _populate_stack(sub, {"children": list(below_children)}, ctx, resolver, strict)
    adj_start = int(round(model._as_float(adj_layer.get("start")) * ctx.fps))
    last = max(0, ctx.total_frames - 1)

    def content_fn(local_frame: int):
        absolute = min(max(int(local_frame) + adj_start, 0), last)
        return np.asarray(sub.render_frame(absolute), dtype=np.float32).copy()

    return content_fn


# ── content producers ────────────────────────────────────────────────────


def _content_fn_for(layer: dict[str, Any], ctx: ResolveContext, resolver, strict) -> ContentFn | None:
    ltype = str(layer.get("type"))
    if ltype in {"adjustment", "null"}:
        return None  # no direct pixels in M1.1; adjustment layers are handled separately
    if ltype == "solid":
        fn = _solid_content(layer, ctx)
    elif ltype == "composition":
        fn = _composition_content(layer, ctx, resolver, strict)
    elif resolver is not None:
        fn = resolver(layer, ctx)
    else:
        fn = None

    if fn is None:
        if strict:
            raise CompileError(f"no content for layer {layer.get('id')} (type {ltype}); pass a resolver")
        return None

    # Wrap the content_fn to apply per-layer effects (the effect chain).
    effects = layer.get("effects") or []
    if effects:
        fn = _wrap_with_effects(fn, effects, ctx)

    return fn


def _solid_content(layer: dict[str, Any], ctx: ResolveContext) -> ContentFn:
    color = _rgba01(layer.get("props", {}).get("color"), default=(0.0, 0.0, 0.0, 1.0))
    frame = np.empty((ctx.height, ctx.width, 4), dtype=np.float32)
    frame[:] = np.array(color, dtype=np.float32)

    def content_fn(_local_frame: int):
        return frame.copy()

    return content_fn


def _composition_content(layer: dict[str, Any], ctx: ResolveContext, resolver, strict) -> ContentFn:
    from gemia.video.layers import LayerStack

    dur = model._as_float(layer.get("duration"))
    sub_total = max(1, int(round(dur * ctx.fps)))
    sub = LayerStack(width=ctx.width, height=ctx.height, fps=ctx.fps, total_frames=sub_total)
    sub_ctx = ResolveContext(ctx.width, ctx.height, ctx.fps, sub_total, ctx.assets)
    _populate_stack(sub, layer, sub_ctx, resolver, strict)

    def content_fn(local_frame: int):
        idx = min(max(int(local_frame), 0), sub_total - 1)
        return sub.render_frame(idx)

    return content_fn


def _matte_fn(layer, resolved, siblings, ctx) -> Callable[[int], "np.ndarray"] | None:
    mask = layer.get("mask")
    if not isinstance(mask, dict):
        return None
    kind = str(mask.get("kind"))
    if kind == "shape":
        # The mask travels with its layer's local time, so a path's default
        # duration is the *layer*'s span (in frames), not the comp's.
        dur = model._as_float(layer.get("duration"))
        layer_frames = int(round(dur * ctx.fps))
        return _shape_matte(mask, ctx, layer_frames=layer_frames)
    if kind == "pixel":
        return _pixel_matte(mask, ctx)
    if kind not in {"alpha_matte", "luma_matte"}:
        return None
    source_id = str(mask.get("source_layer_id") or "")
    source_fn = resolved.get(source_id)
    if source_fn is None:
        return None
    source_layer = next((s for s in siblings if str(s.get("id")) == source_id), None)
    masked_start = int(round(model._as_float(layer.get("start")) * ctx.fps))
    source_start = int(round(model._as_float((source_layer or {}).get("start")) * ctx.fps))
    invert = bool(mask.get("invert"))
    luma = kind == "luma_matte"

    def matte(local_frame: int):
        absolute = masked_start + int(local_frame)
        src_local = absolute - source_start
        content = source_fn(max(src_local, 0))
        rgba = np.asarray(content, dtype=np.float32)
        if luma:
            alpha = 0.299 * rgba[..., 0] + 0.587 * rgba[..., 1] + 0.114 * rgba[..., 2]
            alpha = alpha * rgba[..., 3]
        else:
            alpha = rgba[..., 3]
        return 1.0 - alpha if invert else alpha

    return matte


def _pixel_matte(mask: dict[str, Any], ctx: ResolveContext) -> Callable[[int], "np.ndarray"] | None:
    """A pixel/bitmap mask -> per-frame alpha.

    Supports two homes:
    * inline ``alpha`` / ``data``: a 2D numeric array in [0, 1] or [0, 255].
    * ``asset_id``: an image asset whose alpha/luma/r/g/b channel becomes alpha.

    The resulting alpha is canvas-sized and resolution-independent at render
    time: the backend resizes it again onto the transformed layer frame.
    """
    alpha = _pixel_mask_array(mask, ctx)
    if alpha is None:
        return None
    alpha = _postprocess_mask_alpha(alpha, mask, ctx.width, ctx.height)

    def matte(_local_frame: int):
        return alpha

    return matte


def _pixel_mask_array(mask: dict[str, Any], ctx: ResolveContext) -> "np.ndarray | None":
    raw = mask.get("alpha")
    if raw is None:
        raw = mask.get("data")
    if raw is not None:
        arr = np.asarray(raw, dtype=np.float32)
        if arr.ndim == 3:
            arr = arr[..., 0]
        if arr.ndim != 2 or arr.size == 0:
            return None
        if float(np.nanmax(arr)) > 1.0:
            arr = arr / 255.0
        return np.nan_to_num(arr, nan=0.0, posinf=1.0, neginf=0.0)

    asset_id = str(mask.get("asset_id") or "")
    asset = ctx.asset(asset_id)
    if not isinstance(asset, dict):
        return None
    path = asset.get("path") or asset.get("source_path") or asset.get("uri")
    if not path:
        return None
    path_obj = Path(str(path)).expanduser()
    if not path_obj.exists():
        return None

    from PIL import Image as PILImage

    rgba = np.asarray(PILImage.open(path_obj).convert("RGBA"), dtype=np.float32) / 255.0
    channel = str(mask.get("channel") or "alpha").lower()
    if channel in {"alpha", "a"}:
        return rgba[..., 3]
    if channel in {"red", "r"}:
        return rgba[..., 0]
    if channel in {"green", "g"}:
        return rgba[..., 1]
    if channel in {"blue", "b"}:
        return rgba[..., 2]
    # luma/default: useful for black/white mask plates.
    return 0.299 * rgba[..., 0] + 0.587 * rgba[..., 1] + 0.114 * rgba[..., 2]


def _postprocess_mask_alpha(alpha: "np.ndarray", mask: dict[str, Any], width: int, height: int) -> "np.ndarray":
    import cv2

    arr = np.asarray(alpha, dtype=np.float32)
    if arr.shape != (height, width):
        arr = cv2.resize(arr, (width, height), interpolation=cv2.INTER_LINEAR)
    arr = np.clip(arr, 0.0, 1.0)

    threshold = mask.get("threshold")
    if threshold is not None:
        thr = float(threshold)
        softness = max(0.0, float(mask.get("softness") or 0.0))
        if softness > 0:
            arr = np.clip((arr - thr) / softness, 0.0, 1.0)
        else:
            arr = (arr >= thr).astype(np.float32)

    feather = float(mask.get("feather") or 0.0)
    if feather > 0:
        sigma = feather * min(width, height)
        if sigma > 0:
            arr = cv2.GaussianBlur(arr, (0, 0), sigmaX=sigma, sigmaY=sigma, borderType=cv2.BORDER_DEFAULT)
    if mask.get("invert"):
        arr = 1.0 - arr
    return np.clip(arr, 0.0, 1.0).astype(np.float32)


# ── shape masks ───────────────────────────────────────────────────────────


#: Per-frame-animatable scalar fields of a shape mask's box/centre. Each may
#: carry a keyframe track or an expression; anything not animated keeps its
#: static value from the shape dict.
_ANIMATABLE_SHAPE_FIELDS: tuple[str, ...] = (
    "cx", "cy", "rx", "ry", "w", "h",
    "x0", "y0", "x1", "y1", "radius",
)


def _shape_animation(mask: dict[str, Any], fps: float) -> tuple[dict[str, Any], dict[str, str]]:
    """Collect a shape mask's keyframe tracks and expressions, if any.

    Animation data is read from two equivalent locations so it survives the doc
    pipeline: ``mask["keyframes"]`` / ``mask["expression(s)"]`` (handy for direct
    callers and tests) and the same keys nested inside ``mask["shape"]`` (which
    is the part :func:`lumenframe.model._normalize_mask` preserves verbatim, so
    an animated mask round-trips through ``normalize_doc``). The shape-nested
    location wins on conflict (it is the canonical, persisted home).

    Returns ``(tracks, exprs)`` where ``tracks`` maps an animatable field name to
    a built ``KeyframeTrack`` whose time axis is *frames* (keyframe ``t`` seconds
    times ``fps`` — the same convention as :func:`_keyframe_tracks`), and
    ``exprs`` maps a field name to its expression string. Either may be empty;
    both empty means the mask is static (and the caller takes the byte-identical
    cached path).
    """
    from gemia.video.keyframe import KeyframeTrack

    shape = mask.get("shape") if isinstance(mask.get("shape"), dict) else {}

    def _merge(name: str) -> dict[str, Any]:
        merged: dict[str, Any] = {}
        top = mask.get(name)
        if isinstance(top, dict):
            merged.update(top)
        nested = shape.get(name)
        if isinstance(nested, dict):
            merged.update(nested)  # shape-nested wins
        return merged

    raw_keyframes = _merge("keyframes")
    raw_exprs = _merge("expression")
    extra_exprs = _merge("expressions")
    if extra_exprs:
        merged_exprs = dict(raw_exprs)
        merged_exprs.update(extra_exprs)
        raw_exprs = merged_exprs

    tracks: dict[str, Any] = {}
    for field, points in raw_keyframes.items():
        if field not in _ANIMATABLE_SHAPE_FIELDS or not isinstance(points, list):
            continue
        track = KeyframeTrack()
        added = 0
        for pt in points:
            if not isinstance(pt, dict) or pt.get("value") is None:
                continue
            try:
                value = float(pt["value"])
            except (TypeError, ValueError):
                continue
            frame = float(model._as_float(pt.get("t")) * fps)
            easing = _easing_for(str(pt.get("interp") or "linear"))
            track.add_keyframe(frame, value, easing)
            added += 1
        if added:
            tracks[field] = track

    exprs: dict[str, str] = {}
    for field, expr in raw_exprs.items():
        if field not in _ANIMATABLE_SHAPE_FIELDS:
            continue
        if isinstance(expr, dict):
            expr = expr.get("expr")
        if isinstance(expr, str) and expr.strip():
            exprs[field] = expr

    return tracks, exprs


def _shape_path(mask: dict[str, Any]) -> dict[str, Any] | None:
    """Extract a normalised path spec that drives the mask centre, if any.

    Like :func:`_shape_animation`, the spec is read from two equivalent homes so
    it survives the doc pipeline: ``mask["path"]`` (handy for direct callers /
    tests) and ``mask["shape"]["path"]`` (the part :func:`model._normalize_mask`
    copies verbatim, so a path round-trips through ``normalize_doc``). The
    shape-nested location wins on conflict.

    A valid spec needs at least two ``points`` (each a 2-list of normalised
    ``[x, y]`` in canvas space). Returns a dict with cleaned ``points`` (list of
    ``(x, y)`` float tuples), ``kind`` (``"linear"`` | ``"bezier"``),
    ``duration`` (seconds or ``None`` for "use the layer duration") and ``loop``
    (bool). Returns ``None`` when no usable path is present (so the caller keeps
    the existing static / keyframe behaviour byte-identical).
    """
    shape = mask.get("shape") if isinstance(mask.get("shape"), dict) else {}
    spec = shape.get("path")
    if not isinstance(spec, dict):
        spec = mask.get("path")
    if not isinstance(spec, dict):
        return None

    raw_points = spec.get("points")
    if not isinstance(raw_points, (list, tuple)):
        return None
    points: list[tuple[float, float]] = []
    for p in raw_points:
        if isinstance(p, (list, tuple)) and len(p) >= 2:
            try:
                points.append((float(p[0]), float(p[1])))
            except (TypeError, ValueError):
                continue
    if len(points) < 2:
        return None

    kind = str(spec.get("kind") or "linear").lower()
    if kind not in {"linear", "bezier"}:
        kind = "linear"

    duration: float | None
    if spec.get("duration") is None:
        duration = None
    else:
        try:
            duration = float(spec["duration"])
        except (TypeError, ValueError):
            duration = None
        else:
            if duration <= 0:
                duration = None

    return {
        "points": points,
        "kind": kind,
        "duration": duration,
        "loop": bool(spec.get("loop", False)),
    }


def _path_point(points: list[tuple[float, float]], kind: str, t: float) -> tuple[float, float]:
    """Position along a normalised path at parameter ``t`` in ``[0, 1]``.

    ``"bezier"`` treats ``points`` as a single cubic/higher Bezier control
    polygon and evaluates it via De Casteljau (so the curve is pulled toward the
    interior control points but only passes through the first and last). With
    exactly two points it degenerates to the straight chord. ``"linear"`` walks
    a piecewise-linear polyline through every point, with ``t`` distributed
    uniformly across the ``n - 1`` segments.
    """
    t = 0.0 if t < 0.0 else 1.0 if t > 1.0 else t

    if kind == "bezier":
        # De Casteljau: repeatedly linearly interpolate adjacent points.
        pts = list(points)
        while len(pts) > 1:
            pts = [
                (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)
                for a, b in zip(pts, pts[1:])
            ]
        return pts[0]

    # piecewise-linear through every point
    n = len(points) - 1
    if n <= 0:
        return points[0]
    pos = t * n
    i = int(pos)
    if i >= n:
        return points[-1]
    frac = pos - i
    a, b = points[i], points[i + 1]
    return (a[0] + (b[0] - a[0]) * frac, a[1] + (b[1] - a[1]) * frac)


def _shape_matte(
    mask: dict[str, Any],
    ctx: ResolveContext,
    *,
    layer_frames: int | None = None,
) -> Callable[[int], "np.ndarray"]:
    """A drawn vector mask (rectangle / ellipse / polygon) → per-frame alpha.

    A *static* mask (no keyframes / expression on its box or centre, and no
    path) is rasterised once in canvas space and the closure returns that cached
    alpha — byte-identical to before. An *animated* mask (keyframes / expression
    on ``cx/cy/rx/ry/w/h/x0/y0/x1/y1/radius``) is rasterised **per frame**: the
    animated scalars are evaluated for the requested frame, folded into a copy of
    the shape dict, and that frame's box is rasterised so the mask moves / scales
    over time.

    A mask carrying a ``path`` spec (see :func:`_shape_path`) is also rasterised
    per frame, with the mask **centre** driven along the path as a function of
    local time ``t in [0, 1]`` (``local_frame / path-duration-in-frames``); the
    path duration defaults to ``layer_frames`` (the layer's own span) so the mask
    travels the whole path over the layer's life. Path-driven ``cx`` / ``cy``
    take precedence over static / keyframed centre values; everything else
    (``rx/ry/type/feather/invert`` and any other keyframed fields) is preserved.

    ``feather`` and ``invert`` are baked in per frame; the backend resizes the
    alpha onto the (transformed) layer frame, so a mask still travels with its
    layer's transform — the After Effects semantic.
    """
    fps = float(ctx.fps) or 1.0
    tracks, exprs = _shape_animation(mask, fps)
    path = _shape_path(mask)
    if not tracks and not exprs and path is None:
        alpha = _rasterise_shape_mask(mask, ctx.width, ctx.height)

        def matte(_local_frame: int):
            return alpha

        return matte

    base_shape = dict(mask.get("shape")) if isinstance(mask.get("shape"), dict) else {}

    # Path local-time normaliser: how many frames map t: 0 -> 1. Prefer the path
    # spec's explicit duration, else the layer's own frame span, else the comp's.
    path_frames = 0
    if path is not None:
        if path["duration"] is not None:
            path_frames = int(round(path["duration"] * fps))
        elif layer_frames and layer_frames > 0:
            path_frames = int(layer_frames)
        else:
            path_frames = int(ctx.total_frames)
        path_frames = max(1, path_frames)

    def _path_centre(frame: int) -> tuple[float, float]:
        # t over the path's duration; loop wraps, else clamps at the final point.
        span = max(1, path_frames - 1) if path_frames > 1 else 1
        raw = float(frame) / float(span)
        if path["loop"]:
            t = raw - math.floor(raw)
        else:
            t = 0.0 if raw < 0.0 else 1.0 if raw > 1.0 else raw
        return _path_point(path["points"], path["kind"], t)

    def _eval_field(field: str, frame: int) -> float | None:
        track = tracks.get(field)
        if track is not None:
            return float(track.evaluate(float(frame)))
        expr = exprs.get(field)
        if expr is not None:
            try:
                from gemia.expressions import SafeEvaluator

                time_sec = float(frame) / fps if fps > 0 else 0.0
                evaluator = SafeEvaluator(
                    time=time_sec,
                    width=float(ctx.width),
                    height=float(ctx.height),
                )
                return float(evaluator.eval(expr))
            except Exception:
                return None
        return None

    def matte(local_frame: int):
        frame = max(0, int(local_frame))
        shape = dict(base_shape)
        for field in _ANIMATABLE_SHAPE_FIELDS:
            value = _eval_field(field, frame)
            if value is not None:
                shape[field] = value
        if path is not None:
            # Path drives the CENTRE; preserve shape/size/feather/invert.
            cx, cy = _path_centre(frame)
            shape["cx"], shape["cy"] = cx, cy
            shape.pop("rect", None)  # a rect box would override cx/cy in _shape_box
            for k in ("x0", "y0", "x1", "y1"):
                shape.pop(k, None)
        frame_mask = dict(mask)
        frame_mask["shape"] = shape
        return _rasterise_shape_mask(frame_mask, ctx.width, ctx.height)

    return matte


def _shape_box(shape: dict[str, Any]) -> tuple[float, float, float, float]:
    """Normalised ``(x0, y0, x1, y1)`` bounding box for a rect/ellipse shape.

    Accepts ``x0/y0/x1/y1``, a ``rect`` list, or a centre form
    (``cx/cy`` + ``rx/ry`` or ``w/h``). Defaults to the full canvas.
    """
    rect = shape.get("rect")
    if isinstance(rect, (list, tuple)) and len(rect) >= 4:
        return (float(rect[0]), float(rect[1]), float(rect[2]), float(rect[3]))
    if all(k in shape for k in ("x0", "y0", "x1", "y1")):
        return (float(shape["x0"]), float(shape["y0"]), float(shape["x1"]), float(shape["y1"]))
    if "cx" in shape and "cy" in shape and any(k in shape for k in ("rx", "ry", "w", "h")):
        cx, cy = float(shape["cx"]), float(shape["cy"])
        half_w = float(shape["w"]) / 2.0 if "w" in shape else float(shape.get("rx", shape.get("ry", 0.0)))
        half_h = float(shape["h"]) / 2.0 if "h" in shape else float(shape.get("ry", shape.get("rx", 0.0)))
        return (cx - half_w, cy - half_h, cx + half_w, cy + half_h)
    return (0.0, 0.0, 1.0, 1.0)


def _filled_rounded_rect(img: "np.ndarray", x0: int, y0: int, x1: int, y1: int, r: int) -> None:
    import cv2

    if x1 <= x0 or y1 <= y0:
        return
    r = min(r, (x1 - x0) // 2, (y1 - y0) // 2)
    if r <= 0:
        cv2.rectangle(img, (x0, y0), (x1, y1), 255, -1)
        return
    cv2.rectangle(img, (x0 + r, y0), (x1 - r, y1), 255, -1)
    cv2.rectangle(img, (x0, y0 + r), (x1, y1 - r), 255, -1)
    for cx, cy in ((x0 + r, y0 + r), (x1 - r, y0 + r), (x0 + r, y1 - r), (x1 - r, y1 - r)):
        cv2.circle(img, (cx, cy), r, 255, -1, lineType=cv2.LINE_AA)


def _shape_fill_contours(shape: dict[str, Any]) -> list[list[tuple[float, float]]]:
    """Return closed vector contours in normalised coordinates.

    ``polygon`` keeps its historical straight-point behaviour. ``path`` accepts
    either ``points`` or ``contours``; each contour may be a plain point list or
    ``{"points": ..., "kind": "linear"|"bezier"}``. ``bezier`` samples the
    control polygon with De Casteljau, giving masks curved vector boundaries.
    """
    raw_contours = shape.get("contours")
    if isinstance(raw_contours, (list, tuple)) and raw_contours:
        specs = list(raw_contours)
    else:
        specs = [{"points": shape.get("points"), "kind": shape.get("kind")}]

    contours: list[list[tuple[float, float]]] = []
    default_kind = "bezier" if str(shape.get("type") or "").lower() == "bezier" else "linear"
    sample_count = int(float(shape.get("samples") or 64))
    sample_count = max(8, min(sample_count, 512))
    for spec in specs:
        if isinstance(spec, dict):
            raw_points = spec.get("points")
            kind = str(spec.get("kind") or shape.get("kind") or default_kind).lower()
        else:
            raw_points = spec
            kind = default_kind
        points = _clean_points(raw_points)
        if len(points) < 3:
            continue
        if kind == "bezier":
            denom = max(1, sample_count - 1)
            sampled = [_path_point(points, "bezier", i / denom) for i in range(sample_count)]
            contours.append(sampled)
        else:
            contours.append(points)
    return contours


def _clean_points(raw_points: Any) -> list[tuple[float, float]]:
    if not isinstance(raw_points, (list, tuple)):
        return []
    points: list[tuple[float, float]] = []
    for p in raw_points:
        if isinstance(p, (list, tuple)) and len(p) >= 2:
            try:
                points.append((float(p[0]), float(p[1])))
            except (TypeError, ValueError):
                continue
    return points


def _rasterise_shape_mask(mask: dict[str, Any], width: int, height: int) -> "np.ndarray":
    """Rasterise a shape-mask spec to a float32 ``(H, W)`` alpha in ``[0, 1]``.

    Coordinates are normalised to the canvas (``[0, 1]``) so a mask is
    resolution-independent. ``feather`` (a fraction of the smaller canvas
    dimension) softens the edge; ``invert`` flips coverage.
    """
    import cv2

    shape = mask.get("shape") if isinstance(mask.get("shape"), dict) else {}
    stype = str(shape.get("type") or "rectangle").lower()
    img = np.zeros((height, width), dtype=np.uint8)

    def _px(nx: Any, ny: Any) -> tuple[int, int]:
        return (int(round(float(nx) * width)), int(round(float(ny) * height)))

    if stype in {"polygon", "poly", "path", "bezier"}:
        contours = [
            np.array([_px(x, y) for x, y in contour], dtype=np.int32)
            for contour in _shape_fill_contours(shape)
            if len(contour) >= 3
        ]
        if contours:
            if str(shape.get("fill_rule") or "").lower() == "evenodd":
                for contour in contours:
                    plate = np.zeros_like(img)
                    cv2.fillPoly(plate, [contour], 255, lineType=cv2.LINE_AA)
                    img = cv2.bitwise_xor(img, plate)
            else:
                cv2.fillPoly(img, contours, 255, lineType=cv2.LINE_AA)
    else:
        x0n, y0n, x1n, y1n = _shape_box(shape)
        x0, y0 = _px(x0n, y0n)
        x1, y1 = _px(x1n, y1n)
        if x1 < x0:
            x0, x1 = x1, x0
        if y1 < y0:
            y0, y1 = y1, y0
        if stype in {"ellipse", "circle", "oval"}:
            cx, cy = (x0 + x1) // 2, (y0 + y1) // 2
            ax, ay = (x1 - x0) // 2, (y1 - y0) // 2
            if ax > 0 and ay > 0:
                cv2.ellipse(img, (cx, cy), (ax, ay), 0, 0, 360, 255, -1, lineType=cv2.LINE_AA)
        else:  # rectangle (default)
            radius = float(shape.get("radius") or 0.0)
            r = int(round(radius * min(width, height)))
            if r > 0:
                _filled_rounded_rect(img, x0, y0, x1, y1, r)
            elif x1 > x0 and y1 > y0:
                cv2.rectangle(img, (x0, y0), (x1, y1), 255, -1)

    alpha = img.astype(np.float32) / 255.0
    feather = float(mask.get("feather") or 0.0)
    if feather > 0:
        sigma = feather * min(width, height)
        if sigma > 0:
            alpha = cv2.GaussianBlur(alpha, (0, 0), sigmaX=sigma, sigmaY=sigma, borderType=cv2.BORDER_DEFAULT)
    if mask.get("invert"):
        alpha = 1.0 - alpha
    return np.clip(alpha, 0.0, 1.0).astype(np.float32)


def _resolve_property_with_expression(
    layer: dict,
    property_name: str,
    frame_index: int,
    fps: float,
    canvas_width: float,
    canvas_height: float,
    default: float = 0.0,
) -> float:
    """Resolve a layer property value, prioritizing expressions over keyframes.

    Precedence: expression (time-driven) > keyframes > static value > default
    
    Args:
        layer: Layer dict that may have "expressions" and "keyframes"
        property_name: Property like "opacity" or "transform.x"
        frame_index: Current frame number
        fps: Frames per second
        canvas_width, canvas_height: Canvas dimensions for binding
        default: Fallback value if no binding found
    
    Returns:
        Evaluated property value as float
    """
    from gemia.expressions import SafeEvaluator, ExprError
    
    # Check if property has an expression bound
    expressions = layer.get("expressions") or {}
    if property_name in expressions:
        expr_data = expressions[property_name]
        expr_str = expr_data.get("expr", "")
        
        try:
            time_sec = frame_index / fps
            layer_duration = float(layer.get("duration", 1.0))
            evaluator = SafeEvaluator(
                time=time_sec,
                duration=layer_duration,
                width=canvas_width,
                height=canvas_height,
            )
            result = evaluator.eval(expr_str)
            return float(result)
        except ExprError:
            # If expression fails, fall through to keyframes
            pass
        except Exception:
            # Any other error, fall through
            pass
    
    # If no expression, check keyframes (this is handled by the caller via KeyframeTrack)
    # Fallback to static value
    static = layer.get(property_name) if property_name not in ("transform.x", "transform.y", "transform.rotation", "transform.scale_x", "transform.scale_y") else None
    if static is not None:
        return float(static)
    
    return default



# ── transform / keyframes ─────────────────────────────────────────────────


def _centred_position(width, height, scale, rotation_deg, x, y) -> tuple[int, int]:
    """Top-left placement that keeps canvas-sized content centred at (centre+x, centre+y).

    Accounts for the backend resizing content by ``scale`` and growing the
    rotation bounding box, so the visual centre lands where the model says.
    """
    rad = math.radians(rotation_deg)
    c, s = abs(math.cos(rad)), abs(math.sin(rad))
    bound_w = (height * scale * s) + (width * scale * c)
    bound_h = (height * scale * c) + (width * scale * s)
    px = (width - bound_w) / 2.0 + x
    py = (height - bound_h) / 2.0 + y
    return (int(round(px)), int(round(py)))


#: lumenframe keyframe property -> backend Layer property the renderer reads.
_KEYFRAME_PROP_MAP = {
    "opacity": "opacity",
    "transform.x": "position_x",
    "transform.y": "position_y",
    "x": "position_x",
    "y": "position_y",
    "transform.scale_x": "scale",
    "transform.scale": "scale",
    "scale": "scale",
    "transform.rotation": "rotation_deg",
    "rotation": "rotation_deg",
}


def _keyframe_tracks(layer: dict[str, Any], fps: float):
    from gemia.video.keyframe import KeyframeTrack

    out: dict[str, Any] = {}
    for prop, points in (layer.get("keyframes") or {}).items():
        target = _KEYFRAME_PROP_MAP.get(str(prop))
        if not target or not isinstance(points, list):
            continue
        track = KeyframeTrack()
        added = 0
        for pt in points:
            if not isinstance(pt, dict) or pt.get("value") is None:
                continue
            try:
                value = float(pt["value"])
            except (TypeError, ValueError):
                continue
            frame = float(model._as_float(pt.get("t")) * fps)
            easing = _easing_for(str(pt.get("interp") or "linear"))
            track.add_keyframe(frame, value, easing)
            added += 1
        if added:
            out[target] = track
    return out


def _easing_for(interp: str) -> str:
    """Map a LayerPatch ``interp`` name to a ``KeyframeTrack`` easing name.

    The only consumer is :meth:`gemia.video.keyframe.KeyframeTrack.add_keyframe`,
    whose vocabulary is the *underscore* set ``{linear, ease_in, ease_out,
    ease_in_out, bezier}`` (plus a ``bezier(x1,y1,x2,y2)`` literal). Earlier this
    table emitted CSS-style hyphenated names (``ease-out``, ``ease-in-out``,
    ``step``) which ``add_keyframe`` rejects — so every non-``linear`` keyframe
    interp raised at render time. Aligning the output to the track's vocabulary
    makes ``set_keyframe`` easing actually render:

    * ``hold`` has no per-keyframe step in this engine → falls back to ``linear``;
    * ``ease`` / ``bezier`` (no control points) → the smooth ``ease_in_out``.
    """
    return {
        "linear": "linear", "hold": "linear", "ease": "ease_in_out",
        "ease_in": "ease_in", "ease_out": "ease_out",
        "ease_in_out": "ease_in_out", "bezier": "ease_in_out",
    }.get(interp, "linear")


def _time_remap_fn(layer: dict[str, Any], fps: float) -> Callable[[int], int] | None:
    """Build the ``time_map_fn`` for a layer carrying a ``time_remap`` curve.

    The render backend hands ``time_map_fn`` the *output-local* frame (absolute
    frame minus the layer's ``start_frame``) and feeds its return value — the
    *source-local* frame — to the layer's ``content_fn``. So:

        out_sec    = output_local_frame / fps                 (output timeline)
        src_sec    = eval_time_remap(curve, out_sec)          (source timeline)
        src_local  = to_frame(src_sec - source_in, fps)       (content_fn arg)

    This drives content sampling only; transform / opacity keyframes and
    expressions stay on the OUTPUT frame (the backend evaluates those off the
    absolute frame index, untouched by this map). A ``time_remap`` therefore
    overrides any constant-speed source mapping for the same layer.
    """
    remap = layer.get("time_remap")
    if not isinstance(remap, dict) or not (remap.get("keyframes")):
        return None
    source_in = model._as_float(layer.get("source_in"))

    def time_map_fn(output_local_frame: int) -> int:
        out_sec = timebase.to_seconds(int(output_local_frame), fps)
        src_sec = model.eval_time_remap(remap, out_sec)
        return max(0, timebase.to_frame(src_sec - source_in, fps))

    return time_map_fn


# ── small helpers ──────────────────────────────────────────────────────────


def _safe_blend(mode: Any) -> str:
    # The whitelist is sourced from the backend's BLEND_MODES so the two never
    # drift; any mode the compositor implements passes validation. Unknown modes
    # degrade to normal (the compositor itself also falls back safely, so a stray
    # name can never crash a render).
    from gemia.video.layers import BLEND_MODES
    supported = set(BLEND_MODES)
    m = str(mode or "normal")
    return m if m in supported else "normal"


def _rgba01(value: Any, *, default: tuple[float, float, float, float]):
    if isinstance(value, str) and value.startswith("#") and len(value) in (7, 9):
        hexs = value[1:]
        r = int(hexs[0:2], 16) / 255.0
        g = int(hexs[2:4], 16) / 255.0
        b = int(hexs[4:6], 16) / 255.0
        a = int(hexs[6:8], 16) / 255.0 if len(hexs) == 8 else 1.0
        return (r, g, b, a)
    if isinstance(value, (list, tuple)) and len(value) in (3, 4):
        vals = [float(v) for v in value]
        if max(vals) > 1.0:
            vals = [v / 255.0 for v in vals]
        if len(vals) == 3:
            vals.append(1.0)
        return tuple(vals)  # type: ignore[return-value]
    return default


# ── effect chain application ──────────────────────────────────────────────


def _wrap_with_effects(base_fn: ContentFn, effects: list[dict[str, Any]], ctx: ResolveContext) -> ContentFn:
    """Wrap a content_fn to apply per-layer effects in order.

    Args:
        base_fn: The original content_fn(frame_index) -> RGBAFrame.
        effects: List of effect dicts {type, params, enabled}.
        ctx: ResolveContext for canvas dimensions.

    Returns:
        A new content_fn that applies effects in order.
    """
    def wrapped_fn(frame_index: int) -> np.ndarray:
        frame = base_fn(frame_index)
        for effect in effects:
            if not effect.get("enabled", True):
                continue
            eff_type = str(effect.get("type", ""))
            params = dict(effect.get("params") or {})
            frame = _apply_effect(frame, eff_type, params, ctx)
        return frame

    return wrapped_fn


# ── transitions (synthesised at compile, never mutate the doc) ─────────────
#
# ``add_transition`` records ``props.transitions[edge] = {kind, duration}`` on a
# layer (edge is ``"in"`` / ``"out"``). Nothing in the doc tells the renderer how
# to draw them, so we synthesise a *transient* per-frame effect here: the layer's
# ``content_fn`` is wrapped so that, over the first ``duration`` seconds of the
# layer (the in edge) and the last ``duration`` seconds (the out edge), the
# transition modulates the canvas-sized content. ``content_fn`` is handed a
# layer-local frame index (absolute minus ``start_frame``), which is exactly the
# progress signal we need — so the wrapper is a pure function of that index and
# the doc is never touched.

#: Transition kinds the renderer can synthesise (used by the catalogue docs).
TRANSITION_KINDS: tuple[str, ...] = (
    "fade",
    "dissolve",
    "wipe_l",
    "wipe_r",
    "wipe_u",
    "wipe_d",
    "slide",
)


def _edge_progress(local_frame: int, in_frames: int, out_frames: int, span_frames: int) -> tuple[str, float]:
    """Return ``(edge, t)`` for a layer-local frame.

    ``t`` ramps ``0 -> 1`` across the ``in`` edge and ``1 -> 0`` across the
    ``out`` edge; outside both edges ``t == 1.0`` (fully shown). ``edge`` is
    ``"in"`` / ``"out"`` / ``""`` so spatial kinds know which direction to grow.
    The ramp uses ``local_frame / edge_frames`` so frame 0 is fully hidden
    (``t == 0``) and the last edge frame reaches full (``t == 1``).
    """
    span = max(1, int(span_frames))
    last = span - 1
    f = max(0, min(int(local_frame), last))

    # In edge takes priority near the start; out edge near the end.
    if in_frames > 0 and f < in_frames:
        denom = float(min(in_frames, last)) or 1.0
        return "in", max(0.0, min(1.0, f / denom))
    if out_frames > 0 and f > last - out_frames:
        # Frames remaining until the layer ends (0 on the final frame).
        remaining = last - f
        denom = float(min(out_frames, last)) or 1.0
        return "out", max(0.0, min(1.0, remaining / denom))
    return "", 1.0


def _apply_transition(frame: np.ndarray, kind: str, edge: str, t: float) -> np.ndarray:
    """Modulate a canvas-sized RGBA ``frame`` by transition ``kind`` at progress ``t``.

    ``t`` is the *reveal* fraction (1.0 = fully shown). ``edge`` is ``"in"`` /
    ``"out"`` and selects the growth direction for spatial kinds. fade / dissolve
    scale alpha; wipe_* reveal a growing band via a hard column/row mask; slide
    translates the content in from (in) / out to (out) the edge direction.
    """
    frame = np.asarray(frame, dtype=np.float32)
    if frame.ndim != 3 or frame.shape[2] != 4:
        return frame
    t = max(0.0, min(1.0, float(t)))
    kind = str(kind).lower()

    if kind in ("fade", "dissolve"):
        out = frame.copy()
        out[..., 3:4] = out[..., 3:4] * t
        return out

    if kind in ("wipe_l", "wipe_r", "wipe_u", "wipe_d"):
        h, w = frame.shape[:2]
        out = frame.copy()
        if kind in ("wipe_l", "wipe_r"):
            reveal = int(round(t * w))
            mask = np.zeros((w,), dtype=np.float32)
            if reveal > 0:
                # wipe_l reveals from the left edge; wipe_r from the right.
                if kind == "wipe_l":
                    mask[:reveal] = 1.0
                else:
                    mask[w - reveal:] = 1.0
            out[..., 3] = out[..., 3] * mask[np.newaxis, :]
        else:
            reveal = int(round(t * h))
            mask = np.zeros((h,), dtype=np.float32)
            if reveal > 0:
                # wipe_u reveals from the top edge; wipe_d from the bottom.
                if kind == "wipe_u":
                    mask[:reveal] = 1.0
                else:
                    mask[h - reveal:] = 1.0
            out[..., 3] = out[..., 3] * mask[:, np.newaxis]
        return out

    if kind == "slide":
        # Slide in from the left edge (in) / out to the right edge (out): shift
        # the content horizontally by (1 - t) of the width, zero-filling vacated
        # pixels so alpha goes transparent there.
        h, w = frame.shape[:2]
        shift = int(round((1.0 - t) * w))
        out = np.zeros_like(frame, dtype=np.float32)
        if shift <= 0:
            return frame.copy()
        if shift >= w:
            return out
        out[:, shift:, :] = frame[:, : w - shift, :]
        return out

    # Unknown kind: fall back to a fade so the transition still has an effect.
    out = frame.copy()
    out[..., 3:4] = out[..., 3:4] * t
    return out


def _wrap_with_transitions(
    base_fn: ContentFn,
    transitions: dict[str, Any],
    span_frames: int,
    fps: float,
) -> ContentFn:
    """Wrap a content_fn so in/out transitions modulate it per layer-local frame.

    ``transitions`` is ``props.transitions`` — ``{"in": {kind, duration}, ...}``.
    ``span_frames`` is the layer's drawn length in frames; ``fps`` converts a
    transition ``duration`` (seconds) to a frame count. The doc is not mutated.
    """
    in_spec = transitions.get("in") if isinstance(transitions.get("in"), dict) else None
    out_spec = transitions.get("out") if isinstance(transitions.get("out"), dict) else None

    def _frames(spec: dict[str, Any] | None) -> int:
        if not spec:
            return 0
        dur = model._as_float(spec.get("duration"))
        return max(0, int(round(dur * fps)))

    in_frames = _frames(in_spec)
    out_frames = _frames(out_spec)
    if in_frames <= 0 and out_frames <= 0:
        return base_fn

    in_kind = str((in_spec or {}).get("kind") or "fade")
    out_kind = str((out_spec or {}).get("kind") or "fade")

    def wrapped_fn(local_frame: int) -> np.ndarray:
        frame = base_fn(local_frame)
        edge, t = _edge_progress(local_frame, in_frames, out_frames, span_frames)
        if edge == "in":
            return _apply_transition(frame, in_kind, "in", t)
        if edge == "out":
            return _apply_transition(frame, out_kind, "out", t)
        return frame

    return wrapped_fn


# ── effect dispatch table ─────────────────────────────────────────────────
#
# Each built-in effect is a small adapter ``fn(frame, params, ctx) -> frame``
# that reads its params (with the same defaults as before) and calls the
# underlying ``_apply_*`` kernel. The ``EFFECTS`` dict maps an effect *type*
# string to its adapter — adding a new effect is now "a new function + one dict
# entry", with no edits to the dispatch logic in :func:`_apply_effect`.
#
# Aliases (e.g. ``flip`` for ``mirror``) are real, separate keys pointing at the
# same adapter, so the table's key set is the canonical effect-type vocabulary
# (which :mod:`lumenframe.catalog` is checked against by a drift guard test).


def _effect_gaussian_blur(frame: np.ndarray, params: dict[str, Any], ctx: ResolveContext) -> np.ndarray:
    radius = float(params.get("radius", 5.0))
    return _apply_gaussian_blur_rgba(frame, radius)


def _effect_color_grade(frame: np.ndarray, params: dict[str, Any], ctx: ResolveContext) -> np.ndarray:
    brightness = float(params.get("brightness", 0.0))
    contrast = float(params.get("contrast", 1.0))
    saturation = float(params.get("saturation", 1.0))
    # DaVinci-style colour wheels + white balance (identity defaults are no-ops).
    lift = float(params.get("lift", 0.0))
    gamma = float(params.get("gamma", 1.0))
    gain = float(params.get("gain", 1.0))
    temperature = float(params.get("temperature", 0.0))
    tint = float(params.get("tint", 0.0))
    return _apply_color_grade(
        frame,
        brightness,
        contrast,
        saturation,
        lift=lift,
        gamma=gamma,
        gain=gain,
        temperature=temperature,
        tint=tint,
    )


def _effect_brightness(frame: np.ndarray, params: dict[str, Any], ctx: ResolveContext) -> np.ndarray:
    brightness = float(params.get("value", 0.0))
    return _apply_color_grade(frame, brightness, 1.0, 1.0)


def _effect_contrast(frame: np.ndarray, params: dict[str, Any], ctx: ResolveContext) -> np.ndarray:
    contrast = float(params.get("value", 1.0))
    return _apply_color_grade(frame, 0.0, contrast, 1.0)


def _effect_saturation(frame: np.ndarray, params: dict[str, Any], ctx: ResolveContext) -> np.ndarray:
    saturation = float(params.get("value", 1.0))
    return _apply_color_grade(frame, 0.0, 1.0, saturation)


def _effect_invert(frame: np.ndarray, params: dict[str, Any], ctx: ResolveContext) -> np.ndarray:
    return _apply_invert(frame)


def _effect_grayscale(frame: np.ndarray, params: dict[str, Any], ctx: ResolveContext) -> np.ndarray:
    amount = float(params.get("amount", 1.0))
    return _apply_grayscale(frame, amount)


def _effect_mirror(frame: np.ndarray, params: dict[str, Any], ctx: ResolveContext) -> np.ndarray:
    direction = str(params.get("direction", "horizontal"))
    return _apply_mirror(frame, direction)


def _effect_crop(frame: np.ndarray, params: dict[str, Any], ctx: ResolveContext) -> np.ndarray:
    x0 = float(params.get("x0", 0.0))
    y0 = float(params.get("y0", 0.0))
    x1 = float(params.get("x1", 1.0))
    y1 = float(params.get("y1", 1.0))
    return _apply_crop(frame, x0, y0, x1, y1)


def _effect_vignette(frame: np.ndarray, params: dict[str, Any], ctx: ResolveContext) -> np.ndarray:
    amount = float(params.get("amount", 0.5))
    return _apply_vignette(frame, amount)


def _effect_sharpen(frame: np.ndarray, params: dict[str, Any], ctx: ResolveContext) -> np.ndarray:
    amount = float(params.get("amount", 1.0))
    return _apply_sharpen(frame, amount)


def _effect_hue_rotate(frame: np.ndarray, params: dict[str, Any], ctx: ResolveContext) -> np.ndarray:
    degrees = float(params.get("degrees") or params.get("value") or 0.0)
    return _apply_hue_rotate(frame, degrees)


def _effect_chroma_key(frame: np.ndarray, params: dict[str, Any], ctx: ResolveContext) -> np.ndarray:
    key_color = params.get("key_color", "#00FF00")
    threshold = float(params.get("threshold", 0.4))
    softness = float(params.get("softness", 0.1))
    return _apply_chroma_key(frame, key_color, threshold, softness)


def _effect_advanced_chroma_key(frame: np.ndarray, params: dict[str, Any], ctx: ResolveContext) -> np.ndarray:
    key_color = params.get("key_color", "#00FF00")
    similarity = float(params.get("similarity", params.get("threshold", 0.22)))
    softness = float(params.get("softness", 0.12))
    spill = float(params.get("spill", params.get("despill", 0.5)))
    edge_blur = float(params.get("edge_blur", 0.0))
    return _apply_advanced_chroma_key(
        frame,
        key_color,
        similarity=similarity,
        softness=softness,
        spill=spill,
        edge_blur=edge_blur,
    )


def _effect_luma_key(frame: np.ndarray, params: dict[str, Any], ctx: ResolveContext) -> np.ndarray:
    threshold = float(params.get("threshold", 0.5))
    softness = float(params.get("softness", 0.1))
    mode = str(params.get("mode", "key_dark")).lower()
    return _apply_luma_key(frame, threshold=threshold, softness=softness, mode=mode)


def _effect_curves(frame: np.ndarray, params: dict[str, Any], ctx: ResolveContext) -> np.ndarray:
    """DaVinci-style tone curves on r/g/b/rgb/luma; identity points are a no-op.

    Delegates to the standalone :func:`lumenframe.effects.curves.apply_curves`
    kernel (NumPy only, alpha-preserving). ``channel`` selects which channel(s)
    the transfer applies to and ``points`` is the ``[[x, y], ...]`` control set.
    """
    from lumenframe.effects.curves import apply_curves

    channel = str(params.get("channel", "rgb"))
    points = params.get("points")
    return apply_curves(frame, channel=channel, points=points)


EffectFn = Callable[["np.ndarray", dict[str, Any], "ResolveContext"], "np.ndarray"]

#: Built-in effect dispatch table: effect type -> adapter ``fn(frame, params, ctx)``.
#: This is the single source of truth for the built-in effect vocabulary; a new
#: effect is a new ``_effect_*`` function plus one entry here — no if/elif edits.
#: ``flip`` is an alias of ``mirror`` (same adapter, behaviour-preserving).
EFFECTS: dict[str, EffectFn] = {
    "gaussian_blur": _effect_gaussian_blur,
    "color_grade": _effect_color_grade,
    "brightness": _effect_brightness,
    "contrast": _effect_contrast,
    "saturation": _effect_saturation,
    "invert": _effect_invert,
    "grayscale": _effect_grayscale,
    "mirror": _effect_mirror,
    "flip": _effect_mirror,
    "crop": _effect_crop,
    "vignette": _effect_vignette,
    "sharpen": _effect_sharpen,
    "hue_rotate": _effect_hue_rotate,
    "chroma_key": _effect_chroma_key,
    "advanced_chroma_key": _effect_advanced_chroma_key,
    "luma_key": _effect_luma_key,
    "curves": _effect_curves,
}


def _apply_effect(frame: np.ndarray, effect_type: str, params: dict[str, Any], ctx: ResolveContext) -> np.ndarray:
    """Apply a single effect to an RGBA frame.

    Built-in effects are dispatched through the :data:`EFFECTS` table; effects
    not in the table fall back to the third-party :mod:`gemia.registry`, and an
    unknown / failing effect is silently skipped (behaviour-preserving).
    """
    frame = np.asarray(frame, dtype=np.float32)
    if frame.ndim != 3 or frame.shape[2] != 4:
        return frame

    # Built-in effects via dispatch table.
    fn = EFFECTS.get(effect_type)
    if fn is not None:
        return fn(frame, params, ctx)

    # Attempt to resolve via gemia registry (for extensions).
    try:
        from gemia.registry import resolve
        func = resolve(effect_type)
        # Call the function with the BGR image (without alpha).
        bgr = frame[..., :3]
        alpha = frame[..., 3:4]
        processed = func(bgr, **params)
        return np.concatenate([processed, alpha], axis=2).astype(np.float32)
    except Exception:
        # Unknown effect or resolution failed; skip silently.
        return frame


def _apply_gaussian_blur_rgba(frame: np.ndarray, radius: float) -> np.ndarray:
    """Apply Gaussian blur to RGBA frame using premultiplied alpha."""
    import cv2

    frame = np.asarray(frame, dtype=np.float32)
    if frame.ndim != 3 or frame.shape[2] != 4:
        return frame

    if radius <= 0:
        return frame

    # Separate RGB and alpha.
    rgb = frame[..., :3]
    alpha = frame[..., 3:4]

    # Premultiply: rgb * alpha to avoid haloing at semi-transparent edges.
    rgb_premult = rgb * alpha

    # Apply blur to premultiplied RGB and alpha separately.
    ksize = int(2 * round(radius) + 1)
    blurred_rgb_premult = cv2.GaussianBlur(rgb_premult, (ksize, ksize), radius)
    blurred_alpha_2d = cv2.GaussianBlur(alpha[..., 0], (ksize, ksize), radius)
    blurred_alpha = blurred_alpha_2d[..., np.newaxis]

    # Unpremultiply: divide rgb back by alpha (avoid division by zero).
    eps = 1e-6
    blurred_rgb = blurred_rgb_premult / (blurred_alpha + eps)
    blurred_rgb = np.clip(blurred_rgb, 0.0, 1.0)

    return np.concatenate([blurred_rgb, blurred_alpha], axis=2).astype(np.float32)


def _apply_color_grade(
    frame: np.ndarray,
    brightness: float,
    contrast: float,
    saturation: float,
    *,
    lift: float = 0.0,
    gamma: float = 1.0,
    gain: float = 1.0,
    temperature: float = 0.0,
    tint: float = 0.0,
) -> np.ndarray:
    """Apply DaVinci-style colour grading to an RGBA frame.

    Pipeline (RGB only; alpha is preserved): brightness → contrast →
    lift/gamma/gain colour wheels → temperature/tint white-balance →
    saturation.

    The colour-wheel stage implements the standard primaries model
    ``out = (gain * in) ** (1 / gamma) + lift`` per channel (clamped to
    ``[0, 1]``). ``temperature``/``tint`` apply a warm/cool and
    green/magenta channel balance respectively.

    Identity defaults — ``lift=0``, ``gamma=1``, ``gain=1``,
    ``temperature=0``, ``tint=0`` — are a strict no-op: each new stage is
    skipped entirely when its params sit at identity, so callers that only
    use brightness/contrast/saturation are byte-for-byte unchanged (golden
    identical).
    """
    frame = np.asarray(frame, dtype=np.float32)
    if frame.ndim != 3 or frame.shape[2] != 4:
        return frame

    rgb = frame[..., :3].copy()
    alpha = frame[..., 3:4]

    # Apply brightness (add to all channels).
    rgb = np.clip(rgb + brightness, 0.0, 1.0)

    # Apply contrast (scale around 0.5).
    rgb = 0.5 + contrast * (rgb - 0.5)
    rgb = np.clip(rgb, 0.0, 1.0)

    # Colour wheels: lift / gamma / gain  ->  out = (gain * in)^(1/gamma) + lift.
    # Skipped entirely at identity so the legacy path stays golden-identical.
    if not (
        np.isclose(lift, 0.0)
        and np.isclose(gamma, 1.0)
        and np.isclose(gain, 1.0)
    ):
        g = float(gamma)
        if g <= 0.0:
            g = 1e-6  # guard: keep the power finite for degenerate gamma.
        rgb = rgb * float(gain)
        rgb = np.clip(rgb, 0.0, None)  # power on negatives is undefined.
        rgb = np.power(rgb, 1.0 / g)
        rgb = rgb + float(lift)
        rgb = np.clip(rgb, 0.0, 1.0)

    # Temperature / tint white balance: warm/cool (R<->B) and green/magenta
    # (G<->R+B) channel balance. Skipped at identity (both 0) for no-op.
    if not (np.isclose(temperature, 0.0) and np.isclose(tint, 0.0)):
        rgb = rgb.copy()
        temp = float(temperature)
        tnt = float(tint)
        # Warm (+temp) boosts red and trims blue; cool (-temp) does the reverse.
        rgb[..., 0] = rgb[..., 0] * (1.0 + 0.3 * temp)
        rgb[..., 2] = rgb[..., 2] * (1.0 - 0.3 * temp)
        # +tint pushes toward green, -tint toward magenta (R+B).
        rgb[..., 1] = rgb[..., 1] * (1.0 + 0.3 * tnt)
        rgb[..., 0] = rgb[..., 0] * (1.0 - 0.15 * tnt)
        rgb[..., 2] = rgb[..., 2] * (1.0 - 0.15 * tnt)
        rgb = np.clip(rgb, 0.0, 1.0)

    # Apply saturation (desaturate by blending towards greyscale).
    if not np.isclose(saturation, 1.0):
        # Compute greyscale and blend with original.
        grey = 0.299 * rgb[..., 0] + 0.587 * rgb[..., 1] + 0.114 * rgb[..., 2]
        grey = np.stack([grey, grey, grey], axis=2)
        rgb = (1.0 - saturation) * grey + saturation * rgb
        rgb = np.clip(rgb, 0.0, 1.0)

    return np.concatenate([rgb, alpha], axis=2).astype(np.float32)


def _apply_invert(frame: np.ndarray) -> np.ndarray:
    """Invert RGB channels, preserve alpha."""
    frame = np.asarray(frame, dtype=np.float32)
    if frame.ndim != 3 or frame.shape[2] != 4:
        return frame

    rgb = frame[..., :3]
    alpha = frame[..., 3:4]
    inverted_rgb = 1.0 - rgb
    return np.concatenate([inverted_rgb, alpha], axis=2).astype(np.float32)


def _apply_grayscale(frame: np.ndarray, amount: float) -> np.ndarray:
    """Blend towards greyscale; amount 0=full color, 1=full grey."""
    frame = np.asarray(frame, dtype=np.float32)
    if frame.ndim != 3 or frame.shape[2] != 4:
        return frame

    rgb = frame[..., :3].copy()
    alpha = frame[..., 3:4]

    amount = max(0.0, min(1.0, amount))
    if amount > 0.0:
        grey = 0.299 * rgb[..., 0] + 0.587 * rgb[..., 1] + 0.114 * rgb[..., 2]
        grey = np.stack([grey, grey, grey], axis=2)
        rgb = (1.0 - amount) * rgb + amount * grey
        rgb = np.clip(rgb, 0.0, 1.0)

    return np.concatenate([rgb, alpha], axis=2).astype(np.float32)


def _apply_mirror(frame: np.ndarray, direction: str) -> np.ndarray:
    """Flip/mirror along horizontal, vertical, or both axes."""
    frame = np.asarray(frame, dtype=np.float32)
    if frame.ndim != 3 or frame.shape[2] != 4:
        return frame

    direction = str(direction).lower()
    if direction in {"horizontal", "h", "x"}:
        return np.flip(frame, axis=1).copy().astype(np.float32)
    elif direction in {"vertical", "v", "y"}:
        return np.flip(frame, axis=0).copy().astype(np.float32)
    elif direction in {"both", "xy", "all"}:
        return np.flip(np.flip(frame, axis=0), axis=1).copy().astype(np.float32)
    return frame


def _apply_crop(frame: np.ndarray, x0: float, y0: float, x1: float, y1: float) -> np.ndarray:
    """Crop to normalized rectangle [x0, y0, x1, y1] ∈ [0,1]; outside α=0."""
    frame = np.asarray(frame, dtype=np.float32)
    if frame.ndim != 3 or frame.shape[2] != 4:
        return frame

    h, w = frame.shape[:2]
    x0, y0, x1, y1 = max(0.0, min(1.0, x0)), max(0.0, min(1.0, y0)), max(0.0, min(1.0, x1)), max(0.0, min(1.0, y1))
    if x0 >= x1 or y0 >= y1:
        # Crop area is degenerate; return fully transparent.
        return np.zeros_like(frame, dtype=np.float32)

    px0, py0 = int(x0 * w), int(y0 * h)
    px1, py1 = int(x1 * w), int(y1 * h)
    px0, py0, px1, py1 = max(0, px0), max(0, py0), min(w, px1), min(h, py1)

    result = np.zeros_like(frame, dtype=np.float32)
    if px0 < px1 and py0 < py1:
        result[py0:py1, px0:px1] = frame[py0:py1, px0:px1]
    return result


def _apply_vignette(frame: np.ndarray, amount: float) -> np.ndarray:
    """Apply radial edge darkening with gaussian falloff from center."""
    import cv2

    frame = np.asarray(frame, dtype=np.float32)
    if frame.ndim != 3 or frame.shape[2] != 4:
        return frame

    h, w = frame.shape[:2]
    amount = max(0.0, min(1.0, amount))

    if amount == 0.0:
        return frame

    # Create a gaussian falloff from center outward.
    cy, cx = h / 2.0, w / 2.0
    y, x = np.ogrid[:h, :w]
    dist = np.sqrt((x - cx) ** 2 + (y - cy) ** 2)
    max_dist = np.sqrt(cx ** 2 + cy ** 2)
    normalized_dist = np.clip(dist / max_dist, 0.0, 1.0)

    # Gaussian falloff: 1.0 at center, 0.0 at edges.
    falloff = np.exp(-2.0 * (normalized_dist ** 2))
    # Blend between 1.0 (no vignette) and falloff (full vignette).
    vignette = 1.0 - amount * (1.0 - falloff)
    vignette = np.stack([vignette, vignette, vignette, np.ones((h, w))], axis=2)

    rgb = frame[..., :3] * vignette[..., :3]
    alpha = frame[..., 3:4]
    return np.concatenate([rgb, alpha], axis=2).astype(np.float32)


def _apply_sharpen(frame: np.ndarray, amount: float) -> np.ndarray:
    """Apply unsharp mask: original + amount*(original - gaussian_blur)."""
    import cv2

    frame = np.asarray(frame, dtype=np.float32)
    if frame.ndim != 3 or frame.shape[2] != 4:
        return frame

    amount = max(0.0, amount)
    if amount == 0.0:
        return frame

    rgb = frame[..., :3]
    alpha = frame[..., 3:4]

    # Gaussian blur with radius 2.
    ksize = 5
    blurred = cv2.GaussianBlur(rgb, (ksize, ksize), 1.0)

    # Unsharp mask.
    sharpened = rgb + amount * (rgb - blurred)
    sharpened = np.clip(sharpened, 0.0, 1.0)

    return np.concatenate([sharpened, alpha], axis=2).astype(np.float32)


def _apply_hue_rotate(frame: np.ndarray, degrees: float) -> np.ndarray:
    """Rotate hue in HSV space."""
    import cv2

    frame = np.asarray(frame, dtype=np.float32)
    if frame.ndim != 3 or frame.shape[2] != 4:
        return frame

    rgb = frame[..., :3]
    alpha = frame[..., 3:4]

    # Convert to HSV. OpenCV expects BGR uint8, so convert.
    bgr_uint8 = (rgb[..., ::-1] * 255.0).astype(np.uint8)  # RGB -> BGR, float to uint8
    hsv = cv2.cvtColor(bgr_uint8, cv2.COLOR_BGR2HSV_FULL)  # HSV in [0,255]

    # Rotate hue channel.
    degrees = float(degrees)
    hue_shift = int((degrees / 360.0) * 255.0) % 256
    hsv[..., 0] = (hsv[..., 0].astype(np.int32) + hue_shift) % 256

    # Convert back.
    bgr_out = cv2.cvtColor(hsv.astype(np.uint8), cv2.COLOR_HSV2BGR_FULL)
    rgb_out = bgr_out[..., ::-1]  # BGR -> RGB
    rgb_out = rgb_out.astype(np.float32) / 255.0

    return np.concatenate([rgb_out, alpha], axis=2).astype(np.float32)


def _apply_chroma_key(frame: np.ndarray, key_color: str | tuple, threshold: float, softness: float) -> np.ndarray:
    """Distance-based alpha keying: pixels near key_color become transparent."""
    frame = np.asarray(frame, dtype=np.float32)
    if frame.ndim != 3 or frame.shape[2] != 4:
        return frame

    # Parse key_color.
    key_rgb = _rgba01(key_color, default=(0.0, 1.0, 0.0, 1.0))[:3]
    key_rgb = np.array(key_rgb, dtype=np.float32)

    rgb = frame[..., :3]
    alpha = frame[..., 3:4]

    # Euclidean distance in RGB space.
    dist = np.sqrt(np.sum((rgb - key_rgb) ** 2, axis=2, keepdims=True))

    # Distance-based keying: 0 when dist < threshold, 1 when dist > threshold+softness.
    threshold = max(0.0, threshold)
    softness = max(0.0, softness)
    edge = threshold + softness
    if edge <= threshold:
        # No softness; hard boundary.
        key_alpha = (dist > threshold).astype(np.float32)
    else:
        # Softness ramp.
        key_alpha = np.clip((dist - threshold) / softness, 0.0, 1.0)

    # Multiply into existing alpha.
    new_alpha = alpha * key_alpha
    return np.concatenate([rgb, new_alpha], axis=2).astype(np.float32)


def _apply_advanced_chroma_key(
    frame: np.ndarray,
    key_color: str | tuple,
    *,
    similarity: float,
    softness: float,
    spill: float,
    edge_blur: float,
) -> np.ndarray:
    """HSV-aware chroma key with soft edges and simple despill.

    This stays deterministic and dependency-light while being less brittle than
    raw RGB distance: hue/chroma distance decides the matte, optional blur
    softens the edge, and despill reduces the key-colour channel on kept pixels.
    """
    import cv2

    frame = np.asarray(frame, dtype=np.float32)
    if frame.ndim != 3 or frame.shape[2] != 4:
        return frame

    rgb = np.clip(frame[..., :3].copy(), 0.0, 1.0)
    alpha = frame[..., 3:4]
    key_rgb = np.array(_rgba01(key_color, default=(0.0, 1.0, 0.0, 1.0))[:3], dtype=np.float32)

    rgb_dist = np.sqrt(np.sum((rgb - key_rgb.reshape(1, 1, 3)) ** 2, axis=2, keepdims=True))

    hsv = cv2.cvtColor((rgb[..., ::-1] * 255.0).astype(np.uint8), cv2.COLOR_BGR2HSV_FULL).astype(np.float32) / 255.0
    key_hsv = cv2.cvtColor((key_rgb.reshape(1, 1, 3)[..., ::-1] * 255.0).astype(np.uint8), cv2.COLOR_BGR2HSV_FULL).astype(np.float32) / 255.0
    hue_dist = np.abs(hsv[..., 0:1] - float(key_hsv[0, 0, 0]))
    hue_dist = np.minimum(hue_dist, 1.0 - hue_dist)
    sat = hsv[..., 1:2]
    key_sat = float(key_hsv[0, 0, 1])
    sat_dist = np.abs(sat - key_sat) * 0.35

    # RGB distance guards grey/low-saturation pixels; hue distance catches soft
    # green/blue spill near subject edges.
    matte_dist = np.minimum(rgb_dist, hue_dist * 2.2 + sat_dist)
    similarity = max(0.0, float(similarity))
    softness = max(0.0, float(softness))
    if softness > 0:
        keep = np.clip((matte_dist - similarity) / softness, 0.0, 1.0)
    else:
        keep = (matte_dist > similarity).astype(np.float32)

    if edge_blur > 0:
        keep = cv2.GaussianBlur(keep[..., 0], (0, 0), sigmaX=edge_blur, sigmaY=edge_blur)[..., np.newaxis]
        keep = np.clip(keep, 0.0, 1.0)

    spill = max(0.0, min(1.0, float(spill)))
    if spill > 0:
        key_channel = int(np.argmax(key_rgb))
        other_channels = [i for i in range(3) if i != key_channel]
        neutral = np.max(rgb[..., other_channels], axis=2)
        spill_zone = np.clip(1.0 - keep[..., 0], 0.0, 1.0)
        spill_zone = np.clip(spill_zone + (1.0 - sat[..., 0]) * 0.15, 0.0, 1.0)
        target = np.minimum(rgb[..., key_channel], neutral)
        rgb[..., key_channel] = (
            rgb[..., key_channel] * (1.0 - spill * spill_zone)
            + target * (spill * spill_zone)
        )

    return np.concatenate([rgb, alpha * keep], axis=2).astype(np.float32)


def _apply_luma_key(frame: np.ndarray, *, threshold: float, softness: float, mode: str) -> np.ndarray:
    """Key by brightness. mode=key_dark removes dark pixels; key_bright removes bright."""
    frame = np.asarray(frame, dtype=np.float32)
    if frame.ndim != 3 or frame.shape[2] != 4:
        return frame
    rgb = frame[..., :3]
    alpha = frame[..., 3:4]
    luma = (0.299 * rgb[..., 0:1] + 0.587 * rgb[..., 1:2] + 0.114 * rgb[..., 2:3])
    threshold = max(0.0, min(1.0, float(threshold)))
    softness = max(0.0, float(softness))
    if mode in {"key_bright", "bright", "high"}:
        dist = threshold - luma
    else:
        dist = luma - threshold
    if softness > 0:
        keep = np.clip(dist / softness, 0.0, 1.0)
    else:
        keep = (dist >= 0.0).astype(np.float32)
    return np.concatenate([rgb, alpha * keep], axis=2).astype(np.float32)
