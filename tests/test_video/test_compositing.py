"""Tests for gemia.video.compositing."""
from gemia.video.compositing import overlay
from gemia.video.analysis import get_metadata


class TestOverlay:
    def test_self_overlay(self, sample_video_path, tmp_path):
        out = str(tmp_path / "overlay.mp4")
        overlay(sample_video_path, sample_video_path, out, x=10, y=10)
        meta = get_metadata(out)
        assert meta["duration"] > 0
