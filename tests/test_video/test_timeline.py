"""Tests for gemia.video.timeline."""
import pytest

from gemia.video.timeline import cut, concat, speed, reverse
from gemia.video.analysis import get_metadata


class TestCut:
    def test_basic(self, sample_video_path, tmp_path):
        out = str(tmp_path / "cut.mp4")
        cut(sample_video_path, out, start_sec=0.0, end_sec=1.0)
        meta = get_metadata(out)
        assert 0.5 < meta["duration"] < 1.5


class TestConcat:
    def test_two_clips(self, sample_video_path, tmp_path):
        out = str(tmp_path / "concat.mp4")
        concat([sample_video_path, sample_video_path], out)
        meta = get_metadata(out)
        orig = get_metadata(sample_video_path)
        assert meta["duration"] > orig["duration"] * 1.5

    def test_empty(self, tmp_path):
        with pytest.raises(ValueError):
            concat([], str(tmp_path / "empty.mp4"))


class TestSpeed:
    def test_faster(self, sample_video_path, tmp_path):
        out = str(tmp_path / "fast.mp4")
        speed(sample_video_path, out, factor=2.0)
        meta = get_metadata(out)
        orig = get_metadata(sample_video_path)
        assert meta["duration"] < orig["duration"]

    def test_invalid(self, sample_video_path, tmp_path):
        with pytest.raises(ValueError):
            speed(sample_video_path, str(tmp_path / "bad.mp4"), factor=0)


class TestReverse:
    def test_basic(self, sample_video_path, tmp_path):
        out = str(tmp_path / "reversed.mp4")
        reverse(sample_video_path, out)
        meta = get_metadata(out)
        assert meta["duration"] > 0
