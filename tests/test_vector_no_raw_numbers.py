"""G3 (charter §7) — no raw craft numbers on ``vector_motion``'s creative face.

P2/G3: the model tunes vector_motion only through SEMANTIC axes (0..1) and named
vocabulary — never raw geometry/easing recipe numbers. This pins:
  1. the tunable axis set is the closed, normalized ``SEMANTIC_AXES`` (drift);
  2. the schema exposes no raw-geometry knob (op/brief/place/layer_id/feedback);
  3. the model-facing text (tool schema + behaviour catalog) trips no
     closed-domain craft-recipe pattern — reusing the adversarially-tested
     ``find_craft_leak`` detector.
Anti-triviality: ``find_craft_leak`` DOES catch a planted motion recipe, so the
clean result in (3) is meaningful (charter placement exemption keeps duration/
seed/start/lane legal).
"""
from __future__ import annotations

from gemia.lus import find_craft_leak
from gemia.tools._schema import TOOL_SCHEMAS
from lumenframe.vector import behaviors
from lumenframe.vector.behaviors import BEHAVIOR_CATALOG
from lumenframe.vector.params import NEUTRAL, SEMANTIC_AXES


def _vector_motion_fn() -> dict:
    for tool in TOOL_SCHEMAS:
        fn = tool.get("function", {})
        if fn.get("name") == "vector_motion":
            return fn
    raise AssertionError("vector_motion not found in TOOL_SCHEMAS")


def test_semantic_axes_are_the_closed_normalized_set() -> None:
    """The creative knobs are exactly the 7 named 0..1 axes — a closed set, no
    raw params (drift-pin)."""
    assert SEMANTIC_AXES == (
        "energy", "smoothness", "playfulness", "elegance",
        "complexity", "density", "organicness",
    )
    assert set(NEUTRAL) == set(SEMANTIC_AXES)
    assert all(0.0 <= v <= 1.0 for v in NEUTRAL.values())


def test_schema_exposes_no_raw_geometry_knob() -> None:
    """vector_motion's params are op/brief/place/layer_id/feedback — none is a
    raw-geometry / easing tuning param, so the model cannot hand-set craft
    numbers through the tool face."""
    props = _vector_motion_fn()["parameters"]["properties"]
    forbidden = {"x", "y", "dx", "dy", "stagger", "easing", "cubic_bezier",
                 "bezier", "keyframes", "control_points", "offset", "coords"}
    leaked = set(props) & forbidden
    assert not leaked, f"vector_motion schema exposes raw-craft knob(s): {leaked}"


def test_creative_face_carries_no_craft_recipe() -> None:
    """The model-facing text (schema description + param descriptions + the
    behaviour catalog) trips no closed-domain craft-recipe pattern."""
    behaviors._load()
    fn = _vector_motion_fn()
    texts: list[str] = [str(fn.get("description", ""))]
    for prop in fn["parameters"]["properties"].values():
        if isinstance(prop, dict) and prop.get("description"):
            texts.append(str(prop["description"]))
    for entry in BEHAVIOR_CATALOG:
        texts.append(str(entry.get("summary", "")))
        texts.append(str(entry.get("name", "")))

    for text in texts:
        leak = find_craft_leak(text)
        assert leak is None, (
            f"vector_motion creative face carries a raw {leak[0]} craft recipe "
            f"(closed by `{leak[1]}`): {text[:110]!r}"
        )


def test_no_raw_numbers_check_is_nontrivial() -> None:
    """Anti-triviality: ``find_craft_leak`` fires on a planted motion recipe, so
    the clean creative-face result is not vacuous."""
    planted = "easing: [0.42, 0.0, 0.58, 1.0]"
    leak = find_craft_leak(planted)
    assert leak is not None and leak[0] == "motion", (
        "the craft detector must catch a raw motion easing recipe"
    )
