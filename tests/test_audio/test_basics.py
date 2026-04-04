"""Tests for gemia.audio.basics."""
import numpy as np
import pytest
import tempfile
from pathlib import Path

from gemia.audio.basics import load, save, trim, concat, mix


class TestSaveLoad:
    def test_roundtrip(self, sample_audio, tmp_path):
        path = str(tmp_path / "test.wav")
        save(path, sample_audio, sr=22050)
        loaded, sr = load(path, sr=22050)
        assert sr == 22050
        assert loaded.dtype == np.float32
        assert abs(len(loaded) - len(sample_audio)) <= 1


class TestTrim:
    def test_basic(self, sample_audio):
        out = trim(sample_audio, sr=22050, start_sec=0.1, end_sec=0.5)
        expected_len = int(0.4 * 22050)
        assert abs(len(out) - expected_len) <= 1

    def test_no_end(self, sample_audio):
        out = trim(sample_audio, sr=22050, start_sec=0.5)
        assert len(out) < len(sample_audio)


class TestConcat:
    def test_two_tracks(self, sample_audio):
        out = concat(sample_audio, sample_audio)
        assert len(out) == 2 * len(sample_audio)

    def test_empty(self):
        with pytest.raises(ValueError):
            concat()


class TestMix:
    def test_equal_mix(self, sample_audio):
        silence = np.zeros_like(sample_audio)
        out = mix([sample_audio, silence])
        np.testing.assert_allclose(out, sample_audio, atol=1e-6)

    def test_weights(self, sample_audio):
        out = mix([sample_audio], weights=[0.5])
        np.testing.assert_allclose(out, sample_audio * 0.5, atol=1e-6)

    def test_weight_mismatch(self, sample_audio):
        with pytest.raises(ValueError):
            mix([sample_audio], weights=[1.0, 2.0])
