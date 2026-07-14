from __future__ import annotations

import pytest

from gemia.tool_router import classify_request
from gemia.turn_ledger import TurnLedger, extract_acceptance_criteria


@pytest.mark.parametrize(
    ("user_text", "field", "correct", "wrong"),
    (
        ("亮度降低20%", "brightness", -0.2, 0.2),
        ("decrease brightness by 20%", "brightness", -0.2, 0.2),
        ("对比度降低20%", "contrast", 0.8, 0.2),
        ("曝光降低0.3", "exposure", -0.3, 0.3),
    ),
)
def test_decreased_color_parameters_use_the_right_sign_and_baseline(
    user_text: str, field: str, correct: float, wrong: float
) -> None:
    wrong_ledger = TurnLedger(user_text, workflow="video_edit")
    wrong_ledger.record_outcome(
        "adjust_media",
        {"status": "success", "asset_id": "wrong", "kind": "video"},
        call_args={"asset_id": "source", field: wrong},
    )
    assert wrong_ledger.steps["op:color"].status == "open"

    right_ledger = TurnLedger(user_text, workflow="video_edit")
    right_ledger.record_outcome(
        "adjust_media",
        {"status": "success", "asset_id": "right", "kind": "video"},
        call_args={"asset_id": "source", field: correct},
    )
    assert right_ledger.steps["op:color"].status == "done"


@pytest.mark.parametrize(
    ("user_text", "correct", "wrong"),
    (
        ("把视频加速20%", 1.2, 0.2),
        ("把视频减速20%", 0.8, 0.2),
        ("speed up the video by 20%", 1.2, 0.2),
        ("slow the video down by 20%", 0.8, 0.2),
        ("加速到200%", 2.0, 0.2),
    ),
)
def test_relative_speed_percentages_are_not_absolute_factors(
    user_text: str, correct: float, wrong: float
) -> None:
    wrong_ledger = TurnLedger(user_text, workflow="video_edit")
    wrong_ledger.record_outcome(
        "edit_video",
        {"status": "success", "asset_id": "wrong", "kind": "video"},
        call_args={
            "asset_id": "source",
            "operation": "speed",
            "speed_factor": wrong,
        },
    )
    assert wrong_ledger.steps["op:retime"].status == "open"

    right_ledger = TurnLedger(user_text, workflow="video_edit")
    right_ledger.record_outcome(
        "edit_video",
        {"status": "success", "asset_id": "right", "kind": "video"},
        call_args={
            "asset_id": "source",
            "operation": "speed",
            "speed_factor": correct,
        },
    )
    assert right_ledger.steps["op:retime"].status == "done"


def test_attached_voiceover_binds_exact_text_to_the_inserted_audio_asset() -> None:
    ledger = TurnLedger("给视频添加旁白“欢迎回来”", workflow="video_edit")
    ledger.record_outcome(
        "narrate",
        {"status": "success", "asset_id": "aud-bad", "kind": "audio"},
        call_args={"text": "再见"},
    )
    ledger.record_outcome(
        "timeline_insert_clip",
        {"status": "success"},
        call_args={"asset_id": "aud-bad", "track_id": "A1"},
    )
    assert ledger.steps["op:voiceover"].status == "open"

    ledger.record_outcome(
        "narrate",
        {"status": "success", "asset_id": "aud-good", "kind": "audio"},
        call_args={"text": "欢迎回来"},
    )
    assert ledger.steps["op:voiceover"].status == "open"
    ledger.record_outcome(
        "timeline_insert_clip",
        {"status": "success"},
        call_args={"asset_id": "aud-good", "track_id": "A1"},
    )
    assert ledger.steps["op:voiceover"].status == "done"


def test_title_duration_is_an_insert_parameter_not_final_media_duration() -> None:
    request = "在第2秒插入持续3秒的标题“开场”"
    ledger = TurnLedger(request, workflow="timeline")
    assert "duration" not in ledger.criteria

    ledger.record_outcome(
        "timeline_insert_clip",
        {"status": "success"},
        call_args={
            "at_time": 2.0,
            "duration_sec": 1.0,
            "text": {"content": "开场"},
        },
    )
    assert ledger.steps["op:insert"].status == "open"

    ledger.record_outcome(
        "timeline_insert_clip",
        {"status": "success"},
        call_args={
            "at_time": 2.0,
            "duration_sec": 3.0,
            "text": {"content": "开场"},
        },
    )
    assert ledger.steps["op:insert"].status == "done"


def test_equal_final_and_title_durations_keep_the_final_duration_requirement() -> None:
    criteria = extract_acceptance_criteria(
        "制作3秒视频，并在第2秒插入持续3秒的标题“开场”"
    )

    assert criteria["duration"].expected == 3.0


@pytest.mark.parametrize(
    "user_text",
    (
        "在2秒到5秒添加字幕“你好”",
        "从2秒到5秒加蒙版",
        "让音乐淡入2秒",
        "给字幕显示3秒",
        "add a title at 2 seconds for 3 seconds",
    ),
)
def test_operation_scoped_durations_do_not_become_final_duration_criteria(
    user_text: str,
) -> None:
    assert "duration" not in extract_acceptance_criteria(user_text)


@pytest.mark.parametrize(
    "user_text",
    (
        "给视频添加一个100x100的logo图片",
        "在视频中插入1080x1080图片",
        "add a 100x100 logo image to the video",
        "add a PNG logo to the video",
    ),
)
def test_overlay_source_specs_do_not_constrain_the_final_video(user_text: str) -> None:
    ledger = TurnLedger(user_text)

    assert ledger.expected_final_kinds == frozenset({"video"})
    assert "dimensions" not in ledger.criteria
    assert "format" not in ledger.criteria
    assert "op:insert" in ledger.steps


def test_unsupported_transition_diagnostic_keeps_operation_open() -> None:
    ledger = TurnLedger("添加0.5秒溶解转场", workflow="timeline")
    ledger.record_outcome(
        "timeline_add_transition",
        {"status": "success"},
        call_args={
            "kind": "dissolve",
            "duration_sec": 0.5,
            "warning": "unsupported; renders as a hard cut",
        },
    )

    assert ledger.steps["op:transition"].status == "open"


@pytest.mark.parametrize(
    "user_text",
    (
        "add captions to the video",
        "把视频裁成7秒",
        "remove the first 2 seconds from the video",
        "cut off the first 2 seconds",
        "裁掉开头2秒",
        "删除视频前2秒",
        "insert a title at 2 seconds",
    ),
)
def test_common_edit_phrases_receive_an_edit_or_timeline_pack(user_text: str) -> None:
    workflows = classify_request(user_text).workflows

    assert workflows
    assert workflows[0] in {"video_edit", "timeline"}
    assert {"video_edit", "timeline"}.intersection(workflows)
