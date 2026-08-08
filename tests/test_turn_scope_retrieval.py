"""Regressions for the forced-work bug class (2026-07-17).

User report: Lumeri kept being force-marched into production work.  Repro
session `v3-284fb6971b2e`: "我想基于lumeri的logo做一个宣传片，你先把logo找到"
routed the turn into storyboard/motion_graphics, whose ledger demanded shot
plans and final video, so the model could never complete the commanded step
("find the logo") with a prose answer and was force-stopped incomplete_goal.

Three guards pinned here:
1. Retrieval-without-production requests route to inspect/files packs, not
   production packs keyed off topic nouns (logo, 宣传片, 动画…).
2. An explicit "先…" staged directive scopes the router/ledger to the
   commanded clause; the deferred goal stays context only.
3. Read/diagnostic tool failures are not blocking completion blockers — a
   bad-args inspect_timeline cannot wedge the turn into an unwinnable state.
"""

from __future__ import annotations

import pytest

from gemia.tool_router import ToolRouter, _PRODUCTION_PACKS
from gemia.turn_control import extract_scoped_directive
from gemia.turn_ledger import TurnLedger


# ── 1. staged-directive extraction ───────────────────────────────────


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("我想基于lumeri的logo做一个宣传片，你先把logo找到", "把logo找到"),
        ("先把logo找到", "把logo找到"),
        ("首先把logo找到，之后做片头", "把logo找到"),
        ("你先把素材导入，然后我们再谈脚本", "把素材导入"),
        ("我要做个短片。你先搜一下有没有海边素材。", "搜一下有没有海边素材"),
        ("I want a promo film. First, find the logo", "find the logo"),
    ],
)
def test_scoped_directive_extracts_commanded_clause(text: str, expected: str) -> None:
    assert extract_scoped_directive(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "帮我剪个视频",          # no staging marker
        "先别动时间线",          # inhibition, not a commanded step
        "优先保证画质",          # 优先 is vocabulary, not staging
        "事先说明一下规则",      # 事先 likewise
        "先生成一张海报",        # 先生 segmentation ambiguity → stay conservative
        "把开头剪掉3秒",
        "",
    ],
)
def test_scoped_directive_conservative_misses(text: str) -> None:
    assert extract_scoped_directive(text) is None


# ── 2. retrieval requests must not route to production packs ─────────


@pytest.mark.parametrize(
    "text",
    [
        "帮我找一下lumeri的logo",
        "把logo找到",
        "看看有没有海边的素材",
        "find the intro clip",
    ],
)
def test_retrieval_request_avoids_production_packs(text: str) -> None:
    workflows = ToolRouter(text).decision.workflows
    assert workflows, text
    assert not set(workflows) & set(_PRODUCTION_PACKS), (text, workflows)
    assert "media_inspect" in workflows


@pytest.mark.parametrize(
    ("text", "required_pack"),
    [
        ("帮我做一个宣传片", "storyboard"),
        ("给视频加个logo", "video_edit"),
        ("生成一张海报", "image"),
        ("找到那段片头然后剪掉前3秒", "video_edit"),  # retrieval+edit stays production
    ],
)
def test_production_requests_keep_production_routing(
    text: str, required_pack: str
) -> None:
    assert required_pack in ToolRouter(text).decision.workflows


# ── 3. the original repro completes as a retrieval turn ──────────────


def test_repro_scoped_retrieval_turn_completes_without_final_asset() -> None:
    request = "我想基于lumeri的logo做一个宣传片，你先把logo找到"
    scope = extract_scoped_directive(request) or request
    router = ToolRouter(scope)
    ledger = TurnLedger(
        scope,
        workflow=router.decision.primary_workflow,
        workflows=router.decision.workflows,
    )
    assert ledger.requires_final_asset is False
    assert list(ledger.steps) == ["inspect"]

    ledger.record_outcome("search_library", {"status": "ok", "result_count": 3})
    assert ledger.open_observations() == ()


# ── 4. read/diagnostic failures do not block completion ──────────────


def test_read_tool_failure_is_not_a_blocking_blocker() -> None:
    ledger = TurnLedger("做一个片头", workflow="storyboard", workflows=("storyboard",))
    ledger.record_outcome(
        "inspect_timeline",
        {"status": "error", "error_code": "E_UNCAUGHT"},
        call_id="c1",
    )
    observations = ledger.open_observations()
    assert not [item for item in observations if item.startswith("failure:")], observations
    # The failure is still recorded as feedback for the model.
    assert "c1" in ledger.unresolved_failures


# ── 5. opinion/small-talk questions are INFORMATION, not forced work ─


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("今天有什么想法？", "information"),
        ("你觉得呢", "information"),
        ("随便聊聊", "information"),
        ("你怎么看这个片子", "information"),
        ("what do you think?", "information"),
        ("any ideas for the intro?", "information"),
        # Action verbs must still win over the opinion carve-out.
        ("把这个想法做出来", "actionable"),
        ("有什么想法？帮我剪出来", "actionable"),
        ("给我个惊喜", "actionable"),
    ],
)
def test_smalltalk_questions_are_information(text: str, expected: str) -> None:
    from gemia.turn_control import classify_turn_intent

    assert classify_turn_intent(text).value == expected


def test_mutation_tool_failure_still_blocks() -> None:
    ledger = TurnLedger("做一个片头", workflow="storyboard", workflows=("storyboard",))
    ledger.record_outcome(
        "generate_video",
        {"status": "error", "error_code": "E_TOOL_FAILED"},
        call_id="c2",
    )
    observations = ledger.open_observations()
    assert [item for item in observations if item.startswith("failure:c2")], observations
