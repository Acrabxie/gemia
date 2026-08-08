"""Smoke + integration tests for generate_image via OpenAI subscription.

Two layers:
  - **Mocked**: stub `OpenAIImageClient.generate_image` so we exercise the
    dispatcher, base64-decode-to-file path, asset registration, metadata
    scrubbing (no base64 in result), and DISPATCHER registration WITHOUT
    hitting the network.
  - **Live**: real API call gated behind an explicit environment variable so
    CI never spends the local subscription quota.

A separate test verifies BudgetGuard correctly intercepts a
generate_image request that would exceed the per-session cap.
"""
from __future__ import annotations

import asyncio
import base64
import os
from pathlib import Path

import pytest

from gemia.ai.openai_image_client import OpenAIImageAPIError, OpenAIImageClient
from gemia.budget_guard import BudgetGuard, _TOOL_COSTS
from gemia.tools import DISPATCHER
from gemia.tools import generate_image as generate_image_tool
from gemia.tools._context import AssetRegistry, ToolContext


# Smallest possible valid PNG (1x1 red pixel). Used as the mocked
# provider response body so the dispatcher does real base64 decode +
# file write but the test stays under a millisecond.
_TINY_PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8/5+hHgAHggJ/PchI7wAAAABJRU5ErkJggg=="
)


def _ctx(tmp_path: Path) -> ToolContext:
    return ToolContext(
        session_id="test-generate-image",
        output_dir=tmp_path,
        registry=AssetRegistry(),
        emit_progress=lambda _u: None,
        extra={
            "provider": "openai_subscription",
            "openai_subscription_image_endpoint": "http://127.0.0.1:7808/v1/images/generations",
        },
    )


# ─────────────────────── DISPATCHER registration ───────────────────────


def test_generate_image_is_no_longer_a_stub() -> None:
    fn = DISPATCHER["generate_image"]
    assert not fn.__name__.startswith("stub_"), (
        f"generate_image still wired as a stub: {fn.__name__}"
    )


def test_cost_table_keeps_image_budget_guard_price() -> None:
    # Keep the existing paid-media safety estimate until the subscription
    # quota meter gets its own accounting unit.
    assert _TOOL_COSTS["generate_image"]["usd"] == pytest.approx(0.101)


# ──────────────────────── Mocked client tests ────────────────────────


def _stub_client(monkeypatch, *, image_bytes: bytes = _TINY_PNG_BYTES,
                 mime_type: str = "image/png", model_text: str | None = None) -> dict:
    """Replace OpenAIImageClient.generate_image with a stub that returns
    fixed bytes. Returns a dict the test can inspect for "what was called"."""
    seen: dict = {}

    async def fake_generate_image(self, **kwargs):
        seen.update(kwargs)
        return {
            "image_bytes": image_bytes,
            "mime_type": mime_type,
            "model": kwargs.get("model", "gpt-image-2"),
            "raw_response_meta": {
                "model": kwargs.get("model"),
                "finish_reason": "STOP",
                "model_text": model_text,
                "safety_ratings": [],
                "usage_metadata": {"totalTokenCount": 100},
            },
        }

    monkeypatch.setattr(OpenAIImageClient, "__init__", lambda self, **kwargs: None)
    monkeypatch.setattr(OpenAIImageClient, "generate_image", fake_generate_image)
    return seen


def test_dispatcher_decodes_base64_and_writes_png_to_workdir(
    monkeypatch, tmp_path: Path
) -> None:
    seen = _stub_client(monkeypatch)
    ctx = _ctx(tmp_path)

    result = asyncio.run(
        generate_image_tool.dispatch(
            {"prompt": "a cyberpunk city skyline at night", "aspect_ratio": "16:9"},
            ctx,
        )
    )

    # Result shape
    assert result["asset_id"].startswith("img_")
    record = ctx.registry.get(result["asset_id"])
    assert record.kind == "image"
    assert record.path.suffix == ".png"
    assert record.path.exists()
    assert record.path.read_bytes() == _TINY_PNG_BYTES

    # Args passed through correctly
    assert seen["prompt"] == "a cyberpunk city skyline at night"
    assert seen["model"] == "gpt-image-2"
    assert seen["size"] == "1536x1024"


def test_dispatcher_result_never_contains_base64(monkeypatch, tmp_path: Path) -> None:
    """Hard constraint: SSE event payload must not carry base64.
    The dispatcher's return dict IS the model_result/event_result the
    agent loop forwards; nothing in it may contain raw image bytes."""
    _stub_client(monkeypatch)
    ctx = _ctx(tmp_path)

    result = asyncio.run(
        generate_image_tool.dispatch({"prompt": "test"}, ctx)
    )

    # Walk the result recursively, fail if any string looks base64-image-ish
    # (length > 1KB and starts with iVBOR / /9j/ etc.) or any bytes appear.
    def _scan(value) -> None:
        if isinstance(value, (bytes, bytearray)):
            pytest.fail(f"raw bytes leaked into dispatcher result: {len(value)}B")
        if isinstance(value, str) and len(value) > 1024:
            prefix = value[:20]
            assert not (
                prefix.startswith("iVBOR") or prefix.startswith("/9j/")
            ), f"base64-image-shaped string leaked into result: {prefix}…"
        if isinstance(value, dict):
            for v in value.values():
                _scan(v)
        elif isinstance(value, list):
            for v in value:
                _scan(v)

    _scan(result)


def test_dispatcher_appends_style_to_prompt(monkeypatch, tmp_path: Path) -> None:
    seen = _stub_client(monkeypatch)
    ctx = _ctx(tmp_path)

    asyncio.run(
        generate_image_tool.dispatch(
            {"prompt": "a dog", "style": "watercolor, soft light"}, ctx
        )
    )

    assert "Style: watercolor, soft light" in seen["prompt"]


def test_dispatcher_loads_reference_assets_as_bytes(
    monkeypatch, tmp_path: Path
) -> None:
    seen = _stub_client(monkeypatch)
    ctx = _ctx(tmp_path)
    ref_path = tmp_path / "ref.png"
    ref_path.write_bytes(_TINY_PNG_BYTES)
    ref_id = ctx.registry.add_external(ref_path).asset_id

    asyncio.run(
        generate_image_tool.dispatch(
            {"prompt": "match this style", "reference_asset_ids": [ref_id]}, ctx
        )
    )

    assert len(seen["reference_images"]) == 1
    assert seen["reference_images"][0] == _TINY_PNG_BYTES


def test_dispatcher_rejects_non_image_reference(monkeypatch, tmp_path: Path) -> None:
    _stub_client(monkeypatch)
    ctx = _ctx(tmp_path)
    video_path = tmp_path / "ref.mp4"
    video_path.write_bytes(b"fake video")
    bad_ref = ctx.registry.add_external(video_path).asset_id

    with pytest.raises(ValueError, match="expected image"):
        asyncio.run(
            generate_image_tool.dispatch(
                {"prompt": "x", "reference_asset_ids": [bad_ref]}, ctx
            )
        )


def test_dispatcher_rejects_empty_prompt(monkeypatch, tmp_path: Path) -> None:
    _stub_client(monkeypatch)
    ctx = _ctx(tmp_path)
    with pytest.raises(ValueError, match="non-empty prompt"):
        asyncio.run(generate_image_tool.dispatch({"prompt": "   "}, ctx))


def test_dispatcher_rejects_non_subscription_provider(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    ctx.extra["provider"] = "vertex"
    with pytest.raises(RuntimeError, match="only when the selected provider is openai_subscription"):
        asyncio.run(generate_image_tool.dispatch({"prompt": "x"}, ctx))


def test_dispatcher_propagates_subscription_bridge_5xx_unchanged(monkeypatch, tmp_path: Path) -> None:
    """Provider 5xx must not be retried or hidden — model gets the traceback."""
    async def fake_call(self, **_kwargs):
        raise OpenAIImageAPIError("subscription image bridge HTTP 503",
                                  status=503)

    monkeypatch.setattr(OpenAIImageClient, "__init__", lambda self, **kwargs: None)
    monkeypatch.setattr(OpenAIImageClient, "generate_image", fake_call)

    ctx = _ctx(tmp_path)
    with pytest.raises(OpenAIImageAPIError, match="503"):
        asyncio.run(generate_image_tool.dispatch({"prompt": "x"}, ctx))


# ──────────────────────────── budget_guard ────────────────────────────


def test_budget_guard_blocks_generate_image_when_cap_exceeded() -> None:
    """Pre-spend most of a tiny cap, then check generate_image — should fail."""
    guard = BudgetGuard(max_usd=0.05, max_seconds=600.0)
    decision = guard.check("generate_image")
    assert decision.ok is False
    assert "exceed cap" in decision.reason
    assert decision.estimated_cost_usd == pytest.approx(0.101)
    # alternatives should suggest search_library (cheaper, see _cheaper())
    assert "search_library" in decision.alternatives


def test_budget_guard_allows_generate_image_when_under_cap() -> None:
    guard = BudgetGuard(max_usd=5.0, max_seconds=600.0)
    decision = guard.check("generate_image")
    assert decision.ok is True
    assert decision.estimated_cost_usd == pytest.approx(0.101)


# ──────────────────────────── live test ────────────────────────────


@pytest.mark.skipif(
    os.environ.get("LUMERI_RUN_LIVE_OPENAI_SUBSCRIPTION") != "1",
    reason="live OpenAI subscription call disabled; set LUMERI_RUN_LIVE_OPENAI_SUBSCRIPTION=1 to enable",
)
def test_live_generate_image_writes_real_png(tmp_path: Path) -> None:
    """Real call against GPT Image 2 through the local subscription bridge.

    Gated by env var so CI/test suite stays cheap. Requires an active local
    Codex subscription bridge on port 7808."""
    ctx = _ctx(tmp_path)
    result = asyncio.run(
        generate_image_tool.dispatch(
            {"prompt": "A single red apple on a white plate, studio lighting"},
            ctx,
        )
    )
    asset = ctx.registry.get(result["asset_id"])
    body = asset.path.read_bytes()
    assert len(body) > 5000, f"image too small to be real: {len(body)}B"
    # Verify PNG/JPEG magic
    assert body[:8] == b"\x89PNG\r\n\x1a\n" or body[:3] == b"\xff\xd8\xff", (
        f"file does not look like a real image: {body[:16]!r}"
    )
    print(f"\n[live] wrote {asset.path} ({len(body):,}B) "
          f"finish_reason={result['metadata']['provider_finish_reason']} "
          f"usage={result['metadata']['provider_usage']}")
