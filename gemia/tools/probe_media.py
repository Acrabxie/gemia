"""probe_media: cheap physical media metadata for registered assets."""
from __future__ import annotations

from fractions import Fraction
from typing import Any

from gemia.tools._context import ToolContext
from gemia.tools._ffmpeg import audio_stream, ffprobe_metadata, video_stream


async def dispatch(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    asset_id = str(args["asset_id"])
    record = ctx.registry.get(asset_id)
    if record.kind == "lottie":
        from gemia.video.timeline_assets import probe_media as probe_timeline_media

        meta = probe_timeline_media(str(record.path))
        return _shape_result(asset_id, record.kind, meta)

    meta = ffprobe_metadata(record.path)
    fmt = meta.get("format") if isinstance(meta.get("format"), dict) else {}
    video = video_stream(meta) or {}
    audio = audio_stream(meta) or {}
    duration_sec = _float_or_zero(fmt.get("duration"))
    width = int(video.get("width") or 0)
    height = int(video.get("height") or 0)
    fps = _rate(video.get("avg_frame_rate") or video.get("r_frame_rate"))
    result = {
        "asset_id": asset_id,
        "kind": record.kind,
        "duration_sec": duration_sec,
        "duration_ms": int(round(duration_sec * 1000)),
        "width": width,
        "height": height,
        "fps": fps,
        "codec": str(video.get("codec_name") or audio.get("codec_name") or ""),
        "video_codec": str(video.get("codec_name") or ""),
        "audio_codec": str(audio.get("codec_name") or ""),
        "channels": int(audio.get("channels") or 0),
        "sample_rate": int(audio.get("sample_rate") or 0),
        "bit_rate": int(fmt.get("bit_rate") or 0),
        "file_size_bytes": int(fmt.get("size") or record.path.stat().st_size),
        "has_video": bool(video),
        "has_audio": bool(audio),
        "stream_count": len(meta.get("streams") or []),
    }
    return result


def _shape_result(asset_id: str, kind: str, meta: dict[str, Any]) -> dict[str, Any]:
    duration_sec = _float_or_zero(meta.get("duration"))
    return {
        "asset_id": asset_id,
        "kind": kind,
        "duration_sec": duration_sec,
        "duration_ms": int(round(duration_sec * 1000)),
        "width": int(meta.get("width") or 0),
        "height": int(meta.get("height") or 0),
        "fps": _float_or_zero(meta.get("fps")),
        "codec": str(meta.get("codec") or ""),
        "video_codec": str(meta.get("codec") or ""),
        "audio_codec": str(meta.get("audio_codec") or ""),
        "channels": int(meta.get("channels") or 0),
        "sample_rate": int(meta.get("sample_rate") or 0),
        "bit_rate": int(meta.get("bit_rate") or 0),
        "file_size_bytes": int(meta.get("file_size_bytes") or 0),
        "has_video": kind in {"video", "image", "lottie"},
        "has_audio": bool(meta.get("has_audio")),
        "stream_count": 1,
    }


def _rate(value: Any) -> float:
    if not value:
        return 0.0
    try:
        frac = Fraction(str(value))
    except Exception:
        return 0.0
    if frac.denominator == 0:
        return 0.0
    return round(float(frac), 6)


def _float_or_zero(value: Any) -> float:
    try:
        return float(value or 0.0)
    except Exception:
        return 0.0


__all__ = ["dispatch"]
