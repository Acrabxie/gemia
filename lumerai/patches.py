from __future__ import annotations

import copy
import re
import uuid
from typing import Any, Callable

from gemia.project_model import (
    IMAGE_DURATION,
    _normalize_shot,
    _normalize_timeline_clip,
    empty_shotlist,
    normalize_project,
    normalize_shotlist,
)
from gemia.video.layers import BLEND_MODES

# Timeline v1 (M1) contract: seconds as floats, rounded to 6 places on write,
# compared with EPSILON tolerance. See docs/timeline-v1/01-op-vocabulary.md.
EPSILON = 1e-3

_EXTENDED_INSERT_KEYS = {"track_id", "at", "ripple"}
_TRANSITION_KINDS = {"cut", "dissolve", "wipe", "fade"}
# Audio attributes ride on the same effects map (M6): gain_db/fade_in/fade_out
# join the existing `muted` so timeline_set_clip_effects covers the whole audio
# surface with no extra verb, and they round-trip through OTIO metadata for free.
# Compositing (direct-UI BLEND op): blend_mode rides the same effects map so the
# existing set_clip_effects op stores it on the clip and the renderer reads it
# off effects (compositing_graph -> Layer.blend_mode). Validated against the
# renderer's canonical BLEND_MODES set (gemia.video.layers).
_EFFECT_KEYS = {
    "rotation", "mirrored", "muted", "speed", "blur_radius", "opacity", "x", "y", "scale",
    "gain_db", "fade_in", "fade_out", "blend_mode",
}
_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


class TimelinePatchError(ValueError):
    """Structured timeline patch failure; str(e) carries both code and message."""

    def __init__(self, code: str, message: str) -> None:
        self.code = str(code)
        self.message = str(message)
        super().__init__(f"{self.code}: {self.message}")


def apply_timeline_patches(project_state: dict[str, Any] | None, patches: list[dict[str, Any]]) -> dict[str, Any]:
    # Deep-copy after normalization so ops can never pollute the caller's dict:
    # the whole patch list applies atomically or the original state stays valid.
    project = copy.deepcopy(normalize_project(project_state or {}))
    needs_validation = False
    for patch in patches:
        if not isinstance(patch, dict) or patch.get("version") != 1:
            raise ValueError("Unsupported TimelinePatch")
        for op in patch.get("ops") or []:
            needs_validation = _apply_op(project, op) or needs_validation
    _recompute_duration(project)
    if needs_validation:
        validate_project(project)
    return project


def _apply_op(project: dict[str, Any], op: dict[str, Any]) -> bool:
    """Apply one op in place; return True when it used the M1 vocabulary.

    Legacy-form ``insert_clip``/``replace_clip`` (no ``track_id``/``at``/``ripple``
    keys) keep their historical behaviour byte-for-byte and do not arm the
    post-apply ``validate_project`` pass, so existing emitters stay green.
    """
    if not isinstance(op, dict):
        raise ValueError("TimelinePatch op must be an object")
    operation = str(op.get("op") or "")
    if operation == "replace_clip" or (
        operation == "insert_clip" and not _EXTENDED_INSERT_KEYS.intersection(op)
    ):
        _apply_legacy_clip_op(project, operation, op)
        return False
    handler = _OP_HANDLERS.get(operation)
    if handler is None:
        raise TimelinePatchError("E_OP_UNKNOWN", f"Unsupported TimelinePatch op: {operation}")
    handler(project, op)
    return True


def _apply_legacy_clip_op(project: dict[str, Any], operation: str, op: dict[str, Any]) -> None:
    """Historical insert_clip/replace_clip behaviour (kept verbatim for compat)."""
    data = op.get("data") if isinstance(op.get("data"), dict) else {}
    asset = data.get("asset") if isinstance(data.get("asset"), dict) else None
    clip = copy.deepcopy(data.get("clip")) if isinstance(data.get("clip"), dict) else None
    if not clip:
        raise ValueError(f"{operation} is missing clip data")
    provenance = op.get("provenance") if isinstance(op.get("provenance"), dict) else None
    if provenance is not None:
        clip["provenance"] = provenance
    if asset:
        _upsert_asset(project, copy.deepcopy(asset))
    if operation == "insert_clip":
        project["timeline"]["clips"].append(clip)
        return
    clip_id = str(op.get("clip_id") or clip.get("id") or "")
    if not clip_id:
        raise ValueError("replace_clip is missing clip_id")
    clips = project["timeline"]["clips"]
    for index, existing in enumerate(clips):
        if isinstance(existing, dict) and str(existing.get("id")) == clip_id:
            replacement = {**existing, **clip, "id": clip_id}
            clips[index] = replacement
            return
    raise ValueError(f"replace_clip target not found: {clip_id}")


def _upsert_asset(project: dict[str, Any], asset: dict[str, Any]) -> None:
    asset_id = str(asset.get("id") or asset.get("asset_id") or "")
    if not asset_id:
        raise ValueError("TimelinePatch asset is missing id")
    asset["id"] = asset_id
    asset["asset_id"] = str(asset.get("asset_id") or asset_id)
    assets = project["assets"]
    for index, existing in enumerate(assets):
        if isinstance(existing, dict) and str(existing.get("id")) == asset_id:
            assets[index] = {**existing, **asset}
            return
    assets.append(asset)


def _recompute_duration(project: dict[str, Any]) -> None:
    end = 0.0
    for clip in project.get("timeline", {}).get("clips") or []:
        if isinstance(clip, dict):
            end = max(end, float(clip.get("start") or 0.0) + float(clip.get("duration") or 0.0))
    project["timeline"]["duration"] = round(end, 6)


# ── Timeline v1 (M1) whole-project invariants ───────────────────────


def validate_project(project: dict[str, Any]) -> None:
    """Raise TimelinePatchError when any Timeline v1 invariant is violated."""
    timeline = project.get("timeline") if isinstance(project.get("timeline"), dict) else {}
    tracks = {
        str(track.get("id")): track
        for track in timeline.get("tracks") or []
        if isinstance(track, dict)
    }
    by_track: dict[str, list[dict[str, Any]]] = {}
    for clip in timeline.get("clips") or []:
        if not isinstance(clip, dict):
            continue
        track_id = str(clip.get("track_id") or "")
        if track_id not in tracks:
            raise TimelinePatchError(
                "E_NOT_FOUND", f"clip {clip.get('id')} references missing track {track_id}"
            )
        by_track.setdefault(track_id, []).append(clip)
        media_kind = str(clip.get("media_kind") or "video")
        duration = _as_float(clip.get("duration"))
        source_in = _as_float(clip.get("source_in"))
        source_out = _as_float(clip.get("source_out"))
        if media_kind in {"video", "image", "audio", "lottie"}:
            legacy_forced_image = (
                media_kind == "image"
                and abs(source_in) <= EPSILON
                and abs(source_out - IMAGE_DURATION) <= EPSILON
            )
            if not legacy_forced_image and abs((source_out - source_in) - duration) > EPSILON:
                raise TimelinePatchError(
                    "E_RANGE",
                    f"clip {clip.get('id')} duration {duration} does not match "
                    f"source range [{source_in}, {source_out}]",
                )
        elif media_kind == "text":
            config = clip.get("text_config") if isinstance(clip.get("text_config"), dict) else {}
            if not str(config.get("content") or ""):
                raise TimelinePatchError(
                    "E_BAD_ARG", f"text clip {clip.get('id')} requires non-empty text_config.content"
                )
    for track_id, group in by_track.items():
        group.sort(key=lambda item: _as_float(item.get("start")))
        for prev, nxt in zip(group, group[1:]):
            if _clip_end(prev) - EPSILON > _as_float(nxt.get("start")):
                raise TimelinePatchError(
                    "E_OVERLAP",
                    f"clips {prev.get('id')} and {nxt.get('id')} overlap on track {track_id}",
                )


# ── shared helpers for the M1 op vocabulary ─────────────────────────


def _as_float(value: Any, fallback: float = 0.0) -> float:
    try:
        parsed = float(value)
        return parsed if parsed == parsed else fallback
    except (TypeError, ValueError):
        return fallback


def _number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
        raise TimelinePatchError("E_BAD_ARG", f"{name} must be a number, got {value!r}")
    return float(value)


def _clip_end(clip: dict[str, Any]) -> float:
    return _as_float(clip.get("start")) + _as_float(clip.get("duration"))


def _find_clip(project: dict[str, Any], clip_id: str) -> dict[str, Any] | None:
    for clip in project["timeline"].get("clips") or []:
        if isinstance(clip, dict) and str(clip.get("id")) == str(clip_id):
            return clip
    return None


def _require_clip(project: dict[str, Any], op: dict[str, Any]) -> dict[str, Any]:
    clip_id = str(op.get("clip_id") or "")
    if not clip_id:
        raise TimelinePatchError("E_BAD_ARG", f"{op.get('op')} requires clip_id")
    clip = _find_clip(project, clip_id)
    if clip is None:
        raise TimelinePatchError("E_NOT_FOUND", f"clip not found: {clip_id}")
    return clip


def _track_by_id(project: dict[str, Any], track_id: str) -> dict[str, Any] | None:
    for track in project["timeline"].get("tracks") or []:
        if isinstance(track, dict) and str(track.get("id")) == str(track_id):
            return track
    return None


def _require_track(project: dict[str, Any], track_id: str) -> dict[str, Any]:
    track = _track_by_id(project, track_id)
    if track is None:
        raise TimelinePatchError("E_NOT_FOUND", f"track not found: {track_id}")
    return track


def _ensure_media_matches_track(media_kind: str, track: dict[str, Any]) -> None:
    kind = str(track.get("kind") or "")
    if media_kind == "audio":
        if kind != "audio":
            raise TimelinePatchError(
                "E_TRACK_KIND", f"audio clip requires an audio track, got {kind} ({track.get('id')})"
            )
    elif media_kind == "video":
        if kind != "video":
            raise TimelinePatchError(
                "E_TRACK_KIND", f"video clip requires a video track, got {kind} ({track.get('id')})"
            )
    elif media_kind == "image":
        # Images can be on video or overlay tracks (enabling image-based shotlist main chain)
        if kind not in {"video", "overlay"}:
            raise TimelinePatchError(
                "E_TRACK_KIND",
                f"image clip requires a video or overlay track, got {kind} ({track.get('id')})",
            )
    elif media_kind in {"text", "lottie"}:
        if kind != "overlay":
            raise TimelinePatchError(
                "E_TRACK_KIND",
                f"{media_kind} clip requires an overlay track, got {kind} ({track.get('id')})",
            )
    else:
        raise TimelinePatchError("E_TRACK_KIND", f"unsupported media_kind in v1: {media_kind}")


def _track_clips(
    project: dict[str, Any], track_id: str, *, exclude: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    clips = [
        clip
        for clip in project["timeline"].get("clips") or []
        if isinstance(clip, dict) and str(clip.get("track_id")) == str(track_id) and clip is not exclude
    ]
    clips.sort(key=lambda item: _as_float(item.get("start")))
    return clips


def _ensure_no_overlap(
    project: dict[str, Any],
    track_id: str,
    start: float,
    duration: float,
    *,
    exclude: dict[str, Any] | None = None,
) -> None:
    for other in _track_clips(project, track_id, exclude=exclude):
        other_start = _as_float(other.get("start"))
        if start < _clip_end(other) - EPSILON and other_start < start + duration - EPSILON:
            raise TimelinePatchError(
                "E_OVERLAP", f"clip overlaps {other.get('id')} on track {track_id}"
            )


def _ripple_shift(
    project: dict[str, Any],
    track_id: str,
    threshold: float,
    delta: float,
    *,
    exclude: dict[str, Any] | None = None,
) -> None:
    """Shift clips on the track whose start >= threshold (EPSILON tolerant) by delta."""
    if abs(delta) <= EPSILON:
        return
    for clip in _track_clips(project, track_id, exclude=exclude):
        if _as_float(clip.get("start")) >= threshold - EPSILON:
            clip["start"] = round(max(_as_float(clip.get("start")) + delta, 0.0), 6)


def _stamp_provenance(op: dict[str, Any], *clips: dict[str, Any]) -> None:
    provenance = op.get("provenance") if isinstance(op.get("provenance"), dict) else None
    if provenance is None:
        return
    for clip in clips:
        clip["provenance"] = copy.deepcopy(provenance)


def _asset_by_id(project: dict[str, Any], asset_id: str) -> dict[str, Any] | None:
    for asset in project.get("assets") or []:
        if isinstance(asset, dict) and str(asset.get("id")) == str(asset_id):
            return asset
    return None


def _new_clip_id() -> str:
    return f"clip_{uuid.uuid4().hex[:8]}"


def _reindex_tracks(tracks: list[dict[str, Any]]) -> None:
    for index, track in enumerate(tracks):
        if isinstance(track, dict):
            track["index"] = index


# ── M1 op handlers ──────────────────────────────────────────────────


def _op_insert_clip(project: dict[str, Any], op: dict[str, Any]) -> None:
    """Extended insert_clip (op carries track_id/at/ripple). Spec §3.1."""
    data = op.get("data") if isinstance(op.get("data"), dict) else {}
    raw_clip = copy.deepcopy(data.get("clip")) if isinstance(data.get("clip"), dict) else None
    if not raw_clip:
        raise TimelinePatchError("E_BAD_ARG", "insert_clip is missing clip data")
    asset = data.get("asset") if isinstance(data.get("asset"), dict) else None
    if asset:
        _upsert_asset(project, copy.deepcopy(asset))

    track_id = str(op.get("track_id") or raw_clip.get("track_id") or "V1")
    track = _require_track(project, track_id)
    media_kind = str(raw_clip.get("media_kind") or "video")
    _ensure_media_matches_track(media_kind, track)
    raw_clip["track_id"] = track_id

    explicit_id = str(raw_clip.get("id") or "")
    if explicit_id and _find_clip(project, explicit_id) is not None:
        raise TimelinePatchError("E_BAD_ARG", f"clip id already exists: {explicit_id}")
    raw_clip["id"] = explicit_id or _new_clip_id()

    duration = _as_float(raw_clip.get("duration"))
    if duration <= 0:
        asset_obj = _asset_by_id(project, str(raw_clip.get("asset_id") or ""))
        if asset_obj is not None:
            duration = _as_float(asset_obj.get("duration"))
    if duration <= 0:
        duration = _as_float(raw_clip.get("source_out")) - _as_float(raw_clip.get("source_in"))
    if duration <= 0 and media_kind in {"image", "text", "lottie"}:
        duration = IMAGE_DURATION
    if duration <= 0:
        raise TimelinePatchError("E_BAD_ARG", "insert_clip cannot determine clip duration")
    raw_clip["duration"] = round(duration, 6)

    ripple = bool(op.get("ripple", False))
    at = op.get("at")
    if at is None or at == "append":
        start = max((_clip_end(c) for c in _track_clips(project, track_id)), default=0.0)
    elif isinstance(at, dict) and "time" in at and "index" not in at:
        start = _number(at.get("time"), "insert_clip at.time")
        if start < 0:
            raise TimelinePatchError("E_BAD_ARG", "insert_clip at.time must be >= 0")
        if ripple:
            _ripple_shift(project, track_id, start, duration)
        _ensure_no_overlap(project, track_id, start, duration)
    elif isinstance(at, dict) and "index" in at and "time" not in at:
        index = at.get("index")
        if isinstance(index, bool) or not isinstance(index, int):
            raise TimelinePatchError("E_BAD_ARG", "insert_clip at.index must be an integer")
        existing = _track_clips(project, track_id)
        if index < 0 or index > len(existing):
            raise TimelinePatchError(
                "E_BAD_ARG", f"insert_clip at.index out of range: {index} (track has {len(existing)} clips)"
            )
        if index == len(existing):
            start = max((_clip_end(c) for c in existing), default=0.0)
        else:
            start = _as_float(existing[index].get("start"))
            # Index insertion ripples by nature; the ripple flag is ignored.
            _ripple_shift(project, track_id, start, duration)
    else:
        raise TimelinePatchError(
            "E_BAD_ARG", 'insert_clip "at" must be "append", {"time": t} or {"index": i}'
        )
    raw_clip["start"] = round(start, 6)

    clip = _normalize_timeline_clip(raw_clip)
    if media_kind == "text":
        config = clip.get("text_config") or {}
        if not str(config.get("content") or ""):
            raise TimelinePatchError("E_BAD_ARG", "text clip requires non-empty text_config.content")
        if not _COLOR_RE.match(str(config.get("color") or "")):
            raise TimelinePatchError(
                "E_BAD_ARG", f"text_config.color must look like #rrggbb, got {config.get('color')!r}"
            )
    _stamp_provenance(op, clip)
    project["timeline"]["clips"].append(clip)


def _op_delete_clip(project: dict[str, Any], op: dict[str, Any]) -> None:
    """Spec §3.2."""
    clip = _require_clip(project, op)
    clips = project["timeline"]["clips"]
    clips[:] = [item for item in clips if item is not clip]
    if bool(op.get("ripple", False)):
        _ripple_shift(
            project,
            str(clip.get("track_id") or ""),
            _as_float(clip.get("start")),
            -_as_float(clip.get("duration")),
        )


def _op_move_clip(project: dict[str, Any], op: dict[str, Any]) -> None:
    """Spec §3.3 — ripple only closes the gap left at the original position."""
    clip = _require_clip(project, op)
    has_start = op.get("start") is not None
    has_track = op.get("track_id") is not None
    if not has_start and not has_track:
        raise TimelinePatchError("E_BAD_ARG", "move_clip requires start and/or track_id")
    old_track_id = str(clip.get("track_id") or "")
    old_start = _as_float(clip.get("start"))
    duration = _as_float(clip.get("duration"))
    new_track_id = str(op.get("track_id")) if has_track else old_track_id
    if has_track:
        track = _require_track(project, new_track_id)
        _ensure_media_matches_track(str(clip.get("media_kind") or "video"), track)
    new_start = _number(op.get("start"), "move_clip.start") if has_start else old_start
    if new_start < 0:
        raise TimelinePatchError("E_BAD_ARG", "move_clip.start must be >= 0")
    moved = new_track_id != old_track_id or abs(new_start - old_start) > EPSILON
    if moved and bool(op.get("ripple", False)):
        _ripple_shift(project, old_track_id, old_start, -duration, exclude=clip)
    _ensure_no_overlap(project, new_track_id, new_start, duration, exclude=clip)
    clip["track_id"] = new_track_id
    clip["start"] = round(new_start, 6)
    _stamp_provenance(op, clip)


def _op_trim_clip(project: dict[str, Any], op: dict[str, Any]) -> None:
    """Spec §3.4 — duration = source_out - source_in (speed stays reserved)."""
    clip = _require_clip(project, op)
    media_kind = str(clip.get("media_kind") or "video")
    if media_kind not in {"video", "image", "audio", "lottie"}:
        raise TimelinePatchError(
            "E_BAD_ARG", f"trim_clip only supports video/image/audio/lottie clips, got {media_kind}"
        )
    if op.get("source_in") is None and op.get("source_out") is None:
        raise TimelinePatchError("E_BAD_ARG", "trim_clip requires source_in and/or source_out")
    source_in = (
        _number(op.get("source_in"), "trim_clip.source_in")
        if op.get("source_in") is not None
        else _as_float(clip.get("source_in"))
    )
    source_out = (
        _number(op.get("source_out"), "trim_clip.source_out")
        if op.get("source_out") is not None
        else _as_float(clip.get("source_out"))
    )
    if source_in < 0 or source_out <= source_in:
        raise TimelinePatchError(
            "E_RANGE", f"invalid trim range [{source_in}, {source_out}] (need 0 <= in < out)"
        )
    asset = _asset_by_id(project, str(clip.get("asset_id") or ""))
    asset_duration = _as_float(asset.get("duration")) if asset else 0.0
    if asset_duration > 0 and source_out > asset_duration + EPSILON:
        raise TimelinePatchError(
            "E_RANGE", f"source_out {source_out} exceeds asset duration {asset_duration}"
        )
    old_duration = _as_float(clip.get("duration"))
    new_duration = source_out - source_in
    delta = new_duration - old_duration
    track_id = str(clip.get("track_id") or "")
    if abs(delta) > EPSILON:
        if bool(op.get("ripple", False)):
            _ripple_shift(project, track_id, _as_float(clip.get("start")), delta, exclude=clip)
        elif delta > 0:
            _ensure_no_overlap(
                project, track_id, _as_float(clip.get("start")), new_duration, exclude=clip
            )
    clip["source_in"] = round(source_in, 6)
    clip["source_out"] = round(source_out, 6)
    clip["duration"] = round(new_duration, 6)
    _stamp_provenance(op, clip)


def _op_split_clip(project: dict[str, Any], op: dict[str, Any]) -> None:
    """Spec §3.5 — identity rule: both halves keep asset_id; ids stay distinct."""
    clip = _require_clip(project, op)
    at_time = _number(op.get("at_time"), "split_clip.at_time")
    start = _as_float(clip.get("start"))
    end = _clip_end(clip)
    if at_time <= start + EPSILON or at_time >= end - EPSILON:
        raise TimelinePatchError(
            "E_BAD_ARG",
            f"split_clip at_time must fall strictly inside ({start}, {end}), got {at_time}",
        )
    new_clip_id = str(op.get("new_clip_id") or "") or _new_clip_id()
    if _find_clip(project, new_clip_id) is not None:
        raise TimelinePatchError("E_BAD_ARG", f"new_clip_id already exists: {new_clip_id}")
    front_duration = at_time - start
    back = copy.deepcopy(clip)
    back["id"] = new_clip_id
    back["start"] = round(at_time, 6)
    back["duration"] = round(end - at_time, 6)
    if str(clip.get("media_kind") or "video") == "text":
        clip["source_in"] = 0.0
        clip["source_out"] = round(front_duration, 6)
        back["source_in"] = 0.0
        back["source_out"] = round(end - at_time, 6)
    else:
        source_in = _as_float(clip.get("source_in"))
        back["source_in"] = round(source_in + front_duration, 6)
        clip["source_out"] = round(source_in + front_duration, 6)
    clip["duration"] = round(front_duration, 6)
    back["transition_after"] = copy.deepcopy(clip.get("transition_after"))
    clip["transition_after"] = None
    _stamp_provenance(op, clip, back)
    clips = project["timeline"]["clips"]
    clips.insert(clips.index(clip) + 1, back)


def _op_set_clip_time(project: dict[str, Any], op: dict[str, Any]) -> None:
    """Spec §3.6 — duration uses trim semantics, start uses move semantics."""
    clip = _require_clip(project, op)
    has_start = op.get("start") is not None
    has_duration = op.get("duration") is not None
    if not has_start and not has_duration:
        raise TimelinePatchError("E_BAD_ARG", "set_clip_time requires start and/or duration")
    media_kind = str(clip.get("media_kind") or "video")
    track_id = str(clip.get("track_id") or "")
    ripple = bool(op.get("ripple", False))
    if has_duration:
        if media_kind not in {"image", "text", "lottie"}:
            raise TimelinePatchError(
                "E_BAD_ARG", f"set_clip_time duration only supports image/text/lottie clips, got {media_kind}"
            )
        new_duration = _number(op.get("duration"), "set_clip_time.duration")
        if new_duration <= 0:
            raise TimelinePatchError("E_BAD_ARG", "set_clip_time.duration must be > 0")
        delta = new_duration - _as_float(clip.get("duration"))
        if abs(delta) > EPSILON:
            if ripple:
                _ripple_shift(project, track_id, _as_float(clip.get("start")), delta, exclude=clip)
            elif delta > 0:
                _ensure_no_overlap(
                    project, track_id, _as_float(clip.get("start")), new_duration, exclude=clip
                )
        clip["duration"] = round(new_duration, 6)
        clip["source_in"] = 0.0
        clip["source_out"] = round(new_duration, 6)
    if has_start:
        new_start = _number(op.get("start"), "set_clip_time.start")
        if new_start < 0:
            raise TimelinePatchError("E_BAD_ARG", "set_clip_time.start must be >= 0")
        old_start = _as_float(clip.get("start"))
        if abs(new_start - old_start) > EPSILON:
            if ripple:
                _ripple_shift(
                    project, track_id, old_start, -_as_float(clip.get("duration")), exclude=clip
                )
            _ensure_no_overlap(
                project, track_id, new_start, _as_float(clip.get("duration")), exclude=clip
            )
            clip["start"] = round(new_start, 6)
    _stamp_provenance(op, clip)


def _op_add_transition(project: dict[str, Any], op: dict[str, Any]) -> None:
    """Spec §3.8."""
    clip = _require_clip(project, op)
    kind = str(op.get("kind") or "")
    if kind not in _TRANSITION_KINDS:
        raise TimelinePatchError(
            "E_BAD_ARG", f"add_transition kind must be one of {sorted(_TRANSITION_KINDS)}, got {kind!r}"
        )
    if kind == "cut":
        clip["transition_after"] = None
        _stamp_provenance(op, clip)
        return
    duration_sec = (
        _number(op.get("duration_sec"), "add_transition.duration_sec")
        if op.get("duration_sec") is not None
        else 0.5
    )
    if duration_sec <= 0:
        raise TimelinePatchError("E_BAD_ARG", "add_transition.duration_sec must be > 0")
    track_id = str(clip.get("track_id") or "")
    following = [
        other
        for other in _track_clips(project, track_id, exclude=clip)
        if _as_float(other.get("start")) > _as_float(clip.get("start"))
    ]
    if not following:
        raise TimelinePatchError(
            "E_BAD_ARG", f"add_transition requires a following clip on track {track_id}"
        )
    nxt = following[0]
    if abs(_clip_end(clip) - _as_float(nxt.get("start"))) > EPSILON:
        raise TimelinePatchError(
            "E_BAD_ARG",
            f"clips {clip.get('id')} and {nxt.get('id')} are not adjacent; move/ripple them together first",
        )
    if duration_sec > min(_as_float(clip.get("duration")), _as_float(nxt.get("duration"))) + EPSILON:
        raise TimelinePatchError(
            "E_BAD_ARG", "add_transition.duration_sec exceeds the duration of an adjacent clip"
        )
    clip["transition_after"] = {"kind": kind, "duration_sec": round(duration_sec, 6)}
    _stamp_provenance(op, clip)


def _op_set_clip_effects(project: dict[str, Any], op: dict[str, Any]) -> None:
    """Spec §3.9 — whitelisted merge; explicit null deletes a key."""
    clip = _require_clip(project, op)
    effects = op.get("effects")
    if not isinstance(effects, dict):
        raise TimelinePatchError("E_BAD_ARG", "set_clip_effects requires an effects object")
    current = clip.get("effects") if isinstance(clip.get("effects"), dict) else {}
    for key, value in effects.items():
        if key not in _EFFECT_KEYS:
            raise TimelinePatchError("E_BAD_ARG", f"effects key not allowed in v1: {key}")
        if value is None:
            current.pop(key, None)
            continue
        current[key] = _validated_effect_value(str(key), value)
    clip["effects"] = current
    _stamp_provenance(op, clip)


def _validated_effect_value(key: str, value: Any) -> Any:
    if key in {"mirrored", "muted"}:
        if not isinstance(value, bool):
            raise TimelinePatchError("E_BAD_ARG", f"effects.{key} must be a boolean")
        return value
    if key == "blend_mode":
        # Direct-UI BLEND op. Validate against the renderer's canonical set so a
        # stored mode is always one compositing_graph -> Layer.blend_mode can
        # actually composite (gemia.video.layers._blend_rgb).
        if not isinstance(value, str) or value not in BLEND_MODES:
            raise TimelinePatchError(
                "E_BAD_ARG",
                f"effects.blend_mode must be one of {sorted(BLEND_MODES)}, got {value!r}",
            )
        return value
    number = _number(value, f"effects.{key}")
    if key == "rotation":
        if number not in (0.0, 90.0, 180.0, 270.0):
            raise TimelinePatchError("E_BAD_ARG", "effects.rotation must be one of 0/90/180/270")
        return int(number)
    if key in {"speed", "scale"} and number <= 0:
        raise TimelinePatchError("E_BAD_ARG", f"effects.{key} must be > 0")
    if key == "blur_radius" and number < 0:
        raise TimelinePatchError("E_BAD_ARG", "effects.blur_radius must be >= 0")
    if key == "opacity" and not 0.0 <= number <= 1.0:
        raise TimelinePatchError("E_BAD_ARG", "effects.opacity must be within [0, 1]")
    # Audio attributes (M6). gain_db is unbounded (dB, may be negative); fades are
    # non-negative seconds. The renderer reads them off effects at export time.
    if key in {"fade_in", "fade_out"} and number < 0:
        raise TimelinePatchError("E_BAD_ARG", f"effects.{key} must be >= 0")
    return number


def _op_add_track(project: dict[str, Any], op: dict[str, Any]) -> None:
    """Spec §3.10 (M6) — video/overlay land before audio; audio sits at the end."""
    kind = str(op.get("kind") or "")
    if kind not in {"video", "overlay", "audio"}:
        raise TimelinePatchError(
            "E_BAD_ARG", f"add_track kind must be video, overlay or audio, got {kind!r}"
        )
    tracks = project["timeline"]["tracks"]
    existing_ids = {str(track.get("id")) for track in tracks if isinstance(track, dict)}
    prefix = {"overlay": "OV", "audio": "A"}.get(kind, "V")
    label = {"overlay": "Overlay", "audio": "Audio"}.get(kind, "Video")
    track_id = str(op.get("track_id") or "")
    if track_id:
        if track_id in existing_ids:
            raise TimelinePatchError("E_BAD_ARG", f"track id already exists: {track_id}")
        default_name = track_id
    else:
        ordinal = 1
        while f"{prefix}{ordinal}" in existing_ids:
            ordinal += 1
        track_id = f"{prefix}{ordinal}"
        default_name = f"{label} {ordinal}"
    new_track = {
        "id": track_id,
        "kind": kind,
        "name": str(op.get("name") or default_name),
        "index": 0,
        "locked": False,
        "muted": False,
        "duck_under": None,
    }
    same_kind = [i for i, track in enumerate(tracks) if str(track.get("kind")) == kind]
    if same_kind:
        insert_at = same_kind[-1] + 1
    else:
        first_audio = next(
            (i for i, track in enumerate(tracks) if str(track.get("kind")) == "audio"), None
        )
        insert_at = first_audio if first_audio is not None else len(tracks)
    tracks.insert(insert_at, new_track)
    _reindex_tracks(tracks)


def _op_set_track(project: dict[str, Any], op: dict[str, Any]) -> None:
    """M7 §set_track — track-level fields (currently the ducking relationship).

    ``duck_under = T`` marks this audio track as a bed that ducks whenever
    track ``T`` (the sidechain trigger, e.g. a voice track) is loud. ``null``
    clears it. Validates kind, target existence/kind, self-reference, and that
    the relationship would not form a cycle.
    """
    track_id = str(op.get("track_id") or "")
    if not track_id:
        raise TimelinePatchError("E_BAD_ARG", "set_track requires track_id")
    track = _require_track(project, track_id)
    if str(track.get("kind")) != "audio":
        raise TimelinePatchError(
            "E_TRACK_KIND", f"set_track only supports audio tracks, got {track.get('kind')} ({track_id})"
        )
    if "duck_under" not in op:
        return  # nothing to change (track existence/kind already validated)
    duck = op.get("duck_under")
    if duck is None or str(duck) == "":
        track["duck_under"] = None
        return
    duck_id = str(duck)
    if duck_id == track_id:
        raise TimelinePatchError("E_BAD_ARG", "duck_under cannot reference the track itself")
    target = _track_by_id(project, duck_id)
    if target is None:
        raise TimelinePatchError("E_NOT_FOUND", f"duck_under target not found: {duck_id}")
    if str(target.get("kind")) != "audio":
        raise TimelinePatchError(
            "E_TRACK_KIND", f"duck_under target must be an audio track: {duck_id}"
        )
    # Cycle guard: walk the existing duck_under chain from the proposed target;
    # if it loops back to this track, the new edge would close a cycle.
    seen = {track_id}
    cursor: str | None = duck_id
    while cursor is not None:
        if cursor in seen:
            raise TimelinePatchError(
                "E_BAD_ARG", f"duck_under would create a cycle through {cursor}"
            )
        seen.add(cursor)
        nxt = _track_by_id(project, cursor)
        nxt_duck = nxt.get("duck_under") if isinstance(nxt, dict) else None
        cursor = str(nxt_duck) if nxt_duck else None
    track["duck_under"] = duck_id


def _op_remove_track(project: dict[str, Any], op: dict[str, Any]) -> None:
    """Spec §3.10 — only empty tracks may be removed; V1 never."""
    track_id = str(op.get("track_id") or "")
    if not track_id:
        raise TimelinePatchError("E_BAD_ARG", "remove_track requires track_id")
    track = _require_track(project, track_id)
    if track_id == "V1":
        raise TimelinePatchError("E_BAD_ARG", "track V1 cannot be removed")
    if _track_clips(project, track_id):
        raise TimelinePatchError("E_BAD_ARG", f"track is not empty: {track_id}")
    tracks = project["timeline"]["tracks"]
    tracks[:] = [item for item in tracks if item is not track]
    _reindex_tracks(tracks)


def _op_set_timeline_format(project: dict[str, Any], op: dict[str, Any]) -> None:
    """Spec §3.11 — timeline fields only; render_settings stays untouched (M4)."""
    timeline = project["timeline"]
    if op.get("fps") is None and op.get("width") is None and op.get("height") is None:
        raise TimelinePatchError(
            "E_BAD_ARG", "set_timeline_format requires at least one of fps/width/height"
        )
    if op.get("fps") is not None:
        fps = _number(op.get("fps"), "set_timeline_format.fps")
        if not 1.0 <= fps <= 120.0:
            raise TimelinePatchError("E_BAD_ARG", f"fps must be within [1, 120], got {fps}")
        timeline["fps"] = round(fps, 6)
    for key in ("width", "height"):
        if op.get(key) is None:
            continue
        number = _number(op.get(key), f"set_timeline_format.{key}")
        if abs(number - round(number)) > 1e-9:
            raise TimelinePatchError("E_BAD_ARG", f"{key} must be an integer, got {number}")
        value = int(round(number))
        if not 16 <= value <= 7680:
            raise TimelinePatchError("E_BAD_ARG", f"{key} must be within [16, 7680], got {value}")
        timeline[key] = value


def _op_add_marker(project: dict[str, Any], op: dict[str, Any]) -> None:
    """Spec §3.12."""
    time = _number(op.get("time"), "add_marker.time")
    if time < 0:
        raise TimelinePatchError("E_BAD_ARG", "add_marker.time must be >= 0")
    timeline = project["timeline"]
    markers = timeline.get("markers")
    if not isinstance(markers, list):
        markers = []
        timeline["markers"] = markers
    markers.append(
        {
            "id": str(op.get("marker_id") or "") or f"marker_{uuid.uuid4().hex[:8]}",
            "time": round(time, 6),
            "label": str(op.get("label") or ""),
        }
    )


def _op_upsert_asset(project: dict[str, Any], op: dict[str, Any]) -> None:
    """Spec §3.13 — first-class wrapper over the historical _upsert_asset."""
    asset = op.get("asset")
    if not isinstance(asset, dict):
        raise TimelinePatchError("E_BAD_ARG", "upsert_asset requires an asset object")
    if not str(asset.get("id") or asset.get("asset_id") or ""):
        raise TimelinePatchError("E_BAD_ARG", "upsert_asset asset is missing id")
    _upsert_asset(project, copy.deepcopy(asset))


def _op_set_shotlist(project: dict[str, Any], op: dict[str, Any]) -> None:
    """Replace the whole storyboard IR (outline/storyboard-driven editing)."""
    raw = op.get("shotlist")
    if not isinstance(raw, dict):
        raise TimelinePatchError("E_BAD_ARG", "set_shotlist requires a 'shotlist' object")
    project["shotlist"] = normalize_shotlist(raw)


def _op_update_shot(project: dict[str, Any], op: dict[str, Any]) -> None:
    """Merge ``fields`` into a single shot located by ``shot_id``.

    Targeted revision so the model can mark a shot ``filled`` (asset_id) or
    ``placed`` (clip_id), retime it, or reword its text without resending the
    whole shotlist. Immutable identity keys (``id``) cannot be changed here.
    """
    shot_id = str(op.get("shot_id") or "")
    if not shot_id:
        raise TimelinePatchError("E_BAD_ARG", "update_shot requires a 'shot_id'")
    fields = op.get("fields") if isinstance(op.get("fields"), dict) else None
    if fields is None:
        raise TimelinePatchError("E_BAD_ARG", "update_shot requires a 'fields' object")
    shotlist = project.get("shotlist")
    if not isinstance(shotlist, dict):
        shotlist = empty_shotlist()
        project["shotlist"] = shotlist
    for scene_idx, scene in enumerate(shotlist.get("scenes") or []):
        if not isinstance(scene, dict):
            continue
        shots = scene.get("shots") or []
        for shot_idx, shot in enumerate(shots):
            if isinstance(shot, dict) and str(shot.get("id") or "") == shot_id:
                merged = {**shot, **{k: v for k, v in fields.items() if k != "id"}}
                merged["id"] = shot_id
                shots[shot_idx] = _normalize_shot(
                    merged, scene_idx=scene_idx, shot_idx=shot_idx
                )
                return
    raise TimelinePatchError("E_NOT_FOUND", f"update_shot: no shot with id {shot_id}")


_OP_HANDLERS: dict[str, Callable[[dict[str, Any], dict[str, Any]], None]] = {
    "set_shotlist": _op_set_shotlist,
    "update_shot": _op_update_shot,
    "insert_clip": _op_insert_clip,
    "delete_clip": _op_delete_clip,
    "move_clip": _op_move_clip,
    "trim_clip": _op_trim_clip,
    "split_clip": _op_split_clip,
    "set_clip_time": _op_set_clip_time,
    "add_transition": _op_add_transition,
    "set_clip_effects": _op_set_clip_effects,
    "add_track": _op_add_track,
    "remove_track": _op_remove_track,
    "set_track": _op_set_track,
    "set_timeline_format": _op_set_timeline_format,
    "add_marker": _op_add_marker,
    "upsert_asset": _op_upsert_asset,
}
