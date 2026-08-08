"""Appearance editing and render contracts used by the direct canvas UI."""
from __future__ import annotations

import pytest

from lumenframe import apply_layer_patch, empty_doc
from lumenframe.compile import CompileError, compile_to_layer_stack
from lumenframe.ops import LayerPatchError


def patch(*ops):
    return {"version": 1, "ops": list(ops)}


def add_solid(doc, layer_id, color, *, duration=1.0, start=0.0, **fields):
    return apply_layer_patch(doc, patch({
        "op": "add_layer",
        "id": layer_id,
        "type": "solid",
        "color": color,
        "start": start,
        "duration": duration,
        **fields,
    }))


def layer(doc, layer_id):
    from lumenframe.model import find_layer

    found = find_layer(doc, layer_id)
    assert found is not None
    return found


def test_effects_can_be_disabled_and_reordered_without_losing_params():
    doc = add_solid(empty_doc(width=8, height=8, fps=2), "fill", "#FF0000")
    doc = apply_layer_patch(doc, patch(
        {"op": "add_effect", "layer_id": "fill", "effect": {
            "id": "invert", "type": "invert", "params": {"amount": 1.0},
        }},
        {"op": "add_effect", "layer_id": "fill", "effect": {
            "id": "blur", "type": "gaussian_blur", "params": {"radius": 4},
        }},
        {"op": "set_effect_enabled", "layer_id": "fill", "effect_id": "invert", "enabled": False},
        {"op": "reorder_effect", "layer_id": "fill", "effect_id": "blur", "index": 0},
    ))

    effects = layer(doc, "fill")["effects"]
    assert [effect["id"] for effect in effects] == ["blur", "invert"]
    assert effects[1]["enabled"] is False
    assert effects[1]["params"] == {"amount": 1.0}


def test_effect_toggle_and_order_reject_invalid_values():
    doc = add_solid(empty_doc(), "fill", "#FF0000")
    doc = apply_layer_patch(doc, patch({
        "op": "add_effect", "layer_id": "fill",
        "effect": {"id": "blur", "type": "gaussian_blur"},
    }))

    with pytest.raises(LayerPatchError) as enabled_error:
        apply_layer_patch(doc, patch({
            "op": "set_effect_enabled", "layer_id": "fill",
            "effect_id": "blur", "enabled": "false",
        }))
    assert enabled_error.value.code == "E_ARG"

    with pytest.raises(LayerPatchError) as order_error:
        apply_layer_patch(doc, patch({
            "op": "reorder_effect", "layer_id": "fill",
            "effect_id": "blur", "index": 1,
        }))
    assert order_error.value.code == "E_RANGE"


def test_set_gradient_preserves_omitted_fields_and_normalizes_stops():
    doc = apply_layer_patch(empty_doc(width=8, height=8, fps=2), patch({
        "op": "add_gradient",
        "id": "gradient",
        "mode": "linear",
        "stops": [[0.0, "#000000"], [1.0, "#FFFFFF"]],
        "angle": 15,
        "duration": 1.0,
    }))
    doc = apply_layer_patch(doc, patch({
        "op": "set_gradient",
        "layer_id": "gradient",
        "mode": "radial",
        "center": [0.25, 0.75],
        "radius": 0.4,
    }))
    props = layer(doc, "gradient")["props"]
    assert props == {
        "mode": "radial",
        "stops": [[0.0, "#000000"], [1.0, "#FFFFFF"]],
        "angle": 15.0,
        "center": [0.25, 0.75],
        "radius": 0.4,
    }

    doc = apply_layer_patch(doc, patch({
        "op": "set_gradient",
        "layer_id": "gradient",
        "stops": [[1.0, "#33445566"], [0.0, "#112233"]],
    }))
    assert layer(doc, "gradient")["props"]["stops"] == [
        [0.0, "#112233"], [1.0, "#33445566"],
    ]


@pytest.mark.parametrize(
    ("update", "code"),
    [
        ({}, "E_ARG"),
        ({"stops": [[0.0, "#FFF"], [1.0, "#FFFFFF"]]}, "E_ARG"),
        ({"stops": [[-0.1, "#000000"], [1.0, "#FFFFFF"]]}, "E_RANGE"),
        ({"center": [0.5, 1.2]}, "E_RANGE"),
        ({"radius": 0.0}, "E_RANGE"),
        ({"mode": "conic"}, "E_ARG"),
    ],
)
def test_set_gradient_strict_validation(update, code):
    doc = apply_layer_patch(empty_doc(), patch({
        "op": "add_gradient",
        "id": "gradient",
        "stops": [[0.0, "#000000"], [1.0, "#FFFFFF"]],
        "duration": 1.0,
    }))
    with pytest.raises(LayerPatchError) as error:
        apply_layer_patch(doc, patch({
            "op": "set_gradient", "layer_id": "gradient", **update,
        }))
    assert error.value.code == code


def test_set_gradient_rejects_non_gradient_layer():
    doc = add_solid(empty_doc(), "fill", "#FF0000")
    with pytest.raises(LayerPatchError) as error:
        apply_layer_patch(doc, patch({
            "op": "set_gradient", "layer_id": "fill", "angle": 45,
        }))
    assert error.value.code == "E_TYPE"


def test_clip_to_below_uses_source_alpha_in_canvas_coordinates_after_transform():
    doc = add_solid(empty_doc(width=12, height=8, fps=2), "background", "#0000FF")
    doc = add_solid(doc, "source", "#FF0000")
    doc = apply_layer_patch(doc, patch({
        "op": "set_transform", "layer_id": "source", "scale": 0.5, "x": 2,
    }))
    doc = add_solid(doc, "clipped", "#00FF00")
    doc = apply_layer_patch(doc, patch({
        "op": "clip_to_below", "layer_id": "clipped", "enabled": True,
    }))

    frame = compile_to_layer_stack(doc).render_frame(0)
    assert frame[4, 7, :3] == pytest.approx([0.0, 1.0, 0.0], abs=1e-4)
    assert frame[4, 2, :3] == pytest.approx([0.0, 0.0, 1.0], abs=1e-4)


def test_clip_to_below_multiplies_with_both_source_and_clipped_masks():
    doc = add_solid(empty_doc(width=16, height=8, fps=2), "source", "#FF0000")
    doc = apply_layer_patch(doc, patch({
        "op": "set_mask", "layer_id": "source",
        "mask": {"kind": "shape", "shape": {
            "type": "rectangle", "x0": 0.0, "y0": 0.0, "x1": 0.75, "y1": 1.0,
        }},
    }))
    doc = add_solid(doc, "clipped", "#00FF00")
    doc = apply_layer_patch(doc, patch(
        {"op": "set_mask", "layer_id": "clipped", "mask": {
            "kind": "shape", "shape": {
                "type": "rectangle", "x0": 0.25, "y0": 0.0, "x1": 1.0, "y1": 1.0,
            },
        }},
        {"op": "clip_to_below", "layer_id": "clipped", "enabled": True},
    ))

    frame = compile_to_layer_stack(doc).render_frame(0)
    assert frame[4, 2, :3] == pytest.approx([1.0, 0.0, 0.0], abs=1e-4)
    assert frame[4, 8, :3] == pytest.approx([0.0, 1.0, 0.0], abs=1e-4)
    assert frame[4, 14, 3] == pytest.approx(0.0, abs=1e-4)


def test_clip_to_below_uses_source_opacity_and_chains_alpha():
    doc = add_solid(empty_doc(width=4, height=4, fps=2), "source", "#FF0000")
    doc = apply_layer_patch(doc, patch({
        "op": "set_opacity", "layer_id": "source", "opacity": 0.4,
    }))
    doc = add_solid(doc, "clipped", "#00FF00")
    doc = apply_layer_patch(doc, patch({
        "op": "clip_to_below", "layer_id": "clipped", "enabled": True,
    }))

    alpha = compile_to_layer_stack(doc).render_frame(0)[2, 2, 3]
    assert alpha == pytest.approx(0.64, abs=1e-5)


def test_clip_to_below_can_chain_through_an_already_clipped_layer():
    doc = add_solid(empty_doc(width=4, height=4, fps=2), "source", "#FF0000")
    doc = apply_layer_patch(doc, patch({
        "op": "set_opacity", "layer_id": "source", "opacity": 0.5,
    }))
    doc = add_solid(doc, "middle", "#00FF00")
    doc = apply_layer_patch(doc, patch({
        "op": "clip_to_below", "layer_id": "middle", "enabled": True,
    }))
    doc = add_solid(doc, "top", "#0000FF")
    doc = apply_layer_patch(doc, patch({
        "op": "clip_to_below", "layer_id": "top", "enabled": True,
    }))

    alpha = compile_to_layer_stack(doc).render_frame(0)[2, 2, 3]
    assert alpha == pytest.approx(0.875, abs=1e-5)


def test_clip_to_below_without_or_after_inactive_source_is_transparent():
    no_source = add_solid(empty_doc(width=4, height=4, fps=2), "clipped", "#00FF00")
    no_source = apply_layer_patch(no_source, patch({
        "op": "clip_to_below", "layer_id": "clipped", "enabled": True,
    }))
    assert compile_to_layer_stack(no_source).render_frame(0)[2, 2, 3] == pytest.approx(0.0)

    inactive = add_solid(
        empty_doc(width=4, height=4, fps=2), "source", "#FF0000", duration=0.5,
    )
    inactive = add_solid(inactive, "clipped", "#00FF00", duration=1.0)
    inactive = apply_layer_patch(inactive, patch({
        "op": "clip_to_below", "layer_id": "clipped", "enabled": True,
    }))
    stack = compile_to_layer_stack(inactive)
    assert stack.render_frame(0)[2, 2, 1] == pytest.approx(1.0)
    assert stack.render_frame(1)[2, 2, 3] == pytest.approx(0.0)


def test_unknown_enabled_effect_is_an_explicit_compile_error():
    doc = add_solid(empty_doc(width=4, height=4, fps=2), "fill", "#FF0000")
    doc = apply_layer_patch(doc, patch({
        "op": "add_effect", "layer_id": "fill", "type": "lumenframe.test.unknown",
    }))
    with pytest.raises(CompileError, match="unknown effect 'lumenframe.test.unknown'"):
        compile_to_layer_stack(doc).render_frame(0)


def test_extension_effect_failure_is_an_explicit_compile_error(monkeypatch):
    import gemia.registry

    def failing_extension(_frame, **_params):
        raise RuntimeError("extension exploded")

    monkeypatch.setattr(gemia.registry, "resolve", lambda _name: failing_extension)
    doc = add_solid(empty_doc(width=4, height=4, fps=2), "fill", "#FF0000")
    doc = apply_layer_patch(doc, patch({
        "op": "add_effect", "layer_id": "fill", "type": "extension.test.failure",
    }))
    with pytest.raises(CompileError, match="effect 'extension.test.failure' failed: extension exploded"):
        compile_to_layer_stack(doc).render_frame(0)


def test_disabled_unknown_effect_is_still_skipped(monkeypatch):
    import gemia.registry

    def should_not_resolve(_name):
        raise AssertionError("disabled effects must not resolve")

    monkeypatch.setattr(gemia.registry, "resolve", should_not_resolve)
    doc = add_solid(empty_doc(width=4, height=4, fps=2), "fill", "#FF0000")
    doc = apply_layer_patch(doc, patch({
        "op": "add_effect", "layer_id": "fill", "type": "extension.disabled",
        "enabled": False,
    }))
    pixel = compile_to_layer_stack(doc).render_frame(0)[2, 2]
    assert pixel == pytest.approx([1.0, 0.0, 0.0, 1.0], abs=1e-5)
