"""Audio dynamics: normalize, compress, adjust_gain."""
from __future__ import annotations

import numpy as np


def normalize(audio: np.ndarray, *, target_peak: float = 0.95) -> np.ndarray:
    """Normalize audio so the peak amplitude equals *target_peak*.

    Args:
        audio: Input audio, float32 [-1, 1].
        target_peak: Desired peak amplitude (0, 1].

    Returns:
        Normalized audio, float32.
    """
    peak = np.abs(audio).max()
    if peak < 1e-8:
        return audio.copy()
    return (audio * (target_peak / peak)).astype(np.float32)


def compress(audio: np.ndarray, *, threshold: float = -20.0,
             ratio: float = 4.0, attack_ms: float = 5.0,
             release_ms: float = 50.0, sr: int = 22050) -> np.ndarray:
    """Simple feed-forward compressor.

    Args:
        audio: Input audio, float32 [-1, 1].
        threshold: Threshold in dBFS (e.g. -20).
        ratio: Compression ratio (e.g. 4:1).
        attack_ms: Attack time in milliseconds.
        release_ms: Release time in milliseconds.
        sr: Sample rate.

    Returns:
        Compressed audio, float32 [-1, 1].
    """
    audio = np.asarray(audio, dtype=np.float32)
    threshold_lin = 10 ** (threshold / 20.0)
    attack_coeff = np.exp(-1.0 / (attack_ms / 1000.0 * sr)) if attack_ms > 0 else 0.0
    release_coeff = np.exp(-1.0 / (release_ms / 1000.0 * sr)) if release_ms > 0 else 0.0

    flat = audio.ravel()
    envelope = np.zeros(len(flat), dtype=np.float32)
    env = 0.0
    for i in range(len(flat)):
        level = abs(flat[i])
        coeff = attack_coeff if level > env else release_coeff
        env = coeff * env + (1 - coeff) * level
        envelope[i] = env

    gain = np.ones_like(envelope)
    above = envelope > threshold_lin
    if above.any():
        over_db = 20 * np.log10(np.maximum(envelope[above], 1e-10)) - threshold
        reduction_db = over_db * (1 - 1 / ratio)
        gain[above] = 10 ** (-reduction_db / 20.0)

    result = flat * gain
    return np.clip(result.reshape(audio.shape), -1, 1).astype(np.float32)


def adjust_gain(audio: np.ndarray, *, db: float = 0.0) -> np.ndarray:
    """Adjust volume by *db* decibels.

    Args:
        audio: Input audio.
        db: Gain adjustment in dB. Positive = louder, negative = quieter.

    Returns:
        Gain-adjusted audio, float32, clipped to [-1, 1].
    """
    factor = 10 ** (db / 20.0)
    return np.clip(audio * factor, -1, 1).astype(np.float32)
