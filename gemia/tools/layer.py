"""lumen_*: lumenframe layer editing verbs with persistent storage.

This module exposes the lumenframe LayerPatch vocabulary as agent verbs
(similar to timeline_* verbs). The document state is persisted to the project
and survives across session boundaries.

Design contract:
- Each verb compiles to a LayerPatch and applies it atomically via
  ``apply_layer_patch``.
- ``get_lumenframe`` returns the compact tree plus bounded structured details
  for appearance, effects, masks, timing, and type-specific properties.
- ``lumen_patch`` is the low-level verb that accepts a raw ops list;
  convenience verbs (add_layer, set_transform, etc.) are built on top.
- **Persistence**: if ctx.project (ProjectHandle) is available, doc is stored
  in project["lumenframe"] field and persists across sessions. Fallback to
  in-memory cache for non-project sessions. Lumenframe is **orthogonal**
  to timeline — no mutation of timeline fields.
"""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from gemia.tools._context import ToolContext

_logger = logging.getLogger(__name__)

# Path cache to ensure once-resolved paths stay stable
_LUMENFRAME_PATH_CACHE: dict[tuple[str, str], Path | None] = {}

try:
    from lumenframe import (
        apply_layer_patch,
        describe_ops,
        empty_doc,
        find_layer,
        find_parent,
        new_layer,
        normalize_doc,
    )
    from lumenframe.compile import compile_to_layer_stack
except ImportError:
    # Graceful fallback if lumenframe is not available
    empty_doc = None
    apply_layer_patch = None
    describe_ops = None
    new_layer = None
    find_layer = None
    find_parent = None
    normalize_doc = None
    compile_to_layer_stack = None


# Session-local doc state cache (keyed by session_id) — fallback for non-project sessions
_DOC_CACHE: dict[str, dict[str, Any]] = {}
_DOC_HISTORY_CACHE: dict[str, dict[str, Any]] = {}

# ``get_lumenframe`` is model-facing, so its structured detail view must stay
# useful without reflecting an arbitrarily large document back into the prompt.
_LAYER_DETAILS_LIMIT = 128
_LAYER_EFFECTS_LIMIT = 32
_LAYER_DETAIL_SEQUENCE_LIMIT = 32
_LAYER_DETAIL_MAPPING_LIMIT = 32
_LAYER_DETAIL_DEPTH_LIMIT = 4
_LAYER_DETAIL_STRING_LIMIT = 512


def lumenframe_revision(doc: dict[str, Any]) -> str:
    """Return the stable optimistic-concurrency token for one LumenDoc."""
    payload = json.dumps(doc, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _history_file_path(ctx: ToolContext) -> Path | None:
    file_path = _lumenframe_file_path(ctx)
    return file_path.with_name("lumenframe-history.json") if file_path is not None else None


def _history_entry(
    doc: dict[str, Any], *, label: str, client_op_id: str | None = None,
) -> dict[str, Any]:
    return {
        "revision": lumenframe_revision(doc),
        "at": datetime.now(timezone.utc).isoformat(),
        "label": str(label or "lumenframe edit"),
        "client_op_id": str(client_op_id or "") or None,
        "doc": copy.deepcopy(doc),
    }


def _load_lumenframe_history(ctx: ToolContext, current: dict[str, Any]) -> dict[str, Any]:
    """Load and reconcile the bounded, durable full-snapshot edit journal."""
    path = _history_file_path(ctx)
    raw: dict[str, Any] | None = None
    if path is not None and path.exists():
        try:
            candidate = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(candidate, dict):
                raw = candidate
        except Exception:
            raw = None
    elif path is None:
        candidate = _DOC_HISTORY_CACHE.get(ctx.session_id)
        if isinstance(candidate, dict):
            raw = copy.deepcopy(candidate)

    entries = list(raw.get("entries") or []) if isinstance(raw, dict) else []
    entries = [
        entry for entry in entries
        if isinstance(entry, dict) and isinstance(entry.get("doc"), dict)
    ]
    client_ops = list(raw.get("client_ops") or []) if isinstance(raw, dict) else []
    client_ops = [
        record for record in client_ops
        if isinstance(record, dict) and str(record.get("id") or "")
    ][-100:]
    try:
        cursor = int(raw.get("cursor")) if isinstance(raw, dict) else -1
    except (TypeError, ValueError):
        cursor = -1
    cursor = min(max(cursor, -1), len(entries) - 1)

    current_revision = lumenframe_revision(current)
    if cursor < 0 or str(entries[cursor].get("revision") or "") != current_revision:
        # Preserve compatibility with documents written before this journal (or
        # by an older agent verb): the actual project document becomes a new tip.
        entries = entries[: cursor + 1]
        entries.append(_history_entry(current, label="reconcile persisted document"))
        cursor = len(entries) - 1
    return {
        "version": 1,
        "cursor": cursor,
        "entries": entries,
        "client_ops": client_ops,
    }


def _write_lumenframe_history(ctx: ToolContext, history: dict[str, Any]) -> bool:
    path = _history_file_path(ctx)
    if path is None:
        _DOC_HISTORY_CACHE[ctx.session_id] = copy.deepcopy(history)
        return True
    return _write_lumenframe_atomic(path, history)


def lumenframe_history_status(
    ctx: ToolContext, doc: dict[str, Any] | None = None,
) -> dict[str, Any]:
    current = doc if isinstance(doc, dict) else _lumendoc(ctx)
    history = _load_lumenframe_history(ctx, current)
    cursor = int(history["cursor"])
    return {
        "revision": lumenframe_revision(current),
        "can_undo": cursor > 0,
        "can_redo": cursor + 1 < len(history["entries"]),
        "history_length": len(history["entries"]),
        "history_cursor": cursor,
    }


def lumenframe_client_op_seen(
    ctx: ToolContext,
    client_op_id: str | None,
    doc: dict[str, Any] | None = None,
) -> bool:
    if not client_op_id:
        return False
    current = doc if isinstance(doc, dict) else _lumendoc(ctx)
    history = _load_lumenframe_history(ctx, current)
    return any(
        str(record.get("id") or "") == str(client_op_id)
        for record in history.get("client_ops") or []
    )


def _commit_lumendoc(
    ctx: ToolContext,
    before: dict[str, Any],
    after: dict[str, Any],
    *,
    label: str,
    client_op_id: str | None = None,
) -> bool:
    history = _load_lumenframe_history(ctx, before)
    cursor = int(history["cursor"])
    entries = history["entries"][: cursor + 1]
    entries.append(_history_entry(after, label=label, client_op_id=client_op_id))
    if len(entries) > 100:
        entries = entries[-100:]
    client_ops = list(history.get("client_ops") or [])
    if client_op_id:
        client_ops.append({
            "id": str(client_op_id),
            "kind": "edit",
            "result_revision": lumenframe_revision(after),
        })
    updated_history = {
        "version": 1,
        "cursor": len(entries) - 1,
        "entries": entries,
        "client_ops": client_ops[-100:],
    }

    if not _save_lumendoc(ctx, after):
        return False
    if not _write_lumenframe_history(ctx, updated_history):
        # Best-effort rollback keeps the canonical doc at the prior revision.
        _save_lumendoc(ctx, before)
        return False
    return True


def restore_lumendoc_history(
    ctx: ToolContext,
    *,
    direction: str,
    steps: int = 1,
    client_op_id: str | None = None,
    base_revision: str | None = None,
) -> dict[str, Any]:
    current = _lumendoc(ctx)
    history = _load_lumenframe_history(ctx, current)
    if lumenframe_client_op_seen(ctx, client_op_id, current):
        return {
            "doc": copy.deepcopy(current),
            "duplicate": True,
            **lumenframe_history_status(ctx, current),
        }
    current_revision = lumenframe_revision(current)
    if base_revision is not None and str(base_revision) != current_revision:
        raise LumenframeRevisionConflict(current_revision)
    cursor = int(history["cursor"])
    amount = max(1, min(int(steps), 50))
    if direction == "undo":
        target = max(0, cursor - amount)
    elif direction == "redo":
        target = min(len(history["entries"]) - 1, cursor + amount)
    else:
        raise ValueError("direction must be 'undo' or 'redo'")
    doc = copy.deepcopy(history["entries"][target]["doc"])
    if not _save_lumendoc(ctx, doc):
        raise OSError("failed to persist lumenframe history restore")
    history["cursor"] = target
    if client_op_id:
        client_ops = list(history.get("client_ops") or [])
        client_ops.append({
            "id": str(client_op_id),
            "kind": direction,
            "result_revision": lumenframe_revision(doc),
        })
        history["client_ops"] = client_ops[-100:]
    if not _write_lumenframe_history(ctx, history):
        _save_lumendoc(ctx, current)
        raise OSError("failed to persist lumenframe history cursor")
    return {"doc": doc, **lumenframe_history_status(ctx, doc)}


class LumenframeRevisionConflict(ValueError):  # noqa: N818 - public API name
    """A direct edit was based on a stale LumenFrame snapshot."""

    code = "E_REVISION_CONFLICT"

    def __init__(self, current_revision: str) -> None:
        self.current_revision = current_revision
        super().__init__("lumenframe document changed; refresh and retry")


def get_lumendoc_snapshot(ctx: ToolContext) -> dict[str, Any]:
    """Return a private copy of the canonical LumenFrame document."""
    return copy.deepcopy(_lumendoc(ctx))


def apply_lumendoc_ops(
    ctx: ToolContext,
    ops: list[dict[str, Any]],
    *,
    label: str = "lumenframe edit",
    client_op_id: str | None = None,
    base_revision: str | None = None,
) -> dict[str, Any]:
    """Apply one atomic LayerPatch with revision and durable history semantics."""
    if apply_layer_patch is None:
        raise RuntimeError("lumenframe module not available")
    if not isinstance(ops, list) or not ops:
        raise ValueError("lumenframe edit requires a non-empty ops list")

    before = _lumendoc(ctx)
    if lumenframe_client_op_seen(ctx, client_op_id, before):
        return {
            "doc": copy.deepcopy(before),
            "duplicate": True,
            **lumenframe_history_status(ctx, before),
        }
    current_revision = lumenframe_revision(before)
    if base_revision is not None and str(base_revision) != current_revision:
        raise LumenframeRevisionConflict(current_revision)

    after = apply_layer_patch(before, {"version": 1, "ops": copy.deepcopy(ops)})
    if not _commit_lumendoc(
        ctx, before, after, label=label, client_op_id=client_op_id,
    ):
        raise OSError("failed to persist lumenframe document and history")
    return {
        "doc": after,
        "duplicate": False,
        **lumenframe_history_status(ctx, after),
    }


def _lumenframe_file_path(ctx: ToolContext) -> Path | None:
    """Get the path to the lumenframe.json file for this project, if applicable.

    Paths are cached per (session_id, project_id) to ensure stability across
    the session lifetime.
    """
    if ctx.project is None:
        return None

    cache_key = (ctx.session_id, ctx.project.project_id)
    if cache_key in _LUMENFRAME_PATH_CACHE:
        return _LUMENFRAME_PATH_CACHE[cache_key]

    try:
        project_dir = ctx.project.store.project_dir(ctx.project.project_id)
        path = project_dir / "lumenframe.json"
        _LUMENFRAME_PATH_CACHE[cache_key] = path
        return path
    except Exception:
        _LUMENFRAME_PATH_CACHE[cache_key] = None
        return None


def _lumendoc(ctx: ToolContext) -> dict[str, Any]:
    """Retrieve or initialize the session's lumenframe document.

    **Persistence strategy:**
    - If ctx.project (ProjectHandle) is available: load from <project_dir>/lumenframe.json,
      lazy-initialize and persist if missing. This keeps lumenframe orthogonal to
      the main project state dict (timeline, assets, etc.).
    - Else: fall back to in-session memory cache (_DOC_CACHE)

    **Data safety:**
    - If lumenframe.json is corrupted/unreadable, rename it to lumenframe.json.corrupt-<timestamp>
      (preserving the broken data for diagnostic purposes) and return a fresh empty_doc().
    - This prevents silent data loss and helps with debugging.

    This ensures lumenframe docs persist across session boundaries (like timeline)
    while remaining fully orthogonal to timeline structure.

    CRITICAL: Only read/write <project_dir>/lumenframe.json, never touch project state / timeline / clips.
    """
    if empty_doc is None or normalize_doc is None:
        raise RuntimeError("lumenframe module not available")

    # Prefer persistent project storage if available
    if ctx.project is not None:
        file_path = _lumenframe_file_path(ctx)
        if file_path is not None:
            try:
                if file_path.exists():
                    # Load from file
                    raw = json.loads(file_path.read_text(encoding="utf-8"))
                    doc = normalize_doc(raw)
                    return doc
                else:
                    # Lazy initialize: create, save, and return
                    doc = empty_doc()
                    file_path.parent.mkdir(parents=True, exist_ok=True)
                    _write_lumenframe_atomic(file_path, doc)
                    return doc
            except (json.JSONDecodeError, ValueError) as e:
                # Corrupted JSON: rename the file for diagnostic purposes
                _logger.warning(
                    f"Corrupted lumenframe.json at {file_path}: {e}. "
                    "Renaming to preserve diagnostics, returning fresh empty_doc()."
                )
                try:
                    corrupt_path = file_path.parent / f"lumenframe.json.corrupt-{datetime.now(timezone.utc).isoformat()}"
                    os.replace(str(file_path), str(corrupt_path))
                except Exception as rename_err:
                    _logger.warning(f"Failed to rename corrupt file {file_path}: {rename_err}")
                # Return a fresh doc
                doc = empty_doc()
                return doc
            except Exception as e:
                # Other read errors: log and fall back
                _logger.warning(f"Failed to read lumenframe.json at {file_path}: {e}. Falling back to memory cache.")
                pass

    # Fallback: in-session memory cache
    key = ctx.session_id
    if key not in _DOC_CACHE:
        _DOC_CACHE[key] = empty_doc()
    return _DOC_CACHE[key]


def peek_lumendoc(ctx: ToolContext) -> dict[str, Any] | None:
    """Read-only view of the session's lumenframe document, or ``None`` if the
    session has none yet.

    Unlike ``_lumendoc`` this never lazy-creates ``lumenframe.json`` and never
    seeds ``_DOC_CACHE`` — it is safe to call on every prompt build (the agent
    loop injects the layer summary/ops catalog from here). In v3 ``ctx.project``
    is always set, so the file is the source of truth; the memory cache is only
    consulted for project-less contexts (unit tests, embedded use).
    """
    if empty_doc is None or normalize_doc is None:
        return None
    if ctx.project is not None:
        file_path = _lumenframe_file_path(ctx)
        if file_path is None or not file_path.exists():
            return None
        try:
            return normalize_doc(json.loads(file_path.read_text(encoding="utf-8")))
        except Exception:
            # Corruption recovery (rename + fresh doc) is _lumendoc's job at
            # edit time; a prompt build just declines to inject.
            return None
    return _DOC_CACHE.get(ctx.session_id)


def _write_lumenframe_atomic(file_path: Path, doc: dict[str, Any]) -> bool:
    """Write lumenframe document atomically: temp file → fsync → rename.

    This prevents corruption from partial writes or power loss.

    Returns True on success, False on write failure. On failure, logs warning but does not raise.
    """
    temp_path = None
    try:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        json_text = json.dumps(copy.deepcopy(doc), ensure_ascii=False, indent=2)

        # Write to temporary file in same directory (same filesystem)
        temp_fd, temp_path = tempfile.mkstemp(dir=str(file_path.parent), prefix=".lumenframe.", suffix=".tmp")
        try:
            os.write(temp_fd, json_text.encode("utf-8"))
            os.fsync(temp_fd)  # Ensure data reaches disk
        finally:
            os.close(temp_fd)

        # Atomic rename
        os.replace(temp_path, str(file_path))
        return True
    except OSError as e:
        _logger.warning(f"Failed to write lumenframe.json atomically to {file_path}: {e}")
        # Clean up temp file if it exists
        if temp_path is not None:
            try:
                os.unlink(temp_path)
            except Exception:
                pass
        return False
    except Exception as e:
        _logger.warning(f"Unexpected error writing lumenframe.json to {file_path}: {e}")
        # Clean up temp file if it exists
        if temp_path is not None:
            try:
                os.unlink(temp_path)
            except Exception:
                pass
        return False


def _save_lumendoc(ctx: ToolContext, doc: dict[str, Any]) -> bool:
    """Store the document, persisting to project file or memory cache.

    - If ctx.project: write to <project_dir>/lumenframe.json atomically
    - Else: store in memory cache (_DOC_CACHE)

    **Data safety:**
    - Uses atomic temp-file-then-rename pattern to prevent corruption.
    - A project-backed write never falls through to memory on disk failure.

    This keeps lumenframe editing fully orthogonal to the main project state
    and timeline patch history.

    CRITICAL: Only write <project_dir>/lumenframe.json, never touch project state / timeline / clips.
    """
    if ctx.project is not None:
        file_path = _lumenframe_file_path(ctx)
        if file_path is None:
            _DOC_CACHE[ctx.session_id] = copy.deepcopy(doc)
            return False
        file_path.parent.mkdir(parents=True, exist_ok=True)
        persisted = _write_lumenframe_atomic(file_path, doc)
        if not persisted:
            # Keep the attempted state available for diagnostics/legacy
            # embedded callers, but the False return remains authoritative:
            # project-backed callers must never report this as committed.
            _DOC_CACHE[ctx.session_id] = copy.deepcopy(doc)
        return persisted

    # Project-less embedded/tests use a session-local in-memory document.
    _DOC_CACHE[ctx.session_id] = copy.deepcopy(doc)
    return True


def _compact_tree_summary(layer: dict[str, Any], depth: int = 0, max_depth: int = 4) -> str:
    """Recursively build a compact summary of a layer tree."""
    if depth > max_depth:
        return ""

    indent = "  " * depth
    # Full id, never truncated: the model addresses layers (delete/patch/
    # set_visibility) by this exact string. Truncating to 12 chars produced a
    # display id like "shape_1571eb" for a real "shape_1571eb06a035", so every
    # edit the model copied from this tree failed with E_NOT_FOUND.
    layer_id = str(layer.get("id", "?"))
    layer_type = str(layer.get("type", "?"))
    layer_name = str(layer.get("name", ""))
    visible = "👁" if layer.get("visible", True) else "🚫"
    locked = "🔒" if layer.get("locked", False) else ""

    line = f"{indent}{visible} {layer_type:12} {layer_id:12} {layer_name} {locked}"
    parts = [line]

    for child in layer.get("children") or []:
        parts.append(_compact_tree_summary(child, depth + 1, max_depth))

    return "\n".join(p for p in parts if p)


def _bounded_detail_value(
    value: Any,
    *,
    depth: int = 0,
    omit_keys: frozenset[str] = frozenset(),
) -> Any:
    """Return a JSON-safe, size-bounded copy for model-facing metadata."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        if len(value) <= _LAYER_DETAIL_STRING_LIMIT:
            return value
        return value[:_LAYER_DETAIL_STRING_LIMIT] + "…"
    if depth >= _LAYER_DETAIL_DEPTH_LIMIT:
        return "<truncated>"
    if isinstance(value, dict):
        items = [
            (str(key), item)
            for key, item in value.items()
            if str(key) not in omit_keys
        ]
        result = {
            key: _bounded_detail_value(
                item,
                depth=depth + 1,
                omit_keys=omit_keys,
            )
            for key, item in items[:_LAYER_DETAIL_MAPPING_LIMIT]
        }
        if len(items) > _LAYER_DETAIL_MAPPING_LIMIT:
            result["_truncated_fields"] = len(items) - _LAYER_DETAIL_MAPPING_LIMIT
        return result
    if isinstance(value, (list, tuple)):
        result = [
            _bounded_detail_value(
                item,
                depth=depth + 1,
                omit_keys=omit_keys,
            )
            for item in value[:_LAYER_DETAIL_SEQUENCE_LIMIT]
        ]
        if len(value) > _LAYER_DETAIL_SEQUENCE_LIMIT:
            result.append({
                "_truncated_items": len(value) - _LAYER_DETAIL_SEQUENCE_LIMIT,
            })
        return result
    return _bounded_detail_value(str(value), depth=depth)


def _mask_detail(mask: Any) -> dict[str, Any] | None:
    """Summarise a mask without echoing inline pixel alpha/data payloads."""
    if not isinstance(mask, dict):
        return None
    summary: dict[str, Any] = {
        "kind": str(mask.get("kind") or "shape"),
        "invert": bool(mask.get("invert", False)),
        "feather": float(mask.get("feather") or 0.0),
    }
    for key in (
        "source_layer_id", "asset_id", "channel", "mode", "threshold",
        "softness", "width", "height",
    ):
        if key in mask:
            summary[key] = _bounded_detail_value(mask[key])
    if isinstance(mask.get("shape"), dict):
        summary["shape"] = _bounded_detail_value(
            mask["shape"],
            omit_keys=frozenset({"alpha", "data"}),
        )
    if "alpha" in mask or "data" in mask:
        summary["has_inline_data"] = True
    return summary


def _type_props_detail(layer: dict[str, Any]) -> dict[str, Any] | None:
    layer_type = str(layer.get("type") or "")
    props = layer.get("props")
    if not isinstance(props, dict):
        return None
    if layer_type == "gradient":
        keys = ("mode", "stops", "angle", "center", "radius")
    elif layer_type == "shape":
        keys = (
            "kind", "fill", "stroke", "rect", "points", "cx", "cy",
            "rx", "ry", "radius", "opacity_baked",
        )
    else:
        return None
    return {
        key: _bounded_detail_value(props[key])
        for key in keys
        if key in props
    }


def _layer_detail(layer: dict[str, Any]) -> dict[str, Any]:
    start = float(layer.get("start") or 0.0)
    duration = float(layer.get("duration") or 0.0)
    transform = layer.get("transform") if isinstance(layer.get("transform"), dict) else {}
    effects = [effect for effect in layer.get("effects") or [] if isinstance(effect, dict)]
    detail: dict[str, Any] = {
        "id": str(layer.get("id") or ""),
        "type": str(layer.get("type") or "unknown"),
        "name": str(layer.get("name") or ""),
        "timing": {"start": start, "duration": duration, "end": start + duration},
        "opacity": float(layer.get("opacity", 1.0)),
        "blend_mode": str(layer.get("blend_mode") or "normal"),
        "clip_to_below": bool(layer.get("clip_to_below", False)),
        "transform": {
            key: _bounded_detail_value(transform[key])
            for key in (
                "x", "y", "scale_x", "scale_y", "rotation", "anchor_x", "anchor_y",
            )
            if key in transform
        },
        "effects": [
            {
                "id": str(effect.get("id") or ""),
                "type": str(effect.get("type") or "unknown"),
                "enabled": bool(effect.get("enabled", True)),
                "params": _bounded_detail_value(effect.get("params") or {}),
            }
            for effect in effects[:_LAYER_EFFECTS_LIMIT]
        ],
        "mask": _mask_detail(layer.get("mask")),
    }
    if len(effects) > _LAYER_EFFECTS_LIMIT:
        detail["effects_truncated"] = len(effects) - _LAYER_EFFECTS_LIMIT
    props = _type_props_detail(layer)
    if props is not None:
        detail["props"] = props
    return detail


def _collect_layer_details(root: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
    """Flatten the editable tree in display order, with an explicit hard cap."""
    children = [child for child in root.get("children") or [] if isinstance(child, dict)]
    pending = list(reversed(children))
    details: list[dict[str, Any]] = []
    while pending and len(details) < _LAYER_DETAILS_LIMIT:
        layer = pending.pop()
        details.append(_layer_detail(layer))
        nested = [child for child in layer.get("children") or [] if isinstance(child, dict)]
        pending.extend(reversed(nested))
    return details, bool(pending)


async def dispatch_get(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Inspect the session's lumenframe document state.

    Returns: compact summary of layers, selection, canvas.
    """
    if apply_layer_patch is None:
        raise RuntimeError("lumenframe module not available")

    doc = get_lumendoc_snapshot(ctx)
    root = doc.get("root", {})
    canvas = doc.get("canvas", {})
    selection = doc.get("selection", [])

    root_tree = _compact_tree_summary(root) if root else "(empty composition)"
    layer_details, layer_details_truncated = _collect_layer_details(root)

    return {
        "applied": True,
        "canvas": {
            "width": canvas.get("width"),
            "height": canvas.get("height"),
            "fps": canvas.get("fps"),
        },
        "root_layers": root_tree,
        "layer_details": layer_details,
        "layer_details_truncated": layer_details_truncated,
        "selection_ids": selection,
        "doc_id": doc.get("id"),
    }


async def dispatch_patch(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Apply a raw LayerPatch (one or more ops) atomically.

    This is the low-level verb; agents can call it directly or use
    convenience verbs (add_layer, set_transform, etc.) which wrap it.

    Args:
        ops: list of LayerPatch ops (e.g. [{"op": "add_layer", ...}, ...])

    Returns:
        applied: True if successful
        error: error message if failed
        doc_summary: compact tree after patch
    """
    if apply_layer_patch is None:
        raise RuntimeError("lumenframe module not available")

    ops = args.get("ops")
    if not ops:
        raise ValueError("lumen_patch: missing required argument 'ops'")

    if not isinstance(ops, list):
        raise ValueError(f"lumen_patch: ops must be a list, got {type(ops).__name__}")

    try:
        result = apply_lumendoc_ops(
            ctx,
            ops,
            label=str(args.get("label") or "agent lumenframe edit"),
            client_op_id=str(args.get("client_op_id") or "") or None,
            base_revision=(
                str(args["base_revision"])
                if args.get("base_revision") is not None else None
            ),
        )
        new_doc = result["doc"]

        root = new_doc.get("root", {})
        root_tree = _compact_tree_summary(root) if root else "(empty)"

        return {
            "applied": True,
            "ops_count": len(ops),
            "root_layers": root_tree,
            "selection_ids": new_doc.get("selection", []),
            "revision": result["revision"],
            "can_undo": result["can_undo"],
            "can_redo": result["can_redo"],
            "duplicate": result["duplicate"],
        }
    except Exception as e:
        # Apply failed; doc unchanged. Return structured error.
        error_code = getattr(e, "code", "E_PERSIST" if isinstance(e, OSError) else "E_UNKNOWN")
        error_msg = str(getattr(e, "message", str(e)))
        return {
            "applied": False,
            "error_code": error_code,
            "error_message": error_msg,
            "recovery": (
                "fix_args" if error_code.startswith("E_ARG")
                else ("transient_retry" if error_code == "E_PERSIST" else "none")
            ),
        }


async def dispatch_add_layer(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Convenience verb: create a new layer.

    Args:
        type: layer type (video, image, text, shape, audio, adjustment, solid, null, composition)
        name: optional layer name
        parent_id: optional parent layer id (default: root)
        index: optional insert position (default: end)
        at_time: optional start time on parent timeline (default: 0)

    Returns: result of dispatch_patch
    """
    layer_type = args.get("type")
    if not layer_type:
        raise ValueError("add_layer: missing required 'type'")

    op = {
        "op": "add_layer",
        "type": layer_type,
    }

    if "name" in args and args["name"]:
        op["name"] = str(args["name"])
    if "parent_id" in args and args["parent_id"]:
        op["parent_id"] = str(args["parent_id"])
    if "index" in args and args["index"] is not None:
        op["index"] = int(args["index"])
    if "at_time" in args and args["at_time"] is not None:
        op["at_time"] = float(args["at_time"])

    return await dispatch_patch({"ops": [op]}, ctx)


async def dispatch_set_transform(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Convenience verb: transform a layer (move, scale, rotate).

    Args:
        layer_id: id of layer to transform (required)
        x: canvas x offset (pixels from centre)
        y: canvas y offset
        scale: uniform scale (overrides scale_x/scale_y)
        scale_x: horizontal scale
        scale_y: vertical scale
        rotation: rotation in degrees
        anchor_x: anchor point x (0..1)
        anchor_y: anchor point y (0..1)
    """
    layer_id = args.get("layer_id")
    if not layer_id:
        raise ValueError("set_transform: missing required 'layer_id'")

    op = {"op": "set_transform", "layer_id": str(layer_id)}

    for key in ["x", "y", "scale_x", "scale_y", "rotation", "anchor_x", "anchor_y"]:
        if key in args and args[key] is not None:
            op[key] = float(args[key])

    # Unified scale overrides scale_x/scale_y
    if "scale" in args and args["scale"] is not None:
        scale_val = float(args["scale"])
        op["scale_x"] = scale_val
        op["scale_y"] = scale_val

    return await dispatch_patch({"ops": [op]}, ctx)


async def dispatch_set_opacity(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Convenience verb: set layer opacity.

    Args:
        layer_id: id of layer (required)
        opacity: opacity value 0..1 (required)
    """
    layer_id = args.get("layer_id")
    opacity = args.get("opacity")

    if not layer_id:
        raise ValueError("set_opacity: missing required 'layer_id'")
    if opacity is None:
        raise ValueError("set_opacity: missing required 'opacity'")

    op = {"op": "set_opacity", "layer_id": str(layer_id), "opacity": float(opacity)}
    return await dispatch_patch({"ops": [op]}, ctx)


async def dispatch_delete_layer(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Convenience verb: delete a layer.

    Args:
        layer_id: id of layer to delete (or layer_ids for multiple)
    """
    layer_id = args.get("layer_id")
    layer_ids = args.get("layer_ids")

    if not layer_id and not layer_ids:
        raise ValueError("delete_layer: need layer_id or layer_ids")

    ids_list = [layer_id] if layer_id else list(layer_ids or [])

    ops = [{"op": "delete_layer", "layer_ids": ids_list}]
    return await dispatch_patch({"ops": ops}, ctx)


async def dispatch_move_layer(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Convenience verb: move a layer (reparent, reorder, retime, relane).

    Args:
        layer_id: id of layer to move (required)
        parent_id: new parent (optional)
        index: new z-order within parent (optional)
        lane: new lane hint (optional)
        start: new start time (optional)
    """
    layer_id = args.get("layer_id")
    if not layer_id:
        raise ValueError("move_layer: missing required 'layer_id'")

    op = {"op": "move_layer", "layer_id": str(layer_id)}

    if "parent_id" in args and args["parent_id"]:
        op["parent_id"] = str(args["parent_id"])
    if "index" in args and args["index"] is not None:
        op["index"] = int(args["index"])
    if "lane" in args and args["lane"] is not None:
        op["lane"] = str(args["lane"])
    if "start" in args and args["start"] is not None:
        op["start"] = float(args["start"])

    return await dispatch_patch({"ops": [op]}, ctx)


async def dispatch_set_visibility(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Convenience verb: show/hide a layer.

    Args:
        layer_id: id of layer (required)
        visible: True to show, False to hide (required)
    """
    layer_id = args.get("layer_id")
    visible = args.get("visible")

    if not layer_id:
        raise ValueError("set_visibility: missing required 'layer_id'")
    if visible is None:
        raise ValueError("set_visibility: missing required 'visible'")

    op = {"op": "set_visibility", "layer_id": str(layer_id), "visible": bool(visible)}
    return await dispatch_patch({"ops": [op]}, ctx)


async def dispatch_select(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Convenience verb: change selection.

    Args:
        layer_ids: list of ids to select (required)
        mode: 'replace' (default), 'add', 'toggle', 'clear'
    """
    layer_ids = args.get("layer_ids")
    mode = args.get("mode", "replace")

    if not layer_ids and mode != "clear":
        raise ValueError("select: need layer_ids (or mode='clear')")

    op = {"op": "select", "layer_ids": list(layer_ids or []), "mode": mode}
    return await dispatch_patch({"ops": [op]}, ctx)


async def dispatch_set_mask(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Attach, replace, or clear a layer mask.

    Supports vector shape masks, direct pixel/alpha masks, and alpha/luma track
    mattes. This is a convenience wrapper over the core set_mask LayerPatch op.
    """
    layer_id = args.get("layer_id")
    if not layer_id:
        raise ValueError("set_mask: missing required 'layer_id'")
    if bool(args.get("clear", False)):
        return await dispatch_patch({"ops": [{"op": "set_mask", "layer_id": str(layer_id), "mask": None}]}, ctx)

    kind = str(args.get("kind") or "shape").lower()
    if kind in {"alpha", "alpha_matte"}:
        kind = "alpha_matte"
    elif kind in {"luma", "luma_matte"}:
        kind = "luma_matte"
    elif kind in {"bitmap", "pixel_mask"}:
        kind = "pixel"
    elif kind not in {"shape", "pixel"}:
        raise ValueError(f"set_mask: unknown kind {kind!r}")

    mask: dict[str, Any] = {"kind": kind}
    for key in ("invert", "feather"):
        if args.get(key) is not None:
            mask[key] = args[key]

    if kind == "shape":
        shape = args.get("shape")
        if not isinstance(shape, dict):
            raise ValueError("set_mask: kind=shape requires shape object")
        mask["shape"] = shape
    elif kind == "pixel":
        for key in ("asset_id", "alpha", "data", "channel", "threshold", "softness", "width", "height"):
            if args.get(key) is not None:
                mask[key] = args[key]
        if "asset_id" not in mask and "alpha" not in mask and "data" not in mask:
            raise ValueError("set_mask: kind=pixel requires asset_id or alpha/data")
    else:
        source_layer_id = args.get("source_layer_id")
        if not source_layer_id:
            raise ValueError(f"set_mask: kind={kind} requires source_layer_id")
        mask["source_layer_id"] = str(source_layer_id)

    return await dispatch_patch({"ops": [{"op": "set_mask", "layer_id": str(layer_id), "mask": mask}]}, ctx)


async def dispatch_key(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Apply a layer keying effect: chroma, advanced_chroma, or luma."""
    layer_id = str(args.get("layer_id") or "")
    if not layer_id:
        raise ValueError("lumen_key: missing required 'layer_id'")

    method = str(args.get("method") or "advanced_chroma").lower()
    effect_type = {
        "chroma": "chroma_key",
        "chroma_key": "chroma_key",
        "advanced": "advanced_chroma_key",
        "advanced_chroma": "advanced_chroma_key",
        "advanced_chroma_key": "advanced_chroma_key",
        "luma": "luma_key",
        "luma_key": "luma_key",
    }.get(method)
    if effect_type is None:
        raise ValueError(f"lumen_key: unknown method {method!r}")

    params = dict(args.get("params") or {})
    for key in ("key_color", "threshold", "similarity", "softness", "spill", "despill", "edge_blur", "mode"):
        if args.get(key) is not None:
            params[key] = args[key]

    ops: list[dict[str, Any]] = []
    if args.get("replace_existing", True) and find_layer is not None:
        doc = _lumendoc(ctx)
        layer = find_layer(doc, layer_id)
        if isinstance(layer, dict):
            for effect in layer.get("effects") or []:
                if str(effect.get("type")) in {"chroma_key", "advanced_chroma_key", "luma_key"}:
                    effect_id = effect.get("id")
                    if effect_id:
                        ops.append({"op": "remove_effect", "layer_id": layer_id, "effect_id": str(effect_id)})

    ops.append({
        "op": "add_effect",
        "layer_id": layer_id,
        "effect": {"type": effect_type, "params": params},
    })
    return await dispatch_patch({"ops": ops}, ctx)


async def dispatch_render(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Render the current lumenframe document to a video or image file.

    Compiles the lumenframe doc and renders it to MP4 (default) or PNG frame.
    Missing media assets degrade gracefully (skipped).

    Args:
        format: "video" (default, MP4) or "frame" (single PNG at specified index)
        frame_index: For format="frame", the frame number to render (default 0)

    Returns:
        Dict with asset_id, path, dimensions, duration (for video), etc.
    """
    if apply_layer_patch is None or compile_to_layer_stack is None:
        return {
            "applied": False,
            "error_code": "E_NOT_AVAILABLE",
            "error_message": "lumenframe compile module not available",
        }

    doc = get_lumendoc_snapshot(ctx)
    fmt = str(args.get("format", "video")).lower()
    frame_index = int(args.get("frame_index", 0))

    # Compile the document to a renderable stack
    try:
        stack = compile_to_layer_stack(doc, strict=False)
    except Exception as e:
        return {
            "applied": False,
            "error_code": "E_COMPILE",
            "error_message": f"Compile failed: {str(e)}",
        }

    # Determine output format and path
    if fmt == "frame":
        output_kind = "image"
        output_ext = ".png"
    else:
        output_kind = "video"
        output_ext = ".mp4"
        fmt = "video"

    asset_id = ctx.registry.allocate_id(output_kind)
    out_path = ctx.child_path(asset_id, output_ext)

    # Render
    try:
        if fmt == "video":
            # Render full video
            out_path_str = stack.render_to_video(str(out_path))
            width, height = stack.width, stack.height
            total_frames = stack.total_frames
            duration_sec = total_frames / stack.fps

            summary = f"lumenframe render ({width}×{height} @ {stack.fps} fps, {total_frames} frames)"
            ctx.registry.register_output(
                asset_id,
                kind=output_kind,
                path=Path(out_path_str),
                summary=summary,
            )

            return {
                "applied": True,
                "asset_id": asset_id,
                "path": str(out_path),
                "width": width,
                "height": height,
                "fps": stack.fps,
                "total_frames": total_frames,
                "duration_sec": duration_sec,
                "format": "mp4",
                "summary": summary,
            }
        else:  # frame
            # Render single frame
            frame_index = min(max(frame_index, 0), stack.total_frames - 1)
            frame_rgba = stack.render_frame(frame_index)

            # Convert to PIL Image and save as PNG
            import numpy as np
            from PIL import Image as PILImage

            # Ensure float32 [0, 1]
            frame_uint8 = np.asarray(frame_rgba * 255, dtype=np.uint8)
            img = PILImage.fromarray(frame_uint8, "RGBA")
            img.save(str(out_path))

            width, height = stack.width, stack.height
            summary = f"lumenframe frame render (frame {frame_index} of {stack.total_frames})"
            ctx.registry.register_output(
                asset_id,
                kind=output_kind,
                path=out_path,
                summary=summary,
            )

            return {
                "applied": True,
                "asset_id": asset_id,
                "path": str(out_path),
                "width": width,
                "height": height,
                "frame_index": frame_index,
                "total_frames": stack.total_frames,
                "format": "png",
                "summary": summary,
            }
    except Exception as e:
        return {
            "applied": False,
            "error_code": "E_RENDER",
            "error_message": f"Render failed: {str(e)}",
        }


async def dispatch_set_range(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Frame-native placement: set a layer's timeline range by frame bounds."""
    op = {
        "op": "set_range",
        "layer_id": str(args.get("layer_id") or ""),
        "frame_in": args.get("frame_in"),
        "frame_out": args.get("frame_out"),
    }
    return await dispatch_patch({"ops": [op]}, ctx)


async def dispatch_set_lane(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Put an existing layer on a lane/track."""
    op = {
        "op": "set_lane",
        "layer_id": str(args.get("layer_id") or ""),
        "lane": args.get("lane"),
    }
    return await dispatch_patch({"ops": [op]}, ctx)


async def dispatch_retime_segment(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Speed-change only a sub-range of a layer."""
    op: dict[str, Any] = {
        "op": "retime_segment",
        "layer_id": str(args.get("layer_id") or ""),
        "speed": args.get("speed"),
    }
    for key in ("t0", "t1", "frame0", "frame1"):
        if args.get(key) is not None:
            op[key] = args[key]
    return await dispatch_patch({"ops": [op]}, ctx)


async def dispatch_reverse(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Reverse a whole layer or only a selected sub-range."""
    op: dict[str, Any] = {
        "op": "reverse",
        "layer_id": str(args.get("layer_id") or ""),
    }
    for key in ("t0", "t1", "frame0", "frame1"):
        if args.get(key) is not None:
            op[key] = args[key]
    return await dispatch_patch({"ops": [op]}, ctx)


async def dispatch_time_remap(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Attach an explicit output-time -> source-time remap curve."""
    op: dict[str, Any] = {
        "op": "set_time_remap",
        "layer_id": str(args.get("layer_id") or ""),
        "keyframes": args.get("keyframes"),
    }
    if args.get("extrapolate") is not None:
        op["extrapolate"] = str(args["extrapolate"])
    return await dispatch_patch({"ops": [op]}, ctx)


async def dispatch_speed_ramp(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Apply a named speed-ramp preset."""
    op: dict[str, Any] = {
        "op": "speed_ramp",
        "layer_id": str(args.get("layer_id") or ""),
        "preset": str(args.get("preset") or ""),
    }
    if args.get("extrapolate") is not None:
        op["extrapolate"] = str(args["extrapolate"])
    return await dispatch_patch({"ops": [op]}, ctx)


async def dispatch_ripple_delete(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Delete a layer and close the gap on the same lane."""
    op = {
        "op": "ripple_delete",
        "layer_id": str(args.get("layer_id") or ""),
    }
    return await dispatch_patch({"ops": [op]}, ctx)


async def dispatch_merge_compositions(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Merge source composition timelines into another composition."""
    op: dict[str, Any] = {
        "op": "merge_compositions",
        "source_ids": args.get("source_ids"),
        "into_id": str(args.get("into_id") or ""),
        "mode": str(args.get("mode") or "overlay"),
    }
    if args.get("offset") is not None:
        op["offset"] = float(args["offset"])
    if args.get("keep_sources") is not None:
        op["keep_sources"] = bool(args["keep_sources"])
    return await dispatch_patch({"ops": [op]}, ctx)


async def dispatch_set_work_area(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Set or clear the canvas work area used by range preview/export defaults."""
    if normalize_doc is None:
        raise RuntimeError("lumenframe module not available")
    before = get_lumendoc_snapshot(ctx)
    doc = copy.deepcopy(before)
    canvas = doc.setdefault("canvas", {})
    clear = bool(args.get("clear", False))
    if clear:
        canvas.pop("work_area", None)
    else:
        if args.get("t_in") is None or args.get("t_out") is None:
            return {
                "applied": False,
                "error_code": "E_ARG",
                "error_message": "lumen_set_work_area needs t_in and t_out, or clear=true",
                "recovery": "fix_args",
            }
        canvas["work_area"] = {"in": float(args["t_in"]), "out": float(args["t_out"])}
    try:
        normalized = normalize_doc(doc)
    except Exception as exc:
        return {
            "applied": False,
            "error_code": getattr(exc, "code", "E_ARG"),
            "error_message": str(exc),
            "recovery": "fix_args",
        }
    if not _commit_lumendoc(
        ctx, before, normalized, label="agent lumenframe work area edit",
    ):
        return {
            "applied": False,
            "error_code": "E_PERSIST",
            "error_message": "failed to persist lumenframe work area",
            "recovery": "transient_retry",
        }
    return {
        "applied": True,
        "work_area": normalized.get("canvas", {}).get("work_area"),
    }



async def dispatch_set_expression(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Bind a time-driven expression to a layer property (e.g., opacity, rotation).

    Expressions allow properties to be animated using mathematical functions of time,
    without manually creating keyframes. Safe evaluator restricts to whitelist.

    Args:
        layer_id: Layer to bind expression to
        property: Property name (e.g., "opacity", "transform.rotation", "transform.scale_x")
        expression: Expression string (e.g., "0.5 + 0.2 * sin(time * 8)") — time in seconds

    Available bindings in expression:
        - time: Current time in seconds (frame_index / fps)
        - duration: Layer total duration in seconds

    Available functions:
        - Math: sin, cos, sqrt, abs, min, max, floor, ceil
        - Easing: linear, ease_in_quad, ease_out_quad, ease_in_out_quad,
                  ease_in_cubic, ease_out_cubic, ease_in_out_cubic
    
    Returns:
        {
            "layer_id": str,
            "property": str,
            "expression": str,
            "valid": bool,
            "summary": str
        }
    """
    from gemia.expressions import validate_expression

    if apply_layer_patch is None:
        return {
            "error_code": "E_UNAVAILABLE",
            "error_message": "lumenframe module not available",
        }

    layer_id = str(args.get("layer_id") or "").strip()
    property_name = str(args.get("property") or "").strip()
    expression_str = str(args.get("expression") or "").strip()

    if not layer_id:
        return {
            "error_code": "E_ARG",
            "error_message": "Missing required argument: layer_id",
        }
    if not property_name:
        return {
            "error_code": "E_ARG",
            "error_message": "Missing required argument: property",
        }
    if not expression_str:
        return {
            "error_code": "E_ARG",
            "error_message": "Missing required argument: expression",
        }

    # Validate expression
    is_valid, err_msg = validate_expression(expression_str)
    if not is_valid:
        return {
            "layer_id": layer_id,
            "property": property_name,
            "expression": expression_str,
            "valid": False,
            "error_code": "E_UNSAFE",
            "error_message": f"Expression validation failed: {err_msg}",
        }

    # Apply the set_expression op
    try:
        apply_lumendoc_ops(
            ctx,
            [{
                "op": "set_expression",
                "layer_id": layer_id,
                "property": property_name,
                "expression": expression_str,
            }],
            label="agent lumenframe expression edit",
        )

        return {
            "layer_id": layer_id,
            "property": property_name,
            "expression": expression_str,
            "valid": True,
            "summary": f"Bound expression to {layer_id}.{property_name}: {expression_str}",
        }
    except Exception as e:
        return {
            "layer_id": layer_id,
            "property": property_name,
            "expression": expression_str,
            "valid": False,
            "error_code": "E_PERSIST" if isinstance(e, OSError) else "E_APPLY",
            "error_message": f"Failed to apply expression: {str(e)}",
        }


def clear_lumenframe_session(session_id: str) -> None:
    """Clear the lumenframe document cache for a session.

    Call this when a session ends to clean up memory. Can be integrated into
    agent_loop_v3's session teardown if/when one is added.

    Args:
        session_id: The session identifier to clear from _DOC_CACHE.
    """
    if session_id in _DOC_CACHE:
        del _DOC_CACHE[session_id]
    _DOC_HISTORY_CACHE.pop(session_id, None)
    # The path cache is keyed by (session_id, project_id) tuples — purge those
    # too, or every closed session leaks its entries.
    for key in [k for k in _LUMENFRAME_PATH_CACHE if k[0] == session_id]:
        del _LUMENFRAME_PATH_CACHE[key]


# Dispatchers table (called by agent_loop_v3.py via DISPATCHER)
dispatch = dispatch_patch  # The main entry point
dispatch_get_lumenframe = dispatch_get
dispatch_lumen_patch = dispatch_patch
dispatch_lumen_add_layer = dispatch_add_layer
dispatch_lumen_set_transform = dispatch_set_transform
dispatch_lumen_set_opacity = dispatch_set_opacity
dispatch_lumen_delete_layer = dispatch_delete_layer
dispatch_lumen_move_layer = dispatch_move_layer
dispatch_lumen_set_visibility = dispatch_set_visibility
dispatch_lumen_select = dispatch_select
dispatch_lumen_set_mask = dispatch_set_mask
dispatch_lumen_key = dispatch_key
dispatch_lumen_render = dispatch_render
dispatch_lumen_set_range = dispatch_set_range
dispatch_lumen_set_lane = dispatch_set_lane
dispatch_lumen_retime_segment = dispatch_retime_segment
dispatch_lumen_reverse = dispatch_reverse
dispatch_lumen_time_remap = dispatch_time_remap
dispatch_lumen_speed_ramp = dispatch_speed_ramp
dispatch_lumen_ripple_delete = dispatch_ripple_delete
dispatch_lumen_merge_compositions = dispatch_merge_compositions
dispatch_lumen_set_work_area = dispatch_set_work_area


__all__ = [
    "dispatch",
    "dispatch_get",
    "dispatch_patch",
    "dispatch_add_layer",
    "dispatch_set_transform",
    "dispatch_set_opacity",
    "dispatch_delete_layer",
    "dispatch_move_layer",
    "dispatch_set_visibility",
    "dispatch_select",
    "dispatch_set_mask",
    "dispatch_key",
    "dispatch_render",
    "dispatch_set_range",
    "dispatch_set_lane",
    "dispatch_retime_segment",
    "dispatch_reverse",
    "dispatch_time_remap",
    "dispatch_speed_ramp",
    "dispatch_ripple_delete",
    "dispatch_merge_compositions",
    "dispatch_set_work_area",
    "dispatch_set_expression",
    "lumenframe_revision",
    "lumenframe_history_status",
    "lumenframe_client_op_seen",
    "get_lumendoc_snapshot",
    "apply_lumendoc_ops",
    "restore_lumendoc_history",
    "LumenframeRevisionConflict",
    "clear_lumenframe_session",
]
