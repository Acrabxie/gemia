"""Tests for waveform_monitor, vectorscope, histogram_rgb, check_clipping."""
import numpy as np
import pytest

from gemia.picture.analysis import waveform_monitor, vectorscope, histogram_rgb, check_clipping


class TestWaveformMonitor:
    def test_shape(self, sample_image):
        wf = waveform_monitor(sample_image)
        assert wf.ndim == 2
        assert wf.shape[1] == 256
        assert wf.dtype == np.float32

    def test_custom_width(self, sample_image):
        wf = waveform_monitor(sample_image, width=64)
        assert wf.shape[1] == 64

    def test_range(self, sample_image):
        wf = waveform_monitor(sample_image)
        assert wf.min() >= 0.0
        assert wf.max() <= 1.0

    def test_grayscale_input(self, sample_gray):
        wf = waveform_monitor(sample_gray, width=32)
        assert wf.ndim == 2
        assert wf.shape[1] == 32


class TestVectorscope:
    def test_shape(self, sample_image):
        scope = vectorscope(sample_image)
        assert scope.shape == (256, 256)
        assert scope.dtype == np.float32

    def test_custom_size(self, sample_image):
        scope = vectorscope(sample_image, size=128)
        assert scope.shape == (128, 128)

    def test_range(self, sample_image):
        scope = vectorscope(sample_image)
        assert scope.min() >= 0.0
        assert scope.max() <= 1.0

    def test_grayscale(self):
        gray = np.full((32, 32, 3), 0.5, dtype=np.float32)
        scope = vectorscope(gray, size=64)
        assert scope.sum() > 0


class TestHistogramRgb:
    def test_keys(self, sample_image):
        h = histogram_rgb(sample_image)
        assert set(h.keys()) == {"r", "g", "b"}

    def test_shape(self, sample_image):
        h = histogram_rgb(sample_image)
        for ch in "rgb":
            assert h[ch].shape == (256,)

    def test_custom_bins(self, sample_image):
        h = histogram_rgb(sample_image, bins=64)
        for ch in "rgb":
            assert h[ch].shape == (64,)

    def test_sum_equals_pixels(self, sample_image):
        h = histogram_rgb(sample_image)
        n_pixels = sample_image.shape[0] * sample_image.shape[1]
        for ch in "rgb":
            assert int(h[ch].sum()) == n_pixels

    def test_uniform_image(self):
        img = np.full((16, 16, 3), 0.5, dtype=np.float32)
        h = histogram_rgb(img)
        for ch in "rgb":
            nz = np.count_nonzero(h[ch])
            assert nz <= 2


class TestCheckClipping:
    def test_no_clipping(self, sample_image):
        result = check_clipping(sample_image, ceiling=0.99)
        assert "highlight_pct" in result
        assert "shadow_pct" in result
        assert "is_clipped" in result

    def test_all_white_clipped(self):
        img = np.ones((16, 16, 3), dtype=np.float32)
        result = check_clipping(img, ceiling=0.95)
        assert result["highlight_pct"] > 0.0
        assert result["is_clipped"] is True

    def test_all_black_shadow(self):
        img = np.zeros((16, 16, 3), dtype=np.float32)
        result = check_clipping(img)
        assert result["shadow_pct"] > 0.0
        assert result["is_clipped"] is True

    def test_midgray_no_clipping(self):
        img = np.full((16, 16, 3), 0.5, dtype=np.float32)
        result = check_clipping(img, ceiling=0.95)
        assert result["highlight_pct"] == 0.0
        assert result["shadow_pct"] == 0.0
        assert result["is_clipped"] is False
