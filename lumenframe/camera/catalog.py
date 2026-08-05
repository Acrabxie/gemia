"""Agent-facing catalog — the whole camera vocabulary in one call.

Mirrors ``vector.catalog``: a machine-readable dict for the tool surface and a
compact prose block for a prompt. A test pins these to the *real* registrations
(the move registry, the style book, the feedback vocab) so the docs can never
silently drift from what the library actually does.
"""
from __future__ import annotations

from typing import Any

from lumenframe.camera import camera as cam
from lumenframe.camera.params import CAMERA_AXES, feedback_vocab
from lumenframe.camera.styles import camera_styles


def camera_catalog() -> dict[str, Any]:
    """Everything an agent needs to write a camera brief or a feedback phrase."""
    book = camera_styles()
    return {
        "axes": list(CAMERA_AXES),
        "axis_notes": {
            "energy": "speed + magnitude of the move",
            "smoothness": "gentleness of the acceleration (soft vs punchy)",
            "drama": "how motivated / large the push toward the subject",
            "drift": "organic handheld amount (0 locked → 1 loose operator)",
        },
        "moves": cam.MOVES.catalog(),
        "styles": book.catalog()["styles"],
        "style_aliases": book.catalog()["aliases"],
        "default_style": book.default,
        "feedback_vocabulary": feedback_vocab().vocabulary(),
        "brief_shape": {
            "move": "one of moves",
            "subject": {"x": "0..1", "y": "0..1", "scale": "optional"},
            "style": "one of styles or an alias",
            "feeling": ["adjective", "…"],
            "energy": "0..1 shorthand override",
            "duration": "seconds (≤60)",
            "canvas": {"width": "px", "height": "px"},
            "params": {"axis": "0..1 override"},
            "seed": "int",
        },
    }


def describe_camera() -> str:
    """Compact prompt block: moves + styles + brief shape."""
    book = camera_styles()
    lines = [
        "camera_move briefs: {move, subject:{x,y in 0..1, scale?}, style, feeling:[…],",
        " energy?, duration, canvas:{width,height}, params:{energy|smoothness|drama|drift:0..1}, seed}",
        cam.MOVES.describe("Moves:"),
        book.describe("Camera"),
        "Feedback: more/less + " + ", ".join(feedback_vocab().vocabulary()[:14]) + ", …",
    ]
    return "\n".join(lines)
