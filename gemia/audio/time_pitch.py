"""Time and pitch operations: time_stretch, pitch_shift, detect_bpm."""
from __future__ import annotations

import numpy as np


def time_stretch(audio: np.ndarray, *, rate: float) -> np.ndarray:
    """Time-stretch without changing pitch.

    Args:
        audio: Input mono audio, float32 [-1, 1].
        rate: Stretch factor. >1 = faster/shorter, <1 = slower/longer.

    Returns:
        Time-stretched audio, float32.
    """
    import librosa  # noqa: PLC0415
    if rate <= 0:
        raise ValueError("rate must be > 0.")
    result = librosa.effects.time_stretch(audio.astype(np.float32), rate=rate)
    return result.astype(np.float32)


def pitch_shift(audio: np.ndarray, *, sr: int = 22050,
                n_steps: float = 0.0) -> np.ndarray:
    """Shift pitch without changing duration.

    Args:
        audio: Input mono audio, float32.
        sr: Sample rate.
        n_steps: Number of semitones to shift. Positive = higher pitch.

    Returns:
        Pitch-shifted audio, float32.
    """
    import librosa  # noqa: PLC0415
    result = librosa.effects.pitch_shift(audio.astype(np.float32), sr=sr, n_steps=n_steps)
    return result.astype(np.float32)


def detect_bpm(audio: np.ndarray, *, sr: int = 22050) -> float:
    """Estimate the tempo (BPM) of an audio signal.

    Args:
        audio: Input mono audio, float32.
        sr: Sample rate.

    Returns:
        Estimated BPM as a float.
    """
    import librosa  # noqa: PLC0415
    onset_env = librosa.onset.onset_strength(y=audio.astype(np.float32), sr=sr)
    tempo = librosa.feature.tempo(onset_envelope=onset_env, sr=sr)
    if hasattr(tempo, "__len__"):
        return float(tempo[0])
    return float(tempo)
