"""Design tokens for the scene-template library — the *element* layer.

Where a template (``intro``, ``bullet_list``, …) is a *structure* — a named
arrangement of layers — this module is the shared box of **atoms** every
structure is built from: a small set of curated colour palettes, a type scale
that tracks the canvas height, and safe-area / positioning helpers so a template
lays out correctly on a 16:9 *or* a 9:16 canvas.

Nothing here touches the document or emits an op. These are pure lookups + a
little arithmetic; the templates import them so a caller can restyle a whole
scene by naming one palette (``palette="lumeri"``) instead of hand-passing a
dozen hex strings, and so a component pinned to the "lower third" lands in the
lower third of *whatever* canvas it is given.

Coordinate convention (matching :data:`lumenframe.model.DEFAULT_TRANSFORM`):
canvas is ``width × height`` px, the transform origin ``(0, 0)`` is dead-centre,
``x`` grows right and ``y`` grows *down*. Every helper returns px-from-centre.
"""
from __future__ import annotations

from typing import Any

#: Default canvas the helpers assume when a template is called without an
#: explicit size (matches :data:`lumenframe.model.DEFAULT_CANVAS`).
DEFAULT_W: int = 1920
DEFAULT_H: int = 1080


# ── palettes ──────────────────────────────────────────────────────────────
#
# A palette is a flat dict of named roles. Keys are stable across palettes so a
# template only ever reads ``p["accent"]`` / ``p["text"]`` / … and works with
# any palette (or a caller's custom override). ``grad`` is a background gradient
# as ``[[pos0..1, "#rrggbb"], …]`` for ``add_gradient``.
#
#   bg          full-canvas base fill (darkest / lightest ground)
#   surface     raised panel / bar / chip fill
#   text        primary type on ``bg``
#   subtext     secondary / caption type
#   accent      brand / emphasis colour (rules, big numbers, key words)
#   accent_soft lighter accent for secondary emphasis / gradients
#   grad        background gradient stops

PALETTES: dict[str, dict[str, Any]] = {
    # Lumeri brand — ice-blue light-ribbons on deep space (the house palette).
    "lumeri": {
        "bg": "#0A0E14",
        "surface": "#131C27",
        "text": "#F2FAFD",
        "subtext": "#9DB4C0",
        "accent": "#5FC6DE",
        "accent_soft": "#8BD8EA",
        "grad": [[0.0, "#070B11"], [0.55, "#0E1C2A"], [1.0, "#16405A"]],
    },
    # Professional neutral dark — the safe default for talking-head / explainer.
    "ink": {
        "bg": "#0B0D10",
        "surface": "#181C22",
        "text": "#FFFFFF",
        "subtext": "#9AA3AD",
        "accent": "#4C8BF5",
        "accent_soft": "#7FB0FF",
        "grad": [[0.0, "#080A0D"], [1.0, "#141A22"]],
    },
    # Light / office — decks, docs, product walkthroughs on a clean ground.
    "paper": {
        "bg": "#F6F8FB",
        "surface": "#FFFFFF",
        "text": "#12151A",
        "subtext": "#5B6672",
        "accent": "#2B6EF2",
        "accent_soft": "#6FA0FF",
        "grad": [[0.0, "#FFFFFF"], [1.0, "#E9EFF6"]],
    },
    # Editorial mono — high-contrast black/white for quotes & statements.
    "noir": {
        "bg": "#050505",
        "surface": "#141414",
        "text": "#FFFFFF",
        "subtext": "#8A8A8A",
        "accent": "#FFFFFF",
        "accent_soft": "#CFCFCF",
        "grad": [[0.0, "#000000"], [1.0, "#151515"]],
    },
    # Warm — lifestyle / launch / celebratory moments.
    "sunset": {
        "bg": "#1A0F14",
        "surface": "#2A171E",
        "text": "#FFF3EC",
        "subtext": "#C9A69A",
        "accent": "#FF7A59",
        "accent_soft": "#FFC24B",
        "grad": [[0.0, "#1A0F14"], [0.6, "#3A1B22"], [1.0, "#7A3B2E"]],
    },
}

#: The default palette used when a template is called with no ``palette``.
DEFAULT_PALETTE = "lumeri"


def resolve_palette(palette: str | dict[str, Any] | None) -> dict[str, Any]:
    """Return a full palette dict for a name, a partial override, or ``None``.

    * ``None`` → the default palette (:data:`DEFAULT_PALETTE`).
    * a name (``"ink"``) → that palette, or the default if the name is unknown
      (kept lenient so a typo restyles rather than raising mid-render).
    * a dict → merged *over* the default palette, so a caller can pass just
      ``{"accent": "#ff0055"}`` and inherit every other role.

    The result is always a fresh dict carrying every role key.
    """
    base = dict(PALETTES[DEFAULT_PALETTE])
    if palette is None:
        return base
    if isinstance(palette, str):
        return dict(PALETTES.get(palette, base))
    if isinstance(palette, dict):
        base.update(palette)
        return base
    return base


def palette_names() -> list[str]:
    """Sorted list of the built-in palette names."""
    return sorted(PALETTES)


# ── type scale ─────────────────────────────────────────────────────────────
#
# Sizes are a fraction of canvas height so a title is the same *visual* weight
# whether the canvas is 1080 tall (16:9) or 1920 tall (9:16 portrait). Values
# are point sizes for a ``text`` layer's ``font_size``.

_TYPE_SCALE: dict[str, float] = {
    "display": 0.135,   # hero number / one-word statement
    "title": 0.072,     # scene headline
    "heading": 0.050,   # section / list heading
    "subhead": 0.038,   # supporting line under a title
    "body": 0.030,      # list items, quote body
    "caption": 0.024,   # attributions, footnotes, subtitles
    "kicker": 0.019,    # eyebrow / label above a title
}


def type_size(role: str, height: int = DEFAULT_H) -> int:
    """Point size for a type ``role`` (``title``/``body``/…) on a ``height`` canvas.

    Unknown roles fall back to ``body`` so a template never renders 0-px text.
    """
    frac = _TYPE_SCALE.get(role, _TYPE_SCALE["body"])
    return max(10, round(frac * float(height)))


# ── layout / safe-area ──────────────────────────────────────────────────────
#
# All returns are px offsets from canvas centre. The safe margin (fraction of
# the relevant axis) keeps content clear of the frame edge — 8% side, 9%
# top/bottom, the broadcast-ish title-safe band.

SIDE_MARGIN = 0.08
EDGE_MARGIN = 0.09
#: y of the "lower third" band centre (fraction of height below centre).
LOWER_THIRD = 0.30


def left_edge(width: int = DEFAULT_W) -> float:
    """x of the left safe margin (negative == left of centre)."""
    return -(0.5 - SIDE_MARGIN) * float(width)


def right_edge(width: int = DEFAULT_W) -> float:
    """x of the right safe margin (positive == right of centre)."""
    return (0.5 - SIDE_MARGIN) * float(width)


def top_edge(height: int = DEFAULT_H) -> float:
    """y of the top safe margin (negative == above centre)."""
    return -(0.5 - EDGE_MARGIN) * float(height)


def bottom_edge(height: int = DEFAULT_H) -> float:
    """y of the bottom safe margin (positive == below centre)."""
    return (0.5 - EDGE_MARGIN) * float(height)


def lower_third_y(height: int = DEFAULT_H) -> float:
    """y of the lower-third caption band."""
    return LOWER_THIRD * float(height)


def line_step(role: str = "body", height: int = DEFAULT_H, lead: float = 1.5) -> float:
    """Vertical distance between successive lines of a type ``role`` (px).

    ``lead`` is the leading multiple (line-height ÷ font-size); 1.5 gives an
    airy, readable list. Used to stack bullet / list items.
    """
    return type_size(role, height) * float(lead)


# ── coordinate bridge ────────────────────────────────────────────────────
#
# ``text`` layers position via ``set_transform`` in px-from-centre; ``add_shape``
# takes *normalised* canvas coords (0,0 top-left → 1,1 bottom-right, y down).
# These convert a px-from-centre coordinate into the normalised space so a chip
# / rule / bar can be pinned to the same row as its text.

def nx(x: float, width: int = DEFAULT_W) -> float:
    """Normalise a px-from-centre ``x`` into ``[0,1]`` canvas space."""
    return 0.5 + float(x) / float(width)


def ny(y: float, height: int = DEFAULT_H) -> float:
    """Normalise a px-from-centre ``y`` into ``[0,1]`` canvas space."""
    return 0.5 + float(y) / float(height)


__all__ = [
    "DEFAULT_W",
    "DEFAULT_H",
    "PALETTES",
    "DEFAULT_PALETTE",
    "resolve_palette",
    "palette_names",
    "type_size",
    "left_edge",
    "right_edge",
    "top_edge",
    "bottom_edge",
    "lower_third_y",
    "line_step",
    "nx",
    "ny",
    "SIDE_MARGIN",
    "EDGE_MARGIN",
    "LOWER_THIRD",
]
