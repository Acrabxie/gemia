"""lumenframe.kinetic — the kinetic typography point library.

Animated titles, lower-thirds, quotes, lyrics and credits as a creative brief,
not a pile of point sizes and keyframe delays. The agent speaks layout + style +
feeling; the library enforces the typographic taste floor (a modular type scale,
three-channel hierarchy, TV title-safe margins, optical leading, a readable
measure and a pace-derived reveal rhythm) and emits a self-contained,
HyperFrames-safe animated SVG that rides the existing ``html`` layer.

Public surface:

* :func:`~lumenframe.kinetic.api.build`  — brief → ``{scene, svg, plan, notes}``.
* :func:`~lumenframe.kinetic.api.adjust` — feedback → rebuilt, same seed.
* :func:`~lumenframe.kinetic.catalog.kinetic_catalog` / ``describe_kinetic`` —
  the vocabulary for composing briefs.
* :func:`~lumenframe.kinetic.tool.dispatch` — the single ``kinetic_type`` tool.

Importing this module registers the library with the craft spine so it appears
in ``craft_catalog()``.
"""
from __future__ import annotations

from lumenframe.craft import LibraryMeta, register_library

from lumenframe.kinetic.api import BriefError, adjust, build  # noqa: F401
from lumenframe.kinetic.catalog import describe_kinetic, kinetic_catalog  # noqa: F401
from lumenframe.kinetic.render import scene_to_svg, validate_svg  # noqa: F401

__all__ = [
    "build", "adjust", "BriefError",
    "kinetic_catalog", "describe_kinetic",
    "scene_to_svg", "validate_svg",
]

register_library(LibraryMeta(
    name="kinetic_type",
    domain="kinetic typography",
    summary="animated titles/text — modular scale, safe margins, pace-timed reveals",
    rides="html",
    output="text scene (self-contained animated SVG)",
    catalog_fn=kinetic_catalog,
    ops=("create", "adjust", "catalog"),
    aliases=("kinetic", "title", "lower_third"),
))
