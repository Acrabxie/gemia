"""``lumenframe.compose`` — the composition / framing point library.

Where should the subject sit in frame? This library answers with a **reframe
recipe** (a crop in source ``0..1``, a scale, the anchor the subject lands on)
plus a self-contained guide-overlay SVG. It rides the *transform / crop* layer —
it never renders pixels, only decides the window — and it enforces a structural
taste floor so amateur framing is hard to produce:

* the primary subject's eye-line lands on a thirds / golden **anchor**, never
  dead centre unless the framing is ``centered``;
* **headroom** tracks tightness but the head is never cropped;
* a facing subject gets **lead room** ahead of it;
* a **horizon** snaps to a third, never the middle;
* **secondary mass** balances to the opposite third; the crop stays inside the
  source and holds the target aspect with safe margins.

The agent speaks framing language — ``framing="golden"``, ``feeling=["airy"]``,
``facing="right"`` — and the geometry is chosen for it, deterministically per
seed. See :mod:`lumenframe.compose.tool` for the single ``compose_frame`` tool
(op: create | adjust | catalog).
"""
from __future__ import annotations

from lumenframe.craft import LibraryMeta, register_library

from lumenframe.compose.api import BriefError, adjust_frame, build_frame  # noqa: F401
from lumenframe.compose.catalog import compose_catalog, describe_compose  # noqa: F401
from lumenframe.compose.render import compose_overlay_svg, validate_overlay  # noqa: F401

register_library(LibraryMeta(
    name="compose",
    domain="composition / framing",
    summary="where the subject sits in frame — a tasteful reframe recipe + guide overlay",
    rides="transform (crop/reframe)",
    output="reframe recipe + guide-overlay SVG",
    catalog_fn=compose_catalog,
    aliases=("frame", "reframe", "composition"),
))

__all__ = [
    "build_frame", "adjust_frame", "BriefError",
    "compose_catalog", "describe_compose",
    "compose_overlay_svg", "validate_overlay",
]
