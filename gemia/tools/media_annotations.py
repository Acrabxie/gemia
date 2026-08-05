"""Tools for persistent media-library annotations."""
from __future__ import annotations

from typing import Any

from gemia.tools._context import ProgressUpdate, ToolContext


def _account_id(ctx: ToolContext) -> str:
    return "local"


async def dispatch_annotate(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    account_id = _account_id(ctx)
    if not account_id:
        raise ValueError("annotate_media requires a local workspace")
    from gemia.media_annotations import annotate_asset_heuristic
    from gemia.media_library import list_assets

    asset_ids = args.get("library_asset_ids") or args.get("asset_ids") or []
    if isinstance(asset_ids, str):
        asset_ids = [asset_ids]
    if not isinstance(asset_ids, list):
        raise ValueError("annotate_media asset_ids must be a list")
    if not asset_ids and args.get("all"):
        kind = str(args.get("kind") or "video")
        max_assets = max(1, min(int(args.get("max_assets") or 20), 100))
        asset_ids = [
            asset.get("asset_id")
            for asset in list_assets(account_id, kind=kind, q=str(args.get("query") or ""), limit=max_assets)
        ]
    if not asset_ids:
        raise ValueError("annotate_media requires asset_ids or all=true")

    mode = str(args.get("mode") or "quick")
    language = str(args.get("language") or "auto")
    max_assets = max(1, min(int(args.get("max_assets") or len(asset_ids)), 100))
    tags = args.get("tags") if isinstance(args.get("tags"), list) else None
    replace_existing = bool(args.get("replace_existing", True))

    results: list[dict[str, Any]] = []
    total = min(len(asset_ids), max_assets)
    for index, asset_id in enumerate([str(item) for item in asset_ids[:max_assets]], start=1):
        ctx.emit_progress(ProgressUpdate(percent=(index - 1) * 100 / max(total, 1), message=f"annotating {asset_id}"))
        results.append(
            annotate_asset_heuristic(
                account_id,
                asset_id,
                mode=mode,
                language=language,
                tags=tags,
                replace_existing=replace_existing,
            )
        )
    ctx.emit_progress(ProgressUpdate(percent=100.0, message=f"annotated {len(results)} asset(s)"))
    return {
        "asset_count": len(results),
        "results": results,
        "summary": f"annotated {len(results)} media-library asset(s)",
    }


async def dispatch_get(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    account_id = _account_id(ctx)
    if not account_id:
        raise ValueError("get_media_annotations requires a local workspace")
    from gemia.media_annotations import list_annotations

    asset_id = str(args.get("library_asset_id") or args.get("asset_id") or "")
    annotations = list_annotations(account_id, asset_id)
    return {
        "asset_id": asset_id,
        "annotation_count": len(annotations),
        "annotations": annotations,
        "summary": f"{len(annotations)} annotation(s) on {asset_id}",
    }


async def dispatch_write(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    account_id = _account_id(ctx)
    if not account_id:
        raise ValueError("write_media_annotation requires a local workspace")
    from gemia.media_annotations import (
        MediaAnnotationError,
        create_annotation,
        get_annotation,
        update_annotation,
    )

    asset_id = str(args.get("library_asset_id") or args.get("asset_id") or "")
    annotation_id = str(args.get("annotation_id") or "")
    writable_fields = (
        "scope",
        "start_sec",
        "end_sec",
        "frame",
        "label",
        "note",
        "tags",
        "category",
        "confidence",
        "language",
        "metadata",
    )
    payload = {key: args[key] for key in writable_fields if key in args}
    if "metadata" in payload and not isinstance(payload["metadata"], dict):
        raise ValueError("write_media_annotation metadata must be an object")

    if annotation_id:
        current = get_annotation(account_id, asset_id, annotation_id)
        if current.get("source") == "user":
            raise MediaAnnotationError(
                "write_media_annotation cannot update a user annotation; "
                "create a new annotation instead"
            )
        annotation = update_annotation(account_id, asset_id, annotation_id, payload)
    else:
        payload.setdefault("scope", "asset")
        payload.setdefault("language", "auto")
        payload["source"] = "gemini"
        annotation = create_annotation(account_id, asset_id, payload)
    return {
        "asset_id": asset_id,
        "annotation": annotation,
        "summary": f"saved annotation {annotation['annotation_id']} on {asset_id}",
    }


__all__ = ["dispatch_annotate", "dispatch_get", "dispatch_write"]
