#!/usr/bin/env python3
"""Render and verify the canonical 120-second Echo Protocol V1 delivery.

This is a formal host operator, not a second renderer.  It resumes the durable
production session and invokes the installed ``inspect_timeline``,
``project_export`` and ``verify_delivery`` capabilities through
``SessionRunner.run_production_verb``.  Tool idempotency is bound to the exact
project revision and an explicit attempt number so a process restart can
reconcile completed work without rendering or charging twice.

The render command stops in ``verifying``.  The verify command consumes a
structured visual review and may move the run only to ``ready_for_review`` (or
the verifier's fail-closed ``revising`` state).  Human acceptance is never
performed here.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import subprocess
import sys
from typing import Any, Mapping

# A direct invocation must resolve imports from this isolated checkout, not a
# different editable Lumeri installation on the same machine.
REPO_ROOT = Path(__file__).resolve().parents[1]
try:
    sys.path.remove(str(REPO_ROOT))
except ValueError:
    pass
sys.path.insert(0, str(REPO_ROOT))

from gemia.production_acceptance import FORMAL_REQUIRED_REVIEW_CHECKS
from gemia.compat import ffprobe_path
from gemia.render_receipt import (
    CANONICAL_RENDER_SEMANTICS_VERSION,
    canonical_render_semantics,
    file_sha256,
    summarize_probe,
)
from gemia.session_manager import SessionManager
from scripts import build_echo_protocol_v1 as board_builder

SESSION_ID = board_builder.SESSION_ID
RUN_ID = board_builder.RUN_ID
RENDER_OPERATOR_VERSION = "echo-protocol-v1-render-operator-1"
FORMAL_TIMEOUT_SEC = 1800.0
EPSILON = 1e-6


class EchoRenderOperatorError(RuntimeError):
    """The current production revision cannot safely advance."""


def _stable_digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _run_view(manager: Any, runner: Any) -> dict[str, Any]:
    value = manager.get_run(runner.project_id, runner.run_id)
    if not isinstance(value, Mapping):
        raise EchoRenderOperatorError("production run is unreadable")
    return dict(value)


def _state(run: Mapping[str, Any]) -> str:
    return str(run.get("production_state") or run.get("state") or "").strip()


def _project_revision(manager: Any, runner: Any) -> int:
    value = manager.get_project(runner.project_id)
    if not isinstance(value, Mapping):
        raise EchoRenderOperatorError("durable project record is unreadable")
    revision = value.get("project_revision", value.get("revision"))
    try:
        parsed = int(revision)
    except (TypeError, ValueError) as exc:
        raise EchoRenderOperatorError("durable project revision is missing") from exc
    if parsed < 0:
        raise EchoRenderOperatorError("durable project revision cannot be negative")
    return parsed


def _budget(run: Mapping[str, Any]) -> dict[str, Any]:
    value = run.get("budget")
    if not isinstance(value, Mapping):
        raise EchoRenderOperatorError("production run has no canonical budget view")
    return dict(value)


def _assert_no_veo(budget: Mapping[str, Any]) -> None:
    calls = int(budget.get("veo_reserved_calls") or 0)
    duration = float(budget.get("veo_reserved_duration_sec") or 0.0)
    if calls != 0 or abs(duration) > EPSILON:
        raise EchoRenderOperatorError(
            f"Echo V1 render operator forbids Veo: calls={calls}, duration={duration}"
        )


def _assert_budget_unchanged(
    before: Mapping[str, Any], after: Mapping[str, Any]
) -> None:
    _assert_no_veo(after)
    if _stable_digest(dict(before)) != _stable_digest(dict(after)):
        raise EchoRenderOperatorError(
            "zero-cost render/verification operator changed the media budget"
        )


def _attempt(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise EchoRenderOperatorError("attempt must be a positive integer") from exc
    if parsed < 1 or parsed > 999:
        raise EchoRenderOperatorError("attempt must be between 1 and 999")
    return parsed


def _call_identity(
    *, project_revision: int, attempt: int, operation: str
) -> tuple[str, str]:
    stem = f"echo-v1-r{project_revision}-a{attempt}-{operation}"
    return f"trace-{stem}", stem


def _registry(runner: Any) -> Any:
    registry = getattr(getattr(runner, "agent", None), "registry", None)
    if registry is None:
        registry = getattr(runner, "registry", None)
    if registry is None or not callable(getattr(registry, "get", None)):
        raise EchoRenderOperatorError(
            "runner does not expose its durable asset registry"
        )
    return registry


def _is_tmp_path(value: Any) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    try:
        resolved = Path(text).expanduser().resolve(strict=False)
    except (OSError, RuntimeError):
        return True
    return (
        resolved == Path("/tmp")
        or resolved == Path("/private/tmp")
        or any(
            root in resolved.parents for root in (Path("/tmp"), Path("/private/tmp"))
        )
    )


def _asset_record(
    runner: Any,
    asset_id: str,
    *,
    kind: str,
    role: str,
) -> Any:
    if not str(asset_id or "").strip():
        raise EchoRenderOperatorError(f"{role} asset id is missing")
    try:
        record = _registry(runner).get(str(asset_id))
    except KeyError as exc:
        raise EchoRenderOperatorError(
            f"{role} asset is absent from the durable registry: {asset_id}"
        ) from exc
    if str(getattr(record, "kind", "")) != kind:
        raise EchoRenderOperatorError(
            f"{role} asset must be {kind}, got {getattr(record, 'kind', None)!r}"
        )
    path = Path(getattr(record, "path", "")).expanduser().resolve(strict=False)
    if _is_tmp_path(path):
        raise EchoRenderOperatorError(f"{role} asset cannot live under /tmp: {path}")
    if not path.is_file():
        raise EchoRenderOperatorError(f"{role} asset file is missing: {path}")
    return record


def _receipt(
    result: Mapping[str, Any],
    *,
    role: str,
    project_revision: int,
) -> dict[str, Any]:
    value = result.get("render_receipt")
    if not isinstance(value, Mapping):
        raise EchoRenderOperatorError(f"{role} has no canonical RenderReceipt")
    receipt = dict(value)
    if int(receipt.get("project_revision") or -1) != int(project_revision):
        raise EchoRenderOperatorError(
            f"{role} receipt is stale: expected revision {project_revision}, "
            f"got {receipt.get('project_revision')!r}"
        )
    graph_hash = str(receipt.get("graph_hash") or "")
    if not graph_hash or graph_hash != str(result.get("graph_hash") or ""):
        raise EchoRenderOperatorError(
            f"{role} graph identity is missing or inconsistent"
        )
    dropped = list(result.get("dropped_fields") or [])
    receipt_dropped = list(receipt.get("dropped_fields") or [])
    if dropped or receipt_dropped:
        raise EchoRenderOperatorError(
            f"{role} dropped canonical fields: {dropped or receipt_dropped}"
        )
    return receipt


def _validate_inspection(
    runner: Any,
    result: Mapping[str, Any],
    *,
    project_revision: int,
) -> dict[str, Any]:
    receipt = _receipt(
        result,
        role="timeline inspection",
        project_revision=project_revision,
    )
    preview_id = str(result.get("preview_asset_id") or "")
    preview = _asset_record(
        runner, preview_id, kind="video", role="timeline inspection preview"
    )
    preview_source = getattr(preview, "source", {})
    if not isinstance(preview_source, Mapping):
        preview_source = {}
    if (
        str(preview_source.get("graph_hash") or "") != receipt["graph_hash"]
        or int(preview_source.get("project_revision") or -1) != project_revision
    ):
        raise EchoRenderOperatorError("timeline inspection preview is stale")

    frame_ids = [str(value) for value in (result.get("frame_asset_ids") or [])]
    if len(frame_ids) != 12 or len(set(frame_ids)) != 12:
        raise EchoRenderOperatorError(
            "formal render inspection requires exactly 12 distinct frame assets"
        )
    for asset_id in frame_ids:
        frame = _asset_record(runner, asset_id, kind="image", role="inspection frame")
        source = getattr(frame, "source", {})
        if not isinstance(source, Mapping):
            source = {}
        if (
            str(source.get("graph_hash") or "") != receipt["graph_hash"]
            or int(source.get("project_revision") or -1) != project_revision
        ):
            raise EchoRenderOperatorError(
                f"inspection frame is stale or belongs to another graph: {asset_id}"
            )

    contact_sheet_id = str(result.get("contact_sheet_asset_id") or "")
    if contact_sheet_id:
        contact = _asset_record(
            runner, contact_sheet_id, kind="image", role="inspection contact sheet"
        )
        source = getattr(contact, "source", {})
        if not isinstance(source, Mapping):
            source = {}
        if (
            str(source.get("graph_hash") or "") != receipt["graph_hash"]
            or int(source.get("project_revision") or -1) != project_revision
        ):
            raise EchoRenderOperatorError("inspection contact sheet is stale")

    return {
        "receipt": receipt,
        "preview_asset_id": preview_id,
        "frame_asset_ids": frame_ids,
        "contact_sheet_asset_id": contact_sheet_id or None,
        "preview_path": str(getattr(preview, "path")),
    }


def _finite_float(value: Any, *, name: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise EchoRenderOperatorError(f"final export {name} is missing") from exc
    if not math.isfinite(parsed):
        raise EchoRenderOperatorError(f"final export {name} is not finite")
    return parsed


def _validate_export(
    runner: Any,
    result: Mapping[str, Any],
    *,
    project_revision: int,
    expected_graph_hash: str,
) -> dict[str, Any]:
    receipt = _receipt(result, role="final export", project_revision=project_revision)
    if str(receipt.get("graph_hash") or "") != str(expected_graph_hash or ""):
        raise EchoRenderOperatorError(
            "preview/export canonical graph hash mismatch; refusing delivery"
        )
    if (
        str(result.get("machine_status") or "") != "passed"
        or str(receipt.get("machine_status") or "") != "passed"
    ):
        raise EchoRenderOperatorError(
            "final export exists but its machine delivery gate did not pass"
        )
    if list(result.get("machine_blockers") or []) or list(
        receipt.get("machine_blockers") or []
    ):
        raise EchoRenderOperatorError("final export has machine blockers")

    duration = _finite_float(result.get("duration"), name="duration")
    if not 119.5 <= duration <= 120.5:
        raise EchoRenderOperatorError(
            f"final export duration {duration:.6f}s is outside 119.5..120.5s"
        )
    try:
        width = int(result.get("width") or 0)
        height = int(result.get("height") or 0)
    except (TypeError, ValueError) as exc:
        raise EchoRenderOperatorError("final export dimensions are invalid") from exc
    if (width, height) != (1920, 1080):
        raise EchoRenderOperatorError(
            f"final export must be 1920x1080, got {width}x{height}"
        )
    if result.get("has_audio") is not True:
        raise EchoRenderOperatorError("final export has no rendered audio track")

    export_id = str(result.get("asset_id") or "")
    export_record = _asset_record(runner, export_id, kind="video", role="final export")
    export_path = (
        Path(str(result.get("export_path") or "")).expanduser().resolve(strict=False)
    )
    record_path = (
        Path(getattr(export_record, "path", "")).expanduser().resolve(strict=False)
    )
    receipt_path = (
        Path(str(receipt.get("output_path") or "")).expanduser().resolve(strict=False)
    )
    if not str(result.get("export_path") or "") or _is_tmp_path(export_path):
        raise EchoRenderOperatorError("final export path is missing or under /tmp")
    if export_path != record_path or export_path != receipt_path:
        raise EchoRenderOperatorError(
            "final export result, registry and RenderReceipt point to different files"
        )
    if not export_path.is_file():
        raise EchoRenderOperatorError(f"final export file is missing: {export_path}")
    return {
        "receipt": receipt,
        "export_asset_id": export_id,
        "export_path": str(export_path),
        "duration": duration,
        "width": width,
        "height": height,
        "has_audio": True,
    }


def _render_evidence_payload(
    *,
    project_revision: int,
    attempt: int,
    inspection_result: Mapping[str, Any],
    inspection: Mapping[str, Any],
    export_result: Mapping[str, Any],
    final_export: Mapping[str, Any],
    budget: Mapping[str, Any],
) -> dict[str, Any]:
    inspect_receipt = dict(inspection["receipt"])
    export_receipt = dict(final_export["receipt"])
    return {
        "render_operator_version": RENDER_OPERATOR_VERSION,
        "attempt": attempt,
        "project_revision": project_revision,
        "graph_hash": export_receipt["graph_hash"],
        "preview": {
            "tool_call_id": str(inspection_result.get("production_tool_call_id") or ""),
            "render_id": str(inspect_receipt.get("render_id") or ""),
            "asset_id": inspection["preview_asset_id"],
            "path": inspection["preview_path"],
            "frame_asset_ids": list(inspection["frame_asset_ids"]),
            "contact_sheet_asset_id": inspection["contact_sheet_asset_id"],
            "project_revision": int(inspect_receipt["project_revision"]),
            "graph_hash": inspect_receipt["graph_hash"],
            "dropped_fields": [],
        },
        "export": {
            "tool_call_id": str(export_result.get("production_tool_call_id") or ""),
            "render_id": str(export_receipt.get("render_id") or ""),
            "asset_id": final_export["export_asset_id"],
            "path": final_export["export_path"],
            "project_revision": int(export_receipt["project_revision"]),
            "graph_hash": export_receipt["graph_hash"],
            "machine_status": "passed",
            "duration": final_export["duration"],
            "width": final_export["width"],
            "height": final_export["height"],
            "has_audio": True,
            "dropped_fields": [],
        },
        "checks": {
            "inspect_before_export": True,
            "twelve_distinct_frames": True,
            "preview_export_graph_parity": True,
            "revision_bound": True,
            "machine_status_passed": True,
            "no_dropped_fields": True,
            "no_tmp_outputs": True,
            "budget_unchanged": True,
            "ai_video_generation_calls": 0,
        },
        "budget_before": dict(budget),
        "budget_after": dict(budget),
    }


def execute_render(
    manager: Any,
    runner: Any,
    *,
    attempt: int = 1,
    timeout_sec: float = FORMAL_TIMEOUT_SEC,
) -> dict[str, Any]:
    """Render/reconcile one exact project revision, then enter verifying."""

    attempt = _attempt(attempt)
    timeout = float(timeout_sec)
    if timeout < FORMAL_TIMEOUT_SEC:
        raise EchoRenderOperatorError(
            f"formal render timeout must be at least {FORMAL_TIMEOUT_SEC:.0f}s"
        )
    run_before = _run_view(manager, runner)
    state_before = _state(run_before)
    if state_before not in {"rendering", "verifying"}:
        raise EchoRenderOperatorError(
            f"render operator requires rendering/verifying state, got {state_before!r}"
        )
    budget_before = _budget(run_before)
    _assert_no_veo(budget_before)
    project_revision = _project_revision(manager, runner)
    run_bound_revision = int(run_before.get("project_revision") or project_revision)
    if run_bound_revision != project_revision:
        raise EchoRenderOperatorError(
            "production run is not synchronized to the current project revision"
        )

    inspect_trace, inspect_key = _call_identity(
        project_revision=project_revision,
        attempt=attempt,
        operation="inspect",
    )
    inspection_result = runner.run_production_verb(
        "inspect_timeline",
        {
            "start_sec": 0.0,
            "end_sec": 120.0,
            "max_frames": 12,
            "label": f"echo-v1-r{project_revision}-a{attempt}-inspect",
        },
        trace_id=inspect_trace,
        idempotency_key=inspect_key,
        timeout=timeout,
    )
    if state_before == "verifying" and not bool(
        inspection_result.get("production_duplicate")
    ):
        raise EchoRenderOperatorError(
            "verifying recovery requires the completed revision-bound inspection receipt"
        )
    inspection = _validate_inspection(
        runner,
        inspection_result,
        project_revision=project_revision,
    )
    after_inspect = _run_view(manager, runner)
    _assert_budget_unchanged(budget_before, _budget(after_inspect))
    if _project_revision(manager, runner) != project_revision:
        raise EchoRenderOperatorError(
            "project revision changed during timeline inspection"
        )

    export_trace, export_key = _call_identity(
        project_revision=project_revision,
        attempt=attempt,
        operation="export",
    )
    export_result = runner.run_production_verb(
        "project_export",
        {
            "quality": "1080p",
            "label": f"echo-v1-r{project_revision}-a{attempt}-final",
        },
        trace_id=export_trace,
        idempotency_key=export_key,
        timeout=timeout,
    )
    if state_before == "verifying" and not bool(
        export_result.get("production_duplicate")
    ):
        raise EchoRenderOperatorError(
            "verifying recovery requires the completed revision-bound export receipt"
        )
    final_export = _validate_export(
        runner,
        export_result,
        project_revision=project_revision,
        expected_graph_hash=str(inspection["receipt"]["graph_hash"]),
    )
    run_after_render = _run_view(manager, runner)
    budget_after_render = _budget(run_after_render)
    _assert_budget_unchanged(budget_before, budget_after_render)
    if _project_revision(manager, runner) != project_revision:
        raise EchoRenderOperatorError("project revision changed during final export")

    evidence_id = f"ev-echo-render-v1-r{project_revision}-a{attempt}"
    evidence_trace = f"trace-{evidence_id}"
    payload = _render_evidence_payload(
        project_revision=project_revision,
        attempt=attempt,
        inspection_result=inspection_result,
        inspection=inspection,
        export_result=export_result,
        final_export=final_export,
        budget=budget_before,
    )
    manager.record_evidence(
        runner.project_id,
        runner.run_id,
        evidence_id=evidence_id,
        kind="canonical_render",
        project_revision=project_revision,
        trace_id=evidence_trace,
        payload=payload,
    )

    latest = _run_view(manager, runner)
    latest_state = _state(latest)
    transitioned = False
    if latest_state == "rendering":
        manager.transition_run(
            runner.project_id,
            runner.run_id,
            "verifying",
            expected_revision=int(
                latest.get("production_revision") or latest.get("revision") or 0
            ),
            trace_id=f"trace-echo-render-v1-r{project_revision}-a{attempt}-complete",
        )
        transitioned = True
    elif latest_state != "verifying":
        raise EchoRenderOperatorError(
            f"render evidence cannot advance unexpected state {latest_state!r}"
        )

    final_run = _run_view(manager, runner)
    final_budget = _budget(final_run)
    _assert_budget_unchanged(budget_before, final_budget)
    if _state(final_run) != "verifying":
        raise EchoRenderOperatorError("render operator did not stop in verifying")
    return {
        "ok": True,
        "replayed": bool(
            inspection_result.get("production_duplicate")
            and export_result.get("production_duplicate")
        ),
        "production_state": "verifying",
        "project_revision": project_revision,
        "attempt": attempt,
        "evidence_id": evidence_id,
        "transitioned": transitioned,
        "graph_hash": final_export["receipt"]["graph_hash"],
        "preview_asset_id": inspection["preview_asset_id"],
        "inspection_asset_ids": list(inspection["frame_asset_ids"]),
        "contact_sheet_asset_id": inspection["contact_sheet_asset_id"],
        "export_asset_id": final_export["export_asset_id"],
        "export_path": final_export["export_path"],
        "machine_status": "passed",
        "duration": final_export["duration"],
        "width": final_export["width"],
        "height": final_export["height"],
        "has_audio": True,
        "dropped_fields": [],
        "budget": final_budget,
        "veo_calls": 0,
    }


def _review_payload(
    review_checks: Mapping[str, Any], review_notes: Mapping[str, Any]
) -> tuple[dict[str, bool], dict[str, str]]:
    if not isinstance(review_checks, Mapping) or not isinstance(review_notes, Mapping):
        raise EchoRenderOperatorError("review_checks and review_notes must be objects")
    checks: dict[str, bool] = {}
    notes: dict[str, str] = {}
    for name in FORMAL_REQUIRED_REVIEW_CHECKS:
        value = review_checks.get(name)
        note = str(review_notes.get(name) or "").strip()
        if type(value) is not bool or not note:
            raise EchoRenderOperatorError(
                f"review {name!r} requires a boolean result and a concrete note"
            )
        checks[name] = value
        notes[name] = note[:1000]
    return checks, notes


def execute_verify(
    manager: Any,
    runner: Any,
    render_result: Mapping[str, Any],
    *,
    review_checks: Mapping[str, Any],
    review_notes: Mapping[str, Any],
    attempt: int = 1,
    timeout_sec: float = FORMAL_TIMEOUT_SEC,
) -> dict[str, Any]:
    """Apply structured QA to a completed render without human acceptance."""

    attempt = _attempt(attempt)
    timeout = float(timeout_sec)
    if timeout < FORMAL_TIMEOUT_SEC:
        raise EchoRenderOperatorError(
            f"formal verification timeout must be at least {FORMAL_TIMEOUT_SEC:.0f}s"
        )
    if not isinstance(render_result, Mapping):
        raise EchoRenderOperatorError("render_result must be an object")
    checks, notes = _review_payload(review_checks, review_notes)
    run_before = _run_view(manager, runner)
    state_before = _state(run_before)
    if state_before not in {"verifying", "ready_for_review"}:
        raise EchoRenderOperatorError(
            f"delivery verification requires verifying/ready_for_review state, got {state_before!r}"
        )
    budget_before = _budget(run_before)
    _assert_no_veo(budget_before)
    project_revision = _project_revision(manager, runner)
    if int(render_result.get("project_revision") or -1) != project_revision:
        raise EchoRenderOperatorError(
            "render_result is not bound to the current project revision"
        )
    if str(render_result.get("machine_status") or "") != "passed":
        raise EchoRenderOperatorError(
            "render_result did not pass the machine render gate"
        )
    if list(render_result.get("dropped_fields") or []):
        raise EchoRenderOperatorError("render_result contains dropped fields")

    preview_id = str(render_result.get("preview_asset_id") or "")
    export_id = str(render_result.get("export_asset_id") or "")
    frame_ids = [
        str(value) for value in (render_result.get("inspection_asset_ids") or [])
    ]
    if len(frame_ids) != 12 or len(set(frame_ids)) != 12:
        raise EchoRenderOperatorError("render_result must contain 12 distinct frames")
    _asset_record(runner, preview_id, kind="video", role="verification preview")
    _asset_record(runner, export_id, kind="video", role="verification export")
    for asset_id in frame_ids:
        _asset_record(runner, asset_id, kind="image", role="verification frame")

    trace_id, idempotency_key = _call_identity(
        project_revision=project_revision,
        attempt=attempt,
        operation=f"verify-s{CANONICAL_RENDER_SEMANTICS_VERSION}",
    )
    result = runner.run_production_verb(
        "verify_delivery",
        {
            "export_asset_id": export_id,
            "preview_asset_id": preview_id,
            "inspection_asset_ids": frame_ids,
            "review_checks": checks,
            "review_notes": notes,
        },
        trace_id=trace_id,
        idempotency_key=idempotency_key,
        timeout=timeout,
    )
    budget_after = _budget(_run_view(manager, runner))
    _assert_budget_unchanged(budget_before, budget_after)
    state_after = _state(_run_view(manager, runner))
    reported_state = str(result.get("production_state") or state_after)
    if state_after == "accepted" or reported_state == "accepted":
        raise EchoRenderOperatorError(
            "machine verification must never perform human acceptance"
        )
    if result.get("status") == "failed" or reported_state != "ready_for_review":
        raise EchoRenderOperatorError(
            f"delivery verification did not reach ready_for_review: {reported_state!r}"
        )
    if state_after != "ready_for_review":
        raise EchoRenderOperatorError(
            f"durable run disagrees with verification result: {state_after!r}"
        )
    return {
        **dict(result),
        "ok": True,
        "production_state": "ready_for_review",
        "project_revision": project_revision,
        "human_review_required": True,
        "accepted": False,
        "budget": budget_after,
        "veo_calls": 0,
    }


def _probe_delivery_path(path: Path) -> dict[str, Any]:
    try:
        proc = subprocess.run(
            [
                ffprobe_path(),
                "-v",
                "error",
                "-show_format",
                "-show_streams",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise EchoRenderOperatorError(f"cannot probe failed delivery: {path}") from exc
    if proc.returncode != 0:
        raise EchoRenderOperatorError(
            f"failed delivery probe returned {proc.returncode}: {proc.stderr[-1000:]}"
        )
    try:
        value = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise EchoRenderOperatorError(
            "failed delivery probe returned invalid JSON"
        ) from exc
    return summarize_probe(value if isinstance(value, dict) else {})


def execute_prepare_audio_tail_rerender(
    manager: Any,
    runner: Any,
    render_result: Mapping[str, Any],
    *,
    attempt: int = 1,
    probe_fn: Any = _probe_delivery_path,
    hash_fn: Any = file_sha256,
) -> dict[str, Any]:
    """Record the stale sidechain render defect and reopen the same revision.

    This operator does not patch the creative project. It proves that the
    failed artifact used legacy render semantics and that its audio stream
    ended before picture, records those facts, then advances only
    ``revising -> rendering`` so the same project revision can be exported by
    the corrected canonical renderer.
    """

    attempt = _attempt(attempt)
    run_before = _run_view(manager, runner)
    budget_before = _budget(run_before)
    _assert_no_veo(budget_before)
    project_revision = _project_revision(manager, runner)
    if int(render_result.get("project_revision") or -1) != project_revision:
        raise EchoRenderOperatorError(
            "failed render result is not bound to the current project revision"
        )
    evidence_id = (
        f"ev-echo-audio-tail-remediation-r{project_revision}-a{attempt}-"
        f"s{CANONICAL_RENDER_SEMANTICS_VERSION}"
    )
    state_before = _state(run_before)
    if state_before == "rendering":
        if evidence_id not in {
            str(value) for value in run_before.get("evidence_ids") or []
        }:
            raise EchoRenderOperatorError(
                "rendering state lacks the audio-tail remediation evidence"
            )
        return {
            "ok": True,
            "replayed": True,
            "production_state": "rendering",
            "project_revision": project_revision,
            "evidence_id": evidence_id,
            "budget": budget_before,
            "veo_calls": 0,
        }
    if state_before != "revising":
        raise EchoRenderOperatorError(
            f"audio-tail remediation requires revising state, got {state_before!r}"
        )

    export_id = str(render_result.get("export_asset_id") or "")
    export_record = _asset_record(
        runner, export_id, kind="video", role="failed verification export"
    )
    source = getattr(export_record, "source", {})
    receipt = source.get("render_receipt") if isinstance(source, Mapping) else None
    if not isinstance(receipt, Mapping):
        raise EchoRenderOperatorError("failed export has no canonical RenderReceipt")
    receipt_revision = int(receipt.get("project_revision") or -1)
    semantics_version = int(receipt.get("render_semantics_version") or -1)
    if receipt_revision != project_revision:
        raise EchoRenderOperatorError(
            "failed export receipt belongs to another revision"
        )
    if semantics_version < 1 or semantics_version >= CANONICAL_RENDER_SEMANTICS_VERSION:
        raise EchoRenderOperatorError(
            "audio-tail remediation requires a legacy render-semantics receipt"
        )
    export_path = Path(getattr(export_record, "path", "")).expanduser().resolve()
    receipt_path = Path(str(receipt.get("output_path") or "")).expanduser().resolve()
    if receipt_path != export_path:
        raise EchoRenderOperatorError("failed export receipt points to another file")
    actual_sha256 = str(hash_fn(export_path))
    receipt_sha256 = str(receipt.get("output_sha256") or "")
    record_sha256 = str(getattr(export_record, "sha256", "") or "")
    if not receipt_sha256 or actual_sha256 != receipt_sha256:
        raise EchoRenderOperatorError("failed export bytes no longer match its receipt")
    if record_sha256 and actual_sha256 != record_sha256:
        raise EchoRenderOperatorError(
            "failed export bytes no longer match its asset record"
        )

    probe = probe_fn(export_path)
    container_duration = float(probe.get("container_duration") or 0.0)
    video_duration = float(probe.get("video_duration") or 0.0)
    audio_duration = float(probe.get("audio_duration") or 0.0)
    if not (
        119.5 <= container_duration <= 120.5
        and 119.5 <= video_duration <= 120.5
        and audio_duration > 0.0
        and video_duration - audio_duration > 0.25
    ):
        raise EchoRenderOperatorError(
            "failed export does not reproduce the sidechain audio-tail defect"
        )
    failed_acceptance_id = str((run_before.get("evidence_ids") or [""])[-1])
    if not failed_acceptance_id:
        raise EchoRenderOperatorError(
            "revising state lacks the failed acceptance evidence"
        )
    payload = {
        "operator": RENDER_OPERATOR_VERSION,
        "defect": "sidechain_detector_eof_truncated_audio_tail",
        "project_revision": project_revision,
        "failed_acceptance_evidence_id": failed_acceptance_id,
        "failed_export": {
            "asset_id": export_id,
            "path": str(export_path),
            "sha256": actual_sha256,
            "graph_hash": str(receipt.get("graph_hash") or ""),
            "render_semantics_version": semantics_version,
            "container_duration": container_duration,
            "video_duration": video_duration,
            "audio_duration": audio_duration,
        },
        "remediation": {
            "project_patch": False,
            "target_render_semantics_version": CANONICAL_RENDER_SEMANTICS_VERSION,
            "canonical_render_semantics": canonical_render_semantics(),
            "requires_new_attempt": attempt + 1,
        },
        "budget": budget_before,
        "veo_calls": 0,
    }
    manager.record_evidence(
        runner.project_id,
        runner.run_id,
        evidence_id=evidence_id,
        kind="renderer_remediation",
        project_revision=project_revision,
        trace_id=f"trace-{evidence_id}",
        payload=payload,
    )
    latest = _run_view(manager, runner)
    _assert_budget_unchanged(budget_before, _budget(latest))
    transition = manager.transition_run(
        runner.project_id,
        runner.run_id,
        "rendering",
        expected_revision=int(
            latest.get("production_revision") or latest.get("revision") or 0
        ),
        trace_id=f"trace-{evidence_id}-complete",
    )
    final_run = _run_view(manager, runner)
    final_budget = _budget(final_run)
    _assert_budget_unchanged(budget_before, final_budget)
    if _project_revision(manager, runner) != project_revision:
        raise EchoRenderOperatorError(
            "renderer remediation changed the project revision"
        )
    if _state(final_run) != "rendering":
        raise EchoRenderOperatorError("renderer remediation did not reopen rendering")
    return {
        "ok": True,
        "replayed": False,
        "production_state": str(
            transition.get("production_state") or transition.get("state") or "rendering"
        ),
        "project_revision": project_revision,
        "evidence_id": evidence_id,
        "failed_acceptance_evidence_id": failed_acceptance_id,
        "failed_audio_duration": audio_duration,
        "budget": final_budget,
        "veo_calls": 0,
    }


def _manager(output_root: Path) -> SessionManager:
    return SessionManager(
        output_root=output_root,
        max_sessions=2,
        idle_timeout_sec=0,
        sweep_interval_sec=0,
    )


def render(output_root: Path, *, attempt: int = 1) -> dict[str, Any]:
    manager = _manager(output_root)
    runner = manager.resume_session(SESSION_ID)
    try:
        if str(runner.run_id) != RUN_ID:
            raise EchoRenderOperatorError(f"unexpected production run: {runner.run_id}")
        return execute_render(manager, runner, attempt=attempt)
    finally:
        manager.close_all()


def verify(
    output_root: Path,
    render_result: Mapping[str, Any],
    *,
    review_checks: Mapping[str, Any],
    review_notes: Mapping[str, Any],
    attempt: int = 1,
) -> dict[str, Any]:
    manager = _manager(output_root)
    runner = manager.resume_session(SESSION_ID)
    try:
        if str(runner.run_id) != RUN_ID:
            raise EchoRenderOperatorError(f"unexpected production run: {runner.run_id}")
        return execute_verify(
            manager,
            runner,
            render_result,
            review_checks=review_checks,
            review_notes=review_notes,
            attempt=attempt,
        )
    finally:
        manager.close_all()


def prepare_audio_tail_rerender(
    output_root: Path,
    render_result: Mapping[str, Any],
    *,
    attempt: int = 1,
) -> dict[str, Any]:
    manager = _manager(output_root)
    runner = manager.resume_session(SESSION_ID)
    try:
        if str(runner.run_id) != RUN_ID:
            raise EchoRenderOperatorError(f"unexpected production run: {runner.run_id}")
        return execute_prepare_audio_tail_rerender(
            manager,
            runner,
            render_result,
            attempt=attempt,
        )
    finally:
        manager.close_all()


def _read_json_object(path: Path, *, role: str) -> dict[str, Any]:
    try:
        value = json.loads(path.expanduser().resolve().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EchoRenderOperatorError(f"cannot read {role} JSON: {path}") from exc
    if not isinstance(value, dict):
        raise EchoRenderOperatorError(f"{role} JSON must contain an object")
    return value


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("render", "verify", "prepare-rerender"))
    parser.add_argument(
        "--output-root",
        type=Path,
        default=Path.home() / ".gemia" / "v3",
    )
    parser.add_argument("--attempt", type=int, default=1)
    parser.add_argument("--render-result-json", type=Path)
    parser.add_argument("--review-json", type=Path)
    args = parser.parse_args()
    output_root = args.output_root.expanduser().resolve()
    if args.command == "render":
        if args.render_result_json is not None or args.review_json is not None:
            parser.error("render does not accept --render-result-json/--review-json")
        result = render(output_root, attempt=args.attempt)
    elif args.command == "verify":
        if args.render_result_json is None or args.review_json is None:
            parser.error("verify requires --render-result-json and --review-json")
        rendered = _read_json_object(args.render_result_json, role="render result")
        review = _read_json_object(args.review_json, role="review")
        result = verify(
            output_root,
            rendered,
            review_checks=review.get("review_checks") or {},
            review_notes=review.get("review_notes") or {},
            attempt=args.attempt,
        )
    else:
        if args.render_result_json is None or args.review_json is not None:
            parser.error(
                "prepare-rerender requires --render-result-json and no --review-json"
            )
        rendered = _read_json_object(args.render_result_json, role="render result")
        result = prepare_audio_tail_rerender(
            output_root,
            rendered,
            attempt=args.attempt,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
