"""Canonical Gemia project schema and migration helpers."""
from __future__ import annotations

import hashlib
import mimetypes
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_SCHEMA = "gemia.project"
PROJECT_SCHEMA_VERSION = 1
DEFAULT_WIDTH = 1920
DEFAULT_HEIGHT = 1080
DEFAULT_FPS = 30.0
IMAGE_DURATION = 3.0

MEDIA_KINDS = {"video", "image", "audio", "text", "lottie"}
TRACK_KINDS = {"video", "overlay", "audio"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
AUDIO_EXTENSIONS = {".flac", ".wav", ".mp3", ".m4a", ".aac"}
LOTTIE_EXTENSIONS = {".json", ".lottie"}


def empty_project(*, account_id: str | None = None, title: str = "Untitled Project") -> dict[str, Any]:
    now = _utc_now()
    return {
        "schema": PROJECT_SCHEMA,
        "version": PROJECT_SCHEMA_VERSION,
        "project_id": f"project_{uuid.uuid4().hex[:12]}",
        "account_id": account_id,
        "title": title or "Untitled Project",
        "created_at": now,
        "updated_at": now,
        "assets": [],
        "timeline": {
            "fps": DEFAULT_FPS,
            "width": DEFAULT_WIDTH,
            "height": DEFAULT_HEIGHT,
            "duration": 0.0,
            "tracks": _default_tracks(),
            "clips": [],
            "markers": [],
        },
        "render_settings": {
            "format": "mp4",
            "video_codec": "h264",
            "audio_codec": "aac",
            "width": DEFAULT_WIDTH,
            "height": DEFAULT_HEIGHT,
            "fps": DEFAULT_FPS,
        },
        "ui_state": {
            "selected_clip_id": None,
            "playhead": 0.0,
            "zoom": 1.0,
            "snap_enabled": True,
        },
        "metadata": {
            "generator": "gemia",
        },
        "shotlist": empty_shotlist(),
        "quanta": empty_quanta(),
    }


# ── shotlist / storyboard IR ──────────────────────────────────────────────
# The shotlist is an outline/storyboard-driven editing plan that lives inside
# project_state so it inherits the append-only patch log (undo + audit) for
# free, exactly like timeline edits. It is orthogonal to the timeline: shots
# describe *intent* (what each beat should show, how long, what text), while
# clips are the *result* once a shot is filled with an asset and assembled.

SHOT_SOURCES = {"search", "generate", "unset"}
SHOT_STATUSES = {"draft", "filled", "placed"}
_SHOT_TRANSITIONS = {"cut", "dissolve", "wipe", "fade"}


def empty_shotlist() -> dict[str, Any]:
    return {
        "logline": "",
        "style": "",
        "target_duration_sec": None,
        "scenes": [],
    }


def _normalize_transition(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    kind = str(raw.get("kind") or "cut").strip().lower()
    if kind not in _SHOT_TRANSITIONS:
        kind = "cut"
    if kind == "cut":
        return None
    return {"kind": kind, "duration_sec": max(0.0, _float_or(raw.get("duration_sec"), 0.5))}


def _normalize_shot(raw: Any, *, scene_idx: int, shot_idx: int) -> dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    source = str(raw.get("source") or "unset").strip().lower()
    if source not in SHOT_SOURCES:
        source = "unset"
    status = str(raw.get("status") or "draft").strip().lower()
    if status not in SHOT_STATUSES:
        status = "draft"
    asset_id = _optional_str(raw.get("asset_id"))
    library_asset_id = _optional_str(raw.get("library_asset_id"))
    raw_evidence = raw.get("evidence")
    if not library_asset_id and isinstance(raw_evidence, dict):
        library_asset_id = _optional_str(raw_evidence.get("library_asset_id"))
    source_in: float | None = None
    source_out: float | None = None
    if asset_id or library_asset_id:
        try:
            candidate_in = float(raw.get("source_in"))
            candidate_out = float(raw.get("source_out"))
            if candidate_in >= 0.0 and candidate_out > candidate_in:
                source_in = round(candidate_in, 3)
                source_out = round(candidate_out, 3)
        except (TypeError, ValueError):
            pass
    evidence = (
        _normalize_shot_evidence(raw_evidence)
        if (asset_id or library_asset_id)
        else None
    )
    alternatives = _normalize_shot_alternatives(raw.get("alternatives"))
    return {
        "id": str(raw.get("id") or "") or f"s{scene_idx}_shot{shot_idx}",
        "description": str(raw.get("description") or ""),
        "duration_sec": max(0.1, _float_or(raw.get("duration_sec"), 3.0)),
        "on_screen_text": _optional_str(raw.get("on_screen_text")),
        "narration": _optional_str(raw.get("narration")),
        "mood": _optional_str(raw.get("mood")),
        "source": source,
        "search_query": _optional_str(raw.get("search_query")),
        "asset_id": asset_id,
        "library_asset_id": library_asset_id,
        "source_in": source_in,
        "source_out": source_out,
        "evidence": evidence,
        "alternatives": alternatives,
        "clip_id": _optional_str(raw.get("clip_id")),
        "transition_after": _normalize_transition(raw.get("transition_after")),
        "status": status,
        "notes": _optional_str(raw.get("notes")),
    }


def _normalize_shot_evidence(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    evidence_id = str(raw.get("evidence_id") or "").strip()
    annotation_id = str(raw.get("annotation_id") or "").strip()
    library_asset_id = str(raw.get("library_asset_id") or "").strip()
    if not evidence_id or not annotation_id or not library_asset_id:
        return None
    confidence = raw.get("confidence")
    score = raw.get("score")
    evidence: dict[str, Any] = {
        "evidence_id": evidence_id,
        "library_asset_id": library_asset_id,
        "annotation_id": annotation_id,
        "label": str(raw.get("label") or "")[:200],
        "note": str(raw.get("note") or "")[:5000],
        "tags": [str(item)[:60] for item in raw.get("tags") or []][:30],
        "category": str(raw.get("category") or "")[:80],
        "source": str(raw.get("source") or "user")[:32],
        "confidence": (
            round(max(0.0, min(float(confidence), 1.0)), 3)
            if isinstance(confidence, (int, float))
            else None
        ),
        "matched_terms": [str(item)[:80] for item in raw.get("matched_terms") or []][:20],
        "score": round(float(score), 6) if isinstance(score, (int, float)) else 0.0,
        "score_components": _normalize_json_dict(raw.get("score_components")),
        "ranking_reasons": [str(item)[:200] for item in raw.get("ranking_reasons") or []][:10],
        "query": str(raw.get("query") or "")[:500],
        "decision": str(raw.get("decision") or "observe")[:32],
        "decision_control_annotation_id": _optional_str(
            raw.get("decision_control_annotation_id")
        ),
        "authority_tier": max(0, min(int(_float_or(raw.get("authority_tier"), 0.0)), 9)),
        "algorithm_version": str(raw.get("algorithm_version") or "")[:80],
        "metadata": _normalize_json_dict(raw.get("metadata")),
        "provenance": _normalize_json_dict(raw.get("provenance")),
        "created_at": str(raw.get("created_at") or "")[:80],
        "updated_at": str(raw.get("updated_at") or "")[:80],
    }
    for key in ("start_sec", "end_sec", "duration_sec", "match_rank", "raw_score"):
        value = raw.get(key)
        if isinstance(value, (int, float)):
            evidence[key] = round(float(value), 6)
    return evidence


def _normalize_shot_alternatives(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    alternatives: list[dict[str, Any]] = []
    for item in raw[:3]:
        normalized = _normalize_shot_evidence(item)
        if normalized is None:
            continue
        normalized["reason_not_selected"] = str(
            item.get("reason_not_selected") or "lower-ranked candidate"
        )[:240]
        alternatives.append(normalized)
    return alternatives


def _normalize_json_dict(raw: Any, *, _depth: int = 0) -> dict[str, Any]:
    if not isinstance(raw, dict) or _depth >= 4:
        return {}
    out: dict[str, Any] = {}
    for key, value in list(raw.items())[:40]:
        clean_key = str(key)[:100]
        if isinstance(value, dict):
            out[clean_key] = _normalize_json_dict(value, _depth=_depth + 1)
        elif isinstance(value, list):
            clean_list: list[Any] = []
            for item in value[:40]:
                if isinstance(item, dict):
                    clean_list.append(_normalize_json_dict(item, _depth=_depth + 1))
                elif isinstance(item, (str, int, float, bool)) or item is None:
                    clean_list.append(item if not isinstance(item, str) else item[:500])
            out[clean_key] = clean_list
        elif isinstance(value, (str, int, float, bool)) or value is None:
            out[clean_key] = value if not isinstance(value, str) else value[:1000]
    return out


def _normalize_scene(raw: Any, *, scene_idx: int) -> dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    shots_raw = raw.get("shots") if isinstance(raw.get("shots"), list) else []
    shots = [
        _normalize_shot(shot, scene_idx=scene_idx, shot_idx=i)
        for i, shot in enumerate(shots_raw)
    ]
    return {
        "id": str(raw.get("id") or "") or f"scene{scene_idx}",
        "title": str(raw.get("title") or ""),
        "shots": shots,
    }


def normalize_shotlist(raw: Any) -> dict[str, Any]:
    """Coerce a (possibly partial, model-authored) shotlist into canonical shape.

    Never raises: unknown/garbage entries are dropped, ids are backfilled, and
    numeric/enum fields are clamped so a half-formed draft still round-trips.
    """
    if not isinstance(raw, dict):
        return empty_shotlist()
    scenes_raw = raw.get("scenes") if isinstance(raw.get("scenes"), list) else []
    scenes = [_normalize_scene(scene, scene_idx=i) for i, scene in enumerate(scenes_raw)]
    target = raw.get("target_duration_sec")
    return {
        "logline": str(raw.get("logline") or ""),
        "style": str(raw.get("style") or ""),
        "target_duration_sec": (
            round(_float_or(target, 0.0), 3) if isinstance(target, (int, float)) else None
        ),
        "scenes": scenes,
    }


def iter_shots(shotlist: dict[str, Any]):
    """Yield ``(scene, shot)`` pairs in scene/shot order for a normalized shotlist."""
    for scene in (shotlist or {}).get("scenes") or []:
        if not isinstance(scene, dict):
            continue
        for shot in scene.get("shots") or []:
            if isinstance(shot, dict):
                yield scene, shot


# ── quanta IR ───────────────────────────────────────────────────────────────
# The quanta is a discrete video — one ordered state tree (quanta-kernel-plan
# §2) living inside project_state next to the shotlist, so every mutation
# inherits the append-only patch log (undo + audit) for free. Node kinds are
# DERIVED from structure, never stored: a node declaring ``blocks`` is a
# content scope (geometry solves once per scope); its children are state
# leaves (discrete render states); everything above is a group. The v1 flat
# ``{slides, default_path}`` shape stays accepted forever as authoring sugar
# and lifts deterministically through ``gemia.quanta.traverse.lift_flat_quanta``.
#
# Normalization here is STRUCTURE-tolerant only: missing fields are
# backfilled, garbage entries are dropped (but never a whole node), ids are
# auto-filled. REFERENCE integrity (duplicate ids, dangling link targets,
# hotspot blocks, dwell_sec > 0, nested content scopes, link mounts on
# groups) is deliberately NOT enforced here — lumerai.patches validates it
# strictly at patch time so ProjectStore.load() can never raise on stored
# state. To keep that split honest, normalize deliberately PRESERVES the two
# shapes validation must reject instead of silently stripping them: a state
# child that authored ``blocks`` keeps them (nested-content rejection), and a
# group that authored ``links`` keeps them (link-mount rejection).

QUANTA_VERSION = 2
QUANTA_BLOCK_KINDS = {"text", "stat", "image", "shape", "group"}
QUANTA_DEFAULT_DWELL = 3.0
QUANTA_ADVANCE_MODES = {"wait", "auto"}
_QUANTA_TRANSITIONS = {"cut", "fade"}  # v1 = cut + single-sided fade (spec §3.4)


def empty_quanta() -> dict[str, Any]:
    return {
        "version": QUANTA_VERSION,
        "theme": {"tokens": {}, "mood": "", "aspect": "16:9"},
        "root": {"id": "root", "children": []},
    }


def _normalize_quanta_block(
    raw: Any,
    *,
    block_path: tuple[int, ...] = (1,),
    _ancestors: frozenset[int] = frozenset(),
) -> dict[str, Any] | None:
    """Normalize one semantic content block, including nested groups.

    Every accepted block receives a stable id. Model-authored ids survive;
    otherwise the source position becomes ``blk_1``, ``blk_1_2``, and so on.
    Unknown kinds/keys are dropped and cyclic/depth-hostile Python input is
    truncated, keeping the public ``normalize_quanta`` contract never-raises for
    JSON-shaped input.
    """
    if not isinstance(raw, dict):
        return None
    kind = str(raw.get("kind") or "").strip().lower()
    if kind not in QUANTA_BLOCK_KINDS:
        return None
    fallback_id = "blk_" + "_".join(str(part) for part in block_path)
    block: dict[str, Any] = {
        "id": str(raw.get("id") or "").strip() or fallback_id,
        "kind": kind,
    }
    role = _optional_str(raw.get("role"))
    if role:
        block["role"] = role
    if kind == "text":
        block["text"] = str(raw.get("text") or "")
        bullets = raw.get("bullets") if isinstance(raw.get("bullets"), list) else []
        block["bullets"] = [str(item) for item in bullets if str(item or "")]
        style_token = _optional_str(raw.get("style_token"))
        if style_token:
            block["style_token"] = style_token
    elif kind == "stat":
        block["value"] = str(raw.get("value") or "")
        block["label"] = str(raw.get("label") or "")
    elif kind == "image":
        block["asset_id"] = _optional_str(raw.get("asset_id"))
        block["source"] = _optional_str(raw.get("source"))
        query = _optional_str(raw.get("query"))
        if query:
            block["query"] = query
    elif kind == "shape":
        block["shape"] = str(raw.get("shape") or "rect")
        fill_token = _optional_str(raw.get("fill_token"))
        if fill_token:
            block["fill_token"] = fill_token
    elif kind == "group":
        children_raw = raw.get("children") if isinstance(raw.get("children"), list) else []
        raw_identity = id(raw)
        if raw_identity in _ancestors or len(block_path) >= 32:
            children_raw = []
        ancestors = _ancestors | {raw_identity}
        children = [
            _normalize_quanta_block(
                child,
                block_path=(*block_path, child_idx + 1),
                _ancestors=ancestors,
            )
            for child_idx, child in enumerate(children_raw)
        ]
        block["children"] = [child for child in children if child is not None]
    return block


def _quanta_leaf_block_ids(blocks: Any) -> list[str]:
    """Return renderable (non-group) block ids in deterministic paint order."""
    ids: list[str] = []
    if not isinstance(blocks, list):
        return ids
    for block in blocks:
        if not isinstance(block, dict):
            continue
        if block.get("kind") == "group":
            ids.extend(_quanta_leaf_block_ids(block.get("children")))
        else:
            block_id = str(block.get("id") or "")
            if block_id:
                ids.append(block_id)
    return ids


def _normalize_link(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    trigger = str(raw.get("trigger") or "").strip()
    target = str(raw.get("target") or "").strip()
    if not trigger or not target:
        return None
    if target.startswith("slide:"):  # v1 grammar lifts to the v2 spelling
        target = "quantum:" + target[len("slide:"):]
    return {"trigger": trigger, "target": target}


def _normalize_state(
    raw: Any,
    *,
    state_idx: int,
    scope_id: str,
    leaf_block_ids: list[str],
    used_ids: set[str] | None = None,
) -> dict[str, Any]:
    """One discrete render state (v1's build). ``advance`` is presentation
    semantics: wait = hold until interaction, auto = advance after dwell."""
    raw = raw if isinstance(raw, dict) else {}
    visible_raw = raw.get("visible_block_ids")
    # v1 builds authored before visibility existed meant "the whole scope" at
    # every state. An explicit [] is meaningful and must remain an empty first
    # state; only a missing/wrong-type value backfills.
    visible = (
        [str(item or "").strip() for item in visible_raw]
        if isinstance(visible_raw, list)
        else list(leaf_block_ids)
    )
    advance = str(raw.get("advance") or "wait").strip().lower()
    authored = str(raw.get("id") or "")
    state_id = authored or f"b{state_idx + 1}"
    # Same rule as the flat lift: state ids are document-unique by scope
    # prefix, and already-prefixed ids never re-prefix (round-trip stable).
    if not state_id.startswith(f"{scope_id}_"):
        state_id = f"{scope_id}_{state_id}"
    if not authored and used_ids is not None and state_id in used_ids:
        # Backfilled ids dodge occupied slots (an id-less state inserted
        # among authored siblings must not collide); AUTHORED duplicates are
        # kept verbatim so strict validation rejects them loudly.
        bump = state_idx + 1
        while f"{scope_id}_b{bump}" in used_ids:
            bump += 1
        state_id = f"{scope_id}_b{bump}"
    state: dict[str, Any] = {
        "id": state_id,
        # A missing/garbage dwell backfills to a sane default; an EXPLICIT
        # numeric value is preserved even when <= 0 so strict validation can
        # reject it.
        "dwell_sec": _float_or(raw.get("dwell_sec"), QUANTA_DEFAULT_DWELL),
        "visible_block_ids": visible,
        "advance": advance if advance in QUANTA_ADVANCE_MODES else "wait",
        "hidden": bool(raw.get("hidden")),
    }
    if isinstance(raw.get("blocks"), list):
        # Nested content under a content scope: preserved verbatim so strict
        # validation can reject it loudly instead of normalize eating it.
        state["blocks"] = list(raw["blocks"])
    return state


def _normalize_content_node(raw: dict[str, Any], *, fallback_id: str) -> dict[str, Any]:
    """A content scope (v1's slide): blocks + state children."""
    blocks_raw = raw.get("blocks") if isinstance(raw.get("blocks"), list) else []
    blocks = [
        _normalize_quanta_block(block, block_path=(block_idx + 1,))
        for block_idx, block in enumerate(blocks_raw)
    ]
    blocks = [block for block in blocks if block is not None]
    leaf_block_ids = _quanta_leaf_block_ids(blocks)
    node_id = str(raw.get("id") or "") or fallback_id
    # ``children`` is the tree spelling; ``builds`` stays accepted as v1 sugar.
    # An op that sets ``builds`` explicitly must pop ``children`` first.
    states_raw = raw.get("children")
    if not isinstance(states_raw, list):
        states_raw = raw.get("builds") if isinstance(raw.get("builds"), list) else []
    states = []
    used_state_ids: set[str] = {
        str(state.get("id") or "")
        for state in states_raw
        if isinstance(state, dict) and str(state.get("id") or "")
    }
    used_state_ids |= {
        f"{node_id}_{sid}" for sid in list(used_state_ids)
        if not sid.startswith(f"{node_id}_")
    }
    for i, state in enumerate(states_raw):
        if not isinstance(state, dict):
            continue
        normalized_state = _normalize_state(
            state, state_idx=i, scope_id=node_id,
            leaf_block_ids=leaf_block_ids, used_ids=used_state_ids,
        )
        used_state_ids.add(normalized_state["id"])
        states.append(normalized_state)
    if not states:  # every scope has at least its full render state
        states = [{
            "id": f"{node_id}_b1",
            "dwell_sec": QUANTA_DEFAULT_DWELL,
            "visible_block_ids": list(leaf_block_ids),
            "advance": "wait",
            "hidden": False,
        }]
    links_raw = raw.get("links") if isinstance(raw.get("links"), list) else []
    links = [_normalize_link(link) for link in links_raw]
    transition_raw = raw.get("transition") if isinstance(raw.get("transition"), dict) else {}
    kind = str(transition_raw.get("kind") or "cut").strip().lower()
    if kind not in _QUANTA_TRANSITIONS:
        kind = "cut"
    return {
        "id": node_id,
        "layout": str(raw.get("layout") or "content"),
        "title": str(raw.get("title") or ""),
        "blocks": blocks,
        "notes": str(raw.get("notes") or ""),
        "mood_override": _optional_str(raw.get("mood_override")),
        "links": [link for link in links if link is not None],
        "transition": {"kind": kind},
        "hidden": bool(raw.get("hidden")),
        "children": states,
    }


def _normalize_group_node(
    raw: dict[str, Any],
    *,
    fallback_id: str,
    node_path: tuple[int, ...],
    _ancestors: frozenset[int],
) -> dict[str, Any]:
    group: dict[str, Any] = {
        "id": str(raw.get("id") or "") or fallback_id,
        "title": str(raw.get("title") or ""),
        "hidden": bool(raw.get("hidden")),
        "children": _normalize_quanta_children(
            raw.get("children"), node_path=node_path, _ancestors=_ancestors
        ),
    }
    if isinstance(raw.get("links"), list):
        # Groups cannot mount interaction edges in v1 — preserved verbatim so
        # strict validation rejects loudly instead of normalize eating them.
        group["links"] = [
            link
            for link in (_normalize_link(item) for item in raw["links"])
            if link is not None
        ]
    return group


def _normalize_quanta_children(
    raw_children: Any,
    *,
    node_path: tuple[int, ...],
    _ancestors: frozenset[int],
) -> list[dict[str, Any]]:
    if not isinstance(raw_children, list):
        return []
    if len(node_path) >= 32:
        return []
    out: list[dict[str, Any]] = []
    for child_idx, child in enumerate(raw_children):
        if not isinstance(child, dict):
            continue
        identity = id(child)
        if identity in _ancestors:
            continue
        child_path = (*node_path, child_idx + 1)
        fallback_id = "q" + "_".join(str(part) for part in child_path)
        if isinstance(child.get("blocks"), list):
            out.append(_normalize_content_node(child, fallback_id=fallback_id))
        else:
            out.append(_normalize_group_node(
                child,
                fallback_id=fallback_id,
                node_path=child_path,
                _ancestors=_ancestors | {identity},
            ))
    return out


def _normalize_slide(raw: Any, *, slide_idx: int) -> dict[str, Any]:
    """Normalize one v1 flat slide (kept as the lift sugar's front half)."""
    raw = raw if isinstance(raw, dict) else {}
    node = _normalize_content_node(dict(raw), fallback_id=f"s{slide_idx + 1}")
    builds = [
        {
            "id": state["id"],
            "dwell_sec": state["dwell_sec"],
            "visible_block_ids": state["visible_block_ids"],
            "advance": state["advance"],
        }
        for state in node.pop("children")
    ]
    node.pop("hidden", None)
    hidden = bool(raw.get("hidden")) if isinstance(raw, dict) else False
    slide = {**node, "builds": builds}
    if hidden:
        slide["hidden"] = True
    return slide


def normalize_quanta(raw: Any) -> dict[str, Any]:
    """Coerce a (possibly partial, model-authored) quanta into the canonical
    v2 state tree. Never raises — mirror of ``normalize_shotlist``.

    Accepts either the v2 tree (``root``) or the v1 flat authoring sugar
    (``slides`` [+ ``default_path``], which orders the lifted scopes and is
    then dropped — DFS leaf order IS the default path in v2).
    """
    if not isinstance(raw, dict):
        return empty_quanta()
    theme_raw = raw.get("theme") if isinstance(raw.get("theme"), dict) else {}
    tokens = theme_raw.get("tokens") if isinstance(theme_raw.get("tokens"), dict) else {}
    theme = {
        "tokens": dict(tokens),
        "mood": str(theme_raw.get("mood") or ""),
        "aspect": str(theme_raw.get("aspect") or "16:9"),
    }
    root_raw = raw.get("root")
    if isinstance(root_raw, dict):
        return {
            "version": QUANTA_VERSION,
            "theme": theme,
            "root": {
                "id": "root",
                "children": _normalize_quanta_children(
                    root_raw.get("children"),
                    node_path=(),
                    _ancestors=frozenset({id(root_raw)}),
                ),
            },
        }
    # v1 flat sugar: normalize slides first, then lift deterministically.
    from gemia.quanta.traverse import lift_flat_quanta  # deferred: keeps model import light

    slides_raw = raw.get("slides") if isinstance(raw.get("slides"), list) else []
    slides = [_normalize_slide(slide, slide_idx=i) for i, slide in enumerate(slides_raw)]
    path_raw = raw.get("default_path") if isinstance(raw.get("default_path"), list) else []
    default_path = [str(item) for item in path_raw if str(item or "")]
    lifted = lift_flat_quanta({
        "version": 1,
        "theme": theme,
        "slides": slides,
        "default_path": default_path,
    })
    # Lift output is canonical except for state-id backfill spelling; one
    # more tree pass keeps ``normalize(normalize(x)) == normalize(x)`` exact.
    return normalize_quanta(lifted)


def normalize_project(
    project: dict[str, Any] | None = None,
    *,
    project_state: dict[str, Any] | None = None,
    account_id: str | None = None,
) -> dict[str, Any]:
    """Return a Project Schema v1 payload from canonical or legacy input."""
    if is_canonical_project(project):
        return _normalize_canonical_project(project or {}, account_id=account_id)
    return _project_from_legacy_state(project_state or project or {}, account_id=account_id)


def is_canonical_project(value: Any) -> bool:
    return isinstance(value, dict) and value.get("schema") == PROJECT_SCHEMA and isinstance(value.get("timeline"), dict)


def clip_count(project: Any) -> int:
    if is_canonical_project(project):
        clips = (project.get("timeline") or {}).get("clips")
        return len(clips) if isinstance(clips, list) else 0
    if isinstance(project, dict):
        clips = project.get("clips")
        return len(clips) if isinstance(clips, list) else 0
    return 0


def legacy_project_state_from_project(project: dict[str, Any]) -> dict[str, Any]:
    """Return the UI's current flat ProjectState shape from a canonical project."""
    normalized = normalize_project(project)
    timeline = normalized["timeline"]
    assets = {item.get("id"): item for item in normalized["assets"] if isinstance(item, dict)}
    clips: list[dict[str, Any]] = []
    for item in timeline.get("clips") or []:
        if not isinstance(item, dict):
            continue
        asset = assets.get(item.get("asset_id")) or {}
        media_kind = str(item.get("media_kind") or asset.get("media_kind") or "video")
        duration = _float_or(item.get("duration"), IMAGE_DURATION if media_kind == "image" else 0.1)
        source_in = _float_or(item.get("source_in"), 0.0)
        source_out = _float_or(item.get("source_out"), source_in + duration)
        clips.append(
            {
                "id": str(item.get("id") or f"clip_{len(clips)}"),
                "assetId": str(item.get("asset_id") or asset.get("asset_id") or asset.get("id") or ""),
                "trackId": str(item.get("track_id") or _default_track_id_for_media_kind(media_kind)),
                "mediaKind": media_kind,
                "mimeType": str(asset.get("mime_type") or ""),
                "name": str(item.get("name") or asset.get("name") or "media"),
                "serverPath": str(asset.get("source_path") or ""),
                "previewSrc": str(asset.get("preview_src") or ""),
                "duration": duration,
                "inPoint": source_in,
                "outPoint": source_out,
                "keep": bool(item.get("enabled", True)),
                "summary": item.get("summary") if isinstance(item.get("summary"), dict) else None,
                "thumbnailSrc": (item.get("thumbnails") or [None])[0],
                "thumbnailStrip": item.get("thumbnails") if isinstance(item.get("thumbnails"), list) else [],
                "waveformPeaks": item.get("waveform_peaks") if isinstance(item.get("waveform_peaks"), list) else [],
                "metadata": asset.get("metadata") if isinstance(asset.get("metadata"), dict) else {},
                "transitionAfter": item.get("transition_after") if isinstance(item.get("transition_after"), dict) else None,
                "effects": item.get("effects") if isinstance(item.get("effects"), dict) else _default_effects(),
                "provenance": item.get("provenance") if isinstance(item.get("provenance"), dict) else None,
            }
        )
    ui = normalized.get("ui_state") if isinstance(normalized.get("ui_state"), dict) else {}
    return {
        "projectId": normalized["project_id"],
        "title": normalized["title"],
        "createdAt": normalized["created_at"],
        "updatedAt": normalized["updated_at"],
        "clips": clips,
        "selectedClipId": ui.get("selected_clip_id") if isinstance(ui.get("selected_clip_id"), str) else None,
        "playhead": _float_or(ui.get("playhead"), 0.0),
        "zoom": _float_or(ui.get("zoom"), 1.0),
        "snapEnabled": bool(ui.get("snap_enabled", True)),
    }


def _project_from_legacy_state(project_state: dict[str, Any], *, account_id: str | None) -> dict[str, Any]:
    project = empty_project(account_id=account_id, title=_title_from_legacy_state(project_state))
    if isinstance(project_state, dict):
        if project_state.get("projectId"):
            project["project_id"] = str(project_state["projectId"])
        if project_state.get("title"):
            project["title"] = str(project_state["title"])[:120]
        if project_state.get("createdAt"):
            project["created_at"] = str(project_state["createdAt"])
        if project_state.get("updatedAt"):
            project["updated_at"] = str(project_state["updatedAt"])
    clips = project_state.get("clips") if isinstance(project_state, dict) else []
    if not isinstance(clips, list):
        clips = []
    assets_by_key: dict[str, dict[str, Any]] = {}
    timeline_clips: list[dict[str, Any]] = []
    cursor = 0.0
    for raw_clip in clips:
        if not isinstance(raw_clip, dict):
            continue
        asset = _asset_from_clip(raw_clip)
        asset_key = str(asset.get("source_path") or asset.get("id"))
        if asset_key not in assets_by_key:
            assets_by_key[asset_key] = asset
        else:
            asset = assets_by_key[asset_key]
        clip = _timeline_clip_from_legacy(raw_clip, asset, start=cursor)
        cursor += _float_or(clip.get("duration"), 0.1)
        timeline_clips.append(clip)
    project["assets"] = list(assets_by_key.values())
    project["timeline"]["clips"] = timeline_clips
    project["timeline"]["duration"] = round(cursor, 6)
    project["ui_state"] = {
        "selected_clip_id": _optional_str(project_state.get("selectedClipId")) if isinstance(project_state, dict) else None,
        "playhead": _float_or(project_state.get("playhead") if isinstance(project_state, dict) else 0.0, 0.0),
        "zoom": _float_or(project_state.get("zoom") if isinstance(project_state, dict) else 1.0, 1.0),
        "snap_enabled": bool(project_state.get("snapEnabled", True)) if isinstance(project_state, dict) else True,
    }
    return project


def _normalize_canonical_project(project: dict[str, Any], *, account_id: str | None) -> dict[str, Any]:
    normalized = empty_project(account_id=account_id, title=str(project.get("title") or "Untitled Project"))
    normalized["project_id"] = str(project.get("project_id") or normalized["project_id"])
    normalized["created_at"] = str(project.get("created_at") or normalized["created_at"])
    normalized["updated_at"] = _utc_now()
    if project.get("account_id") and account_id is None:
        normalized["account_id"] = str(project.get("account_id"))

    assets: list[dict[str, Any]] = []
    asset_ids: set[str] = set()
    for raw_asset in project.get("assets") or []:
        if not isinstance(raw_asset, dict):
            continue
        asset = _normalize_asset(raw_asset)
        if asset["id"] in asset_ids:
            continue
        asset_ids.add(asset["id"])
        assets.append(asset)
    normalized["assets"] = assets

    raw_timeline = project.get("timeline") if isinstance(project.get("timeline"), dict) else {}
    normalized["timeline"].update(
        {
            "fps": _float_or(raw_timeline.get("fps"), DEFAULT_FPS),
            "width": int(_float_or(raw_timeline.get("width"), DEFAULT_WIDTH)),
            "height": int(_float_or(raw_timeline.get("height"), DEFAULT_HEIGHT)),
            "tracks": _normalize_tracks(raw_timeline.get("tracks")),
            "markers": raw_timeline.get("markers") if isinstance(raw_timeline.get("markers"), list) else [],
        }
    )
    clips: list[dict[str, Any]] = []
    end_time = 0.0
    for raw_clip in raw_timeline.get("clips") or []:
        if not isinstance(raw_clip, dict):
            continue
        clip = _normalize_timeline_clip(raw_clip)
        clips.append(clip)
        end_time = max(end_time, _float_or(clip.get("start"), 0.0) + _float_or(clip.get("duration"), 0.0))
    normalized["timeline"]["clips"] = clips
    normalized["timeline"]["duration"] = round(end_time, 6)

    raw_render = project.get("render_settings") if isinstance(project.get("render_settings"), dict) else {}
    normalized["render_settings"].update({key: value for key, value in raw_render.items() if key in normalized["render_settings"]})

    ui = project.get("ui_state") if isinstance(project.get("ui_state"), dict) else {}
    normalized["ui_state"] = {
        "selected_clip_id": _optional_str(ui.get("selected_clip_id")),
        "playhead": _float_or(ui.get("playhead"), 0.0),
        "zoom": _float_or(ui.get("zoom"), 1.0),
        "snap_enabled": bool(ui.get("snap_enabled", True)),
    }
    metadata = project.get("metadata") if isinstance(project.get("metadata"), dict) else {}
    normalized["metadata"] = {**normalized["metadata"], **metadata}
    normalized["shotlist"] = normalize_shotlist(project.get("shotlist"))
    # Pass the quanta through explicitly: normalize rebuilds from empty_project()
    # and copies known keys, so without this line ProjectStore.load() would
    # silently strip project_state.quanta on every read (spec §2.4).
    normalized["quanta"] = normalize_quanta(project.get("quanta"))
    return normalized


def _asset_from_clip(clip: dict[str, Any]) -> dict[str, Any]:
    source_path = str(clip.get("serverPath") or clip.get("server_path") or clip.get("path") or "")
    name = str(clip.get("name") or Path(source_path).name or "media")
    metadata = clip.get("metadata") if isinstance(clip.get("metadata"), dict) else {}
    media_kind = _media_kind_for_clip(clip, metadata)
    mime_type = str(clip.get("mimeType") or metadata.get("mime_type") or mimetypes.guess_type(name)[0] or "")
    duration = IMAGE_DURATION if media_kind == "image" else _float_or(clip.get("duration") or metadata.get("duration"), 0.0)
    asset_id = str(clip.get("assetId") or clip.get("asset_id") or _asset_id(source_path or name))
    return {
        "id": asset_id,
        "asset_id": asset_id,
        "name": name,
        "media_kind": media_kind,
        "mime_type": mime_type,
        "source_path": source_path,
        "preview_src": _optional_str(clip.get("previewSrc")),
        "duration": duration,
        "metadata": metadata,
        "created_at": _utc_now(),
    }


def _normalize_asset(asset: dict[str, Any]) -> dict[str, Any]:
    name = str(asset.get("name") or Path(str(asset.get("source_path") or "")).name or "media")
    source_path = str(asset.get("source_path") or "")
    media_kind = str(asset.get("media_kind") or _media_kind_for_name(name, str(asset.get("mime_type") or "")))
    if media_kind not in MEDIA_KINDS:
        media_kind = "video"
    metadata = asset.get("metadata") if isinstance(asset.get("metadata"), dict) else {}
    asset_id = str(asset.get("asset_id") or asset.get("id") or _asset_id(source_path or name))
    return {
        "id": asset_id,
        "asset_id": asset_id,
        "name": name,
        "media_kind": media_kind,
        "mime_type": str(asset.get("mime_type") or metadata.get("mime_type") or mimetypes.guess_type(name)[0] or ""),
        "source_path": source_path,
        "preview_src": _optional_str(asset.get("preview_src")),
        "duration": IMAGE_DURATION if media_kind == "image" else _float_or(asset.get("duration") or metadata.get("duration"), 0.0),
        "metadata": metadata,
        "thumbnail_src": _optional_str(asset.get("thumbnail_src")),
        "thumbnails": asset.get("thumbnails") if isinstance(asset.get("thumbnails"), list) else [],
        "waveform_peaks": asset.get("waveform_peaks") if isinstance(asset.get("waveform_peaks"), list) else [],
        "fingerprint": _optional_str(asset.get("fingerprint")),
        "status": str(asset.get("status") or "ready"),
        "created_at": str(asset.get("created_at") or _utc_now()),
    }


def _timeline_clip_from_legacy(clip: dict[str, Any], asset: dict[str, Any], *, start: float) -> dict[str, Any]:
    media_kind = str(asset.get("media_kind") or "video")
    source_in = _float_or(clip.get("inPoint"), 0.0)
    real_duration = _float_or(
        clip.get("duration")
        or asset.get("duration")
        or (asset.get("metadata") if isinstance(asset.get("metadata"), dict) else {}).get("duration"),
        0.0,
    )
    raw_source_out = _float_or(
        clip.get("outPoint"),
        IMAGE_DURATION if media_kind == "image" else (source_in + (real_duration or 0.1)),
    )
    is_trimmed = bool(clip.get("trimmed")) or source_in > 0.01
    if is_trimmed:
        source_out = max(source_in + 0.1, raw_source_out)
        duration = max(0.1, source_out - source_in)
    else:
        duration = max(0.1, real_duration or max(0.1, raw_source_out - source_in))
        source_out = source_in + duration
    if media_kind == "image":
        duration = IMAGE_DURATION
        source_in = 0.0
        source_out = IMAGE_DURATION
    elif media_kind == "lottie":
        duration = max(0.1, real_duration or max(0.1, raw_source_out - source_in))
        source_out = source_in + duration
    return {
        "id": str(clip.get("id") or f"clip_{uuid.uuid4().hex[:8]}"),
        "asset_id": asset["id"],
        "track_id": str(clip.get("trackId") or _default_track_id_for_media_kind(media_kind)),
        "name": str(clip.get("name") or asset.get("name") or "media"),
        "media_kind": media_kind,
        "start": round(start, 6),
        "duration": round(duration, 6),
        "source_in": round(source_in, 6),
        "source_out": round(source_out, 6),
        "enabled": bool(clip.get("keep", True)),
        "effects": clip.get("effects") if isinstance(clip.get("effects"), dict) else _default_effects(),
        "transition_after": clip.get("transitionAfter") if isinstance(clip.get("transitionAfter"), dict) else None,
        "summary": clip.get("summary") if isinstance(clip.get("summary"), dict) else None,
        "thumbnails": clip.get("thumbnailStrip") if isinstance(clip.get("thumbnailStrip"), list) else [],
        "waveform_peaks": clip.get("waveformPeaks") if isinstance(clip.get("waveformPeaks"), list) else [],
        "keyframes": clip.get("keyframes") if isinstance(clip.get("keyframes"), list) else [],
        "text_config": None,
        "provenance": clip.get("provenance") if isinstance(clip.get("provenance"), dict) else None,
    }


def _normalize_timeline_clip(clip: dict[str, Any]) -> dict[str, Any]:
    media_kind = str(clip.get("media_kind") or "video")
    if media_kind not in MEDIA_KINDS:
        media_kind = "video"
    if media_kind == "image":
        # Respect an explicit positive duration; only fall back to the legacy
        # forced IMAGE_DURATION when duration is missing or <= 0.
        explicit = _float_or(clip.get("duration"), 0.0)
        duration = explicit if explicit > 0 else IMAGE_DURATION
        source_in = max(_float_or(clip.get("source_in"), 0.0), 0.0)
        source_out = _float_or(clip.get("source_out"), 0.0)
        if source_out <= source_in + 1e-3:
            source_out = source_in + duration
    elif media_kind == "text":
        # Text clips carry no source media: duration defaults like images and
        # the source range is pinned to [0, duration].
        explicit = _float_or(clip.get("duration"), 0.0)
        duration = explicit if explicit > 0 else IMAGE_DURATION
        source_in = 0.0
        source_out = duration
    elif media_kind == "lottie":
        duration = max(_float_or(clip.get("duration"), 0.1), 0.1)
        source_in = max(_float_or(clip.get("source_in"), 0.0), 0.0)
        source_out = _float_or(clip.get("source_out"), source_in + duration)
        if source_out <= source_in + 1e-3:
            source_out = source_in + duration
    else:
        duration = max(_float_or(clip.get("duration"), 0.1), 0.1)
        source_in = _float_or(clip.get("source_in"), 0.0)
        source_out = _float_or(clip.get("source_out"), source_in + duration)
        if source_in <= 0.01 and source_out < source_in + duration - 0.01:
            source_out = source_in + duration
    return {
        "id": str(clip.get("id") or f"clip_{uuid.uuid4().hex[:8]}"),
        "asset_id": str(clip.get("asset_id") or ""),
        "track_id": str(clip.get("track_id") or _default_track_id_for_media_kind(media_kind)),
        "name": str(clip.get("name") or "media"),
        "media_kind": media_kind,
        "start": max(_float_or(clip.get("start"), 0.0), 0.0),
        "duration": duration,
        "source_in": source_in,
        "source_out": source_out,
        "enabled": bool(clip.get("enabled", True)),
        "effects": clip.get("effects") if isinstance(clip.get("effects"), dict) else _default_effects(),
        "transition_after": clip.get("transition_after") if isinstance(clip.get("transition_after"), dict) else None,
        "summary": clip.get("summary") if isinstance(clip.get("summary"), dict) else None,
        "thumbnails": clip.get("thumbnails") if isinstance(clip.get("thumbnails"), list) else [],
        "waveform_peaks": clip.get("waveform_peaks") if isinstance(clip.get("waveform_peaks"), list) else [],
        "keyframes": clip.get("keyframes") if isinstance(clip.get("keyframes"), list) else [],
        "text_config": _normalize_text_config(clip.get("text_config")) if media_kind == "text" else None,
        "provenance": clip.get("provenance") if isinstance(clip.get("provenance"), dict) else None,
    }


def _normalize_text_config(value: Any) -> dict[str, Any]:
    """Normalize the text clip payload described in Timeline v1 §2.3."""
    config = value if isinstance(value, dict) else {}
    position = config.get("position")
    if isinstance(position, dict):
        position = {"x": _float_or(position.get("x"), 0.0), "y": _float_or(position.get("y"), 0.0)}
    else:
        position = None
    align = str(config.get("align") or "center")
    if align not in {"left", "center", "right"}:
        align = "center"
    return {
        "content": str(config.get("content") or ""),
        "font_size": _float_or(config.get("font_size"), 64.0),
        "color": str(config.get("color") or "#ffffff"),
        "position": position,
        "align": align,
    }


def _normalize_tracks(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        return _default_tracks()
    tracks: list[dict[str, Any]] = []
    next_video_index = 1
    next_overlay_index = 1
    next_audio_index = 1
    for index, raw in enumerate(value):
        if not isinstance(raw, dict):
            continue
        raw_id = str(raw.get("id", ""))
        raw_kind = str(raw.get("kind") or "").strip().lower()
        if raw_kind == "overlay" or raw_id.startswith("OV"):
            kind = "overlay"
            prefix = "OV"
            default_name = "Overlay"
            fallback_id = f"OV{next_overlay_index}"
            next_overlay_index += 1
        elif raw_kind == "audio" or raw_id.startswith("A"):
            kind = "audio"
            prefix = "A"
            default_name = "Audio"
            fallback_id = f"A{next_audio_index}"
            next_audio_index += 1
        else:
            kind = "video"
            prefix = "V"
            default_name = "Video"
            fallback_id = f"V{next_video_index}"
            next_video_index += 1
        normalized_id = str(raw.get("id") or fallback_id)
        tracks.append(
            {
                "id": normalized_id,
                "kind": kind,
                "name": str(raw.get("name") or default_name),
                "index": int(_float_or(raw.get("index"), index)),
                "locked": bool(raw.get("locked", False)),
                "muted": bool(raw.get("muted", False)),
                "duck_under": _optional_str(raw.get("duck_under")),
            }
        )
    return tracks or _default_tracks()


def _default_tracks() -> list[dict[str, Any]]:
    return [
        {"id": "V1", "kind": "video", "name": "Video 1", "index": 0, "locked": False, "muted": False, "duck_under": None},
        {"id": "A1", "kind": "audio", "name": "Audio 1", "index": 1, "locked": False, "muted": False, "duck_under": None},
    ]


def _default_effects() -> dict[str, Any]:
    return {"rotation": 0, "mirrored": False, "muted": False, "audioDetached": False, "speed": 1}


def _media_kind_for_clip(clip: dict[str, Any], metadata: dict[str, Any]) -> str:
    raw = str(clip.get("mediaKind") or clip.get("media_kind") or metadata.get("media_kind") or "")
    if raw in MEDIA_KINDS:
        return raw
    return _media_kind_for_name(str(clip.get("name") or clip.get("serverPath") or ""), str(clip.get("mimeType") or metadata.get("mime_type") or ""))


def _media_kind_for_name(name: str, mime_type: str = "") -> str:
    mime = mime_type.lower()
    if mime in {"application/dotlottie", "application/vnd.lottie+json"}:
        return "lottie"
    if mime.startswith("image/"):
        return "image"
    if mime.startswith("audio/"):
        return "audio"
    suffix = Path(name).suffix.lower()
    if suffix in IMAGE_EXTENSIONS:
        return "image"
    if suffix in AUDIO_EXTENSIONS:
        return "audio"
    if suffix in LOTTIE_EXTENSIONS:
        return "lottie"
    return "video"


def _default_track_id_for_media_kind(media_kind: str) -> str:
    if media_kind == "audio":
        return "A1"
    if media_kind in {"image", "text", "lottie"}:
        return "OV1"
    return "V1"


def _title_from_legacy_state(project_state: dict[str, Any]) -> str:
    clips = project_state.get("clips") if isinstance(project_state, dict) else None
    if isinstance(clips, list) and clips:
        first = clips[0]
        if isinstance(first, dict) and first.get("name"):
            return str(first["name"])[:120]
    return "Untitled Project"


def _asset_id(value: str) -> str:
    digest = hashlib.sha1(value.encode("utf-8")).hexdigest()[:16]
    return f"asset_{digest}"


def _float_or(value: Any, fallback: float) -> float:
    try:
        parsed = float(value)
        return parsed if parsed == parsed else fallback
    except (TypeError, ValueError):
        return fallback


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = [
    "QUANTA_BLOCK_KINDS",
    "QUANTA_VERSION",
    "IMAGE_DURATION",
    "PROJECT_SCHEMA",
    "PROJECT_SCHEMA_VERSION",
    "clip_count",
    "empty_quanta",
    "empty_project",
    "is_canonical_project",
    "legacy_project_state_from_project",
    "normalize_quanta",
    "normalize_project",
]
