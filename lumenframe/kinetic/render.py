"""Compile a text scene into a self-contained, HyperFrames-safe animated SVG.

The scene from :mod:`lumenframe.kinetic.api` is a pure recipe of laid-out runs
with reveal tracks; this module turns it into one ``<svg>`` document with an
inline ``<style>`` of ``@keyframes`` — **no external anything**. It rides the
existing ``html`` layer (an SVG string is exactly what that layer renders); it
never forks a renderer.

HyperFrames safety is a hard contract, enforced by :func:`validate_svg`:

* no ``url(...)`` — so no external references and no CSS ``mask``/``filter`` refs;
* no ``data:`` URIs, no ``xlink``/``href`` of any kind, no ``<image>``;
* no ``<script>`` / ``<foreignObject>`` / ``<iframe>``;
* generic font families only (``sans-serif`` / ``serif``);
* a byte ceiling, so a runaway scene can never poison a document's renders.

Reveals are expressed with universally-supported CSS animation: whole-line
reveals animate the ``<text>`` (``translateY`` %, ``scale``, or a ``clip-path:
inset()`` wipe — a basic shape, not a ``url()`` mask); word/char reveals cascade
``opacity`` on ``<tspan>`` units. Hierarchy dimming rides ``fill-opacity`` so the
reveal's ``opacity`` animation multiplies cleanly on top of it.
"""
from __future__ import annotations

import re
from typing import Any

#: Hard byte ceiling for a single scene's SVG (a full-screen title is a few KB;
#: this is generous headroom, and a wall against a pathological credits roll).
MAX_SVG_BYTES = 300_000

#: Substrings that must never appear — the HyperFrames blocklist.
_FORBIDDEN = ("url(", "data:", "xlink", "href", "javascript:",
              "<script", "<foreignobject", "<iframe", "<image")

#: Inline event handlers (``onload=`` / ``onclick=`` …) — a script surface even
#: without a ``<script>`` tag; forbidden in any tag/attribute.
_EVENT_ATTR = re.compile(r"\son[a-z]+\s*=")

_ESCAPE = {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}


class RenderError(ValueError):
    """Raised when a scene compiles to something not render-safe."""


def _esc(text: str) -> str:
    return "".join(_ESCAPE.get(ch, ch) for ch in str(text))


def _norm_word(word: str) -> str:
    return "".join(ch for ch in str(word).lower() if ch.isalnum())


def _keyframes(scene: dict[str, Any]) -> str:
    """The ``@keyframes`` block — only the shapes the scene actually uses.

    ``translateY``/``scale`` are % / unitless against the element's own box
    (``transform-box: fill-box``), so one keyframe fits every size. The wipe is a
    ``clip-path: inset()`` basic shape (no ``url()``).
    """
    used = {r["reveal"]["keyframe"] for r in scene["runs"] if r.get("reveal")}
    blocks = [".ktx{transform-box:fill-box;transform-origin:50% 50%}"]
    if "ktRise" in used:
        blocks.append("@keyframes ktRise{from{opacity:0;transform:translateY(45%)}"
                      "to{opacity:1;transform:translateY(0)}}")
    if "ktPop" in used:
        blocks.append("@keyframes ktPop{from{opacity:0;transform:scale(0.72)}"
                      "to{opacity:1;transform:scale(1)}}")
    if "ktWipe" in used:
        blocks.append("@keyframes ktWipe{from{opacity:0.001;clip-path:inset(0 100% 0 0)}"
                      "to{opacity:1;clip-path:inset(0 0 0 0)}}")
    if "ktFade" in used:
        blocks.append("@keyframes ktFade{from{opacity:0}to{opacity:1}}")
    scroll = scene.get("scroll")
    if scroll:
        blocks.append(
            "@keyframes ktScroll{from{transform:translateY(%(from)spx)}"
            "to{transform:translateY(%(to)spx)}}" % scroll)
    return "".join(blocks)


def _anim(keyframe: str, dur: float, curve: str, delay: float) -> str:
    return f"animation:{keyframe} {round(dur, 3)}s {curve} {round(delay, 3)}s both"


def _emph_tspan(word: str, accent: str) -> str:
    return f'<tspan fill="{_esc(accent)}" fill-opacity="1">{_esc(word)}</tspan>'


def _spans_with_emphasis(text: str, emphasis: set[str], accent: str) -> str:
    """Render a static line, colouring any emphasis words with the accent hue."""
    if not emphasis:
        return _esc(text)
    out: list[str] = []
    for i, word in enumerate(text.split(" ")):
        if i:
            out.append(" ")
        out.append(_emph_tspan(word, accent) if _norm_word(word) in emphasis else _esc(word))
    return "".join(out)


def _run_common(run: dict[str, Any]) -> str:
    """The shared presentation attributes for a run's ``<text>`` element.

    ``family`` and ``color`` are string tokens that can originate from a
    caller-supplied palette/style, so they are escaped before interpolation — an
    unescaped ``"`` would otherwise break out of the attribute and smuggle in a
    handler (numeric fields are library-derived and safe).
    """
    return (f'x="{run["x"]}" y="{run["y"]}" '
            f'font-family="{_esc(run["family"])}" font-size="{run["size"]}" '
            f'font-weight="{run["weight"]}" fill="{_esc(run["color"])}" '
            f'fill-opacity="{run["fill_opacity"]}" '
            f'text-anchor="{run["align"]}" xml:space="preserve"')


def _render_run(run: dict[str, Any], emphasis: set[str], accent: str) -> str:
    rev = run.get("reveal")
    tracking = f'letter-spacing:{run["tracking"]}em'
    if rev is None:  # a scrolling / static run — the group animates it
        body = _spans_with_emphasis(run["text"], emphasis, accent)
        return f'<text {_run_common(run)} style="{tracking}">{body}</text>'

    unit = rev["unit"]
    curve = rev["ease_curve"]
    if unit == "run":
        anim = _anim(rev["keyframe"], rev["dur"], curve, rev["base_delay"])
        body = _spans_with_emphasis(run["text"], emphasis, accent)
        return (f'<text class="ktx" {_run_common(run)} '
                f'style="{tracking};{anim}">{body}</text>')

    # word / char cascade: parent text is static, each unit tspan fades in.
    parts: list[str] = []
    if unit == "word":
        words = run["text"].split(" ")
        for i, word in enumerate(words):
            if i:
                parts.append("<tspan> </tspan>")
            delay = rev["base_delay"] + i * rev["unit_stagger"]
            fill = f' fill="{_esc(accent)}" fill-opacity="1"' if _norm_word(word) in emphasis else ""
            parts.append(f'<tspan class="ktx"{fill} '
                         f'style="{_anim(rev["keyframe"], rev["dur"], curve, delay)}">'
                         f'{_esc(word)}</tspan>')
    else:  # char
        idx = 0
        for ch in run["text"]:
            if ch == " ":
                parts.append("<tspan> </tspan>")
                continue
            delay = rev["base_delay"] + idx * rev["unit_stagger"]
            parts.append(f'<tspan class="ktx" '
                         f'style="{_anim(rev["keyframe"], rev["dur"], curve, delay)}">'
                         f'{_esc(ch)}</tspan>')
            idx += 1
    return f'<text {_run_common(run)} style="{tracking}">{"".join(parts)}</text>'


def scene_to_svg(scene: dict[str, Any]) -> str:
    """Compile a validated scene recipe into its animated SVG document string."""
    w, h = scene["canvas"]["width"], scene["canvas"]["height"]
    accent = scene["palette"]["accent"]
    emphasis = set(scene.get("emphasis") or [])

    parts: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" '
        f'width="{w}" height="{h}" preserveAspectRatio="xMidYMid meet" '
        f'font-family="{_esc(scene["grid"]["family"])}">',
        f"<style>{_keyframes(scene)}</style>",
    ]
    bg = scene.get("background")
    if bg:
        parts.append(f'<rect x="0" y="0" width="{w}" height="{h}" fill="{_esc(bg)}"/>')

    body = "".join(_render_run(r, emphasis, accent) for r in scene["runs"])
    scroll = scene.get("scroll")
    if scroll:
        dur = round(scroll["dur"], 3)
        parts.append(f'<g class="ktx" style="animation:ktScroll {dur}s linear '
                     f'{round(scroll.get("delay", 0.0), 3)}s both">{body}</g>')
    else:
        parts.append(f"<g>{body}</g>")
    parts.append("</svg>")
    return "".join(parts)


def validate_svg(svg: str) -> str:
    """Assert the SVG is HyperFrames-safe and within budget; return it.

    Raises :class:`RenderError` on any violation — call it *before* the SVG
    reaches a document, so an unsafe scene can never poison later renders.
    """
    if not isinstance(svg, str) or not svg.lstrip().startswith("<svg"):
        raise RenderError("output is not an <svg> document")
    if not svg.rstrip().endswith("</svg>"):
        raise RenderError("<svg> is not closed")
    if "viewBox" not in svg:
        raise RenderError("<svg> has no viewBox (would not scale in the frame)")
    n = len(svg.encode("utf-8"))
    if n > MAX_SVG_BYTES:
        raise RenderError(f"svg too large: {n} bytes > {MAX_SVG_BYTES}")
    low = svg.lower()
    # The xmlns namespace is the one legitimate URI; check everything else.
    scan = low.replace('xmlns="http://www.w3.org/2000/svg"', "")
    # Scan *structure*, not copy: text-node bodies are _esc'd and cannot contain
    # raw markup, so a legitimate title carrying letters like "url(" / "data:" /
    # "href" / "xlink" must not trip the blocklist. Blank out every text node
    # (the ``>…<`` spans) so only tags and attributes — the real injection
    # surface — remain to be scanned.
    scan = re.sub(r">[^<]*<", "><", scan)
    for bad in _FORBIDDEN:
        if bad in scan:
            raise RenderError(f"forbidden token in svg: {bad!r}")
    if _EVENT_ATTR.search(scan):
        raise RenderError("forbidden inline event handler (on*=) in svg")
    return svg
