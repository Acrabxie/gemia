"""Tests for gemia.picture.analysis."""
import numpy as np
import pytest

from gemia.picture.analysis import histogram, dominant_colors, edge_detect


class TestHistogram:
    def test_bgr(self, sample_image):
        h = histogram(sample_image)
        assert set(h.keys()) == {"b", "g", "r"}
        assert h["b"].shape == (256,)

    def test_gray(self, sample_gray):
        h = histogram(sample_gray)
        assert "gray" in h

    def test_batch(self, sample_image):
        out = histogram([sample_image, sample_image])
        assert len(out) == 2


class TestDominantColors:
    def test_basic(self, sample_image):
        colors = dominant_colors(sample_image, k=3)
        assert colors.shape == (3, 3)
        assert colors.dtype == np.float32

    def test_k1(self, sample_image):
        colors = dominant_colors(sample_image, k=1)
        assert colors.shape == (1, 3)


class TestEdgeDetect:
    def test_canny(self, sample_image):
        edges = edge_detect(sample_image, method="canny")
        assert edges.ndim == 2
        assert edges.dtype == np.float32
        assert edges.min() >= 0 and edges.max() <= 1

    def test_sobel(self, sample_image):
        edges = edge_detect(sample_image, method="sobel")
        assert edges.ndim == 2

    def test_gray_input(self, sample_gray):
        edges = edge_detect(sample_gray)
        assert edges.ndim == 2

    def test_unknown_method(self, sample_image):
        with pytest.raises(ValueError):
            edge_detect(sample_image, method="laplacian")
