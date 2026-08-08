"""FTS5 semantic media search — normalizer, trigger sync, and the source landmine.

Offline only: annotations are seeded directly through the public API against a
temp account root; no Gemini/vision network calls. Covers docs/semantic-search-
media-plan.md §8.1 acceptance items T1/T2/T3.
"""
from __future__ import annotations

import asyncio
import sqlite3
import subprocess
from pathlib import Path

import pytest

from gemia import accounts
from gemia import budget_guard
from gemia import media_annotations as MA
from gemia import media_search as MS
from gemia import plan_mode
from gemia.media_library import import_media, library_path
from gemia.tools import DISPATCHER, TOOL_NAMES, TOOL_SCHEMAS
from gemia.tools._context import AssetRegistry, ToolContext


def _patch_account_roots(monkeypatch, tmp_path: Path) -> None:
    root = tmp_path / "accounts"
    monkeypatch.setattr(accounts, "ACCOUNTS_ROOT", root)
    monkeypatch.setattr(accounts, "ACTIVE_ACCOUNT_PATH", root / "active.json")
    monkeypatch.setattr(accounts, "CONFIG_PATH", tmp_path / "config.json")


def _make_video(path: Path, seconds: float = 2.0) -> Path:
    subprocess.run(
        [
            "ffmpeg", "-y", "-f", "lavfi",
            "-i", f"testsrc2=duration={seconds}:size=128x128:rate=15",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path),
        ],
        capture_output=True,
        check=True,
    )
    return path


def _make_image(path: Path) -> Path:
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=blue:s=64x48:d=0.1",
         "-frames:v", "1", str(path)],
        capture_output=True,
        check=True,
    )
    return path


# --------------------------------------------------------------------------- T1

def test_fts_normalize_golden() -> None:
    assert MS.fts_normalize("日落") == "日落"
    assert MS.fts_normalize("海边日落") == "海边 边日 日落"
    assert MS.fts_normalize("SunSet Beach") == "sunset beach"
    assert MS.fts_normalize("猫咪 跳跃") == "猫咪 跳跃"
    assert MS.fts_normalize("") == ""
    assert MS.fts_normalize("   ,，。!  ") == ""
    # single CJK char stays a single token (no bigram)
    assert MS.fts_normalize("海") == "海"


def test_build_match_expr() -> None:
    assert MS.build_match_expr("日落") == "日落"          # 2-char CJK bigram, exact
    assert MS.build_match_expr("海") == "海*"             # single CJK -> prefix
    assert MS.build_match_expr("sun") == "sun*"          # latin -> prefix
    assert MS.build_match_expr("海边日落") == "海边 边日 日落"
    assert MS.build_match_expr("  ") == ""                # empty is safe


# --------------------------------------------------------------------- T1 (query)

def _seed_beach_and_cat(acct: str, tmp_path: Path) -> tuple[dict, dict]:
    beach = import_media(acct, _make_video(tmp_path / "beach.mp4", 2.0))
    cat = import_media(acct, _make_image(tmp_path / "cat.png"))
    MA.upsert_annotations(
        acct, beach["asset_id"],
        [
            {"scope": "asset", "label": "drone shot of sunset over the sea",
             "note": "海边日落 航拍", "tags": ["海边", "日落", "航拍", "sunset", "sea"],
             "category": "summary", "source": "gemini_vision", "confidence": 0.85},
            {"scope": "time_range", "start_sec": 0.5, "end_sec": 1.8,
             "label": "sun dips below the horizon at the beach",
             "note": "太阳落到海平面 海边日落", "tags": ["海边日落", "日落"],
             "category": "segment", "source": "gemini_vision", "confidence": 0.82},
        ],
        replace_source="gemini_vision",
    )
    MA.upsert_annotations(
        acct, cat["asset_id"],
        [{"scope": "asset", "label": "a cat jumping on a sofa", "note": "猫咪 跳跃",
          "tags": ["猫咪", "跳跃", "cat"], "category": "summary",
          "source": "gemini_vision", "confidence": 0.8}],
        replace_source="gemini_vision",
    )
    return beach, cat


def test_search_hits_and_timecodes(monkeypatch, tmp_path: Path) -> None:
    _patch_account_roots(monkeypatch, tmp_path)
    acct = "google_search"
    beach, cat = _seed_beach_and_cat(acct, tmp_path)

    # zh single -> beach, with a real time range inside the 2s clip
    r = MS.search_media_annotations(acct, "日落")
    assert r["result_count"] == 1
    hit = r["results"][0]
    assert hit["library_asset_id"] == beach["asset_id"]
    ranges = hit["time_ranges"]
    assert ranges and ranges[0]["start_sec"] == 0.5 and ranges[0]["end_sec"] == 1.8
    assert "日落" in hit["matched_terms"]

    # bigram AND: 海边日落 -> 海边 边日 日落 (contiguous only on beach)
    assert MS.search_media_annotations(acct, "海边日落")["result_count"] == 1
    # overlap bigram
    assert MS.search_media_annotations(acct, "边日")["result_count"] == 1
    # single-CJK prefix
    assert MS.search_media_annotations(acct, "海")["result_count"] == 1
    # english prefix
    assert MS.search_media_annotations(acct, "sunset")["result_count"] == 1
    assert MS.search_media_annotations(acct, "sun")["result_count"] == 1

    # AND-miss across two assets -> OR fallback flips fuzzy and returns both
    both = MS.search_media_annotations(acct, "日落 猫咪")
    assert both["fuzzy"] is True
    assert both["result_count"] == 2

    # kind filter
    assert MS.search_media_annotations(acct, "日落", kind="image")["result_count"] == 0
    assert MS.search_media_annotations(acct, "日落", kind="video")["result_count"] == 1
    assert MS.search_media_annotations(acct, "猫咪", kind="image")["result_count"] == 1

    # unindexed_count = assets with no vision-index 'ok' state (none written yet)
    assert MS.search_media_annotations(acct, "日落")["unindexed_count"] == 2


def test_search_empty_and_no_match(monkeypatch, tmp_path: Path) -> None:
    _patch_account_roots(monkeypatch, tmp_path)
    acct = "google_empty"
    # search before any annotation exists must not raise
    r = MS.search_media_annotations(acct, "日落")
    assert r["result_count"] == 0 and r["results"] == []
    _seed_beach_and_cat(acct, tmp_path)
    assert MS.search_media_annotations(acct, "完全不存在的词zzz")["result_count"] == 0


def test_injection_safety(monkeypatch, tmp_path: Path) -> None:
    _patch_account_roots(monkeypatch, tmp_path)
    acct = "google_inject"
    _seed_beach_and_cat(acct, tmp_path)
    # FTS5 operators must not survive normalization or raise a syntax error
    for evil in ['"del" OR *', "日落) OR (1", "^日落$", "NEAR(a b)", "* * *"]:
        out = MS.search_media_annotations(acct, evil)
        assert isinstance(out["result_count"], int)


# --------------------------------------------------------------------------- T2

def test_trigger_sync_and_integrity(monkeypatch, tmp_path: Path) -> None:
    _patch_account_roots(monkeypatch, tmp_path)
    acct = "google_triggers"
    asset = import_media(acct, _make_image(tmp_path / "x.png"))
    aid = asset["asset_id"]

    ann = MA.create_annotation(acct, aid, {
        "scope": "asset", "label": "misty forest at dawn",
        "note": "晨雾 森林", "tags": ["森林", "晨雾"], "source": "user",
    })
    assert MS.search_media_annotations(acct, "森林")["result_count"] == 1

    # UPDATE: old term drops, new term matches
    MA.update_annotation(acct, aid, ann["annotation_id"], {"note": "沙漠 骆驼", "tags": ["沙漠"]})
    assert MS.search_media_annotations(acct, "森林")["result_count"] == 0
    assert MS.search_media_annotations(acct, "沙漠")["result_count"] == 1

    # DELETE: gone from the index
    MA.delete_annotation(acct, aid, ann["annotation_id"])
    assert MS.search_media_annotations(acct, "沙漠")["result_count"] == 0

    # external-content FTS integrity must hold after insert/update/delete churn
    import sqlite3
    conn = sqlite3.connect(library_path(acct))
    try:
        conn.execute("INSERT INTO media_annotations_fts(media_annotations_fts) VALUES('integrity-check')")
    finally:
        conn.close()


# --------------------------------------------------------------------------- T3

def test_source_enum_landmine(monkeypatch, tmp_path: Path) -> None:
    """upsert(replace_source='gemini_vision') must NOT delete the user's own rows.

    Before the _VALID_SOURCE extension, _source() coerced the unknown
    'gemini_vision' to 'user', so re-indexing would DELETE user annotations. This
    pins the fix (red on the old enum, green now).
    """
    _patch_account_roots(monkeypatch, tmp_path)
    acct = "google_landmine"
    asset = import_media(acct, _make_image(tmp_path / "y.png"))
    aid = asset["asset_id"]

    MA.create_annotation(acct, aid, {
        "scope": "asset", "label": "hand-written keeper note",
        "note": "用户手写标注", "source": "user",
    })
    # a vision re-index pass replaces only its own source
    MA.upsert_annotations(acct, aid, [
        {"scope": "asset", "label": "vision caption", "note": "视觉标注",
         "source": "gemini_vision"},
    ], replace_source="gemini_vision")

    rows = MA.list_annotations(acct, aid)
    sources = sorted(r["source"] for r in rows)
    assert "user" in sources, "user annotation must survive a gemini_vision re-index"
    assert "gemini_vision" in sources
    assert len(rows) == 2

    # 'gemini_vision' and 'heuristic' are now first-class, not coerced to 'user'
    assert MA._source("gemini_vision") == "gemini_vision"
    assert MA._source("heuristic") == "heuristic"
    with pytest.raises(MA.MediaAnnotationError, match="invalid annotation source: bogus"):
        MA._source("bogus")


def test_upsert_prevalidates_and_rolls_back_as_one_transaction(
    monkeypatch, tmp_path: Path
) -> None:
    _patch_account_roots(monkeypatch, tmp_path)
    acct = "google_atomic_reindex"
    asset = import_media(acct, _make_image(tmp_path / "atomic.png"))
    aid = asset["asset_id"]
    previous = MA.create_annotation(
        acct,
        aid,
        {
            "scope": "asset",
            "label": "last known good caption",
            "source": "gemini_vision",
        },
    )

    # Every row is normalized before the replacement DELETE can run.
    with pytest.raises(MA.MediaAnnotationError, match="invalid annotation source"):
        MA.upsert_annotations(
            acct,
            aid,
            [
                {"scope": "asset", "label": "valid new caption", "source": "gemini_vision"},
                {"scope": "asset", "label": "bad source", "source": "unknown_model"},
            ],
            replace_source="gemini_vision",
        )
    assert [row["annotation_id"] for row in MA.list_annotations(acct, aid)] == [
        previous["annotation_id"]
    ]

    # A database failure after DELETE and one INSERT must roll the whole replacement
    # back, including restoring the old row.
    monkeypatch.setattr(MA, "_annotation_id", lambda: "ann_deadbeefdeadbeef")
    with pytest.raises(sqlite3.IntegrityError):
        MA.upsert_annotations(
            acct,
            aid,
            [
                {"scope": "asset", "label": "first", "source": "gemini_vision"},
                {"scope": "asset", "label": "second", "source": "gemini_vision"},
            ],
            replace_source="gemini_vision",
        )
    remaining = MA.list_annotations(acct, aid)
    assert [row["annotation_id"] for row in remaining] == [previous["annotation_id"]]
    assert remaining[0]["label"] == "last known good caption"


# --------------------------------------------------------------------- tool wiring

def test_search_media_wired() -> None:
    assert "search_media" in TOOL_NAMES
    assert "search_media" in DISPATCHER
    assert budget_guard._TOOL_COSTS["search_media"]["usd"] == 0.0
    # read-only tool must be plan-mode allowed, else test_plan_mode coverage goes red
    assert "search_media" in plan_mode.PLAN_ALLOWED_TOOLS
    by_name = {t["function"]["name"]: t for t in TOOL_SCHEMAS}
    assert by_name["search_media"]["function"]["parameters"]["required"] == ["query"]


def _ctx(tmp_path: Path, account_id: str) -> ToolContext:
    return ToolContext(
        session_id="search-media-test",
        output_dir=tmp_path,
        registry=AssetRegistry(),
        emit_progress=lambda _: None,
        extra={"account_id": account_id},
    )


def test_search_media_dispatch_registers_session_asset(monkeypatch, tmp_path: Path) -> None:
    _patch_account_roots(monkeypatch, tmp_path)
    acct = "google_tool"
    beach, cat = _seed_beach_and_cat(acct, tmp_path)
    ctx = _ctx(tmp_path, acct)

    out = asyncio.run(DISPATCHER["search_media"]({"query": "日落", "kind": "video"}, ctx))
    assert out["result_count"] == 1
    hit = out["results"][0]
    # the returned id is a live session asset the model can hand to timeline tools
    assert ctx.registry.contains(hit["asset_id"])
    assert hit["library_asset_id"] == beach["asset_id"]
    assert hit["time_ranges"] and hit["time_ranges"][0]["start_sec"] == 0.5
    assert out["unindexed_count"] >= 1
    assert "annotate_media" in out["index_hint"]

    # empty query is a hard error (mirrors search_library)
    with pytest.raises(ValueError):
        asyncio.run(DISPATCHER["search_media"]({"query": "  "}, ctx))

    # no-match is a normal empty response, never an exception
    miss = asyncio.run(DISPATCHER["search_media"]({"query": "完全没有的东西zzz"}, ctx))
    assert miss["result_count"] == 0


def test_search_media_no_account(monkeypatch, tmp_path: Path) -> None:
    # isolate account roots to an EMPTY tmp dir so current_account_id() resolves to
    # nothing — never let this probe the user's real ~/.gemia library.
    _patch_account_roots(monkeypatch, tmp_path)
    ctx = ToolContext(
        session_id="s", output_dir=tmp_path, registry=AssetRegistry(),
        emit_progress=lambda _: None, extra={},
    )
    # no account_id and no active process account -> graceful empty, not a crash
    out = asyncio.run(DISPATCHER["search_media"]({"query": "日落"}, ctx))
    assert out["result_count"] == 0
