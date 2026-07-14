"""``bracket_frame`` element — four L-shaped corner brackets (a focus/crop frame).

An *element* is a pure ``(**params) -> [op dicts]`` overlay macro (see
:mod:`lumenframe.elements`): it draws one graphic and composes onto whatever is
beneath it. This one stamps the four ``⌐ ¬ / L ⌐`` corner brackets of a camera
focus / crop frame around an invisible box ``box_w × box_h`` centred on
``(x, y)`` px-from-centre. Each corner is an L-shaped two-segment polyline (an
``add_shape`` line through three points) reaching ``corner`` px along each edge,
so the middle of every side stays open — it frames without boxing. Real pixels,
no background — colour, box size, arm ``corner`` length and stroke ``width_px``
are params so it restyles in place.
"""
from __future__ import annotations

from typing import Any

from lumenframe.templates import theme


def bracket_frame(
    *,
    x: float = 0.0,
    y: float = 0.0,
    box_w: float = 600.0,
    box_h: float = 400.0,
    corner: float = 90.0,
    width_px: float = 10.0,
    color: str | None = None,
    start: float = 0.0,
    duration: float = 3.0,
    prefix: str = "bracket_frame",
    animate: bool = True,
    width: int = theme.DEFAULT_W,
    height: int = theme.DEFAULT_H,
) -> list[dict[str, Any]]:
    """Draw four L-shaped corner brackets around a ``box_w × box_h`` box.

    Args:
        x, y: centre of the framed box, px from canvas centre (x right+, y down+).
        box_w, box_h: width / height of the (invisible) box the brackets hug, px.
        corner: length of each bracket arm in px (how far it reaches along the
            box edge from the corner). Clamped so two arms never overlap.
        width_px: stroke thickness of the brackets in px.
        color: stroke colour; ``None`` == the brand ice-blue accent.
        start, duration: timeline placement of the layers (seconds).
        prefix: id prefix so the same element can be stamped twice without clash.
        animate: when True, each layer fades in (opacity 0→1 over ~0.4s).
        width, height: canvas size the px→normalised bridge tracks.

    Returns:
        A list of op dicts (four ``add_shape`` lines, plus their fade-in
        keyframes) — no background, overlay only.
    """
    col = color or theme.PALETTES["lumeri"]["accent"]
    start = float(start)
    duration = float(duration)
    hw = float(box_w) / 2.0
    hh = float(box_h) / 2.0
    # Keep an arm from overrunning half a side (would fuse adjacent corners).
    c = max(1.0, min(float(corner), hw, hh))

    # Box corners (px-from-centre).
    tl = (x - hw, y - hh)
    tr = (x + hw, y - hh)
    bl = (x - hw, y + hh)
    br = (x + hw, y + hh)

    def N(pt: tuple[float, float]) -> list[float]:
        return [theme.nx(pt[0], width), theme.ny(pt[1], height)]

    # Each corner: an L through (arm-end, corner, arm-end) reaching inward.
    corners = {
        "tl": [(tl[0], tl[1] + c), tl, (tl[0] + c, tl[1])],
        "tr": [(tr[0] - c, tr[1]), tr, (tr[0], tr[1] + c)],
        "bl": [(bl[0], bl[1] - c), bl, (bl[0] + c, bl[1])],
        "br": [(br[0] - c, br[1]), br, (br[0], br[1] - c)],
    }

    ops: list[dict[str, Any]] = []
    ids: list[str] = []
    for key, pts in corners.items():
        cid = f"{prefix}_{key}"
        ids.append(cid)
        ops.append(
            {"op": "add_shape", "id": cid, "name": f"Bracket {key.upper()}", "kind": "line",
             "points": [N(p) for p in pts],
             "stroke": {"color": col, "width": float(width_px)},
             "at_time": start, "duration": duration}
        )

    if animate:
        for cid in ids:
            ops += _fade_in(cid, start, 0.4, clamp=duration)

    return ops


def _fade_in(layer_id: str, t0: float, fade: float, *, clamp: float | None = None) -> list[dict[str, Any]]:
    """Two opacity keyframes (0 → 1) for a soft fade-in at absolute time ``t0``."""
    t1 = t0 + fade
    if clamp is not None:
        t1 = min(t1, t0 + max(0.05, clamp))
    return [
        {"op": "set_keyframe", "layer_id": layer_id, "property": "opacity", "t": round(t0, 4), "value": 0.0, "interp": "linear"},
        {"op": "set_keyframe", "layer_id": layer_id, "property": "opacity", "t": round(t1, 4), "value": 1.0, "interp": "ease_out"},
    ]
