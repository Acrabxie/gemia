"""``edit_grammar`` — cut craft as ONE agent verb (op create|adjust|catalog).

``create`` turns a list of clips + a cut style into a reasoned *cut plan*
(straight cuts by default, J/L cuts, cut-on-action, a capped transition budget);
``adjust`` re-plans from feedback ('more energetic', '更梦幻'); ``catalog`` lists
the styles and transition vocabulary. The engine lives in :mod:`lumenframe.edit`;
this thin surface returns the plan (timeline ops the model then applies).
"""
from __future__ import annotations

from typing import Any

from gemia.tools._context import ToolContext

try:  # pragma: no cover
    from lumenframe.edit.tool import dispatch as _lib_dispatch
    _IMPORT_ERROR: str | None = None
except ImportError as exc:
    _lib_dispatch = None  # type: ignore[assignment]
    _IMPORT_ERROR = str(exc)


async def dispatch(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    if _lib_dispatch is None:
        return {"applied": False, "error_code": "E_NOT_AVAILABLE",
                "error_message": f"lumenframe.edit not importable: {_IMPORT_ERROR}"}
    return await _lib_dispatch(args, ctx)
