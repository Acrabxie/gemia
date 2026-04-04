"""Tests for gemia.picture.pixel."""
import numpy as np
import pytest

from gemia.picture.pixel import blur, sharpen, denoise, add_grain, convolve


class TestBlur:
    def test_gaussian(self, sample_image):
        out = blur(sample_image, radius=2.0, method="gaussian")
        assert out.shape == sample_image.shape
        assert out.dtype == np.float32

    def test_box(self, sample_image):
        out = blur(sample_image, radius=2.0, method="box")
        assert out.shape == sample_image.shape

    def test_median(self, sample_image):
        out = blur(sample_image, radius=2.0, method="median")
        assert out.shape == sample_image.shape

    def test_unknown_method(self, sample_image):
        with pytest.raises(ValueError):
            blur(sample_image, method="unknown")

    def test_batch(self, sample_image):
        out = blur([sample_image, sample_image], radius=1.0)
        assert len(out) == 2


class TestSharpen:
    def test_basic(self, sample_image):
        out = sharpen(sample_image, amount=1.5)
        assert out.shape == sample_image.shape
        assert out.min() >= 0 and out.max() <= 1


class TestDenoise:
    def test_basic(self, sample_image):
        out = denoise(sample_image, strength=5.0)
        assert out.shape == sample_image.shape


class TestAddGrain:
    def test_deterministic(self, sample_image):
        a = add_grain(sample_image, intensity=0.1, seed=42)
        b = add_grain(sample_image, intensity=0.1, seed=42)
        np.testing.assert_array_equal(a, b)

    def test_different_seeds(self, sample_image):
        a = add_grain(sample_image, intensity=0.1, seed=1)
        b = add_grain(sample_image, intensity=0.1, seed=2)
        assert not np.array_equal(a, b)


class TestConvolve:
    def test_identity_kernel(self, sample_image):
        kernel = np.array([[0, 0, 0], [0, 1, 0], [0, 0, 0]], dtype=np.float32)
        out = convolve(sample_image, kernel=kernel)
        np.testing.assert_allclose(out, sample_image, atol=1e-5)

    def test_bad_kernel(self, sample_image):
        with pytest.raises(ValueError):
            convolve(sample_image, kernel=np.zeros((3,)))
