"""Per-clip SegmentDocument helpers.

Segment documents are intentionally kept inside the canonical project snapshot
under ``project['segments']``.  This gives them the same append-only ProjectStore
history as the main timeline while retaining an independent segment revision and
patch log.  Reading a legacy clip never mutates the project: callers receive an
implicit one-material/one-layer/one-state document until the first creative edit.
"""
from __future__ import annotations

import copy
import uuid
from datetime import datetime, timezone
from typing import Any

from lumerai.patches import TimelinePatchError

SEGMENT_SCHEMA = "lumeri.segment"
SEGMENT_VERSION = 1


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_segment_id(clip_id: str) -> str:
    return f"seg_{clip_id}_{uuid.uuid4().hex[:8]}"


def _asset_for_clip(project: dict[str, Any], clip: dict[str, Any]) -> dict[str, Any]:
    aid = str(clip.get("asset_id") or "")
    return next(
        (a for a in project.get("assets") or [] if isinstance(a, dict) and str(a.get("id") or a.get("asset_id") or "") == aid),
        {},
    )


def implicit_segment(project: dict[str, Any], clip: dict[str, Any], *, segment_id: str | None = None) -> dict[str, Any]:
    """Return a read-only structural view for a flat clip."""
    clip_id = str(clip.get("id") or "clip")
    asset = _asset_for_clip(project, clip)
    media_kind = str(clip.get("media_kind") or asset.get("media_kind") or "video")
    duration = max(0.1, float(clip.get("duration") or 0.1))
    material_id = f"material_{clip_id}"
    layer_id = f"layer_{clip_id}"
    state_id = f"state_{clip_id}"
    return {
        "schema": SEGMENT_SCHEMA,
        "version": SEGMENT_VERSION,
        "segment_id": str(segment_id or clip.get("segment_ref") or f"implicit_{clip_id}"),
        "clip_id": clip_id,
        "persisted": False,
        "revision": 0,
        "source": {
            "asset_id": str(clip.get("asset_id") or ""),
            "media_kind": media_kind,
            "name": str(clip.get("name") or asset.get("name") or "素材"),
            "mime_type": str(asset.get("mime_type") or ""),
            "preview_src": str(asset.get("preview_src") or ""),
            "waveform_peaks": copy.deepcopy(asset.get("waveform_peaks") or []),
        },
        "timeline": [{
            "id": material_id,
            "kind": media_kind,
            "start": 0.0,
            "duration": duration,
            "source_in": float(clip.get("source_in") or 0.0),
            "source_out": float(clip.get("source_out") or duration),
            "revision": 0,
        }],
        "layers": [{
            "id": layer_id,
            "name": "素材",
            "kind": "audio" if media_kind == "audio" else "media",
            "material_id": material_id,
            "visible": True,
            "revision": 0,
            "reserved_by": None,
        }],
        "states": [{
            "id": state_id,
            "name": "默认",
            "dwell_sec": duration,
            "advance": "manual",
            "visible_layer_ids": [layer_id],
            "revision": 0,
        }],
        "reservations": {},
        "handback_required": False,
        "history": [],
        "branches": [],
    }


def persisted_segment(project: dict[str, Any], clip: dict[str, Any], *, create: bool = False) -> dict[str, Any]:
    segments = project.setdefault("segments", {})
    ref = str(clip.get("segment_ref") or "")
    if ref and isinstance(segments.get(ref), dict):
        return segments[ref]
    if not create:
        return implicit_segment(project, clip)
    segment_id = new_segment_id(str(clip.get("id") or "clip"))
    doc = implicit_segment(project, clip, segment_id=segment_id)
    doc["persisted"] = True
    segments[segment_id] = doc
    clip["segment_ref"] = segment_id
    return doc


def normalize_segment(raw: dict[str, Any], *, clip_id: str | None = None) -> dict[str, Any]:
    """Keep segment payload bounded and deterministic on every project read."""
    doc = copy.deepcopy(raw if isinstance(raw, dict) else {})
    doc["schema"] = SEGMENT_SCHEMA
    doc["version"] = SEGMENT_VERSION
    doc["segment_id"] = str(doc.get("segment_id") or "")
    doc["clip_id"] = str(doc.get("clip_id") or clip_id or "")
    doc["persisted"] = bool(doc.get("persisted", True))
    try:
        doc["revision"] = max(0, int(doc.get("revision") or 0))
    except (TypeError, ValueError):
        doc["revision"] = 0
    for key in ("source", "timeline", "layers", "states", "reservations", "history", "branches"):
        if key not in doc or not isinstance(doc[key], (dict, list)):
            doc[key] = {} if key in {"source", "reservations"} else []
    doc["handback_required"] = bool(doc.get("handback_required", False))
    return doc


def _find_entity(doc: dict[str, Any], entity_ref: str) -> tuple[str, dict[str, Any]] | None:
    ref = str(entity_ref or "")
    for key in ("timeline", "layers", "states"):
        for item in doc.get(key) or []:
            if isinstance(item, dict) and str(item.get("id") or "") == ref:
                return key, item
    return None


def _claim(doc: dict[str, Any], entity_refs: list[str], *, actor: str, client_id: str) -> None:
    reservations = doc.setdefault("reservations", {})
    actor = str(actor or "human").lower()
    for ref in entity_refs:
        ref = str(ref or "")
        if not ref:
            continue
        existing = reservations.get(ref)
        if existing and str(existing.get("owner") or "") not in {actor, ""}:
            if actor == "agent" or str(existing.get("owner") or "") == "human":
                raise TimelinePatchError("E_ENTITY_RESERVED", "这个对象正在由用户编辑")
        if actor == "human":
            reservations[ref] = {
                "owner": "human",
                "client_id": client_id,
                "claimed_at": existing.get("claimed_at") if existing else _now(),
            }


def _touch(item: dict[str, Any]) -> None:
    item["revision"] = max(0, int(item.get("revision") or 0)) + 1


def apply_segment_edit(project: dict[str, Any], op: dict[str, Any]) -> dict[str, Any]:
    """Apply one atomic segment edit to a canonical project in place."""
    clip_id = str(op.get("clip_id") or "")
    clip = next((c for c in project.get("timeline", {}).get("clips", []) if str(c.get("id") or "") == clip_id), None)
    if not isinstance(clip, dict):
        raise TimelinePatchError("E_NOT_FOUND", f"clip not found: {clip_id}")
    segment = persisted_segment(project, clip, create=True)
    expected = op.get("expected_segment_revision")
    if expected is not None:
        try:
            expected = int(expected)
        except (TypeError, ValueError):
            raise TimelinePatchError("E_BAD_ARG", "expected_segment_revision must be an integer")
        actual = int(segment.get("revision") or 0)
        if expected != actual:
            raise TimelinePatchError("E_SEGMENT_REVISION_CONFLICT", f"片段已更新到第 {actual} 版")
    client_id = str(op.get("client_op_id") or "segment-edit")
    actor = str(op.get("actor") or "human").lower()
    if actor not in {"human", "agent"}:
        actor = "human"
    entity_ref = str(op.get("entity_ref") or "")
    entity_refs = [str(x) for x in (op.get("entity_refs") or []) if str(x or "")]
    if entity_ref and entity_ref not in entity_refs:
        entity_refs.append(entity_ref)
    # Agent requests are checked immediately before the mutation, not from a
    # stale prompt snapshot.
    _claim(segment, entity_refs, actor=actor, client_id=client_id)
    action = str(op.get("edit") or op.get("action") or "set_layer")
    changed_refs = list(entity_refs)
    if action in {"set_layer", "set_object"}:
        found = _find_entity(segment, entity_ref)
        if not found or found[0] != "layers":
            raise TimelinePatchError("E_NOT_FOUND", f"图层不存在: {entity_ref}")
        changes = op.get("changes") if isinstance(op.get("changes"), dict) else {}
        allowed = {"name", "visible", "opacity", "blend_mode", "x", "y", "scale", "rotation", "kind"}
        found[1].update({str(k): copy.deepcopy(v) for k, v in changes.items() if str(k) in allowed})
        _touch(found[1])
    elif action in {"set_timeline", "set_material"}:
        found = _find_entity(segment, entity_ref)
        if not found or found[0] != "timeline":
            raise TimelinePatchError("E_NOT_FOUND", f"时间对象不存在: {entity_ref}")
        changes = op.get("changes") if isinstance(op.get("changes"), dict) else {}
        for key in ("start", "duration", "source_in", "source_out", "kind"):
            if key in changes:
                found[1][key] = copy.deepcopy(changes[key])
        if "start" in changes:
            try:
                found[1]["start"] = max(0.0, float(found[1].get("start") or 0.0))
            except (TypeError, ValueError):
                raise TimelinePatchError("E_BAD_ARG", "内部时间起点必须是数字")
        if "duration" in changes:
            try:
                found[1]["duration"] = max(0.1, float(found[1].get("duration") or 0.1))
            except (TypeError, ValueError):
                raise TimelinePatchError("E_BAD_ARG", "内部时长必须是数字")
            # The first material is the outer Clip's authoritative coverage
            # in the lazy one-material representation. Keep the main timeline
            # duration/source range aligned so preview and export cannot drift.
            timeline_items = segment.get("timeline") or []
            if timeline_items and found[1] is timeline_items[0]:
                clip["duration"] = round(found[1]["duration"], 6)
                source_in = float(clip.get("source_in") or 0.0)
                clip["source_out"] = round(source_in + found[1]["duration"], 6)
        _touch(found[1])
    elif action in {"set_state", "state_set"}:
        found = _find_entity(segment, entity_ref)
        if not found or found[0] != "states":
            raise TimelinePatchError("E_NOT_FOUND", f"状态不存在: {entity_ref}")
        changes = op.get("changes") if isinstance(op.get("changes"), dict) else {}
        for key in ("name", "dwell_sec", "advance", "visible_layer_ids"):
            if key in changes:
                found[1][key] = copy.deepcopy(changes[key])
        _touch(found[1])
    elif action in {"state_add", "state_copy"}:
        states = segment.setdefault("states", [])
        if action == "state_copy":
            source = _find_entity(segment, entity_ref)
            if not source or source[0] != "states":
                raise TimelinePatchError("E_NOT_FOUND", f"状态不存在: {entity_ref}")
            state = copy.deepcopy(source[1])
        else:
            state = {}
        state["id"] = str(op.get("new_state_id") or f"state_{uuid.uuid4().hex[:8]}")
        state.setdefault("name", "新状态")
        state.setdefault("dwell_sec", 1.0)
        state.setdefault("advance", "manual")
        state.setdefault("visible_layer_ids", [str(x.get("id")) for x in segment.get("layers") or [] if isinstance(x, dict)])
        states.append(state)
        changed_refs.append(state["id"])
    elif action == "state_delete":
        states = segment.setdefault("states", [])
        before = len(states)
        segment["states"] = [s for s in states if str(s.get("id") or "") != entity_ref]
        if len(segment["states"]) == before:
            raise TimelinePatchError("E_NOT_FOUND", f"状态不存在: {entity_ref}")
    elif action == "state_reorder":
        states = segment.setdefault("states", [])
        idx = op.get("index")
        if not isinstance(idx, int) or not (0 <= idx < len(states)):
            raise TimelinePatchError("E_BAD_ARG", "状态排序位置无效")
        found = next((s for s in states if str(s.get("id") or "") == entity_ref), None)
        if found is None:
            raise TimelinePatchError("E_NOT_FOUND", f"状态不存在: {entity_ref}")
        states.remove(found)
        states.insert(min(idx, len(states)), found)
    elif action == "save":
        segment["reservations"] = {}
        segment["handback_required"] = False
    elif action == "branch":
        branch_id = str(op.get("branch_id") or f"branch_{uuid.uuid4().hex[:8]}")
        branch = copy.deepcopy(segment)
        branch["segment_id"] = branch_id
        branch["persisted"] = True
        branch["revision"] = 0
        branch["handback_required"] = False
        segment.setdefault("branches", []).append({"branch_id": branch_id, "created_at": _now(), "source_revision": segment.get("revision", 0)})
        project.setdefault("segments", {})[branch_id] = branch
        return branch
    else:
        raise TimelinePatchError("E_BAD_ARG", f"不支持的片段操作: {action}")
    segment["revision"] = int(segment.get("revision") or 0) + 1
    if action == "save":
        segment["handback_required"] = False
    elif actor == "human":
        segment["handback_required"] = True
    segment.setdefault("history", []).append({
        "revision": segment["revision"],
        "action": action,
        "entity_refs": changed_refs,
        "actor": actor,
        "client_op_id": client_id,
        "at": _now(),
    })
    segment["history"] = segment["history"][-200:]
    return segment


def segment_manifest(project: dict[str, Any], clip: dict[str, Any]) -> dict[str, Any]:
    doc = persisted_segment(project, clip, create=False)
    return {
        "segment_id": doc.get("segment_id"),
        "clip_id": clip.get("id"),
        "persisted": bool(doc.get("persisted")),
        "revision": int(doc.get("revision") or 0),
        "timeline": [{
            "id": x.get("id"),
            "revision": x.get("revision", 0),
            "start": float(x.get("start") or 0.0),
            "duration": float(x.get("duration") or 0.0),
        } for x in doc.get("timeline") or [] if isinstance(x, dict)],
        "layers": [{"id": x.get("id"), "revision": x.get("revision", 0), "reserved_by": (doc.get("reservations") or {}).get(x.get("id"), {}).get("owner")} for x in doc.get("layers") or [] if isinstance(x, dict)],
        "states": [{
            "id": x.get("id"),
            "revision": x.get("revision", 0),
            "dwell_sec": float(x.get("dwell_sec") or 0.0),
            "advance": str(x.get("advance") or "manual"),
            "visible_layer_ids": [str(item) for item in x.get("visible_layer_ids") or []],
        } for x in doc.get("states") or [] if isinstance(x, dict)],
        "handback_required": bool(doc.get("handback_required")),
    }


__all__ = ["SEGMENT_SCHEMA", "SEGMENT_VERSION", "implicit_segment", "normalize_segment", "persisted_segment", "apply_segment_edit", "segment_manifest", "new_segment_id"]
