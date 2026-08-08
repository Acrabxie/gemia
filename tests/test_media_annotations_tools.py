from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from gemia import accounts
from gemia import media_annotations as MA
from gemia.media_library import import_media
from gemia.tools import DISPATCHER, TOOL_NAMES, TOOL_SCHEMAS
from gemia.tools._context import AssetRegistry, ToolContext


def _patch_account_roots(monkeypatch, tmp_path: Path) -> None:
    root = tmp_path / "accounts"
    monkeypatch.setattr(accounts, "ACCOUNTS_ROOT", root)
    monkeypatch.setattr(accounts, "ACTIVE_ACCOUNT_PATH", root / "active.json")
    monkeypatch.setattr(accounts, "CONFIG_PATH", tmp_path / "config.json")


def _make_image(path: Path) -> Path:
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=orange:s=80x60:d=0.1",
            "-frames:v",
            "1",
            str(path),
        ],
        capture_output=True,
        check=True,
    )
    return path


def _ctx(tmp_path: Path, account_id: str) -> ToolContext:
    return ToolContext(
        session_id="media-ann-test",
        output_dir=tmp_path,
        registry=AssetRegistry(),
        emit_progress=lambda _: None,
        extra={"account_id": account_id},
    )


def test_media_annotation_tools_registered() -> None:
    for name in ("annotate_media", "get_media_annotations", "write_media_annotation", "prepare_roughcut"):
        assert name in TOOL_NAMES
        assert name in DISPATCHER
    by_name = {tool["function"]["name"]: tool for tool in TOOL_SCHEMAS}
    assert by_name["write_media_annotation"]["function"]["parameters"]["required"] == ["asset_id", "label"]


def test_media_annotation_tools_write_and_read(monkeypatch, tmp_path: Path) -> None:
    _patch_account_roots(monkeypatch, tmp_path)
    account_id = "google_tool_account"
    asset = import_media(account_id, _make_image(tmp_path / "still.png"))
    ctx = _ctx(tmp_path, account_id)

    saved = asyncio.run(
        DISPATCHER["write_media_annotation"](
            {
                "asset_id": asset["asset_id"],
                "scope": "asset",
                "label": "usable still",
                "note": "good thumbnail candidate",
                "tags": ["thumbnail", "keeper"],
                "category": "selection",
                "confidence": 0.8,
                "language": "en",
                "metadata": {"model": "baseline", "prompt_version": 1},
            },
            ctx,
        )
    )
    assert saved["annotation"]["source"] == "gemini"

    read = asyncio.run(DISPATCHER["get_media_annotations"]({"asset_id": asset["asset_id"]}, ctx))
    assert read["annotation_count"] == 1
    assert read["annotations"][0]["label"] == "usable still"

    batch = asyncio.run(
        DISPATCHER["annotate_media"](
            {"asset_ids": [asset["asset_id"]], "mode": "quick", "language": "zh"},
            ctx,
        )
    )
    assert batch["asset_count"] == 1
    assert batch["results"][0]["annotation_count"] >= 1


def test_agent_annotation_update_is_partial(monkeypatch, tmp_path: Path) -> None:
    _patch_account_roots(monkeypatch, tmp_path)
    account_id = "google_partial_update"
    asset = import_media(account_id, _make_image(tmp_path / "partial.png"))
    ctx = _ctx(tmp_path, account_id)
    original = MA.create_annotation(
        account_id,
        asset["asset_id"],
        {
            "scope": "asset",
            "label": "first caption",
            "note": "keep this note",
            "tags": ["keeper"],
            "category": "selection",
            "confidence": 0.81,
            "source": "gemini",
            "language": "en",
            "metadata": {"model": "baseline", "prompt_version": 4},
        },
    )

    updated = asyncio.run(
        DISPATCHER["write_media_annotation"](
            {
                "asset_id": asset["asset_id"],
                "annotation_id": original["annotation_id"],
                "label": "revised caption",
            },
            ctx,
        )
    )["annotation"]

    assert updated["label"] == "revised caption"
    assert updated["note"] == "keep this note"
    assert updated["tags"] == ["keeper"]
    assert updated["category"] == "selection"
    assert updated["confidence"] == 0.81
    assert updated["source"] == "gemini"
    assert updated["language"] == "en"
    assert updated["metadata"] == {"model": "baseline", "prompt_version": 4}


def test_agent_cannot_overwrite_user_annotation(monkeypatch, tmp_path: Path) -> None:
    _patch_account_roots(monkeypatch, tmp_path)
    account_id = "google_user_annotation_guard"
    asset = import_media(account_id, _make_image(tmp_path / "user.png"))
    ctx = _ctx(tmp_path, account_id)
    user_annotation = MA.create_annotation(
        account_id,
        asset["asset_id"],
        {
            "scope": "asset",
            "label": "my keeper note",
            "note": "human-authored",
            "source": "user",
        },
    )

    with pytest.raises(
        MA.MediaAnnotationError,
        match="cannot update a user annotation; create a new annotation instead",
    ):
        asyncio.run(
            DISPATCHER["write_media_annotation"](
                {
                    "asset_id": asset["asset_id"],
                    "annotation_id": user_annotation["annotation_id"],
                    "label": "agent rewrite",
                },
                ctx,
            )
        )

    unchanged = MA.get_annotation(
        account_id, asset["asset_id"], user_annotation["annotation_id"]
    )
    assert unchanged["label"] == "my keeper note"
    assert unchanged["note"] == "human-authored"
    assert unchanged["source"] == "user"
