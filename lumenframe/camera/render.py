"""Compose the track and emit a self-contained, frame-safe preview.

Two jobs:

* :func:`compose_track` — flatten the base move (its named easing baked in) plus
  the seeded handheld layer into a single list of ``{t, scale, tx, ty, rot}``
  samples, each passed through :func:`~lumenframe.camera.camera.fit_to_frame`
  so the composed result is provably frame-safe. This is the canonical thing a
  ``transform`` layer consumes and the thing the taste-floor tests assert on.
* :func:`track_to_svg` — a self-contained SVG whose CSS ``@keyframes`` play the
  composed transform on a built-in placeholder. It is HyperFrames-safe by
  construction: no ``url()``, no ``data:``, no remote/``xlink`` refs, no
  ``<script>`` — :func:`validate_camera_svg` rejects any output that is not.

:func:`track_to_transform_ops` is the thin adapter that hands the same track to
a lumenframe transform layer as keyframes (so the library rides the transform
layer instead of forking one).
"""
from __future__ import annotations

from typing import Any

from lumenframe.camera import camera as cam

#: Hard ceiling on preview size (a transform preview is tiny; anything larger is
#: a bug we refuse to hand to the renderer).
MAX_SVG_BYTES = 200_000
_FORBIDDEN = ("url(", "data:", "<script", "xlink", "http://", "https://", "</script")

#: The taste-floor predicate the tests assert on is ``covers_frame(eps=0.75)``.
#: :func:`compose_track` fits translate against this *tighter* internal tolerance
#: so the reserved ``0.75 - _FIT_EPS`` px comfortably absorbs the emitted output
#: rounding (translate → 3 dp, scale → 5 dp, rot → 4 dp). The worst rounding
#: perturbation is well under a hundredth of a pixel; 0.15 px of reserve is a
#: ~15× margin and stays far from the ``k = 0`` covering guarantee.
_FIT_EPS = 0.6
#: The translate rounding quantum (3 dp) — the step the post-condition repair
#: marches back toward centre by if rounding ever tips a corner out.
_TX_QUANT = 0.001


def _snap_inside(scale: float, tx: float, ty: float, rot: float,
                 w: float, h: float) -> tuple[float, float]:
    """Hard post-condition: pull an *already rounded* sample back inside.

    The fit reserves enough headroom that this is a no-op on every realistic
    brief, but it is the belt that makes the frame-safety floor unbreakable: if
    output rounding ever nudged a corner across ``covers_frame(eps=0.75)`` we
    march ``(tx, ty)`` straight toward centre by the translate quantum until the
    asserted predicate holds. ``(0, 0)`` always covers, so this terminates.
    """
    if cam.covers_frame(scale, tx, ty, rot, w, h):
        return tx, ty

    def _toward_zero(v: float) -> float:
        if abs(v) < _TX_QUANT:
            return 0.0
        return round(v - _TX_QUANT, 3) if v > 0 else round(v + _TX_QUANT, 3)

    while not cam.covers_frame(scale, tx, ty, rot, w, h):
        tx, ty = _toward_zero(tx), _toward_zero(ty)
        if tx == 0.0 and ty == 0.0:
            break
    return tx, ty


def _base_at(keyframes: list[dict[str, Any]], t: float) -> tuple[float, float, float, float]:
    """The base move (scale, tx, ty, rot) at normalised time ``t``, eased.

    Locates the segment containing ``t`` and interpolates with that segment's
    named easing curve — so the eased "feel" the plan names is exactly what the
    composed sample reflects.
    """
    if t <= keyframes[0]["t"]:
        k = keyframes[0]
        return k["scale"], k["tx"], k["ty"], k["rot"]
    for i in range(1, len(keyframes)):
        a, b = keyframes[i - 1], keyframes[i]
        if t <= b["t"] or i == len(keyframes) - 1:
            span = (b["t"] - a["t"]) or 1.0
            u = min(max((t - a["t"]) / span, 0.0), 1.0)
            e = cam.cubic_bezier(b["ease"] or "ease_in_out", u)
            return (
                a["scale"] + (b["scale"] - a["scale"]) * e,
                a["tx"] + (b["tx"] - a["tx"]) * e,
                a["ty"] + (b["ty"] - a["ty"]) * e,
                a["rot"] + (b["rot"] - a["rot"]) * e,
            )
    k = keyframes[-1]
    return k["scale"], k["tx"], k["ty"], k["rot"]


def compose_track(track: dict[str, Any]) -> list[dict[str, Any]]:
    """Base move + handheld layer → frame-safe composed samples.

    Sample times follow the handheld grid when present (so no sine detail is
    lost); otherwise a duration-based uniform grid represents the eased move.
    Every sample is shrunk toward centre if needed so it covers the frame —
    determinism is preserved (pure maths, no RNG here).
    """
    keyframes = track["keyframes"]
    w = float(track["canvas"]["width"])
    h = float(track["canvas"]["height"])
    handheld = track.get("handheld")

    if handheld and handheld.get("samples"):
        times = [s["t"] for s in handheld["samples"]]
        noise = {s["t"]: s for s in handheld["samples"]}
    else:
        n = int(min(max(round(float(track["duration"]) * 8), 12), 48))
        times = [i / n for i in range(n + 1)]
        noise = {}

    out: list[dict[str, Any]] = []
    for t in times:
        scale, tx, ty, rot = _base_at(keyframes, t)
        ns = noise.get(t)
        if ns:
            tx += ns["tx"]
            ty += ns["ty"]
            rot += ns["rot"]
        # Round scale/rot to the values we will EMIT, then fit translate against
        # those exact values with headroom below the 0.75 taste floor — so the
        # only rounding left to absorb is the translate quantum. Round translate,
        # then hard-clamp the emitted sample back inside as a post-condition.
        rscale = round(scale, 5)
        rrot = round(rot, 4)
        tx, ty = cam.fit_to_frame(rscale, tx, ty, rrot, w, h, eps=_FIT_EPS)
        rtx, rty = _snap_inside(rscale, round(tx, 3), round(ty, 3), rrot, w, h)
        assert cam.covers_frame(rscale, rtx, rty, rrot, w, h), \
            "compose_track frame-safety post-condition violated"
        out.append({"t": round(t, 4), "scale": rscale,
                    "tx": rtx, "ty": rty, "rot": rrot})
    return out


def track_to_svg(track: dict[str, Any]) -> str:
    """A self-contained SVG that plays the composed track on a placeholder.

    The placeholder is a plain built-in scene (a ground, a horizon rule, and a
    ring + crosshair marking the focal subject) — enough to *read* the move.
    The animation is a CSS ``@keyframes`` block with a full ``translate scale
    rotate`` per sample; timing is ``linear`` because the easing is already
    baked into the sample positions.
    """
    w = int(track["canvas"]["width"])
    h = int(track["canvas"]["height"])
    dur = float(track["duration"])
    samples = compose_track(track)
    focal = track.get("focal") or {"x": 0.5, "y": 0.5}
    fx, fy = focal["x"] * w, focal["y"] * h

    frames = []
    for s in samples:
        pct = round(s["t"] * 100, 3)
        frames.append(
            f"{pct}%{{transform:translate({s['tx']:.2f}px,{s['ty']:.2f}px) "
            f"scale({s['scale']:.4f}) rotate({s['rot']:.3f}deg);}}"
        )
    keyframes_css = "".join(frames)

    # Placeholder scene — solid fills only, no url()/gradient refs.
    marker_r = max(18, int(min(w, h) * 0.05))
    horizon = int(h * 0.62)
    scene = (
        f'<rect x="0" y="0" width="{w}" height="{h}" fill="#0E1C2A"/>'
        f'<rect x="0" y="{horizon}" width="{w}" height="{h - horizon}" fill="#122536"/>'
        f'<line x1="0" y1="{horizon}" x2="{w}" y2="{horizon}" stroke="#1D3346" stroke-width="3"/>'
        f'<circle cx="{fx:.1f}" cy="{fy:.1f}" r="{marker_r}" fill="none" '
        f'stroke="#5FC6DE" stroke-width="4"/>'
        f'<line x1="{fx - marker_r * 1.6:.1f}" y1="{fy:.1f}" x2="{fx + marker_r * 1.6:.1f}" '
        f'y2="{fy:.1f}" stroke="#5FC6DE" stroke-width="2"/>'
        f'<line x1="{fx:.1f}" y1="{fy - marker_r * 1.6:.1f}" x2="{fx:.1f}" '
        f'y2="{fy + marker_r * 1.6:.1f}" stroke="#5FC6DE" stroke-width="2"/>'
    )

    style = (
        f"@keyframes cam_track{{{keyframes_css}}}"
        f".cam-stage{{transform-box:fill-box;transform-origin:50% 50%;"
        f"animation:cam_track {dur:.3f}s linear infinite;}}"
        f"@media (prefers-reduced-motion:reduce){{.cam-stage{{animation:none;}}}}"
    )

    svg = (
        f'<svg viewBox="0 0 {w} {h}" width="{w}" height="{h}" '
        f'xmlns="http://www.w3.org/2000/svg" preserveAspectRatio="xMidYMid slice">'
        f"<style>{style}</style>"
        f'<g class="cam-stage">{scene}</g>'
        f"</svg>"
    )
    return svg


def validate_camera_svg(svg: str) -> str:
    """Reject a preview that is unsafe or oversized; return it unchanged if ok.

    ``xmlns`` carries the SVG namespace URL, which is legitimate; every *other*
    ``http(s)`` occurrence (a remote ref) is refused, as are ``url()``,
    ``data:``, ``xlink`` and ``<script>``.
    """
    if not isinstance(svg, str) or not svg.startswith("<svg"):
        raise ValueError("preview is not an SVG document")
    if len(svg.encode("utf-8")) > MAX_SVG_BYTES:
        raise ValueError(f"preview exceeds {MAX_SVG_BYTES} bytes")
    if "viewBox" not in svg:
        raise ValueError("preview has no viewBox")
    probe = svg.replace('xmlns="http://www.w3.org/2000/svg"', "")
    for token in _FORBIDDEN:
        if token in probe:
            raise ValueError(f"preview contains forbidden token {token!r}")
    return svg


def track_to_transform_ops(track: dict[str, Any]) -> dict[str, Any]:
    """Adapt the track to a lumenframe ``transform`` layer payload.

    The library *rides* the transform layer rather than forking a renderer: the
    composed samples become transform keyframes (scale/translate/rotate in the
    layer's own units), and the base move + handheld are kept alongside so the
    move can be re-derived. This is the spec a transform-layer op consumes.
    """
    samples = compose_track(track)
    return {
        "type": "transform_track",
        "duration": track["duration"],
        "canvas": dict(track["canvas"]),
        "keyframes": [
            {"t": s["t"], "scale": s["scale"], "translate": [s["tx"], s["ty"]], "rotate": s["rot"]}
            for s in samples
        ],
        "source": {"move": track["move"], "base": track["keyframes"], "handheld": track.get("handheld")},
    }
