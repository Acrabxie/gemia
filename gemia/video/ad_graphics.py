"""Commercial ad-graphics primitives for Lumeri.

This module borrows the useful shape of Hyperframes without vendoring it:
each render produces a deterministic video plus sidecar composition artifacts
that describe timed blocks, layout, colors, and reusable component intent.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont


@dataclass(frozen=True)
class AdGraphicsRenderResult:
    output_path: str
    metadata_path: str
    composition_path: str
    frame_count: int


_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}

_PALETTES: dict[str, dict[str, tuple[int, int, int, int]]] = {
    "ice": {
        "bg": (14, 17, 20, 255),
        "fg": (245, 248, 250, 255),
        "muted": (166, 176, 186, 255),
        "panel": (25, 29, 35, 218),
        "panel_alt": (54, 61, 70, 180),
        "accent": (121, 216, 255, 255),
        "accent_soft": (121, 216, 255, 72),
    },
    "mono": {
        "bg": (242, 243, 245, 255),
        "fg": (22, 24, 27, 255),
        "muted": (94, 100, 108, 255),
        "panel": (255, 255, 255, 225),
        "panel_alt": (217, 221, 226, 205),
        "accent": (67, 186, 235, 255),
        "accent_soft": (67, 186, 235, 58),
    },
    "night": {
        "bg": (7, 9, 13, 255),
        "fg": (255, 255, 255, 255),
        "muted": (176, 184, 194, 255),
        "panel": (8, 10, 14, 228),
        "panel_alt": (31, 37, 44, 190),
        "accent": (88, 202, 255, 255),
        "accent_soft": (88, 202, 255, 80),
    },
}


def render_ad_title_pack(
    input_path: str,
    output_path: str,
    *,
    title: str = "Lumeri",
    subtitle: str = "",
    kicker: str = "",
    cta: str = "",
    duration: float = 3.0,
    style: str = "ice",
    width: int = 1920,
    height: int = 1080,
    fps: int = 30,
) -> str:
    """Render a commercial title pack over footage or as a standalone card."""
    spec = _composition_spec(
        "ad_title_pack",
        style=style,
        duration=duration,
        blocks=[
            {"id": "kicker", "kind": "text", "role": "eyebrow", "text": kicker},
            {"id": "title", "kind": "text", "role": "headline", "text": title},
            {"id": "subtitle", "kind": "text", "role": "supporting", "text": subtitle},
            {"id": "cta", "kind": "button", "role": "call_to_action", "text": cta},
        ],
    )

    def draw(ctx: _DrawContext) -> None:
        progress = _ease_out_cubic(ctx.local_progress)
        if not ctx.has_source:
            _draw_ad_background(ctx.image, ctx.palette, ctx.frame_index)
        wash = Image.new("RGBA", ctx.image.size, (0, 0, 0, int(84 * progress)))
        ctx.image.alpha_composite(wash)
        margin = int(ctx.width * 0.08)
        panel_w = int(ctx.width * 0.72)
        panel_h = int(ctx.height * (0.46 if cta else 0.38))
        x = margin
        y = int(ctx.height * (0.5 - 0.20 * progress))
        _rounded_rect(ctx.draw, (x, y, x + panel_w, y + panel_h), radius=int(ctx.height * 0.035), fill=ctx.palette["panel"])
        _draw_accent_rule(ctx.draw, x + 28, y + 28, int(panel_w * progress), ctx.palette)
        cursor = y + 54
        if kicker:
            _draw_text(ctx.draw, kicker.upper(), (x + 42, cursor), int(ctx.height * 0.027), ctx.palette["accent"], bold=True)
            cursor += int(ctx.height * 0.056)
        _draw_text(
            ctx.draw,
            title,
            (x + 42, cursor),
            int(ctx.height * 0.082),
            ctx.palette["fg"],
            bold=True,
            max_width=panel_w - 84,
        )
        cursor += int(ctx.height * 0.16)
        if subtitle:
            _draw_text(
                ctx.draw,
                subtitle,
                (x + 45, cursor),
                int(ctx.height * 0.034),
                ctx.palette["muted"],
                max_width=panel_w - 90,
            )
        if cta:
            _draw_button(ctx.draw, cta, (x + 42, y + panel_h - int(ctx.height * 0.092)), ctx.palette, ctx.height)
        _draw_corner_pattern(ctx.draw, ctx.width, ctx.height, ctx.palette, progress)

    return _render_ad_scene(
        input_path,
        output_path,
        effect="lumeri_ad_title_pack",
        style=style,
        width=width,
        height=height,
        fps=fps,
        duration=duration,
        active_duration=duration,
        draw=draw,
        composition=spec,
        params={"title": title, "subtitle": subtitle, "kicker": kicker, "cta": cta},
    )


def render_lower_third(
    input_path: str,
    output_path: str,
    *,
    title: str = "Lumeri",
    subtitle: str = "",
    duration: float = 4.0,
    start_sec: float = 0.0,
    style: str = "ice",
    position: str = "left",
    width: int = 1920,
    height: int = 1080,
    fps: int = 30,
) -> str:
    """Render a polished lower-third label over the current video."""
    spec = _composition_spec(
        "lower_third",
        style=style,
        duration=duration,
        blocks=[
            {"id": "title", "kind": "text", "role": "headline", "text": title},
            {"id": "subtitle", "kind": "text", "role": "supporting", "text": subtitle},
        ],
    )

    def draw(ctx: _DrawContext) -> None:
        if not ctx.active:
            return
        enter = _ease_out_cubic(min(ctx.local_progress * 3.0, 1.0))
        leave = _ease_out_cubic(min((1.0 - ctx.local_progress) * 4.0, 1.0))
        alpha_scale = min(enter, leave)
        panel_w = int(ctx.width * 0.42)
        panel_h = int(ctx.height * (0.16 if subtitle else 0.12))
        y = int(ctx.height * 0.72)
        side_left = position.lower() not in {"right", "r"}
        target_x = int(ctx.width * 0.055) if side_left else ctx.width - panel_w - int(ctx.width * 0.055)
        x = int(target_x + (-70 if side_left else 70) * (1.0 - enter))
        panel = _with_alpha(ctx.palette["panel"], int(ctx.palette["panel"][3] * alpha_scale))
        _rounded_rect(ctx.draw, (x, y, x + panel_w, y + panel_h), radius=18, fill=panel)
        _rounded_rect(
            ctx.draw,
            (x, y, x + 9, y + panel_h),
            radius=8,
            fill=_with_alpha(ctx.palette["accent"], int(255 * alpha_scale)),
        )
        _draw_text(ctx.draw, title, (x + 28, y + 22), int(ctx.height * 0.036), _with_alpha(ctx.palette["fg"], int(255 * alpha_scale)), bold=True, max_width=panel_w - 56)
        if subtitle:
            _draw_text(ctx.draw, subtitle, (x + 28, y + 70), int(ctx.height * 0.024), _with_alpha(ctx.palette["muted"], int(255 * alpha_scale)), max_width=panel_w - 56)

    return _render_ad_scene(
        input_path,
        output_path,
        effect="lumeri_lower_third",
        style=style,
        width=width,
        height=height,
        fps=fps,
        duration=max(duration + start_sec, duration),
        active_start=start_sec,
        active_duration=duration,
        draw=draw,
        composition=spec,
        params={"title": title, "subtitle": subtitle, "position": position, "start_sec": start_sec},
    )


def render_cta_card(
    input_path: str,
    output_path: str,
    *,
    headline: str = "Ready to create?",
    body: str = "",
    button_text: str = "Try Lumeri",
    duration: float = 2.5,
    start_sec: float | None = None,
    style: str = "ice",
    width: int = 1920,
    height: int = 1080,
    fps: int = 30,
) -> str:
    """Render an end-card call-to-action panel."""
    probe = _probe_media(input_path, width=width, height=height, fps=fps, duration=duration)
    resolved_start = start_sec
    if resolved_start is None and probe["has_source"]:
        resolved_start = max(0.0, float(probe["duration"]) - max(float(duration), 0.5))
    if resolved_start is None:
        resolved_start = 0.0
    spec = _composition_spec(
        "cta_card",
        style=style,
        duration=duration,
        blocks=[
            {"id": "headline", "kind": "text", "role": "headline", "text": headline},
            {"id": "body", "kind": "text", "role": "supporting", "text": body},
            {"id": "button", "kind": "button", "role": "call_to_action", "text": button_text},
        ],
    )

    def draw(ctx: _DrawContext) -> None:
        if not ctx.active:
            return
        progress = _ease_out_cubic(min(ctx.local_progress * 2.0, 1.0))
        if not ctx.has_source:
            _draw_ad_background(ctx.image, ctx.palette, ctx.frame_index)
        dim = Image.new("RGBA", ctx.image.size, (0, 0, 0, int(116 * progress)))
        ctx.image.alpha_composite(dim)
        card_w = int(ctx.width * 0.58)
        card_h = int(ctx.height * 0.44)
        x = int((ctx.width - card_w) / 2)
        y = int(ctx.height * 0.26 + 36 * (1.0 - progress))
        _rounded_rect(ctx.draw, (x, y, x + card_w, y + card_h), radius=26, fill=ctx.palette["panel"])
        _draw_corner_pattern(ctx.draw, ctx.width, ctx.height, ctx.palette, progress)
        _draw_text(ctx.draw, headline, (x + 54, y + 54), int(ctx.height * 0.062), ctx.palette["fg"], bold=True, max_width=card_w - 108)
        if body:
            _draw_text(ctx.draw, body, (x + 56, y + int(ctx.height * 0.17)), int(ctx.height * 0.03), ctx.palette["muted"], max_width=card_w - 112)
        _draw_button(ctx.draw, button_text, (x + 56, y + card_h - int(ctx.height * 0.11)), ctx.palette, ctx.height)

    return _render_ad_scene(
        input_path,
        output_path,
        effect="lumeri_cta_card",
        style=style,
        width=width,
        height=height,
        fps=fps,
        duration=max(duration + resolved_start, duration),
        active_start=resolved_start,
        active_duration=duration,
        draw=draw,
        composition=spec,
        params={"headline": headline, "body": body, "button_text": button_text, "start_sec": resolved_start},
        probe_override=probe,
    )


def render_product_callout(
    input_path: str,
    output_path: str,
    *,
    label: str = "Key Feature",
    detail: str = "",
    badge: str = "",
    point_x: float = 0.72,
    point_y: float = 0.42,
    duration: float = 3.0,
    start_sec: float = 0.0,
    style: str = "ice",
    width: int = 1920,
    height: int = 1080,
    fps: int = 30,
) -> str:
    """Render a product callout with pointer line, label, and optional badge."""
    spec = _composition_spec(
        "product_callout",
        style=style,
        duration=duration,
        blocks=[
            {"id": "label", "kind": "text", "role": "headline", "text": label},
            {"id": "detail", "kind": "text", "role": "supporting", "text": detail},
            {"id": "badge", "kind": "badge", "role": "offer", "text": badge},
        ],
    )

    def draw(ctx: _DrawContext) -> None:
        if not ctx.active:
            return
        progress = _ease_out_cubic(min(ctx.local_progress * 2.4, 1.0))
        px = int(_clamp(point_x, 0.05, 0.95) * ctx.width)
        py = int(_clamp(point_y, 0.08, 0.88) * ctx.height)
        card_w = int(ctx.width * 0.32)
        card_h = int(ctx.height * (0.22 if detail else 0.16))
        card_x = max(28, min(ctx.width - card_w - 28, px - int(card_w * 0.88)))
        card_y = max(28, min(ctx.height - card_h - 28, py - int(ctx.height * 0.25)))
        end_x = card_x + card_w - 18
        end_y = card_y + card_h + 10
        ctx.draw.line((end_x, end_y, px, py), fill=_with_alpha(ctx.palette["accent"], int(255 * progress)), width=max(2, int(ctx.height * 0.006)))
        radius = int(11 + 15 * math.sin(ctx.frame_index / max(ctx.fps, 1) * math.pi))
        ctx.draw.ellipse((px - radius, py - radius, px + radius, py + radius), outline=_with_alpha(ctx.palette["accent"], 210), width=3)
        ctx.draw.ellipse((px - 5, py - 5, px + 5, py + 5), fill=ctx.palette["accent"])
        _rounded_rect(ctx.draw, (card_x, card_y, card_x + card_w, card_y + card_h), radius=20, fill=ctx.palette["panel"])
        if badge:
            _draw_badge(ctx.draw, badge, (card_x + 24, card_y + 20), ctx.palette, ctx.height)
            text_y = card_y + int(ctx.height * 0.075)
        else:
            text_y = card_y + 24
        _draw_text(ctx.draw, label, (card_x + 24, text_y), int(ctx.height * 0.034), ctx.palette["fg"], bold=True, max_width=card_w - 48)
        if detail:
            _draw_text(ctx.draw, detail, (card_x + 24, text_y + int(ctx.height * 0.054)), int(ctx.height * 0.023), ctx.palette["muted"], max_width=card_w - 48)

    return _render_ad_scene(
        input_path,
        output_path,
        effect="lumeri_product_callout",
        style=style,
        width=width,
        height=height,
        fps=fps,
        duration=max(duration + start_sec, duration),
        active_start=start_sec,
        active_duration=duration,
        draw=draw,
        composition=spec,
        params={"label": label, "detail": detail, "badge": badge, "point_x": point_x, "point_y": point_y},
    )


def render_shimmer_sweep(
    input_path: str,
    output_path: str,
    *,
    text: str = "Lumeri",
    subtitle: str = "",
    duration: float = 2.0,
    style: str = "ice",
    width: int = 1920,
    height: int = 1080,
    fps: int = 30,
) -> str:
    """Render a premium shimmer/sweep text animation."""
    spec = _composition_spec(
        "shimmer_sweep",
        style=style,
        duration=duration,
        blocks=[
            {"id": "text", "kind": "text", "role": "headline", "text": text},
            {"id": "subtitle", "kind": "text", "role": "supporting", "text": subtitle},
            {"id": "sweep", "kind": "animation", "role": "highlight", "easing": "seekable_linear"},
        ],
    )

    def draw(ctx: _DrawContext) -> None:
        progress = ctx.local_progress
        if not ctx.has_source:
            _draw_ad_background(ctx.image, ctx.palette, ctx.frame_index)
        overlay = Image.new("RGBA", ctx.image.size, (0, 0, 0, 0))
        overlay_draw = ImageDraw.Draw(overlay)
        font_size = int(ctx.height * 0.105)
        font = _font(font_size, bold=True)
        bbox = overlay_draw.textbbox((0, 0), text, font=font)
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]
        x = int((ctx.width - tw) / 2)
        y = int(ctx.height * 0.42 - th / 2)
        overlay_draw.text((x, y), text, font=font, fill=ctx.palette["fg"])
        shine_x = int((x - tw * 0.35) + progress * (tw * 1.7))
        overlay_draw.polygon(
            [
                (shine_x - 34, y - 16),
                (shine_x + 38, y - 16),
                (shine_x + 3, y + th + 30),
                (shine_x - 70, y + th + 30),
            ],
            fill=ctx.palette["accent_soft"],
        )
        ctx.image.alpha_composite(overlay)
        _draw_accent_rule(ctx.draw, int(ctx.width * 0.35), y + th + 34, int(ctx.width * 0.30), ctx.palette)
        if subtitle:
            _draw_text(ctx.draw, subtitle, (int(ctx.width * 0.32), y + th + 58), int(ctx.height * 0.03), ctx.palette["muted"], max_width=int(ctx.width * 0.36))

    return _render_ad_scene(
        input_path,
        output_path,
        effect="lumeri_shimmer_sweep",
        style=style,
        width=width,
        height=height,
        fps=fps,
        duration=duration,
        active_duration=duration,
        draw=draw,
        composition=spec,
        params={"text": text, "subtitle": subtitle},
    )


def compose_overlay_on_video(
    input_path: str,
    output_path: str,
    *,
    overlay_path: str,
    opacity: float = 0.9,
    position: str = "center",
    scale: float = 1.0,
    start_sec: float = 0.0,
    duration: float | None = None,
) -> str:
    """Composite an image/video ad overlay onto a base video with deterministic timing."""
    base_probe = _probe_media(input_path, width=1920, height=1080, fps=30, duration=3.0)
    if not base_probe["has_source"] or base_probe["kind"] != "video":
        raise FileNotFoundError(f"Base video is required for overlay composition: {input_path}")
    overlay = Path(overlay_path).expanduser().resolve()
    if not overlay.exists():
        raise FileNotFoundError(f"Overlay does not exist: {overlay}")

    output = _resolve_output(output_path)
    cap = cv2.VideoCapture(str(base_probe["path"]))
    overlay_cap = cv2.VideoCapture(str(overlay)) if overlay.suffix.lower() not in _IMAGE_EXTS else None
    overlay_image = cv2.imread(str(overlay), cv2.IMREAD_UNCHANGED) if overlay_cap is None else None
    writer = _video_writer(output, int(base_probe["width"]), int(base_probe["height"]), float(base_probe["fps"]))
    last_overlay: np.ndarray | None = None
    frame_count = int(base_probe["frames"])
    end_sec = float("inf") if duration is None else start_sec + max(float(duration), 0.01)
    try:
        for index in range(frame_count):
            ok, frame = cap.read()
            if not ok:
                break
            t = index / max(float(base_probe["fps"]), 1.0)
            if start_sec <= t <= end_sec:
                current_overlay = overlay_image
                if overlay_cap is not None:
                    ok_overlay, video_overlay = overlay_cap.read()
                    if ok_overlay:
                        last_overlay = video_overlay
                    current_overlay = last_overlay
                if current_overlay is not None:
                    frame = _blend_overlay(frame, current_overlay, opacity=float(opacity), position=position, scale=scale)
            writer.write(frame)
    finally:
        cap.release()
        if overlay_cap is not None:
            overlay_cap.release()
        writer.release()

    metadata_path = output.with_suffix(".ad_graphics.json")
    composition_path = output.with_suffix(".ad_composition.html")
    composition = _composition_spec(
        "overlay_composite",
        style="ice",
        duration=duration or float(base_probe["duration"]),
        blocks=[{"id": "overlay", "kind": "media", "source": str(overlay), "opacity": opacity, "position": position}],
    )
    _write_composition_artifacts(
        output,
        effect="lumeri_overlay_composite",
        probe=base_probe,
        frame_count=frame_count,
        style="ice",
        params={"overlay_path": str(overlay), "opacity": opacity, "position": position, "scale": scale, "start_sec": start_sec, "duration": duration},
        composition=composition,
        metadata_path=metadata_path,
        composition_path=composition_path,
    )
    return str(output)


@dataclass
class _DrawContext:
    image: Image.Image
    draw: ImageDraw.ImageDraw
    frame_index: int
    fps: float
    width: int
    height: int
    time_sec: float
    local_progress: float
    active: bool
    has_source: bool
    palette: dict[str, tuple[int, int, int, int]]


def _render_ad_scene(
    input_path: str,
    output_path: str,
    *,
    effect: str,
    style: str,
    width: int,
    height: int,
    fps: int,
    duration: float,
    draw: Callable[[_DrawContext], None],
    composition: dict[str, Any],
    params: dict[str, Any],
    active_start: float = 0.0,
    active_duration: float | None = None,
    probe_override: dict[str, Any] | None = None,
) -> str:
    output = _resolve_output(output_path)
    probe = probe_override or _probe_media(input_path, width=width, height=height, fps=fps, duration=duration)
    palette = _palette(style)
    total_frames = int(probe["frames"])
    writer = _video_writer(output, int(probe["width"]), int(probe["height"]), float(probe["fps"]))
    cap = cv2.VideoCapture(str(probe["path"])) if probe["kind"] == "video" and probe["path"] else None
    source_image = cv2.imread(str(probe["path"])) if probe["kind"] == "image" and probe["path"] else None
    try:
        for index in range(total_frames):
            frame = _base_frame(index, probe, cap, source_image, palette)
            rgba = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)).convert("RGBA")
            drawer = ImageDraw.Draw(rgba, "RGBA")
            time_sec = index / max(float(probe["fps"]), 1.0)
            local = (time_sec - active_start) / max(float(active_duration or duration), 0.001)
            active = 0.0 <= local <= 1.0
            if not probe["has_source"]:
                active = True
            ctx = _DrawContext(
                image=rgba,
                draw=drawer,
                frame_index=index,
                fps=float(probe["fps"]),
                width=int(probe["width"]),
                height=int(probe["height"]),
                time_sec=time_sec,
                local_progress=_clamp(local, 0.0, 1.0),
                active=active,
                has_source=bool(probe["has_source"]),
                palette=palette,
            )
            draw(ctx)
            writer.write(cv2.cvtColor(np.asarray(rgba.convert("RGB")), cv2.COLOR_RGB2BGR))
    finally:
        if cap is not None:
            cap.release()
        writer.release()
    _write_composition_artifacts(
        output,
        effect=effect,
        probe=probe,
        frame_count=total_frames,
        style=style,
        params=params,
        composition=composition,
        metadata_path=output.with_suffix(".ad_graphics.json"),
        composition_path=output.with_suffix(".ad_composition.html"),
    )
    return str(output)


def _probe_media(input_path: str, *, width: int, height: int, fps: int, duration: float) -> dict[str, Any]:
    source = Path(input_path).expanduser().resolve() if input_path else None
    if source and source.exists() and source.suffix.lower() in _IMAGE_EXTS:
        image = cv2.imread(str(source))
        if image is not None:
            h, w = image.shape[:2]
            frame_count = max(1, int(max(duration, 0.1) * max(fps, 1)))
            return {
                "kind": "image",
                "path": source,
                "has_source": True,
                "width": int(w),
                "height": int(h),
                "fps": float(fps),
                "frames": frame_count,
                "duration": frame_count / max(float(fps), 1.0),
            }
    if source and source.exists():
        cap = cv2.VideoCapture(str(source))
        try:
            if cap.isOpened():
                w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or width or 1920)
                h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or height or 1080)
                source_fps = float(cap.get(cv2.CAP_PROP_FPS) or fps or 30.0)
                frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
                if frames <= 0:
                    frames = max(1, int(max(duration, 0.1) * max(source_fps, 1.0)))
                return {
                    "kind": "video",
                    "path": source,
                    "has_source": True,
                    "width": w,
                    "height": h,
                    "fps": source_fps,
                    "frames": frames,
                    "duration": frames / max(source_fps, 1.0),
                }
        finally:
            cap.release()
    frame_count = max(1, int(max(duration, 0.1) * max(fps, 1)))
    return {
        "kind": "blank",
        "path": None,
        "has_source": False,
        "width": int(width or 1920),
        "height": int(height or 1080),
        "fps": float(fps or 30),
        "frames": frame_count,
        "duration": frame_count / max(float(fps or 30), 1.0),
    }


def _base_frame(
    index: int,
    probe: dict[str, Any],
    cap: cv2.VideoCapture | None,
    source_image: np.ndarray | None,
    palette: dict[str, tuple[int, int, int, int]],
) -> np.ndarray:
    width = int(probe["width"])
    height = int(probe["height"])
    if cap is not None:
        ok, frame = cap.read()
        if ok:
            return _fit_frame(frame, width, height)
    if source_image is not None:
        return _fit_frame(source_image, width, height)
    bg = np.zeros((height, width, 3), dtype=np.uint8)
    bg[:, :] = (palette["bg"][2], palette["bg"][1], palette["bg"][0])
    accent = palette["accent"]
    sweep = int((index * 7) % max(width, 1))
    cv2.rectangle(bg, (max(0, sweep - width // 8), 0), (min(width, sweep + width // 8), height), (accent[2] // 5, accent[1] // 5, accent[0] // 5), -1)
    return bg


def _fit_frame(frame: np.ndarray, width: int, height: int) -> np.ndarray:
    if frame.shape[1] == width and frame.shape[0] == height:
        return frame
    return cv2.resize(frame, (width, height), interpolation=cv2.INTER_AREA)


def _video_writer(output: Path, width: int, height: int, fps: float) -> cv2.VideoWriter:
    output.parent.mkdir(parents=True, exist_ok=True)
    writer = cv2.VideoWriter(str(output), cv2.VideoWriter_fourcc(*"mp4v"), max(float(fps), 1.0), (width, height))
    if not writer.isOpened():
        raise RuntimeError(f"Could not open video writer for {output}")
    return writer


def _resolve_output(output_path: str) -> Path:
    output = Path(output_path).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    return output


def _write_composition_artifacts(
    output: Path,
    *,
    effect: str,
    probe: dict[str, Any],
    frame_count: int,
    style: str,
    params: dict[str, Any],
    composition: dict[str, Any],
    metadata_path: Path,
    composition_path: Path,
) -> None:
    composition = dict(composition)
    composition["canvas"] = {"width": int(probe["width"]), "height": int(probe["height"]), "fps": float(probe["fps"])}
    composition["source"] = {"kind": probe["kind"], "path": str(probe["path"]) if probe["path"] else None}
    composition_path.write_text(_composition_html(composition), encoding="utf-8")
    metadata = {
        "schema_version": 1,
        "effect": effect,
        "rendered_at": datetime.now(timezone.utc).isoformat(),
        "output_path": str(output),
        "composition_path": str(composition_path),
        "hyperframes_inspired": True,
        "composition_contract": {
            "data_composition_id": composition["id"],
            "data_start": 0,
            "data_duration": composition["duration"],
            "data_track_index": 0,
            "deterministic_seekable_timeline": True,
            "transparent_overlay_intent": True,
        },
        "style": style,
        "params": params,
        "source": {"kind": probe["kind"], "path": str(probe["path"]) if probe["path"] else None},
        "frame_count": frame_count,
        "fps": float(probe["fps"]),
        "duration": frame_count / max(float(probe["fps"]), 1.0),
        "composition": composition,
    }
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _composition_spec(kind: str, *, style: str, duration: float, blocks: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "id": f"lumeri-{kind}",
        "kind": kind,
        "style": style,
        "duration": max(float(duration), 0.1),
        "timeline": {"adapter": "lumeri-seekable", "repeat": "none", "clock": "frame-index"},
        "blocks": [
            {
                **block,
                "data-start": block.get("data-start", 0),
                "data-duration": block.get("data-duration", duration),
                "data-track-index": block.get("data-track-index", index),
            }
            for index, block in enumerate(blocks)
            if str(block.get("text", block.get("source", block.get("kind", "")))).strip()
        ],
    }


def _composition_html(spec: dict[str, Any]) -> str:
    width = int(spec.get("canvas", {}).get("width") or 1920)
    height = int(spec.get("canvas", {}).get("height") or 1080)
    blocks = "\n".join(
        "    <section class=\"block\" "
        f"data-block-id=\"{_escape(block.get('id'))}\" "
        f"data-kind=\"{_escape(block.get('kind'))}\" "
        f"data-start=\"{_escape(block.get('data-start'))}\" "
        f"data-duration=\"{_escape(block.get('data-duration'))}\" "
        f"data-track-index=\"{_escape(block.get('data-track-index'))}\">{_escape(block.get('text') or block.get('source') or block.get('role'))}</section>"
        for block in spec.get("blocks", [])
    )
    return (
        "<!doctype html>\n"
        "<html><head><meta charset=\"utf-8\"><title>Lumeri Ad Composition</title>\n"
        "<style>body{margin:0;background:#0e1114;color:#f5f8fa;font-family:Arial,sans-serif}.composition{position:relative;overflow:hidden}.block{position:relative;margin:24px;padding:16px;border-radius:16px;background:rgba(255,255,255,.08)}</style>\n"
        "</head><body>\n"
        f"  <main class=\"composition\" data-composition-id=\"{_escape(spec.get('id'))}\" data-width=\"{width}\" data-height=\"{height}\" data-duration=\"{_escape(spec.get('duration'))}\" style=\"width:{width}px;height:{height}px\">\n"
        f"{blocks}\n"
        "  </main>\n"
        "  <script>window.__lumeriComposition = "
        + json.dumps(spec, ensure_ascii=False)
        + ";</script>\n"
        "</body></html>\n"
    )


def _escape(value: Any) -> str:
    text = str(value if value is not None else "")
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _palette(style: str) -> dict[str, tuple[int, int, int, int]]:
    return dict(_PALETTES.get(str(style or "ice").lower(), _PALETTES["ice"]))


def _draw_ad_background(image: Image.Image, palette: dict[str, tuple[int, int, int, int]], frame_index: int) -> None:
    width, height = image.size
    bg = Image.new("RGBA", image.size, palette["bg"])
    draw = ImageDraw.Draw(bg, "RGBA")
    for y in range(0, height, max(1, height // 36)):
        alpha = int(16 + 26 * y / max(height, 1))
        draw.rectangle((0, y, width, y + max(1, height // 36)), fill=(*palette["panel_alt"][:3], alpha))
    offset = (frame_index * 6) % max(width, 1)
    for x in range(-width, width * 2, max(36, width // 16)):
        draw.line((x + offset, height, x + offset + int(width * 0.28), 0), fill=palette["accent_soft"], width=2)
    image.alpha_composite(bg)


def _draw_corner_pattern(draw: ImageDraw.ImageDraw, width: int, height: int, palette: dict[str, tuple[int, int, int, int]], progress: float) -> None:
    alpha = int(120 * _clamp(progress, 0.0, 1.0))
    color = _with_alpha(palette["accent"], alpha)
    step = max(18, width // 80)
    for index in range(8):
        x0 = width - int(width * 0.16) + index * step
        y0 = int(height * 0.08) + index * step // 2
        draw.rounded_rectangle((x0, y0, x0 + step * 2, y0 + step // 2), radius=step // 4, fill=color)


def _draw_accent_rule(draw: ImageDraw.ImageDraw, x: int, y: int, length: int, palette: dict[str, tuple[int, int, int, int]]) -> None:
    draw.rounded_rectangle((x, y, x + max(length, 12), y + 6), radius=3, fill=palette["accent"])


def _draw_button(draw: ImageDraw.ImageDraw, text: str, xy: tuple[int, int], palette: dict[str, tuple[int, int, int, int]], canvas_height: int) -> None:
    font_size = int(canvas_height * 0.027)
    font = _font(font_size, bold=True)
    padding_x = int(canvas_height * 0.035)
    padding_y = int(canvas_height * 0.018)
    bbox = draw.textbbox((0, 0), text, font=font)
    w = bbox[2] - bbox[0] + padding_x * 2
    h = bbox[3] - bbox[1] + padding_y * 2
    x, y = xy
    _rounded_rect(draw, (x, y, x + w, y + h), radius=h // 2, fill=palette["accent"])
    draw.text((x + padding_x, y + padding_y - 2), text, font=font, fill=(7, 12, 16, 255))


def _draw_badge(draw: ImageDraw.ImageDraw, text: str, xy: tuple[int, int], palette: dict[str, tuple[int, int, int, int]], canvas_height: int) -> None:
    font_size = int(canvas_height * 0.019)
    font = _font(font_size, bold=True)
    padding_x = int(canvas_height * 0.018)
    padding_y = int(canvas_height * 0.009)
    bbox = draw.textbbox((0, 0), text, font=font)
    w = bbox[2] - bbox[0] + padding_x * 2
    h = bbox[3] - bbox[1] + padding_y * 2
    x, y = xy
    _rounded_rect(draw, (x, y, x + w, y + h), radius=h // 2, fill=palette["accent"])
    draw.text((x + padding_x, y + padding_y - 1), text.upper(), font=font, fill=(8, 12, 16, 255))


def _draw_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    xy: tuple[int, int],
    size: int,
    fill: tuple[int, int, int, int],
    *,
    bold: bool = False,
    max_width: int | None = None,
) -> None:
    if not text:
        return
    font = _font(max(int(size), 8), bold=bold)
    x, y = xy
    lines = _wrap_lines(draw, text, font, max_width) if max_width else [text]
    line_height = int(size * 1.16)
    for line in lines:
        draw.text((x, y), line, font=font, fill=fill)
        y += line_height


def _wrap_lines(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont | ImageFont.ImageFont, max_width: int | None) -> list[str]:
    if not max_width:
        return [text]
    words = str(text).split()
    if not words:
        return [text]
    if len(words) == 1 and draw.textbbox((0, 0), words[0], font=font)[2] > max_width:
        lines: list[str] = []
        current = ""
        for char in words[0]:
            candidate = current + char
            if current and draw.textbbox((0, 0), candidate, font=font)[2] > max_width:
                lines.append(current)
                current = char
            else:
                current = candidate
        if current:
            lines.append(current)
        return lines
    lines: list[str] = []
    current = words[0]
    for word in words[1:]:
        candidate = f"{current} {word}"
        if draw.textbbox((0, 0), candidate, font=font)[2] <= max_width:
            current = candidate
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


def _font(size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = [
        "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
        "/Library/Fonts/Arial Unicode.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf" if bold else "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
    ]
    for candidate in candidates:
        try:
            return ImageFont.truetype(candidate, size=size)
        except Exception:
            continue
    return ImageFont.load_default()


def _rounded_rect(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], *, radius: int, fill: tuple[int, int, int, int]) -> None:
    draw.rounded_rectangle(box, radius=max(int(radius), 0), fill=fill)


def _with_alpha(color: tuple[int, int, int, int], alpha: int) -> tuple[int, int, int, int]:
    return (color[0], color[1], color[2], max(0, min(255, int(alpha))))


def _ease_out_cubic(x: float) -> float:
    value = _clamp(x, 0.0, 1.0)
    return 1.0 - (1.0 - value) ** 3


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, float(value)))


def _blend_overlay(base: np.ndarray, overlay: np.ndarray, *, opacity: float, position: str, scale: float) -> np.ndarray:
    frame = base.copy()
    if overlay.ndim == 2:
        overlay = cv2.cvtColor(overlay, cv2.COLOR_GRAY2BGRA)
    if overlay.shape[2] == 3:
        alpha = np.full(overlay.shape[:2], int(255 * _clamp(opacity, 0.0, 1.0)), dtype=np.uint8)
        overlay_rgba = np.dstack([overlay, alpha])
    else:
        overlay_rgba = overlay.copy()
        overlay_rgba[:, :, 3] = (overlay_rgba[:, :, 3].astype(np.float32) * _clamp(opacity, 0.0, 1.0)).astype(np.uint8)
    ow = max(1, int(overlay_rgba.shape[1] * max(float(scale), 0.05)))
    oh = max(1, int(overlay_rgba.shape[0] * max(float(scale), 0.05)))
    overlay_rgba = cv2.resize(overlay_rgba, (ow, oh), interpolation=cv2.INTER_AREA)
    h, w = frame.shape[:2]
    x, y = _position_xy(position, w, h, ow, oh)
    x2 = min(w, x + ow)
    y2 = min(h, y + oh)
    if x2 <= x or y2 <= y:
        return frame
    patch = overlay_rgba[: y2 - y, : x2 - x]
    alpha = patch[:, :, 3:4].astype(np.float32) / 255.0
    frame[y:y2, x:x2] = (patch[:, :, :3].astype(np.float32) * alpha + frame[y:y2, x:x2].astype(np.float32) * (1 - alpha)).astype(np.uint8)
    return frame


def _position_xy(position: str, width: int, height: int, ow: int, oh: int) -> tuple[int, int]:
    value = str(position or "center").lower()
    x = (width - ow) // 2
    y = (height - oh) // 2
    margin = max(12, int(min(width, height) * 0.04))
    if "left" in value:
        x = margin
    if "right" in value:
        x = width - ow - margin
    if "top" in value:
        y = margin
    if "bottom" in value:
        y = height - oh - margin
    return max(0, x), max(0, y)


__all__ = [
    "AdGraphicsRenderResult",
    "render_ad_title_pack",
    "render_lower_third",
    "render_cta_card",
    "render_product_callout",
    "render_shimmer_sweep",
    "compose_overlay_on_video",
]
