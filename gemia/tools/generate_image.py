"""Generate an image through the OpenAI ChatGPT subscription bridge.

The host keeps image bytes out of tool results/SSE: the bridge response is
decoded locally, written into the session workspace, and registered as an
asset.  This verb is intentionally fail-closed unless the selected provider
is exactly ``openai_subscription``; there is no Vertex/API-key fallback.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from gemia.ai.openai_image_client import (
    DEFAULT_ENDPOINT,
    OpenAIImageClient,
    endpoint_from_chat_url,
)
from gemia.budget_guard import tool_cost_usd
from gemia.production_budget import claim_paid_media_call
from gemia.tools._context import ToolContext


_PROVIDER = "openai_subscription"
_MODEL = "gpt-image-2"
_DEFAULT_IMAGE_SIZE = "1024x1024"


async def dispatch(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    provider = str((ctx.extra or {}).get("provider") or "").strip().lower()
    if provider != _PROVIDER:
        raise RuntimeError(
            "generate_image is available only when the selected provider is "
            "openai_subscription; no other image provider fallback is enabled"
        )

    prompt = str(args.get("prompt") or "").strip()
    if not prompt:
        raise ValueError("generate_image requires a non-empty prompt")
    aspect_ratio = args.get("aspect_ratio")
    if aspect_ratio is not None:
        aspect_ratio = str(aspect_ratio).strip() or None
    style = str(args.get("style") or "").strip()
    if style:
        prompt = f"{prompt}. Style: {style}"

    reference_bytes: list[bytes] = []
    reference_ids = list(args.get("reference_asset_ids") or [])
    for ref_id in reference_ids:
        ref_record = ctx.registry.get(str(ref_id))
        if ref_record.kind != "image":
            raise ValueError(
                f"reference_asset_id {ref_id!r} is {ref_record.kind!r}, expected image"
            )
        reference_bytes.append(Path(ref_record.path).read_bytes())

    client = _client_from_ctx(ctx)
    new_id = ctx.registry.allocate_id("image")
    paid = claim_paid_media_call(ctx.extra if isinstance(ctx.extra, dict) else None)
    paid_call, paid_ledger = paid if paid is not None else (None, None)
    size = _image_size_for_aspect_ratio(aspect_ratio)
    try:
        result = await client.generate_image(
            prompt=prompt,
            model=_MODEL,
            size=size,
            quality="high",
            reference_images=reference_bytes,
            verb="generate_image",
            estimated_cost_usd=(
                paid_call.estimated_usd
                if paid_call is not None
                else tool_cost_usd("generate_image")
            ),
            asset_id=new_id,
            request_id=paid_call.request_id if paid_call is not None else None,
        )
    except BaseException as exc:
        if paid_call is not None and paid_ledger is not None:
            paid_ledger.mark_uncertain(
                paid_call.reservation_id,
                error=f"{type(exc).__name__}: {exc}",
            )
        raise

    if paid_call is not None and paid_ledger is not None:
        paid_ledger.settle(
            paid_call.reservation_id,
            actual_usd=paid_call.estimated_usd,
            result_asset_id=new_id,
        )

    image_bytes: bytes = result["image_bytes"]
    mime_type: str = result["mime_type"]
    ext = _extension_for_mime(mime_type)
    out_path = ctx.child_path(new_id, ext)
    out_path.write_bytes(image_bytes)

    raw_meta = result.get("raw_response_meta") or {}
    summary_text_from_model = raw_meta.get("model_text")
    style_chip = f" [{aspect_ratio}]" if aspect_ratio else ""
    short_prompt = prompt if len(prompt) < 80 else prompt[:77] + "…"
    summary = f"generated image{style_chip} via {result['model']}: {short_prompt!r}"

    record = ctx.registry.register_output(
        new_id,
        kind="image",
        path=out_path,
        summary=summary,
        lineage=list(map(str, reference_ids)),
        source={
            "kind": "generated_image",
            "provider": _PROVIDER,
            "model": result["model"],
            "request_id": (
                paid_call.request_id if paid_call is not None else result.get("request_id")
            ),
            "reservation_id": paid_call.reservation_id if paid_call is not None else None,
            "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            "reference_asset_ids": list(map(str, reference_ids)),
            "receipt": {
                "provider_request_id": (
                    paid_call.request_id
                    if paid_call is not None
                    else result.get("request_id")
                ),
                "budget_reservation_id": (
                    paid_call.reservation_id if paid_call is not None else None
                ),
                "estimated_cost_usd": (
                    paid_call.estimated_usd
                    if paid_call is not None
                    else tool_cost_usd("generate_image")
                ),
                "charge_status": "settled" if paid_call is not None else "provider_audited",
                "materialization_status": "materialized",
            },
        },
        license={
            "rights_basis": "generated_under_configured_provider_account",
            "provider": _PROVIDER,
            "usage_restrictions": "subject_to_provider_terms_and_input_content_rights",
            "attribution_required": False,
        },
    )
    if paid_call is not None and paid_ledger is not None:
        paid_ledger.mark_asset_materialized(
            paid_call.reservation_id,
            result_asset_id=new_id,
            asset_path=record.path,
            asset_sha256=record.sha256,
        )

    return {
        "asset_id": new_id,
        "summary": record.summary,
        "metadata": {
            "model": result["model"],
            "mime_type": mime_type,
            "size_bytes": len(image_bytes),
            "aspect_ratio": aspect_ratio,
            "image_size": size,
            "reference_asset_ids": list(map(str, reference_ids)),
            "provider_finish_reason": raw_meta.get("finish_reason"),
            "provider_text": summary_text_from_model,
            "provider_usage": raw_meta.get("usage_metadata"),
            "request_id": (
                paid_call.request_id if paid_call is not None else result.get("request_id")
            ),
            "reservation_id": paid_call.reservation_id if paid_call is not None else None,
        },
    }


def _client_from_ctx(ctx: ToolContext) -> OpenAIImageClient:
    """One subscription image client per session, cached on ctx.extra."""
    extra = ctx.extra if isinstance(ctx.extra, dict) else {}
    cached = extra.get("_openai_image_client")
    if isinstance(cached, OpenAIImageClient):
        return cached
    endpoint = str(extra.get("openai_subscription_image_endpoint") or "").strip()
    if not endpoint:
        endpoint = endpoint_from_chat_url(
            str(extra.get("openai_subscription_chat_endpoint") or "")
        )
    client = OpenAIImageClient(endpoint=endpoint or DEFAULT_ENDPOINT)
    extra["_openai_image_client"] = client
    return client


def _image_size_for_aspect_ratio(aspect_ratio: str | None) -> str:
    if aspect_ratio in {"9:16", "3:4"}:
        return "1024x1536"
    if aspect_ratio in {"16:9", "4:3"}:
        return "1536x1024"
    return _DEFAULT_IMAGE_SIZE


def _extension_for_mime(mime_type: str) -> str:
    mt = (mime_type or "").lower().strip()
    if mt in {"image/png", ""}:
        return ".png"
    if mt in {"image/jpeg", "image/jpg"}:
        return ".jpg"
    if mt == "image/webp":
        return ".webp"
    return ".bin"


__all__ = ["dispatch"]
