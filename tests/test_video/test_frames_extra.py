"""Tests for optical_flow_interpolate, retime, stabilize."""
import numpy as np
import pytest

from gemia.video.frames import optical_flow_interpolate, retime, stabilize


class TestOpticalFlowInterpolate:
    def test_single_step(self, sample_image):
        a = sample_image
        b = np.zeros_like(sample_image)
        frames = optical_flow_interpolate(a, b, steps=1)
        assert len(frames) == 1
        assert frames[0].shape == a.shape
        assert frames[0].dtype == np.float32

    def test_multiple_steps(self, sample_image):
        a = sample_image
        b = np.ones_like(sample_image)
        frames = optical_flow_interpolate(a, b, steps=3)
        assert len(frames) == 3

    def test_output_range(self, sample_image):
        b = np.zeros_like(sample_image)
        frames = optical_flow_interpolate(sample_image, b, steps=2)
        for f in frames:
            assert f.min() >= 0.0
            assert f.max() <= 1.0


class TestRetime:
    def test_returns_path(self, sample_video_path, tmp_path):
        out = str(tmp_path / "retimed.mp4")
        result = retime(
            sample_video_path, out,
            speed_map=[(0.0, 1.0), (1.0, 2.0)],
            method="linear",
        )
        assert result == out

    def test_output_exists(self, sample_video_path, tmp_path):
        import os
        out = str(tmp_path / "retimed2.mp4")
        retime(
            sample_video_path, out,
            speed_map=[(0.0, 1.0)],
            method="linear",
        )
        assert os.path.exists(out)

    def test_optical_flow_method(self, sample_video_path, tmp_path):
        import os
        out = str(tmp_path / "retimed_of.mp4")
        retime(
            sample_video_path, out,
            speed_map=[(0.0, 0.5)],
            method="optical_flow",
        )
        assert os.path.exists(out)


class TestStabilize:
    def test_returns_path(self, sample_video_path, tmp_path):
        out = str(tmp_path / "stabilized.mp4")
        result = stabilize(sample_video_path, out, smoothness=5)
        assert result == out

    def test_output_exists(self, sample_video_path, tmp_path):
        import os
        out = str(tmp_path / "stabilized2.mp4")
        stabilize(sample_video_path, out, smoothness=5)
        assert os.path.exists(out)

    def test_invalid_path_raises(self, tmp_path):
        out = str(tmp_path / "stab.mp4")
        with pytest.raises(FileNotFoundError):
            stabilize("/nonexistent/video.mp4", out)
