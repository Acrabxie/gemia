from __future__ import annotations

import pytest

from gemia.tool_capability_registry import (
    CapabilityRegistryError,
    ToolCapabilityRegistry,
    build_default_registry,
)
from gemia.tool_router import SYSTEM_TOOLS
from gemia.tools._schema import TOOL_NAMES


def _schema(name: str) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": f"{name} description",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    }


async def _dispatch(_args, _ctx):
    return {"ok": True}


def test_default_registry_is_the_complete_runtime_surface() -> None:
    registry = build_default_registry()
    assert registry.names == tuple(TOOL_NAMES)
    assert {
        name for name in registry.names
        if registry.get(name).surface == "internal"
    } == set(SYSTEM_TOOLS)
    stock = registry.get("stock_media")
    assert stock.estimated_usd == 0.0
    assert "storyboard" in stock.workflows
    assert stock.plan_mode == "blocked"
    assert registry.dispatcher("stock_media") is stock.dispatcher


def test_model_schema_help_and_dispatch_come_from_same_record() -> None:
    registry = build_default_registry()
    schema, = registry.schemas(["generate_video"])
    help_row, = registry.help_rows(["generate_video"])
    capability = registry.get("generate_video")
    assert schema["function"]["description"] == help_row["description"]
    assert capability.paid_media is True
    assert capability.estimated_usd > 0


def test_compile_fails_closed_on_unrouted_or_unpriced_capability() -> None:
    with pytest.raises(CapabilityRegistryError) as exc:
        ToolCapabilityRegistry.compile(
            schemas=[_schema("new_tool")],
            dispatchers={"new_tool": _dispatch},
            workflow_packs={},
            plan_allowed={"new_tool"},
            plan_blocked=set(),
            tool_costs={},
        )
    text = str(exc.value)
    assert "unrouted capabilities" in text
    assert "missing cost policy" in text


def test_unknown_schema_selection_is_rejected() -> None:
    registry = build_default_registry()
    with pytest.raises(CapabilityRegistryError):
        registry.schemas(["not-a-real-tool"])


def test_union_typed_patch_value_accepts_json_values_without_hashing_type_list() -> None:
    registry = build_default_registry()
    registry.validate_arguments(
        "patch_design_state",
        {
            "document": "creative_ir",
            "operation": "merge",
            "path": "/intent",
            "value": {"brief": "independent production"},
            "expected_revision": 0,
        },
    )
    registry.validate_arguments(
        "patch_design_state",
        {
            "document": "creative_ir",
            "operation": "set",
            "path": "/beat_order",
            "value": ["opening"],
            "expected_revision": 0,
        },
    )
