"""Verbs must fail honestly with typed, actionable errors — never silently
substitute something close and report success.

These guard the "no silent fallback" contract (RULES "黑白静默套暖色"): a
self-correcting agent can only fix a mistake it is actually told about.
"""
from __future__ import annotations

import pytest

import gemia.tools.color_grade as color_grade
import gemia.tools.edit_image as edit_image
from gemia.errors import ToolError


def test_color_grade_named_look_resolves() -> None:
    filter_str, label = color_grade._resolve_look("teal orange")
    assert label == "teal_orange"
    assert filter_str  # a real ffmpeg filter, not empty


def test_color_grade_unknown_look_raises_typed_error_not_silent_warm() -> None:
    with pytest.raises(ToolError) as exc_info:
        color_grade._resolve_look("black and white")
    err = exc_info.value
    payload = err.to_payload()
    assert payload["error_code"] == "E_UNSUPPORTED"
    assert payload["recovery"] == "fix_args"
    # The real options are handed to the model so it can pick a valid one.
    assert payload["valid_options"] == [
        "warm", "cool", "vintage", "cinematic", "teal_orange", "neutral",
    ]
    assert "grayscale" in payload["hint"].lower() or "black" in payload["hint"].lower()


def test_remove_background_bad_feather_raises_typed_error(tmp_path) -> None:
    # remove_background is now real ML matting; it must still reject bad params
    # with a typed error rather than guessing (the "no silent fallback" contract).
    import asyncio

    from gemia.tools._context import AssetRegistry, ToolContext

    reg = AssetRegistry()
    src = tmp_path / "src.png"
    src.write_bytes(b"image")  # feather is validated before the pixels are read
    asset = reg.add_external(src).asset_id
    ctx = ToolContext(
        session_id="t", output_dir=tmp_path, registry=reg, emit_progress=lambda _u: None
    )
    with pytest.raises(ToolError) as exc_info:
        asyncio.run(
            edit_image.dispatch(
                {"asset_id": asset, "operation": "remove_background",
                 "params": {"feather": "soft"}},
                ctx,
            )
        )
    payload = exc_info.value.to_payload()
    assert payload["error_code"] == "E_BAD_ARG"
    assert payload["recovery"] == "fix_args"
