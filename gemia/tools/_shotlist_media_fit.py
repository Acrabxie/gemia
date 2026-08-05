"""Bridge the pure outline fitter to the account library and session registry."""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from gemia.media_annotations import list_annotations
from gemia.media_library import get_asset
from gemia.media_search import search_media_annotations
from gemia.project_model import iter_shots
from gemia.shotlist_fit import fit_shotlist_to_media
from gemia.tools._context import ToolContext
from gemia.tools._library_session import account_id_for, ensure_session_asset


def fit_media_for_context(
    ctx: ToolContext,
    shotlist: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Fit available persisted evidence without generating or buying media.

    Retrieval and registration finish before the caller writes the shotlist, so
    a search/availability failure cannot leave a half-fitted project document.
    """
    project_account = (
        str(ctx.project.load().get("account_id") or "")
        if ctx.project is not None
        else ""
    )
    account_id = account_id_for(ctx) or project_account
    if not account_id:
        raise ValueError("fit_media requires a local media library")

    session_ids: dict[str, str] = {}

    def provider(query: str, _shot: dict[str, Any]) -> dict[str, Any]:
        raw = search_media_annotations(account_id, query, kind="video", limit=20)
        available: list[dict[str, Any]] = []
        for result in raw.get("results") or []:
            if not isinstance(result, dict):
                continue
            library_asset_id = str(result.get("library_asset_id") or "")
            asset = get_asset(account_id, library_asset_id) if library_asset_id else None
            session_asset_id = ensure_session_asset(ctx, asset) if asset else None
            if not session_asset_id:
                continue
            enriched = deepcopy(result)
            enriched["annotations"] = list_annotations(account_id, library_asset_id)
            available.append(enriched)
            session_ids[library_asset_id] = session_asset_id
        enriched_result = deepcopy(raw)
        enriched_result["results"] = available
        enriched_result["result_count"] = len(available)
        return enriched_result

    fitted, report = fit_shotlist_to_media(
        shotlist,
        provider,
        overwrite=False,
    )
    assigned_ids = {
        str(item.get("library_asset_id") or "")
        for item in report.get("assignments") or []
        if isinstance(item, dict)
    }
    for _scene, shot in iter_shots(fitted):
        library_asset_id = str(shot.get("library_asset_id") or "")
        if library_asset_id in assigned_ids:
            session_asset_id = session_ids.get(library_asset_id)
            if not session_asset_id:
                raise ValueError(
                    f"selected library asset {library_asset_id!r} is unavailable in this session"
                )
            shot["asset_id"] = session_asset_id

    report = deepcopy(report)
    report["registered_library_assets"] = sorted(assigned_ids)
    return fitted, report


__all__ = ["fit_media_for_context"]
