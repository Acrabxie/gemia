"""Tests for gemia.audio.time_pitch."""
import numpy as np
import pytest

from gemia.audio.time_pitch import time_stretch, pitch_shift, detect_bpm


class TestTimeStretch:
    def test_faster(self, sample_audio):
        out = time_stretch(sample_audio, rate=2.0)
        # 2x faster ≈ half the length
        assert len(out) < len(sample_audio)

    def test_slower(self, sample_audio):
        out = time_stretch(sample_audio, rate=0.5)
        assert len(out) > len(sample_audio)

    def test_invalid_rate(self, sample_audio):
        with pytest.raises(ValueError):
            time_stretch(sample_audio, rate=0)


class TestPitchShift:
    def test_up(self, sample_audio):
        out = pitch_shift(sample_audio, sr=22050, n_steps=2)
        assert out.dtype == np.float32
        assert len(out) == len(sample_audio)


class TestDetectBpm:
    def test_basic(self, sample_audio):
        bpm = detect_bpm(sample_audio, sr=22050)
        assert isinstance(bpm, float)
        assert bpm > 0
