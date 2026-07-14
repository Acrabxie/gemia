"""subtitle -- put a timed subtitle track on a video (burn or soft-mux).

Bridges the buried ``gemia.video.subtitles`` engine into the agent surface. Two
sources:

- ``from_text`` (default, always works): you already have the words — a
  narration script, a caption block, or explicit ``cues``. The text is split
  into cues spread across the video's duration, written to SRT, and burned in.
  No ASR, so timing is even but wording is exact.
- ``transcribe``: no script in hand — recover the words from the video's own
  speech with Whisper, then burn. Requires ``openai-whisper``; if it isn't
  installed the tool says so instead of failing silently.

Returns a NEW video asset_id (the original is never mutated). Use ``burn=false``
to mux a toggleable soft-subtitle track instead of hard-coding it into the
picture (output is ``.mkv``/``.mp4`` with a selectable track).
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from gemia.tools._context import ProgressUpdate, ToolContext
from gemia.tools._ffmpeg import ffprobe_duration

_MAX_CUE_CHARS = 42  # wrap point for a readable single subtitle line


def _split_cues(text: str, duration: float) -> list[dict[str, Any]]:
    """Split narration text into time-even cues across ``duration`` seconds.

    Timing is proportional to each cue's character count so long lines dwell
    longer. Sentence punctuation (CJK and Latin) is preferred as the break.
    """
    text = " ".join(text.split())
    if not text:
        return []
    # Split on sentence enders first, then hard-wrap over-long pieces.
    pieces = [p.strip() for p in re.split(r"(?<=[.!?。！？;；])\s*", text) if p.strip()]
    segments: list[str] = []
    for piece in pieces:
        while len(piece) > _MAX_CUE_CHARS:
            cut = piece.rfind(" ", 0, _MAX_CUE_CHARS)
            cut = cut if cut > 0 else _MAX_CUE_CHARS  # CJK has no spaces
            segments.append(piece[:cut].strip())
            piece = piece[cut:].strip()
        if piece:
            segments.append(piece)
    if not segments:
        return []
    total_chars = sum(len(s) for s in segments) or 1
    cues: list[dict[str, Any]] = []
    t = 0.0
    for i, seg in enumerate(segments):
        share = (len(seg) / total_chars) * duration
        start = t
        end = duration if i == len(segments) - 1 else min(duration, t + share)
        cues.append({"start": round(start, 3), "end": round(max(end, start + 0.3), 3), "text": seg})
        t = end
    return cues


def _normalize_cues(raw: Any) -> list[dict[str, Any]]:
    cues: list[dict[str, Any]] = []
    for entry in raw or []:
        if not isinstance(entry, dict):
            continue
        text = str(entry.get("text") or "").strip()
        if not text:
            continue
        cues.append({
            "start": round(float(entry.get("start") or 0.0), 3),
            "end": round(float(entry.get("end") or 0.0), 3),
            "text": text,
        })
    return cues


async def dispatch(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    asset_id = str(args.get("asset_id") or "")
    if not asset_id:
        raise ValueError("subtitle requires an 'asset_id' (the video to caption)")
    record = ctx.registry.get(asset_id)
    if record.kind != "video":
        raise ValueError(f"subtitle input {asset_id} is {record.kind!r}, expected video")

    source = str(args.get("source") or "text").strip().lower()
    if source not in {"text", "transcribe"}:
        raise ValueError("subtitle source must be 'text' or 'transcribe'")
    burn = bool(args.get("burn", True))
    style = args.get("style") if isinstance(args.get("style"), dict) else {}

    from gemia.video import subtitles as _subs

    new_id = ctx.registry.allocate_id("video")

    # ── transcribe: recover words from the video's own speech (Whisper) ──
    if source == "transcribe":
        if not burn:
            raise ValueError("subtitle source='transcribe' currently only supports burn=true")
        out_path = ctx.child_path(new_id, ".mp4")
        language = str(args.get("language") or "en").strip() or "en"
        ctx.emit_progress(ProgressUpdate(percent=15, message="transcribing speech (Whisper)", eta_sec=30))
        try:
            _subs.auto_subtitle(str(record.path), str(out_path), language=language)
        except ImportError as exc:
            raise ValueError(
                "subtitle source='transcribe' needs Whisper — run `pip install openai-whisper`, "
                "or pass source='text' with the script you already have."
            ) from exc
        record_out = ctx.registry.register_output(
            new_id, kind="video", path=out_path,
            summary=f"transcribed+burned subtitles ({language}) from {asset_id}",
            lineage=(asset_id,),
        )
        return {"asset_id": new_id, "summary": record_out.summary,
                "metadata": {"source": "transcribe", "language": language, "burned": True}}

    # ── from_text: caption from the words you already have ──
    cues = _normalize_cues(args.get("cues"))
    if not cues:
        text = str(args.get("text") or "").strip()
        if not text:
            raise ValueError("subtitle source='text' requires 'text' (or explicit 'cues')")
        duration = float(ffprobe_duration(record.path))
        cues = _split_cues(text, duration)
    if not cues:
        raise ValueError("subtitle produced no cues from the given text")

    srt_path = ctx.child_path(new_id, ".srt")
    _subs.make_srt(cues, str(srt_path))

    ctx.emit_progress(ProgressUpdate(percent=60, message=("burning" if burn else "muxing") + " subtitles", eta_sec=10))
    if burn:
        out_path = ctx.child_path(new_id, ".mp4")
        _subs.burn_subtitles(
            str(record.path), str(srt_path), str(out_path),
            font_size=int(style.get("font_size", style.get("fontsize", 28))),
            font_color=str(style.get("font_color", style.get("fontcolor", "white"))),
            outline_color=str(style.get("outline_color", "black")),
            outline_width=int(style.get("outline_width", 2)),
            margin_v=int(style.get("margin_v", 40)),
        )
    else:
        # mov_text (mp4-native) is the toggleable soft-subtitle codec; keep the
        # container mp4 so the muxed track is selectable in QuickTime/VLC.
        out_path = ctx.child_path(new_id, ".mp4")
        _subs.mux_subtitle_track(
            str(record.path), str(srt_path), str(out_path),
            language=str(args.get("language") or "eng"),
            title=str(args.get("title") or "Subtitles"),
        )

    record_out = ctx.registry.register_output(
        new_id, kind="video", path=out_path,
        summary=f"{'burned' if burn else 'soft'} subtitles ({len(cues)} cues) on {asset_id}",
        lineage=(asset_id,),
    )
    return {
        "asset_id": new_id,
        "summary": record_out.summary,
        "metadata": {"source": "text", "cue_count": len(cues), "burned": burn,
                     "srt_path": str(srt_path)},
    }


__all__ = ["dispatch"]
