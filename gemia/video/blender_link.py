"""LumeriLink primitives for Blender-backed spatial video effects."""
from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from gemia.errors import MediaProcessingError, UserInputError
from gemia.video.timeline_assets import IMAGE_EXTENSIONS, media_kind_for_path, probe_media


@dataclass(frozen=True)
class BlenderLinkRenderResult:
    output_path: str
    metadata_path: str
    renderer: str
    blender_path: str | None


_BLENDER_LINK_OPERATIONS: dict[str, dict[str, Any]] = {
    "spatial_scene": {
        "label": "Spatial scene",
        "description": "Place the source clip on a luminous 3D panel with grid depth and a gentle camera move.",
        "tags": ["space", "panel", "depth-grid", "parallax"],
        "default_args": {"style": "ice_blue", "intensity": 0.65, "camera_orbit_degrees": 18.0},
    },
    "parallax_orbit": {
        "label": "Parallax orbit",
        "description": "Use a stronger camera orbit for a spatial product-shot or object-stage feeling.",
        "tags": ["camera", "orbit", "parallax", "3d"],
        "default_args": {"style": "cinematic", "intensity": 0.76, "camera_orbit_degrees": 30.0},
    },
    "depth_grid": {
        "label": "Depth grid",
        "description": "Emphasize the Blender depth floor and perspective lines around the media plane.",
        "tags": ["depth", "grid", "floor", "scan"],
        "default_args": {"style": "ice_blue", "intensity": 0.82, "camera_orbit_degrees": 10.0},
    },
    "neon_hologram": {
        "label": "Neon hologram",
        "description": "Render the source as a brighter neon holographic panel in a volumetric-looking stage.",
        "tags": ["neon", "hologram", "volumetric", "glow"],
        "default_args": {"style": "neon", "intensity": 0.9, "camera_orbit_degrees": 22.0},
    },
}

_OPERATION_ALIASES = {
    "spatial": "spatial_scene",
    "space": "spatial_scene",
    "stage": "spatial_scene",
    "orbit": "parallax_orbit",
    "parallax": "parallax_orbit",
    "grid": "depth_grid",
    "depth": "depth_grid",
    "hologram": "neon_hologram",
    "neon": "neon_hologram",
    "volumetric": "neon_hologram",
}


def blender_link_capabilities() -> dict[str, Any]:
    """Return the backend Blender bridge contract and supported operations."""
    status = blender_link_status()
    operations = []
    for operation_id, spec in _BLENDER_LINK_OPERATIONS.items():
        operations.append(
            {
                "id": operation_id,
                "label": spec["label"],
                "description": spec["description"],
                "tags": list(spec["tags"]),
                "supported_inputs": ["video", "image"],
                "output_mime": "video/mp4",
                "default_args": dict(spec["default_args"]),
            }
        )
    return {
        "protocol": "lumerilink.blender.v1",
        "available": status["available"],
        "blender_path": status["blender_path"],
        "version": status["version"],
        "operations": operations,
        "execute_endpoint": "/blender-link/execute",
        "status_endpoint": "/blender-link/status",
        "env_vars": ["LUMERI_BLENDER_PATH", "GEMIA_BLENDER_PATH"],
        "fallback_renderer": "opencv_fallback",
    }


def render_blender_link_operation(
    input_path: str,
    output_path: str,
    *,
    operation: str = "spatial_scene",
    style: str | None = None,
    intensity: float | None = None,
    duration_sec: float | None = None,
    start_sec: float = 0.0,
    width: int = 1920,
    height: int = 1080,
    fps: int = 30,
    camera_orbit_degrees: float | None = None,
    preserve_audio: bool = True,
    prefer_blender: bool = True,
) -> str:
    """Execute a named LumeriLink operation through Blender or the fallback renderer."""
    operation_id, spec = _operation_spec(operation)
    defaults = spec["default_args"]
    return render_blender_spatial_scene(
        input_path,
        output_path,
        operation=operation_id,
        style=style or str(defaults["style"]),
        intensity=float(intensity if intensity is not None else defaults["intensity"]),
        duration_sec=duration_sec,
        start_sec=start_sec,
        width=width,
        height=height,
        fps=fps,
        camera_orbit_degrees=float(
            camera_orbit_degrees
            if camera_orbit_degrees is not None
            else defaults["camera_orbit_degrees"]
        ),
        preserve_audio=preserve_audio,
        prefer_blender=prefer_blender,
    )


def render_blender_spatial_scene(
    input_path: str,
    output_path: str,
    *,
    operation: str = "spatial_scene",
    style: str = "ice_blue",
    intensity: float = 0.65,
    duration_sec: float | None = None,
    start_sec: float = 0.0,
    width: int = 1920,
    height: int = 1080,
    fps: int = 30,
    camera_orbit_degrees: float = 18.0,
    preserve_audio: bool = True,
    prefer_blender: bool = True,
) -> str:
    """Render a spatial/parallax scene by linking the clip into Blender.

    The source media is placed as a luminous 3D panel in a simple Blender scene
    with depth grid, light rings, and an animated camera move. When Blender is
    unavailable the function writes a deterministic OpenCV spatial preview so
    the desktop workflow still produces an MP4.
    """
    operation_id, operation_spec = _operation_spec(operation)
    source = Path(input_path).expanduser()
    if not source.exists() or not source.is_file():
        raise UserInputError(f"Blender spatial input not found: {input_path}")
    if media_kind_for_path(source) == "audio":
        raise UserInputError("Blender spatial effects need a video or image source.")

    output = Path(output_path).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    style = _normalize_style(style)
    intensity = max(0.0, min(float(intensity), 1.0))
    fps = max(1, min(int(fps), 60))
    width = max(160, int(width))
    height = max(90, int(height))
    start_sec = max(float(start_sec or 0.0), 0.0)

    source_for_render = source
    work_dir = output.parent / f".lumerilink_blender_{uuid.uuid4().hex[:8]}"
    work_dir.mkdir(parents=True, exist_ok=True)
    try:
        meta = _probe_source(source)
        media_kind = str(meta.get("media_kind") or media_kind_for_path(source))
        input_duration = float(meta.get("duration") or 0.0)
        if media_kind == "image":
            render_duration = max(float(duration_sec or 3.0), 0.1)
        else:
            available = max(input_duration - start_sec, 0.1)
            render_duration = max(float(duration_sec or available), 0.1)
            render_duration = min(render_duration, available)
            if start_sec > 0.01 or (duration_sec is not None and render_duration < input_duration - 0.01):
                source_for_render = _trim_source_for_blender(
                    source,
                    work_dir / f"source_{uuid.uuid4().hex[:6]}.mp4",
                    start_sec=start_sec,
                    duration_sec=render_duration,
                )
                meta = _probe_source(source_for_render)
                input_duration = float(meta.get("duration") or render_duration)

        frame_count = max(1, int(round(render_duration * fps)))
        render_target = output
        mux_audio = (
            preserve_audio
            and media_kind == "video"
            and bool(meta.get("has_audio"))
            and output.suffix.lower() == ".mp4"
        )
        if mux_audio:
            render_target = work_dir / "spatial_silent.mp4"

        blender_path = _find_blender() if prefer_blender else None
        renderer = "opencv_fallback"
        if blender_path:
            spec = _build_blender_spec(
                input_path=source_for_render,
                output_path=render_target,
                frames_dir=work_dir / "blender_frames",
                media_kind=media_kind,
                metadata=meta,
                style=style,
                intensity=intensity,
                duration=render_duration,
                fps=fps,
                frame_count=frame_count,
                width=width,
                height=height,
                camera_orbit_degrees=camera_orbit_degrees,
                operation=operation_id,
                operation_label=operation_spec["label"],
            )
            script_path = work_dir / "lumerilink_blender_scene.py"
            script_path.write_text(_blender_script(spec), encoding="utf-8")
            renderer = _try_run_blender(
                blender_path,
                script_path,
                render_target,
                frames_dir=Path(spec["frames_dir"]),
                fps=fps,
                timeout=max(45, int(render_duration * 12)),
            ) or renderer

        if renderer == "opencv_fallback":
            _render_spatial_fallback(
                source_for_render,
                render_target,
                style=style,
                intensity=intensity,
                duration=render_duration,
                width=width,
                height=height,
                fps=fps,
                camera_orbit_degrees=camera_orbit_degrees,
                operation_label=str(operation_spec["label"]),
            )

        if mux_audio:
            _mux_audio(render_target, source_for_render, output, duration_sec=render_duration)

        _write_metadata(
            output,
            input_path=source,
            render_input=source_for_render,
            renderer=renderer,
            blender_path=blender_path,
            style=style,
            intensity=intensity,
            duration=render_duration,
            fps=fps,
            width=width,
            height=height,
            camera_orbit_degrees=camera_orbit_degrees,
            operation=operation_id,
            operation_label=str(operation_spec["label"]),
            metadata=meta,
        )
        return str(output)
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def blender_link_status() -> dict[str, Any]:
    """Return local Blender availability for desktop diagnostics."""
    blender = _find_blender()
    version = ""
    if blender:
        try:
            proc = subprocess.run([blender, "--version"], capture_output=True, text=True, timeout=6)
            version = (proc.stdout or proc.stderr).splitlines()[0].strip()
        except Exception:
            version = ""
    return {
        "available": bool(blender),
        "blender_path": blender,
        "version": version,
    }


def _find_blender() -> str | None:
    candidates = [
        os.environ.get("LUMERI_BLENDER_PATH", ""),
        os.environ.get("GEMIA_BLENDER_PATH", ""),
        shutil.which("blender") or "",
        "/Applications/Blender.app/Contents/MacOS/Blender",
    ]
    for item in candidates:
        if item and Path(item).exists():
            return str(Path(item))
    return None


def _probe_source(path: Path) -> dict[str, Any]:
    meta = probe_media(str(path))
    if meta.get("media_kind") == "image" and (not meta.get("width") or not meta.get("height")):
        from PIL import Image

        with Image.open(path) as image:
            meta["width"], meta["height"] = image.size
            meta["duration"] = 3.0
    return meta


def _trim_source_for_blender(source: Path, output: Path, *, start_sec: float, duration_sec: float) -> Path:
    cmd = [
        "ffmpeg", "-y",
        "-ss", f"{start_sec:.6f}",
        "-i", str(source),
        "-t", f"{duration_sec:.6f}",
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        str(output),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise MediaProcessingError("Could not prepare source clip for Blender.", detail=proc.stderr[-1200:])
    return output


def _build_blender_spec(**kwargs: Any) -> dict[str, Any]:
    meta = dict(kwargs.pop("metadata"))
    source_width = int(meta.get("width") or kwargs["width"])
    source_height = int(meta.get("height") or kwargs["height"])
    aspect = source_width / max(source_height, 1)
    panel_width = 5.2
    panel_height = panel_width / max(aspect, 0.1)
    if panel_height > 3.4:
        panel_height = 3.4
        panel_width = panel_height * aspect
    return {
        **kwargs,
        "input_path": str(Path(kwargs["input_path"]).resolve()),
        "output_path": str(Path(kwargs["output_path"]).resolve()),
        "frames_dir": str(Path(kwargs["frames_dir"]).resolve()),
        "panel_width": panel_width,
        "panel_height": panel_height,
        "source_width": source_width,
        "source_height": source_height,
    }


def _try_run_blender(
    blender_path: str,
    script_path: Path,
    output_path: Path,
    *,
    frames_dir: Path,
    fps: int,
    timeout: int,
) -> str | None:
    try:
        proc = subprocess.run(
            [blender_path, "-b", "--python", str(script_path)],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if proc.returncode != 0:
            return None
        frames = sorted(frames_dir.glob("frame_*.png"))
        if not frames:
            return None
        encode = subprocess.run(
            [
                "ffmpeg", "-y",
                "-framerate", str(fps),
                "-i", str(frames_dir / "frame_%04d.png"),
                "-c:v", "libx264",
                "-pix_fmt", "yuv420p",
                str(output_path),
            ],
            capture_output=True,
            text=True,
        )
        if encode.returncode != 0 or not output_path.exists() or output_path.stat().st_size <= 0:
            return None
        return "blender"
    except Exception:
        return None


def _blender_script(spec: dict[str, Any]) -> str:
    spec_json = json.dumps(spec, ensure_ascii=False)
    return f'''
import json
import math
from pathlib import Path

import bpy
from mathutils import Vector

SPEC = json.loads({json.dumps(spec_json)})

def look_at(obj, target):
    direction = Vector(target) - obj.location
    obj.rotation_euler = direction.to_track_quat('-Z', 'Y').to_euler()

def set_principled_input(node, names, value):
    for name in names:
        if name in node.inputs:
            node.inputs[name].default_value = value
            return

bpy.ops.object.select_all(action='SELECT')
bpy.ops.object.delete()

scene = bpy.context.scene
scene.frame_start = 1
scene.frame_end = int(SPEC["frame_count"])
scene.frame_set(1)
scene.render.fps = int(SPEC["fps"])
scene.render.resolution_x = int(SPEC["width"])
scene.render.resolution_y = int(SPEC["height"])
try:
    scene.render.engine = "BLENDER_EEVEE_NEXT"
except Exception:
    scene.render.engine = "BLENDER_EEVEE"
frames_dir = Path(SPEC["frames_dir"])
frames_dir.mkdir(parents=True, exist_ok=True)
scene.render.filepath = str(frames_dir / "frame_")
scene.render.image_settings.file_format = "PNG"
if hasattr(scene, "eevee"):
    scene.eevee.taa_render_samples = 32
scene.world = bpy.data.worlds.new("Lumeri Space World") if scene.world is None else scene.world
scene.world.color = (0.012, 0.016, 0.02)

palette = {{
    "ice_blue": ((0.38, 0.86, 1.0, 1.0), (0.015, 0.035, 0.055, 1.0)),
    "cinematic": ((0.95, 0.78, 0.46, 1.0), (0.025, 0.02, 0.018, 1.0)),
    "neon": ((0.72, 0.45, 1.0, 1.0), (0.02, 0.014, 0.04, 1.0)),
}}
accent, bg = palette.get(SPEC["style"], palette["ice_blue"])
intensity = float(SPEC["intensity"])

mat = bpy.data.materials.new("LumeriLink Source Media")
mat.use_nodes = True
nodes = mat.node_tree.nodes
bsdf = nodes.get("Principled BSDF")
tex = nodes.new("ShaderNodeTexImage")
img = bpy.data.images.load(SPEC["input_path"])
if SPEC["media_kind"] == "video":
    img.source = "MOVIE"
    tex.image_user.use_auto_refresh = True
    tex.image_user.frame_start = 1
    tex.image_user.frame_duration = int(SPEC["frame_count"])
tex.image = img
if bsdf:
    mat.node_tree.links.new(tex.outputs["Color"], bsdf.inputs["Base Color"])
    set_principled_input(bsdf, ("Emission Color", "Emission"), accent)
    set_principled_input(bsdf, ("Emission Strength",), 0.12 + intensity * 0.35)
    set_principled_input(bsdf, ("Roughness",), 0.42)

bpy.ops.mesh.primitive_plane_add(size=1.0, location=(0, 0, 2.05), rotation=(math.radians(90), 0, 0))
panel = bpy.context.object
panel.name = "LumeriLink media plane"
panel.scale = (float(SPEC["panel_width"]), float(SPEC["panel_height"]), 1.0)
panel.data.materials.append(mat)

frame_mat = bpy.data.materials.new("LumeriLink frame")
frame_mat.diffuse_color = accent
for name, loc, scale in [
    ("frame_top", (0, 0.03, 2.05 + SPEC["panel_height"] / 2 + 0.045), (SPEC["panel_width"] + 0.14, 0.05, 0.035)),
    ("frame_bottom", (0, 0.03, 2.05 - SPEC["panel_height"] / 2 - 0.045), (SPEC["panel_width"] + 0.14, 0.05, 0.035)),
    ("frame_left", (-SPEC["panel_width"] / 2 - 0.045, 0.03, 2.05), (0.035, 0.05, SPEC["panel_height"] + 0.14)),
    ("frame_right", (SPEC["panel_width"] / 2 + 0.045, 0.03, 2.05), (0.035, 0.05, SPEC["panel_height"] + 0.14)),
]:
    bpy.ops.mesh.primitive_cube_add(size=1.0, location=loc)
    obj = bpy.context.object
    obj.name = name
    obj.scale = scale
    obj.data.materials.append(frame_mat)

grid_mat = bpy.data.materials.new("LumeriLink grid")
grid_mat.diffuse_color = (accent[0], accent[1], accent[2], 0.42)
for i in range(-8, 9):
    curve = bpy.data.curves.new(f"grid_x_{{i}}", "CURVE")
    curve.dimensions = "3D"
    curve.resolution_u = 1
    curve.bevel_depth = 0.006
    spline = curve.splines.new("POLY")
    spline.points.add(1)
    x = i * 0.55
    spline.points[0].co = (x, -3.8, 0.0, 1)
    spline.points[1].co = (x, 2.6, 0.0, 1)
    obj = bpy.data.objects.new(curve.name, curve)
    bpy.context.collection.objects.link(obj)
    obj.data.materials.append(grid_mat)
for j in range(0, 11):
    curve = bpy.data.curves.new(f"grid_z_{{j}}", "CURVE")
    curve.dimensions = "3D"
    curve.resolution_u = 1
    curve.bevel_depth = 0.006
    spline = curve.splines.new("POLY")
    spline.points.add(1)
    y = -3.8 + j * 0.65
    spline.points[0].co = (-4.6, y, 0.0, 1)
    spline.points[1].co = (4.6, y, 0.0, 1)
    obj = bpy.data.objects.new(curve.name, curve)
    bpy.context.collection.objects.link(obj)
    obj.data.materials.append(grid_mat)

for index, radius in enumerate((2.0, 2.7, 3.4)):
    bpy.ops.mesh.primitive_torus_add(major_radius=radius, minor_radius=0.012, major_segments=128, minor_segments=8, location=(0, -0.08 * index, 2.05))
    ring = bpy.context.object
    ring.name = f"LumeriLink orbit ring {{index + 1}}"
    ring.rotation_euler = (math.radians(90), math.radians(18 * index), math.radians(7 * index))
    ring.data.materials.append(frame_mat)
    ring.keyframe_insert(data_path="rotation_euler", frame=1)
    ring.rotation_euler.z += math.radians(45 + 25 * index)
    ring.keyframe_insert(data_path="rotation_euler", frame=int(SPEC["frame_count"]))

bpy.ops.object.light_add(type="AREA", location=(0, -2.5, 5.0))
key = bpy.context.object
key.name = "LumeriLink key light"
key.data.energy = 420 + intensity * 580
key.data.size = 5.2
bpy.ops.object.light_add(type="POINT", location=(-2.5, -1.3, 2.6))
rim = bpy.context.object
rim.name = "LumeriLink rim light"
rim.data.energy = 100 + intensity * 240
rim.data.color = (accent[0], accent[1], accent[2])

bpy.ops.object.camera_add(location=(-1.15, -6.2, 2.65))
camera = bpy.context.object
camera.name = "LumeriLink camera"
scene.camera = camera
camera.data.lens = 32
orbit = math.radians(float(SPEC["camera_orbit_degrees"]))
for frame, x, z, lens in [
    (1, -math.sin(orbit) * 1.25, 2.62, 32),
    (max(1, int(SPEC["frame_count"])), math.sin(orbit) * 1.25, 2.82, 38),
]:
    scene.frame_set(frame)
    camera.location = (x, -6.2, z)
    camera.data.lens = lens
    look_at(camera, (0, 0, 1.85))
    camera.keyframe_insert(data_path="location", frame=frame)
    camera.keyframe_insert(data_path="rotation_euler", frame=frame)
    camera.data.keyframe_insert(data_path="lens", frame=frame)

bpy.ops.render.render(animation=True)
'''


def _render_spatial_fallback(
    source: Path,
    output: Path,
    *,
    style: str,
    intensity: float,
    duration: float,
    width: int,
    height: int,
    fps: int,
    camera_orbit_degrees: float,
    operation_label: str,
) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    writer = cv2.VideoWriter(str(output), cv2.VideoWriter_fourcc(*"mp4v"), fps, (width, height))
    if not writer.isOpened():
        raise MediaProcessingError(f"Could not open BlenderLink fallback writer: {output}")

    cap = None
    still = None
    kind = media_kind_for_path(source)
    if kind == "image":
        still = cv2.imread(str(source), cv2.IMREAD_COLOR)
        if still is None:
            raise MediaProcessingError(f"Could not read source image: {source}")
    else:
        cap = cv2.VideoCapture(str(source))
        if not cap.isOpened():
            raise MediaProcessingError(f"Could not read source video: {source}")

    total_frames = max(1, int(round(duration * fps)))
    accent = np.array(_bgr_accent(style), dtype=np.float32)
    try:
        for index in range(total_frames):
            if cap is not None:
                ok, frame = cap.read()
                if not ok:
                    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                    ok, frame = cap.read()
                if not ok:
                    break
            else:
                frame = still.copy()
            rendered = _fallback_frame(
                frame,
                frame_index=index,
                total_frames=total_frames,
                width=width,
                height=height,
                accent=accent,
                intensity=intensity,
                orbit_degrees=camera_orbit_degrees,
                operation_label=operation_label,
            )
            writer.write(rendered)
    finally:
        if cap is not None:
            cap.release()
        writer.release()

    if not output.exists() or output.stat().st_size <= 0:
        raise MediaProcessingError("BlenderLink fallback produced no video frames.")


def _fallback_frame(
    frame: np.ndarray,
    *,
    frame_index: int,
    total_frames: int,
    width: int,
    height: int,
    accent: np.ndarray,
    intensity: float,
    orbit_degrees: float,
    operation_label: str,
) -> np.ndarray:
    t = frame_index / max(total_frames - 1, 1)
    bg = np.zeros((height, width, 3), dtype=np.uint8)
    for y in range(height):
        blend = y / max(height - 1, 1)
        color = np.array([16, 14, 12], dtype=np.float32) * (1.0 - blend) + accent * (0.14 + 0.18 * blend)
        bg[y, :, :] = np.clip(color, 0, 255)
    horizon = int(height * 0.64)
    for i in range(12):
        y = horizon + int((height - horizon) * (i / 11) ** 1.7)
        cv2.line(bg, (0, y), (width, y), accent.tolist(), 1, cv2.LINE_AA)
    for i in range(-10, 11):
        x0 = width // 2 + i * 42
        cv2.line(bg, (width // 2, horizon), (x0, height), accent.tolist(), 1, cv2.LINE_AA)

    src_h, src_w = frame.shape[:2]
    aspect = src_w / max(src_h, 1)
    panel_w = int(width * 0.58)
    panel_h = int(panel_w / max(aspect, 0.1))
    if panel_h > int(height * 0.58):
        panel_h = int(height * 0.58)
        panel_w = int(panel_h * aspect)
    resized = cv2.resize(frame, (panel_w, panel_h), interpolation=cv2.INTER_AREA)
    sway = math.sin((t - 0.5) * math.radians(orbit_degrees) * 2.0)
    skew = int(panel_w * 0.08 * sway)
    cx = width // 2 + int(sway * width * 0.045)
    cy = int(height * 0.42)
    src_pts = np.float32([[0, 0], [panel_w, 0], [panel_w, panel_h], [0, panel_h]])
    dst_pts = np.float32([
        [cx - panel_w // 2 + skew, cy - panel_h // 2],
        [cx + panel_w // 2 + skew, cy - panel_h // 2 + int(abs(skew) * 0.18)],
        [cx + panel_w // 2 - skew, cy + panel_h // 2],
        [cx - panel_w // 2 - skew, cy + panel_h // 2 + int(abs(skew) * 0.18)],
    ])
    matrix = cv2.getPerspectiveTransform(src_pts, dst_pts)
    warped = cv2.warpPerspective(resized, matrix, (width, height))
    mask = cv2.warpPerspective(np.full((panel_h, panel_w), 255, dtype=np.uint8), matrix, (width, height))
    glow = cv2.GaussianBlur(mask, (0, 0), 18)
    glow_layer = np.dstack([glow] * 3).astype(np.float32) / 255.0 * accent.reshape(1, 1, 3) * (0.28 + intensity * 0.45)
    bg = np.clip(bg.astype(np.float32) + glow_layer, 0, 255).astype(np.uint8)
    alpha = (mask.astype(np.float32) / 255.0)[..., None]
    bg = np.clip(bg.astype(np.float32) * (1.0 - alpha) + warped.astype(np.float32) * alpha, 0, 255).astype(np.uint8)
    cv2.polylines(bg, [dst_pts.astype(np.int32)], True, accent.tolist(), max(1, int(2 + intensity * 2)), cv2.LINE_AA)
    cv2.putText(bg, f"LumeriLink / Blender / {operation_label}", (28, height - 34), cv2.FONT_HERSHEY_SIMPLEX, 0.62, accent.tolist(), 1, cv2.LINE_AA)
    return bg


def _mux_audio(video_path: Path, audio_source: Path, output: Path, *, duration_sec: float) -> None:
    tmp = output.with_name(f"{output.stem}_mux_{uuid.uuid4().hex[:6]}.mp4")
    proc = subprocess.run(
        [
            "ffmpeg", "-y",
            "-i", str(video_path),
            "-i", str(audio_source),
            "-t", f"{duration_sec:.6f}",
            "-map", "0:v:0",
            "-map", "1:a:0?",
            "-c:v", "copy",
            "-c:a", "aac",
            "-shortest",
            str(tmp),
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        shutil.copy2(video_path, output)
        tmp.unlink(missing_ok=True)
        return
    tmp.replace(output)


def _write_metadata(
    output: Path,
    *,
    input_path: Path,
    render_input: Path,
    renderer: str,
    blender_path: str | None,
    style: str,
    intensity: float,
    duration: float,
    fps: int,
    width: int,
    height: int,
    camera_orbit_degrees: float,
    operation: str,
    operation_label: str,
    metadata: dict[str, Any],
) -> None:
    payload = {
        "effect": "lumerilink_blender_spatial_scene",
        "protocol": "lumerilink.blender.v1",
        "operation": operation,
        "operation_label": operation_label,
        "renderer": renderer,
        "blender_path": blender_path,
        "input_path": str(input_path),
        "render_input": str(render_input),
        "output_path": str(output),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "style": style,
        "intensity": intensity,
        "duration": duration,
        "fps": fps,
        "width": width,
        "height": height,
        "camera_orbit_degrees": camera_orbit_degrees,
        "source_metadata": metadata,
    }
    output.with_suffix(".blenderlink.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _operation_spec(operation: str) -> tuple[str, dict[str, Any]]:
    normalized = str(operation or "spatial_scene").strip().lower().replace("-", "_").replace(" ", "_")
    normalized = _OPERATION_ALIASES.get(normalized, normalized)
    if normalized not in _BLENDER_LINK_OPERATIONS:
        available = ", ".join(sorted(_BLENDER_LINK_OPERATIONS))
        raise UserInputError(f"Unsupported BlenderLink operation: {operation}. Available operations: {available}")
    return normalized, dict(_BLENDER_LINK_OPERATIONS[normalized])


def _normalize_style(style: str) -> str:
    normalized = str(style or "ice_blue").strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {"ice": "ice_blue", "blue": "ice_blue", "space": "ice_blue", "cyber": "neon"}
    normalized = aliases.get(normalized, normalized)
    return normalized if normalized in {"ice_blue", "cinematic", "neon"} else "ice_blue"


def _bgr_accent(style: str) -> tuple[int, int, int]:
    if style == "cinematic":
        return (76, 178, 245)
    if style == "neon":
        return (255, 110, 184)
    return (255, 220, 96)
