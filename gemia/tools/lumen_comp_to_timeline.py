"""lumen_comp_to_timeline: place the lumenframe composition on the clip timeline.

Bridge contract of docs/timeline-canonical-plan.md §3 (D2): the composition
enters the timeline as a NORMAL video clip whose asset carries
``metadata.comp_ref`` provenance — every existing invariant (track kind,
duration == source range, overlap), the web renderer, and export pass 1 treat
it with zero special cases.

The window ``[t_in, t_out)`` is rendered once to a content-addressed cache
file under the project's renders dir (``comp_<hash12>_<in_ms>_<out_ms>.mp4``);
re-invoking with the same window and unchanged document reuses the cache.

Comp clips are LIVE references (After Effects precomp semantics): export
pass 0 (:func:`gemia.project_export._refresh_comp_assets`) re-renders stale
windows when the sha256 of ``lumenframe.json`` changes.

Undo asymmetry (also stated in the tool schema): undoing timeline steps never
restores composition content. If you undo past a refresh, the clip points at
the older rendered file (stale but playable); the comp document itself stays
at its latest state.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from gemia.tools._context import ToolContext
from gemia.tools import layer as _layer

try:
    from lumenframe.render_range import export_range as _export_range
    from lumenframe.compile import compile_to_layer_stack as _compile
    from lumenframe import timebase as _timebase
except ImportError:  # pragma: no cover - graceful fallback if lumenframe missing
    _export_range = None
    _compile = None
    _timebase = None


def _new_clip_id() -> str:
    import uuid

    return f"clip_{uuid.uuid4().hex[:8]}"


def _doc_hash(ctx: ToolContext, doc: dict[str, Any]) -> tuple[str, str]:
    """sha256 of the persisted ``lumenframe.json`` BYTES (contract §3.2 step 2).

    When the doc only exists in the session memory cache, hash the canonical
    JSON serialization instead and mark the source ``"memory"`` (best-effort;
    memory docs are session-scoped).
    """
    file_path = _layer._lumenframe_file_path(ctx)
    if file_path is not None and file_path.exists():
        return hashlib.sha256(file_path.read_bytes()).hexdigest(), "file"
    payload = json.dumps(doc, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(payload).hexdigest(), "memory"


async def dispatch(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Render ``[t_in, t_out)`` of the comp and insert it as a timeline clip.

    Args:
        t_in: Inclusive start time in seconds.
        t_out: Exclusive end time in seconds. Must be > ``t_in``. Clamped to
            the composition's actual duration; the clip uses the clamped window.
        track_id: Target video track (default ``V1``; auto-created if missing).
        at_time / at_index: Optional placement (mutually exclusive); default append.
        ripple: Shift later clips to make room when placing by time. Default False.

    Returns:
        ``{applied, asset_id, clip_id, track_id, start, duration, t_in, t_out,
        cache_hit, path, seq, timeline}`` on success;
        ``{applied: False, error_code, error_message}`` on failure.
    """
    if _export_range is None or _compile is None or _timebase is None:
        return {
            "applied": False,
            "error_code": "E_NOT_AVAILABLE",
            "error_message": "lumenframe modules not available",
        }

    if ctx.project is None:
        return {
            "applied": False,
            "error_code": "E_NO_PROJECT",
            "error_message": "lumen_comp_to_timeline needs a project-backed session (ctx.project is None)",
        }

    t_in = args.get("t_in")
    t_out = args.get("t_out")
    if t_in is None or t_out is None:
        return {
            "applied": False,
            "error_code": "E_ARG",
            "error_message": "lumen_comp_to_timeline: need both 't_in' and 't_out' (seconds)",
        }
    try:
        t_in_f = float(t_in)
        t_out_f = float(t_out)
    except (TypeError, ValueError) as e:
        return {
            "applied": False,
            "error_code": "E_ARG",
            "error_message": f"lumen_comp_to_timeline: invalid t_in/t_out: {e}",
        }
    if not (t_in_f < t_out_f):
        return {
            "applied": False,
            "error_code": "E_ARG",
            "error_message": f"lumen_comp_to_timeline: need t_in < t_out (got t_in={t_in_f}, t_out={t_out_f})",
        }

    at_time = args.get("at_time")
    at_index = args.get("at_index")
    if at_time is not None and at_index is not None:
        return {
            "applied": False,
            "error_code": "E_ARG",
            "error_message": "lumen_comp_to_timeline: pass either at_time or at_index, not both",
        }

    doc = _layer._lumendoc(ctx)

    # Clamp the window to the compiled doc's real extent FIRST (same math as
    # export_range) so the clip's duration always equals the rendered file's
    # length — the timeline duration invariant depends on this.
    try:
        stack = _compile(doc, strict=False)
        fps = float(stack.fps)
        total = int(stack.total_frames)
    except Exception as e:
        return {
            "applied": False,
            "error_code": "E_RENDER",
            "error_message": f"compile_to_layer_stack failed: {e}",
        }
    frame_in = min(max(0, int(_timebase.to_frame(t_in_f, fps))), total)
    frame_out = min(max(0, int(_timebase.to_frame(t_out_f, fps))), total)
    if frame_in >= frame_out:
        return {
            "applied": False,
            "error_code": "E_EMPTY_RANGE",
            "error_message": (
                f"window [{t_in_f}, {t_out_f}) is empty after clamping to the "
                f"composition ({total} frames @ {fps:g} fps)"
            ),
        }
    eff_t_in = round(frame_in / fps, 6)
    eff_t_out = round(frame_out / fps, 6)
    duration = round((frame_out - frame_in) / fps, 6)

    doc_hash, hash_source = _doc_hash(ctx, doc)

    project = ctx.project
    renders_dir = project.store.renders_dir(project.project_id)
    renders_dir.mkdir(parents=True, exist_ok=True)
    in_ms = int(round(eff_t_in * 1000))
    out_ms = int(round(eff_t_out * 1000))
    cache_path = renders_dir / f"comp_{doc_hash[:12]}_{in_ms}_{out_ms}.mp4"

    cache_hit = cache_path.exists() and cache_path.stat().st_size > 0
    if not cache_hit:
        try:
            _export_range(doc, eff_t_in, eff_t_out, str(cache_path))
        except ValueError as e:
            return {
                "applied": False,
                "error_code": "E_EMPTY_RANGE",
                "error_message": f"export_range produced no frames: {e}",
            }
        except Exception as e:
            return {
                "applied": False,
                "error_code": "E_RENDER",
                "error_message": f"export_range failed: {e}",
            }

    asset_id = ctx.registry.allocate_id("video")
    summary = f"lumenframe comp [{eff_t_in:.3f}s, {eff_t_out:.3f}s) as timeline clip"
    ctx.registry.register_output(asset_id, kind="video", path=cache_path, summary=summary)

    comp_ref = {
        "doc_id": str(doc.get("id") or ""),
        "t_in": eff_t_in,
        "t_out": eff_t_out,
        "step": 1,
        "doc_hash": f"sha256:{doc_hash}",
        "doc_hash_source": hash_source,
        "rendered_at": datetime.now(timezone.utc).isoformat(),
    }
    asset_payload = {
        "id": asset_id,
        "asset_id": asset_id,
        "name": cache_path.name,
        "media_kind": "video",
        "source_path": str(cache_path),
        "duration": duration,
        "metadata": {"comp_ref": comp_ref},
    }
    clip = {
        "id": _new_clip_id(),
        "asset_id": asset_id,
        "media_kind": "video",
        "name": cache_path.name,
        "duration": duration,
        "source_in": 0.0,
        "source_out": duration,
    }

    ops: list[dict[str, Any]] = []
    state = project.load()
    tracks = state.get("timeline", {}).get("tracks") or []
    track_id = str(args.get("track_id") or "") or "V1"
    if not any(str(t.get("id")) == track_id for t in tracks):
        ops.append({"op": "add_track", "kind": "video", "track_id": track_id})
    clip["track_id"] = track_id

    at: Any = "append"
    if at_time is not None:
        at = {"time": round(float(at_time), 6)}
    elif at_index is not None:
        at = {"index": int(at_index)}

    # ONE atomic patch: the embedded asset upsert + clip insert land together,
    # so the whole bridge insertion is undoable as a single step (contract §3.4).
    ops.append({
        "op": "insert_clip",
        "data": {"asset": asset_payload, "clip": clip},
        "track_id": track_id,
        "at": at,
        "ripple": bool(args.get("ripple", False)),
        "provenance": {"verb": "lumen_comp_to_timeline", "session_id": ctx.session_id},
    })
    result = project.apply_ops(ops, label="lumen_comp_to_timeline")

    placed = next(
        (
            c
            for c in (project.load().get("timeline", {}).get("clips") or [])
            if str(c.get("id")) == clip["id"]
        ),
        clip,
    )
    return {
        "applied": True,
        "asset_id": asset_id,
        "clip_id": clip["id"],
        "track_id": track_id,
        "start": placed.get("start"),
        "duration": duration,
        "t_in": eff_t_in,
        "t_out": eff_t_out,
        "cache_hit": cache_hit,
        "path": str(cache_path),
        "seq": result.get("patch_seq_end"),
        "timeline": project.compact_text(),
        "summary": summary,
    }


__all__ = ["dispatch"]
