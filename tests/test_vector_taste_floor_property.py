"""G1 (charter §7) — brief-space property-fuzz taste floor for ``vector_motion``.

The per-behaviour / per-(intent,style) tests in ``test_vector_creative.py``
assert the floor on FIXED points. G1 requires a PROPERTY fuzz: sample the brief
space (intent × style × subject × duration × seed) and assert the CORE
taste-floor invariants hold on EVERY synthesized scene — an amateur output must
be structurally unreachable, not merely absent from the hand-picked examples.

Paired with an anti-triviality meta-test (``test_taste_floor_test_is_nontrivial``)
proving the floor assertions have teeth: a deliberately-broken scene fails them,
so the fuzz cannot pass vacuously.
"""
from __future__ import annotations

import random

import pytest

from lumenframe.vector import api, choreography, styles
from lumenframe.vector import scene as vscene
from lumenframe.vector.svg import compile_scene

_ALL_STYLES = sorted(styles.STYLES)
_ALL_INTENTS = sorted(choreography.INTENT_ARCS)
# Proven-valid subjects only (an invalid subject would be a brief error, not a
# floor breach — the fuzz targets the floor, not input validation).
_SUBJECTS = [
    {"kind": "logo_text", "text": "Lumeri", "mark": "ring"},
    {"kind": "logo_text", "text": "Aurora", "mark": "ring"},
    {"kind": "abstract"},
]
_DURATIONS = [1.5, 4.0, 8.0]


def _iter_tracks(scene: dict):
    """Yield (owner_label, points) over node AND particle-instance tracks."""
    for node in vscene.walk(scene):
        for prop, points in (node.get("tracks") or {}).items():
            yield f"{node['id']}.{prop}", points
        if node.get("kind") == "particles":
            instances = (node.get("particles") or {}).get("instances") or []
            for k, inst in enumerate(instances):
                for prop, points in (inst.get("tracks") or {}).items():
                    yield f"{node['id']}[{k}].{prop}", points


def _assert_keyframes_within_and_sorted(scene: dict, duration: float) -> None:
    """Floor: every animation keyframe is t-sorted and inside [0, duration].
    A scene that flings a keyframe past its window animates garbage."""
    eps = 1e-6
    for owner, points in _iter_tracks(scene):
        times = [float(p["t"]) for p in points]
        assert times == sorted(times), f"{owner}: keyframes not t-sorted: {times}"
        for t in times:
            assert -eps <= t <= duration + eps, (
                f"{owner}: keyframe t={t} escapes [0, {duration}]"
            )


def _assert_svg_floor(svg: str) -> None:
    """Floor: no CSS default-ease reliance (every animated element rule declares
    an animation-timing-function) and the SVG is render-safe / local-only."""
    assert "<style>" in svg, "compiled scene must carry a <style> block"
    style_block = svg.split("<style>", 1)[1].split("</style>", 1)[0]
    for rule in (r for r in style_block.split("}") if "animation-name:" in r):
        assert "animation-timing-function:" in rule, (
            "an animated element rule relies on the CSS default ease (would "
            f"render 'ease' where the engine chose a curve): {rule.strip()[:80]}"
        )
    assert "url(" not in style_block, "style block must be url()-free"
    for forbidden in ("data:", "javascript:", "<script"):
        assert forbidden not in svg, f"SVG must be local-only (found {forbidden!r})"


def _fuzz_briefs(n: int, seed: int = 20260717):
    rng = random.Random(seed)
    for _ in range(n):
        yield {
            "subject": rng.choice(_SUBJECTS),
            "intent": rng.choice(_ALL_INTENTS),
            "style": rng.choice(_ALL_STYLES),
            "duration": rng.choice(_DURATIONS),
            "seed": rng.randint(0, 9999),
        }


def test_taste_floor_holds_across_brief_space() -> None:
    """Every fuzzed brief yields a scene that validates and whose animation
    respects the floor (keyframes in-window & t-sorted, no default ease,
    render-safe SVG)."""
    checked = 0
    for brief in _fuzz_briefs(60):
        scene = api.build_scene(brief)["scene"]
        vscene.validate_scene(scene)  # structural validity (must not raise)
        _assert_keyframes_within_and_sorted(scene, float(brief["duration"]))
        _assert_svg_floor(compile_scene(scene))
        checked += 1
    assert checked == 60


@pytest.mark.parametrize("intent", _ALL_INTENTS)
@pytest.mark.parametrize("style", _ALL_STYLES)
def test_taste_floor_holds_on_every_intent_style_pair(intent: str, style: str) -> None:
    """Exhaustive grid corner: every intent×style pair must satisfy the floor
    (a random fuzz can under-sample the grid)."""
    brief = {"subject": _SUBJECTS[0], "intent": intent, "style": style,
             "duration": 4.0, "seed": 11}
    scene = api.build_scene(brief)["scene"]
    vscene.validate_scene(scene)
    _assert_keyframes_within_and_sorted(scene, 4.0)
    _assert_svg_floor(compile_scene(scene))


def test_taste_floor_test_is_nontrivial() -> None:
    """Anti-triviality: the floor assertions must FAIL on a deliberately-broken
    scene — otherwise the fuzz passes vacuously."""
    scene = api.build_scene(
        {"subject": _SUBJECTS[0], "intent": "reveal", "style": "playful",
         "duration": 4.0, "seed": 11})["scene"]
    _assert_keyframes_within_and_sorted(scene, 4.0)  # the honest scene passes

    # Break it: fling a keyframe far past the window on the first track found.
    broke = False
    for owner, points in _iter_tracks(scene):
        if points:
            points.append({"t": 999.0})
            broke = True
            break
    assert broke, "expected the synthesized scene to carry at least one track"
    with pytest.raises(AssertionError):
        _assert_keyframes_within_and_sorted(scene, 4.0)
