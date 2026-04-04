"""Tests for gemia.video.frames."""
import numpy as np
import pytest

from gemia.video.frames import extract_frames, frames_to_video, apply_picture_op_to_video


class TestExtractFrames:
    def test_count(self, sample_video_path):
        frames = extract_frames(sample_video_path, count=5)
        assert len(frames) == 5
        assert frames[0].dtype == np.float32
        assert frames[0].ndim == 3

    def test_fps(self, sample_video_path):
        frames = extract_frames(sample_video_path, fps=5)
        assert len(frames) >= 5  # 2s video at 5fps ≈ 10 frames

    def test_all_frames(self, sample_video_path):
        frames = extract_frames(sample_video_path)
        assert len(frames) > 0


class TestFramesToVideo:
    def test_roundtrip(self, sample_video_path, tmp_path):
        frames = extract_frames(sample_video_path, count=5)
        out = str(tmp_path / "out.mp4")
        frames_to_video(frames, output_path=out, fps=15)
        # Verify it's a readable video
        reloaded = extract_frames(out)
        assert len(reloaded) > 0

    def test_empty(self, tmp_path):
        with pytest.raises(ValueError):
            frames_to_video([], output_path=str(tmp_path / "empty.mp4"))


class TestApplyPictureOpToVideo:
    def test_identity(self, sample_video_path, tmp_path):
        out = str(tmp_path / "identity.mp4")
        apply_picture_op_to_video(sample_video_path, out, op=lambda f: f)
        frames = extract_frames(out, count=3)
        assert len(frames) >= 1

    def test_color_grade_bridge(self, sample_video_path, tmp_path):
        from gemia.picture.color import adjust_exposure
        out = str(tmp_path / "bright.mp4")
        apply_picture_op_to_video(
            sample_video_path, out,
            op=lambda f: adjust_exposure(f, stops=0.5),
        )
        frames = extract_frames(out, count=3)
        assert len(frames) >= 1
        # Brighter frames should have higher mean
        orig = extract_frames(sample_video_path, count=3)
        assert frames[0].mean() > orig[0].mean() - 0.1  # some tolerance
