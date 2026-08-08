"""Tests for lumenframe layer editing tools (gemia.tools.layer)."""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from gemia.tools._context import AssetRegistry, ProgressUpdate, ToolContext
from gemia.tools import layer as layer_module

# Import lumenframe directly for test utilities
from lumenframe import find_layer


@pytest.fixture
def tmp_session(tmp_path: Path) -> ToolContext:
    """Create a minimal ToolContext for layer tests.

    Each test gets a unique session_id to avoid cross-test state pollution
    in the _DOC_CACHE.
    """
    import uuid
    session_id = f"test_layer_{uuid.uuid4().hex[:8]}"
    return ToolContext(
        session_id=session_id,
        output_dir=tmp_path,
        registry=AssetRegistry(),
        emit_progress=lambda _: None,
    )


def test_get_lumenframe_empty_doc(tmp_session: ToolContext) -> None:
    """Test get_lumenframe on a fresh, empty document."""
    result = asyncio.run(layer_module.dispatch_get({}, tmp_session))

    assert result["applied"] is True
    assert "canvas" in result
    assert "root_layers" in result
    assert result["layer_details"] == []
    assert result["layer_details_truncated"] is False
    assert "selection_ids" in result
    assert result["canvas"]["width"] == 1920
    assert result["canvas"]["height"] == 1080


def test_get_lumenframe_returns_bounded_complex_layer_details(
    tmp_session: ToolContext,
) -> None:
    inline_alpha = [1.0] * 4096
    result = asyncio.run(
        layer_module.dispatch_patch(
            {
                "ops": [
                    {
                        "op": "add_gradient",
                        "id": "gradient_detail",
                        "name": "Backdrop",
                        "mode": "linear",
                        "stops": [[0.0, "#101827"], [1.0, "#6EE7F9"]],
                        "angle": 25,
                        "duration": 3.0,
                    },
                    {
                        "op": "set_transform",
                        "layer_id": "gradient_detail",
                        "x": 12,
                        "y": -8,
                        "scale": 1.25,
                        "rotation": 15,
                    },
                    {
                        "op": "set_opacity",
                        "layer_id": "gradient_detail",
                        "opacity": 0.4,
                    },
                    {
                        "op": "set_blend_mode",
                        "layer_id": "gradient_detail",
                        "blend_mode": "multiply",
                    },
                    {
                        "op": "clip_to_below",
                        "layer_id": "gradient_detail",
                        "enabled": True,
                    },
                    {
                        "op": "add_effect",
                        "layer_id": "gradient_detail",
                        "effect": {
                            "id": "blur_detail",
                            "type": "gaussian_blur",
                            "enabled": False,
                            "params": {
                                "radius": 12,
                                "curve": list(range(100)),
                            },
                        },
                    },
                    {
                        "op": "set_mask",
                        "layer_id": "gradient_detail",
                        "mask": {
                            "kind": "pixel",
                            "width": 64,
                            "height": 64,
                            "alpha": inline_alpha,
                            "data": inline_alpha,
                            "feather": 2,
                        },
                    },
                    {
                        "op": "add_shape",
                        "id": "shape_detail",
                        "name": "Orb",
                        "kind": "ellipse",
                        "fill": "#FF00AA",
                        "rect": [0.1, 0.2, 0.9, 0.8],
                        "duration": 2.0,
                    },
                ],
            },
            tmp_session,
        )
    )
    assert result["applied"] is True

    inspected = asyncio.run(layer_module.dispatch_get({}, tmp_session))
    assert "Backdrop" in inspected["root_layers"]
    assert "Orb" in inspected["root_layers"]
    details = {item["id"]: item for item in inspected["layer_details"]}

    gradient = details["gradient_detail"]
    assert gradient["type"] == "gradient"
    assert gradient["name"] == "Backdrop"
    assert gradient["timing"] == {"start": 0.0, "duration": 3.0, "end": 3.0}
    assert gradient["opacity"] == 0.4
    assert gradient["blend_mode"] == "multiply"
    assert gradient["clip_to_below"] is True
    assert gradient["transform"] == {
        "x": 12.0,
        "y": -8.0,
        "scale_x": 1.25,
        "scale_y": 1.25,
        "rotation": 15.0,
        "anchor_x": 0.5,
        "anchor_y": 0.5,
    }
    assert gradient["effects"][0]["id"] == "blur_detail"
    assert gradient["effects"][0]["type"] == "gaussian_blur"
    assert gradient["effects"][0]["enabled"] is False
    assert gradient["effects"][0]["params"]["radius"] == 12
    assert gradient["effects"][0]["params"]["curve"][-1] == {
        "_truncated_items": 68,
    }
    assert gradient["mask"] == {
        "kind": "pixel",
        "invert": False,
        "feather": 2.0,
        "width": 64,
        "height": 64,
        "has_inline_data": True,
    }
    assert gradient["props"] == {
        "mode": "linear",
        "stops": [[0.0, "#101827"], [1.0, "#6EE7F9"]],
        "angle": 25.0,
    }
    assert details["shape_detail"]["props"] == {
        "kind": "ellipse",
        "fill": "#FF00AA",
        "rect": [0.1, 0.2, 0.9, 0.8],
        "opacity_baked": False,
    }


def test_get_lumenframe_caps_layer_details(tmp_session: ToolContext) -> None:
    doc = layer_module.get_lumendoc_snapshot(tmp_session)
    doc["root"]["children"] = [
        layer_module.new_layer("solid", id=f"detail_{index}")
        for index in range(layer_module._LAYER_DETAILS_LIMIT + 2)
    ]
    assert layer_module._save_lumendoc(tmp_session, doc) is True

    result = asyncio.run(layer_module.dispatch_get({}, tmp_session))
    assert len(result["layer_details"]) == layer_module._LAYER_DETAILS_LIMIT
    assert result["layer_details"][0]["id"] == "detail_0"
    assert result["layer_details"][-1]["id"] == (
        f"detail_{layer_module._LAYER_DETAILS_LIMIT - 1}"
    )
    assert result["layer_details_truncated"] is True


def test_lumen_patch_add_single_layer(tmp_session: ToolContext) -> None:
    """Test adding a single layer via lumen_patch."""
    ops = [
        {
            "op": "add_layer",
            "type": "video",
            "name": "my_video",
        }
    ]
    result = asyncio.run(layer_module.dispatch_patch({"ops": ops}, tmp_session))

    assert result["applied"] is True
    assert result["ops_count"] == 1
    assert "my_video" in result["root_layers"]


def test_lumen_add_layer_convenience(tmp_session: ToolContext) -> None:
    """Test the convenience lumen_add_layer verb."""
    result = asyncio.run(
        layer_module.dispatch_add_layer(
            {"type": "image", "name": "photo"},
            tmp_session
        )
    )

    assert result["applied"] is True
    assert "photo" in result["root_layers"]


def test_lumen_patch_multiple_ops(tmp_session: ToolContext) -> None:
    """Test applying multiple ops in one patch."""
    ops = [
        {"op": "add_layer", "type": "video", "name": "v1"},
        {"op": "add_layer", "type": "audio", "name": "a1"},
        {"op": "add_layer", "type": "text", "name": "title"},
    ]
    result = asyncio.run(layer_module.dispatch_patch({"ops": ops}, tmp_session))

    assert result["applied"] is True
    assert result["ops_count"] == 3
    tree = result["root_layers"]
    assert "v1" in tree
    assert "a1" in tree
    assert "title" in tree


def test_compact_tree_summary_shows_full_layer_id() -> None:
    """The layer tree must show full ids — the model deletes/patches by this
    exact string. A 12-char truncation once turned "shape_1571eb06a035" into
    "shape_1571eb", so every edit copied from the tree failed E_NOT_FOUND."""
    root = {
        "id": "root",
        "type": "composition",
        "name": "Root",
        "visible": True,
        "children": [
            {"id": "shape_1571eb06a035", "type": "shape", "name": "Orb", "visible": True},
            {"id": "text_f384694679cf", "type": "text", "name": "Title", "visible": True},
        ],
    }
    tree = layer_module._compact_tree_summary(root)
    assert "shape_1571eb06a035" in tree
    assert "text_f384694679cf" in tree


def test_get_tree_ids_are_addressable(tmp_session: ToolContext) -> None:
    """The id shown in root_layers must be the real, deletable id: adding a
    layer then deleting it by the id read back from the tree must succeed."""
    asyncio.run(
        layer_module.dispatch_add_layer(
            {"type": "shape", "name": "Vector Motion Demo"}, tmp_session
        )
    )
    get_result = asyncio.run(layer_module.dispatch_get({}, tmp_session))
    layer_id = get_result["selection_ids"][0]
    # The full id (not a truncated prefix) is present verbatim in the tree.
    assert layer_id in get_result["root_layers"]
    delete_result = asyncio.run(
        layer_module.dispatch_delete_layer({"layer_id": layer_id}, tmp_session)
    )
    assert delete_result["applied"] is True


def test_lumen_set_opacity(tmp_session: ToolContext) -> None:
    """Test setting layer opacity."""
    # First add a layer
    add_result = asyncio.run(
        layer_module.dispatch_add_layer(
            {"type": "video", "name": "transparent"},
            tmp_session
        )
    )
    assert add_result["applied"] is True

    # Get the doc to extract the layer id
    get_result = asyncio.run(layer_module.dispatch_get({}, tmp_session))
    doc = layer_module._lumendoc(tmp_session)
    root = doc.get("root", {})
    children = root.get("children", [])
    assert len(children) > 0
    layer_id = children[0]["id"]

    # Set opacity
    opacity_result = asyncio.run(
        layer_module.dispatch_set_opacity(
            {"layer_id": layer_id, "opacity": 0.5},
            tmp_session
        )
    )

    assert opacity_result["applied"] is True
    doc = layer_module._lumendoc(tmp_session)
    layer = find_layer(doc, layer_id)
    assert layer is not None
    assert layer.get("opacity") == 0.5


def test_lumen_set_transform(tmp_session: ToolContext) -> None:
    """Test setting layer transform (position, scale, rotation)."""
    # Add a layer
    add_result = asyncio.run(
        layer_module.dispatch_add_layer(
            {"type": "video", "name": "moving"},
            tmp_session
        )
    )
    doc = layer_module._lumendoc(tmp_session)
    root = doc.get("root", {})
    children = root.get("children", [])
    layer_id = children[0]["id"]

    # Set transform
    result = asyncio.run(
        layer_module.dispatch_set_transform(
            {
                "layer_id": layer_id,
                "x": 100,
                "y": 200,
                "scale": 1.5,
                "rotation": 45,
            },
            tmp_session
        )
    )

    assert result["applied"] is True
    doc = layer_module._lumendoc(tmp_session)
    layer = find_layer(doc, layer_id)
    assert layer is not None
    transform = layer.get("transform", {})
    assert transform.get("x") == 100
    assert transform.get("y") == 200
    assert transform.get("scale_x") == 1.5
    assert transform.get("scale_y") == 1.5
    assert transform.get("rotation") == 45


def test_lumen_set_mask_convenience_accepts_pixel_alpha(tmp_session: ToolContext) -> None:
    add_result = asyncio.run(
        layer_module.dispatch_add_layer(
            {"type": "solid", "name": "masked"},
            tmp_session,
        )
    )
    assert add_result["applied"] is True
    doc = layer_module._lumendoc(tmp_session)
    layer_id = doc["root"]["children"][0]["id"]

    result = asyncio.run(
        layer_module.dispatch_set_mask(
            {
                "layer_id": layer_id,
                "kind": "pixel",
                "alpha": [[1, 0], [1, 0]],
                "feather": 0,
            },
            tmp_session,
        )
    )

    assert result["applied"] is True
    layer = find_layer(layer_module._lumendoc(tmp_session), layer_id)
    assert layer is not None
    assert layer["mask"]["kind"] == "pixel"
    assert layer["mask"]["alpha"] == [[1, 0], [1, 0]]


def test_lumen_key_replaces_existing_keying_effect(tmp_session: ToolContext) -> None:
    asyncio.run(layer_module.dispatch_add_layer({"type": "solid", "name": "keyed"}, tmp_session))
    doc = layer_module._lumendoc(tmp_session)
    layer_id = doc["root"]["children"][0]["id"]

    first = asyncio.run(
        layer_module.dispatch_key(
            {"layer_id": layer_id, "method": "chroma", "key_color": "#00FF00"},
            tmp_session,
        )
    )
    assert first["applied"] is True
    second = asyncio.run(
        layer_module.dispatch_key(
            {"layer_id": layer_id, "method": "advanced_chroma", "key_color": "#0000FF", "spill": 0.75},
            tmp_session,
        )
    )
    assert second["applied"] is True

    layer = find_layer(layer_module._lumendoc(tmp_session), layer_id)
    assert layer is not None
    effects = layer.get("effects") or []
    assert [effect["type"] for effect in effects] == ["advanced_chroma_key"]
    assert effects[0]["params"]["key_color"] == "#0000FF"
    assert effects[0]["params"]["spill"] == 0.75


def test_lumen_set_visibility(tmp_session: ToolContext) -> None:
    """Test hiding and showing layers."""
    # Add a layer
    add_result = asyncio.run(
        layer_module.dispatch_add_layer(
            {"type": "video", "name": "toggle_me"},
            tmp_session
        )
    )
    doc = layer_module._lumendoc(tmp_session)
    root = doc.get("root", {})
    children = root.get("children", [])
    layer_id = children[0]["id"]

    # Hide it
    hide_result = asyncio.run(
        layer_module.dispatch_set_visibility(
            {"layer_id": layer_id, "visible": False},
            tmp_session
        )
    )
    assert hide_result["applied"] is True

    doc = layer_module._lumendoc(tmp_session)
    layer = find_layer(doc, layer_id)
    assert layer is not None
    assert layer.get("visible") is False

    # Show it
    show_result = asyncio.run(
        layer_module.dispatch_set_visibility(
            {"layer_id": layer_id, "visible": True},
            tmp_session
        )
    )
    assert show_result["applied"] is True

    doc = layer_module._lumendoc(tmp_session)
    layer = layer_module.find_layer(doc, layer_id)
    assert layer.get("visible") is True


def test_lumen_delete_layer(tmp_session: ToolContext) -> None:
    """Test deleting layers."""
    # Add three layers
    for i, name in enumerate(["v1", "v2", "v3"]):
        asyncio.run(
            layer_module.dispatch_add_layer(
                {"type": "video", "name": name},
                tmp_session
            )
        )

    doc = layer_module._lumendoc(tmp_session)
    root = doc.get("root", {})
    children = root.get("children", [])
    assert len(children) == 3

    layer_id_to_delete = children[1]["id"]

    # Delete the middle one
    result = asyncio.run(
        layer_module.dispatch_delete_layer(
            {"layer_id": layer_id_to_delete},
            tmp_session
        )
    )
    assert result["applied"] is True

    doc = layer_module._lumendoc(tmp_session)
    root = doc.get("root", {})
    children = root.get("children", [])
    assert len(children) == 2
    assert all(c["id"] != layer_id_to_delete for c in children)


def test_lumen_move_layer(tmp_session: ToolContext) -> None:
    """Test moving a layer (reorder, reparent, retime)."""
    # Add a composition and some layers
    ops = [
        {"op": "add_layer", "type": "composition", "name": "comp1"},
        {"op": "add_layer", "type": "video", "name": "v1"},
    ]
    asyncio.run(layer_module.dispatch_patch({"ops": ops}, tmp_session))

    doc = layer_module._lumendoc(tmp_session)
    root = doc.get("root", {})
    children = root.get("children", [])
    comp_id = children[0]["id"]
    v1_id = children[1]["id"]

    # Move v1 into comp1 at start time 1.0
    result = asyncio.run(
        layer_module.dispatch_move_layer(
            {
                "layer_id": v1_id,
                "parent_id": comp_id,
                "start": 1.0,
            },
            tmp_session
        )
    )
    if not result["applied"]:
        pytest.fail(f"move_layer failed: {result}")
    assert result["applied"] is True

    doc = layer_module._lumendoc(tmp_session)
    comp = layer_module.find_layer(doc, comp_id)
    assert comp is not None
    comp_children = comp.get("children", [])
    assert len(comp_children) == 1
    assert comp_children[0]["id"] == v1_id
    assert comp_children[0]["start"] == 1.0


def test_lumen_select(tmp_session: ToolContext) -> None:
    """Test changing selection."""
    # Add two layers
    for name in ["a", "b"]:
        asyncio.run(
            layer_module.dispatch_add_layer(
                {"type": "video", "name": name},
                tmp_session
            )
        )

    doc = layer_module._lumendoc(tmp_session)
    root = doc.get("root", {})
    children = root.get("children", [])
    id_a = children[0]["id"]
    id_b = children[1]["id"]

    # Select both
    result = asyncio.run(
        layer_module.dispatch_select(
            {"layer_ids": [id_a, id_b], "mode": "replace"},
            tmp_session
        )
    )
    assert result["applied"] is True

    doc = layer_module._lumendoc(tmp_session)
    selection = doc.get("selection", [])
    assert set(selection) == {id_a, id_b}


def test_lumen_patch_error_handling(tmp_session: ToolContext) -> None:
    """Test that invalid patches return structured errors."""
    # Try to add a layer with unknown type
    ops = [
        {"op": "add_layer", "type": "unknown_type", "name": "bad"}
    ]
    result = asyncio.run(layer_module.dispatch_patch({"ops": ops}, tmp_session))

    assert result["applied"] is False
    assert "error_code" in result
    assert "error_message" in result


def test_lumen_patch_missing_args(tmp_session: ToolContext) -> None:
    """Test that malformed ops raise errors."""
    # add_layer with unknown type should fail validation
    ops = [{"op": "add_layer", "type": "nonexistent_type"}]
    result = asyncio.run(layer_module.dispatch_patch({"ops": ops}, tmp_session))

    assert result["applied"] is False
    assert "error_code" in result


# The slimmed model-facing lumenframe surface: everything layer-editing goes
# through lumen_patch ops; the 18 lumen_* convenience verbs were removed from
# the schema (their dispatchers remain in _REAL for internal use only).
CURRENT_LUMEN_SURFACE = [
    "get_lumenframe",
    "lumen_patch",
    "lumen_comp_to_timeline",
    "lumen_render",
    "lumen_render_range",
    "lumen_seek",
]

# Representative removed convenience verbs — must NOT be model-facing anymore.
REMOVED_LUMEN_VERBS = [
    "lumen_add_layer",
    "lumen_set_transform",
    "lumen_set_opacity",
    "lumen_delete_layer",
    "lumen_move_layer",
    "lumen_set_visibility",
    "lumen_select",
]


def test_tool_registration_in_dispatcher() -> None:
    """Verify the current lumenframe surface is registered in DISPATCHER and
    the removed convenience verbs are no longer exposed."""
    from gemia.tools import DISPATCHER

    for name in CURRENT_LUMEN_SURFACE:
        assert name in DISPATCHER, f"{name} missing from DISPATCHER"

    for name in REMOVED_LUMEN_VERBS:
        assert name not in DISPATCHER, (
            f"{name} was removed from the model-facing surface but is still "
            "in DISPATCHER"
        )


def test_tool_schemas_in_registry() -> None:
    """Verify the current lumenframe tool schemas are in TOOL_SCHEMAS and the
    removed convenience-verb schemas are gone."""
    from gemia.tools import TOOL_SCHEMAS

    tool_names = [t["function"]["name"] for t in TOOL_SCHEMAS]

    for name in CURRENT_LUMEN_SURFACE:
        assert name in tool_names, f"{name} missing from TOOL_SCHEMAS"

    for name in REMOVED_LUMEN_VERBS:
        assert name not in tool_names, (
            f"{name} was removed from the model-facing surface but still has "
            "a schema in TOOL_SCHEMAS"
        )


def test_lumenframe_doc_persistence_across_calls(tmp_session: ToolContext) -> None:
    """Verify that the document state persists across tool calls."""
    # Add a layer
    asyncio.run(
        layer_module.dispatch_add_layer(
            {"type": "video", "name": "persistent"},
            tmp_session
        )
    )

    # Get the doc and inspect it
    get_result = asyncio.run(layer_module.dispatch_get({}, tmp_session))
    assert "persistent" in get_result["root_layers"]

    # Call a different dispatcher, then check the layer is still there
    doc = layer_module._lumendoc(tmp_session)
    root = doc.get("root", {})
    children = root.get("children", [])
    layer_id = children[0]["id"]

    asyncio.run(
        layer_module.dispatch_set_opacity(
            {"layer_id": layer_id, "opacity": 0.7},
            tmp_session
        )
    )

    # Check that the layer is still there with updated opacity
    doc = layer_module._lumendoc(tmp_session)
    layer = find_layer(doc, layer_id)
    assert layer is not None
    assert layer["name"] == "persistent"
    assert layer.get("opacity") == 0.7


def test_delete_layer_removes_from_tree(tmp_session: ToolContext) -> None:
    """Test that delete_layer removes layer from tree."""
    # Add a layer via convenience verb
    result = asyncio.run(
        layer_module.dispatch_add_layer(
            {"type": "video", "name": "to_delete"},
            tmp_session
        )
    )
    assert result["applied"] is True

    # Delete it
    doc = layer_module._lumendoc(tmp_session)
    layer_id = doc["root"]["children"][0]["id"]

    del_result = asyncio.run(
        layer_module.dispatch_delete_layer({"layer_id": layer_id}, tmp_session)
    )
    assert del_result["applied"] is True

    # Verify it's gone
    doc = layer_module._lumendoc(tmp_session)
    assert len(doc["root"].get("children", [])) == 0


def test_move_layer_reparents(tmp_session: ToolContext) -> None:
    """Test move_layer with reparenting."""
    # Create composition and layer
    ops = [
        {"op": "add_layer", "type": "composition", "name": "parent"},
        {"op": "add_layer", "type": "video", "name": "child"},
    ]
    asyncio.run(layer_module.dispatch_patch({"ops": ops}, tmp_session))

    doc = layer_module._lumendoc(tmp_session)
    parent_id = doc["root"]["children"][0]["id"]
    child_id = doc["root"]["children"][1]["id"]

    # Move child into parent
    result = asyncio.run(
        layer_module.dispatch_move_layer(
            {"layer_id": child_id, "parent_id": parent_id},
            tmp_session
        )
    )
    assert result["applied"] is True

    # Verify parentage
    doc = layer_module._lumendoc(tmp_session)
    parent = find_layer(doc, parent_id)
    assert parent is not None
    assert len(parent.get("children", [])) == 1
    assert parent["children"][0]["id"] == child_id


def test_set_visibility_toggle(tmp_session: ToolContext) -> None:
    """Test toggling visibility on a layer."""
    # Add layer
    asyncio.run(
        layer_module.dispatch_add_layer(
            {"type": "audio", "name": "sound"},
            tmp_session
        )
    )
    doc = layer_module._lumendoc(tmp_session)
    layer_id = doc["root"]["children"][0]["id"]

    # Hide it
    result = asyncio.run(
        layer_module.dispatch_set_visibility(
            {"layer_id": layer_id, "visible": False},
            tmp_session
        )
    )
    assert result["applied"] is True

    doc = layer_module._lumendoc(tmp_session)
    layer = find_layer(doc, layer_id)
    assert layer is not None
    assert layer.get("visible") is False


def test_select_multiple_layers(tmp_session: ToolContext) -> None:
    """Test multi-layer selection."""
    # Add three layers
    for name in ["v1", "v2", "v3"]:
        asyncio.run(
            layer_module.dispatch_add_layer(
                {"type": "video", "name": name},
                tmp_session
            )
        )

    doc = layer_module._lumendoc(tmp_session)
    ids = [c["id"] for c in doc["root"]["children"]]

    # Select first two
    result = asyncio.run(
        layer_module.dispatch_select(
            {"layer_ids": ids[:2], "mode": "replace"},
            tmp_session
        )
    )
    assert result["applied"] is True

    doc = layer_module._lumendoc(tmp_session)
    selection = doc.get("selection", [])
    assert set(selection) == set(ids[:2])


def test_invalid_op_returns_structured_error(tmp_session: ToolContext) -> None:
    """Test that invalid ops return structured error_code."""
    ops = [{"op": "invalid_op_type", "some_arg": "value"}]
    result = asyncio.run(layer_module.dispatch_patch({"ops": ops}, tmp_session))

    assert result["applied"] is False
    assert "error_code" in result
    assert "error_message" in result
    # Should be a specific code from lumenframe
    assert result["error_code"] == "E_OP_UNKNOWN"


def test_prompt_injection_placeholders_replaced() -> None:
    """Test that system prompt placeholders are properly replaced."""
    from gemia.agent_loop_v3 import AgentLoopV3
    from pathlib import Path
    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        loop = AgentLoopV3(
            session_id="test_injection",
            output_dir=Path(tmpdir),
            emit_event=lambda _: None,
        )

        messages = loop.render_messages()
        assert len(messages) > 0

        system_msg = messages[0]
        assert system_msg["role"] == "system"
        content = system_msg["content"]

        # Both placeholders should be replaced (not literally present)
        assert "{{lumenframe_ops}}" not in content
        assert "{{lumenframe}}" not in content
        assert "{{search_engine}}" not in content
        assert "Runtime web search (internal context; do not announce unless asked)" in content
        assert "built_in_" in content

        # Should contain lumenframe sections
        assert "Layer Document" in content or "lumenframe" in content.lower()


def test_clear_lumenframe_session_removes_cache(tmp_session: ToolContext) -> None:
    """Test that clear_lumenframe_session cleans up the doc cache."""
    # Add a doc to the cache
    asyncio.run(
        layer_module.dispatch_add_layer(
            {"type": "video", "name": "test"},
            tmp_session
        )
    )

    # Verify it's in cache
    assert tmp_session.session_id in layer_module._DOC_CACHE

    # Clear it
    layer_module.clear_lumenframe_session(tmp_session.session_id)

    # Verify it's gone
    assert tmp_session.session_id not in layer_module._DOC_CACHE
