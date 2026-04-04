"""gemia.video — Video primitive operations.

Uses FFmpeg for timeline operations and OpenCV for frame-level processing.
The bridge function ``apply_picture_op_to_video`` lets any gemia.picture
function be applied to every frame of a video.
"""
from gemia.video.frames import extract_frames, frames_to_video, apply_picture_op_to_video
from gemia.video.timeline import cut, concat, speed, reverse
from gemia.video.compositing import overlay, add_audio_track
from gemia.video.analysis import detect_scenes, get_metadata

__all__ = [
    "extract_frames", "frames_to_video", "apply_picture_op_to_video",
    "cut", "concat", "speed", "reverse",
    "overlay", "add_audio_track",
    "detect_scenes", "get_metadata",
]
