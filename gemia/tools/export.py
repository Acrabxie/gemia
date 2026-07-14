"""export: final encode of an asset at a requested quality, codec and format."""
from __future__ import annotations

import re
from typing import Any

from gemia.errors import (
    RECOVERY_FIX_ARGS,
    RECOVERY_SWITCH_TOOL,
    ToolError,
)
from gemia.tools._context import ToolContext
from gemia.tools._ffmpeg import ffprobe_duration, run_ffmpeg_with_progress


# Height + rate-control per quality tier. crf is the H.264 baseline; H.265 adds
# a fixed offset (see _CODECS) for comparable perceptual quality at a smaller file.
_QUALITY_PROFILES: dict[str, dict[str, Any]] = {
    "4k":    {"height": 2160, "crf": "16", "preset": "slow"},
    "1080p": {"height": 1080, "crf": "18", "preset": "slow"},
    "720p":  {"height": 720,  "crf": "20", "preset": "medium"},
    "480p":  {"height": 480,  "crf": "23", "preset": "medium"},
    "draft": {"height": 480,  "crf": "28", "preset": "veryfast"},
}

# codec -> (ffmpeg encoder, crf offset vs the H.264 baseline, extra flags for mp4/mov)
_CODECS: dict[str, dict[str, Any]] = {
    "h264": {"encoder": "libx264", "crf_offset": 0, "extra": []},
    "h265": {"encoder": "libx265", "crf_offset": 4, "extra": ["-tag:v", "hvc1"]},
}
_CODEC_ALIASES = {"hevc": "h265", "avc": "h264", "x264": "h264", "x265": "h265"}

_FORMAT_TUNING: dict[str, dict[str, list[str]]] = {
    "mp4": {
        "audio": ["-c:a", "aac", "-b:a", "192k"],
        "container": ["-movflags", "+faststart"],
    },
    "mov": {
        "audio": ["-c:a", "aac", "-b:a", "192k"],
        "container": [],
    },
    "webm": {
        "video": ["-c:v", "libvpx-vp9", "-b:v", "0"],
        "audio": ["-c:a", "libopus", "-b:a", "160k"],
        "container": [],
    },
    "gif": {
        "video": [],
        "audio": ["-an"],
        "container": [],
    },
}
_H26X_FORMATS = {"mp4", "mov"}

_PLATFORM_TWEAKS: dict[str, list[str]] = {
    "youtube":   [],
    "instagram": ["-t", "60"],
    "tiktok":    ["-t", "180"],
    "twitter":   ["-t", "140", "-fs", "512M"],
    "prores":    [],
    "generic":   [],
}

_BITRATE_RE = re.compile(r"^\d+(\.\d+)?[kKmM]?$")


def _double_bitrate(value: str) -> str:
    """Return roughly 2x a bitrate string (e.g. '8M' -> '16M') for -bufsize."""
    m = re.match(r"^(\d+(?:\.\d+)?)([kKmM]?)$", value)
    if not m:
        return value
    num = float(m.group(1)) * 2
    unit = m.group(2)
    return f"{num:g}{unit}"


def _h26x_video_args(profile: dict[str, Any], *, codec: str,
                     video_bitrate: str | None, color: str) -> list[str]:
    spec = _CODECS[codec]
    args: list[str] = ["-c:v", spec["encoder"], "-pix_fmt", "yuv420p", *spec["extra"]]
    if video_bitrate:
        args += ["-b:v", video_bitrate, "-maxrate", video_bitrate,
                 "-bufsize", _double_bitrate(video_bitrate)]
        args += ["-preset", str(profile["preset"])]
    else:
        crf = str(int(profile["crf"]) + int(spec["crf_offset"]))
        args += ["-crf", crf, "-preset", str(profile["preset"])]
    if color == "bt709":
        args += ["-color_primaries", "bt709", "-color_trc", "bt709",
                 "-colorspace", "bt709"]
    return args


async def dispatch(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    asset_id = str(args["asset_id"])
    fmt = str(args["format"])
    quality = str(args["quality"])
    platform = str(args.get("platform") or "generic")
    codec = _CODEC_ALIASES.get(str(args.get("codec") or "h264").lower(),
                               str(args.get("codec") or "h264").lower())
    color = str(args.get("color") or "auto").lower()
    fps = args.get("fps")
    video_bitrate = args.get("video_bitrate")
    audio_bitrate = args.get("audio_bitrate")

    src = ctx.registry.get(asset_id)
    if src.kind != "video":
        raise ToolError(
            f"export applies to video assets; {asset_id} is a {src.kind}.",
            code="E_UNSUPPORTED",
            recovery=RECOVERY_SWITCH_TOOL,
            hint="Pass a video asset_id, or use a different verb for this asset type.",
        )

    if fmt not in _FORMAT_TUNING:
        raise ToolError(
            f"unknown format {fmt!r}.", code="E_UNSUPPORTED", recovery=RECOVERY_FIX_ARGS,
            valid_options=list(_FORMAT_TUNING.keys()),
            hint=f"Format must be one of: {', '.join(_FORMAT_TUNING.keys())}",
        )
    if quality not in _QUALITY_PROFILES:
        raise ToolError(
            f"unknown quality {quality!r}.", code="E_UNSUPPORTED", recovery=RECOVERY_FIX_ARGS,
            valid_options=list(_QUALITY_PROFILES.keys()),
            hint=f"Quality must be one of: {', '.join(_QUALITY_PROFILES.keys())}",
        )
    if platform not in _PLATFORM_TWEAKS:
        raise ToolError(
            f"unknown platform {platform!r}.", code="E_UNSUPPORTED", recovery=RECOVERY_FIX_ARGS,
            valid_options=list(_PLATFORM_TWEAKS.keys()),
            hint=f"Platform must be one of: {', '.join(_PLATFORM_TWEAKS.keys())}",
        )
    if codec not in _CODECS:
        raise ToolError(
            f"unknown codec {codec!r}.", code="E_UNSUPPORTED", recovery=RECOVERY_FIX_ARGS,
            valid_options=["h264", "h265"],
            hint="codec must be 'h264' or 'h265' (aliases: hevc/avc). Only applies to mp4/mov.",
        )
    if color not in ("auto", "bt709"):
        raise ToolError(
            f"unknown color {color!r}.", code="E_UNSUPPORTED", recovery=RECOVERY_FIX_ARGS,
            valid_options=["auto", "bt709"], hint="color must be 'auto' or 'bt709'.",
        )
    if video_bitrate is not None and not _BITRATE_RE.match(str(video_bitrate)):
        raise ToolError(
            f"invalid video_bitrate {video_bitrate!r}.", code="E_BAD_ARG",
            recovery=RECOVERY_FIX_ARGS, hint="Use a value like '8M' or '800k'.",
        )
    if audio_bitrate is not None and not _BITRATE_RE.match(str(audio_bitrate)):
        raise ToolError(
            f"invalid audio_bitrate {audio_bitrate!r}.", code="E_BAD_ARG",
            recovery=RECOVERY_FIX_ARGS, hint="Use a value like '192k'.",
        )
    if fps is not None:
        try:
            fps = float(fps)
            if not (1.0 <= fps <= 120.0):
                raise ValueError
        except (TypeError, ValueError):
            raise ToolError(
                f"invalid fps {fps!r}.", code="E_BAD_ARG", recovery=RECOVERY_FIX_ARGS,
                hint="fps must be a number between 1 and 120.",
            )

    profile = _QUALITY_PROFILES[quality]
    tuning = _FORMAT_TUNING[fmt]
    duration = ffprobe_duration(src.path)
    output_kind = "image" if fmt == "gif" else "video"
    new_id = ctx.registry.allocate_id(output_kind)
    out_path = ctx.child_path(new_id, f".{fmt}")

    scale_filter = f"scale=-2:{profile['height']}"
    if color == "bt709" and fmt != "gif":
        # scale resets primaries/transfer, so re-stamp them in the filtergraph;
        # the -color_* output flags alone only fix colorspace (matrix).
        scale_filter += (":out_color_matrix=bt709"
                         ",setparams=color_primaries=bt709:color_trc=bt709:colorspace=bt709")
    video_args: list[str] = ["-vf", scale_filter]
    if fmt in _H26X_FORMATS:
        video_args += _h26x_video_args(profile, codec=codec,
                                       video_bitrate=str(video_bitrate) if video_bitrate else None,
                                       color=color)
    else:
        video_args += list(tuning.get("video", []))
        if fmt != "gif" and profile.get("crf"):
            video_args += ["-crf", str(profile["crf"]), "-preset", str(profile["preset"])]
    if fps is not None and fmt != "gif":
        video_args += ["-r", f"{fps:g}"]

    audio_args = list(tuning["audio"])
    if audio_bitrate and fmt != "gif":
        audio_args += ["-b:a", str(audio_bitrate)]

    cmd = [
        "ffmpeg", "-y",
        "-i", str(src.path),
        *video_args,
        *audio_args,
        *tuning["container"],
        *_PLATFORM_TWEAKS[platform],
        str(out_path),
    ]
    await run_ffmpeg_with_progress(cmd, total_seconds=duration, progress=ctx.emit_progress)

    codec_label = codec if fmt in _H26X_FORMATS else ("vp9" if fmt == "webm" else fmt)
    rate_label = f"{video_bitrate}bps" if video_bitrate else f"crf{profile.get('crf')}"
    summary = (
        f"exported {asset_id} as {fmt}/{quality} ({profile['height']}p, {codec_label}, "
        f"{rate_label}{', bt709' if color == 'bt709' else ''}, platform={platform}) "
        f"-> {out_path.name}"
    )
    record = ctx.registry.register_output(
        new_id, kind=output_kind, path=out_path, summary=summary, lineage=[asset_id]
    )
    return {
        "asset_id": new_id,
        "kind": record.kind,
        "summary": record.summary,
        "metadata": {
            "format": fmt,
            "quality": quality,
            "platform": platform,
            "codec": codec_label,
            "color": color,
            "fps": fps,
            "video_bitrate": video_bitrate,
            "height_target": profile["height"],
            "duration_sec": duration,
        },
    }


__all__ = ["dispatch"]
