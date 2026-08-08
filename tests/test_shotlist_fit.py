from __future__ import annotations

from copy import deepcopy

from gemia.media_evidence import rank_evidence_candidates
from gemia.shotlist_fit import fit_shotlist_to_media


def _row(
    annotation_id: str,
    start: float,
    end: float,
    *,
    confidence: float = 0.7,
    source: str = "gemini_vision",
    metadata: dict | None = None,
) -> dict:
    return {
        "scope": "time_range",
        "annotation_id": annotation_id,
        "start_sec": start,
        "end_sec": end,
        "label": f"label {annotation_id}",
        "note": f"note {annotation_id}",
        "tags": ["one", annotation_id],
        "category": "keeper",
        "confidence": confidence,
        "source": source,
        "metadata": metadata or {"camera": {"move": "pan"}},
        "created_at": "2026-07-20T10:00:00+00:00",
        "updated_at": "2026-07-20T10:00:00+00:00",
        "match_rank": -0.2,
    }


def _asset(
    asset_id: str,
    rows: list[dict],
    *,
    score: float = 0.5,
    annotations: list[dict] | None = None,
    duration: float = 60.0,
) -> dict:
    return {
        "library_asset_id": asset_id,
        "name": f"{asset_id}.mp4",
        "kind": "video",
        "duration": duration,
        "score": score,
        "matched_terms": ["city", "opening"],
        "asset_labels": ["documentary"],
        "time_ranges": deepcopy(rows),
        "annotations": deepcopy(annotations if annotations is not None else rows),
    }


def _result(*assets: dict) -> dict:
    return {"query": "q", "results": list(assets)}


def _shotlist(*shots: dict) -> dict:
    return {"logline": "fit", "scenes": [{"id": "scene", "shots": list(shots)}]}


def test_only_traceable_ranges_that_cover_the_shot_become_candidates() -> None:
    asset_scope = _row("asset_only", 0.0, 30.0)
    asset_scope["scope"] = "asset"
    result = _result(
        _asset(
            "asset_a",
            [asset_scope, _row("too_short", 1.0, 2.5), _row("valid", 3.0, 7.0)],
        )
    )

    ranked = rank_evidence_candidates(
        result, query="opening", desired_duration_sec=3.0
    )

    assert [item["annotation_id"] for item in ranked] == ["valid"]
    assert (ranked[0]["start_sec"], ranked[0]["end_sec"]) == (3.0, 7.0)
    assert ranked[0]["provenance"]["annotation"]["note"] == "note valid"
    assert ranked[0]["metadata"] == {"camera": {"move": "pan"}}


def test_user_reject_matches_stable_span_and_prefer_is_not_fake_confidence() -> None:
    reject = _row(
        "user_reject",
        4.0,
        8.0,
        source="user",
        metadata={
            "evidence": {
                "version": 1,
                "decision": "reject",
                "claim_key": "editorial.usability",
            }
        },
    )
    # The machine id changes on re-index; the user's stable asset/span decision
    # must still veto the replacement row by midpoint.
    for machine_id in ("machine_before", "machine_after"):
        ranked = rank_evidence_candidates(
            _result(
                _asset(
                    "asset_reindexed",
                    [_row(machine_id, 5.0, 7.0)],
                    annotations=[reject],
                )
            ),
            query="answer",
            desired_duration_sec=1.0,
        )
        assert ranked == []

    prefer = _row(
        "user_prefer",
        10.0,
        16.0,
        source="user",
        metadata={
            "evidence": {
                "version": 1,
                "decision": "prefer",
                "claim_key": "editorial.usability",
            }
        },
    )
    preferred_observation = _row("machine_low_conf", 11.0, 15.0, confidence=0.2)
    ordinary = _row("machine_high_conf", 20.0, 24.0, confidence=0.99)
    ranked = rank_evidence_candidates(
        _result(
            _asset(
                "preferred_asset",
                [preferred_observation],
                score=-2.0,
                annotations=[prefer],
            ),
            _asset("ordinary_asset", [ordinary], score=4.0),
        ),
        query="answer",
        desired_duration_sec=3.0,
    )

    assert ranked[0]["annotation_id"] == "machine_low_conf"
    assert ranked[0]["decision"] == "prefer"
    assert ranked[0]["authority_tier"] == 1
    assert ranked[0]["confidence"] == 0.2
    assert ranked[0]["score_components"]["confidence"] == 0.4
    assert (
        ranked[0]["provenance"]["decision_control"]["annotation_id"]
        == "user_prefer"
    )


def test_global_assignment_maximizes_coverage_before_local_score() -> None:
    x = _asset("asset_x", [_row("x", 0.0, 5.0)], score=5.0)
    y = _asset("asset_y", [_row("y", 10.0, 15.0)], score=-1.0)
    original = _shotlist(
        {"id": "A", "search_query": "flexible", "duration_sec": 3.0},
        {"id": "B", "search_query": "only x", "duration_sec": 3.0},
    )

    def provider(query: str, _shot: dict) -> dict:
        return _result(x, y) if query == "flexible" else _result(x)

    fitted, report = fit_shotlist_to_media(original, provider)
    shots = fitted["scenes"][0]["shots"]

    assert original["scenes"][0]["shots"][0].get("library_asset_id") is None
    assert report["solver"] == "exact"
    assert report["coverage"] == {"filled": 2, "total": 2}
    assert [shot["library_asset_id"] for shot in shots] == ["asset_y", "asset_x"]
    assert (shots[0]["source_in"], shots[0]["source_out"]) == (10.0, 13.0)
    assert (shots[1]["source_in"], shots[1]["source_out"]) == (0.0, 3.0)


def test_high_iou_ranges_are_not_reused_but_distinct_ranges_are() -> None:
    overlapping_a = _row("overlap_a", 0.0, 10.0)
    overlapping_b = _row("overlap_b", 1.0, 11.0)  # IoU 9/11 >= .8
    distinct = _row("distinct", 20.0, 30.0)
    result = _result(
        _asset("long_asset", [overlapping_a, overlapping_b, distinct], score=1.0)
    )
    original = _shotlist(
        {"id": "one", "search_query": "moment", "duration_sec": 2.0},
        {"id": "two", "search_query": "moment", "duration_sec": 2.0},
        {"id": "three", "search_query": "moment", "duration_sec": 2.0},
    )

    fitted, report = fit_shotlist_to_media(original, lambda _q, _s: result)
    assigned = [item["annotation_id"] for item in report["assignments"]]

    assert report["coverage"] == {"filled": 2, "total": 3}
    assert "distinct" in assigned
    assert not ({"overlap_a", "overlap_b"} <= set(assigned))
    filled = [
        shot for shot in fitted["scenes"][0]["shots"] if shot.get("evidence")
    ]
    assert len(filled) == 2


def test_full_provenance_and_three_explainable_alternatives_are_preserved() -> None:
    rows = [
        _row(f"ann_{index}", float(index * 10), float(index * 10 + 6), confidence=0.8)
        for index in range(5)
    ]
    result = _result(_asset("asset_many", rows, score=1.25))
    original = _shotlist(
        {"id": "hero", "search_query": "city opening", "duration_sec": 3.0}
    )

    fitted_a, report_a = fit_shotlist_to_media(original, {"hero": result})
    fitted_b, report_b = fit_shotlist_to_media(original, {"hero": result})
    shot = fitted_a["scenes"][0]["shots"][0]

    assert fitted_a == fitted_b
    assert report_a == report_b
    assert shot["asset_id"] is None
    assert shot["library_asset_id"] == "asset_many"
    assert shot["evidence"]["evidence_id"] == shot["evidence"]["annotation_id"]
    assert shot["evidence"]["note"].startswith("note ann_")
    assert shot["evidence"]["tags"][0] == "one"
    assert shot["evidence"]["metadata"] == {"camera": {"move": "pan"}}
    assert shot["evidence"]["provenance"]["asset"]["name"] == "asset_many.mp4"
    assert shot["evidence"]["provenance"]["annotation"]["category"] == "keeper"
    assert len(shot["alternatives"]) == 3
    assert all(item["reason_not_selected"] for item in shot["alternatives"])
