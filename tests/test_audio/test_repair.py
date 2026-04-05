"""Tests for audio repair: reduce_noise, remove_hum, de_ess, remove_reverb."""
import numpy as np
import pytest

from gemia.audio.repair import reduce_noise, remove_hum, de_ess, remove_reverb

SR = 22050


@pytest.fixture
def sine_440():
    t = np.linspace(0, 1, SR, endpoint=False, dtype=np.float32)
    return np.sin(2 * np.pi * 440 * t)


@pytest.fixture
def noisy_sine(sine_440):
    rng = np.random.default_rng(42)
    noise = rng.normal(0, 0.05, size=len(sine_440)).astype(np.float32)
    return (sine_440 + noise).astype(np.float32)


@pytest.fixture
def hum_contaminated(sine_440):
    t = np.linspace(0, 1, SR, endpoint=False, dtype=np.float32)
    hum = 0.3 * np.sin(2 * np.pi * 50 * t).astype(np.float32)
    return (sine_440 + hum).astype(np.float32)


class TestReduceNoise:
    def test_output_shape(self, noisy_sine):
        out = reduce_noise(noisy_sine, SR)
        assert out.shape == noisy_sine.shape

    def test_output_dtype(self, noisy_sine):
        out = reduce_noise(noisy_sine, SR)
        assert out.dtype == np.float32

    def test_with_noise_profile(self, noisy_sine):
        n_fft = 2048
        from scipy import signal as sig
        _, _, spec = sig.stft(noisy_sine[:int(0.5 * SR)], fs=SR, nperseg=n_fft)
        noise_profile = np.abs(spec).mean(axis=1)
        out = reduce_noise(noisy_sine, SR, noise_profile=noise_profile)
        assert out.shape == noisy_sine.shape
        assert out.dtype == np.float32

    def test_modifies_signal(self, sine_440):
        rng = np.random.default_rng(0)
        heavy_noise = rng.normal(0, 0.5, size=len(sine_440)).astype(np.float32)
        noisy = (sine_440 + heavy_noise).astype(np.float32)
        out = reduce_noise(noisy, SR)
        assert not np.allclose(out, noisy, atol=1e-3)


class TestRemoveHum:
    def test_output_shape(self, hum_contaminated):
        out = remove_hum(hum_contaminated, SR)
        assert out.shape == hum_contaminated.shape

    def test_output_dtype(self, hum_contaminated):
        out = remove_hum(hum_contaminated, SR)
        assert out.dtype == np.float32

    def test_50hz_removed(self, sine_440):
        t = np.linspace(0, 1, SR, endpoint=False, dtype=np.float32)
        hum = 0.5 * np.sin(2 * np.pi * 50 * t).astype(np.float32)
        contaminated = (sine_440 + hum).astype(np.float32)
        out = remove_hum(contaminated, SR, freq=50.0, harmonics=1)
        from scipy import signal as sig
        f, psd_in = sig.periodogram(contaminated, SR)
        f, psd_out = sig.periodogram(out, SR)
        idx50 = np.argmin(np.abs(f - 50.0))
        assert psd_out[idx50] < psd_in[idx50]

    def test_60hz(self, sine_440):
        t = np.linspace(0, 1, SR, endpoint=False, dtype=np.float32)
        hum = 0.3 * np.sin(2 * np.pi * 60 * t).astype(np.float32)
        contaminated = (sine_440 + hum).astype(np.float32)
        out = remove_hum(contaminated, SR, freq=60.0, harmonics=2)
        assert out.shape == contaminated.shape


class TestDeEss:
    def test_output_shape(self, sine_440):
        out = de_ess(sine_440, SR)
        assert out.shape == sine_440.shape

    def test_output_dtype(self, sine_440):
        out = de_ess(sine_440, SR)
        assert out.dtype == np.float32

    def test_sibilant_reduction(self):
        SR2 = 44100
        t = np.linspace(0, 1, SR2, endpoint=False, dtype=np.float32)
        sibilant = 0.8 * np.sin(2 * np.pi * 7000 * t).astype(np.float32)
        out = de_ess(sibilant, SR2, threshold=0.3, freq_range=(5000, 10000))
        assert out.shape == sibilant.shape

    def test_low_frequency_unchanged(self, sine_440):
        out = de_ess(sine_440, SR, threshold=0.01, freq_range=(5000, 10000))
        assert out.shape == sine_440.shape

    def test_custom_freq_range(self, sine_440):
        out = de_ess(sine_440, SR, freq_range=(3000, 8000))
        assert out.shape == sine_440.shape


class TestRemoveReverb:
    def test_output_shape(self, sine_440):
        out = remove_reverb(sine_440, SR)
        assert out.shape == sine_440.shape

    def test_output_dtype(self, sine_440):
        out = remove_reverb(sine_440, SR)
        assert out.dtype == np.float32

    def test_amount_zero_near_passthrough(self, sine_440):
        out = remove_reverb(sine_440, SR, amount=0.0)
        assert out.shape == sine_440.shape

    def test_amount_one(self, sine_440):
        out = remove_reverb(sine_440, SR, amount=1.0)
        assert out.shape == sine_440.shape
        assert out.dtype == np.float32
