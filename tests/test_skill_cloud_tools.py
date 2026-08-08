from __future__ import annotations

import asyncio
from typing import Any

import pytest

from gemia.cloud_accounts import CloudAccountClient
from gemia.plan_mode import PLAN_ALLOWED_TOOLS, PLAN_BLOCKED_TOOLS
from gemia.tool_router import ToolRouter
from gemia.tools import DISPATCHER
from gemia.tools._schema import TOOL_NAMES
from gemia.tools import skill_cloud


class TokenStore:
    def __init__(self) -> None:
        self.value = "refresh-one"

    def get(self) -> str | None:
        return self.value

    def set(self, token: str) -> None:
        self.value = token

    def delete(self) -> None:
        self.value = None


class Transport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any] | None, str]] = []
        self.account = {
            "id": "account-one",
            "email": "creator@example.com",
            "onboarding_completed": True,
            "provider_mode": "managed",
            "provider": "lumeri",
        }

    def request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        access_token: str = "",
    ) -> tuple[int, dict[str, Any]]:
        self.calls.append((method, path, payload, access_token))
        if path == "/v1/auth/refresh":
            return 200, {
                "access_token": "access-one",
                "refresh_token": "refresh-two",
                "account": self.account,
            }
        if path == "/v1/me":
            return 200, self.account
        if method == "POST" and path == "/v1/skill-cloud/artifacts":
            return 201, {
                "created": True,
                "artifact": {
                    "kind": payload["kind"],
                    "id": payload["id"],
                    "version": payload["version"],
                    "title": payload["title"],
                    "visibility": payload["visibility"],
                },
            }
        if method == "GET" and path == "/v1/skill-cloud/artifacts?kind=skill":
            return 200, {"artifacts": [{"id": "edit-cleanly", "kind": "skill"}]}
        if method == "GET" and path.endswith(
            "/skill/edit-cleanly/1.0.0?content_sha256=" + "a" * 64
        ):
            return 200, {"id": "edit-cleanly", "kind": "skill", "version": "1.0.0"}
        return 404, {"error": "not_found", "message": "not found"}


def _payload(*, visibility: str = "private", public_confirmed: bool = False) -> dict[str, Any]:
    return {
        "kind": "skill",
        "id": "edit-cleanly",
        "version": "1.0.0",
        "title": "Edit cleanly",
        "description": "Reusable editing guidance.",
        "definition": {"steps": ["Inspect", "Edit", "Verify"]},
        "instructions": "Preserve the project and verify visible output.",
        "visibility": visibility,
        "public_confirmed": public_confirmed,
    }


def test_cloud_account_client_uses_account_bound_bearer_requests() -> None:
    transport = Transport()
    client = CloudAccountClient(transport, TokenStore())

    uploaded = client.publish_skill_artifact(_payload())
    assert uploaded["created"] is True
    assert uploaded["artifact"]["id"] == "edit-cleanly"
    assert client.list_skill_artifacts(kind="skill")[0]["id"] == "edit-cleanly"
    assert client.load_skill_artifact(
        kind="skill",
        artifact_id="edit-cleanly",
        version="1.0.0",
        content_sha256="a" * 64,
    )["version"] == "1.0.0"

    cloud_calls = [call for call in transport.calls if "/skill-cloud/" in call[1]]
    assert cloud_calls
    assert all(call[3] == "access-one" for call in cloud_calls)


def test_agent_tools_are_fully_installed_and_persistent() -> None:
    names = {"publish_cloud_guide", "list_cloud_guides", "load_cloud_guide"}
    assert names <= set(TOOL_NAMES)
    assert names <= set(DISPATCHER)
    assert "publish_cloud_guide" in PLAN_BLOCKED_TOOLS
    assert {"list_cloud_guides", "load_cloud_guide"} <= PLAN_ALLOWED_TOOLS
    for request in ("你好", "剪一条宣传片", "继续"):
        assert names <= set(ToolRouter(request).active_tool_names)


def test_public_upload_requires_explicit_user_confirmation(monkeypatch) -> None:
    class StubClient:
        def __init__(self) -> None:
            self.payload = None

        def publish_skill_artifact(self, payload):
            self.payload = payload
            return {
                "created": True,
                "artifact": {
                    "kind": payload["kind"],
                    "title": payload["title"],
                    "version": payload["version"],
                    "visibility": payload["visibility"],
                },
            }

    stub = StubClient()
    monkeypatch.setattr(skill_cloud, "_client", lambda: stub)
    with pytest.raises(ValueError, match="明确要求"):
        asyncio.run(
            skill_cloud.dispatch_publish_cloud_guide(
                _payload(visibility="public", public_confirmed=False), None
            )
        )
    assert stub.payload is None

    result = asyncio.run(
        skill_cloud.dispatch_publish_cloud_guide(
            _payload(visibility="public", public_confirmed=True), None
        )
    )
    assert result["artifact"]["visibility"] == "public"
    assert stub.payload["public_confirmed"] is True
