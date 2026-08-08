#!/usr/bin/env python3
"""Copy the frozen Echo Protocol v3 session into the durable production store.

The migration is intentionally one-way and fail-closed: source files are never
moved or deleted, an existing destination is never overwritten, every media
copy is hash-verified, and every rewritten legacy /tmp path is recorded.

Only the canonical editable project and the media referenced by its current
state enter the runtime store.  Historical renders, exports, build scratch and
other forensic material stay in the frozen baseline.  Files are copied as data
streams: carrying macOS metadata to an exFAT production disk creates AppleDouble
sidecars and can turn a small project into hundreds of megabytes of allocation.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
try:
    sys.path.remove(str(REPO_ROOT))
except ValueError:
    pass
sys.path.insert(0, str(REPO_ROOT))

from gemia.production_budget import ProductionMediaBudget
from gemia.production_store import ProductionStore, default_reality_contract
from gemia.tools._context import AssetRegistry


_EXPECTED_SESSION_ID = "v3-00a7080c78e7"
_EXPECTED_BASELINE_TREE_SHA256 = "dfc6dfa341a0404f2a55a186987d987c656e9010f3b7bac333aa92e08f2fbbc0"
_EXPECTED_BASELINE_FILE_COUNT = 174
_EXPECTED_BASELINE_TOTAL_BYTES = 62_574_198
_EXPECTED_STATE_SHA256 = "5795a65c133cca03cf9764590ce8f3e4e76d43f8289caff4d3a2cc52a1e01f4f"
_EXPECTED_SEED_SHA256 = "ec1aee087c0d7bef6b9fb795d8b5036323dc7fcec8e771e7adab91d3e4b47884"
_EXPECTED_PROJECT_META_SHA256 = "4db5a90e1905fdf1b6668becfeca3b7f6876350c4724603f39af2ab944736b93"
_EXPECTED_HISTORY_SHA256 = "e42a4d611fccf76b8e8a6b7bcfe6e4a27975b4db979c75067efb5c0dd1efb7c7"
_EXPECTED_SESSION_META_SHA256 = "1e6ead2a662fbec5cfff240b27a4ae2917f8335dd64deb06cb79a1516e022ab3"
_EXPECTED_TRANSCRIPT_SHA256 = "e4cea1dc8843ba3aed1bc5e6b8b32fe1a1ea75970deaf8c8d45bc089c6164640"
_EXPECTED_ASSET_IDS = tuple(
    [f"aud_{index:03d}" for index in range(4, 18)]
    + [f"img_{index:03d}" for index in (*range(1, 12), 13, 14, 15)]
)
_EXPECTED_MEDIA_BYTES = 45_209_052


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _tree_inventory(root: Path) -> dict[str, Any]:
    """Return a stable content inventory used to prove the baseline stayed frozen."""
    files: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise RuntimeError(f"frozen baseline must not contain symlinks: {path}")
        if path.is_file():
            files.append(
                {
                    "path": str(path.relative_to(root)),
                    "size": path.stat().st_size,
                    "sha256": _sha256(path),
                }
            )
    encoded = json.dumps(
        files,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return {
        "file_count": len(files),
        "total_bytes": sum(int(item["size"]) for item in files),
        "tree_sha256": hashlib.sha256(encoded).hexdigest(),
        "files": files,
    }


def _json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"expected JSON object: {path}")
    return value


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".migration-tmp")
    temp.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    temp.replace(path)


def _copy_file_verified(source: Path, destination: Path) -> str:
    """Copy one regular file without macOS metadata and verify its content."""
    if source.is_symlink() or not source.is_file():
        raise RuntimeError(f"migration source must be a regular file: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Python's optimized macOS copyfile path can carry extended attributes even
    # when metadata copying was not requested.  On exFAT those become ``._``
    # AppleDouble files with a large allocation unit.  Stream only the data fork.
    with source.open("rb") as input_stream, destination.open("xb") as output_stream:
        shutil.copyfileobj(input_stream, output_stream, length=1024 * 1024)
    source_hash = _sha256(source)
    if _sha256(destination) != source_hash:
        raise RuntimeError(f"copy hash mismatch: {source} -> {destination}")
    return source_hash


def _asset_relative_path(
    raw_path: str,
    *,
    session_id: str,
    source_workdir: Path,
) -> Path:
    """Resolve a legacy state path to a safe path inside the frozen workdir."""
    value = str(raw_path or "")
    legacy_roots = (
        f"/private/tmp/lumeri-v3/workdirs/{session_id}",
        f"/tmp/lumeri-v3/workdirs/{session_id}",
    )
    relative: Path | None = None
    for root in legacy_roots:
        if value == root or value.startswith(root + "/"):
            relative = Path(value[len(root) :].lstrip("/"))
            break
    if relative is None:
        candidate = Path(value).expanduser()
        try:
            relative = candidate.resolve().relative_to(source_workdir.resolve())
        except (OSError, ValueError):
            raise RuntimeError(f"asset path is outside the frozen workdir: {value}")
    if not relative.parts or relative.is_absolute() or ".." in relative.parts:
        raise RuntimeError(f"unsafe migrated asset path: {value}")
    return relative


def _copy_canonical_project(source_project: Path, project_dir: Path) -> dict[str, Any]:
    """Copy the current state and fingerprint unsafe legacy replay history.

    The legacy seed plus its historical patches does not reproduce the current
    state byte-for-byte or semantically (asset metadata and image durations are
    lost).  Installing that history as the active undo chain would corrupt the
    first migrated edit.  The current state therefore becomes the new seed and
    active patch history starts at zero.  The old chain remains fully preserved
    in the frozen baseline and is indexed here by content hash for audit.
    """
    project_dir.mkdir(parents=True, exist_ok=False)
    state_source = source_project / "state.json"
    _copy_file_verified(state_source, project_dir / "state.json")

    source_meta = _json(source_project / "meta.json")
    patches = source_project / "patches"
    patch_index: list[dict[str, Any]] = []
    if patches.is_dir():
        for source in sorted(patches.glob("*.json")):
            if source.name.startswith("._"):
                continue
            if source.is_symlink():
                raise RuntimeError(f"symlink is not allowed in patch history: {source}")
            if source.is_file():
                entry = _json(source)
                patch_index.append(
                    {
                        "seq": int(entry.get("seq") or 0),
                        "file": source.name,
                        "sha256": _sha256(source),
                    }
                )
    expected_patch_seq = int(source_meta.get("patch_seq") or 0)
    actual_sequences = [item["seq"] for item in patch_index]
    if actual_sequences != list(range(1, expected_patch_seq + 1)):
        raise RuntimeError(
            "legacy patch history is incomplete or non-contiguous: "
            f"expected 1..{expected_patch_seq}, got {actual_sequences[:3]}..."
        )
    audit = {
        "schema": "lumeri.legacy-patch-audit",
        "version": 1,
        "active": False,
        "reason": "legacy seed plus patches does not reproduce current state",
        "source_project": str(source_project),
        "source_state_sha256": _sha256(state_source),
        "source_seed_sha256": _sha256(source_project / "seed.json"),
        "source_meta_sha256": _sha256(source_project / "meta.json"),
        "patch_count": len(patch_index),
        "patches": patch_index,
    }
    _write_json(project_dir / "legacy-patch-audit.json", audit)
    (project_dir / "patches").mkdir(parents=True, exist_ok=True)
    (project_dir / "renders").mkdir(parents=True, exist_ok=True)
    return audit


def _rewrite(value: Any, replacements: dict[str, str]) -> tuple[Any, int]:
    if isinstance(value, str):
        result = value
        count = 0
        for old, new in replacements.items():
            occurrences = result.count(old)
            if occurrences:
                result = result.replace(old, new)
                count += occurrences
        return result, count
    if isinstance(value, list):
        output: list[Any] = []
        count = 0
        for item in value:
            rewritten, item_count = _rewrite(item, replacements)
            output.append(rewritten)
            count += item_count
        return output, count
    if isinstance(value, dict):
        output: dict[str, Any] = {}
        count = 0
        for key, item in value.items():
            rewritten, item_count = _rewrite(item, replacements)
            output[str(key)] = rewritten
            count += item_count
        return output, count
    return value, 0


def _runtime_messages(history: dict[str, Any]) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    for raw in history.get("messages") or []:
        if not isinstance(raw, dict):
            continue
        content = str(raw.get("content") or "").strip()
        if not content:
            continue
        role = "user" if raw.get("role") == "user" else "assistant"
        messages.append({"role": role, "content": content})
    return messages


def _validate_source_baseline(baseline: Path) -> dict[str, Any]:
    """Validate the one frozen Echo baseline completely before any write."""
    if baseline.name != _EXPECTED_SESSION_ID:
        raise RuntimeError(f"unexpected Echo baseline id: {baseline.name}")
    source_workdir = baseline / "workdir"
    source_project = source_workdir / "project" / _EXPECTED_SESSION_ID
    source_session = baseline / "session"
    pinned_files = {
        source_project / "state.json": _EXPECTED_STATE_SHA256,
        source_project / "seed.json": _EXPECTED_SEED_SHA256,
        source_project / "meta.json": _EXPECTED_PROJECT_META_SHA256,
        baseline / "history.json": _EXPECTED_HISTORY_SHA256,
        source_session / "meta.json": _EXPECTED_SESSION_META_SHA256,
        source_session / "transcript.jsonl": _EXPECTED_TRANSCRIPT_SHA256,
    }
    for path, expected_hash in pinned_files.items():
        if not path.is_file() or _sha256(path) != expected_hash:
            raise RuntimeError(f"frozen baseline pin mismatch: {path}")

    inventory = _tree_inventory(baseline)
    if (
        inventory["tree_sha256"] != _EXPECTED_BASELINE_TREE_SHA256
        or inventory["file_count"] != _EXPECTED_BASELINE_FILE_COUNT
        or inventory["total_bytes"] != _EXPECTED_BASELINE_TOTAL_BYTES
    ):
        raise RuntimeError("frozen baseline inventory does not match the pinned archive")

    state = _json(source_project / "state.json")
    if state.get("project_id") != "project_0964cf2ee90e":
        raise RuntimeError("unexpected canonical Echo project id")
    assets = [item for item in (state.get("assets") or []) if isinstance(item, dict)]
    asset_ids = tuple(sorted(str(item.get("id") or item.get("asset_id") or "") for item in assets))
    if asset_ids != tuple(sorted(_EXPECTED_ASSET_IDS)):
        raise RuntimeError(f"unexpected Echo asset closure: {asset_ids}")
    relative_paths: set[Path] = set()
    expected_media: dict[str, dict[str, str]] = {}
    media_bytes = 0
    for asset in assets:
        asset_id = str(asset.get("id") or asset.get("asset_id") or "")
        relative = _asset_relative_path(
            str(asset.get("source_path") or ""),
            session_id=_EXPECTED_SESSION_ID,
            source_workdir=source_workdir,
        )
        if len(relative.parts) != 1 or relative.name.startswith("._"):
            raise RuntimeError(f"derived or nested asset is not allowed in runtime closure: {relative}")
        if relative in relative_paths:
            raise RuntimeError(f"duplicate asset path in runtime closure: {relative}")
        relative_paths.add(relative)
        source = source_workdir / relative
        if not source.is_file() or source.is_symlink():
            raise RuntimeError(f"missing regular source asset: {source}")
        media_bytes += source.stat().st_size
        expected_media[asset_id] = {
            "relative_path": str(relative),
            "sha256": _sha256(source),
        }
    if len(assets) != 28 or media_bytes != _EXPECTED_MEDIA_BYTES:
        raise RuntimeError(
            f"unexpected referenced media closure: count={len(assets)} bytes={media_bytes}"
        )

    project_meta = _json(source_project / "meta.json")
    expected_patch_seq = int(project_meta.get("patch_seq") or 0)
    patch_sequences: list[int] = []
    for path in sorted((source_project / "patches").glob("*.json")):
        if path.name.startswith("._"):
            continue
        patch_sequences.append(int(_json(path).get("seq") or 0))
    if expected_patch_seq != 93 or patch_sequences != list(range(1, 94)):
        raise RuntimeError("legacy patch chain is incomplete or non-contiguous")

    history = _json(baseline / "history.json")
    session_meta = _json(source_session / "meta.json")
    if history.get("session_id") != _EXPECTED_SESSION_ID or len(history.get("messages") or []) != 10:
        raise RuntimeError("legacy history does not match the pinned Echo session")
    if session_meta.get("session_id") != _EXPECTED_SESSION_ID or int(session_meta.get("turn_count") or 0) != 5:
        raise RuntimeError("legacy session metadata does not match the pinned Echo session")
    transcript_lines = (source_session / "transcript.jsonl").read_text(encoding="utf-8").splitlines()
    if len(transcript_lines) != 3685:
        raise RuntimeError("legacy transcript line count does not match the pinned archive")
    for line_number, line in enumerate(transcript_lines, start=1):
        try:
            json.loads(line)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"invalid transcript JSON at line {line_number}") from exc
    return {
        "inventory": inventory,
        "state": state,
        "history": history,
        "session_meta": session_meta,
        "relative_paths": relative_paths,
        "expected_media": expected_media,
    }


def _verify_migrated_runtime(
    *,
    baseline: Path,
    storage_root: Path,
    logical_root: Path,
    project_id: str,
    run_id: str,
    session_id: str,
    expected_media: dict[str, dict[str, str]],
    expected_project_revision: int,
) -> dict[str, Any]:
    """Re-open every durable layer and verify the published migration closure."""
    workdir = storage_root / "workdirs" / session_id
    logical_workdir = logical_root / "workdirs" / session_id
    project_dir = storage_root / "projects" / project_id
    session_dir = storage_root / "sessions" / session_id
    for required in (workdir, project_dir, session_dir):
        if not required.is_dir():
            raise RuntimeError(f"migration destination is incomplete: {required}")

    state = _json(project_dir / "state.json")
    seed = _json(project_dir / "seed.json")
    meta = _json(project_dir / "meta.json")
    if seed != state or int(meta.get("patch_seq") or 0) != 0:
        raise RuntimeError("migrated current state/seed/patch sequence invariant failed")
    active_patches = [
        path
        for path in (project_dir / "patches").glob("*.json")
        if not path.name.startswith("._")
    ]
    if active_patches:
        raise RuntimeError("legacy patch files leaked into active undo history")
    state_text = (project_dir / "state.json").read_text(encoding="utf-8")
    if "/private/tmp/lumeri-v3" in state_text or '"/tmp/lumeri-v3' in state_text:
        raise RuntimeError("migrated canonical state contains a temporary legacy path")

    expected_names = {Path(item["relative_path"]).name for item in expected_media.values()}
    actual_names = {
        path.name
        for path in workdir.iterdir()
        if path.is_file() and not path.name.startswith("._")
    }
    if actual_names != expected_names or len(expected_names) != 28:
        raise RuntimeError("runtime workdir contains an unexpected media closure")
    for asset_id, copied in expected_media.items():
        path = workdir / copied["relative_path"]
        if not path.is_file() or _sha256(path) != copied["sha256"]:
            raise RuntimeError(f"migrated media hash mismatch: {asset_id}")

    store = ProductionStore(storage_root)
    project = store.load_project(project_id)
    run = store.load_run(project_id, run_id)
    session = store.load_session(session_id)
    runtime = store.load_runtime_state(session_id)
    # During staging, run.json already carries the post-publication logical
    # ledger path.  Verify the physical staging ledger directly; after the
    # atomic directory rename both paths become identical.
    physical_budget_path = store.run_dir(project_id, run_id) / "budget.json"
    budget = ProductionMediaBudget.open(physical_budget_path).snapshot()
    if int(project.get("revision") or 0) != expected_project_revision:
        raise RuntimeError("production project revision does not match migration receipt")
    if int(run.get("project_revision") or 0) != expected_project_revision or run.get("state") != "sourcing":
        raise RuntimeError("production run did not restore at the sourcing stage")
    if (
        float(budget.get("cap_usd") or 0.0) != 15.0
        or float(budget.get("baseline_spend_usd") or 0.0) != 1.525
        or float(budget.get("committed_usd") or 0.0) != 1.525
        or budget.get("reconciliation_blockers")
        or int(budget.get("duplicate_billing_count") or 0) != 0
    ):
        raise RuntimeError("migrated production budget invariant failed")
    if session.get("project_id") != project_id or session.get("run_id") != run_id:
        raise RuntimeError("migrated session is not bound to the production run")
    if Path(str(session.get("output_dir") or "")).resolve() != logical_workdir.resolve():
        raise RuntimeError("migrated session output_dir is not bound to the published workdir")
    if (
        runtime.get("project_id") != project_id
        or runtime.get("run_id") != run_id
        or int(runtime.get("project_revision") or 0) != expected_project_revision
        or len(runtime.get("messages") or []) != 10
    ):
        raise RuntimeError("migrated runtime state/messages are incomplete")

    registry = AssetRegistry.load(store.asset_registry_path(project_id))
    records = {record.asset_id: record for record in registry.list_records()}
    if set(records) != set(_EXPECTED_ASSET_IDS):
        raise RuntimeError("migrated asset registry closure is incomplete")
    for asset_id, record in records.items():
        expected = expected_media[asset_id]
        expected_logical_path = (logical_workdir / expected["relative_path"]).resolve()
        physical_path = (workdir / expected["relative_path"]).resolve()
        if (
            record.path.resolve() != expected_logical_path
            or not physical_path.is_file()
            or _sha256(physical_path) != expected["sha256"]
            or not record.license
            or not record.source.get("kind")
        ):
            raise RuntimeError(f"migrated asset registry record is incomplete: {asset_id}")

    if _sha256(session_dir / "legacy-history.json") != _EXPECTED_HISTORY_SHA256:
        raise RuntimeError("migrated legacy history hash mismatch")
    if _sha256(session_dir / "transcript.jsonl") != _EXPECTED_TRANSCRIPT_SHA256:
        raise RuntimeError("migrated transcript hash mismatch")
    if _tree_inventory(baseline)["tree_sha256"] != _EXPECTED_BASELINE_TREE_SHA256:
        raise RuntimeError("source baseline changed during destination verification")
    return {
        "passed": True,
        "project_revision": expected_project_revision,
        "project_patch_seq": 0,
        "asset_count": len(records),
        "runtime_media_bytes": sum(
            (workdir / item["relative_path"]).stat().st_size
            for item in expected_media.values()
        ),
        "message_count": len(runtime.get("messages") or []),
        "run_state": run.get("state"),
        "baseline_spend_usd": budget.get("baseline_spend_usd"),
        "hard_cap_usd": budget.get("cap_usd"),
        "duplicate_billing_count": budget.get("duplicate_billing_count"),
    }


def _rewrite_json_tree(root: Path, replacements: dict[str, str]) -> list[dict[str, Any]]:
    """Rewrite absolute staging paths in all JSON records before publication."""
    changed: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*.json")):
        if path.name.startswith("._"):
            continue
        value = _json(path)
        before = _sha256(path)
        rewritten, count = _rewrite(value, replacements)
        if count:
            _write_json(path, rewritten)
            changed.append(
                {
                    "path": str(path.relative_to(root)),
                    "replacement_count": count,
                    "before_sha256": before,
                    "after_sha256": _sha256(path),
                }
            )
    return changed


def _publish_into_existing_root(
    *,
    storage_root: Path,
    output_root: Path,
    project_id: str,
    session_id: str,
) -> Path:
    """Publish only this migration's namespaces into an existing v3 root.

    ``~/.gemia/v3`` can already contain the frozen ``baselines`` archive and
    telemetry files.  Replacing that whole directory would violate the
    preservation contract.  Each new namespace is staged on the same APFS
    filesystem, collision-checked before the first rename, then published with
    a durable receipt that names every completed component.  Unrelated entries
    are never copied, moved or rewritten.
    """

    components = [
        (
            "workdir",
            storage_root / "workdirs" / session_id,
            output_root / "workdirs" / session_id,
        ),
        (
            "session",
            storage_root / "sessions" / session_id,
            output_root / "sessions" / session_id,
        ),
        (
            "project",
            storage_root / "projects" / project_id,
            output_root / "projects" / project_id,
        ),
        (
            "status",
            storage_root / "migration-status.json",
            output_root / "migration-status.json",
        ),
    ]
    for label, source, target in components:
        if not source.exists():
            raise RuntimeError(f"missing staged migration component {label}: {source}")
        if target.exists():
            raise RuntimeError(f"refusing migration namespace collision: {target}")

    receipt_path = output_root / f".{session_id}.migration-publish.json"
    if receipt_path.exists():
        raise RuntimeError(f"unfinished migration publish receipt already exists: {receipt_path}")
    receipt: dict[str, Any] = {
        "schema": "lumeri.migration-publish-receipt",
        "version": 1,
        "project_id": project_id,
        "session_id": session_id,
        "storage_root": str(storage_root),
        "output_root": str(output_root),
        "state": "publishing",
        "completed": [],
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    _write_json(receipt_path, receipt)
    for label, source, target in components:
        target.parent.mkdir(parents=True, exist_ok=True)
        source.replace(target)
        receipt["completed"].append(label)
        receipt["updated_at"] = datetime.now(timezone.utc).isoformat()
        _write_json(receipt_path, receipt)
    return receipt_path


def migrate(*, baseline: Path, output_root: Path, run_id: str) -> dict[str, Any]:
    baseline = baseline.expanduser().resolve()
    output_root = output_root.expanduser().resolve()
    session_id = baseline.name
    project_id = session_id
    source_workdir = baseline / "workdir"
    source_project = source_workdir / "project" / session_id
    source_session = baseline / "session"
    history_path = baseline / "history.json"
    if not source_project.is_dir() or not history_path.is_file():
        raise RuntimeError(f"incomplete frozen baseline: {baseline}")
    preflight = _validate_source_baseline(baseline)
    baseline_inventory = preflight["inventory"]
    source_state = preflight["state"]
    expected_media = preflight["expected_media"]

    merge_into_existing = False
    if output_root.exists():
        manifest_path = output_root / "projects" / project_id / "migration-manifest.json"
        if manifest_path.is_file():
            existing = _json(manifest_path)
            if (
                existing.get("schema") != "lumeri.legacy-production-migration"
                or existing.get("source_baseline") != str(baseline)
                or existing.get("run_id") != run_id
                or _json(output_root / "migration-status.json").get("source_tree_sha256")
                != _EXPECTED_BASELINE_TREE_SHA256
            ):
                raise RuntimeError("existing destination does not match this pinned migration")
            verification = _verify_migrated_runtime(
                baseline=baseline,
                storage_root=output_root,
                logical_root=output_root,
                project_id=project_id,
                run_id=run_id,
                session_id=session_id,
                expected_media=expected_media,
                expected_project_revision=int(existing.get("project_revision") or 0),
            )
            existing["verification"] = verification
            existing["idempotent"] = True
            existing["reverified_at"] = datetime.now(timezone.utc).isoformat()
            _write_json(manifest_path, existing)
            _write_json(
                output_root / "migration-status.json",
                {
                    "schema": "lumeri.migration-status",
                    "version": 1,
                    "status": "complete",
                    "source_baseline": str(baseline),
                    "source_tree_sha256": _EXPECTED_BASELINE_TREE_SHA256,
                    "run_id": run_id,
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                    "verification": verification,
                },
            )
            return existing
        managed_targets = (
            output_root / "projects" / project_id,
            output_root / "sessions" / session_id,
            output_root / "workdirs" / session_id,
            output_root / "migration-status.json",
            output_root / f".{session_id}.migration-publish.json",
        )
        collisions = [path for path in managed_targets if path.exists()]
        if collisions:
            raise RuntimeError(
                "refusing incomplete or foreign migration namespace collision: "
                + ", ".join(str(path) for path in collisions)
            )
        merge_into_existing = True

    output_root.parent.mkdir(parents=True, exist_ok=True)
    staging_parent = output_root if merge_into_existing else output_root.parent
    storage_root = Path(
        tempfile.mkdtemp(
            prefix=f".{output_root.name}.migration-",
            dir=str(staging_parent),
        )
    ).resolve()
    workdir = storage_root / "workdirs" / session_id
    logical_workdir = output_root / "workdirs" / session_id
    project_dir = storage_root / "projects" / project_id
    logical_project_dir = output_root / "projects" / project_id
    session_dir = storage_root / "sessions" / session_id
    logical_session_dir = output_root / "sessions" / session_id
    _write_json(
        storage_root / "migration-status.json",
        {
            "schema": "lumeri.migration-status",
            "version": 1,
            "status": "running",
            "source_baseline": str(baseline),
            "source_tree_sha256": baseline_inventory["tree_sha256"],
            "output_root": str(output_root),
            "staging_root": str(storage_root),
            "run_id": run_id,
            "started_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    print(f"copy referenced media -> {workdir}", flush=True)
    workdir.mkdir(parents=True, exist_ok=False)
    copied_media: dict[str, dict[str, str]] = {}
    for asset in source_state.get("assets") or []:
        if not isinstance(asset, dict):
            continue
        asset_id = str(asset.get("id") or asset.get("asset_id") or "")
        kind = str(asset.get("media_kind") or "")
        if not asset_id or kind not in {"image", "audio", "video", "lottie"}:
            continue
        relative = _asset_relative_path(
            str(asset.get("source_path") or ""),
            session_id=session_id,
            source_workdir=source_workdir,
        )
        source = source_workdir / relative
        destination = workdir / relative
        copied_media[asset_id] = {
            "relative_path": str(relative),
            "sha256": _copy_file_verified(source, destination),
        }
    if copied_media != expected_media:
        raise RuntimeError("copied media closure differs from pinned preflight")
    print(f"copy canonical project -> {project_dir}", flush=True)
    legacy_patch_audit = _copy_canonical_project(source_project, project_dir)

    old_roots = {
        f"/private/tmp/lumeri-v3/workdirs/{session_id}": str(logical_workdir),
        f"/tmp/lumeri-v3/workdirs/{session_id}": str(logical_workdir),
    }
    rewritten_files: list[dict[str, Any]] = []
    for path in sorted(project_dir.rglob("*.json")):
        # exFAT stores macOS extended attributes in AppleDouble files such as
        # ``._0001.json``.  They are metadata, not JSON project records.
        if path.name.startswith("._"):
            continue
        value = _json(path)
        before = _sha256(path)
        rewritten, replacements = _rewrite(value, old_roots)
        if replacements:
            _write_json(path, rewritten)
            rewritten_files.append(
                {
                    "path": str(path.relative_to(project_dir)),
                    "replacement_count": replacements,
                    "before_sha256": before,
                    "after_sha256": _sha256(path),
                }
            )

    state_path = project_dir / "state.json"
    state = _json(state_path)
    migrated_at = datetime.now(timezone.utc).isoformat()
    state["metadata"] = {
        **dict(state.get("metadata") or {}),
        "migration": {
            "kind": "legacy_v3_to_production_v2",
            "source_session_id": session_id,
            "migrated_at": migrated_at,
            "legacy_patch_history_active": False,
            "legacy_patch_audit": "legacy-patch-audit.json",
        },
    }
    state["updated_at"] = migrated_at
    _write_json(state_path, state)
    # Current normalized state is the only trustworthy migrated edit baseline.
    # All new undo history begins after migration at patch sequence zero.
    _write_json(project_dir / "seed.json", state)
    source_project_meta = _json(source_project / "meta.json")
    _write_json(
        project_dir / "meta.json",
        {
            "project_id": project_id,
            "created_at": source_project_meta.get("created_at") or migrated_at,
            "updated_at": migrated_at,
            "patch_seq": 0,
            "undo_log": [],
            "migration": {
                "legacy_patch_count": legacy_patch_audit["patch_count"],
                "legacy_patch_audit": "legacy-patch-audit.json",
                "active_seed": "migrated current state",
            },
        },
    )
    if _json(project_dir / "seed.json") != _json(state_path):
        raise RuntimeError("migrated seed must exactly equal the current project state")
    active_patch_files = [
        path
        for path in (project_dir / "patches").glob("*.json")
        if not path.name.startswith("._")
    ]
    if active_patch_files:
        raise RuntimeError("legacy patches must not be installed as active undo history")
    canonical_text = (project_dir / "state.json").read_text(encoding="utf-8")
    if "/private/tmp/lumeri-v3" in canonical_text or '"/tmp/lumeri-v3' in canonical_text:
        raise RuntimeError("migrated project still contains a legacy temporary path")

    records: list[dict[str, Any]] = []
    verified_media: list[dict[str, Any]] = []
    for asset in state.get("assets") or []:
        if not isinstance(asset, dict):
            continue
        asset_id = str(asset.get("id") or asset.get("asset_id") or "")
        kind = str(asset.get("media_kind") or "")
        copied = copied_media.get(asset_id)
        if not asset_id or kind not in {"image", "audio", "video", "lottie"}:
            continue
        if not copied:
            raise RuntimeError(f"referenced media was not copied: {asset_id}")
        relative = Path(copied["relative_path"])
        physical_path = (workdir / relative).resolve()
        logical_path = (logical_workdir / relative).resolve()
        if not physical_path.is_file():
            raise RuntimeError(f"migrated asset is missing: {asset_id} -> {physical_path}")
        source_path = source_workdir / relative
        if not source_path.is_file():
            raise RuntimeError(f"baseline asset is missing: {asset_id} -> {source_path}")
        source_hash = copied["sha256"]
        copied_hash = _sha256(physical_path)
        if source_hash != copied_hash:
            raise RuntimeError(f"copy hash mismatch for {asset_id}")
        role = ""
        if kind == "audio":
            role = "music" if asset_id == "aud_017" else "narration"
            source = {
                "kind": "owned_audio",
                "provider": "local_synthesis" if role == "music" else "local_tts",
                "role": role,
                "receipt_id": "echo-protocol-baseline",
                "creation_basis": (
                    "project-local synthesized score"
                    if role == "music"
                    else "project-local text-to-speech narration"
                ),
            }
            license_info = {"basis": "project_created_owned_audio"}
        elif kind == "image":
            source = {
                "kind": "generated_image",
                "provider": "vertex",
                "receipt_id": "echo-protocol-baseline",
            }
            license_info = {"basis": "provider_generated_asset"}
        else:
            source = {
                "kind": "owned_video",
                "provider": "legacy_project",
                "receipt_id": "echo-protocol-baseline",
                "real_motion_verified": False,
            }
            license_info = {"basis": "legacy_project_asset"}
        records.append(
            {
                "asset_id": asset_id,
                "kind": kind,
                "path": str(logical_path),
                "summary": f"migrated Echo Protocol {kind}",
                "created_at": asset.get("created_at") or state.get("created_at"),
                "lineage": [],
                "sha256": copied_hash,
                "source": source,
                "license": license_info,
            }
        )
        verified_media.append(
            {
                "asset_id": asset_id,
                "relative_path": str(relative),
                "sha256": copied_hash,
                "role": role,
            }
        )
    registry = AssetRegistry.from_dict({"records": records, "counters": {}})

    store = ProductionStore(storage_root)
    project = store.create_project(project_id)
    # Match SessionRunner's revision identity: raw project state plus only
    # registry records referenced by that state, in stable asset-id order.
    referenced = {
        str(item.get("id") or item.get("asset_id") or "")
        for item in (state.get("assets") or [])
        if isinstance(item, dict)
    }
    registry_payload = {
        "records": [
            record.to_dict()
            for record in sorted(registry.list_records(), key=lambda item: item.asset_id)
            if record.asset_id in referenced
        ]
    }
    state_hash = hashlib.sha256(
        json.dumps(
            {"project": state, "assets": registry_payload},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()
    meta = _json(project_dir / "meta.json")
    project = store.observe_project_state(
        project_id,
        state_hash=state_hash,
        timeline_patch_seq=int(meta.get("patch_seq") or 0),
    )
    registry.save(store.asset_registry_path(project_id))

    contract = default_reality_contract(
        brief=(
            "继续完成现有120秒《回声协议》：复用既有图像、旁白与配乐，"
            "补足至少10条授权完整的真实动态素材、本地MG与完整声音，"
            "总外部媒体成本不超过15美元。"
        ),
        hard_cap_usd=15.0,
    )
    # RealityContract defaults are intentionally project-agnostic. The legacy
    # Echo case binds every case-specific delivery and creative gate here.
    contract["deliverable"]["duration_sec"] = 120.0
    contract["deliverable"]["audio"]["required_roles"] = [
        "music",
        "narration",
        "sfx",
    ]
    contract["acceptance"].update(
        {
            "edit_units": {"min": 36, "max": 48},
            "median_shot_duration_max_sec": 3.0,
            "verified_motion_min_sec": 60.0,
            "licensed_public_motion_assets_min": 10,
            "static_shot_max_sec": 3.0,
        }
    )
    contract["media_policy"].update(
        {
            "source_priority": [
                "owned_existing",
                "licensed_public_stock",
                "local_compositing",
                "generated_video_if_blocked",
            ],
            "full_ai_video_default": False,
            "generated_video_requires_recorded_blocker": True,
            "generated_video_default_calls": 0,
            "generated_video_attempt_cap": 3,
            "generated_video_duration_cap_sec": 24,
            "generated_image_default_cap": 0,
            "generated_image_blocker_cap": 5,
        }
    )
    store.create_run(project_id, run_id, reality_contract=contract, hard_cap_usd=15.0)
    store.sync_run_project_revision(project_id, run_id, int(project["revision"]))
    store.transition_run(project_id, run_id, "preflight", trace_id="migration")
    store.transition_run(project_id, run_id, "sourcing", trace_id="migration")
    budget = store.media_budget(project_id, run_id)
    budget.import_baseline(
        import_key="echo-protocol-legacy-minimum",
        amount_usd="1.525",
        evidence={
            "basis": "frozen production record minimum",
            "source_session_id": session_id,
        },
    )
    store.refresh_budget_summary(project_id, run_id)

    history = _json(history_path)
    old_meta = _json(source_session / "meta.json")
    store.create_session_record(
        session_id,
        project_id=project_id,
        run_id=run_id,
        output_dir=logical_workdir,
        account_id=str(history.get("account_id") or ""),
        remote=False,
    )
    session_record = store.load_session(session_id)
    session_record.update(
        {
            "created_at": old_meta.get("created_at") or session_record["created_at"],
            "turn_count": int(old_meta.get("turn_count") or 0),
            "plan_mode": bool(old_meta.get("plan_mode", False)),
            "status": "closed",
            "legacy_history": "legacy-history.json",
        }
    )
    _write_json(store.session_meta_path(session_id), session_record)
    _copy_file_verified(history_path, session_dir / "legacy-history.json")
    transcript = source_session / "transcript.jsonl"
    if transcript.is_file():
        _copy_file_verified(transcript, session_dir / "transcript.jsonl")
    store.save_runtime_state(
        session_id,
        {
            "session_id": session_id,
            "project_id": project_id,
            "run_id": run_id,
            "project_revision": int(project["revision"]),
            "messages": _runtime_messages(history),
            "pinned_intent": contract["brief"],
            "last_user_message": next(
                (
                    message["content"]
                    for message in reversed(_runtime_messages(history))
                    if message["role"] == "user"
                ),
                None,
            ),
            "turn_count": int(old_meta.get("turn_count") or 0),
            "compacted_history": [],
            "plan_mode": False,
            "background_lineage": {},
        },
    )

    final_baseline_inventory = _tree_inventory(baseline)
    if final_baseline_inventory["tree_sha256"] != baseline_inventory["tree_sha256"]:
        raise RuntimeError("frozen baseline changed while migration was running")

    manifest = {
        "schema": "lumeri.legacy-production-migration",
        "version": 1,
        "source_baseline": str(baseline),
        "source_baseline_inventory": baseline_inventory,
        "output_root": str(output_root),
        "session_id": session_id,
        "project_id": project_id,
        "run_id": run_id,
        "project_revision": int(project["revision"]),
        "project_state_sha256": _sha256(state_path),
        "rewritten_files": rewritten_files,
        "verified_media": verified_media,
        "verified_media_count": len(verified_media),
        "legacy_patch_audit": {
            "path": str(logical_project_dir / "legacy-patch-audit.json"),
            "sha256": _sha256(project_dir / "legacy-patch-audit.json"),
            "patch_count": legacy_patch_audit["patch_count"],
            "active": False,
        },
        "runtime_copy_policy": {
            "included": [
                "current project state",
                "current state as the new editable seed",
                "fresh patch history for post-migration undo",
                "media referenced by current state",
                "legacy messages and transcript",
                "hash index for legacy patch audit",
            ],
            "baseline_only": [
                "legacy patch bodies",
                "legacy seed",
                "historical renders",
                "historical exports",
                "build scratch",
                "thumbnails",
            ],
            "copy_metadata": False,
        },
        "baseline_spend_usd": budget.snapshot()["baseline_spend_usd"],
        "destination_index": {
            "workdir": str(logical_workdir),
            "project": str(logical_project_dir),
            "session": str(logical_session_dir),
            "asset_registry": str(
                output_root / store.asset_registry_path(project_id).relative_to(storage_root)
            ),
            "budget_ledger": str(output_root / budget.path.relative_to(storage_root)),
        },
        "migrated_at": migrated_at,
        "idempotent": False,
    }
    manifest_path = project_dir / "migration-manifest.json"
    _write_json(manifest_path, manifest)

    staging_path_rewrites = _rewrite_json_tree(
        storage_root,
        {str(storage_root): str(output_root)},
    )
    for path in storage_root.rglob("*.json*"):
        if path.name.startswith("._") or not path.is_file():
            continue
        if str(storage_root).encode("utf-8") in path.read_bytes():
            raise RuntimeError(f"staging path leaked into durable record: {path}")
    manifest = _json(manifest_path)
    manifest["staging_path_rewrites"] = staging_path_rewrites
    staging_verification = _verify_migrated_runtime(
        baseline=baseline,
        storage_root=storage_root,
        logical_root=output_root,
        project_id=project_id,
        run_id=run_id,
        session_id=session_id,
        expected_media=expected_media,
        expected_project_revision=int(project["revision"]),
    )
    manifest["staging_verification"] = staging_verification
    _write_json(manifest_path, manifest)
    _write_json(
        storage_root / "migration-status.json",
        {
            "schema": "lumeri.migration-status",
            "version": 1,
            "status": "verified_staging",
            "source_baseline": str(baseline),
            "source_tree_sha256": _EXPECTED_BASELINE_TREE_SHA256,
            "output_root": str(output_root),
            "run_id": run_id,
            "verified_at": datetime.now(timezone.utc).isoformat(),
            "verification": staging_verification,
        },
    )

    if output_root.exists() and not merge_into_existing:
        raise RuntimeError(f"destination appeared before atomic publish: {output_root}")
    publish_receipt_path: Path | None = None
    if merge_into_existing:
        publish_receipt_path = _publish_into_existing_root(
            storage_root=storage_root,
            output_root=output_root,
            project_id=project_id,
            session_id=session_id,
        )
    else:
        storage_root.replace(output_root)

    verification = _verify_migrated_runtime(
        baseline=baseline,
        storage_root=output_root,
        logical_root=output_root,
        project_id=project_id,
        run_id=run_id,
        session_id=session_id,
        expected_media=expected_media,
        expected_project_revision=int(project["revision"]),
    )
    published_manifest_path = logical_project_dir / "migration-manifest.json"
    manifest = _json(published_manifest_path)
    manifest["verification"] = verification
    manifest["idempotent"] = False
    manifest["published_at"] = datetime.now(timezone.utc).isoformat()
    _write_json(published_manifest_path, manifest)
    _write_json(
        output_root / "migration-status.json",
        {
            "schema": "lumeri.migration-status",
            "version": 1,
            "status": "complete",
            "source_baseline": str(baseline),
            "source_tree_sha256": _EXPECTED_BASELINE_TREE_SHA256,
            "run_id": run_id,
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "verification": verification,
        },
    )
    if publish_receipt_path is not None:
        receipt = _json(publish_receipt_path)
        receipt["state"] = "complete"
        receipt["completed_at"] = datetime.now(timezone.utc).isoformat()
        receipt["verification"] = verification
        _write_json(publish_receipt_path, receipt)
        completed_receipt = output_root / "migration-publish-receipt.json"
        if completed_receipt.exists():
            raise RuntimeError(f"migration publish receipt collision: {completed_receipt}")
        publish_receipt_path.replace(completed_receipt)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--run-id", default="echo-protocol-production")
    args = parser.parse_args()
    manifest = migrate(
        baseline=args.baseline,
        output_root=args.output_root,
        run_id=args.run_id,
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
