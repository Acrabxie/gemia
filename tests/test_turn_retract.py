"""Retract-last-turn semantics: content-anchored truncation of _messages.

The anchor is the last REAL user input (run_turn's argument), never a
host-injected role="user" row (background notes, failure nudges), and a
caller-supplied expected_message must match or the retract is refused.
"""
from __future__ import annotations

import asyncio
import tempfile
from types import MethodType

from gemia.agent_loop_v3 import AgentLoopV3


def _loop() -> AgentLoopV3:
    return AgentLoopV3(session_id="s-retract", output_dir=tempfile.mkdtemp(), gemini_client=object())


def _seed_turn(loop: AgentLoopV3, text: str, *, replies: int = 1) -> None:
    loop._messages.append({"role": "user", "content": text})
    loop._last_user_message = text
    for i in range(replies):
        loop._messages.append({"role": "assistant", "content": f"reply {i} to {text}"})


def test_retract_drops_user_message_and_everything_after() -> None:
    loop = _loop()
    _seed_turn(loop, "first")
    _seed_turn(loop, "second", replies=2)

    assert loop.retract_last_turn() == "second"
    assert [m["content"] for m in loop._messages] == ["first", "reply 0 to first"]


def test_retract_twice_refuses_second_time() -> None:
    loop = _loop()
    _seed_turn(loop, "only")
    assert loop.retract_last_turn() == "only"
    assert loop.retract_last_turn() is None
    assert loop._messages == []


def test_retract_with_no_history_returns_none() -> None:
    assert _loop().retract_last_turn() is None


def test_expected_message_mismatch_refuses_and_keeps_history() -> None:
    loop = _loop()
    _seed_turn(loop, "real turn")
    assert loop.retract_last_turn("stale UI view") is None
    assert len(loop._messages) == 2


def test_expected_message_match_retracts() -> None:
    loop = _loop()
    _seed_turn(loop, "real turn")
    assert loop.retract_last_turn("real turn") == "real turn"


def test_host_injected_user_rows_are_not_anchors() -> None:
    """A background note is role=user but must not survive as retract target:
    retract anchors on the real input and removes the note with the turn."""
    loop = _loop()
    _seed_turn(loop, "make a clip")
    loop._messages.append({"role": "user", "content": "[host] background job finished"})
    loop._messages.append({"role": "assistant", "content": "resumed"})

    assert loop.retract_last_turn() == "make a clip"
    assert loop._messages == []


def test_anchor_rewritten_away_refuses() -> None:
    """If trimming/compaction removed the anchor row, retract must refuse
    rather than delete an unrelated slice."""
    loop = _loop()
    _seed_turn(loop, "gone")
    loop._messages = [m for m in loop._messages if m.get("content") != "gone"]
    assert loop.retract_last_turn() is None
    assert len(loop._messages) == 1


def test_turn_start_checkpoint_contains_newest_retract_anchor() -> None:
    checkpoints: list[dict] = []
    loop = AgentLoopV3(
        session_id="s-checkpoint",
        output_dir=tempfile.mkdtemp(),
        gemini_client=object(),
        extra={
            "persist_runtime_checkpoint": lambda: checkpoints.append(
                loop.snapshot_runtime_state()
            )
        },
    )

    async def finish_without_model(self, user_message, display_intent):
        assert checkpoints

    loop._drive_turn = MethodType(finish_without_model, loop)
    asyncio.run(loop.run_turn("survive restart"))

    assert checkpoints[0]["last_user_message"] == "survive restart"
    assert checkpoints[0]["messages"][-1] == {
        "role": "user",
        "content": "survive restart",
    }
