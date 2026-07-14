from __future__ import annotations

import pytest

from gemia.turn_ledger import TurnLedger, extract_acceptance_criteria


def test_trim_range_extracts_output_duration_instead_of_start_time() -> None:
    criteria = extract_acceptance_criteria("截取 2 秒到 5 秒")

    assert criteria["duration"].expected == 3.0


def test_trim_range_shorthand_and_offsets_are_both_enforced() -> None:
    ledger = TurnLedger("截取2-5秒", workflow="video_edit")
    assert ledger.criteria["duration"].expected == 3.0

    ledger.record_outcome(
        "edit_video",
        {"status": "success", "asset_id": "v-wrong-range", "kind": "video"},
        call_args={
            "asset_id": "v-source",
            "operation": "trim",
            "trim": {"start_sec": 0.0, "end_sec": 3.0},
        },
    )
    ledger.record_outcome(
        "probe_media", {"asset_id": "v-wrong-range", "duration_sec": 3.0}
    )
    ledger.record_outcome(
        "analyze_media", {"asset_id": "v-wrong-range", "summary": "reviewed"}
    )
    assert ledger.steps["op:trim"].status == "open"
    assert ledger.can_complete() is False

    ledger.record_outcome(
        "edit_video",
        {"status": "success", "asset_id": "v-correct-range", "kind": "video"},
        call_args={
            "asset_id": "v-wrong-range",
            "operation": "trim",
            "trim": {"start_sec": 2.0, "end_sec": 5.0},
        },
    )
    assert ledger.steps["op:trim"].status == "done"


def test_verbose_trim_range_extracts_and_enforces_offsets() -> None:
    ledger = TurnLedger("截取从2秒开始，到5秒结束", workflow="video_edit")
    assert ledger.criteria["duration"].expected == 3.0
    ledger.record_outcome(
        "edit_video",
        {"status": "success", "asset_id": "v-range", "kind": "video"},
        call_args={
            "asset_id": "v-source",
            "operation": "trim",
            "trim": {"start_sec": 2.0, "end_sec": 5.0},
        },
    )
    assert ledger.steps["op:trim"].status == "done"


@pytest.mark.parametrize(
    ("user_text", "expected"),
    (
        ("做三版海报", 3),
        ("生成 3 个不同版本的视频", 3),
        ("做两套图片方案", 2),
        ("生成三张不同风格的海报", 3),
    ),
)
def test_output_count_understands_version_and_option_classifiers(
    user_text: str, expected: int
) -> None:
    criteria = extract_acceptance_criteria(user_text)

    assert criteria["asset_count"].expected == expected


def test_monochrome_requires_saturation_evidence() -> None:
    ledger = TurnLedger("把视频调成黑白", workflow="video_edit")
    ledger.record_outcome(
        "adjust_media",
        {"status": "success", "asset_id": "v-bright", "kind": "video"},
        call_args={"asset_id": "v-source", "brightness": 0.2},
    )

    assert ledger.steps["op:color"].status == "open"

    ledger.record_outcome(
        "adjust_media",
        {"status": "success", "asset_id": "v-mono", "kind": "video"},
        call_args={"asset_id": "v-bright", "saturation": 0.0},
    )

    assert ledger.steps["op:color"].status == "done"


def test_named_color_grade_cannot_claim_monochrome() -> None:
    ledger = TurnLedger("把视频调成黑白", workflow="video_edit")
    ledger.record_outcome(
        "color_grade",
        {"status": "success", "asset_id": "v-warm", "kind": "video"},
        call_args={"asset_id": "v-source", "look": "warm"},
    )

    assert ledger.steps["op:color"].status == "open"


def test_grayscale_phrase_creates_the_same_strict_color_step() -> None:
    ledger = TurnLedger("把视频调成灰度", workflow="video_edit")
    ledger.record_outcome(
        "adjust_media",
        {"status": "success", "asset_id": "v-bright", "kind": "video"},
        call_args={"asset_id": "v-source", "brightness": 0.2},
    )
    assert ledger.steps["op:color"].status == "open"


def test_requested_speed_factor_must_match_applied_factor() -> None:
    ledger = TurnLedger("把视频加速两倍", workflow="video_edit")
    ledger.record_outcome(
        "edit_video",
        {"status": "success", "asset_id": "v-barely-fast", "kind": "video"},
        call_args={
            "asset_id": "v-source",
            "operation": "speed",
            "speed_factor": 1.1,
        },
    )

    assert ledger.steps["op:retime"].status == "open"

    ledger.record_outcome(
        "edit_video",
        {"status": "success", "asset_id": "v-2x", "kind": "video"},
        call_args={
            "asset_id": "v-barely-fast",
            "operation": "speed",
            "speed_factor": 2.0,
        },
    )

    assert ledger.steps["op:retime"].status == "done"


def test_requested_speed_factor_applies_to_lumen_retime() -> None:
    ledger = TurnLedger("把视频加速两倍", workflow="lumen_time")
    ledger.record_outcome(
        "lumen_retime_segment",
        {"status": "success"},
        call_args={"layer_id": "layer-1", "speed": 1.25},
    )
    assert ledger.steps["op:retime"].status == "open"

    ledger.record_outcome(
        "lumen_retime_segment",
        {"status": "success"},
        call_args={"layer_id": "layer-1", "speed": 2.0},
    )
    assert ledger.steps["op:retime"].status == "done"


def test_two_x_speed_phrase_is_not_mistaken_for_any_speed_change() -> None:
    ledger = TurnLedger("把视频改成2倍速", workflow="video_edit")
    ledger.record_outcome(
        "edit_video",
        {"status": "success", "asset_id": "v-1-1x", "kind": "video"},
        call_args={
            "asset_id": "v-source",
            "operation": "speed",
            "speed_factor": 1.1,
        },
    )
    assert ledger.steps["op:retime"].status == "open"


@pytest.mark.parametrize(
    ("user_text", "wrong_factor"),
    (
        ("加速到200%", 1.1),
        ("播放速度设为原来的两倍", 1.1),
        ("把视频放慢到0.5倍", 0.8),
    ),
)
def test_speed_variants_require_the_exact_requested_factor(
    user_text: str, wrong_factor: float
) -> None:
    ledger = TurnLedger(user_text, workflow="video_edit")
    ledger.record_outcome(
        "edit_video",
        {"status": "success", "asset_id": "v-wrong-speed", "kind": "video"},
        call_args={
            "asset_id": "v-source",
            "operation": "speed",
            "speed_factor": wrong_factor,
        },
    )
    assert ledger.steps["op:retime"].status == "open"


def test_source_image_pack_is_not_an_image_deliverable() -> None:
    ledger = TurnLedger(
        "用这张图片生成视频",
        workflow="video_generation",
        workflows=("video_generation", "image"),
    )
    ledger.record_outcome(
        "generate_video",
        {"status": "success", "asset_id": "v-final", "kind": "video"},
    )
    ledger.record_outcome(
        "analyze_media", {"asset_id": "v-final", "summary": "reviewed"}
    )

    assert ledger.expected_final_kinds == frozenset({"video"})
    assert ledger.can_complete() is True


def test_transform_target_overrides_misrouted_primary_source_kind() -> None:
    ledger = TurnLedger(
        "把这张图片做成视频",
        workflow="image",
        workflows=("image", "video_generation"),
    )
    assert ledger.expected_final_kinds == frozenset({"video"})


def test_multi_deliverable_count_is_tracked_per_kind() -> None:
    ledger = TurnLedger(
        "生成两张图片和一段音频",
        workflow="image",
        workflows=("image", "audio"),
    )
    ledger.record_outcome(
        "generate_image",
        {"status": "success", "asset_id": "img-1", "kind": "image"},
    )
    ledger.record_outcome(
        "generate_audio",
        {"status": "success", "asset_id": "aud-1", "kind": "audio"},
    )
    ledger.record_outcome(
        "host_visual_review", {"status": "success", "asset_ids": ["img-1"]}
    )
    assert ledger.criteria["asset_count:image"].actual == 1
    assert ledger.can_complete() is False

    ledger.record_outcome(
        "generate_image",
        {"status": "success", "asset_id": "img-2", "kind": "image"},
    )
    assert set(ledger.final_asset_ids) == {"img-1", "img-2", "aud-1"}
    assert ledger.criteria["asset_count:image"].status == "passed"


@pytest.mark.parametrize(
    "user_text",
    ("生成两张图片和音频", "生成两张图片，另配音频"),
)
def test_single_explicit_count_stays_per_kind_when_audio_is_also_required(
    user_text: str,
) -> None:
    ledger = TurnLedger(user_text, workflow="image", workflows=("image", "audio"))
    assert ledger.expected_final_kinds == frozenset({"image", "audio"})
    assert ledger.criteria["asset_count:image"].expected == 2
    assert "asset_count" not in ledger.criteria


@pytest.mark.parametrize(
    ("user_text", "criterion", "expected"),
    (
        ("把 MOV 视频转成 MP4", "format", "mp4"),
        ("将 60fps 素材导出为 30fps", "fps", 30.0),
        ("把 1920x1080 横屏素材改成 1080x1920 竖屏", "dimensions", (1080, 1920)),
        ("把 10 秒视频裁成 3 秒", "duration", 3.0),
        ("把 16:9 视频改成 9:16", "aspect", (9, 16)),
    ),
)
def test_source_to_target_specs_use_the_target_value(
    user_text: str, criterion: str, expected: object
) -> None:
    assert extract_acceptance_criteria(user_text)[criterion].expected == expected
