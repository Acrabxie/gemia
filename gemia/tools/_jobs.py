"""Job registry for async task submission and polling.

Supports Veo/Lyria generation jobs and future v4 build tasks.
Shared with AssetRegistry in ToolContext.

Core types:
- ``JobRecord``: dataclass representing a single async job submission.
- ``JobRegistry``: session-scoped job_id → record mapping with persistence.

A job lifecycle:
1. submit() → creates record, returns job_id + asset_id_pending
2. Model calls check_job() or wait_for_job() with job_id
3. update_from_poll() → updates status + final_path/error
4. persist via save() at session end, reload via load() at session start
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4


def _now() -> str:
    """Return current UTC time in ISO8601 format."""
    return datetime.now(timezone.utc).isoformat()


@dataclass
class JobRecord:
    """Single async job submission record.

    Fields:
        job_id: unique identifier (e.g. "veo_a1b2c3")
        kind: "video" | "audio" | "image-async" | "build"
        provider: identifier for the provider (e.g. "ai_studio:veo-3.1-fast")
        operation_name: LRO name (Veo) or process id (build)
        pending_asset_id: pre-allocated asset_id waiting for this job
        submitted_at: ISO8601 UTC timestamp
        estimated_eta_sec: estimated seconds to completion
        last_polled_at: ISO8601 UTC timestamp of last poll, None if never polled
        last_polled_status: "submitted" | "queued" | "running" | "done" | "failed"
        final_path: Path to result asset when status=="done"
        final_error: Error message when status=="failed"
        summary: human-readable description of the job
        submitted_mono: time.monotonic() value at submission (not serialized)
        pid: local process id for kind=="shell"/"build" jobs (None for LROs)
        pgid: process GROUP id — persisted at spawn because getpgid() on a
            dead group leader raises; kill/cleanup paths must use this
        started_epoch: time.time() at spawn, identity anchor for pid-reuse
            checks after a restart
        announced: completion already surfaced to the model (notification
            injected or the model itself polled the terminal state) — the
            watcher must not announce twice
        request_id/reservation_id: durable links to the paid-media budget and
            provider audit records (None for local jobs)
        estimated_cost_usd: reservation estimate retained across restarts
    """
    job_id: str
    kind: str
    provider: str
    operation_name: str
    pending_asset_id: str
    submitted_at: str
    estimated_eta_sec: float
    last_polled_at: str | None
    last_polled_status: str
    final_path: Path | None
    final_error: str | None
    summary: str
    submitted_mono: float = field(default_factory=time.monotonic, repr=False)
    pid: int | None = None
    pgid: int | None = None
    started_epoch: float | None = None
    announced: bool = False
    request_id: str | None = None
    reservation_id: str | None = None
    estimated_cost_usd: float = 0.0
    budget_ledger_path: str | None = None
    budget_run_id: str | None = None
    prompt_sha256: str | None = None
    source_lineage: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dict, skipping submitted_mono."""
        d = asdict(self)
        d.pop("submitted_mono", None)
        if d["final_path"] is not None:
            d["final_path"] = str(d["final_path"])
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> JobRecord:
        """Deserialize from dict, resetting submitted_mono to current time."""
        d = dict(d)  # copy to avoid mutation
        if d.get("final_path"):
            d["final_path"] = Path(d["final_path"])
        if d.get("source_lineage") is not None:
            d["source_lineage"] = tuple(str(item) for item in d.get("source_lineage") or [])
        obj = cls(**d)
        obj.submitted_mono = time.monotonic()
        return obj


class JobRegistry:
    """Session-scoped job_id → JobRecord mapping with persistence."""

    def __init__(self) -> None:
        self._records: dict[str, JobRecord] = {}

    def submit(
        self,
        *,
        kind: str,
        provider: str,
        operation_name: str,
        pending_asset_id: str,
        estimated_eta_sec: float,
        summary: str,
        job_id: str | None = None,
        request_id: str | None = None,
        reservation_id: str | None = None,
        estimated_cost_usd: float = 0.0,
        budget_ledger_path: str | None = None,
        budget_run_id: str | None = None,
        prompt_sha256: str | None = None,
        source_lineage: tuple[str, ...] | list[str] = (),
    ) -> JobRecord:
        """Submit a new async job.

        Args:
            kind: "video" | "audio" | "image-async" | "build"
            provider: provider identifier
            operation_name: LRO name or process id
            pending_asset_id: pre-allocated asset_id
            estimated_eta_sec: estimated completion time
            summary: human-readable description
            job_id: optional; if None, generates "kind_<uuid4 hex[:8]>"

        Returns:
            Newly created JobRecord with last_polled_status="submitted"
        """
        if job_id is None:
            job_id = f"{kind}_{uuid4().hex[:8]}"

        if job_id in self._records:
            raise ValueError(f"job_id already exists: {job_id!r}")

        record = JobRecord(
            job_id=job_id,
            kind=kind,
            provider=provider,
            operation_name=operation_name,
            pending_asset_id=pending_asset_id,
            submitted_at=_now(),
            estimated_eta_sec=estimated_eta_sec,
            last_polled_at=None,
            last_polled_status="submitted",
            final_path=None,
            final_error=None,
            summary=summary,
            submitted_mono=time.monotonic(),
            request_id=str(request_id) if request_id else None,
            reservation_id=str(reservation_id) if reservation_id else None,
            estimated_cost_usd=float(estimated_cost_usd),
            budget_ledger_path=str(budget_ledger_path) if budget_ledger_path else None,
            budget_run_id=str(budget_run_id) if budget_run_id else None,
            prompt_sha256=str(prompt_sha256) if prompt_sha256 else None,
            source_lineage=tuple(str(item) for item in source_lineage),
        )
        self._records[job_id] = record
        return record

    def get(self, job_id: str) -> JobRecord:
        """Retrieve a job record by id.

        Raises:
            KeyError: if job_id not found, with list of known ids in message
        """
        try:
            return self._records[job_id]
        except KeyError:
            known = ", ".join(self._records.keys()) or "(none)"
            raise KeyError(
                f"job_id not in registry: {job_id!r}. Known: {known}"
            ) from None

    def list_pending(self) -> list[JobRecord]:
        """Return all jobs with last_polled_status not in {done, failed}."""
        return [
            r for r in self._records.values()
            if r.last_polled_status not in ("done", "failed")
        ]

    def list_records(self) -> list[JobRecord]:
        """Return every record (any status), insertion order."""
        return list(self._records.values())

    def update_from_poll(
        self,
        job_id: str,
        status: str,
        *,
        final_path: Path | None = None,
        error: str | None = None,
    ) -> JobRecord:
        """Update a job record after polling.

        Args:
            job_id: job identifier
            status: "submitted" | "queued" | "running" | "done" | "failed"
            final_path: path to result asset if status=="done"
            error: error message if status=="failed"

        Returns:
            Updated JobRecord

        Raises:
            KeyError: if job_id not found
        """
        record = self.get(job_id)
        record.last_polled_at = _now()
        record.last_polled_status = status

        if status == "done" and final_path is not None:
            record.final_path = Path(final_path).resolve()
        if status == "failed" and error is not None:
            record.final_error = error

        return record

    def compact_text_for_prompt(self) -> str:
        """Render pending jobs for system prompt.

        Example output:
            - veo_a1b2c3 [video, ETA was 120s, last status: running] → v_007
            - lyria_d4e5f6 [audio, ETA was 60s, last status: done] → aud_004

        Returns "(no pending jobs)" if no pending jobs exist.
        """
        pending = self.list_pending()
        if not pending:
            return "(no pending jobs)"

        lines = []
        for r in pending:
            elapsed = time.monotonic() - r.submitted_mono
            eta_str = f"ETA was {r.estimated_eta_sec:.0f}s"
            line = (
                f"- {r.job_id} [{r.kind}, {eta_str}, "
                f"submitted {elapsed:.0f}s ago, last status: {r.last_polled_status}] "
                f"→ {r.pending_asset_id}"
            )
            lines.append(line)

        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        """Serialize registry to dict for persistence."""
        return {
            job_id: record.to_dict()
            for job_id, record in self._records.items()
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> JobRegistry:
        """Deserialize registry from dict."""
        registry = cls()
        for job_id, record_dict in d.items():
            registry._records[job_id] = JobRecord.from_dict(record_dict)
        return registry

    def save(self, path: Path) -> None:
        """Persist registry to file with atomic rename.

        Writes to a temporary file first, then renames atomically to avoid
        partial writes on process crash.
        """
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)

        # Write to temporary file
        tmp_path = path.parent / f"{path.name}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, indent=2)

        # Atomic rename
        os.replace(tmp_path, path)

    @classmethod
    def load(cls, path: Path) -> JobRegistry:
        """Load registry from file, creating empty registry if file doesn't exist."""
        path = Path(path)
        if not path.exists():
            return cls()

        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)

        return cls.from_dict(data)


__all__ = [
    "JobRecord",
    "JobRegistry",
]
