"""``rhythm_edit`` — music-driven cutting as ONE agent verb (op create|adjust|catalog).

``create`` turns a tempo + arrangement into a beat grid and a phrase-aware,
beat-aligned *cut plan* (accents on strong beats, density following energy,
build/drop acceleration); ``adjust`` re-derives from feedback; ``catalog`` lists
the sync patterns. The engine lives in :mod:`lumenframe.rhythm`; this thin surface
returns the beat grid + cut plan (timeline cut ops the model then applies).
"""
from __future__ import annotations

from typing import Any

from gemia.tools._context import ToolContext

try:  # pragma: no cover
    from lumenframe.rhythm.tool import dispatch as _lib_dispatch
    _IMPORT_ERROR: str | None = None
except ImportError as exc:
    _lib_dispatch = None  # type: ignore[assignment]
    _IMPORT_ERROR = str(exc)


async def dispatch(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    if _lib_dispatch is None:
        return {"applied": False, "error_code": "E_NOT_AVAILABLE",
                "error_message": f"lumenframe.rhythm not importable: {_IMPORT_ERROR}"}
    return await _lib_dispatch(args, ctx)
