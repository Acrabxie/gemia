"""``compose`` — composition / framing as ONE agent verb (op create|adjust|catalog).

``create`` turns subject bounding boxes + a framing into a *reframe recipe*
(thirds/golden anchor, hard subject containment so the head is never cropped,
honest horizon snapping) plus a guide-overlay SVG; ``adjust`` re-derives from
feedback; ``catalog`` lists the framings. The engine lives in
:mod:`lumenframe.compose`; this thin surface returns the reframe recipe (the model
applies it via lumen_set_transform / crop). Canvas defaults to the doc canvas.
"""
from __future__ import annotations

from typing import Any

from gemia.tools._context import ToolContext

try:  # pragma: no cover
    from lumenframe.compose.tool import dispatch as _lib_dispatch
    _IMPORT_ERROR: str | None = None
except ImportError as exc:
    _lib_dispatch = None  # type: ignore[assignment]
    _IMPORT_ERROR = str(exc)


def _with_doc_canvas(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    brief = args.get("brief")
    if not isinstance(brief, dict) or brief.get("canvas"):
        return args
    try:
        from gemia.tools import layer as _layer
        canvas = (_layer._lumendoc(ctx) or {}).get("canvas")
    except Exception:
        canvas = None
    if not isinstance(canvas, dict) or not canvas.get("width") or not canvas.get("height"):
        return args
    return {**args, "brief": {**brief, "canvas": {"width": int(canvas["width"]),
                                                  "height": int(canvas["height"])}}}


async def dispatch(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    if _lib_dispatch is None:
        return {"applied": False, "error_code": "E_NOT_AVAILABLE",
                "error_message": f"lumenframe.compose not importable: {_IMPORT_ERROR}"}
    return await _lib_dispatch(_with_doc_canvas(args, ctx), ctx)
