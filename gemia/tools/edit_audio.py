"""edit_audio: standalone gain and fade preprocessing for audio assets."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from gemia.errors import RECOVERY_FIX_ARGS, ToolError
from gemia.tools._context import ToolContext
from gemia.tools._ffmpeg import ffprobe_duration, run_ffmpeg_with_progress


async def dispatch(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    asset_id = str(args["asset_id"])
    record = ctx.registry.get(asset_id)
    if record.kind != "audio":
        raise ToolError(
            f"edit_audio requires an audio asset; {asset_id} is a {record.kind}.",
            code="E_UNSUPPORTED",
            recovery=RECOVERY_FIX_ARGS,
            hint="Pass an audio asset_id. Use adjust_media for image/video brightness or saturation.",
        )

    gain_db = _number(args.get("gain_db", 0.0), "gain_db", -60.0, 36.0)
    fade_in = _number(args.get("fade_in_sec", 0.0), "fade_in_sec", 0.0, 3600.0)
    fade_out = _number(args.get("fade_out_sec", 0.0), "fade_out_sec", 0.0, 3600.0)
    if abs(gain_db) <= 1e-9 and fade_in <= 0 and fade_out <= 0:
        raise ToolError(
            "edit_audio needs at least one change: gain_db, fade_in_sec, or fade_out_sec.",
            code="E_BAD_ARG",
            recovery=RECOVERY_FIX_ARGS,
        )

    duration = ffprobe_duration(record.path)
    if duration > 0:
        fade_in = min(fade_in, duration)
        fade_out = min(fade_out, duration)

    filters = _audio_filters(gain_db=gain_db, fade_in=fade_in, fade_out=fade_out, duration=duration)

    new_id = ctx.registry.allocate_id("audio")
    out_path = ctx.child_path(new_id, ".wav")
    cmd = [
        "ffmpeg", "-y",
        "-i", str(record.path),
        "-af", ",".join(filters),
        "-c:a", "pcm_s16le",
        str(out_path),
    ]
    await run_ffmpeg_with_progress(cmd, total_seconds=duration, progress=ctx.emit_progress)
    summary = _summary(asset_id, duration, gain_db, fade_in, fade_out)
    out = ctx.registry.register_output(
        new_id,
        kind="audio",
        path=out_path,
        summary=summary,
        lineage=[asset_id],
    )
    return {
        "asset_id": new_id,
        "summary": out.summary,
        "metadata": {
            "duration_sec": duration,
            "gain_db": gain_db,
            "fade_in_sec": fade_in,
            "fade_out_sec": fade_out,
            "filters": filters,
        },
    }


def _audio_filters(*, gain_db: float, fade_in: float, fade_out: float, duration: float) -> list[str]:
    filters: list[str] = []
    if abs(gain_db) > 1e-9:
        multiplier = 10 ** (gain_db / 20.0)
        filters.append(f"volume={multiplier:.8f}")
    if fade_in > 0:
        filters.append(f"afade=t=in:st=0:d={fade_in:.6f}")
    if fade_out > 0:
        start = max(0.0, duration - fade_out) if duration > 0 else 0.0
        filters.append(f"afade=t=out:st={start:.6f}:d={fade_out:.6f}")
    return filters


def _number(value: Any, name: str, low: float, high: float) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError) as exc:
        raise ToolError(f"{name} must be a number.", code="E_BAD_ARG", recovery=RECOVERY_FIX_ARGS) from exc
    if not low <= numeric <= high:
        raise ToolError(
            f"{name} must be in [{low}, {high}], got {numeric}.",
            code="E_BAD_ARG",
            recovery=RECOVERY_FIX_ARGS,
        )
    return numeric


def _summary(asset_id: str, duration: float, gain_db: float, fade_in: float, fade_out: float) -> str:
    parts = []
    if abs(gain_db) > 1e-9:
        parts.append(f"gain {gain_db:+.1f}dB")
    if fade_in > 0:
        parts.append(f"fade in {fade_in:.2f}s")
    if fade_out > 0:
        parts.append(f"fade out {fade_out:.2f}s")
    return f"edited {asset_id}: {', '.join(parts)} ({duration:.2f}s)"


__all__ = ["dispatch", "_audio_filters"]
