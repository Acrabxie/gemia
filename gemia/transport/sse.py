"""SSE transport for the Lumeri v3 agent loop.

Invariants (grep-verifiable):

1. ``tool_exec_progress`` events MUST come from a real progress callback
   inside a tool dispatcher (FFmpeg stderr ``out_time=`` parsing, Veo job
   poll, etc.). The host never synthesizes progress with sleep + fake
   percentages. A tool that has no real progress channel (e.g.
   ``analyze_media``) emits only ``tool_exec_start`` and
   ``tool_exec_result``; the frontend renders an indeterminate spinner
   in between. That is honest reporting, not a defect.

2. ``model_text_delta`` events MUST come from a real Gemini stream
   chunk. The host never synthesizes "thinking...", "executing...", or
   any other status narration. If the model is silent, the user-facing
   stream stays silent.

This module owns per-session in-process event buffers. Producers (the
agent loop running in a worker thread) call ``emit`` from any thread.
The SSE HTTP handler (in server.py) calls ``iter_events`` from its
request thread and writes chunks to the response until ``close`` is
signaled.

Each emit assigns a monotonic per-session ``event_id``. A bounded ring
buffer (``REPLAY_BUFFER_SIZE`` events) keeps recent events so a
reconnecting client with ``Last-Event-ID`` can replay everything it
missed before resuming the live stream. The buffer is per-session; old
events are discarded once the buffer is full.
"""
from __future__ import annotations

import json
import threading
from collections import deque
from typing import Any, Iterator


REPLAY_BUFFER_SIZE = 200

class _SessionState:
    __slots__ = ("buffer", "next_id", "closed")

    def __init__(self, *, last_event_id: int = 0) -> None:
        self.buffer: deque[tuple[int, dict[str, Any]]] = deque(maxlen=REPLAY_BUFFER_SIZE)
        self.next_id: int = last_event_id + 1
        self.closed: bool = False


class SseSessionRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)
        self._states: dict[str, _SessionState] = {}

    def register(self, session_id: str, *, last_event_id: int = 0) -> None:
        last_event_id = int(last_event_id)
        if last_event_id < 0:
            raise ValueError("last_event_id must be non-negative")
        with self._cv:
            if session_id in self._states:
                raise ValueError(f"session already registered: {session_id}")
            self._states[session_id] = _SessionState(last_event_id=last_event_id)

    def is_registered(self, session_id: str) -> bool:
        with self._lock:
            return session_id in self._states

    def emit(self, session_id: str, event: dict[str, Any]) -> int | None:
        """Emit one event, return its assigned event_id (or None if not registered)."""
        with self._cv:
            state = self._states.get(session_id)
            if state is None or state.closed:
                return None
            event_id = state.next_id
            state.next_id += 1
            state.buffer.append((event_id, dict(event)))
            self._cv.notify_all()
        return event_id

    def close(self, session_id: str) -> None:
        with self._cv:
            state = self._states.get(session_id)
            if state is None:
                return
            state.closed = True
            self._cv.notify_all()

    def unregister(self, session_id: str) -> None:
        with self._cv:
            self._states.pop(session_id, None)
            self._cv.notify_all()

    def replay_from(self, session_id: str, last_event_id: int) -> list[tuple[int, dict[str, Any]]]:
        """Return all buffered events with id > last_event_id, oldest first."""
        with self._lock:
            state = self._states.get(session_id)
            if state is None:
                return []
            return [(eid, ev) for (eid, ev) in state.buffer if eid > last_event_id]

    def latest_event_id(self, session_id: str) -> int | None:
        with self._lock:
            state = self._states.get(session_id)
            if state is None:
                return None
            return state.next_id - 1 if state.next_id > 1 else 0

    def oldest_event_id(self, session_id: str) -> int | None:
        with self._lock:
            state = self._states.get(session_id)
            if state is None or not state.buffer:
                return None
            return state.buffer[0][0]

    def _get_state(self, session_id: str) -> _SessionState | None:
        with self._lock:
            return self._states.get(session_id)


REGISTRY = SseSessionRegistry()


def format_sse_chunk(event_id: int, event: dict[str, Any]) -> bytes:
    payload = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
    return f"id: {event_id}\ndata: {payload}\n\n".encode("utf-8")


def iter_events(session_id: str, *, last_event_id: int | None = None) -> Iterator[bytes]:
    """Yield SSE-formatted bytes for a session until close() is signaled.

    If ``last_event_id`` is provided, first replays every buffered event
    with id greater than ``last_event_id`` (in order), then resumes
    live streaming. If the requested id is older than the replay buffer,
    a synthetic ``replay_gap`` event is emitted before the remaining
    replay so the client knows it must resync instead of silently missing
    history.
    """
    cutoff = int(last_event_id) if last_event_id is not None else 0
    gap_checked = False
    while True:
        chunks: list[tuple[int, dict[str, Any]]] = []
        with REGISTRY._cv:  # noqa: SLF001
            state = REGISTRY._states.get(session_id)  # noqa: SLF001
            if state is None:
                return

            if last_event_id is not None and not gap_checked:
                gap_checked = True
                if state.buffer:
                    oldest = state.buffer[0][0]
                    latest = state.next_id - 1
                    if latest > cutoff and cutoff < oldest - 1:
                        gap_id = oldest - 1
                        chunks.append(
                            (
                                gap_id,
                                {
                                    "kind": "replay_gap",
                                    "requested_last_event_id": cutoff,
                                    "oldest_available_event_id": oldest,
                                    "latest_event_id": latest,
                                    "missed_event_count": (oldest - 1) - cutoff,
                                },
                            )
                        )
                        cutoff = gap_id

            for eid, ev in state.buffer:
                if eid > cutoff:
                    chunks.append((eid, ev))

            if chunks:
                cutoff = chunks[-1][0]
            elif state.closed:
                return
            else:
                REGISTRY._cv.wait()
                continue

        for eid, ev in chunks:
            yield format_sse_chunk(eid, ev)


__all__ = [
    "REGISTRY",
    "REPLAY_BUFFER_SIZE",
    "SseSessionRegistry",
    "format_sse_chunk",
    "iter_events",
]
