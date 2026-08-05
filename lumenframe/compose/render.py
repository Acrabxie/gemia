"""Recipe → a self-contained guide-overlay SVG (and its safety gate).

The reframe recipe is the load-bearing output; this module draws the *guide
overlay* an editor sees on top of the reframed picture — the chosen grid (thirds
lines or the golden spiral), the anchor the subject was placed on, the horizon
line, and a rectangle per subject. Coordinates are crop-relative ``0..1`` mapped
onto the delivered canvas, so the overlay lines up with what the crop produces.

The SVG is **HyperFrames-safe by construction**: only ``<line>`` / ``<rect>`` /
``<circle>`` / ``<path>`` / ``<polyline>`` with literal hex colours — no
``url(...)``, no ``data:`` URIs, no ``xlink`` / ``href`` / external images, no
``<script>``. :func:`validate_overlay` re-checks that invariant and the size cap
before the string is ever handed on, so a bad overlay can never poison a render.
"""
from __future__ import annotations

import math
from typing import Any

#: Guide colours — the Lumeri ice-blue accent family, literal so the SVG needs
#: no defs/gradients (which would require url() refs).
_GRID = "#5FC6DE"
_GRID_SOFT = "#9DB4C0"
_ANCHOR = "#FFFFFF"
_ANCHOR_RING = "#5FC6DE"
_PRIMARY = "#8BD8EA"
_SECONDARY = "#ABE5F1"
_HORIZON = "#F2FAFD"

#: Reject overlays larger than this (a guide is a few hundred elements at most).
MAX_OVERLAY_BYTES = 200_000

_FORBIDDEN = ("url(", "data:", "<script", "xlink", "href", "<image",
              "<foreignobject", "@import", "<use")


def _f(v: float) -> str:
    """Compact fixed-point number (stable across runs, no sci-notation)."""
    return f"{float(v):.3f}".rstrip("0").rstrip(".") or "0"


def compose_overlay_svg(recipe: dict[str, Any], width: int = 1920,
                        height: int = 1080) -> str:
    """Render a guide overlay for ``recipe`` at ``width`` × ``height`` px.

    Deterministic: identical recipes render byte-identical SVG. The overlay is
    transparent except for the guides, so it composites straight over the
    reframed frame.
    """
    w, h = int(width), int(height)
    guides = recipe.get("guides") or {}
    parts: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" '
        f'width="{w}" height="{h}">',
        f'<rect x="0" y="0" width="{w}" height="{h}" fill="none" '
        f'stroke="{_GRID_SOFT}" stroke-width="2" stroke-opacity="0.35"/>',
    ]

    # ── grid lines ────────────────────────────────────────────────────────
    for vx in guides.get("v_lines") or []:
        x = _f(float(vx) * w)
        parts.append(f'<line x1="{x}" y1="0" x2="{x}" y2="{h}" '
                     f'stroke="{_GRID}" stroke-width="1.5" stroke-opacity="0.55"/>')
    for hy in guides.get("h_lines") or []:
        y = _f(float(hy) * h)
        parts.append(f'<line x1="0" y1="{y}" x2="{w}" y2="{y}" '
                     f'stroke="{_GRID}" stroke-width="1.5" stroke-opacity="0.55"/>')

    # ── golden spiral (guide only) ────────────────────────────────────────
    if guides.get("spiral"):
        pts = _golden_spiral_points(w, h)
        poly = " ".join(f"{_f(x)},{_f(y)}" for x, y in pts)
        parts.append(f'<polyline points="{poly}" fill="none" stroke="{_GRID}" '
                     f'stroke-width="1.5" stroke-opacity="0.45"/>')

    # ── horizon line ──────────────────────────────────────────────────────
    if "horizon_line" in guides:
        y = _f(float(guides["horizon_line"]) * h)
        parts.append(f'<line x1="0" y1="{y}" x2="{w}" y2="{y}" '
                     f'stroke="{_HORIZON}" stroke-width="2.5" stroke-opacity="0.7" '
                     f'stroke-dasharray="12 8"/>')

    # ── subject markers ───────────────────────────────────────────────────
    for m in guides.get("subject_markers") or []:
        if not m.get("in_frame"):
            continue
        bx, by, bw, bh = (float(v) for v in m["bbox"])
        col = _PRIMARY if m.get("primary") else _SECONDARY
        sw = 3 if m.get("primary") else 1.8
        parts.append(
            f'<rect x="{_f(bx * w)}" y="{_f(by * h)}" width="{_f(bw * w)}" '
            f'height="{_f(bh * h)}" fill="none" stroke="{col}" '
            f'stroke-width="{sw}" stroke-opacity="0.9"/>')

    # ── the anchor point (drawn last, on top) ─────────────────────────────
    anchor = guides.get("anchor")
    if anchor:
        ax, ay = _f(float(anchor[0]) * w), _f(float(anchor[1]) * h)
        parts.append(f'<circle cx="{ax}" cy="{ay}" r="9" fill="none" '
                     f'stroke="{_ANCHOR_RING}" stroke-width="2.5"/>')
        parts.append(f'<circle cx="{ax}" cy="{ay}" r="3.5" fill="{_ANCHOR}"/>')

    parts.append("</svg>")
    return "".join(parts)


def _golden_spiral_points(width: int, height: int, samples: int = 160) -> list[tuple[float, float]]:
    """A logarithmic golden spiral (r ∝ phi^θ) sampled as a polyline.

    Winds inward toward the lower-right phi intersection; a pure guide, so a
    smooth polyline reads correctly without needing SVG arc/def machinery.
    """
    phi = (1.0 + math.sqrt(5.0)) / 2.0
    b = math.log(phi) / (math.pi / 2.0)
    cx, cy = 0.618 * width, 0.618 * height
    r0 = max(width, height) * 0.62
    pts: list[tuple[float, float]] = []
    theta_max = math.pi * 3.75
    for i in range(samples + 1):
        t = theta_max * (i / samples)
        r = r0 * math.exp(-b * t)
        pts.append((cx + r * math.cos(t + math.pi), cy + r * math.sin(t + math.pi)))
    return pts


def validate_overlay(svg: str, *, max_bytes: int = MAX_OVERLAY_BYTES) -> None:
    """Assert the overlay is well-formed and HyperFrames-safe, or raise.

    Guards the render boundary: an SVG that is not a string, not enclosed in a
    single ``<svg>…</svg>``, too large, or that contains any external / scripted
    reference is rejected so it can never reach a render pass.
    """
    if not isinstance(svg, str):
        raise ValueError("overlay must be a string")
    if len(svg) > max_bytes:
        raise ValueError(f"overlay too large ({len(svg)} > {max_bytes} bytes)")
    stripped = svg.strip()
    if not stripped.startswith("<svg") or not stripped.endswith("</svg>"):
        raise ValueError("overlay must be a single <svg>…</svg> document")
    low = svg.lower()
    for token in _FORBIDDEN:
        if token in low:
            raise ValueError(f"overlay contains unsafe token {token!r}")
