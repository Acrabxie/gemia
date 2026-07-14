"""Audio mixing: bus routing, sidechain compression, auto-ducking."""
from __future__ import annotations

import numpy as np


def create_bus(
    track_list: list[np.ndarray],
    *,
    gains: list[float] | None = None,
    pans: list[float] | None = None,
    sr: int = 22050,
) -> np.ndarray:
    """Mix multiple mono tracks with gain and pan into stereo output.

    Args:
        track_list: List of mono float32 audio arrays.
        gains: Per-track gain multipliers. Defaults to 1.0 for each track.
        pans: Per-track pan values. -1.0=full left, 0.0=center, 1.0=full right.
            Defaults to 0.0 (center) for each track.
        sr: Sample rate (unused, kept for API consistency).

    Returns:
        Stereo float32 array of shape (2, samples), clipped to [-1, 1].
    """
    if not track_list:
        raise ValueError("track_list must not be empty.")
    n = len(track_list)
    gains_arr = gains if gains is not None else [1.0] * n
    pans_arr = pans if pans is not None else [0.0] * n

    if len(gains_arr) != n:
        raise ValueError(f"gains length ({len(gains_arr)}) != track_list length ({n}).")
    if len(pans_arr) != n:
        raise ValueError(f"pans length ({len(pans_arr)}) != track_list length ({n}).")

    max_len = max(len(t) for t in track_list)
    stereo = np.zeros((2, max_len), dtype=np.float32)

    for track, g, p in zip(track_list, gains_arr, pans_arr):
        track = np.asarray(track, dtype=np.float32)
        if track.ndim != 1:
            raise ValueError("Each track in track_list must be mono (1D).")
        pan_clamped = max(-1.0, min(1.0, p))
        left_gain = g * (1.0 - pan_clamped) / 2.0 * np.sqrt(2.0)
        right_gain = g * (1.0 + pan_clamped) / 2.0 * np.sqrt(2.0)
        stereo[0, : len(track)] += track * left_gain
        stereo[1, : len(track)] += track * right_gain

    return np.clip(stereo, -1.0, 1.0).astype(np.float32)


def sidechain_compress(
    main: np.ndarray,
    trigger: np.ndarray,
    *,
    sr: int,
    threshold: float = 0.3,
    ratio: float = 4.0,
    attack_ms: float = 10.0,
    release_ms: float = 100.0,
) -> np.ndarray:
    """Compress main signal when trigger exceeds threshold.

    Args:
        main: Main audio signal, float32 mono (samples,).
        trigger: Sidechain trigger signal, float32 mono (same length or shorter).
        sr: Sample rate in Hz.
        threshold: Amplitude threshold above which compression activates [0, 1].
        ratio: Compression ratio (e.g. 4.0 = 4:1).
        attack_ms: Attack time in milliseconds.
        release_ms: Release time in milliseconds.

    Returns:
        Compressed main signal, float32.
    """
    main = np.asarray(main, dtype=np.float32)
    trigger = np.asarray(trigger, dtype=np.float32)

    attack_coef = np.exp(-1.0 / (sr * attack_ms / 1000.0 + 1e-9))
    release_coef = np.exp(-1.0 / (sr * release_ms / 1000.0 + 1e-9))

    n = len(main)
    trig_len = min(len(trigger), n)
    envelope = np.zeros(n, dtype=np.float32)
    env = 0.0

    for i in range(n):
        amp = abs(trigger[i]) if i < trig_len else 0.0
        if amp > env:
            env = attack_coef * env + (1.0 - attack_coef) * amp
        else:
            env = release_coef * env + (1.0 - release_coef) * amp
        envelope[i] = env

    gain = np.ones(n, dtype=np.float32)
    above = envelope > threshold
    excess_db = 20.0 * np.log10(np.maximum(envelope[above], 1e-9)) - 20.0 * np.log10(threshold + 1e-9)
    reduction_db = excess_db * (1.0 - 1.0 / ratio)
    gain[above] = np.power(10.0, -reduction_db / 20.0)

    return np.clip(main * gain, -1.0, 1.0).astype(np.float32)


def auto_duck(
    music: np.ndarray,
    voice: np.ndarray,
    *,
    sr: int,
    reduction_db: float = 12.0,
    attack_ms: float = 50.0,
    release_ms: float = 500.0,
) -> np.ndarray:
    """Automatically reduce music level when voice is present.

    Args:
        music: Background music signal, float32 mono (samples,).
        voice: Voice signal used as the ducking trigger, float32 mono.
        sr: Sample rate in Hz.
        reduction_db: How much to reduce music when voice is present (dB).
        attack_ms: Attack time in milliseconds.
        release_ms: Release time in milliseconds.

    Returns:
        Ducked music signal, float32 mono.
    """
    music = np.asarray(music, dtype=np.float32)
    voice = np.asarray(voice, dtype=np.float32)

    threshold = 0.05
    max_reduction = float(10.0 ** (-abs(reduction_db) / 20.0))

    attack_coef = np.exp(-1.0 / (sr * attack_ms / 1000.0 + 1e-9))
    release_coef = np.exp(-1.0 / (sr * release_ms / 1000.0 + 1e-9))

    n = len(music)
    voice_len = min(len(voice), n)
    envelope = np.zeros(n, dtype=np.float32)
    env = 0.0

    for i in range(n):
        amp = abs(voice[i]) if i < voice_len else 0.0
        if amp > env:
            env = attack_coef * env + (1.0 - attack_coef) * amp
        else:
            env = release_coef * env + (1.0 - release_coef) * amp
        envelope[i] = env

    voice_active = envelope > threshold
    duck_factor = np.where(voice_active, max_reduction, 1.0).astype(np.float32)

    return np.clip(music * duck_factor, -1.0, 1.0).astype(np.float32)
