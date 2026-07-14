"""In-memory LRU cache for deterministic Gemini planner responses.

Cache hits skip the network round-trip (which dominates plan_from_primitives
latency) when the user re-runs the same request inside the TTL window. Keys
are derived from user-controlled inputs only — never from non-deterministic
LLM-derived state like video_context — so identical user actions reuse a
plan even if upstream inspection differs.

Disabled by setting ``GEMIA_PLAN_CACHE=0``.
"""
from __future__ import annotations

import copy
import hashlib
import json
import time
from collections import OrderedDict
from threading import Lock
from typing import Any

from gemia.config_defaults import (
    PLAN_CACHE_ENABLED,
    PLAN_CACHE_MAX_ENTRIES,
    PLAN_CACHE_TTL_SEC,
)


def _hash_key(parts: dict[str, Any]) -> str:
    blob = json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class PlanCache:
    """LRU + TTL plan cache. Thread-safe enough for concurrent web requests."""

    def __init__(
        self,
        *,
        ttl_sec: float = PLAN_CACHE_TTL_SEC,
        max_entries: int = PLAN_CACHE_MAX_ENTRIES,
        enabled: bool = PLAN_CACHE_ENABLED,
    ) -> None:
        self.ttl_sec = ttl_sec
        self.max_entries = max_entries
        self.enabled = enabled
        self._store: OrderedDict[str, tuple[float, dict[str, Any]]] = OrderedDict()
        self._lock = Lock()
        self.hits = 0
        self.misses = 0

    def get(self, key: str) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                self.misses += 1
                return None
            ts, value = entry
            if time.time() - ts > self.ttl_sec:
                self._store.pop(key, None)
                self.misses += 1
                return None
            self._store.move_to_end(key)
            self.hits += 1
            # Defensive copy — callers commonly mutate plan dicts (e.g. setting
            # input_path/output_path) and we don't want those mutations to
            # leak back into the cache.
            return copy.deepcopy(value)

    def set(self, key: str, value: dict[str, Any]) -> None:
        if not self.enabled or not isinstance(value, dict):
            return
        with self._lock:
            self._store[key] = (time.time(), copy.deepcopy(value))
            self._store.move_to_end(key)
            while len(self._store) > self.max_entries:
                self._store.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                "enabled": self.enabled,
                "size": len(self._store),
                "max": self.max_entries,
                "ttl_sec": self.ttl_sec,
                "hits": self.hits,
                "misses": self.misses,
            }


# Module-level singleton; tests can call .clear() on it.
plan_cache = PlanCache()


def make_plan_key(
    *,
    backend: str,
    model: str,
    request: str,
    input_path: str,
    output_path: str,
    answers: dict[str, str] | None,
    project_state: dict[str, Any] | None,
) -> str:
    """Derive a cache key from user-controlled planner inputs only.

    Excludes LLM-derived fields (video_context, font_library) so that an
    upstream inspection retry does not invalidate cache entries.
    """
    return _hash_key(
        {
            "backend": backend,
            "model": model,
            "request": request,
            "input_path": input_path,
            "output_path": output_path,
            "answers": dict(sorted((answers or {}).items())),
            "project_state": project_state or {},
        }
    )
