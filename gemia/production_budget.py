"""Persistent paid-media budget ledger for a single production run.

This module is intentionally independent from the agent loop.  A caller first
reserves money using a stable idempotency key, then atomically claims the right
to submit the provider request.  Only the first claimant is allowed to send a
paid request.  A request whose outcome is unknown remains charged at its
estimate until a human or a reconciliation process settles it.

Money is stored as integer micro-US dollars.  The ledger is protected by both a
per-process lock and an advisory file lock, and every mutation is persisted via
an atomic replace.  That makes parallel child agents and process restarts see
the same hard cap instead of maintaining independent in-memory counters.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import tempfile
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Iterator


DEFAULT_CAP_USD = Decimal("15.00")
DEFAULT_WARNING_USD = Decimal("12.00")
DEFAULT_BASELINE_SPEND_USD = Decimal("0")
DEFAULT_VEO_MAX_CALLS = 3
DEFAULT_VEO_MAX_DURATION_SEC = 24.0
DEFAULT_PRICING_VERSION = "2026-07-19"
LEDGER_SCHEMA_VERSION = 1
PAID_MEDIA_CONTEXT_KEY = "paid_media_call"

_ACTIVE_STATUSES = frozenset({"reserved", "submitted", "uncertain"})
_FINAL_STATUSES = frozenset({"settled", "released"})
_LOCKS_GUARD = threading.Lock()
_LOCKS: dict[str, threading.RLock] = {}


def _absolute_path(path: str | Path) -> Path:
    """Lexically normalize a trusted ledger path without touching the disk."""

    return Path(os.path.abspath(os.path.expanduser(str(path))))


class ProductionBudgetError(RuntimeError):
    """Base class for persistent production-budget errors."""


class PaidMediaSubmissionClaimedError(ProductionBudgetError):
    """Raised when an automatic retry tries to submit an existing reservation."""


def usd_to_microusd(value: Decimal | float | int | str) -> int:
    """Convert dollars to integer micro-dollars without binary-float drift."""
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"invalid USD amount: {value!r}") from exc
    if not amount.is_finite() or amount < 0:
        raise ValueError(f"USD amount must be finite and >= 0, got {value!r}")
    return int((amount * Decimal("1000000")).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def microusd_to_usd(value: int) -> float:
    return float(Decimal(int(value)) / Decimal("1000000"))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()
    return f"{prefix}_{digest[:24]}"


def _thread_lock(path: Path) -> threading.RLock:
    key = str(path.resolve())
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.RLock())


@dataclass(frozen=True)
class MediaBudgetReservation:
    reservation_id: str
    idempotency_key: str
    tool_name: str
    provider: str
    model: str
    estimated_microusd: int
    status: str
    request_id: str
    provider_operation_id: str | None = None
    result_asset_id: str | None = None
    actual_microusd: int | None = None
    error: str | None = None
    requested_duration_sec: float | None = None
    asset_materialization_status: str | None = None
    asset_path: str | None = None
    asset_sha256: str | None = None

    @property
    def estimated_usd(self) -> float:
        return microusd_to_usd(self.estimated_microusd)

    @property
    def actual_usd(self) -> float | None:
        if self.actual_microusd is None:
            return None
        return microusd_to_usd(self.actual_microusd)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "MediaBudgetReservation":
        return cls(
            reservation_id=str(value.get("reservation_id") or ""),
            idempotency_key=str(value.get("idempotency_key") or ""),
            tool_name=str(value.get("tool_name") or ""),
            provider=str(value.get("provider") or ""),
            model=str(value.get("model") or ""),
            estimated_microusd=int(value.get("estimated_microusd") or 0),
            status=str(value.get("status") or "reserved"),
            request_id=str(value.get("request_id") or ""),
            provider_operation_id=(
                str(value["provider_operation_id"])
                if value.get("provider_operation_id")
                else None
            ),
            result_asset_id=(str(value["result_asset_id"]) if value.get("result_asset_id") else None),
            actual_microusd=(
                int(value["actual_microusd"])
                if value.get("actual_microusd") is not None
                else None
            ),
            error=(str(value["error"]) if value.get("error") else None),
            requested_duration_sec=(
                float(value["requested_duration_sec"])
                if value.get("requested_duration_sec") is not None
                else None
            ),
            asset_materialization_status=(
                str(value["asset_materialization_status"])
                if value.get("asset_materialization_status")
                else None
            ),
            asset_path=(str(value["asset_path"]) if value.get("asset_path") else None),
            asset_sha256=(str(value["asset_sha256"]) if value.get("asset_sha256") else None),
        )


@dataclass(frozen=True)
class MediaBudgetDecision:
    ok: bool
    created: bool
    reservation: MediaBudgetReservation | None
    reason: str = ""
    warning: bool = False
    available_microusd: int = 0

    @property
    def available_usd(self) -> float:
        return microusd_to_usd(self.available_microusd)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "created": self.created,
            "reason": self.reason,
            "warning": self.warning,
            "available_usd": self.available_usd,
            "reservation": (
                {
                    **self.reservation.__dict__,
                    "estimated_usd": self.reservation.estimated_usd,
                    "actual_usd": self.reservation.actual_usd,
                }
                if self.reservation is not None
                else None
            ),
        }


@dataclass(frozen=True)
class PaidMediaCall:
    """Portable context injected into ``ToolContext.extra`` by the loop."""

    ledger_path: str
    run_id: str
    reservation_id: str
    request_id: str
    idempotency_key: str
    estimated_microusd: int
    requested_duration_sec: float | None = None

    @property
    def estimated_usd(self) -> float:
        return microusd_to_usd(self.estimated_microusd)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ledger_path": self.ledger_path,
            "run_id": self.run_id,
            "reservation_id": self.reservation_id,
            "request_id": self.request_id,
            "idempotency_key": self.idempotency_key,
            "estimated_microusd": self.estimated_microusd,
            "estimated_usd": self.estimated_usd,
            "requested_duration_sec": self.requested_duration_sec,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "PaidMediaCall":
        required = ("ledger_path", "run_id", "reservation_id", "request_id", "idempotency_key")
        missing = [key for key in required if not str(value.get(key) or "").strip()]
        if missing:
            raise ProductionBudgetError(
                "paid-media context is missing required fields: " + ", ".join(missing)
            )
        estimated = value.get("estimated_microusd")
        if estimated is None:
            estimated = usd_to_microusd(value.get("estimated_usd") or 0)
        return cls(
            ledger_path=str(value["ledger_path"]),
            run_id=str(value["run_id"]),
            reservation_id=str(value["reservation_id"]),
            request_id=str(value["request_id"]),
            idempotency_key=str(value["idempotency_key"]),
            estimated_microusd=int(estimated),
            requested_duration_sec=(
                float(value["requested_duration_sec"])
                if value.get("requested_duration_sec") is not None
                else None
            ),
        )


class ProductionMediaBudget:
    """A persistent, run-scoped hard cap for external paid media calls."""

    def __init__(
        self,
        path: str | Path,
        *,
        run_id: str,
        cap_usd: Decimal | float | int | str = DEFAULT_CAP_USD,
        warning_usd: Decimal | float | int | str = DEFAULT_WARNING_USD,
        veo_max_calls: int = DEFAULT_VEO_MAX_CALLS,
        veo_max_duration_sec: float = DEFAULT_VEO_MAX_DURATION_SEC,
        pricing_version: str = DEFAULT_PRICING_VERSION,
    ) -> None:
        self.path = _absolute_path(path)
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        self.run_id = str(run_id).strip()
        if not self.run_id:
            raise ValueError("run_id must be non-empty")
        cap = usd_to_microusd(cap_usd)
        warning = usd_to_microusd(warning_usd)
        baseline = usd_to_microusd(DEFAULT_BASELINE_SPEND_USD)
        if warning > cap:
            raise ValueError("warning_usd cannot exceed cap_usd")
        if int(veo_max_calls) < 0 or float(veo_max_duration_sec) < 0:
            raise ValueError("Veo limits must be >= 0")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._transaction(create=True) as document:
            if not document:
                document.update(
                    {
                        "schema_version": LEDGER_SCHEMA_VERSION,
                        "run_id": self.run_id,
                        "cap_microusd": cap,
                        "warning_microusd": warning,
                        "baseline_microusd": baseline,
                        "baseline_imports": {},
                        "veo_max_calls": int(veo_max_calls),
                        "veo_max_duration_sec": float(veo_max_duration_sec),
                        "pricing_version": str(pricing_version),
                        "created_at": _now(),
                        "updated_at": _now(),
                        "reservations": {},
                    }
                )
            self._validate_document(document)

    @classmethod
    def open(cls, path: str | Path) -> "ProductionMediaBudget":
        ledger_path = _absolute_path(path)
        try:
            document = json.loads(ledger_path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ProductionBudgetError(f"production budget ledger not found: {ledger_path}") from exc
        except (json.JSONDecodeError, OSError) as exc:
            raise ProductionBudgetError(f"cannot read production budget ledger {ledger_path}: {exc}") from exc
        if not isinstance(document, dict) or not str(document.get("run_id") or ""):
            raise ProductionBudgetError(f"invalid production budget ledger: {ledger_path}")
        # Opening an existing ledger is an observation, not a mutation.  Going
        # through ``__init__`` used to reopen a transaction and fsync-replace
        # the file even for a snapshot, which made reads both slow and
        # semantically dishonest on external filesystems.
        ledger = cls.__new__(cls)
        ledger.path = ledger_path
        ledger.lock_path = ledger_path.with_suffix(ledger_path.suffix + ".lock")
        ledger.run_id = str(document["run_id"])
        ledger._validate_document(document)
        return ledger

    @contextmanager
    def _transaction(self, *, create: bool = False) -> Iterator[dict[str, Any]]:
        lock = _thread_lock(self.path)
        with lock:
            self.lock_path.parent.mkdir(parents=True, exist_ok=True)
            with self.lock_path.open("a+", encoding="utf-8") as lock_file:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                try:
                    if self.path.exists():
                        try:
                            value = json.loads(self.path.read_text(encoding="utf-8"))
                        except (json.JSONDecodeError, OSError) as exc:
                            raise ProductionBudgetError(
                                f"cannot read production budget ledger {self.path}: {exc}"
                            ) from exc
                        if not isinstance(value, dict):
                            raise ProductionBudgetError(
                                f"production budget ledger is not a JSON object: {self.path}"
                            )
                        document = value
                    elif create:
                        document = {}
                    else:
                        raise ProductionBudgetError(
                            f"production budget ledger not found: {self.path}"
                        )
                    before = json.dumps(
                        document,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                        default=str,
                    )
                    yield document
                    after = json.dumps(
                        document,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                        default=str,
                    )
                    if document and after != before:
                        document["updated_at"] = _now()
                        self._write_atomic(document)
                finally:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _write_atomic(self, document: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.", suffix=".tmp", dir=str(self.path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(document, stream, ensure_ascii=False, indent=2, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_name, self.path)
        finally:
            try:
                Path(temp_name).unlink(missing_ok=True)
            except OSError:
                pass

    def _validate_document(self, document: dict[str, Any]) -> None:
        if int(document.get("schema_version") or 0) != LEDGER_SCHEMA_VERSION:
            raise ProductionBudgetError(
                f"unsupported production budget schema: {document.get('schema_version')!r}"
            )
        if str(document.get("run_id") or "") != self.run_id:
            raise ProductionBudgetError(
                f"ledger run_id mismatch: expected {self.run_id!r}, "
                f"found {document.get('run_id')!r}"
            )
        if not isinstance(document.get("reservations"), dict):
            raise ProductionBudgetError("production budget reservations must be an object")

    @staticmethod
    def _committed_microusd(document: dict[str, Any]) -> int:
        total = int(document.get("baseline_microusd") or 0)
        for raw in (document.get("reservations") or {}).values():
            if not isinstance(raw, dict):
                continue
            status = str(raw.get("status") or "")
            estimate = int(raw.get("estimated_microusd") or 0)
            if status in _ACTIVE_STATUSES:
                total += estimate
            elif status == "settled":
                actual = raw.get("actual_microusd")
                total += int(actual if actual is not None else estimate)
        return total

    @staticmethod
    def _find_by_idempotency_key(
        document: dict[str, Any], idempotency_key: str
    ) -> dict[str, Any] | None:
        for value in (document.get("reservations") or {}).values():
            if isinstance(value, dict) and value.get("idempotency_key") == idempotency_key:
                return value
        return None

    def import_baseline(
        self,
        *,
        import_key: str,
        amount_usd: Decimal | float | int | str,
        evidence: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Import already-incurred spend exactly once for a migrated run.

        New runs start at zero.  A migrated run such as Echo Protocol must call
        this explicitly (using provider receipts when available) so historical
        spend is neither silently forgotten nor charged to unrelated runs.
        """
        key = str(import_key).strip()
        if not key:
            raise ValueError("import_key must be non-empty")
        amount = usd_to_microusd(amount_usd)
        with self._transaction() as document:
            self._validate_document(document)
            imports = document.setdefault("baseline_imports", {})
            if not isinstance(imports, dict):
                raise ProductionBudgetError("baseline_imports must be an object")
            existing = imports.get(key)
            if isinstance(existing, dict):
                if int(existing.get("amount_microusd") or 0) != amount:
                    raise ProductionBudgetError(
                        "baseline import key is already bound to a different amount"
                    )
                return dict(existing)
            committed = self._committed_microusd(document)
            cap = int(document.get("cap_microusd") or 0)
            if committed + amount > cap:
                raise ProductionBudgetError(
                    "baseline import would exceed the production hard cap"
                )
            record = {
                "import_key": key,
                "amount_microusd": amount,
                "amount_usd": microusd_to_usd(amount),
                "evidence": dict(evidence or {}),
                "imported_at": _now(),
            }
            imports[key] = record
            document["baseline_microusd"] = int(document.get("baseline_microusd") or 0) + amount
            return dict(record)

    def reserve(
        self,
        *,
        idempotency_key: str,
        tool_name: str,
        estimated_usd: Decimal | float | int | str,
        provider: str = "",
        model: str = "",
        requested_duration_sec: float | None = None,
    ) -> MediaBudgetDecision:
        key = str(idempotency_key).strip()
        tool = str(tool_name).strip()
        if not key or not tool:
            raise ValueError("idempotency_key and tool_name must be non-empty")
        estimate = usd_to_microusd(estimated_usd)
        duration = None
        if requested_duration_sec is not None:
            try:
                duration = float(requested_duration_sec)
            except (TypeError, ValueError) as exc:
                raise ValueError("requested_duration_sec must be a number") from exc
            if not math.isfinite(duration) or duration <= 0:
                raise ValueError("requested_duration_sec must be finite and > 0")
        with self._transaction() as document:
            self._validate_document(document)
            cap = int(document["cap_microusd"])
            warning_at = int(document["warning_microusd"])
            existing = self._find_by_idempotency_key(document, key)
            if existing is not None:
                reservation = MediaBudgetReservation.from_dict(existing)
                semantic_match = (
                    reservation.tool_name == tool
                    and reservation.provider == str(provider)
                    and reservation.model == str(model)
                    and reservation.estimated_microusd == estimate
                    and reservation.requested_duration_sec == duration
                )
                committed = self._committed_microusd(document)
                if not semantic_match:
                    return MediaBudgetDecision(
                        ok=False,
                        created=False,
                        reservation=reservation,
                        reason="idempotency key is already bound to a different paid call",
                        warning=committed >= warning_at,
                        available_microusd=max(0, cap - committed),
                    )
                return MediaBudgetDecision(
                    ok=reservation.status != "released",
                    created=False,
                    reservation=reservation,
                    reason=(
                        "reservation already exists; automatic resubmission is forbidden"
                        if reservation.status != "reserved"
                        else "reservation already exists"
                    ),
                    warning=committed >= warning_at,
                    available_microusd=max(0, cap - committed),
                )

            committed = self._committed_microusd(document)
            if tool == "generate_video":
                if duration is None:
                    return MediaBudgetDecision(
                        ok=False,
                        created=False,
                        reservation=None,
                        reason="Veo reservation requires requested_duration_sec",
                        warning=committed >= warning_at,
                        available_microusd=max(0, cap - committed),
                    )
                video_rows = [
                    row
                    for row in (document.get("reservations") or {}).values()
                    if isinstance(row, dict)
                    and row.get("tool_name") == "generate_video"
                    and row.get("status") != "released"
                ]
                used_duration = sum(
                    float(row.get("requested_duration_sec") or 0.0) for row in video_rows
                )
                max_calls = int(document.get("veo_max_calls", DEFAULT_VEO_MAX_CALLS))
                max_duration = float(
                    document.get("veo_max_duration_sec", DEFAULT_VEO_MAX_DURATION_SEC)
                )
                if len(video_rows) + 1 > max_calls:
                    return MediaBudgetDecision(
                        ok=False,
                        created=False,
                        reservation=None,
                        reason=f"Veo call limit would be exceeded: {len(video_rows) + 1} > {max_calls}",
                        warning=committed >= warning_at,
                        available_microusd=max(0, cap - committed),
                    )
                if used_duration + duration > max_duration + 1e-9:
                    return MediaBudgetDecision(
                        ok=False,
                        created=False,
                        reservation=None,
                        reason=(
                            f"Veo duration limit would be exceeded: "
                            f"{used_duration + duration:g}s > {max_duration:g}s"
                        ),
                        warning=committed >= warning_at,
                        available_microusd=max(0, cap - committed),
                    )
            projected = committed + estimate
            if projected > cap:
                return MediaBudgetDecision(
                    ok=False,
                    created=False,
                    reservation=None,
                    reason=(
                        f"production paid-media cap would be exceeded: "
                        f"${microusd_to_usd(projected):.6f} > ${microusd_to_usd(cap):.2f}"
                    ),
                    warning=committed >= warning_at,
                    available_microusd=max(0, cap - committed),
                )

            reservation_id = _stable_id("pmr", self.run_id, key)
            request_id = _stable_id("pmq", self.run_id, key)
            raw = {
                "reservation_id": reservation_id,
                "idempotency_key": key,
                "tool_name": tool,
                "provider": str(provider),
                "model": str(model),
                "estimated_microusd": estimate,
                "actual_microusd": None,
                "status": "reserved",
                "request_id": request_id,
                "provider_operation_id": None,
                "result_asset_id": None,
                "error": None,
                "requested_duration_sec": duration,
                "asset_materialization_status": None,
                "asset_path": None,
                "asset_sha256": None,
                "created_at": _now(),
                "updated_at": _now(),
            }
            document["reservations"][reservation_id] = raw
            reservation = MediaBudgetReservation.from_dict(raw)
            return MediaBudgetDecision(
                ok=True,
                created=True,
                reservation=reservation,
                warning=projected >= warning_at,
                available_microusd=max(0, cap - projected),
            )

    def get(self, reservation_id: str) -> MediaBudgetReservation:
        with self._transaction() as document:
            self._validate_document(document)
            raw = (document.get("reservations") or {}).get(str(reservation_id))
            if not isinstance(raw, dict):
                raise KeyError(f"unknown production budget reservation: {reservation_id!r}")
            return MediaBudgetReservation.from_dict(raw)

    def claim_submission(self, reservation_id: str, *, request_id: str | None = None) -> bool:
        """Atomically move ``reserved -> submitted``; exactly one caller wins."""
        with self._transaction() as document:
            self._validate_document(document)
            raw = (document.get("reservations") or {}).get(str(reservation_id))
            if not isinstance(raw, dict):
                raise KeyError(f"unknown production budget reservation: {reservation_id!r}")
            if str(raw.get("status") or "") != "reserved":
                return False
            expected_request_id = str(raw.get("request_id") or "")
            if request_id is not None and str(request_id) != expected_request_id:
                raise ProductionBudgetError("request_id does not match its reservation")
            raw["status"] = "submitted"
            raw["submitted_at"] = _now()
            raw["updated_at"] = _now()
            return True

    def attach_provider_operation(self, reservation_id: str, operation_id: str) -> None:
        with self._transaction() as document:
            self._validate_document(document)
            raw = (document.get("reservations") or {}).get(str(reservation_id))
            if not isinstance(raw, dict):
                raise KeyError(f"unknown production budget reservation: {reservation_id!r}")
            if str(raw.get("status") or "") not in {"submitted", "uncertain", "settled"}:
                raise ProductionBudgetError(
                    f"cannot attach a provider operation while status={raw.get('status')!r}"
                )
            raw["provider_operation_id"] = str(operation_id)
            raw["updated_at"] = _now()

    def settle(
        self,
        reservation_id: str,
        *,
        actual_usd: Decimal | float | int | str | None = None,
        result_asset_id: str | None = None,
        provider_operation_id: str | None = None,
    ) -> MediaBudgetReservation:
        with self._transaction() as document:
            self._validate_document(document)
            raw = (document.get("reservations") or {}).get(str(reservation_id))
            if not isinstance(raw, dict):
                raise KeyError(f"unknown production budget reservation: {reservation_id!r}")
            status = str(raw.get("status") or "")
            if status == "released":
                raise ProductionBudgetError("a released reservation cannot be settled")
            if status == "settled":
                return MediaBudgetReservation.from_dict(raw)
            actual = (
                usd_to_microusd(actual_usd)
                if actual_usd is not None
                else int(raw.get("estimated_microusd") or 0)
            )
            # The hard cap must also hold if a provider reports an over-estimate.
            old_status = raw.get("status")
            old_actual = raw.get("actual_microusd")
            raw["status"] = "settled"
            raw["actual_microusd"] = actual
            committed = self._committed_microusd(document)
            if committed > int(document["cap_microusd"]):
                raw["status"] = old_status
                raw["actual_microusd"] = old_actual
                raise ProductionBudgetError(
                    "reported actual cost would exceed the production hard cap"
                )
            if result_asset_id:
                raw["result_asset_id"] = str(result_asset_id)
                raw["asset_materialization_status"] = "pending"
            if provider_operation_id:
                raw["provider_operation_id"] = str(provider_operation_id)
            raw["settled_at"] = _now()
            raw["updated_at"] = _now()
            return MediaBudgetReservation.from_dict(raw)

    def mark_asset_materialized(
        self,
        reservation_id: str,
        *,
        result_asset_id: str,
        asset_path: str | Path,
        asset_sha256: str,
    ) -> MediaBudgetReservation:
        """Close the charged→local-asset crash window with durable evidence."""
        path = Path(asset_path).expanduser().resolve()
        if not path.is_file():
            raise ProductionBudgetError(f"materialized asset is missing: {path}")
        digest = str(asset_sha256).strip()
        if not digest:
            raise ProductionBudgetError("materialized asset requires a sha256")
        with self._transaction() as document:
            self._validate_document(document)
            raw = (document.get("reservations") or {}).get(str(reservation_id))
            if not isinstance(raw, dict):
                raise KeyError(f"unknown production budget reservation: {reservation_id!r}")
            if str(raw.get("status") or "") != "settled":
                raise ProductionBudgetError("only a settled paid call can materialize an asset")
            existing_asset = str(raw.get("result_asset_id") or "")
            if existing_asset and existing_asset != str(result_asset_id):
                raise ProductionBudgetError("result asset id does not match the settled receipt")
            raw["result_asset_id"] = str(result_asset_id)
            raw["asset_materialization_status"] = "materialized"
            raw["asset_path"] = str(path)
            raw["asset_sha256"] = digest
            raw["materialized_at"] = _now()
            raw["updated_at"] = _now()
            return MediaBudgetReservation.from_dict(raw)

    def mark_uncertain(self, reservation_id: str, *, error: str = "") -> MediaBudgetReservation:
        with self._transaction() as document:
            self._validate_document(document)
            raw = (document.get("reservations") or {}).get(str(reservation_id))
            if not isinstance(raw, dict):
                raise KeyError(f"unknown production budget reservation: {reservation_id!r}")
            status = str(raw.get("status") or "")
            if status in _FINAL_STATUSES:
                return MediaBudgetReservation.from_dict(raw)
            raw["status"] = "uncertain"
            raw["error"] = str(error)[:1000] or None
            raw["uncertain_at"] = _now()
            raw["updated_at"] = _now()
            return MediaBudgetReservation.from_dict(raw)

    def release(self, reservation_id: str, *, reason: str = "") -> MediaBudgetReservation:
        """Release only work that was definitely never submitted to a provider."""
        with self._transaction() as document:
            self._validate_document(document)
            raw = (document.get("reservations") or {}).get(str(reservation_id))
            if not isinstance(raw, dict):
                raise KeyError(f"unknown production budget reservation: {reservation_id!r}")
            status = str(raw.get("status") or "")
            if status == "released":
                return MediaBudgetReservation.from_dict(raw)
            if status != "reserved":
                raise ProductionBudgetError(
                    "submitted or uncertain paid work cannot be released automatically"
                )
            raw["status"] = "released"
            raw["error"] = str(reason)[:1000] or None
            raw["released_at"] = _now()
            raw["updated_at"] = _now()
            return MediaBudgetReservation.from_dict(raw)

    def reconcile_orphaned_reservation(
        self,
        reservation_id: str,
        *,
        tool_call_id: str,
        owner_id: str = "",
        reason: str = "execution owner exited before committing its receipt",
    ) -> dict[str, Any]:
        """Apply the crash policy for an orphaned formal tool receipt.

        ``reserved`` is durable proof that the mandatory provider-submission
        claim never happened, so that estimate can be released.  Once the
        ledger says ``submitted`` (or later), the amount stays committed:
        resume cannot know whether the provider accepted the request.  Every
        decision is appended to the reservation itself so a crash between the
        ledger update and tool-receipt update remains idempotent and auditable.
        """

        receipt_id = str(tool_call_id or "").strip()
        if not receipt_id:
            raise ProductionBudgetError(
                "orphan reconciliation requires a tool_call_id"
            )
        reservation_key = str(reservation_id or "").strip()
        audit_id = _stable_id(
            "pmrec", self.run_id, reservation_key, receipt_id
        )
        with self._transaction() as document:
            self._validate_document(document)
            raw = (document.get("reservations") or {}).get(reservation_key)
            if not isinstance(raw, dict):
                raise KeyError(
                    f"unknown production budget reservation: {reservation_id!r}"
                )
            history = raw.setdefault("reconciliation_history", [])
            if not isinstance(history, list):
                raise ProductionBudgetError(
                    "reservation reconciliation_history must be an array"
                )
            for existing in history:
                if isinstance(existing, dict) and existing.get("audit_id") == audit_id:
                    reservation = MediaBudgetReservation.from_dict(raw)
                    return {
                        "action": str(existing.get("action") or ""),
                        "audit": dict(existing),
                        "reservation": {
                            **reservation.__dict__,
                            "estimated_usd": reservation.estimated_usd,
                            "actual_usd": reservation.actual_usd,
                        },
                    }

            before = str(raw.get("status") or "")
            now = _now()
            detail = str(reason or "")[:1000]
            if before == "reserved":
                action = "released_unsubmitted"
                raw["status"] = "released"
                raw["error"] = detail or None
                raw["released_at"] = now
            elif before == "submitted":
                action = "retained_estimate_uncertain"
                raw["status"] = "uncertain"
                raw["error"] = detail or raw.get("error")
                raw["uncertain_at"] = now
            elif before == "uncertain":
                action = "preserved_uncertain"
            elif before == "settled":
                action = "preserved_settled"
            elif before == "released":
                action = "preserved_released"
            else:
                # Unknown legacy states are never credited back automatically.
                action = "retained_estimate_unknown"
                raw["status"] = "uncertain"
                raw["error"] = detail or raw.get("error")
                raw["uncertain_at"] = now
            audit = {
                "audit_id": audit_id,
                "action": action,
                "tool_call_id": receipt_id,
                "owner_id": str(owner_id or ""),
                "status_before": before,
                "status_after": str(raw.get("status") or ""),
                "reason": detail,
                "reconciled_at": now,
            }
            history.append(audit)
            raw["last_reconciliation"] = dict(audit)
            raw["updated_at"] = now
            reservation = MediaBudgetReservation.from_dict(raw)
            return {
                "action": action,
                "audit": dict(audit),
                "reservation": {
                    **reservation.__dict__,
                    "estimated_usd": reservation.estimated_usd,
                    "actual_usd": reservation.actual_usd,
                },
            }

    def call_context(self, reservation_id: str) -> PaidMediaCall:
        reservation = self.get(reservation_id)
        return PaidMediaCall(
            ledger_path=str(self.path),
            run_id=self.run_id,
            reservation_id=reservation.reservation_id,
            request_id=reservation.request_id,
            idempotency_key=reservation.idempotency_key,
            estimated_microusd=reservation.estimated_microusd,
            requested_duration_sec=reservation.requested_duration_sec,
        )

    def snapshot(self) -> dict[str, Any]:
        with self._transaction() as document:
            self._validate_document(document)
            committed = self._committed_microusd(document)
            cap = int(document["cap_microusd"])
            warning_at = int(document["warning_microusd"])
            baseline = int(document.get("baseline_microusd") or 0)
            spent = baseline
            reserved = 0
            counts: dict[str, int] = {}
            for raw in (document.get("reservations") or {}).values():
                if isinstance(raw, dict):
                    status = str(raw.get("status") or "unknown")
                    counts[status] = counts.get(status, 0) + 1
                    estimate = int(raw.get("estimated_microusd") or 0)
                    if status in _ACTIVE_STATUSES:
                        reserved += estimate
                    elif status == "settled":
                        actual = raw.get("actual_microusd")
                        spent += int(actual if actual is not None else estimate)
            reconciliation_blockers = [
                {
                    "code": "charged_asset_missing",
                    "reservation_id": str(raw.get("reservation_id") or ""),
                    "request_id": str(raw.get("request_id") or ""),
                    "result_asset_id": str(raw.get("result_asset_id") or ""),
                }
                for raw in (document.get("reservations") or {}).values()
                if isinstance(raw, dict)
                and raw.get("status") == "settled"
                and raw.get("result_asset_id")
                and raw.get("asset_materialization_status") != "materialized"
            ]
            billed_keys: list[str] = []
            for raw in (document.get("reservations") or {}).values():
                if not isinstance(raw, dict) or raw.get("status") == "released":
                    continue
                # A provider operation is the strongest billing identity. A
                # deterministic request id remains the fail-closed fallback
                # before a long-running operation id is attached.
                identity = str(
                    raw.get("provider_operation_id") or raw.get("request_id") or ""
                )
                if identity:
                    billed_keys.append(identity)
            duplicate_billing_count = len(billed_keys) - len(set(billed_keys))
            tool_reserved_calls: dict[str, int] = {}
            for row in (document.get("reservations") or {}).values():
                if not isinstance(row, dict) or row.get("status") == "released":
                    continue
                tool = str(row.get("tool_name") or "")
                if tool:
                    tool_reserved_calls[tool] = tool_reserved_calls.get(tool, 0) + 1
            return {
                "run_id": self.run_id,
                "ledger_path": str(self.path),
                "pricing_version": document.get("pricing_version"),
                "cap_usd": microusd_to_usd(cap),
                "warning_usd": microusd_to_usd(warning_at),
                "baseline_spend_usd": microusd_to_usd(baseline),
                "committed_usd": microusd_to_usd(committed),
                "available_usd": microusd_to_usd(max(0, cap - committed)),
                "limit_usd": microusd_to_usd(cap),
                "spent_usd": microusd_to_usd(spent),
                "reserved_usd": microusd_to_usd(reserved),
                "remaining_usd": microusd_to_usd(max(0, cap - spent - reserved)),
                "over_cap": spent + reserved > cap,
                "warning": committed >= warning_at,
                "blocked": committed >= cap,
                "reservation_counts": counts,
                "reconciliation_blockers": reconciliation_blockers,
                "duplicate_billing_count": duplicate_billing_count,
                "tool_reserved_calls": tool_reserved_calls,
                "veo_max_calls": int(document.get("veo_max_calls", DEFAULT_VEO_MAX_CALLS)),
                "veo_max_duration_sec": float(
                    document.get("veo_max_duration_sec", DEFAULT_VEO_MAX_DURATION_SEC)
                ),
                "veo_reserved_calls": sum(
                    1
                    for row in (document.get("reservations") or {}).values()
                    if isinstance(row, dict)
                    and row.get("tool_name") == "generate_video"
                    and row.get("status") != "released"
                ),
                "veo_reserved_duration_sec": round(
                    sum(
                        float(row.get("requested_duration_sec") or 0.0)
                        for row in (document.get("reservations") or {}).values()
                        if isinstance(row, dict)
                        and row.get("tool_name") == "generate_video"
                        and row.get("status") != "released"
                    ),
                    6,
                ),
            }


def paid_media_call_from_extra(extra: dict[str, Any] | None) -> PaidMediaCall | None:
    if not isinstance(extra, dict):
        return None
    raw = extra.get(PAID_MEDIA_CONTEXT_KEY)
    if raw is None:
        return None
    if isinstance(raw, PaidMediaCall):
        return raw
    if not isinstance(raw, dict):
        raise ProductionBudgetError("paid_media_call context must be an object")
    return PaidMediaCall.from_dict(raw)


def claim_paid_media_call(extra: dict[str, Any] | None) -> tuple[PaidMediaCall, ProductionMediaBudget] | None:
    """Claim the single automatic provider submission represented by ``extra``."""
    call = paid_media_call_from_extra(extra)
    if call is None:
        return None
    ledger = ProductionMediaBudget.open(call.ledger_path)
    if ledger.run_id != call.run_id:
        raise ProductionBudgetError("paid-media call run_id does not match its ledger")
    if not ledger.claim_submission(call.reservation_id, request_id=call.request_id):
        status = ledger.get(call.reservation_id).status
        raise PaidMediaSubmissionClaimedError(
            f"paid-media reservation {call.reservation_id} is already {status}; "
            "automatic resubmission is forbidden"
        )
    return call, ledger


__all__ = [
    "DEFAULT_CAP_USD",
    "DEFAULT_WARNING_USD",
    "DEFAULT_BASELINE_SPEND_USD",
    "DEFAULT_VEO_MAX_CALLS",
    "DEFAULT_VEO_MAX_DURATION_SEC",
    "DEFAULT_PRICING_VERSION",
    "LEDGER_SCHEMA_VERSION",
    "PAID_MEDIA_CONTEXT_KEY",
    "ProductionBudgetError",
    "PaidMediaSubmissionClaimedError",
    "MediaBudgetReservation",
    "MediaBudgetDecision",
    "PaidMediaCall",
    "ProductionMediaBudget",
    "paid_media_call_from_extra",
    "claim_paid_media_call",
    "usd_to_microusd",
    "microusd_to_usd",
]
