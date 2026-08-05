"""Small deterministic production probes that cannot replace the main film.

The probes exercise intent scoping and local motion-graphics repair.  They are
kept as ordinary project data so their behavior is replayable and diffable.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping


_LETTERS = tuple("Acrab")
_LANDING_FRAMES = (24, 54, 84, 114, 144)
_LETTER_X = (250.0, 430.0, 610.0, 790.0, 970.0)


def build_acrab_bounce_plan() -> dict[str, Any]:
    """Return a yellow ball bouncing across A-c-r-a-b in sequence."""

    total_frames = 180
    layers: list[dict[str, Any]] = [
        {
            "id": "background",
            "type": "solid",
            "color": [0.025, 0.035, 0.065, 1.0],
            "position": [0, 0],
            "size": [1280, 720],
            "duration": total_frames,
            "z_index": 0,
        }
    ]
    for index, (letter, x) in enumerate(zip(_LETTERS, _LETTER_X)):
        layers.append(
            {
                "id": f"letter-{index}-{letter.lower()}",
                "type": "text",
                "text": letter,
                "position": [x, 430.0],
                "font_config": {"size": 118, "color": [0.957, 0.969, 1.0, 1.0]},
                "duration": total_frames,
                "z_index": 10,
            }
        )

    points: list[dict[str, Any]] = [
        {"frame": 0, "value": [_LETTER_X[0], 190.0]},
    ]
    previous_frame = 0
    previous_x = _LETTER_X[0]
    for landing_frame, x in zip(_LANDING_FRAMES, _LETTER_X):
        midpoint = previous_frame + max(1, (landing_frame - previous_frame) // 2)
        points.append(
            {
                "frame": midpoint,
                "value": [round((previous_x + x) / 2.0, 3), 170.0],
                "ease": "out_quad",
            }
        )
        points.append(
            {
                "frame": landing_frame,
                "value": [x, 382.0],
                "ease": "in_quad",
                "landing_index": len(points) // 2,
            }
        )
        previous_frame, previous_x = landing_frame, x
    points.append({"frame": 179, "value": [_LETTER_X[-1], 170.0]})
    layers.append(
        {
            "id": "yellow-ball",
            "type": "solid",
            "color": [1.0, 0.82, 0.06, 1.0],
            "position": [_LETTER_X[0], 190.0],
            "size": [58, 58],
            "duration": total_frames,
            "z_index": 20,
            "keyframes": {"position": {"points": points}},
        }
    )
    return {
        "version": 1,
        "width": 1280,
        "height": 720,
        "fps": 30,
        "total_frames": total_frames,
        "layers": layers,
        "metadata": {
            "probe": "acrab-yellow-ball-sequential-bounce",
            "letters": list(_LETTERS),
            "landing_frames": list(_LANDING_FRAMES),
            "deterministic": True,
            "not_a_main_case": True,
        },
    }


def revise_acrab_bounce_landing(
    plan: Mapping[str, Any],
    *,
    letter_index: int,
    x_offset: float = 0.0,
    y_offset: float = 0.0,
) -> dict[str, Any]:
    """Patch exactly one letter landing without rebuilding the composition."""

    if letter_index < 0 or letter_index >= len(_LETTERS):
        raise ValueError(f"letter_index must be in 0..{len(_LETTERS) - 1}")
    updated = deepcopy(dict(plan))
    layers = updated.get("layers") if isinstance(updated.get("layers"), list) else []
    ball = next(
        (layer for layer in layers if isinstance(layer, dict) and layer.get("id") == "yellow-ball"),
        None,
    )
    if ball is None:
        raise ValueError("plan has no yellow-ball layer")
    position = ball.get("keyframes", {}).get("position", {})
    points = position.get("points") if isinstance(position, dict) else None
    if not isinstance(points, list):
        raise ValueError("yellow-ball layer has no position keyframes")
    target_frame = _LANDING_FRAMES[letter_index]
    target = next(
        (
            point
            for point in points
            if isinstance(point, dict) and int(point.get("frame") or -1) == target_frame
        ),
        None,
    )
    if target is None:
        raise ValueError(f"yellow-ball landing frame is missing: {target_frame}")
    value = target.get("value") if isinstance(target.get("value"), list) else []
    if len(value) != 2:
        raise ValueError("yellow-ball landing value must be [x,y]")
    target["value"] = [float(value[0]) + float(x_offset), float(value[1]) + float(y_offset)]
    updated.setdefault("metadata", {})["last_local_revision"] = {
        "letter_index": letter_index,
        "landing_frame": target_frame,
        "x_offset": float(x_offset),
        "y_offset": float(y_offset),
    }
    return updated


__all__ = ["build_acrab_bounce_plan", "revise_acrab_bounce_landing"]
