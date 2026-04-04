"""Tests for gemia.audio.frequency."""
import numpy as np

from gemia.audio.frequency import eq, highpass, lowpass


def _sine(freq: float, sr: int = 22050, duration: float = 1.0) -> np.ndarray:
    t = np.linspace(0, duration, int(sr * duration), endpoint=False, dtype=np.float32)
    return np.sin(2 * np.pi * freq * t)


class TestHighpass:
    def test_removes_low_freq(self):
        low = _sine(50)
        high = _sine(5000)
        mixed = (low + high) * 0.5
        out = highpass(mixed, freq=1000, sr=22050)
        # Low frequency energy should be attenuated
        assert np.abs(out).mean() < np.abs(mixed).mean()


class TestLowpass:
    def test_removes_high_freq(self):
        low = _sine(100)
        high = _sine(8000)
        mixed = (low + high) * 0.5
        out = lowpass(mixed, freq=500, sr=22050)
        assert out.dtype == np.float32


class TestEq:
    def test_no_bands(self, sample_audio):
        out = eq(sample_audio)
        np.testing.assert_array_equal(out, sample_audio)

    def test_boost_band(self, sample_audio):
        out = eq(sample_audio, sr=22050, bands=[{"freq": 440, "gain_db": 6.0}])
        assert out.dtype == np.float32
