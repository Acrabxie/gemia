"""Audio basics: load, save, trim, concat, mix."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import soundfile as sf


def load(path: str, *, sr: int = 22050, mono: bool = True) -> tuple[np.ndarray, int]:
    """Load an audio file and return ``(samples, sample_rate)``.

    Args:
        path: Path to any audio format supported by libsndfile (wav, flac,
            ogg) or ffmpeg-decodable via librosa (mp3, aac, etc.).
        sr: Target sample rate.  The audio is resampled if it differs.
        mono: If True, mix down to single channel.

    Returns:
        ``(audio, sr)`` where *audio* is float32 ndarray, shape ``(samples,)``
        if mono or ``(channels, samples)`` if stereo.
    """
    import librosa  # noqa: PLC0415
    y, loaded_sr = librosa.load(path, sr=sr, mono=mono)
    return y.astype(np.float32), loaded_sr


def save(path: str, audio: np.ndarray, *, sr: int = 22050,
         format: str | None = None) -> None:
    """Save audio to a file.

    Args:
        path: Output file path.  Format is inferred from extension if
            *format* is not given.
        audio: float32 array, shape ``(samples,)`` or ``(channels, samples)``.
        sr: Sample rate.
        format: Explicit format string (``'WAV'``, ``'FLAC'``, etc.).
    """
    audio = np.asarray(audio, dtype=np.float32)
    if audio.ndim == 2:
        # soundfile expects (samples, channels)
        audio = audio.T
    sf.write(path, audio, sr, format=format)


def trim(audio: np.ndarray, *, sr: int,
         start_sec: float = 0.0,
         end_sec: float | None = None) -> np.ndarray:
    """Trim audio to ``[start_sec, end_sec]``.

    Args:
        audio: Input audio, float32 ``(samples,)`` or ``(channels, samples)``.
        sr: Sample rate.
        start_sec: Start time in seconds.
        end_sec: End time in seconds.  ``None`` = end of audio.

    Returns:
        Trimmed audio, same dtype and channel layout.
    """
    start = int(round(start_sec * sr))
    if audio.ndim == 1:
        end = len(audio) if end_sec is None else int(round(end_sec * sr))
        return audio[start:end].copy()
    else:
        end = audio.shape[1] if end_sec is None else int(round(end_sec * sr))
        return audio[:, start:end].copy()


def concat(*tracks: np.ndarray) -> np.ndarray:
    """Concatenate multiple audio arrays along the time axis.

    All tracks must have the same number of channels (or all be mono).

    Args:
        *tracks: Audio arrays to concatenate.

    Returns:
        Concatenated audio, float32.
    """
    if not tracks:
        raise ValueError("At least one track is required.")
    if tracks[0].ndim == 1:
        return np.concatenate(tracks, axis=0).astype(np.float32)
    else:
        return np.concatenate(tracks, axis=1).astype(np.float32)


def mix(tracks: list[np.ndarray], *, weights: list[float] | None = None) -> np.ndarray:
    """Mix (sum) multiple audio tracks with optional gain weights.

    Tracks are zero-padded to the length of the longest.

    Args:
        tracks: List of audio arrays.
        weights: Per-track gain multipliers.  Defaults to equal weight.

    Returns:
        Mixed audio, float32, clipped to [-1, 1].
    """
    if not tracks:
        raise ValueError("At least one track is required.")
    w = weights or [1.0] * len(tracks)
    if len(w) != len(tracks):
        raise ValueError(f"weights length ({len(w)}) != tracks length ({len(tracks)}).")

    max_len = max(t.shape[-1] for t in tracks)
    result = np.zeros(max_len, dtype=np.float32) if tracks[0].ndim == 1 else \
        np.zeros((tracks[0].shape[0], max_len), dtype=np.float32)
    for t, g in zip(tracks, w):
        n = t.shape[-1]
        if t.ndim == 1:
            result[:n] += t * g
        else:
            result[:, :n] += t * g
    return np.clip(result, -1, 1).astype(np.float32)
