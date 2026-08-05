"""``camera_move`` — the camera point library as ONE agent tool.

Single tool, op discriminator (the ``vector_motion`` shape):

* ``op:"create"``  — a movement brief → a frame-safe transform track + a
  self-contained CSS/SVG preview. The preview is render-validated *before* it is
  returned so an unsafe or oversized SVG can never leave this layer.
* ``op:"adjust"``  — feedback phrases ("more handheld", "更稳") against a stored
  brief: fold the semantic deltas in and re-derive the track with the same seed.
* ``op:"catalog"`` — the creative vocabulary (moves, styles, axes, feedback
  words) for the model to compose briefs from.

Pure: it imports no gemia runtime and returns plain dicts via ``ok()`` / ``err()``.
The gemia adapter that writes the track onto a transform layer is a separate,
thin wrapper (exactly as ``gemia/tools/vector_motion.py`` wraps ``vector.api``).
"""
from __future__ import annotations

from typing import Any

from lumenframe.craft import err, ok, tool_dispatch

from lumenframe.camera.api import BriefError, adjust_track, build_track
from lumenframe.camera.catalog import camera_catalog
from lumenframe.camera.render import track_to_svg, validate_camera_svg

TOOL = "camera_move"


def _preview(track: dict[str, Any]) -> str:
    svg = track_to_svg(track)
    return validate_camera_svg(svg)


def _create(args: dict[str, Any]) -> dict[str, Any]:
    brief = args.get("brief")
    if not isinstance(brief, dict):
        return err("E_ARG", f"{TOOL} create: 'brief' must be an object "
                            "(see op:'catalog' for the vocabulary)")
    try:
        result = build_track(brief)
    except BriefError as exc:
        return err("E_ARG", f"{TOOL} create: {exc}", recovery="fix_args")
    try:
        preview = _preview(result["track"])
    except ValueError as exc:
        return err("E_RENDER", f"{TOOL} create: preview is not render-safe: {exc}")
    return ok(
        track=result["track"],
        plan=result["plan"],
        preview_svg=preview,
        preview_bytes=len(preview),
        notes=result["notes"],
        next="adjust with op:'adjust' + feedback phrases; the track drives a transform layer",
    )


def _adjust(args: dict[str, Any]) -> dict[str, Any]:
    brief = args.get("brief")
    feedback = args.get("feedback")
    if not isinstance(brief, dict):
        return err("E_ARG", f"{TOOL} adjust: 'brief' must be the stored object")
    if not isinstance(feedback, list) or not feedback:
        return err("E_ARG", f"{TOOL} adjust: 'feedback' must be a non-empty list "
                            "of phrases like 'more handheld' / '更稳'")
    try:
        result = adjust_track(brief, [str(p) for p in feedback])
    except BriefError as exc:
        return err("E_ARG", f"{TOOL} adjust: {exc}", recovery="fix_args")
    try:
        preview = _preview(result["track"])
    except ValueError as exc:
        return err("E_RENDER", f"{TOOL} adjust: rebuilt preview is not render-safe: {exc}")
    return ok(
        track=result["track"],
        plan=result["plan"],
        brief=result["brief"],
        preview_svg=preview,
        preview_bytes=len(preview),
        notes=result["notes"],
        next="op:'adjust' again to keep refining; the track drives a transform layer",
    )


async def dispatch(args: dict[str, Any], ctx: Any = None) -> dict[str, Any]:
    """Route a ``camera_move`` call by ``op`` (create | adjust | catalog)."""
    return tool_dispatch(
        args, tool=TOOL, catalog_fn=camera_catalog, create=_create, adjust=_adjust,
    )
