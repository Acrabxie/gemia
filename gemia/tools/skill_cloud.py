"""Persistent Lumeri Agent tools for the account-scoped Skill Cloud."""
from __future__ import annotations

from typing import Any

from gemia import cloud_accounts
from gemia.tools._context import ToolContext


def _client() -> cloud_accounts.CloudAccountClient:
    if not cloud_accounts.enabled():
        raise cloud_accounts.CloudAuthError(
            "Skill Cloud 需要先启用并登录 Lumeri 云账户",
            code="skill_cloud_disabled",
            status=503,
        )
    return cloud_accounts.client()


async def dispatch_publish_cloud_guide(
    args: dict[str, Any], ctx: ToolContext
) -> dict[str, Any]:
    """Publish one private-by-default declarative Skill or Workflow."""
    kind = str(args.get("kind") or "").strip().lower()
    visibility = str(args.get("visibility") or "private").strip().lower()
    public_confirmed = args.get("public_confirmed") is True
    if visibility == "public" and not public_confirmed:
        raise ValueError(
            "公开发布必须来自用户的明确要求，并设置 public_confirmed=true"
        )
    payload = {
        "kind": kind,
        "id": str(args.get("id") or "").strip(),
        "version": str(args.get("version") or "").strip(),
        "title": str(args.get("title") or "").strip(),
        "description": str(args.get("description") or "").strip(),
        "visibility": visibility,
        "public_confirmed": public_confirmed,
        "definition": args.get("definition"),
        "instructions": str(args.get("instructions") or "").strip(),
        "bundle_base64": str(args.get("bundle_base64") or "").strip(),
    }
    result = _client().publish_skill_artifact(payload)
    artifact = result["artifact"]
    return {
        **result,
        "summary": (
            f"{'Published' if result['created'] else 'Updated visibility for'} "
            f"{artifact.get('kind')} '{artifact.get('title')}' v{artifact.get('version')} "
            f"to Skill Cloud as {artifact.get('visibility')}."
        ),
    }


async def dispatch_list_cloud_guides(
    args: dict[str, Any], ctx: ToolContext
) -> dict[str, Any]:
    kind = str(args.get("kind") or "").strip().lower()
    artifacts = _client().list_skill_artifacts(kind=kind)
    return {"artifacts": artifacts, "count": len(artifacts)}


async def dispatch_load_cloud_guide(
    args: dict[str, Any], ctx: ToolContext
) -> dict[str, Any]:
    return _client().load_skill_artifact(
        kind=str(args.get("kind") or "").strip().lower(),
        artifact_id=str(args.get("id") or "").strip(),
        version=str(args.get("version") or "").strip(),
        content_sha256=str(args.get("content_sha256") or "").strip().lower(),
    )


__all__ = [
    "dispatch_list_cloud_guides",
    "dispatch_load_cloud_guide",
    "dispatch_publish_cloud_guide",
]
