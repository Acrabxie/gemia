"""Transition primitives for video clips."""
from __future__ import annotations

from contextlib import suppress
import math
import subprocess
from pathlib import Path

import cv2
import numpy as np

from gemia.video.timeline import _has_audio_stream, _probe_duration, _run


def transition_dissolve(
    input_a: str,
    input_b: str,
    output_path: str,
    *,
    duration_sec: float,
) -> str:
    """Dissolve from ``input_a`` into ``input_b`` using FFmpeg ``xfade``."""
    if duration_sec <= 0:
        raise ValueError("duration_sec must be > 0.")

    duration_a = _probe_duration(input_a)
    duration_b = _probe_duration(input_b)
    max_duration = min(duration_a, duration_b)
    if duration_sec >= max_duration:
        raise ValueError(
            "duration_sec must be smaller than both input clip durations."
        )

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    offset = duration_a - duration_sec
    has_audio = _has_audio_stream(input_a) and _has_audio_stream(input_b)

    width, height, fps = _probe_video_metrics(input_a)

    filter_parts = [
        _normalize_video_filter(0, width=width, height=height, fps=fps),
        _normalize_video_filter(1, width=width, height=height, fps=fps),
        f"[v0][v1]xfade=transition=dissolve:duration={duration_sec}:offset={offset}[vout]",
    ]
    if has_audio:
        filter_parts.extend(
            [
                _normalize_audio_filter(0),
                _normalize_audio_filter(1),
                f"[a0][a1]acrossfade=d={duration_sec}[aout]",
            ]
        )

    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        input_a,
        "-i",
        input_b,
        "-filter_complex",
        ";".join(filter_parts),
        "-map",
        "[vout]",
    ]
    if has_audio:
        cmd += ["-map", "[aout]"]
    cmd += [
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        "-c:a",
        "aac",
        output_path,
    ]
    _run(cmd)
    return output_path


_WIPE_MAP = {
    "left": "wipeleft",
    "right": "wiperight",
    "up": "wipeup",
    "down": "wipedown",
}

_PUSH_MAP = {
    "left": "slideleft",
    "right": "slideright",
    "up": "slideup",
    "down": "slidedown",
}

_CUSTOM_MAP = {
    "circle": "circlecrop",
    "radial": "radial",
    "fade_black": "fadeblack",
    "fade_white": "fadewhite",
    "pixelize": "pixelize",
}

_SHUTTER_ALIASES = {
    "aperture",
    "aperture_blades",
    "camera_shutter",
    "iris",
    "iris_blades",
    "shutter",
    "six_blade_shutter",
}


def _xfade_transition(
    input_a: str,
    input_b: str,
    output_path: str,
    *,
    transition: str,
    duration_sec: float,
) -> str:
    """Internal helper that applies any named xfade transition."""
    if duration_sec <= 0:
        raise ValueError("duration_sec must be > 0.")

    duration_a = _probe_duration(input_a)
    duration_b = _probe_duration(input_b)
    max_duration = min(duration_a, duration_b)
    if duration_sec >= max_duration:
        raise ValueError(
            "duration_sec must be smaller than both input clip durations."
        )

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    offset = duration_a - duration_sec
    has_audio = _has_audio_stream(input_a) and _has_audio_stream(input_b)

    width, height, fps = _probe_video_metrics(input_a)

    filter_parts = [
        _normalize_video_filter(0, width=width, height=height, fps=fps),
        _normalize_video_filter(1, width=width, height=height, fps=fps),
        f"[v0][v1]xfade=transition={transition}:duration={duration_sec}:offset={offset}[vout]",
    ]
    if has_audio:
        filter_parts.extend(
            [
                _normalize_audio_filter(0),
                _normalize_audio_filter(1),
                f"[a0][a1]acrossfade=d={duration_sec}[aout]",
            ]
        )

    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        input_a,
        "-i",
        input_b,
        "-filter_complex",
        ";".join(filter_parts),
        "-map",
        "[vout]",
    ]
    if has_audio:
        cmd += ["-map", "[aout]"]
    cmd += [
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        "-c:a",
        "aac",
        output_path,
    ]
    _run(cmd)
    return output_path


def _probe_video_metrics(input_path: str) -> tuple[int, int, float]:
    probe = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height,avg_frame_rate",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            input_path,
        ],
        capture_output=True,
        text=True,
    )
    if probe.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {input_path}\nSTDERR:\n{probe.stderr}")
    values = [line.strip() for line in probe.stdout.splitlines() if line.strip()]
    if len(values) < 3:
        raise ValueError(f"Could not determine video metrics for {input_path}.")
    width = _even_dimension(int(values[0]))
    height = _even_dimension(int(values[1]))
    fps = _parse_frame_rate(values[2])
    return width, height, fps


def _parse_frame_rate(raw: str) -> float:
    if "/" in raw:
        numerator, denominator = raw.split("/", 1)
        fps = float(numerator) / float(denominator)
    else:
        fps = float(raw)
    if fps <= 0:
        raise ValueError(f"Invalid frame rate: {raw}")
    return fps


def _even_dimension(value: int) -> int:
    if value <= 0:
        raise ValueError(f"Invalid video dimension: {value}")
    even_value = value if value % 2 == 0 else value - 1
    return max(2, even_value)


def _normalize_video_filter(index: int, *, width: int, height: int, fps: float) -> str:
    fps_value = f"{fps:.6f}".rstrip("0").rstrip(".")
    return (
        f"[{index}:v]settb=AVTB,setpts=PTS-STARTPTS,"
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,"
        f"fps={fps_value},format=yuv420p[v{index}]"
    )


def _normalize_audio_filter(index: int) -> str:
    return f"[{index}:a]asetpts=PTS-STARTPTS,aresample=48000[a{index}]"


def transition_wipe(
    input_a: str,
    input_b: str,
    output_path: str,
    *,
    direction: str,
    duration_sec: float,
) -> str:
    """Wipe transition from ``input_a`` into ``input_b``.

    Args:
        direction: One of ``"left"``, ``"right"``, ``"up"``, ``"down"``.
        duration_sec: Overlap duration in seconds.
    """
    xfade = _WIPE_MAP.get(direction)
    if xfade is None:
        raise ValueError(
            f"direction must be one of {list(_WIPE_MAP)}, got {direction!r}."
        )
    return _xfade_transition(
        input_a, input_b, output_path,
        transition=xfade,
        duration_sec=duration_sec,
    )


def transition_push(
    input_a: str,
    input_b: str,
    output_path: str,
    *,
    direction: str,
    duration_sec: float,
) -> str:
    """Push/slide transition from ``input_a`` into ``input_b``.

    Args:
        direction: One of ``"left"``, ``"right"``, ``"up"``, ``"down"``.
        duration_sec: Overlap duration in seconds.
    """
    xfade = _PUSH_MAP.get(direction)
    if xfade is None:
        raise ValueError(
            f"direction must be one of {list(_PUSH_MAP)}, got {direction!r}."
        )
    return _xfade_transition(
        input_a, input_b, output_path,
        transition=xfade,
        duration_sec=duration_sec,
    )


def transition_custom(
    input_a: str,
    input_b: str,
    output_path: str,
    *,
    mask_fn: str,
    duration_sec: float,
    hold_sec: float = 0.0,
    edge_highlight: bool = False,
    highlight_strength: float = 0.65,
) -> str:
    """Custom / preset transition from ``input_a`` into ``input_b``.

    ``mask_fn`` can be one of the built-in presets
    (``"circle"``, ``"radial"``, ``"pixelize"``, ``"camera_shutter"``)
    or any raw xfade transition name supported by the installed FFmpeg build.

    Raises:
        ValueError: If ``mask_fn`` is not a known preset and not a valid
            xfade name (validated by attempting the encode).
    """
    normalized = str(mask_fn or "").strip().lower().replace("-", "_")
    if normalized in _SHUTTER_ALIASES:
        return transition_shutter(
            input_a,
            input_b,
            output_path,
            duration_sec=duration_sec,
            hold_sec=hold_sec,
            edge_highlight=edge_highlight,
            highlight_strength=highlight_strength,
        )

    xfade = _CUSTOM_MAP.get(normalized, normalized)
    known_presets = set(_CUSTOM_MAP)
    # Raw xfade names are allowed but presets that don't map are rejected.
    # We only hard-reject names that look like they were intended as preset
    # aliases but aren't in our map.
    if normalized not in known_presets and normalized not in {
        # expose a few common raw names so callers aren't surprised
        "circlecrop", "squeezeh", "squeezev", "radial", "pixelize",
        "hblur", "fadeblack", "fadewhite", "smoothleft", "smoothright",
        "smoothup", "smoothdown", "circleopen", "circleclose",
        "diagtl", "diagtr", "diagbl", "diagbr",
        "hlslice", "hrslice", "vuslice", "vdslice",
        "coverleft", "coverright", "coverup", "coverdown",
        "revealleft", "revealright", "revealup", "revealdown",
        "dissolve", "fade",
    }:
        raise ValueError(
            f"Unknown mask_fn {mask_fn!r}. "
            f"Built-in presets: {sorted(known_presets | _SHUTTER_ALIASES)}. "
            "Pass a raw xfade transition name to bypass preset lookup."
        )
    return _xfade_transition(
        input_a, input_b, output_path,
        transition=xfade,
        duration_sec=duration_sec,
    )


def transition_shutter(
    input_a: str,
    input_b: str,
    output_path: str,
    *,
    duration_sec: float = 1.0,
    blade_count: int = 6,
    hold_sec: float = 0.0,
    edge_highlight: bool = False,
    highlight_strength: float = 0.65,
) -> str:
    """Render a deterministic black multi-blade camera shutter transition.

    The transition closes straight aperture blades over the tail of ``input_a``
    for the first half of ``duration_sec`` and opens them over the head of
    ``input_b`` for the second half. Optional ``hold_sec`` keeps the aperture
    fully closed for a short beat, and ``edge_highlight`` adds a subtle metallic
    bevel cue along the blade edges. It is intended for camera shutter, iris,
    aperture blade, and similar no-text transition requests.
    """
    if duration_sec <= 0:
        raise ValueError("duration_sec must be > 0.")
    blade_count = max(3, int(blade_count))
    hold_sec = max(0.0, min(float(hold_sec), float(duration_sec) * 0.8))
    highlight_strength = max(0.0, min(float(highlight_strength), 1.0))

    duration_a = _probe_duration(input_a)
    duration_b = _probe_duration(input_b)
    max_duration = min(duration_a, duration_b)
    if duration_sec >= max_duration:
        raise ValueError(
            "duration_sec must be smaller than both input clip durations."
        )

    width, height, fps = _probe_video_metrics(input_a)
    output = Path(output_path).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    temp_video = output.with_name(f"{output.stem}.shutter-tmp{output.suffix}")

    offset = duration_a - duration_sec
    total_duration = offset + duration_b
    total_frames = max(1, int(round(total_duration * fps)))
    writer = cv2.VideoWriter(
        str(temp_video),
        cv2.VideoWriter_fourcc(*"mp4v"),
        float(fps),
        (width, height),
    )
    if not writer.isOpened():
        raise RuntimeError(f"Cannot open temporary shutter video: {temp_video}")

    cap_a = cv2.VideoCapture(str(input_a))
    cap_b = cv2.VideoCapture(str(input_b))
    if not cap_a.isOpened() or not cap_b.isOpened():
        cap_a.release()
        cap_b.release()
        writer.release()
        with suppress(FileNotFoundError):
            temp_video.unlink()
        raise FileNotFoundError("Cannot open one of the shutter transition inputs.")

    last_a: np.ndarray | None = None
    last_b: np.ndarray | None = None
    try:
        for frame_index in range(total_frames):
            timestamp = frame_index / float(fps)
            if timestamp < offset:
                frame, last_a = _read_frame_at(cap_a, timestamp, width, height, last_a)
            elif timestamp < offset + duration_sec:
                elapsed = timestamp - offset
                progress = _shutter_progress(elapsed, duration_sec=duration_sec, hold_sec=hold_sec)
                if progress < 0.5:
                    frame, last_a = _read_frame_at(cap_a, timestamp, width, height, last_a)
                else:
                    b_time = min(duration_b, timestamp - offset)
                    frame, last_b = _read_frame_at(cap_b, b_time, width, height, last_b)
                frame = _apply_shutter_blades(
                    frame,
                    progress,
                    blade_count=blade_count,
                    edge_highlight=edge_highlight,
                    highlight_strength=highlight_strength,
                )
            else:
                b_time = min(duration_b, timestamp - offset)
                frame, last_b = _read_frame_at(cap_b, b_time, width, height, last_b)
            writer.write(frame)
    finally:
        cap_a.release()
        cap_b.release()
        writer.release()

    try:
        _mux_shutter_output(
            temp_video,
            input_a,
            input_b,
            output,
            duration_sec=duration_sec,
            offset=offset,
            duration_b=duration_b,
        )
    finally:
        with suppress(FileNotFoundError):
            temp_video.unlink()
    return str(output)


def _read_frame_at(
    cap: cv2.VideoCapture,
    seconds: float,
    width: int,
    height: int,
    fallback: np.ndarray | None,
) -> tuple[np.ndarray, np.ndarray]:
    cap.set(cv2.CAP_PROP_POS_MSEC, max(0.0, float(seconds)) * 1000.0)
    ok, frame = cap.read()
    if not ok or frame is None:
        if fallback is not None:
            return fallback.copy(), fallback
        frame = np.zeros((height, width, 3), dtype=np.uint8)
    elif frame.shape[1] != width or frame.shape[0] != height:
        frame = cv2.resize(frame, (width, height), interpolation=cv2.INTER_AREA)
    return frame, frame.copy()


def _apply_shutter_blades(
    frame: np.ndarray,
    progress: float,
    *,
    blade_count: int,
    edge_highlight: bool = False,
    highlight_strength: float = 0.65,
) -> np.ndarray:
    height, width = frame.shape[:2]
    progress = max(0.0, min(float(progress), 1.0))
    phase = progress * 2.0 if progress <= 0.5 else (1.0 - progress) * 2.0
    coverage = _smoothstep(max(0.0, min(phase, 1.0)))
    if coverage <= 0.005:
        return frame

    cx = width / 2.0
    cy = height / 2.0
    max_radius = math.hypot(width, height)
    radius = max_radius * (1.0 - coverage)
    mask = np.full((height, width), 255, dtype=np.uint8)
    if radius > 1.0:
        rotation = -math.pi / 2.0 + coverage * math.radians(18.0)
        points = []
        for index in range(blade_count):
            angle = rotation + 2.0 * math.pi * index / blade_count
            points.append((int(round(cx + math.cos(angle) * radius)), int(round(cy + math.sin(angle) * radius))))
        cv2.fillConvexPoly(mask, np.array(points, dtype=np.int32), 0)

    out = frame.copy()
    blade_region = mask > 0
    if edge_highlight:
        surface = _shutter_blade_surface(height, width, coverage, highlight_strength=highlight_strength)
        out[blade_region] = surface[blade_region]
    else:
        out[blade_region] = (2, 2, 3)
    if radius > 8.0:
        edge_color = (42, 42, 42)
        highlight_value = int(round(90 + 120 * max(0.0, min(float(highlight_strength), 1.0))))
        highlight_color = (
            highlight_value,
            highlight_value,
            min(255, highlight_value + 14),
        )
        shadow_color = (16, 16, 18)
        trail_layer = np.zeros_like(out) if edge_highlight else None
        for index in range(blade_count):
            angle = -math.pi / 2.0 + 2.0 * math.pi * index / blade_count + coverage * math.radians(18.0)
            inner = (
                int(round(cx + math.cos(angle) * radius)),
                int(round(cy + math.sin(angle) * radius)),
            )
            outer = (
                int(round(cx + math.cos(angle + 0.18) * max_radius)),
                int(round(cy + math.sin(angle + 0.18) * max_radius)),
            )
            cv2.line(out, inner, outer, edge_color, 2, lineType=cv2.LINE_AA)
            if edge_highlight:
                dx = math.cos(angle + math.pi / 2.0)
                dy = math.sin(angle + math.pi / 2.0)
                hi_inner = (int(round(inner[0] + dx * 2)), int(round(inner[1] + dy * 2)))
                hi_outer = (int(round(outer[0] + dx * 2)), int(round(outer[1] + dy * 2)))
                shadow_inner = (int(round(inner[0] - dx * 2)), int(round(inner[1] - dy * 2)))
                shadow_outer = (int(round(outer[0] - dx * 2)), int(round(outer[1] - dy * 2)))
                cv2.line(out, shadow_inner, shadow_outer, shadow_color, 3, lineType=cv2.LINE_AA)
                cv2.line(out, hi_inner, hi_outer, highlight_color, 2, lineType=cv2.LINE_AA)
                if trail_layer is not None:
                    trail_angle = angle + math.radians(8.0)
                    trail_inner = (
                        int(round(cx + math.cos(trail_angle) * max(radius - 4.0, 0.0))),
                        int(round(cy + math.sin(trail_angle) * max(radius - 4.0, 0.0))),
                    )
                    trail_outer = (
                        int(round(cx + math.cos(trail_angle + 0.18) * max_radius)),
                        int(round(cy + math.sin(trail_angle + 0.18) * max_radius)),
                    )
                    cv2.line(trail_layer, trail_inner, trail_outer, highlight_color, 5, lineType=cv2.LINE_AA)
        if trail_layer is not None:
            trail_layer = cv2.GaussianBlur(trail_layer, (0, 0), sigmaX=3.2, sigmaY=3.2)
            trail_mask = blade_region[:, :, None]
            blended = cv2.addWeighted(out, 1.0, trail_layer, 0.18, 0.0)
            out = np.where(trail_mask, blended, out)
    return out


def _shutter_blade_surface(
    height: int,
    width: int,
    coverage: float,
    *,
    highlight_strength: float,
) -> np.ndarray:
    yy, xx = np.indices((height, width), dtype=np.float32)
    coverage = max(0.0, min(float(coverage), 1.0))
    highlight_strength = max(0.0, min(float(highlight_strength), 1.0))
    center_x = max(float(width - 1), 1.0) / 2.0
    center_y = max(float(height - 1), 1.0) / 2.0
    radius = np.sqrt((xx - center_x) ** 2 + (yy - center_y) ** 2)
    radial = 1.0 - np.clip(radius / max(math.hypot(width, height) * 0.5, 1.0), 0.0, 1.0)
    brushed = (np.sin(xx * 0.095 + yy * 0.018 + coverage * 5.0) + 1.0) * 0.5
    fine = (np.sin(xx * 0.43 + yy * 0.031) + 1.0) * 0.5
    base = 4.0 + brushed * 6.0 + fine * 3.0 + radial * (3.0 + 2.0 * highlight_strength)
    blue = np.clip(base * 1.10 + 1.0, 3.0, 22.0)
    green = np.clip(base * 1.02, 3.0, 20.0)
    red = np.clip(base * 0.92, 2.0, 18.0)
    return np.dstack((blue, green, red)).astype(np.uint8)


def _shutter_progress(elapsed: float, *, duration_sec: float, hold_sec: float) -> float:
    hold_sec = max(0.0, min(float(hold_sec), float(duration_sec) * 0.8))
    motion_sec = max(0.001, (float(duration_sec) - hold_sec) / 2.0)
    elapsed = max(0.0, min(float(elapsed), float(duration_sec)))
    if elapsed < motion_sec:
        return 0.5 * (elapsed / motion_sec)
    if elapsed < motion_sec + hold_sec:
        return 0.5
    return 0.5 + 0.5 * ((elapsed - motion_sec - hold_sec) / motion_sec)


def _smoothstep(value: float) -> float:
    value = max(0.0, min(float(value), 1.0))
    return value * value * (3.0 - 2.0 * value)


def _mux_shutter_output(
    temp_video: Path,
    input_a: str,
    input_b: str,
    output: Path,
    *,
    duration_sec: float,
    offset: float,
    duration_b: float,
) -> None:
    has_audio = _has_audio_stream(input_a) and _has_audio_stream(input_b)
    if not has_audio:
        _run(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(temp_video),
                "-an",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-movflags",
                "+faststart",
                str(output),
            ]
        )
        return

    audio_filter = (
        f"[1:a]atrim=0:{offset:.6f},asetpts=PTS-STARTPTS,aresample=48000[a0pre];"
        f"[1:a]atrim={offset:.6f}:{offset + duration_sec:.6f},asetpts=PTS-STARTPTS,aresample=48000[a0fade];"
        f"[2:a]atrim=0:{duration_sec:.6f},asetpts=PTS-STARTPTS,aresample=48000[a1fade];"
        f"[2:a]atrim={duration_sec:.6f}:{duration_b:.6f},asetpts=PTS-STARTPTS,aresample=48000[a1post];"
        f"[a0fade][a1fade]acrossfade=d={duration_sec:.6f}[across];"
        "[a0pre][across][a1post]concat=n=3:v=0:a=1[aout]"
    )
    _run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(temp_video),
            "-i",
            input_a,
            "-i",
            input_b,
            "-filter_complex",
            audio_filter,
            "-map",
            "0:v",
            "-map",
            "[aout]",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            "-c:a",
            "aac",
            str(output),
        ]
    )
