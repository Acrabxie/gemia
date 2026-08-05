"""Tiny in-process event bus for the Lumeri runtime kernel.

The bus is deliberately synchronous and single-process: it's the
foundation for streamed sandbox output, agent-loop telemetry, and an
eventual SSE bridge to the UI. For now it's read by JSONL sinks and
test handlers.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


@dataclass
class Event:
    type: str
    payload: dict[str, Any] = field(default_factory=dict)
    timestamp: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.type, "timestamp": self.timestamp, "payload": self.payload}


Handler = Callable[[Event], None]


class EventBus:
    def __init__(self) -> None:
        self._handlers: list[Handler] = []

    def subscribe(self, handler: Handler) -> None:
        self._handlers.append(handler)

    def emit(self, event_type: str, payload: dict[str, Any] | None = None) -> Event:
        event = Event(
            type=event_type,
            payload=dict(payload or {}),
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
        for handler in list(self._handlers):
            try:
                handler(event)
            except Exception:
                # Bus must never crash producers; subscribers are best-effort.
                continue
        return event


class JsonlEventSink:
    """Append events to a newline-delimited JSON log file."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def __call__(self, event: Event) -> None:
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event.to_dict(), ensure_ascii=False) + "\n")


class MemoryEventSink:
    """Collect events in-memory for tests."""

    def __init__(self) -> None:
        self.events: list[Event] = []

    def __call__(self, event: Event) -> None:
        self.events.append(event)

    def types(self) -> list[str]:
        return [e.type for e in self.events]
