"""Composable graphic elements — the Lumeri overlay building-block library.

An *element* is a pure function ``(**params) -> list[op_dict]``: it expands into
the exact same op vocabulary an agent (or the editor) would emit, so the
``apply_element`` op just feeds the result through the normal dispatch. The
parallel to :mod:`lumenframe.templates` is deliberate — but where a *template*
stamps a whole styled *scene* (its own background, title, layout), an *element*
draws a single graphic (an arrow, a badge, a chevron) and **overlays** it onto
whatever is already on the canvas. An element therefore MUST NOT paint a
full-frame background; it composes, it does not own the frame.

Two audiences, one source of truth:

* the **agent** — :func:`describe_elements` renders a compact prompt block so a
  natural-language request maps onto ``apply_element``;
* **authors** — :func:`element_catalog` returns structured metadata (name,
  category, params, example) they can introspect and extend.

Adding an element
-----------------
1. Write ``my_element(**params) -> list[op_dict]`` in a module here (build from
   :mod:`lumenframe.templates.theme` for palette / coordinate helpers). Position
   via ``x``/``y`` (px from centre) + size params; colour via a ``color`` param
   defaulting to the brand accent; unique layer ids via a ``prefix``.
2. Register it in :data:`ELEMENTS` and add an :data:`ELEMENT_CATALOG` entry.
   ``apply_element``, the op catalogue and the agent prompt pick it up
   automatically.

Conventions
-----------
* Elements return ops only — never raw layers — so every layer is validated by
  the standard ``add_shape`` / ``add_layer`` path.
* No element draws a full-canvas background — elements are overlays.
* Every element shares the :data:`SHARED_PARAMS` (position / colour / canvas
  size / timing / prefix / animate); only its *content* params are listed per
  entry.
* Times are in seconds (the doc is seconds-canonical).
"""
from __future__ import annotations

from typing import Any, Callable

# theme (the design-token layer) must import first — the element modules below
# build on it, and importing it here makes it a package attribute before any
# element module runs ``from lumenframe.templates import theme``.
from lumenframe.templates import theme
from lumenframe.elements.arrow import arrow
from lumenframe.elements.chevron import chevron
from lumenframe.elements.bracket_frame import bracket_frame
from lumenframe.elements.underline import underline
from lumenframe.elements.circle import circle
from lumenframe.elements.outline_frame import outline_frame
from lumenframe.elements.polygon import polygon
from lumenframe.elements.pill import pill
from lumenframe.elements.sparkle import sparkle
from lumenframe.elements.checkmark import checkmark
from lumenframe.elements.cross import cross
from lumenframe.elements.dot_grid import dot_grid
from lumenframe.elements.line_grid import line_grid
from lumenframe.elements.diagonal_stripes import diagonal_stripes
from lumenframe.elements.wave_ribbon import wave_ribbon
from lumenframe.elements.sunburst import sunburst
from lumenframe.elements.progress_bar import progress_bar

Element = Callable[..., list[dict[str, Any]]]

#: name -> element function. The single source of truth consumed by
#: ``apply_element`` (lumenframe.ops) and the catalogue.
ELEMENTS: dict[str, Element] = {
    "arrow": arrow,
    # mark
    "chevron": chevron,
    "bracket_frame": bracket_frame,
    "underline": underline,
    # shape
    "circle": circle,
    "outline_frame": outline_frame,
    "polygon": polygon,
    "pill": pill,
    # emphasis
    "sparkle": sparkle,
    "checkmark": checkmark,
    "cross": cross,
    # pattern
    "dot_grid": dot_grid,
    "line_grid": line_grid,
    "diagonal_stripes": diagonal_stripes,
    # ribbon
    "wave_ribbon": wave_ribbon,
    "sunburst": sunburst,
    # data
    "progress_bar": progress_bar,
}

#: Positioning / styling / timing params every element accepts (documented once
#: here instead of on every catalogue entry).
SHARED_PARAMS: tuple[str, ...] = (
    "x", "y", "color", "start", "duration", "prefix", "animate", "width", "height",
)

#: Structured metadata — one entry per element. ``params`` lists only the
#: *content* params (the shared params live in :data:`SHARED_PARAMS`); ``*`` is
#: informational. A test pins these names to the real signatures.
ELEMENT_CATALOG: list[dict[str, Any]] = [
    {"name": "arrow", "category": "marker",
     "summary": "Directional arrow (shaft + triangular head) pointing along angle_deg.",
     "params": ["length", "angle_deg", "width_px", "head_size"],
     "example": {"element": "arrow", "params": {"x": 0, "y": 0, "length": 400, "angle_deg": 90}}},

    # ── mark ────────────────────────────────────────────────────────────────
    {"name": "chevron", "category": "mark",
     "summary": "A \">\" chevron, or a stack of `count` chevrons pointing along `angle`, drawn as two-segment add_shape polylines.",
     "params": ["size", "angle", "width_px", "count", "gap"],
     "example": {"element": "chevron", "params": {"x": 0, "y": 0, "size": 140, "angle": 0, "count": 3, "gap": 100}}},
    {"name": "bracket_frame", "category": "mark",
     "summary": "Four L-shaped corner brackets forming a focus/crop frame around a box_w x box_h box, each an add_shape polyline.",
     "params": ["box_w", "box_h", "corner", "width_px"],
     "example": {"element": "bracket_frame", "params": {"x": 0, "y": 0, "box_w": 700, "box_h": 460, "corner": 100}}},
    {"name": "underline", "category": "mark",
     "summary": "An emphasis underline: a thin pill-rounded add_shape rect sitting under a text region; fades in when animate.",
     "params": ["length", "thickness"],
     "example": {"element": "underline", "params": {"x": 0, "y": 120, "length": 520, "thickness": 18}}},

    # ── shape ───────────────────────────────────────────────────────────────
    {"name": "circle", "category": "shape",
     "summary": "A filled disc or a hollow ring (outline) drawn as an add_shape ellipse.",
     "params": ["radius", "mode", "stroke"],
     "example": {"element": "circle", "params": {"x": 0, "y": 0, "radius": 220, "mode": "ring", "stroke": 16}}},
    {"name": "outline_frame", "category": "shape",
     "summary": "A hollow rectangle outline (stroke only) with optional rounded corners.",
     "params": ["box_w", "box_h", "stroke", "radius"],
     "example": {"element": "outline_frame", "params": {"x": 0, "y": 0, "box_w": 700, "box_h": 420, "stroke": 10, "radius": 24}}},
    {"name": "polygon", "category": "shape",
     "summary": "A regular N-gon (triangle, pentagon, hexagon...) from center+radius+sides+rotation; fill or outline.",
     "params": ["radius", "sides", "rotation", "mode", "fill", "stroke"],
     "example": {"element": "polygon", "params": {"x": 0, "y": 0, "radius": 220, "sides": 6, "rotation": 0, "mode": "fill"}}},
    {"name": "pill", "category": "shape",
     "summary": "A rounded-rect chip/badge, filled, with optional centred text on it.",
     "params": ["box_w", "box_h", "text", "text_color", "fill", "radius", "font_size"],
     "example": {"element": "pill", "params": {"x": 0, "y": 0, "box_w": 360, "box_h": 120, "text": "LIVE"}}},

    # ── emphasis ────────────────────────────────────────────────────────────
    {"name": "sparkle", "category": "emphasis",
     "summary": "A 4-point sparkle/twinkle drawn as a concave 8-vertex polygon (four long on-axis points), filled with the brand accent.",
     "params": ["size"],
     "example": {"element": "sparkle", "params": {"x": 0, "y": 0, "size": 180}}},
    {"name": "checkmark", "category": "emphasis",
     "summary": "A check tick drawn as a single two-segment polyline (short down-stroke into the elbow, long up-stroke) with rounded caps in the brand accent.",
     "params": ["size", "width_px"],
     "example": {"element": "checkmark", "params": {"x": 0, "y": 0, "size": 200, "width_px": 26}}},
    {"name": "cross", "category": "emphasis",
     "summary": "A plus '+' or diagonal 'x' cross drawn as two crossed add_shape line segments (kind='plus'|'x') with rounded caps in the brand accent.",
     "params": ["size", "width_px", "kind"],
     "example": {"element": "cross", "params": {"x": 0, "y": 0, "size": 180, "kind": "x"}}},

    # ── pattern ─────────────────────────────────────────────────────────────
    {"name": "dot_grid", "category": "pattern",
     "summary": "A bounded cols x rows lattice of small filled dots (one add_shape ellipse each), centred at x,y.",
     "params": ["cols", "rows", "gap", "dot_radius"],
     "example": {"element": "dot_grid", "params": {"x": 0, "y": 0, "cols": 6, "rows": 4, "gap": 90, "dot_radius": 10}}},
    {"name": "line_grid", "category": "pattern",
     "summary": "A bounded box_w x box_h grid of thin rules (cols+1 vertical + rows+1 horizontal add_shape lines), centred at x,y.",
     "params": ["cols", "rows", "box_w", "box_h", "width"],
     "example": {"element": "line_grid", "params": {"x": 0, "y": 0, "cols": 6, "rows": 4, "box_w": 720, "box_h": 480, "width": 3}}},
    {"name": "diagonal_stripes", "category": "pattern",
     "summary": "A bounded box_w x box_h band of count parallel diagonal stripes at angle (add_shape lines), each Liang-Barsky-clipped to the box.",
     "params": ["box_w", "box_h", "count", "angle", "width"],
     "example": {"element": "diagonal_stripes", "params": {"x": 0, "y": 0, "box_w": 640, "box_h": 480, "count": 8, "angle": 45, "width": 6}}},

    # ── ribbon ──────────────────────────────────────────────────────────────
    {"name": "wave_ribbon", "category": "ribbon",
     "summary": "Horizontal sine-wave ribbon (the Lumeri light-ribbon motif) sampled into a single add_shape polyline.",
     "params": ["span", "amplitude", "periods", "thickness", "samples"],
     "example": {"element": "wave_ribbon", "params": {"x": 0, "y": 0, "span": 1200, "amplitude": 90, "periods": 2, "thickness": 8}}},
    {"name": "sunburst", "category": "ribbon",
     "summary": "Radial burst of evenly-angled rays (one add_shape line each) running from inner_r to outer_r about a centre.",
     "params": ["rays", "inner_r", "outer_r", "width_px"],
     "example": {"element": "sunburst", "params": {"x": 0, "y": 0, "rays": 16, "inner_r": 60, "outer_r": 260, "width_px": 6}}},

    # ── data ────────────────────────────────────────────────────────────────
    {"name": "progress_bar", "category": "data",
     "summary": "Rounded track rect plus an accent fill rect running left-to-right to progress(0..1); a horizontal progress/loading bar.",
     "params": ["length", "thickness", "progress*", "track_color"],
     "example": {"element": "progress_bar", "params": {"x": 0, "y": 0, "length": 900, "thickness": 36, "progress": 0.6}}},
]


def element_names() -> list[str]:
    """Sorted list of registered element names."""
    return sorted(ELEMENTS)


def element_catalog() -> list[dict[str, Any]]:
    """Structured metadata for every registered element (a fresh copy)."""
    return [dict(entry) for entry in ELEMENT_CATALOG]


def expand_element(name: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Expand element ``name`` with ``params`` into a list of op dicts.

    Raises:
        KeyError: if ``name`` is not a registered element.
    """
    fn = ELEMENTS[name]
    return fn(**(params or {}))


def describe_elements() -> str:
    """Render the element library as a compact agent-facing prompt block.

    One line per element (``name [category] — summary``) plus its content params,
    with the shared params and palette note stated once at the top. Appended to
    :func:`lumenframe.describe_ops` so the agent sees the whole library exactly
    where it sees the op vocabulary.
    """
    lines = [
        "Graphic elements — `apply_element element=<name> params={…}` stamps a",
        "single overlay graphic (arrow, marker, …) onto the current canvas via the",
        "normal op dispatch. Elements OVERLAY — they never paint a background.",
        "Shared params on every element: " + ", ".join(SHARED_PARAMS) + ".",
        "Colour defaults to the brand ice-blue accent (" + theme.PALETTES["lumeri"]["accent"] + ").",
        "",
    ]
    for entry in ELEMENT_CATALOG:
        params = ", ".join(entry.get("params", [])) or "—"
        lines.append(f"• {entry['name']} [{entry['category']}] — {entry['summary']}")
        lines.append(f"    params: {params}")
    return "\n".join(lines)


__all__ = [
    "ELEMENTS",
    "ELEMENT_CATALOG",
    "SHARED_PARAMS",
    "Element",
    "element_names",
    "element_catalog",
    "describe_elements",
    "expand_element",
    "theme",
    # element functions
    "arrow",
    "chevron",
    "bracket_frame",
    "underline",
    "circle",
    "outline_frame",
    "polygon",
    "pill",
    "sparkle",
    "checkmark",
    "cross",
    "dot_grid",
    "line_grid",
    "diagonal_stripes",
    "wave_ribbon",
    "sunburst",
    "progress_bar",
]
