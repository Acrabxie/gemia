"""AI image generation primitives powered by Gemini (Nano Banana).

Functions
---------
generate_image    : text prompt → new image (no input image needed)
edit_image        : edit an existing image with a natural-language instruction
style_transfer    : apply a visual style to an image / video frame
blend_images      : blend the input image with a second image file

All four functions delegate to ``GenerativeClient`` from
``gemia.ai.generative_client``.  ``edit_image``, ``style_transfer``, and
``blend_images`` have ``img`` as first argument and therefore receive
automatic per-frame bridging when the plan input is a video.
``generate_image`` has ``prompt`` as first argument — the engine handles it
as a standalone generative step without an input image.
"""
from __future__ import annotations

import cv2
import numpy as np

from gemia.primitives_common import Image, batchable, ensure_float32

# Import at module level so tests can patch "gemia.picture.generative.GenerativeClient"
try:
    from gemia.ai.generative_client import GenerativeClient
except ImportError:  # pragma: no cover — missing optional deps at import time
    GenerativeClient = None  # type: ignore[assignment,misc]


def generate_image(
    prompt: str,
    *,
    aspect_ratio: str = "16:9",
    style: str = "",
    model_tier: str = "flash",
) -> Image:
    """Generate an image from a text description using Gemini image generation (Nano Banana).

    This function creates a brand-new image from scratch — no input image is needed.
    Suitable for generating title cards, backgrounds, or standalone visual assets.

    Args:
        prompt: Text description of the image to generate.
        aspect_ratio: Target aspect ratio hint passed to the model, e.g. ``"16:9"``,
            ``"9:16"``, ``"1:1"``, ``"4:3"``. Default ``"16:9"``.
        style: Optional style hint, e.g. ``"oil painting"``, ``"cyberpunk neon"``.
            When provided it is appended to the prompt. Default ``""`` (no style).
        model_tier: ``"flash"`` (NB2, faster/cheaper) or ``"pro"`` (NB Pro, higher
            quality). Default ``"flash"``.

    Returns:
        float32 BGR ndarray, shape (H, W, 3), values in [0, 1].

    Raises:
        RuntimeError: If neither ``GEMINI_API_KEY`` nor ``OPENROUTER_API_KEY`` is set,
            or if the API call fails.
    """
    if style:
        full_prompt = f"{prompt}. Style: {style}. Aspect ratio: {aspect_ratio}."
    else:
        full_prompt = f"{prompt}. Aspect ratio: {aspect_ratio}."
    client = GenerativeClient(model_tier=model_tier)
    return client.generate_image_from_text(full_prompt)


@batchable
def edit_image(
    img: Image,
    *,
    instruction: str,
    model_tier: str = "flash",
) -> Image:
    """Edit an image based on a natural-language instruction using Gemini image generation.

    Uses Gemini's image-in / image-out capability to modify the input image
    according to the given instruction.  When used in a video plan, the engine
    applies this function to every frame automatically (same as any other
    ``gemia.picture.*`` function).

    Supports batch input via the ``@batchable`` decorator: pass a single image or
    a list of images; a list is returned for list input.

    Args:
        img: Input float32 BGR ndarray (single image or list via @batchable).
        instruction: Editing instruction, e.g. ``"make the background black and white"``,
            ``"add rain effect"``, ``"remove background"``.
        model_tier: ``"flash"`` or ``"pro"``. Default ``"flash"``.

    Returns:
        Edited float32 BGR ndarray (same shape as input).

    Raises:
        RuntimeError: If the API call fails or no API key is configured.
    """
    client = GenerativeClient(model_tier=model_tier)
    return client.generate_image_from_image_and_text(img, instruction)


@batchable
def style_transfer(
    img: Image,
    *,
    style_prompt: str,
    model_tier: str = "flash",
) -> Image:
    """Apply a visual style to an image using Gemini image generation (Nano Banana).

    Useful for artistic style conversions such as "cyberpunk neon", "水墨画 ink wash",
    "oil painting", "Studio Ghibli", "watercolor", etc.  When used in a video plan,
    the engine applies this to every frame automatically, effectively style-transferring
    the entire video.

    Supports batch input via the ``@batchable`` decorator.

    Args:
        img: Input float32 BGR ndarray to stylize.
        style_prompt: Style description, e.g. ``"cyberpunk neon with blue highlights"``,
            ``"traditional Chinese ink wash painting"``。
        model_tier: ``"flash"`` or ``"pro"``. Default ``"flash"``.

    Returns:
        Stylized float32 BGR ndarray.

    Raises:
        RuntimeError: If the API call fails or no API key is configured.
    """
    client = GenerativeClient(model_tier=model_tier)
    prompt = f"Apply this visual style to the image: {style_prompt}. Keep the same composition and subject."
    return client.generate_image_from_image_and_text(img, prompt)


def blend_images(
    img_a: Image,
    *,
    img_b_path: str,
    prompt: str,
    model_tier: str = "flash",
) -> Image:
    """Blend the input image with a second image (specified by path) guided by a prompt.

    The second image is loaded from ``img_b_path`` at call time, so the path must
    exist on disk.  ``img_b_path`` is a plain string (JSON-serializable) rather than
    an ndarray so it can be specified in plan ``args``.

    Args:
        img_a: Primary input float32 BGR ndarray (typically the current video frame or
            pipeline output).
        img_b_path: Absolute or relative path to the second image file (JPEG or PNG).
            The file must exist when this function is called.
        prompt: Blending guidance, e.g. ``"blend these two images seamlessly with a
            soft gradient transition"``.
        model_tier: ``"flash"`` or ``"pro"``. Default ``"flash"``.

    Returns:
        Blended float32 BGR ndarray.

    Raises:
        FileNotFoundError: If ``img_b_path`` does not exist or cannot be read.
        RuntimeError: If the API call fails or no API key is configured.
    """
    img_b_raw = cv2.imread(img_b_path)
    if img_b_raw is None:
        raise FileNotFoundError(f"blend_images: cannot read second image from '{img_b_path}'.")
    img_b = ensure_float32(img_b_raw)
    client = GenerativeClient(model_tier=model_tier)
    return client.blend_two_images(img_a, img_b, prompt)
