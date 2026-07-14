"""Lottie inspection tools for v3 sessions."""
from __future__ import annotations

from typing import Any

from gemia.tools._context import ToolContext
from gemia.video.lottie_renderer import save_lottie_frame_png, select_lottie_renderer


async def dispatch_inspect(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    asset_id = str(args.get("asset_id") or "")
    if not asset_id:
        raise ValueError("inspect_lottie requires asset_id")
    record = ctx.registry.get(asset_id)
    if record.kind != "lottie":
        raise ValueError(f"inspect_lottie requires a lottie asset, got {record.kind!r}")

    renderer = select_lottie_renderer()
    metadata = renderer.get_metadata(str(record.path))
    fps = float(metadata.get("fps") or 30.0)
    frames = max(int(metadata.get("frames") or 1), 1)

    frame_arg = args.get("frame")
    time_arg = args.get("time_sec", args.get("time"))
    if frame_arg is not None and time_arg is not None:
        raise ValueError("pass either frame or time_sec, not both")
    if frame_arg is not None:
        frame_index = int(frame_arg)
    elif time_arg is not None:
        frame_index = round(float(time_arg) * fps)
    else:
        frame_index = 0
    frame_index = max(0, min(frame_index, frames - 1))

    width = int(args.get("width") or metadata.get("width") or 512)
    height = int(args.get("height") or metadata.get("height") or 512)
    new_id = ctx.registry.allocate_id("image")
    out_path = ctx.child_path(new_id, ".png")
    frame_meta = save_lottie_frame_png(
        record.path,
        out_path,
        width=width,
        height=height,
        frame_index=frame_index,
    )
    frame_record = ctx.registry.register_output(
        new_id,
        kind="image",
        path=out_path,
        summary=f"frame {frame_index} from Lottie {asset_id}",
        lineage=[asset_id],
    )
    return {
        "asset_id": new_id,
        "source_asset_id": asset_id,
        "summary": frame_record.summary,
        "frame": frame_index,
        "time_sec": frame_index / max(fps, 1.0),
        "metadata": {
            **metadata,
            "renderer": frame_meta.get("renderer"),
            "output_width": width,
            "output_height": height,
        },
        "thumbnail_path": str(out_path),
        "thumbnail_for_next_message": True,
    }


__all__ = ["dispatch_inspect"]
