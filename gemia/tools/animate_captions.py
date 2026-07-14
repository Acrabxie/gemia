"""animate_captions -- per-word animated captions (karaoke/word-pop style).

Bridges the buried ``gemia.video.animated_subtitles`` renderer into the agent
surface. Unlike ``subtitle`` (a static line-level cue track) this animates
word-by-word — the spoken word highlights/pops as it lands, the TikTok/Reels
caption look.

You supply the words. Because we control the script, no ASR is needed:
``text`` distributes the words evenly across the clip, or pass explicit
``word_timings`` ([{word, start_seconds, end_seconds}]) for exact sync (e.g.
from a forced-aligner later). Returns a NEW video asset; the original is
untouched. Rendering is per-frame (PIL), so it is slower than a plain burn —
use it for the hero caption pass, not every clip.
"""
from __future__ import annotations

from typing import Any

from gemia.tools._context import ProgressUpdate, ToolContext

_PRESETS = {"karaoke_pop", "quiet_captions"}


def _normalize_word_timings(raw: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        word = str(item.get("word") or "").strip()
        if not word:
            continue
        entry: dict[str, Any] = {"word": word}
        if item.get("start_seconds") is not None:
            entry["start_seconds"] = float(item["start_seconds"])
        if item.get("end_seconds") is not None:
            entry["end_seconds"] = float(item["end_seconds"])
        out.append(entry)
    return out


async def dispatch(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    asset_id = str(args.get("asset_id") or "")
    if not asset_id:
        raise ValueError("animate_captions requires an 'asset_id' (the video to caption)")
    record = ctx.registry.get(asset_id)
    if record.kind != "video":
        raise ValueError(f"animate_captions input {asset_id} is {record.kind!r}, expected video")

    word_timings = _normalize_word_timings(args.get("word_timings"))
    transcript = str(args.get("text") or "").strip()
    if not word_timings and not transcript:
        raise ValueError("animate_captions requires 'text' (the words) or explicit 'word_timings'")

    preset = str(args.get("preset") or "karaoke_pop").strip()
    if preset not in _PRESETS:
        raise ValueError(f"animate_captions preset must be one of {sorted(_PRESETS)}")
    font_size = int(args.get("font_size") or 54)
    if font_size <= 0:
        raise ValueError("font_size must be > 0")

    from gemia.video.animated_subtitles import render_ai_animated_subtitles_plan

    new_id = ctx.registry.allocate_id("video")
    out_path = ctx.child_path(new_id, ".mp4")
    ctx.emit_progress(ProgressUpdate(percent=10, message=f"rendering animated captions ({preset})", eta_sec=30))
    render_ai_animated_subtitles_plan(
        str(record.path),
        str(out_path),
        word_timings=word_timings or None,
        transcript=transcript or None,
        preset=preset,
        font_size=font_size,
        active_color=str(args.get("active_color") or "yellow"),
        inactive_color=str(args.get("inactive_color") or "white"),
    )
    word_count = len(word_timings) if word_timings else len(transcript.split())
    ctx.emit_progress(ProgressUpdate(percent=100, message="animated captions ready", eta_sec=0))
    record_out = ctx.registry.register_output(
        new_id,
        kind="video",
        path=out_path,
        summary=f"animated captions ({preset}, {word_count} words) on {asset_id}",
        lineage=(asset_id,),
    )
    return {
        "asset_id": new_id,
        "summary": record_out.summary,
        "metadata": {
            "preset": preset,
            "word_count": word_count,
            "timed": bool(word_timings),
            "font_size": font_size,
        },
    }


__all__ = ["dispatch"]
