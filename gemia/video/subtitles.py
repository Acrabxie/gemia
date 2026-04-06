"""Subtitle generation: transcribe audio to SRT/VTT, burn or mux into video."""
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path


def _run(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(
            f"Command failed: {' '.join(cmd)}\nSTDERR:\n{proc.stderr}"
        )


# ── SRT helpers ──────────────────────────────────────────────────────────────

def _secs_to_srt_ts(s: float) -> str:
    """Convert fractional seconds to SRT timestamp ``HH:MM:SS,mmm``."""
    h = int(s // 3600)
    m = int((s % 3600) // 60)
    sec = int(s % 60)
    ms = int(round((s - int(s)) * 1000))
    return f"{h:02d}:{m:02d}:{sec:02d},{ms:03d}"


def make_srt(entries: list[dict], output_path: str) -> str:
    """Write an SRT subtitle file from a list of cue entries.

    Args:
        entries: List of dicts with keys ``start`` (float seconds),
            ``end`` (float seconds), and ``text`` (str).
        output_path: Destination ``.srt`` file path.

    Returns:
        The *output_path*.

    Example::

        make_srt([
            {"start": 0.0, "end": 2.5, "text": "Hello, world!"},
            {"start": 3.0, "end": 5.0, "text": "This is Gemia."},
        ], "out/subs.srt")
    """
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    for i, entry in enumerate(entries, 1):
        lines.append(str(i))
        lines.append(f"{_secs_to_srt_ts(entry['start'])} --> {_secs_to_srt_ts(entry['end'])}")
        lines.append(entry["text"].strip())
        lines.append("")
    Path(output_path).write_text("\n".join(lines), encoding="utf-8")
    return output_path


def make_vtt(entries: list[dict], output_path: str) -> str:
    """Write a WebVTT subtitle file from a list of cue entries.

    Args:
        entries: List of dicts with keys ``start`` (float seconds),
            ``end`` (float seconds), and ``text`` (str).
        output_path: Destination ``.vtt`` file path.

    Returns:
        The *output_path*.
    """
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    lines = ["WEBVTT", ""]
    for entry in entries:
        start = _secs_to_srt_ts(entry["start"]).replace(",", ".")
        end = _secs_to_srt_ts(entry["end"]).replace(",", ".")
        lines.append(f"{start} --> {end}")
        lines.append(entry["text"].strip())
        lines.append("")
    Path(output_path).write_text("\n".join(lines), encoding="utf-8")
    return output_path


# ── Whisper transcription ─────────────────────────────────────────────────────

def transcribe_to_srt(
    input_path: str,
    output_path: str,
    *,
    language: str = "auto",
    model: str = "base",
) -> str:
    """Transcribe audio/video speech to an SRT subtitle file using Whisper.

    Requires the ``openai-whisper`` package (``pip install openai-whisper``).

    Args:
        input_path: Source audio or video file.
        output_path: Destination ``.srt`` file.
        language: ISO-639-1 language code (e.g. ``'en'``, ``'zh'``),
            or ``'auto'`` to let Whisper detect it.
        model: Whisper model size: ``'tiny'``, ``'base'``, ``'small'``,
            ``'medium'``, ``'large'``.

    Returns:
        The *output_path*.
    """
    try:
        import whisper  # type: ignore
    except ImportError:
        raise ImportError(
            "openai-whisper is required: pip install openai-whisper"
        )

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    m = whisper.load_model(model)
    kwargs: dict = {"word_timestamps": False}
    if language != "auto":
        kwargs["language"] = language
    result = m.transcribe(input_path, **kwargs)

    entries = [
        {"start": seg["start"], "end": seg["end"], "text": seg["text"]}
        for seg in result["segments"]
    ]
    return make_srt(entries, output_path)


def transcribe_to_vtt(
    input_path: str,
    output_path: str,
    *,
    language: str = "auto",
    model: str = "base",
) -> str:
    """Transcribe audio/video speech to a WebVTT subtitle file using Whisper.

    Requires the ``openai-whisper`` package (``pip install openai-whisper``).

    Args:
        input_path: Source audio or video file.
        output_path: Destination ``.vtt`` file.
        language: ISO-639-1 language code or ``'auto'``.
        model: Whisper model size.

    Returns:
        The *output_path*.
    """
    try:
        import whisper  # type: ignore
    except ImportError:
        raise ImportError(
            "openai-whisper is required: pip install openai-whisper"
        )

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    m = whisper.load_model(model)
    kwargs: dict = {"word_timestamps": False}
    if language != "auto":
        kwargs["language"] = language
    result = m.transcribe(input_path, **kwargs)

    entries = [
        {"start": seg["start"], "end": seg["end"], "text": seg["text"]}
        for seg in result["segments"]
    ]
    return make_vtt(entries, output_path)


# ── Burn / mux subtitles into video ──────────────────────────────────────────

def burn_subtitles(
    input_path: str,
    subtitle_path: str,
    output_path: str,
    *,
    font_size: int = 24,
    font_color: str = "white",
    outline_color: str = "black",
    outline_width: int = 2,
    margin_v: int = 30,
) -> str:
    """Burn (hard-code) subtitles into a video using ffmpeg subtitles filter.

    Args:
        input_path: Source video file.
        subtitle_path: ``.srt`` or ``.ass`` subtitle file.
        output_path: Destination video file.
        font_size: Font size in points.
        font_color: Subtitle text colour (CSS name or ``&HAABBGGRR``).
        outline_color: Outline/shadow colour.
        outline_width: Outline thickness in pixels.
        margin_v: Bottom margin in pixels.

    Returns:
        The *output_path*.
    """
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    # Escape colons in path for ffmpeg filter syntax (Windows-safe)
    sub_path_esc = str(Path(subtitle_path).resolve()).replace("\\", "/").replace(":", "\\:")
    force_style = (
        f"FontSize={font_size},"
        f"PrimaryColour=&H00{_css_to_bgr(font_color)},"
        f"OutlineColour=&H00{_css_to_bgr(outline_color)},"
        f"Outline={outline_width},"
        f"MarginV={margin_v}"
    )
    vf = f"subtitles='{sub_path_esc}':force_style='{force_style}'"
    _run([
        "ffmpeg", "-y", "-i", input_path,
        "-vf", vf,
        "-c:v", "libx264", "-c:a", "aac",
        output_path,
    ])
    return output_path


def add_subtitle_track(
    input_path: str,
    subtitle_path: str,
    output_path: str,
    *,
    language: str = "eng",
    title: str = "",
) -> str:
    """Mux a subtitle file into a video as a selectable soft subtitle track.

    The subtitles are stored in the container (MKV/MP4) and can be toggled
    by the viewer — they are *not* burned into the picture.

    Args:
        input_path: Source video file.
        subtitle_path: ``.srt`` subtitle file.
        output_path: Destination file. Use ``.mkv`` for full compatibility.
        language: BCP-47 / ISO-639-2 language tag for the subtitle track.
        title: Human-readable track name shown in players.

    Returns:
        The *output_path*.
    """
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-y",
        "-i", input_path,
        "-i", subtitle_path,
        "-c:v", "copy", "-c:a", "copy", "-c:s", "mov_text",
        "-metadata:s:s:0", f"language={language}",
    ]
    if title:
        cmd += ["-metadata:s:s:0", f"title={title}"]
    cmd += ["-map", "0:v", "-map", "0:a", "-map", "1:0", output_path]
    _run(cmd)
    return output_path


def extract_subtitle_track(
    input_path: str,
    output_path: str,
    *,
    stream_index: int = 0,
) -> str:
    """Extract an embedded subtitle track from a video to an SRT file.

    Args:
        input_path: Source video/MKV with embedded subtitles.
        output_path: Destination ``.srt`` file.
        stream_index: Subtitle stream index (0 = first subtitle track).

    Returns:
        The *output_path*.
    """
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    _run([
        "ffmpeg", "-y", "-i", input_path,
        "-map", f"0:s:{stream_index}",
        output_path,
    ])
    return output_path


# ── Internal colour helpers ───────────────────────────────────────────────────

_NAMED_COLORS: dict[str, str] = {
    "white": "FFFFFF", "black": "000000", "red": "0000FF",
    "green": "00FF00", "blue": "FF0000", "yellow": "00FFFF",
    "cyan": "FFFF00", "magenta": "FF00FF",
}


def _css_to_bgr(color: str) -> str:
    """Convert a CSS colour name or ``#RRGGBB`` hex to ``BBGGRR`` for ASS."""
    color = color.strip().lower()
    if color in _NAMED_COLORS:
        hex6 = _NAMED_COLORS[color]
    elif color.startswith("#"):
        hex6 = color.lstrip("#").upper()
        if len(hex6) == 3:
            hex6 = "".join(c * 2 for c in hex6)
    else:
        return "FFFFFF"
    r, g, b = hex6[0:2], hex6[2:4], hex6[4:6]
    return f"{b}{g}{r}"
