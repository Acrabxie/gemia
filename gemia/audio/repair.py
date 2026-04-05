"""Audio repair: noise reduction, hum removal, de-essing, reverb reduction."""
from __future__ import annotations

import numpy as np
from scipy import signal


def reduce_noise(
    waveform: np.ndarray,
    sample_rate: int,
    *,
    noise_profile: np.ndarray | None = None,
) -> np.ndarray:
    """Spectral subtraction noise reduction.

    Args:
        waveform: Input audio, float32 mono (samples,).
        sample_rate: Sample rate in Hz.
        noise_profile: Optional noise power spectrum (magnitude array). If None,
            estimated from the first 0.5 seconds.

    Returns:
        Denoised audio, float32.
    """
    waveform = np.asarray(waveform, dtype=np.float32)
    n_fft = 2048
    hop = n_fft // 4

    f, t, spec = signal.stft(waveform, fs=sample_rate, nperseg=n_fft, noverlap=n_fft - hop)
    magnitude = np.abs(spec)
    phase = np.angle(spec)

    if noise_profile is None:
        noise_frames = max(1, int(0.5 * sample_rate / hop))
        noise_profile = magnitude[:, :noise_frames].mean(axis=1, keepdims=True)
    else:
        noise_profile = np.asarray(noise_profile, dtype=np.float32).reshape(-1, 1)

    alpha = 2.0
    beta = 0.01
    clean_mag = np.maximum(magnitude - alpha * noise_profile, beta * noise_profile)
    clean_spec = clean_mag * np.exp(1j * phase)

    _, restored = signal.istft(clean_spec, fs=sample_rate, nperseg=n_fft, noverlap=n_fft - hop)
    restored = restored[: len(waveform)]
    if len(restored) < len(waveform):
        restored = np.pad(restored, (0, len(waveform) - len(restored)))
    return restored.astype(np.float32)


def remove_hum(
    waveform: np.ndarray,
    sample_rate: int,
    *,
    freq: float = 50.0,
    harmonics: int = 3,
) -> np.ndarray:
    """Notch filter at freq and its harmonics.

    Args:
        waveform: Input audio, float32 mono (samples,).
        sample_rate: Sample rate in Hz.
        freq: Fundamental hum frequency in Hz (50 or 60).
        harmonics: Number of harmonics to notch (including fundamental).

    Returns:
        Filtered audio, float32.
    """
    waveform = np.asarray(waveform, dtype=np.float32)
    result = waveform.copy()
    for n in range(1, harmonics + 1):
        f0 = freq * n
        if f0 >= sample_rate / 2:
            break
        Q = 30.0
        b, a = signal.iirnotch(f0, Q, fs=sample_rate)
        result = signal.filtfilt(b, a, result).astype(np.float32)
    return result


def de_ess(
    waveform: np.ndarray,
    sample_rate: int,
    *,
    threshold: float = 0.5,
    freq_range: tuple[float, float] = (5000, 10000),
) -> np.ndarray:
    """Dynamic EQ to reduce sibilance in a high frequency range.

    Args:
        waveform: Input audio, float32 mono (samples,).
        sample_rate: Sample rate in Hz.
        threshold: RMS level above which de-essing activates [0, 1].
        freq_range: (low_hz, high_hz) frequency range for sibilance detection.

    Returns:
        De-essed audio, float32.
    """
    waveform = np.asarray(waveform, dtype=np.float32)
    low_hz, high_hz = freq_range

    nyq = sample_rate / 2.0
    low_norm = np.clip(low_hz / nyq, 0.0, 0.999)
    high_norm = np.clip(high_hz / nyq, 0.0, 0.999)

    if low_norm >= high_norm or low_norm <= 0 or high_norm <= 0:
        return waveform.copy()

    b, a = signal.butter(4, [low_norm, high_norm], btype="band")
    sibilance = signal.filtfilt(b, a, waveform).astype(np.float32)

    block = 512
    n_blocks = (len(waveform) + block - 1) // block
    result = waveform.copy()

    for i in range(n_blocks):
        start = i * block
        end = min(start + block, len(waveform))
        rms = float(np.sqrt(np.mean(sibilance[start:end] ** 2)))
        if rms > threshold:
            reduction = threshold / (rms + 1e-9)
            result[start:end] = (
                waveform[start:end] - sibilance[start:end] * (1.0 - reduction)
            )

    return result.astype(np.float32)


def remove_reverb(
    waveform: np.ndarray,
    sample_rate: int,
    *,
    amount: float = 0.5,
) -> np.ndarray:
    """Simple spectral subtraction approach to reverb reduction.

    Args:
        waveform: Input audio, float32 mono (samples,).
        sample_rate: Sample rate in Hz.
        amount: Strength of reverb reduction [0, 1].

    Returns:
        Reverb-reduced audio, float32.
    """
    waveform = np.asarray(waveform, dtype=np.float32)
    n_fft = 2048
    hop = n_fft // 4

    f, t, spec = signal.stft(waveform, fs=sample_rate, nperseg=n_fft, noverlap=n_fft - hop)
    magnitude = np.abs(spec)
    phase = np.angle(spec)

    window = max(1, int(0.1 * sample_rate / hop))
    from scipy.ndimage import uniform_filter1d
    reverb_est = uniform_filter1d(magnitude, size=window, axis=1)

    clean_mag = np.maximum(magnitude - amount * reverb_est, (1.0 - amount) * magnitude)
    clean_spec = clean_mag * np.exp(1j * phase)

    _, restored = signal.istft(clean_spec, fs=sample_rate, nperseg=n_fft, noverlap=n_fft - hop)
    restored = restored[: len(waveform)]
    if len(restored) < len(waveform):
        restored = np.pad(restored, (0, len(waveform) - len(restored)))
    return restored.astype(np.float32)
