"""Riding the effect layer — a recipe becomes a preview SVG + an ffmpeg filter.

The grade library never renders pixels; it emits two things the *existing*
pipeline consumes:

* :func:`grade_preview_svg` — a self-contained SVG that bakes the recipe into an
  SVG ``<filter>`` (``feColorMatrix`` for white balance, ``feComponentTransfer``
  for the per-channel tone curve, ``feColorMatrix type="saturate"`` for
  saturation) applied to a **built-in** test swatch (a grey ramp + skin/sky/
  foliage patches). This lets a colourist see the grade on a neutral reference
  without any footage. It is HyperFrames-safe: an inner fragment (no
  ``<html>/<head>/<body>``), no ``data:``/remote/``//`` URLs, no ``xlink``, no
  ``<script>`` — only internal ``url(#id)`` fragment references, which the
  HyperFrames HTML validator permits.
* :func:`grade_ffmpeg_filter` — an ``eq``/``curves``/``colorbalance`` filter
  string so the same recipe can grade real frames in the render pipeline.

:func:`validate_grade_svg` is the render-safety gate: it rejects any unsafe or
oversized SVG before it can reach a document.
"""
from __future__ import annotations

import re
from typing import Any

from lumenframe.grade import grade as _g

#: Hard byte cap for a preview SVG (a swatch is tiny; anything large is a bug).
MAX_SVG_BYTES = 60_000

#: Tokens that must never appear in a HyperFrames-safe SVG fragment. ``url(#``
#: (internal fragment) is explicitly allowed; ``//``, ``data:``, ``xlink`` and
#: script are not.
_UNSAFE_RE = re.compile(
    r"<script|</?(?:html|head|body)\b|xlink:|javascript:|data:|(?:https?:)?//|@import|<iframe",
    re.IGNORECASE,
)

#: The built-in reference swatch: (label, rgb 0..1). A grey ramp is drawn
#: separately; these are the memory colours a colourist watches.
_SWATCH: tuple[tuple[str, tuple[float, float, float]], ...] = (
    ("skin", (0.78, 0.52, 0.38)),
    ("sky", (0.36, 0.55, 0.78)),
    ("foliage", (0.35, 0.52, 0.30)),
    ("neutral", (0.5, 0.5, 0.5)),
)

_CURVE_SAMPLES = 17  # feComponentTransfer table resolution


def _fmt(x: float, nd: int = 5) -> str:
    """Deterministic float formatting: fixed decimals, no trailing-zero drift."""
    return f"{round(float(x), nd):.{nd}f}".rstrip("0").rstrip(".") or "0"


def _channel_table(recipe: dict[str, Any], ch: str) -> str:
    """``tableValues`` for one channel's ``feComponentTransfer`` function."""
    vals = [_g.tone_channel(i / (_CURVE_SAMPLES - 1), recipe, ch)
            for i in range(_CURVE_SAMPLES)]
    return " ".join(_fmt(v) for v in vals)


def _wb_matrix(recipe: dict[str, Any]) -> str:
    """A 4×5 ``feColorMatrix`` for white balance (channel gains only).

    Mirrors the temperature/tint maths in :func:`grade.apply_recipe_rgb` so the
    preview matches the authoritative colour path.
    """
    t, ti = recipe["temperature"], recipe["tint"]
    rg = (1.0 + 0.45 * t) * (1.0 + 0.15 * ti)
    gg = (1.0 - 0.22 * ti)
    bg = (1.0 - 0.45 * t) * (1.0 + 0.15 * ti)
    rows = [
        f"{_fmt(rg)} 0 0 0 0",
        f"0 {_fmt(gg)} 0 0 0",
        f"0 0 {_fmt(bg)} 0 0",
        "0 0 0 1 0",
    ]
    return "  ".join(rows)


def grade_preview_svg(recipe: dict[str, Any], width: int = 480, height: int = 120) -> str:
    """A self-contained, HyperFrames-safe SVG preview of the recipe on a swatch.

    The filter chain is WB → per-channel tone curve → saturation, applied to a
    grey ramp and the memory-colour patches. A ``vignette`` overlay approximates
    the optical finish. Deterministic: identical bytes for identical recipes.
    """
    w, h = int(width), int(height)
    # feColorMatrix saturate is uniform, so approximate the low-sat-weighted
    # vibrance boost by folding half of it into the effective saturation (still
    # under the enforced ceiling — the swatch mirrors apply_recipe_rgb).
    sat_eff = min(_g.SAT_CEILING, recipe["saturation"] + recipe.get("vibrance", 0.0) * 0.5)
    sat = _g.clamp01(sat_eff / max(1e-6, _g.SAT_CEILING)) * _g.SAT_CEILING
    fid = "grade"
    parts: list[str] = [
        f'<svg viewBox="0 0 {w} {h}" width="{w}" height="{h}" '
        'preserveAspectRatio="none" role="img" aria-label="grade preview swatch">',
        "<defs>",
        f'<linearGradient id="ramp" x1="0" y1="0" x2="1" y2="0">'
        f'<stop offset="0" stop-color="#000000"/>'
        f'<stop offset="1" stop-color="#ffffff"/></linearGradient>',
        f'<filter id="{fid}" color-interpolation-filters="sRGB" '
        'x="0" y="0" width="100%" height="100%">',
        f'<feColorMatrix type="matrix" values="{_wb_matrix(recipe)}"/>',
        '<feComponentTransfer>',
        f'<feFuncR type="table" tableValues="{_channel_table(recipe, "r")}"/>',
        f'<feFuncG type="table" tableValues="{_channel_table(recipe, "g")}"/>',
        f'<feFuncB type="table" tableValues="{_channel_table(recipe, "b")}"/>',
        '</feComponentTransfer>',
        f'<feColorMatrix type="saturate" values="{_fmt(sat)}"/>',
        '</filter>',
        f'<radialGradient id="vig" cx="0.5" cy="0.5" r="0.75">'
        f'<stop offset="0.55" stop-color="#000000" stop-opacity="0"/>'
        f'<stop offset="1" stop-color="#000000" stop-opacity="{_fmt(recipe["vignette"])}"/>'
        f'</radialGradient>',
        "</defs>",
        f'<g filter="url(#{fid})">',
        f'<rect x="0" y="0" width="{w}" height="{h}" fill="url(#ramp)"/>',
    ]
    patch_w = w / len(_SWATCH)
    band_h = h * 0.42
    for i, (_label, (r, g, b)) in enumerate(_SWATCH):
        hexc = "#%02x%02x%02x" % (round(r * 255), round(g * 255), round(b * 255))
        parts.append(
            f'<rect x="{_fmt(i * patch_w)}" y="{_fmt(h - band_h)}" '
            f'width="{_fmt(patch_w)}" height="{_fmt(band_h)}" fill="{hexc}"/>'
        )
    parts.append("</g>")
    if recipe["vignette"] > 0.0:
        parts.append(f'<rect x="0" y="0" width="{w}" height="{h}" fill="url(#vig)"/>')
    parts.append("</svg>")
    return "".join(parts)


def grade_ffmpeg_filter(recipe: dict[str, Any]) -> str:
    """An ffmpeg filtergraph string (``eq``/``colorbalance``/``vignette``).

    A faithful-enough approximation of the recipe for the real render pipeline:
    ``eq`` carries contrast/saturation/gamma/brightness, ``colorbalance`` carries
    the shadow/highlight split, and ``vignette`` the mood. Deterministically
    formatted so the string is byte-stable per recipe.
    """
    amount = recipe["contrast"]["amount"]
    contrast = 1.0 + amount  # ffmpeg eq contrast is 1.0-neutral
    brightness = _g._lift_luma(recipe) * 0.5  # faded floor → slight brightness
    gamma = sum(recipe["gamma"].values()) / 3.0
    lift, gain = recipe["lift"], recipe["gain"]
    # colorbalance shadows (rs/gs/bs) from the lift chroma, highlights (rh/gh/bh)
    # from the gain chroma; both re-centred so a neutral recipe reads as zeros.
    lm = _g._lift_luma(recipe)
    gm = sum(gain.values()) / 3.0
    # fold vibrance into eq saturation (uniform approximation), capped at ceiling.
    sat_eff = min(_g.SAT_CEILING, recipe["saturation"] + recipe.get("vibrance", 0.0) * 0.5)
    parts = [
        f"eq=contrast={_fmt(contrast, 4)}:saturation={_fmt(sat_eff, 4)}"
        f":gamma={_fmt(gamma, 4)}:brightness={_fmt(brightness, 4)}",
        f"colorbalance=rs={_fmt(lift['r'] - lm, 4)}:gs={_fmt(lift['g'] - lm, 4)}"
        f":bs={_fmt(lift['b'] - lm, 4)}:rh={_fmt(gain['r'] - gm, 4)}"
        f":gh={_fmt(gain['g'] - gm, 4)}:bh={_fmt(gain['b'] - gm, 4)}",
    ]
    if recipe["vignette"] > 0.0:
        parts.append("vignette=PI/4")
    return ",".join(parts)


def validate_grade_svg(svg: str) -> None:
    """Render-safety gate: reject unsafe or oversized preview SVG.

    Raises :class:`ValueError` for a full document, any unsafe token
    (``//``/``data:``/``xlink``/script/``@import``/iframe) or an oversized
    payload. Internal ``url(#id)`` fragment references pass.
    """
    if not isinstance(svg, str) or not svg.strip():
        raise ValueError("preview SVG is empty")
    if not svg.lstrip().startswith("<svg"):
        raise ValueError("preview SVG must start with <svg>")
    if len(svg.encode("utf-8")) > MAX_SVG_BYTES:
        raise ValueError(f"preview SVG exceeds {MAX_SVG_BYTES} bytes")
    m = _UNSAFE_RE.search(svg)
    if m:
        raise ValueError(f"preview SVG contains an unsafe token: {m.group(0)!r}")


def validate_grade_recipe(recipe: dict[str, Any]) -> None:
    """Physical-limit gate on a recipe (defends the taste floor at the edge).

    Raises :class:`ValueError` if any enforced bound is violated: saturation over
    the ceiling, black/white points outside the hard band, or a non-finite value.
    """
    import math

    def _finite(x: float) -> bool:
        return isinstance(x, (int, float)) and math.isfinite(x)

    if not (0.0 <= recipe["saturation"] <= _g.SAT_CEILING + 1e-6):
        raise ValueError(f"saturation {recipe['saturation']} exceeds ceiling {_g.SAT_CEILING}")
    if not (0.0 <= recipe["black_point"] <= _g.BLACK_POINT_HARD_MAX + 1e-6):
        raise ValueError(f"black_point {recipe['black_point']} outside safe band")
    if not (_g.WHITE_POINT_HARD_MIN - 1e-6 <= recipe["white_point"] <= 1.0):
        raise ValueError(f"white_point {recipe['white_point']} outside safe band")
    for wheel in ("lift", "gamma", "gain"):
        for ch in "rgb":
            if not _finite(recipe[wheel][ch]):
                raise ValueError(f"{wheel}.{ch} is not finite")
