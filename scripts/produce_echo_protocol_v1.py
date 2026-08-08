#!/usr/bin/env python3
"""Restart-safe formal production operator for Echo Protocol V1.

Every external-media operation flows through SessionRunner.run_production_verb:
the host claims an immutable tool receipt, binds the run-scoped budget ledger,
then dispatches with a trace and idempotency key.  The script never calls a
provider primitive directly and never prints connector credentials.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

REPO_ROOT = Path(__file__).resolve().parents[1]
try:
    sys.path.remove(str(REPO_ROOT))
except ValueError:
    pass
sys.path.insert(0, str(REPO_ROOT))

from gemia.production_media_checks import inspect_video_motion
from gemia.session_manager import SessionManager
from gemia.tools._ffmpeg import ffprobe_metadata, video_stream


SESSION_ID = "v3-00a7080c78e7"
RUN_ID = "echo-protocol-production"
SOURCE_REVIEW_SCHEMA = "lumeri.echo-source-visual-review"
SOURCE_REVIEW_VERSION = 1
SOURCE_REVIEW_DIRNAME = "source-visual-review"
SOURCE_MANIFEST_FILENAME = "manifest.json"
MIN_SOURCE_WIDTH = 1920
MIN_SOURCE_HEIGHT = 1080

SOURCE_SPECS: tuple[dict[str, str], ...] = (
    {"slot": "s01", "query": "rainy city night traffic aerial"},
    {"slot": "s02", "query": "elevated train city traffic skyline"},
    {"slot": "s03", "query": "earth orbit satellite space station"},
    {"slot": "s04", "query": "robotics laboratory scientist experiment"},
    {"slot": "s05", "query": "blue server data center corridor"},
    {"slot": "s06", "query": "moon surface space lunar landscape"},
    {"slot": "s07", "query": "abstract data stream digital network"},
    {"slot": "s08", "query": "drone flight fast city aerial"},
    {"slot": "s09", "query": "tunnel speed industrial sparks"},
    {"slot": "s10", "query": "abstract particles blue white light"},
)


class SourceGateError(RuntimeError):
    """A stock asset is real, but is not yet safe to use in production."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _valid_web_url(value: Any) -> bool:
    parsed = urlparse(str(value or "").strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    with tempfile.NamedTemporaryFile(
        mode="wb",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        temporary = Path(handle.name)
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _source_review_dir(runner) -> Path:
    return Path(runner.output_dir) / "production-evidence" / SOURCE_REVIEW_DIRNAME


def _source_manifest_path(runner) -> Path:
    return _source_review_dir(runner) / SOURCE_MANIFEST_FILENAME


def _empty_source_manifest(runner) -> dict[str, Any]:
    return {
        "schema": SOURCE_REVIEW_SCHEMA,
        "version": SOURCE_REVIEW_VERSION,
        "project_id": str(runner.project_id),
        "run_id": str(runner.run_id),
        "project_revision": int(runner.project_revision),
        "required_slots": [item["slot"] for item in SOURCE_SPECS],
        "status": "pending_review",
        "slots": {},
        "created_at": _utc_now(),
        "updated_at": _utc_now(),
    }


def _load_source_manifest(runner, *, required: bool = False) -> dict[str, Any]:
    path = _source_manifest_path(runner)
    if not path.is_file():
        if required:
            raise SourceGateError(
                "source visual-review manifest is missing; run source first"
            )
        return _empty_source_manifest(runner)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise SourceGateError(f"source visual-review manifest is unreadable: {exc}") from exc
    if not isinstance(value, dict):
        raise SourceGateError("source visual-review manifest must be an object")
    if value.get("schema") != SOURCE_REVIEW_SCHEMA:
        raise SourceGateError("source visual-review manifest schema mismatch")
    if int(value.get("version") or 0) != SOURCE_REVIEW_VERSION:
        raise SourceGateError("source visual-review manifest version mismatch")
    if str(value.get("project_id") or "") != str(runner.project_id):
        raise SourceGateError("source visual-review manifest project mismatch")
    if str(value.get("run_id") or "") != str(runner.run_id):
        raise SourceGateError("source visual-review manifest run mismatch")
    slots = value.get("slots")
    if not isinstance(slots, dict):
        raise SourceGateError("source visual-review manifest slots must be an object")
    return value


def _manifest_status(manifest: dict[str, Any]) -> str:
    slots = manifest.get("slots") if isinstance(manifest.get("slots"), dict) else {}
    decisions: list[str] = []
    for spec in SOURCE_SPECS:
        entry = slots.get(spec["slot"])
        entry = entry if isinstance(entry, dict) else {}
        review = entry.get("review")
        review = review if isinstance(review, dict) else {}
        decisions.append(str(review.get("decision") or ""))
    if decisions and all(decision == "approve" for decision in decisions):
        return "passed"
    if "reject" in decisions:
        return "changes_requested"
    return "pending_review"


def _save_source_manifest(runner, manifest: dict[str, Any]) -> Path:
    manifest["project_revision"] = int(runner.project_revision)
    manifest["required_slots"] = [item["slot"] for item in SOURCE_SPECS]
    manifest["status"] = _manifest_status(manifest)
    manifest["updated_at"] = _utc_now()
    path = _source_manifest_path(runner)
    _write_json_atomic(path, manifest)
    return path


def _stable_digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _identity(phase: str, step: str, args: dict[str, Any]) -> tuple[str, str]:
    digest = _stable_digest({"phase": phase, "step": step, "args": args})[:20]
    return (
        f"trace-echo-{phase}-{step}-{digest}"[:96],
        f"echo-protocol-production:{phase}:{step}:{digest}",
    )


def _call(runner, phase: str, step: str, tool: str, args: dict[str, Any]) -> dict[str, Any]:
    trace_id, idempotency_key = _identity(phase, step, args)
    return runner.run_production_verb(
        tool,
        args,
        trace_id=trace_id,
        idempotency_key=idempotency_key,
        timeout=300,
    )


def _record_evidence(
    manager: SessionManager,
    runner,
    *,
    evidence_id: str,
    kind: str,
    payload: dict[str, Any],
    trace_id: str,
) -> dict[str, Any]:
    return manager.record_evidence(
        runner.project_id,
        runner.run_id,
        kind=kind,
        payload=payload,
        project_revision=runner.project_revision,
        trace_id=trace_id,
        evidence_id=evidence_id,
    )


def _open(output_root: Path) -> tuple[SessionManager, Any]:
    manager = SessionManager(
        output_root=output_root,
        max_sessions=2,
        idle_timeout_sec=0,
        sweep_interval_sec=0,
    )
    runner = manager.resume_session(SESSION_ID)
    if runner.run_id != RUN_ID:
        manager.close_all()
        raise RuntimeError(f"unexpected production run: {runner.run_id}")
    return manager, runner


def preflight(output_root: Path) -> dict[str, Any]:
    manager, runner = _open(output_root)
    try:
        snapshot = runner.snapshot()
        if snapshot["production_state"] != "sourcing":
            raise RuntimeError(
                f"Echo preflight requires sourcing state, got {snapshot['production_state']}"
            )
        if len(snapshot["assets"]) < 28 or snapshot["asset_mix"]["missing"]:
            raise RuntimeError("migrated Echo asset closure is incomplete")
        budget = snapshot["budget"]
        if float(budget.get("spent_usd") or 0) < 1.525:
            raise RuntimeError("legacy Echo spend was not imported")
        if float(budget.get("limit_usd") or 0) != 15.0:
            raise RuntimeError("production media hard cap is not $15")

        probe_args = {"asset_id": "img_001", "verify_motion": False}
        probe = _call(runner, "preflight", "probe-img-001", "probe_media", probe_args)
        if probe.get("kind") != "image" or not probe.get("has_video"):
            raise RuntimeError("baseline image probe failed")
        # Evidence and the formal tool receipt must share one trace.  A second
        # synthetic "receipt" trace would break the audit chain even though
        # both records describe the same probe.
        trace_id, _ = _identity("preflight", "probe-img-001", probe_args)
        _record_evidence(
            manager,
            runner,
            evidence_id="ev-echo-preflight-v1",
            kind="production_preflight",
            trace_id=trace_id,
            payload={
                "project_revision": runner.project_revision,
                "baseline_asset_count": len(snapshot["assets"]),
                "asset_mix": snapshot["asset_mix"],
                "budget": budget,
                "probe": probe,
            },
        )
        return {
            "ok": True,
            "project_revision": runner.project_revision,
            "production_state": snapshot["production_state"],
            "asset_count": len(snapshot["assets"]),
            "spent_usd": budget.get("spent_usd"),
            "remaining_usd": budget.get("remaining_usd"),
            "probe_asset_id": probe.get("asset_id"),
        }
    finally:
        manager.close_all()


def _probe_physical_source(path: Path) -> dict[str, Any]:
    """Probe the bytes now; never trust a prior tool receipt for source reuse."""

    metadata = ffprobe_metadata(path)
    fmt = metadata.get("format") if isinstance(metadata.get("format"), dict) else {}
    stream = video_stream(metadata) or {}
    try:
        duration_sec = float(fmt.get("duration") or 0.0)
    except (TypeError, ValueError):
        duration_sec = 0.0
    motion = inspect_video_motion(path)
    return {
        "duration_sec": duration_sec,
        "width": int(stream.get("width") or 0),
        "height": int(stream.get("height") or 0),
        "video_codec": str(stream.get("codec_name") or ""),
        "has_video": bool(stream),
        "motion_evidence": motion,
    }


def _write_contact_sheet(
    source_path: Path,
    destination: Path,
    *,
    slot: str,
    asset_id: str,
    duration_sec: float,
) -> dict[str, Any]:
    """Persist six labelled frames that a reviewer can actually inspect."""

    try:
        import cv2
        from PIL import Image, ImageDraw, ImageOps
    except ImportError as exc:  # pragma: no cover - production dependency failure
        raise SourceGateError(f"contact-sheet dependencies are unavailable: {exc}") from exc

    fractions = (0.05, 0.23, 0.41, 0.59, 0.77, 0.95)
    sample_times = [round(max(0.0, duration_sec * value), 3) for value in fractions]
    capture = cv2.VideoCapture(str(source_path))
    if not capture.isOpened():
        capture.release()
        raise SourceGateError(f"cannot decode source for contact sheet: {asset_id}")

    tiles: list[Any] = []
    try:
        for timestamp in sample_times:
            capture.set(cv2.CAP_PROP_POS_MSEC, timestamp * 1000.0)
            ok, frame = capture.read()
            if not ok or frame is None:
                raise SourceGateError(
                    f"contact-sheet frame decode failed for {asset_id} at {timestamp:.3f}s"
                )
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            image = Image.fromarray(rgb)
            tile = ImageOps.fit(
                image,
                (480, 270),
                method=Image.Resampling.LANCZOS,
                centering=(0.5, 0.5),
            )
            labelled = Image.new("RGB", (480, 300), color=(8, 12, 20))
            labelled.paste(tile, (0, 0))
            ImageDraw.Draw(labelled).text(
                (12, 278),
                f"{slot} · {asset_id} · {timestamp:.2f}s",
                fill=(232, 238, 248),
            )
            tiles.append(labelled)
    finally:
        capture.release()

    if len(tiles) != 6:
        raise SourceGateError(f"contact sheet for {asset_id} does not contain six frames")
    sheet = Image.new("RGB", (1440, 600), color=(8, 12, 20))
    for index, tile in enumerate(tiles):
        sheet.paste(tile, ((index % 3) * 480, (index // 3) * 300))

    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w+b",
        dir=destination.parent,
        prefix=f".{destination.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        temporary = Path(handle.name)
        sheet.save(handle, format="JPEG", quality=90, optimize=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, destination)
    return {
        "path": str(destination),
        "sha256": _sha256_file(destination),
        "width": 1440,
        "height": 600,
        "frame_count": 6,
        "sample_times_sec": sample_times,
    }


def _audit_source_record(
    runner,
    record,
    *,
    slot: str,
    query: str,
    previous: dict[str, Any] | None = None,
    expected_provider: str | None = None,
    expected_provider_asset_id: str | None = None,
) -> dict[str, Any]:
    """Recompute provenance, bytes, media facts and visual evidence for a slot."""

    if record.kind != "video":
        raise SourceGateError(f"source slot {slot} is not a video asset")
    path = Path(record.path)
    if not path.is_file() or path.stat().st_size <= 0:
        raise SourceGateError(f"source slot {slot} is missing or empty")

    actual_sha256 = _sha256_file(path)
    if not str(record.sha256 or ""):
        raise SourceGateError(f"source slot {slot} has no registered content hash")
    if actual_sha256 != str(record.sha256):
        raise SourceGateError(
            f"source slot {slot} bytes changed: registered hash does not match disk"
        )

    source = dict(record.source or {})
    license_info = dict(record.license or {})
    provider = str(source.get("provider") or "").strip().lower()
    provider_asset_id = str(source.get("provider_asset_id") or "").strip()
    source_url = str(source.get("url") or "").strip()
    license_name = str(license_info.get("name") or "").strip()
    license_url = str(license_info.get("url") or "").strip()
    if source.get("kind") != "public_stock" or provider not in {"pexels", "pixabay"}:
        raise SourceGateError(f"source slot {slot} lacks a supported public-stock provider")
    if not provider_asset_id:
        raise SourceGateError(f"source slot {slot} lacks provider_asset_id")
    if not _valid_web_url(source_url):
        raise SourceGateError(f"source slot {slot} lacks a valid source URL")
    if not license_name or not _valid_web_url(license_url):
        raise SourceGateError(f"source slot {slot} lacks license name+URL")
    if expected_provider and provider != str(expected_provider).strip().lower():
        raise SourceGateError(f"source slot {slot} provider changed during fetch")
    if expected_provider_asset_id and provider_asset_id != str(
        expected_provider_asset_id
    ).strip():
        raise SourceGateError(f"source slot {slot} provider asset id changed during fetch")

    probe = _probe_physical_source(path)
    width = int(probe.get("width") or 0)
    height = int(probe.get("height") or 0)
    duration_sec = float(probe.get("duration_sec") or 0.0)
    motion = probe.get("motion_evidence") or {}
    if not probe.get("has_video"):
        raise SourceGateError(f"source slot {slot} has no decodable video stream")
    if duration_sec < 6.0:
        raise SourceGateError(f"source slot {slot} is shorter than 6 seconds")
    if width < MIN_SOURCE_WIDTH or height < MIN_SOURCE_HEIGHT or width <= height:
        raise SourceGateError(
            f"source slot {slot} must be landscape 1080p or better; got {width}x{height}"
        )
    if not bool(motion.get("real_motion_verified")):
        raise SourceGateError(f"source slot {slot} did not pass the real-motion check")

    sheet_name = f"{slot}-{record.asset_id}-{actual_sha256[:16]}.jpg"
    sheet = _write_contact_sheet(
        path,
        _source_review_dir(runner) / "contact-sheets" / sheet_name,
        slot=slot,
        asset_id=record.asset_id,
        duration_sec=duration_sec,
    )
    fingerprint = _stable_digest(
        {
            "slot": slot,
            "asset_id": record.asset_id,
            "sha256": actual_sha256,
            "provider": provider,
            "provider_asset_id": provider_asset_id,
            "source_url": source_url,
            "license_name": license_name,
            "license_url": license_url,
            "probe": {
                "duration_sec": duration_sec,
                "width": width,
                "height": height,
                "video_codec": probe.get("video_codec"),
            },
            "contact_sheet_sha256": sheet["sha256"],
        }
    )
    review: dict[str, Any] = {}
    if (
        isinstance(previous, dict)
        and str(previous.get("fingerprint") or "") == fingerprint
        and isinstance(previous.get("review"), dict)
        and str(previous["review"].get("decision") or "") in {"approve", "reject"}
    ):
        review = dict(previous["review"])

    runner.agent.registry.update_record(
        record.asset_id,
        source_patch={
            "real_motion_verified": True,
            "motion_evidence": motion,
            "production_validation_sha256": actual_sha256,
            "production_validation_at": _utc_now(),
        },
    )
    return {
        "slot": slot,
        "query": query,
        "asset_id": record.asset_id,
        "path": str(path),
        "sha256": actual_sha256,
        "provider": provider,
        "provider_asset_id": provider_asset_id,
        "source_url": source_url,
        "license": {
            "name": license_name,
            "url": license_url,
            "attribution": str(license_info.get("attribution") or ""),
        },
        "probe": probe,
        "contact_sheet": sheet,
        "fingerprint": fingerprint,
        "machine_gate": "passed",
        "review": review,
        "audited_at": _utc_now(),
    }


def _candidate_ok(item: dict[str, Any], used: set[tuple[str, str]]) -> bool:
    provider = str(item.get("provider") or "").lower()
    result_id = str(item.get("id") or "")
    width = int(item.get("width") or 0)
    height = int(item.get("height") or 0)
    duration = float(item.get("duration") or 0.0)
    return bool(
        provider in {"pexels", "pixabay"}
        and result_id
        and (provider, result_id) not in used
        and width >= MIN_SOURCE_WIDTH
        and height >= MIN_SOURCE_HEIGHT
        and width > height
        and duration >= 6.0
        and item.get("download_url")
        and item.get("license")
    )


def _existing_slot(runner, slot: str):
    for record in runner.agent.registry.list_records():
        if (
            record.kind == "video"
            and str((record.source or {}).get("production_slot") or "") == slot
            and str((record.source or {}).get("production_source_status") or "")
            not in {"rejected", "machine_rejected"}
        ):
            return record
    return None


def source(output_root: Path) -> dict[str, Any]:
    manager, runner = _open(output_root)
    accepted: list[dict[str, Any]] = []
    try:
        run = manager.get_run(runner.project_id, runner.run_id)
        if run["production_state"] != "sourcing":
            raise RuntimeError(
                f"stock sourcing requires sourcing state, got {run['production_state']}"
            )
        manifest = _load_source_manifest(runner)
        manifest_slots = manifest["slots"]
        used: set[tuple[str, str]] = {
            (
                str((record.source or {}).get("provider") or "").lower(),
                str((record.source or {}).get("provider_asset_id") or ""),
            )
            for record in runner.agent.registry.list_records()
            if (record.source or {}).get("kind") == "public_stock"
            and (record.source or {}).get("provider_asset_id")
        }

        for spec in SOURCE_SPECS:
            slot, query = spec["slot"], spec["query"]
            existing = _existing_slot(runner, slot)
            if existing is not None:
                try:
                    entry = _audit_source_record(
                        runner,
                        existing,
                        slot=slot,
                        query=query,
                        previous=manifest_slots.get(slot),
                    )
                except SourceGateError as exc:
                    runner.agent.registry.update_record(
                        existing.asset_id,
                        source_patch={
                            "production_source_status": "machine_rejected",
                            "production_source_rejection": str(exc),
                        },
                    )
                    trace_id = f"trace-echo-source-machine-reject-{slot}-{existing.asset_id}"
                    _record_evidence(
                        manager,
                        runner,
                        evidence_id=f"ev-source-machine-reject-{slot}-{existing.asset_id}",
                        kind="source_machine_gate",
                        trace_id=trace_id,
                        payload={
                            "slot": slot,
                            "asset_id": existing.asset_id,
                            "accepted": False,
                            "error": str(exc),
                            "reused": True,
                        },
                    )
                else:
                    prior_decision = str(entry.get("review", {}).get("decision") or "")
                    status = "approved" if prior_decision == "approve" else "pending_review"
                    runner.agent.registry.update_record(
                        existing.asset_id,
                        source_patch={
                            "production_slot": slot,
                            "production_query": query,
                            "production_source_status": status,
                        },
                    )
                    manifest_slots[slot] = entry
                    manifest_path = _save_source_manifest(runner, manifest)
                    evidence_id = f"ev-source-machine-{slot}-{entry['sha256'][:16]}"
                    _record_evidence(
                        manager,
                        runner,
                        evidence_id=evidence_id,
                        kind="source_machine_gate",
                        trace_id=f"trace-{evidence_id}",
                        payload={**entry, "accepted": True, "reused": True},
                    )
                    accepted.append(
                        {
                            "slot": slot,
                            "asset_id": existing.asset_id,
                            "provider": entry["provider"],
                            "provider_asset_id": entry["provider_asset_id"],
                            "sha256": entry["sha256"],
                            "contact_sheet": entry["contact_sheet"]["path"],
                            "review_decision": prior_decision or "pending",
                            "reused": True,
                        }
                    )
                    continue

            search_args = {
                "action": "search",
                "query": query,
                "provider": "auto",
                "media_type": "video",
                "orientation": "landscape",
                "limit": 12,
            }
            search_result = _call(
                runner,
                "sourcing",
                f"{slot}-search",
                "stock_media",
                search_args,
            )
            candidates = [
                item
                for item in (search_result.get("results") or [])
                if isinstance(item, dict) and _candidate_ok(item, used)
            ]
            search_trace, _ = _identity("sourcing", f"{slot}-search", search_args)
            _record_evidence(
                manager,
                runner,
                evidence_id=f"ev-stock-{slot}-search",
                kind="stock_search",
                trace_id=search_trace,
                payload={
                    "slot": slot,
                    "query": query,
                    "provider_errors": search_result.get("errors") or [],
                    "candidate_count": len(candidates),
                    "candidates": search_result.get("results") or [],
                },
            )
            if not candidates:
                raise RuntimeError(
                    f"no landscape 1080p >=6s stock result for {slot}: {query}"
                )

            selected: dict[str, Any] | None = None
            for candidate_index, candidate in enumerate(candidates[:3], start=1):
                provider = str(candidate["provider"]).lower()
                result_id = str(candidate["id"])
                fetch_args = {
                    "action": "fetch",
                    "query": query,
                    "provider": provider,
                    "media_type": "video",
                    "orientation": "landscape",
                    "limit": 12,
                    "result_id": result_id,
                }
                fetched = _call(
                    runner,
                    "sourcing",
                    f"{slot}-fetch-{candidate_index}",
                    "stock_media",
                    fetch_args,
                )
                asset_id = str(fetched.get("asset_id") or "")
                if not asset_id:
                    raise RuntimeError(f"stock fetch did not return an asset for {slot}")
                record = runner.agent.registry.get(asset_id)
                try:
                    entry = _audit_source_record(
                        runner,
                        record,
                        slot=slot,
                        query=query,
                        previous=None,
                        expected_provider=provider,
                        expected_provider_asset_id=result_id,
                    )
                except SourceGateError as exc:
                    runner.agent.registry.update_record(
                        asset_id,
                        source_patch={
                            "production_source_status": "machine_rejected",
                            "production_source_rejection": str(exc),
                        },
                    )
                    reject_id = f"ev-source-machine-reject-{slot}-{asset_id}"
                    _record_evidence(
                        manager,
                        runner,
                        evidence_id=reject_id,
                        kind="source_machine_gate",
                        trace_id=f"trace-{reject_id}",
                        payload={
                            "slot": slot,
                            "candidate": candidate,
                            "fetch": fetched,
                            "accepted": False,
                            "error": str(exc),
                            "reused": False,
                        },
                    )
                    continue
                runner.agent.registry.update_record(
                    asset_id,
                    source_patch={
                        "production_slot": slot,
                        "production_query": query,
                        "production_source_status": "pending_review",
                    },
                )
                manifest_slots[slot] = entry
                manifest_path = _save_source_manifest(runner, manifest)
                evidence_id = f"ev-source-machine-{slot}-{entry['sha256'][:16]}"
                _record_evidence(
                    manager,
                    runner,
                    evidence_id=evidence_id,
                    kind="source_machine_gate",
                    trace_id=f"trace-{evidence_id}",
                    payload={**entry, "accepted": True, "reused": False},
                )
                used.add((provider, result_id))
                selected = {
                    "slot": slot,
                    "asset_id": asset_id,
                    "provider": provider,
                    "provider_asset_id": result_id,
                    "duration_sec": entry["probe"].get("duration_sec"),
                    "width": entry["probe"].get("width"),
                    "height": entry["probe"].get("height"),
                    "sha256": entry["sha256"],
                    "contact_sheet": entry["contact_sheet"]["path"],
                    "review_decision": "pending",
                    "reused": False,
                }
                break
            if selected is None:
                raise RuntimeError(f"first three real candidates failed motion gate for {slot}")
            accepted.append(selected)
            print(
                json.dumps(
                    {
                        "phase": "sourcing",
                        "completed": len(accepted),
                        "total": len(SOURCE_SPECS),
                        "slot": slot,
                        "asset_id": selected["asset_id"],
                        "provider": selected["provider"],
                        "duration_sec": selected["duration_sec"],
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )

        if len({item["asset_id"] for item in accepted}) != 10:
            raise RuntimeError("sourcing did not produce ten distinct machine-verified assets")
        manifest_path = _save_source_manifest(runner, manifest)
        budget = manager.get_run(runner.project_id, runner.run_id)["budget"]
        return {
            "ok": True,
            # A physical probe is not a creative decision.  Only
            # review_sources() may move this run to rough_cut.
            "production_state": "sourcing",
            "project_revision": runner.project_revision,
            "accepted": accepted,
            "visual_review_required": manifest["status"] != "passed",
            "visual_review_status": manifest["status"],
            "visual_review_manifest": str(manifest_path),
            "contact_sheets": [item["contact_sheet"] for item in accepted],
            "spent_usd": budget.get("spent_usd"),
            "reserved_usd": budget.get("reserved_usd"),
            "remaining_usd": budget.get("remaining_usd"),
            "duplicate_billing_count": budget.get("duplicate_billing_count"),
        }
    finally:
        manager.close_all()


def _assert_manifest_ready_for_transition(runner, manifest: dict[str, Any]) -> None:
    """Fail closed if approved evidence no longer describes the current bytes."""

    if _manifest_status(manifest) != "passed":
        raise SourceGateError("all ten source slots require explicit visual approval")
    slots = manifest.get("slots") if isinstance(manifest.get("slots"), dict) else {}
    asset_ids: set[str] = set()
    provider_results: set[tuple[str, str]] = set()
    review_root = _source_review_dir(runner).resolve()
    for spec in SOURCE_SPECS:
        slot = spec["slot"]
        entry = slots.get(slot)
        if not isinstance(entry, dict) or entry.get("machine_gate") != "passed":
            raise SourceGateError(f"source slot {slot} lacks a passed machine gate")
        review = entry.get("review")
        if not isinstance(review, dict) or review.get("decision") != "approve":
            raise SourceGateError(f"source slot {slot} lacks human visual approval")
        asset_id = str(entry.get("asset_id") or "")
        if not asset_id or asset_id in asset_ids:
            raise SourceGateError("visual-review manifest must contain ten distinct assets")
        asset_ids.add(asset_id)
        record = runner.agent.registry.get(asset_id)
        if str((record.source or {}).get("production_slot") or "") != slot:
            raise SourceGateError(f"source slot {slot} no longer points to {asset_id}")

        actual_sha256 = _sha256_file(Path(record.path))
        if actual_sha256 != str(record.sha256 or "") or actual_sha256 != str(
            entry.get("sha256") or ""
        ):
            raise SourceGateError(f"source slot {slot} changed after visual review")
        provider = str((record.source or {}).get("provider") or "").strip().lower()
        provider_asset_id = str(
            (record.source or {}).get("provider_asset_id") or ""
        ).strip()
        source_url = str((record.source or {}).get("url") or "").strip()
        license_name = str((record.license or {}).get("name") or "").strip()
        license_url = str((record.license or {}).get("url") or "").strip()
        if (
            provider not in {"pexels", "pixabay"}
            or not provider_asset_id
            or not _valid_web_url(source_url)
            or not license_name
            or not _valid_web_url(license_url)
        ):
            raise SourceGateError(f"source slot {slot} provenance became incomplete")
        if (
            provider != str(entry.get("provider") or "")
            or provider_asset_id != str(entry.get("provider_asset_id") or "")
            or source_url != str(entry.get("source_url") or "")
            or license_name != str((entry.get("license") or {}).get("name") or "")
            or license_url != str((entry.get("license") or {}).get("url") or "")
        ):
            raise SourceGateError(f"source slot {slot} provenance changed after review")
        provider_key = (provider, provider_asset_id)
        if provider_key in provider_results:
            raise SourceGateError("visual-review manifest repeats a provider asset")
        provider_results.add(provider_key)

        probe = entry.get("probe") if isinstance(entry.get("probe"), dict) else {}
        motion = (
            probe.get("motion_evidence")
            if isinstance(probe.get("motion_evidence"), dict)
            else {}
        )
        if (
            int(probe.get("width") or 0) < MIN_SOURCE_WIDTH
            or int(probe.get("height") or 0) < MIN_SOURCE_HEIGHT
            or int(probe.get("width") or 0) <= int(probe.get("height") or 0)
            or float(probe.get("duration_sec") or 0.0) < 6.0
            or not bool(motion.get("real_motion_verified"))
        ):
            raise SourceGateError(f"source slot {slot} no longer satisfies media gates")

        sheet = (
            entry.get("contact_sheet")
            if isinstance(entry.get("contact_sheet"), dict)
            else {}
        )
        sheet_path = Path(str(sheet.get("path") or ""))
        try:
            sheet_path.resolve().relative_to(review_root)
        except (OSError, ValueError) as exc:
            raise SourceGateError(f"source slot {slot} contact sheet escaped evidence root") from exc
        if (
            not sheet_path.is_file()
            or int(sheet.get("frame_count") or 0) != 6
            or _sha256_file(sheet_path) != str(sheet.get("sha256") or "")
        ):
            raise SourceGateError(f"source slot {slot} contact sheet is missing or changed")


def review_sources(
    output_root: Path,
    *,
    slot: str,
    decision: str,
    reviewer: str,
    note: str,
    reviewer_type: str = "operator",
) -> dict[str, Any]:
    """Record one explicit visual decision and advance only after all ten pass."""

    slot_value = str(slot or "").strip().lower()
    decision_value = str(decision or "").strip().lower()
    reviewer_value = str(reviewer or "").strip()
    reviewer_type_value = str(reviewer_type or "").strip().lower()
    note_value = str(note or "").strip()
    queries = {item["slot"]: item["query"] for item in SOURCE_SPECS}
    if slot_value not in queries:
        raise SourceGateError(f"unknown source-review slot: {slot_value}")
    if decision_value not in {"approve", "reject"}:
        raise SourceGateError("source-review decision must be approve or reject")
    if not reviewer_value:
        raise SourceGateError("source-review requires an explicit reviewer identity")
    if reviewer_type_value not in {"agent", "human", "operator"}:
        raise SourceGateError(
            "source-review reviewer_type must be agent, human or operator"
        )
    if not note_value:
        raise SourceGateError("source-review requires a content-specific note")

    manager, runner = _open(output_root)
    try:
        run = manager.get_run(runner.project_id, runner.run_id)
        state = str(run.get("production_state") or run.get("state") or "")
        manifest = _load_source_manifest(runner, required=True)
        slots = manifest["slots"]
        existing_entry = slots.get(slot_value)
        if not isinstance(existing_entry, dict):
            raise SourceGateError(f"source slot {slot_value} has not been machine-verified")

        if state == "rough_cut":
            prior = (
                existing_entry.get("review")
                if isinstance(existing_entry.get("review"), dict)
                else {}
            )
            if manifest.get("status") == "passed" and decision_value == "approve":
                return {
                    "ok": True,
                    "replayed": True,
                    "production_state": "rough_cut",
                    "slot": slot_value,
                    "decision": str(prior.get("decision") or "approve"),
                    "visual_review_status": "passed",
                    "visual_review_manifest": str(_source_manifest_path(runner)),
                }
            raise SourceGateError("source review is closed after rough_cut begins")
        if state != "sourcing":
            raise SourceGateError(f"source review requires sourcing state, got {state}")

        asset_id = str(existing_entry.get("asset_id") or "")
        record = runner.agent.registry.get(asset_id)
        if str((record.source or {}).get("production_slot") or "") != slot_value:
            raise SourceGateError(f"source slot {slot_value} no longer points to {asset_id}")
        audited = _audit_source_record(
            runner,
            record,
            slot=slot_value,
            query=queries[slot_value],
            previous=existing_entry,
        )
        prior_review = (
            audited.get("review") if isinstance(audited.get("review"), dict) else {}
        )
        replayed = bool(
            prior_review.get("decision") == decision_value
            and prior_review.get("reviewer") == reviewer_value
            and prior_review.get("reviewer_type") == reviewer_type_value
            and prior_review.get("note") == note_value
        )
        if replayed:
            review = prior_review
        else:
            review = {
                "kind": f"{reviewer_type_value}_visual_review",
                "decision": decision_value,
                "reviewer": reviewer_value,
                "reviewer_type": reviewer_type_value,
                "note": note_value,
                "asset_id": asset_id,
                "asset_sha256": audited["sha256"],
                "contact_sheet_sha256": audited["contact_sheet"]["sha256"],
                "reviewed_at": _utc_now(),
            }
        audited["review"] = review
        slots[slot_value] = audited
        runner.agent.registry.update_record(
            asset_id,
            source_patch={
                "production_source_status": (
                    "approved" if decision_value == "approve" else "rejected"
                ),
                "production_visual_review": review,
            },
        )
        manifest_path = _save_source_manifest(runner, manifest)
        review_digest = _stable_digest(
            {
                "slot": slot_value,
                "fingerprint": audited["fingerprint"],
                "decision": decision_value,
                "reviewer": reviewer_value,
                "reviewer_type": reviewer_type_value,
                "note": note_value,
            }
        )[:16]
        evidence_id = f"ev-source-review-{slot_value}-{review_digest}"
        _record_evidence(
            manager,
            runner,
            evidence_id=evidence_id,
            kind="source_visual_review",
            trace_id=f"trace-{evidence_id}",
            payload={
                "slot": slot_value,
                "review": review,
                "fingerprint": audited["fingerprint"],
                "contact_sheet": audited["contact_sheet"],
            },
        )

        transition = None
        if manifest["status"] == "passed":
            _assert_manifest_ready_for_transition(runner, manifest)
            manifest.setdefault("passed_at", _utc_now())
            manifest_path = _save_source_manifest(runner, manifest)
            aggregate_digest = _stable_digest(
                {
                    item["slot"]: slots[item["slot"]]["fingerprint"]
                    for item in SOURCE_SPECS
                }
            )[:16]
            aggregate_id = f"ev-source-visual-pass-{aggregate_digest}"
            _record_evidence(
                manager,
                runner,
                evidence_id=aggregate_id,
                kind="source_visual_review_passed",
                trace_id=f"trace-{aggregate_id}",
                payload={
                    "manifest_path": str(manifest_path),
                    "manifest_sha256": _sha256_file(manifest_path),
                    "approved_slots": [item["slot"] for item in SOURCE_SPECS],
                    "reviewers": sorted(
                        {
                            str(slots[item["slot"]]["review"]["reviewer"])
                            for item in SOURCE_SPECS
                        }
                    ),
                },
            )
            transition = manager.transition_run(
                runner.project_id,
                runner.run_id,
                "rough_cut",
                trace_id="trace-echo-source-visual-review-passed",
            )

        current_state = (
            str(transition.get("state") or "rough_cut") if transition else "sourcing"
        )
        return {
            "ok": True,
            "replayed": replayed,
            "production_state": current_state,
            "slot": slot_value,
            "asset_id": asset_id,
            "decision": decision_value,
            "visual_review_status": manifest["status"],
            "visual_review_manifest": str(manifest_path),
            "contact_sheet": audited["contact_sheet"]["path"],
            "approved_count": sum(
                1
                for item in SOURCE_SPECS
                if str(
                    (slots.get(item["slot"]) or {}).get("review", {}).get("decision")
                    or ""
                )
                == "approve"
            ),
        }
    finally:
        manager.close_all()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("preflight", "source", "review-sources"))
    parser.add_argument(
        "--output-root",
        type=Path,
        default=Path.home() / ".gemia" / "v3",
    )
    parser.add_argument("--slot", choices=tuple(item["slot"] for item in SOURCE_SPECS))
    parser.add_argument("--decision", choices=("approve", "reject"))
    parser.add_argument("--reviewer")
    parser.add_argument(
        "--reviewer-type",
        choices=("agent", "human", "operator"),
        default="operator",
    )
    parser.add_argument("--note")
    args = parser.parse_args()
    if args.command == "preflight":
        result = preflight(args.output_root)
    elif args.command == "source":
        result = source(args.output_root)
    else:
        if not args.slot or not args.decision or not args.reviewer or not args.note:
            parser.error(
                "review-sources requires --slot, --decision, --reviewer and --note"
            )
        result = review_sources(
            args.output_root,
            slot=args.slot,
            decision=args.decision,
            reviewer=args.reviewer,
            note=args.note,
            reviewer_type=args.reviewer_type,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
