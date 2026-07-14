"""Timeline operations: cut, concat, speed, reverse."""
from __future__ import annotations

import math
import subprocess
import uuid
from pathlib import Path


def _run(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(
            f"ffmpeg failed: {' '.join(cmd)}\nSTDERR:\n{proc.stderr}"
        )


def cut(input_path: str, output_path: str, *,
        start_sec: float, end_sec: float) -> str:
    """Cut a segment from a video.

    Uses stream copy when possible for speed.

    Args:
        input_path: Source video.
        output_path: Destination.
        start_sec: Start time in seconds.
        end_sec: End time in seconds.

    Returns:
        The *output_path*.
    """
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    _run([
        "ffmpeg", "-y",
        "-ss", str(start_sec), "-to", str(end_sec),
        "-i", input_path,
        "-c:v", "libx264", "-c:a", "aac",
        output_path,
    ])
    return output_path


def concat(paths: list[str], output_path: str) -> str:
    """Concatenate multiple video files in order.

    Args:
        paths: Ordered list of input video paths.
        output_path: Destination.

    Returns:
        The *output_path*.
    """
    if not paths:
        raise ValueError("At least one input path is required.")
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    list_file = Path(output_path).parent / f".concat_{uuid.uuid4().hex[:8]}.txt"
    list_file.write_text(
        "\n".join(f"file '{Path(p).resolve()}'" for p in paths) + "\n"
    )
    try:
        _run([
            "ffmpeg", "-y", "-f", "concat", "-safe", "0",
            "-i", str(list_file),
            "-c:v", "libx264", "-c:a", "aac",
            output_path,
        ])
    finally:
        list_file.unlink(missing_ok=True)
    return output_path


def speed(input_path: str, output_path: str, *, factor: float) -> str:
    """Change playback speed.

    Args:
        input_path: Source video.
        output_path: Destination.
        factor: Speed multiplier. >1 = faster, <1 = slower.

    Returns:
        The *output_path*.
    """
    if factor <= 0:
        raise ValueError("factor must be > 0.")
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    vf = f"setpts={1/factor}*PTS"
    cmd = [
        "ffmpeg", "-y", "-i", input_path,
        "-filter:v", vf,
    ]
    # atempo only supports [0.5, 100.0]
    if 0.5 <= factor <= 100.0:
        cmd += ["-filter:a", f"atempo={factor}"]
    else:
        cmd += ["-an"]
    cmd += ["-c:v", "libx264", "-c:a", "aac", output_path]
    _run(cmd)
    return output_path


def rotate_video(input_path: str, output_path: str, *, degrees: int = 90) -> str:
    """Rotate a video by a multiple of 90 degrees.

    Args:
        input_path: Source video.
        output_path: Destination.
        degrees: Rotation angle. Must be 90, 180, or 270.
            90 = clockwise 90°, 270 = counter-clockwise 90°.

    Returns:
        The *output_path*.
    """
    if degrees not in (90, 180, 270):
        raise ValueError("degrees must be 90, 180, or 270.")
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    # FFmpeg transpose: 1=CW90, 2=CCW90; for 180 chain two transpose=1
    if degrees == 90:
        vf = "transpose=1"
    elif degrees == 270:
        vf = "transpose=2"
    else:  # 180
        vf = "transpose=1,transpose=1"
    _run([
        "ffmpeg", "-y", "-i", input_path,
        "-vf", vf,
        "-c:v", "libx264", "-c:a", "aac",
        output_path,
    ])
    return output_path


def flip_video(input_path: str, output_path: str, *, direction: str = "horizontal") -> str:
    """Flip (mirror) a video horizontally or vertically.

    Args:
        input_path: Source video.
        output_path: Destination.
        direction: ``'horizontal'`` (hflip) or ``'vertical'`` (vflip).

    Returns:
        The *output_path*.
    """
    if direction not in ("horizontal", "vertical"):
        raise ValueError("direction must be 'horizontal' or 'vertical'.")
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    vf = "hflip" if direction == "horizontal" else "vflip"
    _run([
        "ffmpeg", "-y", "-i", input_path,
        "-vf", vf,
        "-c:v", "libx264", "-c:a", "aac",
        output_path,
    ])
    return output_path


def reverse(input_path: str, output_path: str) -> str:
    """Reverse a video (and its audio).

    Args:
        input_path: Source video.
        output_path: Destination.

    Returns:
        The *output_path*.
    """
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    _run([
        "ffmpeg", "-y", "-i", input_path,
        "-vf", "reverse", "-af", "areverse",
        "-c:v", "libx264", "-c:a", "aac",
        output_path,
    ])
    return output_path


def ripple_trim(input_path: str, output_path: str, *, edge: str, delta_sec: float) -> str:
    """Trim time from the start or end of a video.

    Args:
        input_path: Source video.
        output_path: Destination.
        edge: ``'start'`` or ``'end'``.
        delta_sec: Seconds to trim.

    Returns:
        The *output_path*.
    """
    if edge not in ("start", "end"):
        raise ValueError("edge must be 'start' or 'end'.")
    if delta_sec < 0:
        raise ValueError("delta_sec must be >= 0.")
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
        raise RuntimeError(
            f"ffprobe failed: {input_path}\nSTDERR:\n{probe.stderr}"
        )
    duration = float(probe.stdout.strip())
    if delta_sec > duration:
        raise ValueError("delta_sec must be <= input duration.")

    if edge == "start":
        start_sec = delta_sec
        end_sec = duration
    else:
        start_sec = 0.0
        end_sec = duration - delta_sec

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    _run([
        "ffmpeg", "-y",
        "-ss", str(start_sec), "-to", str(end_sec),
        "-i", input_path,
        "-c:v", "libx264", "-c:a", "aac",
        output_path,
    ])
    return output_path


def _probe_duration(input_path: str) -> float:
    """Return media duration in seconds via ffprobe."""
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
        raise RuntimeError(
            f"ffprobe failed: {input_path}\nSTDERR:\n{probe.stderr}"
        )
    return float(probe.stdout.strip())


def _probe_frame_rate(input_path: str) -> float:
    """Return the primary video stream frame rate in fps via ffprobe."""
    probe = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=avg_frame_rate",
            "-of", "default=noprint_wrappers=1:nokey=1",
            input_path,
        ],
        capture_output=True,
        text=True,
    )
    if probe.returncode != 0:
        raise RuntimeError(
            f"ffprobe failed: {input_path}\nSTDERR:\n{probe.stderr}"
        )
    raw = probe.stdout.strip()
    if not raw or raw == "0/0":
        raise ValueError(f"Could not determine frame rate for {input_path}.")
    num, denom = raw.split("/", 1)
    fps = float(num) / float(denom)
    if fps <= 0:
        raise ValueError(f"Invalid frame rate for {input_path}: {raw}")
    return fps


def _has_audio_stream(input_path: str) -> bool:
    """Return whether the input contains at least one audio stream."""
    probe = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-select_streams", "a",
            "-show_entries", "stream=index",
            "-of", "csv=p=0",
            input_path,
        ],
        capture_output=True,
        text=True,
    )
    if probe.returncode != 0:
        raise RuntimeError(
            f"ffprobe failed: {input_path}\nSTDERR:\n{probe.stderr}"
        )
    return bool(probe.stdout.strip())


def roll_edit(
    input_a: str,
    input_b: str,
    output_a: str,
    output_b: str,
    *,
    delta_sec: float,
) -> tuple[str, str]:
    """Roll the seam between two adjacent clips without changing total duration.

    Because the inputs are already independent media files, the function cannot
    reveal unavailable source frames beyond either file boundary. To preserve
    total duration, the shortened side is trimmed while the opposite side is
    extended by cloning its boundary frame and padding audio with silence.
    """
    duration_a = _probe_duration(input_a)
    duration_b = _probe_duration(input_b)

    if delta_sec >= 0:
        if delta_sec >= duration_a:
            raise ValueError("delta_sec trims clip_a to zero or negative duration.")
    else:
        shift = -delta_sec
        if shift >= duration_b:
            raise ValueError("delta_sec trims clip_b to zero or negative duration.")

    Path(output_a).parent.mkdir(parents=True, exist_ok=True)
    Path(output_b).parent.mkdir(parents=True, exist_ok=True)

    if delta_sec >= 0:
        _run([
            "ffmpeg", "-y",
            "-ss", "0.0", "-to", str(duration_a - delta_sec),
            "-i", input_a,
            "-c:v", "libx264", "-c:a", "aac",
            output_a,
        ])
        _run([
            "ffmpeg", "-y",
            "-i", input_b,
            "-vf", f"tpad=start_duration={delta_sec}:start_mode=clone",
            "-af", f"adelay={int(round(delta_sec * 1000))}:all=1",
            "-c:v", "libx264", "-c:a", "aac",
            output_b,
        ])
    else:
        shift = -delta_sec
        _run([
            "ffmpeg", "-y",
            "-i", input_a,
            "-vf", f"tpad=stop_duration={shift}:stop_mode=clone",
            "-af", f"apad=pad_dur={shift}",
            "-c:v", "libx264", "-c:a", "aac",
            output_a,
        ])
        _run([
            "ffmpeg", "-y",
            "-ss", str(shift), "-to", str(duration_b),
            "-i", input_b,
            "-c:v", "libx264", "-c:a", "aac",
            output_b,
        ])
    return output_a, output_b


def slip_edit(
    input_path: str,
    output_path: str,
    *,
    offset_sec: float,
    duration_sec: float,
) -> str:
    """Slip a clip's content while keeping timeline position and duration fixed.

    Args:
        input_path: Source media.
        output_path: Destination media.
        offset_sec: Source in-point in seconds.
        duration_sec: Output duration in seconds.

    Returns:
        The *output_path*.
    """
    if offset_sec < 0:
        raise ValueError("offset_sec must be >= 0.")
    if duration_sec <= 0:
        raise ValueError("duration_sec must be > 0.")

    source_duration = _probe_duration(input_path)
    end_sec = offset_sec + duration_sec
    if end_sec > source_duration:
        raise ValueError(
            "offset_sec + duration_sec must be <= input duration."
        )

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    _run([
        "ffmpeg", "-y",
        "-ss", str(offset_sec),
        "-i", input_path,
        "-t", str(duration_sec),
        "-c:v", "libx264", "-c:a", "aac",
        output_path,
    ])
    return output_path


def slide_edit(input_path: str, output_path: str, *, delta_sec: float) -> str:
    """Slide a clip on the timeline by padding or trimming its start.

    Simplified single-file primitive semantics:
    - ``delta_sec > 0``: prepend black frames and silence.
    - ``delta_sec < 0``: remove ``abs(delta_sec)`` seconds from the start.

    Args:
        input_path: Source media.
        output_path: Destination media.
        delta_sec: Timeline shift in seconds.

    Returns:
        The *output_path*.
    """
    duration = _probe_duration(input_path)
    if delta_sec < 0 and abs(delta_sec) >= duration:
        raise ValueError("abs(delta_sec) must be < input duration.")

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    if delta_sec == 0:
        _run([
            "ffmpeg", "-y",
            "-i", input_path,
            "-c:v", "libx264", "-c:a", "aac",
            output_path,
        ])
        return output_path

    if delta_sec > 0:
        _run([
            "ffmpeg", "-y",
            "-i", input_path,
            "-vf", f"tpad=start_duration={delta_sec}:start_mode=add:color=black",
            "-af", f"adelay={int(round(delta_sec * 1000))}:all=1",
            "-c:v", "libx264", "-c:a", "aac",
            output_path,
        ])
        return output_path

    shift = abs(delta_sec)
    target_duration = duration - shift
    fps = _probe_frame_rate(input_path)
    stop_pad = target_duration - (math.floor((target_duration * fps) + 1e-9) / fps)
    vf = f"trim=start={shift},setpts=PTS-STARTPTS"
    if stop_pad > 1e-6:
        vf += f",tpad=stop_duration={stop_pad}:stop_mode=clone"
    _run([
        "ffmpeg", "-y",
        "-i", input_path,
        "-vf", vf,
        "-af", f"atrim=start={shift},asetpts=PTS-STARTPTS",
        "-c:v", "libx264", "-c:a", "aac",
        output_path,
    ])
    return output_path


def freeze_frame(
    input_path: str,
    output_path: str,
    *,
    timestamp_sec: float,
    freeze_duration_sec: float,
) -> str:
    """Freeze the last frame at ``timestamp_sec`` for ``freeze_duration_sec`` seconds."""
    if timestamp_sec < 0:
        raise ValueError("timestamp_sec must be >= 0.")
    if freeze_duration_sec < 0:
        raise ValueError("freeze_duration_sec must be >= 0.")

    duration = _probe_duration(input_path)
    if timestamp_sec > duration:
        raise ValueError("timestamp_sec must be <= input duration.")

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    _run([
        "ffmpeg", "-y",
        "-i", input_path,
        "-vf",
        (
            f"trim=start=0:end={timestamp_sec},setpts=PTS-STARTPTS,"
            f"tpad=stop_mode=clone:stop_duration={freeze_duration_sec}"
        ),
        "-af",
        (
            f"atrim=start=0:end={timestamp_sec},asetpts=PTS-STARTPTS,"
            f"apad=pad_dur={freeze_duration_sec}"
        ),
        "-c:v", "libx264", "-c:a", "aac",
        output_path,
    ])
    return output_path


def nest_clips(
    input_paths: list[str],
    output_path: str,
    *,
    crossfade_sec: float = 0.0,
) -> str:
    """Nest multiple clips into a single sub-sequence media file.

    With ``crossfade_sec == 0``, this is equivalent to ``concat``.
    With ``crossfade_sec > 0``, adjacent clips are joined via FFmpeg
    ``xfade`` transitions, and audio is crossfaded when all inputs contain
    audio streams.
    """
    if not input_paths:
        raise ValueError("At least one input path is required.")
    if crossfade_sec < 0:
        raise ValueError("crossfade_sec must be >= 0.")
    if crossfade_sec == 0 or len(input_paths) == 1:
        return concat(input_paths, output_path)

    durations = [_probe_duration(path) for path in input_paths]
    for i in range(len(durations) - 1):
        max_crossfade = min(durations[i], durations[i + 1])
        if crossfade_sec >= max_crossfade:
            raise ValueError(
                "crossfade_sec must be smaller than each adjacent clip pair duration."
            )

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    has_audio = all(_has_audio_stream(path) for path in input_paths)

    cmd = ["ffmpeg", "-y"]
    for path in input_paths:
        cmd += ["-i", path]

    filter_parts: list[str] = []
    video_label = "v0"
    filter_parts.append(f"[0:v]setpts=PTS-STARTPTS[{video_label}]")
    if has_audio:
        audio_label = "a0"
        filter_parts.append(f"[0:a]asetpts=PTS-STARTPTS[{audio_label}]")
    else:
        audio_label = ""

    elapsed = durations[0]
    for i in range(1, len(input_paths)):
        next_video = f"vsrc{i}"
        filter_parts.append(f"[{i}:v]setpts=PTS-STARTPTS[{next_video}]")
        next_audio = ""
        if has_audio:
            next_audio = f"asrc{i}"
            filter_parts.append(f"[{i}:a]asetpts=PTS-STARTPTS[{next_audio}]")

        video_out = f"vxf{i}"
        offset = elapsed - crossfade_sec
        filter_parts.append(
            f"[{video_label}][{next_video}]xfade=transition=fade:duration={crossfade_sec}:offset={offset}[{video_out}]"
        )
        video_label = video_out

        if has_audio:
            audio_out = f"axf{i}"
            filter_parts.append(
                f"[{audio_label}][{next_audio}]acrossfade=d={crossfade_sec}[{audio_out}]"
            )
            audio_label = audio_out

        elapsed += durations[i] - crossfade_sec

    cmd += ["-filter_complex", ";".join(filter_parts), "-map", f"[{video_label}]"]
    if has_audio:
        cmd += ["-map", f"[{audio_label}]"]
    cmd += ["-c:v", "libx264", "-c:a", "aac", output_path]
    _run(cmd)
    return output_path


# ---------------------------------------------------------------------------
# #61  timeline_from_script
# ---------------------------------------------------------------------------
def timeline_from_script(
    clips: list[str],
    script_lines: list[str],
    output_path: str,
    *,
    words_per_second: float = 2.5,
    min_clip_sec: float = 2.0,
    max_clip_sec: float = 10.0,
) -> str:
    """Assemble clips into a timed sequence driven by a text script.

    Estimates the on-screen duration of each line by counting words, then
    trims the corresponding clip to that duration and concatenates.

    Inspired by DaVinci Resolve 19 *IntelliScript* timeline generator.

    Args:
        clips: List of source video paths (one per script line, or cycled).
        script_lines: Text lines — each line maps to one clip.
        output_path: Destination video path.
        words_per_second: Reading/narration pace.  Default 2.5 wps.
        min_clip_sec: Minimum clip duration in seconds.  Default 2.0.
        max_clip_sec: Maximum clip duration in seconds.  Default 10.0.

    Returns:
        output_path
    """
    import tempfile
    if not clips or not script_lines:
        raise ValueError("clips and script_lines must not be empty")

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    tmp_dir = Path(tempfile.mkdtemp())
    trimmed: list[str] = []

    for idx, line in enumerate(script_lines):
        src = clips[idx % len(clips)]
        word_count = max(1, len(line.split()))
        duration = word_count / words_per_second
        duration = min(max(duration, min_clip_sec), max_clip_sec)

        out = str(tmp_dir / f"seg_{idx:04d}.mp4")
        proc = subprocess.run(
            [
                "ffmpeg", "-y",
                "-ss", "0",
                "-i", src,
                "-t", f"{duration:.3f}",
                "-map", "0:v:0?", "-map", "0:a?",
                "-c:v", "libx264", "-pix_fmt", "yuv420p",
                "-c:a", "aac", out,
            ],
            capture_output=True, text=True,
        )
        if proc.returncode == 0:
            trimmed.append(out)

    if not trimmed:
        raise RuntimeError("timeline_from_script: no segments could be extracted")

    from gemia.video.timeline import concat as _concat
    _concat(trimmed, output_path)
    return output_path


# ---------------------------------------------------------------------------
# speed_ramp
# ---------------------------------------------------------------------------

def speed_ramp(
    input_path: str,
    output_path: str,
    *,
    keyframes: list[dict],
) -> str:
    """Apply a variable-speed ramp driven by time keyframes.

    Each keyframe is a dict with ``"time"`` (float seconds in the *output*)
    and ``"speed"`` (float multiplier, e.g. 2.0 = 2× speed).  Between
    keyframes the speed is linearly interpolated.

    Args:
        input_path: Source video file.
        output_path: Destination video file.
        keyframes: List of ``{"time": float, "speed": float}`` dicts, sorted
            ascending by ``time``.  At least one entry is required.

    Returns:
        The *output_path*.

    Example::

        speed_ramp("clip.mp4", "ramped.mp4", keyframes=[
            {"time": 0.0,  "speed": 1.0},
            {"time": 2.0,  "speed": 0.25},  # slow-mo
            {"time": 4.0,  "speed": 4.0},   # fast-forward
        ])
    """
    if not keyframes:
        raise ValueError("keyframes must contain at least one entry")

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    if len(keyframes) == 1:
        # Constant speed — use simple setpts
        spd = float(keyframes[0]["speed"])
        vf = f"setpts={1.0/spd:.6f}*PTS"
        af = f"atempo={min(max(spd, 0.5), 100.0):.4f}"
        _run([
            "ffmpeg", "-y", "-i", input_path,
            "-vf", vf, "-af", af,
            "-c:v", "libx264", "-c:a", "aac",
            output_path,
        ])
        return output_path

    # Build a piecewise setpts expression that maps input PTS to output PTS.
    # For each segment [t_i, t_{i+1}] with average speed s_avg, the input
    # duration consumed is (t_{i+1} - t_i) * s_avg.  We accumulate input
    # time offsets to build the mapping.
    kfs = sorted(keyframes, key=lambda k: k["time"])

    # Compute cumulative input time at each output keyframe boundary
    # input_time[i] = total source seconds consumed up to output time kfs[i]["time"]
    input_times = [0.0]
    for i in range(len(kfs) - 1):
        t_out_start = float(kfs[i]["time"])
        t_out_end = float(kfs[i + 1]["time"])
        t_out_dur = t_out_end - t_out_start
        # Average speed over segment (linear interp midpoint)
        s_avg = (float(kfs[i]["speed"]) + float(kfs[i + 1]["speed"])) / 2.0
        input_times.append(input_times[-1] + t_out_dur * s_avg)

    # Build an ffmpeg expr: given input PTS (in seconds), output PTS.
    # setpts uses PTS in *input* time-base seconds; we want output seconds.
    # We invert: given input_sec, find which segment it falls in and compute output_sec.
    # Build nested if() expression.
    # For segment i: input in [input_times[i], input_times[i+1]]
    #   output = kfs[i]["time"] + (input - input_times[i]) / s_avg_i
    segments = []
    for i in range(len(kfs) - 1):
        s_avg = (float(kfs[i]["speed"]) + float(kfs[i + 1]["speed"])) / 2.0
        t_in_start = input_times[i]
        t_out_start = float(kfs[i]["time"])
        seg = (
            f"if(lte(T,{input_times[i+1]:.6f}),"
            f"{t_out_start:.6f}+(T-{t_in_start:.6f})/{s_avg:.6f}*TB"
            f"*({s_avg:.6f})"
        )
        segments.append(seg)

    # Fallback for time beyond last keyframe: use last speed
    last_spd = float(kfs[-1]["speed"])
    last_in = input_times[-1]
    last_out = float(kfs[-1]["time"])
    fallback = f"{last_out:.6f}+(T-{last_in:.6f})/{last_spd:.6f}"

    # Build closing parens
    expr = fallback
    for seg in reversed(segments):
        expr = seg + "," + expr + ")"

    # Use simpler approach: just setpts with constant average speed per segment
    # by splitting into segments, re-encoding each at the right speed, and concatenating
    import tempfile as _tf
    segments_paths: list[str] = []
    with _tf.TemporaryDirectory() as td:
        for i in range(len(kfs) - 1):
            t_out_start = float(kfs[i]["time"])
            t_out_end = float(kfs[i + 1]["time"])
            t_out_dur = max(t_out_end - t_out_start, 0.01)
            in_start = input_times[i]
            in_dur = input_times[i + 1] - input_times[i]
            s_avg = (float(kfs[i]["speed"]) + float(kfs[i + 1]["speed"])) / 2.0
            s_avg = max(s_avg, 0.01)

            seg_src = str(Path(td) / f"src_{i}.mp4")
            seg_out = str(Path(td) / f"seg_{i}.mp4")

            # Extract source segment
            proc = subprocess.run([
                "ffmpeg", "-y",
                "-ss", str(in_start), "-t", str(in_dur),
                "-i", input_path,
                "-c:v", "libx264", "-c:a", "aac", seg_src,
            ], capture_output=True, text=True)
            if proc.returncode != 0:
                continue

            # Apply speed
            atempo = min(max(s_avg, 0.5), 100.0)
            proc2 = subprocess.run([
                "ffmpeg", "-y", "-i", seg_src,
                "-vf", f"setpts={1.0/s_avg:.6f}*PTS",
                "-af", f"atempo={atempo:.4f}",
                "-c:v", "libx264", "-c:a", "aac", seg_out,
            ], capture_output=True, text=True)
            if proc2.returncode == 0:
                segments_paths.append(seg_out)

        # Add tail from last keyframe to end
        tail_src = str(Path(td) / "src_tail.mp4")
        tail_out = str(Path(td) / "seg_tail.mp4")
        last_spd = float(kfs[-1]["speed"])
        proc_tail = subprocess.run([
            "ffmpeg", "-y",
            "-ss", str(input_times[-1]),
            "-i", input_path,
            "-c:v", "libx264", "-c:a", "aac", tail_src,
        ], capture_output=True, text=True)
        if proc_tail.returncode == 0:
            atempo = min(max(last_spd, 0.5), 100.0)
            proc_tail2 = subprocess.run([
                "ffmpeg", "-y", "-i", tail_src,
                "-vf", f"setpts={1.0/last_spd:.6f}*PTS",
                "-af", f"atempo={atempo:.4f}",
                "-c:v", "libx264", "-c:a", "aac", tail_out,
            ], capture_output=True, text=True)
            if proc_tail2.returncode == 0:
                segments_paths.append(tail_out)

        if not segments_paths:
            raise RuntimeError("speed_ramp: no segments could be encoded")

        # Copy segments to persistent paths before concat
        persistent = []
        for j, sp in enumerate(segments_paths):
            dst = str(Path(td) / f"final_{j}.mp4")
            import shutil as _sh
            _sh.copy2(sp, dst)
            persistent.append(dst)

        concat(persistent, output_path)
    return output_path
