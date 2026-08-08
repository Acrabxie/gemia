from __future__ import annotations

import asyncio
import io
import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from gemia import lumenframe_canvas, v3_routes
from gemia.project_store import ProjectHandle
from gemia.tools import layer as layer_store
from gemia.tools._context import AssetRegistry, ToolContext
from lumenframe import apply_layer_patch, empty_doc


def _patch(*ops: dict) -> dict:
    return {"version": 1, "ops": list(ops)}


@pytest.fixture
def canvas_ctx(tmp_path: Path):
    session_id = f"canvas-{uuid.uuid4().hex}"
    handle = ProjectHandle.open(
        tmp_path / "projects", "canvas-project", session_id=session_id,
    )
    ctx = ToolContext(
        session_id=session_id,
        output_dir=tmp_path / "outputs",
        registry=AssetRegistry(),
        emit_progress=lambda _update: None,
        project=handle,
    )
    source = tmp_path / "source.png"
    Image.new("RGBA", (20, 10), color=(255, 0, 0, 255)).save(source)
    doc = empty_doc(width=100, height=80, fps=10)
    doc["assets"].append({"id": "asset-red", "path": str(source)})
    doc = apply_layer_patch(doc, _patch({
        "op": "add_layer",
        "id": "picture",
        "type": "image",
        "asset_id": "asset-red",
        "name": "Picture",
        "duration": 1.0,
    }))
    assert layer_store._save_lumendoc(ctx, doc) is True
    yield ctx
    layer_store.clear_lumenframe_session(session_id)


def test_metadata_uses_rendered_alpha_bounds_and_canvas_coordinates(canvas_ctx) -> None:
    payload = lumenframe_canvas.canvas_metadata(canvas_ctx, frame=0)

    assert payload["canvas"] == {
        "width": 100,
        "height": 80,
        "fps": 10.0,
        "frame": 0,
        "time": 0.0,
        "total_frames": 10,
    }
    assert payload["rotation_css_sign"] == -1
    assert payload["can_undo"] is False
    layer = payload["layers"][0]
    assert layer["id"] == "picture"
    assert layer["editable"] is True
    assert layer["disabled_reason"] is None
    assert layer["transform_editable"] is True
    assert layer["property_editable"] is True
    assert layer["property_disabled_reason"] is None
    assert layer["opacity"] == 1.0
    assert layer["blend_mode"] == "normal"
    assert layer["clip_to_below"] is False
    assert layer["effects"] == []
    assert layer["mask"] is None
    assert layer["time"] == {"start": 0.0, "duration": 1.0, "end": 1.0}
    assert layer["transform"] == {
        "x": 0.0, "y": 0.0, "scale": 1.0, "rotation": 0.0,
    }
    assert layer["bounds"] == {"x": 40, "y": 35, "width": 20, "height": 10}
    assert "multiply" in payload["capabilities"]["blend_modes"]
    assert payload["capabilities"]["effect_types"][0]["type"] == "gaussian_blur"
    assert payload["capabilities"]["add_layer_types"] == [
        "gradient", "shape", "adjustment",
    ]

    moved = lumenframe_canvas.apply_canvas_operation(canvas_ctx, {
        "op": "set_transform",
        "layer_id": "picture",
        "frame": 0,
        "base_revision": payload["revision"],
        "client_op_id": "bounds-move",
        "transform": {"x": 10, "y": -5},
    })
    refreshed = lumenframe_canvas.canvas_metadata(canvas_ctx, frame=0)
    assert refreshed["revision"] == moved["revision"]
    assert refreshed["layers"][0]["bounds"] == {
        "x": 50, "y": 30, "width": 20, "height": 10,
    }


def test_metadata_redacts_inline_pixel_mask_payloads(canvas_ctx) -> None:
    doc = layer_store.get_lumendoc_snapshot(canvas_ctx)
    doc["root"]["children"][0]["mask"] = {
        "kind": "pixel",
        "width": 2,
        "height": 1,
        "data": [[1.0, 0.0]],
        "invert": False,
        "feather": 0.0,
    }
    assert layer_store._save_lumendoc(canvas_ctx, doc) is True

    mask = lumenframe_canvas.canvas_metadata(canvas_ctx)["layers"][0]["mask"]
    assert mask["kind"] == "pixel"
    assert mask["width"] == 2
    assert mask["height"] == 1
    assert mask["has_inline_data"] is True
    assert "data" not in mask
    assert "alpha" not in mask


def test_clipped_layer_bounds_and_only_png_include_the_lower_alpha_source(canvas_ctx) -> None:
    doc = layer_store.get_lumendoc_snapshot(canvas_ctx)
    doc = apply_layer_patch(doc, _patch(
        {
            "op": "add_shape",
            "id": "matte",
            "kind": "rect",
            "fill": "#ffffff",
            "rect": [0.45, 0.4, 0.55, 0.6],
            "duration": 1.0,
            "index": 0,
        },
        {"op": "clip_to_below", "layer_id": "picture", "enabled": True},
    ))
    assert layer_store._save_lumendoc(canvas_ctx, doc) is True

    metadata = lumenframe_canvas.canvas_metadata(canvas_ctx, frame=0)
    picture = next(layer for layer in metadata["layers"] if layer["id"] == "picture")
    assert picture["bounds"] == {"x": 45, "y": 35, "width": 11, "height": 10}

    _, _, png = lumenframe_canvas.render_canvas_png(
        canvas_ctx,
        frame=0,
        mode="only",
        layer_id="picture",
        revision=metadata["revision"],
    )
    assert Image.open(io.BytesIO(png)).convert("RGBA").getbbox() == (45, 35, 56, 45)


def test_transform_is_atomic_revisioned_idempotent_and_undoable(canvas_ctx) -> None:
    initial = lumenframe_canvas.canvas_metadata(canvas_ctx, frame=0)
    request = {
        "op": "set_transform",
        "layer_id": "picture",
        "frame": 0,
        "base_revision": initial["revision"],
        "client_op_id": "drag-1",
        "transform": {"x": 12, "y": -5, "scale": 1.5, "rotation": 15},
    }
    changed = lumenframe_canvas.apply_canvas_operation(canvas_ctx, request)
    assert changed["duplicate"] is False
    assert changed["revision"] != initial["revision"]
    assert changed["can_undo"] is True
    persisted = layer_store.get_lumendoc_snapshot(canvas_ctx)
    assert persisted["root"]["children"][0]["transform"]["x"] == 12.0
    assert persisted["root"]["children"][0]["transform"]["scale_x"] == 1.5
    assert persisted["root"]["children"][0]["transform"]["scale_y"] == 1.5

    # Retry with the same client id is safe even though its base revision is stale.
    duplicate = lumenframe_canvas.apply_canvas_operation(canvas_ctx, request)
    assert duplicate["duplicate"] is True
    assert duplicate["revision"] == changed["revision"]
    assert layer_store.lumenframe_history_status(canvas_ctx)["history_length"] == 2

    undone = lumenframe_canvas.apply_canvas_operation(canvas_ctx, {
        "op": "undo",
        "steps": 1,
        "base_revision": changed["revision"],
        "client_op_id": "undo-1",
    })
    assert undone["can_redo"] is True
    assert layer_store.get_lumendoc_snapshot(canvas_ctx)["root"]["children"][0]["transform"]["x"] == 0.0
    duplicate_undo = lumenframe_canvas.apply_canvas_operation(canvas_ctx, {
        "op": "undo",
        "steps": 1,
        "base_revision": changed["revision"],
        "client_op_id": "undo-1",
    })
    assert duplicate_undo["duplicate"] is True
    assert duplicate_undo["revision"] == undone["revision"]

    redone = lumenframe_canvas.apply_canvas_operation(canvas_ctx, {
        "op": "redo",
        "base_revision": undone["revision"],
        "client_op_id": "redo-1",
    })
    assert redone["revision"] == changed["revision"]
    assert layer_store.get_lumendoc_snapshot(canvas_ctx)["root"]["children"][0]["transform"]["x"] == 12.0


def test_history_journal_survives_a_new_session_context(canvas_ctx, tmp_path) -> None:
    initial = lumenframe_canvas.canvas_metadata(canvas_ctx)
    changed = lumenframe_canvas.apply_canvas_operation(canvas_ctx, {
        "op": "set_transform",
        "layer_id": "picture",
        "frame": 0,
        "base_revision": initial["revision"],
        "client_op_id": "cross-session-edit",
        "transform": {"x": 21},
    })
    second_session = f"canvas-reopen-{uuid.uuid4().hex}"
    second_handle = ProjectHandle.open(
        canvas_ctx.project.store.root,
        canvas_ctx.project.project_id,
        session_id=second_session,
    )
    second_ctx = ToolContext(
        session_id=second_session,
        output_dir=tmp_path / "second-output",
        registry=AssetRegistry(),
        emit_progress=lambda _update: None,
        project=second_handle,
    )
    try:
        reopened = lumenframe_canvas.canvas_metadata(second_ctx)
        assert reopened["revision"] == changed["revision"]
        assert reopened["can_undo"] is True
        undone = lumenframe_canvas.apply_canvas_operation(second_ctx, {
            "op": "undo",
            "steps": 1,
            "base_revision": reopened["revision"],
            "client_op_id": "cross-session-undo",
        })
        assert undone["revision"] == initial["revision"]
        assert layer_store.get_lumendoc_snapshot(second_ctx)["root"]["children"][0]["transform"]["x"] == 0.0
    finally:
        layer_store.clear_lumenframe_session(second_session)


def test_stale_revision_conflicts_without_mutation(canvas_ctx) -> None:
    initial = lumenframe_canvas.canvas_metadata(canvas_ctx)
    first = lumenframe_canvas.apply_canvas_operation(canvas_ctx, {
        "op": "set_transform",
        "layer_id": "picture",
        "frame": 0,
        "base_revision": initial["revision"],
        "client_op_id": "first",
        "transform": {"x": 8},
    })
    with pytest.raises(layer_store.LumenframeRevisionConflict) as raised:
        lumenframe_canvas.apply_canvas_operation(canvas_ctx, {
            "op": "set_transform",
            "layer_id": "picture",
            "frame": 0,
            "base_revision": initial["revision"],
            "client_op_id": "stale",
            "transform": {"x": 99},
        })
    assert raised.value.current_revision == first["revision"]
    assert layer_store.get_lumendoc_snapshot(canvas_ctx)["root"]["children"][0]["transform"]["x"] == 8.0


@pytest.mark.parametrize("binding", ["locked", "animated"])
def test_locked_and_animated_layers_are_read_only(canvas_ctx, binding: str) -> None:
    doc = layer_store.get_lumendoc_snapshot(canvas_ctx)
    if binding == "locked":
        doc["root"]["children"][0]["locked"] = True
    else:
        doc = apply_layer_patch(doc, _patch({
            "op": "set_keyframe",
            "layer_id": "picture",
            "property": "transform.x",
            "t": 0.0,
            "value": 0.0,
        }))
    assert layer_store._save_lumendoc(canvas_ctx, doc) is True
    metadata = lumenframe_canvas.canvas_metadata(canvas_ctx)
    assert metadata["layers"][0]["editable"] is False
    expected = "locked" if binding == "locked" else "animated_transform"
    assert metadata["layers"][0]["disabled_reason"] == expected
    with pytest.raises(lumenframe_canvas.CanvasRequestError) as raised:
        lumenframe_canvas.apply_canvas_operation(canvas_ctx, {
            "op": "set_transform",
            "layer_id": "picture",
            "frame": 0,
            "base_revision": metadata["revision"],
            "client_op_id": f"blocked-{binding}",
            "transform": {"x": 5},
        })
    assert raised.value.code == "E_LAYER_LOCKED"


def test_appearance_effect_stack_and_clipping_are_revisioned_and_idempotent(canvas_ctx) -> None:
    initial = lumenframe_canvas.canvas_metadata(canvas_ctx)
    opacity_request = {
        "op": "set_opacity",
        "layer_id": "picture",
        "opacity": 0.65,
        "base_revision": initial["revision"],
        "client_op_id": "appearance-opacity",
    }
    opacity = lumenframe_canvas.apply_canvas_operation(canvas_ctx, opacity_request)
    duplicate = lumenframe_canvas.apply_canvas_operation(canvas_ctx, opacity_request)
    assert duplicate["duplicate"] is True
    assert duplicate["revision"] == opacity["revision"]

    blend = lumenframe_canvas.apply_canvas_operation(canvas_ctx, {
        "op": "set_blend_mode",
        "layer_id": "picture",
        "blend_mode": "screen",
        "base_revision": opacity["revision"],
        "client_op_id": "appearance-blend",
    })
    first = lumenframe_canvas.apply_canvas_operation(canvas_ctx, {
        "op": "add_effect",
        "layer_id": "picture",
        "effect_type": "gaussian_blur",
        "params": {"radius": 4},
        "base_revision": blend["revision"],
        "client_op_id": "blur-first",
    })
    second = lumenframe_canvas.apply_canvas_operation(canvas_ctx, {
        "op": "add_effect",
        "layer_id": "picture",
        "effect_type": "gaussian_blur",
        "params": {"radius": 9},
        "base_revision": first["revision"],
        "client_op_id": "blur-second",
    })
    disabled = lumenframe_canvas.apply_canvas_operation(canvas_ctx, {
        "op": "set_effect_enabled",
        "layer_id": "picture",
        "effect_id": first["effect_id"],
        "enabled": False,
        "base_revision": second["revision"],
        "client_op_id": "blur-disable",
    })
    tuned = lumenframe_canvas.apply_canvas_operation(canvas_ctx, {
        "op": "set_effect_params",
        "layer_id": "picture",
        "effect_id": first["effect_id"],
        "params": {"radius": 12.5},
        "base_revision": disabled["revision"],
        "client_op_id": "blur-tune",
    })
    reordered = lumenframe_canvas.apply_canvas_operation(canvas_ctx, {
        "op": "reorder_effect",
        "layer_id": "picture",
        "effect_id": second["effect_id"],
        "index": 0,
        "base_revision": tuned["revision"],
        "client_op_id": "blur-reorder",
    })
    clipped = lumenframe_canvas.apply_canvas_operation(canvas_ctx, {
        "op": "clip_to_below",
        "layer_id": "picture",
        "enabled": True,
        "base_revision": reordered["revision"],
        "client_op_id": "clip-enable",
    })
    removed = lumenframe_canvas.apply_canvas_operation(canvas_ctx, {
        "op": "remove_effect",
        "layer_id": "picture",
        "effect_id": first["effect_id"],
        "base_revision": clipped["revision"],
        "client_op_id": "blur-remove",
    })

    doc = layer_store.get_lumendoc_snapshot(canvas_ctx)
    layer = doc["root"]["children"][0]
    assert layer["opacity"] == pytest.approx(0.65)
    assert layer["blend_mode"] == "screen"
    assert layer["clip_to_below"] is True
    assert [effect["id"] for effect in layer["effects"]] == [second["effect_id"]]
    metadata_layer = lumenframe_canvas.canvas_metadata(canvas_ctx)["layers"][0]
    assert metadata_layer["clip_to_below"] is True
    assert metadata_layer["effects"] == [{
        "id": second["effect_id"],
        "type": "gaussian_blur",
        "enabled": True,
        "params": {"radius": 9.0},
    }]
    assert removed["can_undo"] is True


def test_add_layers_gradient_mask_and_adjustment_range_share_one_safe_api(canvas_ctx) -> None:
    initial = lumenframe_canvas.canvas_metadata(canvas_ctx)
    gradient_request = {
        "op": "add_gradient",
        "mode": "linear",
        "stops": [[1, "#ffffff"], [0, "#112233ff"]],
        "angle": 25,
        "base_revision": initial["revision"],
        "client_op_id": "add-gradient",
    }
    gradient = lumenframe_canvas.apply_canvas_operation(canvas_ctx, gradient_request)
    duplicate_gradient = lumenframe_canvas.apply_canvas_operation(canvas_ctx, gradient_request)
    assert duplicate_gradient["duplicate"] is True
    assert duplicate_gradient["layer_id"] == gradient["layer_id"]
    assert sum(
        layer["id"] == gradient["layer_id"]
        for layer in layer_store.get_lumendoc_snapshot(canvas_ctx)["root"]["children"]
    ) == 1

    shape = lumenframe_canvas.apply_canvas_operation(canvas_ctx, {
        "op": "add_shape",
        "kind": "ellipse",
        "fill": "#00cc88",
        "rect": [0.2, 0.1, 0.8, 0.9],
        "base_revision": gradient["revision"],
        "client_op_id": "add-shape",
    })
    adjustment = lumenframe_canvas.apply_canvas_operation(canvas_ctx, {
        "op": "add_adjustment_layer",
        "base_revision": shape["revision"],
        "client_op_id": "add-adjustment",
    })
    radial = lumenframe_canvas.apply_canvas_operation(canvas_ctx, {
        "op": "set_gradient",
        "layer_id": gradient["layer_id"],
        "mode": "radial",
        "stops": [[0, "#ff0000"], [0.4, "#00ff00"], [1, "#0000ff"]],
        "center": [0.3, 0.6],
        "radius": 0.75,
        "base_revision": adjustment["revision"],
        "client_op_id": "gradient-radial",
    })
    masked = lumenframe_canvas.apply_canvas_operation(canvas_ctx, {
        "op": "set_mask",
        "layer_id": shape["layer_id"],
        "mask": {
            "kind": "shape",
            "shape": {"type": "ellipse", "rect": [0.1, 0.2, 0.9, 0.8]},
            "invert": True,
            "feather": 0.04,
        },
        "base_revision": radial["revision"],
        "client_op_id": "shape-mask",
    })
    ranged = lumenframe_canvas.apply_canvas_operation(canvas_ctx, {
        "op": "set_time",
        "layer_id": adjustment["layer_id"],
        "start": 0.2,
        "duration": 0.5,
        "base_revision": masked["revision"],
        "client_op_id": "adjustment-range",
    })

    doc = layer_store.get_lumendoc_snapshot(canvas_ctx)
    by_id = {layer["id"]: layer for layer in doc["root"]["children"]}
    assert by_id[gradient["layer_id"]]["duration"] == 1.0
    gradient_props = by_id[gradient["layer_id"]]["props"]
    assert gradient_props["mode"] == "radial"
    assert gradient_props["stops"] == [
        [0.0, "#ff0000"], [0.4, "#00ff00"], [1.0, "#0000ff"],
    ]
    assert gradient_props["center"] == [0.3, 0.6]
    assert gradient_props["radius"] == 0.75
    assert by_id[shape["layer_id"]]["duration"] == 1.0
    assert by_id[shape["layer_id"]]["mask"]["shape"] == {
        "type": "ellipse", "rect": [0.1, 0.2, 0.9, 0.8],
    }
    assert by_id[adjustment["layer_id"]]["start"] == 0.2
    assert by_id[adjustment["layer_id"]]["duration"] == 0.5

    metadata = lumenframe_canvas.canvas_metadata(canvas_ctx, frame=3)
    meta_by_id = {layer["id"]: layer for layer in metadata["layers"]}
    gradient_meta = meta_by_id[gradient["layer_id"]]["gradient"]
    assert gradient_meta["mode"] == "radial"
    assert gradient_meta["stops"] == [
        [0.0, "#ff0000"], [0.4, "#00ff00"], [1.0, "#0000ff"],
    ]
    assert gradient_meta["center"] == [0.3, 0.6]
    assert gradient_meta["radius"] == 0.75
    assert meta_by_id[shape["layer_id"]]["shape"]["kind"] == "ellipse"
    assert meta_by_id[shape["layer_id"]]["mask"].get("has_inline_data") is not True
    assert meta_by_id[adjustment["layer_id"]]["property_editable"] is True
    assert meta_by_id[adjustment["layer_id"]]["transform_editable"] is False
    assert meta_by_id[adjustment["layer_id"]]["disabled_reason"] == "unsupported_layer_type"

    cleared = lumenframe_canvas.apply_canvas_operation(canvas_ctx, {
        "op": "clear_mask",
        "layer_id": shape["layer_id"],
        "base_revision": ranged["revision"],
        "client_op_id": "shape-mask-clear",
    })
    assert cleared["revision"] != ranged["revision"]
    assert next(
        layer for layer in layer_store.get_lumendoc_snapshot(canvas_ctx)["root"]["children"]
        if layer["id"] == shape["layer_id"]
    )["mask"] is None


def test_appearance_edits_ignore_transform_animation_but_respect_lock(canvas_ctx) -> None:
    doc = layer_store.get_lumendoc_snapshot(canvas_ctx)
    doc = apply_layer_patch(doc, _patch({
        "op": "set_keyframe",
        "layer_id": "picture",
        "property": "transform.x",
        "t": 0.0,
        "value": 0.0,
    }))
    assert layer_store._save_lumendoc(canvas_ctx, doc) is True
    metadata = lumenframe_canvas.canvas_metadata(canvas_ctx)
    assert metadata["layers"][0]["transform_editable"] is False
    assert metadata["layers"][0]["property_editable"] is True
    changed = lumenframe_canvas.apply_canvas_operation(canvas_ctx, {
        "op": "set_opacity",
        "layer_id": "picture",
        "opacity": 0.4,
        "base_revision": metadata["revision"],
        "client_op_id": "animated-appearance",
    })
    assert changed["duplicate"] is False

    locked_doc = layer_store.get_lumendoc_snapshot(canvas_ctx)
    locked_doc["root"]["children"][0]["locked"] = True
    assert layer_store._save_lumendoc(canvas_ctx, locked_doc) is True
    locked = lumenframe_canvas.canvas_metadata(canvas_ctx)
    assert locked["layers"][0]["property_editable"] is False
    with pytest.raises(lumenframe_canvas.CanvasRequestError) as raised:
        lumenframe_canvas.apply_canvas_operation(canvas_ctx, {
            "op": "clip_to_below",
            "layer_id": "picture",
            "enabled": True,
            "base_revision": locked["revision"],
            "client_op_id": "locked-appearance",
        })
    assert raised.value.code == "E_LAYER_LOCKED"


@pytest.mark.parametrize("operation", [
    {"op": "set_opacity", "layer_id": "picture", "opacity": 1.01},
    {"op": "set_blend_mode", "layer_id": "picture", "blend_mode": "mystery"},
    {"op": "add_effect", "layer_id": "picture", "effect_type": "sharpen"},
    {"op": "add_shape", "kind": "polygon", "fill": "#ffffff"},
    {"op": "add_adjustment_layer", "duration": 0},
    {
        "op": "set_mask",
        "layer_id": "picture",
        "mask": {"kind": "pixel", "data": [[1.0]]},
    },
    {
        "op": "set_opacity",
        "layer_id": "picture",
        "opacity": 0.5,
        "ops": [{"op": "delete_layer", "layer_id": "picture"}],
    },
])
def test_canvas_rejects_out_of_range_or_raw_patch_operations(canvas_ctx, operation) -> None:
    before = lumenframe_canvas.canvas_metadata(canvas_ctx)
    request = {
        **operation,
        "base_revision": before["revision"],
        "client_op_id": f"invalid-{uuid.uuid4().hex}",
    }
    with pytest.raises(lumenframe_canvas.CanvasRequestError) as raised:
        lumenframe_canvas.apply_canvas_operation(canvas_ctx, request)
    assert raised.value.code == "E_BAD_ARG"
    assert lumenframe_canvas.canvas_metadata(canvas_ctx)["revision"] == before["revision"]


def test_png_modes_share_canonical_render_and_revision_guard(canvas_ctx) -> None:
    metadata = lumenframe_canvas.canvas_metadata(canvas_ctx)
    _, revision, composite_png = lumenframe_canvas.render_canvas_png(
        canvas_ctx, frame=0, mode="composite", revision=metadata["revision"],
    )
    assert revision == metadata["revision"]
    composite = Image.open(io.BytesIO(composite_png)).convert("RGBA")
    assert composite.size == (100, 80)
    assert composite.getpixel((50, 40))[0] == 255

    _, _, only_png = lumenframe_canvas.render_canvas_png(
        canvas_ctx,
        frame=0,
        mode="only",
        layer_id="picture",
        revision=metadata["revision"],
    )
    assert Image.open(io.BytesIO(only_png)).convert("RGBA").getbbox() == (40, 35, 60, 45)

    _, _, excluded_png = lumenframe_canvas.render_canvas_png(
        canvas_ctx,
        frame=0,
        mode="exclude",
        layer_id="picture",
        revision=metadata["revision"],
    )
    assert Image.open(io.BytesIO(excluded_png)).convert("RGBA").getbbox() is None

    with pytest.raises(layer_store.LumenframeRevisionConflict):
        lumenframe_canvas.render_canvas_png(
            canvas_ctx, frame=0, revision="sha256:stale",
        )


def test_document_or_history_write_failure_never_returns_success(canvas_ctx, monkeypatch) -> None:
    before = lumenframe_canvas.canvas_metadata(canvas_ctx)
    original_write = layer_store._write_lumenframe_atomic

    def fail_history(path, payload):
        if Path(path).name == "lumenframe-history.json":
            return False
        return original_write(path, payload)

    monkeypatch.setattr(layer_store, "_write_lumenframe_atomic", fail_history)
    with pytest.raises(OSError):
        lumenframe_canvas.apply_canvas_operation(canvas_ctx, {
            "op": "set_transform",
            "layer_id": "picture",
            "frame": 0,
            "base_revision": before["revision"],
            "client_op_id": "history-fail",
            "transform": {"x": 42},
        })
    assert layer_store.get_lumendoc_snapshot(canvas_ctx)["root"]["children"][0]["transform"]["x"] == 0.0

    monkeypatch.setattr(layer_store, "_write_lumenframe_atomic", lambda *_args, **_kwargs: False)
    result = asyncio.run(layer_store.dispatch_set_transform({
        "layer_id": "picture", "x": 33,
    }, canvas_ctx))
    assert result["applied"] is False
    assert result["error_code"] == "E_PERSIST"
    assert layer_store.get_lumendoc_snapshot(canvas_ctx)["root"]["children"][0]["transform"]["x"] == 0.0


class _Handler:
    def __init__(self, payload: dict | None = None) -> None:
        raw = json.dumps(payload).encode("utf-8") if payload is not None else b""
        self.headers = {"Content-Length": str(len(raw))}
        self.rfile = io.BytesIO(raw)
        self.wfile = io.BytesIO()
        self.status: int | None = None
        self.response_headers: dict[str, str] = {}
        self.path = "/"

    def send_response(self, status: int) -> None:
        self.status = status

    def send_header(self, key: str, value: str) -> None:
        self.response_headers[key] = value

    def end_headers(self) -> None:
        pass

    @property
    def json(self) -> dict:
        return json.loads(self.wfile.getvalue().decode("utf-8"))


def test_http_canvas_contract_serialization_and_conflict(canvas_ctx, monkeypatch) -> None:
    edit_calls = 0

    def run_project_edit(fn, **_kwargs):
        nonlocal edit_calls
        edit_calls += 1
        return fn()

    runner = SimpleNamespace(
        session_id=canvas_ctx.session_id,
        agent=SimpleNamespace(_tool_ctx=canvas_ctx),
        run_project_edit=run_project_edit,
    )
    manager = SimpleNamespace(
        get=lambda session_id: runner if session_id == canvas_ctx.session_id else None,
    )
    monkeypatch.setattr(v3_routes, "get_manager", lambda: manager)

    metadata_handler = _Handler()
    metadata_handler.path = f"/sessions/{canvas_ctx.session_id}/lumenframe/canvas?frame=0"
    assert v3_routes.try_handle(metadata_handler, method="GET") is True
    assert metadata_handler.status == 200
    assert metadata_handler.response_headers["Cache-Control"] == "no-store"
    initial_revision = metadata_handler.json["revision"]

    frame_handler = _Handler()
    frame_handler.path = (
        f"/sessions/{canvas_ctx.session_id}/lumenframe/frame.png"
        f"?frame=0&revision={initial_revision}"
    )
    assert v3_routes.try_handle(frame_handler, method="GET") is True
    assert frame_handler.status == 200
    assert frame_handler.response_headers["Content-Type"] == "image/png"
    assert frame_handler.wfile.getvalue().startswith(b"\x89PNG")

    head_handler = _Handler()
    head_handler.path = frame_handler.path
    assert v3_routes.try_handle(head_handler, method="HEAD") is True
    assert head_handler.status == 200
    assert int(head_handler.response_headers["Content-Length"]) > 0
    assert head_handler.wfile.getvalue() == b""

    edit_handler = _Handler({
        "op": "set_transform",
        "layer_id": "picture",
        "frame": 0,
        "base_revision": initial_revision,
        "client_op_id": "route-drag",
        "transform": {"x": 7, "y": 3, "scale": 1.2, "rotation": 5},
    })
    edit_handler.path = f"/sessions/{canvas_ctx.session_id}/lumenframe/op"
    assert v3_routes.try_handle(edit_handler, method="POST") is True
    assert edit_handler.status == 200
    assert edit_handler.json["revision"] != initial_revision
    assert edit_calls == 1

    duplicate_handler = _Handler({
        "op": "set_transform",
        "layer_id": "picture",
        "frame": 0,
        "base_revision": initial_revision,
        "client_op_id": "route-drag",
        "transform": {"x": 7, "y": 3, "scale": 1.2, "rotation": 5},
    })
    duplicate_handler.path = edit_handler.path
    assert v3_routes.try_handle(duplicate_handler, method="POST") is True
    assert duplicate_handler.status == 200
    assert duplicate_handler.json["duplicate"] is True
    assert edit_calls == 2

    stale_handler = _Handler({
        "op": "set_transform",
        "layer_id": "picture",
        "frame": 0,
        "base_revision": initial_revision,
        "client_op_id": "route-stale",
        "transform": {"x": 99},
    })
    stale_handler.path = edit_handler.path
    assert v3_routes.try_handle(stale_handler, method="POST") is True
    assert stale_handler.status == 409
    assert stale_handler.json["code"] == "E_REVISION_CONFLICT"
    assert stale_handler.json["current_revision"] == edit_handler.json["revision"]

    undo_handler = _Handler({
        "op": "undo",
        "steps": 50,
        "base_revision": edit_handler.json["revision"],
        "client_op_id": "route-undo-many",
    })
    undo_handler.path = edit_handler.path
    assert v3_routes.try_handle(undo_handler, method="POST") is True
    assert undo_handler.status == 200
    assert undo_handler.json["can_redo"] is True
    assert layer_store.get_lumendoc_snapshot(canvas_ctx)["root"]["children"][0]["transform"]["x"] == 0.0


@pytest.mark.parametrize("missing", ["base_revision", "client_op_id"])
def test_http_mutation_requires_revision_and_idempotency_key(canvas_ctx, monkeypatch, missing) -> None:
    runner = SimpleNamespace(
        session_id=canvas_ctx.session_id,
        agent=SimpleNamespace(_tool_ctx=canvas_ctx),
        run_project_edit=lambda fn, **_kwargs: fn(),
    )
    monkeypatch.setattr(
        v3_routes,
        "get_manager",
        lambda: SimpleNamespace(get=lambda _session_id: runner),
    )
    body = {
        "op": "set_transform",
        "layer_id": "picture",
        "frame": 0,
        "base_revision": lumenframe_canvas.canvas_metadata(canvas_ctx)["revision"],
        "client_op_id": "required-fields",
        "transform": {"x": 2},
    }
    body.pop(missing)
    handler = _Handler(body)
    handler.path = f"/sessions/{canvas_ctx.session_id}/lumenframe/op"
    assert v3_routes.try_handle(handler, method="POST") is True
    assert handler.status == 400
    assert handler.json["code"] == "E_BAD_ARG"
