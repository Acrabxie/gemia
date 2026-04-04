"""Tests for gemia.video.analysis."""
from gemia.video.analysis import get_metadata, detect_scenes


class TestGetMetadata:
    def test_basic(self, sample_video_path):
        meta = get_metadata(sample_video_path)
        assert meta["width"] == 128
        assert meta["height"] == 128
        assert 1.5 < meta["duration"] < 2.5
        assert meta["fps"] > 0
        assert meta["codec"] == "h264"


class TestDetectScenes:
    def test_basic(self, sample_video_path):
        scenes = detect_scenes(sample_video_path, threshold=30.0)
        assert isinstance(scenes, list)
        # testsrc2 has scene changes built in
