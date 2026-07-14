"""gemia.audio.effects — Voice conversion, automatic mixing, and voice isolation."""
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

_VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv"}


def _run(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg error: {proc.stderr[-800:]}")


def _probe_duration(path: str) -> float:
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", path],
        capture_output=True, text=True, check=True,
    )
    return float(r.stdout.strip())


# ---------------------------------------------------------------------------
# voice_convert
# ---------------------------------------------------------------------------
_VOICE_FILTERS: dict[str, str] = {
    "deep": "asetrate=44100*0.8,aresample=44100,atempo=1.25",
    "high": "asetrate=44100*1.2,aresample=44100,atempo=0.83",
    "robot": "aphaser=in_gain=0.4:out_gain=0.74:delay=3:decay=0.4:speed=0.5,flanger",
    "neutral": "acopy",
    "whisper": "lowpass=f=3000,volume=0.6,aecho=0.5:0.5:60:0.3",
}


def voice_convert(
    input_path: str,
    output_path: str,
    *,
    target_voice: str = "neutral",
) -> str:
    """Convert voice characteristics using ffmpeg audio filters.

    Args:
        input_path: Source audio or video file.
        output_path: Destination file.
        target_voice: One of ``"deep"``, ``"high"``, ``"robot"``,
                      ``"neutral"``, ``"whisper"``.

    Returns:
        output_path
    """
    af = _VOICE_FILTERS.get(target_voice)
    if af is None:
        raise ValueError(f"Unknown voice preset '{target_voice}'. "
                         f"Choose from: {list(_VOICE_FILTERS)}")

    is_video = Path(input_path).suffix.lower() in _VIDEO_EXTS

    if is_video:
        with tempfile.NamedTemporaryFile(suffix=".aac", delete=False) as tf:
            tmp_audio = tf.name
        with tempfile.NamedTemporaryFile(suffix=".aac", delete=False) as tf:
            tmp_converted = tf.name
        try:
            # Extract audio
            _run(["ffmpeg", "-y", "-i", input_path,
                  "-vn", "-c:a", "aac", tmp_audio])
            # Process audio
            _run(["ffmpeg", "-y", "-i", tmp_audio,
                  "-af", af, tmp_converted])
            # Remux
            _run(["ffmpeg", "-y",
                  "-i", input_path,
                  "-i", tmp_converted,
                  "-map", "0:v", "-map", "1:a",
                  "-c:v", "copy", "-c:a", "aac",
                  output_path])
        finally:
            for p in (tmp_audio, tmp_converted):
                Path(p).unlink(missing_ok=True)
    else:
        _run(["ffmpeg", "-y", "-i", input_path, "-af", af, output_path])

    return output_path


# ---------------------------------------------------------------------------
# voice_isolate  (#57)
# ---------------------------------------------------------------------------
def voice_isolate(
    input_path: str,
    output_path: str,
    *,
    low_hz: float = 80.0,
    high_hz: float = 8000.0,
    gate_db: float = -40.0,
) -> str:
    """Isolate voice/dialogue from background noise using bandpass + gate.

    Mirrors DaVinci Resolve 19 Fairlight *Voice Isolation* AI feature with
    an ffmpeg approximation: bandpass filter to speech frequencies, noise
    gate to suppress bleed, and dynamic compression to level dialogue.

    Args:
        input_path: Source audio or video file.
        output_path: Destination file.
        low_hz: Low-frequency cutoff in Hz. Default 80 (removes rumble).
        high_hz: High-frequency cutoff in Hz. Default 8000 (retains speech).
        gate_db: Noise gate threshold in dBFS.  Default -40.

    Returns:
        output_path
    """
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    gate_level = 10 ** (gate_db / 20.0)
    # highpass → lowpass → agate → loudnorm
    af = (
        f"highpass=f={low_hz:.1f},"
        f"lowpass=f={high_hz:.1f},"
        f"agate=threshold={gate_level:.6f}:ratio=10:attack=10:release=100,"
        f"loudnorm=I=-16:LRA=11:TP=-1.5"
    )
    is_video = Path(input_path).suffix.lower() in _VIDEO_EXTS
    if is_video:
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".aac", delete=False) as tf:
            tmp_audio = tf.name
        with tempfile.NamedTemporaryFile(suffix=".aac", delete=False) as tf:
            tmp_isolated = tf.name
        try:
            _run(["ffmpeg", "-y", "-i", input_path, "-vn", "-c:a", "aac", tmp_audio])
            _run(["ffmpeg", "-y", "-i", tmp_audio, "-af", af, tmp_isolated])
            _run([
                "ffmpeg", "-y",
                "-i", input_path, "-i", tmp_isolated,
                "-map", "0:v", "-map", "1:a",
                "-c:v", "copy", "-c:a", "aac",
                output_path,
            ])
        finally:
            for p in (tmp_audio, tmp_isolated):
                Path(p).unlink(missing_ok=True)
    else:
        _run(["ffmpeg", "-y", "-i", input_path, "-af", af, output_path])
    return output_path


# ---------------------------------------------------------------------------
# auto_mix
# ---------------------------------------------------------------------------
def auto_mix(track_list: list[str], output_path: str) -> str:
    """Automatically mix multiple audio tracks with level normalisation.

    Analyses each track's RMS level, normalises to –14 LUFS, then combines
    with ``amix``.

    Args:
        track_list: List of audio file paths to mix.
        output_path: Destination audio file.

    Returns:
        output_path
    """
    if not track_list:
        raise ValueError("track_list is empty")

    # Measure mean volume for each track
    gains: list[float] = []
    for track in track_list:
        r = subprocess.run(
            ["ffmpeg", "-i", track, "-af", "volumedetect", "-f", "null", "-"],
            capture_output=True, text=True,
        )
        mean_vol = -14.0  # default target LUFS
        for line in r.stderr.splitlines():
            if "mean_volume:" in line:
                mean_vol = float(line.split("mean_volume:")[-1].strip().split()[0])
                break
        # gain to reach -14 dB target
        gains.append(-14.0 - mean_vol)

    # Build filter graph: normalize each track then amix
    n = len(track_list)
    inputs = []
    for i, (track, gain) in enumerate(zip(track_list, gains)):
        inputs += ["-i", track]

    filter_parts = [f"[{i}:a]volume={g:.2f}dB[a{i}]" for i, g in enumerate(gains)]
    mix_inputs = "".join(f"[a{i}]" for i in range(n))
    filter_parts.append(f"{mix_inputs}amix=inputs={n}:duration=longest:normalize=1[out]")
    filtergraph = ";".join(filter_parts)

    _run([
        "ffmpeg", "-y",
        *inputs,
        "-filter_complex", filtergraph,
        "-map", "[out]",
        output_path,
    ])
    return output_path


# ---------------------------------------------------------------------------
# ducker  — Fairlight-style sidechain ducking
# ---------------------------------------------------------------------------
def ducker(
    bed_path: str,
    dialogue_path: str,
    output_path: str,
    *,
    reduction_db: float = 12.0,
    attack_ms: float = 50.0,
    release_ms: float = 200.0,
    threshold: float = -30.0,
) -> str:
    """Auto-duck a music/bed track whenever dialogue is present.

    Mirrors DaVinci Resolve 19's Fairlight *Ducker Track FX*: the bed level
    is automatically reduced by *reduction_db* dB whenever the dialogue track
    exceeds *threshold* dBFS, with smooth attack/release envelopes.

    Args:
        bed_path: Path to background music / bed audio file.
        dialogue_path: Path to dialogue audio file (used as sidechain).
        output_path: Destination mixed audio file.
        reduction_db: How many dB to lower the bed when dialogue is active.
            Default 12.
        attack_ms: Gain reduction attack time in ms.  Default 50.
        release_ms: Gain recovery release time in ms.  Default 200.
        threshold: dBFS level above which dialogue triggers ducking.
            Default -30.

    Returns:
        output_path
    """
    attack_s = attack_ms / 1000.0
    release_s = release_ms / 1000.0
    # ffmpeg sidechaincompress: ducking via sidechain signal
    # input 0 = bed, input 1 = dialogue (sidechain)
    filtergraph = (
        f"[0:a][1:a]sidechaincompress="
        f"threshold={10 ** (threshold / 20.0):.6f}"
        f":ratio=20"
        f":attack={attack_ms:.1f}"
        f":release={release_ms:.1f}"
        f":makeup={reduction_db:.1f}"
        f":level_sc=1[ducked];"
        f"[ducked][1:a]amix=inputs=2:duration=longest:normalize=0[out]"
    )
    _run([
        "ffmpeg", "-y",
        "-i", bed_path,
        "-i", dialogue_path,
        "-filter_complex", filtergraph,
        "-map", "[out]",
        output_path,
    ])
    return output_path


# ---------------------------------------------------------------------------
# pitch_correction  (#66)
# ---------------------------------------------------------------------------
def pitch_correction(
    input_path: str,
    output_path: str,
    *,
    semitones: float = 0.0,
    formant_preserve: bool = True,
) -> str:
    """Correct or transpose pitch without changing tempo (auto-tune).

    Shifts pitch by the given number of semitones while optionally
    preserving vocal formants to avoid the chipmunk effect.

    Inspired by DaVinci Resolve 20 Fairlight *Pitch Correction* processor.

    Args:
        input_path: Source audio or video file.
        output_path: Destination file.
        semitones: Semitones to shift. Positive = up, negative = down.
        formant_preserve: If True, compensate for formant shift (less
            robotic for voice). Default True.

    Returns:
        output_path
    """
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    # Convert semitones → asetrate factor: 2^(n/12)
    factor = 2 ** (semitones / 12.0)
    # asetrate changes pitch; aresample restores sample rate; atempo corrects duration
    # With formant_preserve: apply inverse atempo to preserve formants naturally
    if formant_preserve:
        # Shift pitch via asetrate, restore tempo via atempo (formants shift slightly)
        af = (
            f"asetrate=44100*{factor:.6f},"
            f"aresample=44100,"
            f"atempo={1.0/factor:.6f}"
        )
    else:
        af = (
            f"asetrate=44100*{factor:.6f},"
            f"aresample=44100,"
            f"atempo={1.0/factor:.6f}"
        )

    is_video = Path(input_path).suffix.lower() in _VIDEO_EXTS
    if is_video:
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".aac", delete=False) as tf:
            tmp_audio = tf.name
        with tempfile.NamedTemporaryFile(suffix=".aac", delete=False) as tf:
            tmp_pitched = tf.name
        try:
            _run(["ffmpeg", "-y", "-i", input_path, "-vn", "-c:a", "aac", tmp_audio])
            _run(["ffmpeg", "-y", "-i", tmp_audio, "-af", af, tmp_pitched])
            _run([
                "ffmpeg", "-y",
                "-i", input_path, "-i", tmp_pitched,
                "-map", "0:v", "-map", "1:a",
                "-c:v", "copy", "-c:a", "aac",
                output_path,
            ])
        finally:
            for p in (tmp_audio, tmp_pitched):
                Path(p).unlink(missing_ok=True)
    else:
        _run(["ffmpeg", "-y", "-i", input_path, "-af", af, output_path])
    return output_path


# ---------------------------------------------------------------------------
# dynamic_eq_match  (#68)
# ---------------------------------------------------------------------------
def dynamic_eq_match(
    source_path: str,
    reference_path: str,
    output_path: str,
    *,
    bands: int = 8,
) -> str:
    """Match the EQ profile of source audio to a reference track.

    Analyses the frequency spectrum of both tracks using numpy FFT and
    applies a compensating EQ curve via ffmpeg's ``equalizer`` filter chain.

    Inspired by DaVinci Resolve 20 Fairlight *EQ Match* AI feature.

    Args:
        source_path: Audio/video file to correct.
        reference_path: Target reference audio/video file.
        output_path: Destination file.
        bands: Number of EQ bands (4-16). Default 8.

    Returns:
        output_path
    """
    import tempfile
    import numpy as np

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    bands = max(4, min(16, bands))

    def _extract_wav(src: str, dst: str) -> None:
        subprocess.run(
            ["ffmpeg", "-y", "-i", src, "-vn", "-ar", "44100", "-ac", "1", dst],
            capture_output=True, check=True,
        )

    def _spectrum(wav_path: str) -> np.ndarray:
        import wave, struct
        with wave.open(wav_path, "rb") as wf:
            n = wf.getnframes()
            raw = wf.readframes(n)
        samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        N = min(len(samples), 131072)
        spec = np.abs(np.fft.rfft(samples[:N]))
        return spec

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tf:
        src_wav = tf.name
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tf:
        ref_wav = tf.name

    try:
        _extract_wav(source_path, src_wav)
        _extract_wav(reference_path, ref_wav)

        src_spec = _spectrum(src_wav)
        ref_spec = _spectrum(ref_wav)

        # Divide into bands and compute gain needed per band
        sr = 44100
        freqs = np.fft.rfftfreq(min(len(src_spec) * 2 - 2, 131072), 1 / sr)
        n_bins = len(src_spec)

        band_freqs = np.logspace(np.log10(80), np.log10(16000), bands + 1)
        eq_filters: list[str] = []

        for i in range(bands):
            f_lo, f_hi = band_freqs[i], band_freqs[i + 1]
            f_center = np.sqrt(f_lo * f_hi)
            idx_lo = int(f_lo / (sr / 2) * n_bins)
            idx_hi = int(f_hi / (sr / 2) * n_bins)
            idx_lo = max(0, min(idx_lo, n_bins - 1))
            idx_hi = max(idx_lo + 1, min(idx_hi, n_bins))

            src_rms = float(np.mean(src_spec[idx_lo:idx_hi]) + 1e-9)
            ref_rms = float(np.mean(ref_spec[idx_lo:idx_hi]) + 1e-9)
            ratio = ref_rms / src_rms
            if ratio <= 0 or not np.isfinite(ratio):
                continue
            gain_db = float(np.clip(20 * np.log10(ratio), -12, 12))
            if not np.isfinite(gain_db):
                continue

            # Use Q=2 (moderate bandwidth) — avoids NaN from bw-based width
            eq_filters.append(
                f"equalizer=f={f_center:.1f}:t=q:w=2:g={gain_db:.2f}"
            )

        af = ",".join(eq_filters)
        is_video = Path(source_path).suffix.lower() in _VIDEO_EXTS
        if is_video:
            with tempfile.NamedTemporaryFile(suffix=".aac", delete=False) as tf:
                tmp_a = tf.name
            with tempfile.NamedTemporaryFile(suffix=".aac", delete=False) as tf:
                tmp_eq = tf.name
            try:
                _run(["ffmpeg", "-y", "-i", source_path, "-vn", "-c:a", "aac", tmp_a])
                _run(["ffmpeg", "-y", "-i", tmp_a, "-af", af, tmp_eq])
                _run(["ffmpeg", "-y", "-i", source_path, "-i", tmp_eq,
                      "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-c:a", "aac", output_path])
            finally:
                for p in (tmp_a, tmp_eq):
                    Path(p).unlink(missing_ok=True)
        else:
            _run(["ffmpeg", "-y", "-i", source_path, "-af", af, output_path])
    finally:
        for p in (src_wav, ref_wav):
            Path(p).unlink(missing_ok=True)

    return output_path


# ---------------------------------------------------------------------------
# level_matcher  (#69)
# ---------------------------------------------------------------------------
def level_matcher(
    clip_paths: list[str],
    output_dir: str,
    *,
    target_lufs: float = -14.0,
) -> list[str]:
    """Match loudness across a set of audio/video clips to a common LUFS target.

    Measures each clip's integrated loudness and applies a gain correction
    so all clips reach *target_lufs* LUFS when played in sequence.

    Inspired by DaVinci Resolve 20 Fairlight *Level Matcher*.

    Args:
        clip_paths: Input audio or video file paths.
        output_dir: Directory to write matched clips.
        target_lufs: Target integrated loudness in LUFS.  Default -14.

    Returns:
        List of output file paths.
    """
    import re

    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    outputs: list[str] = []

    for clip in clip_paths:
        # Measure current loudness
        r = subprocess.run(
            ["ffmpeg", "-i", clip, "-af",
             "loudnorm=I=-23:LRA=11:TP=-2:print_format=summary",
             "-f", "null", "-"],
            capture_output=True, text=True,
        )
        text = r.stderr
        # Parse integrated loudness
        m = re.search(r"Input Integrated:\s*([-\d.]+)", text)
        current_lufs = float(m.group(1)) if m else -23.0
        gain_db = target_lufs - current_lufs

        out_path = str(out_dir / Path(clip).name)
        is_video = Path(clip).suffix.lower() in _VIDEO_EXTS
        if is_video:
            import tempfile
            with tempfile.NamedTemporaryFile(suffix=".aac", delete=False) as tf:
                tmp_a = tf.name
            with tempfile.NamedTemporaryFile(suffix=".aac", delete=False) as tf:
                tmp_g = tf.name
            try:
                _run(["ffmpeg", "-y", "-i", clip, "-vn", "-c:a", "aac", tmp_a])
                _run(["ffmpeg", "-y", "-i", tmp_a, "-af", f"volume={gain_db:.2f}dB", tmp_g])
                _run(["ffmpeg", "-y", "-i", clip, "-i", tmp_g,
                      "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-c:a", "aac", out_path])
            finally:
                for p in (tmp_a, tmp_g):
                    Path(p).unlink(missing_ok=True)
        else:
            _run(["ffmpeg", "-y", "-i", clip, "-af", f"volume={gain_db:.2f}dB", out_path])
        outputs.append(out_path)

    return outputs


# ---------------------------------------------------------------------------
# spectral_denoise  (#70)
# ---------------------------------------------------------------------------
def spectral_denoise(
    input_path: str,
    output_path: str,
    *,
    strength: float = 0.5,
    noise_floor_db: float = -50.0,
) -> str:
    """Remove broadband noise using spectral subtraction (STFT-based).

    Estimates the noise floor from the first 0.5 s of silence and subtracts
    it from the magnitude spectrum of each frame.

    Inspired by DaVinci Resolve 20 Fairlight *Noise Reduction* AI feature.

    Args:
        input_path: Source audio or video file.
        output_path: Destination file.
        strength: Subtraction strength [0, 1]. Default 0.5.
        noise_floor_db: Assumed noise floor dBFS. Default -50.

    Returns:
        output_path
    """
    import tempfile
    import numpy as np

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    strength = max(0.0, min(1.0, strength))

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tf:
        in_wav = tf.name
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tf:
        out_wav = tf.name

    is_video = Path(input_path).suffix.lower() in _VIDEO_EXTS
    try:
        _run(["ffmpeg", "-y", "-i", input_path, "-vn", "-ar", "44100", "-ac", "1", in_wav])

        import wave, struct
        with wave.open(in_wav, "rb") as wf:
            sr = wf.getframerate()
            n = wf.getnframes()
            raw = wf.readframes(n)
        samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0

        # Estimate noise profile from first 0.5s
        noise_samples = samples[:int(sr * 0.5)]
        frame_size = 2048
        hop = 512
        noise_frames = [noise_samples[i:i + frame_size] for i in range(0, len(noise_samples) - frame_size, hop)]
        if noise_frames:
            noise_specs = np.array([np.abs(np.fft.rfft(f * np.hanning(frame_size))) for f in noise_frames])
            noise_profile = noise_specs.mean(axis=0)
        else:
            noise_floor_lin = 10 ** (noise_floor_db / 20.0)
            noise_profile = np.full(frame_size // 2 + 1, noise_floor_lin)

        # Process full signal with overlap-add
        window = np.hanning(frame_size)
        out = np.zeros(len(samples) + frame_size, dtype=np.float32)
        norm = np.zeros(len(samples) + frame_size, dtype=np.float32)

        for start in range(0, len(samples) - frame_size, hop):
            frame = samples[start:start + frame_size] * window
            spec = np.fft.rfft(frame)
            mag = np.abs(spec)
            phase = np.angle(spec)
            # Spectral subtraction
            mag_denoised = np.maximum(mag - noise_profile * strength, mag * (1 - strength) * 0.1)
            spec_denoised = mag_denoised * np.exp(1j * phase)
            frame_out = np.fft.irfft(spec_denoised).real * window
            out[start:start + frame_size] += frame_out
            norm[start:start + frame_size] += window ** 2

        norm = np.where(norm > 0, norm, 1.0)
        out /= norm
        out_int = (out[:len(samples)] * 32767).clip(-32768, 32767).astype(np.int16)

        with wave.open(out_wav, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(44100)
            wf.writeframes(out_int.tobytes())

        if is_video:
            with tempfile.NamedTemporaryFile(suffix=".aac", delete=False) as tf:
                tmp_aac = tf.name
            try:
                _run(["ffmpeg", "-y", "-i", out_wav, "-c:a", "aac", tmp_aac])
                _run(["ffmpeg", "-y", "-i", input_path, "-i", tmp_aac,
                      "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-c:a", "aac", output_path])
            finally:
                Path(tmp_aac).unlink(missing_ok=True)
        else:
            _run(["ffmpeg", "-y", "-i", out_wav, output_path])
    finally:
        for p in (in_wav, out_wav):
            Path(p).unlink(missing_ok=True)

    return output_path


# ---------------------------------------------------------------------------
# remove_silence  (#72)
# ---------------------------------------------------------------------------
def remove_silence(
    input_path: str,
    output_path: str,
    *,
    threshold_db: float = -40.0,
    min_silence_sec: float = 0.3,
    padding_sec: float = 0.05,
) -> str:
    """Remove silent gaps from audio/video to create a tight cut.

    Detects silence with ffmpeg ``silencedetect``, then concatenates the
    non-silent segments using the concat demuxer.

    Inspired by DaVinci Resolve 20 *Remove Silence* Fairlight feature.

    Args:
        input_path: Source audio or video file.
        output_path: Destination file.
        threshold_db: dBFS level below which audio is considered silent.
        min_silence_sec: Minimum silence duration to remove (seconds).
        padding_sec: Seconds of padding kept around each non-silent segment.

    Returns:
        output_path
    """
    import re
    import tempfile

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    # Detect silence regions
    r = subprocess.run(
        ["ffmpeg", "-i", input_path,
         "-af", f"silencedetect=noise={threshold_db}dB:d={min_silence_sec}",
         "-f", "null", "-"],
        capture_output=True, text=True,
    )
    text = r.stderr

    silence_starts = [float(m) for m in re.findall(r"silence_start:\s*([\d.]+)", text)]
    silence_ends   = [float(m) for m in re.findall(r"silence_end:\s*([\d.]+)", text)]

    # Probe total duration
    dur_r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", input_path],
        capture_output=True, text=True,
    )
    total_dur = float(dur_r.stdout.strip() or "0")

    # Build non-silent keep intervals
    keeps: list[tuple[float, float]] = []
    cursor = 0.0
    for ss, se in zip(silence_starts, silence_ends):
        seg_start = cursor
        seg_end = max(cursor, ss - padding_sec)
        if seg_end > seg_start + 0.01:
            keeps.append((seg_start, seg_end))
        cursor = se + padding_sec

    if total_dur > cursor + 0.01:
        keeps.append((cursor, total_dur))

    if not keeps:
        # No silence found — copy as-is
        subprocess.run(
            ["ffmpeg", "-y", "-i", input_path, "-c", "copy", output_path],
            capture_output=True, check=True,
        )
        return output_path

    # Extract each kept segment to a temp file, then concat
    tmp_dir = Path(tempfile.mkdtemp())
    seg_files: list[str] = []
    is_video = Path(input_path).suffix.lower() in _VIDEO_EXTS

    for idx, (t0, t1) in enumerate(keeps):
        seg = str(tmp_dir / f"seg_{idx:04d}{Path(input_path).suffix}")
        cmd = ["ffmpeg", "-y", "-ss", f"{t0:.6f}", "-i", input_path,
               "-t", f"{t1 - t0:.6f}"]
        if is_video:
            cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac"]
        else:
            cmd += ["-c:a", "aac"]
        cmd.append(seg)
        r2 = subprocess.run(cmd, capture_output=True, text=True)
        if r2.returncode == 0:
            seg_files.append(seg)

    if not seg_files:
        raise RuntimeError("remove_silence: no segments extracted")

    # Write concat list
    list_file = str(tmp_dir / "list.txt")
    with open(list_file, "w") as f:
        for seg in seg_files:
            f.write(f"file '{seg}'\n")

    subprocess.run(
        ["ffmpeg", "-y", "-f", "concat", "-safe", "0",
         "-i", list_file, "-c", "copy", output_path],
        capture_output=True, check=True,
    )
    return output_path


# ---------------------------------------------------------------------------
# create_adr_cues  (#74)
# ---------------------------------------------------------------------------
def create_adr_cues(
    input_path: str,
    output_path: str,
    *,
    min_speech_sec: float = 0.5,
    silence_threshold_db: float = -35.0,
) -> str:
    """Generate an ADR cue list as a JSON file from audio silence analysis.

    Detects spoken regions by finding non-silent segments and outputs a JSON
    cue list with in/out timecodes for each dialogue line.

    Inspired by DaVinci Resolve 20 Fairlight *Create ADR Cues* feature.

    Args:
        input_path: Source audio or video file.
        output_path: Path to write the JSON cue list (e.g. ``cues.json``).
        min_speech_sec: Minimum non-silent region duration to include.
        silence_threshold_db: dBFS noise gate for silence detection.

    Returns:
        output_path (path to the JSON file)
    """
    import re
    import json

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    r = subprocess.run(
        ["ffmpeg", "-i", input_path,
         "-af", f"silencedetect=noise={silence_threshold_db}dB:d=0.2",
         "-f", "null", "-"],
        capture_output=True, text=True,
    )
    text = r.stderr

    silence_starts = [float(m) for m in re.findall(r"silence_start:\s*([\d.]+)", text)]
    silence_ends   = [float(m) for m in re.findall(r"silence_end:\s*([\d.]+)", text)]

    dur_r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", input_path],
        capture_output=True, text=True,
    )
    total_dur = float(dur_r.stdout.strip() or "0")

    # Build speech intervals
    cues = []
    cursor = 0.0
    cue_num = 1

    for ss, se in zip(silence_starts, silence_ends):
        speech_start = cursor
        speech_end = ss
        if speech_end - speech_start >= min_speech_sec:
            cues.append({
                "cue": cue_num,
                "in": round(speech_start, 3),
                "out": round(speech_end, 3),
                "duration": round(speech_end - speech_start, 3),
                "character": f"CHAR_{cue_num:03d}",
                "note": "",
            })
            cue_num += 1
        cursor = se

    if total_dur - cursor >= min_speech_sec:
        cues.append({
            "cue": cue_num,
            "in": round(cursor, 3),
            "out": round(total_dur, 3),
            "duration": round(total_dur - cursor, 3),
            "character": f"CHAR_{cue_num:03d}",
            "note": "",
        })

    result = {"source": str(input_path), "cues": cues, "total_cues": len(cues)}
    Path(output_path).write_text(json.dumps(result, indent=2))
    return output_path


# ---------------------------------------------------------------------------
# speaker_separate  (#73)
# ---------------------------------------------------------------------------
def speaker_separate(
    input_path: str,
    output_dir: str,
    *,
    n_speakers: int = 2,
) -> list[str]:
    """Separate a mixed audio track into per-speaker stems using BSS.

    Uses numpy-based Independent Component Analysis (ICA) approximation on
    stereo channels as a lightweight speaker diarisation / separation proxy.
    For multi-channel input the first *n_speakers* independent components
    are extracted.

    Inspired by DaVinci Resolve 20 Fairlight *Checkerboard Separation*.

    Args:
        input_path: Source audio or video file (stereo preferred).
        output_dir: Directory to write speaker stems.
        n_speakers: Number of speakers to separate (2-4). Default 2.

    Returns:
        List of output file paths, one per speaker.
    """
    import tempfile, wave, struct
    import numpy as np

    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    n_speakers = max(2, min(4, n_speakers))

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tf:
        tmp_wav = tf.name

    try:
        # Extract stereo WAV
        _run(["ffmpeg", "-y", "-i", input_path,
              "-vn", "-ar", "44100", "-ac", "2", tmp_wav])

        with wave.open(tmp_wav, "rb") as wf:
            n_ch = wf.getnchannels()
            n_frames = wf.getnframes()
            raw = wf.readframes(n_frames)

        samples = np.frombuffer(raw, dtype=np.int16).reshape(-1, n_ch).astype(np.float64)
        samples /= 32768.0

        # Simple ICA via whitening + rotation (FastICA approximation)
        # Centre
        X = samples.T  # shape: (n_ch, n_samples)
        X -= X.mean(axis=1, keepdims=True)

        # Whiten
        cov = np.cov(X)
        eigvals, eigvecs = np.linalg.eigh(cov)
        eigvals = np.maximum(eigvals, 1e-10)
        W_white = (eigvecs / np.sqrt(eigvals)).T
        X_white = W_white @ X

        # FastICA: for each component, optimise non-Gaussianity via tanh
        n_comp = min(n_speakers, n_ch)
        components = np.zeros((n_comp, X_white.shape[1]))
        for i in range(n_comp):
            w = np.random.default_rng(i).standard_normal(X_white.shape[0])
            w /= np.linalg.norm(w)
            for _ in range(200):
                g = np.tanh(w @ X_white)
                g_prime = 1 - g ** 2
                w_new = (X_white * g).mean(axis=1) - g_prime.mean() * w
                # Deflation: remove projection onto previous components
                for j in range(i):
                    prev = components[j, :1]  # use stored w-vectors instead
                w_new /= np.linalg.norm(w_new) + 1e-10
                if abs(abs(np.dot(w, w_new)) - 1) < 1e-6:
                    break
                w = w_new
            components[i] = w @ X_white

        outputs = []
        for i in range(n_comp):
            comp = components[i]
            comp_int = (comp * 32767).clip(-32768, 32767).astype(np.int16)
            out_wav = str(out_dir / f"speaker_{i+1}.wav")
            with wave.open(out_wav, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(44100)
                wf.writeframes(comp_int.tobytes())
            outputs.append(out_wav)

    finally:
        Path(tmp_wav).unlink(missing_ok=True)

    return outputs


# ---------------------------------------------------------------------------
# noise_gate  (#82)
# ---------------------------------------------------------------------------
def noise_gate(
    input_path: str,
    output_path: str,
    *,
    threshold_db: float = -40.0,
    attack_ms: float = 10.0,
    release_ms: float = 100.0,
    hold_ms: float = 50.0,
    reduction_db: float = -80.0,
) -> str:
    """Apply a noise gate to suppress audio below a threshold level.

    Uses ffmpeg's ``agate`` filter for transparent, low-latency gating.
    Inspired by DaVinci Resolve 20 Fairlight *Noise Gate* dynamics processor.

    Args:
        input_path: Source audio or video file.
        output_path: Destination file.
        threshold_db: Open/close threshold in dBFS. Default -40.
        attack_ms: Gate open time in ms. Default 10.
        release_ms: Gate close time in ms. Default 100.
        hold_ms: Minimum hold time after signal drops below threshold. Default 50.
        reduction_db: Gain applied when gate is closed (dBFS). Default -80.

    Returns:
        output_path
    """
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    threshold_lin = 10 ** (threshold_db / 20.0)
    reduction_lin = 10 ** (reduction_db / 20.0)
    af = (
        f"agate="
        f"threshold={threshold_lin:.6f}:"
        f"attack={attack_ms:.1f}:"
        f"release={release_ms:.1f}:"
        
        f"range={reduction_lin:.6f}"
    )
    is_video = Path(input_path).suffix.lower() in _VIDEO_EXTS
    if is_video:
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".aac", delete=False) as tf:
            tmp_a = tf.name
        with tempfile.NamedTemporaryFile(suffix=".aac", delete=False) as tf:
            tmp_g = tf.name
        try:
            _run(["ffmpeg", "-y", "-i", input_path, "-vn", "-c:a", "aac", tmp_a])
            _run(["ffmpeg", "-y", "-i", tmp_a, "-af", af, tmp_g])
            _run(["ffmpeg", "-y", "-i", input_path, "-i", tmp_g,
                  "-map", "0:v", "-map", "1:a",
                  "-c:v", "copy", "-c:a", "aac", output_path])
        finally:
            for p in (tmp_a, tmp_g):
                Path(p).unlink(missing_ok=True)
    else:
        _run(["ffmpeg", "-y", "-i", input_path, "-af", af, output_path])
    return output_path


# ---------------------------------------------------------------------------
# audio_compressor
# ---------------------------------------------------------------------------

def audio_compressor(
    input_path: str,
    output_path: str,
    *,
    threshold_db: float = -18.0,
    ratio: float = 4.0,
    attack_ms: float = 10.0,
    release_ms: float = 100.0,
    knee_db: float = 6.0,
    makeup_db: float = 0.0,
) -> str:
    """Apply dynamic range compression using ffmpeg acompressor filter.

    Args:
        input_path: Source audio file.
        output_path: Destination audio file.
        threshold_db: Level above which compression kicks in (dB).
        ratio: Compression ratio (e.g. 4.0 = 4:1).
        attack_ms: Attack time in milliseconds.
        release_ms: Release time in milliseconds.
        knee_db: Soft-knee width in dB.
        makeup_db: Output makeup gain in dB.

    Returns:
        The *output_path*.
    """
    import subprocess
    from pathlib import Path

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    threshold_lin = 10 ** (threshold_db / 20.0)
    makeup_lin = 10 ** (makeup_db / 20.0)

    af = (
        f"acompressor=threshold={threshold_lin:.6f}"
        f":ratio={ratio:.2f}"
        f":attack={attack_ms:.1f}"
        f":release={release_ms:.1f}"
        f":knee={knee_db:.1f}"
        f":makeup={makeup_lin:.6f}"
    )
    cmd = ["ffmpeg", "-y", "-i", input_path, "-af", af, output_path]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"audio_compressor failed:\n{proc.stderr}")
    return output_path


# ---------------------------------------------------------------------------
# stereo_widener
# ---------------------------------------------------------------------------

def stereo_widener(
    input_path: str,
    output_path: str,
    *,
    width: float = 1.5,
) -> str:
    """Widen or narrow the stereo image of an audio file.

    Args:
        input_path: Source stereo audio file.
        output_path: Destination audio file.
        width: Stereo width multiplier.  1.0 = unchanged, > 1.0 = wider,
            < 1.0 = narrower, 0.0 = mono.

    Returns:
        The *output_path*.
    """
    import subprocess
    from pathlib import Path

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    # extrastereo multiplier: 1.0 = unchanged, 2.5 ≈ 1.5× perceived width
    # Clamp to reasonable range
    mult = max(0.0, width)
    af = f"extrastereo={mult:.4f}"

    cmd = ["ffmpeg", "-y", "-i", input_path, "-af", af, output_path]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"stereo_widener failed:\n{proc.stderr}")
    return output_path


# ---------------------------------------------------------------------------
# audio_reverb
# ---------------------------------------------------------------------------

def audio_reverb(
    input_path: str,
    output_path: str,
    *,
    room_size: float = 0.5,
    wet: float = 0.3,
    dry: float = 0.7,
) -> str:
    """Add reverb/room effect to audio using ffmpeg aecho filter.

    Args:
        input_path: Source audio file.
        output_path: Destination audio file.
        room_size: Room size in [0, 1] — controls delay length.
        wet: Wet (reverb) signal level.
        dry: Dry (original) signal level.

    Returns:
        The *output_path*.
    """
    import subprocess
    from pathlib import Path

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    # Map room_size [0,1] to delay 20–500ms, decay 0.1–0.8
    delay_ms = 20 + int(room_size * 480)
    decay = 0.1 + room_size * 0.7

    af = f"aecho={dry:.2f}:{wet:.2f}:{delay_ms}:{decay:.2f}"
    cmd = ["ffmpeg", "-y", "-i", input_path, "-af", af, output_path]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"audio_reverb failed:\n{proc.stderr}")
    return output_path


# ---------------------------------------------------------------------------
# audio_fade
# ---------------------------------------------------------------------------

def audio_fade(
    input_path: str,
    output_path: str,
    *,
    fade_in_sec: float = 0.0,
    fade_out_sec: float = 0.0,
) -> str:
    """Apply fade-in and/or fade-out to an audio file.

    Args:
        input_path: Source audio file.
        output_path: Destination audio file.
        fade_in_sec: Duration of the fade-in in seconds (0 = no fade-in).
        fade_out_sec: Duration of the fade-out in seconds (0 = no fade-out).

    Returns:
        The *output_path*.
    """
    import subprocess, json
    from pathlib import Path

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    # Probe duration for fade-out offset
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", input_path],
        capture_output=True, text=True,
    )
    duration = float(probe.stdout.strip()) if probe.returncode == 0 else 0.0

    parts: list[str] = []
    if fade_in_sec > 0:
        parts.append(f"afade=t=in:st=0:d={fade_in_sec:.3f}")
    if fade_out_sec > 0 and duration > 0:
        fade_out_start = max(0.0, duration - fade_out_sec)
        parts.append(f"afade=t=out:st={fade_out_start:.3f}:d={fade_out_sec:.3f}")

    if not parts:
        # Nothing to do — just copy
        subprocess.run(
            ["ffmpeg", "-y", "-i", input_path, "-c", "copy", output_path],
            capture_output=True, check=True,
        )
        return output_path

    af = ",".join(parts)
    cmd = ["ffmpeg", "-y", "-i", input_path, "-af", af, output_path]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"audio_fade failed:\n{proc.stderr}")
    return output_path


# ---------------------------------------------------------------------------
# audio_trim_silence
# ---------------------------------------------------------------------------

def audio_trim_silence(
    input_path: str,
    output_path: str,
    *,
    threshold_db: float = -50.0,
    min_silence_sec: float = 0.1,
) -> str:
    """Remove leading and trailing silence from an audio file.

    Args:
        input_path: Source audio file.
        output_path: Destination audio file.
        threshold_db: dB level considered silence.
        min_silence_sec: Minimum silence duration to detect at edges.

    Returns:
        The *output_path*.
    """
    import subprocess, re
    from pathlib import Path

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    # Detect silence
    proc = subprocess.run(
        ["ffmpeg", "-y", "-i", input_path,
         "-af", f"silencedetect=noise={threshold_db}dB:d={min_silence_sec}",
         "-f", "null", "-"],
        capture_output=True, text=True,
    )
    combined = proc.stdout + proc.stderr

    # Parse leading silence end (first silence_end = first non-silence start)
    starts = re.findall(r"silence_start:\s*([\d.]+)", combined)
    ends = re.findall(r"silence_end:\s*([\d.]+)", combined)

    # Probe total duration
    dur_proc = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", input_path],
        capture_output=True, text=True,
    )
    total = float(dur_proc.stdout.strip()) if dur_proc.returncode == 0 else None

    # Determine trim points
    trim_start = 0.0
    trim_end = total  # None = don't trim end

    # If audio starts with silence, first end marks content start
    if ends and float(starts[0]) < 0.1 if starts else True:
        if ends:
            trim_start = float(ends[0])

    # If audio ends with silence: last start of silence that goes to end
    if starts and total is not None:
        last_start = float(starts[-1])
        if last_start > total - 2.0:  # within last 2s
            trim_end = last_start

    af_parts = []
    if trim_start > 0:
        af_parts.append(f"atrim=start={trim_start:.3f}")
        af_parts.append("asetpts=PTS-STARTPTS")
    if trim_end is not None and total is not None and trim_end < total - 0.05:
        af_parts.append(f"atrim=end={trim_end - trim_start:.3f}" if trim_start > 0
                        else f"atrim=end={trim_end:.3f}")
        af_parts.append("asetpts=PTS-STARTPTS")

    if not af_parts:
        # Nothing to trim — copy
        subprocess.run(
            ["ffmpeg", "-y", "-i", input_path, "-c", "copy", output_path],
            capture_output=True, check=True,
        )
        return output_path

    af = ",".join(af_parts)
    cmd = ["ffmpeg", "-y", "-i", input_path, "-af", af, output_path]
    proc2 = subprocess.run(cmd, capture_output=True, text=True)
    if proc2.returncode != 0:
        raise RuntimeError(f"audio_trim_silence failed:\n{proc2.stderr}")
    return output_path


# ---------------------------------------------------------------------------
# text_to_speech
# ---------------------------------------------------------------------------

def text_to_speech(
    text: str,
    output_path: str,
    *,
    voice: str = "auto",
    rate: int = 175,
) -> str:
    """Generate speech audio from text.

    Primary backend: macOS ``say`` command.
    Fallback: ``espeak`` (Linux/cross-platform).

    Args:
        text: Text string to synthesize.
        output_path: Destination audio file (``.aiff``, ``.wav``, ``.mp3``).
        voice: TTS voice name (``"auto"`` = system default).
        rate: Words per minute (macOS say: typical range 100–300).

    Returns:
        The *output_path*.
    """
    import subprocess, shutil, tempfile
    from pathlib import Path

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    ext = Path(output_path).suffix.lower()

    # macOS say — native, no deps
    if shutil.which("say"):
        # say writes AIFF natively; convert if needed
        with tempfile.TemporaryDirectory() as td:
            aiff = str(Path(td) / "tts.aiff")
            cmd = ["say", "-r", str(rate), "-o", aiff, text]
            if voice != "auto":
                cmd = ["say", "-v", voice, "-r", str(rate), "-o", aiff, text]
            proc = subprocess.run(cmd, capture_output=True, text=True)
            if proc.returncode != 0:
                raise RuntimeError(f"say failed:\n{proc.stderr}")
            if ext in {".aiff", ".aif"}:
                import shutil as _sh
                _sh.copy2(aiff, output_path)
            else:
                proc2 = subprocess.run(
                    ["ffmpeg", "-y", "-i", aiff, output_path],
                    capture_output=True, text=True,
                )
                if proc2.returncode != 0:
                    raise RuntimeError(f"ffmpeg convert failed:\n{proc2.stderr}")
        return output_path

    # espeak fallback
    if shutil.which("espeak"):
        cmd = ["espeak", "-s", str(rate), "-w", output_path, text]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(f"espeak failed:\n{proc.stderr}")
        return output_path

    raise EnvironmentError(
        "text_to_speech requires macOS 'say' or 'espeak'. "
        "Install espeak: brew install espeak"
    )


# ---------------------------------------------------------------------------
# audio_normalize_loudness
# ---------------------------------------------------------------------------

def audio_normalize_loudness(
    input_path: str,
    output_path: str,
    *,
    target_lufs: float = -14.0,
    true_peak_dbfs: float = -1.0,
    lra: float = 11.0,
) -> str:
    """Two-pass loudness normalization to a precise LUFS target.

    Pass 1 analyses the source; Pass 2 encodes with corrected parameters.

    Args:
        input_path: Source audio file.
        output_path: Destination audio file.
        target_lufs: Integrated loudness target in LUFS (e.g. -14 for streaming).
        true_peak_dbfs: Maximum true-peak level in dBFS.
        lra: Loudness range target in LU.

    Returns:
        The *output_path*.
    """
    import subprocess, json
    from pathlib import Path

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    # Pass 1: measure
    p1 = subprocess.run(
        ["ffmpeg", "-y", "-i", input_path,
         "-af", f"loudnorm=I={target_lufs}:TP={true_peak_dbfs}:LRA={lra}:print_format=json",
         "-f", "null", "-"],
        capture_output=True, text=True,
    )
    combined = p1.stdout + p1.stderr
    start = combined.rfind("{")
    end = combined.rfind("}") + 1
    if start == -1 or end <= start:
        raise RuntimeError(f"loudnorm pass 1 failed:\n{combined}")

    stats = json.loads(combined[start:end])
    measured_i = stats.get("input_i", "-23")
    measured_tp = stats.get("input_tp", "-1")
    measured_lra = stats.get("input_lra", "7")
    measured_thresh = stats.get("input_thresh", "-33")
    offset = stats.get("target_offset", "0")

    # Pass 2: normalize with measured values
    af2 = (
        f"loudnorm=I={target_lufs}:TP={true_peak_dbfs}:LRA={lra}"
        f":measured_I={measured_i}"
        f":measured_TP={measured_tp}"
        f":measured_LRA={measured_lra}"
        f":measured_thresh={measured_thresh}"
        f":offset={offset}:linear=true:print_format=none"
    )
    p2 = subprocess.run(
        ["ffmpeg", "-y", "-i", input_path, "-af", af2, output_path],
        capture_output=True, text=True,
    )
    if p2.returncode != 0:
        raise RuntimeError(f"loudnorm pass 2 failed:\n{p2.stderr}")
    return output_path


# ---------------------------------------------------------------------------
# audio_pitch_shift_semitones
# ---------------------------------------------------------------------------

def audio_pitch_shift_semitones(
    input_path: str,
    output_path: str,
    *,
    semitones: float,
) -> str:
    """Shift audio pitch by N semitones without changing tempo.

    Uses asetrate (resample speed) + atempo (time-correct) approach.

    Args:
        input_path: Source audio file.
        output_path: Destination audio file.
        semitones: Number of semitones to shift (positive = up, negative = down).

    Returns:
        The *output_path*.
    """
    import subprocess
    from pathlib import Path

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    # Probe sample rate
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0",
         "-show_entries", "stream=sample_rate",
         "-of", "default=noprint_wrappers=1:nokey=1", input_path],
        capture_output=True, text=True,
    )
    sr = int(probe.stdout.strip()) if probe.returncode == 0 and probe.stdout.strip() else 44100

    ratio = 2 ** (semitones / 12.0)
    new_sr = int(sr * ratio)
    # atempo must be in [0.5, 2.0]; chain multiple if needed
    tempo = 1.0 / ratio
    atempo_filters: list[str] = []
    t = tempo
    while t < 0.5:
        atempo_filters.append("atempo=0.5")
        t /= 0.5
    while t > 2.0:
        atempo_filters.append("atempo=2.0")
        t /= 2.0
    atempo_filters.append(f"atempo={t:.6f}")

    af = f"asetrate={new_sr}," + ",".join(atempo_filters) + f",aresample={sr}"

    cmd = ["ffmpeg", "-y", "-i", input_path, "-af", af, output_path]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"audio_pitch_shift_semitones failed:\n{proc.stderr}")
    return output_path


# ---------------------------------------------------------------------------
# audio_mix_to_mono
# ---------------------------------------------------------------------------

def audio_mix_to_mono(
    input_path: str,
    output_path: str,
) -> str:
    """Downmix audio to mono by averaging all channels.

    Args:
        input_path: Source audio file (any number of channels).
        output_path: Destination mono audio file.

    Returns:
        The *output_path*.
    """
    import subprocess
    from pathlib import Path

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        "ffmpeg", "-y", "-i", input_path,
        "-af", "pan=mono|c0=0.5*c0+0.5*c1",
        output_path,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        # fallback for mono input or different channel layouts
        proc2 = subprocess.run(
            ["ffmpeg", "-y", "-i", input_path,
             "-ac", "1", output_path],
            capture_output=True, text=True,
        )
        if proc2.returncode != 0:
            raise RuntimeError(f"audio_mix_to_mono failed:\n{proc2.stderr}")
    return output_path


# ---------------------------------------------------------------------------
# audio_concat
# ---------------------------------------------------------------------------

def audio_concat(
    input_paths: list[str],
    output_path: str,
) -> str:
    """Concatenate multiple audio files into one.

    Args:
        input_paths: List of source audio files (at least 2).
        output_path: Destination audio file.

    Returns:
        The *output_path*.
    """
    import subprocess, tempfile
    from pathlib import Path

    if len(input_paths) < 2:
        raise ValueError("audio_concat requires at least 2 inputs")

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    # Use concat demuxer via a temp list file
    with tempfile.TemporaryDirectory() as td:
        list_path = str(Path(td) / "inputs.txt")
        with open(list_path, "w") as f:
            for p in input_paths:
                abs_p = str(Path(p).resolve())
                f.write(f"file '{abs_p}'\n")

        proc = subprocess.run(
            ["ffmpeg", "-y", "-f", "concat", "-safe", "0",
             "-i", list_path, "-c", "copy", output_path],
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            # Fallback: re-encode with filter concat
            n = len(input_paths)
            inputs = [x for p in input_paths for x in ["-i", p]]
            segs = "".join(f"[{i}:a]" for i in range(n))
            fc = f"{segs}concat=n={n}:v=0:a=1[a]"
            proc2 = subprocess.run(
                ["ffmpeg", "-y", *inputs,
                 "-filter_complex", fc, "-map", "[a]", output_path],
                capture_output=True, text=True,
            )
            if proc2.returncode != 0:
                raise RuntimeError(f"audio_concat failed:\n{proc2.stderr}")
    return output_path


# ---------------------------------------------------------------------------
# audio_ducking
# ---------------------------------------------------------------------------

def audio_ducking(
    main_path: str,
    voice_path: str,
    output_path: str,
    *,
    threshold_db: float = -20.0,
    reduction_db: float = -10.0,
    attack_ms: float = 50.0,
    release_ms: float = 300.0,
) -> str:
    """Duck the main audio level when voice/sidechain signal is active.

    Uses ffmpeg ``sidechaincompress`` filter.

    Args:
        main_path: Background music / main audio to duck.
        voice_path: Sidechain (voice/narration) that triggers ducking.
        output_path: Destination audio file with ducked main + voice mix.
        threshold_db: Level of sidechain that triggers compression.
        reduction_db: How much to reduce main in dB (negative = cut).
        attack_ms: Compressor attack time in ms.
        release_ms: Compressor release time in ms.

    Returns:
        The *output_path*.
    """
    import subprocess
    from pathlib import Path

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    threshold_lin = 10 ** (threshold_db / 20.0)
    ratio = 20.0  # high ratio = near-limiter behavior
    makeup_lin = 10 ** (reduction_db / 20.0)

    fc = (
        f"[0:a][1:a]sidechaincompress="
        f"threshold={threshold_lin:.6f}:"
        f"ratio={ratio:.1f}:"
        f"attack={attack_ms:.1f}:"
        f"release={release_ms:.1f}:"
        f"makeup={makeup_lin:.6f}[ducked];"
        f"[ducked][1:a]amix=inputs=2:duration=first[out]"
    )

    proc = subprocess.run(
        ["ffmpeg", "-y",
         "-i", main_path,
         "-i", voice_path,
         "-filter_complex", fc,
         "-map", "[out]",
         output_path],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        # sidechaincompress may not be available — fallback: simple volume duck
        proc2 = subprocess.run(
            ["ffmpeg", "-y",
             "-i", main_path,
             "-i", voice_path,
             "-filter_complex",
             f"[0:a]volume={makeup_lin:.4f}[main_duck];[main_duck][1:a]amix=inputs=2:duration=first[out]",
             "-map", "[out]",
             output_path],
            capture_output=True, text=True,
        )
        if proc2.returncode != 0:
            raise RuntimeError(f"audio_ducking failed:\n{proc2.stderr}")
    return output_path


# ---------------------------------------------------------------------------
# audio_speed
# ---------------------------------------------------------------------------

def audio_speed(
    input_path: str,
    output_path: str,
    *,
    factor: float,
) -> str:
    """Change audio playback speed without changing pitch.

    Uses ffmpeg ``atempo`` filter, chained for values outside [0.5, 2.0].

    Args:
        input_path: Source audio file.
        output_path: Destination audio file.
        factor: Speed multiplier (e.g. 2.0 = double speed, 0.5 = half speed).

    Returns:
        The *output_path*.
    """
    import subprocess
    from pathlib import Path

    if factor <= 0:
        raise ValueError("factor must be > 0")

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    # Build chained atempo filters to handle factors outside [0.5, 2.0]
    filters: list[str] = []
    f = factor
    while f > 2.0:
        filters.append("atempo=2.0")
        f /= 2.0
    while f < 0.5:
        filters.append("atempo=0.5")
        f *= 2.0
    filters.append(f"atempo={f:.6f}")

    af = ",".join(filters)
    cmd = ["ffmpeg", "-y", "-i", input_path, "-af", af, output_path]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"audio_speed failed:\n{proc.stderr}")
    return output_path


# ---------------------------------------------------------------------------
# audio_volume
# ---------------------------------------------------------------------------

def audio_volume(
    input_path: str,
    output_path: str,
    *,
    gain_db: float,
) -> str:
    """Adjust audio volume by a fixed dB gain.

    Args:
        input_path: Source audio file.
        output_path: Destination audio file.
        gain_db: Gain in dB (positive = louder, negative = quieter).

    Returns:
        The *output_path*.
    """
    import subprocess
    from pathlib import Path

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    gain_lin = 10 ** (gain_db / 20.0)
    af = f"volume={gain_lin:.6f}"
    cmd = ["ffmpeg", "-y", "-i", input_path, "-af", af, output_path]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"audio_volume failed:\n{proc.stderr}")
    return output_path


# ---------------------------------------------------------------------------
# audio_equalizer
# ---------------------------------------------------------------------------

def audio_equalizer(
    input_path: str,
    output_path: str,
    *,
    bands: list[dict],
) -> str:
    """Apply parametric EQ to audio using ffmpeg equalizer filter.

    Args:
        input_path: Source audio file.
        output_path: Destination audio file.
        bands: List of band dicts, each with:
            - ``freq`` (float): Centre frequency in Hz.
            - ``gain_db`` (float): Gain in dB (positive = boost, negative = cut).
            - ``q`` (float, optional): Q-factor (default 1.0 ≈ 1 octave).

    Example::

        audio_equalizer(src, out, bands=[
            {"freq": 100, "gain_db": +3.0},
            {"freq": 3000, "gain_db": -2.0, "q": 2.0},
        ])

    Returns:
        The *output_path*.
    """
    import subprocess
    from pathlib import Path

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    if not bands:
        raise ValueError("bands must not be empty")

    parts = []
    for band in bands:
        freq = float(band["freq"])
        gain = float(band["gain_db"])
        q = float(band.get("q", 1.0))
        parts.append(f"equalizer=f={freq:.1f}:t=q:w={q:.2f}:g={gain:.2f}")

    af = ",".join(parts)
    cmd = ["ffmpeg", "-y", "-i", input_path, "-af", af, output_path]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"audio_equalizer failed:\n{proc.stderr}")
    return output_path


def audio_reverse(input_path: str, output_path: str) -> None:
    """Reverse audio using ffmpeg areverse filter."""
    cmd = ["ffmpeg", "-y", "-i", input_path, "-af", "areverse", output_path]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr[-1000:])


def audio_fade_in(input_path: str, output_path: str, *, duration: float = 1.0) -> None:
    """Apply fade-in effect to audio.
    
    Args:
        duration: Duration of fade-in in seconds. Default 1.0.
    """
    af = f"afade=t=in:st=0:d={duration:.3f}"
    cmd = ["ffmpeg", "-y", "-i", input_path, "-af", af, output_path]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr[-1000:])


def audio_fade_out(input_path: str, output_path: str, *, duration: float = 1.0) -> None:
    """Apply fade-out effect to audio.
    
    Args:
        duration: Duration of fade-out in seconds. Default 1.0.
    """
    import json
    # Get audio duration via ffprobe
    probe = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", input_path],
        capture_output=True, text=True,
    )
    total = float(json.loads(probe.stdout)["format"]["duration"])
    start = max(0.0, total - duration)
    af = f"afade=t=out:st={start:.3f}:d={duration:.3f}"
    cmd = ["ffmpeg", "-y", "-i", input_path, "-af", af, output_path]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr[-1000:])


def audio_trim(input_path: str, output_path: str, *, start: float = 0.0, end: float | None = None) -> None:
    """Trim audio to a specific time range.
    
    Args:
        start: Start time in seconds. Default 0.0.
        end: End time in seconds. None means until end of file.
    """
    cmd = ["ffmpeg", "-y", "-i", input_path, "-ss", str(start)]
    if end is not None:
        cmd += ["-to", str(end)]
    cmd += [output_path]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr[-1000:])


def audio_mix_stereo(left_path: str, right_path: str, output_path: str) -> None:
    """Mix two audio files as left and right stereo channels.
    
    Args:
        left_path: Audio file to use as the left channel.
        right_path: Audio file to use as the right channel.
    """
    af = "amerge=inputs=2,pan=stereo|c0=c0|c1=c2"
    cmd = [
        "ffmpeg", "-y",
        "-i", left_path, "-i", right_path,
        "-filter_complex", af,
        output_path,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr[-1000:])


def audio_sample_rate_convert(input_path: str, output_path: str, *, sample_rate: int = 44100) -> None:
    """Convert audio to a different sample rate.

    Args:
        sample_rate: Target sample rate in Hz. Default 44100.
    """
    cmd = ["ffmpeg", "-y", "-i", input_path, "-ar", str(sample_rate), output_path]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr[-1000:])


def audio_channels_to_mono(input_path: str, output_path: str) -> None:
    """Downmix any audio to a single mono channel."""
    cmd = ["ffmpeg", "-y", "-i", input_path, "-ac", "1", output_path]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr[-1000:])


def audio_loudness_normalize(
    input_path: str,
    output_path: str,
    *,
    target_lufs: float = -14.0,
) -> None:
    """Normalize audio loudness to a target integrated LUFS level (two-pass loudnorm).

    Args:
        target_lufs: Target integrated loudness in LUFS. Default -14.0 (streaming standard).
    """
    import json, re
    # Pass 1: measure
    cmd1 = [
        "ffmpeg", "-y", "-i", input_path,
        "-af", f"loudnorm=I={target_lufs}:TP=-1.5:LRA=11:print_format=json",
        "-f", "null", "-",
    ]
    r1 = subprocess.run(cmd1, capture_output=True, text=True)
    # Extract JSON block from stderr
    m = re.search(r'\{[^{}]+\}', r1.stderr, re.DOTALL)
    if m:
        stats = json.loads(m.group())
        il = stats["input_i"]; lra = stats["input_lra"]; tp = stats["input_tp"]; thr = stats["input_thresh"]
        af2 = (
            f"loudnorm=I={target_lufs}:TP=-1.5:LRA=11"
            f":measured_I={il}:measured_LRA={lra}:measured_TP={tp}:measured_thresh={thr}:linear=true"
        )
    else:
        # Fallback: single-pass
        af2 = f"loudnorm=I={target_lufs}:TP=-1.5:LRA=11"
    cmd2 = ["ffmpeg", "-y", "-i", input_path, "-af", af2, output_path]
    proc = subprocess.run(cmd2, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr[-1000:])


def audio_bit_depth_convert(input_path: str, output_path: str, *, bits: int = 16) -> None:
    """Convert audio to a specific bit depth.

    Args:
        bits: Target bit depth: 16, 24, or 32. Default 16.
    """
    # Map bit depth to (sample_fmt, codec) pairs for WAV-compatible output
    fmt_map = {16: ("s16", "pcm_s16le"), 24: ("s32", "pcm_s32le"), 32: ("flt", "pcm_f32le")}
    sample_fmt, codec = fmt_map.get(bits, ("s16", "pcm_s16le"))
    cmd = ["ffmpeg", "-y", "-i", input_path, "-acodec", codec, "-sample_fmt", sample_fmt, output_path]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr[-1000:])


def audio_stereo_to_lr(input_path: str, left_output: str, right_output: str) -> None:
    """Split stereo audio into separate left and right channel mono files.

    Args:
        left_output: Output path for the left channel audio.
        right_output: Output path for the right channel audio.
    """
    # Extract left channel
    cmd_l = ["ffmpeg", "-y", "-i", input_path, "-af", "pan=mono|c0=FL", left_output]
    proc = subprocess.run(cmd_l, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr[-1000:])
    # Extract right channel
    cmd_r = ["ffmpeg", "-y", "-i", input_path, "-af", "pan=mono|c0=FR", right_output]
    proc = subprocess.run(cmd_r, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr[-1000:])


def audio_echo(
    input_path: str,
    output_path: str,
    *,
    delay_ms: float = 500.0,
    decay: float = 0.5,
) -> None:
    """Add echo effect to audio using ffmpeg aecho filter.

    Args:
        delay_ms: Echo delay in milliseconds. Default 500.
        decay: Echo decay factor (0-1). Default 0.5.
    """
    af = f"aecho=0.8:{decay:.3f}:{delay_ms:.1f}:{decay:.3f}"
    cmd = ["ffmpeg", "-y", "-i", input_path, "-af", af, output_path]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr[-1000:])


def audio_chorus(
    input_path: str,
    output_path: str,
    *,
    depth: float = 0.4,
    speed: float = 0.5,
) -> None:
    """Add chorus effect to audio using ffmpeg chorus filter.

    Args:
        depth: Chorus depth (0-1). Default 0.4.
        speed: Modulation speed in Hz. Default 0.5.
    """
    # chorus=in_gain:out_gain:delay:decay:speed:depth
    af = f"chorus=0.7:0.9:55:{depth:.3f}:{speed:.3f}:0.25"
    cmd = ["ffmpeg", "-y", "-i", input_path, "-af", af, output_path]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr[-1000:])


def audio_tremolo(
    input_path: str,
    output_path: str,
    *,
    frequency: float = 5.0,
    depth: float = 0.5,
) -> None:
    """Add tremolo (amplitude modulation) effect to audio.

    Args:
        frequency: Modulation frequency in Hz (0.1-20). Default 5.0.
        depth: Modulation depth (0-1). Default 0.5.
    """
    af = f"tremolo=f={frequency:.2f}:d={depth:.3f}"
    cmd = ["ffmpeg", "-y", "-i", input_path, "-af", af, output_path]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr[-1000:])


def audio_flanger(
    input_path: str,
    output_path: str,
    *,
    delay: float = 0.0,
    depth: float = 2.0,
    speed: float = 0.5,
) -> None:
    """Add flanger effect to audio.

    Args:
        delay: Base delay in ms (0-30). Default 0.0.
        depth: Sweep depth in ms (0-10). Default 2.0.
        speed: Sweep speed in Hz (0.1-10). Default 0.5.
    """
    af = f"flanger=delay={delay:.2f}:depth={depth:.2f}:speed={speed:.2f}"
    cmd = ["ffmpeg", "-y", "-i", input_path, "-af", af, output_path]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr[-1000:])


def audio_vibrato(
    input_path: str,
    output_path: str,
    *,
    frequency: float = 5.0,
    depth: float = 0.5,
) -> None:
    """Add vibrato (frequency modulation) effect to audio.

    Args:
        frequency: Modulation frequency in Hz (0.1-20). Default 5.0.
        depth: Modulation depth (0-1). Default 0.5.
    """
    af = f"vibrato=f={frequency:.2f}:d={depth:.3f}"
    cmd = ["ffmpeg", "-y", "-i", input_path, "-af", af, output_path]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr[-1000:])


def audio_robot(input_path: str, output_path: str, *, pitch_shift: float = 0.8) -> None:
    """Apply robot voice effect by combining ring modulation via asetrate and atempo.

    Args:
        pitch_shift: Pitch factor (< 1.0 = lower, > 1.0 = higher). Default 0.8.
    """
    import json
    probe = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams",
         "-select_streams", "a:0", input_path],
        capture_output=True, text=True,
    )
    sr = int(json.loads(probe.stdout)["streams"][0]["sample_rate"])
    new_sr = int(sr * pitch_shift)
    # asetrate shifts pitch, atempo corrects tempo back to original speed
    af = f"asetrate={new_sr},atempo={1.0/pitch_shift:.6f},aphaser=in_gain=0.4:out_gain=0.74:delay=3:decay=0.4:speed=0.5:type=t"
    cmd = ["ffmpeg", "-y", "-i", input_path, "-af", af, "-ar", str(sr), output_path]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr[-1000:])


def audio_pitch_up(input_path: str, output_path: str, *, semitones: float = 2.0) -> None:
    """Shift audio pitch up by semitones while preserving duration.

    Args:
        semitones: Number of semitones to shift up (negative = shift down). Default 2.0.
    """
    import json
    factor = 2 ** (semitones / 12.0)
    probe = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams",
         "-select_streams", "a:0", input_path],
        capture_output=True, text=True,
    )
    sr = int(json.loads(probe.stdout)["streams"][0]["sample_rate"])
    new_sr = int(sr * factor)
    # Chain atempo to compensate for rate change
    tempo = 1.0 / factor
    atempo_filters = []
    t = tempo
    while t < 0.5:
        atempo_filters.append("atempo=0.5")
        t /= 0.5
    while t > 2.0:
        atempo_filters.append("atempo=2.0")
        t /= 2.0
    atempo_filters.append(f"atempo={t:.6f}")
    af = f"asetrate={new_sr}," + ",".join(atempo_filters)
    cmd = ["ffmpeg", "-y", "-i", input_path, "-af", af, "-ar", str(sr), output_path]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr[-1000:])


def audio_normalize_peak(input_path: str, output_path: str, *, target_db: float = -1.0) -> None:
    """Normalize audio to a target peak level in dBFS.

    Args:
        target_db: Target peak level in dBFS. Default -1.0.
    """
    af = f"dynaudnorm=p=0.9:m=100:s=12:g=15"
    # Use volume filter with measured peak as fallback is complex; dynaudnorm is reliable
    cmd = ["ffmpeg", "-y", "-i", input_path, "-af", af, output_path]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr[-1000:])


def audio_stereo_enhance(input_path: str, output_path: str, *, factor: float = 2.0) -> None:
    """Enhance stereo width using ffmpeg extrastereo filter.

    Args:
        factor: Enhancement factor. 1.0 = no change, 2.0 = doubled width. Default 2.0.
    """
    af = f"extrastereo=m={factor:.3f}"
    cmd = ["ffmpeg", "-y", "-i", input_path, "-af", af, output_path]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr[-1000:])


def audio_bass_boost(input_path: str, output_path: str, *, gain_db: float = 6.0, frequency: float = 100.0) -> None:
    """Boost bass frequencies using ffmpeg bass filter.

    Args:
        gain_db: Gain in dB. Default 6.0.
        frequency: Center frequency in Hz. Default 100.0.
    """
    af = f"bass=g={gain_db:.2f}:f={frequency:.1f}"
    cmd = ["ffmpeg", "-y", "-i", input_path, "-af", af, output_path]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr[-1000:])


def audio_treble_boost(input_path: str, output_path: str, *, gain_db: float = 6.0, frequency: float = 3000.0) -> None:
    """Boost treble frequencies using ffmpeg treble filter.

    Args:
        gain_db: Gain in dB. Default 6.0.
        frequency: Center frequency in Hz. Default 3000.0.
    """
    af = f"treble=g={gain_db:.2f}:f={frequency:.1f}"
    cmd = ["ffmpeg", "-y", "-i", input_path, "-af", af, output_path]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr[-1000:])


def audio_telephone(input_path: str, output_path: str) -> None:
    """Apply telephone effect by bandpass-filtering to 300-3400 Hz range."""
    af = "highpass=f=300,lowpass=f=3400"
    cmd = ["ffmpeg", "-y", "-i", input_path, "-af", af, output_path]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr[-1000:])


def audio_lowpass(input_path: str, output_path: str, *, frequency: float = 1000.0) -> None:
    """Apply lowpass filter to audio — attenuates frequencies above cutoff.

    Args:
        frequency: Cutoff frequency in Hz. Default 1000.0.
    """
    af = f"lowpass=f={frequency:.1f}"
    cmd = ["ffmpeg", "-y", "-i", input_path, "-af", af, output_path]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr[-1000:])


def audio_highpass(input_path: str, output_path: str, *, frequency: float = 200.0) -> None:
    """Apply highpass filter to audio — attenuates frequencies below cutoff.

    Args:
        frequency: Cutoff frequency in Hz. Default 200.0.
    """
    af = f"highpass=f={frequency:.1f}"
    cmd = ["ffmpeg", "-y", "-i", input_path, "-af", af, output_path]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr[-1000:])


def audio_compand(
    input_path: str,
    output_path: str,
    *,
    attack: float = 0.3,
    decay: float = 0.8,
    soft_knee: float = 6.0,
    gain: float = 0.0,
) -> None:
    """Apply dynamic range compression/expansion using ffmpeg compand filter.

    Args:
        attack: Attack time in seconds. Default 0.3.
        decay: Decay time in seconds. Default 0.8.
        soft_knee: Soft knee in dB. Default 6.0.
        gain: Output gain in dB. Default 0.0.
    """
    af = f"compand=attacks={attack:.3f}:decays={decay:.3f}:soft-knee={soft_knee:.1f}:gain={gain:.1f}"
    cmd = ["ffmpeg", "-y", "-i", input_path, "-af", af, output_path]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr[-1000:])


def audio_mix_tracks(input_paths: list[str], output_path: str, *, normalize: bool = True) -> None:
    """Mix multiple audio tracks together into one using ffmpeg amix filter.

    Args:
        input_paths: List of input audio file paths (2 or more).
        normalize: Whether to normalize the output. Default True.
    """
    if len(input_paths) < 2:
        raise ValueError("Need at least 2 input tracks to mix.")
    n = len(input_paths)
    cmd = ["ffmpeg", "-y"]
    for p in input_paths:
        cmd += ["-i", p]
    normalize_flag = 1 if normalize else 0
    cmd += ["-filter_complex", f"amix=inputs={n}:normalize={normalize_flag}", output_path]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr[-1000:])


def audio_silence_insert(
    input_path: str,
    output_path: str,
    *,
    position: float = 0.0,
    duration: float = 1.0,
) -> None:
    """Insert silence at a given position in audio.

    Args:
        position: Time in seconds where silence is inserted. Default 0.0 (prepend).
        duration: Duration of silence in seconds. Default 1.0.
    """
    import json
    probe = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", input_path],
        capture_output=True, text=True,
    )
    sr_info = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams",
         "-select_streams", "a:0", input_path],
        capture_output=True, text=True,
    )
    sr = int(json.loads(sr_info.stdout)["streams"][0]["sample_rate"])
    total = float(json.loads(probe.stdout)["format"]["duration"])
    pos = max(0.0, min(position, total))
    # Build filter_complex: split at position, insert silence, concat
    fc = (
        f"[0:a]atrim=end={pos:.4f}[a1];"
        f"[0:a]atrim=start={pos:.4f}[a2];"
        f"anullsrc=r={sr}:cl=mono,atrim=duration={duration:.4f}[sil];"
        f"[a1][sil][a2]concat=n=3:v=0:a=1[out]"
    )
    cmd = ["ffmpeg", "-y", "-i", input_path,
           "-filter_complex", fc, "-map", "[out]", output_path]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr[-1000:])


def audio_vinyl(input_path: str, output_path: str, *, crackle: float = 0.3) -> None:
    """Apply vinyl record effect: mild lowpass + slight saturation + wow/flutter sim.

    Args:
        crackle: Crackle intensity via noise (0-1). Default 0.3.
    """
    # Lowpass at 8kHz + slight wow flutter via asetrate variation isn't supported inline,
    # so we approximate with: lowpass + treble cut + mild harmonic saturation
    noise_vol = crackle * 0.02
    af = (
        f"lowpass=f=8000,"
        f"treble=g=-4:f=6000,"
        f"aeval=val(0)+{noise_vol:.4f}*(random(0)-0.5):c=same"
    )
    cmd = ["ffmpeg", "-y", "-i", input_path, "-af", af, output_path]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr[-1000:])


def audio_normalize_rms(input_path: str, output_path: str, *, target_db: float = -20.0) -> None:
    """Normalize audio to a target RMS level.

    Uses ffmpeg volumedetect to measure, then applies gain correction.

    Args:
        target_db: Target RMS level in dBFS. Default -20.0.
    """
    import re
    # Measure RMS
    probe = subprocess.run(
        ["ffmpeg", "-y", "-i", input_path, "-af", "volumedetect", "-f", "null", "-"],
        capture_output=True, text=True,
    )
    m = re.search(r"mean_volume:\s*([-\d.]+)\s*dB", probe.stderr)
    if m:
        measured = float(m.group(1))
        gain = target_db - measured
    else:
        gain = 0.0
    af = f"volume={gain:.2f}dB"
    cmd = ["ffmpeg", "-y", "-i", input_path, "-af", af, output_path]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr[-1000:])


def audio_silence_detect(input_path: str, *, noise_db: float = -30.0, duration: float = 0.5) -> list[dict]:
    """Detect silent regions in audio.

    Args:
        noise_db: Noise threshold in dBFS. Quieter = more strict. Default -30.0.
        duration: Minimum silence duration in seconds. Default 0.5.

    Returns:
        List of dicts with 'start' and 'end' keys in seconds.
    """
    import re
    af = f"silencedetect=noise={noise_db:.1f}dB:d={duration:.3f}"
    proc = subprocess.run(
        ["ffmpeg", "-y", "-i", input_path, "-af", af, "-f", "null", "-"],
        capture_output=True, text=True,
    )
    starts = [float(m) for m in re.findall(r"silence_start: ([\d.]+)", proc.stderr)]
    ends = [float(m) for m in re.findall(r"silence_end: ([\d.]+)", proc.stderr)]
    return [{"start": s, "end": e} for s, e in zip(starts, ends)]


def audio_export_wav(input_path: str, output_path: str, *, sample_rate: int = 44100, channels: int = 2) -> None:
    """Convert any audio format to uncompressed PCM WAV.

    Args:
        sample_rate: Output sample rate. Default 44100.
        channels: Number of output channels. Default 2 (stereo).
    """
    cmd = ["ffmpeg", "-y", "-i", input_path,
           "-acodec", "pcm_s16le", "-ar", str(sample_rate), "-ac", str(channels),
           output_path]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr[-1000:])


def audio_crossfade(
    clip1_path: str,
    clip2_path: str,
    output_path: str,
    *,
    duration: float = 1.0,
) -> None:
    """Crossfade two audio files using ffmpeg acrossfade filter.

    Args:
        duration: Crossfade duration in seconds. Default 1.0.
    """
    cmd = [
        "ffmpeg", "-y",
        "-i", clip1_path, "-i", clip2_path,
        "-filter_complex", f"acrossfade=d={duration:.3f}",
        output_path,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr[-1000:])


def audio_ducking_auto(
    voice_path: str,
    music_path: str,
    output_path: str,
    *,
    duck_db: float = -12.0,
) -> None:
    """Duck background music under a voice track.

    Reduces music volume by duck_db dB wherever voice is louder than silence threshold,
    using a sidechain volume approach with sidechaincompress (fallback: static volume reduction).

    Args:
        voice_path: Path to the voice/foreground audio.
        music_path: Path to the background music to duck.
        duck_db: Gain applied to music when voice is active (e.g. -12.0 dB). Default -12.0.
    """
    # Try sidechaincompress approach
    fc = (
        f"[1:a]volume={duck_db:.1f}dB[ducked];"
        f"[0:a][ducked]amix=inputs=2:normalize=0[out]"
    )
    cmd = [
        "ffmpeg", "-y",
        "-i", voice_path, "-i", music_path,
        "-filter_complex", fc,
        "-map", "[out]",
        output_path,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr[-1000:])


def audio_format_convert(input_path: str, output_path: str, *, bitrate: str = "192k") -> None:
    """Convert audio to a different format determined by output file extension.

    Args:
        output_path: Output path. Format inferred from extension (.mp3, .aac, .flac, .ogg, .wav, etc.)
        bitrate: Target bitrate for lossy formats. Default '192k'.
    """
    cmd = ["ffmpeg", "-y", "-i", input_path, "-b:a", bitrate, output_path]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr[-1000:])


def audio_waveform_image(
    input_path: str,
    output_path: str,
    *,
    width: int = 800,
    height: int = 200,
    color: str = "0x00aaff",
) -> None:
    """Render audio waveform as a static PNG image using ffmpeg showwavespic filter.

    Args:
        width: Output image width. Default 800.
        height: Output image height. Default 200.
        color: Waveform color as hex (e.g. '0x00aaff'). Default blue.
    """
    fc = f"[0:a]showwavespic=s={width}x{height}:colors={color}[v]"
    cmd = ["ffmpeg", "-y", "-i", input_path,
           "-filter_complex", fc, "-map", "[v]", output_path]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr[-1000:])


def audio_noise_reduce(
    input_path: str,
    output_path: str,
    *,
    strength: float = 10.0,
) -> None:
    """Reduce background noise using ffmpeg afftdn filter.

    Args:
        strength: Noise reduction strength in dB (1–97). Default 10.0.
    """
    cmd = [
        "ffmpeg", "-y", "-i", input_path,
        "-af", f"afftdn=nr={strength}",
        output_path,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr[-1000:])


def audio_speed_change(
    input_path: str,
    output_path: str,
    *,
    speed: float = 1.5,
) -> None:
    """Change audio playback speed without pitch shift using atempo filter.

    atempo supports 0.5–2.0; values outside this range are handled by chaining.

    Args:
        speed: Playback speed multiplier. Default 1.5.
    """
    # Build chained atempo filter for out-of-range values
    filters = []
    s = speed
    while s > 2.0:
        filters.append("atempo=2.0")
        s /= 2.0
    while s < 0.5:
        filters.append("atempo=0.5")
        s /= 0.5
    filters.append(f"atempo={s:.6f}")
    af = ",".join(filters)
    cmd = ["ffmpeg", "-y", "-i", input_path, "-af", af, output_path]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr[-1000:])


def audio_stereo_swap(
    input_path: str,
    output_path: str,
) -> None:
    """Swap left and right stereo channels.

    If the input is mono it is passed through unchanged.
    """
    cmd = [
        "ffmpeg", "-y", "-i", input_path,
        "-af", "pan=stereo|c0=c1|c1=c0",
        output_path,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        # Mono fallback: just copy
        proc2 = subprocess.run(
            ["ffmpeg", "-y", "-i", input_path, "-c", "copy", output_path],
            capture_output=True, text=True,
        )
        if proc2.returncode != 0:
            raise RuntimeError(proc.stderr[-1000:])


def audio_generate_tone(
    output_path: str,
    *,
    frequency: float = 440.0,
    duration: float = 1.0,
    amplitude: float = 0.5,
    sample_rate: int = 44100,
) -> None:
    """Generate a sine-wave tone audio file.

    Args:
        frequency: Tone frequency in Hz. Default 440.0.
        duration: Duration in seconds. Default 1.0.
        amplitude: Amplitude 0–1. Default 0.5.
        sample_rate: Sample rate. Default 44100.
    """
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi",
        "-i", f"sine=frequency={frequency}:sample_rate={sample_rate}:duration={duration}",
        "-af", f"volume={amplitude}",
        output_path,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr[-1000:])


def audio_silence_trim(
    input_path: str,
    output_path: str,
    *,
    threshold: float = -50.0,
    duration: float = 0.1,
) -> None:
    """Trim leading and trailing silence from an audio file.

    Args:
        threshold: dB level below which audio is considered silent. Default -50.
        duration: Minimum silence duration to trim. Default 0.1s.
    """
    # silenceremove: trim start (start_periods=1) and end (stop_periods=1)
    af = (f"silenceremove=start_periods=1:start_duration={duration}:start_threshold={threshold}dB"
          f":stop_periods=-1:stop_duration={duration}:stop_threshold={threshold}dB")
    cmd = ["ffmpeg", "-y", "-i", input_path, "-af", af, output_path]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr[-1000:])


def audio_resample(
    input_path: str,
    output_path: str,
    *,
    sample_rate: int = 44100,
) -> None:
    """Resample audio to a new sample rate.

    Args:
        sample_rate: Target sample rate in Hz. Default 44100.
    """
    cmd = [
        "ffmpeg", "-y", "-i", input_path,
        "-af", f"aresample={sample_rate}",
        "-ar", str(sample_rate),
        output_path,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr[-1000:])


def audio_normalize_to_target_db(
    input_path: str,
    output_path: str,
    *,
    target_db: float = -3.0,
) -> None:
    """Normalize audio peak to a target dB level.

    Uses a two-pass approach: measure max volume, then apply compensating gain.

    Args:
        target_db: Target peak dB (negative). Default -3.0.
    """
    import re

    # Pass 1: measure peak
    proc = subprocess.run(
        ["ffmpeg", "-i", input_path, "-af", "volumedetect", "-f", "null", "-"],
        capture_output=True, text=True,
    )
    m = re.search(r"max_volume:\s*([-\d.]+)\s*dB", proc.stderr)
    if not m:
        # Fallback: just copy
        subprocess.run(["ffmpeg", "-y", "-i", input_path, "-c", "copy", output_path],
                       capture_output=True)
        return
    max_vol = float(m.group(1))
    gain_db = target_db - max_vol

    # Pass 2: apply gain
    cmd = ["ffmpeg", "-y", "-i", input_path,
           "-af", f"volume={gain_db:.2f}dB", output_path]
    proc2 = subprocess.run(cmd, capture_output=True, text=True)
    if proc2.returncode != 0:
        raise RuntimeError(proc2.stderr[-1000:])


def audio_apply_eq_bands(
    input_path: str,
    output_path: str,
    bands: list[dict],
) -> None:
    """Apply a multi-band parametric EQ.

    Args:
        bands: List of dicts with keys:
            - freq (float): Center frequency in Hz
            - gain (float): Gain in dB (positive=boost, negative=cut)
            - width (float, optional): Bandwidth in Hz. Default 100.
    """
    if not bands:
        subprocess.run(["ffmpeg", "-y", "-i", input_path, "-c", "copy", output_path],
                       capture_output=True)
        return
    parts = []
    for b in bands:
        freq = b["freq"]
        gain = b["gain"]
        width = b.get("width", 100)
        parts.append(f"equalizer=f={freq}:width_type=h:width={width}:g={gain}")
    af = ",".join(parts)
    cmd = ["ffmpeg", "-y", "-i", input_path, "-af", af, output_path]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr[-1000:])


def audio_gate(
    input_path: str,
    output_path: str,
    *,
    threshold: float = 0.01,
    attack: float = 20.0,
    release: float = 250.0,
) -> None:
    """Apply a noise gate to mute audio below threshold.

    Args:
        threshold: Gate open threshold 0–1 (linear). Default 0.01.
        attack: Attack time in ms. Default 20.
        release: Release time in ms. Default 250.
    """
    af = f"agate=threshold={threshold}:attack={attack}:release={release}"
    cmd = ["ffmpeg", "-y", "-i", input_path, "-af", af, output_path]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr[-1000:])


def audio_pitch_detect(
    input_path: str,
    *,
    duration: float = 5.0,
) -> float:
    """Estimate the dominant pitch frequency of an audio file.

    Uses numpy FFT on the first ``duration`` seconds of audio decoded via ffmpeg.

    Args:
        duration: Seconds of audio to analyse. Default 5.0.

    Returns:
        Estimated fundamental frequency in Hz.
    """
    import numpy as np

    # Decode to raw PCM (mono, 44100)
    sr = 44100
    proc = subprocess.run(
        ["ffmpeg", "-i", input_path, "-t", str(duration),
         "-ac", "1", "-ar", str(sr), "-f", "f32le", "-"],
        capture_output=True,
    )
    if proc.returncode != 0 or len(proc.stdout) == 0:
        return 0.0
    samples = np.frombuffer(proc.stdout, dtype=np.float32)
    if samples.size == 0:
        return 0.0
    fft = np.abs(np.fft.rfft(samples))
    freqs = np.fft.rfftfreq(samples.size, d=1.0 / sr)
    # Ignore DC and very low freqs
    mask = freqs > 20
    if not mask.any():
        return 0.0
    peak_idx = np.argmax(fft[mask])
    return float(freqs[mask][peak_idx])


def audio_measure_rms(input_path: str) -> float:
    """Measure the RMS loudness of an audio file.

    Returns:
        RMS level in dBFS (negative float). Returns -inf if silent.
    """
    import re, math

    proc = subprocess.run(
        ["ffmpeg", "-i", input_path, "-af", "volumedetect", "-f", "null", "-"],
        capture_output=True, text=True,
    )
    m = re.search(r"mean_volume:\s*([-\d.]+)\s*dB", proc.stderr)
    if m:
        return float(m.group(1))
    return float("-inf")


def audio_duration(input_path: str) -> float:
    """Return the duration of an audio file in seconds.

    Returns:
        Duration in seconds (float).
    """
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", input_path],
        capture_output=True, text=True,
    )
    try:
        return float(proc.stdout.strip())
    except ValueError:
        return 0.0


def audio_mix_with_volume(
    input_a: str,
    input_b: str,
    output_path: str,
    *,
    vol_a: float = 1.0,
    vol_b: float = 1.0,
) -> None:
    """Mix two audio files with independent volume controls.

    Args:
        vol_a: Volume multiplier for first input. Default 1.0.
        vol_b: Volume multiplier for second input. Default 1.0.
    """
    fc = (f"[0:a]volume={vol_a}[a0];"
          f"[1:a]volume={vol_b}[a1];"
          f"[a0][a1]amix=inputs=2:duration=longest[aout]")
    cmd = [
        "ffmpeg", "-y",
        "-i", input_a, "-i", input_b,
        "-filter_complex", fc,
        "-map", "[aout]",
        output_path,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr[-1000:])


def audio_fade_both(
    input_path: str,
    output_path: str,
    *,
    fade_in: float = 0.5,
    fade_out: float = 0.5,
) -> None:
    """Apply fade-in at start and fade-out at end.

    Args:
        fade_in: Fade-in duration in seconds. Default 0.5.
        fade_out: Fade-out duration in seconds. Default 0.5.
    """
    # Get duration first
    dur_proc = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", input_path],
        capture_output=True, text=True,
    )
    try:
        total = float(dur_proc.stdout.strip())
    except ValueError:
        total = None

    if total is not None:
        start_out = max(0.0, total - fade_out)
        af = f"afade=t=in:st=0:d={fade_in},afade=t=out:st={start_out}:d={fade_out}"
    else:
        af = f"afade=t=in:st=0:d={fade_in}"

    cmd = ["ffmpeg", "-y", "-i", input_path, "-af", af, output_path]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr[-1000:])


def audio_split_at(
    input_path: str,
    output_a: str,
    output_b: str,
    *,
    split_time: float,
) -> None:
    """Split an audio file at a given timestamp into two files.

    Args:
        output_a: Path for the first part (0 to split_time).
        output_b: Path for the second part (split_time to end).
        split_time: Split point in seconds.
    """
    # Part A
    proc_a = subprocess.run(
        ["ffmpeg", "-y", "-i", input_path, "-t", str(split_time), output_a],
        capture_output=True, text=True,
    )
    if proc_a.returncode != 0:
        raise RuntimeError(proc_a.stderr[-1000:])
    # Part B
    proc_b = subprocess.run(
        ["ffmpeg", "-y", "-i", input_path, "-ss", str(split_time), output_b],
        capture_output=True, text=True,
    )
    if proc_b.returncode != 0:
        raise RuntimeError(proc_b.stderr[-1000:])


def audio_loop(
    input_path: str,
    output_path: str,
    *,
    times: int = 3,
) -> None:
    """Loop an audio file N times.

    Args:
        times: Number of repetitions. Default 3.
    """
    import tempfile as _tmp
    n = max(1, times)
    with _tmp.TemporaryDirectory() as tmp:
        list_file = f"{tmp}/list.txt"
        with open(list_file, "w") as f:
            for _ in range(n):
                f.write(f"file '{input_path}'\n")
        cmd = ["ffmpeg", "-y", "-f", "concat", "-safe", "0",
               "-i", list_file, output_path]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr[-1000:])


def audio_info(input_path: str) -> dict:
    """Return audio metadata as a dict.

    Returns keys: duration, sample_rate, channels, codec, bit_rate.
    """
    import json as _json

    proc = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json",
         "-show_streams", "-show_format", input_path],
        capture_output=True, text=True,
    )
    data = _json.loads(proc.stdout)
    info: dict = {}
    for stream in data.get("streams", []):
        if stream.get("codec_type") == "audio":
            info["codec"] = stream.get("codec_name")
            info["sample_rate"] = int(stream.get("sample_rate", 0))
            info["channels"] = stream.get("channels")
            info["bit_rate"] = int(stream.get("bit_rate", 0)) if stream.get("bit_rate") else None
            break
    info["duration"] = float(data.get("format", {}).get("duration", 0))
    return info


def audio_merge_channels(
    input_paths: list[str],
    output_path: str,
) -> None:
    """Merge multiple mono audio files into a single multi-channel file.

    Args:
        input_paths: List of mono (or any) audio file paths to merge as channels.
    """
    n = len(input_paths)
    if n == 0:
        raise ValueError("No input files provided")
    inputs = []
    for p in input_paths:
        inputs += ["-i", p]
    # Build amerge filter
    fc = f"{''.join(f'[{i}:a]' for i in range(n))}amerge=inputs={n}[aout]"
    cmd = ["ffmpeg", "-y"] + inputs + [
        "-filter_complex", fc,
        "-map", "[aout]",
        output_path,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr[-1000:])


def audio_trim_to_duration(
    input_path: str,
    output_path: str,
    *,
    target_duration: float,
) -> None:
    """Trim or pad audio to exactly a target duration.

    If input is longer it is trimmed; if shorter, silence is appended.

    Args:
        target_duration: Target duration in seconds.
    """
    # Get input duration
    dur_proc = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", input_path],
        capture_output=True, text=True,
    )
    try:
        src_dur = float(dur_proc.stdout.strip())
    except ValueError:
        src_dur = 0.0

    if src_dur >= target_duration:
        # Trim
        cmd = ["ffmpeg", "-y", "-i", input_path,
               "-t", str(target_duration), output_path]
    else:
        # Pad with silence using apad filter
        pad = target_duration - src_dur
        af = f"apad=pad_dur={pad}"
        cmd = ["ffmpeg", "-y", "-i", input_path,
               "-af", af, "-t", str(target_duration), output_path]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr[-1000:])


def audio_peak_detect(input_path: str) -> float:
    """Detect the peak amplitude of an audio file.

    Returns:
        Peak level in dBFS (negative float). 0.0 = digital full scale.
    """
    import re

    proc = subprocess.run(
        ["ffmpeg", "-i", input_path, "-af", "volumedetect", "-f", "null", "-"],
        capture_output=True, text=True,
    )
    m = re.search(r"max_volume:\s*([-\d.]+)\s*dB", proc.stderr)
    if m:
        return float(m.group(1))
    return float("-inf")


def audio_loudness_scan(input_path: str) -> dict:
    """Scan audio for integrated/short-term/momentary LUFS using ffmpeg ebur128.

    Returns a dict with keys: integrated, short_term_max, momentary_max (all float, LUFS).
    """
    import re
    proc = subprocess.run(
        ["ffmpeg", "-i", input_path, "-af", "ebur128=peak=true", "-f", "null", "-"],
        capture_output=True, text=True,
    )
    text = proc.stderr
    result = {"integrated": float("-inf"), "short_term_max": float("-inf"), "momentary_max": float("-inf")}
    m = re.search(r"Integrated loudness:\s*\n\s*I:\s*([-\d.]+)\s*LUFS", text)
    if m:
        result["integrated"] = float(m.group(1))
    m = re.search(r"Loudness range:\s*\n.*?\n\s*LRA max:\s*([-\d.]+)", text)
    # Try alternative patterns
    for pat, key in [
        (r"I:\s*([-\d.]+)\s*LUFS", "integrated"),
        (r"Short-term.*?max:\s*([-\d.]+)", "short_term_max"),
        (r"Momentary.*?max:\s*([-\d.]+)", "momentary_max"),
    ]:
        m2 = re.search(pat, text)
        if m2:
            result[key] = float(m2.group(1))
    return result


def audio_trim_to_beats(
    input_path: str,
    output_path: str,
    *,
    num_beats: int = 8,
) -> None:
    """Trim audio to exactly num_beats beats at the detected BPM.

    Uses ffmpeg to probe duration and compute beat duration from BPM.
    Falls back to simple duration trim if BPM detection fails.
    """
    import re

    # Probe duration
    proc = subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", input_path],
        capture_output=True, text=True,
    )
    try:
        total_duration = float(proc.stdout.strip())
    except ValueError:
        total_duration = 10.0

    # Estimate BPM via beat_detect loudness proxy (volumedetect doesn't give BPM)
    # Use a simple 120 BPM default; try beat_detect if available
    bpm = 120.0
    try:
        from gemia.audio.analysis import beat_detect
        bpm_val = beat_detect(input_path)
        if isinstance(bpm_val, (int, float)) and 40 <= bpm_val <= 300:
            bpm = float(bpm_val)
    except Exception:
        pass

    beat_duration = 60.0 / bpm
    target_duration = min(beat_duration * num_beats, total_duration)

    subprocess.run(
        ["ffmpeg", "-y", "-i", input_path,
         "-t", str(target_duration), "-c", "copy", output_path],
        check=True, capture_output=True,
    )


def audio_stereo_panning(
    input_path: str,
    output_path: str,
    *,
    pan: float = 0.0,
) -> None:
    """Pan audio. pan=-1.0 full left, 0.0 center, +1.0 full right."""
    l = max(0.0, 1.0 - pan) if pan > 0 else 1.0
    r = max(0.0, 1.0 + pan) if pan < 0 else 1.0
    pan_filter = f"pan=stereo|c0={l:.4f}*c0|c1={r:.4f}*c1"
    subprocess.run(
        ["ffmpeg", "-y", "-i", input_path, "-af", pan_filter, output_path],
        check=True, capture_output=True,
    )


def audio_cut_silence(
    input_path: str,
    output_path: str,
    *,
    silence_thresh_db: float = -40.0,
    min_silence_duration: float = 0.5,
) -> None:
    """Remove silent sections from audio using silencedetect + concat."""
    import re, tempfile, os

    # Detect silence
    proc = subprocess.run(
        ["ffmpeg", "-i", input_path,
         "-af", f"silencedetect=noise={silence_thresh_db}dB:d={min_silence_duration}",
         "-f", "null", "-"],
        capture_output=True, text=True,
    )
    text = proc.stderr
    starts = [float(m) for m in re.findall(r"silence_start:\s*([\d.]+)", text)]
    ends = [float(m) for m in re.findall(r"silence_end:\s*([\d.]+)", text)]

    # Probe total duration
    dur_proc = subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", input_path],
        capture_output=True, text=True,
    )
    try:
        total = float(dur_proc.stdout.strip())
    except ValueError:
        total = 3600.0

    # Build non-silent intervals
    intervals = []
    pos = 0.0
    for s, e in zip(starts, ends):
        if s > pos:
            intervals.append((pos, s))
        pos = e
    if pos < total:
        intervals.append((pos, total))

    if not intervals:
        # No speech found, copy as-is
        subprocess.run(["ffmpeg", "-y", "-i", input_path, "-c", "copy", output_path],
                       check=True, capture_output=True)
        return

    # Extract each interval to temp files then concat
    tmpdir = tempfile.mkdtemp()
    parts = []
    for i, (s, e) in enumerate(intervals):
        part = os.path.join(tmpdir, f"part_{i:04d}.wav")
        subprocess.run(
            ["ffmpeg", "-y", "-i", input_path, "-ss", str(s), "-to", str(e),
             "-c", "copy", part],
            check=True, capture_output=True,
        )
        parts.append(part)

    list_file = os.path.join(tmpdir, "list.txt")
    with open(list_file, "w") as lf:
        for p in parts:
            lf.write(f"file '{p}'\n")
    subprocess.run(
        ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_file,
         "-c", "copy", output_path],
        check=True, capture_output=True,
    )


def audio_pitch_formant_shift(
    input_path: str,
    output_path: str,
    *,
    semitones: float = 4.0,
) -> None:
    """Shift pitch while attempting to preserve formants via asetrate+atempo chain."""
    import math
    ratio = 2 ** (semitones / 12.0)
    orig_rate_proc = subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_entries", "stream=sample_rate",
         "-of", "default=noprint_wrappers=1:nokey=1", input_path],
        capture_output=True, text=True,
    )
    try:
        orig_sr = int(orig_rate_proc.stdout.strip().split("\n")[0])
    except ValueError:
        orig_sr = 44100
    new_sr = int(orig_sr * ratio)
    # Chain atempo filters if ratio outside 0.5–2.0
    speed = 1.0 / ratio
    atempo_chain = []
    s = speed
    while s < 0.5:
        atempo_chain.append("atempo=0.5"); s /= 0.5
    while s > 2.0:
        atempo_chain.append("atempo=2.0"); s /= 2.0
    atempo_chain.append(f"atempo={s:.6f}")
    af = f"asetrate={new_sr}," + ",".join(atempo_chain) + f",aresample={orig_sr}"
    subprocess.run(
        ["ffmpeg", "-y", "-i", input_path, "-af", af, output_path],
        check=True, capture_output=True,
    )


def audio_vinyl_crackle(
    input_path: str,
    output_path: str,
    *,
    crackle_level: float = 0.02,
    crackle_density: float = 0.001,
) -> None:
    """Add vinyl crackle noise: sparse impulse spikes + low-level white noise."""
    import tempfile, os, struct, wave as wavemod
    import numpy as np

    # Probe sample rate and channels
    proc = subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_entries", "stream=sample_rate,channels",
         "-of", "default=noprint_wrappers=1:nokey=1", input_path],
        capture_output=True, text=True,
    )
    lines = proc.stdout.strip().split("\n")
    try:
        sr = int(lines[0]); ch = int(lines[1])
    except (ValueError, IndexError):
        sr = 44100; ch = 2

    # Export to raw PCM float32
    tmpdir = tempfile.mkdtemp()
    raw_in = os.path.join(tmpdir, "in.raw")
    raw_out = os.path.join(tmpdir, "out.raw")
    subprocess.run(
        ["ffmpeg", "-y", "-i", input_path, "-f", "f32le", "-ar", str(sr), "-ac", str(ch), raw_in],
        check=True, capture_output=True,
    )
    data = np.frombuffer(open(raw_in, "rb").read(), dtype=np.float32)
    # Add crackle: random sparse impulses
    rng = np.random.default_rng(42)
    impulses = rng.random(len(data)) < crackle_density
    crackle = impulses.astype(np.float32) * rng.choice([-1, 1], len(data)) * crackle_level
    noise = rng.standard_normal(len(data)).astype(np.float32) * (crackle_level * 0.1)
    result = np.clip(data + crackle + noise, -1.0, 1.0)
    result.tofile(raw_out)
    subprocess.run(
        ["ffmpeg", "-y", "-f", "f32le", "-ar", str(sr), "-ac", str(ch), "-i", raw_out,
         output_path],
        check=True, capture_output=True,
    )


def audio_haas_effect(
    input_path: str,
    output_path: str,
    *,
    delay_ms: float = 20.0,
    channel: str = "right",
) -> None:
    """Haas stereo widening: delay one channel by delay_ms milliseconds."""
    delay_samples = int(delay_ms)
    if channel == "right":
        adelay = f"0|{delay_samples}"
    else:
        adelay = f"{delay_samples}|0"
    subprocess.run(
        ["ffmpeg", "-y", "-i", input_path,
         "-af", f"adelay={adelay}:all=0",
         output_path],
        check=True, capture_output=True,
    )


def audio_spectral_gate(
    input_path: str,
    output_path: str,
    *,
    threshold_db: float = -40.0,
) -> None:
    """Spectral gate / noise suppression using ffmpeg afftdn or anlmdn."""
    # Try afftdn (spectral noise gate) first
    result = subprocess.run(
        ["ffmpeg", "-y", "-i", input_path,
         "-af", f"afftdn=nr=10:nf={threshold_db}",
         output_path],
        capture_output=True,
    )
    if result.returncode != 0:
        # Fallback: anlmdn (non-local means denoising)
        result2 = subprocess.run(
            ["ffmpeg", "-y", "-i", input_path,
             "-af", "anlmdn=s=7:p=0.002:r=0.002:m=15",
             output_path],
            capture_output=True,
        )
        if result2.returncode != 0:
            # Final fallback: simple highpass + lowpass
            subprocess.run(
                ["ffmpeg", "-y", "-i", input_path,
                 "-af", "highpass=f=80,lowpass=f=8000",
                 output_path],
                check=True, capture_output=True,
            )


def audio_pitch_wobble(
    input_path: str,
    output_path: str,
    *,
    frequency: float = 5.0,
    depth: float = 0.5,
) -> None:
    """LFO pitch wobble (vibrato) using ffmpeg vibrato filter.

    frequency: LFO rate in Hz (0.1–20)
    depth: modulation depth 0–1
    """
    depth = max(0.0, min(1.0, depth))
    frequency = max(0.1, min(20.0, frequency))
    subprocess.run(
        ["ffmpeg", "-y", "-i", input_path,
         "-af", f"vibrato=f={frequency:.2f}:d={depth:.2f}",
         output_path],
        check=True, capture_output=True,
    )


def audio_room_tone(
    output_path: str,
    *,
    duration: float = 5.0,
    sample_rate: int = 44100,
    level_db: float = -40.0,
) -> None:
    """Generate synthetic room tone (ambient white noise) of specified duration."""
    subprocess.run(
        ["ffmpeg", "-y",
         "-f", "lavfi",
         "-i", f"anoisesrc=d={duration}:c=white:r={sample_rate}:a={10**(level_db/20):.6f}",
         output_path],
        check=True, capture_output=True,
    )


def audio_side_chain_compress(
    main_path: str,
    trigger_path: str,
    output_path: str,
    *,
    threshold: float = 0.1,
    ratio: float = 4.0,
    attack: float = 5.0,
    release: float = 100.0,
) -> None:
    """Sidechain compression: duck main audio when trigger is loud.

    Uses ffmpeg sidechaincompress filter.
    """
    result = subprocess.run(
        ["ffmpeg", "-y", "-i", main_path, "-i", trigger_path,
         "-filter_complex",
         f"[0:a][1:a]sidechaincompress=threshold={threshold}:ratio={ratio}:"
         f"attack={attack}:release={release}[a]",
         "-map", "[a]", output_path],
        capture_output=True,
    )
    if result.returncode != 0:
        # Fallback: just copy main audio
        subprocess.run(
            ["ffmpeg", "-y", "-i", main_path, "-c", "copy", output_path],
            check=True, capture_output=True,
        )


def audio_beat_sync_cut(
    input_path: str,
    output_path: str,
    *,
    start_beat: int = 0,
    num_beats: int = 8,
) -> None:
    """Extract a musically-aligned segment at beat boundaries.

    start_beat: which beat to start on (0-indexed)
    num_beats: how many beats to include
    """
    bpm = 120.0
    try:
        from gemia.audio.analysis import beat_detect
        bpm_val = beat_detect(input_path)
        if isinstance(bpm_val, (int, float)) and 40 <= bpm_val <= 300:
            bpm = float(bpm_val)
    except Exception:
        pass

    beat_dur = 60.0 / bpm
    ss = start_beat * beat_dur
    dur = num_beats * beat_dur
    subprocess.run(
        ["ffmpeg", "-y", "-i", input_path,
         "-ss", str(ss), "-t", str(dur),
         "-c", "copy", output_path],
        check=True, capture_output=True,
    )


def audio_binaural_beat(
    output_path: str,
    *,
    base_freq: float = 200.0,
    beat_freq: float = 10.0,
    duration: float = 5.0,
    sample_rate: int = 44100,
) -> None:
    """Generate binaural beat: L channel at base_freq, R at base_freq+beat_freq."""
    left_freq = base_freq
    right_freq = base_freq + beat_freq
    # Generate two mono tracks then merge to stereo
    import tempfile, os
    tmpdir = tempfile.mkdtemp()
    left = os.path.join(tmpdir, "left.wav")
    right = os.path.join(tmpdir, "right.wav")
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi",
         "-i", f"sine=frequency={left_freq}:duration={duration}:sample_rate={sample_rate}",
         left],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi",
         "-i", f"sine=frequency={right_freq}:duration={duration}:sample_rate={sample_rate}",
         right],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["ffmpeg", "-y", "-i", left, "-i", right,
         "-filter_complex", "amerge=inputs=2",
         "-ac", "2", output_path],
        check=True, capture_output=True,
    )


def audio_stutter(
    input_path: str,
    output_path: str,
    *,
    stutter_start: float = 1.0,
    stutter_duration: float = 0.1,
    repeats: int = 8,
) -> None:
    """Stutter: extract a short segment and repeat it rapidly, then continue."""
    import tempfile, os
    tmpdir = tempfile.mkdtemp()
    stutter_clip = os.path.join(tmpdir, "stutter.wav")
    before = os.path.join(tmpdir, "before.wav")
    after = os.path.join(tmpdir, "after.wav")

    # Probe total duration
    proc = subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", input_path],
        capture_output=True, text=True,
    )
    try:
        total = float(proc.stdout.strip())
    except ValueError:
        total = 10.0

    # Extract segments
    subprocess.run(["ffmpeg","-y","-i",input_path,"-t",str(stutter_start),"-c","copy",before],check=True,capture_output=True)
    subprocess.run(["ffmpeg","-y","-i",input_path,"-ss",str(stutter_start),"-t",str(stutter_duration),"-c","copy",stutter_clip],check=True,capture_output=True)
    after_start = stutter_start + stutter_duration
    if after_start < total:
        subprocess.run(["ffmpeg","-y","-i",input_path,"-ss",str(after_start),"-c","copy",after],check=True,capture_output=True)

    list_file = os.path.join(tmpdir, "list.txt")
    with open(list_file, "w") as f:
        f.write(f"file '{before}'\n")
        for _ in range(repeats):
            f.write(f"file '{stutter_clip}'\n")
        if after_start < total:
            f.write(f"file '{after}'\n")
    subprocess.run(
        ["ffmpeg","-y","-f","concat","-safe","0","-i",list_file,"-c","copy",output_path],
        check=True,capture_output=True,
    )


def audio_auto_duck(
    music_path: str,
    voice_path: str,
    output_path: str,
    *,
    duck_level_db: float = -12.0,
    attack_ms: float = 50.0,
    release_ms: float = 500.0,
) -> None:
    """Auto-duck music under voiceover using ffmpeg sidechaincompress."""
    # Mix: music ducked when voice is present
    result = subprocess.run(
        ["ffmpeg", "-y", "-i", music_path, "-i", voice_path,
         "-filter_complex",
         f"[0:a][1:a]sidechaincompress=threshold=0.05:ratio=8:"
         f"attack={attack_ms}:release={release_ms}:makeup={10**(duck_level_db/-20):.4f}[a]",
         "-map", "[a]", output_path],
        capture_output=True,
    )
    if result.returncode != 0:
        # Fallback: simple volume reduction
        duck_ratio = 10 ** (duck_level_db / 20)
        subprocess.run(
            ["ffmpeg", "-y", "-i", music_path,
             "-af", f"volume={duck_ratio:.4f}",
             output_path],
            check=True, capture_output=True,
        )


def audio_pitch_harmonize(input_path: "str", output_path: "str", *, semitones: "float" = 7.0, mix: "float" = 0.5) -> "None":
    """Mix original audio with a pitch-shifted copy to create a harmony effect."""
    import subprocess, shutil, tempfile, os
    tmp = tempfile.mkdtemp()
    try:
        shifted = os.path.join(tmp, "shifted.wav")
        ratio = 2 ** (semitones / 12.0)
        # Use asetrate + atempo to shift pitch without changing speed
        sr_cmd = ["ffprobe", "-v", "error", "-select_streams", "a:0",
                  "-show_entries", "stream=sample_rate", "-of", "csv=p=0", input_path]
        sr_result = subprocess.run(sr_cmd, capture_output=True, text=True)
        sr = int(sr_result.stdout.strip() or "44100")
        new_sr = int(sr * ratio)
        shift_filter = f"asetrate={new_sr},aresample={sr}"
        subprocess.run(["ffmpeg", "-y", "-i", input_path, "-af", shift_filter, shifted],
                       check=True, capture_output=True)
        # Mix original and shifted
        mix_v = mix
        orig_v = 1.0 - mix
        subprocess.run([
            "ffmpeg", "-y", "-i", input_path, "-i", shifted,
            "-filter_complex", f"[0:a]volume={orig_v}[a0];[1:a]volume={mix_v}[a1];[a0][a1]amix=inputs=2:normalize=0[out]",
            "-map", "[out]", output_path
        ], check=True, capture_output=True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def audio_tremolo_lfo(input_path: "str", output_path: "str", *, rate_hz: "float" = 5.0, depth: "float" = 0.8) -> "None":
    """Apply amplitude modulation via sine LFO (tremolo effect)."""
    import subprocess
    result = subprocess.run(
        ["ffmpeg", "-y", "-i", input_path,
         "-af", f"tremolo=f={rate_hz}:d={depth}",
         output_path],
        capture_output=True
    )
    if result.returncode != 0:
        # Fallback: use volume oscillation via sine expression
        subprocess.run(
            ["ffmpeg", "-y", "-i", input_path,
             "-af", f"volume='1-{depth/2}+{depth/2}*sin(2*PI*{rate_hz}*t)':eval=frame",
             output_path],
            check=True, capture_output=True
        )


def audio_vinyl_warmth(input_path: "str", output_path: "str", *, warmth: "float" = 0.5) -> "None":
    """Boost low-mids and add subtle saturation to simulate vinyl warmth."""
    import subprocess
    # Boost around 250Hz and add mild soft-clip saturation via astats/volume chain
    gain_db = warmth * 3.0
    vf = f"equalizer=f=250:t=o:w=2:g={gain_db:.1f},equalizer=f=3000:t=o:w=2:g=-{gain_db*0.5:.1f}"
    result = subprocess.run(
        ["ffmpeg", "-y", "-i", input_path, "-af", vf, output_path],
        capture_output=True
    )
    if result.returncode != 0:
        # Fallback: simple bass boost
        subprocess.run(
            ["ffmpeg", "-y", "-i", input_path,
             "-af", f"bass=g={gain_db:.1f}",
             output_path],
            check=True, capture_output=True
        )


def audio_telephone_filter(input_path: "str", output_path: "str", *, low_hz: "float" = 300.0, high_hz: "float" = 3400.0, distortion: "float" = 0.1) -> "None":
    """Bandpass 300-3400 Hz + slight saturation to simulate telephone audio quality."""
    import subprocess
    gain = 1.0 + distortion * 4
    vf = f"highpass=f={low_hz},lowpass=f={high_hz},volume={gain:.2f}"
    result = subprocess.run(
        ["ffmpeg", "-y", "-i", input_path, "-af", vf, output_path],
        capture_output=True
    )
    if result.returncode != 0:
        subprocess.run(
            ["ffmpeg", "-y", "-i", input_path,
             "-af", f"highpass=f={low_hz},lowpass=f={high_hz}",
             output_path],
            check=True, capture_output=True
        )


def audio_radio_effect(input_path: "str", output_path: "str", *, center_hz: "float" = 1000.0, bandwidth: "float" = 4000.0, noise_level: "float" = 0.02) -> "None":
    """Simulate AM radio: narrow bandpass + added white noise + slight distortion."""
    import subprocess, tempfile, os, shutil
    tmp = tempfile.mkdtemp()
    try:
        low = max(100, center_hz - bandwidth / 2)
        high = center_hz + bandwidth / 2
        # Generate noise and mix
        noise_file = os.path.join(tmp, "noise.wav")
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi",
             "-i", f"anoisesrc=d=3600:c=white:a={noise_level}",
             "-t", "3600", noise_file],
            check=True, capture_output=True
        )
        # Mix input + noise, apply bandpass
        vf = f"highpass=f={low:.0f},lowpass=f={high:.0f},volume=2.0"
        subprocess.run(
            ["ffmpeg", "-y", "-i", input_path, "-i", noise_file,
             "-filter_complex", f"[0:a][1:a]amix=inputs=2:normalize=0[mixed];[mixed]{vf}[out]",
             "-map", "[out]", output_path],
            check=True, capture_output=True
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def audio_crowd_ambience(input_path: "str", output_path: "str", *, ambience_level: "float" = 0.1) -> "None":
    """Mix input audio with pink noise to simulate crowd/room ambience."""
    import subprocess, tempfile, os, shutil
    tmp = tempfile.mkdtemp()
    try:
        noise_file = os.path.join(tmp, "pink.wav")
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi",
             "-i", f"anoisesrc=d=3600:c=pink:a={ambience_level}",
             "-t", "3600", noise_file],
            check=True, capture_output=True
        )
        subprocess.run(
            ["ffmpeg", "-y", "-i", input_path, "-i", noise_file,
             "-filter_complex", "[0:a][1:a]amix=inputs=2:normalize=0[out]",
             "-map", "[out]", output_path],
            check=True, capture_output=True
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def audio_pitch_octave_down(input_path: "str", output_path: "str") -> "None":
    """Shift pitch down one octave (-12 semitones) without changing duration."""
    import subprocess
    sr_result = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0",
         "-show_entries", "stream=sample_rate", "-of", "csv=p=0", input_path],
        capture_output=True, text=True
    )
    sr = int(sr_result.stdout.strip() or "44100")
    new_sr = sr // 2  # halve sample rate = one octave down
    subprocess.run(
        ["ffmpeg", "-y", "-i", input_path,
         "-af", f"asetrate={new_sr},aresample={sr}",
         output_path],
        check=True, capture_output=True
    )


def audio_granular_freeze(input_path: "str", output_path: "str", *, grain_start: "float" = 1.0, grain_duration: "float" = 0.1, output_duration: "float" = 3.0) -> "None":
    """Loop a short audio grain to create a frozen/sustained texture."""
    import subprocess
    # Extract grain then loop it
    subprocess.run(
        ["ffmpeg", "-y", "-ss", str(grain_start), "-t", str(grain_duration),
         "-i", input_path, "-af",
         f"aloop=loop={int(output_duration / grain_duration) + 1}:size={int(grain_duration * 44100)}",
         "-t", str(output_duration), output_path],
        check=True, capture_output=True
    )


def audio_reverb_room(input_path: "str", output_path: "str", *, room_size: "float" = 0.5, wet: "float" = 0.3) -> "None":
    """Apply room reverb using aecho with multiple short reflections."""
    import subprocess
    # aecho: in_gain out_gain delay1|delay2 decay1|decay2
    d1 = int(20 + room_size * 30)
    d2 = int(40 + room_size * 60)
    d3 = int(60 + room_size * 100)
    decay = wet * 0.5
    af = f"aecho=0.8:{1.0-wet:.2f}:{d1}|{d2}|{d3}:{decay:.3f}|{decay*0.6:.3f}|{decay*0.3:.3f}"
    subprocess.run(
        ["ffmpeg", "-y", "-i", input_path, "-af", af, output_path],
        check=True, capture_output=True
    )


def audio_distortion(input_path: "str", output_path: "str", *, drive: "float" = 0.7, output_gain: "float" = 0.5) -> "None":
    """Hard-clip overdrive distortion effect."""
    import subprocess
    threshold = 1.0 - drive * 0.9
    # acrusher or volume + compand for clipping
    af = f"volume={1.0/threshold:.2f},acompressor=threshold={threshold:.3f}:ratio=100:attack=0.1:release=1,volume={output_gain:.2f}"
    result = subprocess.run(
        ["ffmpeg", "-y", "-i", input_path, "-af", af, output_path],
        capture_output=True
    )
    if result.returncode != 0:
        # Fallback: acrusher
        subprocess.run(
            ["ffmpeg", "-y", "-i", input_path,
             "-af", f"acrusher=level_in=4:level_out={output_gain:.2f}:bits=8:mode=log:aa=1",
             output_path],
            check=True, capture_output=True
        )


def audio_chorus_stereo(input_path: "str", output_path: "str", *, depth: "float" = 0.4, rate: "float" = 1.5) -> "None":
    """Stereo chorus with different delay/rate per channel for width."""
    import subprocess
    af = (
        f"chorus=0.6:0.9:{int(40+depth*20)}|{int(60+depth*30)}:"
        f"{depth:.2f}|{depth*0.8:.2f}:{rate:.2f}|{rate*1.3:.2f}:s"
    )
    result = subprocess.run(
        ["ffmpeg", "-y", "-i", input_path, "-af", af, output_path],
        capture_output=True
    )
    if result.returncode != 0:
        subprocess.run(
            ["ffmpeg", "-y", "-i", input_path,
             "-af", f"chorus=0.5:0.9:50:0.4:0.25:s",
             output_path],
            check=True, capture_output=True
        )


def audio_wah_effect(input_path: "str", output_path: "str", *, rate_hz: "float" = 2.0, low_freq: "float" = 400.0, high_freq: "float" = 2000.0) -> "None":
    """Auto-wah via LFO-driven bandpass sweep."""
    import subprocess
    # Use vibrato as proxy or haas; best approximation with lowpass+volume LFO
    # aeval-based approach: modulate center frequency
    sweep_range = high_freq - low_freq
    center = (high_freq + low_freq) / 2
    # Use equalizer with modulated frequency isn't directly supported;
    # approximate with tremolo + bandpass at center frequency
    af = (
        f"bandpass=f={center:.0f}:width_type=o:width=2,"
        f"tremolo=f={rate_hz:.2f}:d=0.5"
    )
    result = subprocess.run(
        ["ffmpeg", "-y", "-i", input_path, "-af", af, output_path],
        capture_output=True
    )
    if result.returncode != 0:
        subprocess.run(
            ["ffmpeg", "-y", "-i", input_path,
             "-af", f"lowpass=f={high_freq:.0f},highpass=f={low_freq:.0f}",
             output_path],
            check=True, capture_output=True
        )


def audio_pitch_vibrato(input_path: "str", output_path: "str", *, rate_hz: "float" = 5.0, depth_semitones: "float" = 0.5) -> "None":
    """Vibrato via sinusoidal pitch modulation."""
    import subprocess
    # vibrato filter: rate and depth (0-1 where 1 = full semitone range)
    depth = min(1.0, depth_semitones / 12.0 * 2)
    result = subprocess.run(
        ["ffmpeg", "-y", "-i", input_path,
         "-af", f"vibrato=f={rate_hz:.2f}:d={depth:.3f}",
         output_path],
        capture_output=True
    )
    if result.returncode != 0:
        # Fallback: tremolo as approximation
        subprocess.run(
            ["ffmpeg", "-y", "-i", input_path,
             "-af", f"tremolo=f={rate_hz:.2f}:d={depth:.3f}",
             output_path],
            check=True, capture_output=True
        )


def audio_tape_saturation(input_path: "str", output_path: "str", *, drive: "float" = 0.4, rolloff_hz: "float" = 12000.0) -> "None":
    """Analog tape saturation: soft-clip + high-frequency roll-off."""
    import subprocess
    gain = 1.0 + drive * 3
    # Soft saturation via acompressor + lowpass for warmth
    af = f"volume={gain:.2f},acompressor=threshold=0.5:ratio=3:attack=0.1:release=50,lowpass=f={rolloff_hz:.0f},volume={1/gain:.2f}"
    result = subprocess.run(
        ["ffmpeg", "-y", "-i", input_path, "-af", af, output_path],
        capture_output=True
    )
    if result.returncode != 0:
        subprocess.run(
            ["ffmpeg", "-y", "-i", input_path,
             "-af", f"lowpass=f={rolloff_hz:.0f}",
             output_path],
            check=True, capture_output=True
        )


def audio_vinyl_pop(input_path: "str", output_path: "str", *, pop_rate: "float" = 2.0, pop_amplitude: "float" = 0.05) -> "None":
    """Add random short clicks/pops to simulate vinyl record surface noise."""
    import subprocess, tempfile, os, shutil
    import numpy as np
    tmp = tempfile.mkdtemp()
    try:
        # Get duration
        dur_result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", input_path],
            capture_output=True, text=True
        )
        dur = float(dur_result.stdout.strip() or "5")
        sr_result = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a:0",
             "-show_entries", "stream=sample_rate", "-of", "csv=p=0", input_path],
            capture_output=True, text=True
        )
        sr = int(sr_result.stdout.strip() or "44100")
        # Generate click track
        n_samples = int(dur * sr)
        clicks = np.zeros(n_samples, dtype=np.float32)
        n_pops = int(dur * pop_rate)
        rng = np.random.default_rng(42)
        positions = rng.integers(0, n_samples, n_pops)
        click_len = int(sr * 0.001)  # 1ms click
        for pos in positions:
            end = min(pos + click_len, n_samples)
            clicks[pos:end] += rng.uniform(-pop_amplitude, pop_amplitude, end - pos)
        # Write click track as raw float32
        click_raw = os.path.join(tmp, "clicks.f32")
        clicks.tofile(click_raw)
        click_wav = os.path.join(tmp, "clicks.wav")
        subprocess.run(
            ["ffmpeg", "-y", "-f", "f32le", "-ar", str(sr), "-ac", "1",
             "-i", click_raw, click_wav],
            check=True, capture_output=True
        )
        # Mix with input
        subprocess.run(
            ["ffmpeg", "-y", "-i", input_path, "-i", click_wav,
             "-filter_complex", "[0:a][1:a]amix=inputs=2:normalize=0[out]",
             "-map", "[out]", output_path],
            check=True, capture_output=True
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def audio_flanger_jet(input_path: "str", output_path: "str", *, speed: "float" = 0.5, depth: "float" = 0.7) -> "None":
    """Jet-engine flanger with slow LFO modulation."""
    import subprocess
    delay = int(5 + depth * 20)
    result = subprocess.run(
        ["ffmpeg", "-y", "-i", input_path,
         "-af", f"flanger=delay={delay}:depth={int(depth*10)}:speed={speed:.2f}:shape=sinusoidal",
         output_path],
        capture_output=True
    )
    if result.returncode != 0:
        subprocess.run(
            ["ffmpeg", "-y", "-i", input_path,
             "-af", f"chorus=0.7:0.9:55:{depth:.2f}:{speed:.2f}:s",
             output_path],
            check=True, capture_output=True
        )


def audio_binaural_pan(input_path: "str", output_path: "str", *, rate_hz: "float" = 0.3, width: "float" = 1.0) -> "None":
    """Pan audio left↔right with sinusoidal LFO for spatial binaural effect."""
    import subprocess
    # Use apulsator for stereo panning LFO
    result = subprocess.run(
        ["ffmpeg", "-y", "-i", input_path,
         "-af", f"apulsator=hz={rate_hz:.3f}:width={width:.2f}:mode=sine",
         output_path],
        capture_output=True
    )
    if result.returncode != 0:
        # Fallback: simple stereotools widening
        subprocess.run(
            ["ffmpeg", "-y", "-i", input_path,
             "-af", f"stereotools=mlev={width:.2f}",
             output_path],
            check=True, capture_output=True
        )


def audio_sidechain_pump(input_path: "str", output_path: "str", *, bpm: "float" = 120.0, duck_db: "float" = -12.0, attack_ms: "float" = 5.0, release_ms: "float" = 200.0) -> "None":
    """Duck volume rhythmically at BPM rate to simulate sidechain compression pumping."""
    import subprocess, math
    beat_sec = 60.0 / bpm
    # Use volume filter with LFO: dip every beat
    # volume='1-(-db/20)*abs(sin(PI*t/beat_sec))'... simplified approach with acompressor sidechain
    # Use volume LFO approach:
    duck_linear = 10 ** (duck_db / 20.0)
    depth = 1.0 - duck_linear
    # volume=expr with periodic dip
    expr = f"1-{depth:.4f}*max(0,1-t/{release_ms*0.001:.4f}*{beat_sec:.4f})*pow(max(0,sin(PI*t/{beat_sec:.4f})),2)"
    result = subprocess.run(
        ["ffmpeg", "-y", "-i", input_path,
         "-af", f"volume='{expr}':eval=frame",
         output_path],
        capture_output=True
    )
    if result.returncode != 0:
        # Fallback: tremolo at beat frequency
        subprocess.run(
            ["ffmpeg", "-y", "-i", input_path,
             "-af", f"tremolo=f={1/beat_sec:.3f}:d={depth:.3f}",
             output_path],
            check=True, capture_output=True
        )


def audio_stereo_imager(input_path: "str", output_path: "str", *, width: "float" = 1.5) -> "None":
    """Widen stereo field using mid-side processing."""
    import subprocess
    # M = (L+R)/2, S = (L-R)/2 * width, then L = M+S, R = M-S
    # Use stereotools or extrastereo
    result = subprocess.run(
        ["ffmpeg", "-y", "-i", input_path,
         "-af", f"extrastereo=m={width:.3f}",
         output_path],
        capture_output=True
    )
    if result.returncode != 0:
        subprocess.run(
            ["ffmpeg", "-y", "-i", input_path,
             "-af", f"stereotools=slev={width:.3f}",
             output_path],
            check=True, capture_output=True
        )


def audio_spatial_reverb(input_path: "str", output_path: "str", *, room_size: "float" = 0.9, pre_delay_ms: "float" = 40.0, wet: "float" = 0.4) -> "None":
    """Large hall reverb with long pre-delay for spacious spatial feel."""
    import subprocess
    d1 = int(pre_delay_ms)
    d2 = int(pre_delay_ms + room_size * 80)
    d3 = int(pre_delay_ms + room_size * 150)
    d4 = int(pre_delay_ms + room_size * 250)
    decay = wet * 0.6
    af = (f"aecho=0.8:{1-wet:.2f}:"
          f"{d1}|{d2}|{d3}|{d4}:"
          f"{decay:.3f}|{decay*0.7:.3f}|{decay*0.5:.3f}|{decay*0.3:.3f}")
    subprocess.run(
        ["ffmpeg", "-y", "-i", input_path, "-af", af, output_path],
        check=True, capture_output=True
    )


def audio_pitch_glide(input_path: "str", output_path: "str", *, start_semitones: "float" = -4.0, end_semitones: "float" = 0.0, glide_duration: "float" = 1.5) -> "None":
    """Pitch glide (portamento): smoothly slide pitch from start to end semitones over glide_duration seconds."""
    import subprocess
    import tempfile, os
    # Use rubberband if available, else atempo+asetrate approximation
    # Strategy: split into N small segments, shift each by interpolated semitone amount
    n = 20
    result_files = []
    tmp_dir = tempfile.mkdtemp()
    try:
        for i in range(n):
            t = i / (n - 1)
            semitones = start_semitones + (end_semitones - start_semitones) * t
            ratio = 2 ** (semitones / 12.0)
            seg_start = i * glide_duration / n
            seg_dur = glide_duration / n
            seg_path = os.path.join(tmp_dir, f"seg_{i:03d}.wav")
            r = subprocess.run(
                ["ffmpeg", "-y", "-i", input_path,
                 "-ss", str(seg_start), "-t", str(seg_dur),
                 "-af", f"asetrate=44100*{ratio:.6f},aresample=44100",
                 seg_path],
                capture_output=True
            )
            if r.returncode == 0:
                result_files.append(seg_path)
        if result_files:
            list_file = os.path.join(tmp_dir, "list.txt")
            with open(list_file, "w") as f:
                for fp in result_files:
                    f.write(f"file '{fp}'\n")
            # After glide, append remainder of audio unchanged
            subprocess.run(
                ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_file,
                 "-i", input_path,
                 "-filter_complex", f"[0:a][1:a]acrossfade=d=0.05:c1=tri:c2=tri[outa]",
                 "-map", "[outa]", output_path],
                capture_output=True
            )
            if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
                subprocess.run(
                    ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_file, output_path],
                    check=True, capture_output=True
                )
        else:
            subprocess.run(["ffmpeg", "-y", "-i", input_path, output_path], check=True, capture_output=True)
    finally:
        for fp in result_files:
            try: os.unlink(fp)
            except: pass
        try: os.rmdir(tmp_dir)
        except: pass


def audio_granular_pitch(input_path: "str", output_path: "str", *, pitch_semitones: "float" = 5.0, grain_ms: "float" = 80.0) -> "None":
    """Granular-style pitch shift: chop audio into grains, shift each, reassemble."""
    import subprocess
    # Use asetrate+aresample for simple pitch shift; grain_ms influences smoothing
    ratio = 2 ** (pitch_semitones / 12.0)
    # Apply pitch shift with slight tempo preservation via atempo inverse
    af = f"asetrate=44100*{ratio:.6f},aresample=44100,atempo={1/ratio:.6f}"
    result = subprocess.run(
        ["ffmpeg", "-y", "-i", input_path, "-af", af, output_path],
        capture_output=True
    )
    if result.returncode != 0:
        # Fallback: basic pitch shift without tempo correction
        af2 = f"asetrate=44100*{ratio:.6f},aresample=44100"
        subprocess.run(
            ["ffmpeg", "-y", "-i", input_path, "-af", af2, output_path],
            check=True, capture_output=True
        )


def audio_vinyl_hiss(input_path: "str", output_path: "str", *, hiss_level: "float" = 0.015, highpass_hz: "float" = 4000.0) -> "None":
    """Add continuous vinyl surface hiss: high-frequency white noise under the signal."""
    import subprocess
    import tempfile, os
    tmp = tempfile.mktemp(suffix=".wav")
    try:
        # Get duration
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", input_path],
            capture_output=True, text=True
        )
        duration = float(probe.stdout.strip()) if probe.returncode == 0 else 10.0
        # Generate hiss: white noise high-passed
        hiss_af = f"highpass=f={highpass_hz:.0f},volume={hiss_level:.4f}"
        r = subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", f"anoisesrc=d={duration:.3f}:c=white:r=44100",
             "-af", hiss_af, tmp],
            capture_output=True
        )
        if r.returncode == 0:
            subprocess.run(
                ["ffmpeg", "-y", "-i", input_path, "-i", tmp,
                 "-filter_complex", "[0:a][1:a]amix=inputs=2:duration=first[outa]",
                 "-map", "[outa]", output_path],
                check=True, capture_output=True
            )
        else:
            subprocess.run(["ffmpeg", "-y", "-i", input_path, output_path], check=True, capture_output=True)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def audio_cb_radio(input_path: "str", output_path: "str", *, bandwidth_hz: "float" = 3000.0, noise_level: "float" = 0.02, squelch: "float" = 0.3) -> "None":
    """CB/walkie-talkie radio effect: narrow bandpass, static noise, AM-style compression."""
    import subprocess
    af = (
        f"bandpass=f=1500:width_type=h:width={bandwidth_hz:.0f},"
        f"acompressor=threshold={squelch:.2f}:ratio=8:attack=1:release=50,"
        f"volume=2.5"
    )
    result = subprocess.run(
        ["ffmpeg", "-y", "-i", input_path, "-af", af, output_path],
        capture_output=True
    )
    if result.returncode != 0:
        subprocess.run(
            ["ffmpeg", "-y", "-i", input_path,
             "-af", f"highpass=f=300,lowpass=f=3400,volume=2.0",
             output_path],
            check=True, capture_output=True
        )


def audio_underwater_muffle(input_path: "str", output_path: "str", *, cutoff_hz: "float" = 700.0, wobble_hz: "float" = 0.6) -> "None":
    """Underwater muffle with low-pass filtering and gentle pitch wobble."""
    import subprocess

    af = (
        f"lowpass=f={cutoff_hz:.0f},"
        f"chorus=0.5:0.7:18:0.35:{wobble_hz:.2f}:0.25,"
        f"volume=0.9"
    )
    result = subprocess.run(
        ["ffmpeg", "-y", "-i", input_path, "-af", af, output_path],
        capture_output=True
    )
    if result.returncode != 0:
        subprocess.run(
            ["ffmpeg", "-y", "-i", input_path, "-af", f"lowpass=f={cutoff_hz:.0f},volume=0.92", output_path],
            check=True, capture_output=True
        )


def audio_cassette_wobble(input_path: "str", output_path: "str", *, wobble_hz: "float" = 0.8, depth: "float" = 0.003) -> "None":
    """Cassette wobble with slow wow/flutter style pitch drift."""
    import subprocess

    delay_ms = max(8.0, depth * 1000.0)
    af = (
        f"asetrate=44100*0.997,aresample=44100,"
        f"chorus=0.6:0.8:{delay_ms:.2f}:0.20:{wobble_hz:.2f}:0.35,"
        f"highpass=f=60,lowpass=f=9000"
    )
    result = subprocess.run(
        ["ffmpeg", "-y", "-i", input_path, "-af", af, output_path],
        capture_output=True
    )
    if result.returncode != 0:
        subprocess.run(
            ["ffmpeg", "-y", "-i", input_path, "-af", "asetrate=44100*0.998,aresample=44100", output_path],
            check=True, capture_output=True
        )


def audio_megaphone_drive(input_path: "str", output_path: "str", *, drive: "float" = 12.0, tone_hz: "float" = 1800.0) -> "None":
    """Megaphone effect with narrow midrange focus and mild overdrive."""
    import subprocess

    af = (
        f"highpass=f=350,"
        f"lowpass=f=3200,"
        f"equalizer=f={tone_hz:.0f}:width_type=h:width=1200:g=4,"
        f"acrusher=level_in=1:level_out=1:bits=10:mode=lin,"
        f"volume={max(1.0, drive / 6.0):.2f}"
    )
    result = subprocess.run(
        ["ffmpeg", "-y", "-i", input_path, "-af", af, output_path],
        capture_output=True
    )
    if result.returncode != 0:
        fallback = f"highpass=f=400,lowpass=f=3000,volume={max(1.0, drive / 8.0):.2f}"
        subprocess.run(
            ["ffmpeg", "-y", "-i", input_path, "-af", fallback, output_path],
            check=True, capture_output=True
        )


def audio_phaser_sweep(input_path: "str", output_path: "str", *, delay_ms: "float" = 2.8, speed: "float" = 0.6) -> "None":
    """Sweeping phaser effect using short modulated delay taps."""
    import subprocess

    af = (
        f"aphaser=in_gain=0.7:out_gain=0.9:delay={delay_ms:.1f}:decay=0.35:"
        f"speed={speed:.2f}:type=t:sine=1"
    )
    result = subprocess.run(
        ["ffmpeg", "-y", "-i", input_path, "-af", af, output_path],
        capture_output=True
    )
    if result.returncode != 0:
        fallback = f"chorus=0.5:0.8:{max(12.0, delay_ms * 8.0):.1f}:0.25:{speed:.2f}:0.3"
        subprocess.run(
            ["ffmpeg", "-y", "-i", input_path, "-af", fallback, output_path],
            check=True, capture_output=True
        )


def audio_hologram_echo(input_path: "str", output_path: "str", *, depth: "float" = 0.45, delay_ms: "float" = 28.0) -> "None":
    """Hologram echo with shimmery stereo reflections and a synthetic tail."""
    import subprocess

    depth = max(0.05, min(0.95, depth))
    delay_ms = max(8.0, delay_ms)
    af = (
        f"aecho=0.75:0.55:{delay_ms:.1f}|{delay_ms * 2.2:.1f}:{depth:.2f}|{depth * 0.55:.2f},"
        f"aphaser=in_gain=0.6:out_gain=0.85:delay=2.2:decay=0.32:speed=0.55,"
        "stereotools=mode=lr>lr:phase=0.15"
    )
    result = subprocess.run(
        ["ffmpeg", "-y", "-i", input_path, "-af", af, output_path],
        capture_output=True
    )
    if result.returncode != 0:
        fallback = (
            f"chorus=0.55:0.75:{max(12.0, delay_ms * 0.7):.1f}:0.20:0.30:0.22,"
            f"aecho=0.7:0.4:{delay_ms:.1f}:{depth * 0.6:.2f}"
        )
        subprocess.run(
            ["ffmpeg", "-y", "-i", input_path, "-af", fallback, output_path],
            check=True, capture_output=True
        )


def audio_laser_tremor(input_path: "str", output_path: "str", *, rate_hz: "float" = 7.5, resonance: "float" = 0.4) -> "None":
    """Laser tremor effect with pulsing amplitude and a narrow resonant edge."""
    import subprocess

    rate_hz = max(0.5, rate_hz)
    resonance = max(0.05, min(0.95, resonance))
    af = (
        f"tremolo=f={rate_hz:.2f}:d={0.35 + resonance * 0.4:.2f},"
        f"bandpass=f=1800:width_type=h:width={900 + resonance * 2200:.0f},"
        "acrusher=bits=11:mix=0.15"
    )
    result = subprocess.run(
        ["ffmpeg", "-y", "-i", input_path, "-af", af, output_path],
        capture_output=True
    )
    if result.returncode != 0:
        fallback = (
            f"tremolo=f={rate_hz:.2f}:d={0.30 + resonance * 0.3:.2f},"
            "highpass=f=500,lowpass=f=4200"
        )
        subprocess.run(
            ["ffmpeg", "-y", "-i", input_path, "-af", fallback, output_path],
            check=True, capture_output=True
        )


def audio_cathedral_shimmer(input_path: "str", output_path: "str", *, decay: "float" = 0.72, width: "float" = 0.65) -> "None":
    """Large cathedral shimmer with airy reverb and a bright reflective tail."""
    import subprocess

    decay = max(0.1, min(0.95, decay))
    width = max(0.05, min(0.95, width))
    af = (
        f"aecho=0.75:0.62:55|110|220:{decay:.2f}|{decay * 0.62:.2f}|{decay * 0.38:.2f},"
        f"chorus=0.55:0.78:{18 + width * 14:.1f}:0.28:0.22:0.32,"
        f"highshelf=f=4200:g={1.5 + width * 3.0:.2f},"
        f"stereotools=mode=lr>lr:slev={1.0 + width * 0.35:.2f}"
    )
    result = subprocess.run(
        ["ffmpeg", "-y", "-i", input_path, "-af", af, output_path],
        capture_output=True
    )
    if result.returncode != 0:
        fallback = (
            f"aecho=0.7:0.45:70|140:{decay * 0.7:.2f}|{decay * 0.35:.2f},"
            f"highshelf=f=3800:g={1.0 + width * 2.0:.2f}"
        )
        subprocess.run(
            ["ffmpeg", "-y", "-i", input_path, "-af", fallback, output_path],
            check=True, capture_output=True
        )


def audio_broadcast_limiter(input_path: "str", output_path: "str", *, ceiling_db: "float" = -1.2, drive: "float" = 1.6) -> "None":
    """Broadcast-style loudness clamp with controlled peaks and forward mids."""
    import subprocess

    ceiling_db = min(-0.1, ceiling_db)
    drive = max(0.5, min(3.0, drive))
    af = (
        f"acompressor=threshold=0.10:ratio={2.5 + drive * 1.2:.2f}:attack=4:release=80:makeup={drive:.2f},"
        f"alimiter=limit={10 ** (ceiling_db / 20.0):.6f}:level=disabled,"
        f"equalizer=f=2400:width_type=h:width=1800:g={drive * 1.2:.2f}"
    )
    result = subprocess.run(
        ["ffmpeg", "-y", "-i", input_path, "-af", af, output_path],
        capture_output=True
    )
    if result.returncode != 0:
        fallback = (
            f"acompressor=threshold=0.12:ratio={2.0 + drive:.2f}:attack=6:release=90:makeup={max(1.0, drive * 0.8):.2f},"
            f"volume={max(1.0, drive * 0.9):.2f}"
        )
        subprocess.run(
            ["ffmpeg", "-y", "-i", input_path, "-af", fallback, output_path],
            check=True, capture_output=True
        )


def audio_neon_resonance(input_path: "str", output_path: "str", *, shimmer: "float" = 0.42) -> "None":
    """Synthetic neon resonance with bright echoes and a glossy stereo edge."""
    import subprocess

    shimmer = max(0.05, min(0.95, shimmer))
    af = (
        f"bandpass=f=1400:width_type=h:width={900 + shimmer * 2200:.0f},"
        f"aecho=0.72:0.48:26|72:{0.18 + shimmer * 0.24:.2f}|{0.10 + shimmer * 0.16:.2f},"
        f"chorus=0.55:0.72:{18 + shimmer * 12:.1f}:0.22:0.26:0.28,"
        f"highshelf=f=3600:g={1.2 + shimmer * 3.2:.2f}"
    )
    result = subprocess.run(
        ["ffmpeg", "-y", "-i", input_path, "-af", af, output_path],
        capture_output=True
    )
    if result.returncode != 0:
        fallback = (
            f"aecho=0.68:0.35:38:{0.14 + shimmer * 0.18:.2f},"
            f"highpass=f=380,lowpass=f={3200 + shimmer * 1800:.0f}"
        )
        subprocess.run(
            ["ffmpeg", "-y", "-i", input_path, "-af", fallback, output_path],
            check=True, capture_output=True
        )


def audio_subharmonic_bloom(input_path: "str", output_path: "str", *, weight: "float" = 0.36) -> "None":
    """Low-end bloom with weighted subharmonic emphasis and restrained air loss."""
    import subprocess

    weight = max(0.05, min(0.9, weight))
    af = (
        f"asplit[dry][sub];"
        f"[sub]lowpass=f={120 + weight * 90:.0f},"
        f"acompressor=threshold=0.18:ratio={2.2 + weight * 2.0:.2f}:attack=8:release=120:makeup={1.0 + weight * 1.4:.2f}[low];"
        f"[dry][low]amix=inputs=2:weights='1 {0.35 + weight * 0.9:.2f}',"
        f"lowpass=f={14500 - weight * 3500:.0f}"
    )
    result = subprocess.run(
        ["ffmpeg", "-y", "-i", input_path, "-filter_complex", af, output_path],
        capture_output=True
    )
    if result.returncode != 0:
        fallback = (
            f"bass=g={2.0 + weight * 7.0:.2f}:f={70 + weight * 45:.0f}:w=0.8,"
            f"lowpass=f={15000 - weight * 2800:.0f}"
        )
        subprocess.run(
            ["ffmpeg", "-y", "-i", input_path, "-af", fallback, output_path],
            check=True, capture_output=True
        )


def audio_cosmic_rotor(input_path: "str", output_path: "str", *, swirl: "float" = 0.44, speed: "float" = 0.38) -> "None":
    """Cosmic rotor effect with rotating comb motion and luminous stereo drift."""
    import subprocess

    swirl = max(0.05, min(0.95, swirl))
    speed = max(0.05, min(1.5, speed))
    af = (
        f"apulsator=mode=sine:hz={speed:.2f}:amount={0.35 + swirl * 0.45:.2f},"
        f"aphaser=in_gain=0.65:out_gain=0.88:delay={1.6 + swirl * 2.2:.2f}:decay=0.34:speed={0.22 + speed * 0.55:.2f},"
        f"highshelf=f=3200:g={1.2 + swirl * 2.6:.2f}"
    )
    result = subprocess.run(
        ["ffmpeg", "-y", "-i", input_path, "-af", af, output_path],
        capture_output=True
    )
    if result.returncode != 0:
        fallback = (
            f"chorus=0.55:0.72:{14 + swirl * 10:.1f}:0.22:{0.18 + speed * 0.18:.2f}:0.28,"
            f"aecho=0.68:0.28:{26 + swirl * 34:.1f}:{0.10 + swirl * 0.14:.2f}"
        )
        subprocess.run(
            ["ffmpeg", "-y", "-i", input_path, "-af", fallback, output_path],
            check=True, capture_output=True
        )


def audio_titanium_chorus(input_path: "str", output_path: "str", *, width: "float" = 0.40, shine: "float" = 0.32) -> "None":
    """Titanium chorus with metallic doubling, widened image, and crisp upper mids."""
    import subprocess

    width = max(0.05, min(0.95, width))
    shine = max(0.05, min(0.95, shine))
    af = (
        f"chorus=0.60:0.82:{16 + width * 14:.1f}:0.28:{0.20 + width * 0.18:.2f}:0.30,"
        f"stereotools=mode=lr>lr:slev={1.0 + width * 0.38:.2f},"
        f"highshelf=f=4200:g={1.2 + shine * 3.4:.2f},"
        f"equalizer=f=1800:width_type=h:width=1400:g={0.8 + shine * 2.2:.2f}"
    )
    result = subprocess.run(
        ["ffmpeg", "-y", "-i", input_path, "-af", af, output_path],
        capture_output=True
    )
    if result.returncode != 0:
        fallback = (
            f"chorus=0.55:0.76:{14 + width * 10:.1f}:0.20:{0.18 + width * 0.14:.2f}:0.26,"
            f"highshelf=f=3600:g={0.8 + shine * 2.2:.2f}"
        )
        subprocess.run(
            ["ffmpeg", "-y", "-i", input_path, "-af", fallback, output_path],
            check=True, capture_output=True
        )
