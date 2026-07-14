"""Video analysis: detect_scenes, get_metadata."""
from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess

import cv2
import numpy as np

from gemia.primitives_common import ensure_float32


def get_metadata(path: str) -> dict:
    """Get video metadata via ffprobe.

    Args:
        path: Video file path.

    Returns:
        Dict with keys: ``duration`` (float seconds), ``width``, ``height``,
        ``fps`` (float), ``codec``, ``audio_codec``, ``file_size_bytes``.
    """
    proc = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration,size:stream=width,height,r_frame_rate,codec_name,codec_type",
            "-of", "json",
            path,
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {proc.stderr}")
    data = json.loads(proc.stdout)

    fmt = data.get("format", {})
    streams = data.get("streams", [])
    video_stream = next((s for s in streams if s.get("codec_type") == "video"), {})
    audio_stream = next((s for s in streams if s.get("codec_type") == "audio"), {})

    fps_str = video_stream.get("r_frame_rate", "30/1")
    try:
        num, den = fps_str.split("/")
        fps = float(num) / float(den)
    except Exception:
        fps = 30.0

    return {
        "duration": float(fmt.get("duration", 0)),
        "width": int(video_stream.get("width", 0)),
        "height": int(video_stream.get("height", 0)),
        "fps": fps,
        "codec": video_stream.get("codec_name", ""),
        "audio_codec": audio_stream.get("codec_name", ""),
        "file_size_bytes": int(fmt.get("size", 0)),
    }


def detect_scenes(path: str, *, threshold: float = 30.0) -> list[float]:
    """Detect scene changes by frame difference.

    Args:
        path: Video file path.
        threshold: Mean absolute difference threshold (0-255 scale).
            Lower values = more sensitive.

    Returns:
        List of timestamps (seconds) where scene changes occur.
    """
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        raise FileNotFoundError(f"Cannot open video: {path}")

    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    prev_frame = None
    scenes: list[float] = []
    idx = 0

    while True:
        ret, frame = cap.read()
        if not ret:
            break
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).astype(np.float32)
        if prev_frame is not None:
            diff = np.abs(gray - prev_frame).mean()
            if diff > threshold:
                scenes.append(idx / fps)
        prev_frame = gray
        idx += 1

    cap.release()
    return scenes


def track_point(video_path: str, *, initial_point: tuple[float, float]) -> list[tuple[float, float]]:
    """Track a single point across all frames using optical flow.

    Args:
        video_path: Input video path.
        initial_point: (x, y) pixel coordinates in the first frame.

    Returns:
        List of (x, y) coordinates, one per frame.
    """
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise FileNotFoundError(f"Cannot open video: {video_path}")

    ret, frame = cap.read()
    if not ret:
        cap.release()
        return []

    prev_gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    pt = np.array([[initial_point]], dtype=np.float32)
    track: list[tuple[float, float]] = [(float(pt[0, 0, 0]), float(pt[0, 0, 1]))]

    lk_params = dict(winSize=(15, 15), maxLevel=2,
                     criteria=(cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 10, 0.03))

    while True:
        ret, frame = cap.read()
        if not ret:
            break
        curr_gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        new_pt, status, _ = cv2.calcOpticalFlowPyrLK(prev_gray, curr_gray, pt, None, **lk_params)
        if status is not None and status[0, 0] == 1:
            pt = new_pt
        track.append((float(pt[0, 0, 0]), float(pt[0, 0, 1])))
        prev_gray = curr_gray

    cap.release()
    return track


def track_plane(video_path: str, *, initial_quad: list[tuple[float, float]]) -> list[list[tuple[float, float]]]:
    """Track a planar quad region across all frames using homography.

    Args:
        video_path: Input video path.
        initial_quad: List of 4 (x, y) corner points in the first frame.

    Returns:
        List of quads (each a list of 4 (x,y) points), one per frame.
    """
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise FileNotFoundError(f"Cannot open video: {video_path}")

    ret, frame = cap.read()
    if not ret:
        cap.release()
        return []

    prev_gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    quad = np.array(initial_quad, dtype=np.float32)
    quads: list[list[tuple[float, float]]] = [[(float(p[0]), float(p[1])) for p in quad]]

    lk_params = dict(winSize=(15, 15), maxLevel=2,
                     criteria=(cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 10, 0.03))

    while True:
        ret, frame = cap.read()
        if not ret:
            break
        curr_gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        pts = quad.reshape(-1, 1, 2)
        new_pts, status, _ = cv2.calcOpticalFlowPyrLK(prev_gray, curr_gray, pts, None, **lk_params)
        good_old = pts[status.ravel() == 1]
        good_new = new_pts[status.ravel() == 1]

        if len(good_old) >= 4:
            H, mask = cv2.findHomography(good_old, good_new, cv2.RANSAC)
            if H is not None:
                new_quad = cv2.perspectiveTransform(quad.reshape(-1, 1, 2), H)
                quad = new_quad.reshape(-1, 2)
            else:
                valid = status.ravel() == 1
                quad[valid] = new_pts[valid].reshape(-1, 2)
        else:
            valid = status.ravel() == 1
            if valid.any():
                quad[valid] = new_pts[valid].reshape(-1, 2)

        quads.append([(float(p[0]), float(p[1])) for p in quad])
        prev_gray = curr_gray

    cap.release()
    return quads


def _first_sound_time(path: str, *, method: str = "audio") -> float:
    """Estimate the first non-silent timestamp in a clip."""
    if method != "audio":
        raise ValueError(f"Unsupported multicam sync method: {method}")

    meta = get_metadata(path)
    if not meta.get("audio_codec"):
        return 0.0

    proc = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-i", path,
            "-af", "silencedetect=noise=-30dB:d=0.05",
            "-f", "null", "-",
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg silencedetect failed: {proc.stderr}")

    match = re.search(r"silence_end:\s*([0-9.]+)", proc.stderr)
    if match:
        return float(match.group(1))
    return 0.0


def scene_detect(video_path: str, output_path: str | None = None, *, threshold: float = 0.3) -> list[float]:
    """Detect scene change timestamps using ffmpeg select filter.

    Args:
        video_path: Input video file path.
        output_path: Optional path to write a JSON file with timestamps.
        threshold: Scene change sensitivity (0-1). Lower = more sensitive.

    Returns:
        List of timestamps (seconds) where scene changes occur.
    """
    cmd = [
        "ffmpeg", "-i", video_path,
        "-vf", f"select='gt(scene,{threshold})',showinfo",
        "-f", "null", "-",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    combined = proc.stdout + proc.stderr

    timestamps: list[float] = []
    for line in combined.splitlines():
        m = re.search(r"pts_time:([0-9.]+)", line)
        if m:
            timestamps.append(float(m.group(1)))

    if output_path is not None:
        Path(output_path).write_text(json.dumps(timestamps))

    return timestamps


def auto_highlight(video_path: str, output_path: str, *, duration: float = 60.0) -> str:
    """Create a highlight reel by selecting the most visually dynamic segments.

    Args:
        video_path: Input video file path.
        output_path: Output highlight video path.
        duration: Target duration of the highlight reel in seconds.

    Returns:
        output_path
    """
    import tempfile
    from gemia.video.timeline import concat

    scenes = scene_detect(video_path)

    # If too few scenes, supplement with evenly-spaced samples
    min_scenes = int(duration / 5)
    if len(scenes) < min_scenes:
        meta = get_metadata(video_path)
        total = meta["duration"]
        extra_count = min_scenes - len(scenes)
        step = total / (extra_count + 1)
        extra = [step * (i + 1) for i in range(extra_count)]
        scenes = sorted(set(scenes) | set(extra))

    if not scenes:
        scenes = [0.0]

    # Select timestamps distributed evenly across video
    clip_dur = min(5.0, duration / max(len(scenes), 1))
    # Limit total clips so sum ~= duration
    max_clips = max(1, int(duration / clip_dur))
    # Distribute selections evenly across scenes list
    step = max(1, len(scenes) // max_clips)
    selected = scenes[::step][:max_clips]

    tmp_dir = tempfile.mkdtemp()
    clips: list[str] = []
    for i, ts in enumerate(selected):
        clip_path = str(Path(tmp_dir) / f"clip_{i:04d}.mp4")
        cmd = [
            "ffmpeg", "-y",
            "-ss", f"{ts:.6f}",
            "-i", video_path,
            "-t", f"{clip_dur:.6f}",
            "-map", "0:v:0?",
            "-map", "0:a?",
            "-c:v", "libx264",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            clip_path,
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode == 0:
            clips.append(clip_path)

    if not clips:
        raise RuntimeError("auto_highlight: no clips could be extracted")

    concat(clips, output_path)
    return output_path


def multicam_sync(input_paths: list[str], output_dir: str, *, method: str = "audio") -> list[str]:
    """Synchronize simultaneously recorded camera clips by trimming leading offsets."""
    if not input_paths:
        raise ValueError("input_paths must not be empty")

    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    offsets = [_first_sound_time(path, method=method) for path in input_paths]
    sync_point = max(offsets)

    outputs: list[str] = []
    for idx, input_path in enumerate(input_paths):
        output_path = out_dir / f"cam_{idx}.mp4"
        cmd = [
            "ffmpeg", "-y", "-i", input_path,
            "-ss", f"{sync_point:.6f}",
            "-map", "0:v:0?",
            "-map", "0:a?",
            "-c:v", "libx264",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            "-movflags", "+faststart",
            str(output_path),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(f"ffmpeg failed: {' '.join(cmd)}\nSTDERR:\n{proc.stderr}")
        outputs.append(str(output_path))

    return outputs


# ---------------------------------------------------------------------------
# #60  smart_multicam
# ---------------------------------------------------------------------------
def smart_multicam(
    camera_paths: list[str],
    output_path: str,
    *,
    clip_duration: float = 5.0,
    strategy: str = "round_robin",
) -> str:
    """Assemble multi-camera footage by auto-selecting angles.

    Inspired by DaVinci Resolve 19 *AI Multicam* feature. Synchronises clips
    via audio (using :func:`multicam_sync`), then cuts between cameras using
    the chosen strategy.

    Args:
        camera_paths: List of video paths (one per camera).
        output_path: Destination assembled video path.
        clip_duration: Duration (seconds) of each camera cut.  Default 5.
        strategy: ``"round_robin"`` cycles cameras evenly; ``"motion"``
            picks the camera with the most motion per segment.

    Returns:
        output_path
    """
    import tempfile
    from gemia.video.timeline import cut, concat as _concat

    if not camera_paths:
        raise ValueError("camera_paths must not be empty")
    if len(camera_paths) == 1:
        raise ValueError("smart_multicam requires at least 2 cameras")

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    tmp_dir = Path(tempfile.mkdtemp())

    # Sync cameras
    sync_dir = str(tmp_dir / "synced")
    synced = multicam_sync(camera_paths, sync_dir)

    # Determine total usable duration
    metas = [get_metadata(p) for p in synced]
    total_dur = min(m["duration"] for m in metas)
    n_cams = len(synced)

    segments: list[str] = []
    t = 0.0
    cam_idx = 0

    while t + clip_duration <= total_dur:
        if strategy == "motion":
            # Pick camera with highest inter-frame motion in this window
            best_cam = 0
            best_score = -1.0
            for ci, cam_path in enumerate(synced):
                cap = cv2.VideoCapture(cam_path)
                cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
                prev = None
                score = 0.0
                for _ in range(int(metas[ci]["fps"] * min(2.0, clip_duration))):
                    ret, frame = cap.read()
                    if not ret:
                        break
                    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).astype(np.float32)
                    if prev is not None:
                        score += float(np.abs(gray - prev).mean())
                    prev = gray
                cap.release()
                if score > best_score:
                    best_score = score
                    best_cam = ci
            cam_idx = best_cam
        else:
            # round-robin
            cam_idx = cam_idx % n_cams

        seg_path = str(tmp_dir / f"seg_{len(segments):04d}.mp4")
        cut(synced[cam_idx], seg_path, start_sec=t, end_sec=t + clip_duration)
        segments.append(seg_path)

        t += clip_duration
        if strategy == "round_robin":
            cam_idx += 1

    if not segments:
        raise RuntimeError("smart_multicam: no segments assembled")

    _concat(segments, output_path)
    return output_path
