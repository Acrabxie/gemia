"""Acceptance coverage for non-timeline evidence-to-outline fitting."""
from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

from gemia import accounts
from gemia import media_annotations as MA
from gemia.media_evidence import evaluate_evidence_candidates
from gemia.media_library import import_media
from gemia.project_model import iter_shots
from gemia.project_store import ProjectHandle
from gemia.tools import DISPATCHER, TOOL_SCHEMAS
from gemia.tools._context import AssetRegistry, ToolContext


def _patch_account_roots(monkeypatch, tmp_path: Path) -> None:
    root = tmp_path / "accounts"
    monkeypatch.setattr(accounts, "ACCOUNTS_ROOT", root)
    monkeypatch.setattr(accounts, "ACTIVE_ACCOUNT_PATH", root / "active.json")
    monkeypatch.setattr(accounts, "CONFIG_PATH", tmp_path / "config.json")


def _video(path: Path, color: str = "blue") -> Path:
    subprocess.run(
        [
            "ffmpeg", "-y", "-f", "lavfi", "-i",
            f"color=c={color}:s=128x72:r=15:d=8",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path),
        ],
        check=True,
        capture_output=True,
    )
    return path


def _ctx(tmp_path: Path, account_id: str) -> ToolContext:
    return ToolContext(
        session_id="outline-fit",
        output_dir=tmp_path,
        registry=AssetRegistry(),
        emit_progress=lambda _update: None,
        project=ProjectHandle.open(tmp_path / "project", "outline-fit", session_id="outline-fit"),
        extra={"account_id": account_id},
    )


def _call(name: str, args: dict, ctx: ToolContext) -> dict:
    return asyncio.run(DISPATCHER[name](args, ctx))


def _seed_two_assets(account_id: str, tmp_path: Path) -> tuple[dict, dict]:
    blue = import_media(account_id, _video(tmp_path / "blue.mp4", "blue"))
    red = import_media(account_id, _video(tmp_path / "red.mp4", "red"))
    MA.upsert_annotations(
        account_id,
        blue["asset_id"],
        [
            {
                "scope": "time_range",
                "start_sec": 1.25,
                "end_sec": 5.75,
                "label": "city sunrise aerial opening",
                "tags": ["city", "sunrise", "aerial"],
                "source": "gemini_vision",
                "confidence": 0.7,
            },
            {
                "scope": "time_range",
                "start_sec": 1.25,
                "end_sec": 5.75,
                "label": "creator preferred opening",
                "source": "user",
                "metadata": {
                    "evidence": {
                        "decision": "prefer",
                        "claim_key": "editorial.usability",
                    }
                },
            },
        ],
    )
    MA.upsert_annotations(
        account_id,
        red["asset_id"],
        [
            {
                "scope": "time_range",
                "start_sec": 0.5,
                "end_sec": 4.5,
                "label": "city sunrise aerial alternate",
                "tags": ["city", "sunrise", "aerial"],
                "source": "gemini_vision",
                "confidence": 0.99,
            },
            {
                "scope": "time_range",
                "start_sec": 4.5,
                "end_sec": 7.5,
                "label": "rain on glass detail",
                "tags": ["rain", "glass"],
                "source": "gemini_vision",
                "confidence": 0.9,
            },
        ],
    )
    return blue, red


def test_set_shotlist_fits_persistent_evidence_before_one_write_and_assembles_ranges(
    monkeypatch, tmp_path: Path
) -> None:
    _patch_account_roots(monkeypatch, tmp_path)
    account_id = "google_outline_fit"
    blue, red = _seed_two_assets(account_id, tmp_path)
    ctx = _ctx(tmp_path, account_id)

    result = _call(
        "set_shotlist",
        {
            "fit_media": True,
            "shotlist": {
                "scenes": [
                    {
                        "shots": [
                            {"id": "opening", "duration_sec": 3, "search_query": "city sunrise aerial"},
                            {"id": "detail", "duration_sec": 2, "search_query": "rain glass"},
                        ]
                    }
                ]
            },
        },
        ctx,
    )
    assert result["evidence_fit"]["coverage"] == {"filled": 2, "total": 2}

    reloaded = ProjectHandle.open(
        tmp_path / "project", "outline-fit", session_id="outline-fit-reload"
    ).load()["shotlist"]
    shots = {shot["id"]: shot for _scene, shot in iter_shots(reloaded)}
    assert shots["opening"]["library_asset_id"] == blue["asset_id"]
    assert shots["opening"]["evidence"]["decision"] == "prefer"
    assert "explicit user preference" in shots["opening"]["evidence"]["ranking_reasons"][0]
    assert shots["opening"]["evidence"]["provenance"]["asset"]["library_asset_id"] == blue["asset_id"]
    assert shots["opening"]["evidence"]["provenance"]["annotation"]["annotation_id"]
    assert (shots["opening"]["source_in"], shots["opening"]["source_out"]) == (1.25, 4.25)
    assert shots["detail"]["library_asset_id"] == red["asset_id"]
    assert (shots["detail"]["source_in"], shots["detail"]["source_out"]) == (4.5, 6.5)
    assert shots["opening"]["alternatives"]

    # Simulate a service/session restart: the project still holds its durable
    # library ids, while the transient session registry is empty.
    restarted = _ctx(tmp_path, account_id)
    assembled = _call("assemble_shotlist", {}, restarted)
    assert assembled["assembled"] == 2
    clips = [
        clip for clip in restarted.project.load()["timeline"]["clips"]
        if clip.get("media_kind") == "video"
    ]
    assert [(clip["source_in"], clip["source_out"]) for clip in clips] == [
        (1.25, 4.25),
        (4.5, 6.5),
    ]
    assert [clip["provenance"]["shot_id"] for clip in clips] == ["opening", "detail"]


def test_outline_fit_deduplicates_near_identical_ranges_across_nodes(
    monkeypatch, tmp_path: Path
) -> None:
    _patch_account_roots(monkeypatch, tmp_path)
    account_id = "google_outline_dedupe"
    asset = import_media(account_id, _video(tmp_path / "overlap.mp4"))
    MA.upsert_annotations(
        account_id,
        asset["asset_id"],
        [
            {
                "scope": "time_range", "start_sec": 1, "end_sec": 5,
                "label": "interview answer primary", "tags": ["interview", "answer"],
                "source": "gemini_vision", "confidence": 0.9,
            },
            {
                "scope": "time_range", "start_sec": 1.1, "end_sec": 5.1,
                "label": "interview answer duplicate", "tags": ["interview", "answer"],
                "source": "gemini_vision", "confidence": 0.8,
            },
        ],
    )
    ctx = _ctx(tmp_path, account_id)
    result = _call(
        "set_shotlist",
        {
            "fit_media": True,
            "shotlist": {
                "scenes": [{"shots": [
                    {"id": "a", "duration_sec": 3, "search_query": "interview answer"},
                    {"id": "b", "duration_sec": 3, "search_query": "interview answer"},
                ]}]
            },
        },
        ctx,
    )
    assert result["evidence_fit"]["coverage"] == {"filled": 1, "total": 2}
    assert result["evidence_fit"]["unfilled"][0]["reason"] == (
        "no non-overlapping evidence in the global assignment"
    )


def test_fit_media_is_agent_reachable_on_non_timeline_shotlist_tools() -> None:
    schemas = {item["function"]["name"]: item["function"] for item in TOOL_SCHEMAS}
    assert "fit_shotlist_media" not in schemas
    assert "fit_shotlist_media" not in DISPATCHER
    for name in ("draft_shotlist", "set_shotlist"):
        assert "fit_media" in schemas[name]["parameters"]["properties"]


def test_explicit_user_reject_removes_candidate() -> None:
    result = evaluate_evidence_candidates(
        {
            "results": [
                {
                    "library_asset_id": "asset_one",
                    "duration": 10,
                    "score": 1,
                    "matched_terms": ["interview"],
                    "time_ranges": [
                        {
                            "scope": "time_range",
                            "annotation_id": "ann_model",
                            "start_sec": 1,
                            "end_sec": 6,
                            "label": "interview answer",
                            "source": "gemini_vision",
                            "confidence": 0.95,
                        }
                    ],
                    "annotations": [
                        {
                            "scope": "time_range",
                            "annotation_id": "ann_reject",
                            "start_sec": 1,
                            "end_sec": 6,
                            "label": "do not use",
                            "source": "user",
                            "metadata": {
                                "evidence": {
                                    "decision": "reject",
                                    "claim_key": "editorial.usability",
                                }
                            },
                        }
                    ],
                }
            ]
        },
        query="interview",
        desired_duration_sec=3,
    )
    assert result["candidates"] == []
    assert result["excluded"][0]["reason"] == "rejected by user correction ann_reject"


def test_user_reject_survives_machine_reindex_and_blocks_real_fit(
    monkeypatch, tmp_path: Path
) -> None:
    _patch_account_roots(monkeypatch, tmp_path)
    account_id = "google_outline_reject"
    asset = import_media(account_id, _video(tmp_path / "reject.mp4"))
    MA.create_annotation(
        account_id,
        asset["asset_id"],
        {
            "scope": "time_range",
            "start_sec": 1,
            "end_sec": 6,
            "label": "creator says unusable",
            "source": "user",
            "metadata": {
                "evidence": {
                    "version": 1,
                    "decision": "reject",
                    "claim_key": "editorial.usability",
                }
            },
        },
    )
    first = MA.upsert_annotations(
        account_id,
        asset["asset_id"],
        [{
            "scope": "time_range", "start_sec": 1, "end_sec": 6,
            "label": "interview answer", "source": "heuristic", "confidence": 0.99,
        }],
        replace_source="heuristic",
    )[0]["annotation_id"]
    second = MA.upsert_annotations(
        account_id,
        asset["asset_id"],
        [{
            "scope": "time_range", "start_sec": 1, "end_sec": 6,
            "label": "interview answer", "source": "heuristic", "confidence": 0.99,
        }],
        replace_source="heuristic",
    )[0]["annotation_id"]
    assert first != second
    assert any(item["source"] == "user" for item in MA.list_annotations(account_id, asset["asset_id"]))

    ctx = _ctx(tmp_path, account_id)
    result = _call(
        "set_shotlist",
        {
            "fit_media": True,
            "shotlist": {"scenes": [{"shots": [{
                "id": "answer", "duration_sec": 3, "search_query": "interview answer",
            }]}]},
        },
        ctx,
    )
    assert result["evidence_fit"]["coverage"] == {"filled": 0, "total": 1}
    stored = ctx.project.load()["shotlist"]["scenes"][0]["shots"][0]
    assert stored["asset_id"] is None and stored["evidence"] is None
