"""Video summary helpers backed by Gemini when available."""
from __future__ import annotations

import json
import mimetypes
import textwrap
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_SUMMARY_PATH = Path("project_summary.json")


def video_summarize(video_path: str) -> dict[str, Any]:
    """Return a concise content summary for one video.

    The function prefers Gemia's native Gemini video-understanding adapter, but
    always returns a useful metadata envelope when credentials, ffprobe, network,
    or the source video itself are unavailable.
    """
    path = Path(video_path).expanduser()
    result = _fallback_summary(path)
    if not path.exists() or not path.is_file():
        result["status"] = "missing"
        result["error"] = f"video not found: {path}"
        return result

    try:
        from gemia.ai.gemini_adapter import GeminiAdapter

        adapter = GeminiAdapter()
        context = _run_gemini_summary(adapter, path)
    except Exception as exc:
        result["status"] = "fallback"
        result["error"] = str(exc)
        return result

    return _merge_model_context(result, context)


def batch_summarize(video_list: list[str]) -> list[dict[str, Any]]:
    """Summarize videos and persist the batch to ``project_summary.json``."""
    results = [video_summarize(path) for path in video_list]
    PROJECT_SUMMARY_PATH.write_text(
        json.dumps(results, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return results


def _run_gemini_summary(adapter: Any, path: Path) -> dict[str, Any]:
    if not getattr(adapter, "can_read_video", lambda _path: False)(str(path)):
        raise RuntimeError("Gemini adapter cannot read this video path.")
    context = _await_if_needed(
        adapter.generate_video_context_json(
            _summary_system_prompt(),
            {
                "request": "Summarize this source video for an editing project.",
                "input_path": str(path),
                "summary_shape": {
                    "duration": 8.3,
                    "summary": "one or two sentences",
                    "mood": "short mood label",
                    "key_frame": "best visual moment or timestamp",
                    "suggested_use": "where this clip fits in the edit",
                    "keep": True,
                },
            },
            tag="video-summary",
        )
    )
    if not isinstance(context, dict):
        raise RuntimeError("Gemini summary response was not a JSON object.")
    return context


def _await_if_needed(value: Any) -> Any:
    if not hasattr(value, "__await__"):
        return value

    import asyncio

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(value)
    raise RuntimeError(f"video_summarize cannot run inside an active event loop: {loop!r}")


def _fallback_summary(path: Path) -> dict[str, Any]:
    exists = path.exists() and path.is_file()
    metadata = _metadata_for_path(path) if exists else {}
    size = path.stat().st_size if exists else None
    return {
        "video_path": str(path),
        "status": "fallback",
        "backend": "metadata",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "duration": float(metadata.get("duration") or 0.0),
        "summary": _fallback_text(path, metadata, size),
        "mood": "unknown",
        "key_frame": "metadata only",
        "suggested_use": "Review manually before using in the final sequence.",
        "keep": exists,
        "metadata": metadata,
    }


def _metadata_for_path(path: Path) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "file_name": path.name,
        "file_size_bytes": path.stat().st_size,
        "mime_type": mimetypes.guess_type(str(path))[0] or "application/octet-stream",
    }
    try:
        from gemia.video.analysis import get_metadata

        probed = get_metadata(str(path))
        metadata.update(probed)
    except Exception as exc:
        metadata["metadata_error"] = str(exc)
    return metadata


def _fallback_text(path: Path, metadata: dict[str, Any], size: int | None) -> str:
    if not path.exists():
        return "Video summary is unavailable because the file does not exist."
    duration = metadata.get("duration")
    dimensions = ""
    if metadata.get("width") and metadata.get("height"):
        dimensions = f", {metadata['width']}x{metadata['height']}"
    if duration:
        return f"Metadata-only summary for {path.name}: {float(duration):.2f}s{dimensions}."
    if size is not None:
        return f"Metadata-only summary for {path.name}: {size} bytes."
    return f"Metadata-only summary for {path.name}."


def _merge_model_context(base: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    merged.update({
        "status": "summarized",
        "backend": "gemini_native",
        "error": None,
        "model_context": context,
    })
    for key in ("summary", "mood", "key_frame", "suggested_use", "keep"):
        if key in context and context.get(key) is not None:
            merged[key] = context[key]
    if not isinstance(merged.get("keep"), bool):
        merged["keep"] = bool(merged.get("keep"))
    base_duration = _safe_float(base.get("duration"))
    model_duration = _safe_float(context.get("duration"))
    # Duration is timeline-critical. Prefer probed metadata over model text so
    # a summary response cannot silently shrink or stretch imported clips.
    merged["duration"] = base_duration if base_duration > 0 else model_duration
    return merged


def _safe_float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _summary_system_prompt() -> str:
    return textwrap.dedent(
        """
        You are Gemia's video summary analyst.
        Return JSON only. No markdown.

        Read the attached source video directly. Produce a compact project
        summary that can help an editor understand the clip before editing.

        Shape:
        {
          "duration": 8.3,
          "summary": "one or two sentences",
          "mood": "short mood label",
          "key_frame": "timestamp and visual description",
          "suggested_use": "opening / cutaway / transition / background / discard reason",
          "keep": true
        }

        If audio or visuals are unclear, say so in summary or suggested_use
        rather than inventing details. Set keep false when the clip appears
        unusable, repetitive, too blurry, or not worth an editor's time.
        """
    ).strip()
