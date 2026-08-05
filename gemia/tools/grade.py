"""``grade`` — creative colour grading as ONE agent verb (op create|adjust|catalog).

``create`` turns a look + feelings into a deterministic grade *recipe* (protected
tone curve, complementary split toning, skin-safe by default) plus a preview SVG
and an ``ffmpeg_filter`` string — the recipe the model then applies to footage.
``adjust`` folds feedback ('warmer', '更电影感') into the recipe; ``catalog`` lists
the look vocabulary. The creative engine lives in :mod:`lumenframe.grade`; this is
the thin gemia dispatch surface (no doc mutation — a grade is a recipe).
"""
from __future__ import annotations

from typing import Any

from gemia.tools._context import ToolContext

try:  # pragma: no cover
    from lumenframe.grade.tool import dispatch as _lib_dispatch
    _IMPORT_ERROR: str | None = None
except ImportError as exc:
    _lib_dispatch = None  # type: ignore[assignment]
    _IMPORT_ERROR = str(exc)


async def dispatch(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    if _lib_dispatch is None:
        return {"applied": False, "error_code": "E_NOT_AVAILABLE",
                "error_message": f"lumenframe.grade not importable: {_IMPORT_ERROR}"}
    return await _lib_dispatch(args, ctx)
