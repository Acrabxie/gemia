from __future__ import annotations

from gemia.agent_loop_v3 import _filter_provider_schemas
from gemia.tools._schema import TOOL_SCHEMAS


def _image_schema() -> dict:
    return next(
        schema
        for schema in TOOL_SCHEMAS
        if schema["function"]["name"] == "generate_image"
    )


def test_non_subscription_provider_does_not_receive_image_tool_schema() -> None:
    schemas = [*_filter_provider_schemas([_image_schema()], provider="vertex")]
    assert schemas == []
    for provider in ("openai", "custom", "gemini", "claude", "openrouter", ""):
        assert _filter_provider_schemas([_image_schema()], provider=provider) == []


def test_openai_subscription_provider_receives_image_tool_schema() -> None:
    schema = _image_schema()
    assert _filter_provider_schemas([schema], provider="openai_subscription") == [schema]
