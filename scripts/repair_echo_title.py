#!/usr/bin/env python3
"""Repair only the 118–120 second title card in Echo Protocol V1.

The first full render exposed a real delivery blocker: FFmpeg accepted the
private PingFang UI collection but rendered two title glyphs as LastResort
boxes.  This operator installs the already-proven CJK-font correction as one
new local asset and one atomic project patch.  It never calls a provider,
never spends media budget, never rewrites the other 41 visual units, and can
resume after a crash between render, registry, patch, evidence, and state
transition.

This is intentionally a production operator, not a general title editor.  Its
scope is bound to the reviewed ``echo_v1_u42`` interval and review evidence.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import mimetypes
import sys
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager, suppress
from copy import deepcopy
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
with suppress(ValueError):
    sys.path.remove(str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT))

from gemia import echo_local_media as local_media  # noqa: E402
from gemia.echo_local_media import (  # noqa: E402
    probe_media,
    render_image_motion,
    sha256_file,
)
from gemia.project_model import iter_shots  # noqa: E402
from gemia.session_manager import SessionManager  # noqa: E402
from lumerai.export_support import clip_dropped_fields  # noqa: E402
from lumerai.patches import apply_timeline_patches  # noqa: E402
from scripts import build_echo_protocol_v1 as board_builder  # noqa: E402
from scripts import echo_visual_pass as visual_pass  # noqa: E402

SESSION_ID = board_builder.SESSION_ID
RUN_ID = board_builder.RUN_ID
REPAIR_VERSION = "echo-title-cjk-repair-1"
TARGET_CLIP_ID = "echo_v1_u42"
TARGET_SHOT_ID = "echo_v1_42"
TARGET_UNIT_INDEX = 42
TARGET_START_SEC = 118.0
TARGET_DURATION_SEC = 2.0
TARGET_TEXT = "回声协议"
REVIEW_EVIDENCE_ID = "ev-737dc453579c"
EPSILON = board_builder.EPSILON


class EchoTitleRepairError(RuntimeError):
    """The title repair cannot prove an exact, recoverable local mutation."""


def _stable_digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _record_value(record: Any, name: str, default: Any = None) -> Any:
    if isinstance(record, Mapping):
        return record.get(name, default)
    return getattr(record, name, default)


def _project_handle(runner: Any) -> Any:
    project = getattr(getattr(runner, "agent", None), "project", None)
    if project is None:
        project = getattr(runner, "project", None)
    if project is None or not callable(getattr(project, "load", None)):
        raise EchoTitleRepairError(
            "runner does not expose its canonical project handle"
        )
    return project


def _registry(runner: Any) -> Any:
    registry = getattr(getattr(runner, "agent", None), "registry", None)
    if registry is None or not callable(getattr(registry, "get", None)):
        raise EchoTitleRepairError("runner does not expose its durable asset registry")
    return registry


def _budget(run: Mapping[str, Any]) -> dict[str, Any]:
    value = run.get("budget")
    if not isinstance(value, Mapping):
        raise EchoTitleRepairError("production run has no canonical budget view")
    return dict(value)


def _assert_no_veo(budget: Mapping[str, Any]) -> None:
    calls = int(budget.get("veo_reserved_calls") or 0)
    duration = float(budget.get("veo_reserved_duration_sec") or 0.0)
    if calls != 0 or abs(duration) > EPSILON:
        raise EchoTitleRepairError(
            f"title repair forbids Veo: calls={calls}, duration={duration}"
        )


def _is_tmp_path(value: str | Path) -> bool:
    path = Path(value).expanduser().resolve(strict=False)
    roots = (Path("/tmp"), Path("/private/tmp"))
    return any(path == root or root in path.parents for root in roots)


def _clips(state: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [
        dict(item)
        for item in (state.get("timeline") or {}).get("clips") or []
        if isinstance(item, Mapping)
    ]


def _clip_by_id(state: Mapping[str, Any], clip_id: str) -> dict[str, Any]:
    matches = [clip for clip in _clips(state) if str(clip.get("id") or "") == clip_id]
    if len(matches) != 1:
        raise EchoTitleRepairError(f"expected one {clip_id} clip, found {len(matches)}")
    return matches[0]


def _shots(state: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [deepcopy(shot) for _scene, shot in iter_shots(state.get("shotlist") or {})]


def _shot_by_id(state: Mapping[str, Any], shot_id: str) -> dict[str, Any]:
    matches = [shot for shot in _shots(state) if str(shot.get("id") or "") == shot_id]
    if len(matches) != 1:
        raise EchoTitleRepairError(f"expected one {shot_id} shot, found {len(matches)}")
    return matches[0]


def _project_assets(state: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for raw in state.get("assets") or []:
        if not isinstance(raw, Mapping):
            continue
        asset_id = str(raw.get("id") or raw.get("asset_id") or "")
        if not asset_id or asset_id in result:
            raise EchoTitleRepairError(
                f"duplicate or empty project asset id: {asset_id!r}"
            )
        result[asset_id] = deepcopy(dict(raw))
    return result


def _audio_snapshot(state: Mapping[str, Any]) -> list[dict[str, Any]]:
    return sorted(
        [
            deepcopy(clip)
            for clip in _clips(state)
            if str(clip.get("media_kind") or "") == "audio"
        ],
        key=lambda clip: str(clip.get("id") or ""),
    )


def _non_target_clip_snapshot(state: Mapping[str, Any]) -> list[dict[str, Any]]:
    return sorted(
        [
            deepcopy(clip)
            for clip in _clips(state)
            if str(clip.get("id") or "") != TARGET_CLIP_ID
        ],
        key=lambda clip: str(clip.get("id") or ""),
    )


def _non_target_shot_snapshot(state: Mapping[str, Any]) -> list[dict[str, Any]]:
    return sorted(
        [shot for shot in _shots(state) if str(shot.get("id") or "") != TARGET_SHOT_ID],
        key=lambda shot: str(shot.get("id") or ""),
    )


def _assert_record_bytes(record: Any, *, asset_id: str) -> tuple[Path, str]:
    if str(_record_value(record, "kind", "")) != "video":
        raise EchoTitleRepairError(f"title asset is not video: {asset_id}")
    path = Path(str(_record_value(record, "path", ""))).expanduser().resolve()
    if _is_tmp_path(path):
        raise EchoTitleRepairError(
            f"title asset cannot reference temporary storage: {path}"
        )
    if not path.is_file() or path.stat().st_size <= 0:
        raise EchoTitleRepairError(f"title asset file is missing or empty: {asset_id}")
    actual_hash = sha256_file(path)
    if str(_record_value(record, "sha256", "") or "") != actual_hash:
        raise EchoTitleRepairError(f"title asset hash changed: {asset_id}")
    return path, actual_hash


def _validate_locked_state(
    runner: Any,
    state: Mapping[str, Any],
    board: tuple[board_builder.BoardUnit, ...],
) -> tuple[str, str]:
    """Validate the whole locked board and classify only u42 as pre/post."""

    board_builder.validate_board(board)
    expected_ids = [unit.clip_id for unit in board]
    clips = _clips(state)
    v1 = [clip for clip in clips if str(clip.get("track_id") or "") == "V1"]
    if len(v1) != 42 or sorted(str(clip.get("id") or "") for clip in v1) != sorted(
        expected_ids
    ):
        raise EchoTitleRepairError(
            "title repair requires the locked 42 stable V1 clips"
        )
    if any(str(clip.get("track_id") or "") == "OV1" for clip in clips):
        raise EchoTitleRepairError(
            "title repair refuses a timeline containing legacy OV1 clips"
        )
    if any(str(clip.get("media_kind") or "") != "video" for clip in v1):
        raise EchoTitleRepairError("all 42 V1 units must already be canonical video")

    by_id = {str(clip.get("id") or ""): clip for clip in v1}
    board_digest = board_builder.board_digest(board)
    for unit in board:
        clip = by_id[unit.clip_id]
        if (
            abs(float(clip.get("start") or 0.0) - unit.start_sec) > EPSILON
            or abs(float(clip.get("duration") or 0.0) - unit.duration_sec) > EPSILON
        ):
            raise EchoTitleRepairError(f"locked timing changed for {unit.shot_id}")
        if unit.is_public_motion and (
            str(clip.get("asset_id") or "") != unit.asset_id
            or abs(float(clip.get("source_in") or 0.0) - unit.source_in) > EPSILON
            or abs(float(clip.get("source_out") or 0.0) - unit.source_out) > EPSILON
            or (clip.get("effects") or {}).get("muted") is not True
        ):
            raise EchoTitleRepairError(
                f"reviewed public footage changed: {unit.shot_id}"
            )
        provenance = dict(clip.get("provenance") or {})
        if (
            str(provenance.get("run_id") or "") != RUN_ID
            or str(provenance.get("unit_id") or "") != unit.shot_id
            or str(provenance.get("board_digest") or "") != board_digest
        ):
            raise EchoTitleRepairError(f"production provenance changed: {unit.shot_id}")
        if (
            unit.clip_id != TARGET_CLIP_ID
            and not unit.is_public_motion
            and str(provenance.get("source") or "") != "echo_protocol_v1_visual_pass"
        ):
            raise EchoTitleRepairError(
                f"non-target local visual changed: {unit.shot_id}"
            )
        dropped = clip_dropped_fields(clip)
        if dropped:
            raise EchoTitleRepairError(
                f"{unit.shot_id} contains unsupported fields: {dropped}"
            )

    shots = _shots(state)
    if len(shots) != 42 or [str(shot.get("id") or "") for shot in shots] != [
        unit.shot_id for unit in board
    ]:
        raise EchoTitleRepairError("shotlist is not the locked 42-unit board")
    for unit, shot in zip(board, shots, strict=True):
        if (
            str(shot.get("clip_id") or "") != unit.clip_id
            or str(shot.get("status") or "") != "placed"
        ):
            raise EchoTitleRepairError(f"shot/clip binding changed: {unit.shot_id}")

    audio = _audio_snapshot(state)
    board_builder._assert_expected_audio(state)
    a3 = [clip for clip in audio if str(clip.get("track_id") or "") == "A3"]
    if len(audio) != 25 or len(a3) != 11:
        raise EchoTitleRepairError(
            f"completed sound pass changed: expected 25 audio / 11 SFX, got {len(audio)} / {len(a3)}"
        )
    if (
        abs(float((state.get("timeline") or {}).get("duration") or 0.0) - 120.0)
        > EPSILON
    ):
        raise EchoTitleRepairError("title repair requires an exact 120-second timeline")
    dropped = [
        {"clip_id": str(clip.get("id") or ""), **item}
        for clip in clips
        for item in clip_dropped_fields(clip)
    ]
    if dropped:
        raise EchoTitleRepairError(f"timeline contains unsupported fields: {dropped}")

    target = by_id[TARGET_CLIP_ID]
    target_shot = _shot_by_id(state, TARGET_SHOT_ID)
    if (
        abs(float(target.get("start") or 0.0) - TARGET_START_SEC) > EPSILON
        or abs(float(target.get("duration") or 0.0) - TARGET_DURATION_SEC) > EPSILON
        or str(target_shot.get("on_screen_text") or "") != TARGET_TEXT
        or str(target_shot.get("asset_id") or "") != str(target.get("asset_id") or "")
    ):
        raise EchoTitleRepairError(
            "target title identity or 118–120 second range changed"
        )
    provenance = dict(target.get("provenance") or {})
    source_name = str(provenance.get("source") or "")
    if source_name == "echo_protocol_v1_visual_pass":
        if (
            provenance.get("source_asset_id") is not None
            or str(provenance.get("style") or "") != "title"
        ):
            raise EchoTitleRepairError("pre-repair title provenance is incomplete")
        mode = "pre"
        old_asset_id = str(target.get("asset_id") or "")
    elif source_name == "echo_protocol_title_repair":
        required = {
            "repair_version": REPAIR_VERSION,
            "review_evidence_id": REVIEW_EVIDENCE_ID,
            "title_text": TARGET_TEXT,
        }
        if any(
            str(provenance.get(key) or "") != value for key, value in required.items()
        ):
            raise EchoTitleRepairError(
                "persisted title-repair provenance is incomplete"
            )
        repair_fingerprint = str(provenance.get("repair_fingerprint") or "")
        old_asset_id = str(provenance.get("replaces_asset_id") or "")
        if not repair_fingerprint or not old_asset_id:
            raise EchoTitleRepairError(
                "persisted title repair lacks fingerprint/replaced asset"
            )
        mode = "post"
    else:
        raise EchoTitleRepairError("mixed or unknown title-repair state detected")

    registry = _registry(runner)
    try:
        old_record = registry.get(old_asset_id)
    except KeyError as exc:
        raise EchoTitleRepairError(
            f"replaced title asset vanished: {old_asset_id}"
        ) from exc
    _assert_record_bytes(old_record, asset_id=old_asset_id)
    if old_asset_id not in _project_assets(state):
        raise EchoTitleRepairError("old title project asset must remain recoverable")
    return mode, old_asset_id


def _repair_identity(
    output_dir: Path,
    *,
    font_resolver: Callable[[], Path],
) -> dict[str, Any]:
    font_path = Path(font_resolver()).expanduser().resolve()
    if not font_path.is_file():
        raise EchoTitleRepairError(f"resolved CJK font is missing: {font_path}")
    font_hash = sha256_file(font_path)
    fingerprint = _stable_digest(
        {
            "repair_version": REPAIR_VERSION,
            "clip_id": TARGET_CLIP_ID,
            "shot_id": TARGET_SHOT_ID,
            "start_sec": TARGET_START_SEC,
            "duration_sec": TARGET_DURATION_SEC,
            "text": TARGET_TEXT,
            "font": {"path": str(font_path), "sha256": font_hash},
            "output_spec": {
                "width": local_media.VIDEO_WIDTH,
                "height": local_media.VIDEO_HEIGHT,
                "fps": local_media.VIDEO_FPS,
                "codec": "h264",
                "pixel_format": "yuv420p",
                "audio_streams": 0,
            },
        }
    )
    root = output_dir.expanduser().resolve() / "production-media" / RUN_ID / "repairs"
    path = root / f"{TARGET_CLIP_ID}-title-cjk-{fingerprint[:16]}.mp4"
    if _is_tmp_path(path):
        raise EchoTitleRepairError("repair output must use persistent project storage")
    return {
        "repair_fingerprint": fingerprint,
        "font_path": font_path,
        "font_sha256": font_hash,
        "path": path,
    }


def _verify_rendered_title(
    board: tuple[board_builder.BoardUnit, ...],
    identity: Mapping[str, Any],
    rendered: Mapping[str, Any],
    *,
    probe_fn: Callable[[str | Path], dict[str, Any]],
    old_asset_id: str,
) -> dict[str, Any]:
    unit = next(unit for unit in board if unit.clip_id == TARGET_CLIP_ID)
    try:
        item = visual_pass._verified_render_result(
            unit,
            style="title",
            requested_path=Path(identity["path"]),
            rendered=rendered,
            probe_fn=probe_fn,
        )
    except Exception as exc:
        raise EchoTitleRepairError(
            f"corrective title render failed validation: {exc}"
        ) from exc
    base_source = dict(item["source"])
    enriched_source = {
        **base_source,
        "production_run_id": RUN_ID,
        "production_unit_id": TARGET_SHOT_ID,
        "production_unit_index": TARGET_UNIT_INDEX,
        "production_style": "title",
        "production_fingerprint": str(item["fingerprint"]),
        "production_output_sha256": str(item["sha256"]),
        "repair_version": REPAIR_VERSION,
        "repair_fingerprint": str(identity["repair_fingerprint"]),
        "repair_font_path": str(identity["font_path"]),
        "repair_font_sha256": str(identity["font_sha256"]),
        "replaces_asset_id": old_asset_id,
        "review_evidence_id": REVIEW_EVIDENCE_ID,
    }
    item.update(
        {
            "base_source": base_source,
            "source": enriched_source,
            "repair_version": REPAIR_VERSION,
            "repair_fingerprint": str(identity["repair_fingerprint"]),
            "font_path": Path(identity["font_path"]),
            "font_sha256": str(identity["font_sha256"]),
            "replaces_asset_id": old_asset_id,
            "review_evidence_id": REVIEW_EVIDENCE_ID,
        }
    )
    return item


def _expected_registry_fields(item: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "kind": "video",
        "path": Path(item["path"]).expanduser().resolve(),
        "summary": str(item["summary"]),
        "lineage": (),
        "sha256": str(item["sha256"]),
        "source": dict(item["source"]),
        "license": dict(item["license"]),
    }


def _assert_repair_record(record: Any, expected: Mapping[str, Any]) -> None:
    actual = {
        "kind": str(_record_value(record, "kind", "")),
        "path": Path(str(_record_value(record, "path", ""))).expanduser().resolve(),
        "summary": str(_record_value(record, "summary", "")),
        "lineage": tuple(_record_value(record, "lineage", ()) or ()),
        "sha256": str(_record_value(record, "sha256", "")),
        "source": dict(_record_value(record, "source", {}) or {}),
        "license": dict(_record_value(record, "license", {}) or {}),
    }
    failed = [name for name in expected if actual[name] != expected[name]]
    if failed:
        raise EchoTitleRepairError(
            f"registered title repair conflicts for {_record_value(record, 'asset_id', '')}: "
            + ", ".join(failed)
        )
    _assert_record_bytes(record, asset_id=str(_record_value(record, "asset_id", "")))


def _repair_candidates(registry: Any, expected: Mapping[str, Any]) -> list[Any]:
    candidates: list[Any] = []
    wanted_source = expected["source"]
    for record in registry.list_records():
        path = Path(str(_record_value(record, "path", ""))).expanduser().resolve()
        source = dict(_record_value(record, "source", {}) or {})
        same_repair = (
            str(source.get("production_run_id") or "") == RUN_ID
            and str(source.get("production_unit_id") or "") == TARGET_SHOT_ID
            and str(source.get("repair_version") or "") == REPAIR_VERSION
            and str(source.get("repair_fingerprint") or "")
            == str(wanted_source["repair_fingerprint"])
        )
        if path == expected["path"] or same_repair:
            candidates.append(record)
    return candidates


def reconcile_repair_asset(registry: Any, item: Mapping[str, Any]) -> Any:
    """Reuse one exact repair record or register one new persistent asset."""

    expected = _expected_registry_fields(item)
    candidates = _repair_candidates(registry, expected)
    if len(candidates) > 1:
        raise EchoTitleRepairError(
            "multiple registry records claim the same title repair"
        )
    if candidates:
        _assert_repair_record(candidates[0], expected)
        return candidates[0]
    asset_id = registry.allocate_id("video")
    try:
        record = registry.register_output(
            asset_id,
            kind="video",
            path=expected["path"],
            summary=expected["summary"],
            lineage=expected["lineage"],
            source=expected["source"],
            license=expected["license"],
        )
    except Exception as exc:
        raise EchoTitleRepairError(
            "failed to register the corrective title asset"
        ) from exc
    _assert_repair_record(record, expected)
    return record


def _strip_registry_enrichment(source: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in source.items()
        if not key.startswith("production_")
        and not key.startswith("repair_")
        and key not in {"replaces_asset_id", "review_evidence_id"}
    }


def _recover_repair_item(
    runner: Any,
    state: Mapping[str, Any],
    *,
    probe_fn: Callable[[str | Path], dict[str, Any]],
) -> dict[str, Any]:
    clip = _clip_by_id(state, TARGET_CLIP_ID)
    asset_id = str(clip.get("asset_id") or "")
    try:
        record = _registry(runner).get(asset_id)
    except KeyError as exc:
        raise EchoTitleRepairError(
            f"corrective title asset vanished: {asset_id}"
        ) from exc
    path, output_hash = _assert_record_bytes(record, asset_id=asset_id)
    source = dict(_record_value(record, "source", {}) or {})
    if (
        str(source.get("repair_version") or "") != REPAIR_VERSION
        or str(source.get("review_evidence_id") or "") != REVIEW_EVIDENCE_ID
        or str(source.get("production_unit_id") or "") != TARGET_SHOT_ID
    ):
        raise EchoTitleRepairError("corrective title registry provenance is incomplete")
    repair_fingerprint = str(source.get("repair_fingerprint") or "")
    local_fingerprint = str(source.get("production_fingerprint") or "")
    old_asset_id = str(source.get("replaces_asset_id") or "")
    font_path = Path(str(source.get("repair_font_path") or "")).expanduser().resolve()
    font_hash = str(source.get("repair_font_sha256") or "")
    if (
        not repair_fingerprint
        or not local_fingerprint
        or not old_asset_id
        or not font_path.is_file()
        or sha256_file(font_path) != font_hash
    ):
        raise EchoTitleRepairError("corrective title identity/font facts changed")
    expected_path = (
        Path(runner.output_dir).expanduser().resolve()
        / "production-media"
        / RUN_ID
        / "repairs"
        / f"{TARGET_CLIP_ID}-title-cjk-{repair_fingerprint[:16]}.mp4"
    )
    if path != expected_path:
        raise EchoTitleRepairError(
            "corrective title path does not match its repair identity"
        )
    sidecar = visual_pass._load_sidecar(path)
    base_source = _strip_registry_enrichment(source)
    expected_sidecar_registration = visual_pass._sidecar_registration(
        path=path,
        summary=str(_record_value(record, "summary", "")),
        lineage=[],
        source=base_source,
        license_data=dict(_record_value(record, "license", {}) or {}),
    )
    if (
        str(sidecar.get("fingerprint") or "") != local_fingerprint
        or str(sidecar.get("output_sha256") or "") != output_hash
        or sidecar.get("registration") != expected_sidecar_registration
    ):
        raise EchoTitleRepairError(
            "corrective title sidecar no longer binds bytes/source facts"
        )
    probe = probe_fn(path)
    try:
        visual_pass._assert_video_probe(
            probe, duration_sec=TARGET_DURATION_SEC, unit_id=TARGET_SHOT_ID
        )
    except Exception as exc:
        raise EchoTitleRepairError(
            f"corrective title media spec changed: {exc}"
        ) from exc
    item = {
        "asset_id": asset_id,
        "unit_index": TARGET_UNIT_INDEX,
        "unit_id": TARGET_SHOT_ID,
        "clip_id": TARGET_CLIP_ID,
        "source_asset_id": None,
        "style": "title",
        "path": path,
        "sha256": output_hash,
        "fingerprint": local_fingerprint,
        "probe": probe,
        "probe_summary": visual_pass._probe_summary(probe),
        "lineage": [],
        "base_source": base_source,
        "source": source,
        "license": dict(_record_value(record, "license", {}) or {}),
        "summary": str(_record_value(record, "summary", "")),
        "repair_version": REPAIR_VERSION,
        "repair_fingerprint": repair_fingerprint,
        "font_path": font_path,
        "font_sha256": font_hash,
        "replaces_asset_id": old_asset_id,
        "review_evidence_id": REVIEW_EVIDENCE_ID,
    }
    _assert_repair_record(record, _expected_registry_fields(item))
    return item


def _project_asset(item: Mapping[str, Any]) -> dict[str, Any]:
    path = Path(item["path"])
    return {
        "id": str(item["asset_id"]),
        "asset_id": str(item["asset_id"]),
        "name": path.name,
        "media_kind": "video",
        "mime_type": mimetypes.guess_type(path.name)[0] or "video/mp4",
        "source_path": str(path),
        "duration": TARGET_DURATION_SEC,
        "fingerprint": str(item["fingerprint"]),
        "metadata": {
            "duration": TARGET_DURATION_SEC,
            "width": local_media.VIDEO_WIDTH,
            "height": local_media.VIDEO_HEIGHT,
            "fps": local_media.VIDEO_FPS,
            "sha256": str(item["sha256"]),
            "lineage": [],
            "source": dict(item["source"]),
            "license": dict(item["license"]),
            "production_run_id": RUN_ID,
            "production_unit_id": TARGET_SHOT_ID,
            "production_style": "title",
            "production_fingerprint": str(item["fingerprint"]),
            "repair_version": REPAIR_VERSION,
            "repair_fingerprint": str(item["repair_fingerprint"]),
            "replaces_asset_id": str(item["replaces_asset_id"]),
            "review_evidence_id": REVIEW_EVIDENCE_ID,
        },
    }


def build_title_repair_ops(
    current_state: Mapping[str, Any], item: Mapping[str, Any]
) -> tuple[list[dict[str, Any]], str]:
    target = _clip_by_id(current_state, TARGET_CLIP_ID)
    trace_id = f"trace-echo-title-repair-{str(item['repair_fingerprint'])[:20]}"
    provenance = {
        "source": "echo_protocol_title_repair",
        "run_id": RUN_ID,
        "trace_id": trace_id,
        "unit_id": TARGET_SHOT_ID,
        "board_digest": str((target.get("provenance") or {}).get("board_digest") or ""),
        "style": "title",
        "fingerprint": str(item["fingerprint"]),
        "repair_version": REPAIR_VERSION,
        "repair_fingerprint": str(item["repair_fingerprint"]),
        "replaces_asset_id": str(item["replaces_asset_id"]),
        "review_evidence_id": REVIEW_EVIDENCE_ID,
        "title_text": TARGET_TEXT,
    }
    clip = deepcopy(target)
    clip["asset_id"] = str(item["asset_id"])
    clip["name"] = Path(item["path"]).name
    clip.pop("provenance", None)
    ops = [
        {
            "op": "update_shot",
            "shot_id": TARGET_SHOT_ID,
            "fields": {
                "asset_id": str(item["asset_id"]),
                "clip_id": TARGET_CLIP_ID,
                "status": "placed",
                "source": "unset",
                "notes": (
                    f"{REPAIR_VERSION}|unit={TARGET_SHOT_ID}|style=title"
                    f"|fingerprint={item['fingerprint']}"
                    f"|repair_fingerprint={item['repair_fingerprint']}"
                    f"|replaces_asset_id={item['replaces_asset_id']}"
                ),
            },
        },
        {"op": "upsert_asset", "asset": _project_asset(item)},
        {
            "op": "delete_clip",
            "clip_id": TARGET_CLIP_ID,
            "ripple": False,
            "provenance": {"run_id": RUN_ID, "trace_id": trace_id},
        },
        {
            "op": "insert_clip",
            "track_id": "V1",
            "at": {"time": TARGET_START_SEC},
            "ripple": False,
            "data": {"clip": clip},
            "provenance": provenance,
        },
    ]
    return ops, trace_id


def dry_run_title_repair(
    current_state: Mapping[str, Any],
    ops: list[dict[str, Any]],
    runner: Any,
    board: tuple[board_builder.BoardUnit, ...],
    item: Mapping[str, Any],
) -> dict[str, Any]:
    before_audio = _audio_snapshot(current_state)
    before_non_target = _non_target_clip_snapshot(current_state)
    before_shots = _non_target_shot_snapshot(current_state)
    before_assets = _project_assets(current_state)
    old_target = _clip_by_id(current_state, TARGET_CLIP_ID)
    old_shot = _shot_by_id(current_state, TARGET_SHOT_ID)

    dry_state = apply_timeline_patches(
        dict(current_state), [{"version": 1, "ops": deepcopy(ops)}]
    )
    mode, old_asset_id = _validate_locked_state(runner, dry_state, board)
    if mode != "post" or old_asset_id != str(item["replaces_asset_id"]):
        raise EchoTitleRepairError(
            "dry run did not produce the exact repaired title state"
        )
    if _audio_snapshot(dry_state) != before_audio:
        raise EchoTitleRepairError("title repair changed the sound mix")
    if _non_target_clip_snapshot(dry_state) != before_non_target:
        raise EchoTitleRepairError(
            "title repair changed a non-target clip or transition"
        )
    if _non_target_shot_snapshot(dry_state) != before_shots:
        raise EchoTitleRepairError("title repair changed a non-target shot")
    after_assets = _project_assets(dry_state)
    changed_assets = [
        asset_id
        for asset_id, asset in before_assets.items()
        if after_assets.get(asset_id) != asset
    ]
    if changed_assets:
        raise EchoTitleRepairError(
            "title repair changed existing project assets: " + ", ".join(changed_assets)
        )
    if set(after_assets) != set(before_assets) | {str(item["asset_id"])}:
        raise EchoTitleRepairError(
            "title repair added anything except its one new asset"
        )

    new_target = _clip_by_id(dry_state, TARGET_CLIP_ID)
    ignored_clip_fields = {"asset_id", "name", "provenance"}
    if {
        key: value
        for key, value in old_target.items()
        if key not in ignored_clip_fields
    } != {
        key: value
        for key, value in new_target.items()
        if key not in ignored_clip_fields
    }:
        raise EchoTitleRepairError(
            "title repair changed target timing/effects beyond asset provenance"
        )
    new_shot = _shot_by_id(dry_state, TARGET_SHOT_ID)
    ignored_shot_fields = {"asset_id", "notes"}
    if {
        key: value for key, value in old_shot.items() if key not in ignored_shot_fields
    } != {
        key: value for key, value in new_shot.items() if key not in ignored_shot_fields
    }:
        raise EchoTitleRepairError("title repair changed target shot semantics")
    return dry_state


def _validate_post_project_asset(
    state: Mapping[str, Any], item: Mapping[str, Any]
) -> None:
    assets = _project_assets(state)
    expected = _project_asset(item)
    actual = assets.get(str(item["asset_id"]))
    required_fields = {
        key: expected[key]
        for key in (
            "id",
            "asset_id",
            "name",
            "media_kind",
            "mime_type",
            "source_path",
            "duration",
            "fingerprint",
            "metadata",
        )
    }
    if not actual or any(
        actual.get(key) != value for key, value in required_fields.items()
    ):
        raise EchoTitleRepairError(
            "corrective title project asset facts are incomplete"
        )
    if str(item["replaces_asset_id"]) not in assets:
        raise EchoTitleRepairError("old title asset was not preserved in the project")


def _patch_entry(
    project: Any,
    *,
    patch_seq: int,
    item: Mapping[str, Any],
    trace_id: str,
) -> dict[str, Any]:
    store = project.store
    path = Path(store.patches_dir(project.project_id)) / f"{patch_seq:04d}.json"
    try:
        entry = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EchoTitleRepairError(
            f"repair patch receipt is missing or invalid: {path}"
        ) from exc
    patch = entry.get("patch") if isinstance(entry.get("patch"), Mapping) else {}
    ops = patch.get("ops") if isinstance(patch.get("ops"), list) else []
    if (
        int(entry.get("seq") or 0) != patch_seq
        or str(entry.get("script_hash") or "") != f"{trace_id}:{REPAIR_VERSION}"
        or int(patch.get("version") or 0) != 1
        or [str(op.get("op") or "") for op in ops if isinstance(op, Mapping)]
        != ["update_shot", "upsert_asset", "delete_clip", "insert_clip"]
    ):
        raise EchoTitleRepairError(
            "repair patch receipt is not the exact four-op mutation"
        )
    update, upsert, delete, insert = ops
    provenance = dict(insert.get("provenance") or {})
    inserted_clip = dict((insert.get("data") or {}).get("clip") or {})
    if (
        str(update.get("shot_id") or "") != TARGET_SHOT_ID
        or str((update.get("fields") or {}).get("asset_id") or "")
        != str(item["asset_id"])
        or dict(upsert.get("asset") or {}) != _project_asset(item)
        or str(delete.get("clip_id") or "") != TARGET_CLIP_ID
        or delete.get("ripple") is not False
        or str(insert.get("track_id") or "") != "V1"
        or abs(float((insert.get("at") or {}).get("time") or 0.0) - TARGET_START_SEC)
        > EPSILON
        or insert.get("ripple") is not False
        or str(inserted_clip.get("id") or "") != TARGET_CLIP_ID
        or str(inserted_clip.get("asset_id") or "") != str(item["asset_id"])
        or str(provenance.get("repair_fingerprint") or "")
        != str(item["repair_fingerprint"])
        or str(provenance.get("replaces_asset_id") or "")
        != str(item["replaces_asset_id"])
        or str(provenance.get("review_evidence_id") or "") != REVIEW_EVIDENCE_ID
    ):
        raise EchoTitleRepairError(
            "repair patch receipt targets more than the reviewed title"
        )
    return {
        "path": str(path),
        "sha256": sha256_file(path),
        "digest": _stable_digest(patch),
        "ops": [str(op.get("op") or "") for op in ops],
    }


def _evidence_payload(
    runner: Any,
    state: Mapping[str, Any],
    item: Mapping[str, Any],
    *,
    patch_seq: int,
    patch_receipt: Mapping[str, Any],
    budget: Mapping[str, Any],
) -> dict[str, Any]:
    old_record = _registry(runner).get(str(item["replaces_asset_id"]))
    old_path, old_hash = _assert_record_bytes(
        old_record, asset_id=str(item["replaces_asset_id"])
    )
    return {
        "repair_version": REPAIR_VERSION,
        "repair_fingerprint": str(item["repair_fingerprint"]),
        "review_evidence_id": REVIEW_EVIDENCE_ID,
        "target": {
            "shot_id": TARGET_SHOT_ID,
            "clip_id": TARGET_CLIP_ID,
            "title_text": TARGET_TEXT,
            "start_sec": TARGET_START_SEC,
            "end_sec": TARGET_START_SEC + TARGET_DURATION_SEC,
            "old_asset_id": str(item["replaces_asset_id"]),
            "old_path": str(old_path),
            "old_sha256": old_hash,
            "new_asset_id": str(item["asset_id"]),
            "new_path": str(item["path"]),
            "new_sha256": str(item["sha256"]),
            "local_render_fingerprint": str(item["fingerprint"]),
            "font_path": str(item["font_path"]),
            "font_sha256": str(item["font_sha256"]),
            "probe": dict(item["probe_summary"]),
        },
        "project_revision_before": int(runner.project_revision) - 1,
        "project_revision_after": int(runner.project_revision),
        "patch_seq_before": patch_seq - 1,
        "patch_seq_after": patch_seq,
        "patch_receipt": dict(patch_receipt),
        "non_target_clip_digest": _stable_digest(_non_target_clip_snapshot(state)),
        "non_target_shot_digest": _stable_digest(_non_target_shot_snapshot(state)),
        "audio_digest": _stable_digest(_audio_snapshot(state)),
        "checks": {
            "single_atomic_patch": True,
            "target_interval_only": True,
            "non_target_clips_unchanged": True,
            "non_target_shots_unchanged": True,
            "audio_clips_unchanged": True,
            "old_asset_preserved": True,
            "new_asset_registered": True,
            "media_spec_1080p_30_h264_yuv420p_no_audio": True,
            "local_media_cost_usd": 0.0,
            "ai_video_generation_calls": 0,
            "budget_unchanged": True,
            "human_text_integrity_review_required": True,
        },
        "budget_before": dict(budget),
        "budget_after": dict(budget),
    }


def execute_title_repair(
    manager: Any,
    runner: Any,
    source_assets: Mapping[str, str],
    *,
    render_fn: Callable[..., dict[str, Any]] = render_image_motion,
    probe_fn: Callable[[str | Path], dict[str, Any]] = probe_media,
    font_resolver: Callable[[], Path] = local_media._resolve_pingfang_font,
) -> dict[str, Any]:
    """Run or reconcile the exact title correction for the current revision."""

    board = board_builder.build_board(source_assets)
    run = manager.get_run(runner.project_id, runner.run_id)
    state_name = str(run.get("production_state") or run.get("state") or "")
    if REVIEW_EVIDENCE_ID not in {
        str(value) for value in run.get("evidence_ids") or []
    }:
        raise EchoTitleRepairError(
            "the title repair is not bound to its failed visual review"
        )
    budget_before = _budget(run)
    _assert_no_veo(budget_before)
    budget_digest = _stable_digest(budget_before)

    project = _project_handle(runner)
    current_state = project.load()
    mode, old_asset_id = _validate_locked_state(runner, current_state, board)

    if state_name == "rendering":
        if mode != "post":
            raise EchoTitleRepairError(
                "rendering state does not contain the repaired title"
            )
        item = _recover_repair_item(runner, current_state, probe_fn=probe_fn)
        _validate_post_project_asset(current_state, item)
        trace_id = f"trace-echo-title-repair-{str(item['repair_fingerprint'])[:20]}"
        patch_seq = int(
            project.store.load_meta(project.project_id).get("patch_seq") or 0
        )
        receipt = _patch_entry(
            project, patch_seq=patch_seq, item=item, trace_id=trace_id
        )
        evidence_id = f"ev-echo-title-repair-{str(item['repair_fingerprint'])[:16]}"
        latest = manager.get_run(runner.project_id, runner.run_id)
        if evidence_id not in {
            str(value) for value in latest.get("evidence_ids") or []
        }:
            raise EchoTitleRepairError(
                "rendering state lacks the revision-bound repair evidence"
            )
        budget_after = _budget(latest)
        if _stable_digest(budget_after) != budget_digest:
            raise EchoTitleRepairError("replayed title repair changed the media budget")
        return {
            "ok": True,
            "replayed": True,
            "patch_applied": False,
            "production_state": "rendering",
            "project_revision": int(runner.project_revision),
            "patch_seq": patch_seq,
            "evidence_id": evidence_id,
            "old_asset_id": old_asset_id,
            "new_asset_id": str(item["asset_id"]),
            "repair_fingerprint": str(item["repair_fingerprint"]),
            "patch_receipt": receipt,
            "budget": budget_after,
            "veo_calls": 0,
        }
    if state_name != "revising":
        raise EchoTitleRepairError(
            f"title repair requires revising state, got {state_name!r}"
        )

    patch_applied = False
    if mode == "pre":
        project_revision_before = int(runner.project_revision)
        patch_seq_before = int(
            project.store.load_meta(project.project_id).get("patch_seq") or 0
        )
        identity = _repair_identity(
            Path(runner.output_dir), font_resolver=font_resolver
        )
        try:
            rendered = render_fn(
                Path(runner.output_dir) / "programmatic-title-input-not-used",
                Path(identity["path"]),
                TARGET_DURATION_SEC,
                "title",
                TARGET_UNIT_INDEX,
                source_asset_id=None,
            )
        except Exception as exc:
            raise EchoTitleRepairError(
                f"corrective title render failed: {exc}"
            ) from exc
        item = _verify_rendered_title(
            board,
            identity,
            rendered,
            probe_fn=probe_fn,
            old_asset_id=old_asset_id,
        )
        record = reconcile_repair_asset(_registry(runner), item)
        item["asset_id"] = str(_record_value(record, "asset_id", ""))
        if not item["asset_id"]:
            raise EchoTitleRepairError("registry returned an empty corrective asset id")
        if (
            int(runner.project_revision) != project_revision_before
            or int(project.store.load_meta(project.project_id).get("patch_seq") or 0)
            != patch_seq_before
        ):
            raise EchoTitleRepairError(
                "asset registration unexpectedly changed project state"
            )
        ops, trace_id = build_title_repair_ops(current_state, item)
        dry_run_title_repair(current_state, ops, runner, board, item)
        precommit_budget = _budget(manager.get_run(runner.project_id, runner.run_id))
        _assert_no_veo(precommit_budget)
        if _stable_digest(precommit_budget) != budget_digest:
            raise EchoTitleRepairError("media budget changed before the title patch")
        runner.run_project_edit(
            lambda: project.apply_ops(ops, label=f"{trace_id}:{REPAIR_VERSION}"),
            expected_project_revision=project_revision_before,
            timeout=300,
        )
        patch_applied = True
        if int(runner.project_revision) != project_revision_before + 1:
            raise EchoTitleRepairError(
                "title repair must advance project revision exactly once"
            )
        patch_seq = int(
            project.store.load_meta(project.project_id).get("patch_seq") or 0
        )
        if patch_seq != patch_seq_before + 1:
            raise EchoTitleRepairError(
                "title repair must append exactly one project patch"
            )
        final_state = project.load()
    else:
        item = _recover_repair_item(runner, current_state, probe_fn=probe_fn)
        if str(item["replaces_asset_id"]) != old_asset_id:
            raise EchoTitleRepairError(
                "recovered repair changed the replaced asset identity"
            )
        trace_id = f"trace-echo-title-repair-{str(item['repair_fingerprint'])[:20]}"
        patch_seq = int(
            project.store.load_meta(project.project_id).get("patch_seq") or 0
        )
        final_state = current_state

    final_mode, final_old_asset_id = _validate_locked_state(runner, final_state, board)
    if final_mode != "post" or final_old_asset_id != str(item["replaces_asset_id"]):
        raise EchoTitleRepairError(
            "persisted title repair failed final state validation"
        )
    _validate_post_project_asset(final_state, item)
    patch_receipt = _patch_entry(
        project, patch_seq=patch_seq, item=item, trace_id=trace_id
    )

    run_after = manager.get_run(runner.project_id, runner.run_id)
    budget_after = _budget(run_after)
    _assert_no_veo(budget_after)
    if _stable_digest(budget_after) != budget_digest:
        raise EchoTitleRepairError("zero-cost title repair changed the media budget")

    evidence_id = f"ev-echo-title-repair-{str(item['repair_fingerprint'])[:16]}"
    evidence_trace = f"trace-{evidence_id}"
    payload = _evidence_payload(
        runner,
        final_state,
        item,
        patch_seq=patch_seq,
        patch_receipt=patch_receipt,
        budget=budget_after,
    )
    manager.record_evidence(
        runner.project_id,
        runner.run_id,
        evidence_id=evidence_id,
        kind="localized_title_repair",
        project_revision=int(runner.project_revision),
        trace_id=evidence_trace,
        payload=payload,
    )
    latest = manager.get_run(runner.project_id, runner.run_id)
    transition = manager.transition_run(
        runner.project_id,
        runner.run_id,
        "rendering",
        expected_revision=int(
            latest.get("production_revision") or latest.get("revision") or 0
        ),
        trace_id=f"trace-echo-title-repair-{str(item['repair_fingerprint'])[:16]}-complete",
    )
    return {
        "ok": True,
        "replayed": False,
        "patch_applied": patch_applied,
        "production_state": str(
            transition.get("production_state") or transition.get("state") or "rendering"
        ),
        "project_revision": int(runner.project_revision),
        "patch_seq": patch_seq,
        "evidence_id": evidence_id,
        "review_evidence_id": REVIEW_EVIDENCE_ID,
        "old_asset_id": str(item["replaces_asset_id"]),
        "new_asset_id": str(item["asset_id"]),
        "repair_version": REPAIR_VERSION,
        "repair_fingerprint": str(item["repair_fingerprint"]),
        "output_path": str(item["path"]),
        "output_sha256": str(item["sha256"]),
        "patch_receipt": patch_receipt,
        "budget": budget_after,
        "veo_calls": 0,
    }


@contextmanager
def _operator_lock(output_dir: Path) -> Iterator[None]:
    path = (
        output_dir.expanduser().resolve()
        / "production-media"
        / RUN_ID
        / ".title-repair.lock"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise EchoTitleRepairError(
                "another title-repair operator is already active"
            ) from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def repair_title(output_root: Path) -> dict[str, Any]:
    manager = SessionManager(
        output_root=output_root,
        max_sessions=2,
        idle_timeout_sec=0,
        sweep_interval_sec=0,
    )
    runner = manager.resume_session(SESSION_ID)
    try:
        if str(runner.run_id) != RUN_ID:
            raise EchoTitleRepairError(f"unexpected production run: {runner.run_id}")
        source_assets, _source_review = board_builder._load_reviewed_sources(runner)
        with _operator_lock(Path(runner.output_dir)):
            return execute_title_repair(manager, runner, source_assets)
    finally:
        manager.close_all()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("repair-title",))
    parser.add_argument(
        "--output-root",
        type=Path,
        default=Path.home() / ".gemia" / "v3",
    )
    args = parser.parse_args()
    result = repair_title(args.output_root.expanduser().resolve())
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
