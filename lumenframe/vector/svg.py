"""VectorScene → one self-contained animated SVG document.

The output targets the HyperFrames render runtime (Chromium), which allows
modern CSS on SVG:

* node transforms animate via the **individual transform properties**
  (``translate`` / ``rotate`` / ``scale``, CSS Transforms L2) so each track
  stays an independent animation — no union-resampling of a monolithic
  ``transform`` except for ``x``+``y`` which merge into one ``translate``.
* ``draw`` uses ``pathLength="1"`` + ``stroke-dasharray: 1`` and animates
  ``stroke-dashoffset`` — renderer-portable, no measured path lengths.
* ``d`` morphs animate the CSS ``d: path(…)`` property; behaviours guarantee
  every value in a track shares one command structure (aligned cubics).
* per-segment easing is expressed inside ``@keyframes`` blocks via
  ``animation-timing-function`` on each step (CSS semantics: the function
  declared AT a keyframe shapes the segment leaving it).

Hard constraints (enforced here, validated upstream by
``gemia/hyperframes_adapter``): fully self-contained — no external URLs, no
``data:`` URIs, **no ``url()`` in CSS text** (gradients/filters are wired via
*presentation attributes* ``fill="url(#id)"``, which the CSS validator never
sees), no scripts. System font stack only.

Coordinates: the scene is canvas-centred; a root ``<g>`` translates to the
SVG's top-left space. Static node placement lives on a wrapper ``<g
transform="…">`` *attribute*; CSS animations on the element itself compose
with it (attribute × CSS property), so static pose and animated deltas never
fight.
"""
from __future__ import annotations

import re
from typing import Any

from lumenframe.vector import geometry, motion
from lumenframe.vector import scene as vscene

#: CSS property per track prop (x/y merge into translate separately).
_PROP_CSS: dict[str, str] = {
    "opacity": "opacity",
    "fill_opacity": "fill-opacity",
    "stroke_opacity": "stroke-opacity",
    "draw": "stroke-dashoffset",
    "d": "d",
    "fill": "fill",
    "stroke": "stroke",
    "scale": "scale",
    "rotation": "rotate",
}

_ID_RE = re.compile(r"[^A-Za-z0-9_-]+")


class SvgCompileError(ValueError):
    """Raised when a scene cannot be compiled to SVG."""


def compile_scene(scene: dict[str, Any], *, standalone: bool = False) -> str:
    """Compile a validated VectorScene into an animated SVG string.

    ``standalone=False`` (default) emits the HTML-embedding form WITHOUT an
    ``xmlns`` declaration: HTML5 parsers namespace inline ``<svg>``
    automatically, and the HyperFrames validator rejects any ``//`` in the
    stage html — which the xmlns URI would trip. ``standalone=True`` adds
    the namespace for a self-contained ``.svg`` file deliverable.
    """
    vscene.validate_scene(scene)
    width = int(scene["width"])
    height = int(scene["height"])
    duration = float(scene["duration"])

    ctx = _Ctx(duration=duration)
    body: list[str] = []
    for node in scene.get("nodes") or []:
        body.append(_emit_node(node, ctx))

    background = scene.get("background")
    bg = (
        f'<rect x="0" y="0" width="{width}" height="{height}" fill="{_esc(str(background))}"/>'
        if background
        else ""
    )
    defs = f"<defs>{''.join(ctx.defs)}</defs>" if ctx.defs else ""
    css = "\n".join(ctx.css)
    xmlns = ' xmlns="http://www.w3.org/2000/svg"' if standalone else ""
    return (
        f'<svg{xmlns} width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}">'
        f"<style>{css}</style>{defs}{bg}"
        f'<g transform="translate({width / 2:g}, {height / 2:g})">'
        f"{''.join(body)}"
        "</g></svg>"
    )


class _Ctx:
    """Per-compile accumulator: CSS rules, defs, id uniqueness."""

    def __init__(self, *, duration: float) -> None:
        self.duration = duration
        self.css: list[str] = []
        self.defs: list[str] = []
        self._used: set[str] = set()
        self._glow = False

    def uid(self, raw: str) -> str:
        base = _ID_RE.sub("_", str(raw)) or "n"
        if base[0].isdigit():
            base = "n" + base
        cand, i = base, 1
        while cand in self._used:
            i += 1
            cand = f"{base}_{i}"
        self._used.add(cand)
        return cand

    def glow_filter(self) -> str:
        """Register the shared soft-glow filter once; return its id."""
        if not self._glow:
            self._glow = True
            self.defs.append(
                '<filter id="vglow" x="-60%" y="-60%" width="220%" height="220%">'
                '<feGaussianBlur stdDeviation="6" result="b"/>'
                '<feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge>'
                "</filter>"
            )
        return "vglow"


# ── node emission ────────────────────────────────────────────────────────


def _emit_node(node: dict[str, Any], ctx: _Ctx) -> str:
    kind = node.get("kind")
    nid = ctx.uid(node.get("id") or kind)

    if kind == "group":
        inner = "".join(_emit_node(child, ctx) for child in node.get("children") or [])
        element = f'<g id="{nid}">{inner}</g>'
    elif kind == "path":
        element = _path_element(node, nid, ctx)
    elif kind == "text":
        element = _text_element(node, nid, ctx)
    elif kind == "particles":
        element = _particles_element(node, nid, ctx)
    else:  # pragma: no cover - validate_scene guards this
        raise SvgCompileError(f"unknown node kind {kind!r}")

    _emit_animations(node, nid, ctx)
    wrapper_tf = _static_transform(node)
    if wrapper_tf:
        return f'<g transform="{wrapper_tf}">{element}</g>'
    return element


def _static_transform(node: dict[str, Any]) -> str:
    tf = node.get("transform") or {}
    x, y = float(tf.get("x") or 0), float(tf.get("y") or 0)
    scale = float(tf.get("scale", 1.0))
    rot = float(tf.get("rotation") or 0)
    parts: list[str] = []
    if abs(x) > 1e-9 or abs(y) > 1e-9:
        parts.append(f"translate({x:g}, {y:g})")
    if abs(rot) > 1e-9:
        parts.append(f"rotate({rot:g})")
    if abs(scale - 1.0) > 1e-9:
        parts.append(f"scale({scale:g})")
    return " ".join(parts)


def _paint(value: Any, nid: str, which: str, ctx: _Ctx) -> str:
    """A paint attribute value: hex string, or a gradient dict → defs + url ref.

    Gradient dict: ``{"gradient": [[pos, "#hex"], …], "angle": degrees}``.
    The reference is emitted as a *presentation attribute* (never CSS url()).
    """
    if value is None:
        return "none"
    if isinstance(value, str):
        return _esc(value)
    if isinstance(value, dict) and value.get("gradient"):
        gid = ctx.uid(f"g_{nid}_{which}")
        angle = float(value.get("angle") or 0.0)
        vec = geometry.vrot((1.0, 0.0), angle)
        x1, y1 = 0.5 - vec[0] / 2, 0.5 - vec[1] / 2
        x2, y2 = 0.5 + vec[0] / 2, 0.5 + vec[1] / 2
        stops = "".join(
            f'<stop offset="{float(pos) * 100:g}%" stop-color="{_esc(str(color))}"/>'
            for pos, color in value["gradient"]
        )
        ctx.defs.append(
            f'<linearGradient id="{gid}" x1="{x1:g}" y1="{y1:g}" x2="{x2:g}" y2="{y2:g}">'
            f"{stops}</linearGradient>"
        )
        return f"url(#{gid})"
    raise SvgCompileError(f"unsupported paint value {value!r}")


def _style_attrs(node: dict[str, Any], nid: str, ctx: _Ctx) -> str:
    style = node.get("style") or {}
    attrs: list[str] = []
    attrs.append(f'fill="{_paint(style.get("fill"), nid, "f", ctx)}"')
    stroke = style.get("stroke")
    if stroke:
        attrs.append(f'stroke="{_paint(stroke, nid, "s", ctx)}"')
        attrs.append(f'stroke-width="{float(style.get("stroke_width") or 1.0):g}"')
        attrs.append(f'stroke-linecap="{_esc(str(style.get("line_cap") or "round"))}"')
        attrs.append(f'stroke-linejoin="{_esc(str(style.get("line_join") or "round"))}"')
    opacity = float(style.get("opacity", 1.0))
    if opacity < 1.0 - 1e-9:
        attrs.append(f'opacity="{opacity:g}"')
    if style.get("glow"):
        attrs.append(f'filter="url(#{ctx.glow_filter()})"')
    return " ".join(attrs)


def _path_element(node: dict[str, Any], nid: str, ctx: _Ctx) -> str:
    d = geometry.to_svg_d(node["path"])
    extra = ""
    if "draw" in (node.get("tracks") or {}):
        # Normalised draw-on: dash pattern spans exactly one path length.
        extra = ' pathLength="1" stroke-dasharray="1"'
    return f'<path id="{nid}" d="{d}" {_style_attrs(node, nid, ctx)}{extra}/>'


def _text_element(node: dict[str, Any], nid: str, ctx: _Ctx) -> str:
    spec = node.get("text") or {}
    anchor = {"left": "start", "center": "middle", "right": "end"}.get(
        str(spec.get("align") or "center"), "middle"
    )
    ls = float(spec.get("letter_spacing") or 0.0)
    ls_attr = f' letter-spacing="{ls:g}"' if abs(ls) > 1e-9 else ""
    return (
        f'<text id="{nid}" x="0" y="0" text-anchor="{anchor}" '
        f'dominant-baseline="central" font-family="{_esc(str(spec.get("font_family")))}" '
        f'font-size="{float(spec.get("font_size") or 96):g}" '
        f'font-weight="{_esc(str(spec.get("font_weight") or 700))}"{ls_attr} '
        f"{_style_attrs(node, nid, ctx)}>{_esc(str(spec.get('content')))}</text>"
    )


def _particles_element(node: dict[str, Any], nid: str, ctx: _Ctx) -> str:
    style = node.get("style") or {}
    fill = style.get("fill")
    parts: list[str] = []
    for k, inst in enumerate((node.get("particles") or {}).get("instances") or []):
        iid = f"{nid}_i{k}"
        x, y, r = float(inst.get("x") or 0), float(inst.get("y") or 0), float(inst.get("r") or 3)
        paint = _paint(inst.get("fill", fill), iid, "f", ctx)
        shape = str(inst.get("shape") or "dot")
        rest_op = float(inst.get("opacity", 1.0))
        # A static instance carries its rest opacity as a presentation
        # attribute; an animated one gets it as the opacity track's base
        # (scaled in _emit_animations), so either way the design is honoured.
        op_attr = "" if (abs(rest_op - 1.0) <= 1e-6 or "opacity" in (inst.get("tracks") or {})) \
            else f' opacity="{_g(rest_op)}"'
        if shape == "spark":
            d = geometry.to_svg_d(geometry.star((0.0, 0.0), r * 1.6, r * 0.5, 4))
            el = f'<path id="{iid}" d="{d}" fill="{paint}"{op_attr}/>'
        else:
            el = f'<circle id="{iid}" cx="0" cy="0" r="{r:g}" fill="{paint}"{op_attr}/>'
        # Instance rest position on the wrapper; instance tracks animate deltas.
        parts.append(f'<g transform="translate({x:g}, {y:g})">{el}</g>')
        _emit_animations(inst, iid, ctx, base_opacity=rest_op)
    glow = f' filter="url(#{ctx.glow_filter()})"' if style.get("glow") else ""
    return f'<g id="{nid}"{glow}>{"".join(parts)}</g>'


# ── animation emission ───────────────────────────────────────────────────


def _emit_animations(owner: dict[str, Any], nid: str, ctx: _Ctx, *, base_opacity: float = 1.0) -> None:
    """Emit @keyframes + the composed ``animation`` rule for one element."""
    tracks: dict[str, list[dict[str, Any]]] = dict(owner.get("tracks") or {})
    if not tracks:
        return

    anims: list[tuple[str, float, float]] = []  # (name, delay, dur)
    needs_transform_box = False

    # x + y merge into one translate animation.
    tx, ty = tracks.pop("x", None), tracks.pop("y", None)
    if tx or ty:
        points = _merge_xy(tx or [], ty or [])
        name = f"a_{nid}_tr"
        anims.append(_keyframes(ctx, name, points, "translate",
                                lambda v: f"{_g(v[0])}px {_g(v[1])}px"))
        needs_transform_box = True

    # The element's DESIGNED base opacity (style.opacity for a node, the rest
    # opacity for a particle instance). An opacity track is 0..1 progress, so
    # it must scale by this base — otherwise a blob designed at 0.4 opacity
    # animates to a fully opaque 1.0, destroying layered translucency.
    style = owner.get("style") or {}
    base_op = float(style.get("opacity", base_opacity))

    for prop in sorted(tracks):
        points = tracks[prop]
        css_prop = _PROP_CSS.get(prop)
        if css_prop is None:  # pragma: no cover - validate_scene guards
            raise SvgCompileError(f"track prop {prop!r} has no CSS mapping")
        if prop in ("scale", "rotation"):
            needs_transform_box = True
        name = f"a_{nid}_{prop}"
        fmt = _formatter(prop)
        if prop == "opacity" and abs(base_op - 1.0) > 1e-6:
            fmt = lambda v, _b=base_op: _g(max(0.0, min(1.0, float(v))) * _b)
        anims.append(_keyframes(ctx, name, points, css_prop, fmt))

    names = ", ".join(a[0] for a in anims)
    durs = ", ".join(f"{_g(a[2])}s" for a in anims)
    delays = ", ".join(f"{_g(a[1])}s" for a in anims)
    fills = ", ".join(["both"] * len(anims))
    # CSS's initial animation-timing-function is `ease`, and keyframe steps
    # WITHOUT their own timing-function inherit the ELEMENT's value — so any
    # segment we author as "linear" (baked curves in orbit/scatter/drift, the
    # dense _merge_xy resamples) would silently ease unless we set the element
    # default to linear here. Per-keyframe eases still override this per step.
    timings = ", ".join(["linear"] * len(anims))
    extra = "transform-box: fill-box; transform-origin: center; " if needs_transform_box else ""
    ctx.css.append(
        f"#{nid} {{ {extra}animation-name: {names}; animation-duration: {durs}; "
        f"animation-delay: {delays}; animation-fill-mode: {fills}; "
        f"animation-timing-function: {timings}; }}"
    )


def _keyframes(
    ctx: _Ctx,
    name: str,
    points: list[dict[str, Any]],
    css_prop: str,
    fmt,
) -> tuple[str, float, float]:
    """Emit one @keyframes rule; return (name, delay_s, duration_s)."""
    if not points:
        raise SvgCompileError(f"empty track for {name}")
    t0 = float(points[0]["t"])
    t1 = float(points[-1]["t"])
    dur = max(t1 - t0, 0.001)

    steps: list[str] = []
    for p in points:
        pct = (float(p["t"]) - t0) / dur * 100.0
        ease = str(p.get("ease") or "linear")
        timing = "" if ease == "linear" else f" animation-timing-function: {motion.ease_to_css(ease)};"
        steps.append(f"{_g(round(pct, 4))}% {{ {css_prop}: {fmt(p['value'])};{timing} }}")
    ctx.css.append(f"@keyframes {name} {{ {' '.join(steps)} }}")
    return (name, t0, dur)


def _merge_xy(
    tx: list[dict[str, Any]], ty: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Merge x/y tracks into translate keyframes of (x, y) tuples.

    When both tracks share identical times (the common case — builders write
    them together) the merge is exact, keeping each segment's ease from the
    x track (they were authored as one gesture). Otherwise both tracks are
    resampled at the union of their times plus 3 subdivisions per interval —
    curves bake to dense linear keyframes, preserving shape within ~1%.
    """
    times_x = [round(float(p["t"]), 6) for p in tx]
    times_y = [round(float(p["t"]), 6) for p in ty]
    if tx and ty and times_x == times_y:
        return [
            {"t": px["t"], "value": (float(px["value"]), float(py["value"])),
             "ease": px.get("ease", "linear")}
            for px, py in zip(tx, ty)
        ]
    if not ty:
        return [{"t": p["t"], "value": (float(p["value"]), 0.0), "ease": p.get("ease")} for p in tx]
    if not tx:
        return [{"t": p["t"], "value": (0.0, float(p["value"])), "ease": p.get("ease")} for p in ty]

    union = sorted(set(times_x) | set(times_y))
    dense: list[float] = []
    for a, b in zip(union, union[1:]):
        dense.append(a)
        for i in range(1, 4):
            dense.append(round(a + (b - a) * i / 4.0, 6))
    dense.append(union[-1])
    return [
        {"t": t, "value": (_sample(tx, t), _sample(ty, t)), "ease": "linear"}
        for t in dense
    ]


def _sample(points: list[dict[str, Any]], t: float) -> float:
    """Evaluate a scalar track at time ``t`` honouring per-segment easing."""
    if not points:
        return 0.0
    if t <= float(points[0]["t"]):
        return float(points[0]["value"])
    for a, b in zip(points, points[1:]):
        ta, tb = float(a["t"]), float(b["t"])
        if ta <= t <= tb:
            if tb - ta <= 1e-9:
                return float(b["value"])
            u = motion.ease_value(str(a.get("ease") or "linear"), (t - ta) / (tb - ta))
            return float(a["value"]) + (float(b["value"]) - float(a["value"])) * u
    return float(points[-1]["value"])


def _formatter(prop: str):
    if prop == "draw":
        return lambda v: _g(max(0.0, min(1.0, 1.0 - float(v))))
    if prop == "d":
        return lambda v: f'path("{geometry.to_svg_d(v)}")'
    if prop in ("fill", "stroke"):
        return lambda v: _esc(str(v))
    if prop == "rotation":
        return lambda v: f"{_g(float(v))}deg"
    return lambda v: _g(float(v))


def _g(v: float) -> str:
    s = f"{float(v):.4f}".rstrip("0").rstrip(".")
    return s if s not in ("", "-0") else "0"


_ESC = {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}


def _esc(s: str) -> str:
    out = []
    for ch in s:
        out.append(_ESC.get(ch, ch))
    return "".join(out)
