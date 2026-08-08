"""User-decision answers fail closed until a pending question validates."""
from __future__ import annotations

from typing import Any

import gemia.v3_routes as routes
class _PendingRunner:
    session_id = "decision-session"

    def __init__(self, question: dict[str, Any]) -> None:
        self.question = question
        self.delivered: list[tuple[str, dict[str, Any]]] = []

    def get_pending_question(self, question_id: str) -> dict[str, Any] | None:
        return self.question if question_id == "ask_required" else None

    def deliver_ask_answer(self, question_id: str, answers: dict[str, Any]) -> bool:
        self.delivered.append((question_id, answers))
        return True


def test_invalid_pending_question_schema_does_not_release_wait(monkeypatch) -> None:
    runner = _PendingRunner(
        {
            "question_id": "ask_required",
            "title": "Choose source",
            "controls": {"source": {"type": "not-a-real-control"}},
        }
    )
    responses: list[tuple[int, dict[str, Any]]] = []
    monkeypatch.setattr(
        routes,
        "_read_json_body",
        lambda _handler: {
            "question_id": "ask_required",
            "answers": {"source": "a"},
        },
    )
    monkeypatch.setattr(
        routes,
        "_json_response",
        lambda _handler, status, payload: responses.append((status, payload)),
    )
    assert routes._ask_response(object(), runner) is True  # type: ignore[arg-type]

    assert responses == [
        (
            422,
            {
                "error": "pending question schema is invalid",
                "code": "E_ASK_INVALID_SCHEMA",
                "question_id": "ask_required",
            },
        )
    ]
    assert runner.delivered == []


def test_invalid_nested_control_schema_does_not_release_wait(monkeypatch) -> None:
    runner = _PendingRunner(
        {
            "question_id": "ask_required",
            "title": "Choose source",
            "controls": {
                "source": {
                    "type": "panel",
                    "fields": {"camera": {"type": "not-a-real-control"}},
                }
            },
        }
    )
    responses: list[tuple[int, dict[str, Any]]] = []
    monkeypatch.setattr(
        routes,
        "_read_json_body",
        lambda _handler: {
            "question_id": "ask_required",
            "answers": {"source": {"camera": "a"}},
        },
    )
    monkeypatch.setattr(
        routes,
        "_json_response",
        lambda _handler, status, payload: responses.append((status, payload)),
    )

    assert routes._ask_response(object(), runner) is True  # type: ignore[arg-type]
    assert responses[0][0] == 422
    assert responses[0][1]["code"] == "E_ASK_INVALID_SCHEMA"
    assert runner.delivered == []


def test_missing_required_answers_do_not_use_schema_defaults(monkeypatch) -> None:
    runner = _PendingRunner(
        {
            "question_id": "ask_required",
            "title": "Choose source",
            "controls": {
                "source": {
                    "type": "select",
                    "options": ["camera-a", "camera-b"],
                    "default": "camera-a",
                }
            },
        }
    )
    responses: list[tuple[int, dict[str, Any]]] = []
    monkeypatch.setattr(
        routes,
        "_read_json_body",
        lambda _handler: {"question_id": "ask_required", "answers": {}},
    )
    monkeypatch.setattr(
        routes,
        "_json_response",
        lambda _handler, status, payload: responses.append((status, payload)),
    )

    assert routes._ask_response(object(), runner) is True  # type: ignore[arg-type]
    assert responses[0][0] == 422
    assert responses[0][1]["code"] == "E_ASK_INVALID_ANSWER"
    assert responses[0][1]["field_errors"] == {"source": "answer required"}
    assert runner.delivered == []


def test_unvalidated_custom_panel_does_not_release_wait(monkeypatch) -> None:
    runner = _PendingRunner(
        {
            "question_id": "ask_required",
            "title": "Custom decision",
            "controls": {"custom": {"type": "custom_panel", "schema": {}}},
        }
    )
    responses: list[tuple[int, dict[str, Any]]] = []
    monkeypatch.setattr(
        routes,
        "_read_json_body",
        lambda _handler: {
            "question_id": "ask_required",
            "answers": {"custom": {}},
        },
    )
    monkeypatch.setattr(
        routes,
        "_json_response",
        lambda _handler, status, payload: responses.append((status, payload)),
    )

    assert routes._ask_response(object(), runner) is True  # type: ignore[arg-type]
    assert responses[0][0] == 422
    assert responses[0][1]["code"] == "E_ASK_INVALID_SCHEMA"
    assert runner.delivered == []
