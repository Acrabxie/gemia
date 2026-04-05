"""Tests for blend modes, chroma_key, luma_key, create_edge_mask."""
import numpy as np
import pytest

from gemia.picture.composite import (
    blend_multiply, blend_screen, blend_overlay, blend_soft_light,
    blend_hard_light, blend_color_dodge, blend_color_burn, blend_difference,
    blend_exclusion, blend_hue, blend_saturation, blend_color, blend_luminosity,
    chroma_key, luma_key, create_edge_mask,
)


@pytest.fixture
def white(sample_image):
    return np.ones_like(sample_image)


@pytest.fixture
def black(sample_image):
    return np.zeros_like(sample_image)


@pytest.fixture
def gray(sample_image):
    return np.full_like(sample_image, 0.5)


class TestBlendMultiply:
    def test_multiply_by_white(self, sample_image, white):
        out = blend_multiply(sample_image, white)
        np.testing.assert_allclose(out, sample_image, atol=1e-5)

    def test_multiply_by_black(self, sample_image, black):
        out = blend_multiply(sample_image, black)
        np.testing.assert_allclose(out, black, atol=1e-5)

    def test_output_shape(self, sample_image, gray):
        out = blend_multiply(sample_image, gray)
        assert out.shape == sample_image.shape
        assert out.dtype == np.float32

    def test_clamp(self):
        a = np.ones((4, 4, 3), dtype=np.float32) * 2.0
        b = np.ones((4, 4, 3), dtype=np.float32) * 2.0
        out = blend_multiply(a, b)
        assert out.max() <= 1.0
        assert out.min() >= 0.0


class TestBlendScreen:
    def test_screen_white(self, sample_image, white):
        out = blend_screen(sample_image, white)
        np.testing.assert_allclose(out, white, atol=1e-5)

    def test_screen_black(self, sample_image, black):
        out = blend_screen(sample_image, black)
        np.testing.assert_allclose(out, sample_image, atol=1e-5)

    def test_always_lighter(self, sample_image, gray):
        out = blend_screen(sample_image, gray)
        assert np.all(out >= sample_image - 1e-5)


class TestBlendOverlay:
    def test_output_dtype(self, sample_image, gray):
        out = blend_overlay(sample_image, gray)
        assert out.dtype == np.float32
        assert out.shape == sample_image.shape

    def test_clamp(self):
        a = np.ones((4, 4, 3), dtype=np.float32)
        b = np.ones((4, 4, 3), dtype=np.float32)
        out = blend_overlay(a, b)
        assert out.max() <= 1.0
        assert out.min() >= 0.0


class TestBlendSoftLight:
    def test_neutral_at_half(self, sample_image, gray):
        out = blend_soft_light(sample_image, gray)
        np.testing.assert_allclose(out, sample_image, atol=1e-4)

    def test_output_range(self, sample_image, gray):
        out = blend_soft_light(sample_image, gray)
        assert out.max() <= 1.0
        assert out.min() >= 0.0


class TestBlendHardLight:
    def test_output_shape(self, sample_image, gray):
        out = blend_hard_light(sample_image, gray)
        assert out.shape == sample_image.shape
        assert out.dtype == np.float32

    def test_clamp(self):
        a = np.ones((4, 4, 3), dtype=np.float32)
        b = np.ones((4, 4, 3), dtype=np.float32)
        out = blend_hard_light(a, b)
        assert out.max() <= 1.0
        assert out.min() >= 0.0


class TestBlendColorDodge:
    def test_dodge_by_black(self, sample_image, black):
        out = blend_color_dodge(sample_image, black)
        np.testing.assert_allclose(out, sample_image, atol=1e-4)

    def test_clamp(self, sample_image, white):
        out = blend_color_dodge(sample_image, white)
        assert out.max() <= 1.0


class TestBlendColorBurn:
    def test_burn_by_white(self, sample_image, white):
        out = blend_color_burn(sample_image, white)
        np.testing.assert_allclose(out, sample_image, atol=1e-4)

    def test_clamp(self, sample_image, black):
        out = blend_color_burn(sample_image, black)
        assert out.min() >= 0.0


class TestBlendDifference:
    def test_same_image_zero(self, sample_image):
        out = blend_difference(sample_image, sample_image)
        np.testing.assert_allclose(out, np.zeros_like(sample_image), atol=1e-5)

    def test_commutative(self, sample_image, gray):
        out1 = blend_difference(sample_image, gray)
        out2 = blend_difference(gray, sample_image)
        np.testing.assert_allclose(out1, out2, atol=1e-5)


class TestBlendExclusion:
    def test_exclusion_gray_is_flat(self, gray):
        out = blend_exclusion(gray, gray)
        expected = gray + gray - 2.0 * gray * gray
        np.testing.assert_allclose(out, expected, atol=1e-5)

    def test_clamp(self):
        a = np.ones((4, 4, 3), dtype=np.float32)
        b = np.ones((4, 4, 3), dtype=np.float32)
        out = blend_exclusion(a, b)
        assert out.max() <= 1.0
        assert out.min() >= 0.0


class TestHlsBlendModes:
    def test_hue_shape(self, sample_image, gray):
        out = blend_hue(sample_image, gray)
        assert out.shape == sample_image.shape
        assert out.dtype == np.float32

    def test_saturation_shape(self, sample_image, gray):
        out = blend_saturation(sample_image, gray)
        assert out.shape == sample_image.shape

    def test_color_shape(self, sample_image, gray):
        out = blend_color(sample_image, gray)
        assert out.shape == sample_image.shape

    def test_luminosity_shape(self, sample_image, gray):
        out = blend_luminosity(sample_image, gray)
        assert out.shape == sample_image.shape

    def test_all_hls_in_range(self, sample_image, gray):
        for fn in [blend_hue, blend_saturation, blend_color, blend_luminosity]:
            out = fn(sample_image, gray)
            assert out.min() >= 0.0, f"{fn.__name__} produced negative values"
            assert out.max() <= 1.0, f"{fn.__name__} produced values > 1"


class TestChromaKey:
    def test_green_screen(self):
        img = np.zeros((16, 16, 3), dtype=np.float32)
        img[:8, :, 1] = 1.0
        out = chroma_key(img, key_color=(0.0, 1.0, 0.0), tolerance=0.2)
        assert out.shape == (16, 16, 4)
        assert out.dtype == np.float32
        assert out[:8, :, 3].mean() < 0.5

    def test_non_keyed_area_opaque(self):
        img = np.full((16, 16, 3), 0.8, dtype=np.float32)
        out = chroma_key(img, key_color=(0.0, 1.0, 0.0), tolerance=0.2)
        assert out[:, :, 3].mean() > 0.9

    def test_alpha_range(self, sample_image):
        out = chroma_key(sample_image, key_color=(0.3, 0.0, 0.0))
        assert out[:, :, 3].min() >= 0.0
        assert out[:, :, 3].max() <= 1.0


class TestLumaKey:
    def test_removes_black(self):
        img = np.zeros((16, 16, 3), dtype=np.float32)
        out = luma_key(img, low=0.0, high=0.1)
        assert out.shape == (16, 16, 4)
        assert out[:, :, 3].mean() < 0.1

    def test_keeps_bright(self):
        img = np.ones((16, 16, 3), dtype=np.float32)
        out = luma_key(img, low=0.0, high=0.1)
        assert out[:, :, 3].mean() > 0.9

    def test_alpha_in_range(self, sample_image):
        out = luma_key(sample_image)
        assert out[:, :, 3].min() >= 0.0
        assert out[:, :, 3].max() <= 1.0


class TestCreateEdgeMask:
    def test_shape(self, sample_image):
        mask = create_edge_mask(sample_image)
        assert mask.ndim == 2
        assert mask.shape == sample_image.shape[:2]
        assert mask.dtype == np.float32

    def test_range(self, sample_image):
        mask = create_edge_mask(sample_image)
        assert mask.min() >= 0.0
        assert mask.max() <= 1.0

    def test_flat_image_no_edges(self):
        flat = np.full((32, 32, 3), 0.5, dtype=np.float32)
        mask = create_edge_mask(flat, radius=2.0, feather=1.0)
        assert mask.max() < 0.5

    def test_checkered_has_edges(self):
        img = np.zeros((32, 32, 3), dtype=np.float32)
        img[::2, ::2] = 1.0
        mask = create_edge_mask(img, radius=1.0, feather=0.5)
        assert mask.max() > 0.0


class TestBatchable:
    def test_blend_multiply_batch(self, sample_image, gray):
        result = blend_multiply([sample_image, gray], gray)
        assert isinstance(result, list)
        assert len(result) == 2
