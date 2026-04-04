"""Tests for gemia.video.generative — mocked API calls."""
from __future__ import annotations

import pytest
from unittest.mock import patch, MagicMock


class TestGenerateVideo:
    def test_returns_path(self):
        with patch("gemia.video.generative.VeoClient") as MockClient:
            MockClient.return_value.generate.return_value = "/tmp/fake.mp4"
            from gemia.video.generative import generate_video
            result = generate_video("cityscape at night")
        assert isinstance(result, str)
        assert result.endswith(".mp4")

    def test_params_passed(self):
        with patch("gemia.video.generative.VeoClient") as MockClient:
            instance = MockClient.return_value
            instance.generate.return_value = "/tmp/fake.mp4"
            from gemia.video.generative import generate_video
            generate_video("test", duration=10.0, aspect_ratio="9:16")
            instance.generate.assert_called_once_with("test", duration=10.0, aspect_ratio="9:16")

    def test_default_params(self):
        with patch("gemia.video.generative.VeoClient") as MockClient:
            instance = MockClient.return_value
            instance.generate.return_value = "/tmp/default.mp4"
            from gemia.video.generative import generate_video
            generate_video("ocean waves")
            instance.generate.assert_called_once_with("ocean waves", duration=5.0, aspect_ratio="16:9")

    def test_client_instantiated(self):
        with patch("gemia.video.generative.VeoClient") as MockClient:
            MockClient.return_value.generate.return_value = "/tmp/fake.mp4"
            from gemia.video.generative import generate_video
            generate_video("test prompt")
        MockClient.assert_called_once()


class TestGenerateVideoFromImage:
    def test_returns_path(self, tmp_path):
        import cv2
        import numpy as np
        img_path = str(tmp_path / "frame.png")
        cv2.imwrite(img_path, np.zeros((64, 64, 3), dtype=np.uint8))

        with patch("gemia.video.generative.VeoClient") as MockClient:
            MockClient.return_value.generate_from_image.return_value = "/tmp/fake.mp4"
            from gemia.video.generative import generate_video_from_image
            result = generate_video_from_image(img_path, prompt="pan left")
        assert isinstance(result, str)

    def test_args_passed(self, tmp_path):
        import cv2
        import numpy as np
        img_path = str(tmp_path / "frame.png")
        cv2.imwrite(img_path, np.zeros((64, 64, 3), dtype=np.uint8))

        with patch("gemia.video.generative.VeoClient") as MockClient:
            instance = MockClient.return_value
            instance.generate_from_image.return_value = "/tmp/fake.mp4"
            from gemia.video.generative import generate_video_from_image
            generate_video_from_image(img_path, prompt="zoom out slowly", duration=8.0)
            instance.generate_from_image.assert_called_once_with(
                img_path, "zoom out slowly", duration=8.0
            )

    def test_default_duration(self, tmp_path):
        import cv2
        import numpy as np
        img_path = str(tmp_path / "frame2.png")
        cv2.imwrite(img_path, np.zeros((64, 64, 3), dtype=np.uint8))

        with patch("gemia.video.generative.VeoClient") as MockClient:
            instance = MockClient.return_value
            instance.generate_from_image.return_value = "/tmp/fake.mp4"
            from gemia.video.generative import generate_video_from_image
            generate_video_from_image(img_path, prompt="tilt up")
            _, call_kwargs = instance.generate_from_image.call_args
            assert call_kwargs.get("duration") == 5.0


class TestExtendVideo:
    def test_returns_path(self):
        with patch("gemia.video.generative.VeoClient") as MockClient:
            MockClient.return_value.extend.return_value = "/tmp/fake_ext.mp4"
            from gemia.video.generative import extend_video
            result = extend_video("/fake/input.mp4", prompt="fade to black")
        assert isinstance(result, str)

    def test_args_passed(self):
        with patch("gemia.video.generative.VeoClient") as MockClient:
            instance = MockClient.return_value
            instance.extend.return_value = "/tmp/fake_ext.mp4"
            from gemia.video.generative import extend_video
            extend_video("/fake/input.mp4", prompt="camera pulls back", duration=5.0)
            instance.extend.assert_called_once_with(
                "/fake/input.mp4", "camera pulls back", duration=5.0
            )

    def test_default_duration(self):
        with patch("gemia.video.generative.VeoClient") as MockClient:
            instance = MockClient.return_value
            instance.extend.return_value = "/tmp/fake_ext.mp4"
            from gemia.video.generative import extend_video
            extend_video("/fake/input.mp4", prompt="slow fade out")
            _, call_kwargs = instance.extend.call_args
            assert call_kwargs.get("duration") == 3.0
