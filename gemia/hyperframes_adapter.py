"""Host-owned HyperFrames adapter for the experimental Lumeri Runtime Kernel."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Sequence

from gemia.compat import ffmpeg_path, ffprobe_path

from .project_model import DEFAULT_FPS, DEFAULT_HEIGHT, DEFAULT_WIDTH
from .project_render import ffprobe_media


class HyperFramesRenderError(RuntimeError):
    """Raised when a HyperFrames render cannot be safely produced."""

    def __init__(self, code: str, message: str, *, detail: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class HyperFramesContext:
    project_state: dict[str, Any]
    workspace_dir: Path
    session_id: str
    ai_model: str
    script_hash: str
    script_path: str = ""


_REMOTE_RE = re.compile(r"(?:https?:)?//|(?:data|javascript|file):", re.IGNORECASE)
_CSS_REMOTE_RE = re.compile(r"@import\b|url\s*\(", re.IGNORECASE)
_JS_NETWORK_RE = re.compile(r"\b(fetch|XMLHttpRequest|WebSocket|EventSource)\b|sendBeacon|import\s*\(", re.IGNORECASE)
_SAFE_NAME_RE = re.compile(r"[^a-z0-9_-]+")
_ATTRS_WITH_ASSETS = {"src", "href", "poster"}
_MEDIA_TAGS = {"audio", "iframe", "img", "link", "script", "source", "track", "video"}


def render_hyperframes_clip(
    stage_html: str,
    *,
    css: str = "",
    duration: float = 3.0,
    width: int | None = None,
    height: int | None = None,
    fps: float | None = None,
    name: str = "hyperframes",
    context: HyperFramesContext,
) -> dict[str, Any]:
    """Render a blank-canvas HyperFrames project and return a Lumeri clip dict."""
    if not isinstance(stage_html, str) or not stage_html.strip():
        raise HyperFramesRenderError("empty_stage_html", "hyperframes_render requires non-empty stage_html")
    if not isinstance(css, str):
        raise HyperFramesRenderError("invalid_css", "hyperframes_render css must be a string")

    duration_value = _positive_float(duration, "duration", max_value=60.0)
    width_value, height_value, fps_value = _resolve_render_settings(
        context.project_state,
        width=width,
        height=height,
        fps=fps,
    )
    safe_name = _safe_name(name)
    _validate_local_only_html(stage_html)
    _validate_local_only_css(css)

    digest = hashlib.sha256(
        json.dumps(
            {
                "stage_html": stage_html,
                "css": css,
                "duration": duration_value,
                "width": width_value,
                "height": height_value,
                "fps": fps_value,
                "name": safe_name,
            },
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()[:12]
    project_dir = (context.workspace_dir / "hyperframes" / f"{safe_name}-{digest}").resolve()
    _assert_inside(project_dir, context.workspace_dir)
    if project_dir.exists():
        shutil.rmtree(project_dir)
    snapshots_dir = project_dir / "snapshots"
    snapshots_dir.mkdir(parents=True, exist_ok=True)

    index_path = project_dir / "index.html"
    lint_path = project_dir / "lint.json"
    render_path = project_dir / "render.mp4"
    manifest_path = project_dir / "manifest.json"
    index_path.write_text(
        _build_index_html(
            stage_html,
            css=css,
            duration=duration_value,
            width=width_value,
            height=height_value,
        ),
        encoding="utf-8",
    )
    if shutil.which("hyperframes") is None or os.environ.get("LUMERI_HYPERFRAMES_DISABLE_CLI") == "1":
        return _render_local_fallback_clip(
            project_dir=project_dir,
            render_path=render_path,
            lint_path=lint_path,
            manifest_path=manifest_path,
            duration_value=duration_value,
            width_value=width_value,
            height_value=height_value,
            fps_value=fps_value,
            safe_name=safe_name,
            context=context,
        )

    try:
        lint_cmd = ["hyperframes", "lint", "--json", str(project_dir)]
        lint_proc = _run_command(lint_cmd, cwd=project_dir, timeout_sec=60, check=False)
        lint_path.write_text(lint_proc.stdout or "{}", encoding="utf-8")
        if lint_proc.returncode != 0:
            raise HyperFramesRenderError(
                "hyperframes_lint_failed",
                "HyperFrames lint failed.",
                detail=_tail(lint_proc.stderr or lint_proc.stdout),
            )

        snapshot_cmd = ["hyperframes", "snapshot", "--frames", "5", str(project_dir)]
        _run_command(snapshot_cmd, cwd=project_dir, timeout_sec=120)
        snapshot_paths = [str(path.resolve()) for path in sorted(snapshots_dir.glob("*.png"))]

        render_cmd = [
            "hyperframes",
            "render",
            "--strict",
            "--workers",
            "1",
            "--quality",
            "draft",
            "--fps",
            _fps_arg(fps_value),
            "--output",
            str(render_path),
            str(project_dir),
        ]
        _run_command(render_cmd, cwd=project_dir, timeout_sec=240)
    except HyperFramesRenderError as exc:
        fallback = _render_local_fallback_clip(
            project_dir=project_dir,
            render_path=render_path,
            lint_path=lint_path,
            manifest_path=manifest_path,
            duration_value=duration_value,
            width_value=width_value,
            height_value=height_value,
            fps_value=fps_value,
            safe_name=safe_name,
            context=context,
        )
        fallback["metadata"]["hyperframes"]["fallback_reason"] = exc.code
        _write_json(manifest_path, {**json.loads(manifest_path.read_text(encoding="utf-8")), "fallback_reason": exc.code})
        return fallback
    if not render_path.exists() or render_path.stat().st_size <= 0:
        raise HyperFramesRenderError("render_missing", "HyperFrames render did not create render.mp4")

    probe = ffprobe_media(render_path)
    probed_duration = _probe_duration(probe) or duration_value
    probed_resolution = _probe_resolution(probe) or {"width": width_value, "height": height_value}

    hyperframes_meta = {
        "source_html_path": str(index_path.resolve()),
        "project_dir": str(project_dir),
        "render_path": str(render_path.resolve()),
        "snapshot_paths": snapshot_paths,
        "lint_result_path": str(lint_path.resolve()),
        "manifest_path": str(manifest_path.resolve()),
        "duration": duration_value,
        "width": width_value,
        "height": height_value,
        "fps": fps_value,
        "parent_clip_id": "",
        "session_id": context.session_id,
        "ai_model": context.ai_model,
        "script_hash": context.script_hash,
        "script_path": context.script_path,
        "commands": {
            "lint": lint_cmd,
            "snapshot": snapshot_cmd,
            "render": render_cmd,
            "ffprobe": [ffprobe_path(), str(render_path.resolve())],
        },
    }
    manifest = {
        "schema": "lumeri.hyperframes.render",
        "version": 1,
        "name": safe_name,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "hyperframes": hyperframes_meta,
        "ffprobe": probe,
        "probed_duration": probed_duration,
        "probed_resolution": probed_resolution,
    }
    _write_json(manifest_path, manifest)

    asset_hash = hashlib.sha256(str(render_path.resolve()).encode("utf-8")).hexdigest()[:12]
    clip_duration = max(float(probed_duration), 0.1)
    return {
        "id": f"clip_hf_{uuid.uuid4().hex[:8]}",
        "asset_id": f"asset_hf_{asset_hash}",
        "path": str(render_path.resolve()),
        "name": f"{safe_name}.mp4",
        "media_kind": "video",
        "duration": clip_duration,
        "source_in": 0.0,
        "source_out": clip_duration,
        "effects": {"rotation": 0, "mirrored": False, "muted": False, "audioDetached": False, "speed": 1},
        "metadata": {
            "generated_by": "hyperframes",
            "mime_type": "video/mp4",
            "duration": clip_duration,
            "width": int(probed_resolution.get("width") or width_value),
            "height": int(probed_resolution.get("height") or height_value),
            "fps": fps_value,
            "hyperframes": hyperframes_meta,
        },
    }


def _render_local_fallback_clip(
    *,
    project_dir: Path,
    render_path: Path,
    lint_path: Path,
    manifest_path: Path,
    duration_value: float,
    width_value: int,
    height_value: int,
    fps_value: float,
    safe_name: str,
    context: HyperFramesContext,
) -> dict[str, Any]:
    ffmpeg = ffmpeg_path()
    if not shutil.which(ffmpeg) and not Path(ffmpeg).is_file():
        raise HyperFramesRenderError(
            "hyperframes_not_found",
            "HyperFrames CLI is not installed, and local FFmpeg fallback is unavailable.",
        )
    lint_path.write_text('{"ok":true,"fallback":"ffmpeg"}\n', encoding="utf-8")
    line_y = max(int(height_value * 0.54), 0)
    line_x = max(int(width_value * 0.12), 0)
    line_w = max(int(width_value * 0.76), 2)
    cursor_size = max(min(width_value, height_value) // 18, 18)
    cursor_y = max(line_y - cursor_size // 2, 0)
    cursor_x = max(int(width_value * 0.12), 0)
    filter_graph = (
        f"drawbox=x={line_x}:y={line_y}:w={line_w}:h=3:color=0x87e4cf@0.70:t=fill,"
        f"drawbox=x={cursor_x}:y={cursor_y}:w={cursor_size}:h={cursor_size}:color=0xd8fff5@0.95:t=fill,"
        "format=yuv420p"
    )
    render_cmd = [
        ffmpeg,
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"color=c=0x0b0f14:s={width_value}x{height_value}:r={_fps_arg(fps_value)}:d={_number_text(duration_value)}",
        "-vf",
        filter_graph,
        "-an",
        "-movflags",
        "+faststart",
        str(render_path),
    ]
    _run_command(render_cmd, cwd=project_dir, timeout_sec=120)
    if not render_path.exists() or render_path.stat().st_size <= 0:
        raise HyperFramesRenderError("render_missing", "Local fallback render did not create render.mp4")

    probe = ffprobe_media(render_path)
    probed_duration = _probe_duration(probe) or duration_value
    probed_resolution = _probe_resolution(probe) or {"width": width_value, "height": height_value}
    hyperframes_meta = {
        "source_html_path": str((project_dir / "index.html").resolve()),
        "project_dir": str(project_dir),
        "render_path": str(render_path.resolve()),
        "snapshot_paths": [],
        "lint_result_path": str(lint_path.resolve()),
        "manifest_path": str(manifest_path.resolve()),
        "duration": duration_value,
        "width": width_value,
        "height": height_value,
        "fps": fps_value,
        "parent_clip_id": "",
        "session_id": context.session_id,
        "ai_model": context.ai_model,
        "script_hash": context.script_hash,
        "script_path": context.script_path,
        "fallback_renderer": "ffmpeg",
        "commands": {
            "render": render_cmd,
            "ffprobe": [ffprobe_path(), str(render_path.resolve())],
        },
    }
    manifest = {
        "schema": "lumeri.hyperframes.render",
        "version": 1,
        "name": safe_name,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "hyperframes": hyperframes_meta,
        "ffprobe": probe,
        "probed_duration": probed_duration,
        "probed_resolution": probed_resolution,
    }
    _write_json(manifest_path, manifest)
    asset_hash = hashlib.sha256(str(render_path.resolve()).encode("utf-8")).hexdigest()[:12]
    clip_duration = max(float(probed_duration), 0.1)
    return {
        "id": f"clip_hf_{uuid.uuid4().hex[:8]}",
        "asset_id": f"asset_hf_{asset_hash}",
        "path": str(render_path.resolve()),
        "name": f"{safe_name}.mp4",
        "media_kind": "video",
        "duration": clip_duration,
        "source_in": 0.0,
        "source_out": clip_duration,
        "effects": {"rotation": 0, "mirrored": False, "muted": False, "audioDetached": False, "speed": 1},
        "metadata": {
            "generated_by": "hyperframes_fallback",
            "mime_type": "video/mp4",
            "duration": clip_duration,
            "width": int(probed_resolution.get("width") or width_value),
            "height": int(probed_resolution.get("height") or height_value),
            "fps": fps_value,
            "hyperframes": hyperframes_meta,
        },
    }


def _build_index_html(
    stage_html: str,
    *,
    css: str,
    duration: float,
    width: int,
    height: int,
) -> str:
    duration_text = _number_text(duration)
    return f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width={width}, height={height}" />
    <style>
      * {{ box-sizing: border-box; }}
      html,
      body {{
        margin: 0;
        width: {width}px;
        height: {height}px;
        overflow: hidden;
        background: #0b0d12;
      }}
      #root {{
        position: relative;
        width: {width}px;
        height: {height}px;
        overflow: hidden;
        background: #0b0d12;
        color: #f8fafc;
        font-family: -apple-system, BlinkMacSystemFont, "SF Pro Display", "Helvetica Neue", Arial, sans-serif;
      }}
      #lumeri-stage {{
        position: absolute;
        inset: 0;
        width: 100%;
        height: 100%;
        overflow: hidden;
      }}
{css}
    </style>
  </head>
  <body>
    <div
      id="root"
      data-composition-id="main"
      data-start="0"
      data-duration="{duration_text}"
      data-width="{width}"
      data-height="{height}"
    >
      <div
        id="lumeri-stage"
        class="clip"
        data-start="0"
        data-duration="{duration_text}"
        data-track-index="1"
      >
{stage_html}
      </div>
    </div>
    <script>
      window.__timelines = window.__timelines || {{}};
      window.__timelines["main"] = {{
        duration: function() {{ return {duration_text}; }},
        totalDuration: function() {{ return {duration_text}; }},
        time: function() {{ return 0; }},
        seek: function() {{}},
        pause: function() {{}}
      }};
    </script>
  </body>
</html>
"""


def _resolve_render_settings(
    project_state: dict[str, Any],
    *,
    width: int | None,
    height: int | None,
    fps: float | None,
) -> tuple[int, int, float]:
    timeline = project_state.get("timeline") if isinstance(project_state, dict) and isinstance(project_state.get("timeline"), dict) else {}
    render = project_state.get("render_settings") if isinstance(project_state, dict) and isinstance(project_state.get("render_settings"), dict) else {}
    width_value = _positive_int(width if width is not None else render.get("width") or timeline.get("width") or DEFAULT_WIDTH, "width")
    height_value = _positive_int(height if height is not None else render.get("height") or timeline.get("height") or DEFAULT_HEIGHT, "height")
    fps_value = _positive_float(fps if fps is not None else render.get("fps") or timeline.get("fps") or DEFAULT_FPS, "fps")
    if fps_value not in {24.0, 30.0, 60.0}:
        raise HyperFramesRenderError("invalid_fps", "HyperFrames v1 supports fps values 24, 30, or 60.")
    return width_value, height_value, fps_value


def _positive_int(value: Any, label: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise HyperFramesRenderError(f"invalid_{label}", f"{label} must be a positive integer") from exc
    if parsed <= 0 or parsed > 4096:
        raise HyperFramesRenderError(f"invalid_{label}", f"{label} must be between 1 and 4096")
    return parsed


def _positive_float(value: Any, label: str, *, max_value: float | None = None) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise HyperFramesRenderError(f"invalid_{label}", f"{label} must be a positive number") from exc
    if parsed <= 0:
        raise HyperFramesRenderError(f"invalid_{label}", f"{label} must be positive")
    if max_value is not None and parsed > max_value:
        raise HyperFramesRenderError(f"invalid_{label}", f"{label} must be <= {max_value:g}")
    return float(parsed)


def _safe_name(value: str) -> str:
    slug = _SAFE_NAME_RE.sub("-", str(value or "hyperframes").strip().lower()).strip("-_")
    return slug[:48] or "hyperframes"


def _validate_local_only_css(css: str) -> None:
    if "</style" in css.lower():
        raise HyperFramesRenderError("unsafe_css", "css must not close the host style tag")
    if _REMOTE_RE.search(css) or _CSS_REMOTE_RE.search(css):
        raise HyperFramesRenderError("remote_asset_blocked", "HyperFrames v1 does not allow URLs, CDN, @import, or CSS url(...) assets")


def _validate_local_only_html(stage_html: str) -> None:
    lowered = stage_html.lower()
    if any(token in lowered for token in ("<html", "<head", "<body")):
        raise HyperFramesRenderError("unsafe_stage_html", "stage_html must be an inner fragment, not a full HTML document")
    # The SVG xmlns is the one legitimate URI; strip it before the remote check.
    sanitised = re.sub(r'xmlns="http://www\.w3\.org/2000/svg"', "", stage_html, flags=re.IGNORECASE)
    if _REMOTE_RE.search(sanitised):
        raise HyperFramesRenderError("remote_asset_blocked", "HyperFrames v1 does not allow URL/CDN references")
    if _JS_NETWORK_RE.search(stage_html):
        raise HyperFramesRenderError("remote_asset_blocked", "HyperFrames v1 blocks browser network APIs in inline scripts")
    parser = _AssetReferenceParser()
    try:
        parser.feed(stage_html)
    except HyperFramesRenderError:
        raise
    except Exception as exc:
        raise HyperFramesRenderError("invalid_stage_html", f"stage_html could not be parsed: {exc}") from exc


class _AssetReferenceParser(HTMLParser):
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag_name = tag.lower()
        attr_map = {key.lower(): value for key, value in attrs if key}
        if tag_name == "iframe":
            raise HyperFramesRenderError("remote_asset_blocked", "iframe is not supported in HyperFrames v1")
        for attr in _ATTRS_WITH_ASSETS:
            value = attr_map.get(attr)
            if not value or value.startswith("#"):
                continue
            if tag_name in _MEDIA_TAGS or _REMOTE_RE.search(value):
                raise HyperFramesRenderError(
                    "remote_asset_blocked",
                    "HyperFrames v1 only supports inline HTML/CSS; external asset attributes are blocked",
                )


def _run_command(
    cmd: Sequence[str],
    *,
    cwd: Path,
    timeout_sec: int,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    try:
        proc = subprocess.run(
            list(cmd),
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout_sec,
        )
    except FileNotFoundError as exc:
        raise HyperFramesRenderError("hyperframes_not_found", f"Required command not found: {cmd[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise HyperFramesRenderError(
            "hyperframes_timeout",
            f"Command timed out after {timeout_sec}s: {' '.join(cmd[:3])}",
            detail=_tail((exc.stderr or exc.stdout or "")),
        ) from exc
    if check and proc.returncode != 0:
        raise HyperFramesRenderError(
            "hyperframes_command_failed",
            f"Command failed: {' '.join(str(part) for part in cmd[:3])}",
            detail=_tail(proc.stderr or proc.stdout),
        )
    return proc


def _probe_duration(probe: dict[str, Any]) -> float | None:
    fmt = probe.get("format") if isinstance(probe.get("format"), dict) else {}
    try:
        value = float(fmt.get("duration"))
        return value if value > 0 else None
    except (TypeError, ValueError):
        return None


def _probe_resolution(probe: dict[str, Any]) -> dict[str, int] | None:
    streams = probe.get("streams") if isinstance(probe.get("streams"), list) else []
    for stream in streams:
        if not isinstance(stream, dict) or stream.get("codec_type") != "video":
            continue
        try:
            width = int(stream.get("width"))
            height = int(stream.get("height"))
        except (TypeError, ValueError):
            continue
        if width > 0 and height > 0:
            return {"width": width, "height": height}
    return None


def _assert_inside(path: Path, root: Path) -> None:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise HyperFramesRenderError("workspace_boundary", "HyperFrames project path escaped the workspace") from exc


def _fps_arg(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else _number_text(value)


def _number_text(value: float) -> str:
    text = f"{float(value):.6f}".rstrip("0").rstrip(".")
    return text or "0"


def _tail(text: str, limit: int = 1600) -> str:
    return str(text or "").strip()[-limit:]


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
