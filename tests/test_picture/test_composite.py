"""Tests for gemia.picture.composite."""
import numpy as np
import pytest

from gemia.picture.composite import create_mask, blend, composite


class TestCreateMask:
    def test_threshold(self, sample_image):
        mask = create_mask(sample_image, threshold=0.5)
        assert mask.ndim == 2
        assert set(np.unique(mask)).issubset({0.0, 1.0})

    def test_luminance(self, sample_image):
        mask = create_mask(sample_image, method="luminance")
        assert mask.ndim == 2
        assert mask.dtype == np.float32

    def test_channel(self, sample_image):
        mask = create_mask(sample_image, channel=2)  # R channel
        assert mask.ndim == 2


class TestBlend:
    def test_half_blend(self, sample_image):
        black = np.zeros_like(sample_image)
        out = blend(sample_image, black, alpha=0.5)
        np.testing.assert_allclose(out, sample_image * 0.5, atol=1e-5)

    def test_alpha_zero(self, sample_image):
        other = np.ones_like(sample_image)
        out = blend(sample_image, other, alpha=0.0)
        np.testing.assert_allclose(out, sample_image, atol=1e-6)

    def test_shape_mismatch(self, sample_image):
        small = sample_image[:32, :32]
        with pytest.raises(ValueError):
            blend(sample_image, small)


class TestComposite:
    def test_full_mask(self, sample_image):
        bg = np.zeros_like(sample_image)
        mask = np.ones(sample_image.shape[:2], dtype=np.float32)
        out = composite(sample_image, bg, mask=mask)
        np.testing.assert_allclose(out, sample_image, atol=1e-6)

    def test_zero_mask(self, sample_image):
        bg = np.ones_like(sample_image) * 0.5
        mask = np.zeros(sample_image.shape[:2], dtype=np.float32)
        out = composite(sample_image, bg, mask=mask)
        np.testing.assert_allclose(out, bg, atol=1e-6)

    def test_mask_size_mismatch(self, sample_image):
        bg = sample_image.copy()
        mask = np.ones((10, 10), dtype=np.float32)
        with pytest.raises(ValueError):
            composite(sample_image, bg, mask=mask)
