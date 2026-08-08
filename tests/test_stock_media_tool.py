from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from gemia.tools import stock_media as tool
from gemia.production_budget import PAID_MEDIA_CONTEXT_KEY, ProductionMediaBudget
from gemia.tools._context import AssetRegistry, ToolContext


def _ctx(tmp_path: Path) -> ToolContext:
    return ToolContext(
        session_id="v3-stock-test",
        output_dir=tmp_path,
        registry=AssetRegistry(),
        emit_progress=lambda _payload: None,
    )


def test_search_returns_compact_licensed_results(monkeypatch, tmp_path: Path) -> None:
    def fake_search(**kwargs):
        assert kwargs["provider"] == "pixabay"
        assert kwargs["safe_search"] is True
        return {
            "query": kwargs["query"],
            "provider": "pixabay",
            "media_type": "video",
            "results": [{"id": "p1", "license": "Pixabay Content License"}],
            "errors": [],
        }

    monkeypatch.setattr(tool, "search_stock_media", fake_search)
    result = asyncio.run(
        tool.dispatch(
            {"action": "search", "query": "rainy city", "provider": "pixabay"},
            _ctx(tmp_path),
        )
    )
    assert result["result_count"] == 1
    assert result["results"][0]["license"] == "Pixabay Content License"


def test_fetch_registers_project_asset_with_provenance(monkeypatch, tmp_path: Path) -> None:
    def fake_fetch(_input, output, **kwargs):
        path = Path(output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"video")
        path.with_suffix(path.suffix + ".stock.json").write_text(
            json.dumps(
                {
                    "source": {
                        "provider": "pexels",
                        "id": "123",
                        "license": "Pexels License",
                        "source_url": "https://www.pexels.com/video/123",
                        "attribution": "Pexels creator",
                    }
                }
            ),
            encoding="utf-8",
        )
        assert kwargs["import_to_media_library"] is False
        return str(path)

    monkeypatch.setattr(tool, "fetch_stock_media", fake_fetch)
    ctx = _ctx(tmp_path)
    result = asyncio.run(
        tool.dispatch(
            {"action": "fetch", "query": "orbital earth", "provider": "pexels"},
            ctx,
        )
    )
    assert result["asset_id"].startswith("v_")
    assert result["license"] == "Pexels License"
    assert result["source_url"].endswith("/123")
    assert ctx.registry.contains(result["asset_id"])
    record = ctx.registry.get(result["asset_id"])
    assert record.source["kind"] == "public_stock"
    assert record.source["provider_asset_id"] == "123"
    assert record.license["name"] == "Pexels License"
    assert record.license["url"] == "https://www.pexels.com/license/"


def test_fetch_rejects_workspace_escape(monkeypatch, tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside-stock.mp4"
    outside.write_bytes(b"video")
    monkeypatch.setattr(tool, "fetch_stock_media", lambda *_args, **_kwargs: str(outside))
    with pytest.raises(ValueError, match="escaped"):
        asyncio.run(
            tool.dispatch({"action": "fetch", "query": "space"}, _ctx(tmp_path))
        )


def test_formal_search_requires_and_settles_zero_dollar_reservation(
    monkeypatch, tmp_path: Path
) -> None:
    ledger = ProductionMediaBudget(
        tmp_path / "budget.json", run_id="run-stock", cap_usd=15
    )
    decision = ledger.reserve(
        idempotency_key="run-stock:session:turn:call",
        tool_name="stock_media",
        estimated_usd=0,
        provider="pixabay",
    )
    assert decision.reservation is not None
    ctx = _ctx(tmp_path)
    ctx.extra.update(
        {
            "production_store": object(),
            "project_id": "project-stock",
            "run_id": "run-stock",
            "tool_call_context": {
                "trace_id": "turn-stock",
                "idempotency_key": "run-stock:session:turn:call",
            },
            PAID_MEDIA_CONTEXT_KEY: ledger.call_context(
                decision.reservation.reservation_id
            ).to_dict(),
        }
    )
    monkeypatch.setattr(
        tool,
        "search_stock_media",
        lambda **kwargs: {
            "query": kwargs["query"],
            "provider": kwargs["provider"],
            "media_type": "video",
            "results": [],
            "errors": [],
        },
    )
    result = asyncio.run(
        tool.dispatch(
            {"action": "search", "query": "city", "provider": "pixabay"}, ctx
        )
    )
    assert result["trace_id"] == "turn-stock"
    assert ledger.get(decision.reservation.reservation_id).status == "settled"
    assert ledger.snapshot()["committed_usd"] == 0
