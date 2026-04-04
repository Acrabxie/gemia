"""AI video generation primitives powered by Veo 3.1 via laozhang.ai.

Functions
---------
generate_video             : text → video file path
generate_video_from_image  : image file + text → video file path
extend_video               : video file + text → extended video file path

All three functions delegate to ``VeoClient`` from ``gemia.ai.veo_client``.
The engine routes ``generate_video`` specially (no ``input_path`` needed);
``generate_video_from_image`` and ``extend_video`` receive the current
pipeline file path as their first positional argument.
"""
from __future__ import annotations

# Import at module level so tests can patch "gemia.video.generative.VeoClient"
try:
    from gemia.ai.veo_client import VeoClient
except ImportError:  # pragma: no cover — missing optional deps at import time
    VeoClient = None  # type: ignore[assignment,misc]


def generate_video(
    prompt: str,
    *,
    duration: float = 5.0,
    aspect_ratio: str = "16:9",
) -> str:
    """Generate a video from a text description using Veo 3.1.

    Creates a brand-new video from scratch — no input file is needed.  The
    engine routes this function specially: it is called with only the ``args``
    dict from the plan step (no ``input_path``).

    Args:
        prompt: Text description of the video to generate.
        duration: Duration in seconds (1–60). Default 5.
        aspect_ratio: ``"16:9"``, ``"9:16"``, or ``"1:1"``. Default ``"16:9"``.

    Returns:
        Absolute path to the generated MP4 video file stored in
        ``<repo>/temp/veo/``.

    Raises:
        RuntimeError: If ``LAOZHANG_API_KEY`` is not set or generation fails.
    """
    return VeoClient().generate(prompt, duration=duration, aspect_ratio=aspect_ratio)


def generate_video_from_image(
    image_path: str,
    *,
    prompt: str,
    duration: float = 5.0,
) -> str:
    """Animate a still image into a video using Veo 3.1.

    Submits the image together with a motion prompt to Veo to produce an
    animated video.  When used in a plan, the engine passes the current
    pipeline file path as ``image_path``.

    Args:
        image_path: Path to input image (JPEG or PNG).
        prompt: Motion description, e.g. ``"camera slowly zooms out"``.
        duration: Duration in seconds (1–60). Default 5.

    Returns:
        Absolute path to the generated MP4 video file stored in
        ``<repo>/temp/veo/``.

    Raises:
        FileNotFoundError: If ``image_path`` does not exist.
        RuntimeError: If ``LAOZHANG_API_KEY`` is not set or generation fails.
    """
    return VeoClient().generate_from_image(image_path, prompt, duration=duration)


def extend_video(
    video_path: str,
    *,
    prompt: str,
    duration: float = 3.0,
) -> str:
    """Extend a video with an AI-generated continuation using Veo 3.1.

    Appends a new AI-generated segment to the end of the provided video,
    guided by ``prompt``.  When used in a plan, the engine passes the current
    pipeline file as ``video_path``.

    Args:
        video_path: Path to the input video to extend.
        prompt: Description of the continuation, e.g. ``"fade to black slowly"``.
        duration: Duration of the extension in seconds. Default 3.

    Returns:
        Absolute path to the extended MP4 video file stored in
        ``<repo>/temp/veo/``.

    Raises:
        FileNotFoundError: If ``video_path`` does not exist.
        RuntimeError: If ``LAOZHANG_API_KEY`` is not set or generation fails.
    """
    return VeoClient().extend(video_path, prompt, duration=duration)
