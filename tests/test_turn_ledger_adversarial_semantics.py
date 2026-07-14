from __future__ import annotations

import pytest

from gemia.tool_router import classify_request
from gemia.turn_ledger import TurnLedger, extract_acceptance_criteria


@pytest.mark.parametrize(
    "user_text",
    ("生成一个有音乐的视频", "制作带旁白的视频"),
)
def test_audio_modifier_describes_video_instead_of_audio_deliverable(
    user_text: str,
) -> None:
    ledger = TurnLedger(user_text)

    assert ledger.expected_final_kinds == frozenset({"video"})
    assert classify_request(user_text).workflows[0] == "video_generation"


@pytest.mark.parametrize(
    "user_text",
    ("生成一张爵士音乐海报", "生成一张无声电影海报", "生成音乐节海报"),
)
def test_visual_theme_words_do_not_create_audio_or_video_outputs(
    user_text: str,
) -> None:
    ledger = TurnLedger(user_text)

    assert ledger.expected_final_kinds == frozenset({"image"})
    assert "audio" not in ledger.criteria


@pytest.mark.parametrize(
    ("user_text", "kinds", "count_key", "count"),
    (
        ("generate 3 images", {"image"}, "asset_count", 3),
        ("create two videos", {"video"}, "asset_count", 2),
        ("make a video and a cover image", {"video", "image"}, None, None),
        ("generate an image and an audio track", {"image", "audio"}, None, None),
        ("create 3 short videos", {"video"}, "asset_count", 3),
        ("make three different versions of a video", {"video"}, "asset_count", 3),
    ),
)
def test_english_output_roles_and_counts(
    user_text: str,
    kinds: set[str],
    count_key: str | None,
    count: int | None,
) -> None:
    ledger = TurnLedger(user_text)

    assert ledger.expected_final_kinds == frozenset(kinds)
    if count_key:
        assert ledger.criteria[count_key].expected == count


@pytest.mark.parametrize(
    "user_text",
    (
        "generate a video using 3 images as references",
        "create a video from two clips and a cover image",
        "生成一个视频，使用3张图片作为参考素材",
        "制作视频，素材包括2段音频",
    ),
)
def test_source_counts_and_source_kinds_are_not_deliverables(user_text: str) -> None:
    ledger = TurnLedger(user_text)

    assert ledger.expected_final_kinds == frozenset({"video"})
    assert not any(key.startswith("asset_count") for key in ledger.criteria)


@pytest.mark.parametrize(
    ("user_text", "expected"),
    (
        ("生成两张1080x1080图片", 2),
        ("生成2张16:9海报", 2),
        ("生成两个7秒视频", 2),
        ("generate two 7-second videos", 2),
        ("make two 1080p 7-second videos", 2),
    ),
)
def test_output_count_does_not_capture_spec_numbers(
    user_text: str, expected: int
) -> None:
    assert extract_acceptance_criteria(user_text)["asset_count"].expected == expected


def test_multi_kind_specs_apply_to_their_own_deliverable() -> None:
    ledger = TurnLedger(
        "生成视频（1920x1080）和封面图（1080x1080）",
        workflow="video_generation",
        workflows=("video_generation", "image"),
    )
    ledger.record_outcome(
        "generate_video", {"status": "success", "asset_id": "v", "kind": "video"}
    )
    ledger.record_outcome(
        "generate_image", {"status": "success", "asset_id": "img", "kind": "image"}
    )
    ledger.record_outcome(
        "probe_media", {"asset_id": "v", "width": 1920, "height": 1080}
    )
    ledger.record_outcome(
        "probe_media", {"asset_id": "img", "width": 1080, "height": 1080}
    )
    ledger.record_outcome(
        "host_visual_review", {"asset_ids": ["v", "img"]}
    )

    assert ledger.can_complete() is True


def test_same_kind_slot_specs_stay_bound_to_one_asset() -> None:
    prompt = "生成两个视频，一个3秒16:9横版，一个7秒9:16竖版"

    wrong = TurnLedger(prompt, workflow="video_generation")
    for asset_id in ("v1", "v2"):
        wrong.record_outcome(
            "generate_video", {"status": "success", "asset_id": asset_id, "kind": "video"}
        )
    wrong.record_outcome(
        "probe_media", {"asset_id": "v1", "duration_sec": 3, "width": 900, "height": 1600}
    )
    wrong.record_outcome(
        "probe_media", {"asset_id": "v2", "duration_sec": 7, "width": 1600, "height": 900}
    )
    wrong.record_outcome("host_visual_review", {"asset_ids": ["v1", "v2"]})
    assert wrong.can_complete() is False

    correct = TurnLedger(prompt, workflow="video_generation")
    for asset_id in ("v1", "v2"):
        correct.record_outcome(
            "generate_video", {"status": "success", "asset_id": asset_id, "kind": "video"}
        )
    correct.record_outcome(
        "probe_media", {"asset_id": "v1", "duration_sec": 3, "width": 1600, "height": 900}
    )
    correct.record_outcome(
        "probe_media", {"asset_id": "v2", "duration_sec": 7, "width": 900, "height": 1600}
    )
    correct.record_outcome("host_visual_review", {"asset_ids": ["v1", "v2"]})
    assert correct.can_complete() is True


def test_verifier_cannot_promote_explicit_intermediate_to_cover() -> None:
    ledger = TurnLedger(
        "生成视频和一张封面图",
        workflow="video_generation",
        workflows=("video_generation", "image"),
    )
    ledger.record_outcome(
        "generate_image",
        {"status": "success", "asset_id": "img-ref", "kind": "image"},
        call_args={"prompt": "internal style reference only; not a cover"},
    )
    ledger.record_outcome(
        "generate_video",
        {"status": "success", "asset_id": "v-final", "kind": "video"},
        call_args={"reference_asset_ids": ["img-ref"]},
    )
    ledger.record_outcome(
        "host_visual_review", {"asset_ids": ["img-ref", "v-final"]}
    )

    assert ledger.final_asset_ids == ["v-final"]
    assert "final_asset_kind:image:missing" in ledger.completion_decision().blockers


def test_reference_is_nondestructive_but_composite_replaces_its_base() -> None:
    ledger = TurnLedger("生成两张图片", workflow="image")
    ledger.record_outcome(
        "generate_image", {"status": "success", "asset_id": "img1", "kind": "image"}
    )
    ledger.record_outcome(
        "generate_image",
        {"status": "success", "asset_id": "img2", "kind": "image"},
        call_args={"reference_asset_ids": ["img1"]},
    )
    assert ledger.final_asset_ids == ["img1", "img2"]

    ledger.record_outcome(
        "composite",
        {"status": "success", "asset_id": "img3", "kind": "image"},
        call_args={"base_asset_id": "img1", "overlay_asset_id": "img2"},
    )
    assert ledger.final_asset_ids == ["img2", "img3"]


@pytest.mark.parametrize(
    "user_text",
    ("在第3秒拆分视频片段", "在5秒处加溶解转场", "第2秒插入标题层"),
)
def test_operation_timestamps_are_not_output_duration_requirements(user_text: str) -> None:
    assert "duration" not in extract_acceptance_criteria(user_text)


def test_transition_kind_position_and_duration_are_exact() -> None:
    ledger = TurnLedger("在第5秒加一个0.5秒溶解转场", workflow="timeline")
    ledger.record_outcome(
        "timeline_add_transition",
        {"status": "success"},
        call_args={"clip_id": "c1", "kind": "wipe", "duration_sec": 0.5},
    )
    assert ledger.steps["op:transition"].status == "open"

    ledger.record_outcome(
        "timeline_add_transition",
        {"status": "success"},
        call_args={"clip_id": "c1", "kind": "dissolve", "duration_sec": 0.5},
    )
    assert ledger.steps["op:transition"].status == "done"


@pytest.mark.parametrize(
    ("user_text", "wrong_range", "right_range"),
    (
        ("截取第2秒到第5秒", (0, 3), (2, 5)),
        ("截取00:02到00:05", (0, 3), (2, 5)),
        ("trim from 2 seconds for 3 seconds", (0, 3), (2, 5)),
        ("keep the first 5 seconds", (10, 15), (0, 5)),
        ("截取前5秒", (10, 15), (0, 5)),
    ),
)
def test_trim_range_variants_enforce_source_offsets(
    user_text: str,
    wrong_range: tuple[float, float],
    right_range: tuple[float, float],
) -> None:
    ledger = TurnLedger(user_text, workflow="video_edit")
    ledger.record_outcome(
        "edit_video",
        {"status": "success", "asset_id": "wrong", "kind": "video"},
        call_args={
            "asset_id": "src",
            "operation": "trim",
            "trim": {"start_sec": wrong_range[0], "end_sec": wrong_range[1]},
        },
    )
    assert ledger.steps["op:trim"].status == "open"

    ledger.record_outcome(
        "edit_video",
        {"status": "success", "asset_id": "right", "kind": "video"},
        call_args={
            "asset_id": "wrong",
            "operation": "trim",
            "trim": {"start_sec": right_range[0], "end_sec": right_range[1]},
        },
    )
    assert ledger.steps["op:trim"].status == "done"


@pytest.mark.parametrize("user_text", ("剪掉前2秒", "remove the first 2 seconds"))
def test_removed_leading_duration_is_not_final_duration(user_text: str) -> None:
    ledger = TurnLedger(user_text, workflow="video_edit")
    assert "duration" not in ledger.criteria
    ledger.record_outcome(
        "edit_video",
        {"status": "success", "asset_id": "trimmed", "kind": "video"},
        call_args={
            "asset_id": "src",
            "operation": "trim",
            "trim": {"start_sec": 2.0, "end_sec": None},
        },
    )
    assert ledger.steps["op:trim"].status == "done"


def test_explicit_subtitle_and_title_text_are_exact() -> None:
    subtitle = TurnLedger("添加字幕“你好世界”", workflow="video_edit")
    subtitle.record_outcome(
        "subtitle",
        {"status": "success", "asset_id": "v-sub", "kind": "video"},
        call_args={"asset_id": "src", "text": "再见"},
    )
    assert subtitle.steps["op:subtitle"].status == "open"

    title = TurnLedger("在第2秒插入标题“开场”", workflow="timeline")
    title.record_outcome(
        "timeline_insert_clip",
        {"status": "success"},
        call_args={"at_time": 2.0, "text": {"content": "结束"}},
    )
    assert title.steps["op:insert"].status == "open"


def test_gif_is_an_image_deliverable_and_can_complete() -> None:
    ledger = TurnLedger("将视频导出为 GIF")
    ledger.record_outcome(
        "export",
        {"status": "success", "asset_id": "img-gif", "kind": "image"},
        call_args={"asset_id": "src", "format": "gif"},
    )
    ledger.record_outcome(
        "probe_media", {"asset_id": "img-gif", "format": "gif", "path": "/tmp/f.gif"}
    )
    ledger.record_outcome("host_visual_review", {"asset_id": "img-gif"})

    assert ledger.expected_final_kinds == frozenset({"image"})
    assert ledger.can_complete() is True
