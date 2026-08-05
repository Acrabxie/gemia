"""lumenframe.camera — synthetic camera movement as a Lumeri point library.

Give it a subject and a word ("push_in", "epic", "handheld") and it returns a
*frame-safe transform track* — scale/translate/rotate keyframes with named
easing plus a deterministic, low-frequency handheld layer — and a self-contained
CSS/SVG preview. The craft is enforced, not chosen per call:

* every main move eases in and out (never linear);
* the move is *motivated* — it pushes and settles toward the focal subject;
* the subject is *kept in frame*: no scale/translate/wobble reveals a canvas
  edge, guaranteed by construction and checked in the tests;
* the push scale delta has a subtlety ceiling; handheld is a seeded sum of a
  few low-frequency sines (organic, deterministic), never white noise.

The track rides a lumenframe ``transform`` layer (see :func:`render.track_to_transform_ops`)
— this library never forks a renderer. The single agent tool ``camera_move``
(``lumenframe.camera.tool``) exposes it with an ``op`` discriminator.
"""
from __future__ import annotations

from lumenframe.craft import LibraryMeta, register_library

from lumenframe.camera.api import BriefError, adjust_track, build_track  # noqa: F401
from lumenframe.camera.catalog import camera_catalog, describe_camera  # noqa: F401
from lumenframe.camera.render import (  # noqa: F401
    compose_track,
    track_to_svg,
    track_to_transform_ops,
    validate_camera_svg,
)

__all__ = [
    "build_track", "adjust_track", "BriefError",
    "camera_catalog", "describe_camera",
    "compose_track", "track_to_svg", "track_to_transform_ops", "validate_camera_svg",
]

register_library(LibraryMeta(
    name="camera",
    domain="synthetic camera movement",
    summary="motivated, frame-safe camera moves over a still or clip",
    rides="transform",
    output="transform track",
    catalog_fn=camera_catalog,
    aliases=("camera_move",),
))
