from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from gemia.turn_ledger import TurnLedger, extract_acceptance_criteria
from gemia.tool_outcome import classify_tool_result


@dataclass
class FutureToolOutcome:
    """Minimal stand-in for the planned structured tool_outcome API."""

    ok: bool
    status: str = "success"
    summary: str = ""
    facts: dict[str, Any] = field(default_factory=dict)
    artifacts: list[Any] = field(default_factory=list)
    error_code: str | None = None


def test_extracts_probe_evaluable_media_requirements_deterministically() -> None:
    criteria = extract_acceptance_criteria(
        "请做一个 7 秒、1920x1080、30fps、16:9、MP4、带音乐的视频"
    )
    assert {name: item.expected for name, item in criteria.items()} == {
        "duration": 7.0,
        "dimensions": (1920, 1080),
        "fps": 30.0,
        "aspect": (16, 9),
        "format": "mp4",
        "audio": True,
    }


def test_portrait_1080p_flips_dimensions_and_adds_vertical_aspect() -> None:
    criteria = extract_acceptance_criteria("请输出竖屏 1080p MP4")
    assert criteria["dimensions"].expected == (1080, 1920)
    assert criteria["aspect"].expected == (9, 16)


def test_future_outcome_and_raw_probe_can_complete_verified_asset_workflow() -> None:
    ledger = TurnLedger(
        "请做一个 7 秒、1920x1080、30fps、16:9、MP4、带音乐的视频",
        workflow="video_generation",
    )
    ledger.record_outcome(
        "generate_video",
        FutureToolOutcome(
            ok=True,
            summary="generated",
            facts={"asset_id": "v-001"},
            artifacts=[{"asset_id": "v-001"}],
        ),
        call_id="generate-1",
    )
    ledger.record_outcome(
        "probe_media",
        {
            "asset_id": "v-001",
            "duration_sec": 7.0,
            "width": 1920,
            "height": 1080,
            "fps": 30.0,
            "path": "/tmp/final.mp4",
            "has_audio": True,
        },
        call_id="probe-1",
    )
    ledger.record_outcome(
        "analyze_media",
        {"asset_id": "v-001", "summary": "visual review passed"},
        call_id="review-1",
    )

    assert ledger.final_asset_ids == ["v-001"]
    assert {name: item.status for name, item in ledger.criteria.items()} == {
        "duration": "passed",
        "dimensions": "passed",
        "fps": "passed",
        "aspect": "passed",
        "format": "passed",
        "audio": "passed",
    }
    assert ledger.last_verification_seq > ledger.last_mutation_seq
    assert ledger.completion_decision().complete is True


def test_wrong_duration_remains_a_hard_completion_blocker() -> None:
    ledger = TurnLedger("做一个 7 秒的视频", workflow="video_generation")
    ledger.record_outcome(
        "generate_video",
        {"ok": True, "asset_id": "v-short"},
        call_id="generate-short",
    )
    ledger.record_outcome(
        "probe_media",
        {"asset_id": "v-short", "duration_sec": 3.0},
        call_id="probe-short",
    )
    ledger.record_outcome(
        "analyze_media",
        {"asset_id": "v-short", "summary": "looks coherent"},
        call_id="review-short",
    )

    assert ledger.criteria["duration"].actual == 3.0
    assert ledger.criteria["duration"].status == "failed"
    decision = ledger.completion_decision()
    assert decision.complete is False
    assert "criterion:duration:failed" in decision.blockers


def test_generator_self_report_cannot_satisfy_ffprobe_criteria() -> None:
    ledger = TurnLedger("做一个 7 秒、30fps 的视频", workflow="video_generation")
    ledger.record_outcome(
        "generate_video",
        {
            "status": "success",
            "asset_id": "v-self-report",
            "kind": "video",
            "duration_sec": 7.0,
            "fps": 30.0,
        },
    )
    ledger.record_outcome(
        "analyze_media",
        {
            "status": "success",
            "asset_id": "v-self-report",
            "duration_sec": 7.0,
            "fps": 30.0,
            "summary": "looks fine",
        },
    )

    assert ledger.criteria["duration"].status == "open"
    assert ledger.criteria["fps"].status == "open"
    assert ledger.can_complete() is False


def test_mutation_invalidates_prior_visual_verification_and_old_asset_review() -> None:
    ledger = TurnLedger("生成一个视频", workflow="video_generation")
    ledger.record_outcome("generate_video", {"ok": True, "asset_id": "v-1"})
    ledger.record_outcome("analyze_media", {"asset_id": "v-1", "summary": "reviewed"})
    assert ledger.can_complete() is True

    ledger.record_outcome("edit_video", {"ok": True, "asset_id": "v-2"})
    assert ledger.final_asset_ids == ["v-2"]
    assert ledger.can_complete() is False
    assert "verification:stale_or_missing" in ledger.completion_decision().blockers

    ledger.record_outcome("analyze_media", {"asset_id": "v-1", "summary": "old review"})
    assert ledger.can_complete() is False

    ledger.record_outcome("analyze_media", {"asset_id": "v-2", "summary": "new review"})
    assert ledger.can_complete() is True


def test_pending_and_failed_jobs_prevent_completion() -> None:
    ledger = TurnLedger("做一个视频", workflow="video_generation")
    ledger.record_outcome(
        "generate_video",
        {"ok": True, "status": "submitted", "job_id": "job-1"},
        call_id="submit-1",
    )
    decision = ledger.completion_decision()
    assert decision.complete is False
    assert "pending_job:job-1:submitted" in decision.blockers

    ledger.record_outcome(
        "check_job",
        {"status": "failed", "job_id": "job-1", "error_code": "provider_failed"},
        call_id="check-1",
    )
    decision = ledger.completion_decision()
    assert not any(blocker.startswith("failed_job:job-1") for blocker in decision.blockers)
    assert "failure:check-1:provider_failed" in decision.blockers


def test_raw_false_semantics_do_not_advance_failed_action() -> None:
    ledger = TurnLedger("在时间线插入片段", workflow="timeline")
    record = ledger.record_outcome(
        "timeline_insert_clip",
        {"applied": False, "error_code": "bad_range"},
        call_id="insert-1",
    )

    assert record.ok is False
    assert ledger.last_mutation_seq == 0
    assert ledger.steps["mutate"].status == "failed"
    assert ledger.unresolved_failures["insert-1"].error_code == "bad_range"
    assert ledger.can_complete() is False


def test_legal_noop_is_not_failure_but_does_not_complete_action() -> None:
    ledger = TurnLedger("在时间线插入片段", workflow="timeline")
    record = ledger.record_outcome(
        "timeline_insert_clip",
        classify_tool_result({"applied": False}),
        call_id="insert-noop",
    )

    assert record.state == "noop"
    assert record.ok is True
    assert ledger.unresolved_failures == {}
    assert ledger.last_mutation_seq == 0
    assert ledger.can_complete() is False


def test_canonical_failure_outcome_does_not_advance_ledger() -> None:
    ledger = TurnLedger("执行命令", workflow="general")
    record = ledger.record_outcome(
        "run_shell",
        classify_tool_result({"exit_code": 2, "error": "bad"}),
        call_id="shell-1",
    )

    assert record.state == "failure"
    assert record.error_code == "E_PROCESS_EXIT"
    assert ledger.steps["act"].status == "failed"


def test_pre_mutation_probe_does_not_satisfy_final_asset_criteria() -> None:
    ledger = TurnLedger("做一个 7 秒视频", workflow="video_generation")
    ledger.record_outcome(
        "probe_media",
        {"asset_id": "source", "duration_sec": 7.0},
        call_id="source-probe",
    )
    assert ledger.criteria["duration"].status == "open"
    assert ledger.steps["produce"].status == "open"

    ledger.record_outcome(
        "generate_video",
        {"ok": True, "asset_id": "final"},
        call_id="generate-final",
    )
    assert ledger.criteria["duration"].status == "open"
    assert ledger.can_complete() is False


def test_successful_same_tool_retry_resolves_non_job_failure() -> None:
    ledger = TurnLedger("读取文件", workflow="files")
    ledger.record_outcome(
        "file_read", {"ok": False, "error_code": "not_found"}, call_id="read-1"
    )
    assert "read-1" in ledger.unresolved_failures

    ledger.record_outcome("file_read", {"ok": True, "content": "hello"}, call_id="read-2")
    assert ledger.unresolved_failures == {}
    assert ledger.can_complete() is True


def test_success_on_different_target_does_not_erase_failure() -> None:
    ledger = TurnLedger("检查素材 A 和 B", workflow="media_inspect")
    ledger.record_outcome(
        "probe_media",
        {"status": "failed", "error_code": "E_MISSING", "asset_id": "A"},
        call_id="probe-a",
    )
    ledger.record_outcome(
        "probe_media",
        {"status": "success", "asset_id": "B", "duration_sec": 2.0},
        call_id="probe-b",
    )

    assert "probe-a" in ledger.unresolved_failures
    assert ledger.can_complete() is False


def test_alternative_tool_on_same_target_resolves_failed_attempt() -> None:
    ledger = TurnLedger("检查素材 A", workflow="media_inspect")
    ledger.record_outcome(
        "probe_media",
        {"status": "failed", "error_code": "E_CODEC", "asset_id": "A"},
        call_id="probe-a",
    )
    ledger.record_outcome(
        "analyze_media",
        {"status": "success", "asset_id": "A", "summary": "decoded"},
        call_id="analyze-a",
    )

    assert ledger.unresolved_failures == {}
    assert ledger.can_complete() is True


def test_read_on_same_target_cannot_resolve_failed_write() -> None:
    ledger = TurnLedger("写入文件 /tmp/a.txt", workflow="files")
    ledger.record_outcome(
        "file_write",
        {"status": "failed", "error_code": "E_WRITE", "path": "/tmp/a.txt"},
        call_id="write-a",
    )
    ledger.record_outcome(
        "file_read",
        {"status": "success", "path": "/tmp/a.txt", "content": "old"},
        call_id="read-a",
    )

    assert "write-a" in ledger.unresolved_failures
    assert ledger.can_complete() is False


@pytest.mark.parametrize(
    ("user_text", "workflow", "tool_name"),
    [
        ("查看当前时间线", "timeline", "get_timeline"),
        ("读取当前合成树", "lumen_core", "get_lumenframe"),
    ],
)
def test_read_only_project_state_requests_do_not_require_mutation(
    user_text: str, workflow: str, tool_name: str
) -> None:
    ledger = TurnLedger(user_text, workflow=workflow)
    assert set(ledger.steps) == {"inspect"}
    ledger.record_outcome(tool_name, {"status": "success"})
    assert ledger.can_complete() is True


def test_multi_operation_video_request_requires_every_named_operation() -> None:
    ledger = TurnLedger(
        "给视频加字幕并调色", workflow="video_edit"
    )
    ledger.record_outcome(
        "subtitle",
        {"status": "success", "asset_id": "v-captioned", "kind": "video"},
    )
    ledger.record_outcome(
        "analyze_media", {"status": "success", "asset_id": "v-captioned"}
    )
    assert "step:op:color:open" in ledger.completion_decision().blockers

    ledger.record_outcome(
        "color_grade",
        {"status": "success", "asset_id": "v-final", "kind": "video"},
    )
    ledger.record_outcome(
        "analyze_media", {"status": "success", "asset_id": "v-final"}
    )
    assert ledger.can_complete() is True


def test_multi_operation_timeline_request_requires_transition_after_split() -> None:
    ledger = TurnLedger("把片段拆分后加转场", workflow="timeline")
    ledger.record_outcome("timeline_split_clip", {"status": "success"})
    ledger.record_outcome("inspect_timeline", {"status": "success"})
    assert "step:op:transition:open" in ledger.completion_decision().blockers


def test_edit_video_trim_cannot_claim_a_transition() -> None:
    ledger = TurnLedger("把视频裁剪后加转场", workflow="video_edit")
    ledger.record_outcome(
        "edit_video",
        {"status": "success", "asset_id": "v-trim", "kind": "video"},
        call_args={"asset_id": "v-source", "operation": "trim"},
    )
    assert ledger.steps["op:trim"].status == "done"
    assert ledger.steps["op:transition"].status == "open"

    ledger.record_outcome(
        "arrange_timeline",
        {
            "status": "success",
            "asset_id": "v-final",
            "kind": "video",
            "transitions": [{"between_index": 0, "kind": "dissolve"}],
        },
        call_args={
            "asset_ids": ["v-trim", "v-b"],
            "transitions": [{"between_index": 0, "kind": "dissolve"}],
        },
    )
    assert ledger.steps["op:transition"].status == "done"


def test_adjust_media_and_wrong_edit_operation_cannot_claim_retime() -> None:
    ledger = TurnLedger("把视频加速两倍", workflow="video_edit")
    ledger.record_outcome(
        "adjust_media",
        {"status": "success", "asset_id": "v-bright", "kind": "video"},
        call_args={"asset_id": "v-source", "brightness": 0.2},
    )
    ledger.record_outcome(
        "edit_video",
        {"status": "success", "asset_id": "v-trim", "kind": "video"},
        call_args={"asset_id": "v-bright", "operation": "trim"},
    )
    assert ledger.steps["op:retime"].status == "open"

    ledger.record_outcome(
        "edit_video",
        {"status": "success", "asset_id": "v-fast", "kind": "video"},
        call_args={
            "asset_id": "v-trim",
            "operation": "speed",
            "speed_factor": 2.0,
        },
    )
    assert ledger.steps["op:retime"].status == "done"


def test_explicit_multi_asset_count_requires_every_final_and_full_review() -> None:
    ledger = TurnLedger("生成 3 张图片", workflow="image")
    for index in range(1, 3):
        ledger.record_outcome(
            "generate_image",
            {"status": "success", "asset_id": f"img-{index}", "kind": "image"},
        )
    assert ledger.final_asset_ids == ["img-1", "img-2"]
    assert ledger.criteria["asset_count"].actual == 2
    assert ledger.can_complete() is False

    ledger.record_outcome(
        "generate_image",
        {"status": "success", "asset_id": "img-3", "kind": "image"},
    )
    assert ledger.criteria["asset_count"].status == "passed"
    ledger.record_outcome(
        "host_visual_review",
        {"status": "success", "asset_ids": ["img-1", "img-2"]},
    )
    assert ledger.can_complete() is False
    ledger.record_outcome(
        "host_visual_review",
        {"status": "success", "asset_ids": ["img-3"]},
    )
    assert ledger.can_complete() is True


def test_multi_deliverable_image_and_audio_requires_both_but_only_image_review() -> None:
    ledger = TurnLedger(
        "生成一张图片和一段音频",
        workflow="image",
        workflows=("image", "audio"),
    )
    ledger.record_outcome(
        "generate_image",
        {"status": "success", "asset_id": "img-cover", "kind": "image"},
    )
    ledger.record_outcome(
        "host_visual_review",
        {"status": "success", "asset_ids": ["img-cover"]},
    )

    decision = ledger.completion_decision()
    assert decision.complete is False
    assert "final_asset_kind:audio:missing" in decision.blockers

    ledger.record_outcome(
        "generate_audio",
        {"status": "success", "asset_id": "aud-bed", "kind": "audio"},
    )
    ledger.record_outcome(
        "host_visual_review",
        {"status": "success", "asset_ids": ["img-cover"]},
    )

    assert ledger.final_asset_ids == ["img-cover", "aud-bed"]
    assert "aud-bed" not in ledger.verified_final_asset_ids
    assert ledger.can_complete() is True


def test_multi_deliverable_video_and_cover_preserves_each_kind_on_revision() -> None:
    ledger = TurnLedger(
        "做一个 7 秒视频并生成一张封面图",
        workflow="video_generation",
        workflows=("video_generation", "image"),
    )
    ledger.record_outcome(
        "generate_video",
        {"status": "success", "asset_id": "v-main", "kind": "video"},
    )
    ledger.record_outcome(
        "host_visual_review",
        {"status": "success", "asset_ids": ["v-main"]},
    )
    assert "final_asset_kind:image:missing" in ledger.completion_decision().blockers

    ledger.record_outcome(
        "generate_image",
        {"status": "success", "asset_id": "img-cover-1", "kind": "image"},
    )
    assert ledger.final_asset_ids == ["v-main", "img-cover-1"]
    ledger.record_outcome(
        "probe_media",
        {"status": "success", "asset_id": "v-main", "duration_sec": 7.0},
    )
    ledger.record_outcome(
        "host_visual_review",
        {"status": "success", "asset_ids": ["v-main", "img-cover-1"]},
    )
    assert ledger.can_complete() is True

    ledger.record_outcome(
        "edit_image",
        {"status": "success", "asset_id": "img-cover-2", "kind": "image"},
        call_args={"asset_id": "img-cover-1"},
    )
    assert ledger.final_asset_ids == ["v-main", "img-cover-2"]
    ledger.record_outcome(
        "probe_media",
        {"status": "success", "asset_id": "v-main", "duration_sec": 7.0},
    )
    ledger.record_outcome(
        "host_visual_review",
        {"status": "success", "asset_ids": ["v-main", "img-cover-2"]},
    )
    assert ledger.can_complete() is True


def test_source_clip_count_is_not_mistaken_for_output_multiplicity() -> None:
    ledger = TurnLedger("把 3 段视频合并成一个成片", workflow="video_edit")
    assert "asset_count" not in ledger.criteria


def test_objective_criteria_must_pass_for_every_requested_final_asset() -> None:
    ledger = TurnLedger("生成 2 个视频，每个 7 秒", workflow="video_generation")
    for asset_id in ("v-1", "v-2"):
        ledger.record_outcome(
            "generate_video",
            {"status": "success", "asset_id": asset_id, "kind": "video"},
        )
    ledger.record_outcome(
        "probe_media", {"asset_id": "v-1", "duration_sec": 3.0}
    )
    ledger.record_outcome(
        "probe_media", {"asset_id": "v-2", "duration_sec": 7.0}
    )
    ledger.record_outcome(
        "host_visual_review", {"asset_ids": ["v-1", "v-2"]}
    )
    assert ledger.criteria["duration"].status == "failed"
    assert ledger.can_complete() is False

    ledger.record_outcome(
        "probe_media", {"asset_id": "v-1", "duration_sec": 7.0}
    )
    assert ledger.criteria["duration"].status == "passed"
    assert ledger.can_complete() is True


def test_mutation_promotes_misrouted_read_workflow_and_requires_review() -> None:
    ledger = TurnLedger("把画面调亮一点", workflow="media_inspect")
    ledger.record_outcome(
        "adjust_media",
        {"status": "success", "asset_id": "v-bright", "kind": "video"},
    )
    assert ledger.workflow == "video_edit"
    assert ledger.workflows == ("video_edit",)
    assert ledger.expected_final_kinds == frozenset({"video"})
    assert ledger.can_complete() is False
    ledger.record_outcome(
        "analyze_media", {"status": "success", "asset_id": "v-bright"}
    )
    assert ledger.can_complete() is True


def test_failed_job_can_be_superseded_only_by_verified_fallback() -> None:
    ledger = TurnLedger("做一个 7 秒视频", workflow="video_generation")
    ledger.record_outcome(
        "check_job",
        {
            "status": "failed",
            "job_id": "veo-1",
            "error_code": "E_PROVIDER",
        },
        call_id="veo-failed",
    )
    ledger.record_outcome(
        "generate_video",
        {"status": "success", "asset_id": "v-local", "kind": "video"},
        call_id="local-fallback",
    )
    assert "veo-failed" in ledger.unresolved_failures
    ledger.record_outcome(
        "probe_media",
        {"asset_id": "v-local", "duration_sec": 7.0},
        call_id="probe-local",
    )
    assert "veo-failed" in ledger.unresolved_failures
    ledger.record_outcome(
        "analyze_media", {"asset_id": "v-local", "summary": "reviewed"},
        call_id="review-local",
    )
    assert "veo-failed" not in ledger.unresolved_failures
    assert ledger.can_complete() is True


@pytest.mark.parametrize(
    ("user_text", "workflow", "tool_name", "payload"),
    [
        ("整理这些文件", "files", "file_list", {"files": ["a.mov"]}),
        ("给素材添加标签", "annotations", "search_media", {"results": ["v_001"]}),
    ],
)
def test_read_only_discovery_cannot_complete_mutating_workflow(
    user_text: str, workflow: str, tool_name: str, payload: dict[str, Any]
) -> None:
    ledger = TurnLedger(user_text, workflow=workflow)
    ledger.record_outcome(tool_name, {"status": "success", **payload})
    assert ledger.can_complete() is False
    assert "step:act:open" in ledger.completion_decision().blockers


def test_reference_image_cannot_satisfy_video_deliverable() -> None:
    ledger = TurnLedger("生成一个视频", workflow="video_generation")
    ledger.record_outcome(
        "generate_image",
        {"status": "success", "asset_id": "img-ref", "kind": "image"},
        call_id="reference",
    )
    ledger.record_outcome(
        "analyze_media",
        {"status": "success", "asset_id": "img-ref", "summary": "looks good"},
        call_id="review-reference",
    )

    assert ledger.final_asset_ids == []
    assert ledger.can_complete() is False
    assert "final_asset:missing" in ledger.completion_decision().blockers


def test_take7_short_then_take6_correct_requires_fresh_visual_review() -> None:
    ledger = TurnLedger(
        "做一个 7 秒、1080p、30fps 的视频", workflow="video_generation"
    )
    ledger.record_outcome(
        "generate_video",
        {"status": "success", "asset_id": "v-take7", "kind": "video"},
        call_id="take7",
    )
    ledger.record_outcome(
        "probe_media",
        {"asset_id": "v-take7", "duration_sec": 3.0, "width": 1920, "height": 1080, "fps": 30},
        call_id="probe-take7",
    )
    ledger.record_outcome(
        "analyze_media", {"asset_id": "v-take7", "summary": "reviewed"},
        call_id="review-take7",
    )
    assert ledger.can_complete() is False

    ledger.record_outcome(
        "edit_video",
        {"status": "success", "asset_id": "v-take6", "kind": "video"},
        call_id="take6",
        target_key="asset_id=v-take7",
    )
    ledger.record_outcome(
        "probe_media",
        {"asset_id": "v-take6", "duration_sec": 7.0, "width": 1920, "height": 1080, "fps": 30},
        call_id="probe-take6",
    )
    ledger.record_outcome(
        "analyze_media", {"asset_id": "v-take7", "summary": "stale review"},
        call_id="stale-review",
    )
    assert ledger.can_complete() is False

    ledger.record_outcome(
        "analyze_media", {"asset_id": "v-take6", "summary": "fresh review"},
        call_id="fresh-review",
    )
    assert ledger.last_verification_seq > ledger.last_mutation_seq
    assert ledger.can_complete() is True


def test_storyboard_draft_is_plan_mutation_evidence() -> None:
    ledger = TurnLedger("做一个三镜头分镜", workflow="storyboard")
    ledger.record_outcome("draft_shotlist", {"status": "success", "shot_count": 3})
    ledger.record_outcome("assemble_shotlist", {"status": "success", "clip_count": 3})
    ledger.record_outcome("analyze_media", {"status": "success", "summary": "reviewed"})
    assert ledger.can_complete() is True


def test_conversation_requires_no_tool_activity() -> None:
    ledger = TurnLedger("你好")
    assert ledger.workflow == "conversation"
    assert ledger.steps == {}
    assert ledger.completion_decision().complete is True


def test_step_mutation_is_explicit_and_unknown_step_is_rejected() -> None:
    ledger = TurnLedger("检查素材", workflow="media_inspect")
    ledger.mark_step("inspect", evidence_id="manual")
    assert ledger.can_complete() is True
    with pytest.raises(KeyError):
        ledger.mark_step("not-real")
