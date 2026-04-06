"""Integration tests for Nano Banana + Veo generative primitives.

Validates that:
- All 5 core generative functions can be imported and inspected
- They appear in the AI catalog (registry)
- The engine correctly routes generative plan steps (API calls mocked)
"""
from __future__ import annotations

import inspect
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

# ---------------------------------------------------------------------------
# 1. Import checks
# ---------------------------------------------------------------------------

def test_picture_generative_imports():
    from gemia.picture.generative import generate_image, edit_image, style_transfer
    assert callable(generate_image)
    assert callable(edit_image)
    assert callable(style_transfer)


def test_video_generative_imports():
    from gemia.video.generative import generate_video, generate_video_from_image
    assert callable(generate_video)
    assert callable(generate_video_from_image)


def test_generative_function_signatures():
    from gemia.picture.generative import generate_image, edit_image, style_transfer
    from gemia.video.generative import generate_video, generate_video_from_image

    sig = inspect.signature(generate_image)
    assert "prompt" in sig.parameters

    sig = inspect.signature(edit_image)
    assert "instruction" in sig.parameters

    sig = inspect.signature(style_transfer)
    assert "style_prompt" in sig.parameters

    sig = inspect.signature(generate_video)
    assert "prompt" in sig.parameters

    sig = inspect.signature(generate_video_from_image)
    # first positional arg is input_path / image_path
    params = list(sig.parameters.keys())
    assert len(params) >= 1


# ---------------------------------------------------------------------------
# 2. Registry checks
# ---------------------------------------------------------------------------

def test_registry_contains_generative_functions():
    from gemia.registry import get_registry
    r = get_registry()
    expected = [
        "gemia.picture.generative.generate_image",
        "gemia.picture.generative.edit_image",
        "gemia.picture.generative.style_transfer",
        "gemia.video.generative.generate_video",
        "gemia.video.generative.generate_video_from_image",
    ]
    for fqn in expected:
        assert fqn in r, f"Missing from registry: {fqn}"


def test_catalog_contains_generative_functions():
    from gemia.registry import catalog_for_prompt
    catalog = catalog_for_prompt()
    for fn_name in ["generate_image", "style_transfer", "edit_image",
                    "generate_video", "generate_video_from_image"]:
        assert fn_name in catalog, f"Missing from AI catalog: {fn_name}"


# ---------------------------------------------------------------------------
# 3. Engine routing checks (API calls mocked)
# ---------------------------------------------------------------------------

def _fake_image() -> np.ndarray:
    return np.zeros((64, 64, 3), dtype=np.float32)


def test_engine_routes_generate_image(tmp_path):
    """Engine should call generate_image (no input_path) and save result to PNG."""
    from gemia.engine import PlanEngine

    plan = {
        "version": "2.0",
        "goal": "test generate_image routing",
        "steps": [
            {
                "id": "step_1",
                "function": "gemia.picture.generative.generate_image",
                "args": {"prompt": "a blue sky", "aspect_ratio": "16:9"},
                "input": "$input",
                "output": "$output",
            }
        ],
    }

    # generate_image doesn't read input, but engine validates path exists
    dummy = tmp_path / "dummy.mp4"
    dummy.write_bytes(b"")

    with patch("gemia.picture.generative.GenerativeClient") as MockClient:
        instance = MockClient.return_value
        instance.generate_image_from_text.return_value = _fake_image()

        engine = PlanEngine(root_dir=tmp_path)
        out = engine.execute(plan, str(dummy), str(tmp_path / "out.mp4"))

        instance.generate_image_from_text.assert_called_once()
        assert out.endswith(".png")


def test_engine_routes_style_transfer_on_video(tmp_path):
    """Engine should apply style_transfer per-frame via apply_picture_op_to_video."""
    import cv2
    from gemia.engine import PlanEngine

    # Create a tiny real video to avoid ffmpeg errors
    video_path = str(tmp_path / "input.mp4")
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(video_path, fourcc, 1, (64, 64))
    for _ in range(2):
        writer.write(np.zeros((64, 64, 3), dtype=np.uint8))
    writer.release()

    plan = {
        "version": "2.0",
        "goal": "test style_transfer routing",
        "steps": [
            {
                "id": "step_1",
                "function": "gemia.picture.generative.style_transfer",
                "args": {"style_prompt": "cyberpunk neon"},
                "input": "$input",
                "output": "$output",
            }
        ],
    }

    with patch("gemia.picture.generative.GenerativeClient") as MockClient:
        instance = MockClient.return_value
        instance.generate_image_from_image_and_text.return_value = _fake_image()

        engine = PlanEngine(root_dir=tmp_path)
        out = engine.execute(plan, video_path, str(tmp_path / "out.mp4"))

        assert instance.generate_image_from_image_and_text.called
        assert out.endswith(".mp4")
