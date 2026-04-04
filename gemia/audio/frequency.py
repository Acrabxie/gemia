"""Frequency-domain operations: eq, highpass, lowpass."""
from __future__ import annotations

import numpy as np
from scipy.signal import butter, sosfilt


def eq(audio: np.ndarray, *, sr: int = 22050,
       bands: list[dict] | None = None) -> np.ndarray:
    """Parametric EQ with multiple bands.

    Args:
        audio: Input audio, float32 [-1, 1].
        sr: Sample rate.
        bands: List of EQ band dicts, each with:
            - ``freq`` (Hz): center frequency
            - ``gain_db``: boost/cut in dB
            - ``q`` (optional): quality factor, default 1.0

    Returns:
        EQ'd audio, float32 [-1, 1].

    Example::

        eq(audio, sr=44100, bands=[
            {"freq": 100, "gain_db": 3.0},
            {"freq": 8000, "gain_db": -2.0, "q": 2.0},
        ])
    """
    if not bands:
        return audio.copy()
    result = audio.astype(np.float64)
    for band in bands:
        freq = float(band["freq"])
        gain_db = float(band["gain_db"])
        q = float(band.get("q", 1.0))
        bw = freq / q
        low = max(freq - bw / 2, 1)
        high = min(freq + bw / 2, sr / 2 - 1)
        sos = butter(2, [low, high], btype="band", fs=sr, output="sos")
        band_signal = sosfilt(sos, result)
        gain = 10 ** (gain_db / 20.0) - 1.0
        result = result + gain * band_signal
    return np.clip(result, -1, 1).astype(np.float32)


def highpass(audio: np.ndarray, *, freq: float, sr: int = 22050,
             order: int = 4) -> np.ndarray:
    """Apply a Butterworth high-pass filter.

    Args:
        audio: Input audio.
        freq: Cutoff frequency in Hz.
        sr: Sample rate.
        order: Filter order (default 4).

    Returns:
        Filtered audio, float32 [-1, 1].
    """
    sos = butter(order, freq, btype="high", fs=sr, output="sos")
    result = sosfilt(sos, audio.astype(np.float64))
    return np.clip(result, -1, 1).astype(np.float32)


def lowpass(audio: np.ndarray, *, freq: float, sr: int = 22050,
            order: int = 4) -> np.ndarray:
    """Apply a Butterworth low-pass filter.

    Args:
        audio: Input audio.
        freq: Cutoff frequency in Hz.
        sr: Sample rate.
        order: Filter order (default 4).

    Returns:
        Filtered audio, float32 [-1, 1].
    """
    sos = butter(order, freq, btype="low", fs=sr, output="sos")
    result = sosfilt(sos, audio.astype(np.float64))
    return np.clip(result, -1, 1).astype(np.float32)
