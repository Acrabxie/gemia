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


def _has_drawtext() -> bool:
    """Return True if the installed ffmpeg supports the drawtext filter."""
    r = subprocess.run(["ffmpeg", "-filters"], capture_output=True, text=True)
    return "drawtext" in r.stdout or "drawtext" in r.stderr


def _pil_text_overlay(
    input_path: str,
    output_path: str,
    lines: list[tuple[str, int, tuple[int, int, int]]],  # (text, fontsize, rgb)
    *,
    bg_box: tuple[int, int, int, int] | None = None,  # (x, y, w, h) relative to frame
    bg_alpha: float = 0.7,
    text_x: int = 20,
    text_y_from_bottom: int = 80,
    line_gap: int = 6,
) -> str:
    """Render text overlays using PIL when ffmpeg drawtext is unavailable."""
    import tempfile, shutil, json
    from pathlib import Path as _Path

    # Probe video dimensions
    probe = subprocess.run(
        ["ffprobe", "-v", "error",
         "-select_streams", "v:0",
         "-show_entries", "stream=width,height,r_frame_rate",
         "-of", "json", input_path],
        capture_output=True, text=True, check=True,
    )
    info = json.loads(probe.stdout)["streams"][0]
    W, H = int(info["width"]), int(info["height"])
    fps_str = info.get("r_frame_rate", "30/1")
    fps_num, fps_den = map(int, fps_str.split("/"))
    fps = fps_num / fps_den

    # Extract frames as PNG sequence
    with tempfile.TemporaryDirectory() as td:
        frame_pattern = str(_Path(td) / "frame_%06d.png")
        subprocess.run(
            ["ffmpeg", "-y", "-i", input_path, "-vf", f"fps={fps}", frame_pattern],
            check=True, capture_output=True,
        )

        from PIL import Image, ImageDraw, ImageFont
        import os

        frames = sorted(f for f in os.listdir(td) if f.endswith(".png"))
        for fname in frames:
            fp = str(_Path(td) / fname)
            img = Image.open(fp).convert("RGBA")
            overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
            draw = ImageDraw.Draw(overlay)

            # Draw background box
            if bg_box:
                bx, by, bw, bh = bg_box
                if by < 0:
                    by = H + by
                # -1 means full width
                box_x1 = bx
                box_y1 = by
                box_x2 = (W if bw == -1 else bx + bw)
                box_y2 = by + bh
                if box_x2 > box_x1 and box_y2 > box_y1:
                    draw.rectangle(
                        [box_x1, box_y1, box_x2, box_y2],
                        fill=(0, 0, 0, int(255 * bg_alpha)),
                    )

            # Draw text lines
            y_pos = H - text_y_from_bottom
            for line_text, fontsize, rgb in lines:
                try:
                    font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", fontsize)
                except Exception:
                    font = ImageFont.load_default()
                draw.text((text_x, y_pos), line_text, font=font, fill=(*rgb, 255))
                y_pos += fontsize + line_gap

            combined = Image.alpha_composite(img, overlay).convert("RGB")
            combined.save(fp)

        # Re-encode frames back to video
        audio_tmp = str(_Path(td) / "audio.aac")
        audio_ok = subprocess.run(
            ["ffmpeg", "-y", "-i", input_path, "-vn", "-c:a", "aac", audio_tmp],
            capture_output=True,
        ).returncode == 0 and _Path(audio_tmp).exists() and _Path(audio_tmp).stat().st_size > 0
        cmd_enc = ["ffmpeg", "-y", "-framerate", str(fps), "-i", frame_pattern]
        if audio_ok:
            cmd_enc += ["-i", audio_tmp, "-c:a", "copy"]
        cmd_enc += ["-c:v", "libx264", "-pix_fmt", "yuv420p", output_path]
        subprocess.run(cmd_enc, check=True, capture_output=True)
    return output_path


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


def mux_subtitle_track(
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


# ── Auto-subtitle (transcribe + burn) ────────────────────────────────────────

def auto_subtitle(input_path: str, output_path: str, *, language: str = "en") -> str:
    """Transcribe video audio with Whisper and burn the subtitles into the video.

    Args:
        input_path: Source video file.
        output_path: Destination video file with burned-in subtitles.
        language: ISO-639-1 language code passed to Whisper (e.g. ``'en'``, ``'zh'``).

    Returns:
        The *output_path*.
    """
    try:
        import whisper  # type: ignore
    except ImportError:
        raise ImportError("openai-whisper is required: pip install openai-whisper")

    import uuid

    uid = uuid.uuid4().hex
    wav_path = f"/tmp/gemia_whisper_{uid}.wav"
    srt_path = f"/tmp/gemia_whisper_{uid}.srt"

    try:
        # Step 1: extract mono 16 kHz WAV
        _run([
            "ffmpeg", "-y", "-i", input_path,
            "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1",
            wav_path,
        ])

        # Step 2: transcribe with Whisper
        model = whisper.load_model("base")
        result = model.transcribe(wav_path, language=language)

        # Step 3: write SRT
        entries = [
            {"start": seg["start"], "end": seg["end"], "text": seg["text"]}
            for seg in result["segments"]
        ]
        make_srt(entries, srt_path)

        # Step 4: burn subtitles
        burn_subtitles(input_path, srt_path, output_path)

    finally:
        # Step 5: clean up temp files
        for tmp in (wav_path, srt_path):
            try:
                Path(tmp).unlink(missing_ok=True)
            except Exception:
                pass

    return output_path


# ── Lower-third graphic ───────────────────────────────────────────────────────

def add_lower_third(
    input_path: str,
    output_path: str,
    *,
    title: str,
    subtitle: str = "",
    style: dict | None = None,
) -> str:
    """Overlay a lower-third title/subtitle bar onto a video using ffmpeg.

    Args:
        input_path: Source video file.
        output_path: Destination video file.
        title: Primary text line.
        subtitle: Secondary (smaller) text line below the title.
        style: Optional dict with keys ``bg_color``, ``title_size``,
            ``subtitle_size``, ``color``, ``x``, ``y_offset``, ``duration``.

    Returns:
        The *output_path*.
    """
    s = style or {}
    bg_color = s.get("bg_color", "black@0.7")
    title_size = int(s.get("title_size", 40))
    subtitle_size = int(s.get("subtitle_size", 28))
    color = s.get("color", "white")
    x = int(s.get("x", 20))
    y_offset = int(s.get("y_offset", 80))
    duration = s.get("duration", None)  # float seconds or None for full video

    # Height of the box: title + subtitle (if any) + padding
    if subtitle:
        box_height = title_size + subtitle_size + 4 + 20
    else:
        box_height = title_size + 20

    # Escape single quotes for ffmpeg filter syntax
    title_esc = title.replace("'", "\\'")
    subtitle_esc = subtitle.replace("'", "\\'")

    # Build time-enable expression if duration is given
    if duration is not None:
        enable = f":enable='between(t,0,{float(duration)})'"
    else:
        enable = ""

    box_filter = (
        f"drawbox=x=0:y=ih-{box_height}:w=iw:h={box_height}"
        f":color={bg_color}:t=fill{enable}"
    )
    title_filter = (
        f"drawtext=text='{title_esc}':fontsize={title_size}"
        f":fontcolor={color}:x={x}:y=ih-{y_offset}{enable}"
    )

    vf_parts = [box_filter, title_filter]

    if subtitle:
        subtitle_filter = (
            f"drawtext=text='{subtitle_esc}':fontsize={subtitle_size}"
            f":fontcolor={color}:x={x}:y=ih-{y_offset}+{title_size}+4{enable}"
        )
        vf_parts.append(subtitle_filter)

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    if _has_drawtext():
        vf = ",".join(vf_parts)
        _run([
            "ffmpeg", "-y", "-i", input_path,
            "-vf", vf,
            "-c:v", "libx264", "-c:a", "aac",
            output_path,
        ])
    else:
        # PIL fallback when ffmpeg lacks libfreetype
        lines = [(title, title_size, (255, 255, 255))]
        if subtitle:
            lines.append((subtitle, subtitle_size, (255, 255, 255)))
        _pil_text_overlay(
            input_path, output_path,
            lines,
            bg_box=(0, -box_height, -1, box_height),
            bg_alpha=0.7,
            text_x=x,
            text_y_from_bottom=y_offset,
        )
    return output_path


# ── Animated text overlay ─────────────────────────────────────────────────────


def animated_text(
    input_path: str,
    output_path: str,
    *,
    text: str,
    animation_preset: str = "fade_in",
    duration_sec: float | None = None,
    start_sec: float = 0.0,
    style: dict | None = None,
) -> str:
    """Overlay animated text on a video using ffmpeg drawtext.

    Args:
        input_path: Source video file.
        output_path: Destination video file.
        text: The text string to display.
        animation_preset: One of ``'fade_in'``, ``'fade_out'``,
            ``'fade_in_out'``, ``'slide_up'``, ``'typewriter'``, ``'blink'``.
        duration_sec: How long to show the text (seconds). If *None*, the
            text is shown from *start_sec* to the end of the video.
        start_sec: When (in seconds) to start showing the text.
        style: Optional dict overriding default drawtext style keys
            (``fontsize``, ``fontcolor``, ``x``, ``y``).

    Returns:
        The *output_path*.
    """
    # Resolve duration via ffprobe when not given
    if duration_sec is None:
        probe = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                input_path,
            ],
            capture_output=True,
            text=True,
        )
        if probe.returncode != 0:
            raise RuntimeError(f"ffprobe failed:\n{probe.stderr}")
        total_duration = float(probe.stdout.strip())
        duration_sec = max(0.0, total_duration - start_sec)

    end_sec = start_sec + duration_sec

    # Merge caller overrides with defaults
    default_style: dict = {
        "fontsize": 60,
        "fontcolor": "white",
        "x": "(w-text_w)/2",
        "y": "(h-text_h)/2",
    }
    if style:
        default_style.update(style)

    s = start_sec
    dur = duration_sec
    target_y = default_style["y"]

    def _esc(t: str) -> str:
        """Escape text for ffmpeg drawtext (backslash, colon, single-quote)."""
        return t.replace("\\", "\\\\").replace("'", "\\'").replace(":", "\\:")

    escaped_text = _esc(text)

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    if animation_preset == "typewriter":
        # 10 sequential drawtext segments each revealing more characters
        n_steps = 10
        step_dur = dur / n_steps
        char_total = len(text)
        filters: list[str] = []
        for i in range(n_steps):
            n_chars = max(1, round(char_total * (i + 1) / n_steps))
            partial = _esc(text[:n_chars])
            seg_start = s + i * step_dur
            seg_end = s + (i + 1) * step_dur
            parts = [
                f"text='{partial}'",
                f"fontsize={default_style['fontsize']}",
                f"fontcolor={default_style['fontcolor']}",
                f"x={default_style['x']}",
                f"y={default_style['y']}",
                f"enable='between(t,{seg_start},{seg_end})'",
            ]
            filters.append("drawtext=" + ":".join(parts))
        vf = ",".join(filters)
    else:
        enable = f"between(t,{s},{end_sec})"

        if animation_preset == "fade_in":
            alpha = f"if(lt(t-{s},0.5),(t-{s})/0.5,1)"
            extra = f":alpha='{alpha}'"
            y_expr = str(default_style["y"])
        elif animation_preset == "fade_out":
            alpha = f"if(gt(t-{s},{dur}-0.5),({s}+{dur}-t)/0.5,1)"
            extra = f":alpha='{alpha}'"
            y_expr = str(default_style["y"])
        elif animation_preset == "fade_in_out":
            alpha = (
                f"if(lt(t-{s},0.5),(t-{s})/0.5,"
                f"if(gt(t-{s},{dur}-0.5),({s}+{dur}-t)/0.5,1))"
            )
            extra = f":alpha='{alpha}'"
            y_expr = str(default_style["y"])
        elif animation_preset == "slide_up":
            y_expr = (
                f"if(lt(t-{s},0.5),ih-(ih-({target_y}))*(t-{s})/0.5,{target_y})"
            )
            extra = ""
        elif animation_preset == "blink":
            alpha = f"if(mod(floor((t-{s})*2),2),1,0)"
            extra = f":alpha='{alpha}'"
            y_expr = str(default_style["y"])
        else:
            raise ValueError(f"Unknown animation_preset: {animation_preset!r}")

        vf = (
            f"drawtext="
            f"text='{escaped_text}'"
            f":fontsize={default_style['fontsize']}"
            f":fontcolor={default_style['fontcolor']}"
            f":x={default_style['x']}"
            f":y={y_expr}"
            f":enable='{enable}'"
            f"{extra}"
        )

    if _has_drawtext():
        _run([
            "ffmpeg", "-y", "-i", input_path,
            "-vf", vf,
            "-c:v", "libx264", "-c:a", "aac",
            output_path,
        ])
    else:
        # PIL fallback: render static text overlay (no per-frame animation expressions)
        fontsize = int(default_style.get("fontsize", 60))
        fc = default_style.get("fontcolor", "white")
        rgb = (255, 255, 255) if fc == "white" else (0, 0, 0)
        _pil_text_overlay(input_path, output_path, [(text, fontsize, rgb)])
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


# ── Text overlay ──────────────────────────────────────────────────────────────

def _pos_to_ffmpeg(value: "int | str") -> str:
    """Convert a position value to an ffmpeg drawtext expression string."""
    if isinstance(value, int):
        return str(value)
    if value == "center":
        return "(w-text_w)/2"
    return value  # already an ffmpeg expression


def add_text(
    input_path: str,
    output_path: str,
    *,
    text: str,
    style: dict,
    position: dict,
    keyframe_track: "list[dict] | None" = None,
) -> str:
    """Burn text onto a video using the ffmpeg ``drawtext`` filter.

    Args:
        input_path: Source video file.
        output_path: Destination video file.
        text: Text string to render (used when *keyframe_track* is ``None``).
        style: Rendering options — ``font`` (str, default ``'Arial'``),
            ``fontsize`` (int, default 48), ``fontcolor`` (str, default
            ``'white'``), ``box`` (bool, default ``False``),
            ``boxcolor`` (str, default ``'black@0.5'``),
            ``bold`` (bool, default ``False``).
        position: ``x`` and ``y`` values — integers (pixels) or strings such
            as ``'center'``, ``'(w-text_w)/2'``, or ``'h-100'``.
        keyframe_track: Optional list of ``{"time": float, "text": str}``
            dicts.  Each entry is shown from its ``time`` to the next
            entry's ``time`` (or end of video for the last entry).  When
            ``None`` the *text* argument is shown for the full duration.

    Returns:
        The *output_path*.
    """
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    font = style.get("font", "Arial")
    fontsize = int(style.get("fontsize", 48))
    fontcolor = style.get("fontcolor", "white")
    box = int(bool(style.get("box", False)))
    boxcolor = style.get("boxcolor", "black@0.5")
    bold = int(bool(style.get("bold", False)))

    x_expr = _pos_to_ffmpeg(position.get("x", 0))
    y_expr = _pos_to_ffmpeg(position.get("y", 0))

    base_opts = (
        f":font={font}"
        f":fontsize={fontsize}"
        f":fontcolor={fontcolor}"
        f":box={box}"
        f":boxcolor={boxcolor}"
        f":bold={bold}"
        f":x={x_expr}"
        f":y={y_expr}"
    )

    if keyframe_track is None:
        escaped_text = text.replace("'", "\\'").replace(":", "\\:")
        vf = f"drawtext=text='{escaped_text}'{base_opts}"
    else:
        segments: list[str] = []
        for i, kf in enumerate(keyframe_track):
            t_start = float(kf["time"])
            t_end = (
                float(keyframe_track[i + 1]["time"])
                if i + 1 < len(keyframe_track)
                else 9999999
            )
            seg_text = kf["text"].replace("'", "\\'").replace(":", "\\:")
            enable = f"between(t\\,{t_start}\\,{t_end})"
            segments.append(
                f"drawtext=text='{seg_text}'{base_opts}:enable='{enable}'"
            )
        vf = ",".join(segments)

    if _has_drawtext():
        _run([
            "ffmpeg", "-y", "-i", input_path,
            "-vf", vf,
            "-c:v", "libx264", "-c:a", "aac",
            output_path,
        ])
    else:
        # PIL fallback when ffmpeg lacks libfreetype/drawtext
        fontsize = int(style.get("fontsize", 48))
        fontcolor_str = style.get("fontcolor", "white")
        if fontcolor_str == "white":
            rgb = (255, 255, 255)
        elif fontcolor_str == "black":
            rgb = (0, 0, 0)
        else:
            rgb = (255, 255, 255)
        lines_spec = [(text, fontsize, rgb)]
        if keyframe_track:
            lines_spec = [(kf["text"], fontsize, rgb) for kf in keyframe_track[:1]]
        _pil_text_overlay(input_path, output_path, lines_spec)
    return output_path


def _parse_srt(srt_path: str) -> list[dict]:
    """Parse an SRT file into a list of {start, end, text} dicts."""
    import re
    text = Path(srt_path).read_text(encoding="utf-8")
    pattern = re.compile(
        r"\d+\s*\n"
        r"(\d{2}:\d{2}:\d{2}[,\.]\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}[,\.]\d{3})\s*\n"
        r"([\s\S]*?)(?=\n\d+\s*\n|\Z)",
        re.MULTILINE,
    )

    def ts_to_sec(ts: str) -> float:
        ts = ts.replace(",", ".")
        h, m, rest = ts.split(":")
        s, ms = rest.split(".")
        return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0

    entries = []
    for m in pattern.finditer(text):
        entries.append({
            "start": ts_to_sec(m.group(1)),
            "end": ts_to_sec(m.group(2)),
            "text": m.group(3).strip(),
        })
    return entries


def _pil_subtitle_overlay(
    input_path: str,
    output_path: str,
    entries: list[dict],
    *,
    fontsize: int = 24,
    color: tuple[int, int, int] = (255, 255, 255),
) -> str:
    """Render SRT subtitles onto each frame using PIL."""
    import tempfile, json, os
    from pathlib import Path as _Path
    from PIL import Image, ImageDraw, ImageFont

    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height,r_frame_rate",
         "-of", "json", input_path],
        capture_output=True, text=True, check=True,
    )
    info = json.loads(probe.stdout)["streams"][0]
    W, H = int(info["width"]), int(info["height"])
    fps_str = info.get("r_frame_rate", "30/1")
    fps_num, fps_den = map(int, fps_str.split("/"))
    fps = fps_num / fps_den

    with tempfile.TemporaryDirectory() as td:
        frame_pattern = str(_Path(td) / "frame_%06d.png")
        subprocess.run(
            ["ffmpeg", "-y", "-i", input_path, "-vf", f"fps={fps}", frame_pattern],
            check=True, capture_output=True,
        )

        frames = sorted(f for f in os.listdir(td) if f.endswith(".png"))
        for idx, fname in enumerate(frames):
            t = idx / fps
            active = [e for e in entries if e["start"] <= t < e["end"]]
            if not active:
                continue
            fp = str(_Path(td) / fname)
            img = Image.open(fp).convert("RGBA")
            overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
            draw = ImageDraw.Draw(overlay)
            try:
                font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", fontsize)
            except Exception:
                font = ImageFont.load_default()
            y = H - fontsize - 30
            for entry in active:
                draw.text((W // 2, y), entry["text"], font=font, fill=(*color, 255), anchor="mm")
                y += fontsize + 4
            combined = Image.alpha_composite(img, overlay).convert("RGB")
            combined.save(fp)

        audio_tmp = str(_Path(td) / "audio.aac")
        audio_ok = subprocess.run(
            ["ffmpeg", "-y", "-i", input_path, "-vn", "-c:a", "aac", audio_tmp],
            capture_output=True,
        ).returncode == 0 and _Path(audio_tmp).exists() and _Path(audio_tmp).stat().st_size > 0

        cmd = ["ffmpeg", "-y", "-framerate", str(fps), "-i", frame_pattern]
        if audio_ok:
            cmd += ["-i", audio_tmp, "-c:a", "copy"]
        cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p", output_path]
        subprocess.run(cmd, check=True, capture_output=True)
    return output_path


def add_subtitle_track(
    input_path: str,
    output_path: str,
    *,
    srt_path: str,
    style: "dict | None" = None,
) -> str:
    """Burn SRT subtitles into a video using the ffmpeg ``subtitles`` filter.

    Args:
        input_path: Source video file.
        output_path: Destination video file.
        srt_path: Path to the ``.srt`` subtitle file.
        style: Optional style overrides — ``fontsize`` (int, default 24),
            ``fontcolor`` (str, default ``'white'``), ``outline`` (int,
            default 1), ``shadow`` (int, default 0), ``alignment`` (int
            ASS alignment value, default 2 = bottom-centre).

    Returns:
        The *output_path*.
    """
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    s = style or {}
    fontsize = int(s.get("fontsize", 24))
    fontcolor = s.get("fontcolor", "white")
    outline = int(s.get("outline", 1))
    shadow = int(s.get("shadow", 0))
    alignment = int(s.get("alignment", 2))

    primary_colour = f"&H00{_css_to_bgr(fontcolor)}"
    force_style = (
        f"Fontsize={fontsize},"
        f"PrimaryColour={primary_colour},"
        f"Outline={outline},"
        f"Shadow={shadow},"
        f"Alignment={alignment}"
    )
    sub_path_esc = (
        str(Path(srt_path).resolve()).replace("\\", "/").replace(":", "\\:")
    )
    vf = f"subtitles='{sub_path_esc}':force_style='{force_style}'"
    try:
        _run([
            "ffmpeg", "-y", "-i", input_path,
            "-vf", vf,
            "-c:v", "libx264", "-c:a", "aac",
            output_path,
        ])
    except RuntimeError:
        # libass not available — PIL fallback: parse SRT, burn text per-frame
        entries = _parse_srt(srt_path)
        if fontcolor == "white":
            rgb = (255, 255, 255)
        elif fontcolor == "black":
            rgb = (0, 0, 0)
        else:
            rgb = (255, 255, 255)
        _pil_subtitle_overlay(input_path, output_path, entries, fontsize=fontsize, color=rgb)
    return output_path
