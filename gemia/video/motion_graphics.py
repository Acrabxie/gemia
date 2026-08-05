"""Motion-graphics primitives with optional Manim rendering.

The planner-facing functions here are template based. They can use Manim when
it is installed, but always fall back to a deterministic OpenCV/Pillow renderer
so Gemia can keep producing usable clips in a fresh local setup.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import textwrap
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont


@dataclass(frozen=True)
class MotionGraphicsRenderResult:
    output_path: str
    metadata_path: str
    renderer: str


def render_mg_title_card(
    input_path: str,
    output_path: str,
    *,
    title: str = "Lumeri",
    subtitle: str = "",
    duration: float = 3.0,
    style: str = "ice",
    width: int = 1920,
    height: int = 1080,
    fps: int = 30,
    prefer_manim: bool = True,
) -> str:
    """Render a clean MG title card as a standalone video clip."""
    output = _resolve_output(output_path)
    spec = _base_spec(
        "gemia_mg_title_card",
        input_path=input_path,
        output=output,
        duration=duration,
        style=style,
        width=width,
        height=height,
        fps=fps,
        payload={"title": title, "subtitle": subtitle},
    )
    renderer = "opencv_fallback"
    if prefer_manim:
        renderer = _try_render_manim("title", output, spec) or renderer
    if renderer == "opencv_fallback":
        _render_title_card_fallback(output, spec)
    _write_mg_metadata(output, spec, renderer=renderer)
    return str(output)


def render_mg_formula_reveal(
    input_path: str,
    output_path: str,
    *,
    formula: str = "E = mc^2",
    caption: str = "",
    duration: float = 4.0,
    style: str = "ice",
    width: int = 1920,
    height: int = 1080,
    fps: int = 30,
    prefer_manim: bool = True,
) -> str:
    """Render a formula/equation reveal clip for explainer-style MG scenes."""
    output = _resolve_output(output_path)
    spec = _base_spec(
        "gemia_mg_formula_reveal",
        input_path=input_path,
        output=output,
        duration=duration,
        style=style,
        width=width,
        height=height,
        fps=fps,
        payload={"formula": formula, "caption": caption},
    )
    renderer = "opencv_fallback"
    if prefer_manim:
        renderer = _try_render_manim("formula", output, spec) or renderer
    if renderer == "opencv_fallback":
        _render_formula_fallback(output, spec)
    _write_mg_metadata(output, spec, renderer=renderer)
    return str(output)


def render_mg_process_diagram(
    input_path: str,
    output_path: str,
    *,
    steps: list[str] | None = None,
    title: str = "Process",
    duration: float = 5.0,
    style: str = "ice",
    width: int = 1920,
    height: int = 1080,
    fps: int = 30,
    prefer_manim: bool = True,
) -> str:
    """Render an animated process diagram with sequential step reveals."""
    output = _resolve_output(output_path)
    cleaned_steps = [str(item).strip() for item in (steps or []) if str(item).strip()]
    if not cleaned_steps:
        cleaned_steps = ["Import", "Analyze", "Generate", "Render"]
    spec = _base_spec(
        "gemia_mg_process_diagram",
        input_path=input_path,
        output=output,
        duration=duration,
        style=style,
        width=width,
        height=height,
        fps=fps,
        payload={"title": title, "steps": cleaned_steps[:8]},
    )
    renderer = "opencv_fallback"
    if prefer_manim:
        renderer = _try_render_manim("process", output, spec) or renderer
    if renderer == "opencv_fallback":
        _render_process_fallback(output, spec)
    _write_mg_metadata(output, spec, renderer=renderer)
    return str(output)


def _try_render_manim(template: str, output: Path, spec: dict[str, Any]) -> str | None:
    manim = shutil.which("manim")
    if not manim:
        return None
    work_dir = output.parent / f".gemia_manim_{uuid.uuid4().hex[:8]}"
    media_dir = work_dir / "media"
    script_path = work_dir / "gemia_mg_scene.py"
    work_dir.mkdir(parents=True, exist_ok=True)
    try:
        script_path.write_text(_manim_script(template, spec), encoding="utf-8")
        proc = subprocess.run(
            [
                manim,
                "-q",
                "l",
                "--media_dir",
                str(media_dir),
                "--fps",
                str(spec["fps"]),
                "--resolution",
                f"{spec['height']},{spec['width']}",
                str(script_path),
                "GemiaMGScene",
            ],
            capture_output=True,
            text=True,
            timeout=max(30, int(float(spec["duration"]) * 10)),
        )
        if proc.returncode != 0:
            return None
        candidates = sorted(media_dir.rglob("*.mp4"), key=lambda item: item.stat().st_mtime, reverse=True)
        if not candidates:
            return None
        shutil.copy2(candidates[0], output)
        return "manim"
    except Exception:
        return None
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def _manim_script(template: str, spec: dict[str, Any]) -> str:
    palette = _palette(spec["style"])
    payload = spec["payload"]
    width = int(spec["width"])
    height = int(spec["height"])
    duration = max(float(spec["duration"]), 0.5)
    payload_json = json.dumps(payload, ensure_ascii=False)
    palette_json = json.dumps(palette, ensure_ascii=False)
    template_json = json.dumps(template)
    return f"""
from manim import *
import json

config.pixel_width = {width}
config.pixel_height = {height}
config.frame_rate = {int(spec["fps"])}

PAYLOAD = json.loads({payload_json!r})
PALETTE = json.loads({palette_json!r})
TEMPLATE = json.loads({template_json!r})
DURATION = {duration!r}

class GemiaMGScene(Scene):
    def construct(self):
        self.camera.background_color = PALETTE["bg"]
        if TEMPLATE == "title":
            title = Text(PAYLOAD.get("title", "Lumeri"), font_size=72, color=PALETTE["fg"], weight=BOLD)
            subtitle = Text(PAYLOAD.get("subtitle", ""), font_size=32, color=PALETTE["muted"])
            subtitle.next_to(title, DOWN, buff=0.35)
            underline = Line(LEFT * 2.8, RIGHT * 2.8, color=PALETTE["accent"], stroke_width=6)
            underline.next_to(subtitle if PAYLOAD.get("subtitle") else title, DOWN, buff=0.45)
            group = VGroup(title, subtitle, underline)
            self.play(FadeIn(title, shift=UP * 0.25), run_time=0.7)
            if PAYLOAD.get("subtitle"):
                self.play(FadeIn(subtitle, shift=UP * 0.15), GrowFromCenter(underline), run_time=0.7)
            else:
                self.play(GrowFromCenter(underline), run_time=0.5)
            self.wait(max(DURATION - 1.8, 0.2))
            self.play(FadeOut(group, shift=UP * 0.15), run_time=0.5)
        elif TEMPLATE == "formula":
            formula_text = PAYLOAD.get("formula", "E = mc^2")
            try:
                formula = MathTex(formula_text, font_size=78, color=PALETTE["fg"])
            except Exception:
                formula = Text(formula_text, font_size=62, color=PALETTE["fg"])
            caption = Text(PAYLOAD.get("caption", ""), font_size=28, color=PALETTE["muted"])
            caption.next_to(formula, DOWN, buff=0.45)
            box = SurroundingRectangle(formula, corner_radius=0.18, color=PALETTE["accent"], buff=0.35)
            self.play(Create(box), Write(formula), run_time=1.2)
            if PAYLOAD.get("caption"):
                self.play(FadeIn(caption, shift=UP * 0.12), run_time=0.6)
            self.wait(max(DURATION - 2.1, 0.2))
            self.play(FadeOut(VGroup(box, formula, caption)), run_time=0.5)
        else:
            title = Text(PAYLOAD.get("title", "Process"), font_size=46, color=PALETTE["fg"], weight=BOLD)
            title.to_edge(UP, buff=0.7)
            self.play(FadeIn(title, shift=UP * 0.2), run_time=0.5)
            steps = PAYLOAD.get("steps", [])
            groups = VGroup()
            for index, label in enumerate(steps):
                rect = RoundedRectangle(width=2.15, height=0.72, corner_radius=0.15, color=PALETTE["accent"], stroke_width=3)
                fill = rect.copy().set_fill(PALETTE["surface"], opacity=0.88).set_stroke(opacity=0)
                text = Text(str(label), font_size=23, color=PALETTE["fg"])
                g = VGroup(fill, rect, text)
                groups.add(g)
            groups.arrange(RIGHT, buff=0.45).move_to(ORIGIN)
            arrows = VGroup()
            for index in range(max(len(groups) - 1, 0)):
                arrows.add(Arrow(groups[index].get_right(), groups[index + 1].get_left(), buff=0.12, color=PALETTE["muted"], stroke_width=3))
            for index, g in enumerate(groups):
                self.play(FadeIn(g, shift=UP * 0.15), run_time=0.35)
                if index < len(arrows):
                    self.play(Create(arrows[index]), run_time=0.2)
            self.wait(max(DURATION - 1.2 - len(groups) * 0.45, 0.2))
            self.play(FadeOut(VGroup(title, groups, arrows)), run_time=0.5)
"""


def _render_title_card_fallback(output: Path, spec: dict[str, Any]) -> None:
    palette = _palette(spec["style"])
    payload = spec["payload"]
    _write_video(output, spec, lambda draw, frame, progress: _draw_title(draw, frame, progress, payload, palette))


def _render_formula_fallback(output: Path, spec: dict[str, Any]) -> None:
    palette = _palette(spec["style"])
    payload = spec["payload"]
    _write_video(output, spec, lambda draw, frame, progress: _draw_formula(draw, frame, progress, payload, palette))


def _render_process_fallback(output: Path, spec: dict[str, Any]) -> None:
    palette = _palette(spec["style"])
    payload = spec["payload"]
    _write_video(output, spec, lambda draw, frame, progress: _draw_process(draw, frame, progress, payload, palette))


def _write_video(output: Path, spec: dict[str, Any], draw_frame: Any) -> None:
    width = int(spec["width"])
    height = int(spec["height"])
    fps = max(int(spec["fps"]), 1)
    duration = max(float(spec["duration"]), 0.2)
    total_frames = max(int(round(duration * fps)), 1)
    writer = cv2.VideoWriter(str(output), cv2.VideoWriter_fourcc(*"mp4v"), fps, (width, height))
    if not writer.isOpened():
        raise RuntimeError(f"Could not open MG writer: {output}")
    try:
        for frame_index in range(total_frames):
            progress = frame_index / max(total_frames - 1, 1)
            image = Image.new("RGB", (width, height), _palette(spec["style"])["bg"])
            draw = ImageDraw.Draw(image)
            draw_frame(draw, frame_index, progress)
            frame = cv2.cvtColor(np.asarray(image), cv2.COLOR_RGB2BGR)
            writer.write(frame)
    finally:
        writer.release()
    if output.suffix.lower() == ".mp4" and shutil.which("ffmpeg") is not None:
        from gemia.video.layers import _transcode_browser_mp4

        _transcode_browser_mp4(output, output)


def _draw_title(draw: ImageDraw.ImageDraw, frame: int, progress: float, payload: dict[str, Any], palette: dict[str, str]) -> None:
    width, height = draw.im.size
    title = str(payload.get("title") or "Lumeri")
    subtitle = str(payload.get("subtitle") or "")
    alpha = _ease_in_out(min(progress / 0.28, 1.0)) * _ease_in_out(min((1.0 - progress) / 0.16, 1.0))
    y_shift = int((1.0 - alpha) * 26)
    title_font = _fit_font(title, width * 0.78, 82, 30, bold=True)
    subtitle_font = _fit_font(subtitle, width * 0.7, 34, 18, bold=False)
    title_y = int(height * 0.44) + y_shift
    _center_text(draw, title, title_font, (width // 2, title_y), _mix(palette["muted"], palette["fg"], alpha))
    if subtitle:
        _center_text(draw, subtitle, subtitle_font, (width // 2, int(height * 0.52) + y_shift), _mix(palette["muted"], palette["muted"], alpha))
    line_width = int(width * 0.22 * _ease_in_out(min(max(progress - 0.12, 0.0) / 0.22, 1.0)))
    y = int(height * 0.59)
    draw.rounded_rectangle(
        (width // 2 - line_width, y, width // 2 + line_width, y + max(4, height // 170)),
        radius=height // 200,
        fill=palette["accent"],
    )
    _draw_corner_ticks(draw, width, height, palette, progress)


def _draw_formula(draw: ImageDraw.ImageDraw, frame: int, progress: float, payload: dict[str, Any], palette: dict[str, str]) -> None:
    width, height = draw.im.size
    formula = str(payload.get("formula") or "E = mc^2")
    caption = str(payload.get("caption") or "")
    reveal = _ease_in_out(min(progress / 0.38, 1.0))
    card_w = int(width * 0.66)
    card_h = int(height * 0.28)
    card = (width // 2 - card_w // 2, height // 2 - card_h // 2, width // 2 + card_w // 2, height // 2 + card_h // 2)
    draw.rounded_rectangle(card, radius=height // 40, fill=palette["surface"], outline=palette["accent"], width=max(2, height // 260))
    scan_x = int(card[0] + (card_w * ((progress * 1.4) % 1.0)))
    draw.rectangle((scan_x - 4, card[1] + 12, scan_x + 4, card[3] - 12), fill=palette["accent"])
    font = _fit_font(formula, card_w * 0.82, 74, 24, bold=True)
    visible_chars = max(1, int(len(formula) * reveal))
    _center_text(draw, formula[:visible_chars], font, (width // 2, height // 2 - height // 40), palette["fg"])
    if caption:
        caption_font = _fit_font(caption, card_w * 0.8, 30, 16, bold=False)
        _center_text(draw, caption, caption_font, (width // 2, height // 2 + int(card_h * 0.24)), palette["muted"])


def _draw_process(draw: ImageDraw.ImageDraw, frame: int, progress: float, payload: dict[str, Any], palette: dict[str, str]) -> None:
    width, height = draw.im.size
    title = str(payload.get("title") or "Process")
    steps = [str(item) for item in payload.get("steps", [])][:8]
    _center_text(draw, title, _fit_font(title, width * 0.7, 46, 22, bold=True), (width // 2, int(height * 0.18)), palette["fg"])
    if not steps:
        return
    gap = int(width * 0.02)
    box_w = min(int(width * 0.2), max(int((width * 0.82 - gap * (len(steps) - 1)) / len(steps)), int(width * 0.11)))
    box_h = int(height * 0.13)
    total_w = box_w * len(steps) + gap * (len(steps) - 1)
    start_x = width // 2 - total_w // 2
    y = int(height * 0.48)
    per_step = 1.0 / max(len(steps), 1)
    font = _font(max(15, min(28, int(box_h * 0.25))), bold=True)
    for index, label in enumerate(steps):
        local = _ease_in_out(min(max((progress - index * per_step * 0.68) / (per_step * 0.7), 0.0), 1.0))
        x = start_x + index * (box_w + gap)
        slide = int((1.0 - local) * 38)
        rect = (x, y + slide, x + box_w, y + box_h + slide)
        draw.rounded_rectangle(rect, radius=max(10, box_h // 6), fill=palette["surface"], outline=palette["accent"], width=max(2, height // 360))
        _multiline_center(draw, label, font, rect, palette["fg"])
        if index < len(steps) - 1:
            line_start = (x + box_w + 6, y + box_h // 2 + slide)
            line_end_x = x + box_w + gap - 6
            line_end = (int(line_start[0] + (line_end_x - line_start[0]) * local), line_start[1])
            draw.line((line_start, line_end), fill=palette["muted"], width=max(2, height // 260))
            if local > 0.92:
                draw.polygon([(line_end_x, line_start[1]), (line_end_x - 10, line_start[1] - 7), (line_end_x - 10, line_start[1] + 7)], fill=palette["muted"])


def _base_spec(
    effect: str,
    *,
    input_path: str,
    output: Path,
    duration: float,
    style: str,
    width: int,
    height: int,
    fps: int,
    payload: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "effect": effect,
        "input_path": str(input_path or ""),
        "output_path": str(output),
        "duration": max(float(duration), 0.2),
        "style": style,
        "width": _even(max(int(width), 64)),
        "height": _even(max(int(height), 64)),
        "fps": max(int(fps), 1),
        "payload": payload,
    }


def _resolve_output(output_path: str) -> Path:
    output = Path(output_path).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    return output


def _write_mg_metadata(output: Path, spec: dict[str, Any], *, renderer: str) -> Path:
    metadata_path = output.with_suffix(".mg.json")
    metadata = {
        **spec,
        "renderer": renderer,
        "manim_available": shutil.which("manim") is not None,
        "rendered_at": datetime.now(timezone.utc).isoformat(),
    }
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return metadata_path


def _palette(style: str) -> dict[str, str]:
    key = (style or "ice").lower()
    palettes = {
        "ice": {"bg": "#070A0D", "surface": "#121820", "fg": "#EEF7FA", "muted": "#A9B7C2", "accent": "#8EE7FF"},
        "graphite": {"bg": "#090909", "surface": "#171717", "fg": "#F1F1F1", "muted": "#A5A5A5", "accent": "#D7DEE5"},
        "lumeri": {"bg": "#080A0D", "surface": "#101317", "fg": "#EEF3F7", "muted": "#B2BEC7", "accent": "#5ED7FF"},
        "warm": {"bg": "#10100E", "surface": "#1B1813", "fg": "#FFF9EE", "muted": "#D7C7AE", "accent": "#FFD166"},
    }
    return palettes.get(key, palettes["ice"])


def _font(size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = [
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf" if bold else "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/Library/Fonts/Arial Unicode.ttf",
    ]
    for path in candidates:
        if path and Path(path).exists():
            try:
                return ImageFont.truetype(path, max(int(size), 8))
            except Exception:
                continue
    return ImageFont.load_default()


def _fit_font(text: str, max_width: float, max_size: int, min_size: int, *, bold: bool) -> ImageFont.ImageFont:
    text = text or " "
    probe = Image.new("RGB", (16, 16))
    draw = ImageDraw.Draw(probe)
    for size in range(max_size, min_size - 1, -2):
        font = _font(size, bold=bold)
        box = draw.textbbox((0, 0), text, font=font)
        if box[2] - box[0] <= max_width:
            return font
    return _font(min_size, bold=bold)


def _center_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, center: tuple[int, int], fill: str) -> None:
    box = draw.textbbox((0, 0), text, font=font)
    x = center[0] - (box[2] - box[0]) / 2
    y = center[1] - (box[3] - box[1]) / 2
    draw.text((x, y), text, font=font, fill=fill)


def _multiline_center(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, rect: tuple[int, int, int, int], fill: str) -> None:
    max_width = rect[2] - rect[0] - 18
    lines = _wrap_text(draw, text, font, max_width)
    line_boxes = [draw.textbbox((0, 0), line, font=font) for line in lines]
    heights = [box[3] - box[1] for box in line_boxes]
    total_h = sum(heights) + max(len(lines) - 1, 0) * 4
    y = rect[1] + (rect[3] - rect[1] - total_h) / 2
    for line, box, h in zip(lines, line_boxes, heights):
        x = rect[0] + (rect[2] - rect[0] - (box[2] - box[0])) / 2
        draw.text((x, y), line, font=font, fill=fill)
        y += h + 4


def _wrap_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, max_width: float) -> list[str]:
    if draw.textbbox((0, 0), text, font=font)[2] <= max_width:
        return [text]
    if " " in text:
        lines: list[str] = []
        for raw in textwrap.wrap(text, width=max(6, int(max_width // 18))):
            lines.append(raw)
        return lines[:3]
    lines = []
    current = ""
    for char in text:
        candidate = current + char
        if current and draw.textbbox((0, 0), candidate, font=font)[2] > max_width:
            lines.append(current)
            current = char
        else:
            current = candidate
        if len(lines) >= 2:
            break
    if current:
        lines.append(current)
    return lines[:3] or [text[:1]]


def _draw_corner_ticks(draw: ImageDraw.ImageDraw, width: int, height: int, palette: dict[str, str], progress: float) -> None:
    length = int(min(width, height) * 0.08 * _ease_in_out(min(progress / 0.5, 1.0)))
    margin = int(min(width, height) * 0.08)
    color = palette["surface"]
    stroke = max(2, height // 320)
    for sx, sy in ((margin, margin), (width - margin, margin), (margin, height - margin), (width - margin, height - margin)):
        x2 = sx + length if sx < width / 2 else sx - length
        y2 = sy + length if sy < height / 2 else sy - length
        draw.line((sx, sy, x2, sy), fill=color, width=stroke)
        draw.line((sx, sy, sx, y2), fill=color, width=stroke)


def _mix(a: str, b: str, t: float) -> str:
    ca = _hex_to_rgb(a)
    cb = _hex_to_rgb(b)
    return "#" + "".join(f"{int(ca[i] + (cb[i] - ca[i]) * t):02x}" for i in range(3))


def _hex_to_rgb(value: str) -> tuple[int, int, int]:
    text = value.strip().lstrip("#")
    return int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)


def _ease_in_out(t: float) -> float:
    t = min(max(float(t), 0.0), 1.0)
    return t * t * (3.0 - 2.0 * t)


def _even(value: int) -> int:
    return value if value % 2 == 0 else value - 1


__all__ = [
    "MotionGraphicsRenderResult",
    "render_mg_title_card",
    "render_mg_formula_reveal",
    "render_mg_process_diagram",
]
