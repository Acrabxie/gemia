"""Tests for audio mixer: create_bus, sidechain_compress, auto_duck."""
import numpy as np
import pytest

from gemia.audio.mixer import create_bus, sidechain_compress, auto_duck

SR = 22050


@pytest.fixture
def mono_track():
    t = np.linspace(0, 1, SR, endpoint=False, dtype=np.float32)
    return np.sin(2 * np.pi * 440 * t)


@pytest.fixture
def silence():
    return np.zeros(SR, dtype=np.float32)


class TestCreateBus:
    def test_output_shape(self, mono_track):
        out = create_bus([mono_track, mono_track])
        assert out.shape == (2, SR)
        assert out.dtype == np.float32

    def test_single_track_center(self, mono_track):
        out = create_bus([mono_track], pans=[0.0], gains=[1.0])
        assert out.shape == (2, SR)
        np.testing.assert_allclose(out[0], out[1], atol=1e-5)

    def test_pan_left(self, mono_track):
        out = create_bus([mono_track], pans=[-1.0], gains=[1.0])
        assert out[0].max() > out[1].max()

    def test_pan_right(self, mono_track):
        out = create_bus([mono_track], pans=[1.0], gains=[1.0])
        assert out[1].max() > out[0].max()

    def test_gain_zero(self, mono_track):
        out = create_bus([mono_track], gains=[0.0])
        np.testing.assert_allclose(out, np.zeros_like(out), atol=1e-6)

    def test_clamp(self, mono_track):
        out = create_bus([mono_track, mono_track, mono_track, mono_track], gains=[2.0] * 4)
        assert out.min() >= -1.0
        assert out.max() <= 1.0

    def test_empty_raises(self):
        with pytest.raises(ValueError):
            create_bus([])

    def test_gains_mismatch_raises(self, mono_track):
        with pytest.raises(ValueError):
            create_bus([mono_track], gains=[1.0, 2.0])

    def test_pans_mismatch_raises(self, mono_track):
        with pytest.raises(ValueError):
            create_bus([mono_track], pans=[-1.0, 0.0])

    def test_different_lengths(self, mono_track, silence):
        short = mono_track[:SR // 2]
        out = create_bus([mono_track, short])
        assert out.shape == (2, SR)

    def test_2d_track_raises(self, mono_track):
        with pytest.raises(ValueError):
            create_bus([mono_track.reshape(1, -1)])


class TestSidechainCompress:
    def test_output_shape(self, mono_track):
        out = sidechain_compress(mono_track, mono_track, sr=SR)
        assert out.shape == mono_track.shape
        assert out.dtype == np.float32

    def test_clamp(self, mono_track):
        out = sidechain_compress(mono_track, mono_track, sr=SR, threshold=0.1, ratio=10.0)
        assert out.min() >= -1.0
        assert out.max() <= 1.0

    def test_no_compression_below_threshold(self, silence, mono_track):
        out = sidechain_compress(mono_track, silence, sr=SR, threshold=0.9)
        np.testing.assert_allclose(out, mono_track, atol=1e-5)

    def test_compresses_when_trigger_loud(self, mono_track):
        loud_trigger = np.ones(SR, dtype=np.float32)
        out = sidechain_compress(mono_track, loud_trigger, sr=SR, threshold=0.1, ratio=4.0)
        assert np.abs(out).max() < np.abs(mono_track).max()

    def test_trigger_shorter_than_main(self, mono_track):
        short_trigger = mono_track[:SR // 2]
        out = sidechain_compress(mono_track, short_trigger, sr=SR)
        assert out.shape == mono_track.shape


class TestAutoDuck:
    def test_output_shape(self, mono_track):
        music = mono_track.copy()
        voice = mono_track.copy()
        out = auto_duck(music, voice, sr=SR)
        assert out.shape == music.shape
        assert out.dtype == np.float32

    def test_clamp(self, mono_track):
        out = auto_duck(mono_track, mono_track, sr=SR)
        assert out.min() >= -1.0
        assert out.max() <= 1.0

    def test_silence_voice_no_ducking(self, mono_track, silence):
        out = auto_duck(mono_track, silence, sr=SR)
        np.testing.assert_allclose(out, mono_track, atol=1e-5)

    def test_loud_voice_ducks_music(self, mono_track):
        loud_voice = np.ones(SR, dtype=np.float32)
        out = auto_duck(mono_track, loud_voice, sr=SR, reduction_db=20.0)
        assert np.abs(out).mean() < np.abs(mono_track).mean() * 0.9

    def test_voice_shorter_than_music(self, mono_track):
        short_voice = mono_track[:SR // 2]
        out = auto_duck(mono_track, short_voice, sr=SR)
        assert out.shape == mono_track.shape
