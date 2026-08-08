from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from gemia.ai.openai_image_client import OpenAIImageAPIError
from gemia.production_budget import (
    PAID_MEDIA_CONTEXT_KEY,
    PaidMediaSubmissionClaimedError,
    ProductionMediaBudget,
)
from gemia.tools import generate_image, generate_video
from gemia.tools._context import AssetRegistry, ToolContext
from gemia.tools._jobs import JobRegistry


def _ctx(tmp_path: Path, paid_context: dict) -> ToolContext:
    return ToolContext(
        session_id="paid-test",
        output_dir=tmp_path,
        registry=AssetRegistry(),
        emit_progress=lambda _update: None,
        extra={
            PAID_MEDIA_CONTEXT_KEY: paid_context,
            "provider": "openai_subscription",
        },
    )


def test_image_replay_cannot_submit_provider_twice(monkeypatch, tmp_path: Path) -> None:
    ledger = ProductionMediaBudget(tmp_path / "budget.json", run_id="run-image")
    decision = ledger.reserve(
        idempotency_key="image-shot-1",
        tool_name="generate_image",
        estimated_usd="0.101",
        provider="vertex",
        model="gemini-image",
    )
    assert decision.reservation is not None
    context = ledger.call_context(decision.reservation.reservation_id).to_dict()
    ctx = _ctx(tmp_path, context)
    calls = []

    class FakeClient:
        async def generate_image(self, **kwargs):
            calls.append(kwargs)
            return {
                "image_bytes": b"fake-png",
                "mime_type": "image/png",
                "model": kwargs["model"],
                "request_id": kwargs["request_id"],
                "raw_response_meta": {
                    "model_text": None,
                    "finish_reason": "STOP",
                    "usage_metadata": {},
                },
            }

    monkeypatch.setattr(generate_image, "_client_from_ctx", lambda _ctx: FakeClient())
    first = asyncio.run(generate_image.dispatch({"prompt": "one image"}, ctx))
    assert first["metadata"]["request_id"] == context["request_id"]
    assert first["metadata"]["reservation_id"] == context["reservation_id"]
    assert len(calls) == 1
    settled = ledger.get(context["reservation_id"])
    assert settled.status == "settled"
    assert settled.asset_materialization_status == "materialized"
    asset = ctx.registry.get(first["asset_id"])
    assert asset.source["kind"] == "generated_image"
    assert asset.source["request_id"] == context["request_id"]
    assert asset.source["receipt"]["materialization_status"] == "materialized"
    assert asset.license["rights_basis"] == "generated_under_configured_provider_account"

    with pytest.raises(PaidMediaSubmissionClaimedError, match="resubmission is forbidden"):
        asyncio.run(generate_image.dispatch({"prompt": "one image"}, ctx))
    assert len(calls) == 1


def test_paid_context_does_not_retry_subscription_image(monkeypatch, tmp_path: Path) -> None:
    ledger = ProductionMediaBudget(tmp_path / "budget.json", run_id="run-failover")
    decision = ledger.reserve(
        idempotency_key="image-no-failover",
        tool_name="generate_image",
        estimated_usd="0.101",
    )
    assert decision.reservation is not None
    ctx = _ctx(tmp_path, ledger.call_context(decision.reservation.reservation_id).to_dict())
    calls = []

    class FakeClient:
        async def generate_image(self, **kwargs):
            calls.append(kwargs["model"])
            raise OpenAIImageAPIError("subscription image bridge unavailable", status=503)

    monkeypatch.setattr(generate_image, "_client_from_ctx", lambda _ctx: FakeClient())
    with pytest.raises(OpenAIImageAPIError):
        asyncio.run(generate_image.dispatch({"prompt": "no retry"}, ctx))
    assert calls == ["gpt-image-2"]
    assert ledger.get(decision.reservation.reservation_id).status == "uncertain"


def test_video_duration_must_match_reserved_duration_before_claim(monkeypatch, tmp_path: Path) -> None:
    ledger = ProductionMediaBudget(tmp_path / "budget.json", run_id="run-video")
    decision = ledger.reserve(
        idempotency_key="video-shot-1",
        tool_name="generate_video",
        estimated_usd="2.8",
        requested_duration_sec=8,
    )
    assert decision.reservation is not None
    ctx = _ctx(tmp_path, ledger.call_context(decision.reservation.reservation_id).to_dict())
    monkeypatch.setattr(generate_video, "_client_from_ctx", lambda _ctx: object())
    with pytest.raises(ValueError, match="does not match"):
        asyncio.run(generate_video.dispatch({"prompt": "clip", "duration_sec": 4}, ctx))
    assert ledger.get(decision.reservation.reservation_id).status == "reserved"


def test_veo_job_is_persistable_before_dispatch_returns(monkeypatch, tmp_path: Path) -> None:
    ledger = ProductionMediaBudget(tmp_path / "budget.json", run_id="run-crash-window")
    decision = ledger.reserve(
        idempotency_key="video-crash-window",
        tool_name="generate_video",
        estimated_usd="2.8",
        requested_duration_sec=4,
    )
    assert decision.reservation is not None
    ctx = _ctx(tmp_path, ledger.call_context(decision.reservation.reservation_id).to_dict())
    persisted = []

    def persist_callback(job_id: str) -> None:
        persisted.append((job_id, ctx.jobs.to_dict()))

    ctx.extra["on_background_job"] = persist_callback

    class FakeClient:
        location = "us-central1"

        async def predict_long_running(self, **_kwargs):
            return {"name": "operations/durable-veo"}

    monkeypatch.setattr(generate_video, "_client_from_ctx", lambda _ctx: FakeClient())
    monkeypatch.setattr(
        generate_video,
        "media_model_failover_chain",
        lambda *_args, **_kwargs: ("veo-one",),
    )
    result = asyncio.run(
        generate_video.dispatch({"prompt": "durable clip", "duration_sec": 4}, ctx)
    )

    assert len(persisted) == 1
    persisted_job_id, snapshot = persisted[0]
    assert persisted_job_id == result["job_id"]
    durable = snapshot[persisted_job_id]
    assert durable["operation_name"] == "operations/durable-veo"
    assert durable["request_id"] == decision.reservation.request_id
    assert durable["reservation_id"] == decision.reservation.reservation_id
    assert durable["budget_ledger_path"] == str(ledger.path)
    assert durable["prompt_sha256"]


def test_job_registry_persists_paid_budget_identity_and_loads_legacy_rows() -> None:
    registry = JobRegistry()
    record = registry.submit(
        kind="video",
        provider="vertex:veo",
        operation_name="operations/1",
        pending_asset_id="v_001",
        estimated_eta_sec=120,
        summary="paid veo",
        request_id="request-1",
        reservation_id="reservation-1",
        estimated_cost_usd=2.8,
        budget_ledger_path="/tmp/budget.json",
        budget_run_id="run-1",
    )
    loaded = JobRegistry.from_dict(registry.to_dict()).get(record.job_id)
    assert loaded.request_id == "request-1"
    assert loaded.reservation_id == "reservation-1"
    assert loaded.estimated_cost_usd == 2.8
    assert loaded.budget_ledger_path == "/tmp/budget.json"
    assert loaded.budget_run_id == "run-1"

    legacy = record.to_dict()
    for key in (
        "request_id",
        "reservation_id",
        "estimated_cost_usd",
        "budget_ledger_path",
        "budget_run_id",
    ):
        legacy.pop(key, None)
    legacy_loaded = JobRegistry.from_dict({record.job_id: legacy}).get(record.job_id)
    assert legacy_loaded.request_id is None
    assert legacy_loaded.reservation_id is None
    assert legacy_loaded.estimated_cost_usd == 0.0
