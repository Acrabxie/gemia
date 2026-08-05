"""Shared ffmpeg / ffprobe helpers for v3 tool dispatchers.

``run_ffmpeg_with_progress`` runs ffmpeg via ``subprocess.Popen`` with
``-progress pipe:1`` so we get a clean key=value stream on stdout we can
parse without fighting the carriage-return live-updating stderr line.

Progress callbacks are real: each ``out_time_us=`` line emits one
``ProgressUpdate`` with a percent derived from ``total_seconds``. If
``total_seconds`` is unknown (0 or None), percent stays None and the
frontend renders an indeterminate spinner. Honest reporting.

GPU acceleration
================
Tools no longer hard-code ``libx264``. They ask ``get_video_encoder_args``
for the best encoder available *at runtime*: Apple VideoToolbox on macoS,
NVENC on NVIDIA, QSV on Intel, and libx264/libx265 as the universal
fallback. Detection runs ``ffmpeg -encoders`` once (cached).

Because hardware encoders are pickier than x264 about resolutions, pixel
formats and colour spaces, ``run_ffmpeg_with_progress`` wraps every encode
in a *double-try*: if a GPU command raises ``RuntimeError`` it is rewritten
to the CPU equivalent and run once more. Production never hard-fails just
because a driver rejected a frame.

Set ``GEMIA_VIDEO_ENCODER=cpu`` to force software encoding (used by the test
suite for determinism, and available as an operator escape hatch).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Any

from gemia.compat import ffmpeg_path, ffprobe_path
from gemia.tools._context import ProgressCallback, ProgressUpdate

logger = logging.getLogger(__name__)


def ffprobe_metadata(path: Path) -> dict[str, Any]:
    """Return parsed ffprobe JSON for the given media file."""
    cmd = [
        ffprobe_path(), "-v", "error",
        "-print_format", "json",
        "-show_format", "-show_streams",
        str(path),
    ]
    out = subprocess.check_output(cmd, text=True)
    return json.loads(out)


def ffprobe_duration(path: Path) -> float:
    """Return media duration in seconds (0.0 if unknown)."""
    meta = ffprobe_metadata(path)
    fmt = meta.get("format") or {}
    raw = fmt.get("duration")
    if raw is None:
        return 0.0
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


def video_stream(metadata: dict[str, Any]) -> dict[str, Any] | None:
    for stream in metadata.get("streams") or []:
        if stream.get("codec_type") == "video":
            return stream
    return None


def audio_stream(metadata: dict[str, Any]) -> dict[str, Any] | None:
    for stream in metadata.get("streams") or []:
        if stream.get("codec_type") == "audio":
            return stream
    return None


def short_summary(metadata: dict[str, Any]) -> str:
    """Compact human-readable description of a media file."""
    fmt = metadata.get("format") or {}
    parts: list[str] = []
    try:
        parts.append(f"{float(fmt.get('duration', 0)):.2f}s")
    except (TypeError, ValueError):
        pass
    vs = video_stream(metadata)
    if vs:
        w = vs.get("width")
        h = vs.get("height")
        if w and h:
            parts.append(f"{w}x{h}")
        fps_raw = vs.get("avg_frame_rate") or vs.get("r_frame_rate") or "0/0"
        try:
            num, den = fps_raw.split("/")
            if int(den) != 0:
                parts.append(f"{float(num) / float(den):.1f}fps")
        except (ValueError, ZeroDivisionError):
            pass
        codec = vs.get("codec_name")
        if codec:
            parts.append(str(codec))
    bitrate = fmt.get("bit_rate")
    if bitrate:
        try:
            parts.append(f"{int(bitrate) / 1000:.0f}kbps")
        except (TypeError, ValueError):
            pass
    return " ".join(parts) or "media"


# --------------------------------------------------------------------------- #
# GPU / CPU encoder selection
# --------------------------------------------------------------------------- #

# Logical codec -> the software (CPU) encoder that is always available.
_CPU_ENCODER = {"h264": "libx264", "h265": "libx265"}

# Hardware encoder families, in preference order. VideoToolbox first because
# the primary dev + a large share of production runs on Apple Silicon.
_GPU_FAMILIES = ("videotoolbox", "nvenc", "qsv")

# (logical codec, family) -> ffmpeg encoder name.
_GPU_ENCODER = {
    ("h264", "videotoolbox"): "h264_videotoolbox",
    ("h265", "videotoolbox"): "hevc_videotoolbox",
    ("h264", "nvenc"): "h264_nvenc",
    ("h265", "nvenc"): "hevc_nvenc",
    ("h264", "qsv"): "h264_qsv",
    ("h265", "qsv"): "hevc_qsv",
}

# Reverse map used by the CPU-fallback rewriter.
_GPU_TO_CPU_ENCODER = {
    "h264_videotoolbox": "libx264", "hevc_videotoolbox": "libx265",
    "h264_nvenc": "libx264", "hevc_nvenc": "libx265",
    "h264_qsv": "libx264", "hevc_qsv": "libx265",
}
_ALL_GPU_ENCODERS = frozenset(_GPU_TO_CPU_ENCODER)

# Rate-control flags that take a value and are meaningless to libx264/libx265.
# They must be stripped when downgrading a GPU command to CPU.
_GPU_RATE_FLAGS = {"-q:v", "-cq", "-global_quality"}

_CODEC_ALIASES = {
    "h264": "h264", "avc": "h264", "x264": "h264", "libx264": "h264",
    "h265": "h265", "hevc": "h265", "x265": "h265", "libx265": "h265",
}

# Quality presets. ``default`` reproduces the historical CPU baseline
# (``-preset veryfast -crf 20``) and the documented GPU examples exactly, so
# swapping in ``get_video_encoder_args`` is behaviour-preserving on CPU hosts.
_PRESETS: dict[str, dict[str, str]] = {
    "default": {
        "x_preset": "veryfast", "x264_crf": "20", "x265_crf": "24",
        "vt_q": "65", "nvenc_preset": "p4", "nvenc_cq_h264": "20",
        "nvenc_cq_h265": "24", "qsv_gq_h264": "20", "qsv_gq_h265": "24",
    },
    "high": {
        "x_preset": "slow", "x264_crf": "18", "x265_crf": "22",
        "vt_q": "75", "nvenc_preset": "p6", "nvenc_cq_h264": "18",
        "nvenc_cq_h265": "22", "qsv_gq_h264": "18", "qsv_gq_h265": "22",
    },
    "fast": {
        "x_preset": "ultrafast", "x264_crf": "23", "x265_crf": "28",
        "vt_q": "55", "nvenc_preset": "p1", "nvenc_cq_h264": "24",
        "nvenc_cq_h265": "28", "qsv_gq_h264": "26", "qsv_gq_h265": "30",
    },
}

# Match one ``ffmpeg -encoders`` row: six flag columns then the encoder name.
# The first flag column is the media type (``V`` = video). Legend lines like
# `` V..... = Video`` are skipped because ``=`` is not a name character.
_ENCODER_LINE_RE = re.compile(r"^\s*([A-Z.]{6})\s+([A-Za-z0-9_]+)")


def _normalize_codec(name: str | None) -> str:
    return _CODEC_ALIASES.get(str(name or "").strip().lower(), "h264")


@lru_cache(maxsize=1)
def detect_supported_encoders() -> frozenset[str]:
    """Return the set of video encoder names this ffmpeg build can use.

    Runs ``ffmpeg -encoders`` once and caches the parsed result for the life
    of the process. Returns an empty set (i.e. "CPU only") if ffmpeg is
    missing or the probe fails — never raises.
    """
    try:
        proc = subprocess.run(
            [ffmpeg_path(), "-hide_banner", "-encoders"],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("ffmpeg -encoders probe failed (%s); assuming CPU-only encoding", exc)
        return frozenset()
    encoders: set[str] = set()
    for line in proc.stdout.splitlines():
        m = _ENCODER_LINE_RE.match(line)
        if m and m.group(1)[0] == "V":
            encoders.add(m.group(2))
    return frozenset(encoders)


def _rate_control(kind: str, codec: str, p: dict[str, str]) -> list[str]:
    """Per-encoder rate-control flags for a given preset.

    Hardware encoders reject x264-style ``-crf``/``-preset veryfast``; each
    family gets the flags it actually understands.
    """
    if kind == "videotoolbox":
        # VideoToolbox has no -crf/-preset; -q:v is its constant-quality knob.
        return ["-q:v", p["vt_q"]]
    if kind == "nvenc":
        cq = p["nvenc_cq_h264"] if codec == "h264" else p["nvenc_cq_h265"]
        return ["-preset", p["nvenc_preset"], "-cq", cq]
    if kind == "qsv":
        gq = p["qsv_gq_h264"] if codec == "h264" else p["qsv_gq_h265"]
        return ["-global_quality", gq]
    # cpu (libx264 / libx265)
    crf = p["x264_crf"] if codec == "h264" else p["x265_crf"]
    return ["-preset", p["x_preset"], "-crf", crf]


def _cpu_args(codec: str, p: dict[str, str]) -> list[str]:
    return ["-c:v", _CPU_ENCODER[codec], *_rate_control("cpu", codec, p)]


def cpu_video_encoder_args(codec_name: str = "h264",
                           quality_preset: str = "default") -> list[str]:
    """The guaranteed-available software encoder args (``libx264``/``libx265``).

    Callers that run ffmpeg outside ``run_ffmpeg_with_progress`` (and therefore
    don't get the automatic double-try) use this as their own CPU fallback.
    """
    codec = _normalize_codec(codec_name)
    preset = _PRESETS.get(str(quality_preset).lower(), _PRESETS["default"])
    return _cpu_args(codec, preset)


def get_video_encoder_args(codec_name: str = "h264",
                           quality_preset: str = "default") -> list[str]:
    """Return ``-c:v <encoder> …`` for the best encoder available right now.

    ``codec_name`` is a logical codec (``h264``/``h265``, plus aliases like
    ``hevc``/``avc``). ``quality_preset`` is one of ``default``/``high``/``fast``.

    Resolution order:
      1. ``GEMIA_VIDEO_ENCODER=cpu`` (or any explicit non-auto value) → libx264/5.
      2. Otherwise the first hardware encoder from ``_GPU_FAMILIES`` that
         ``detect_supported_encoders`` reports, in preference order.
      3. Otherwise the CPU encoder.

    On a CPU-only host with the default preset this returns exactly the old
    ``["-c:v", "libx264", "-preset", "veryfast", "-crf", "20"]``.
    """
    codec = _normalize_codec(codec_name)
    preset = _PRESETS.get(str(quality_preset).lower(), _PRESETS["default"])

    forced = os.environ.get("GEMIA_VIDEO_ENCODER", "auto").strip().lower()
    if forced not in ("", "auto", "gpu"):
        # Any explicit value ("cpu", "libx264", "software", "0"…) forces CPU.
        # Deterministic for tests/CI and a clean operator kill-switch.
        return _cpu_args(codec, preset)

    available = detect_supported_encoders()
    for family in _GPU_FAMILIES:
        encoder = _GPU_ENCODER[(codec, family)]
        if encoder in available:
            return ["-c:v", encoder, *_rate_control(family, codec, preset)]
    return _cpu_args(codec, preset)


def _first_encoder(cmd: list[str]) -> str:
    """The encoder token after ``-c:v`` (for logging); ``?`` if absent."""
    for i in range(len(cmd) - 1):
        if cmd[i] == "-c:v":
            return cmd[i + 1]
    return "?"


def downgrade_video_encoder(cmd: list[str]) -> tuple[list[str], bool]:
    """Rewrite a GPU-encoder ffmpeg command to its CPU (libx264/5) equivalent.

    Locates the first ``-c:v <gpu-encoder>``, swaps it for the matching
    software encoder, injects ``-preset veryfast -crf <n>`` (or just the preset
    in bitrate mode, where ``-crf`` would conflict with ``-b:v``), and strips
    the GPU-only rate-control flags (``-q:v``/``-cq``/``-global_quality`` and
    the encoder's ``-preset``).

    Returns ``(new_cmd, changed)``. ``changed`` is False (and ``cmd`` is copied
    verbatim) when there is no GPU encoder to downgrade — the signal the
    double-try wrapper uses to decide whether a retry could possibly help.
    """
    enc_idx: int | None = None
    for i in range(len(cmd) - 1):
        if cmd[i] == "-c:v" and cmd[i + 1] in _GPU_TO_CPU_ENCODER:
            enc_idx = i + 1
            break
    if enc_idx is None:
        return list(cmd), False

    cpu_enc = _GPU_TO_CPU_ENCODER[cmd[enc_idx]]
    codec = "h265" if cpu_enc == "libx265" else "h264"
    bitrate_mode = "-b:v" in cmd
    p = _PRESETS["default"]

    out: list[str] = []
    i = 0
    n = len(cmd)
    while i < n:
        if i == enc_idx:
            out.append(cpu_enc)
            out += ["-preset", p["x_preset"]]
            if not bitrate_mode:
                out += ["-crf", p["x264_crf"] if codec == "h264" else p["x265_crf"]]
            i += 1
            continue
        tok = cmd[i]
        if tok in _GPU_RATE_FLAGS:      # drop GPU-only flag + its value
            i += 2
            continue
        if tok == "-preset":            # CPU preset already injected above
            i += 2
            continue
        out.append(tok)
        i += 1
    return out, True


# --------------------------------------------------------------------------- #
# ffmpeg execution
# --------------------------------------------------------------------------- #

async def _run_ffmpeg_once(
    cmd: list[str],
    *,
    total_seconds: float,
    progress: ProgressCallback,
) -> None:
    """Run ``ffmpeg`` once, forwarding real progress. Raises RuntimeError on
    non-zero exit with the stderr tail in the message."""
    full = list(cmd)
    # Accept both bare "ffmpeg" and a resolved absolute path to the binary.
    if not (full[0] == "ffmpeg" or Path(full[0]).stem == "ffmpeg"):
        raise ValueError("cmd[0] must be 'ffmpeg' or a path to ffmpeg")
    if full[0] == "ffmpeg":
        full[0] = ffmpeg_path()
    if "-loglevel" not in full:
        full[1:1] = ["-loglevel", "error"]
    if "-progress" not in full:
        full[-1:-1] = ["-progress", "pipe:1"]

    loop = asyncio.get_running_loop()
    proc = await loop.run_in_executor(
        None,
        lambda: subprocess.Popen(
            full,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        ),
    )

    stderr_buf: list[str] = []

    async def drain_stderr() -> None:
        assert proc.stderr is not None
        while True:
            line = await loop.run_in_executor(None, proc.stderr.readline)
            if not line:
                return
            stderr_buf.append(line)

    stderr_task = asyncio.create_task(drain_stderr())

    try:
        assert proc.stdout is not None
        while True:
            line = await loop.run_in_executor(None, proc.stdout.readline)
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            if line.startswith("out_time_us="):
                try:
                    us = int(line.split("=", 1)[1])
                except (ValueError, IndexError):
                    continue
                secs = us / 1_000_000.0
                if total_seconds and total_seconds > 0:
                    pct = max(0.0, min(100.0, 100.0 * secs / total_seconds))
                    progress(ProgressUpdate(percent=pct, message=f"{secs:.1f}s / {total_seconds:.1f}s"))
                else:
                    progress(ProgressUpdate(percent=None, message=f"{secs:.1f}s processed"))
            elif line == "progress=end":
                if total_seconds and total_seconds > 0:
                    progress(ProgressUpdate(percent=100.0, message="finalizing"))
                break
    except asyncio.CancelledError:
        proc.kill()
        raise
    finally:
        if proc.poll() is None and asyncio.current_task() is not None and asyncio.current_task().cancelled():
            proc.kill()

    rc = await loop.run_in_executor(None, proc.wait)
    await stderr_task
    if rc != 0:
        tail = "".join(stderr_buf)[-1200:]
        raise RuntimeError(f"ffmpeg failed (exit {rc}): {tail}")


async def run_ffmpeg_with_progress(
    cmd: list[str],
    *,
    total_seconds: float,
    progress: ProgressCallback,
) -> None:
    """Run ``ffmpeg`` and forward real progress to ``progress``.

    ``cmd`` must start with ``ffmpeg`` and end with the output path.
    The helper appends ``-loglevel error`` and ``-progress pipe:1``.

    Double-try fallback: if ``cmd`` uses a GPU encoder and the run raises
    ``RuntimeError`` (a driver/format/colour-space rejection), it is rewritten
    to the CPU encoder and run once more. A CPU-only command re-raises
    immediately — a retry with identical args could not help. Cancellation is
    never retried.
    """
    try:
        await _run_ffmpeg_once(cmd, total_seconds=total_seconds, progress=progress)
        return
    except RuntimeError as exc:
        cpu_cmd, downgraded = downgrade_video_encoder(cmd)
        if not downgraded:
            raise
        logger.warning(
            "GPU encode with %s failed, falling back to CPU %s and retrying once: %s",
            _first_encoder(cmd), _first_encoder(cpu_cmd), str(exc)[:300],
        )
    # Retry outside the except block so a CPU failure reports cleanly, not as
    # "during handling of the above exception".
    await _run_ffmpeg_once(cpu_cmd, total_seconds=total_seconds, progress=progress)


__all__ = [
    "ffprobe_metadata",
    "ffprobe_duration",
    "video_stream",
    "audio_stream",
    "short_summary",
    "detect_supported_encoders",
    "get_video_encoder_args",
    "cpu_video_encoder_args",
    "downgrade_video_encoder",
    "run_ffmpeg_with_progress",
]
