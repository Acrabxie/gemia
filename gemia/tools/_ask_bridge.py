"""Human-in-the-loop bridge for the ``elicit`` verb.

The agent loop runs each session inside a dedicated thread + asyncio event loop
(see ``gemia.session_manager.SessionRunner``). When the model calls ``elicit``,
its dispatcher — running *on that session loop* — emits an ``ask_question`` event
to the frontend and then ``await``s the user's answer. An HTTP handler on a
*different* thread delivers the answer; it must hop back onto the session loop via
``call_soon_threadsafe`` to resolve the awaiting future.

``AskBridge`` encapsulates exactly that plumbing so the agent loop and the routes
stay thin, and so the wait/deliver/timeout logic is unit-testable without standing
up the HTTP server.
"""
from __future__ import annotations

import asyncio
import os
from typing import Any, Callable, Optional

EmitFn = Callable[[dict[str, Any]], None]


def _default_timeout_from_env() -> float:
    try:
        return float(os.environ.get("LUMERI_ASK_TIMEOUT_SEC") or 300.0)
    except (TypeError, ValueError):
        return 300.0


class AskBridge:
    """Per-session registry of pending questions awaiting a user answer."""

    def __init__(
        self,
        emit: EmitFn,
        *,
        default_timeout: Optional[float] = None,
        max_timeout: Optional[float] = None,
    ) -> None:
        self._emit = emit
        self.default_timeout = (
            float(default_timeout) if default_timeout is not None
            else _default_timeout_from_env()
        )
        self.max_timeout = (
            float(max_timeout) if max_timeout is not None else self.default_timeout
        )
        self._pending: dict[str, asyncio.Future] = {}
        self._questions: dict[str, dict[str, Any]] = {}
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    async def emit_and_wait(
        self, question: dict[str, Any], *, timeout: Optional[float] = None
    ) -> Optional[dict[str, Any]]:
        """Emit ``ask_question`` and await the answer on the current loop.

        Returns the raw ``{control_key: value}`` answers dict, or ``None`` if no
        answer arrives within ``timeout`` (the caller then applies defaults).
        """
        loop = asyncio.get_running_loop()
        self._loop = loop
        qid = str(question.get("question_id"))
        fut: asyncio.Future = loop.create_future()
        self._pending[qid] = fut
        self._questions[qid] = question

        self._emit({"kind": "ask_question", "question": question})

        requested = self.default_timeout if timeout is None else float(timeout)
        wait = min(requested, self.max_timeout) if self.max_timeout > 0 else requested
        timer = None
        if wait > 0:
            def _on_timeout() -> None:
                if not fut.done():
                    fut.set_result(None)
            timer = loop.call_later(wait, _on_timeout)
        elif not fut.done():
            fut.set_result(None)

        try:
            return await fut
        finally:
            if timer is not None:
                timer.cancel()
            self._pending.pop(qid, None)
            self._questions.pop(qid, None)

    def deliver(self, question_id: str, answers: dict[str, Any]) -> bool:
        """Resolve a pending question's future from any thread.

        Returns ``True`` if the question is currently pending and delivery was
        scheduled; ``False`` if it is unknown / already answered / loop not ready.
        """
        loop = self._loop
        if loop is None or str(question_id) not in self._pending:
            return False

        def _resolve() -> None:
            fut = self._pending.get(str(question_id))
            if fut is not None and not fut.done():
                fut.set_result(dict(answers or {}))

        loop.call_soon_threadsafe(_resolve)
        return True

    def get_pending_question(self, question_id: str) -> Optional[dict[str, Any]]:
        """Return the question dict for a currently pending question, or None."""
        return self._questions.get(str(question_id))

    def pending_ids(self) -> list[str]:
        return list(self._pending)
