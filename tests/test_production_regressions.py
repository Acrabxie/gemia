from __future__ import annotations

import json

from gemia.production_regressions import (
    build_acrab_bounce_plan,
    revise_acrab_bounce_landing,
)
from gemia.video.layers import execute_layer_plan


def test_acrab_bounce_is_deterministic_and_sequential() -> None:
    first = build_acrab_bounce_plan()
    second = build_acrab_bounce_plan()
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
    assert first["metadata"]["letters"] == list("Acrab")
    assert first["metadata"]["landing_frames"] == [24, 54, 84, 114, 144]

    ball = next(layer for layer in first["layers"] if layer["id"] == "yellow-ball")
    landings = [
        point
        for point in ball["keyframes"]["position"]["points"]
        if point["frame"] in first["metadata"]["landing_frames"]
    ]
    assert [point["value"][0] for point in landings] == [250, 430, 610, 790, 970]


def test_acrab_bounce_compiles_to_playable_frame_stack() -> None:
    plan = build_acrab_bounce_plan()
    stack = execute_layer_plan(plan)
    first = stack.render_frame(0)
    middle = stack.render_frame(84)
    last = stack.render_frame(179)
    assert first.shape == (720, 1280, 4)
    assert middle.shape == first.shape == last.shape
    assert float(abs(first - middle).sum()) > 1.0
    assert float(abs(middle - last).sum()) > 1.0


def test_acrab_bounce_local_repair_changes_only_target_landing() -> None:
    original = build_acrab_bounce_plan()
    revised = revise_acrab_bounce_landing(
        original,
        letter_index=2,
        x_offset=12,
        y_offset=-8,
    )
    original_ball = next(layer for layer in original["layers"] if layer["id"] == "yellow-ball")
    revised_ball = next(layer for layer in revised["layers"] if layer["id"] == "yellow-ball")
    original_points = original_ball["keyframes"]["position"]["points"]
    revised_points = revised_ball["keyframes"]["position"]["points"]
    changed = [
        index
        for index, (before, after) in enumerate(zip(original_points, revised_points))
        if before != after
    ]
    assert len(changed) == 1
    assert revised_points[changed[0]]["frame"] == 84
    assert original["layers"][:-1] == revised["layers"][:-1]
