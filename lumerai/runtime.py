from __future__ import annotations

import copy
import hashlib
import inspect
import json
import linecache
import os
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

from gemia.project_model import IMAGE_DURATION, normalize_project
from gemia.video.effects import video_brightness_contrast, video_info, video_sepia, video_trim


@dataclass
class _RuntimeContext:
    project_state: dict[str, Any]
    output_dir: Path
    project_root: Path
    workspace_dir: Path
    session_id: str
    ai_model: str
    script_hash: str
    script_path: str
    patch_output: TextIO


_CONTEXT: _RuntimeContext | None = None


def configure_runtime(
    *,
    project_state: dict[str, Any] | None = None,
    output_dir: str | Path | None = None,
    project_root: str | Path | None = None,
    workspace_dir: str | Path | None = None,
    session_id: str | None = None,
    ai_model: str | None = None,
    script_hash: str | None = None,
    script_path: str | None = None,
    patch_output: TextIO | None = None,
) -> None:
    """Configure the script runtime.

    Host-side tests may call this directly. The sandbox child normally calls it
    from environment variables before executing an AI-authored script.
    """
    global _CONTEXT
    if project_state is None:
        project_state = _project_state_from_env()
    out = Path(output_dir or os.environ.get("LUMERAI_OUTPUT_DIR") or Path.cwd() / "outputs").expanduser()
    root = Path(project_root or os.environ.get("LUMERAI_PROJECT_ROOT") or Path.cwd()).expanduser()
    session = session_id or os.environ.get("LUMERAI_SESSION_ID") or "session_unknown"
    workspace = Path(
        workspace_dir
        or os.environ.get("LUMERAI_WORKSPACE_DIR")
        or root / "workspaces" / session
    ).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    workspace.mkdir(parents=True, exist_ok=True)
    _CONTEXT = _RuntimeContext(
        project_state=normalize_project(project_state or {}),
        output_dir=out.resolve(),
        project_root=root.resolve(),
        workspace_dir=workspace.resolve(),
        session_id=session,
        ai_model=ai_model or os.environ.get("LUMERAI_AI_MODEL") or "unknown",
        script_hash=script_hash or os.environ.get("LUMERAI_SCRIPT_HASH") or "",
        script_path=script_path or os.environ.get("LUMERAI_SCRIPT_PATH") or "",
        patch_output=patch_output or sys.stdout,
    )


def timeline_state() -> dict[str, Any]:
    """Return a copy of the current normalized project timeline state."""
    return copy.deepcopy(_context().project_state)


def clip_load(path_or_id: str) -> dict[str, Any]:
    """Load a clip by timeline clip id, asset id/name, or an allowed file path."""
    ctx = _context()
    project = ctx.project_state
    assets = {str(asset.get("id")): asset for asset in project.get("assets") or [] if isinstance(asset, dict)}
    query = str(path_or_id)

    for clip in (project.get("timeline") or {}).get("clips") or []:
        if not isinstance(clip, dict):
            continue
        asset = assets.get(str(clip.get("asset_id"))) or {}
        if query in {
            str(clip.get("id") or ""),
            str(clip.get("asset_id") or ""),
            str(clip.get("name") or ""),
            str(asset.get("name") or ""),
            str(asset.get("source_path") or ""),
        }:
            return _clip_ref_from_project_clip(clip, asset)

    for asset in assets.values():
        if query in {
            str(asset.get("id") or ""),
            str(asset.get("asset_id") or ""),
            str(asset.get("name") or ""),
            str(asset.get("source_path") or ""),
        }:
            return _clip_ref_from_asset(asset)

    path = Path(query).expanduser()
    if not path.exists():
        raise FileNotFoundError(f"clip_load could not find clip or asset: {path_or_id}")
    resolved = path.resolve()
    if not _is_allowed_path(resolved, ctx):
        raise PermissionError(f"clip_load path is outside the project boundary: {resolved}")
    info = _safe_video_info(str(resolved))
    asset_id = _asset_id(str(resolved))
    return {
        "id": f"clip_{uuid.uuid4().hex[:8]}",
        "asset_id": asset_id,
        "path": str(resolved),
        "name": resolved.name,
        "media_kind": "video",
        "duration": max(float(info.get("duration") or 0.0), 0.1),
        "source_in": 0.0,
        "source_out": max(float(info.get("duration") or 0.0), 0.1),
        "effects": _default_effects(),
    }


def clip_trim(clip: dict[str, Any], *, start: float = 0.0, end: float | None = None) -> dict[str, Any]:
    """Return a logical trimmed clip without mutating the timeline."""
    loaded = _ensure_clip(clip)
    start_sec = max(float(start), 0.0)
    original_end = float(loaded.get("source_out") or loaded.get("duration") or start_sec + 0.1)
    end_sec = original_end if end is None else float(end)
    if end_sec <= start_sec:
        raise ValueError("clip_trim requires end > start")
    trimmed = copy.deepcopy(loaded)
    trimmed["source_in"] = start_sec
    trimmed["source_out"] = end_sec
    trimmed["duration"] = round(end_sec - start_sec, 6)
    trimmed["id"] = f"clip_{uuid.uuid4().hex[:8]}"
    return trimmed


def clip_color_grade(
    clip: dict[str, Any],
    *,
    preset: str = "warm",
    adjustments: dict[str, Any] | None = None,
    strength: float = 0.8,
) -> dict[str, Any]:
    """Materialize a simple color-graded clip using existing gemia effects."""
    loaded = _ensure_clip(clip)
    path = Path(str(loaded.get("path") or "")).expanduser()
    if not path.exists():
        raise FileNotFoundError(f"clip_color_grade source does not exist: {path}")
    ctx = _context()
    working_input = str(path.resolve())
    source_in = float(loaded.get("source_in") or 0.0)
    source_out = float(loaded.get("source_out") or loaded.get("duration") or 0.0)
    if source_out > source_in and (source_in > 0.001 or source_out < float(_safe_video_info(working_input).get("duration") or source_out) - 0.001):
        trimmed_path = ctx.output_dir / _derived_name(path, "trim")
        video_trim(working_input, str(trimmed_path), start_sec=source_in, end_sec=source_out, stream_copy=False)
        working_input = str(trimmed_path)

    output_path = ctx.output_dir / _derived_name(path, f"grade-{_safe_slug(preset)}")
    adjustments = adjustments or {}
    preset_key = str(preset or "").strip().lower()
    if preset_key in {"warm", "cinematic_warm", "apple_warm", "sepia"}:
        video_sepia(working_input, str(output_path))
    else:
        brightness = float(adjustments.get("brightness", adjustments.get("exposure", 0.0)) or 0.0)
        contrast = float(adjustments.get("contrast", 1.0) or 1.0)
        video_brightness_contrast(working_input, str(output_path), brightness=brightness, contrast=contrast)
    info = _safe_video_info(str(output_path))
    graded = copy.deepcopy(loaded)
    graded.update(
        {
            "id": f"clip_{uuid.uuid4().hex[:8]}",
            "asset_id": _asset_id(str(output_path)),
            "path": str(output_path),
            "name": output_path.name,
            "duration": max(float(info.get("duration") or loaded.get("duration") or IMAGE_DURATION), 0.1),
            "source_in": 0.0,
            "source_out": max(float(info.get("duration") or loaded.get("duration") or IMAGE_DURATION), 0.1),
            "effects": {**(loaded.get("effects") or _default_effects()), "color_grade": {"preset": preset, "strength": strength, "adjustments": adjustments}},
        }
    )
    return graded


def hyperframes_render(
    stage_html: str,
    *,
    css: str = "",
    duration: float = 3.0,
    width: int | None = None,
    height: int | None = None,
    fps: float | None = None,
    name: str = "hyperframes",
) -> dict[str, Any]:
    """Render a local HyperFrames blank-canvas clip through the host adapter."""
    ctx = _context()
    from gemia.hyperframes_adapter import HyperFramesContext, render_hyperframes_clip

    return render_hyperframes_clip(
        stage_html,
        css=css,
        duration=duration,
        width=width,
        height=height,
        fps=fps,
        name=name,
        context=HyperFramesContext(
            project_state=ctx.project_state,
            workspace_dir=ctx.workspace_dir,
            session_id=ctx.session_id,
            ai_model=ctx.ai_model,
            script_hash=ctx.script_hash,
            script_path=ctx.script_path,
        ),
    )


def timeline_insert(clip: dict[str, Any], *, at: float | None = None, track_id: str = "V1") -> dict[str, Any]:
    """Emit an insert_clip TimelinePatch operation."""
    loaded = _ensure_clip(clip)
    insert_at = _timeline_end() if at is None else max(float(at), 0.0)
    data = _patch_data_for_clip(loaded, start=insert_at, track_id=track_id)
    op = {"op": "insert_clip", "target": "timeline", "data": data}
    return _emit_op(op)


def timeline_replace(clip_id: str, clip: dict[str, Any]) -> dict[str, Any]:
    """Emit a replace_clip TimelinePatch operation."""
    loaded = copy.deepcopy(_ensure_clip(clip))
    _link_replacement_parent(loaded, str(clip_id))
    existing = _find_timeline_clip(str(clip_id))
    start = float(existing.get("start") or 0.0) if existing else 0.0
    track_id = str(existing.get("track_id") or "V1") if existing else "V1"
    data = _patch_data_for_clip(loaded, start=start, track_id=track_id)
    op = {"op": "replace_clip", "target": "timeline", "clip_id": str(clip_id), "data": data}
    return _emit_op(op)


def _context() -> _RuntimeContext:
    if _CONTEXT is None:
        configure_runtime()
    assert _CONTEXT is not None
    return _CONTEXT


def _project_state_from_env() -> dict[str, Any]:
    state_path = os.environ.get("LUMERAI_PROJECT_STATE_PATH")
    if not state_path:
        return {}
    path = Path(state_path)
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _emit_op(op: dict[str, Any]) -> dict[str, Any]:
    provenance = _provenance()
    op["provenance"] = provenance
    data = op.get("data")
    if isinstance(data, dict) and isinstance(data.get("clip"), dict):
        data["clip"]["provenance"] = provenance
    patch = {"version": 1, "ops": [op]}
    ctx = _context()
    print(json.dumps(patch, ensure_ascii=False, sort_keys=True), file=ctx.patch_output, flush=True)
    return patch


def _provenance() -> dict[str, Any]:
    ctx = _context()
    frame = _caller_frame()
    line_no = int(frame.lineno) if frame else 0
    filename = frame.filename if frame else ctx.script_path
    snippet = linecache.getline(filename, line_no).strip() if filename and line_no else ""
    return {
        "session_id": ctx.session_id,
        "script_hash": ctx.script_hash,
        "script_line": line_no,
        "script_snippet": snippet,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "ai_model": ctx.ai_model,
    }


def _caller_frame() -> inspect.FrameInfo | None:
    this_file = Path(__file__).resolve()
    for frame in inspect.stack()[2:]:
        try:
            if Path(frame.filename).resolve() != this_file:
                return frame
        except Exception:
            return frame
    return None


def _patch_data_for_clip(clip: dict[str, Any], *, start: float, track_id: str) -> dict[str, Any]:
    asset_id = str(clip.get("asset_id") or _asset_id(str(clip.get("path") or clip.get("name") or "")))
    source_path = str(clip.get("path") or "")
    name = str(clip.get("name") or Path(source_path).name or "media")
    media_kind = str(clip.get("media_kind") or "video")
    duration = max(float(clip.get("duration") or IMAGE_DURATION), 0.1)
    metadata = copy.deepcopy(clip.get("metadata")) if isinstance(clip.get("metadata"), dict) else {}
    asset = {
        "id": asset_id,
        "asset_id": asset_id,
        "name": name,
        "media_kind": media_kind,
        "mime_type": "video/mp4" if media_kind == "video" else "",
        "source_path": source_path,
        "duration": duration,
        "metadata": {"generated_by": "lumerai_script", **metadata},
    }
    timeline_clip = {
        "id": str(clip.get("id") or f"clip_{uuid.uuid4().hex[:8]}"),
        "asset_id": asset_id,
        "track_id": track_id,
        "name": name,
        "media_kind": media_kind,
        "start": round(start, 6),
        "duration": round(duration, 6),
        "source_in": float(clip.get("source_in") or 0.0),
        "source_out": float(clip.get("source_out") or duration),
        "enabled": True,
        "effects": clip.get("effects") if isinstance(clip.get("effects"), dict) else _default_effects(),
    }
    return {"asset": asset, "clip": timeline_clip}


def _clip_ref_from_asset(asset: dict[str, Any]) -> dict[str, Any]:
    path = str(asset.get("source_path") or "")
    duration = float(asset.get("duration") or _safe_video_info(path).get("duration") or 0.1)
    metadata = copy.deepcopy(asset.get("metadata")) if isinstance(asset.get("metadata"), dict) else {}
    return {
        "id": f"clip_{uuid.uuid4().hex[:8]}",
        "asset_id": str(asset.get("id") or asset.get("asset_id") or _asset_id(path)),
        "path": path,
        "name": str(asset.get("name") or Path(path).name or "media"),
        "media_kind": str(asset.get("media_kind") or "video"),
        "duration": duration,
        "source_in": 0.0,
        "source_out": duration,
        "effects": _default_effects(),
        "metadata": metadata,
    }


def _clip_ref_from_project_clip(clip: dict[str, Any], asset: dict[str, Any]) -> dict[str, Any]:
    ref = _clip_ref_from_asset(asset)
    ref.update(
        {
            "id": str(clip.get("id") or ref["id"]),
            "duration": float(clip.get("duration") or ref["duration"]),
            "source_in": float(clip.get("source_in") or 0.0),
            "source_out": float(clip.get("source_out") or clip.get("duration") or ref["duration"]),
            "effects": clip.get("effects") if isinstance(clip.get("effects"), dict) else _default_effects(),
        }
    )
    return ref


def _ensure_clip(clip: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(clip, dict):
        raise TypeError("lumerai clip API expects a clip dict returned by clip_load/clip_trim")
    if not clip.get("path") and not clip.get("asset_id"):
        raise ValueError("clip is missing path/asset_id")
    return clip


def _link_replacement_parent(clip: dict[str, Any], clip_id: str) -> None:
    metadata = clip.get("metadata")
    if not isinstance(metadata, dict):
        return
    hf = metadata.get("hyperframes")
    if not isinstance(hf, dict):
        return
    if not hf.get("parent_clip_id"):
        hf["parent_clip_id"] = clip_id


def _find_timeline_clip(clip_id: str) -> dict[str, Any] | None:
    project = _context().project_state
    for clip in (project.get("timeline") or {}).get("clips") or []:
        if isinstance(clip, dict) and str(clip.get("id")) == clip_id:
            return clip
    return None


def _timeline_end() -> float:
    project = _context().project_state
    end = 0.0
    for clip in (project.get("timeline") or {}).get("clips") or []:
        if isinstance(clip, dict):
            end = max(end, float(clip.get("start") or 0.0) + float(clip.get("duration") or 0.0))
    return end


def _is_allowed_path(path: Path, ctx: _RuntimeContext) -> bool:
    allowed_roots = {ctx.project_root, ctx.output_dir}
    for asset in ctx.project_state.get("assets") or []:
        if isinstance(asset, dict) and asset.get("source_path"):
            allowed_roots.add(Path(str(asset["source_path"])).expanduser().resolve().parent)
    for root in allowed_roots:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def _safe_video_info(path: str) -> dict[str, Any]:
    if not path:
        return {}
    try:
        if Path(path).exists():
            return video_info(path)
    except Exception:
        pass
    return {}


def _derived_name(path: Path, suffix: str) -> str:
    digest = hashlib.sha256(f"{path}:{suffix}:{uuid.uuid4().hex}".encode("utf-8")).hexdigest()[:10]
    return f"{path.stem}.{suffix}.{digest}.mp4"


def _safe_slug(value: str) -> str:
    return "".join(ch if ch.isalnum() else "-" for ch in value.lower()).strip("-")[:40] or "default"


def _asset_id(value: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
    return f"asset_{digest}"


def _default_effects() -> dict[str, Any]:
    return {"rotation": 0, "mirrored": False, "muted": False, "audioDetached": False, "speed": 1}
