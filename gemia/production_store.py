"""Durable production facts for Lumeri v3.

The timeline remains owned by :mod:`gemia.project_store`.  This store owns the
facts that must survive a process restart but do not belong inside the editing
document: project identity/revision, production runs, reality contracts,
evidence, reviews, run-level media spend, session bindings and turn
idempotency.

All JSON snapshots use write-to-temp + ``os.replace``.  Evidence and turn
records are immutable or monotonic, and every run mutation also appends a
small audit event.  The store never contains provider credentials.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, TextIO

from gemia.reality_contract import (
    MAX_MEDIA_BUDGET_USD,
    contract_gaps,
    default_reality_contract,
    normalize_reality_contract,
    required_acceptance_check_codes,
)


_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,95}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_DESIGN_PROGRAM_DIGEST_SCOPE = "creator-source-v2"

PRODUCTION_STATES = (
    "created",
    "preflight",
    "sourcing",
    "rough_cut",
    "sound_pass",
    "visual_pass",
    "rendering",
    "verifying",
    "ready_for_review",
    "revising",
    "accepted",
    "blocked",
    "cancelled",
    "failed",
)

_STATE_TRANSITIONS: dict[str, frozenset[str]] = {
    "created": frozenset({"preflight", "blocked", "cancelled", "failed"}),
    "preflight": frozenset({"sourcing", "blocked", "cancelled", "failed"}),
    "sourcing": frozenset({"rough_cut", "blocked", "cancelled", "failed"}),
    "rough_cut": frozenset({"sound_pass", "blocked", "cancelled", "failed"}),
    "sound_pass": frozenset({"visual_pass", "blocked", "cancelled", "failed"}),
    "visual_pass": frozenset({"rendering", "blocked", "cancelled", "failed"}),
    "rendering": frozenset({"verifying", "blocked", "cancelled", "failed"}),
    "verifying": frozenset({"ready_for_review", "revising", "blocked", "failed"}),
    "ready_for_review": frozenset({"accepted", "revising", "blocked", "cancelled"}),
    "revising": frozenset(
        {"rough_cut", "sound_pass", "visual_pass", "rendering", "blocked", "cancelled", "failed"}
    ),
    "accepted": frozenset({"revising"}),
    "blocked": frozenset(
        {"preflight", "sourcing", "rough_cut", "sound_pass", "visual_pass", "rendering", "cancelled", "failed"}
    ),
    "cancelled": frozenset(),
    "failed": frozenset({"preflight", "cancelled"}),
}

_HUMAN_CREATIVE_DIMENSIONS = (
    "story",
    "pacing",
    "visual",
    "sound",
    "publishable",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ProductionStoreError(RuntimeError):
    """Structured failure intended to cross the HTTP/MCP boundary."""

    code = "E_PRODUCTION_STORE"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.code = str(code or self.code)


class ProductionNotFoundError(ProductionStoreError):
    code = "E_NOT_FOUND"


class ProductionValidationError(ProductionStoreError):
    code = "E_BAD_ARG"


class RevisionConflictError(ProductionStoreError):
    code = "E_REVISION_CONFLICT"

    def __init__(
        self,
        message: str,
        *,
        current_revision: int | None = None,
        code: str | None = None,
    ) -> None:
        super().__init__(message, code=code)
        self.current_revision = (
            int(current_revision) if current_revision is not None else None
        )


class IdempotencyConflictError(ProductionStoreError):
    code = "E_IDEMPOTENCY_CONFLICT"


class StateTransitionError(ProductionStoreError):
    code = "E_STATE_TRANSITION"


class ProductionBudgetError(ProductionStoreError):
    code = "E_BUDGET"


class ProductionStore:
    """Atomic filesystem store rooted at the v3 output root."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()
        self.projects_root = self.root / "projects"
        self.sessions_root = self.root / "sessions"
        self.projects_root.mkdir(parents=True, exist_ok=True)
        self.sessions_root.mkdir(parents=True, exist_ok=True)
        self._locks_guard = threading.Lock()
        self._locks: dict[str, threading.RLock] = {}
        # A formal tool claim owns an advisory lease for the whole dispatcher
        # execution, not merely for the JSON write.  Another process can then
        # distinguish a genuinely abandoned receipt from a still-running
        # owner without relying on a timeout or a racy PID probe.
        self._owner_id = f"owner-{os.getpid()}-{uuid.uuid4().hex}"
        self._tool_call_leases_guard = threading.RLock()
        self._tool_call_leases: dict[str, TextIO] = {}

    # -- paths ---------------------------------------------------------

    def project_dir(self, project_id: str) -> Path:
        self._validate_id(project_id, "project_id")
        return self.projects_root / project_id

    def project_record_path(self, project_id: str) -> Path:
        return self.project_dir(project_id) / "production.json"

    def asset_registry_path(self, project_id: str) -> Path:
        return self.project_dir(project_id) / "assets.json"

    def runs_dir(self, project_id: str) -> Path:
        return self.project_dir(project_id) / "runs"

    def run_dir(self, project_id: str, run_id: str) -> Path:
        self._validate_id(run_id, "run_id")
        return self.runs_dir(project_id) / run_id

    def run_path(self, project_id: str, run_id: str) -> Path:
        return self.run_dir(project_id, run_id) / "run.json"

    def run_events_path(self, project_id: str, run_id: str) -> Path:
        return self.run_dir(project_id, run_id) / "events.jsonl"

    def evidence_dir(self, project_id: str, run_id: str) -> Path:
        return self.run_dir(project_id, run_id) / "evidence"

    def creative_ir_path(self, project_id: str, run_id: str) -> Path:
        return self.run_dir(project_id, run_id) / "creative-ir.json"

    def creative_ir_events_path(self, project_id: str, run_id: str) -> Path:
        return self.run_dir(project_id, run_id) / "creative-ir.events.jsonl"

    def turns_dir(self, project_id: str, run_id: str) -> Path:
        return self.run_dir(project_id, run_id) / "turns"

    def tool_calls_dir(self, project_id: str, run_id: str) -> Path:
        return self.run_dir(project_id, run_id) / "tool-calls"

    def tool_call_mutation_lock_path(
        self, project_id: str, run_id: str, digest: str
    ) -> Path:
        return self.tool_calls_dir(project_id, run_id) / ".locks" / f"{digest}.lock"

    def tool_call_execution_lease_path(
        self, project_id: str, run_id: str, digest: str
    ) -> Path:
        return self.tool_calls_dir(project_id, run_id) / ".leases" / f"{digest}.lock"

    def session_dir(self, session_id: str) -> Path:
        self._validate_id(session_id, "session_id")
        return self.sessions_root / session_id

    def session_meta_path(self, session_id: str) -> Path:
        return self.session_dir(session_id) / "meta.json"

    def runtime_state_path(self, session_id: str) -> Path:
        return self.session_dir(session_id) / "runtime.json"

    # -- projects ------------------------------------------------------

    def create_project(
        self,
        project_id: str,
        *,
        name: str | None = None,
        source_root: str | Path | None = None,
        forked_from_project_id: str | None = None,
    ) -> dict[str, Any]:
        path = self.project_record_path(project_id)
        with self._lock(f"project:{project_id}"):
            if path.exists():
                current = self.load_project(project_id)
                if source_root is not None:
                    requested = str(Path(source_root).expanduser().resolve())
                    existing = str(current.get("source_root") or "")
                    if existing and existing != requested:
                        raise RevisionConflictError(
                            f"project is already bound to another folder: {existing}"
                        )
                return current
            now = _now()
            edit_root = self.project_dir(project_id) / "design"
            edit_root.mkdir(parents=True, exist_ok=True)
            record = {
                "schema": "lumeri.production-project",
                "version": 1,
                "project_id": project_id,
                "name": str(name or "").strip() or project_id,
                "source_root": (
                    str(Path(source_root).expanduser().resolve())
                    if source_root is not None
                    else ""
                ),
                "forked_from_project_id": str(forked_from_project_id or ""),
                "edit_root": str(edit_root.resolve()),
                "created_at": now,
                "updated_at": now,
                # Monotonic across any observed project-state change.  Unlike
                # ProjectStore.patch_seq it never moves backwards on undo.
                "revision": 0,
                "state_hash": "",
                "timeline_patch_seq": 0,
                "design_program_digest_scope": _DESIGN_PROGRAM_DIGEST_SCOPE,
                "design_program_source_hash": "",
                "session_ids": [],
                "run_ids": [],
            }
            self._write_json(path, record)
            return dict(record)

    def load_project(self, project_id: str) -> dict[str, Any]:
        path = self.project_record_path(project_id)
        if not path.exists():
            raise ProductionNotFoundError(f"project not found: {project_id}")
        return self._read_json(path)

    def list_projects(self) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        if not self.projects_root.exists():
            return records
        for path in self.projects_root.glob("*/production.json"):
            if path.name.startswith("._"):
                continue
            try:
                record = self._read_json(path)
            except (OSError, json.JSONDecodeError, ProductionStoreError):
                continue
            if record.get("schema") == "lumeri.production-project":
                records.append(record)
        records.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
        return records

    def observe_project_state(
        self,
        project_id: str,
        *,
        state_hash: str,
        timeline_patch_seq: int,
    ) -> dict[str, Any]:
        """Advance the durable project revision iff the canonical state changed."""

        with self._lock(f"project:{project_id}"):
            record = self.load_project(project_id)
            digest = str(state_hash or "")
            if digest and digest != str(record.get("state_hash") or ""):
                record["revision"] = int(record.get("revision") or 0) + 1
                record["state_hash"] = digest
                record["timeline_patch_seq"] = int(timeline_patch_seq)
                record["updated_at"] = _now()
                self._write_json(self.project_record_path(project_id), record)
            return record

    def bind_session(self, project_id: str, session_id: str) -> dict[str, Any]:
        with self._lock(f"project:{project_id}"):
            record = self.load_project(project_id)
            ids = list(record.get("session_ids") or [])
            if session_id not in ids:
                ids.append(session_id)
                record["session_ids"] = ids
                record["updated_at"] = _now()
                self._write_json(self.project_record_path(project_id), record)
            return record

    # -- production runs ----------------------------------------------

    def create_run(
        self,
        project_id: str,
        run_id: str,
        *,
        reality_contract: dict[str, Any] | None = None,
        hard_cap_usd: float = 15.0,
    ) -> dict[str, Any]:
        self.create_project(project_id)
        path = self.run_path(project_id, run_id)
        with self._lock(f"run:{project_id}:{run_id}"):
            if path.exists():
                return self.load_run(project_id, run_id)
            project = self.load_project(project_id)
            contract = normalize_reality_contract(
                reality_contract,
                hard_cap_usd=min(float(hard_cap_usd), MAX_MEDIA_BUDGET_USD),
            )
            budget_contract = contract.get("budget") if isinstance(contract.get("budget"), dict) else {}
            media_policy = (
                contract.get("media_policy")
                if isinstance(contract.get("media_policy"), dict)
                else {}
            )
            cap = float(budget_contract.get("hard_cap_usd", hard_cap_usd))
            if cap <= 0:
                raise ProductionValidationError("hard_cap_usd must be > 0")
            # ProductionMediaBudget is the ONE active paid-media hard gate.
            # The run record below caches only a display summary; it never
            # authorizes provider work.
            from gemia.production_budget import ProductionMediaBudget

            budget_ledger = ProductionMediaBudget(
                self.run_dir(project_id, run_id) / "budget.json",
                run_id=run_id,
                cap_usd=cap,
                warning_usd=float(budget_contract.get("warning_usd", min(12.0, cap))),
                veo_max_calls=int(media_policy.get("generated_video_attempt_cap") or 0),
                veo_max_duration_sec=float(
                    media_policy.get("generated_video_duration_cap_sec") or 0
                ),
            )
            budget_snapshot = budget_ledger.snapshot()
            from gemia.creative_ir import default_creative_ir

            creative_ir = default_creative_ir(contract)
            now = _now()
            run = {
                "schema": "lumeri.production-run",
                "version": 1,
                "run_id": run_id,
                "project_id": project_id,
                "state": "created",
                "revision": 0,
                "project_revision": int(project.get("revision") or 0),
                "created_at": now,
                "updated_at": now,
                "reality_contract": contract,
                "contract_revision": 0,
                "creative_ir_revision": 0,
                "blockers": [],
                "deliverables": [],
                "evidence_ids": [],
                "review": None,
                "turn_ids": [],
                "budget_ledger_path": str(budget_ledger.path),
                "budget": budget_snapshot,
            }
            self.evidence_dir(project_id, run_id).mkdir(parents=True, exist_ok=True)
            self.turns_dir(project_id, run_id).mkdir(parents=True, exist_ok=True)
            self.tool_calls_dir(project_id, run_id).mkdir(parents=True, exist_ok=True)
            self._write_json(self.creative_ir_path(project_id, run_id), creative_ir)
            self._write_json(path, run)
            self._append_run_event(project_id, run_id, "run_created", {"state": "created"})
        with self._lock(f"project:{project_id}"):
            project = self.load_project(project_id)
            ids = list(project.get("run_ids") or [])
            if run_id not in ids:
                ids.append(run_id)
                project["run_ids"] = ids
                project["updated_at"] = _now()
                self._write_json(self.project_record_path(project_id), project)
        return run

    def load_run(self, project_id: str, run_id: str) -> dict[str, Any]:
        path = self.run_path(project_id, run_id)
        if not path.exists():
            raise ProductionNotFoundError(f"production run not found: {project_id}/{run_id}")
        return self._read_json(path)

    def load_creative_ir(self, project_id: str, run_id: str) -> dict[str, Any]:
        """Load the run's editable Creative IR, with a legacy-safe default."""

        from gemia.creative_ir import normalize_creative_ir

        run = self.load_run(project_id, run_id)
        contract = normalize_reality_contract(
            run.get("reality_contract")
            if isinstance(run.get("reality_contract"), dict)
            else None,
            hard_cap_usd=MAX_MEDIA_BUDGET_USD,
        )
        path = self.creative_ir_path(project_id, run_id)
        value = self._read_json(path) if path.exists() else None
        return normalize_creative_ir(value, contract=contract)

    def patch_design_state(
        self,
        project_id: str,
        run_id: str,
        *,
        document: str,
        operation: str,
        path: str,
        value: Any = None,
        expected_revision: int | None = None,
        trace_id: str | None = None,
    ) -> dict[str, Any]:
        """Apply one atomic contract/Creative-IR patch and bump project revision.

        The model edits design facts through small patches; it cannot replace a
        whole production document or change the run's already-created budget
        authority.
        """

        from gemia.creative_ir import (
            apply_ir_patch,
            compact_creative_ir,
            contract_digest,
            normalize_creative_ir,
        )

        kind = str(document or "").strip().lower()
        if kind not in {"reality_contract", "creative_ir"}:
            raise ProductionValidationError(
                "document must be reality_contract or creative_ir"
            )
        with self._lock(f"run:{project_id}:{run_id}"), self._lock(
            f"project:{project_id}"
        ):
            run = self.load_run(project_id, run_id)
            project = self.load_project(project_id)
            if kind == "reality_contract":
                current_revision = int(run.get("contract_revision") or 0)
                current = normalize_reality_contract(
                    run.get("reality_contract")
                    if isinstance(run.get("reality_contract"), dict)
                    else None,
                    hard_cap_usd=MAX_MEDIA_BUDGET_USD,
                )
            else:
                current = self.load_creative_ir(project_id, run_id)
                current_revision = int(current.get("revision") or 0)
            if (
                expected_revision is not None
                and int(expected_revision) != current_revision
            ):
                raise RevisionConflictError(
                    f"{kind} revision mismatch: expected {expected_revision}, current {current_revision}"
                )
            if kind == "creative_ir" and str(path) == "/active_revision_scope":
                clearing_scope = str(operation).strip().lower() == "remove" or (
                    str(operation).strip().lower() == "set" and value is None
                )
                active_scope = current.get("active_revision_scope")
                if clearing_scope and isinstance(active_scope, dict):
                    timeline_changed = int(project.get("timeline_patch_seq") or 0) > int(
                        active_scope.get("base_timeline_patch_seq") or 0
                    )
                    design_changed = str(project.get("design_program_hash") or "") != str(
                        active_scope.get("base_design_program_hash") or ""
                    )
                    if not timeline_changed and not design_changed:
                        raise ProductionValidationError(
                            "active_revision_scope can be cleared only after a timeline or "
                            "persistent design-program change"
                        )

            patched = apply_ir_patch(
                current,
                operation=operation,
                path=path,
                value=value,
            )
            next_revision = current_revision + 1
            if kind == "reality_contract":
                budget_snapshot = self.media_budget(project_id, run_id).snapshot()
                ledger_cap = float(budget_snapshot["cap_usd"])
                ledger_warning = float(budget_snapshot["warning_usd"])
                patched = normalize_reality_contract(
                    patched,
                    hard_cap_usd=ledger_cap,
                )
                patched_budget = patched.get("budget") or {}
                if (
                    float(patched_budget.get("hard_cap_usd") or 0) != ledger_cap
                    or float(patched_budget.get("warning_usd") or 0) != ledger_warning
                ):
                    raise ProductionBudgetError(
                        "RealityContract cannot change an existing ProductionRun budget authority"
                    )
                run["reality_contract"] = patched
                run["contract_revision"] = next_revision
                # Contract changes rebind the IR without overwriting its human
                # design decisions.
                creative_ir = normalize_creative_ir(
                    self.load_creative_ir(project_id, run_id),
                    contract=patched,
                )
                creative_ir_revision = int(creative_ir.get("revision") or 0) + 1
                creative_ir["revision"] = creative_ir_revision
                creative_ir["updated_at"] = _now()
                creative_ir["contract_digest"] = contract_digest(patched)
                deliverable = patched.get("deliverable") or {}
                creative_ir["canvas"] = {
                    **dict(creative_ir.get("canvas") or {}),
                    "duration_sec": deliverable.get("duration_sec"),
                    "width": deliverable.get("width"),
                    "height": deliverable.get("height"),
                    "fps": deliverable.get("fps"),
                }
                intent = dict(creative_ir.get("intent") or {})
                intent["brief"] = str(patched.get("brief") or "")
                creative_ir["intent"] = intent
                run["creative_ir_revision"] = creative_ir_revision
                self._write_json(
                    self.creative_ir_path(project_id, run_id), creative_ir
                )
                response_document: dict[str, Any] = patched
            else:
                contract = normalize_reality_contract(
                    run.get("reality_contract")
                    if isinstance(run.get("reality_contract"), dict)
                    else None,
                    hard_cap_usd=MAX_MEDIA_BUDGET_USD,
                )
                patched["revision"] = next_revision
                patched["updated_at"] = _now()
                patched = normalize_creative_ir(patched, contract=contract)
                self._write_json(self.creative_ir_path(project_id, run_id), patched)
                run["creative_ir_revision"] = next_revision
                response_document = compact_creative_ir(patched)

            project_revision = int(project.get("revision") or 0) + 1
            project["revision"] = project_revision
            project["updated_at"] = _now()
            if kind == "creative_ir":
                encoded = json.dumps(
                    patched,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
                project["creative_ir_hash"] = hashlib.sha256(encoded).hexdigest()
            else:
                project["reality_contract_hash"] = contract_digest(patched)
            self._write_json(self.project_record_path(project_id), project)

            run["project_revision"] = project_revision
            run["deliverables"] = []
            if str(run.get("state") or "") in {"ready_for_review", "accepted"}:
                run["state"] = "revising"
            if isinstance(run.get("review"), dict):
                run["review"] = {
                    **run["review"],
                    "invalidated_at": _now(),
                    "invalidated_by_project_revision": project_revision,
                }
            run["revision"] = int(run.get("revision") or 0) + 1
            run["updated_at"] = _now()
            self._write_json(self.run_path(project_id, run_id), run)
            event = {
                "document": kind,
                "operation": str(operation),
                "path": str(path),
                "document_revision": next_revision,
                "project_revision": project_revision,
            }
            self._append_run_event(
                project_id,
                run_id,
                "design_state_patched",
                event,
                trace_id=trace_id,
            )
            self.creative_ir_events_path(project_id, run_id).parent.mkdir(
                parents=True, exist_ok=True
            )
            with self.creative_ir_events_path(project_id, run_id).open(
                "a", encoding="utf-8"
            ) as handle:
                handle.write(
                    json.dumps({**event, "ts": _now()}, ensure_ascii=False) + "\n"
                )
            return {
                "document": kind,
                "revision": next_revision,
                "project_revision": project_revision,
                "production_revision": int(run["revision"]),
                "production_state": str(run.get("state") or ""),
                "value": response_document,
            }

    @staticmethod
    def _design_program_digest(root: Path) -> str:
        """Hash the persistent algorithm sources by relative path and bytes."""

        digest = hashlib.sha256()
        if not root.exists():
            return digest.hexdigest()
        for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
            relative_path = path.relative_to(root)
            # Tool environments, caches, and rendered media are runtime
            # products, not creator-authored algorithm sources. Including
            # them made harmless restart/import activity advance the project
            # revision and reject the next user turn as stale.
            if any(
                part in {
                    "toolchain",
                    "renders",
                    "node_modules",
                    ".venv",
                    "venv",
                    "__pycache__",
                    ".cache",
                }
                for part in relative_path.parts
            ):
                continue
            if not path.is_file() or path.is_symlink():
                continue
            relative = relative_path.as_posix().encode("utf-8")
            digest.update(len(relative).to_bytes(8, "big"))
            digest.update(relative)
            try:
                with path.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(chunk)
            except OSError as exc:
                raise ProductionValidationError(
                    f"cannot hash design program file: {path}"
                ) from exc
        return digest.hexdigest()

    def observe_design_program(
        self,
        project_id: str,
        run_id: str,
        *,
        design_root: str | Path | None = None,
        trace_id: str | None = None,
    ) -> dict[str, Any]:
        """Commit a changed project algorithm to the canonical revision graph."""

        root = (
            Path(design_root).expanduser().resolve()
            if design_root is not None
            else (self.project_dir(project_id) / "design").resolve()
        )
        expected_root = (self.project_dir(project_id) / "design").resolve()
        if root != expected_root:
            raise ProductionValidationError(
                "design program root must be the persistent project design directory"
            )
        program_hash = self._design_program_digest(root)
        with self._lock(f"run:{project_id}:{run_id}"), self._lock(
            f"project:{project_id}"
        ):
            project = self.load_project(project_id)
            run = self.load_run(project_id, run_id)
            previous_hash = str(project.get("design_program_hash") or "")
            digest_scope = str(project.get("design_program_digest_scope") or "")
            previous_source_hash = str(
                project.get("design_program_source_hash") or ""
            )
            if (
                previous_hash
                and digest_scope != _DESIGN_PROGRAM_DIGEST_SCOPE
            ):
                # Existing projects used a whole-tree digest that included
                # tool environments, caches, and renders. Establish the new
                # source-only baseline without moving the canonical revision
                # or changing the old opaque hash used by active review
                # scopes. A later real source edit updates both hashes.
                project["design_program_digest_scope"] = (
                    _DESIGN_PROGRAM_DIGEST_SCOPE
                )
                project["design_program_source_hash"] = program_hash
                project["updated_at"] = _now()
                self._write_json(self.project_record_path(project_id), project)
                return {
                    "changed": False,
                    "digest_scope_migrated": True,
                    "design_program_hash": previous_hash,
                    "project_revision": int(project.get("revision") or 0),
                    "production_revision": int(run.get("revision") or 0),
                }
            comparison_hash = previous_source_hash or previous_hash
            if program_hash == comparison_hash:
                return {
                    "changed": False,
                    "design_program_hash": previous_hash or program_hash,
                    "project_revision": int(project.get("revision") or 0),
                    "production_revision": int(run.get("revision") or 0),
                }
            project_revision = int(project.get("revision") or 0) + 1
            project["revision"] = project_revision
            project["design_program_hash"] = program_hash
            project["design_program_digest_scope"] = (
                _DESIGN_PROGRAM_DIGEST_SCOPE
            )
            project["design_program_source_hash"] = program_hash
            project["updated_at"] = _now()
            self._write_json(self.project_record_path(project_id), project)

            run["project_revision"] = project_revision
            run["deliverables"] = []
            if str(run.get("state") or "") in {"ready_for_review", "accepted"}:
                run["state"] = "revising"
            if isinstance(run.get("review"), dict):
                run["review"] = {
                    **run["review"],
                    "invalidated_at": _now(),
                    "invalidated_by_project_revision": project_revision,
                }
            run["revision"] = int(run.get("revision") or 0) + 1
            run["updated_at"] = _now()
            self._write_json(self.run_path(project_id, run_id), run)
            self._append_run_event(
                project_id,
                run_id,
                "design_program_committed",
                {
                    "design_program_hash": program_hash,
                    "project_revision": project_revision,
                    "production_revision": int(run["revision"]),
                },
                trace_id=trace_id,
            )
            return {
                "changed": True,
                "design_program_hash": program_hash,
                "project_revision": project_revision,
                "production_revision": int(run["revision"]),
                "production_state": str(run.get("state") or ""),
            }

    def media_policy_decision(
        self, project_id: str, run_id: str, tool_name: str
    ) -> dict[str, Any]:
        """Return the durable host decision for a generation capability."""

        from gemia.reality_contract import media_policy_decision

        run = self.load_run(project_id, run_id)
        contract = normalize_reality_contract(
            run.get("reality_contract")
            if isinstance(run.get("reality_contract"), dict)
            else None,
            hard_cap_usd=MAX_MEDIA_BUDGET_USD,
        )
        return media_policy_decision(
            contract,
            self.load_creative_ir(project_id, run_id),
            self.media_budget(project_id, run_id).snapshot(),
            tool_name,
        )

    def transition_run(
        self,
        project_id: str,
        run_id: str,
        state: str,
        *,
        expected_revision: int | None = None,
        blocker: dict[str, Any] | None = None,
        trace_id: str | None = None,
    ) -> dict[str, Any]:
        target = str(state or "")
        if target not in PRODUCTION_STATES:
            raise ProductionValidationError(f"unknown production state: {target!r}")
        with self._lock(f"run:{project_id}:{run_id}"):
            run = self.load_run(project_id, run_id)
            current_revision = int(run.get("revision") or 0)
            if expected_revision is not None and int(expected_revision) != current_revision:
                raise RevisionConflictError(
                    f"run revision mismatch: expected {expected_revision}, current {current_revision}"
                )
            current = str(run.get("state") or "created")
            if target != current and target not in _STATE_TRANSITIONS.get(current, frozenset()):
                raise StateTransitionError(f"invalid production transition: {current} -> {target}")
            if target == "ready_for_review" and not self._has_current_machine_evidence(
                project_id, run_id, run=run
            ):
                raise StateTransitionError(
                    "ready_for_review requires passed machine evidence for the current project revision"
                )
            if target != current:
                run["state"] = target
                run["revision"] = current_revision + 1
                run["updated_at"] = _now()
            if target not in {"ready_for_review", "accepted"} and run.get(
                "deliverables"
            ):
                # A review master is an active candidate, not historical
                # evidence.  Returning to production (or failing/cancelling)
                # invalidates it immediately; the immutable acceptance
                # evidence remains in evidence_ids for audit.
                run["deliverables"] = []
                if target == current:
                    run["revision"] = current_revision + 1
                    run["updated_at"] = _now()
            if blocker is not None:
                blockers = list(run.get("blockers") or [])
                blockers.append(dict(blocker))
                run["blockers"] = blockers
                if target == current:
                    run["revision"] = current_revision + 1
                    run["updated_at"] = _now()
            self._write_json(self.run_path(project_id, run_id), run)
            self._append_run_event(
                project_id,
                run_id,
                "production_state_changed",
                {"from": current, "to": target, "revision": run["revision"]},
                trace_id=trace_id,
            )
            return run

    def sync_run_project_revision(
        self, project_id: str, run_id: str, project_revision: int
    ) -> dict[str, Any]:
        with self._lock(f"run:{project_id}:{run_id}"):
            run = self.load_run(project_id, run_id)
            revision = int(project_revision)
            if revision > int(run.get("project_revision") or 0):
                run["project_revision"] = revision
                run["deliverables"] = []
                if str(run.get("state") or "") in {"ready_for_review", "accepted"}:
                    run["state"] = "revising"
                    previous_review = run.get("review")
                    if isinstance(previous_review, dict):
                        run["review"] = {
                            **previous_review,
                            "invalidated_at": _now(),
                            "invalidated_by_project_revision": revision,
                        }
                run["revision"] = int(run.get("revision") or 0) + 1
                run["updated_at"] = _now()
                self._write_json(self.run_path(project_id, run_id), run)
            return run

    def record_deliverable(
        self,
        project_id: str,
        run_id: str,
        *,
        asset_id: str,
        project_revision: int,
        sha256: str,
        graph_hash: str,
        render_id: str,
        render_semantics_version: int,
        evidence_id: str,
        duration_sec: float | None = None,
        trace_id: str | None = None,
    ) -> dict[str, Any]:
        """Commit the one current formal export without storing a host path.

        The immutable acceptance Evidence and AssetRegistry remain the audit
        sources for paths and full receipts.  ProductionRun stores only the
        stable public identity needed to resume human review.  Repeating the
        exact commit is a no-op; a conflicting candidate at the same revision
        fails closed instead of silently changing what the reviewer sees.
        """

        self._validate_id(asset_id, "asset_id")
        self._validate_id(evidence_id, "evidence_id")
        revision = int(project_revision)
        digest = str(sha256 or "").strip().lower()
        graph = str(graph_hash or "").strip().lower()
        render = str(render_id or "").strip()
        semantics = int(render_semantics_version)
        if _SHA256_RE.fullmatch(digest) is None:
            raise ProductionValidationError("deliverable sha256 must be 64 lowercase hex")
        if _SHA256_RE.fullmatch(graph) is None:
            raise ProductionValidationError("deliverable graph_hash must be 64 lowercase hex")
        if not render:
            raise ProductionValidationError("deliverable render_id must be non-empty")
        if semantics < 1:
            raise ProductionValidationError(
                "deliverable render_semantics_version must be positive"
            )
        duration: float | None = None
        if duration_sec is not None:
            duration = float(duration_sec)
            if duration <= 0:
                raise ProductionValidationError("deliverable duration_sec must be > 0")

        lock_path = self.run_dir(project_id, run_id) / ".deliverable.lock"
        with self._cross_process_lock(lock_path), self._lock(
            f"run:{project_id}:{run_id}"
        ):
            project = self.load_project(project_id)
            run = self.load_run(project_id, run_id)
            if revision != int(project.get("revision") or 0) or revision != int(
                run.get("project_revision") or 0
            ):
                raise RevisionConflictError(
                    "deliverable is not bound to the current project revision"
                )
            if str(run.get("state") or "") not in {
                "verifying",
                "ready_for_review",
                "accepted",
            }:
                raise StateTransitionError(
                    "deliverable can only be committed while verifying or reviewable"
                )
            evidence_path = self.evidence_dir(project_id, run_id) / f"{evidence_id}.json"
            if not evidence_path.exists():
                raise ProductionValidationError(
                    "deliverable requires persisted acceptance evidence"
                )
            evidence = self._read_json(evidence_path)
            payload = evidence.get("payload")
            report = payload.get("acceptance_report") if isinstance(payload, dict) else None
            if (
                str(evidence.get("kind") or "") != "production_acceptance"
                or int(evidence.get("project_revision") or -1) != revision
                or not isinstance(payload, dict)
                or str(payload.get("export_asset_id") or "") != asset_id
                or not isinstance(report, dict)
                or report.get("ready_for_review") is not True
                or int(report.get("project_revision") or -1) != revision
                or str(report.get("graph_hash") or "").lower() != graph
                or str(report.get("render_id") or "") != render
            ):
                raise ProductionValidationError(
                    "deliverable does not match its current passed acceptance evidence"
                )

            deliverable: dict[str, Any] = {
                "role": "review_master",
                "asset_id": asset_id,
                "kind": "video",
                "project_revision": revision,
                "sha256": digest,
                "graph_hash": graph,
                "render_id": render,
                "render_semantics_version": semantics,
                "evidence_id": evidence_id,
            }
            if duration is not None:
                deliverable["duration_sec"] = round(duration, 6)
            # Keep the ProductionStore boundary fail-closed.  verify_delivery
            # already checked these bytes, but a caller/backfill must not be
            # able to publish an arbitrary asset id or a stale receipt merely
            # by presenting well-shaped Evidence JSON.
            self._validate_delivery_candidate(
                project_id,
                deliverable,
                verify_bytes=True,
            )
            current = run.get("deliverables")
            if current == [deliverable]:
                return {
                    "deliverable": dict(deliverable),
                    "duplicate": True,
                    "production_state": str(run.get("state") or ""),
                    "project_revision": revision,
                    "production_revision": int(run.get("revision") or 0),
                }
            if current:
                raise IdempotencyConflictError(
                    "current production revision already has a different deliverable"
                )
            run["deliverables"] = [deliverable]
            run["revision"] = int(run.get("revision") or 0) + 1
            run["updated_at"] = _now()
            self._write_json(self.run_path(project_id, run_id), run)
            self._append_run_event(
                project_id,
                run_id,
                "delivery_recorded",
                {
                    "asset_id": asset_id,
                    "project_revision": revision,
                    "production_revision": int(run["revision"]),
                    "sha256": digest,
                    "graph_hash": graph,
                    "evidence_id": evidence_id,
                },
                trace_id=trace_id,
            )
            return {
                "deliverable": dict(deliverable),
                "duplicate": False,
                "production_state": str(run.get("state") or ""),
                "project_revision": revision,
                "production_revision": int(run["revision"]),
            }

    def public_delivery(
        self,
        project_id: str,
        run_id: str,
        *,
        run: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Return the one current review master without leaking host paths.

        A run may retain immutable historical Evidence, but only a deliverable
        bound to the current project revision and matching its AssetRecord and
        RenderReceipt is exposed for human review.  Byte hashing is repeated
        at approval time rather than on every UI poll.
        """

        run = run or self.load_run(project_id, run_id)
        if str(run.get("state") or "") not in {"ready_for_review", "accepted"}:
            return None
        project_revision = int(self.load_project(project_id).get("revision") or 0)
        if int(run.get("project_revision") or -1) != project_revision:
            return None
        deliverables = run.get("deliverables")
        if not isinstance(deliverables, list) or len(deliverables) != 1:
            return None
        candidate = deliverables[0]
        if not isinstance(candidate, dict):
            return None
        try:
            self._validate_delivery_candidate(
                project_id,
                candidate,
                verify_bytes=False,
            )
        except ProductionStoreError:
            return None
        asset_id = str(candidate.get("asset_id") or "")
        allowed = {
            key: candidate[key]
            for key in (
                "role",
                "asset_id",
                "kind",
                "project_revision",
                "sha256",
                "graph_hash",
                "render_id",
                "render_semantics_version",
                "evidence_id",
                "duration_sec",
            )
            if key in candidate
        }
        allowed["url"] = (
            f"/projects/{project_id}/artifacts/{asset_id}"
        )
        return {
            "project_revision": project_revision,
            "review_master": allowed,
        }

    def _validate_delivery_candidate(
        self,
        project_id: str,
        deliverable: dict[str, Any],
        *,
        verify_bytes: bool,
    ) -> None:
        """Validate a persisted review-master identity against real media."""

        if str(deliverable.get("role") or "") != "review_master":
            raise ProductionValidationError("delivery must identify one review_master")
        asset_id = str(deliverable.get("asset_id") or "")
        self._validate_id(asset_id, "asset_id")
        digest = str(deliverable.get("sha256") or "").strip().lower()
        graph_hash = str(deliverable.get("graph_hash") or "").strip().lower()
        render_id = str(deliverable.get("render_id") or "").strip()
        revision = int(deliverable.get("project_revision") or -1)
        semantics = int(deliverable.get("render_semantics_version") or -1)
        if _SHA256_RE.fullmatch(digest) is None or _SHA256_RE.fullmatch(
            graph_hash
        ) is None:
            raise ProductionValidationError("delivery hashes are malformed")

        from gemia.tools._context import AssetRegistry

        try:
            record = AssetRegistry.load(self.asset_registry_path(project_id)).get(
                asset_id
            )
        except (KeyError, OSError, ValueError) as exc:
            raise ProductionValidationError(
                "delivery AssetRecord is missing or unreadable"
            ) from exc
        receipt = record.source.get("render_receipt")
        if record.kind != "video" or not isinstance(receipt, dict):
            raise ProductionValidationError(
                "delivery AssetRecord is not a receipt-backed video"
            )
        try:
            record_path = record.path.expanduser().resolve(strict=True)
            receipt_path = Path(str(receipt.get("output_path") or "")).expanduser().resolve(
                strict=True
            )
        except (OSError, RuntimeError) as exc:
            raise ProductionValidationError("delivery media file is unavailable") from exc
        if not record_path.is_file() or receipt_path != record_path:
            raise ProductionValidationError(
                "delivery AssetRecord and RenderReceipt point to different files"
            )
        if (
            revision != int(self.load_project(project_id).get("revision") or 0)
            or str(record.sha256 or "").lower() != digest
            or str(receipt.get("output_sha256") or "").lower() != digest
            or int(receipt.get("project_revision") or -1) != revision
            or str(receipt.get("graph_hash") or "").lower() != graph_hash
            or str(receipt.get("render_id") or "") != render_id
            or int(receipt.get("render_semantics_version") or -1) != semantics
            or str(receipt.get("machine_status") or "") != "passed"
            or receipt.get("machine_blockers") != []
        ):
            raise ProductionValidationError(
                "delivery no longer matches its AssetRecord and RenderReceipt"
            )
        if verify_bytes:
            hasher = hashlib.sha256()
            try:
                with record_path.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        hasher.update(chunk)
            except OSError as exc:
                raise ProductionValidationError(
                    "delivery media cannot be read for integrity verification"
                ) from exc
            if hasher.hexdigest() != digest:
                raise ProductionValidationError(
                    "delivery bytes changed after machine verification"
                )

    def add_evidence(
        self,
        project_id: str,
        run_id: str,
        *,
        kind: str,
        payload: dict[str, Any],
        project_revision: int,
        trace_id: str | None = None,
        evidence_id: str | None = None,
    ) -> dict[str, Any]:
        evidence_id = evidence_id or f"ev-{uuid.uuid4().hex[:12]}"
        self._validate_id(evidence_id, "evidence_id")
        normalized_payload = json.loads(
            json.dumps(dict(payload or {}), ensure_ascii=False, default=str)
        )
        canonical = {
            "project_id": project_id,
            "run_id": run_id,
            "project_revision": int(project_revision),
            "kind": str(kind or "observation"),
            "payload": normalized_payload,
            "trace_id": str(trace_id or ""),
        }
        evidence_lock = (
            self.evidence_dir(project_id, run_id)
            / ".locks"
            / f"{evidence_id}.lock"
        )
        with self._cross_process_lock(evidence_lock):
            run = self.load_run(project_id, run_id)
            path = self.evidence_dir(project_id, run_id) / f"{evidence_id}.json"
            if path.exists():
                existing = self._read_json(path)
                existing_canonical = {
                    field: existing.get(field)
                    for field in (
                        "project_id",
                        "run_id",
                        "project_revision",
                        "kind",
                        "payload",
                        "trace_id",
                    )
                }
                if existing_canonical == canonical:
                    return existing
                raise IdempotencyConflictError(
                    "evidence_id reused with different canonical content: "
                    f"{evidence_id}"
                )
            evidence = {
                "schema": "lumeri.production-evidence",
                "version": 1,
                "evidence_id": evidence_id,
                **canonical,
                "created_at": _now(),
            }
            self._write_json(path, evidence)
            ids = list(run.get("evidence_ids") or [])
            ids.append(evidence_id)
            run["evidence_ids"] = ids
            run["project_revision"] = max(
                int(run.get("project_revision") or 0), int(project_revision)
            )
            run["revision"] = int(run.get("revision") or 0) + 1
            run["updated_at"] = _now()
            self._write_json(self.run_path(project_id, run_id), run)
            self._append_run_event(
                project_id,
                run_id,
                "evidence_recorded",
                {"evidence_id": evidence_id, "kind": evidence["kind"]},
                trace_id=trace_id,
            )
            return evidence

    def review_run(
        self,
        project_id: str,
        run_id: str,
        *,
        action: str,
        note: str = "",
        start_sec: float | None = None,
        end_sec: float | None = None,
        expected_project_revision: int | None = None,
        reviewer_account_id: str | None = None,
        watched_full_video: bool | None = None,
        creative_checks: dict[str, bool] | None = None,
    ) -> dict[str, Any]:
        action = str(action or "").strip()
        if action not in {"approve", "request_changes"}:
            raise ProductionValidationError("review action must be approve or request_changes")
        if (start_sec is None) != (end_sec is None):
            raise ProductionValidationError("start_sec and end_sec must be provided together")
        if start_sec is not None:
            start_sec, end_sec = float(start_sec), float(end_sec)
            if start_sec < 0 or end_sec <= start_sec:
                raise ProductionValidationError("review range must satisfy 0 <= start_sec < end_sec")
        if action == "request_changes" and not str(note or "").strip():
            raise ProductionValidationError("request_changes requires a non-empty note")

        with self._lock(f"run:{project_id}:{run_id}"):
            project = self.load_project(project_id)
            current_project_revision = int(project.get("revision") or 0)
            if (
                expected_project_revision is not None
                and int(expected_project_revision) != current_project_revision
            ):
                raise RevisionConflictError(
                    "project revision mismatch: "
                    f"expected {expected_project_revision}, current {current_project_revision}"
                )
            run = self.load_run(project_id, run_id)
            contract = normalize_reality_contract(
                run.get("reality_contract")
                if isinstance(run.get("reality_contract"), dict)
                else None,
                hard_cap_usd=MAX_MEDIA_BUDGET_USD,
            )
            acceptance = contract.get("acceptance") or {}
            creative_dimensions = tuple(
                str(value)
                for value in (acceptance.get("creative_dimensions") or [])
                if str(value)
            ) or _HUMAN_CREATIVE_DIMENSIONS
            current_state = str(run.get("state") or "created")
            if action == "approve" and current_state != "ready_for_review":
                raise StateTransitionError(
                    f"run must be ready_for_review before approval, got {current_state}"
                )
            if action == "approve" and not self._has_current_machine_evidence(
                project_id, run_id, run=run
            ):
                raise StateTransitionError(
                    "approval requires passed machine evidence for the current project revision"
                )
            if action == "approve":
                deliverables = run.get("deliverables")
                if not isinstance(deliverables, list) or len(deliverables) != 1:
                    raise StateTransitionError(
                        "approval requires one current persisted review master"
                    )
                candidate = deliverables[0]
                if not isinstance(candidate, dict):
                    raise StateTransitionError(
                        "approval requires one current persisted review master"
                    )
                self._validate_delivery_candidate(
                    project_id,
                    candidate,
                    verify_bytes=True,
                )
            normalized_creative_checks: dict[str, bool] | None = None
            if action == "approve":
                if watched_full_video is not True:
                    raise ProductionValidationError(
                        "approval requires an explicit full-video watch confirmation"
                    )
                if not isinstance(creative_checks, dict):
                    raise ProductionValidationError(
                        "approval requires explicit story, pacing, visual, sound, and "
                        "publishable checks"
                    )
                missing = [name for name in creative_dimensions if name not in creative_checks]
                non_boolean = [
                    name
                    for name in creative_dimensions
                    if name in creative_checks and type(creative_checks[name]) is not bool
                ]
                failed = [
                    name
                    for name in creative_dimensions
                    if creative_checks.get(name) is False
                ]
                if missing or non_boolean:
                    raise ProductionValidationError(
                        "approval requires explicit boolean creative checks; "
                        f"missing={missing}, non_boolean={non_boolean}"
                    )
                if failed:
                    raise ProductionValidationError(
                        "approval cannot accept failed creative checks; "
                        f"failed={failed}; use request_changes"
                    )
                normalized_creative_checks = {
                    name: creative_checks[name] for name in creative_dimensions
                }
            target = "accepted" if action == "approve" else "revising"
            if target != current_state and target not in _STATE_TRANSITIONS.get(
                current_state, frozenset()
            ):
                raise StateTransitionError(
                    f"invalid review transition: {current_state} -> {target}"
                )
            review_id = f"review-{uuid.uuid4().hex[:12]}"
            review = {
                "review_id": review_id,
                "action": action,
                "note": str(note or ""),
                "start_sec": start_sec,
                "end_sec": end_sec,
                "reviewer_account_id": str(reviewer_account_id or ""),
                "project_revision": current_project_revision,
                "created_at": _now(),
            }
            if action == "approve":
                review["watched_full_video"] = True
                review["creative_checks"] = normalized_creative_checks
            evidence_id = f"ev-{uuid.uuid4().hex[:12]}"
            evidence = {
                "schema": "lumeri.production-evidence",
                "version": 1,
                "evidence_id": evidence_id,
                "project_id": project_id,
                "run_id": run_id,
                "project_revision": current_project_revision,
                "kind": "human_review",
                "payload": review,
                "trace_id": review_id,
                "created_at": review["created_at"],
            }
            self._write_json(
                self.evidence_dir(project_id, run_id) / f"{evidence_id}.json", evidence
            )
            run["state"] = target
            run["review"] = review
            if action == "request_changes":
                # The previous master remains in immutable Evidence but is no
                # longer an active review candidate once a real revision was
                # requested.
                run["deliverables"] = []
                from gemia.creative_ir import (
                    compact_creative_ir,
                    contract_digest,
                    normalize_creative_ir,
                )

                contract = normalize_reality_contract(
                    run.get("reality_contract")
                    if isinstance(run.get("reality_contract"), dict)
                    else None,
                    hard_cap_usd=MAX_MEDIA_BUDGET_USD,
                )
                creative_ir = normalize_creative_ir(
                    self.load_creative_ir(project_id, run_id),
                    contract=contract,
                )
                creative_ir_revision = int(creative_ir.get("revision") or 0) + 1
                creative_ir["revision"] = creative_ir_revision
                creative_ir["updated_at"] = _now()
                creative_ir["active_revision_scope"] = {
                    "review_id": review_id,
                    "base_project_revision": current_project_revision,
                    "base_timeline_patch_seq": int(
                        project.get("timeline_patch_seq") or 0
                    ),
                    "base_design_program_hash": str(
                        project.get("design_program_hash") or ""
                    ),
                    "start_sec": start_sec,
                    "end_sec": end_sec,
                    "whole_project": start_sec is None,
                    "request": str(note or "").strip(),
                }
                self._write_json(
                    self.creative_ir_path(project_id, run_id), creative_ir
                )
                next_project_revision = current_project_revision + 1
                project["revision"] = next_project_revision
                project["updated_at"] = _now()
                project["creative_ir_hash"] = hashlib.sha256(
                    json.dumps(
                        creative_ir,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest()
                project["reality_contract_hash"] = contract_digest(contract)
                self._write_json(self.project_record_path(project_id), project)
                run["creative_ir_revision"] = creative_ir_revision
                run["project_revision"] = next_project_revision
                run["review"] = {
                    **review,
                    "resulting_project_revision": next_project_revision,
                    "active_revision_scope": compact_creative_ir(creative_ir)[
                        "active_revision_scope"
                    ],
                }
            run["evidence_ids"] = [*(run.get("evidence_ids") or []), evidence_id]
            if action == "approve":
                run["project_revision"] = current_project_revision
            run["revision"] = int(run.get("revision") or 0) + 1
            run["updated_at"] = _now()
            self._write_json(self.run_path(project_id, run_id), run)
            self._append_run_event(
                project_id,
                run_id,
                "acceptance_updated",
                {"action": action, "state": target, "review_id": review_id},
                trace_id=review_id,
            )
            return run

    def _current_machine_evidence(
        self,
        project_id: str,
        run_id: str,
        *,
        run: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Return the complete formal acceptance Evidence for this revision."""

        run = run or self.load_run(project_id, run_id)
        contract = normalize_reality_contract(
            run.get("reality_contract")
            if isinstance(run.get("reality_contract"), dict)
            else None,
            hard_cap_usd=MAX_MEDIA_BUDGET_USD,
        )
        acceptance = contract.get("acceptance") or {}
        required_codes = required_acceptance_check_codes(contract)
        creative_dimensions = tuple(
            str(value)
            for value in (acceptance.get("creative_dimensions") or [])
            if str(value)
        ) or _HUMAN_CREATIVE_DIMENSIONS
        inspection_min = int(acceptance.get("review_sample_frames_min") or 1)
        agent_review_codes = frozenset(
            f"review_{name}"
            for name in (acceptance.get("agent_review_checks") or [])
        )
        project_revision = int(self.load_project(project_id).get("revision") or 0)
        if int(run.get("project_revision") or 0) != project_revision:
            return False
        for evidence_id in reversed(list(run.get("evidence_ids") or [])):
            if not isinstance(evidence_id, str) or _ID_RE.fullmatch(evidence_id) is None:
                continue
            path = self.evidence_dir(project_id, run_id) / f"{evidence_id}.json"
            if not path.exists():
                continue
            try:
                evidence = self._read_json(path)
            except (OSError, json.JSONDecodeError, ProductionStoreError):
                continue
            if (
                str(evidence.get("schema") or "") != "lumeri.production-evidence"
                or type(evidence.get("version")) is not int
                or evidence.get("version") != 1
                or str(evidence.get("evidence_id") or "") != evidence_id
                or str(evidence.get("project_id") or "") != project_id
                or str(evidence.get("run_id") or "") != run_id
                or str(evidence.get("kind") or "") != "production_acceptance"
            ):
                continue
            if (
                type(evidence.get("project_revision")) is not int
                or evidence.get("project_revision") != project_revision
            ):
                continue
            payload = evidence.get("payload")
            if not isinstance(payload, dict):
                continue
            report = payload.get("acceptance_report")
            if not isinstance(report, dict):
                continue
            if (
                str(report.get("schema") or "") != "lumeri.production-acceptance"
                or type(report.get("version")) is not int
                or report.get("version") != 1
            ):
                continue
            if (
                type(report.get("project_revision")) is not int
                or report.get("project_revision") != project_revision
            ):
                continue
            checks = report.get("checks")
            if not isinstance(checks, list) or not checks:
                continue
            if report.get("ready_for_review") is not True:
                continue
            if report.get("blockers") != []:
                continue
            if report.get("human_review_required") is not True:
                continue
            dimensions = report.get("human_review_dimensions")
            if (
                not isinstance(dimensions, list)
                or any(not isinstance(value, str) for value in dimensions)
                or set(dimensions) != set(creative_dimensions)
                or len(dimensions) != len(creative_dimensions)
            ):
                continue
            if not str(report.get("graph_hash") or "") or not str(
                report.get("render_id") or ""
            ):
                continue
            by_code: dict[str, dict[str, Any]] = {}
            malformed = False
            for check in checks:
                if not isinstance(check, dict):
                    malformed = True
                    break
                code = str(check.get("code") or "")
                if (
                    not code
                    or code in by_code
                    or check.get("ok") is not True
                    or "actual" not in check
                    or "expected" not in check
                ):
                    malformed = True
                    break
                by_code[code] = check
            if malformed:
                continue
            if not required_codes.issubset(by_code):
                continue

            inspection_ids = payload.get("inspection_asset_ids")
            if not isinstance(inspection_ids, list):
                continue
            inspection_id_set = {
                value for value in inspection_ids if isinstance(value, str) and value
            }
            if len(inspection_id_set) < inspection_min or len(inspection_id_set) != len(
                inspection_ids
            ):
                continue
            if not str(payload.get("export_asset_id") or "") or not str(
                payload.get("preview_asset_id") or ""
            ):
                continue
            for code in agent_review_codes:
                actual = by_code[code].get("actual")
                if not isinstance(actual, dict):
                    malformed = True
                    break
                review_frame_ids = actual.get("inspection_asset_ids")
                review_frame_id_set = (
                    {
                        value
                        for value in review_frame_ids
                        if isinstance(value, str) and value
                    }
                    if isinstance(review_frame_ids, list)
                    else set()
                )
                if (
                    str(actual.get("status") or "") != "passed"
                    or not str(actual.get("note") or "").strip()
                    or not isinstance(review_frame_ids, list)
                    or review_frame_id_set != inspection_id_set
                    or len(review_frame_id_set) != len(review_frame_ids)
                ):
                    malformed = True
                    break
            if malformed:
                continue
            return evidence
        return None

    def _has_current_machine_evidence(
        self,
        project_id: str,
        run_id: str,
        *,
        run: dict[str, Any] | None = None,
    ) -> bool:
        return self._current_machine_evidence(
            project_id, run_id, run=run
        ) is not None

    # -- run-level budget ---------------------------------------------

    def media_budget(self, project_id: str, run_id: str):
        """Open the canonical integer-micro-USD ledger for this run."""

        from gemia.production_budget import ProductionMediaBudget

        run = self.load_run(project_id, run_id)
        path = run.get("budget_ledger_path") or (
            self.run_dir(project_id, run_id) / "budget.json"
        )
        return ProductionMediaBudget.open(path)

    def refresh_budget_summary(self, project_id: str, run_id: str) -> dict[str, Any]:
        """Refresh display metadata from the canonical ledger; never gates."""

        with self._lock(f"run:{project_id}:{run_id}"):
            run = self.load_run(project_id, run_id)
            summary = self.media_budget(project_id, run_id).snapshot()
            run["budget"] = summary
            run["updated_at"] = _now()
            self._write_json(self.run_path(project_id, run_id), run)
            return summary

    def reserve_budget(
        self,
        project_id: str,
        run_id: str,
        *,
        idempotency_key: str,
        tool_name: str,
        estimated_usd: float,
        trace_id: str,
    ) -> dict[str, Any]:
        """Compatibility wrapper delegated to ProductionMediaBudget.

        New execution code should use ``media_budget().reserve`` directly so
        provider/model and submission claim stay explicit.
        """

        ledger = self.media_budget(project_id, run_id)
        decision = ledger.reserve(
            idempotency_key=idempotency_key,
            tool_name=tool_name,
            estimated_usd=estimated_usd,
            provider="legacy-wrapper",
            model="",
        )
        self.refresh_budget_summary(project_id, run_id)
        if not decision.ok:
            raise ProductionBudgetError(decision.reason or "production budget refused")
        return decision.to_dict()

    def settle_budget(
        self,
        project_id: str,
        run_id: str,
        *,
        reservation_id: str,
        actual_usd: float,
        status: str = "settled",
    ) -> dict[str, Any]:
        """Compatibility settlement delegated to ProductionMediaBudget."""

        ledger = self.media_budget(project_id, run_id)
        if status == "suspected_charge":
            reservation = ledger.mark_uncertain(
                reservation_id, error="legacy wrapper marked suspected charge"
            )
        else:
            reservation = ledger.settle(reservation_id, actual_usd=actual_usd)
        self.refresh_budget_summary(project_id, run_id)
        return {
            **reservation.__dict__,
            "estimated_usd": reservation.estimated_usd,
            "actual_usd": reservation.actual_usd,
        }

    def _legacy_float_reserve_budget_do_not_use(
        self,
        project_id: str,
        run_id: str,
        *,
        idempotency_key: str,
        tool_name: str,
        estimated_usd: float,
        trace_id: str,
    ) -> dict[str, Any]:
        raise ProductionStoreError(
            "legacy float budget path is disabled; use ProductionMediaBudget"
        )

    def _legacy_float_settle_budget_do_not_use(
        self,
        project_id: str,
        run_id: str,
        *,
        reservation_id: str,
        actual_usd: float,
        status: str = "settled",
    ) -> dict[str, Any]:
        raise ProductionStoreError(
            "legacy float budget path is disabled; use ProductionMediaBudget"
        )

    # -- production tool-call idempotency -----------------------------

    def claim_tool_call(
        self,
        project_id: str,
        run_id: str,
        *,
        tool_name: str,
        args: dict[str, Any],
        trace_id: str,
        idempotency_key: str,
        project_revision: int,
    ) -> dict[str, Any]:
        """Claim one formal tool execution before any provider can be called."""

        name = str(tool_name or "").strip()
        trace = str(trace_id or "").strip()
        key = str(idempotency_key or "").strip()
        if not name or not trace or not key:
            raise ProductionValidationError(
                "formal tool calls require tool_name, trace_id and idempotency_key"
            )
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        path = self.tool_calls_dir(project_id, run_id) / f"{digest}.json"
        normalized_args = json.loads(json.dumps(dict(args or {}), default=str))
        request_hash = hashlib.sha256(
            json.dumps(
                {"tool_name": name, "args": normalized_args},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        mutation_lock = self.tool_call_mutation_lock_path(
            project_id, run_id, digest
        )
        with self._cross_process_lock(mutation_lock):
            if path.exists():
                existing = self._read_json(path)
                if (
                    str(existing.get("idempotency_key") or "") != key
                    or str(existing.get("request_hash") or "") != request_hash
                    or str(existing.get("project_id") or "") != project_id
                    or str(existing.get("run_id") or "") != run_id
                ):
                    raise IdempotencyConflictError(
                        f"tool idempotency key reused with a different request: {key}"
                    )
                return {**existing, "duplicate": True}
            if not self._acquire_tool_call_execution_lease(
                project_id, run_id, digest
            ):
                # This can only happen if a previous owner acquired the
                # execution lease but has not yet committed its receipt.  Do
                # not create a competing receipt or let this caller dispatch.
                raise IdempotencyConflictError(
                    f"tool execution lease is already owned: {key}"
                )
            now = _now()
            try:
                record = {
                    "schema": "lumeri.production-tool-call",
                    "version": 2,
                    "tool_call_id": f"tool-{digest[:16]}",
                    "project_id": project_id,
                    "run_id": run_id,
                    "tool_name": name,
                    "args": normalized_args,
                    "request_hash": request_hash,
                    "trace_id": trace,
                    "idempotency_key": key,
                    "status": "claimed",
                    "project_revision_before": int(project_revision),
                    "project_revision_after": None,
                    "reservation_id": None,
                    "result": None,
                    "error": None,
                    "execution_owner": {
                        "owner_id": self._owner_id,
                        "pid": os.getpid(),
                        "claimed_at": now,
                    },
                    "reconciliation": None,
                    "created_at": now,
                    "updated_at": now,
                }
                self._write_json(path, record)
                self._append_run_event(
                    project_id,
                    run_id,
                    "tool_call_claimed",
                    {
                        "tool_call_id": record["tool_call_id"],
                        "tool_name": name,
                        "owner_id": self._owner_id,
                    },
                    trace_id=trace,
                )
                return {**record, "duplicate": False}
            except Exception:
                self._release_tool_call_execution_lease(digest)
                raise

    def complete_tool_call(
        self,
        project_id: str,
        run_id: str,
        idempotency_key: str,
        *,
        status: str,
        project_revision: int,
        result: dict[str, Any] | None = None,
        error: str | None = None,
        reservation_id: str | None = None,
    ) -> dict[str, Any]:
        target_status = str(status or "")
        if target_status not in {"succeeded", "background", "failed", "uncertain"}:
            raise ProductionValidationError(f"invalid tool-call status: {target_status}")
        key = str(idempotency_key or "").strip()
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        path = self.tool_calls_dir(project_id, run_id) / f"{digest}.json"
        mutation_lock = self.tool_call_mutation_lock_path(
            project_id, run_id, digest
        )
        release_owner = False
        with self._cross_process_lock(mutation_lock):
            if not path.exists():
                raise ProductionNotFoundError(f"tool-call receipt not found: {key}")
            record = self._read_json(path)
            if str(record.get("idempotency_key") or "") != key:
                raise IdempotencyConflictError("tool-call receipt key mismatch")
            current = str(record.get("status") or "")
            if current != "claimed":
                if current == target_status:
                    release_owner = self._owns_tool_call_execution_lease(digest)
                    completed = record
                else:
                    raise IdempotencyConflictError(
                        f"tool-call receipt is already {current}; refusing {target_status} overwrite"
                    )
            else:
                self._assert_tool_call_owner(record, digest)
                now = _now()
                record.update(
                    {
                        "status": target_status,
                        "project_revision_after": int(project_revision),
                        "result": dict(result) if isinstance(result, dict) else None,
                        "error": str(error or "")[:4000] or None,
                        "reservation_id": str(reservation_id or "") or None,
                        "updated_at": now,
                        "completed_at": now,
                        "execution_owner_released_at": now,
                    }
                )
                self._write_json(path, record)
                self._append_run_event(
                    project_id,
                    run_id,
                    "tool_call_completed",
                    {
                        "tool_call_id": record.get("tool_call_id"),
                        "tool_name": record.get("tool_name"),
                        "status": target_status,
                    },
                    trace_id=str(record.get("trace_id") or ""),
                )
                release_owner = True
                completed = record
        if release_owner:
            self._release_tool_call_execution_lease(digest)
        return completed

    def get_tool_call(
        self, project_id: str, run_id: str, idempotency_key: str
    ) -> dict[str, Any]:
        key = str(idempotency_key or "").strip()
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        path = self.tool_calls_dir(project_id, run_id) / f"{digest}.json"
        if not path.exists():
            raise ProductionNotFoundError(f"tool-call receipt not found: {key}")
        record = self._read_json(path)
        if str(record.get("idempotency_key") or "") != key:
            raise IdempotencyConflictError("tool-call receipt key mismatch")
        return record

    def bind_tool_call_reservation(
        self,
        project_id: str,
        run_id: str,
        idempotency_key: str,
        *,
        reservation_id: str,
    ) -> dict[str, Any]:
        """Persist the budget reservation before the dispatcher can submit."""

        key = str(idempotency_key or "").strip()
        reservation = str(reservation_id or "").strip()
        if not key or not reservation:
            raise ProductionValidationError(
                "tool-call reservation binding requires both ids"
            )
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        path = self.tool_calls_dir(project_id, run_id) / f"{digest}.json"
        mutation_lock = self.tool_call_mutation_lock_path(
            project_id, run_id, digest
        )
        with self._cross_process_lock(mutation_lock):
            record = self._read_json(path)
            existing = str(record.get("reservation_id") or "")
            if existing and existing != reservation:
                raise IdempotencyConflictError(
                    "tool-call receipt is already bound to a different reservation"
                )
            if str(record.get("status") or "") != "claimed":
                raise IdempotencyConflictError(
                    f"cannot bind reservation to {record.get('status')} tool call"
                )
            self._assert_tool_call_owner(record, digest)
            if not existing:
                record["reservation_id"] = reservation
                record["updated_at"] = _now()
                self._write_json(path, record)
            return record

    def reconcile_inflight_tool_calls(
        self, project_id: str, run_id: str, *, project_revision: int
    ) -> list[dict[str, Any]]:
        """Reconcile only receipts whose execution owner is truly gone.

        A call-scoped advisory lease remains held for the full dispatcher
        execution.  A busy lease is authoritative evidence that another
        Manager/CLI process is still working, so resume must leave it alone.
        For an orphaned paid call, the budget ledger decides the auditable
        fail-closed action: a never-submitted reservation is released, while
        submitted/uncertain/settled work remains charged or reserved.
        """

        reconciled: list[dict[str, Any]] = []
        directory = self.tool_calls_dir(project_id, run_id)
        if not directory.exists():
            return reconciled
        for path in sorted(directory.glob("*.json")):
            if path.name.startswith("._"):
                continue
            digest = path.stem
            mutation_lock = self.tool_call_mutation_lock_path(
                project_id, run_id, digest
            )
            with self._cross_process_lock(mutation_lock):
                try:
                    record = self._read_json(path)
                except (OSError, json.JSONDecodeError, ProductionStoreError):
                    continue
                if str(record.get("status") or "") != "claimed":
                    continue
                if self._owns_tool_call_execution_lease(digest):
                    continue
                probe = self._try_acquire_tool_call_execution_probe(
                    project_id, run_id, digest
                )
                if probe is None:
                    # Another process still owns the dispatcher execution.
                    continue
                try:
                    # Re-read under both the mutation lock and execution lease
                    # in case completion won the race before our probe.
                    record = self._read_json(path)
                    if str(record.get("status") or "") != "claimed":
                        continue
                    reservation_id = str(record.get("reservation_id") or "") or None
                    target_status = "failed"
                    error = (
                        "execution owner exited before the formal tool result "
                        "was committed"
                    )
                    budget_reconciliation: dict[str, Any] | None = None
                    if reservation_id:
                        try:
                            budget_reconciliation = (
                                self.media_budget(project_id, run_id)
                                .reconcile_orphaned_reservation(
                                    reservation_id,
                                    tool_call_id=str(record.get("tool_call_id") or ""),
                                    owner_id=str(
                                        (record.get("execution_owner") or {}).get(
                                            "owner_id"
                                        )
                                        if isinstance(
                                            record.get("execution_owner"), dict
                                        )
                                        else ""
                                    ),
                                    reason=error,
                                )
                            )
                            action = str(
                                budget_reconciliation.get("action") or ""
                            )
                            if action not in {
                                "released_unsubmitted",
                                "preserved_released",
                            }:
                                target_status = "uncertain"
                                error += (
                                    "; budget retained fail-closed pending "
                                    "reconciliation"
                                )
                            else:
                                error += "; unsubmitted budget reservation released"
                        except Exception as exc:
                            # If even the budget recovery cannot be committed,
                            # never downgrade the receipt to a definite failure.
                            target_status = "uncertain"
                            error += (
                                "; budget reconciliation failed closed: "
                                f"{type(exc).__name__}: {exc}"
                            )
                    now = _now()
                    reconciliation = {
                        "action": "orphan_reconciled",
                        "reconciled_at": now,
                        "reconciled_by": self._owner_id,
                        "previous_owner": record.get("execution_owner"),
                        "budget": budget_reconciliation,
                    }
                    record.update(
                        {
                            "status": target_status,
                            "project_revision_after": int(project_revision),
                            "result": None,
                            "error": error[:4000],
                            "updated_at": now,
                            "completed_at": now,
                            "execution_owner_released_at": now,
                            "reconciliation": reconciliation,
                        }
                    )
                    self._write_json(path, record)
                    self._append_run_event(
                        project_id,
                        run_id,
                        "tool_call_orphan_reconciled",
                        {
                            "tool_call_id": record.get("tool_call_id"),
                            "tool_name": record.get("tool_name"),
                            "status": target_status,
                            "budget_action": (
                                budget_reconciliation or {}
                            ).get("action"),
                        },
                        trace_id=str(record.get("trace_id") or ""),
                    )
                    reconciled.append(record)
                finally:
                    self._release_tool_call_execution_probe(probe)
        if reconciled:
            try:
                self.refresh_budget_summary(project_id, run_id)
            except Exception:
                # The canonical ledger is already durable.  A stale display
                # cache must not undo or mask successful reconciliation.
                pass
        return reconciled

    # -- turn idempotency ---------------------------------------------

    def claim_turn(
        self,
        project_id: str,
        run_id: str,
        *,
        session_id: str,
        client_turn_id: str,
        message: str,
        project_revision: int,
        expected_project_revision: int | None = None,
    ) -> dict[str, Any]:
        key = str(client_turn_id or "").strip()
        if not key:
            raise ProductionValidationError("client_turn_id must be non-empty")
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        path = self.turns_dir(project_id, run_id) / f"{digest}.json"
        message_hash = hashlib.sha256(message.encode("utf-8")).hexdigest()
        with self._lock(f"run:{project_id}:{run_id}"):
            if path.exists():
                existing = self._read_json(path)
                if (
                    str(existing.get("client_turn_id")) != key
                    or str(existing.get("message_hash")) != message_hash
                    or str(existing.get("project_id")) != project_id
                    or str(existing.get("run_id")) != run_id
                ):
                    raise IdempotencyConflictError(
                        f"client_turn_id reused with different request: {key}"
                    )
                return {**existing, "duplicate": True}
            if expected_project_revision is not None and int(
                expected_project_revision
            ) != int(project_revision):
                raise RevisionConflictError(
                    "project revision mismatch: "
                    f"expected {expected_project_revision}, current {project_revision}",
                    current_revision=project_revision,
                )
            now = _now()
            record = {
                "schema": "lumeri.production-turn",
                "version": 1,
                "client_turn_id": key,
                "session_id": session_id,
                "project_id": project_id,
                "run_id": run_id,
                "message": message,
                "message_hash": message_hash,
                "project_revision": int(project_revision),
                "status": "accepted",
                "outcome": None,
                "created_at": now,
                "updated_at": now,
            }
            self._write_json(path, record)
            run = self.load_run(project_id, run_id)
            turn_ids = list(run.get("turn_ids") or [])
            turn_ids.append(key)
            run["turn_ids"] = turn_ids
            run["revision"] = int(run.get("revision") or 0) + 1
            run["updated_at"] = now
            self._write_json(self.run_path(project_id, run_id), run)
            self._append_run_event(
                project_id,
                run_id,
                "turn_claimed",
                {"client_turn_id": key, "session_id": session_id},
                trace_id=key,
            )
            return {**record, "duplicate": False}

    def complete_turn(
        self,
        project_id: str,
        run_id: str,
        client_turn_id: str,
        *,
        status: str,
        outcome: str,
        project_revision: int,
    ) -> dict[str, Any]:
        if status not in {"completed", "cancelled", "failed", "interrupted"}:
            raise ProductionValidationError(f"invalid turn status: {status}")
        key = str(client_turn_id or "")
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        path = self.turns_dir(project_id, run_id) / f"{digest}.json"
        with self._lock(f"run:{project_id}:{run_id}"):
            if not path.exists():
                raise ProductionNotFoundError(f"turn not found: {client_turn_id}")
            record = self._read_json(path)
            if record.get("status") in {"completed", "cancelled", "failed"}:
                return record
            record["status"] = status
            record["outcome"] = str(outcome or "no_change")
            record["project_revision"] = int(project_revision)
            record["updated_at"] = _now()
            self._write_json(path, record)
            run = self.load_run(project_id, run_id)
            run["project_revision"] = max(
                int(run.get("project_revision") or 0), int(project_revision)
            )
            run["revision"] = int(run.get("revision") or 0) + 1
            run["updated_at"] = record["updated_at"]
            self._write_json(self.run_path(project_id, run_id), run)
            self._append_run_event(
                project_id,
                run_id,
                "turn_finished",
                {"client_turn_id": key, "status": status, "outcome": record["outcome"]},
                trace_id=key,
            )
            return record

    def reconcile_inflight_turns(
        self, project_id: str, run_id: str, *, session_id: str
    ) -> list[dict[str, Any]]:
        """Mark crash-left accepted turns interrupted without re-running them."""

        changed: list[dict[str, Any]] = []
        with self._lock(f"run:{project_id}:{run_id}"):
            for path in sorted(self.turns_dir(project_id, run_id).glob("*.json")):
                if path.name.startswith("._"):
                    continue
                try:
                    record = self._read_json(path)
                except (OSError, json.JSONDecodeError):
                    continue
                if record.get("session_id") != session_id:
                    continue
                if record.get("status") not in {"accepted", "running"}:
                    continue
                record["status"] = "interrupted"
                record["outcome"] = "blocked"
                record["updated_at"] = _now()
                self._write_json(path, record)
                changed.append(record)
            if changed:
                run = self.load_run(project_id, run_id)
                run["revision"] = int(run.get("revision") or 0) + 1
                run["updated_at"] = _now()
                self._write_json(self.run_path(project_id, run_id), run)
                self._append_run_event(
                    project_id,
                    run_id,
                    "turns_reconciled",
                    {"session_id": session_id, "count": len(changed)},
                )
        return changed

    # -- sessions/runtime ---------------------------------------------

    def create_session_record(
        self,
        session_id: str,
        *,
        project_id: str,
        run_id: str,
        output_dir: str | Path,
        account_id: str = "",
        remote: bool = False,
    ) -> dict[str, Any]:
        path = self.session_meta_path(session_id)
        with self._lock(f"session:{session_id}"):
            if path.exists():
                raise IdempotencyConflictError(f"session already exists: {session_id}")
            now = _now()
            record = {
                "schema": "lumeri.session",
                "version": 2,
                "session_id": session_id,
                "project_id": project_id,
                "run_id": run_id,
                "output_dir": str(Path(output_dir).expanduser().resolve()),
                "account_id": str(account_id or ""),
                "remote": bool(remote),
                "status": "running",
                "created_at": now,
                "updated_at": now,
                "turn_count": 0,
                "active_client_turn_id": None,
            }
            self._write_json(path, record)
            self.bind_session(project_id, session_id)
            return record

    def load_session(self, session_id: str) -> dict[str, Any]:
        path = self.session_meta_path(session_id)
        if not path.exists():
            raise ProductionNotFoundError(f"session not found: {session_id}")
        return self._read_json(path)

    def update_session(self, session_id: str, updates: dict[str, Any]) -> dict[str, Any]:
        with self._lock(f"session:{session_id}"):
            meta = self.load_session(session_id)
            immutable = {"session_id", "project_id", "run_id", "created_at"}
            changed = False
            for key, value in dict(updates or {}).items():
                if key not in immutable and meta.get(key) != value:
                    meta[key] = value
                    changed = True
            if not changed:
                return meta
            meta["updated_at"] = _now()
            self._write_json(self.session_meta_path(session_id), meta)
            return meta

    def save_runtime_state(self, session_id: str, state: dict[str, Any]) -> Path:
        with self._lock(f"session:{session_id}"):
            path = self.runtime_state_path(session_id)
            payload = {"schema": "lumeri.runtime-state", "version": 1, **state}
            if path.exists():
                try:
                    if self._read_json(path) == payload:
                        return path
                except (OSError, json.JSONDecodeError, ProductionStoreError):
                    pass
            self._write_json(path, payload)
            return path

    def load_runtime_state(self, session_id: str) -> dict[str, Any]:
        path = self.runtime_state_path(session_id)
        if not path.exists():
            return {}
        return self._read_json(path)

    def list_session_records(
        self, *, include_deleted: bool = False
    ) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        if not self.sessions_root.exists():
            return records
        for path in sorted(self.sessions_root.glob("*/meta.json")):
            try:
                record = self._read_json(path)
            except (OSError, json.JSONDecodeError):
                continue
            if (
                record.get("schema") == "lumeri.session"
                and (include_deleted or not record.get("deleted_at"))
            ):
                records.append(record)
        return records

    # -- artifacts ----------------------------------------------------

    def artifact_path(self, project_id: str, asset_id: str) -> Path:
        from gemia.tools._context import AssetRegistry

        registry_path = self.asset_registry_path(project_id)
        if not registry_path.exists():
            raise ProductionNotFoundError(f"project has no asset registry: {project_id}")
        registry = AssetRegistry.load(registry_path)
        try:
            path = registry.get(asset_id).path
        except KeyError as exc:
            raise ProductionNotFoundError(f"asset not found: {project_id}/{asset_id}") from exc
        if not path.exists() or not path.is_file():
            raise ProductionNotFoundError(f"asset file is missing: {project_id}/{asset_id}")
        return path

    # -- internals ----------------------------------------------------

    def _lock(self, key: str) -> threading.RLock:
        with self._locks_guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = self._locks[key] = threading.RLock()
            return lock

    @contextmanager
    def _cross_process_lock(self, path: Path) -> Iterator[None]:
        """Serialize one receipt mutation across Store instances/processes."""

        with self._lock(f"file:{path}"):
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a+", encoding="utf-8") as lock_file:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _acquire_tool_call_execution_lease(
        self, project_id: str, run_id: str, digest: str
    ) -> bool:
        """Hold a per-call lease until its durable terminal receipt exists."""

        with self._tool_call_leases_guard:
            if digest in self._tool_call_leases:
                return True
            path = self.tool_call_execution_lease_path(project_id, run_id, digest)
            path.parent.mkdir(parents=True, exist_ok=True)
            lease = path.open("a+", encoding="utf-8")
            try:
                fcntl.flock(lease.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                lease.close()
                return False
            except Exception:
                lease.close()
                raise
            self._tool_call_leases[digest] = lease
            return True

    def _owns_tool_call_execution_lease(self, digest: str) -> bool:
        with self._tool_call_leases_guard:
            lease = self._tool_call_leases.get(digest)
            return lease is not None and not lease.closed

    def _assert_tool_call_owner(
        self, record: dict[str, Any], digest: str
    ) -> None:
        owner = record.get("execution_owner")
        expected = (
            str(owner.get("owner_id") or "") if isinstance(owner, dict) else ""
        )
        if (
            not self._owns_tool_call_execution_lease(digest)
            or expected != self._owner_id
        ):
            raise IdempotencyConflictError(
                "only the live execution owner may mutate a claimed tool receipt"
            )

    def _release_tool_call_execution_lease(self, digest: str) -> None:
        with self._tool_call_leases_guard:
            lease = self._tool_call_leases.pop(digest, None)
        if lease is None:
            return
        try:
            fcntl.flock(lease.fileno(), fcntl.LOCK_UN)
        finally:
            lease.close()

    def _try_acquire_tool_call_execution_probe(
        self, project_id: str, run_id: str, digest: str
    ) -> TextIO | None:
        path = self.tool_call_execution_lease_path(project_id, run_id, digest)
        path.parent.mkdir(parents=True, exist_ok=True)
        probe = path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(probe.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            probe.close()
            return None
        except Exception:
            probe.close()
            raise
        return probe

    @staticmethod
    def _release_tool_call_execution_probe(probe: TextIO) -> None:
        try:
            fcntl.flock(probe.fileno(), fcntl.LOCK_UN)
        finally:
            probe.close()

    def _append_run_event(
        self,
        project_id: str,
        run_id: str,
        kind: str,
        payload: dict[str, Any],
        *,
        trace_id: str | None = None,
    ) -> None:
        path = self.run_events_path(project_id, run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        line = {
            "event_id": f"pev-{uuid.uuid4().hex[:12]}",
            "kind": str(kind),
            "trace_id": str(trace_id or ""),
            "created_at": _now(),
            "payload": dict(payload or {}),
        }
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(line, ensure_ascii=False, separators=(",", ":")) + "\n")
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ProductionStoreError(f"expected JSON object: {path}")
        return payload

    @staticmethod
    def _write_json(path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        data = json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n"
        try:
            with tmp.open("w", encoding="utf-8") as fh:
                fh.write(data)
                fh.flush()
                try:
                    os.fsync(fh.fileno())
                except OSError:
                    pass
            os.replace(tmp, path)
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    @staticmethod
    def _validate_id(value: str, label: str) -> None:
        if not isinstance(value, str) or not _ID_RE.match(value):
            raise ProductionValidationError(
                f"invalid {label} (must match [A-Za-z0-9][A-Za-z0-9_-]{{0,95}}): {value!r}"
            )


__all__ = [
    "PRODUCTION_STATES",
    "IdempotencyConflictError",
    "ProductionBudgetError",
    "ProductionNotFoundError",
    "ProductionStore",
    "ProductionStoreError",
    "ProductionValidationError",
    "RevisionConflictError",
    "StateTransitionError",
    "default_reality_contract",
]
