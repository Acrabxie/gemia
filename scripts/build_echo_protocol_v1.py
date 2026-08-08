#!/usr/bin/env python3
"""Build the locked 120-second Echo Protocol V1 rough cut.

This operator is deliberately deterministic.  It does not ask a model for a
timeline JSON and it never calls a media provider: ten already-reviewed stock
assets are each used twice, while the remaining units use migrated concept
images as image/locally-produced-MG placeholders.  The complete board lands as
one preflighted ``SessionRunner.run_project_edit`` patch; stable clip ids make
post-crash reconciliation fail closed instead of duplicating timeline work.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import mimetypes
from pathlib import Path
import sys
from typing import Any, Mapping

# Direct ``python scripts/build_echo_protocol_v1.py`` execution otherwise puts
# only ``scripts/`` at the front of sys.path.  On a machine with another
# editable Lumeri checkout installed, that can silently import the wrong
# SessionManager.  Pin this production operator to the repository containing
# the script before importing any ``gemia`` module.
REPO_ROOT = Path(__file__).resolve().parents[1]
try:
    sys.path.remove(str(REPO_ROOT))
except ValueError:
    pass
sys.path.insert(0, str(REPO_ROOT))

from gemia.project_model import iter_shots
from gemia.session_manager import SessionManager
from gemia.tools._ffmpeg import ffprobe_metadata, video_stream
from lumerai.export_support import clip_dropped_fields
from lumerai.patches import apply_timeline_patches


SESSION_ID = "v3-00a7080c78e7"
RUN_ID = "echo-protocol-production"
BOARD_VERSION = "echo-protocol-v1-rough-cut-1"
SOURCE_SLOTS = tuple(f"s{index:02d}" for index in range(1, 11))
EPSILON = 1e-3


class EchoBoardError(RuntimeError):
    """The locked board or its persisted timeline no longer meets the brief."""


@dataclass(frozen=True)
class BoardUnit:
    index: int
    start_sec: float
    end_sec: float
    kind: str
    asset_id: str
    reference: str
    take: str = ""
    description: str = ""

    @property
    def shot_id(self) -> str:
        return f"echo_v1_{self.index:02d}"

    @property
    def clip_id(self) -> str:
        return f"echo_v1_u{self.index:02d}"

    @property
    def duration_sec(self) -> float:
        return round(self.end_sec - self.start_sec, 6)

    @property
    def is_public_motion(self) -> bool:
        return self.kind == "stock"

    @property
    def source_in(self) -> float:
        if not self.is_public_motion:
            return 0.0
        return 0.0 if self.take == "a" else 3.0

    @property
    def source_out(self) -> float:
        return round(self.source_in + self.duration_sec, 6)


# Explicit times are intentional: this is the approved production board, not
# an inferred edit.  ``reference`` is either a migrated image id or one of the
# ten visually-reviewed source slots.
_LOCKED_LAYOUT: tuple[tuple[int, float, float, str, str, str, str], ...] = (
    (1, 0, 2, "mg", "img_001", "", "AI pulse / local MG placeholder"),
    (2, 2, 5, "image", "img_001", "", "Predictive-city concept image"),
    (3, 5, 8, "stock", "s01", "a", "Rainy night city motion"),
    (4, 8, 11, "image", "img_002", "", "City system takeover"),
    (5, 11, 14, "stock", "s01", "b", "Rainy night city alternate window"),
    (6, 14, 17, "stock", "s02", "a", "Elevated rail and city"),
    (7, 17, 20, "image", "img_003", "", "Orbital-city concept image"),
    (8, 20, 23, "stock", "s02", "b", "Elevated rail alternate window"),
    (9, 23, 25, "mg", "img_003", "", "HUD map alert / local MG placeholder"),
    (10, 25, 28, "image", "img_004", "", "Hero beat one setup"),
    (11, 28, 31, "mg", "img_004", "", "Hero beat one / local 2.5D placeholder"),
    (12, 31, 34, "stock", "s03", "a", "Orbital spacecraft world-building"),
    (13, 34, 37, "image", "img_005", "", "Laboratory concept image"),
    (14, 37, 40, "stock", "s03", "b", "Orbital spacecraft alternate window"),
    (15, 40, 43, "stock", "s04", "a", "Robotics laboratory"),
    (16, 43, 46, "stock", "s04", "b", "Robotics laboratory alternate window"),
    (17, 46, 49, "image", "img_006", "", "Quantum lab concept image"),
    (18, 49, 52, "stock", "s05", "a", "Server core macro"),
    (19, 52, 55, "stock", "s05", "b", "Server core alternate window"),
    (20, 55, 58, "image", "img_007", "", "Moon base concept image"),
    (21, 58, 61, "stock", "s06", "a", "Lunar surface"),
    (22, 61, 64, "stock", "s06", "b", "Lunar surface alternate window"),
    (23, 64, 67, "image", "img_008", "", "Data chamber concept image"),
    (24, 67, 70, "stock", "s07", "a", "Data stream motion"),
    (25, 70, 73, "stock", "s07", "b", "Data stream alternate window"),
    (26, 73, 76, "image", "img_009", "", "Memory fracture concept image"),
    (27, 76, 78, "mg", "img_009", "", "Memory fold / local MG placeholder"),
    (28, 78, 81, "image", "img_010", "", "Hero beat two setup"),
    (29, 81, 84, "stock", "s08", "a", "Fast aerial city motion"),
    (30, 84, 86, "mg", "img_010", "", "Hero beat two / local 2.5D placeholder"),
    (31, 86, 89, "stock", "s08", "b", "Fast aerial alternate window"),
    (32, 89, 92, "image", "img_011", "", "Pursuit concept image"),
    (33, 92, 95, "stock", "s09", "a", "Industrial sparks crisis beat"),
    (34, 95, 98, "stock", "s09", "b", "Industrial sparks alternate window"),
    (35, 98, 101, "image", "img_013", "", "Hero beat three setup"),
    (36, 101, 104, "stock", "s10", "a", "Blue-white particles"),
    (37, 104, 106, "mg", "img_013", "", "Hero beat three / local 2.5D placeholder"),
    (38, 106, 109, "stock", "s10", "b", "Particles alternate window"),
    (39, 109, 112, "mg", "img_014", "", "White-field collapse / local MG placeholder"),
    (40, 112, 115, "image", "img_014", "", "Collapse concept image"),
    (41, 115, 118, "mg", "img_014", "", "Iris collapse / local MG placeholder"),
    (42, 118, 120, "mg", "img_015", "", "Title reveal / local MG placeholder"),
)


def _stable_digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_board(source_assets: Mapping[str, str]) -> tuple[BoardUnit, ...]:
    normalized = {str(slot): str(asset_id) for slot, asset_id in source_assets.items()}
    if set(normalized) != set(SOURCE_SLOTS):
        raise EchoBoardError("rough cut requires exactly the ten reviewed source slots")
    if any(not normalized[slot] for slot in SOURCE_SLOTS):
        raise EchoBoardError("reviewed source slots cannot contain empty asset ids")
    if len(set(normalized.values())) != len(SOURCE_SLOTS):
        raise EchoBoardError("the ten source slots must reference ten distinct assets")

    board = tuple(
        BoardUnit(
            index=index,
            start_sec=float(start),
            end_sec=float(end),
            kind=kind,
            asset_id=normalized[reference] if kind == "stock" else reference,
            reference=reference,
            take=take,
            description=description,
        )
        for index, start, end, kind, reference, take, description in _LOCKED_LAYOUT
    )
    validate_board(board)
    return board


def validate_board(board: tuple[BoardUnit, ...]) -> None:
    if len(board) != 42:
        raise EchoBoardError(f"Echo V1 requires 42 units, got {len(board)}")
    cursor = 0.0
    ids: set[str] = set()
    for expected_index, unit in enumerate(board, start=1):
        if unit.index != expected_index or unit.shot_id in ids:
            raise EchoBoardError("board ids/order are not deterministic")
        ids.add(unit.shot_id)
        if abs(unit.start_sec - cursor) > EPSILON:
            raise EchoBoardError(f"board gap/overlap before {unit.shot_id}")
        if unit.duration_sec <= 0 or unit.duration_sec > 3.0 + EPSILON:
            raise EchoBoardError(f"invalid duration for {unit.shot_id}")
        cursor = unit.end_sec
    if abs(cursor - 120.0) > EPSILON:
        raise EchoBoardError(f"board must end at 120 seconds, got {cursor}")

    stock = [unit for unit in board if unit.is_public_motion]
    local = [unit for unit in board if not unit.is_public_motion]
    if len(stock) != 20 or abs(sum(unit.duration_sec for unit in stock) - 60.0) > EPSILON:
        raise EchoBoardError("public motion must be exactly 20 x 3-second units / 60 seconds")
    if len(local) != 22 or abs(sum(unit.duration_sec for unit in local) - 60.0) > EPSILON:
        raise EchoBoardError("image and local-MG units must total 22 units / 60 seconds")

    for slot in SOURCE_SLOTS:
        uses = [unit for unit in stock if unit.reference == slot]
        if len(uses) != 2 or {unit.take for unit in uses} != {"a", "b"}:
            raise EchoBoardError(f"{slot} must be used exactly once as take a and once as take b")
        if len({unit.source_in for unit in uses}) != 2:
            raise EchoBoardError(f"{slot} takes must use different source-in windows")
        if len({unit.asset_id for unit in uses}) != 1:
            raise EchoBoardError(f"{slot} takes must retain one reviewed asset identity")


def build_shotlist(board: tuple[BoardUnit, ...]) -> dict[str, Any]:
    validate_board(board)
    shots: list[dict[str, Any]] = []
    for unit in board:
        role = "public_stock" if unit.is_public_motion else unit.kind
        note = (
            f"{BOARD_VERSION}|start={unit.start_sec:.3f}|end={unit.end_sec:.3f}"
            f"|role={role}|reference={unit.reference}"
        )
        if unit.is_public_motion:
            note += (
                f"|take={unit.take}|source_in={unit.source_in:.3f}"
                f"|source_out={unit.source_out:.3f}"
            )
        shots.append(
            {
                "id": unit.shot_id,
                "description": unit.description,
                "duration_sec": unit.duration_sec,
                "source": "search" if unit.is_public_motion else "unset",
                "asset_id": unit.asset_id,
                "clip_id": unit.clip_id,
                "status": "placed",
                "notes": note,
                "on_screen_text": "回声协议" if unit.index == 42 else None,
            }
        )
    return {
        "logline": "曙光预测系统决定终结人类，林迦进入量子核心做出不可预测的选择。",
        "style": (
            "real-footage-led science-fiction rough cut; existing concept images; "
            "deterministic local MG placeholders; no AI-video generation"
        ),
        "target_duration_sec": 120.0,
        "scenes": [{"id": "echo_v1", "title": "回声协议 V1", "shots": shots}],
    }


def reviewed_sources_from_manifest(
    manifest: Mapping[str, Any], registry: Any
) -> dict[str, str]:
    if str(manifest.get("status") or "") != "passed":
        raise EchoBoardError("source visual-review manifest has not passed")
    slots = manifest.get("slots")
    if not isinstance(slots, Mapping):
        raise EchoBoardError("source visual-review manifest has no slot map")

    result: dict[str, str] = {}
    for slot in SOURCE_SLOTS:
        entry = slots.get(slot)
        if not isinstance(entry, Mapping) or entry.get("machine_gate") != "passed":
            raise EchoBoardError(f"source slot {slot} lacks a passed machine gate")
        review = entry.get("review")
        if not isinstance(review, Mapping) or review.get("decision") != "approve":
            raise EchoBoardError(f"source slot {slot} lacks explicit visual approval")
        asset_id = str(entry.get("asset_id") or "")
        if not asset_id:
            raise EchoBoardError(f"source slot {slot} has no asset id")
        record = registry.get(asset_id)
        if str(getattr(record, "kind", "")) != "video":
            raise EchoBoardError(f"source slot {slot} is not a video asset")
        source = getattr(record, "source", {}) or {}
        if str(source.get("production_slot") or "") != slot:
            raise EchoBoardError(f"source slot {slot} registry binding changed")
        if str(source.get("production_source_status") or "") != "approved":
            raise EchoBoardError(f"source slot {slot} registry approval is missing")
        duration = float((entry.get("probe") or {}).get("duration_sec") or 0.0)
        if duration + EPSILON < 6.0:
            raise EchoBoardError(f"source slot {slot} cannot supply two 3-second windows")
        result[slot] = asset_id
    if len(set(result.values())) != 10:
        raise EchoBoardError("review manifest must bind ten distinct source assets")
    return result


def _load_reviewed_sources(runner: Any) -> tuple[dict[str, str], dict[str, Any]]:
    # Reuse the sourcing operator's byte/hash/contact-sheet checks.  Loading the
    # manifest is read-only and cannot search, download, or call a provider.
    from scripts import produce_echo_protocol_v1 as source_operator

    manifest = source_operator._load_source_manifest(runner, required=True)
    source_operator._assert_manifest_ready_for_transition(runner, manifest)
    mapping = reviewed_sources_from_manifest(manifest, runner.agent.registry)
    manifest_path = source_operator._source_manifest_path(runner)
    return mapping, {
        "status": "passed",
        "path": str(manifest_path),
        "sha256": source_operator._sha256_file(manifest_path),
        "approved_slots": list(SOURCE_SLOTS),
    }


EXPECTED_NARRATION_ASSETS = tuple(f"aud_{index:03d}" for index in range(4, 17))
EXPECTED_MUSIC_ASSET = "aud_017"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _probe_duration(metadata: Mapping[str, Any]) -> float:
    raw = (metadata.get("format") or {}).get("duration")
    if raw is None:
        stream = video_stream(dict(metadata)) or {}
        raw = stream.get("duration")
    try:
        return float(raw or 0.0)
    except (TypeError, ValueError):
        return 0.0


def inspect_board_assets(
    runner: Any,
    board: tuple[BoardUnit, ...],
    *,
    probe_fn=ffprobe_metadata,
) -> dict[str, dict[str, Any]]:
    """Registry + bytes + ffprobe + source-range gate, before any patch."""

    facts: dict[str, dict[str, Any]] = {}
    for unit in board:
        if unit.asset_id in facts:
            continue
        record = runner.agent.registry.get(unit.asset_id)
        expected_kind = "video" if unit.is_public_motion else "image"
        if str(getattr(record, "kind", "")) != expected_kind:
            raise EchoBoardError(
                f"registry kind mismatch for {unit.asset_id}: expected {expected_kind}"
            )
        path = Path(getattr(record, "path", ""))
        if not path.is_file() or path.stat().st_size <= 0:
            raise EchoBoardError(f"asset file is missing or empty: {unit.asset_id}")
        registered_hash = str(getattr(record, "sha256", "") or "")
        actual_hash = _sha256_file(path)
        if not registered_hash or registered_hash != actual_hash:
            raise EchoBoardError(f"asset registry hash mismatch: {unit.asset_id}")
        try:
            metadata = probe_fn(path)
        except Exception as exc:
            raise EchoBoardError(f"ffprobe failed for {unit.asset_id}: {exc}") from exc
        stream = video_stream(metadata)
        if stream is None or int(stream.get("width") or 0) <= 0 or int(
            stream.get("height") or 0
        ) <= 0:
            raise EchoBoardError(f"asset has no decodable visual stream: {unit.asset_id}")
        duration = _probe_duration(metadata)
        facts[unit.asset_id] = {
            "asset_id": unit.asset_id,
            "kind": expected_kind,
            "path": str(path),
            "sha256": actual_hash,
            "width": int(stream.get("width") or 0),
            "height": int(stream.get("height") or 0),
            "duration_sec": duration,
            "asset": {
                "id": unit.asset_id,
                "asset_id": unit.asset_id,
                "name": path.name,
                "media_kind": expected_kind,
                "mime_type": mimetypes.guess_type(path.name)[0] or "",
                "source_path": str(path),
                "duration": duration if expected_kind == "video" else 3.0,
                "metadata": {
                    "sha256": actual_hash,
                    "width": int(stream.get("width") or 0),
                    "height": int(stream.get("height") or 0),
                    "duration": duration if expected_kind == "video" else 3.0,
                    "production_run_id": RUN_ID,
                },
            },
        }

    for unit in board:
        if unit.is_public_motion:
            duration = float(facts[unit.asset_id]["duration_sec"])
            if duration + EPSILON < unit.source_out:
                raise EchoBoardError(
                    f"{unit.shot_id} source range ends at {unit.source_out}, "
                    f"but {unit.asset_id} is only {duration} seconds"
                )
    return facts


def board_digest(board: tuple[BoardUnit, ...]) -> str:
    return _stable_digest(
        [
            {
                "clip_id": unit.clip_id,
                "shot_id": unit.shot_id,
                "start_sec": unit.start_sec,
                "end_sec": unit.end_sec,
                "kind": unit.kind,
                "asset_id": unit.asset_id,
                "source_in": unit.source_in if unit.is_public_motion else 0.0,
                "source_out": unit.source_out,
            }
            for unit in board
        ]
    )


def _project_handle(runner: Any) -> Any:
    project = getattr(getattr(runner, "agent", None), "project", None)
    if project is None:
        tool_ctx = getattr(getattr(runner, "agent", None), "_tool_ctx", None)
        project = getattr(tool_ctx, "project", None)
    if project is None or not callable(getattr(project, "load", None)):
        raise EchoBoardError("runner does not expose its canonical project handle")
    return project


def build_rough_cut_ops(
    current_state: Mapping[str, Any],
    board: tuple[BoardUnit, ...],
    facts: Mapping[str, Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], str, str]:
    digest = board_digest(board)
    trace_id = f"trace-echo-rough-cut-{digest[:20]}"
    ops: list[dict[str, Any]] = [
        {"op": "set_project_title", "title": "回声协议"},
        {"op": "set_shotlist", "shotlist": build_shotlist(board)},
    ]
    for clip in (current_state.get("timeline") or {}).get("clips") or []:
        if str(clip.get("track_id") or "") in {"V1", "OV1"}:
            ops.append(
                {
                    "op": "delete_clip",
                    "clip_id": str(clip.get("id") or ""),
                    "ripple": False,
                    "provenance": {"run_id": RUN_ID, "trace_id": trace_id},
                }
            )

    # Only the ten newly sourced public videos need adding to project assets;
    # the migrated image placeholders must already exist in the project.
    source_by_slot = {
        unit.reference: unit.asset_id for unit in board if unit.is_public_motion
    }
    for slot in SOURCE_SLOTS:
        ops.append(
            {
                "op": "upsert_asset",
                "asset": dict(facts[source_by_slot[slot]]["asset"]),
            }
        )

    for unit in board:
        fact = facts[unit.asset_id]
        clip: dict[str, Any] = {
            "id": unit.clip_id,
            "asset_id": unit.asset_id,
            "track_id": "V1",
            "name": Path(str(fact["path"])).name,
            "media_kind": "video" if unit.is_public_motion else "image",
            "duration": unit.duration_sec,
            "source_in": unit.source_in if unit.is_public_motion else 0.0,
            "source_out": unit.source_out if unit.is_public_motion else unit.duration_sec,
            "enabled": True,
        }
        if unit.is_public_motion:
            clip["effects"] = {"muted": True}
        provenance = {
            "source": "echo_protocol_v1_board",
            "run_id": RUN_ID,
            "trace_id": trace_id,
            "unit_id": unit.shot_id,
            "board_digest": digest,
        }
        ops.append(
            {
                "op": "insert_clip",
                "track_id": "V1",
                "at": {"time": unit.start_sec},
                "ripple": False,
                "data": {"clip": clip},
                "provenance": provenance,
            }
        )
    return ops, trace_id, f"{trace_id}:{BOARD_VERSION}"


def _budget(run: Mapping[str, Any]) -> dict[str, Any]:
    value = run.get("budget")
    if not isinstance(value, Mapping):
        raise EchoBoardError("production run has no canonical budget view")
    return dict(value)


def _assert_no_veo(budget: Mapping[str, Any]) -> None:
    calls = int(budget.get("veo_reserved_calls") or 0)
    duration = float(budget.get("veo_reserved_duration_sec") or 0.0)
    if calls != 0 or abs(duration) > EPSILON:
        raise EchoBoardError(
            f"Echo V1 rough cut forbids Veo: calls={calls}, duration={duration}"
        )


def _project_state(runner: Any) -> dict[str, Any]:
    state = _project_handle(runner).load()
    if not isinstance(state, dict):
        raise EchoBoardError("canonical project state is unreadable")
    return state


def _protected_audio_snapshot(state: Mapping[str, Any]) -> list[dict[str, Any]]:
    protected = []
    for clip in (state.get("timeline") or {}).get("clips") or []:
        if str(clip.get("track_id") or "") not in {"A1", "A2"}:
            continue
        protected.append(
            {
                "id": str(clip.get("id") or ""),
                "asset_id": str(clip.get("asset_id") or ""),
                "track_id": str(clip.get("track_id") or ""),
                "start": float(clip.get("start") or 0.0),
                "duration": float(clip.get("duration") or 0.0),
                "source_in": float(clip.get("source_in") or 0.0),
                "source_out": float(clip.get("source_out") or 0.0),
                "enabled": bool(clip.get("enabled", True)),
                "effects": dict(clip.get("effects") or {}),
            }
        )
    return sorted(protected, key=lambda item: item["id"])


def _assert_expected_audio(state: Mapping[str, Any]) -> list[dict[str, Any]]:
    snapshot = _protected_audio_snapshot(state)
    music = [item for item in snapshot if item["asset_id"] == EXPECTED_MUSIC_ASSET]
    narration = [
        item for item in snapshot if item["asset_id"] in EXPECTED_NARRATION_ASSETS
    ]
    if len(music) != 1 or music[0]["track_id"] != "A1":
        raise EchoBoardError("rough cut must preserve aud_017 music on A1")
    if len(narration) != 13 or any(item["track_id"] != "A2" for item in narration):
        raise EchoBoardError("rough cut must preserve 13 narration clips on A2")
    if {item["asset_id"] for item in narration} != set(EXPECTED_NARRATION_ASSETS):
        raise EchoBoardError("A2 narration asset set is incomplete")
    if len(snapshot) != 14:
        raise EchoBoardError("A1/A2 must contain exactly one music and 13 narration clips")
    return snapshot


def validate_persisted_board(
    runner: Any, board: tuple[BoardUnit, ...], *, expected_digest: str | None = None
) -> dict[str, Any]:
    state = _project_state(runner)
    digest = expected_digest or board_digest(board)
    shotlist = state.get("shotlist") or {}
    shots = [shot for _scene, shot in iter_shots(shotlist)]
    if len(shots) != 42 or [str(shot.get("id")) for shot in shots] != [
        unit.shot_id for unit in board
    ]:
        raise EchoBoardError("persisted shotlist is not the locked 42-unit board")
    if abs(float(shotlist.get("target_duration_sec") or 0.0) - 120.0) > EPSILON:
        raise EchoBoardError("persisted shotlist target is not 120 seconds")

    clips = list((state.get("timeline") or {}).get("clips") or [])
    by_id = {str(clip.get("id") or ""): clip for clip in clips}
    v1_clips = [clip for clip in clips if str(clip.get("track_id") or "") == "V1"]
    if len(v1_clips) != 42:
        raise EchoBoardError(f"rough cut must have exactly 42 V1 clips, got {len(v1_clips)}")
    if any(str(clip.get("track_id") or "") == "OV1" for clip in clips):
        raise EchoBoardError("rough cut still contains an old OV1 visual/text clip")

    motion_seconds = 0.0
    for unit, shot in zip(board, shots):
        clip_id = str(shot.get("clip_id") or "")
        clip = by_id.get(clip_id)
        if clip is None or str(shot.get("status") or "") != "placed":
            raise EchoBoardError(f"{unit.shot_id} has no persisted placed clip")
        if clip_id != unit.clip_id:
            raise EchoBoardError(f"{unit.shot_id} does not use stable clip id {unit.clip_id}")
        if str(clip.get("asset_id") or "") != unit.asset_id:
            raise EchoBoardError(f"{unit.shot_id} asset identity changed")
        if abs(float(clip.get("start") or 0.0) - unit.start_sec) > EPSILON:
            raise EchoBoardError(f"{unit.shot_id} start time changed")
        if abs(float(clip.get("duration") or 0.0) - unit.duration_sec) > EPSILON:
            raise EchoBoardError(f"{unit.shot_id} duration changed")
        if unit.is_public_motion:
            if abs(float(clip.get("source_in") or 0.0) - unit.source_in) > EPSILON:
                raise EchoBoardError(f"{unit.shot_id} source-in window changed")
            if abs(float(clip.get("source_out") or 0.0) - unit.source_out) > EPSILON:
                raise EchoBoardError(f"{unit.shot_id} source-out window changed")
            if (clip.get("effects") or {}).get("muted") is not True:
                raise EchoBoardError(f"{unit.shot_id} public footage must be muted")
            motion_seconds += unit.duration_sec
        elif clip.get("transition_after"):
            raise EchoBoardError(f"{unit.shot_id} image/MG placeholder has a transition")
        provenance = clip.get("provenance") or {}
        if (
            str(provenance.get("run_id") or "") != RUN_ID
            or str(provenance.get("unit_id") or "") != unit.shot_id
            or str(provenance.get("board_digest") or "") != digest
            or not str(provenance.get("trace_id") or "")
        ):
            raise EchoBoardError(f"{unit.shot_id} production provenance is incomplete")
        dropped = clip_dropped_fields(clip)
        if dropped:
            raise EchoBoardError(f"{unit.shot_id} has unsupported fields: {dropped}")
    if abs(motion_seconds - 60.0) > EPSILON:
        raise EchoBoardError("persisted public-footage motion coverage is not 60 seconds")
    timeline_duration = float((state.get("timeline") or {}).get("duration") or 0.0)
    if abs(timeline_duration - 120.0) > EPSILON:
        raise EchoBoardError(f"canonical timeline duration is {timeline_duration}, not 120")
    all_dropped = [
        {"clip_id": str(clip.get("id") or ""), **item}
        for clip in clips
        for item in clip_dropped_fields(clip)
    ]
    if all_dropped:
        raise EchoBoardError(f"rough-cut dry run has dropped fields: {all_dropped}")
    audio = _assert_expected_audio(state)
    return {
        "shot_count": len(shots),
        "v1_clip_count": len(v1_clips),
        "duration_sec": timeline_duration,
        "public_motion_sec": motion_seconds,
        "protected_audio_clip_ids": [item["id"] for item in audio],
        "dropped_fields": [],
    }


def dry_run_rough_cut_patch(
    current_state: Mapping[str, Any],
    ops: list[dict[str, Any]],
    board: tuple[BoardUnit, ...],
    *,
    expected_digest: str,
) -> dict[str, Any]:
    before_audio = _assert_expected_audio(current_state)
    dry_state = apply_timeline_patches(
        dict(current_state), [{"version": 1, "ops": ops}]
    )
    after_audio = _assert_expected_audio(dry_state)
    if after_audio != before_audio:
        raise EchoBoardError("rough-cut patch changes protected A1/A2 clips or timecodes")

    # Validate the dry state without offering it to ProjectStore.  This mirrors
    # validate_persisted_board but is intentionally pure/in-memory.
    shotlist = dry_state.get("shotlist") or {}
    shots = [shot for _scene, shot in iter_shots(shotlist)]
    clips = list((dry_state.get("timeline") or {}).get("clips") or [])
    by_id = {str(clip.get("id") or ""): clip for clip in clips}
    v1 = [clip for clip in clips if str(clip.get("track_id") or "") == "V1"]
    if len(shots) != 42 or len(v1) != 42:
        raise EchoBoardError("rough-cut dry run is not 42 shots / 42 V1 clips")
    if any(str(clip.get("track_id") or "") == "OV1" for clip in clips):
        raise EchoBoardError("rough-cut dry run retained an OV1 clip")
    for unit, shot in zip(board, shots):
        clip = by_id.get(unit.clip_id)
        if clip is None or str(shot.get("clip_id") or "") != unit.clip_id:
            raise EchoBoardError(f"dry run lost stable identity for {unit.shot_id}")
        provenance = clip.get("provenance") or {}
        if str(provenance.get("board_digest") or "") != expected_digest:
            raise EchoBoardError(f"dry run provenance mismatch for {unit.shot_id}")
    if abs(float((dry_state.get("timeline") or {}).get("duration") or 0.0) - 120.0) > EPSILON:
        raise EchoBoardError("rough-cut dry run is not exactly 120 seconds")
    dropped = [
        {"clip_id": str(clip.get("id") or ""), **item}
        for clip in clips
        for item in clip_dropped_fields(clip)
    ]
    if dropped:
        raise EchoBoardError(f"rough-cut dry run has dropped fields: {dropped}")
    return dry_state


def execute_rough_cut(
    manager: Any,
    runner: Any,
    source_assets: Mapping[str, str],
    *,
    source_review: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute the formal rough-cut stage against an opened durable runner."""

    board = build_board(source_assets)
    digest = board_digest(board)
    facts = inspect_board_assets(runner, board)
    run = manager.get_run(runner.project_id, runner.run_id)
    state = str(run.get("production_state") or run.get("state") or "")
    current_budget = _budget(run)
    _assert_no_veo(current_budget)

    # A process may have died after the state transition but before printing
    # the CLI result.  A verified sound-pass board is the idempotent receipt.
    if state == "sound_pass":
        persisted = validate_persisted_board(runner, board, expected_digest=digest)
        return {
            "ok": True,
            "replayed": True,
            "production_state": "sound_pass",
            "project_revision": int(runner.project_revision),
            "board_version": BOARD_VERSION,
            **persisted,
            "budget": current_budget,
            "veo_calls": 0,
        }
    if state != "rough_cut":
        raise EchoBoardError(f"rough-cut operator requires rough_cut state, got {state}")

    budget_before = current_budget
    budget_digest_before = _stable_digest(budget_before)
    shotlist = build_shotlist(board)
    shotlist_digest = _stable_digest(shotlist)
    project = _project_handle(runner)
    current_state = project.load()
    expected_ids = {unit.clip_id for unit in board}
    present_ids = {
        str(clip.get("id") or "")
        for clip in (current_state.get("timeline") or {}).get("clips") or []
    } & expected_ids
    if present_ids and present_ids != expected_ids:
        raise EchoBoardError(
            "partial deterministic rough cut detected; refusing blind rebuild: "
            f"{len(present_ids)}/42 stable clips"
        )

    meta_before = project.store.load_meta(project.project_id)
    patch_seq_before = int(meta_before.get("patch_seq") or 0)
    project_revision_before = int(runner.project_revision)
    patch_applied = False
    if present_ids == expected_ids:
        persisted = validate_persisted_board(runner, board, expected_digest=digest)
        patch_seq_after = patch_seq_before
        project_revision_after = project_revision_before
        patch_trace = str(
            next(
                clip.get("provenance", {}).get("trace_id")
                for clip in (current_state.get("timeline") or {}).get("clips") or []
                if str(clip.get("id") or "") == board[0].clip_id
            )
        )
    else:
        ops, patch_trace, patch_label = build_rough_cut_ops(current_state, board, facts)
        dry_run_rough_cut_patch(
            current_state, ops, board, expected_digest=digest
        )
        runner.run_project_edit(
            lambda: project.apply_ops(ops, label=patch_label),
            expected_project_revision=project_revision_before,
            timeout=300,
        )
        patch_applied = True
        meta_after = project.store.load_meta(project.project_id)
        patch_seq_after = int(meta_after.get("patch_seq") or 0)
        project_revision_after = int(runner.project_revision)
        if patch_seq_after != patch_seq_before + 1:
            raise EchoBoardError(
                f"rough cut must commit one patch, seq {patch_seq_before} -> {patch_seq_after}"
            )
        if project_revision_after != project_revision_before + 1:
            raise EchoBoardError(
                "rough cut must advance the project revision exactly once: "
                f"{project_revision_before} -> {project_revision_after}"
            )
        persisted = validate_persisted_board(runner, board, expected_digest=digest)

    run_after = manager.get_run(runner.project_id, runner.run_id)
    budget_after = _budget(run_after)
    _assert_no_veo(budget_after)
    if _stable_digest(budget_after) != budget_digest_before:
        raise EchoBoardError("zero-cost rough-cut mutations changed the media budget")

    source_windows = [
        {
            "clip_id": unit.clip_id,
            "shot_id": unit.shot_id,
            "slot": unit.reference,
            "take": unit.take,
            "asset_id": unit.asset_id,
            "source_in": unit.source_in,
            "source_out": unit.source_out,
        }
        for unit in board
        if unit.is_public_motion
    ]
    evidence_id = f"ev-echo-rough-cut-v1-{digest[:16]}"
    evidence_trace = f"trace-{evidence_id}"
    manager.record_evidence(
        runner.project_id,
        runner.run_id,
        evidence_id=evidence_id,
        kind="rough_cut_board",
        project_revision=int(runner.project_revision),
        trace_id=evidence_trace,
        payload={
            "board_version": BOARD_VERSION,
            "board_digest": digest,
            "shotlist_digest": shotlist_digest,
            "patch_trace_id": patch_trace,
            "patch_seq": patch_seq_after,
            "source_review": dict(source_review or {"status": "passed"}),
            "source_windows": source_windows,
            "asset_checks": [
                {
                    key: facts[asset_id][key]
                    for key in (
                        "asset_id", "kind", "path", "sha256", "width", "height", "duration_sec"
                    )
                }
                for asset_id in sorted(facts)
            ],
            "checks": {
                **persisted,
                "unit_count": 42,
                "public_asset_count": 10,
                "public_clip_count": 20,
                "public_motion_sec": 60.0,
                "max_unit_duration_sec": 3.0,
                "ai_video_generation_calls": 0,
                "budget_unchanged": True,
                "single_atomic_patch": True,
            },
            "budget_before": budget_before,
            "budget_after": budget_after,
        },
    )
    latest = manager.get_run(runner.project_id, runner.run_id)
    transition = manager.transition_run(
        runner.project_id,
        runner.run_id,
        "sound_pass",
        expected_revision=int(latest.get("production_revision") or latest.get("revision") or 0),
        trace_id="trace-echo-rough-cut-v1-complete",
    )
    return {
        "ok": True,
        "replayed": False,
        "production_state": str(
            transition.get("production_state") or transition.get("state") or "sound_pass"
        ),
        "project_revision": int(runner.project_revision),
        "board_version": BOARD_VERSION,
        "evidence_id": evidence_id,
        "patch_applied": patch_applied,
        "patch_seq": patch_seq_after,
        "patch_trace_id": patch_trace,
        **persisted,
        "budget": budget_after,
        "veo_calls": 0,
    }


def rough_cut(output_root: Path) -> dict[str, Any]:
    manager = SessionManager(
        output_root=output_root,
        max_sessions=2,
        idle_timeout_sec=0,
        sweep_interval_sec=0,
    )
    runner = manager.resume_session(SESSION_ID)
    try:
        if str(runner.run_id) != RUN_ID:
            raise EchoBoardError(f"unexpected production run: {runner.run_id}")
        source_assets, source_review = _load_reviewed_sources(runner)
        return execute_rough_cut(
            manager,
            runner,
            source_assets,
            source_review=source_review,
        )
    finally:
        manager.close_all()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("rough-cut",))
    parser.add_argument(
        "--output-root",
        type=Path,
        default=Path.home() / ".gemia" / "v3",
    )
    args = parser.parse_args()
    result = rough_cut(args.output_root.expanduser().resolve())
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
