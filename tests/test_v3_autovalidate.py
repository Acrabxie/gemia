"""Post-edit self-correction for AgentLoopV3 (opencode pattern #2).

opencode appends LSP diagnostics to a tool_result right after a file edit so the
model is grounded in the new state. We port that to lumenframe: after a
SUCCESSFUL *mutating* lumen verb (tool name starts with ``lumen_`` but is not a
read-only verb), the loop appends a compact POST-STATE digest — the resulting
layer-tree summary + any lumenframe ``validate_doc`` warnings — to that exact
tool's tool_result text the model reads next.

Three things are pinned, all driven through the real ``_drive_turn`` success
path with a fake streaming client (same pattern as the other v3 loop tests):

  * a mutating lumen verb (``lumen_add_layer``, real dispatcher) → its recorded
    tool_result now CONTAINS the post-state digest (the layer-tree summary text
    + the validate line);
  * a NON-lumen tool's recorded tool_result is UNCHANGED (no digest leaks onto
    tools the feature must not touch);
  * an exception raised inside the digest path does NOT break the turn — the
    turn still runs to ``turn_complete`` (the feature is strictly non-fatal).
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, AsyncIterator

import gemia.agent_loop_v3 as loop_mod
from gemia.agent_loop_v3 import AgentLoopV3


# ──────────────────────────────────────────────────────────────────────
# Fake streaming client: calls ONE tool, then ends with text. Mirrors the
# fake-client pattern used by test_v3_doom_loop / test_v3_failure_breaker.
# ──────────────────────────────────────────────────────────────────────


class _CallsOneToolThenStops:
    """Fake model: stream 1 → call ``tool_name`` once; stream 2+ → end with text.

    The trailing text stream lets the loop reach its RC4 completion gate and then
    emit ``turn_complete`` honestly, so each test drives the full success path.
    """

    model = "fake"

    def __init__(self, tool_name: str, raw_args: str) -> None:
        self.calls = 0
        self._tool = tool_name
        self._args = raw_args

    async def stream_turn(
        self, messages: list[dict[str, Any]], *, tools=None, temperature: float = 0.7
    ) -> AsyncIterator[dict[str, Any]]:
        del messages, tools, temperature
        self.calls += 1
        if self.calls == 1:
            yield {"kind": "tool_call_start", "index": 0, "id": "call_1", "name": self._tool}
            yield {"kind": "tool_call_args_delta", "index": 0, "delta": self._args}
            yield {"kind": "finish", "reason": "tool_calls"}
            return
        # Subsequent streams: no tool calls → completion gate → turn_complete.
        yield {"kind": "text_delta", "text": "done"}
        yield {"kind": "finish", "reason": "stop"}


def _tool_result_for(loop: AgentLoopV3, call_id: str) -> str:
    """Return the recorded model-facing tool_result content for ``call_id``."""
    for msg in loop._messages:
        if msg.get("role") == "tool" and msg.get("tool_call_id") == call_id:
            content = msg.get("content")
            assert isinstance(content, str)
            return content
    raise AssertionError(f"no tool_result recorded for call_id={call_id!r}")


# ──────────────────────────────────────────────────────────────────────
# 1. Mutating lumen verb → tool_result gains the POST-STATE digest.
# ──────────────────────────────────────────────────────────────────────


def test_mutating_lumen_edit_appends_post_state_digest(tmp_path: Path) -> None:
    """A successful ``lumen_add_layer`` (real dispatcher) appends the post-state
    digest — layer-tree summary + validate line — into its tool_result text."""
    client = _CallsOneToolThenStops("lumen_add_layer", '{"type": "text", "name": "Title"}')
    events: list[dict[str, Any]] = []
    loop = AgentLoopV3(
        session_id="lumen_edit",
        output_dir=tmp_path,
        gemini_client=client,  # type: ignore[arg-type]
        emit_event=events.append,
    )

    asyncio.run(loop.run_turn("add a title layer"))

    result_text = _tool_result_for(loop, "call_1")

    # The digest is present and is the opencode-style post-edit block.
    assert "[POST-EDIT STATE" in result_text
    assert "Layer tree:" in result_text
    # It reflects the ACTUAL new state: the text layer just added shows up in the
    # compact tree summary (proves the digest is the real post-edit document, not
    # a static string).
    assert "text" in result_text
    assert "Title" in result_text
    # lumenframe.validate_doc ran and the new doc is structurally clean.
    assert "Validate: none" in result_text

    # The original tool_result payload is still intact ahead of the digest
    # (strictly ADDITIVE — nothing was replaced).
    assert '"applied": true' in result_text
    assert result_text.index('"applied": true') < result_text.index("[POST-EDIT STATE")

    # The edit landed, but host-owned completion now requires a post-mutation
    # visual review. A structural digest alone must not be mislabeled complete.
    assert not [e for e in events if e.get("kind") == "turn_complete"]
    assert any(
        e.get("kind") == "turn_error" and e.get("reason") == "incomplete_goal"
        for e in events
    )


# ──────────────────────────────────────────────────────────────────────
# 2. Non-lumen tool → tool_result is UNCHANGED (no digest).
# ──────────────────────────────────────────────────────────────────────


class _PlainSucceeds:
    """A non-lumen dispatcher that returns a fixed successful result."""

    def __init__(self) -> None:
        self.n = 0

    async def __call__(self, args: dict[str, Any], ctx: Any) -> dict[str, Any]:
        self.n += 1
        return {"ok": True, "echo": args.get("q")}


def test_non_lumen_tool_result_has_no_digest(tmp_path: Path, monkeypatch) -> None:
    """A NON-lumen tool must NOT receive the post-state digest. Its recorded
    tool_result is exactly the dispatcher's JSON, with no POST-EDIT block."""
    disp = _PlainSucceeds()
    monkeypatch.setitem(loop_mod.DISPATCHER, "echo_tool", disp)

    client = _CallsOneToolThenStops("echo_tool", '{"q": "hello"}')
    events: list[dict[str, Any]] = []
    loop = AgentLoopV3(
        session_id="non_lumen",
        output_dir=tmp_path,
        gemini_client=client,  # type: ignore[arg-type]
        emit_event=events.append,
    )

    asyncio.run(loop.run_turn("call a plain tool"))

    result_text = _tool_result_for(loop, "call_1")

    # Dispatcher result is present; no digest of any kind was appended.
    assert disp.n == 1
    assert '"ok": true' in result_text
    assert "[POST-EDIT STATE" not in result_text
    assert "Layer tree:" not in result_text
    assert "Validate:" not in result_text

    assert [e for e in events if e.get("kind") == "turn_complete"]
    assert not [e for e in events if e.get("kind") == "turn_error"]


# ──────────────────────────────────────────────────────────────────────
# 3. Exception in the digest path is non-fatal → turn still completes.
# ──────────────────────────────────────────────────────────────────────


def test_digest_exception_does_not_break_turn(tmp_path: Path, monkeypatch) -> None:
    """If the post-state digest path raises, the loop swallows it and the turn
    still runs to completion (the feature is strictly non-fatal). The mutating
    edit itself still succeeds and is recorded — only the digest is missing."""

    def _boom(self: AgentLoopV3) -> str:  # noqa: ANN001
        raise RuntimeError("digest exploded")

    # Force the digest builder to blow up. The caller wraps it in try/except, so
    # the turn must survive and the edit's tool_result must remain valid.
    monkeypatch.setattr(AgentLoopV3, "_lumen_post_state_digest", _boom)

    client = _CallsOneToolThenStops("lumen_add_layer", '{"type": "text", "name": "Boom"}')
    events: list[dict[str, Any]] = []
    loop = AgentLoopV3(
        session_id="digest_boom",
        output_dir=tmp_path,
        gemini_client=client,  # type: ignore[arg-type]
        emit_event=events.append,
    )

    # Must NOT raise despite the digest path raising.
    asyncio.run(loop.run_turn("add a layer, but digest will explode"))

    # The digest exception is still non-fatal: the edit landed and the loop
    # reaches its normal host acceptance decision. It ends incomplete solely
    # because no post-mutation visual review occurred.
    assert not [e for e in events if e.get("kind") == "turn_complete"]
    assert any(
        e.get("kind") == "turn_error" and e.get("reason") == "incomplete_goal"
        for e in events
    )

    result_text = _tool_result_for(loop, "call_1")
    # The underlying edit still succeeded and was recorded...
    assert '"applied": true' in result_text
    # ...but no digest was appended (the builder raised before it could attach).
    assert "[POST-EDIT STATE" not in result_text
