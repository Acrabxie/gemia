"""generate_image — Nano Banana 2 via Vertex AI.

Sync dispatcher (no LRO). The host:
  1. Calls Vertex AI generateContent with the model's prompt.
  2. Receives base64-encoded image bytes in the response.
  3. Decodes to bytes, writes to ``ctx.child_path(new_id, ".png")``.
  4. Registers the file as a new image asset.
  5. Returns ``{asset_id, summary, metadata}`` — base64 NEVER appears in
     the returned dict, so the agent loop's SSE event channel sees only
     the asset_id + URL the frontend can GET. (A 2K PNG is ~3 MB; pushing
     it through SSE would saturate the wire and bloat frontend memory.)

The dispatcher does NOT retry. Provider 5xx errors propagate as
``VertexAPIError`` → ``tool_exec_error`` event → tool_result for the
model. The model decides whether to fall back or surrender — host does
not auto-retry, auto-switch models, or hide failures.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from gemia.ai.google_genai_client import (
    GoogleGenAIClient,
    VertexAPIError,
    VertexAuthMissingError,
)
from gemia.budget_guard import tool_cost_usd
from gemia.tools._context import ToolContext


_DEFAULT_MODEL = "gemini-3.1-flash-image"  # Nano Banana 2 (Vertex)
_DEFAULT_IMAGE_SIZE = "2K"


async def dispatch(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    prompt = str(args.get("prompt") or "").strip()
    if not prompt:
        raise ValueError("generate_image requires a non-empty prompt")
    aspect_ratio = args.get("aspect_ratio")
    if aspect_ratio is not None:
        aspect_ratio = str(aspect_ratio).strip() or None
    style = str(args.get("style") or "").strip()
    if style:
        # Style is a model hint, not a schema parameter at the provider.
        # Append it to the prompt the way users naturally would.
        prompt = f"{prompt}. Style: {style}"

    # Resolve reference assets (image-to-image guidance). The schema says
    # reference_asset_ids must already exist in the session registry; we
    # resolve them to bytes here.
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

    try:
        result = await client.generate_image(
            prompt=prompt,
            model=_DEFAULT_MODEL,
            aspect_ratio=aspect_ratio,
            image_size=_DEFAULT_IMAGE_SIZE,
            reference_images=reference_bytes,
            verb="generate_image",
            estimated_cost_usd=tool_cost_usd("generate_image"),
            asset_id=new_id,
        )
    except VertexAuthMissingError:
        # Honest user-actionable message — re-raise as-is so the agent loop
        # surfaces it cleanly. Distinct error_class lets telemetry quantify
        # "users couldn't run generate_image because Vertex auth not set".
        raise

    image_bytes: bytes = result["image_bytes"]
    mime_type: str = result["mime_type"]
    ext = _extension_for_mime(mime_type)
    out_path = ctx.child_path(new_id, ext)
    out_path.write_bytes(image_bytes)

    summary_text_from_model = result["raw_response_meta"].get("model_text")
    style_chip = f" [{aspect_ratio}]" if aspect_ratio else ""
    short_prompt = prompt if len(prompt) < 80 else prompt[:77] + "…"
    summary = (
        f"generated image{style_chip} via {result['model']}: {short_prompt!r}"
    )
    if summary_text_from_model:
        # The provider sometimes returns descriptive text alongside the
        # image. Surface it in metadata, not the user-facing summary
        # (avoids double narration with the model's own reply text).
        pass

    record = ctx.registry.register_output(
        new_id,
        kind="image",
        path=out_path,
        summary=summary,
        lineage=list(map(str, reference_ids)),
    )

    return {
        "asset_id": new_id,
        "summary": record.summary,
        "metadata": {
            "model": result["model"],
            "mime_type": mime_type,
            "size_bytes": len(image_bytes),
            "aspect_ratio": aspect_ratio,
            "image_size": _DEFAULT_IMAGE_SIZE,
            "reference_asset_ids": list(map(str, reference_ids)),
            "provider_finish_reason": result["raw_response_meta"].get("finish_reason"),
            "provider_text": summary_text_from_model,
            "provider_usage": result["raw_response_meta"].get("usage_metadata"),
        },
    }


def _client_from_ctx(ctx: ToolContext) -> GoogleGenAIClient:
    """One client per session, cached on ctx.extra to avoid re-reading config."""
    cached = ctx.extra.get("_google_genai_client") if isinstance(ctx.extra, dict) else None
    if isinstance(cached, GoogleGenAIClient):
        return cached
    client = GoogleGenAIClient()
    if isinstance(ctx.extra, dict):
        ctx.extra["_google_genai_client"] = client
    return client


def _extension_for_mime(mime_type: str) -> str:
    mt = (mime_type or "").lower().strip()
    if mt in {"image/png", ""}:
        return ".png"
    if mt in {"image/jpeg", "image/jpg"}:
        return ".jpg"
    if mt == "image/webp":
        return ".webp"
    # Unknown mime — preserve the bytes but flag with .bin so downstream
    # tools don't pretend it's a renderable image without inspection.
    return ".bin"


__all__ = ["dispatch"]
