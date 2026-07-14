"""Audio analysis: silence_detect, beat_detect, music_extend, stem_separate."""
from __future__ import annotations

import json
import math
import os
import re
import subprocess
import tempfile
from pathlib import Path


# ---------------------------------------------------------------------------
# beat_detect
# ---------------------------------------------------------------------------

def beat_detect(audio_path: str) -> list[float]:
    """Return a sorted list of beat timestamps (in seconds) for the given audio file.

    Primary backend: librosa.  Falls back to an ffmpeg-based rough estimate
    if librosa is not installed.
    """
    try:
        import librosa  # noqa: PLC0415
    except ImportError:
        return _beat_detect_ffmpeg(audio_path)

    y, sr = librosa.load(audio_path)
    _tempo, beat_frames = librosa.beat.beat_track(y=y, sr=sr)
    timestamps: list[float] = librosa.frames_to_time(beat_frames, sr=sr).tolist()
    return sorted(timestamps)


def _beat_detect_ffmpeg(audio_path: str) -> list[float]:
    """Rough beat detection via ffmpeg ametadata / astats filter."""
    cmd = [
        "ffmpeg", "-i", audio_path,
        "-af", "ametadata=mode=print:file=-:key=lavfi.astats.Overall.RMS_level",
        "-f", "null", "-",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    timestamps: list[float] = []
    for line in result.stderr.splitlines():
        if line.startswith("pts_time:"):
            try:
                t = float(line.split(":")[1].split()[0])
                timestamps.append(t)
            except (IndexError, ValueError):
                pass
    if not timestamps:
        return []
    beats: list[float] = [timestamps[0]]
    for t in timestamps[1:]:
        if t - beats[-1] >= 0.4:
            beats.append(t)
    return sorted(beats)


# ---------------------------------------------------------------------------
# music_extend
# ---------------------------------------------------------------------------

def music_extend(
    audio_path: str,
    output_path: str,
    *,
    target_duration: float,
) -> str:
    """Extend (or trim) audio to *target_duration* seconds.

    If the source is already long enough it is trimmed to target_duration.
    Otherwise the source is looped and crossfaded at loop points.

    Returns *output_path*.
    """
    actual_duration = _probe_duration(audio_path)

    if actual_duration >= target_duration:
        _ffmpeg_run([
            "ffmpeg", "-y",
            "-i", audio_path,
            "-t", str(target_duration),
            "-c", "copy",
            output_path,
        ])
        return output_path

    reps = math.ceil(target_duration / actual_duration)
    crossfade_sec = 0.5

    with tempfile.NamedTemporaryFile(suffix=Path(audio_path).suffix, delete=False) as tmp:
        looped_path = tmp.name

    try:
        # Step 1: create looped version
        _ffmpeg_run([
            "ffmpeg", "-y",
            "-stream_loop", str(reps - 1),
            "-i", audio_path,
            "-t", str(target_duration + crossfade_sec),
            "-c", "copy",
            looped_path,
        ])

        # Step 2: apply acrossfade at the first loop point
        loop_point = actual_duration
        fade_start = max(0.0, loop_point - crossfade_sec / 2)

        filter_complex = (
            f"[0:a]atrim=0:{fade_start + crossfade_sec},asetpts=PTS-STARTPTS[a1];"
            f"[0:a]atrim={fade_start}:{target_duration + crossfade_sec},asetpts=PTS-STARTPTS[a2];"
            f"[a1][a2]acrossfade=d={crossfade_sec}[out]"
        )

        _ffmpeg_run([
            "ffmpeg", "-y",
            "-i", looped_path,
            "-filter_complex", filter_complex,
            "-map", "[out]",
            "-t", str(target_duration),
            output_path,
        ])
    finally:
        try:
            os.unlink(looped_path)
        except OSError:
            pass

    return output_path


def _probe_duration(audio_path: str) -> float:
    """Return the duration of an audio file in seconds using ffprobe."""
    cmd = [
        "ffprobe", "-v", "quiet",
        "-print_format", "json",
        "-show_format",
        audio_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    info = json.loads(result.stdout)
    return float(info["format"]["duration"])


def _ffmpeg_run(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True, capture_output=True)


# ---------------------------------------------------------------------------
# stem_separate
# ---------------------------------------------------------------------------

def stem_separate(audio_path: str, output_dir: str) -> dict[str, str]:
    """Separate audio into vocal, drums, bass, and other stems.

    Primary backend: demucs.  Falls back to ffmpeg frequency-band filters
    if demucs is not installed.

    Returns a dict mapping stem name → file path.
    """
    os.makedirs(output_dir, exist_ok=True)

    try:
        return _stem_separate_demucs(audio_path, output_dir)
    except ImportError:
        return _stem_separate_ffmpeg(audio_path, output_dir)


def _stem_separate_demucs(audio_path: str, output_dir: str) -> dict[str, str]:
    try:
        import torch  # noqa: PLC0415
        from demucs.pretrained import get_model  # noqa: PLC0415
        from demucs.apply import apply_model  # noqa: PLC0415
        import torchaudio  # noqa: PLC0415
    except ImportError as exc:
        raise ImportError("pip install demucs") from exc

    model = get_model("htdemucs")
    model.eval()

    wav, sr = torchaudio.load(audio_path)
    if sr != model.samplerate:
        wav = torchaudio.functional.resample(wav, sr, model.samplerate)
    if wav.shape[0] == 1:
        wav = wav.repeat(2, 1)

    wav = wav.unsqueeze(0)  # (1, C, T)
    with torch.no_grad():
        sources = apply_model(model, wav, device="cpu")[0]  # (stems, C, T)

    stem_names = model.sources  # e.g. ['drums', 'bass', 'other', 'vocals']
    paths: dict[str, str] = {}
    for idx, name in enumerate(stem_names):
        out_path = os.path.join(output_dir, f"{name}.wav")
        torchaudio.save(out_path, sources[idx], model.samplerate)
        paths[name] = out_path

    for key in ("vocals", "drums", "bass", "other"):
        if key not in paths:
            paths[key] = paths.get(stem_names[0], "")

    return paths


def _stem_separate_ffmpeg(audio_path: str, output_dir: str) -> dict[str, str]:
    """Rough stem separation using ffmpeg frequency-band and channel filters."""
    stem_filters = {
        "bass":   "lowpass=f=200",
        "drums":  "bandpass=f=200:width_type=h:w=2000",
        "other":  "highpass=f=2000",
        "vocals": "pan=stereo|c0=c0-c1|c1=c1-c0",
    }
    paths: dict[str, str] = {}
    for name, af in stem_filters.items():
        out_path = os.path.join(output_dir, f"{name}.wav")
        _ffmpeg_run([
            "ffmpeg", "-y",
            "-i", audio_path,
            "-af", af,
            out_path,
        ])
        paths[name] = out_path
    return paths


# ---------------------------------------------------------------------------
# silence_detect (original)
# ---------------------------------------------------------------------------

def silence_detect(
    audio_path: str,
    *,
    threshold_db: float = -40.0,
    min_silence_sec: float = 0.5,
) -> list[dict]:
    """Detect silent segments in an audio or video file.

    Args:
        audio_path: Input audio/video file path.
        threshold_db: Silence threshold in dB (e.g. -40.0).
        min_silence_sec: Minimum duration in seconds for a segment to count as silence.

    Returns:
        List of dicts with keys ``start``, ``end``, ``duration`` (all floats, seconds).
    """
    cmd = [
        "ffmpeg", "-i", audio_path,
        "-af", f"silencedetect=noise={threshold_db}dB:d={min_silence_sec}",
        "-f", "null", "-",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    combined = proc.stdout + proc.stderr

    starts: list[float] = []
    ends: list[float] = []

    for line in combined.splitlines():
        m_start = re.search(r"silence_start:\s*([0-9.]+)", line)
        if m_start:
            starts.append(float(m_start.group(1)))
        m_end = re.search(r"silence_end:\s*([0-9.]+)", line)
        if m_end:
            ends.append(float(m_end.group(1)))

    result: list[dict] = []
    for start, end in zip(starts, ends):
        result.append({"start": start, "end": end, "duration": end - start})

    return result


# ---------------------------------------------------------------------------
# loudness_meter
# ---------------------------------------------------------------------------

def loudness_meter(audio_path: str) -> dict:
    """Measure integrated LUFS, true-peak dBFS, and LRA using ffmpeg ebur128.

    Args:
        audio_path: Source audio or video file.

    Returns:
        Dict with keys ``integrated_lufs`` (float), ``true_peak_dbfs`` (float),
        ``lra`` (float, loudness range), ``threshold_lufs`` (float).
    """
    cmd = [
        "ffmpeg", "-y", "-i", audio_path,
        "-af", "loudnorm=I=-23:TP=-1:LRA=7:print_format=json",
        "-f", "null", "-",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    combined = proc.stdout + proc.stderr

    # Parse the JSON block from loudnorm output
    start = combined.rfind("{")
    end = combined.rfind("}") + 1
    if start != -1 and end > start:
        data = json.loads(combined[start:end])
        return {
            "integrated_lufs": float(data.get("input_i", data.get("output_i", -23))),
            "true_peak_dbfs": float(data.get("input_tp", data.get("output_tp", -1))),
            "lra": float(data.get("input_lra", data.get("output_lra", 7))),
            "threshold_lufs": float(data.get("input_thresh", data.get("output_thresh", -33))),
        }

    # Fallback: parse ebur128 summary lines
    result = {"integrated_lufs": -23.0, "true_peak_dbfs": -1.0, "lra": 7.0, "threshold_lufs": -33.0}
    for line in combined.splitlines():
        if "I:" in line and "LUFS" in line:
            m = re.search(r"I:\s*([-\d.]+)\s*LUFS", line)
            if m:
                result["integrated_lufs"] = float(m.group(1))
        if "True peak:" in line or "Peak:" in line:
            m = re.search(r"(True peak|Peak):\s*([-\d.]+)", line)
            if m:
                result["true_peak_dbfs"] = float(m.group(2))
        if "LRA:" in line:
            m = re.search(r"LRA:\s*([-\d.]+)\s*LU", line)
            if m:
                result["lra"] = float(m.group(1))
    return result


# ---------------------------------------------------------------------------
# audio_visualizer
# ---------------------------------------------------------------------------

def audio_visualizer(
    audio_path: str,
    output_path: str,
    *,
    mode: str = "waveform",
    width: int = 1280,
    height: int = 720,
    fps: int = 30,
    bg_color: str = "black",
    fg_color: str = "0x00ff00",
) -> str:
    """Render audio as an animated visualization video.

    Args:
        audio_path: Source audio file.
        output_path: Destination video file.
        mode: ``"waveform"`` (showwaves), ``"spectrum"`` (showspectrum),
              or ``"combined"`` (waveform + spectrum stacked).
        width: Output video width in pixels.
        height: Output video height in pixels.
        fps: Output frame rate.
        bg_color: Background colour (ffmpeg colour string).
        fg_color: Foreground/wave colour.

    Returns:
        The *output_path*.
    """
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    duration_proc = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", audio_path],
        capture_output=True, text=True,
    )
    duration = float(duration_proc.stdout.strip()) if duration_proc.returncode == 0 else None

    if mode == "spectrum":
        vf = (
            f"showspectrum=s={width}x{height}:mode=combined:color=intensity"
            f":fps={fps}:slide=scroll"
        )
        cmd = [
            "ffmpeg", "-y", "-i", audio_path,
            "-filter_complex", f"[0:a]{vf}[v]",
            "-map", "[v]", "-map", "0:a",
            "-c:v", "libx264", "-c:a", "aac",
            "-pix_fmt", "yuv420p",
            output_path,
        ]
    elif mode == "combined":
        h2 = height // 2
        vf_wave = f"showwaves=s={width}x{h2}:mode=line:colors={fg_color}:rate={fps}"
        vf_spec = f"showspectrum=s={width}x{h2}:mode=combined:fps={fps}:slide=scroll"
        fc = (
            f"[0:a]asplit=2[a1][a2];"
            f"[a1]{vf_wave}[v1];"
            f"[a2]{vf_spec}[v2];"
            f"[v1][v2]vstack[v]"
        )
        cmd = [
            "ffmpeg", "-y", "-i", audio_path,
            "-filter_complex", fc,
            "-map", "[v]", "-map", "0:a",
            "-c:v", "libx264", "-c:a", "aac",
            "-pix_fmt", "yuv420p",
            output_path,
        ]
    else:  # waveform (default)
        vf = f"showwaves=s={width}x{height}:mode=line:colors={fg_color}:rate={fps}"
        cmd = [
            "ffmpeg", "-y", "-i", audio_path,
            "-filter_complex", f"[0:a]{vf}[v]",
            "-map", "[v]", "-map", "0:a",
            "-c:v", "libx264", "-c:a", "aac",
            "-pix_fmt", "yuv420p",
            output_path,
        ]

    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"audio_visualizer failed:\n{proc.stderr}")
    return output_path


# ---------------------------------------------------------------------------
# align_offset / detect_onsets / beat_info / suggest_cut_points  (DAY3 av-sync)
# ---------------------------------------------------------------------------

def _load_mono(audio_path: str, *, sr: int, max_seconds: float | None = None):
    """Load audio as a mono float array at ``sr`` (librosa), capped in length."""
    import librosa  # noqa: PLC0415
    y, _sr = librosa.load(audio_path, sr=sr, mono=True,
                          duration=max_seconds if max_seconds else None)
    return y


def align_offset(
    ref_path: str,
    other_path: str,
    *,
    sr: int = 22050,
    max_offset_sec: float | None = None,
    max_analyze_sec: float = 60.0,
) -> dict:
    """Estimate the time offset between two recordings by waveform cross-correlation.

    Loads both clips mono at ``sr`` (capped to ``max_analyze_sec`` seconds),
    removes the DC mean, and cross-correlates the raw waveforms (sample-accurate,
    FFT-based). This is genuine *waveform* alignment, well suited to syncing the
    same event captured on two mics / cameras.

    Sign convention for ``offset_sec`` (the lag of ``other`` relative to ``ref``):

      offset_sec > 0  -> ``other`` LAGS ``ref`` by that many seconds
                         (trim offset_sec from other's head, or prepend that much
                          silence to ref, to line them up).
      offset_sec < 0  -> ``other`` LEADS ``ref``.

    ``max_offset_sec`` restricts the search to lags within +/- that many seconds.
    Returns ``{"offset_sec", "confidence", "method", "sr"}`` where confidence is
    the normalized correlation at the winning lag (0..1).
    """
    try:
        import numpy as np  # noqa: PLC0415
        from scipy import signal as _sig  # noqa: PLC0415
    except ImportError:
        return {"offset_sec": 0.0, "confidence": 0.0, "method": "unavailable", "sr": sr}

    y_ref = _load_mono(ref_path, sr=sr, max_seconds=max_analyze_sec)
    y_oth = _load_mono(other_path, sr=sr, max_seconds=max_analyze_sec)
    if y_ref.size == 0 or y_oth.size == 0:
        return {"offset_sec": 0.0, "confidence": 0.0, "method": "empty", "sr": sr}

    a = y_oth.astype("float64"); a = a - a.mean()
    b = y_ref.astype("float64"); b = b - b.mean()
    corr = _sig.correlate(a, b, mode="full", method="fft")
    lags = _sig.correlation_lags(a.size, b.size, mode="full")

    if max_offset_sec is not None:
        max_lag = int(round(max_offset_sec * sr))
        mask = np.abs(lags) <= max_lag
        if mask.any():
            corr = corr[mask]
            lags = lags[mask]

    peak = int(np.argmax(corr))
    lag = int(lags[peak])
    denom = float(np.sqrt(np.sum(a * a) * np.sum(b * b)))
    confidence = float(corr[peak] / denom) if denom > 0 else 0.0
    confidence = max(0.0, min(1.0, confidence))
    return {
        "offset_sec": lag / float(sr),
        "confidence": round(confidence, 4),
        "method": "waveform-xcorr",
        "sr": sr,
    }


def detect_onsets(audio_path: str, *, sr: int = 22050,
                  max_analyze_sec: float = 120.0) -> list[float]:
    """Return onset times (seconds) via librosa onset detection."""
    try:
        import librosa  # noqa: PLC0415
    except ImportError:
        return []
    y = _load_mono(audio_path, sr=sr, max_seconds=max_analyze_sec)
    if y.size == 0:
        return []
    onsets = librosa.onset.onset_detect(y=y, sr=sr, units="time", backtrack=False)
    return sorted(float(t) for t in onsets)


def beat_info(audio_path: str) -> dict:
    """Return ``{"tempo_bpm": float, "beats": [sec, ...]}`` via librosa beat tracking."""
    try:
        import librosa  # noqa: PLC0415
        import numpy as np  # noqa: PLC0415
    except ImportError:
        return {"tempo_bpm": 0.0, "beats": sorted(_beat_detect_ffmpeg(audio_path))}

    y, sr = librosa.load(audio_path)
    tempo, beat_frames = librosa.beat.beat_track(y=y, sr=sr)
    beats = librosa.frames_to_time(beat_frames, sr=sr).tolist()
    tempo_val = float(np.atleast_1d(tempo).ravel()[0]) if tempo is not None else 0.0
    return {"tempo_bpm": round(tempo_val, 2), "beats": sorted(float(x) for x in beats)}


def suggest_cut_points(audio_path: str, *, source: str = "beat",
                       every: int = 1, max_points: int = 128) -> list[float]:
    """Suggest cut times from beats (default) or onsets, taking every Nth point."""
    if source == "onset":
        pts = detect_onsets(audio_path)
    elif source == "beat":
        pts = beat_info(audio_path).get("beats", [])
    else:
        return []
    every = max(1, int(every))
    return pts[::every][:max_points]
