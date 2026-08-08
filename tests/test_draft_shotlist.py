"""draft_shotlist: one-line theme -> full promo storyboard, end-to-end.

Covers the pure builder (structure, durations, moods, narration, on-screen
text) and the verb layer (persists through set_shotlist, replace=false previews
without persisting, input validation).
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from gemia.project_store import ProjectHandle
from gemia.project_model import iter_shots, normalize_shotlist
from gemia.tools import DISPATCHER
from gemia.tools._context import AssetRegistry, ToolContext
from gemia.tools.draft_shotlist import build_shotlist


def _ctx(tmp_path: Path) -> ToolContext:
    handle = ProjectHandle.open(tmp_path / "project", "v3-draft01", session_id="v3-draft01")
    return ToolContext(
        session_id="v3-draft01", output_dir=tmp_path, registry=AssetRegistry(),
        emit_progress=lambda _u: None, project=handle,
    )


def _call(verb: str, args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    return asyncio.run(DISPATCHER[verb](args, ctx))


# ── pure builder ─────────────────────────────────────────────────────────
def test_promo_structure_and_durations():
    theme = "一款帮你专注的极简待办 App"
    sl = build_shotlist(theme, template="promo", target_duration_sec=30, lang="zh")
    norm = normalize_shotlist(sl)                     # must survive the IR normalizer
    assert len(norm["scenes"]) == 5                   # Hook→Problem→Solution→Highlights→CTA
    shots = [s for _sc, s in iter_shots(norm)]
    assert len(shots) == 7
    total = sum(float(s["duration_sec"]) for s in shots)
    assert abs(total - 30.0) < 0.6                    # allocation converges on target
    assert all(s.get("narration") for s in shots)     # every shot has a voiceover line
    assert all(s.get("mood") for s in shots)          # every shot has a mood tag
    assert all(s.get("search_query") and theme in s["search_query"] for s in shots)
    assert norm["scenes"][0]["shots"][0]["on_screen_text"] == theme   # hook shows theme
    assert norm["scenes"][-1]["shots"][-1]["on_screen_text"]          # CTA has burned text
    # last shot overall carries no transition_after (nothing follows it)
    assert shots[-1]["transition_after"] is None


def test_story_template_and_language_variants():
    en = build_shotlist("A minimalist focus timer", template="story", target_duration_sec=20, lang="en")
    assert len(en["scenes"]) == 5
    shots = [s for _sc, s in iter_shots(normalize_shotlist(en))]
    assert 5 <= len(shots) <= 7
    assert abs(sum(float(s["duration_sec"]) for s in shots) - 20.0) < 0.6
    # english narration is english (no CJK)
    assert not any("一" <= c <= "鿿" for c in (shots[0]["narration"] or ""))


def test_min_shot_floor_on_tiny_target():
    sl = build_shotlist("x", template="promo", target_duration_sec=5, lang="en")
    shots = [s for _sc, s in iter_shots(normalize_shotlist(sl))]
    assert all(float(s["duration_sec"]) >= 1.5 for s in shots)  # never below floor


# ── verb layer ───────────────────────────────────────────────────────────
def test_dispatch_persists_via_set_shotlist(tmp_path):
    ctx = _ctx(tmp_path)
    out = _call("draft_shotlist", {"theme": "云端协作白板", "target_duration_sec": 24}, ctx)
    assert out["persisted"] is True and out["shot_count"] == 7
    # the shotlist really landed in project_state (undoable patch log)
    stored = ctx.project.load().get("shotlist") or {}
    assert sum(1 for _ in iter_shots(stored)) == 7
    assert stored["scenes"][0]["shots"][0]["on_screen_text"] == "云端协作白板"
    assert out["language"] == "zh"


def test_replace_false_previews_without_persisting(tmp_path):
    ctx = _ctx(tmp_path)
    out = _call("draft_shotlist", {"theme": "A focus timer", "replace": False}, ctx)
    assert out["persisted"] is False and "shotlist" in out
    assert (ctx.project.load().get("shotlist") or {}).get("scenes") in (None, [])  # nothing written


def test_fit_media_preview_uses_internal_fitter_without_persisting(monkeypatch, tmp_path):
    ctx = _ctx(tmp_path)
    called = {"count": 0}

    def fake_fit(_ctx, shotlist):
        called["count"] += 1
        fitted = normalize_shotlist(shotlist)
        fitted["scenes"][0]["shots"][0]["notes"] = "fit-ran"
        return fitted, {"coverage": {"filled": 0, "total": 7}, "assignments": []}

    monkeypatch.setattr(
        "gemia.tools._shotlist_media_fit.fit_media_for_context", fake_fit
    )
    out = _call(
        "draft_shotlist",
        {"theme": "A focus timer", "fit_media": True, "replace": False},
        ctx,
    )
    assert called["count"] == 1
    assert out["evidence_fit"]["coverage"] == {"filled": 0, "total": 7}
    assert out["shotlist"]["scenes"][0]["shots"][0]["notes"] == "fit-ran"
    assert ctx.project.load()["shotlist"]["scenes"] == []


def test_dispatch_is_real_not_stub():
    assert "draft_shotlist" in DISPATCHER
    assert "stub" not in getattr(DISPATCHER["draft_shotlist"], "__qualname__", "").lower()


def test_validation(tmp_path):
    ctx = _ctx(tmp_path)
    with pytest.raises(ValueError):
        _call("draft_shotlist", {"theme": "   "}, ctx)
    with pytest.raises(ValueError):
        _call("draft_shotlist", {"theme": "ok", "template": "nope"}, ctx)
