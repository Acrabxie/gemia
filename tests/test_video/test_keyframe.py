"""Tests for KeyframeTrack and apply_animated_op."""
import numpy as np
import pytest

from gemia.video.keyframe import KeyframeTrack, apply_animated_op


class TestKeyframeTrack:
    def test_empty_track_returns_zero(self):
        track = KeyframeTrack()
        assert track.evaluate(0.5) == 0.0

    def test_single_keyframe(self):
        track = KeyframeTrack()
        track.add_keyframe(0.0, 42.0)
        assert track.evaluate(0.0) == 42.0
        assert track.evaluate(1.0) == 42.0
        assert track.evaluate(-1.0) == 42.0

    def test_linear_interpolation(self):
        track = KeyframeTrack()
        track.add_keyframe(0.0, 0.0)
        track.add_keyframe(1.0, 1.0)
        val = track.evaluate(0.5)
        assert abs(val - 0.5) < 1e-5

    def test_linear_at_endpoints(self):
        track = KeyframeTrack()
        track.add_keyframe(0.0, 10.0)
        track.add_keyframe(2.0, 20.0)
        assert abs(track.evaluate(0.0) - 10.0) < 1e-5
        assert abs(track.evaluate(2.0) - 20.0) < 1e-5

    def test_ease_in(self):
        track = KeyframeTrack()
        track.add_keyframe(0.0, 0.0, easing="ease_in")
        track.add_keyframe(1.0, 1.0)
        mid = track.evaluate(0.5)
        assert mid < 0.5

    def test_ease_out(self):
        track = KeyframeTrack()
        track.add_keyframe(0.0, 0.0, easing="ease_out")
        track.add_keyframe(1.0, 1.0)
        mid = track.evaluate(0.5)
        assert mid > 0.5

    def test_ease_in_out(self):
        track = KeyframeTrack()
        track.add_keyframe(0.0, 0.0, easing="ease_in_out")
        track.add_keyframe(1.0, 1.0)
        mid = track.evaluate(0.5)
        assert abs(mid - 0.5) < 0.01

    def test_bezier(self):
        track = KeyframeTrack()
        track.add_keyframe(0.0, 0.0, easing="bezier")
        track.add_keyframe(1.0, 1.0)
        val = track.evaluate(0.5)
        assert 0.0 <= val <= 1.0

    def test_invalid_easing_raises(self):
        track = KeyframeTrack()
        with pytest.raises(ValueError):
            track.add_keyframe(0.0, 0.0, easing="magic_easing")

    def test_out_of_bounds_clamped(self):
        track = KeyframeTrack()
        track.add_keyframe(1.0, 5.0)
        track.add_keyframe(2.0, 10.0)
        assert track.evaluate(0.0) == 5.0
        assert track.evaluate(3.0) == 10.0

    def test_keyframes_sorted_on_add(self):
        track = KeyframeTrack()
        track.add_keyframe(1.0, 1.0)
        track.add_keyframe(0.0, 0.0)
        assert abs(track.evaluate(0.5) - 0.5) < 1e-5

    def test_multiple_segments(self):
        track = KeyframeTrack()
        track.add_keyframe(0.0, 0.0)
        track.add_keyframe(1.0, 10.0)
        track.add_keyframe(2.0, 0.0)
        assert abs(track.evaluate(1.0) - 10.0) < 1e-5
        assert track.evaluate(1.5) > 0.0

    def test_all_easings_run(self):
        for easing in ["linear", "ease_in", "ease_out", "ease_in_out", "bezier"]:
            track = KeyframeTrack()
            track.add_keyframe(0.0, 0.0, easing=easing)
            track.add_keyframe(1.0, 1.0)
            val = track.evaluate(0.5)
            assert 0.0 <= val <= 1.0, f"easing={easing} out of range: {val}"


class TestApplyAnimatedOp:
    def test_returns_path(self, sample_video_path, tmp_path):
        from gemia.picture.color import adjust_exposure
        out = str(tmp_path / "animated.mp4")
        track = KeyframeTrack()
        track.add_keyframe(0.0, 0.0)
        track.add_keyframe(1.0, 1.0)
        result = apply_animated_op(
            sample_video_path, out,
            op_fn=adjust_exposure,
            param_name="stops",
            keyframe_track=track,
        )
        assert result == out

    def test_output_exists(self, sample_video_path, tmp_path):
        import os
        from gemia.picture.color import adjust_exposure
        out = str(tmp_path / "animated2.mp4")
        track = KeyframeTrack()
        track.add_keyframe(0.0, 0.0)
        apply_animated_op(
            sample_video_path, out,
            op_fn=adjust_exposure,
            param_name="stops",
            keyframe_track=track,
        )
        assert os.path.exists(out)

    def test_invalid_path_raises(self, tmp_path):
        from gemia.picture.color import adjust_exposure
        out = str(tmp_path / "out.mp4")
        track = KeyframeTrack()
        with pytest.raises(FileNotFoundError):
            apply_animated_op(
                "/nonexistent/video.mp4", out,
                op_fn=adjust_exposure,
                param_name="stops",
                keyframe_track=track,
            )
