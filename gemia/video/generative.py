"""AI video generation primitives powered by Veo 3.1 via OpenRouter.

Functions
---------
generate_video             : text → video file path
generate_video_from_image  : image file + text → video file path
extend_video               : video file + text → extended video file path

All three functions try ``VeoClient`` first. If the external provider is
unavailable, they render a local reviewable fallback MP4 and write a
``.veo-fallback.json`` sidecar so Lumeri can keep the creative flow moving.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from gemia.model_strength import strongest_media_model

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
    fallback_on_error: bool = True,
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
        Absolute path to the generated or locally fallback-rendered MP4.
    """
    try:
        return VeoClient().generate(prompt, duration=duration, aspect_ratio=aspect_ratio)
    except Exception as exc:
        if not fallback_on_error:
            raise
        return _render_text_fallback(
            prompt=prompt,
            duration=duration,
            aspect_ratio=aspect_ratio,
            original_error=exc,
        )


def generate_video_from_image(
    image_path: str,
    *,
    prompt: str,
    duration: float = 5.0,
    fallback_on_error: bool = True,
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
    """
    try:
        return VeoClient().generate_from_image(image_path, prompt, duration=duration)
    except FileNotFoundError:
        raise
    except Exception as exc:
        if not fallback_on_error:
            raise
        if not Path(image_path).expanduser().exists():
            raise FileNotFoundError(f"Image not found: {image_path}") from exc
        return _render_image_fallback(
            image_path=image_path,
            prompt=prompt,
            duration=duration,
            original_error=exc,
        )


def extend_video(
    video_path: str,
    *,
    prompt: str,
    duration: float = 3.0,
    fallback_on_error: bool = True,
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
    """
    try:
        return VeoClient().extend(video_path, prompt, duration=duration)
    except FileNotFoundError:
        raise
    except Exception as exc:
        if not fallback_on_error:
            raise
        if not Path(video_path).expanduser().exists():
            raise FileNotFoundError(f"Video not found: {video_path}") from exc
        return _render_extend_fallback(
            video_path=video_path,
            prompt=prompt,
            duration=duration,
            original_error=exc,
        )


def _render_text_fallback(
    *,
    prompt: str,
    duration: float,
    aspect_ratio: str,
    original_error: BaseException,
) -> str:
    output = _fallback_output_path("text")
    width, height = _size_for_aspect_ratio(aspect_ratio)
    try:
        from gemia.video.motion_graphics import render_mg_title_card

        render_mg_title_card(
            "",
            str(output),
            title="Lumeri local preview",
            subtitle=_compact_prompt(prompt),
            duration=_safe_duration(duration),
            style="ice",
            width=width,
            height=height,
            fps=18,
            prefer_manim=False,
        )
    except Exception as fallback_exc:
        raise RuntimeError(_fallback_failed_message(original_error, fallback_exc)) from fallback_exc
    _write_veo_fallback_sidecar(
        output,
        prompt=prompt,
        duration=duration,
        aspect_ratio=aspect_ratio,
        original_error=original_error,
        fallback_mode="prompt_title_card",
    )
    return str(output.resolve())


def _render_image_fallback(
    *,
    image_path: str,
    prompt: str,
    duration: float,
    original_error: BaseException,
) -> str:
    output = _fallback_output_path("image")
    try:
        from gemia.video.effects import image_to_video

        image_to_video(
            str(Path(image_path).expanduser()),
            str(output),
            duration_sec=_safe_duration(duration),
            fps=18,
            width=960,
            height=540,
        )
    except Exception as fallback_exc:
        raise RuntimeError(_fallback_failed_message(original_error, fallback_exc)) from fallback_exc
    _write_veo_fallback_sidecar(
        output,
        prompt=prompt,
        duration=duration,
        aspect_ratio="source_image",
        original_error=original_error,
        fallback_mode="image_to_video",
        input_path=image_path,
    )
    return str(output.resolve())


def _render_extend_fallback(
    *,
    video_path: str,
    prompt: str,
    duration: float,
    original_error: BaseException,
) -> str:
    output = _fallback_output_path("extend")
    try:
        generative_extend(video_path, str(output), duration=_safe_duration(duration))
    except Exception:
        try:
            shutil.copyfile(Path(video_path).expanduser(), output)
        except Exception as fallback_exc:
            raise RuntimeError(_fallback_failed_message(original_error, fallback_exc)) from fallback_exc
    _write_veo_fallback_sidecar(
        output,
        prompt=prompt,
        duration=duration,
        aspect_ratio="source_video",
        original_error=original_error,
        fallback_mode="local_extend_or_passthrough",
        input_path=video_path,
    )
    return str(output.resolve())


def _fallback_output_path(kind: str) -> Path:
    root = os.environ.get("GEMIA_VEO_FALLBACK_DIR")
    if root:
        temp_dir = Path(root).expanduser()
    else:
        temp_dir = Path(__file__).resolve().parent.parent.parent / "temp" / "veo"
    temp_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    return temp_dir / f"veo_fallback_{kind}_{stamp}_{uuid.uuid4().hex[:8]}.mp4"


def _safe_duration(value: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = 5.0
    return min(max(parsed, 0.4), 12.0)


def _size_for_aspect_ratio(aspect_ratio: str) -> tuple[int, int]:
    ratio = str(aspect_ratio or "16:9").strip()
    if ratio == "9:16":
        return 540, 960
    if ratio == "1:1":
        return 720, 720
    return 960, 540


def _compact_prompt(prompt: str, *, max_chars: int = 120) -> str:
    text = " ".join(str(prompt or "").split())
    if len(text) <= max_chars:
        return text or "Veo request"
    return text[: max_chars - 1].rstrip() + "..."


def _sanitize_error(exc: BaseException, *, max_chars: int = 420) -> str:
    text = " ".join(str(exc).split())
    return text[:max_chars]


def _fallback_failed_message(original_error: BaseException, fallback_error: BaseException) -> str:
    return (
        "Veo 外部视频生成接口暂时断开了；Lumeri 尝试生成本地低清预览也失败。"
        f"外部错误：{_sanitize_error(original_error)}；本地预览错误：{_sanitize_error(fallback_error)}"
    )


def _write_veo_fallback_sidecar(
    output: Path,
    *,
    prompt: str,
    duration: float,
    aspect_ratio: str,
    original_error: BaseException,
    fallback_mode: str,
    input_path: str = "",
) -> None:
    sidecar = output.with_suffix(".veo-fallback.json")
    message = (
        "Veo 外部视频生成接口暂时断开了。我先用 Lumeri 本地渲染做一个可看的低清预览，"
        "保留原 prompt 和失败原因；等外部接口恢复后，可以只替换这一段生成结果。"
    )
    payload: dict[str, Any] = {
        "schema_version": 1,
        "effect": "veo_local_fallback_preview",
        "kind": "veo_fallback_preview",
        "status": "fallback",
        "provider": "openrouter/veo",
        "model": strongest_media_model("video", "openrouter", (os.environ.get("VEO_MODEL"),)),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "input_path": str(Path(input_path).expanduser()) if input_path else "",
        "output_path": str(output.resolve()),
        "prompt": str(prompt or ""),
        "duration": _safe_duration(duration),
        "aspect_ratio": str(aspect_ratio or ""),
        "fallback_mode": fallback_mode,
        "original_error": _sanitize_error(original_error),
        "user_message": message,
        "next_action": "先审本地小样；Veo 恢复后只重跑这一段 render pass。",
        "composition": {
            "duration": _safe_duration(duration),
            "blocks": [
                {
                    "id": "veo_fallback_notice",
                    "kind": "text",
                    "role": "fallback_notice",
                    "text": "Veo 暂时不可用，本地小样先行",
                    "data-start": 0,
                    "data-duration": _safe_duration(duration),
                },
                {
                    "id": "veo_prompt",
                    "kind": "text",
                    "role": "prompt",
                    "text": _compact_prompt(prompt),
                    "data-start": 0,
                    "data-duration": _safe_duration(duration),
                },
            ],
        },
        "provenance": {
            "generated_by": "lumeri_local_veo_fallback",
            "external_provider_failed": True,
        },
    }
    sidecar.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# generative_extend  (#34)
# ---------------------------------------------------------------------------
def generative_extend(
    input_path: str,
    output_path: str,
    *,
    duration: float = 2.0,
) -> str:
    """Extend a video by generating new frames at the end.

    Tries optical-flow extrapolation (OpenCV), then falls back to freezing
    the last frame for ``duration`` seconds.

    Args:
        input_path: Source video.
        output_path: Destination video.
        duration: Extension duration in seconds.

    Returns:
        output_path
    """
    import subprocess, tempfile
    from pathlib import Path as _Path
    from gemia.video.timeline import freeze_frame, _probe_duration

    src_dur = _probe_duration(input_path)

    try:
        import cv2, numpy as np
        # Extract last 10 frames
        cap = cv2.VideoCapture(input_path)
        fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

        start_frame = max(0, total - 10)
        cap.set(cv2.CAP_PROP_POS_FRAMES, start_frame)
        frames = []
        while True:
            ret, f = cap.read()
            if not ret:
                break
            frames.append(f)
        cap.release()

        if len(frames) < 2:
            raise ValueError("not enough frames")

        flow = cv2.calcOpticalFlowFarneback(
            cv2.cvtColor(frames[-2], cv2.COLOR_BGR2GRAY),
            cv2.cvtColor(frames[-1], cv2.COLOR_BGR2GRAY),
            None, 0.5, 3, 15, 3, 5, 1.2, 0,
        )

        n_new = int(duration * fps)
        new_frames = []
        last = frames[-1].astype(np.float32)
        for i in range(1, n_new + 1):
            ys, xs = np.mgrid[0:h, 0:w].astype(np.float32)
            map_x = (xs + flow[:, :, 0] * i).clip(0, w - 1)
            map_y = (ys + flow[:, :, 1] * i).clip(0, h - 1)
            warped = cv2.remap(last, map_x, map_y, cv2.INTER_LINEAR)
            new_frames.append(warped.clip(0, 255).astype(np.uint8))

        with tempfile.TemporaryDirectory() as td:
            ext_video = str(_Path(td) / "ext.mp4")
            out_ext = cv2.VideoWriter(ext_video, cv2.VideoWriter_fourcc(*"mp4v"), fps, (w, h))
            for f in new_frames:
                out_ext.write(f)
            out_ext.release()

            # concat list
            list_file = str(_Path(td) / "list.txt")
            with open(list_file, "w") as lf:
                lf.write(f"file '{_Path(input_path).resolve()}'\n")
                lf.write(f"file '{_Path(ext_video).resolve()}'\n")

            subprocess.run([
                "ffmpeg", "-y", "-f", "concat", "-safe", "0",
                "-i", list_file,
                "-c:v", "libx264",
                "-c:a", "aac", "-ac", "2",
                output_path,
            ], check=True, capture_output=True)
        return output_path

    except Exception:
        # Fallback: freeze last frame
        freeze_frame(input_path, output_path,
                     timestamp_sec=max(0.0, src_dur - 0.05),
                     freeze_duration_sec=duration)
        return output_path


# ---------------------------------------------------------------------------
# ai_color_grade  (#35)
# ---------------------------------------------------------------------------
_MOOD_FILTERS = {
    "cinematic": (
        "colorchannelmixer=.9:0:.1:0:.1:.8:.1:0:.1:.1:.9:0,"
        "curves=r='0/0 0.5/0.45 1/1':g='0/0 0.5/0.5 1/1':b='0/0 0.5/0.58 1/1'"
    ),
    "warm": "colorchannelmixer=1.1:0:0:0:0:1:0:0:0:0:0.85:0",
    "cool": "colorchannelmixer=0.85:0:0:0:0:1:0:0:0:0:1.15:0",
    "vintage": "curves=preset=vintage,hue=s=0.7,vignette=PI/4",
    "moody": "eq=contrast=1.2:brightness=-0.05:saturation=0.8,vignette=PI/3",
    "vibrant": "eq=saturation=1.4:contrast=1.05,curves=r='0/0 0.5/0.52 1/1'",
    "bw": "format=gray,format=yuv420p",
}


def ai_color_grade(
    input_path: str,
    output_path: str,
    *,
    mood: str = "cinematic",
) -> str:
    """Apply a colour-grading preset inspired by common AI/cinematic looks.

    Args:
        input_path: Source video or image.
        output_path: Destination.
        mood: One of ``"cinematic"``, ``"warm"``, ``"cool"``, ``"vintage"``,
              ``"moody"``, ``"vibrant"``, ``"bw"``.

    Returns:
        output_path
    """
    import subprocess
    vf = _MOOD_FILTERS.get(mood)
    if vf is None:
        raise ValueError(f"Unknown mood '{mood}'. Choose from: {list(_MOOD_FILTERS)}")

    from pathlib import Path as _Path
    _IMG_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".webp"}
    is_image = _Path(input_path).suffix.lower() in _IMG_EXTS
    audio_args = [] if is_image else ["-c:a", "copy"]

    subprocess.run([
        "ffmpeg", "-y", "-i", input_path,
        "-vf", vf,
        *audio_args,
        output_path,
    ], check=True, capture_output=True)
    return output_path


# ---------------------------------------------------------------------------
# generate_broll  (#33)
# ---------------------------------------------------------------------------
def generate_broll(
    script_text: str,
    output_dir: str,
    *,
    style: str = "cinematic",
) -> list[str]:
    """Download B-roll clips from Pexels matching keywords in script_text.

    Args:
        script_text: Script or description to extract keywords from.
        output_dir: Directory to save downloaded clips.
        style: Colour grade preset to apply (``"cinematic"``, ``"documentary"``,
               ``"vintage"``).

    Returns:
        List of paths to downloaded and styled clips.

    Raises:
        EnvironmentError: If ``PEXELS_API_KEY`` env var is not set.
    """
    import json, re, ssl, subprocess, urllib.parse, urllib.request
    from pathlib import Path as _Path

    import certifi

    from gemia.video.stock_media import _api_key

    pexels_key = _api_key("pexels")
    ssl_context = ssl.create_default_context(cafile=certifi.where())

    _Path(output_dir).mkdir(parents=True, exist_ok=True)

    # Extract keywords: take top 5 non-stopword words by frequency
    stopwords = {"the", "a", "an", "is", "are", "was", "were", "in", "on",
                 "at", "to", "of", "and", "or", "but", "with", "for", "it"}
    words = re.findall(r"\b[a-z]{4,}\b", script_text.lower())
    freq: dict[str, int] = {}
    for w in words:
        if w not in stopwords:
            freq[w] = freq.get(w, 0) + 1
    keywords = sorted(freq, key=lambda k: -freq[k])[:5] or ["nature"]

    _STYLE_FILTERS = {
        "cinematic": "colorchannelmixer=.9:0:.1:0:.1:.8:.1:0:.1:.1:.9:0",
        "documentary": "hue=s=0.7,eq=contrast=1.1:brightness=-0.05",
        "vintage": "hue=s=0.5,curves=preset=vintage",
    }
    vf = _STYLE_FILTERS.get(style, _STYLE_FILTERS["cinematic"])

    output_paths: list[str] = []
    for kw in keywords:
        url = (f"https://api.pexels.com/videos/search"
               f"?query={urllib.parse.quote(kw)}&per_page=1&size=medium")
        req = urllib.request.Request(url, headers={"Authorization": pexels_key})
        try:
            with urllib.request.urlopen(req, timeout=15, context=ssl_context) as resp:
                data = json.loads(resp.read())
        except Exception:
            continue

        videos = data.get("videos", [])
        if not videos:
            continue
        video_files = videos[0].get("video_files", [])
        if not video_files:
            continue
        # Pick SD quality
        file_url = sorted(video_files, key=lambda f: f.get("width", 0))[len(video_files) // 2]["link"]

        raw_path = str(_Path(output_dir) / f"broll_{kw}_raw.mp4")
        styled_path = str(_Path(output_dir) / f"broll_{kw}.mp4")

        req2 = urllib.request.Request(file_url)
        try:
            with urllib.request.urlopen(req2, timeout=30, context=ssl_context) as resp2:
                with open(raw_path, "wb") as fout:
                    fout.write(resp2.read())
        except Exception:
            continue

        subprocess.run([
            "ffmpeg", "-y", "-i", raw_path,
            "-vf", vf,
            "-c:a", "aac", "-ac", "2",
            styled_path,
        ], capture_output=True)
        output_paths.append(styled_path)

    return output_paths


# ---------------------------------------------------------------------------
# hdr_tone_map  (#44)
# ---------------------------------------------------------------------------
def hdr_tone_map(
    input_path: str,
    output_path: str,
    *,
    target_format: str = "hdr10",
) -> str:
    """Tone-map video to HDR format.

    Args:
        input_path: Source video path.
        output_path: Destination video path.
        target_format: ``"hdr10"`` or ``"hlg"``.

    Returns:
        output_path
    """
    import subprocess
    from pathlib import Path as _Path

    trc_map = {
        "hdr10": "smpte2084",
        "hlg": "arib-std-b67",
    }
    trc = trc_map.get(target_format)
    if trc is None:
        raise ValueError(f"Unknown target_format '{target_format}'. Choose from: {list(trc_map)}")

    vf = (
        f"zscale=t=linear:npl=100,format=gbrpf32le,"
        f"zscale=p=bt2020:t={trc}:m=bt2020nc:r=tv,"
        f"tonemap=hable,"
        f"zscale=p=bt2020:t={trc}:m=bt2020nc:r=tv:npl=1000,"
        f"format=yuv420p10le"
    )

    _IMG_EXTS2 = {".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".webp"}
    _is_img = _Path(input_path).suffix.lower() in _IMG_EXTS2
    _audio_args = [] if _is_img else ["-c:a", "copy"]

    _Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg", "-y", "-i", input_path,
            "-vf", vf,
            *_audio_args,
            output_path,
        ],
        check=True, capture_output=True,
    )
    return output_path


# ---------------------------------------------------------------------------
# film_look_creator  (#48) — DaVinci Resolve 19 Film Look Creator FX
# ---------------------------------------------------------------------------
def film_look_creator(
    input_path: str,
    output_path: str,
    *,
    halation: float = 0.3,
    gate_weave: float = 0.5,
    silver_halide: float = 0.4,
    film_stock: str = "kodak",
) -> str:
    """Apply a film-look effect inspired by DaVinci Resolve 19's Film Look Creator.

    Simulates analogue film characteristics: halation (red glow around
    highlights), gate weave (slight random positional jitter), silver-halide
    grain texture, and stock-specific colour response.

    Args:
        input_path: Source video.
        output_path: Destination video.
        halation: Intensity of red halation bloom [0, 1].  Default 0.3.
        gate_weave: Magnitude of gate-weave positional jitter in pixels [0, 3].
            Default 0.5.
        silver_halide: Grain strength [0, 1].  Default 0.4.
        film_stock: Colour response preset — ``"kodak"`` (warm), ``"fuji"``
            (cool/green), or ``"ilford"`` (neutral B&W lift).

    Returns:
        output_path
    """
    _STOCK_EQ = {
        "kodak": "colorchannelmixer=1.05:0:0:0:0:1:0:0:0:0:0.92:0,eq=contrast=1.05:brightness=0.01",
        "fuji":  "colorchannelmixer=0.92:0:0:0:0:1.04:0:0:0:0:1.08:0,eq=contrast=1.02",
        "ilford": "format=gray,format=yuv420p,eq=contrast=1.1:brightness=0.02",
    }
    stock_filter = _STOCK_EQ.get(film_stock, _STOCK_EQ["kodak"])

    grain_strength = int(silver_halide * 20)
    wp = max(1, int(gate_weave))  # gate weave in pixels

    # Gate weave: crop slightly oversized, then shift with geq (compatible approach)
    # Use pad+crop to add margin then crop with animated offset
    vf_parts = [
        # 1. Film stock colour response
        stock_filter,
        # 2. Silver-halide grain
        f"noise=alls={grain_strength}:allf=t+u",
        # 3. Gate weave via slight overscan + animated crop
        f"pad=iw+{wp*2}:ih+{wp*2}:{wp}:{wp}",
        f"crop=iw-{wp*2}:ih-{wp*2}:mod(n\\,{wp}):mod(n\\,{wp})",
        # 4. Vignette for lens falloff
        "vignette=PI/5",
    ]
    vf = ",".join(vf_parts)

    subprocess.run([
        "ffmpeg", "-y", "-i", input_path,
        "-vf", vf,
        "-c:a", "copy",
        output_path,
    ], check=True, capture_output=True)
    return output_path


# ---------------------------------------------------------------------------
# intellitrack_zone  (#49) — DaVinci Resolve 19 IntelliTrack AI zone grading
# ---------------------------------------------------------------------------
def intellitrack_zone(
    input_path: str,
    output_path: str,
    *,
    zone_x: float = 0.5,
    zone_y: float = 0.3,
    zone_radius: float = 0.15,
    grade: str = "warm",
    feather: float = 0.08,
) -> str:
    """Apply selective colour grading inside a tracked circular zone.

    Simulates DaVinci Resolve 19's *IntelliTrack AI* point-tracker used in
    the Color page to selectively grade a region that follows a subject.
    This implementation uses a static circular mask with soft feathering via
    ffmpeg's ``geq`` filter.  For the tracked version, combine with
    ``gemia.video.frames.ai_stabilize`` to pre-stabilise the region first.

    Args:
        input_path: Source video.
        output_path: Destination video.
        zone_x: Horizontal centre of the zone as fraction of frame width [0, 1].
            Default 0.5 (centre).
        zone_y: Vertical centre of the zone as fraction of frame height [0, 1].
            Default 0.3 (upper-centre, typical for face tracking).
        zone_radius: Radius as fraction of frame width [0.05, 0.5].  Default 0.15.
        grade: Grade preset to apply inside zone: ``"warm"``, ``"cool"``,
            ``"boost"``, ``"desaturate"``.  Default ``"warm"``.
        feather: Soft edge width as fraction of frame width.  Default 0.08.

    Returns:
        output_path
    """
    import tempfile as _tmp
    from pathlib import Path as _Path

    _GRADE_PARAMS = {
        "warm":       ("colorchannelmixer=1.08:0:0:0:0:1:0:0:0:0:0.88:0", "eq=saturation=1.1"),
        "cool":       ("colorchannelmixer=0.88:0:0:0:0:1:0:0:0:0:1.12:0", ""),
        "boost":      ("eq=contrast=1.15:brightness=0.03:saturation=1.25", ""),
        "desaturate": ("hue=s=0.2", ""),
    }
    grade_filter, extra = _GRADE_PARAMS.get(grade, _GRADE_PARAMS["warm"])
    inner_filter = f"{grade_filter},{extra}".rstrip(",")

    # Build two streams: graded and original; blend with radial mask via geq
    # geq mask: 1 inside zone (with feather), 0 outside
    cx = zone_x
    cy = zone_y
    r = zone_radius
    f = max(feather, 0.001)
    # mask expression: smooth step from 1 at (r-feather) to 0 at (r+feather)
    mask_expr = (
        f"clip((({r}+{f})*W - sqrt((X-{cx}*W)^2+(Y-{cy}*H)^2))/({f}*W), 0, 1)"
    )

    filter_complex = (
        f"[0:v]split=2[base][gradedin];"
        f"[gradedin]{inner_filter}[graded];"
        f"[base][graded]blend=all_expr='A*(1-{mask_expr})+B*{mask_expr}'[out]"
    )

    subprocess.run([
        "ffmpeg", "-y", "-i", input_path,
        "-filter_complex", filter_complex,
        "-map", "[out]", "-map", "0:a?",
        "-c:a", "copy",
        output_path,
    ], check=True, capture_output=True)
    return output_path
