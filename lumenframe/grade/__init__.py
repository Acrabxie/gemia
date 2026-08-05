"""lumenframe.grade — the colour-grading point library.

A grade is written in feeling, not numbers. The agent picks a **look**
(``teal_orange``, ``film``, ``noir``, …), sprinkles feelings ("moody", "faded",
"更暖"), optionally dials ``intensity``, and gets back a pure **grade recipe**
(white balance, lift/gamma/gain wheels, a protected tone S-curve, saturation,
a shadow/highlight split, black/white points, vignette, grain, halation) plus a
self-contained preview SVG and an ffmpeg filter string that ride the existing
effect layer.

The taste floor (enforced by :mod:`lumenframe.grade.grade`, never chosen per
call): the tone curve is a real protected S — it steepens the mid-tones but pins
its endpoints so the toe and shoulder never hard-clip unless the look demands it
(noir); shadow/highlight split hues are forced complementary for cinematic
looks; saturation has a hard ceiling; skin tones are protected from hue drift for
every non-stylised look; ``intensity`` scales the whole grade toward neutral,
never past a physical limit. Same brief + seed ⇒ byte-identical output.

This module self-registers with the shared :func:`register_library` so the
second-layer catalog (:func:`lumenframe.craft.craft_catalog`) can enumerate it.
"""
from __future__ import annotations

from lumenframe.craft import LibraryMeta, register_library

from lumenframe.grade.api import BriefError, adjust_grade, build_grade  # noqa: F401
from lumenframe.grade.catalog import describe_grade, grade_catalog  # noqa: F401

__all__ = [
    "build_grade",
    "adjust_grade",
    "grade_catalog",
    "describe_grade",
    "BriefError",
]

register_library(LibraryMeta(
    name="grade",
    domain="color grading",
    summary="creative colour grades from a look + feelings — protected tone "
            "curves, complementary split toning, skin-safe by default",
    rides="effect",
    output="grade recipe",
    catalog_fn=grade_catalog,
    aliases=("color_grade", "colour_grade"),
))
