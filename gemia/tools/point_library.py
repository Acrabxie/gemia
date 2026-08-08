"""Agent host surface for locally installed Point Library packages."""
from __future__ import annotations

import base64
from typing import Any

from gemia import cloud_accounts
from gemia.point_library import default_point_library_registry
from gemia.point_library import PointLibraryPackage
from gemia.tools._context import ToolContext


async def dispatch(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Route one semantic Point Library call through the local registry."""
    return await default_point_library_registry().dispatch(args, ctx)


async def dispatch_install(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Install one local or cloud-loaded bundle and activate it atomically."""
    path = str(args.get("path") or "").strip()
    encoded = str(args.get("bundle_base64") or "").strip()
    if path:
        package = PointLibraryPackage.from_file(path)
    elif encoded:
        try:
            package = PointLibraryPackage.from_bytes(base64.b64decode(encoded, validate=True))
        except Exception as exc:
            raise ValueError("bundle_base64 is not valid Point Library package data") from exc
    else:
        raise ValueError("install_point_library requires path or bundle_base64")
    result = default_point_library_registry().install(package).summary()
    return {"installed": True, "library": result, "next": "call point_library op:'catalog' before using it"}


async def dispatch_list(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    values = default_point_library_registry().list()
    return {"libraries": values, "count": len(values)}


async def dispatch_rollback(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    name = str(args.get("library_id") or "").strip()
    version = str(args.get("version") or "").strip()
    if not name or not version:
        raise ValueError("rollback_point_library requires library_id and version")
    result = default_point_library_registry().activate(name, version).summary()
    return {"activated": True, "library": result}


async def dispatch_publish(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Publish a validated local bundle without executing it in the cloud."""
    if not cloud_accounts.enabled():
        raise cloud_accounts.CloudAuthError(
            "Skill Cloud 需要先启用并登录 Lumeri 云账户",
            code="skill_cloud_disabled",
            status=503,
        )
    package = PointLibraryPackage.from_file(str(args.get("path") or "").strip())
    visibility = str(args.get("visibility") or "private").strip().lower()
    public_confirmed = args.get("public_confirmed") is True
    if visibility == "public" and not public_confirmed:
        raise ValueError("公开发布必须来自用户的明确要求，并设置 public_confirmed=true")
    implementation = dict(package.contract.get("implementation") or {})
    payload = {
        "kind": "point_library",
        "id": package.meta.name,
        "version": package.meta.version,
        "title": package.meta.title,
        "description": package.meta.description,
        "visibility": visibility,
        "public_confirmed": public_confirmed,
        "definition": {
            "shape": package.contract.get("shape"),
            "category": package.contract.get("category"),
            "implementation": implementation,
            "catalog": package.catalog.get("entries", []),
            "verification": package.verification,
        },
        "instructions": package.body,
        "bundle_base64": package.to_base64(),
    }
    result = cloud_accounts.client().publish_skill_artifact(payload)
    artifact = result["artifact"]
    return {
        **result,
        "summary": (
            f"Published Point Library '{artifact.get('title')}' v{artifact.get('version')} "
            f"to Skill Cloud as {artifact.get('visibility')}."
        ),
    }


__all__ = [
    "dispatch",
    "dispatch_install",
    "dispatch_list",
    "dispatch_publish",
    "dispatch_rollback",
]
