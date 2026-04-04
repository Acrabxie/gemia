"""Tests for gemia.picture.color."""
import numpy as np
import pytest

from gemia.picture.color import color_grade, adjust_exposure, adjust_temperature, apply_lut


class TestColorGrade:
    def test_preset_warm(self, sample_image):
        out = color_grade(sample_image, preset="warm")
        assert out.shape == sample_image.shape
        assert out.dtype == np.float32

    def test_preset_cyberpunk(self, sample_image):
        out = color_grade(sample_image, preset="cyberpunk")
        assert out.min() >= 0 and out.max() <= 1

    def test_manual_offsets(self, sample_image):
        out = color_grade(sample_image, shadows=(0.1, 0, -0.1))
        assert out.shape == sample_image.shape

    def test_batch(self, sample_image):
        out = color_grade([sample_image, sample_image], preset="cool")
        assert len(out) == 2


class TestAdjustExposure:
    def test_brighter(self, sample_image):
        out = adjust_exposure(sample_image, stops=1.0)
        assert out.mean() > sample_image.mean()

    def test_darker(self, sample_image):
        out = adjust_exposure(sample_image, stops=-1.0)
        assert out.mean() < sample_image.mean()

    def test_zero_stops(self, sample_image):
        out = adjust_exposure(sample_image, stops=0.0)
        np.testing.assert_allclose(out, sample_image, atol=1e-6)


class TestAdjustTemperature:
    def test_warmer(self, sample_image):
        out = adjust_temperature(sample_image, kelvin_shift=30)
        # R channel should increase, B should decrease
        assert out[:, :, 2].mean() >= sample_image[:, :, 2].mean()
        assert out[:, :, 0].mean() <= sample_image[:, :, 0].mean()

    def test_grayscale_raises(self, sample_gray):
        with pytest.raises(ValueError):
            adjust_temperature(sample_gray, kelvin_shift=10)


class TestApplyLut:
    def test_identity_1d(self, sample_image):
        lut = np.linspace(0, 1, 256, dtype=np.float32)
        out = apply_lut(sample_image, lut=lut)
        np.testing.assert_allclose(out, sample_image, atol=0.01)

    def test_bad_lut_shape(self, sample_image):
        with pytest.raises(ValueError):
            apply_lut(sample_image, lut=np.zeros((10, 10)))
