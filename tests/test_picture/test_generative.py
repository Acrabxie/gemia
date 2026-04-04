"""Tests for gemia.picture.generative — mocked API calls."""
from __future__ import annotations

import numpy as np
import pytest
from unittest.mock import patch, MagicMock

from gemia.picture.generative import generate_image, edit_image, style_transfer, blend_images


FAKE_IMG = np.random.rand(64, 64, 3).astype(np.float32)


class TestGenerateImage:
    def test_returns_ndarray(self):
        with patch("gemia.picture.generative.GenerativeClient") as MockClient:
            MockClient.return_value.generate_image_from_text.return_value = FAKE_IMG
            result = generate_image("cyberpunk city", aspect_ratio="16:9")
        assert isinstance(result, np.ndarray)
        assert result.dtype == np.float32

    def test_prompt_built_with_style(self):
        with patch("gemia.picture.generative.GenerativeClient") as MockClient:
            instance = MockClient.return_value
            instance.generate_image_from_text.return_value = FAKE_IMG
            generate_image("sunset", style="oil painting", aspect_ratio="4:3")
            call_args = instance.generate_image_from_text.call_args[0][0]
            assert "oil painting" in call_args
            assert "4:3" in call_args

    def test_prompt_built_without_style(self):
        with patch("gemia.picture.generative.GenerativeClient") as MockClient:
            instance = MockClient.return_value
            instance.generate_image_from_text.return_value = FAKE_IMG
            generate_image("mountain lake", aspect_ratio="16:9")
            call_args = instance.generate_image_from_text.call_args[0][0]
            assert "mountain lake" in call_args
            assert "16:9" in call_args
            assert "Style:" not in call_args

    def test_model_tier_passed(self):
        with patch("gemia.picture.generative.GenerativeClient") as MockClient:
            MockClient.return_value.generate_image_from_text.return_value = FAKE_IMG
            generate_image("test", model_tier="pro")
            MockClient.assert_called_once_with(model_tier="pro")

    def test_default_tier_is_flash(self):
        with patch("gemia.picture.generative.GenerativeClient") as MockClient:
            MockClient.return_value.generate_image_from_text.return_value = FAKE_IMG
            generate_image("test")
            MockClient.assert_called_once_with(model_tier="flash")


class TestEditImage:
    def test_returns_ndarray(self, sample_image):
        with patch("gemia.picture.generative.GenerativeClient") as MockClient:
            MockClient.return_value.generate_image_from_image_and_text.return_value = FAKE_IMG
            result = edit_image(sample_image, instruction="make it purple")
        assert isinstance(result, np.ndarray)

    def test_instruction_passed(self, sample_image):
        with patch("gemia.picture.generative.GenerativeClient") as MockClient:
            instance = MockClient.return_value
            instance.generate_image_from_image_and_text.return_value = FAKE_IMG
            edit_image(sample_image, instruction="add dramatic lighting")
            _, call_prompt = instance.generate_image_from_image_and_text.call_args[0]
            assert "add dramatic lighting" == call_prompt

    def test_batch(self, sample_image):
        with patch("gemia.picture.generative.GenerativeClient") as MockClient:
            MockClient.return_value.generate_image_from_image_and_text.return_value = FAKE_IMG
            result = edit_image([sample_image, sample_image], instruction="add stars")
        assert isinstance(result, list)
        assert len(result) == 2

    def test_batch_all_ndarrays(self, sample_image):
        with patch("gemia.picture.generative.GenerativeClient") as MockClient:
            MockClient.return_value.generate_image_from_image_and_text.return_value = FAKE_IMG
            result = edit_image([sample_image, sample_image, sample_image], instruction="blur edges")
        assert all(isinstance(r, np.ndarray) for r in result)


class TestStyleTransfer:
    def test_single_image(self, sample_image):
        with patch("gemia.picture.generative.GenerativeClient") as MockClient:
            MockClient.return_value.generate_image_from_image_and_text.return_value = FAKE_IMG
            result = style_transfer(sample_image, style_prompt="cyberpunk neon")
        assert isinstance(result, np.ndarray)
        assert result.dtype == np.float32

    def test_batch(self, sample_image):
        with patch("gemia.picture.generative.GenerativeClient") as MockClient:
            MockClient.return_value.generate_image_from_image_and_text.return_value = FAKE_IMG
            result = style_transfer([sample_image] * 3, style_prompt="watercolor")
        assert len(result) == 3

    def test_style_in_prompt(self, sample_image):
        with patch("gemia.picture.generative.GenerativeClient") as MockClient:
            instance = MockClient.return_value
            instance.generate_image_from_image_and_text.return_value = FAKE_IMG
            style_transfer(sample_image, style_prompt="Studio Ghibli")
            _, call_prompt = instance.generate_image_from_image_and_text.call_args[0]
            assert "Studio Ghibli" in call_prompt

    def test_model_tier_pro(self, sample_image):
        with patch("gemia.picture.generative.GenerativeClient") as MockClient:
            MockClient.return_value.generate_image_from_image_and_text.return_value = FAKE_IMG
            style_transfer(sample_image, style_prompt="oil painting", model_tier="pro")
            MockClient.assert_called_once_with(model_tier="pro")


class TestBlendImages:
    def test_returns_ndarray(self, sample_image, tmp_path):
        import cv2
        from gemia.primitives_common import to_uint8
        img_b_path = str(tmp_path / "img_b.png")
        cv2.imwrite(img_b_path, to_uint8(sample_image))

        with patch("gemia.picture.generative.GenerativeClient") as MockClient:
            MockClient.return_value.blend_two_images.return_value = FAKE_IMG
            result = blend_images(sample_image, img_b_path=img_b_path, prompt="blend these two")
        assert isinstance(result, np.ndarray)

    def test_dtype_float32(self, sample_image, tmp_path):
        import cv2
        from gemia.primitives_common import to_uint8
        img_b_path = str(tmp_path / "img_b.png")
        cv2.imwrite(img_b_path, to_uint8(sample_image))

        with patch("gemia.picture.generative.GenerativeClient") as MockClient:
            MockClient.return_value.blend_two_images.return_value = FAKE_IMG
            result = blend_images(sample_image, img_b_path=img_b_path, prompt="merge softly")
        assert result.dtype == np.float32

    def test_missing_img_b_raises(self, sample_image):
        with patch("gemia.picture.generative.GenerativeClient"):
            with pytest.raises((FileNotFoundError, Exception)):
                blend_images(sample_image, img_b_path="/nonexistent/path.png", prompt="test")

    def test_prompt_passed_to_client(self, sample_image, tmp_path):
        import cv2
        from gemia.primitives_common import to_uint8
        img_b_path = str(tmp_path / "img_b.png")
        cv2.imwrite(img_b_path, to_uint8(sample_image))

        with patch("gemia.picture.generative.GenerativeClient") as MockClient:
            instance = MockClient.return_value
            instance.blend_two_images.return_value = FAKE_IMG
            blend_images(sample_image, img_b_path=img_b_path, prompt="blend seamlessly")
            call_args = instance.blend_two_images.call_args
            # Third positional arg is the prompt
            assert call_args[0][2] == "blend seamlessly"
