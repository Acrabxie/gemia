#!/usr/bin/env python3
"""Bake and atomically install the Echo Protocol V1 visual pass.

This production operator is deliberately local-only.  It converts the 22
image/MG placeholders in the locked 42-unit board into deterministic H.264
files, reconciles those files with the durable asset registry, and replaces
the placeholders in one canonical project patch.  The 20 reviewed stock clips
and every audio clip are immutable inputs to this stage.

The operator never searches for media, calls a provider, or spends media
budget.  A crash may leave verified local outputs registered but cannot leave
a partially replaced timeline: the next invocation reconciles exact
path/hash/fingerprint/unit facts and resumes from there.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import sys
from collections.abc import Callable, Mapping
from contextlib import suppress
from copy import deepcopy
from pathlib import Path
from typing import Any

# Direct script execution must import the isolated checkout containing this
# file, never another editable Lumeri installation on the machine.
REPO_ROOT = Path(__file__).resolve().parents[1]
with suppress(ValueError):
    sys.path.remove(str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT))

from gemia.echo_local_media import (  # noqa: E402
    VIDEO_FPS,
    VIDEO_HEIGHT,
    VIDEO_WIDTH,
    probe_media,
    render_image_motion,
    sha256_file,
)
from gemia.project_model import iter_shots  # noqa: E402
from gemia.session_manager import SessionManager  # noqa: E402
from lumerai.export_support import clip_dropped_fields  # noqa: E402
from lumerai.patches import apply_timeline_patches  # noqa: E402
from scripts import build_echo_protocol_v1 as board_builder  # noqa: E402

SESSION_ID = board_builder.SESSION_ID
RUN_ID = board_builder.RUN_ID
VISUAL_PASS_VERSION = "echo-protocol-v1-visual-pass-1"
EPSILON = board_builder.EPSILON

_HUD_UNITS = frozenset({1, 9})
_HERO_UNITS = frozenset({10, 11, 28, 30, 35, 37})
_MEMORY_FOLD_UNITS = frozenset({27})
_WHITE_COLLAPSE_UNITS = frozenset({39, 40})
_IRIS_UNITS = frozenset({41})
_TITLE_UNITS = frozenset({42})


class EchoVisualPassError(RuntimeError):
    """The visual pass cannot prove a safe, exact production mutation."""


def style_for_unit(unit_index: int) -> str:
    """Return the approved deterministic local-motion style for one unit."""

    if unit_index in _HUD_UNITS:
        return "hud"
    if unit_index in _HERO_UNITS:
        return "hero"
    if unit_index in _MEMORY_FOLD_UNITS:
        return "memory_fold"
    if unit_index in _WHITE_COLLAPSE_UNITS:
        return "white_collapse"
    if unit_index in _IRIS_UNITS:
        return "iris"
    if unit_index in _TITLE_UNITS:
        return "title"
    return "ken_burns"


def _stable_digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _project_handle(runner: Any) -> Any:
    project = getattr(getattr(runner, "agent", None), "project", None)
    if project is None:
        tool_ctx = getattr(getattr(runner, "agent", None), "_tool_ctx", None)
        project = getattr(tool_ctx, "project", None)
    if project is None or not callable(getattr(project, "load", None)):
        raise EchoVisualPassError("runner does not expose its canonical project handle")
    return project


def _budget(run: Mapping[str, Any]) -> dict[str, Any]:
    value = run.get("budget")
    if not isinstance(value, Mapping):
        raise EchoVisualPassError("production run has no canonical budget view")
    return dict(value)


def _assert_no_veo(budget: Mapping[str, Any]) -> None:
    calls = int(budget.get("veo_reserved_calls") or 0)
    duration = float(budget.get("veo_reserved_duration_sec") or 0.0)
    if calls != 0 or abs(duration) > EPSILON:
        raise EchoVisualPassError(
            f"Echo V1 visual pass forbids Veo: calls={calls}, duration={duration}"
        )


def _record_value(record: Any, name: str, default: Any = None) -> Any:
    if isinstance(record, Mapping):
        return record.get(name, default)
    return getattr(record, name, default)


def _audio_snapshot(state: Mapping[str, Any]) -> list[dict[str, Any]]:
    clips = [
        deepcopy(clip)
        for clip in (state.get("timeline") or {}).get("clips") or []
        if isinstance(clip, Mapping) and str(clip.get("media_kind") or "") == "audio"
    ]
    return sorted(clips, key=lambda clip: str(clip.get("id") or ""))


def _assert_sound_mix_present(runner: Any, state: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Require the 14 migrated clips plus at least one locally-authored SFX."""

    board_builder._assert_expected_audio(state)
    snapshot = _audio_snapshot(state)
    original_assets = {
        board_builder.EXPECTED_MUSIC_ASSET,
        *board_builder.EXPECTED_NARRATION_ASSETS,
    }
    extras = [clip for clip in snapshot if str(clip.get("asset_id") or "") not in original_assets]
    if not extras:
        raise EchoVisualPassError("visual pass requires the completed local SFX sound pass")
    for clip in extras:
        if str(clip.get("track_id") or "") != "A3":
            raise EchoVisualPassError(f"non-baseline audio must remain on A3: {clip.get('id')}")
        asset_id = str(clip.get("asset_id") or "")
        try:
            record = runner.agent.registry.get(asset_id)
        except KeyError as exc:
            raise EchoVisualPassError(f"SFX registry asset is missing: {asset_id}") from exc
        source = dict(_record_value(record, "source", {}) or {})
        if (
            str(_record_value(record, "kind", "")) != "audio"
            or str(source.get("role") or "") != "sfx"
        ):
            raise EchoVisualPassError(f"A3 asset is not registered as local SFX: {asset_id}")
    return snapshot


def _source_image_fact(runner: Any, source_asset_id: str) -> dict[str, Any]:
    try:
        record = runner.agent.registry.get(source_asset_id)
    except KeyError as exc:
        raise EchoVisualPassError(
            f"source image is absent from registry: {source_asset_id}"
        ) from exc
    if str(_record_value(record, "kind", "")) != "image":
        raise EchoVisualPassError(f"source lineage target is not an image: {source_asset_id}")
    path = Path(str(_record_value(record, "path", ""))).expanduser().resolve()
    if not path.is_file() or path.stat().st_size <= 0:
        raise EchoVisualPassError(f"source image file is missing or empty: {source_asset_id}")
    actual_hash = sha256_file(path)
    registered_hash = str(_record_value(record, "sha256", "") or "")
    if not registered_hash or registered_hash != actual_hash:
        raise EchoVisualPassError(f"source image hash changed: {source_asset_id}")
    source = dict(_record_value(record, "source", {}) or {})
    license_data = dict(_record_value(record, "license", {}) or {})
    if not (source.get("provider") or source.get("receipt_id")):
        raise EchoVisualPassError(f"source image origin is incomplete: {source_asset_id}")
    if not (license_data.get("basis") or license_data.get("name") or license_data.get("url")):
        raise EchoVisualPassError(f"source image license is incomplete: {source_asset_id}")
    return {
        "asset_id": source_asset_id,
        "path": path,
        "sha256": actual_hash,
        "source": source,
        "license": license_data,
    }


def _visual_output_path(
    output_dir: Path,
    unit: board_builder.BoardUnit,
    *,
    style: str,
    source_sha256: str,
) -> Path:
    token = _stable_digest(
        {
            "version": VISUAL_PASS_VERSION,
            "unit_id": unit.shot_id,
            "duration_sec": unit.duration_sec,
            "style": style,
            "source_asset_id": None if style == "title" else unit.asset_id,
            "source_sha256": "" if style == "title" else source_sha256,
        }
    )[:16]
    root = output_dir.expanduser().resolve() / "production-media" / RUN_ID / "visual-pass"
    return root / f"{unit.clip_id}-{style}-{token}.mp4"


def _fraction(value: Any) -> float:
    try:
        numerator, denominator = str(value).split("/", 1)
        return float(numerator) / float(denominator)
    except (TypeError, ValueError, ZeroDivisionError):
        return 0.0


def _probe_duration(probe: Mapping[str, Any]) -> float:
    raw = (probe.get("format") or {}).get("duration")
    if raw is None:
        raw = next(
            (
                stream.get("duration")
                for stream in probe.get("streams") or []
                if isinstance(stream, Mapping) and stream.get("duration") is not None
            ),
            0.0,
        )
    try:
        return float(raw or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _probe_summary(probe: Mapping[str, Any]) -> dict[str, Any]:
    streams = probe.get("streams") if isinstance(probe.get("streams"), list) else []
    return {
        "duration_sec": _probe_duration(probe),
        "streams": [
            {
                key: stream.get(key)
                for key in (
                    "codec_type",
                    "codec_name",
                    "width",
                    "height",
                    "pix_fmt",
                    "avg_frame_rate",
                    "r_frame_rate",
                )
                if stream.get(key) is not None
            }
            for stream in streams
            if isinstance(stream, Mapping)
        ],
    }


def _assert_video_probe(probe: Mapping[str, Any], *, duration_sec: float, unit_id: str) -> None:
    streams = probe.get("streams") if isinstance(probe.get("streams"), list) else []
    videos = [stream for stream in streams if stream.get("codec_type") == "video"]
    audios = [stream for stream in streams if stream.get("codec_type") == "audio"]
    if len(videos) != 1 or audios:
        raise EchoVisualPassError(f"{unit_id} bake must have one video stream and no audio")
    video = videos[0]
    fps = _fraction(video.get("avg_frame_rate") or video.get("r_frame_rate"))
    checks = {
        "codec": str(video.get("codec_name") or "") == "h264",
        "width": int(video.get("width") or 0) == VIDEO_WIDTH,
        "height": int(video.get("height") or 0) == VIDEO_HEIGHT,
        "pixel_format": str(video.get("pix_fmt") or "") == "yuv420p",
        "fps": abs(fps - VIDEO_FPS) < 0.001,
        "duration": abs(_probe_duration(probe) - duration_sec) <= (1.0 / VIDEO_FPS) + 0.001,
    }
    failed = [name for name, passed in checks.items() if not passed]
    if failed:
        raise EchoVisualPassError(f"{unit_id} local bake failed media checks: {', '.join(failed)}")


def _load_sidecar(path: Path) -> dict[str, Any]:
    sidecar_path = path.with_suffix(path.suffix + ".lumeri.json")
    try:
        value = json.loads(sidecar_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EchoVisualPassError(f"invalid local-media sidecar: {sidecar_path}") from exc
    if not isinstance(value, dict):
        raise EchoVisualPassError(f"invalid local-media sidecar object: {sidecar_path}")
    return value


def _sidecar_registration(
    *,
    path: Path,
    summary: str,
    lineage: list[str],
    source: Mapping[str, Any],
    license_data: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "kind": "video",
        "path": str(path),
        "summary": summary,
        "lineage": list(lineage),
        "source": dict(source),
        "license": dict(license_data),
    }


def _verified_render_result(
    unit: board_builder.BoardUnit,
    *,
    style: str,
    requested_path: Path,
    rendered: Mapping[str, Any],
    probe_fn: Callable[[str | Path], dict[str, Any]],
) -> dict[str, Any]:
    path = Path(str(rendered.get("path") or "")).expanduser().resolve()
    if path != requested_path.expanduser().resolve():
        raise EchoVisualPassError(f"{unit.shot_id} renderer returned a different output path")
    if not path.is_file() or path.stat().st_size <= 0:
        raise EchoVisualPassError(f"{unit.shot_id} local bake is missing or empty")
    actual_hash = sha256_file(path)
    if str(rendered.get("sha256") or "") != actual_hash:
        raise EchoVisualPassError(f"{unit.shot_id} renderer hash does not match output bytes")
    fingerprint = str(rendered.get("fingerprint") or "")
    if not fingerprint:
        raise EchoVisualPassError(f"{unit.shot_id} local bake has no fingerprint")
    sidecar = _load_sidecar(path)
    if (
        str(sidecar.get("fingerprint") or "") != fingerprint
        or str(sidecar.get("output_sha256") or "") != actual_hash
    ):
        raise EchoVisualPassError(f"{unit.shot_id} sidecar does not bind fingerprint and bytes")
    if rendered.get("registration_ready") is not True:
        raise EchoVisualPassError(f"{unit.shot_id} renderer did not produce registrable lineage")

    lineage = [str(item) for item in (rendered.get("lineage") or [])]
    source = dict(rendered.get("source") or {})
    license_data = dict(rendered.get("license") or {})
    expected_lineage = [] if style == "title" else [unit.asset_id]
    expected_kind = "owned_video" if style == "title" else "local_mg"
    if lineage != expected_lineage or str(source.get("kind") or "") != expected_kind:
        raise EchoVisualPassError(f"{unit.shot_id} local bake lineage/source kind is wrong")
    if not (license_data.get("basis") or license_data.get("name") or license_data.get("url")):
        raise EchoVisualPassError(f"{unit.shot_id} local bake has no license basis")

    registration = rendered.get("registration")
    if not isinstance(registration, Mapping):
        raise EchoVisualPassError(f"{unit.shot_id} renderer has no registration receipt")
    summary = str(registration.get("summary") or "") or (
        f"Echo Protocol local {style} motion unit {unit.index}"
    )
    expected_sidecar_registration = _sidecar_registration(
        path=path,
        summary=summary,
        lineage=lineage,
        source=source,
        license_data=license_data,
    )
    if (
        dict(registration) != expected_sidecar_registration
        or sidecar.get("registration") != expected_sidecar_registration
    ):
        raise EchoVisualPassError(f"{unit.shot_id} sidecar registration facts changed")

    probe = probe_fn(path)
    _assert_video_probe(probe, duration_sec=unit.duration_sec, unit_id=unit.shot_id)
    return {
        "unit_index": unit.index,
        "unit_id": unit.shot_id,
        "clip_id": unit.clip_id,
        "source_asset_id": None if style == "title" else unit.asset_id,
        "style": style,
        "path": path,
        "sha256": actual_hash,
        "fingerprint": fingerprint,
        "probe": dict(probe),
        "probe_summary": _probe_summary(probe),
        "lineage": lineage,
        "source": source,
        "license": license_data,
        "summary": summary,
    }


def _expected_registry_fields(baked: Mapping[str, Any]) -> dict[str, Any]:
    source = dict(baked["source"])
    source.update(
        {
            "production_run_id": RUN_ID,
            "production_unit_id": str(baked["unit_id"]),
            "production_unit_index": int(baked["unit_index"]),
            "production_style": str(baked["style"]),
            "production_fingerprint": str(baked["fingerprint"]),
            "production_output_sha256": str(baked["sha256"]),
        }
    )
    return {
        "kind": "video",
        "path": Path(baked["path"]).expanduser().resolve(),
        "summary": str(baked["summary"]),
        "lineage": tuple(str(item) for item in baked["lineage"]),
        "sha256": str(baked["sha256"]),
        "source": source,
        "license": dict(baked["license"]),
    }


def _assert_registry_record(record: Any, expected: Mapping[str, Any]) -> None:
    actual_path = Path(str(_record_value(record, "path", ""))).expanduser().resolve()
    comparisons = {
        "kind": str(_record_value(record, "kind", "")) == expected["kind"],
        "path": actual_path == expected["path"],
        "summary": str(_record_value(record, "summary", "")) == expected["summary"],
        "lineage": tuple(_record_value(record, "lineage", ()) or ()) == expected["lineage"],
        "sha256": str(_record_value(record, "sha256", "")) == expected["sha256"],
        "source": dict(_record_value(record, "source", {}) or {}) == expected["source"],
        "license": dict(_record_value(record, "license", {}) or {}) == expected["license"],
    }
    failed = [name for name, passed in comparisons.items() if not passed]
    if failed:
        asset_id = str(_record_value(record, "asset_id", ""))
        raise EchoVisualPassError(
            f"registered local output conflicts for {asset_id}: {', '.join(failed)}"
        )
    path = expected["path"]
    if not path.is_file() or sha256_file(path) != expected["sha256"]:
        raise EchoVisualPassError(
            f"registered local output bytes changed: {_record_value(record, 'asset_id', '')}"
        )


def _registry_candidates(registry: Any, expected: Mapping[str, Any]) -> list[Any]:
    candidates: list[Any] = []
    for record in registry.list_records():
        path = Path(str(_record_value(record, "path", ""))).expanduser().resolve()
        source = dict(_record_value(record, "source", {}) or {})
        same_unit = str(source.get("production_run_id") or "") == RUN_ID and str(
            source.get("production_unit_id") or ""
        ) == str(expected["source"]["production_unit_id"])
        if path == expected["path"] or same_unit:
            candidates.append(record)
    return candidates


def reconcile_registry_output(registry: Any, baked: Mapping[str, Any]) -> Any:
    """Reuse one exact durable output or allocate/register a new real id.

    Asset ids are never inferred from counters or filenames.  A crash after id
    allocation can leave a harmless counter gap; a crash after registration is
    recovered by exact path/hash/fingerprint/unit matching.
    """

    expected = _expected_registry_fields(baked)
    candidates = _registry_candidates(registry, expected)
    if len(candidates) > 1:
        raise EchoVisualPassError(
            f"multiple registry records claim {baked['unit_id']}; refusing ambiguous reuse"
        )
    if candidates:
        _assert_registry_record(candidates[0], expected)
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
        raise EchoVisualPassError(
            f"failed to register local output for {baked['unit_id']} using allocated id"
        ) from exc
    _assert_registry_record(record, expected)
    return record


def _stock_snapshot(
    state: Mapping[str, Any], board: tuple[board_builder.BoardUnit, ...]
) -> list[dict[str, Any]]:
    stock_ids = {unit.clip_id for unit in board if unit.is_public_motion}
    clips = [
        deepcopy(clip)
        for clip in (state.get("timeline") or {}).get("clips") or []
        if isinstance(clip, Mapping) and str(clip.get("id") or "") in stock_ids
    ]
    return sorted(clips, key=lambda clip: str(clip.get("id") or ""))


def classify_visual_timeline(
    state: Mapping[str, Any], board: tuple[board_builder.BoardUnit, ...]
) -> str:
    """Return ``pre`` or ``final``; every mixed/partial state fails closed."""

    board_builder.validate_board(board)
    clips = list((state.get("timeline") or {}).get("clips") or [])
    v1 = [clip for clip in clips if str(clip.get("track_id") or "") == "V1"]
    expected_ids = {unit.clip_id for unit in board}
    actual_ids = {str(clip.get("id") or "") for clip in v1}
    if len(v1) != 42 or actual_ids != expected_ids:
        raise EchoVisualPassError("visual pass requires exactly the locked 42 stable V1 clips")
    by_id = {str(clip.get("id") or ""): clip for clip in v1}
    local_modes: list[str] = []
    for unit in board:
        clip = by_id[unit.clip_id]
        if (
            abs(float(clip.get("start") or 0.0) - unit.start_sec) > EPSILON
            or abs(float(clip.get("duration") or 0.0) - unit.duration_sec) > EPSILON
        ):
            raise EchoVisualPassError(f"visual timing changed for {unit.shot_id}")
        if unit.is_public_motion:
            if (
                str(clip.get("asset_id") or "") != unit.asset_id
                or str(clip.get("media_kind") or "") != "video"
                or abs(float(clip.get("source_in") or 0.0) - unit.source_in) > EPSILON
                or abs(float(clip.get("source_out") or 0.0) - unit.source_out) > EPSILON
                or (clip.get("effects") or {}).get("muted") is not True
            ):
                raise EchoVisualPassError(f"reviewed stock clip changed: {unit.shot_id}")
            continue
        if (
            str(clip.get("media_kind") or "") == "image"
            and str(clip.get("asset_id") or "") == unit.asset_id
        ):
            local_modes.append("pre")
        elif (
            str(clip.get("media_kind") or "") == "video"
            and str(clip.get("asset_id") or "") != unit.asset_id
        ):
            local_modes.append("final")
        else:
            local_modes.append("partial")
    unique = set(local_modes)
    if unique == {"pre"}:
        return "pre"
    if unique == {"final"}:
        return "final"
    raise EchoVisualPassError(
        "partial visual-pass timeline detected; refusing to render or rebuild blindly"
    )


def _bake_local_units(
    runner: Any,
    board: tuple[board_builder.BoardUnit, ...],
    *,
    render_fn: Callable[..., dict[str, Any]],
    probe_fn: Callable[[str | Path], dict[str, Any]],
) -> dict[int, dict[str, Any]]:
    output_dir = Path(runner.output_dir).expanduser().resolve()
    baked: dict[int, dict[str, Any]] = {}
    for unit in board:
        if unit.is_public_motion:
            continue
        style = style_for_unit(unit.index)
        source_fact = None if style == "title" else _source_image_fact(runner, unit.asset_id)
        source_hash = "" if source_fact is None else str(source_fact["sha256"])
        output_path = _visual_output_path(output_dir, unit, style=style, source_sha256=source_hash)
        input_path = (
            output_dir / "programmatic-title-input-not-used"
            if source_fact is None
            else Path(source_fact["path"])
        )
        try:
            result = render_fn(
                input_path,
                output_path,
                unit.duration_sec,
                style,
                unit.index,
                source_asset_id=None if style == "title" else unit.asset_id,
            )
        except Exception as exc:
            raise EchoVisualPassError(f"local render failed for {unit.shot_id}: {exc}") from exc
        verified = _verified_render_result(
            unit,
            style=style,
            requested_path=output_path,
            rendered=result,
            probe_fn=probe_fn,
        )
        record = reconcile_registry_output(runner.agent.registry, verified)
        verified["asset_id"] = str(_record_value(record, "asset_id", ""))
        if not verified["asset_id"]:
            raise EchoVisualPassError(f"registry returned an empty id for {unit.shot_id}")
        baked[unit.index] = verified
    if len(baked) != 22 or len({item["asset_id"] for item in baked.values()}) != 22:
        raise EchoVisualPassError("visual pass requires 22 distinct registered local outputs")
    return baked


def _recover_baked_units(
    runner: Any,
    state: Mapping[str, Any],
    board: tuple[board_builder.BoardUnit, ...],
    *,
    probe_fn: Callable[[str | Path], dict[str, Any]],
) -> dict[int, dict[str, Any]]:
    by_id = {
        str(clip.get("id") or ""): clip
        for clip in (state.get("timeline") or {}).get("clips") or []
        if isinstance(clip, Mapping)
    }
    baked: dict[int, dict[str, Any]] = {}
    for unit in board:
        if unit.is_public_motion:
            continue
        clip = by_id[unit.clip_id]
        asset_id = str(clip.get("asset_id") or "")
        try:
            record = runner.agent.registry.get(asset_id)
        except KeyError as exc:
            raise EchoVisualPassError(
                f"persisted visual output is absent from registry: {asset_id}"
            ) from exc
        path = Path(str(_record_value(record, "path", ""))).expanduser().resolve()
        source = dict(_record_value(record, "source", {}) or {})
        style = style_for_unit(unit.index)
        baked_item = {
            "asset_id": asset_id,
            "unit_index": unit.index,
            "unit_id": unit.shot_id,
            "clip_id": unit.clip_id,
            "source_asset_id": None if style == "title" else unit.asset_id,
            "style": style,
            "path": path,
            "sha256": str(_record_value(record, "sha256", "")),
            "fingerprint": str(source.get("production_fingerprint") or ""),
            "lineage": list(_record_value(record, "lineage", ()) or ()),
            "source": {
                key: value for key, value in source.items() if not key.startswith("production_")
            },
            "license": dict(_record_value(record, "license", {}) or {}),
            "summary": str(_record_value(record, "summary", "")),
        }
        expected = _expected_registry_fields(baked_item)
        _assert_registry_record(record, expected)
        sidecar = _load_sidecar(path)
        if (
            str(sidecar.get("fingerprint") or "") != baked_item["fingerprint"]
            or str(sidecar.get("output_sha256") or "") != baked_item["sha256"]
        ):
            raise EchoVisualPassError(f"persisted sidecar changed for {unit.shot_id}")
        expected_sidecar_registration = _sidecar_registration(
            path=path,
            summary=str(baked_item["summary"]),
            lineage=list(baked_item["lineage"]),
            source=dict(baked_item["source"]),
            license_data=dict(baked_item["license"]),
        )
        if sidecar.get("registration") != expected_sidecar_registration:
            raise EchoVisualPassError(f"persisted sidecar registration changed for {unit.shot_id}")
        probe = probe_fn(path)
        _assert_video_probe(probe, duration_sec=unit.duration_sec, unit_id=unit.shot_id)
        baked_item["probe"] = dict(probe)
        baked_item["probe_summary"] = _probe_summary(probe)
        baked[unit.index] = baked_item
    if len(baked) != 22 or len({item["asset_id"] for item in baked.values()}) != 22:
        raise EchoVisualPassError("persisted local outputs do not have 22 distinct asset ids")
    return baked


def _visual_shotlist(
    current_state: Mapping[str, Any],
    board: tuple[board_builder.BoardUnit, ...],
    baked: Mapping[int, Mapping[str, Any]],
) -> dict[str, Any]:
    shotlist = deepcopy(current_state.get("shotlist") or {})
    shots = [shot for _scene, shot in iter_shots(shotlist)]
    if len(shots) != 42 or [str(shot.get("id") or "") for shot in shots] != [
        unit.shot_id for unit in board
    ]:
        raise EchoVisualPassError("visual pass shotlist is not the locked 42-unit board")
    for unit, shot in zip(board, shots, strict=True):
        if unit.is_public_motion:
            continue
        item = baked[unit.index]
        shot["asset_id"] = str(item["asset_id"])
        shot["clip_id"] = unit.clip_id
        shot["status"] = "placed"
        shot["source"] = "unset"
        shot["notes"] = (
            f"{VISUAL_PASS_VERSION}|unit={unit.shot_id}|style={item['style']}"
            f"|fingerprint={item['fingerprint']}|source_asset_id="
            f"{item['source_asset_id'] or 'programmatic'}"
        )
    shotlist["style"] = (
        "licensed real footage plus deterministic local 2.5D/MG; "
        "all still-image motion baked into canonical video; no AI video"
    )
    return shotlist


def _project_asset(item: Mapping[str, Any], *, duration_sec: float) -> dict[str, Any]:
    path = Path(item["path"])
    return {
        "id": str(item["asset_id"]),
        "asset_id": str(item["asset_id"]),
        "name": path.name,
        "media_kind": "video",
        "mime_type": mimetypes.guess_type(path.name)[0] or "video/mp4",
        "source_path": str(path),
        "duration": duration_sec,
        "fingerprint": str(item["fingerprint"]),
        "metadata": {
            "duration": duration_sec,
            "width": VIDEO_WIDTH,
            "height": VIDEO_HEIGHT,
            "fps": VIDEO_FPS,
            "sha256": str(item["sha256"]),
            "lineage": list(item["lineage"]),
            "source": dict(item["source"]),
            "license": dict(item["license"]),
            "production_run_id": RUN_ID,
            "production_unit_id": str(item["unit_id"]),
            "production_style": str(item["style"]),
            "production_fingerprint": str(item["fingerprint"]),
        },
    }


def build_visual_pass_ops(
    current_state: Mapping[str, Any],
    board: tuple[board_builder.BoardUnit, ...],
    baked: Mapping[int, Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], str, str]:
    digest = _stable_digest(
        [
            {
                "unit_id": item["unit_id"],
                "asset_id": item["asset_id"],
                "style": item["style"],
                "sha256": item["sha256"],
                "fingerprint": item["fingerprint"],
            }
            for _index, item in sorted(baked.items())
        ]
    )
    trace_id = f"trace-echo-visual-pass-{digest[:20]}"
    ops: list[dict[str, Any]] = [
        {"op": "set_shotlist", "shotlist": _visual_shotlist(current_state, board, baked)}
    ]
    for unit in board:
        if unit.is_public_motion:
            continue
        ops.append(
            {
                "op": "upsert_asset",
                "asset": _project_asset(baked[unit.index], duration_sec=unit.duration_sec),
            }
        )
    for unit in board:
        if unit.is_public_motion:
            continue
        ops.append(
            {
                "op": "delete_clip",
                "clip_id": unit.clip_id,
                "ripple": False,
                "provenance": {"run_id": RUN_ID, "trace_id": trace_id},
            }
        )
    board_digest = board_builder.board_digest(board)
    for unit in board:
        if unit.is_public_motion:
            continue
        item = baked[unit.index]
        provenance = {
            "source": "echo_protocol_v1_visual_pass",
            "run_id": RUN_ID,
            "trace_id": trace_id,
            "unit_id": unit.shot_id,
            "board_digest": board_digest,
            "visual_pass_digest": digest,
            "style": str(item["style"]),
            "fingerprint": str(item["fingerprint"]),
            "source_asset_id": item["source_asset_id"],
        }
        clip = {
            "id": unit.clip_id,
            "asset_id": str(item["asset_id"]),
            "track_id": "V1",
            "name": Path(item["path"]).name,
            "media_kind": "video",
            "duration": unit.duration_sec,
            "source_in": 0.0,
            "source_out": unit.duration_sec,
            "enabled": True,
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
    return ops, trace_id, digest


def _visual_digest(baked: Mapping[int, Mapping[str, Any]]) -> str:
    return _stable_digest(
        [
            {
                "unit_id": item["unit_id"],
                "asset_id": item["asset_id"],
                "style": item["style"],
                "sha256": item["sha256"],
                "fingerprint": item["fingerprint"],
            }
            for _index, item in sorted(baked.items())
        ]
    )


def _persisted_visual_receipt(
    state: Mapping[str, Any], board: tuple[board_builder.BoardUnit, ...]
) -> tuple[str, str]:
    by_id = {
        str(clip.get("id") or ""): clip
        for clip in (state.get("timeline") or {}).get("clips") or []
        if isinstance(clip, Mapping)
    }
    first_local = next(unit for unit in board if not unit.is_public_motion)
    provenance = dict((by_id.get(first_local.clip_id) or {}).get("provenance") or {})
    trace_id = str(provenance.get("trace_id") or "")
    digest = str(provenance.get("visual_pass_digest") or "")
    if (
        str(provenance.get("source") or "") != "echo_protocol_v1_visual_pass"
        or not trace_id.startswith("trace-echo-visual-pass-")
        or not digest
    ):
        raise EchoVisualPassError("persisted visual-pass receipt is incomplete")
    return trace_id, digest


def validate_final_visual_state(
    runner: Any,
    state: Mapping[str, Any],
    board: tuple[board_builder.BoardUnit, ...],
    baked: Mapping[int, Mapping[str, Any]],
    *,
    expected_audio: list[dict[str, Any]],
    expected_stock: list[dict[str, Any]],
) -> dict[str, Any]:
    if classify_visual_timeline(state, board) != "final":
        raise EchoVisualPassError("visual-pass result did not replace all 22 local units")
    if _audio_snapshot(state) != expected_audio:
        raise EchoVisualPassError("visual pass changed the original mix or local SFX")
    if _stock_snapshot(state, board) != expected_stock:
        raise EchoVisualPassError("visual pass changed reviewed stock clips")

    clips = list((state.get("timeline") or {}).get("clips") or [])
    by_id = {str(clip.get("id") or ""): clip for clip in clips}
    shots = [shot for _scene, shot in iter_shots(state.get("shotlist") or {})]
    if len(shots) != 42:
        raise EchoVisualPassError("visual-pass result lost the 42-shot board")
    public_motion = 0.0
    expected_visual_digest = _visual_digest(baked)
    receipt_trace, receipt_digest = _persisted_visual_receipt(state, board)
    if receipt_digest != expected_visual_digest:
        raise EchoVisualPassError("persisted visual-pass digest does not match output facts")
    for unit, shot in zip(board, shots, strict=True):
        clip = by_id[unit.clip_id]
        if unit.is_public_motion:
            public_motion += unit.duration_sec
            if str(shot.get("asset_id") or "") != unit.asset_id:
                raise EchoVisualPassError(f"stock shot asset changed: {unit.shot_id}")
            continue
        item = baked[unit.index]
        if (
            str(clip.get("asset_id") or "") != str(item["asset_id"])
            or str(shot.get("asset_id") or "") != str(item["asset_id"])
            or str(shot.get("clip_id") or "") != unit.clip_id
            or str(shot.get("status") or "") != "placed"
        ):
            raise EchoVisualPassError(f"local visual identity changed: {unit.shot_id}")
        provenance = clip.get("provenance") or {}
        if (
            str(provenance.get("source") or "") != "echo_protocol_v1_visual_pass"
            or str(provenance.get("trace_id") or "") != receipt_trace
            or str(provenance.get("run_id") or "") != RUN_ID
            or str(provenance.get("unit_id") or "") != unit.shot_id
            or str(provenance.get("board_digest") or "") != board_builder.board_digest(board)
            or str(provenance.get("visual_pass_digest") or "") != receipt_digest
            or str(provenance.get("style") or "") != str(item["style"])
            or str(provenance.get("fingerprint") or "") != str(item["fingerprint"])
            or provenance.get("source_asset_id") != item["source_asset_id"]
        ):
            raise EchoVisualPassError(f"local visual provenance changed: {unit.shot_id}")
        try:
            record = runner.agent.registry.get(str(item["asset_id"]))
        except KeyError as exc:
            raise EchoVisualPassError(
                f"local output registry record vanished: {unit.shot_id}"
            ) from exc
        _assert_registry_record(record, _expected_registry_fields(item))

    v1 = [clip for clip in clips if str(clip.get("track_id") or "") == "V1"]
    if len(v1) != 42 or any(str(clip.get("media_kind") or "") != "video" for clip in v1):
        raise EchoVisualPassError("final V1 must contain exactly 42 video clips")
    if abs(public_motion - 60.0) > EPSILON:
        raise EchoVisualPassError("reviewed stock motion is not exactly 60 seconds")
    if abs(float((state.get("timeline") or {}).get("duration") or 0.0) - 120.0) > EPSILON:
        raise EchoVisualPassError("visual-pass timeline is not exactly 120 seconds")
    dropped = [
        {"clip_id": str(clip.get("id") or ""), **item}
        for clip in clips
        for item in clip_dropped_fields(clip)
    ]
    if dropped:
        raise EchoVisualPassError(f"visual-pass dry run has dropped fields: {dropped}")

    project_assets = {
        str(asset.get("id") or ""): asset
        for asset in state.get("assets") or []
        if isinstance(asset, Mapping)
    }
    for unit in board:
        if unit.is_public_motion:
            continue
        item = baked[unit.index]
        asset = project_assets.get(str(item["asset_id"]))
        metadata = dict((asset or {}).get("metadata") or {})
        if (
            not asset
            or str(asset.get("media_kind") or "") != "video"
            or Path(str(asset.get("source_path") or "")).expanduser().resolve()
            != Path(item["path"]).expanduser().resolve()
            or str(metadata.get("sha256") or "") != str(item["sha256"])
            or list(metadata.get("lineage") or []) != list(item["lineage"])
            or str(metadata.get("production_style") or "") != str(item["style"])
        ):
            raise EchoVisualPassError(f"project asset facts are incomplete: {unit.shot_id}")
    return {
        "shot_count": 42,
        "v1_clip_count": 42,
        "video_clip_count": 42,
        "local_baked_clip_count": 22,
        "public_stock_clip_count": 20,
        "public_motion_sec": 60.0,
        "duration_sec": 120.0,
        "audio_clip_count": len(expected_audio),
        "dropped_fields": [],
    }


def dry_run_visual_patch(
    current_state: Mapping[str, Any],
    ops: list[dict[str, Any]],
    runner: Any,
    board: tuple[board_builder.BoardUnit, ...],
    baked: Mapping[int, Mapping[str, Any]],
    *,
    expected_audio: list[dict[str, Any]],
    expected_stock: list[dict[str, Any]],
) -> dict[str, Any]:
    dry_state = apply_timeline_patches(dict(current_state), [{"version": 1, "ops": deepcopy(ops)}])
    validate_final_visual_state(
        runner,
        dry_state,
        board,
        baked,
        expected_audio=expected_audio,
        expected_stock=expected_stock,
    )
    return dry_state


def execute_visual_pass(
    manager: Any,
    runner: Any,
    source_assets: Mapping[str, str],
    *,
    render_fn: Callable[..., dict[str, Any]] = render_image_motion,
    probe_fn: Callable[[str | Path], dict[str, Any]] = probe_media,
) -> dict[str, Any]:
    """Run or reconcile the formal visual-pass stage."""

    board = board_builder.build_board(source_assets)
    run = manager.get_run(runner.project_id, runner.run_id)
    state_name = str(run.get("production_state") or run.get("state") or "")
    budget_before = _budget(run)
    _assert_no_veo(budget_before)
    budget_digest = _stable_digest(budget_before)
    project = _project_handle(runner)
    current_state = project.load()
    mode = classify_visual_timeline(current_state, board)
    expected_audio = _assert_sound_mix_present(runner, current_state)
    expected_stock = _stock_snapshot(current_state, board)

    if state_name == "rendering":
        if mode != "final":
            raise EchoVisualPassError("rendering state does not contain the completed visual pass")
        baked = _recover_baked_units(runner, current_state, board, probe_fn=probe_fn)
        checks = validate_final_visual_state(
            runner,
            current_state,
            board,
            baked,
            expected_audio=expected_audio,
            expected_stock=expected_stock,
        )
        return {
            "ok": True,
            "replayed": True,
            "production_state": "rendering",
            "project_revision": int(runner.project_revision),
            "visual_pass_version": VISUAL_PASS_VERSION,
            **checks,
            "budget": budget_before,
            "veo_calls": 0,
        }
    if state_name != "visual_pass":
        raise EchoVisualPassError(
            f"visual-pass operator requires visual_pass state, got {state_name}"
        )

    patch_applied = False
    if mode == "pre":
        # This additionally proves the rough-cut board provenance before any
        # local output is generated or registered.
        board_builder.validate_persisted_board(
            runner, board, expected_digest=board_builder.board_digest(board)
        )
        project_revision_before = int(runner.project_revision)
        meta_before = project.store.load_meta(project.project_id)
        patch_seq_before = int(meta_before.get("patch_seq") or 0)
        baked = _bake_local_units(
            runner,
            board,
            render_fn=render_fn,
            probe_fn=probe_fn,
        )
        # Registry writes above are durable independent facts and must not
        # masquerade as a project edit.  The timeline commit below is the only
        # revision/patch increment in this stage.
        if int(runner.project_revision) != project_revision_before:
            raise EchoVisualPassError(
                "local output registration unexpectedly changed project revision"
            )
        if (
            int(project.store.load_meta(project.project_id).get("patch_seq") or 0)
            != patch_seq_before
        ):
            raise EchoVisualPassError(
                "local output registration unexpectedly changed project patch sequence"
            )
        ops, patch_trace, visual_digest = build_visual_pass_ops(current_state, board, baked)
        dry_run_visual_patch(
            current_state,
            ops,
            runner,
            board,
            baked,
            expected_audio=expected_audio,
            expected_stock=expected_stock,
        )
        # Rendering and registration are local, zero-dollar facts.  Re-read
        # the canonical ledger immediately before the project commit so a
        # concurrent/accidental reservation cannot be hidden by a later
        # post-patch failure.
        precommit_budget = _budget(manager.get_run(runner.project_id, runner.run_id))
        _assert_no_veo(precommit_budget)
        if _stable_digest(precommit_budget) != budget_digest:
            raise EchoVisualPassError(
                "media budget changed during local baking; refusing timeline patch"
            )
        runner.run_project_edit(
            lambda: project.apply_ops(ops, label=f"{patch_trace}:{VISUAL_PASS_VERSION}"),
            expected_project_revision=project_revision_before,
            timeout=600,
        )
        patch_applied = True
        patch_seq_after = int(project.store.load_meta(project.project_id).get("patch_seq") or 0)
        project_revision_after = int(runner.project_revision)
        if patch_seq_after != patch_seq_before + 1:
            raise EchoVisualPassError(
                f"visual pass must commit one patch, seq {patch_seq_before} -> {patch_seq_after}"
            )
        if project_revision_after != project_revision_before + 1:
            raise EchoVisualPassError(
                "visual pass must advance project revision exactly once: "
                f"{project_revision_before} -> {project_revision_after}"
            )
        final_state = project.load()
    else:
        # Crash recovery after the atomic patch but before evidence/transition.
        baked = _recover_baked_units(runner, current_state, board, probe_fn=probe_fn)
        patch_seq_after = int(project.store.load_meta(project.project_id).get("patch_seq") or 0)
        patch_trace, persisted_visual_digest = _persisted_visual_receipt(current_state, board)
        visual_digest = _visual_digest(baked)
        if persisted_visual_digest != visual_digest:
            raise EchoVisualPassError(
                "persisted visual-pass digest does not match registered output facts"
            )
        final_state = current_state

    checks = validate_final_visual_state(
        runner,
        final_state,
        board,
        baked,
        expected_audio=expected_audio,
        expected_stock=expected_stock,
    )
    run_after = manager.get_run(runner.project_id, runner.run_id)
    budget_after = _budget(run_after)
    _assert_no_veo(budget_after)
    if _stable_digest(budget_after) != budget_digest:
        raise EchoVisualPassError("zero-cost visual pass changed the media budget")

    evidence_id = f"ev-echo-visual-pass-v1-{visual_digest[:16]}"
    evidence_trace = f"trace-{evidence_id}"
    manager.record_evidence(
        runner.project_id,
        runner.run_id,
        evidence_id=evidence_id,
        kind="visual_pass_local_bake",
        project_revision=int(runner.project_revision),
        trace_id=evidence_trace,
        payload={
            "visual_pass_version": VISUAL_PASS_VERSION,
            "board_digest": board_builder.board_digest(board),
            "visual_pass_digest": visual_digest,
            "patch_trace_id": patch_trace,
            "patch_seq": patch_seq_after,
            "outputs": [
                {
                    "unit_id": item["unit_id"],
                    "clip_id": item["clip_id"],
                    "asset_id": item["asset_id"],
                    "source_asset_id": item["source_asset_id"],
                    "style": item["style"],
                    "path": str(item["path"]),
                    "sha256": item["sha256"],
                    "fingerprint": item["fingerprint"],
                    "lineage": list(item["lineage"]),
                    "source_kind": item["source"].get("kind"),
                    "license": dict(item["license"]),
                    "probe": dict(item["probe_summary"]),
                }
                for _index, item in sorted(baked.items())
            ],
            "checks": {
                **checks,
                "single_atomic_patch": True,
                "stock_clips_unchanged": True,
                "audio_clips_unchanged": True,
                "local_media_cost_usd": 0.0,
                "ai_video_generation_calls": 0,
                "budget_unchanged": True,
            },
            "budget_before": budget_before,
            "budget_after": budget_after,
        },
    )
    latest = manager.get_run(runner.project_id, runner.run_id)
    transition = manager.transition_run(
        runner.project_id,
        runner.run_id,
        "rendering",
        expected_revision=int(latest.get("production_revision") or latest.get("revision") or 0),
        trace_id="trace-echo-visual-pass-v1-complete",
    )
    return {
        "ok": True,
        "replayed": False,
        "production_state": str(
            transition.get("production_state") or transition.get("state") or "rendering"
        ),
        "project_revision": int(runner.project_revision),
        "visual_pass_version": VISUAL_PASS_VERSION,
        "evidence_id": evidence_id,
        "patch_applied": patch_applied,
        "patch_seq": patch_seq_after,
        "patch_trace_id": patch_trace,
        **checks,
        "budget": budget_after,
        "veo_calls": 0,
    }


def visual_pass(output_root: Path) -> dict[str, Any]:
    manager = SessionManager(
        output_root=output_root,
        max_sessions=2,
        idle_timeout_sec=0,
        sweep_interval_sec=0,
    )
    runner = manager.resume_session(SESSION_ID)
    try:
        if str(runner.run_id) != RUN_ID:
            raise EchoVisualPassError(f"unexpected production run: {runner.run_id}")
        source_assets, _source_review = board_builder._load_reviewed_sources(runner)
        return execute_visual_pass(manager, runner, source_assets)
    finally:
        manager.close_all()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("visual-pass",))
    parser.add_argument(
        "--output-root",
        type=Path,
        default=Path.home() / ".gemia" / "v3",
    )
    args = parser.parse_args()
    result = visual_pass(args.output_root.expanduser().resolve())
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
