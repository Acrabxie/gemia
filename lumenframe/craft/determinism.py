"""Determinism helpers — the same brief must always produce the same output.

Two ingredients, both shared with :mod:`lumenframe.vector`:

* a single **seeded** ``random.Random`` per build — any randomness flows from
  the brief's explicit ``seed``; module-level randomness is a bug.
* a **thread-local id counter** so node/clip/cut ids are stable within a build
  and safe across concurrent builds (the html-layer content-hash render cache
  only hits if bytes are identical, which requires stable ids).

Plus :func:`stable_digest` for content hashing that never depends on process
state (``hash()`` is salted per process; we use blake2 over canonical JSON).
"""
from __future__ import annotations

import hashlib
import json
import random
import threading
from typing import Any


def new_rng(seed: int) -> random.Random:
    """A fresh deterministic RNG for one build."""
    return random.Random(int(seed))


class IdSeq:
    """A thread-local, resettable id counter.

    Each build calls :meth:`reset` once, then :meth:`next` per element. Distinct
    threads never share counter state, so parallel builds stay deterministic
    without locking.
    """

    def __init__(self, prefix: str = "n") -> None:
        self._prefix = prefix
        self._local = threading.local()

    def reset(self) -> None:
        self._local.n = 0

    def _counter(self) -> int:
        return getattr(self._local, "n", 0)

    def next(self, kind: str | None = None) -> str:
        n = self._counter() + 1
        self._local.n = n
        stem = kind or self._prefix
        return f"{stem}_{n:04d}"


def stable_digest(payload: Any, length: int = 16) -> str:
    """Process-independent short hex digest of any JSON-able payload.

    Used for cache keys and signatures. ``sort_keys`` + fixed separators make
    it canonical; blake2b makes it stable across runs (unlike ``hash``).
    """
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.blake2b(blob.encode("utf-8"), digest_size=length).hexdigest()


def round_floats(obj: Any, ndigits: int = 4) -> Any:
    """Recursively round floats so two structurally-equal builds compare equal
    despite float noise (used in signatures / golden comparisons)."""
    if isinstance(obj, float):
        return round(obj, ndigits)
    if isinstance(obj, dict):
        return {k: round_floats(v, ndigits) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [round_floats(v, ndigits) for v in obj]
    return obj
