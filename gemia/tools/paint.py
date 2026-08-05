"""paint_* tools: Gemini-first annotation and local mask edits.

The overlay tool writes a transparent PNG, registers it as an image asset, and
places it on the persistent timeline. The mask tool writes a new image/video
asset so the source asset remains untouched.
"""
from __future__ import annotations

import asyncio
import math
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont

from gemia.errors import RECOVERY_FIX_ARGS, RECOVERY_SWITCH_TOOL, ToolError
from gemia.tools._context import ToolContext
from gemia.tools._ffmpeg import cpu_video_encoder_args, get_video_encoder_args


_OVERLAY_SHAPES = {"stroke", "rect", "rectangle", "ellipse", "circle", "arrow", "highlight", "text"}
_MASK_SHAPES = {"rect", "rectangle", "ellipse", "circle", "polygon", "stroke"}
_MASK_EFFECTS = {"blur", "mosaic", "dim_outside", "highlight", "adjust"}
_HEX_CHARS = set("0123456789abcdefABCDEF")


async def dispatch_overlay(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    project = _project(ctx, "paint_overlay")
    state = project.load()
    width, height = _canvas_size(state)
    shape = _shape(args, allowed=_OVERLAY_SHAPES, default="stroke")
    color = _color(args.get("color"), default="#ff3030")
    opacity = _float(args.get("opacity", 1.0), "opacity", lo=0.0, hi=1.0)
    stroke_width = _float(args.get("width", 10.0), "width", lo=0.1, hi=512.0)
    feather = _float(args.get("feather", 0.0), "feather", lo=0.0, hi=256.0)
    start, duration = _time_range(args)

    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    _draw_overlay(image, shape, args, color=color, width_px=stroke_width, feather=feather)

    asset_id = ctx.registry.allocate_id("image")
    out_path = ctx.child_path(asset_id, ".png")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(out_path)
    ctx.registry.register_output(
        asset_id,
        kind="image",
        path=out_path,
        summary=f"paint overlay ({shape})",
    )

    clip_id = f"clip_{uuid.uuid4().hex[:8]}"
    track_id, needs_track = _pick_overlay_track(
        state,
        start=start,
        duration=duration,
        requested=str(args.get("track_id") or ""),
    )
    clip_name = f"paint: {shape}"
    clip = {
        "id": clip_id,
        "asset_id": asset_id,
        "track_id": track_id,
        "media_kind": "image",
        "name": clip_name,
        "start": round(start, 6),
        "duration": round(duration, 6),
        "source_in": 0.0,
        "source_out": round(duration, 6),
        "effects": {"x": 0.0, "y": 0.0, "scale": 1.0, "opacity": round(opacity, 6)},
        "summary": {"paint": {"kind": "overlay", "shape": shape, "color": color}},
        "provenance": {"verb": "paint_overlay", "session_id": ctx.session_id},
    }
    asset_payload = {
        "id": asset_id,
        "asset_id": asset_id,
        "name": out_path.name,
        "media_kind": "image",
        "source_path": str(out_path),
        "duration": duration,
        "metadata": {"paint": {"kind": "overlay", "shape": shape, "color": color}},
    }
    ops: list[dict[str, Any]] = []
    if needs_track:
        ops.append({"op": "add_track", "kind": "overlay", "track_id": track_id, "name": track_id})
    ops.append(
        {
            "op": "insert_clip",
            "data": {"asset": asset_payload, "clip": clip},
            "track_id": track_id,
            "at": {"time": round(start, 6)},
            "ripple": False,
            "provenance": {"verb": "paint_overlay", "session_id": ctx.session_id},
        }
    )
    result = project.apply_ops(ops, label="paint_overlay")
    return {
        "applied": True,
        "asset_id": asset_id,
        "clip_id": clip_id,
        "track_id": track_id,
        "start": start,
        "duration": duration,
        "seq": result.get("patch_seq_end"),
        "timeline": project.compact_text(),
        "note": "paint overlay inserted on an overlay track; call inspect_timeline to verify the composited pixels",
    }


async def dispatch_mask_effect(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    asset_id = str(args.get("asset_id") or "")
    if not asset_id:
        _bad("paint_mask_effect requires asset_id.")
    record = ctx.registry.get(asset_id)
    if record.kind not in {"image", "video"}:
        raise ToolError(
            f"paint_mask_effect works on image/video assets; {asset_id} is a {record.kind}.",
            code="E_UNSUPPORTED",
            recovery=RECOVERY_SWITCH_TOOL,
            hint="Use paint_overlay for timeline annotations, or pass an image/video asset.",
        )

    effect = _effect(args)
    mask_spec = args.get("mask") if isinstance(args.get("mask"), dict) else args
    params = args.get("params") if isinstance(args.get("params"), dict) else args
    out_id = ctx.registry.allocate_id(record.kind)
    out_path = ctx.child_path(out_id, ".mp4" if record.kind == "video" else _image_ext(record.path))
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if record.kind == "image":
        _process_image(record.path, out_path, mask_spec, effect, params)
        duration = None
    else:
        start, end = _video_time_window(args)
        duration = await asyncio.to_thread(
            _process_video,
            record.path,
            out_path,
            mask_spec,
            effect,
            params,
            start,
            end,
        )

    summary = f"paint mask {effect} on {asset_id}"
    ctx.registry.register_output(
        out_id,
        kind=record.kind,
        path=out_path,
        summary=summary,
        lineage=[asset_id],
    )
    return {
        "asset_id": out_id,
        "summary": summary,
        "metadata": {
            "kind": record.kind,
            "source_asset_id": asset_id,
            "effect": effect,
            "duration_sec": duration,
            "note": "source asset was not modified",
        },
    }


def _project(ctx: ToolContext, tool_name: str):
    if ctx.project is None:
        raise ToolError(
            f"{tool_name} needs a project-backed session.",
            code="E_UNSUPPORTED",
            recovery=RECOVERY_SWITCH_TOOL,
            hint="Use it inside a Lumeri timeline session, not a bare asset-only context.",
        )
    return ctx.project


def _canvas_size(project_state: dict[str, Any]) -> tuple[int, int]:
    timeline = project_state.get("timeline") if isinstance(project_state, dict) else {}
    width = int(_float((timeline or {}).get("width", 1920), "timeline.width", lo=2, hi=8192))
    height = int(_float((timeline or {}).get("height", 1080), "timeline.height", lo=2, hi=8192))
    return width + (width % 2), height + (height % 2)


def _shape(args: dict[str, Any], *, allowed: set[str], default: str) -> str:
    raw = str(args.get("shape") or args.get("type") or default).strip().lower().replace("-", "_")
    if raw not in allowed:
        _bad(f"shape must be one of {sorted(allowed)}, got {raw!r}.")
    if raw == "rectangle":
        return "rect"
    return raw


def _effect(args: dict[str, Any]) -> str:
    raw = str(args.get("effect") or args.get("operation") or "").strip().lower().replace("-", "_")
    if raw not in _MASK_EFFECTS:
        _bad(f"effect must be one of {sorted(_MASK_EFFECTS)}, got {raw!r}.")
    return raw


def _color(value: Any, *, default: str) -> str:
    raw = str(value or default).strip()
    if len(raw) != 7 or not raw.startswith("#") or any(ch not in _HEX_CHARS for ch in raw[1:]):
        _bad(f"color must look like #rrggbb, got {raw!r}.")
    return raw.lower()


def _rgb(value: Any, *, default: str = "#ff3030") -> tuple[int, int, int]:
    color = _color(value, default=default)
    return int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16)


def _float(value: Any, name: str, *, lo: float | None = None, hi: float | None = None) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        _bad(f"{name} must be a number, got {value!r}.")
    if lo is not None and number < lo:
        _bad(f"{name} must be >= {lo}, got {number}.")
    if hi is not None and number > hi:
        _bad(f"{name} must be <= {hi}, got {number}.")
    return number


def _time_range(args: dict[str, Any]) -> tuple[float, float]:
    raw = args.get("time_range")
    if isinstance(raw, (list, tuple)) and len(raw) >= 2:
        start = _float(raw[0], "time_range[0]", lo=0.0)
        end = _float(raw[1], "time_range[1]", lo=0.0)
        if end <= start:
            _bad("time_range end must be greater than start.")
        return start, end - start
    start = _float(args.get("start_sec", args.get("at_time", 0.0)), "start_sec", lo=0.0)
    if args.get("end_sec") is not None:
        end = _float(args.get("end_sec"), "end_sec", lo=0.0)
        if end <= start:
            _bad("end_sec must be greater than start_sec.")
        return start, end - start
    duration = _float(args.get("duration", 3.0), "duration", lo=0.05, hi=36000.0)
    return start, duration


def _video_time_window(args: dict[str, Any]) -> tuple[float, float | None]:
    start = _float(args.get("start_sec", 0.0), "start_sec", lo=0.0)
    if args.get("end_sec") is None:
        return start, None
    end = _float(args.get("end_sec"), "end_sec", lo=0.0)
    if end <= start:
        _bad("end_sec must be greater than start_sec.")
    return start, end


def _points(value: Any, *, min_count: int, name: str = "points") -> list[tuple[float, float]]:
    if not isinstance(value, (list, tuple)) or len(value) < min_count:
        _bad(f"{name} must contain at least {min_count} normalized points.")
    out: list[tuple[float, float]] = []
    for index, item in enumerate(value):
        if isinstance(item, dict):
            raw = [item.get("x"), item.get("y")]
        else:
            raw = item
        if not isinstance(raw, (list, tuple)) or len(raw) < 2:
            _bad(f"{name}[{index}] must be [x, y].")
        x = _float(raw[0], f"{name}[{index}].x", lo=0.0, hi=1.0)
        y = _float(raw[1], f"{name}[{index}].y", lo=0.0, hi=1.0)
        out.append((x, y))
    return out


def _px(point: tuple[float, float], width: int, height: int) -> tuple[int, int]:
    return int(round(point[0] * (width - 1))), int(round(point[1] * (height - 1)))


def _draw_overlay(
    image: Image.Image,
    shape: str,
    args: dict[str, Any],
    *,
    color: str,
    width_px: float,
    feather: float,
) -> None:
    width, height = image.size
    draw = ImageDraw.Draw(image)
    rgb = _rgb(color)
    line_width = max(1, int(round(width_px)))

    if shape == "stroke":
        pts = [_px(p, width, height) for p in _points(args.get("points"), min_count=2)]
        draw.line(pts, fill=(*rgb, 255), width=line_width, joint="curve")
    elif shape == "arrow":
        pts_norm = _points(args.get("points"), min_count=2)
        pts = [_px(p, width, height) for p in pts_norm]
        draw.line(pts, fill=(*rgb, 255), width=line_width, joint="curve")
        _draw_arrow_head(draw, pts[-2], pts[-1], rgb, line_width)
    elif shape in {"rect", "ellipse", "circle"}:
        box = _bbox(args, width, height)
        if shape == "rect":
            draw.rectangle(box, outline=(*rgb, 255), width=line_width)
        else:
            draw.ellipse(box, outline=(*rgb, 255), width=line_width)
    elif shape == "highlight":
        box = _bbox(args, width, height)
        fill_alpha = int(_float(args.get("fill_opacity", 0.35), "fill_opacity", lo=0.0, hi=1.0) * 255)
        draw.rectangle(box, fill=(*rgb, fill_alpha), outline=(*rgb, 255), width=line_width)
    elif shape == "text":
        text = str(args.get("text") or "").strip()
        if not text:
            _bad("text overlay requires a non-empty text field.")
        pts = _points(args.get("points", [[0.5, 0.5]]), min_count=1)
        font_size = int(_float(args.get("font_size", 48), "font_size", lo=6, hi=512))
        try:
            font = ImageFont.truetype("Arial.ttf", font_size)
        except Exception:
            font = ImageFont.load_default()
        draw.text(_px(pts[0], width, height), text, fill=(*rgb, 255), font=font)

    if feather > 0:
        alpha = image.getchannel("A").filter(ImageFilter.GaussianBlur(radius=feather))
        image.putalpha(alpha)


def _draw_arrow_head(
    draw: ImageDraw.ImageDraw,
    prev: tuple[int, int],
    end: tuple[int, int],
    rgb: tuple[int, int, int],
    line_width: int,
) -> None:
    dx = end[0] - prev[0]
    dy = end[1] - prev[1]
    if abs(dx) + abs(dy) < 1:
        return
    angle = math.atan2(dy, dx)
    length = max(line_width * 4.0, 18.0)
    spread = math.radians(30)
    left = (
        end[0] - length * math.cos(angle - spread),
        end[1] - length * math.sin(angle - spread),
    )
    right = (
        end[0] - length * math.cos(angle + spread),
        end[1] - length * math.sin(angle + spread),
    )
    draw.polygon([end, left, right], fill=(*rgb, 255))


def _bbox(spec: dict[str, Any], width: int, height: int) -> tuple[int, int, int, int]:
    rect = spec.get("rect")
    if isinstance(rect, (list, tuple)) and len(rect) >= 4:
        x0, y0, x1, y1 = [_float(v, f"rect[{i}]", lo=0.0, hi=1.0) for i, v in enumerate(rect[:4])]
    elif all(key in spec for key in ("x0", "y0", "x1", "y1")):
        x0 = _float(spec["x0"], "x0", lo=0.0, hi=1.0)
        y0 = _float(spec["y0"], "y0", lo=0.0, hi=1.0)
        x1 = _float(spec["x1"], "x1", lo=0.0, hi=1.0)
        y1 = _float(spec["y1"], "y1", lo=0.0, hi=1.0)
    elif "center" in spec or "radius" in spec:
        center = _points([spec.get("center", [0.5, 0.5])], min_count=1, name="center")[0]
        rx = _float(spec.get("rx", spec.get("radius", 0.25)), "radius", lo=0.0, hi=1.0)
        ry = _float(spec.get("ry", spec.get("radius", 0.25)), "radius", lo=0.0, hi=1.0)
        x0, y0, x1, y1 = center[0] - rx, center[1] - ry, center[0] + rx, center[1] + ry
        if min(x0, y0) < 0 or max(x1, y1) > 1:
            _bad("center/radius shape must stay inside normalized canvas bounds.")
    else:
        pts = _points(spec.get("points"), min_count=2)
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
    if x1 <= x0 or y1 <= y0:
        _bad("shape bounds must have positive width and height.")
    return (
        int(round(x0 * (width - 1))),
        int(round(y0 * (height - 1))),
        int(round(x1 * (width - 1))),
        int(round(y1 * (height - 1))),
    )


def _pick_overlay_track(
    state: dict[str, Any],
    *,
    start: float,
    duration: float,
    requested: str,
) -> tuple[str, bool]:
    timeline = state.get("timeline") if isinstance(state, dict) else {}
    tracks = [t for t in (timeline or {}).get("tracks", []) if isinstance(t, dict)]
    clips = [c for c in (timeline or {}).get("clips", []) if isinstance(c, dict)]
    existing = {str(t.get("id")) for t in tracks}
    if requested:
        return requested, requested not in existing

    overlay_ids = [str(t.get("id")) for t in tracks if str(t.get("kind")) == "overlay"]
    if not overlay_ids:
        overlay_ids = ["OV1"]
    for track_id in overlay_ids:
        if not _track_has_overlap(clips, track_id, start, duration):
            return track_id, track_id not in existing
    index = 1
    while f"OV{index}" in existing:
        index += 1
    return f"OV{index}", True


def _track_has_overlap(clips: list[dict[str, Any]], track_id: str, start: float, duration: float) -> bool:
    end = start + duration
    for clip in clips:
        if str(clip.get("track_id")) != track_id:
            continue
        c_start = float(clip.get("start") or 0.0)
        c_end = c_start + float(clip.get("duration") or 0.0)
        if start < c_end - 1e-3 and c_start < end - 1e-3:
            return True
    return False


def _process_image(src: Path, out: Path, mask_spec: dict[str, Any], effect: str, params: dict[str, Any]) -> None:
    image = Image.open(src).convert("RGB")
    try:
        rgb = np.array(image)
        mask = _mask_array(mask_spec, rgb.shape[1], rgb.shape[0])
        result = _apply_effect_rgb(rgb, mask, effect, params)
        Image.fromarray(result).save(out)
    finally:
        image.close()


def _process_video(
    src: Path,
    out: Path,
    mask_spec: dict[str, Any],
    effect: str,
    params: dict[str, Any],
    start_sec: float,
    end_sec: float | None,
) -> float:
    try:
        import cv2
    except Exception as exc:
        raise ToolError(
            "paint_mask_effect video processing needs OpenCV.",
            code="E_UNSUPPORTED",
            recovery=RECOVERY_SWITCH_TOOL,
        ) from exc

    cap = cv2.VideoCapture(str(src))
    if not cap.isOpened():
        raise ToolError(f"could not open video asset: {src}", code="E_MEDIA", recovery=RECOVERY_SWITCH_TOOL)
    fps = float(cap.get(cv2.CAP_PROP_FPS) or 30.0)
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
    if width <= 0 or height <= 0:
        cap.release()
        raise ToolError("video has invalid dimensions.", code="E_MEDIA", recovery=RECOVERY_SWITCH_TOOL)
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    duration = total_frames / fps if total_frames > 0 else 0.0
    mask = _mask_array(mask_spec, width, height)

    temp = Path(tempfile.mkstemp(prefix="lumeri-paint-mask-", suffix=".mp4")[1])
    writer = cv2.VideoWriter(str(temp), cv2.VideoWriter_fourcc(*"mp4v"), fps, (width, height))
    if not writer.isOpened():
        cap.release()
        temp.unlink(missing_ok=True)
        raise ToolError("could not open temporary video writer.", code="E_MEDIA", recovery=RECOVERY_SWITCH_TOOL)
    try:
        frame_index = 0
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            t = frame_index / fps
            if t >= start_sec and (end_sec is None or t < end_sec):
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                rgb = _apply_effect_rgb(rgb, mask, effect, params)
                frame = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
            writer.write(frame)
            frame_index += 1
    finally:
        cap.release()
        writer.release()
    try:
        _mux_optional_audio(temp, src, out)
    finally:
        temp.unlink(missing_ok=True)
    return duration


def _mask_array(spec: dict[str, Any], width: int, height: int) -> np.ndarray:
    shape = _shape(spec, allowed=_MASK_SHAPES, default="rect")
    feather = _float(spec.get("feather", 0.0), "mask.feather", lo=0.0, hi=256.0)
    invert = bool(spec.get("invert", False))
    mask_img = Image.new("L", (width, height), 0)
    draw = ImageDraw.Draw(mask_img)

    if shape in {"rect", "ellipse", "circle"}:
        box = _bbox(spec, width, height)
        if shape == "rect":
            draw.rectangle(box, fill=255)
        else:
            draw.ellipse(box, fill=255)
    elif shape == "polygon":
        pts = [_px(p, width, height) for p in _points(spec.get("points"), min_count=3, name="mask.points")]
        draw.polygon(pts, fill=255)
    elif shape == "stroke":
        pts = [_px(p, width, height) for p in _points(spec.get("points"), min_count=2, name="mask.points")]
        stroke_width = _float(spec.get("width", 24.0), "mask.width", lo=0.1, hi=512.0)
        draw.line(pts, fill=255, width=max(1, int(round(stroke_width))), joint="curve")

    if feather > 0:
        mask_img = mask_img.filter(ImageFilter.GaussianBlur(radius=feather))
    arr = np.asarray(mask_img).astype(np.float32) / 255.0
    if invert:
        arr = 1.0 - arr
    return arr.clip(0.0, 1.0)


def _apply_effect_rgb(rgb: np.ndarray, mask: np.ndarray, effect: str, params: dict[str, Any]) -> np.ndarray:
    original = rgb.astype(np.float32)
    target = mask.astype(np.float32)
    if effect == "dim_outside":
        target = 1.0 - target
        amount = _float(params.get("amount", 0.45), "amount", lo=0.0, hi=1.0)
        color = np.array(_rgb(params.get("color"), default="#000000"), dtype=np.float32)
        modified = original * (1.0 - amount) + color.reshape(1, 1, 3) * amount
    elif effect == "blur":
        radius = max(1, int(round(_float(params.get("radius", params.get("blur_radius", 16)), "radius", lo=0.1, hi=256.0))))
        kernel = radius * 2 + 1
        import cv2

        modified = cv2.GaussianBlur(original, (kernel, kernel), 0)
    elif effect == "mosaic":
        block = max(2, int(round(_float(params.get("block_size", 18), "block_size", lo=2, hi=256))))
        import cv2

        h, w = rgb.shape[:2]
        small = cv2.resize(rgb, (max(1, w // block), max(1, h // block)), interpolation=cv2.INTER_LINEAR)
        modified = cv2.resize(small, (w, h), interpolation=cv2.INTER_NEAREST).astype(np.float32)
    elif effect == "highlight":
        amount = _float(params.get("amount", 0.35), "amount", lo=0.0, hi=1.0)
        color = np.array(_rgb(params.get("color"), default="#fff36a"), dtype=np.float32)
        modified = original * (1.0 - amount) + color.reshape(1, 1, 3) * amount
    elif effect == "adjust":
        modified = _adjust_rgb(rgb, params).astype(np.float32)
    else:
        _bad(f"unsupported paint mask effect: {effect}")
    alpha = target[..., None]
    out = original * (1.0 - alpha) + modified * alpha
    return np.clip(out, 0, 255).astype(np.uint8)


def _adjust_rgb(rgb: np.ndarray, params: dict[str, Any]) -> np.ndarray:
    brightness = _float(params.get("brightness", 0.0), "brightness", lo=-1.0, hi=1.0)
    contrast = _float(params.get("contrast", 1.0), "contrast", lo=0.0, hi=3.0)
    saturation = _float(params.get("saturation", 1.0), "saturation", lo=0.0, hi=3.0)
    exposure = _float(params.get("exposure", 0.0), "exposure", lo=-5.0, hi=5.0)
    gamma = _float(params.get("gamma", 1.0), "gamma", lo=0.1, hi=10.0)
    image = Image.fromarray(rgb)
    image = ImageEnhance.Contrast(image).enhance(contrast)
    image = ImageEnhance.Color(image).enhance(saturation)
    arr = np.asarray(image).astype(np.float32)
    arr = arr * (2.0 ** exposure)
    arr = arr + brightness * 255.0
    arr = np.clip(arr / 255.0, 0.0, 1.0)
    arr = np.power(arr, 1.0 / gamma)
    return np.clip(arr * 255.0, 0, 255).astype(np.uint8)


def _mux_optional_audio(video_only: Path, source: Path, out: Path) -> None:
    head = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(video_only),
        "-i", str(source),
        "-map", "0:v:0",
        "-map", "1:a?",
    ]
    tail = [
        "-c:a", "aac",
        "-shortest",
        "-movflags", "+faststart",
        str(out),
    ]
    # This mux re-encodes the video track, so it goes through the same GPU
    # encoder as the rest of the pipeline. This runs outside
    # run_ffmpeg_with_progress (raw subprocess), so we carry the double-try
    # here: hardware encoder first, then software, then keep the video and
    # drop the audio mux as a last resort.
    candidates = [get_video_encoder_args("h264")]
    cpu = cpu_video_encoder_args("h264")
    if cpu != candidates[0]:
        candidates.append(cpu)
    for encoder_args in candidates:
        proc = subprocess.run(head + encoder_args + tail, capture_output=True, text=True, timeout=180)
        if proc.returncode == 0:
            return
    shutil.copyfile(video_only, out)


def _image_ext(path: Path) -> str:
    ext = path.suffix.lower()
    return ext if ext in {".png", ".jpg", ".jpeg", ".webp"} else ".png"


def _bad(message: str) -> None:
    raise ToolError(message, code="E_BAD_ARG", recovery=RECOVERY_FIX_ARGS)


__all__ = ["dispatch_overlay", "dispatch_mask_effect"]
