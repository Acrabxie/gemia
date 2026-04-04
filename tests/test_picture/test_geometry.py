"""Tests for gemia.picture.geometry."""
import numpy as np
import pytest

from gemia.picture.geometry import resize, crop, rotate, perspective_transform


class TestResize:
    def test_resize_by_scale(self, sample_image):
        out = resize(sample_image, scale=0.5)
        assert out.shape == (32, 32, 3)
        assert out.dtype == np.float32

    def test_resize_by_width(self, sample_image):
        out = resize(sample_image, width=128)
        assert out.shape[1] == 128
        assert out.shape[0] == 128  # aspect ratio preserved

    def test_resize_by_height(self, sample_image):
        out = resize(sample_image, height=32)
        assert out.shape[0] == 32

    def test_resize_explicit_wh(self, sample_image):
        out = resize(sample_image, width=100, height=50)
        assert out.shape == (50, 100, 3)

    def test_resize_batch(self, sample_image):
        batch = [sample_image, sample_image]
        out = resize(batch, scale=2.0)
        assert len(out) == 2
        assert out[0].shape == (128, 128, 3)

    def test_resize_no_args(self, sample_image):
        with pytest.raises(ValueError):
            resize(sample_image)

    def test_resize_uint8_auto_convert(self, sample_image_uint8):
        out = resize(sample_image_uint8, scale=0.5)
        assert out.dtype == np.float32


class TestCrop:
    def test_crop_basic(self, sample_image):
        out = crop(sample_image, x=10, y=10, width=20, height=20)
        assert out.shape == (20, 20, 3)

    def test_crop_out_of_bounds(self, sample_image):
        with pytest.raises(ValueError):
            crop(sample_image, x=50, y=50, width=30, height=30)

    def test_crop_batch(self, sample_image):
        out = crop([sample_image, sample_image], x=0, y=0, width=32, height=32)
        assert len(out) == 2


class TestRotate:
    def test_rotate_90(self, sample_image):
        out = rotate(sample_image, angle=90)
        assert out.shape == sample_image.shape

    def test_rotate_expand(self, sample_image):
        out = rotate(sample_image, angle=45, expand=True)
        # expanded output should be larger
        assert out.shape[0] >= sample_image.shape[0]


class TestPerspectiveTransform:
    def test_identity_transform(self, sample_image):
        h, w = sample_image.shape[:2]
        src = np.array([[0, 0], [w, 0], [w, h], [0, h]], dtype=np.float32)
        dst = src.copy()
        out = perspective_transform(sample_image, src_points=src, dst_points=dst)
        assert out.shape == sample_image.shape
        np.testing.assert_allclose(out, sample_image, atol=0.01)

    def test_bad_points(self, sample_image):
        with pytest.raises(ValueError):
            perspective_transform(
                sample_image,
                src_points=np.zeros((3, 2)),
                dst_points=np.zeros((3, 2)),
            )
