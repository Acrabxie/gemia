"""Tests for gemia.audio.dynamics."""
import numpy as np

from gemia.audio.dynamics import normalize, compress, adjust_gain


class TestNormalize:
    def test_peak(self, sample_audio):
        out = normalize(sample_audio, target_peak=0.8)
        assert abs(np.abs(out).max() - 0.8) < 0.01

    def test_silence(self):
        silence = np.zeros(1000, dtype=np.float32)
        out = normalize(silence)
        assert np.abs(out).max() == 0.0


class TestCompress:
    def test_reduces_dynamic_range(self, sample_audio):
        loud = sample_audio * 0.9
        out = compress(loud, threshold=-10.0, ratio=4.0, sr=22050)
        assert out.dtype == np.float32
        assert np.abs(out).max() <= 1.0


class TestAdjustGain:
    def test_boost(self, sample_audio):
        out = adjust_gain(sample_audio * 0.1, db=6.0)
        assert np.abs(out).max() > np.abs(sample_audio * 0.1).max()

    def test_cut(self, sample_audio):
        out = adjust_gain(sample_audio, db=-6.0)
        assert np.abs(out).max() < np.abs(sample_audio).max()

    def test_zero_db(self, sample_audio):
        out = adjust_gain(sample_audio, db=0.0)
        np.testing.assert_allclose(out, sample_audio, atol=1e-6)
