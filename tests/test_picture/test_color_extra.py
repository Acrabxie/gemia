"""Tests for apply_3d_lut, color_space_convert, lift_gamma_gain, log_to_linear."""
import tempfile
from pathlib import Path

import numpy as np
import pytest

from gemia.picture.color import apply_3d_lut, color_space_convert, lift_gamma_gain, log_to_linear


def _write_identity_cube(path: str, size: int = 4) -> None:
    """Write a minimal identity .cube LUT file."""
    with open(path, "w") as f:
        f.write("TITLE \"Identity\"\n")
        f.write(f"LUT_3D_SIZE {size}\n")
        for bi in range(size):
            for gi in range(size):
                for ri in range(size):
                    r = ri / (size - 1)
                    g = gi / (size - 1)
                    b = bi / (size - 1)
                    f.write(f"{r:.6f} {g:.6f} {b:.6f}\n")


class TestApply3dLut:
    def test_identity_lut(self, sample_image, tmp_path):
        lut_path = str(tmp_path / "identity.cube")
        _write_identity_cube(lut_path, size=4)
        out = apply_3d_lut(sample_image, lut_path=lut_path)
        assert out.shape == sample_image.shape
        assert out.dtype == np.float32

    def test_output_range(self, sample_image, tmp_path):
        lut_path = str(tmp_path / "identity.cube")
        _write_identity_cube(lut_path, size=4)
        out = apply_3d_lut(sample_image, lut_path=lut_path)
        assert out.min() >= 0.0
        assert out.max() <= 1.0

    def test_identity_approx(self, tmp_path):
        img = np.linspace(0, 1, 3 * 16 * 16, dtype=np.float32).reshape(16, 16, 3)
        img = np.clip(img, 0, 1)
        lut_path = str(tmp_path / "id.cube")
        _write_identity_cube(lut_path, size=16)
        out = apply_3d_lut(img, lut_path=lut_path)
        np.testing.assert_allclose(out, img, atol=0.07)

    def test_invalid_file_raises(self, tmp_path):
        bad_path = str(tmp_path / "bad.cube")
        Path(bad_path).write_text("TITLE bad\n")
        with pytest.raises(ValueError):
            apply_3d_lut(np.zeros((4, 4, 3), dtype=np.float32), lut_path=bad_path)


class TestColorSpaceConvert:
    def test_bgr_to_bgr(self, sample_image):
        out = color_space_convert(sample_image, from_space="bgr", to_space="bgr")
        np.testing.assert_allclose(out, sample_image, atol=1e-5)

    def test_bgr_to_lab_shape(self, sample_image):
        out = color_space_convert(sample_image, from_space="bgr", to_space="lab")
        assert out.shape == sample_image.shape
        assert out.dtype == np.float32

    def test_bgr_to_hsl_roundtrip(self, sample_image):
        hsl = color_space_convert(sample_image, from_space="bgr", to_space="hsl")
        back = color_space_convert(hsl, from_space="hsl", to_space="bgr")
        assert back.shape == sample_image.shape

    def test_bgr_to_yuv_shape(self, sample_image):
        out = color_space_convert(sample_image, from_space="bgr", to_space="yuv")
        assert out.shape == sample_image.shape

    def test_unknown_space_raises(self, sample_image):
        with pytest.raises(ValueError):
            color_space_convert(sample_image, from_space="bgr", to_space="xyz_unknown")


class TestLiftGammaGain:
    def test_neutral(self, sample_image):
        out = lift_gamma_gain(sample_image)
        np.testing.assert_allclose(out, sample_image, atol=1e-5)

    def test_gain_brightens(self, sample_image):
        out = lift_gamma_gain(sample_image, gain=(2.0, 2.0, 2.0))
        assert out.mean() >= sample_image.mean()

    def test_lift_brightens_shadows(self):
        dark = np.full((8, 8, 3), 0.1, dtype=np.float32)
        out = lift_gamma_gain(dark, lift=(0.2, 0.2, 0.2))
        assert out.mean() > dark.mean()

    def test_output_range(self, sample_image):
        out = lift_gamma_gain(sample_image, lift=(0.1, -0.1, 0.0), gamma=(0.8, 1.2, 1.0), gain=(1.5, 0.8, 1.0))
        assert out.min() >= 0.0
        assert out.max() <= 1.0

    def test_batchable(self, sample_image):
        result = lift_gamma_gain([sample_image, sample_image])
        assert isinstance(result, list)
        assert len(result) == 2


class TestLogToLinear:
    def test_slog2(self, sample_image):
        out = log_to_linear(sample_image, log_format="slog2")
        assert out.shape == sample_image.shape
        assert out.dtype == np.float32

    def test_slog3(self, sample_image):
        out = log_to_linear(sample_image, log_format="slog3")
        assert out.shape == sample_image.shape

    def test_logc(self, sample_image):
        out = log_to_linear(sample_image, log_format="logc")
        assert out.shape == sample_image.shape

    def test_log3g10(self, sample_image):
        out = log_to_linear(sample_image, log_format="log3g10")
        assert out.shape == sample_image.shape

    def test_unknown_format_raises(self, sample_image):
        with pytest.raises(ValueError):
            log_to_linear(sample_image, log_format="vlog_unknown")

    def test_non_negative(self, sample_image):
        out = log_to_linear(sample_image, log_format="slog2")
        assert out.min() >= 0.0

    def test_batchable(self, sample_image):
        result = log_to_linear([sample_image, sample_image])
        assert isinstance(result, list)
        assert len(result) == 2
