"""edit_image remove_background — real ML/fallback subject matting.

These tests exercise the plumbing (tool wiring, output kinds, params) so they
pass whether or not the U2Net onnx model is present: with the model the ML path
runs, without it the GrabCut fallback runs. A separate test forces the fallback
so the offline path is checked deterministically.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from gemia.picture import matting
from gemia.tools import edit_image
from gemia.tools._context import AssetRegistry, ToolContext


def _ctx(tmp_path: Path) -> ToolContext:
    return ToolContext(
        session_id="test-matting",
        output_dir=tmp_path,
        registry=AssetRegistry(),
        emit_progress=lambda _u: None,
    )


def _make_subject_image(tmp_path: Path, name: str = "subj") -> Path:
    """A bright centered blob on a dark background — segmentable by GrabCut."""
    img = np.zeros((240, 180, 3), np.uint8)
    img[:] = (20, 20, 20)
    img[40:210, 50:130] = (210, 190, 175)   # a light "subject" rectangle
    path = tmp_path / f"{name}.png"
    Image.fromarray(img).save(path)
    return path


def _register(ctx: ToolContext, path: Path) -> str:
    return ctx.registry.add_external(path).asset_id


def test_schema_lists_remove_background() -> None:
    from gemia.tools._schema import TOOL_SCHEMAS

    ei = next(s for s in TOOL_SCHEMAS if s["function"]["name"] == "edit_image")
    enum = ei["function"]["parameters"]["properties"]["operation"]["enum"]
    assert "remove_background" in enum


def test_model_candidates_include_packaged_data_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    packaged_data = tmp_path / "packaged-data"
    monkeypatch.delenv("LUMERI_MATTING_MODEL", raising=False)
    monkeypatch.setattr(matting, "data_dir", lambda: packaged_data)

    assert packaged_data / "u2net_human_seg.onnx" in matting._model_candidates()


def test_remove_background_transparent_cutout(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    src = _register(ctx, _make_subject_image(tmp_path))
    res = asyncio.run(
        edit_image.dispatch({"asset_id": src, "operation": "remove_background"}, ctx)
    )
    out = ctx.registry.get(res["asset_id"])
    assert out.path.suffix == ".png"
    im = Image.open(out.path)
    assert im.mode == "RGBA"                      # straight-alpha cutout
    assert im.size == (180, 240)
    meta = res["metadata"]
    assert meta["operation"] == "remove_background"
    assert "backend" in meta and "ml" in meta
    assert 0.0 <= meta["coverage"] <= 1.0
    assert meta["transparent"] is True


def test_remove_background_matte_only(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    src = _register(ctx, _make_subject_image(tmp_path))
    res = asyncio.run(
        edit_image.dispatch(
            {"asset_id": src, "operation": "remove_background", "params": {"matte_only": True}},
            ctx,
        )
    )
    im = Image.open(ctx.registry.get(res["asset_id"]).path)
    assert im.mode == "L"                         # grayscale alpha


def test_remove_background_composite_color_is_opaque(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    src = _register(ctx, _make_subject_image(tmp_path))
    res = asyncio.run(
        edit_image.dispatch(
            {"asset_id": src, "operation": "remove_background", "params": {"background": "white"}},
            ctx,
        )
    )
    im = Image.open(ctx.registry.get(res["asset_id"]).path)
    assert im.mode == "RGB"                       # composited → no alpha
    assert res["metadata"]["transparent"] is False


def test_remove_background_over_asset_background(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    src = _register(ctx, _make_subject_image(tmp_path, "subj"))
    bg = _register(ctx, _make_subject_image(tmp_path, "bg"))
    res = asyncio.run(
        edit_image.dispatch(
            {"asset_id": src, "operation": "remove_background", "params": {"background": bg}},
            ctx,
        )
    )
    im = Image.open(ctx.registry.get(res["asset_id"]).path)
    assert im.mode == "RGB"
    assert im.size == (180, 240)


def test_bad_feather_is_typed_error(tmp_path: Path) -> None:
    from gemia.errors import ToolError

    ctx = _ctx(tmp_path)
    src = _register(ctx, _make_subject_image(tmp_path))
    with pytest.raises(ToolError):
        asyncio.run(
            edit_image.dispatch(
                {"asset_id": src, "operation": "remove_background", "params": {"feather": "soft"}},
                ctx,
            )
        )


def test_fallback_grabcut_segments_subject(tmp_path: Path, monkeypatch) -> None:
    """Force the no-model path; GrabCut must still find the bright subject."""
    monkeypatch.setattr(matting, "_session", lambda: None)
    assert matting.describe_backend()["backend"] == "grabcut_fallback"
    img = np.zeros((240, 180, 3), np.uint8)
    img[:] = (15, 15, 15)
    img[40:210, 50:130] = (215, 195, 180)
    alpha = matting.compute_alpha(img[:, :, ::-1])   # expects BGR
    cov = float((alpha > 0.5).mean())
    assert 0.05 < cov < 0.95                          # found *a* subject, not all/none
